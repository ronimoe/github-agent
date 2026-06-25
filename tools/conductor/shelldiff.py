"""shelldiff — external-toolchain ApiDiffer with a structural fail-safe to UNCERTAIN (issue #8).

The ONLY subprocess touchpoint; never imported by `differs.py`. `run`/`which` are injected so the
fail-safe logic is unit-tested offline. EVERY non-success path → (MAJOR, True). Until the runtime is
hermetic (no network / pinned $PATH/$HOME/locale/clock), an authoritative ShellDiffer is CAPPED at
"UNCERTAIN unless a break is found" — it may report MAJOR/MINOR with certainty but NEVER certain-NONE
(a partial/non-hermetic compiler run could under-report). Not wired into `default_registry` — it is a
per-language opt-in; the default stays the pure, deterministic registry.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from apidiff import NONE
from differs import ApiDelta, MAJOR


def _run(argv, cwd=None, stdin=None, timeout=600):
    p = subprocess.run(argv, cwd=cwd, input=stdin, text=True, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _which(binary):
    return shutil.which(binary) is not None


@dataclass
class ShellDifferSpec:
    language: str
    extensions: tuple
    probe: tuple                 # version check, e.g. ("cargo", "--version")
    build_cmd: tuple             # the differ invocation
    parse: object                # (rc, out, err) -> ApiDelta | None


class ShellDiffer:
    def __init__(self, spec: ShellDifferSpec, *, run=_run, which=_which, hermetic: bool = False):
        self.spec = spec
        self.language = spec.language
        self.extensions = spec.extensions
        self._run = run
        self._which = which
        self.hermetic = hermetic

    def _uncertain(self, reason):
        return MAJOR, True, ApiDelta(level=MAJOR, uncertain=True, reasons=[reason])

    def diff_tree(self, repo, base, merged, paths):
        """Caller materializes the FULL base + merged tree (FIX-5); the build_cmd runs against it."""
        if not self._which(self.spec.probe[0]):
            return self._uncertain("uncertain:no-toolchain")
        try:
            rc, out, err = self._run(list(self.spec.probe))
        except Exception:
            return self._uncertain("uncertain:no-toolchain")
        if rc != 0:
            return self._uncertain("uncertain:no-toolchain")
        try:
            rc, out, err = self._run(list(self.spec.build_cmd))
        except Exception:
            return self._uncertain("uncertain:toolchain-error")
        try:
            delta = self.spec.parse(rc, out, err)
        except Exception:
            delta = None
        if delta is None:
            return self._uncertain("uncertain:unparseable")
        if not self.hermetic and delta.level == NONE and not delta.uncertain:
            delta.uncertain = True
            delta.reasons.append("uncertain:non-hermetic")     # cap certain-NONE until hermetic
            return NONE, True, delta
        return delta.level, delta.uncertain, delta

    def diff(self, old_src, new_src):
        # ShellDiffer needs the whole tree; the per-file path degrades to UNCERTAIN (fail-safe).
        return self._uncertain("uncertain:needs-tree")
