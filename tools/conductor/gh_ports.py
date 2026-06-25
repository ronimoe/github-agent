"""gh_ports — real implementations of the release transaction's side-effect ports (issue #5).

`release_txn.run_release` is registry/issue/release-agnostic — it drives any objects exposing
`publish_if_absent`, `close_if_open`/`comment_once`, and (optionally) `create_if_absent`. The P5
sims define the exactly-once contract; these are the real GitHub/registry implementations.

Every port is **idempotent** and maps "already done" to SUCCESS (never a failure), so the
nonce-tag-mutex winner can safely re-run after a crash:
  * publish over an existing version  -> "exists"
  * create an existing GitHub Release -> "exists"
  * close an already-closed issue     -> "already-closed"
  * a duplicate courtesy comment       -> "dup" (marker search)

The command runner is injectable (`run(argv, cwd=None) -> (rc, stdout, stderr)`) so the logic is
unit-tested offline; the default shells out.
"""

from __future__ import annotations

import json
import re
import subprocess


def _run(argv, cwd=None):
    p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


class GitHubIssuePort:
    def __init__(self, repo: str, run=_run):
        self.repo = repo
        self._run = run

    def close_if_open(self, n) -> str:
        rc, out, _ = self._run(["gh", "issue", "view", str(n), "--repo", self.repo, "--json", "state"])
        if rc == 0:
            try:
                if (json.loads(out).get("state") or "").upper() != "OPEN":
                    return "already-closed"
            except json.JSONDecodeError:
                pass
        rc, _, _ = self._run(["gh", "issue", "close", str(n), "--repo", self.repo])
        return "closed" if rc == 0 else "already-closed"

    def comment_once(self, n, marker) -> str:
        rc, out, _ = self._run(["gh", "issue", "view", str(n), "--repo", self.repo, "--json", "comments"])
        if rc == 0:
            try:
                for c in json.loads(out).get("comments", []):
                    if marker in (c.get("body") or ""):
                        return "dup"
            except json.JSONDecodeError:
                pass
        rc, _, _ = self._run(["gh", "issue", "comment", str(n), "--repo", self.repo,
                              "--body", f"Released in {marker}."])
        return "commented" if rc == 0 else "error"


class GitHubReleasePort:
    def __init__(self, repo: str, run=_run):
        self.repo = repo
        self._run = run

    def create_if_absent(self, tag: str, title: str, body: str) -> str:
        rc, _, _ = self._run(["gh", "release", "view", tag, "--repo", self.repo])
        if rc == 0:
            return "exists"
        rc, out, err = self._run(["gh", "release", "create", tag, "--repo", self.repo,
                                  "--title", title, "--notes", body or title])
        if rc == 0:
            return "created"
        if "already exists" in (out + err).lower():
            return "exists"
        raise RuntimeError(f"release create {tag} failed: {err.strip()[:200]}")


class RegistryPort:
    """Generic registry publish with an idempotent already-published match. Presets below."""

    def __init__(self, publish_cmd, already_exists_pattern: str, run=_run, cwd=None):
        self.publish_cmd = list(publish_cmd)
        self.rx = re.compile(already_exists_pattern, re.I)
        self._run = run
        self.cwd = cwd

    def publish_if_absent(self, pkg, version) -> str:
        rc, out, err = self._run(self.publish_cmd, cwd=self.cwd)
        if rc == 0:
            return "published"
        if self.rx.search(out + err):
            return "exists"                              # already published == exactly-once success
        raise RuntimeError(f"publish {pkg}@{version} failed: {(err or out).strip()[:200]}")


def npm_registry(**kw) -> RegistryPort:
    return RegistryPort(["npm", "publish"],
                        r"previously published|EPUBLISHCONFLICT|cannot publish over", **kw)


def cargo_registry(**kw) -> RegistryPort:
    return RegistryPort(["cargo", "publish"],
                        r"already (exists|uploaded)|is already uploaded|crate version .* already", **kw)


def pypi_registry(**kw) -> RegistryPort:
    return RegistryPort(["twine", "upload", "--non-interactive", "dist/*"],
                        r"already exists|file already exists|this filename has already", **kw)
