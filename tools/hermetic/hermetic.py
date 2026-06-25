#!/usr/bin/env python3
"""hermetic — a hermetic resolution gate for Conductor's speculative merge engine.

WHY THIS EXISTS
---------------
The engine regenerates lockfiles (package-lock.json / Cargo.lock / go.sum / ...)
inside each speculative spec and treats the landed lockfile as a *deterministic
function of the manifest prefix*. That determinism holds ONLY under a hermetic
resolver. If a patch version is published mid-pipeline, a registry record mutates,
the resolver/package-manager version drifts, or the host platform differs, the
regenerated lockfile can change — so a spec passes green yet lands a DIFFERENT
closure on trunk, silently breaking the linear-history-only guarantee.

This module enforces a hermetic resolution gate:

  1. PIN + RECORD the exact resolver version AND host-runtime version per ecosystem.
  2. FREEZE the registry for the whole merge cycle (immutable mirror snapshot
     preferred; absolute time-cutoff where the resolver supports it). RELATIVE
     cutoffs (npm --min-release-age, ISO-8601 durations, poetry min-release-age) are
     REJECTED — they drift between specs resolved at different instants.
  3. RECORD platform/arch and assert the emitted lockfile FORMAT version.
  4. ASSERT byte-identical regeneration for the same prefix before a green verdict —
     FAIL CLOSED on any mismatch or missing precondition.
  5. PROBE: a CI check re-resolves a known prefix twice (clean-room) and diffs.

The freeze facts per ecosystem were verified by the `hermetic-resolution-facts`
research pass; key residuals are documented per adapter. No third-party
dependencies — Python 3.11+ stdlib only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict

SEMVER_RE = re.compile(r"(\d+\.\d+\.\d+)")
# ISO-8601 duration (P7D, P3DT4H, ...) — a RELATIVE cutoff, never a hermetic freeze.
ISO_DURATION_RE = re.compile(r"^P(?=\d|T)\d*[YMWD]?", re.IGNORECASE)
IGNORE_COPY = shutil.ignore_patterns(
    ".git", "node_modules", "target", "vendor", ".hermetic", "__pycache__",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_absolute_timestamp(value: str) -> bool:
    """True for YYYY-MM-DD or RFC3339/ISO datetimes; False for relative durations
    (P7D) or anything non-absolute."""
    if not value or ISO_DURATION_RE.match(value):
        return False
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}|$)", value))


# --- resolution epoch --------------------------------------------------------

@dataclass
class ResolutionEpoch:
    """The frozen resolution environment for one merge cycle. Committed so the
    cycle is reproducible. Keyed by ecosystem id."""
    epoch_id: str
    created_at: str = ""
    platform: dict = field(default_factory=dict)
    resolvers: dict = field(default_factory=dict)          # eco -> {tool, version, runtimes:{..}}
    registry_snapshot: dict = field(default_factory=dict)  # eco -> {snapshot_id, url, before, ...}
    formats: dict = field(default_factory=dict)            # eco -> expected lockfile format version

    def get(self, dotted: str):
        node = asdict(self)
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> "ResolutionEpoch":
        try:
            with open(path) as fh:
                return cls(**json.load(fh))
        except (OSError, json.JSONDecodeError, TypeError) as e:
            raise HermeticError(f"cannot load epoch {path}: {e}")  # fail closed, not a traceback


def current_platform() -> dict:
    return {
        "os": _platform.system().lower(),
        "arch": _platform.machine().lower(),
        "libc": (_platform.libc_ver()[0] or "none"),
    }


# --- adapter interface -------------------------------------------------------

class HermeticError(Exception):
    """Any condition that must fail the gate closed."""


class Adapter:
    ecosystem: str = ""
    lockfile_name: str = ""
    manifest_names: tuple = ()
    version_cmd: tuple = ()
    runtime_cmds: dict = {}                 # name -> argv (secondary runtimes, e.g. node/rustc)
    required_epoch_paths: tuple = ()        # must be present + non-null in epoch
    time_freeze_field: str | None = None    # dotted epoch path holding an ABSOLUTE cutoff
    assert_cmd: str = ""                     # native verify-pass (shown by `plan`, not executed offline)
    platform_sensitive: bool = False
    residuals: tuple = ()                    # documented irreducible drift sources

    def detect(self, workdir: str) -> bool:
        return any(os.path.exists(os.path.join(workdir, m)) for m in self.manifest_names)

    def _read_semver(self, cmd) -> str | None:
        try:
            out = subprocess.run(list(cmd), capture_output=True, text=True, check=True).stdout
        except (FileNotFoundError, subprocess.CalledProcessError):
            return None
        m = SEMVER_RE.search(out)
        return m.group(1) if m else None

    def installed_version(self) -> str | None:
        return self._read_semver(self.version_cmd) if self.version_cmd else None

    def runtimes(self) -> dict:
        return {name: self._read_semver(cmd) for name, cmd in self.runtime_cmds.items()}

    def build_invocation(self, epoch: ResolutionEpoch, workdir: str):
        """Return (argv, env) for a *lock-only* deterministic regeneration."""
        raise NotImplementedError

    def format_version(self, data: bytes) -> str | None:
        """Extract the lockfile's own format version from its bytes (asserted as a
        first-class gate field — catches a stealth resolver-major swap)."""
        return None

    def normalize(self, data: bytes) -> bytes:
        """Strip only documented volatile content before byte-comparison."""
        return data

    def regenerate(self, workdir: str, epoch: ResolutionEpoch) -> bytes:
        """Clean-room regenerate: strip the old lockfile, run the frozen invocation,
        read it back. Clean-room is mandatory — a pre-existing lock biases output and
        masks drift."""
        argv, env = self.build_invocation(epoch, workdir)
        lock = os.path.join(workdir, self.lockfile_name)
        if os.path.exists(lock):
            os.remove(lock)
        try:
            proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True)
        except FileNotFoundError:
            raise HermeticError(f"{self.ecosystem}: resolver '{argv[0]}' not installed")
        if proc.returncode != 0:
            raise HermeticError(
                f"{self.ecosystem}: regen exit {proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[:400]}")
        if not os.path.exists(lock):
            raise HermeticError(f"{self.ecosystem}: lockfile {self.lockfile_name} not produced")
        with open(lock, "rb") as fh:
            return fh.read()


def _isolated_env(overrides: dict) -> dict:
    env = dict(os.environ)
    # Neutralize ambient config leakage that silently changes resolution.
    env.update({"LC_ALL": "C", "COREPACK_ENABLE_AUTO_PIN": "0"})
    env.update(overrides)
    return env


# --- real adapters (freeze facts verified by hermetic-resolution-facts) ------

class NpmAdapter(Adapter):
    ecosystem = "npm"
    lockfile_name = "package-lock.json"
    manifest_names = ("package.json",)
    version_cmd = ("npm", "--version")
    runtime_cmds = {"node": ("node", "--version")}
    required_epoch_paths = (
        "resolvers.npm.version",
        "registry_snapshot.npm.before",
        "registry_snapshot.npm.registry",
    )
    time_freeze_field = "registry_snapshot.npm.before"  # ABSOLUTE only; --min-release-age rejected
    assert_cmd = "npm install --frozen-lockfile (against the produced lock)"
    platform_sensitive = False  # conditional: depends on optional-dep metadata completeness
    residuals = (
        "--before pins version selection, not served bytes: dist-tag moves, yanks, "
        "metadata (license/engines/deprecated), and integrity re-serves still drift — "
        "only a truly immutable mirror snapshot defeats these.",
        "the `resolved` field bakes the registry-host URL into lock bytes (pin one "
        "canonical --registry or set omit-lockfile-registry-resolved consistently).",
    )

    def build_invocation(self, epoch, workdir):
        before = epoch.get("registry_snapshot.npm.before")
        registry = epoch.get("registry_snapshot.npm.registry")
        cache = os.path.join(workdir, ".hermetic", "npm-cache")
        argv = [
            "npm", "install", "--package-lock-only",
            "--ignore-scripts", "--no-audit", "--no-fund",
            f"--before={before}", "--registry", registry, "--cache", cache,
        ]
        if epoch.get("registry_snapshot.npm.omit_resolved"):
            argv.append("--omit-lockfile-registry-resolved=true")
        return argv, _isolated_env({"npm_config_cache": cache, "HOME": workdir})

    def format_version(self, data):
        try:
            return str(json.loads(data).get("lockfileVersion"))
        except (ValueError, AttributeError):
            return None


class PnpmAdapter(Adapter):
    ecosystem = "pnpm"
    lockfile_name = "pnpm-lock.yaml"
    manifest_names = ("package.json", "pnpm-workspace.yaml")
    version_cmd = ("pnpm", "--version")
    runtime_cmds = {"node": ("node", "--version")}
    # No --before: registry MUST be an immutable mirror snapshot. minimumReleaseAge /
    # resolutionMode time-based are RELATIVE and rejected as a freeze anchor.
    required_epoch_paths = ("resolvers.pnpm.version", "registry_snapshot.pnpm.registry")
    assert_cmd = "pnpm install --frozen-lockfile"
    platform_sensitive = True  # 11.6+ name-based platform inference can change recorded variants
    residuals = (
        "use `pnpm install --lockfile-only` — NEVER `pnpm update --lockfile-only` "
        "(re-resolves the whole lockfile since v10.9.0) or --force.",
        "settings span both pnpm-workspace.yaml and .npmrc; a lockfileVersion bump "
        "between pnpm majors reserializes the file.",
    )

    def build_invocation(self, epoch, workdir):
        registry = epoch.get("registry_snapshot.pnpm.registry")
        store = os.path.join(workdir, ".hermetic", "pnpm-store")
        argv = ["pnpm", "install", "--lockfile-only", "--ignore-scripts",
                "--registry", registry, "--store-dir", store]
        return argv, _isolated_env({"HOME": workdir})

    def format_version(self, data):
        m = re.search(rb"(?m)^lockfileVersion:\s*['\"]?([\d.]+)", data)
        return m.group(1).decode() if m else None


class YarnAdapter(Adapter):
    ecosystem = "yarn-berry"
    lockfile_name = "yarn.lock"
    manifest_names = ("package.json", ".yarnrc.yml")
    version_cmd = ("yarn", "--version")     # effective runtime is source of truth (yarnPath WINS over packageManager)
    runtime_cmds = {"node": ("node", "--version")}
    required_epoch_paths = ("resolvers.yarn-berry.version", "registry_snapshot.yarn-berry.registry")
    assert_cmd = "yarn install --immutable (against the produced lock)"
    platform_sensitive = True  # default skips non-host OS/CPU pkgs unless supportedArchitectures=all
    residuals = (
        "yarnPath takes precedence over packageManager (packageManager is IGNORED when "
        "yarnPath is present) — assert `yarn --version` == both, or fail closed.",
        "set supportedArchitectures=all for a host-independent lock; __metadata.version/"
        "cacheKey bump across majors reserializes.",
    )

    def build_invocation(self, epoch, workdir):
        registry = epoch.get("registry_snapshot.yarn-berry.registry")
        argv = ["yarn", "install", "--mode=update-lockfile"]
        return argv, _isolated_env({
            "HOME": workdir, "YARN_NPM_REGISTRY_SERVER": registry,
            "YARN_ENABLE_IMMUTABLE_INSTALLS": "false", "YARN_CHECKSUM_BEHAVIOR": "throw",
        })

    def format_version(self, data):
        m = re.search(rb"__metadata:\s*\n\s*version:\s*(\d+)", data)
        return m.group(1).decode() if m else None


class CargoAdapter(Adapter):
    ecosystem = "cargo"
    lockfile_name = "Cargo.lock"
    manifest_names = ("Cargo.toml",)
    version_cmd = ("cargo", "--version")
    runtime_cmds = {"rustc": ("rustc", "--version")}
    # No --before: freeze via a pinned git-index commit / `cargo vendor`. A live-proxying
    # sparse mirror is NOT frozen (cannot pin yanked-status).
    required_epoch_paths = ("resolvers.cargo.version", "registry_snapshot.cargo.index_commit")
    assert_cmd = "cargo generate-lockfile --locked  (fail closed if the lock would change)"
    platform_sensitive = False  # resolver ignores [target]/cfg — genuinely platform-independent
    residuals = (
        "resolver v3 / resolver.incompatible-rust-version (default-on at cargo>=1.84, "
        "edition-2024) changes LOCK CONTENTS — pin and gate it.",
        "format-header drift (fresh v4 write vs seeded v3 preserve) is not fixed by a "
        "toolchain pin alone; commit a seed Cargo.lock with the intended `version = N`.",
    )

    def build_invocation(self, epoch, workdir):
        cargo_home = os.path.join(workdir, ".hermetic", "cargo-home")
        argv = ["cargo", "generate-lockfile", "--frozen"]
        return argv, _isolated_env({"CARGO_HOME": cargo_home, "CARGO_NET_OFFLINE": "true"})

    def format_version(self, data):
        m = re.search(rb"(?m)^version = (\d+)\s*$", data)
        return m.group(1).decode() if m else None


class GoAdapter(Adapter):
    ecosystem = "go"
    lockfile_name = "go.sum"   # go.mod is diffed too (see regenerate)
    manifest_names = ("go.mod",)
    version_cmd = ("go", "env", "GOVERSION")
    # GOTOOLCHAIN=local is the PRIMARY gate condition (default `auto` silently switches
    # the toolchain, changing resolver + format). GOPROXY must be a frozen snapshot with
    # no ,direct suffix and not `off`. (GOFLAGS=-mod=mod is NOT required for tidy;
    # GONOSUMCHECK is not a real Go variable — neither is used here.)
    required_epoch_paths = ("resolvers.go.version", "registry_snapshot.go.goproxy")
    assert_cmd = "go mod verify; diff `go mod edit -json` Go/Toolchain before/after"
    platform_sensitive = False  # MVS ignores build constraints — go.sum is platform-independent
    residuals = (
        "toolchain-version is the DOMINANT drift axis: same go.mod + different Go minor "
        "=> different go.sum and go.mod formatting. Pin GOTOOLCHAIN=local + exact toolchain.",
        "tidy can rewrite the `go`/`toolchain` directives; diff BOTH go.sum and go.mod.",
    )

    def build_invocation(self, epoch, workdir):
        goproxy = epoch.get("registry_snapshot.go.goproxy")
        env = {
            "GOTOOLCHAIN": "local", "GOPROXY": goproxy, "GOSUMDB": "off",
            "GOMODCACHE": os.path.join(workdir, ".hermetic", "gomodcache"),
            "GOPATH": os.path.join(workdir, ".hermetic", "gopath"),
        }
        return ["go", "mod", "tidy"], _isolated_env(env)

    def regenerate(self, workdir, epoch):
        # Go has no single resolved lock; the determinism surface is go.sum AND go.mod.
        argv, env = self.build_invocation(epoch, workdir)
        try:
            proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True)
        except FileNotFoundError:
            raise HermeticError("go: resolver 'go' not installed")
        if proc.returncode != 0:
            raise HermeticError(f"go: tidy exit {proc.returncode}: "
                                f"{(proc.stderr or proc.stdout).strip()[:400]}")
        out = b""
        for name in ("go.mod", "go.sum"):
            p = os.path.join(workdir, name)
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    data = fh.read()
            else:
                data = b""
            out += f"== {name} ==\n".encode() + data + b"\n"
        return out


class UvAdapter(Adapter):
    ecosystem = "python-uv"
    lockfile_name = "uv.lock"
    manifest_names = ("pyproject.toml", "uv.lock")
    version_cmd = ("uv", "--version")
    runtime_cmds = {"python": ("python3", "--version")}
    required_epoch_paths = ("resolvers.python-uv.version", "registry_snapshot.python-uv.exclude_newer")
    time_freeze_field = "registry_snapshot.python-uv.exclude_newer"  # ABSOLUTE RFC3339/date
    assert_cmd = "uv lock --check  (fail closed if the lock would change)"
    platform_sensitive = True  # universality holds only if fork-strategy/environments/requires-python frozen
    residuals = (
        "assert BOTH uv.lock `version` (schema) and `revision` (serialization).",
        "an index lacking PEP 700 upload-time makes uv treat artifacts as unavailable "
        "(resolution can FAIL, not just drift); prefer an immutable mirror snapshot.",
    )

    def build_invocation(self, epoch, workdir):
        cutoff = epoch.get("registry_snapshot.python-uv.exclude_newer")
        argv = ["uv", "lock", "--exclude-newer", cutoff]
        return argv, _isolated_env({"UV_NO_CONFIG": "1", "HOME": workdir})

    def format_version(self, data):
        v = re.search(rb"(?m)^version = (\d+)", data)
        r = re.search(rb"(?m)^revision = (\d+)", data)
        if not v:
            return None
        return f"v{v.group(1).decode()}" + (f".r{r.group(1).decode()}" if r else "")


class PoetryAdapter(Adapter):
    ecosystem = "python-poetry"
    lockfile_name = "poetry.lock"
    manifest_names = ("pyproject.toml", "poetry.lock")
    version_cmd = ("poetry", "--version")
    runtime_cmds = {"python": ("python3", "--version")}
    # Poetry's solver.min-release-age is RELATIVE and FAILS OPEN — never a hermetic freeze.
    # Quarantine to a serialized lane backed by an immutable mirror snapshot.
    required_epoch_paths = ("resolvers.python-poetry.version", "registry_snapshot.python-poetry.snapshot_id")
    assert_cmd = "poetry lock --check"
    platform_sensitive = True
    residuals = (
        "min-release-age fails OPEN on missing upload time and is relative — quarantine to "
        "a serial lane; treat as only CONDITIONALLY byte-identical.",
    )

    def build_invocation(self, epoch, workdir):
        return ["poetry", "lock"], _isolated_env({"HOME": workdir,
                                                   "POETRY_VIRTUALENVS_CREATE": "false"})


class PipToolsAdapter(Adapter):
    ecosystem = "python-pip-tools"
    lockfile_name = "requirements.txt"
    manifest_names = ("requirements.in",)
    version_cmd = ("pip-compile", "--version")
    runtime_cmds = {"python": ("python3", "--version")}
    required_epoch_paths = ("resolvers.python-pip-tools.version",
                            "registry_snapshot.python-pip-tools.index_url")
    assert_cmd = "pip-compile ... and byte-diff (header stripped)"
    platform_sensitive = True  # output is platform + interpreter-specific BY CONSTRUCTION
    residuals = (
        "requirements.txt is per (os, arch, cpython-patch) by construction — regen on the "
        "identical target tuple or fail closed.",
        "strip the volatile command-string header (--no-header) before byte-diff.",
    )

    def build_invocation(self, epoch, workdir):
        index = epoch.get("registry_snapshot.python-pip-tools.index_url")
        argv = ["pip-compile", "--generate-hashes", "--no-emit-index-url", "--no-header",
                "--annotation-style=line", "--index-url", index,
                "--output-file", "requirements.txt", "requirements.in"]
        return argv, _isolated_env({"HOME": workdir, "PIP_CONFIG_FILE": os.devnull})

    def normalize(self, data):
        # Drop the leading volatile comment/header block; keep resolution body.
        lines = data.splitlines(keepends=True)
        i = 0
        while i < len(lines) and lines[i].lstrip().startswith(b"#"):
            i += 1
        return b"".join(lines[i:])


REAL_ADAPTERS = [NpmAdapter(), PnpmAdapter(), YarnAdapter(), CargoAdapter(), GoAdapter(),
                 UvAdapter(), PoetryAdapter(), PipToolsAdapter()]


def adapter_for(ecosystem: str, registry=None) -> Adapter:
    for a in (registry if registry is not None else REAL_ADAPTERS):
        if a.ecosystem == ecosystem:
            return a
    raise HermeticError(f"no adapter for ecosystem '{ecosystem}'")


def detect_adapters(workdir: str, registry=None) -> list:
    return [a for a in (registry if registry is not None else REAL_ADAPTERS) if a.detect(workdir)]


# --- the gate ----------------------------------------------------------------

@dataclass
class GateResult:
    ecosystem: str
    passed: bool
    reason: str
    digest: str | None = None
    runs: int = 0
    digests: list = field(default_factory=list)
    lockfile_format: str | None = None

    def __bool__(self) -> bool:
        return self.passed


def preflight(adapter: Adapter, epoch: ResolutionEpoch, enforce_version: bool) -> str | None:
    """Return a fail-closed reason string, or None if all preconditions hold."""
    for path in adapter.required_epoch_paths:
        if epoch.get(path) in (None, ""):
            return f"missing frozen epoch field: {path}"
    # Time-freeze must be ABSOLUTE — relative cutoffs drift between specs.
    if adapter.time_freeze_field:
        val = epoch.get(adapter.time_freeze_field)
        if val and not is_absolute_timestamp(str(val)):
            return (f"relative/invalid time-freeze at {adapter.time_freeze_field}={val!r} — "
                    "an absolute timestamp (or an immutable mirror snapshot) is required")
    if enforce_version:
        pinned = epoch.get(f"resolvers.{adapter.ecosystem}.version")
        actual = adapter.installed_version()
        if actual is None:
            return f"resolver '{adapter.ecosystem}' not installed / version unknown"
        if pinned and actual != pinned:
            return f"resolver version drift: pinned {pinned}, installed {actual}"
        # Secondary runtimes (node/rustc/python) that influence resolution.
        want_rt = epoch.get(f"resolvers.{adapter.ecosystem}.runtimes") or {}
        have_rt = adapter.runtimes()
        for name, pinned_rt in want_rt.items():
            if pinned_rt and have_rt.get(name) != pinned_rt:
                return (f"{name} runtime drift: pinned {pinned_rt}, "
                        f"installed {have_rt.get(name)}")
    if adapter.platform_sensitive:
        want, have = epoch.get("platform") or {}, current_platform()
        for k in ("os", "arch"):
            if want.get(k) and want.get(k) != have.get(k):
                return (f"platform drift ({adapter.ecosystem} is platform-sensitive): "
                        f"epoch {k}={want.get(k)}, host {k}={have.get(k)}")
    return None


def determinism_gate(adapter: Adapter, src_workdir: str, epoch: ResolutionEpoch,
                     runs: int = 2, enforce_version: bool = True) -> GateResult:
    """Regenerate the lockfile `runs` times in clean copies of the work tree and assert
    byte-identical output. Fails CLOSED on any precondition miss, resolver error, digest
    mismatch, or lockfile-format drift."""
    reason = preflight(adapter, epoch, enforce_version)
    if reason:
        return GateResult(adapter.ecosystem, False, f"fail-closed: {reason}")

    digests: list[str] = []
    formats: set = set()
    for _ in range(max(2, runs)):
        tmp = tempfile.mkdtemp(prefix="hermetic-")
        dst = os.path.join(tmp, "work")
        try:
            shutil.copytree(src_workdir, dst, ignore=IGNORE_COPY, symlinks=True)
            try:
                raw = adapter.regenerate(dst, epoch)
            except HermeticError as e:
                return GateResult(adapter.ecosystem, False, f"fail-closed: {e}")
            formats.add(adapter.format_version(raw))
            digests.append(sha256(adapter.normalize(raw)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    observed_format = next(iter(formats)) if len(formats) == 1 else None
    if len(set(digests)) != 1:
        return GateResult(
            adapter.ecosystem, False,
            "fail-closed: NONDETERMINISTIC regeneration — digests differ across runs "
            "(resolver or registry is not frozen)",
            runs=len(digests), digests=digests)
    if len(formats) != 1:
        return GateResult(adapter.ecosystem, False,
                          f"fail-closed: lockfile format version is unstable across runs: {formats}",
                          runs=len(digests), digests=digests)
    pinned_format = epoch.get(f"formats.{adapter.ecosystem}")
    if pinned_format and observed_format and str(pinned_format) != str(observed_format):
        return GateResult(
            adapter.ecosystem, False,
            f"fail-closed: lockfile format drift — pinned {pinned_format}, emitted {observed_format} "
            "(stealth resolver-major swap)",
            runs=len(digests), digests=digests, lockfile_format=observed_format)
    return GateResult(adapter.ecosystem, True, "byte-identical across runs",
                      digest=digests[0], runs=len(digests), digests=digests,
                      lockfile_format=observed_format)


# --- CLI ---------------------------------------------------------------------

def cmd_record(args) -> int:
    epoch = ResolutionEpoch(epoch_id=args.epoch_id, created_at=args.created_at or "",
                            platform=current_platform())
    for a in detect_adapters(args.workdir):
        entry = {"tool": a.ecosystem, "version": a.installed_version()}
        rt = {k: v for k, v in a.runtimes().items() if v}
        if rt:
            entry["runtimes"] = rt
        epoch.resolvers[a.ecosystem] = entry
        snap = {}
        if a.ecosystem == "npm" and args.before:           # absolute time-pin (npm only)
            snap["before"] = args.before
        if a.ecosystem == "python-uv" and args.before:
            snap["exclude_newer"] = args.before
        if a.ecosystem in ("npm", "pnpm", "yarn-berry") and args.registry:
            snap["registry"] = args.registry
        if a.ecosystem == "cargo" and args.index_commit:
            snap["index_commit"] = args.index_commit
        if a.ecosystem == "go" and args.goproxy:
            snap["goproxy"] = args.goproxy
        epoch.registry_snapshot[a.ecosystem] = snap
        # Record the emitted lockfile format if a lockfile is already present.
        lock = os.path.join(args.workdir, a.lockfile_name)
        if os.path.exists(lock):
            with open(lock, "rb") as fh:
                fv = a.format_version(fh.read())
            if fv:
                epoch.formats[a.ecosystem] = fv
    out = epoch.to_json()
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(out + "\n")
        print(f"wrote epoch -> {args.out}")
    else:
        print(out)
    return 0


def _adapters(args) -> list:
    return [adapter_for(args.ecosystem)] if args.ecosystem else detect_adapters(args.workdir)


def cmd_plan(args) -> int:
    epoch = ResolutionEpoch.load(args.epoch)
    adapters = _adapters(args)
    if not adapters:
        print("no ecosystems detected", file=sys.stderr)
        return 2
    for a in adapters:
        argv, env = a.build_invocation(epoch, os.path.abspath(args.workdir))
        overrides = {k: v for k, v in env.items() if os.environ.get(k) != v}
        print(f"# {a.ecosystem}  ({a.lockfile_name})")
        print("  env:    " + " ".join(f"{k}={v}" for k, v in sorted(overrides.items())))
        print("  regen:  " + " ".join(argv))
        if a.assert_cmd:
            print("  assert: " + a.assert_cmd)
    return 0


def cmd_verify(args) -> int:
    epoch = ResolutionEpoch.load(args.epoch)
    rc = 0
    for a in _adapters(args):
        reason = preflight(a, epoch, enforce_version=not args.no_version_check)
        if reason:
            print(f"[FAIL] {a.ecosystem}: {reason}")
            rc = 1
        else:
            print(f"[ OK ] {a.ecosystem}: preconditions satisfied")
    return rc


def _run_gate(args, runs: int) -> int:
    epoch = ResolutionEpoch.load(args.epoch)
    adapters = _adapters(args)
    if not adapters:
        print("no ecosystems detected", file=sys.stderr)
        return 2
    rc = 0
    for a in adapters:
        res = determinism_gate(a, os.path.abspath(args.workdir), epoch,
                               runs=runs, enforce_version=not args.no_version_check)
        mark = "PASS" if res.passed else "FAIL"
        extra = f"  digest={res.digest[:16]}…" if res.digest else ""
        extra += f"  format={res.lockfile_format}" if res.lockfile_format else ""
        print(f"[{mark}] {res.ecosystem}: {res.reason}{extra}")
        if not res.passed:
            for i, d in enumerate(res.digests):
                print(f"        run{i+1} digest={d[:16]}…")
            rc = 1
    return rc


def cmd_gate(args) -> int:
    return _run_gate(args, runs=args.runs)


def cmd_probe(args) -> int:
    rc = _run_gate(args, runs=max(2, args.runs))
    if rc == 0:
        print("hermetic probe: resolution is deterministic under the epoch ✓")
    else:
        print("hermetic probe: DRIFT DETECTED — resolver/registry not hermetic ✗", file=sys.stderr)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Hermetic resolution gate for Conductor's merge engine.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("record", help="capture a ResolutionEpoch (versions, runtimes, platform, snapshot, format)")
    pr.add_argument("workdir", nargs="?", default=".")
    pr.add_argument("--epoch-id", required=True)
    pr.add_argument("--created-at", default="")
    pr.add_argument("--before", help="ABSOLUTE time-pin (ISO 8601) for npm --before / uv --exclude-newer")
    pr.add_argument("--registry", help="frozen registry/mirror URL (npm/pnpm/yarn)")
    pr.add_argument("--index-commit", help="frozen cargo index commit")
    pr.add_argument("--goproxy", help="frozen GOPROXY snapshot URL")
    pr.add_argument("--out", help="write epoch JSON to this path (default: stdout)")
    pr.set_defaults(func=cmd_record)

    for name, fn, helptext in [("verify", cmd_verify, "check fail-closed preconditions only"),
                               ("plan", cmd_plan, "print the frozen invocation without running it")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("workdir", nargs="?", default=".")
        p.add_argument("--epoch", required=True)
        p.add_argument("--ecosystem")
        p.add_argument("--no-version-check", action="store_true")
        p.set_defaults(func=fn)

    for name, fn, helptext in [("gate", cmd_gate, "assert byte-identical regeneration; fail closed"),
                               ("probe", cmd_probe, "CI drift probe: re-resolve twice and diff")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("workdir", nargs="?", default=".")
        p.add_argument("--epoch", required=True)
        p.add_argument("--ecosystem")
        p.add_argument("--runs", type=int, default=2)
        p.add_argument("--no-version-check", action="store_true")
        p.set_defaults(func=fn)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except HermeticError as e:
        print(f"fail-closed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
