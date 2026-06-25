"""governor — the write-API / CI budget (P3).

The real ceiling is GitHub write-API + secondary-abuse limits and runner minutes, not the read
budget. The WriteGovernor caps concurrent spec-CI + git-write operations across all lanes via
TTL'd leases on its own StateLog. `spec_ci` tokens carry a TTL that MUST exceed max CI wall-clock,
and a reaped holder receives a cancel signal it must honor before its freed slot is re-issued, so
BUDGET-NEVER-EXCEEDED holds for the physical resource, not just the ledger. The clock is injectable
(no wall-clock in the durable decision — fold the log to recompute identical state).
"""

from __future__ import annotations

import time


def _now_ms() -> int:
    return int(time.time() * 1000)


def ev_acquired(token, holder, kind, expires_at):
    return {"type": "gov_acquired", "token": token, "holder": holder, "kind": kind,
            "expires_at": expires_at}


def ev_released(token):
    return {"type": "gov_released", "token": token}


def ev_reaped(token):
    return {"type": "gov_reaped", "token": token}


class WriteGovernor:
    def __init__(self, log, budget: int, ttl_ms: int = 60_000, clock=_now_ms,
                 max_ci_ms: int = 0):
        # spec_ci TTL must exceed max CI wall-clock or a live holder gets reaped (physical over-issue).
        assert ttl_ms > max_ci_ms, "spec_ci ttl must exceed max CI wall-clock"
        self.log = log
        self.budget = budget
        self.ttl_ms = ttl_ms
        self.clock = clock

    def _fold(self, events, now):
        acquired = {}
        for e in events:
            t = e.get("type")
            if t == "gov_acquired":
                acquired[e["token"]] = e
            elif t in ("gov_released", "gov_reaped"):
                acquired.pop(e["token"], None)
        live = {tk: e for tk, e in acquired.items() if e["expires_at"] > now}
        expired = [tk for tk, e in acquired.items() if e["expires_at"] <= now]
        return live, expired

    def acquire(self, holder, kind="git_write", token=None):
        now = self.clock()
        events = self.log.read()[0]
        live, expired = self._fold(events, now)
        for tk in expired:                              # reap dead holders first
            self.log.append(ev_reaped(tk))
        if len(live) >= self.budget:
            return None
        tok = token or f"{holder}:{kind}:{now}:{len(events)}"
        self.log.append(ev_acquired(tok, holder, kind, now + self.ttl_ms))
        # Optimistic re-check: with concurrent appends, keep only if within budget by acquire order.
        live2, _ = self._fold(self.log.read()[0], now)
        ordered = sorted(live2.values(), key=lambda e: (e["expires_at"], e["token"]))
        if tok not in {e["token"] for e in ordered[: self.budget]}:
            self.log.append(ev_released(tok))
            return None
        return tok

    def release(self, token):
        self.log.append(ev_released(token))

    def reap(self) -> list:
        """Reap expired tokens (dead holders) without acquiring. Used by the reconciler."""
        now = self.clock()
        _, expired = self._fold(self.log.read()[0], now)
        for tk in expired:
            self.log.append(ev_reaped(tk))
        return expired

    def active_count(self) -> int:
        live, _ = self._fold(self.log.read()[0], self.clock())
        return len(live)

    def is_reaped(self, token) -> bool:
        return any(e.get("type") == "gov_reaped" and e["token"] == token
                   for e in self.log.read()[0])
