"""risktier — paths that force a required HUMAN approval (P4, ADR-0008). A change touching any
risk-tier path routes the verdict to NEEDS_HUMAN regardless of the mechanical checks; on these
paths a too-low semver bump or a subtle behavioural break cannot silently land."""

from __future__ import annotations

import fnmatch

RISK_GLOBS = [
    "*auth*", "*security*", "*migrations*", "*migrate*",
    ".github/*", ".github/**", "*.lock", "package-lock.json", "go.sum", "Cargo.lock",
    "pnpm-lock.yaml", "yarn.lock", "uv.lock", "poetry.lock",
]


def is_risk(paths) -> bool:
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(base, g) for g in RISK_GLOBS):
            return True
    return False
