"""trailers — single source of truth for Conductor commit-message trailers (issue #9).

`land.land_message` (emit) and `parse_trailers` (parse) MUST be inverse. `Closes` is a LIST — one
`Closes #N` line per issue (the cardinality fix; the old code emitted a single scalar line).
Imported by both `land.py` and `ssot.py` so the labels never drift apart.
"""

from __future__ import annotations

import re

TRAILER_LABELS = {"change_id": "Change-Id", "agent": "Agent-Id",
                  "model": "Model", "batch": "Conductor-Batch"}

_CLOSES_RE = re.compile(r"^Closes #(\d+)$")
_LABEL_RE = {k: re.compile(rf"^{re.escape(v)}:\s*(.+?)\s*$") for k, v in TRAILER_LABELS.items()}


def format_closes(closes) -> list[str]:
    """One `Closes #N` line per issue."""
    return [f"Closes #{int(n)}" for n in closes]


def parse_trailers(message: str) -> dict:
    """Inverse of land_message's trailer block. Untrusted input: a non-numeric `Closes #x` or an
    unknown trailer is dropped, never raised. `Closes` accumulates into a list[int]."""
    out = {"closes": [], "change_id": None, "agent": None, "model": None, "batch": None}
    for raw in message.splitlines():
        line = raw.strip()
        m = _CLOSES_RE.match(line)
        if m:
            out["closes"].append(int(m.group(1)))
            continue
        for key, rx in _LABEL_RE.items():
            mm = rx.match(line)
            if mm:
                out[key] = mm.group(1)
                break
    return out
