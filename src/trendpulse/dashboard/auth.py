"""Operator authorisation for control-plane actions.

Model: the public dashboard is read-only for everyone. Operator actions (new run, replay)
are *dispatched* to the GitHub ``ingest.yml`` workflow — they never execute in the dashboard
process and never use the dashboard's read-only database role. Dispatch requires all of:

* a deployment secret ``TRENDPULSE_GITHUB_DISPATCH_TOKEN`` (fine-grained PAT, one repo,
  Actions: read/write) and ``TRENDPULSE_GITHUB_REPO``;
* a deployment secret ``TRENDPULSE_OPERATOR_PASSPHRASE`` that the operator types in;
* passing the per-session lockout (5 wrong attempts → 15 minutes).

No new dependency: constant-time comparison (``hmac.compare_digest``), state in the Streamlit
session. Configuration changes are not possible from the UI by design (they go through git
review). For multi-user operator access, put the app behind an identity provider instead.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import MutableMapping

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 60 * 60


def _state(store: MutableMapping) -> MutableMapping:
    return store.setdefault("_tp_operator", {"ok_until": 0.0, "fails": 0, "locked_until": 0.0})


def is_operator(store: MutableMapping, now: float | None = None) -> bool:
    return _state(store)["ok_until"] > (now if now is not None else time.time())


def locked(store: MutableMapping, now: float | None = None) -> float:
    """Seconds of lockout remaining (0 if not locked)."""
    now = now if now is not None else time.time()
    return max(0.0, _state(store)["locked_until"] - now)


def sign_in(store: MutableMapping, given: str, expected: str | None, now: float | None = None) -> tuple[bool, str]:
    now = now if now is not None else time.time()
    st = _state(store)
    if not expected:
        return False, "Operator access is not configured on this deployment."
    if locked(store, now):
        return False, f"Locked after {MAX_ATTEMPTS} failed attempts; retry in {int(locked(store, now) // 60) + 1} min."
    if hmac.compare_digest(given.encode(), expected.encode()):
        st.update(ok_until=now + SESSION_TTL_SECONDS, fails=0)
        return True, "Signed in as operator for 60 minutes."
    st["fails"] += 1
    if st["fails"] >= MAX_ATTEMPTS:
        st.update(locked_until=now + LOCKOUT_SECONDS, fails=0)
        return False, "Too many failed attempts — locked for 15 minutes."
    return False, f"Invalid passphrase ({MAX_ATTEMPTS - st['fails']} attempts left)."


def sign_out(store: MutableMapping) -> None:
    _state(store)["ok_until"] = 0.0
