"""Best-effort HTTP callbacks for execution lifecycle events.

Each task may declare up to three optional URLs in ``cron.toml`` under
``[callbacks]``: ``start``, ``end_success`` and ``end_failure``. When a URL is
present the daemon POSTs a JSON payload at the matching moment of a run.
Delivery is best-effort: a small timeout, a single attempt, and failures are
reported to the caller rather than retried.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from .records import ExecutionRecord


class CallbackError(Exception):
    """Raised when an HTTP callback cannot be delivered."""


@dataclass
class CallbackSender:
    timeout: float = 10.0

    def send(self, url: str, payload: dict[str, Any]) -> None:
        """POST ``payload`` as JSON to ``url``, raising CallbackError on failure."""
        if not url:
            return
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                resp.read()
        except (OSError, ValueError) as exc:
            raise CallbackError(f"callback to {url!r} failed: {exc}") from exc


def build_payload(rec: ExecutionRecord) -> dict[str, Any]:
    """Standard JSON payload describing ``rec``; null fields are omitted."""
    payload: dict[str, Any] = {
        "task": rec.task,
        "execution_id": rec.id,
        "status": rec.status,
        "return_code": rec.return_code,
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "ended_at": rec.ended_at.isoformat() if rec.ended_at else None,
    }
    return {key: value for key, value in payload.items() if value is not None}
