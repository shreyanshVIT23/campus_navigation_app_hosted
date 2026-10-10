"""Temporary copies of the data the app writes, for tests and benchmarks.

Call use_copies() before anything imports Utils.loader: the loader reads
os.environ once, and load_dotenv() never overrides variables that are already
set, so the app then only ever sees the copies. Read-only inputs (floor maps,
assets, the faculty sheet) are left where they are. This module must not import
Utils.loader itself.
"""

import os
import shutil
import sys
from pathlib import Path

from dotenv import dotenv_values

SERVER_DIR = Path(__file__).resolve().parent.parent
# .env settings for data the app writes
WRITABLE_SETTINGS = (
    "DB_PATH",
    "DB_EVENT",
    "DB_AUTH_PATH",
    "DB_TEACHER_DATA",
    "AUDIO_DIR",
)


def use_copies(data_dir: Path):
    """Copy every writable data path into data_dir and point os.environ at it."""
    if "Utils.loader" in sys.modules:
        raise RuntimeError(
            "use_copies() must run before Utils.loader is imported; "
            "the app would keep using the real data"
        )
    settings = dotenv_values(SERVER_DIR / ".env")
    for setting in WRITABLE_SETTINGS:
        configured = os.environ.get(setting) or settings.get(setting)
        if not configured:
            continue
        source = (SERVER_DIR / configured).resolve()
        copy = data_dir / source.relative_to(SERVER_DIR)
        if source.is_dir():
            shutil.copytree(source, copy, dirs_exist_ok=True)
        elif source.is_file():
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, copy)
        os.environ[setting] = str(copy)
