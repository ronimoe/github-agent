"""changeset — strict mini-YAML fragment parser (P4 V2, reused by P5).

Fragments are UNTRUSTED input, parsed with a tiny closed grammar (no anchors, flow maps, tabs,
or unknown keys; bump level is a closed enum). The changelog body is opaque text after a `---`
separator, so it cannot inject frontmatter. Format:

    bumps:
      web: minor
      api: patch
    issues: [12, 13]
    agent: agent-7
    ---
    Human-readable changelog body.

(or a single `bump: <level>` with optional `component:`). Unique paths
`.conductor/changesets/<ULID>-<agent>-<sha8>.cset.yaml` make N agents never path-collide.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from apidiff import LEVELS

ALLOWED = {"bumps", "bump", "component", "issues", "issue", "agent", "change_id"}
_ULID32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ChangesetError(Exception):
    pass


@dataclass
class Fragment:
    bumps: dict = field(default_factory=dict)   # component -> level int
    issues: list = field(default_factory=list)
    agent: str | None = None
    body: str = ""
    change_id: str | None = None                # the authoritative join key to the landed commit (#9)


def _parse_issues(val: str) -> list:
    val = val.strip()
    if val.startswith("["):
        if not val.endswith("]"):
            raise ChangesetError(f"malformed issues list: {val!r}")
        inner = val[1:-1].strip()
        nums = [x.strip() for x in inner.split(",") if x.strip()]
    else:
        nums = [val]
    out = []
    for n in nums:
        if not n.lstrip("#").isdigit():
            raise ChangesetError(f"non-numeric issue: {n!r}")
        out.append(int(n.lstrip("#")))
    return out


def parse_fragment(text: str) -> Fragment:
    if "\t" in text:
        raise ChangesetError("tabs are not allowed")
    lines = text.splitlines()
    sep = next((i for i, l in enumerate(lines) if l.strip() == "---" and i > 0), None)
    if sep is None:
        raise ChangesetError("missing '---' separator")
    front, body = lines[:sep], "\n".join(lines[sep + 1:]).strip()
    if not body:
        raise ChangesetError("empty changelog body")

    bumps, single_bump, component, issues, agent, change_id = {}, None, None, [], None, None
    i = 0
    while i < len(front):
        line = front[i]
        if not line.strip():
            i += 1
            continue
        if line[0] in " ":
            raise ChangesetError(f"unexpected indentation: {line!r}")
        if ":" not in line:
            raise ChangesetError(f"malformed line: {line!r}")
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if key not in ALLOWED:
            raise ChangesetError(f"unknown key: {key!r}")
        if key not in ("issues",) and any(c in val for c in "{}&*"):
            raise ChangesetError(f"flow/anchor token not allowed in {key!r}")
        if key == "bumps":
            if val:
                raise ChangesetError("bumps: must be a block, not inline")
            i += 1
            while i < len(front) and front[i].startswith("  "):
                sub = front[i].strip()
                if ":" not in sub:
                    raise ChangesetError(f"malformed bump entry: {sub!r}")
                comp, lv = (s.strip() for s in sub.split(":", 1))
                if lv not in LEVELS:
                    raise ChangesetError(f"bad bump level: {lv!r}")
                bumps[comp] = LEVELS[lv]
                i += 1
            continue
        if key == "bump":
            if val not in LEVELS:
                raise ChangesetError(f"bad bump level: {val!r}")
            single_bump = LEVELS[val]
        elif key == "component":
            component = val
        elif key in ("issues", "issue"):
            issues = _parse_issues(val)
        elif key == "agent":
            agent = val
        elif key == "change_id":
            change_id = val
        i += 1

    if not bumps:
        if single_bump is None:
            raise ChangesetError("no bump declared")
        bumps = {component or "_default": single_bump}
    return Fragment(bumps=bumps, issues=issues, agent=agent, body=body, change_id=change_id)


def gen_ulid(ts_ms: int, rand: bytes) -> str:
    """A monotonic-ish, sortable ULID-like id: 10 chars time + 16 chars randomness (Crockford)."""
    def enc(n, width):
        s = ""
        for _ in range(width):
            s = _ULID32[n & 31] + s
            n >>= 5
        return s
    rnd = int.from_bytes(rand[:10].ljust(10, b"\0"), "big")
    return enc(ts_ms, 10) + enc(rnd, 16)


def fragment_path(ts_ms: int, agent: str, sha8: str, rand: bytes = b"\0" * 10) -> str:
    return f".conductor/changesets/{gen_ulid(ts_ms, rand)}-{agent}-{sha8}.cset.yaml"
