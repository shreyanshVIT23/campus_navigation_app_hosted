"""Caches that refresh themselves when their data file changes.

A file's version is (modified time, size, bytes 24-28 of the file). For SQLite
databases those 4 bytes are the header's file change counter, which rollback-
journal databases increment on every committed write
(https://www.sqlite.org/fileformat.html#file_change_counter), so even quick
writes that keep the modified time are seen.

Reading a version costs a stat and a small read (~0.2 ms on Windows), about as
much as a query. Inside consistent_versions() each file's version is read once
and reused, so one route sees one consistent snapshot of the data.
"""

import contextlib
import copy
import functools
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import ParamSpec, TypeVar

from . import timing

FileVersion = tuple[int, int, bytes]

P = ParamSpec("P")
R = TypeVar("R")

_scope: ContextVar[dict[str, FileVersion] | None] = ContextVar(
    "file_versions", default=None
)


def read_version(path: Path) -> FileVersion:
    stat = path.stat()
    with path.open("rb") as file:
        header = file.read(28)
    return stat.st_mtime_ns, stat.st_size, header[24:28]


def file_version(path: str | Path) -> FileVersion:
    """The file's current version; read once per consistent_versions() scope."""
    key = str(path)
    seen = _scope.get()
    if seen is not None and key in seen:
        return seen[key]
    version = read_version(Path(path))
    if seen is not None:
        seen[key] = version
    return version


@contextlib.contextmanager
def consistent_versions() -> Iterator[None]:
    """Treat every data file as unchanged until the block ends (one route)."""
    if _scope.get() is not None:  # nested: keep the outer snapshot
        yield
        return
    token = _scope.set({})
    try:
        yield
    finally:
        _scope.reset(token)


def cached_by_file(
    name: str, maxsize: int = 1024, copy_result: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Cache a function whose first argument is a data file's path.

    Results are reused until the file changes. None results and exceptions are
    not cached. With copy_result, callers get a shallow copy, so changing a
    returned list never changes the cache; pass False only for values nothing
    modifies (e.g. frozen graphs, bytes).
    """

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        entries: OrderedDict[tuple, R] = OrderedDict()
        lock = threading.Lock()

        @functools.wraps(function)
        def cached(*args: P.args, **kwargs: P.kwargs) -> R:
            path, rest = args[0], args[1:]
            if not isinstance(path, str | Path):
                raise TypeError(f"{name}: the first argument must be a file path")
            key = (str(path), file_version(path), rest, tuple(sorted(kwargs.items())))
            # None is never cached, so it means "not in the cache"
            with lock:
                value = entries.get(key)
                if value is not None:
                    entries.move_to_end(key)
            timing.cache_event(name, value is not None)
            if value is None:
                value = function(*args, **kwargs)
                if value is None:
                    return value
                with lock:
                    entries[key] = value
                    while len(entries) > maxsize:
                        entries.popitem(last=False)
            return copy.copy(value) if copy_result else value

        return cached

    return decorate
