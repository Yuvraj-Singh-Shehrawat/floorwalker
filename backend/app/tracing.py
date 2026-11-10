"""Optional Langfuse tracing. A no-op unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set.

Written against langfuse>=5 (OpenTelemetry-based):
    client.start_as_current_observation(name=..., as_type="agent"|"tool"|"generation"|"span", input=...)
returns a context manager; the yielded object has .update(output=..., usage_details=..., metadata=...).
Nested `with` blocks become nested observations in one trace.

Tracing must never break incident handling, so every call is wrapped.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("floorwalker.tracing")


class _Noop:
    def update(self, **_kwargs) -> None:
        pass


class _Safe:
    def __init__(self, obs) -> None:
        self._obs = obs

    def update(self, **kwargs) -> None:
        try:
            self._obs.update(**kwargs)
        except Exception as exc:   # noqa: BLE001
            log.warning("langfuse update failed: %s", exc)


class Tracer:
    def __init__(self) -> None:
        self.client = None
        if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"):
            try:
                from langfuse import get_client
                self.client = get_client()
                log.info("Langfuse tracing enabled")
            except Exception as exc:   # noqa: BLE001
                log.warning("Langfuse init failed, tracing disabled: %s", exc)

    @property
    def enabled(self) -> bool:
        return self.client is not None

    @contextmanager
    def span(self, name: str, as_type: str = "span", **kwargs):
        cm = None
        if self.client is not None:
            try:
                cm = self.client.start_as_current_observation(name=name, as_type=as_type, **kwargs)
            except Exception as exc:   # noqa: BLE001
                log.warning("langfuse span failed: %s", exc)
        if cm is None:
            yield _Noop()
            return
        with cm as obs:
            yield _Safe(obs)

    def flush(self) -> None:
        if self.client is not None:
            try:
                self.client.flush()
            except Exception as exc:   # noqa: BLE001
                log.warning("langfuse flush failed: %s", exc)
