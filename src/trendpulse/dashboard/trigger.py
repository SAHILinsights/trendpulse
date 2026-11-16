"""Authenticated manual ingestion trigger (optional).

The public dashboard never runs ingestion itself. When — and only when — the
deployment provides a fine-grained GitHub token (Actions: write, this repo
only) *and* an operator passphrase, the Control Center shows a form that
dispatches the ``ingest.yml`` workflow. Visitors without the passphrase
cannot trigger anything; the workflow's own concurrency group and the
database lock still prevent overlapping runs.
"""

from __future__ import annotations

import hmac
import re

import requests

WORKFLOW_FILE = "ingest.yml"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def enabled(token: str | None, repo: str | None, passphrase: str | None) -> bool:
    return bool(token and repo and passphrase)


def check_passphrase(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


def dispatch(token: str, repo: str, ref: str = "main", session=None, action: str = "run",
             run_id: str | None = None, force_search: bool = False) -> tuple[bool, str]:
    """Dispatch ``ingest.yml``. ``action``: run | replay (replay requires a valid run UUID)."""
    from trendpulse.dashboard.ops import valid_run_id

    if action not in {"run", "replay"}:
        return False, f"unsupported action {action!r}"
    if action == "replay" and not valid_run_id(run_id or ""):
        return False, "replay requires a valid run id"
    if not REPO_RE.match(repo or ""):
        return False, "TRENDPULSE_GITHUB_REPO must look like owner/name"
    inputs = {"reason": f"dashboard operator: {action}", "action": action, "run_id": run_id or "",
              "force_search": "true" if force_search else "false"}
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    resp = (session or requests).post(url, json={"ref": ref, "inputs": inputs},
                                      headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"}, timeout=15)
    if resp.status_code == 204:
        return True, "Workflow dispatched. The run appears in Execution history within a few minutes."
    return False, f"GitHub API returned HTTP {resp.status_code}"
