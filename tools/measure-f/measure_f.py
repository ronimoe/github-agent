#!/usr/bin/env python3
"""measure-f — quantify shared-file contention (f) in a git repo's history.

Conductor's parallelism is bounded by Amdahl's law on the *shared resolved
state* of a repo, not by git: every PR that touches a lockfile / manifest /
generated artifact / CI config is forced through a single serialized lane.

    effective_parallelism  S(N, f) = 1 / (f + (1 - f) / N)
    hard ceiling (N -> inf)         = 1 / f
    global lane saturates beyond  N_max ~= service_rate / arrival_per_agent

So the whole question "will Conductor give us ~8x or ~2x on this repo?" reduces
to one measured number: f, the fraction of PR-equivalent commits that touch
shared state. This tool walks real history and computes it three ways:

  * f_naive   — file-path lanes (status quo): any shared file serializes.
  * f_derived — lockfiles + generated code become engine-owned outputs
                (lockfile-as-derived-artifact, regenerate-not-merge), so only
                manifests + CI config still serialize.
  * f_floor   — also move to manifest-KEY lanes: only CI config and genuine
                same-dependency-key collisions are irreducibly serial.

The gap between f_naive and f_floor is exactly the parallelism Conductor's
mitigations can recover on *your* repo. No third-party dependencies: stdlib +
the `git` CLI only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# --- file classification -----------------------------------------------------
# First match wins. Tunable via --config (JSON) which extends these sets.

LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "bun.lockb", "Cargo.lock", "go.sum", "poetry.lock", "Pipfile.lock",
    "uv.lock", "composer.lock", "Gemfile.lock", "packages.lock.json",
    "pubspec.lock", "flake.lock", "mix.lock",
}

MANIFESTS = {
    "package.json", "Cargo.toml", "go.mod", "pyproject.toml", "Pipfile",
    "setup.py", "setup.cfg", "requirements.txt", "build.gradle",
    "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml",
    "composer.json", "Gemfile", "pubspec.yaml", "mix.exs", "*.csproj",
}

CI_GLOBS = [
    ".github/workflows/", ".github/actions/", ".gitlab-ci.yml", ".circleci/",
    "azure-pipelines.yml", "Jenkinsfile", ".buildkite/", ".drone.yml",
    "bitbucket-pipelines.yml", ".conductor/", "turbo.json", "nx.json",
]

# Generated-artifact heuristics (path substrings / suffixes). Marked HEURISTIC
# in output because no rule is perfect — tune per repo with --config.
GENERATED_SUFFIXES = (
    ".pb.go", ".pb.cc", ".pb.h", "_pb2.py", "_pb2.pyi", "_pb2_grpc.py",
    ".gen.go", ".gen.ts", ".gen.js", ".generated.ts", ".generated.go",
    ".g.dart", "_generated.dart", ".designer.cs",
)
GENERATED_SUBSTRINGS = (
    "/generated/", "/__generated__/", "/gen/", "/.gen/", "prisma/client",
    "graphql/generated", "openapi/generated", "/mocks/generated",
)

MANIFEST_BASENAMES_FOR_DEPS = {"package.json", "Cargo.toml", "go.mod", "pyproject.toml"}

# Heuristic: a package.json value that looks like a dependency range.
NPM_RANGE_RE = re.compile(
    r'^\s*[~^>=<]?\s*(?:\d|\*|x|v\d|latest$|workspace:|npm:|file:|link:|catalog:|portal:)',
    re.IGNORECASE,
)
NPM_LINE_RE = re.compile(r'^[+-]\s*"([^"]+)"\s*:\s*"([^"]*)"\s*,?\s*$')
CARGO_TABLE_RE = re.compile(r'^[+-]?\s*\[([^\]]+)\]\s*$')
CARGO_DEP_RE = re.compile(r'^[+-]\s*([A-Za-z0-9_.-]+)\s*=\s*(.+)$')
GOMOD_REQUIRE_RE = re.compile(r'^[+-]\s*([\w.\-/]+\.[\w.\-/]+)\s+v\d[\w.\-+]*')


@dataclass
class Commit:
    sha: str
    date: str
    author: str
    files: list[tuple[str, str]] = field(default_factory=list)  # (status, path)


def run_git(repo: str, args: list[str]) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, *args],
            check=True, capture_output=True, text=True, errors="replace",
        )
    except FileNotFoundError:
        sys.exit("error: `git` not found on PATH.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: git {' '.join(args)} failed:\n{e.stderr.strip()}")
    return out.stdout


def ensure_repo(repo: str) -> None:
    inside = run_git(repo, ["rev-parse", "--is-inside-work-tree"]).strip()
    if inside != "true":
        sys.exit(f"error: {repo!r} is not a git working tree.")


# --- history collection ------------------------------------------------------

REC = "\x1e"   # record separator
FLD = "\x1f"   # field separator


def collect_commits(repo: str, ref: str, max_commits: int, since: str | None,
                    no_merges_diff: bool) -> list[Commit]:
    fmt = f"{REC}%H{FLD}%aI{FLD}%an"
    args = ["log", ref, "--first-parent", "--name-status", "-M",
            f"--format={fmt}"]
    if not no_merges_diff:
        # Show each merge's diff against its first parent (the "PR" diff).
        args.insert(3, "--diff-merges=first-parent")
    if max_commits > 0:
        args.append(f"-n{max_commits}")
    if since:
        args.append(f"--since={since}")
    raw = run_git(repo, args)

    commits: list[Commit] = []
    for chunk in raw.split(REC):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, body = chunk.partition("\n")
        parts = header.split(FLD)
        if len(parts) < 3:
            continue
        c = Commit(sha=parts[0], date=parts[1], author=parts[2])
        for line in body.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            status = cols[0]
            # Renames/copies: R100\told\tnew  -> take the new path.
            path = cols[-1]
            c.files.append((status, path))
        commits.append(c)
    return commits


# --- classification ----------------------------------------------------------

def classify(path: str, cfg: dict) -> str:
    base = os.path.basename(path)
    norm = "/" + path  # leading slash so substring rules like "/gen/" match root dirs

    if base in cfg["lockfiles"]:
        return "lockfile"
    # CI is checked before manifest so workflow YAMLs etc. win.
    for g in cfg["ci_globs"]:
        if g.endswith("/"):
            if ("/" + path).find("/" + g) != -1 or path.startswith(g):
                return "ci"
        elif base == g or path == g or path.endswith("/" + g):
            return "ci"
    if base in cfg["manifests"] or any(
        base.endswith(m[1:]) for m in cfg["manifests"] if m.startswith("*")
    ):
        return "manifest"
    if path.endswith(cfg["generated_suffixes"]) or any(
        s in norm for s in cfg["generated_substrings"]
    ):
        return "generated"
    return "source"


def load_config(path: str | None) -> dict:
    cfg = {
        "lockfiles": set(LOCKFILES),
        "manifests": set(MANIFESTS),
        "ci_globs": list(CI_GLOBS),
        "generated_suffixes": tuple(GENERATED_SUFFIXES),
        "generated_substrings": tuple(GENERATED_SUBSTRINGS),
    }
    if not path:
        return cfg
    with open(path) as fh:
        user = json.load(fh)
    cfg["lockfiles"] |= set(user.get("lockfiles", []))
    cfg["manifests"] |= set(user.get("manifests", []))
    cfg["ci_globs"] += list(user.get("ci_globs", []))
    cfg["generated_suffixes"] += tuple(user.get("generated_suffixes", []))
    cfg["generated_substrings"] += tuple(user.get("generated_substrings", []))
    return cfg


# --- dependency-key extraction (heuristic, npm/cargo/go) ---------------------

def extract_dep_changes(repo: str, ref: str, manifest_paths: list[str],
                        max_commits: int, since: str | None,
                        no_merges_diff: bool) -> dict[str, set[str]]:
    """Map commit SHA -> set of changed dependency keys, parsed from manifest
    patches. Best-effort and clearly approximate; only npm/cargo/go are parsed.
    """
    if not manifest_paths:
        return {}
    fmt = f"{REC}%H"
    args = ["log", ref, "--first-parent", "-M", "-p", f"--format={fmt}"]
    if not no_merges_diff:
        args.insert(3, "--diff-merges=first-parent")
    if max_commits > 0:
        args.append(f"-n{max_commits}")
    if since:
        args.append(f"--since={since}")
    args.append("--")
    args.extend(sorted(set(manifest_paths)))
    raw = run_git(repo, args)

    out: dict[str, set[str]] = {}
    for chunk in raw.split(REC):
        if not chunk.strip():
            continue
        sha, _, patch = chunk.partition("\n")
        sha = sha.strip()
        keys = _parse_patch_dep_keys(patch)
        if keys:
            out[sha] = keys
    return out


def _parse_patch_dep_keys(patch: str) -> set[str]:
    keys: set[str] = set()
    cur_base = ""
    cargo_table = ""
    for line in patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            cur_base = os.path.basename(line[6:].strip())
            cargo_table = ""
            continue
        if line.startswith("diff --git"):
            cur_base = ""
            cargo_table = ""
            continue
        if not line or line[0] not in "+-" or line.startswith(("+++", "---")):
            # context line — still track cargo table headers for state.
            m = CARGO_TABLE_RE.match(line)
            if m and cur_base == "Cargo.toml":
                cargo_table = m.group(1).lower()
            continue

        if cur_base == "package.json":
            m = NPM_LINE_RE.match(line)
            if m and NPM_RANGE_RE.match(m.group(2) or ""):
                keys.add("npm:" + m.group(1))
        elif cur_base == "Cargo.toml":
            t = CARGO_TABLE_RE.match(line)
            if t:
                cargo_table = t.group(1).lower()
                continue
            if "dependencies" in cargo_table:
                m = CARGO_DEP_RE.match(line)
                if m and m.group(1).lower() not in ("version", "features", "default-features", "optional", "path", "git"):
                    keys.add("cargo:" + m.group(1))
        elif cur_base == "go.mod":
            m = GOMOD_REQUIRE_RE.match(line)
            if m:
                keys.add("go:" + m.group(1))
        elif cur_base == "pyproject.toml":
            m = CARGO_DEP_RE.match(line)  # same key = "value" shape
            if m and re.search(r'\d', m.group(2)):
                keys.add("py:" + m.group(1))
    return keys


# --- metrics -----------------------------------------------------------------

def amdahl(n: float, f: float) -> float:
    if f <= 0:
        return float(n)
    return 1.0 / (f + (1.0 - f) / n)


@dataclass
class Result:
    n: int
    date_first: str
    date_last: str
    class_counts: dict          # class -> # commits touching it
    f_naive: float
    f_derived: float
    f_floor: float
    dep_change_rate: float
    same_key_pair_rate: float
    top_keys: list
    samples: dict               # class -> example paths


def analyze(commits: list[Commit], cfg: dict,
            dep_changes: dict[str, set[str]]) -> Result:
    n = len(commits)
    touches = defaultdict(int)            # class -> commits touching >=1 of it
    samples = defaultdict(set)
    n_manifest_or_ci = 0
    n_any_shared = 0
    n_ci = 0

    for c in commits:
        classes = set()
        for status, path in c.files:
            cls = classify(path, cfg)
            classes.add(cls)
            if len(samples[cls]) < 5:
                samples[cls].add(path)
        for cls in classes:
            touches[cls] += 1
        shared = classes & {"lockfile", "manifest", "generated", "ci"}
        if shared:
            n_any_shared += 1
        if classes & {"manifest", "ci"}:
            n_manifest_or_ci += 1
        if "ci" in classes:
            n_ci += 1

    f_naive = n_any_shared / n if n else 0.0
    f_derived = n_manifest_or_ci / n if n else 0.0
    f_ci = n_ci / n if n else 0.0

    # Dependency-key collision estimate.
    dep_commits = [s for s in (c.sha for c in commits) if s in dep_changes]
    d = len(dep_commits)
    dep_change_rate = d / n if n else 0.0
    key_counts = Counter()
    for s in dep_commits:
        for k in dep_changes[s]:
            key_counts[k] += 1
    # P(two random dep-changing commits share >=1 key), upper-bound estimate
    # via sum_k C(count_k, 2) / C(D, 2).
    if d >= 2:
        colliding_pairs = sum(cc * (cc - 1) / 2 for cc in key_counts.values())
        total_pairs = d * (d - 1) / 2
        same_key_pair_rate = min(1.0, colliding_pairs / total_pairs)
    else:
        same_key_pair_rate = 0.0

    # Floor: only CI config + genuine same-key manifest collisions serialize.
    f_floor = min(f_derived, f_ci + dep_change_rate * same_key_pair_rate)

    return Result(
        n=n,
        date_first=commits[-1].date if commits else "",
        date_last=commits[0].date if commits else "",
        class_counts=dict(touches),
        f_naive=f_naive,
        f_derived=f_derived,
        f_floor=f_floor,
        dep_change_rate=dep_change_rate,
        same_key_pair_rate=same_key_pair_rate,
        top_keys=key_counts.most_common(12),
        samples={k: sorted(v) for k, v in samples.items()},
    )


# --- reporting ---------------------------------------------------------------

def pct(x: float) -> str:
    return f"{100 * x:5.1f}%"


def verdict_for(f: float) -> str:
    if f <= 0.08:
        return "GOOD — recovers strong parallelism (~8-12x at N=50)"
    if f <= 0.20:
        return "MARGINAL — meaningful but capped recovery (~3-6x)"
    return "COLLAPSE — serialized; adding agents past ~10 buys little (<3x)"


def render(res: Result, ref: str, tv_min: float, pr_per_agent_hr: float) -> str:
    L = []
    w = L.append
    w("=" * 72)
    w("  measure-f — shared-file contention report")
    w("=" * 72)
    w(f"  ref analyzed     : {ref} (first-parent)")
    w(f"  PR-equiv commits : {res.n}")
    w(f"  date span        : {res.date_first[:10]} .. {res.date_last[:10]}")
    w("")
    w("  Per-class touch rate (% of PR-equiv commits touching >=1 such file)")
    w("  " + "-" * 60)
    order = [("source", "source code"), ("manifest", "manifests"),
             ("lockfile", "lockfiles"), ("generated", "generated (heuristic)"),
             ("ci", "CI / build config")]
    for key, label in order:
        cnt = res.class_counts.get(key, 0)
        rate = cnt / res.n if res.n else 0
        w(f"    {label:<24} {pct(rate)}   ({cnt})")
    w("")
    w("  Contention fraction f  (lower = more parallelism)")
    w("  " + "-" * 60)
    rows = [
        ("f_naive   (file-path lanes, status quo)", res.f_naive),
        ("f_derived (lockfile+generated engine-owned)", res.f_derived),
        ("f_floor   (+ manifest-key lanes)", res.f_floor),
    ]
    w(f"    {'regime':<44}{'f':>7}{'ceiling':>9}")
    for label, f in rows:
        ceil = (1 / f) if f > 0 else float("inf")
        ceil_s = "inf" if math.isinf(ceil) else f"{ceil:5.1f}x"
        w(f"    {label:<44}{f:>7.3f}{ceil_s:>9}")
    w("")
    w("  Effective parallelism S(N, f) = 1 / (f + (1-f)/N)")
    w("  " + "-" * 60)
    Ns = [10, 20, 50]
    w(f"    {'regime':<28}" + "".join(f"N={n:<7}" for n in Ns))
    for label, f in [("f_naive", res.f_naive), ("f_derived", res.f_derived),
                     ("f_floor", res.f_floor)]:
        cells = "".join(f"{amdahl(n, f):>5.1f}x  " for n in Ns)
        w(f"    {label:<28}{cells}")
    w("")
    # Saturation: global lane is M/M/1. service rate mu = 60/tv per hr,
    # arrival of shared-state PRs lambda = f * pr_per_agent_hr * N.
    # rho >= 1  <=>  N >= mu / (f * pr_per_agent_hr).
    mu = 60.0 / tv_min if tv_min > 0 else float("inf")
    w("  Global-lane saturation (unbounded queue beyond N_max)")
    w("  " + "-" * 60)
    w(f"    assumptions: validation T_v={tv_min:g} min  ->  mu={mu:.1f} PR/hr;")
    w(f"                 {pr_per_agent_hr:g} PR/agent/hr arrival")
    for label, f in [("f_naive", res.f_naive), ("f_floor", res.f_floor)]:
        if f > 0:
            n_max = mu / (f * pr_per_agent_hr)
            w(f"    {label:<10} N_max ~= {n_max:5.1f} agents before the lane saturates")
        else:
            w(f"    {label:<10} N_max ~= inf (no shared-state contention)")
    w("")
    w("  Dependency-key analysis (npm/cargo/go, heuristic)")
    w("  " + "-" * 60)
    w(f"    PRs that change a dep key : {pct(res.dep_change_rate)}")
    w(f"    same-key collision rate   : {pct(res.same_key_pair_rate)}"
      "   (P two dep-PRs share a key)")
    if res.top_keys:
        w("    hottest dependency keys (most-changed -> arbitration risk):")
        for k, c in res.top_keys[:8]:
            w(f"      {c:>4}x  {k}")
    w("")
    w("=" * 72)
    w(f"  VERDICT (using f_floor = {res.f_floor:.3f}):")
    w(f"    {verdict_for(res.f_floor)}")
    w("")
    w("  Read this as: Conductor's mitigations move you from f_naive to f_floor.")
    w("  The recovered parallelism is real but bounded by 1/f_floor — it is NOT")
    w("  linear in agent count. f_floor is dominated by CI-config churn + genuine")
    w("  same-dependency-key conflicts, which no merge engine can parallelize.")
    w("=" * 72)
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Measure shared-file contention (f) in a git repo to predict "
                    "Conductor's parallelism ceiling.")
    ap.add_argument("repo", nargs="?", default=".", help="path to the git repo (default: .)")
    ap.add_argument("--ref", default="HEAD", help="branch/ref to analyze (default: HEAD)")
    ap.add_argument("--max-commits", type=int, default=2000,
                    help="cap PR-equiv commits analyzed; 0 = all (default: 2000)")
    ap.add_argument("--since", default=None, help='limit history, e.g. "12 months ago"')
    ap.add_argument("--config", default=None, help="JSON file extending classification rules")
    ap.add_argument("--tv-min", type=float, default=10.0,
                    help="mean spec-validation time in minutes (default: 10)")
    ap.add_argument("--pr-per-agent-hr", type=float, default=2.0,
                    help="PRs produced per agent per hour (default: 2)")
    ap.add_argument("--no-merge-diffs", action="store_true",
                    help="treat merge commits as empty (repos with no squash/rebase)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    ensure_repo(repo)
    cfg = load_config(args.config)

    commits = collect_commits(repo, args.ref, args.max_commits, args.since,
                              args.no_merge_diffs)
    if not commits:
        sys.exit("error: no commits found for the given ref/limits.")

    manifest_paths = [
        p for c in commits for _, p in c.files
        if os.path.basename(p) in MANIFEST_BASENAMES_FOR_DEPS
    ]
    dep_changes = extract_dep_changes(repo, args.ref, manifest_paths,
                                      args.max_commits, args.since,
                                      args.no_merge_diffs)
    res = analyze(commits, cfg, dep_changes)

    if args.json:
        print(json.dumps({
            "ref": args.ref,
            "commits_analyzed": res.n,
            "date_first": res.date_first,
            "date_last": res.date_last,
            "class_touch_counts": res.class_counts,
            "f_naive": round(res.f_naive, 4),
            "f_derived": round(res.f_derived, 4),
            "f_floor": round(res.f_floor, 4),
            "ceiling_naive": round(1 / res.f_naive, 2) if res.f_naive else None,
            "ceiling_floor": round(1 / res.f_floor, 2) if res.f_floor else None,
            "dep_change_rate": round(res.dep_change_rate, 4),
            "same_key_pair_rate": round(res.same_key_pair_rate, 4),
            "top_keys": res.top_keys,
            "verdict": verdict_for(res.f_floor),
        }, indent=2))
    else:
        print(render(res, args.ref, args.tv_min, args.pr_per_agent_hr))


if __name__ == "__main__":
    main()
