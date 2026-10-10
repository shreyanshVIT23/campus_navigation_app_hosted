"""Per-request stage timings for the logs, the Server-Timing header and benchmarks.

    with timed("graph"): ...        # or as a decorator: @timed("graph")

Inside a request (TimingMiddleware) each stage's time is added to that request's
RequestTimings, kept in a ContextVar so concurrent requests never mix. Work run
through run_in_threadpool still reaches the request: AnyIO copies the context into
the worker thread, and the RequestTimings object itself is shared, not copied.
Outside a request a stage is only logged at DEBUG.
"""

import contextlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Patched by tests; seconds, monotonic
clock = time.perf_counter


class RequestTimings:
    def __init__(self):
        # stage -> total milliseconds and number of calls
        self.stages: dict[str, float] = {}
        self.calls: dict[str, int] = {}
        # "graph:hit" -> count
        self.cache: Counter[str] = Counter()

    def add(self, stage: str, ms: float):
        self.stages[stage] = self.stages.get(stage, 0.0) + ms
        self.calls[stage] = self.calls.get(stage, 0) + 1


_current: ContextVar[RequestTimings | None] = ContextVar(
    "request_timings", default=None
)


def current() -> RequestTimings | None:
    return _current.get()


@contextlib.contextmanager
def collect() -> Iterator[RequestTimings]:
    """Collect the stages timed inside this block (one request)."""
    timings = RequestTimings()
    token = _current.set(timings)
    try:
        yield timings
    finally:
        _current.reset(token)


@contextlib.contextmanager
def timed(stage: str) -> Iterator[None]:
    """Time a block, or every call of a decorated function, as `stage`."""
    start = clock()
    try:
        yield
    finally:
        ms = (clock() - start) * 1000
        logger.debug("stage=%s ms=%.2f", stage, ms)
        timings = _current.get()
        if timings is not None:
            timings.add(stage, ms)


_stats_lock = threading.Lock()
_cache_stats: dict[str, dict[str, int]] = {}


def cache_event(cache: str, hit: bool):
    """Count a cache hit or miss, for this request and for the whole process."""
    outcome = "hit" if hit else "miss"
    logger.debug("cache=%s %s", cache, outcome)
    timings = _current.get()
    if timings is not None:
        timings.cache[f"{cache}:{outcome}"] += 1
    with _stats_lock:
        stats = _cache_stats.setdefault(cache, {"hits": 0, "misses": 0})
        stats["hits" if hit else "misses"] += 1


def cache_stats() -> dict[str, dict[str, int]]:
    with _stats_lock:
        return {cache: dict(counts) for cache, counts in _cache_stats.items()}


def reset_cache_stats():
    with _stats_lock:
        _cache_stats.clear()


def cache_summary(timings: RequestTimings) -> str:
    return ",".join(f"{event}={count}" for event, count in timings.cache.items())


def server_timing_header(timings: RequestTimings, total_ms: float) -> str:
    """Server-Timing value: "total;dur=…, <stage>;dur=…, cache;desc=\"…\""."""
    metrics = [f"total;dur={total_ms:.2f}"]
    metrics += [f"{stage};dur={ms:.2f}" for stage, ms in timings.stages.items()]
    if timings.cache:
        metrics.append(f'cache;desc="{cache_summary(timings)}"')
    return ", ".join(metrics)


class TimingMiddleware:
    """Logs one line per HTTP request with its stage timings, and adds them to the
    response as a Server-Timing header (shown in the browser's devtools).

    Pure ASGI rather than BaseHTTPMiddleware, which would stop ContextVar changes
    from propagating (Starlette middleware docs).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = clock()
        status = 500  # unless a response starts; unhandled errors become 500
        with collect() as timings:

            async def send_with_timings(message: Message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    total_ms = (clock() - start) * 1000
                    headers = MutableHeaders(scope=message)
                    headers.append(
                        "Server-Timing", server_timing_header(timings, total_ms)
                    )
                await send(message)

            try:
                await self.app(scope, receive, send_with_timings)
            finally:
                total_ms = (clock() - start) * 1000
                stages = " ".join(f"{s}={ms:.1f}" for s, ms in timings.stages.items())
                cache = f" cache={cache_summary(timings)}" if timings.cache else ""
                logger.info(
                    "%s %s %d %.1fms %s%s",
                    scope["method"],
                    scope["path"],
                    status,
                    total_ms,
                    stages,
                    cache,
                )
