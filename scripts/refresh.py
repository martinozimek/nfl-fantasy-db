"""
Master refresh orchestrator for nfl-fantasy-db.

Checks nflverse for new data and re-ingests as needed.
All underlying scripts are idempotent — safe to re-run at any time.

Usage:
    python scripts/refresh.py                    # smart refresh (new data only)
    python scripts/refresh.py --full             # re-ingest everything from scratch
    python scripts/refresh.py --check            # check for updates, no writes (exit 1 if stale)
    python scripts/refresh.py --source seasons   # refresh one source only
    python scripts/refresh.py --years 2023 2024  # specific seasons only

Sources: seasons, b2s, links, qa
"""

import argparse
import json
import logging
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import get_session, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ALL_SOURCES = ["seasons", "b2s", "links", "qa"]

_NFLVERSE_PLAYER_STATS_TAG = "player_stats"
_NFLVERSE_API = (
    "https://api.github.com/repos/nflverse/nflverse-data/releases/tags/{tag}"
)

_SCRIPTS = Path(__file__).parent


# ---------------------------------------------------------------------------
# Freshness helpers
# ---------------------------------------------------------------------------

def _nflverse_updated_at(tag: str) -> Optional[datetime]:
    """Return the published_at timestamp for a nflverse GitHub release."""
    url = _NFLVERSE_API.format(tag=tag)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nfl-fantasy-db/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        ts = data.get("published_at") or data.get("created_at")
        if ts:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception as exc:
        logger.warning("Could not check nflverse release '%s': %s", tag, exc)
    return None


def _last_season_row_ts(db_path: str) -> Optional[datetime]:
    """
    Approximate freshness: return the latest season_year loaded as a
    proxy datetime (Jan 1 of the year after the max season loaded).
    """
    try:
        from ffnfl.database import NFLPlayerSeason
        with get_session(db_path) as session:
            max_year = session.query(
                __import__("sqlalchemy").func.max(NFLPlayerSeason.season_year)
            ).scalar()
        if max_year:
            return datetime(max_year + 1, 1, 1, tzinfo=timezone.utc)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Status check (--check mode)
# ---------------------------------------------------------------------------

def check_updates(db_path: str) -> bool:
    """
    Print a freshness table and return True if any updates are available.
    """
    updates_available = False

    print("\n{:<20} {:<28} {}".format("Source", "Local freshness", "Remote status"))
    print("-" * 70)

    # player_stats CSV
    local_ts = _last_season_row_ts(db_path)
    local_str = local_ts.strftime("data through %Y") if local_ts else "not ingested"
    remote_ts = _nflverse_updated_at(_NFLVERSE_PLAYER_STATS_TAG)
    if remote_ts and local_ts and remote_ts > local_ts:
        remote_str = f"UPDATED {remote_ts.strftime('%Y-%m-%d')}"
        updates_available = True
    elif local_ts is None:
        remote_str = "NEEDS INGEST"
        updates_available = True
    else:
        remote_str = "up to date"
    print(f"  {'nflverse seasons':<18} {local_str:<28} {remote_str}")

    # b2s derived table
    try:
        from ffnfl.database import NFLB2S
        with get_session(db_path) as session:
            b2s_count = session.query(NFLB2S).count()
        b2s_str = f"{b2s_count} rows" if b2s_count else "not computed"
    except Exception:
        b2s_str = "not computed"
    print(f"  {'b2s labels':<18} {b2s_str:<28} (derived — re-run after seasons)")

    # cfb_link table
    try:
        from ffnfl.database import CFBLink
        with get_session(db_path) as session:
            link_count = session.query(CFBLink).count()
        link_str = f"{link_count} links" if link_count else "not linked"
    except Exception:
        link_str = "not linked"
    print(f"  {'cfb links':<18} {link_str:<28} (derived — re-run after seasons)")

    print()
    return updates_available


# ---------------------------------------------------------------------------
# Per-source refresh
# ---------------------------------------------------------------------------

def _run_script(script_name: str, extra_args: list[str] = None) -> bool:
    """Run a script in the scripts/ directory as a subprocess. Returns True on success."""
    cmd = [sys.executable, str(_SCRIPTS / script_name)] + (extra_args or [])
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        logger.error("Script %s exited with code %d", script_name, result.returncode)
        return False
    return True


def refresh_seasons(db_path: str, years: list[int]) -> None:
    """Re-ingest nflverse player_stats for the given season years."""
    year_args = ["--years"] + [str(y) for y in years]
    _run_script("populate_nfl_seasons.py", ["--db", db_path] + year_args)


def refresh_links(db_path: str, cfb_db: Optional[str] = None) -> None:
    """Re-run fuzzy link matching NFL players → cfb-prospect-db."""
    extra = ["--db", db_path]
    if cfb_db:
        extra += ["--cfb-db", cfb_db]
    _run_script("link_to_cfb.py", extra)


def refresh_b2s(db_path: str) -> None:
    """Recompute B2S training labels from season data."""
    _run_script("compute_b2s.py", ["--db", db_path])


def refresh_qa(db_path: str) -> None:
    """Generate and print the QA report."""
    _run_script("qa_report.py", ["--db", db_path])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh nfl-fantasy-db data sources. Smart (incremental) by default."
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Re-ingest everything from scratch (ignores existing data).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check for new nflverse data without writing anything. Exits 1 if stale.",
    )
    parser.add_argument(
        "--source", type=str, choices=ALL_SOURCES + ["all"], default="all",
        help="Refresh a single source only (default: all).",
    )
    parser.add_argument(
        "--years", type=int, nargs="+",
        default=list(range(2011, 2025)),
        help="NFL season years to ingest (default: 2011–2024).",
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--cfb-db", type=str, default=None,
        help="Path to cfb-prospect-db SQLite file (used by --source links).",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    init_db(db_path)

    if args.check:
        has_updates = check_updates(db_path)
        sys.exit(1 if has_updates else 0)

    sources = ALL_SOURCES if args.source == "all" else [args.source]

    logger.info(
        "Refresh: sources=%s | years=%s | mode=%s",
        ", ".join(sources),
        args.years,
        "FULL" if args.full else "incremental",
    )

    if "seasons" in sources:
        logger.info("=== nflverse player seasons ===")
        refresh_seasons(db_path, args.years)

    if "links" in sources:
        logger.info("=== CFB link matching ===")
        refresh_links(db_path, args.cfb_db)

    if "b2s" in sources:
        logger.info("=== B2S label computation ===")
        refresh_b2s(db_path)

    if "qa" in sources:
        logger.info("=== QA report ===")
        refresh_qa(db_path)

    logger.info("Done.")


if __name__ == "__main__":
    main()
