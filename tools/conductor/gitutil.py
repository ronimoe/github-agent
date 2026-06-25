"""Low-level git plumbing for Conductor's state primitive.

A `WorkRepo` is an ephemeral local git repo with one remote (`origin`). It builds
commits with `git commit-tree` (no checkout) and pushes them with explicit refspecs,
so it exercises the EXACT ref-update semantics Conductor relies on:

  * `ff_push`   — refspec without a leading `+` => FAST-FORWARD-ONLY. The server
                  rejects a non-fast-forward update. This is the durable-state commit
                  primitive (ADR-0003). It works on ANY ref namespace, not just heads.
  * `lease_push`— `--force-with-lease=<ref>:<oid>` => identity assertion. The push is
                  rejected unless the remote ref still equals <oid>. This is the trunk
                  advance primitive.

These semantics are git's own and are implemented identically by GitHub — so a local
bare repo is a faithful test substrate. `remote` can equally be a local path or a
GitHub URL; nothing here is GitHub-specific.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile

# Fixed identity + date => deterministic commit OIDs for the same (tree, parent).
DATE = "2026-06-24T00:00:00+00:00"
_IDENT = {
    "GIT_AUTHOR_NAME": "conductor", "GIT_AUTHOR_EMAIL": "conductor@local",
    "GIT_COMMITTER_NAME": "conductor", "GIT_COMMITTER_EMAIL": "conductor@local",
}


class GitError(Exception):
    pass


def git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _git_minor() -> tuple[int, int]:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
        m = re.search(r"version (\d+)\.(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:
        return (0, 0)


def has_write_tree_merge() -> bool:
    """`git merge-tree --write-tree` (in-memory 3-way merge) landed in git 2.38."""
    return _git_minor() >= (2, 38)


def _git(args, cwd, env=None, check=True, stdin=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, input=stdin, text=True, capture_output=True,
        env={**os.environ, **(env or {})},
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} -> {proc.returncode}: {proc.stderr.strip()}")
    return proc


class WorkRepo:
    """An ephemeral working repo pointed at `remote` (a path or URL)."""

    def __init__(self, remote: str, workdir: str | None = None):
        self.remote = remote
        self.dir = workdir or tempfile.mkdtemp(prefix="conductor-work-")
        self._owns = workdir is None
        _git(["init", "-q"], self.dir)
        _git(["config", "user.name", "conductor"], self.dir)
        _git(["config", "user.email", "conductor@local"], self.dir)
        _git(["remote", "add", "origin", remote], self.dir)

    # --- reads ---------------------------------------------------------------

    def ls_remote(self, ref: str) -> str | None:
        out = _git(["ls-remote", "origin", ref], self.dir).stdout.strip()
        return out.split()[0] if out else None

    def fetch_ref(self, ref: str) -> str | None:
        """Fetch `ref` (always advertised) into a local tracking ref, bringing the tip
        and its history into the local object db, and return the tip OID. None if the
        remote ref does not exist. Fetching the ref — not a raw SHA — is what makes the
        tip's objects available locally for `commit-tree -p`."""
        local = "refs/ctlocal/" + hashlib.sha1(ref.encode()).hexdigest()  # unambiguous (no a/b vs a_b)
        if _git(["fetch", "-q", "origin", f"+{ref}:{local}"], self.dir, check=False).returncode != 0:
            return None
        rp = _git(["rev-parse", local], self.dir, check=False)
        return rp.stdout.strip() if rp.returncode == 0 else None

    def read_blob(self, commit: str, path: str) -> str | None:
        proc = _git(["cat-file", "-p", f"{commit}:{path}"], self.dir, check=False)
        return proc.stdout if proc.returncode == 0 else None

    # --- commit construction (no working tree) -------------------------------

    def write_commit(self, files: dict[str, str], parent: str | None,
                     message: str = "state", date: str = DATE) -> str:
        # Build the tree via a throwaway index so NESTED paths (a/b/c.py) work — `git mktree`
        # only builds flat trees. Deterministic given the same blobs + paths.
        idx = os.path.join(self.dir, ".git", "conductor-index")
        if os.path.exists(idx):
            os.remove(idx)
        env_idx = {"GIT_INDEX_FILE": idx}
        for name, content in sorted(files.items()):
            blob = _git(["hash-object", "-w", "--stdin"], self.dir, stdin=content).stdout.strip()
            _git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"],
                 self.dir, env=env_idx)
        tree = _git(["write-tree"], self.dir, env=env_idx).stdout.strip()
        if os.path.exists(idx):
            os.remove(idx)
        env = {**_IDENT, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
        return _git(args, self.dir, env=env).stdout.strip()

    # --- pushes (the two primitives) -----------------------------------------

    def ff_push(self, ref: str, commit: str) -> tuple[bool, str]:
        """Fast-forward-only push (no leading '+'). Rejected if the remote ref is not
        an ancestor of `commit`."""
        proc = _git(["push", "origin", f"{commit}:{ref}"], self.dir, check=False)
        return proc.returncode == 0, proc.stderr.strip()

    def lease_push(self, ref: str, commit: str, expected: str | None) -> tuple[bool, str]:
        """Identity-checked advance: succeeds only if the remote ref still equals
        `expected`. With expected=None, falls back to fast-forward-only (create/extend)."""
        if expected is None:
            return self.ff_push(ref, commit)
        proc = _git(
            ["push", "origin", f"{commit}:{ref}", f"--force-with-lease={ref}:{expected}"],
            self.dir, check=False,
        )
        return proc.returncode == 0, proc.stderr.strip()

    # --- merge / linear land (the engine's land step) -----------------------

    def merge_tree(self, base: str, other: str) -> tuple[bool, str]:
        """In-memory 3-way merge of `base` and `other` (auto merge-base). Returns
        (True, tree_oid) on a clean merge, (False, conflict_info) on a TEXTUAL conflict.
        Raises GitError on an OPERATIONAL error (bad oid, missing object, git < 2.38) so the
        engine retries rather than mistaking it for a conflict and ejecting a healthy PR.
        Equivalent to the Git DB API's create-tree-via-merge; no working tree touched."""
        proc = _git(["merge-tree", "--write-tree", base, other], self.dir, check=False)
        if proc.returncode == 0:
            lines = proc.stdout.splitlines()
            if not lines or not lines[0].strip():
                raise GitError(f"merge-tree produced no tree for {base}+{other}")
            return True, lines[0].strip()
        if proc.returncode == 1:                      # textual conflict (tree still written)
            return False, proc.stdout.strip()
        raise GitError(f"merge-tree operational error ({proc.returncode}): "
                       f"{(proc.stderr or proc.stdout).strip()}")

    def diff_name_status(self, base: str, head: str) -> list[tuple[str, str]]:
        """(status, path) for every change between `base` and `head` (commit or tree). P4
        runs the verifier on the REALIZED merge tree, not a 2-dot author diff, so the verified
        name-set equals the landed name-set."""
        out = _git(["diff", "--name-status", base, head], self.dir).stdout
        res = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                res.append((parts[0], parts[-1]))
        return res

    def make_annotated_tag(self, name: str, target: str, message: str) -> str:
        """Create a local annotated tag whose object OID embeds `message` (a nonce) — so two
        releasers electing the same (pkg,version) produce DIFFERENT tag OIDs. Returns the OID."""
        env = {**_IDENT, "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE}
        _git(["tag", "-a", "-f", name, "-m", message, target], self.dir, env=env)
        return _git(["rev-parse", f"refs/tags/{name}"], self.dir).stdout.strip()

    def push_tag_create_only(self, name: str) -> str:
        """Push refs/tags/<name> create-only (no force). 'won' iff WE created it; 'lost' if it
        already exists (rejected) OR is byte-identical/up-to-date — the P5 tag-election mutex."""
        proc = _git(["push", "origin", f"refs/tags/{name}"], self.dir, check=False)
        text = proc.stdout + proc.stderr
        return "won" if (proc.returncode == 0 and "[new tag]" in text) else "lost"

    def tree_of(self, commit: str) -> str:
        """The tree OID of a commit — the content key for a spec (P2)."""
        return _git(["rev-parse", f"{commit}^{{tree}}"], self.dir).stdout.strip()

    def commit_onto(self, tree: str, parent: str, message: str,
                    author: dict | None = None, date: str = DATE) -> str:
        """Create a SINGLE-parent commit with `tree` on top of `parent` — a linear
        pushrebase/squash land (NOT a 2-parent merge commit, which `required_linear_history`
        forbids; see ADR-0004). Author identity is preserved from the PR; committer is the bot."""
        env = {**_IDENT, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        if author:
            if author.get("name"):
                env["GIT_AUTHOR_NAME"] = author["name"]
            if author.get("email"):
                env["GIT_AUTHOR_EMAIL"] = author["email"]
        return _git(["commit-tree", tree, "-p", parent, "-m", message],
                    self.dir, env=env).stdout.strip()

    def close(self):
        if self._owns:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
