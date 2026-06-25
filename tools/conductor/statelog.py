#!/usr/bin/env python3
"""statelog — Conductor's durable, append-only state log on an orphan git ref.

This is the ONLY durable-state commit primitive (ADR-0003). Every mutation is an
append-only commit on `refs/conductor/...` whose parent is the EXACT tip the writer
just read, pushed fast-forward-only. GitHub has no expected-old-OID ref CAS; the
fast-forward refspec is what we have, and it is sufficient *because the log is
single-lineage* — so the strict invariant below closes the lost-update hole:

    INVARIANT: a candidate commit's parent is the tip read in THIS attempt. The push
    is fast-forward-only. If another writer advanced the ref past that tip, the push is
    rejected (non-fast-forward); we re-read, re-reduce against the new tip, and retry.

The state lives as one JSON-Lines blob (`log.jsonl`) per commit; reading the state is
reading that blob at the tip. The reducer is the append (events are never mutated).

No external services, no database — the git ref store IS the database. The semantics
are validated against real git in `test_conductor.py`; pointing `remote` at a GitHub
URL is the same code path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from gitutil import WorkRepo, git_available

LOG_FILE = "log.jsonl"


class RetriesExhausted(Exception):
    pass


class AppendResult:
    def __init__(self, ok: bool, commit: str | None, attempts: int, reason: str = ""):
        self.ok, self.commit, self.attempts, self.reason = ok, commit, attempts, reason


class StateLog:
    """An append-only event log bound to a single ref (one lane / the order ref)."""

    def __init__(self, remote: str, ref: str, repo: WorkRepo | None = None):
        self.ref = ref
        self.repo = repo or WorkRepo(remote)
        self._owns = repo is None

    def read(self) -> tuple[list, str | None]:
        """Return (events, tip_oid). tip is None when the ref does not yet exist.
        Fetches the ref so the tip's objects are local (needed to parent the next
        commit on it)."""
        tip = self.repo.fetch_ref(self.ref)
        if not tip:
            return [], None
        content = self.repo.read_blob(tip, LOG_FILE) or ""
        events = [json.loads(line) for line in content.splitlines() if line.strip()]
        return events, tip

    def append(self, event: dict, max_retries: int = 16) -> AppendResult:
        """Append one event, retrying on contention. Lost-update-free: each attempt
        re-reads the tip and parents the new commit on exactly that tip."""
        err = ""
        for attempt in range(1, max_retries + 1):
            events, tip = self.read()                      # S0 = tip (this attempt)
            content = "".join(json.dumps(e, sort_keys=True) + "\n" for e in events + [event])
            commit = self.repo.write_commit({LOG_FILE: content}, parent=tip)
            # Identity-checked append: succeeds only if the ref still equals `tip`
            # (the tip we just read). This is the ADR-0003 invariant, enforced
            # independently of ref-namespace fast-forward protection.
            ok, err = self.repo.lease_push(self.ref, commit, tip)
            if ok:
                return AppendResult(True, commit, attempt)
            # Rejected => someone advanced past our tip. Re-read, re-reduce, retry.
            time.sleep(min(0.02 * attempt, 0.4))
        raise RetriesExhausted(f"{self.ref}: {max_retries} attempts exhausted (last: {err})")

    def close(self):
        if self._owns:
            self.repo.close()


# --- CLI ---------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Conductor append-only state log on a git ref.")
    ap.add_argument("--remote", required=True, help="git remote (a local path or a GitHub URL)")
    ap.add_argument("--ref", required=True, help="e.g. refs/conductor/lane/auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tip", help="print the current ref tip OID")
    sub.add_parser("read", help="print the decoded event log as JSON")
    pa = sub.add_parser("append", help="append one JSON event")
    pa.add_argument("event", help="a JSON object")

    args = ap.parse_args(argv)
    if not git_available():
        print("git is required", file=sys.stderr)
        return 2
    log = StateLog(args.remote, args.ref)
    try:
        if args.cmd == "tip":
            print(log.repo.ls_remote(args.ref) or "")
        elif args.cmd == "read":
            events, tip = log.read()
            print(json.dumps({"tip": tip, "events": events}, indent=2))
        elif args.cmd == "append":
            res = log.append(json.loads(args.event))
            print(json.dumps({"ok": res.ok, "commit": res.commit, "attempts": res.attempts}))
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
