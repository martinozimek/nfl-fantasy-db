"""
Populate nfl_player_game_logs from nflverse player_stats CSV.

Stores one row per player per week with every stat needed to compute
custom fantasy scoring formats: passing, rushing, receiving, 2-pt
conversions, fumbles lost, and special teams TDs.

This is the raw layer for custom scoring. Run compute_scores.py
afterward to produce fantasy point totals per scoring format.

Usage:
    python scripts/populate_game_logs.py
    python scripts/populate_game_logs.py --years 2022 2023 2024
    python scripts/populate_game_logs.py --positions WR RB TE

Source:
    https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats.csv
    Weekly rows, season_type=REG, one row per player per week.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import NFLPlayerGameLog, get_session, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "player_stats/player_stats.csv"
)

_DEFAULT_POSITIONS = {"WR", "RB", "TE"}

# nflverse column → NFLPlayerGameLog attribute
# nflverse uses some different names than our schema
_COL_MAP = {
    # Identity (handled separately)
    # Passing
    "completions":               "completions",
    "attempts":                  "pass_attempts",
    "passing_yards":             "pass_yards",
    "passing_tds":               "pass_tds",
    "interceptions":             "interceptions",
    "passing_2pt_conversions":   "pass_2pt",
    "sack_fumbles_lost":         "sack_fumbles_lost",
    # Rushing
    "carries":                   "rush_attempts",
    "rushing_yards":             "rush_yards",
    "rushing_tds":               "rush_tds",
    "rushing_2pt_conversions":   "rush_2pt",
    "rushing_fumbles_lost":      "rushing_fumbles_lost",
    # Receiving
    "targets":                   "targets",
    "receptions":                "receptions",
    "receiving_yards":           "rec_yards",
    "receiving_tds":             "rec_tds",
    "receiving_2pt_conversions": "rec_2pt",
    "receiving_fumbles_lost":    "receiving_fumbles_lost",
    # Misc
    "special_teams_tds":         "special_teams_tds",
    # nflverse pre-computed scores
    "fantasy_points":            "fantasy_points_std",
    "fantasy_points_ppr":        "fantasy_points_ppr",
}


def _safe_int(val) -> Optional[int]:
    try:
        return int(val) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if pd.notna(val) else None
    except (TypeError, ValueError):
        return None


def fetch(years: list[int], positions: set[str]) -> pd.DataFrame:
    """Download nflverse player_stats and filter to relevant rows."""
    logger.info("Downloading nflverse player_stats...")
    df = pd.read_csv(_PLAYER_STATS_URL, low_memory=False)
    logger.info("  Loaded %d weekly rows total.", len(df))

    df = df[df["season_type"] == "REG"].copy()
    df = df[df["position"].isin(positions)].copy()
    df = df[df["season"].isin(years)].copy()

    logger.info(
        "  After filter: %d rows for years=%s, positions=%s",
        len(df), years, sorted(positions),
    )
    return df


def ingest(db_path: str, df: pd.DataFrame) -> int:
    """Upsert game log rows. Returns count of rows written."""
    if df.empty:
        return 0

    count = 0
    with get_session(db_path) as session:
        for _, row in df.iterrows():
            nflverse_id = str(row.get("player_id") or "")
            season_year = int(row["season"])
            week = int(row["week"])

            existing = (
                session.query(NFLPlayerGameLog)
                .filter(
                    NFLPlayerGameLog.nflverse_id == nflverse_id,
                    NFLPlayerGameLog.season_year == season_year,
                    NFLPlayerGameLog.week == week,
                )
                .first()
            )
            if existing is None:
                existing = NFLPlayerGameLog(
                    nflverse_id=nflverse_id,
                    season_year=season_year,
                    week=week,
                )
                session.add(existing)

            existing.player_name = str(row.get("player_display_name") or "")
            existing.position = row.get("position")
            existing.nfl_team = row.get("recent_team")

            # Map nflverse columns to model attributes
            for nfv_col, attr in _COL_MAP.items():
                val = row.get(nfv_col)
                if attr.endswith(("_std", "_ppr")):
                    setattr(existing, attr, _safe_float(val))
                else:
                    setattr(existing, attr, _safe_int(val))

            count += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate nfl_player_game_logs from nflverse player_stats."
    )
    parser.add_argument(
        "--years", type=int, nargs="+",
        default=list(range(2011, 2025)),
        help="NFL seasons to ingest (default: 2011-2024).",
    )
    parser.add_argument(
        "--positions", type=str, nargs="+",
        default=list(_DEFAULT_POSITIONS),
        help="Positions to include (default: WR RB TE).",
    )
    parser.add_argument("--db", type=str, default=None)
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    positions = {p.upper() for p in args.positions}
    init_db(db_path)

    logger.info("Database: %s", db_path)
    logger.info("Years: %s", args.years)
    logger.info("Positions: %s", positions)

    df = fetch(args.years, positions)

    logger.info("Ingesting %d weekly rows into nfl_player_game_logs...", len(df))
    n = ingest(db_path, df)
    logger.info("Upserted %d game log rows.", n)
    logger.info("Done.")


if __name__ == "__main__":
    main()
