"""
Configuration loader for nfl-fantasy-db.

Reads environment variables from .env in the same directory.
All scripts and the ffnfl package call get_db_path() / get_cfb_db_path()
rather than hardcoding paths.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_ENV = Path(__file__).parent / ".env"
load_dotenv(_ENV, override=False)


def get_db_path() -> str:
    """Return the path to the nfl-fantasy-db SQLite file (from NFL_DB_PATH in .env)."""
    raw = os.getenv("NFL_DB_PATH")
    if not raw:
        raise RuntimeError(
            "NFL_DB_PATH not set. Copy .env.example to .env and fill it in."
        )
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_cfb_db_path() -> str:
    """Return the path to the cfb-prospect-db SQLite file (from CFB_DB_PATH in .env)."""
    raw = os.getenv("CFB_DB_PATH")
    if not raw:
        raise RuntimeError(
            "CFB_DB_PATH not set. Copy .env.example to .env and fill it in."
        )
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return str(path)
