"""In-process one-shot authority; mailbox data never constitutes a grant."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable


def _identity(actor: str, request: dict) -> str:
    return json.dumps([actor, {key: request.get(key) for key in
                      ('tool_use_id', 'tool_name', 'tool_input', 'cwd')}],
                      sort_keys=True, ensure_ascii=False, allow_nan=False)


class ApprovalAuthority:
    def __init__(self):
        self._pending: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def register(self, request_id: str, actor: str, request: dict) -> None:
        identity = _identity(actor, request)
        with self._lock:
            if request_id in self._pending:
                raise ValueError('duplicate permission request')
            self._pending[request_id] = (identity, 'pending')

    def decide(self, request_id: str, actor: str, request: dict,
               evaluate: Callable[[], object]) -> bool:
        identity = _identity(actor, request)
        with self._lock:
            if self._pending.get(request_id) != (identity, 'pending'):
                return False
            self._pending[request_id] = (identity, 'reviewing')
        try:
            approved = evaluate() is None
        except Exception:
            approved = False
        with self._lock:
            # A timeout/cancellation while the UI was open revokes this request.
            if self._pending.get(request_id) != (identity, 'reviewing'):
                return False
            self._pending[request_id] = (identity, 'allowed' if approved else 'denied')
        return approved

    def consume(self, request_id: str, actor: str, request: dict) -> bool:
        identity = _identity(actor, request)
        with self._lock:
            return self._pending.pop(request_id, None) == (identity, 'allowed')

    def cancel(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
