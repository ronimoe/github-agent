"""semver — minimal semantic-version arithmetic (P5). The 0.x major-bump policy is an explicit
flag (default: a MAJOR bump of a 0.x version raises the MINOR, the conventional 0.x behaviour)."""

from __future__ import annotations

import re

from apidiff import NONE, PATCH, MINOR, MAJOR

_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def parse(v: str) -> tuple[int, int, int]:
    m = _RE.match(v.strip())
    if not m:
        raise ValueError(f"bad version {v!r}")
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def fmt(t) -> str:
    return ".".join(map(str, t))


def apply(v: str, level: int, zero_major_bumps_major: bool = False) -> tuple:
    M, m, p = parse(v)
    if level >= MAJOR:
        if M == 0 and not zero_major_bumps_major:
            return (0, m + 1, 0)            # 0.x: a breaking change raises the minor by convention
        return (M + 1, 0, 0)
    if level == MINOR:
        return (M, m + 1, 0)
    if level == PATCH:
        return (M, m, p + 1)
    return (M, m, p)


def bump_version(v: str, level: int, zero_major_bumps_major: bool = False) -> str:
    return fmt(apply(v, level, zero_major_bumps_major))


def join(levels) -> int:
    return max(levels) if levels else NONE
