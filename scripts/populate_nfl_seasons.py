"""
Populate nfl_player_seasons_raw from nflverse player_stats CSV.

Source:
    https://github.com/nflverse/nflverse-data/releases/download/player_stats/player_stats.csv

The nflverse player_stats CSV contains one row per player per week. This script
aggregates to season-level totals (one row per player per season) for the years
and positions of interest.

Usage:
    python scripts/populate_nfl_seasons.py                    # default: 2011-2024
    python scripts/populate_nfl_seasons.py --years 2022 2023  # specific years
    python scripts/populate_nfl_seasons.py --positions WR RB  # specific positions

All operations are idempotent (safe to re-run; existing rows are updated).
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import NFLPlayerSeason, get_session, init_db

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

# Minimum games to bother storing a season row (avoids noise from injured
# players with 1-2 game cameos that will never qualify for B2S)
_MIN_GAMES = 1


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


def fetch_and_aggregate(
    years: list[int],
    positions: set[str],
) -> pd.DataFrame:
    """
    Download the nflverse player_stats CSV and aggregate weekly rows to
    season-level totals for the requested years and positions.

    Returns a DataFrame with one row per (player_name, season_year, position, team).
    """
    logger.info("Downloading nflverse player_stats from GitHub...")
    df = pd.read_csv(_PLAYER_STATS_URL, low_memory=False)
    logger.info("  Loaded %d weekly rows.", len(df))

    # Filter to skill positions and season type = REG (regular season)
    # nflverse uses season_type column: REG, POST, PRE
    df = df[df["season_type"] == "REG"].copy()
    df = df[df["position"].isin(positions)].copy()
    df = df[df["season"].isin(years)].copy()
    logger.info("  After filters: %d rows for years %s, positions %s",
                len(df), years, positions)

    if df.empty:
        return pd.DataFrame()

    # Aggregate weekly to season totals
    agg_cols = {
        "targets": "sum",
        "receptions": "sum",
        "receiving_yards": "sum",
        "receiving_tds": "sum",
        "carries": "sum",
        "rushing_yards": "sum",
        "rushing_tds": "sum",
        "fantasy_points_ppr": "sum",
    }
    # games = count of weeks with at least 1 snap (approximate)
    # nflverse doesn't have a direct games column at row level; proxy = count of non-null weeks
    df["_game"] = 1  # each weekly row is one game appearance

    agg = df.groupby(
        ["player_display_name", "player_id", "position", "recent_team", "season"],
        as_index=False,
    ).agg({**agg_cols, "_game": "sum"})

    agg = agg.rename(columns={
        "player_display_name": "player_name",
        "player_id": "nflverse_id",
        "recent_team": "nfl_team",
        "season": "season_year",
        "_game": "games_played",
        "receiving_yards": "rec_yards",
        "receiving_tds": "rec_tds",
        "carries": "rush_attempts",
        "rushing_yards": "rush_yards",
        "rushing_tds": "rush_tds",
    })

    # PPG (only for games played)
    agg["fantasy_ppg"] = (
        agg["fantasy_points_ppr"] / agg["games_played"]
    ).where(agg["games_played"] > 0).round(3)

    logger.info("  Aggregated to %d season rows.", len(agg))
    return agg


def ingest(db_path: str, df: pd.DataFrame) -> int:
    """
    Upsert season rows into nfl_player_seasons_raw.
    Returns the number of rows upserted.
    """
    if df.empty:
        return 0

    count = 0
    with get_session(db_path) as session:
        for _, row in df.iterrows():
            existing = (
                session.query(NFLPlayerSeason)
                .filter(
                    NFLPlayerSeason.player_name == row["player_name"],
                    NFLPlayerSeason.season_year == int(row["season_year"]),
                    NFLPlayerSeason.nfl_team == row["nfl_team"],
                    NFLPlayerSeason.position == row["position"],
                )
                .first()
            )
            if existing is None:
                existing = NFLPlayerSeason(
                    player_name=row["player_name"],
                    season_year=int(row["season_year"]),
                    nfl_team=row["nfl_team"],
                    position=row["position"],
                )
                session.add(existing)

            existing.nflverse_id = row.get("nflverse_id") or None
            existing.games_played = _safe_int(row.get("games_played"))
            existing.targets = _safe_int(row.get("targets"))
            existing.receptions = _safe_int(row.get("receptions"))
            existing.rec_yards = _safe_int(row.get("rec_yards"))
            existing.rec_tds = _safe_int(row.get("rec_tds"))
            existing.rush_attempts = _safe_int(row.get("rush_attempts"))
            existing.rush_yards = _safe_int(row.get("rush_yards"))
            existing.rush_tds = _safe_int(row.get("rush_tds"))
            existing.fantasy_points_ppr = _safe_float(row.get("fantasy_points_ppr"))
            existing.fantasy_ppg = _safe_float(row.get("fantasy_ppg"))
            count += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate nfl_player_seasons_raw from nflverse player_stats."
    )
    parser.add_argument(
        "--years", type=int, nargs="+",
        default=list(range(2011, 2025)),
        help="NFL seasons to ingest (default: 2011-2024). "
             "Use draft classes 2011-2022 for training data with 3 full seasons by 2024.",
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

    df = fetch_and_aggregate(years=args.years, positions=positions)
    n = ingest(db_path, df)
    logger.info("Upserted %d season rows into nfl_player_seasons_raw.", n)
    logger.info("Done.")


if __name__ == "__main__":
    main()
