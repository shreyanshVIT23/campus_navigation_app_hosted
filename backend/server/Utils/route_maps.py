"""Drawn route maps, kept in memory per route and served by route id.

Each route request gets its own random id, so people routing at the same time
never see each other's maps, and nothing is written to disk. Maps are kept for
ROUTE_MAP_TTL_SECONDS (long enough for the browser to load them) within a memory
budget; the oldest go first. They live in this server process only, so the app
must run as a single process (as the Dockerfile does) or with sticky sessions.
"""

import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable

ROUTE_MAP_TTL_SECONDS = 10 * 60
ROUTE_MAP_MAX_BYTES = 64 * 1024 * 1024
_ROUTE_ID = re.compile(r"[0-9a-f]{32}")

# (building, floor) -> SVG
FloorMaps = dict[tuple[str, int], bytes]


def is_route_id(text: str) -> bool:
    return _ROUTE_ID.fullmatch(text) is not None


class RouteMapStore:
    def __init__(
        self,
        max_bytes: int = ROUTE_MAP_MAX_BYTES,
        ttl_seconds: float = ROUTE_MAP_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        # route id -> (saved at, maps), oldest first
        self._routes: OrderedDict[str, tuple[float, FloorMaps]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def save(self, maps: FloorMaps) -> str:
        route_id = secrets.token_hex(16)
        size = sum(len(svg) for svg in maps.values())
        with self._lock:
            self._drop_expired()
            self._routes[route_id] = (self.clock(), dict(maps))
            self._bytes += size
            while self._bytes > self.max_bytes and len(self._routes) > 1:
                self._forget(next(iter(self._routes)))
        return route_id

    def get(self, route_id: str, building: str, floor: int) -> bytes | None:
        with self._lock:
            self._drop_expired()
            entry = self._routes.get(route_id)
        return None if entry is None else entry[1].get((building, floor))

    def _drop_expired(self):
        oldest_allowed = self.clock() - self.ttl_seconds
        while self._routes:
            route_id, (saved_at, _) = next(iter(self._routes.items()))
            if saved_at >= oldest_allowed:
                return
            self._forget(route_id)

    def _forget(self, route_id: str):
        _, maps = self._routes.pop(route_id)
        self._bytes -= sum(len(svg) for svg in maps.values())


store = RouteMapStore()
