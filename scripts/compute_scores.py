"""
Compute fantasy scores by applying scoring formats to game log data.

Reads from nfl_player_game_logs, applies each scoring format's rules,
and writes season-level totals to nfl_player_season_scores.

The result gives one row per (player, season, format) with:
    games_played       — weeks with a game log row
    fantasy_points_total — sum of weekly fantasy points under this format
    fantasy_ppg          — total / games_played

This is the authoritative source of per-format season scores. The
nfl_player_seasons_raw.fantasy_ppg column stores nflverse PPR only.
Use nfl_player_season_scores for custom formats (including dynasty).

Usage:
    python scripts/compute_scores.py
    python scripts/compute_scores.py --formats ppr dynasty
    python scripts/compute_scores.py --years 2021 2022 2023 2024
    python scripts/compute_scores.py --dry-run    # print top players, no writes

Dependencies:
    Run in order:
        1. populate_game_logs.py    (populates nfl_player_game_logs)
        2. setup_scoring_formats.py (populates scoring_formats)
        3. compute_scores.py        (this script)
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import (
    NFLPlayerGameLog,
    NFLPlayerSeasonScore,
    ScoringFormat,
    get_session,
    init_db,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _game_log_to_stats(log: NFLPlayerGameLog) -> dict:
    """Convert an ORM game log row to a plain dict for scoring."""
    return {
        "pass_yards":             log.pass_yards,
        "pass_tds":               log.pass_tds,
        "pass_2pt":               log.pass_2pt,
        "rush_yards":             log.rush_yards,
        "rush_tds":               log.rush_tds,
        "rush_2pt":               log.rush_2pt,
        "receptions":             log.receptions,
        "rec_yards":              log.rec_yards,
        "rec_tds":                log.rec_tds,
        "rec_2pt":                log.rec_2pt,
        "special_teams_tds":      log.special_teams_tds,
    }


def _game_log_to_penalty_counts(log: NFLPlayerGameLog) -> dict:
    """
    Extract raw penalty counts from a game log row.

    These are NOT included in scoring format computation — they are stored
    separately so users can apply their own league multipliers at query time.
    """
    return {
        "fumbles_lost": (
            (log.sack_fumbles_lost or 0)
            + (log.rushing_fumbles_lost or 0)
            + (log.receiving_fumbles_lost or 0)
        ),
        "interceptions_thrown": (log.interceptions or 0),
    }


def compute(
    db_path: str,
    format_names: Optional[list[str]],
    years: Optional[list[int]],
    dry_run: bool,
) -> None:
    # Load scoring formats
    with get_session(db_path) as session:
        fmt_query = session.query(ScoringFormat)
        if format_names:
            fmt_query = fmt_query.filter(ScoringFormat.name.in_(format_names))
        formats = [
            {"id": f.id, "name": f.name, "rules": f.parse_rules()}
            for f in fmt_query.all()
        ]

    if not formats:
        logger.error(
            "No scoring formats found. Run setup_scoring_formats.py first."
        )
        return

    logger.info("Scoring formats: %s", [f["name"] for f in formats])

    # Load game logs into memory (group by player+season)
    # Structure: {(nflverse_id, season_year): [game_log_dicts]}
    game_groups: dict[tuple, list[dict]] = defaultdict(list)
    player_meta: dict[tuple, dict] = {}   # first seen metadata per key
    penalty_totals: dict[tuple, dict] = {}  # {key: {fumbles_lost, interceptions_thrown}}

    with get_session(db_path) as session:
        log_query = session.query(NFLPlayerGameLog)
        if years:
            log_query = log_query.filter(NFLPlayerGameLog.season_year.in_(years))
        for log in log_query.all():
            key = (log.nflverse_id, log.season_year)
            game_groups[key].append(_game_log_to_stats(log))
            # Accumulate penalty counts (format-independent)
            counts = _game_log_to_penalty_counts(log)
            if key not in penalty_totals:
                penalty_totals[key] = {"fumbles_lost": 0, "interceptions_thrown": 0}
            penalty_totals[key]["fumbles_lost"] += counts["fumbles_lost"]
            penalty_totals[key]["interceptions_thrown"] += counts["interceptions_thrown"]
            if key not in player_meta:
                player_meta[key] = {
                    "player_name": log.player_name,
                    "position": log.position,
                    "nfl_team": log.nfl_team,
                }

    logger.info(
        "Loaded %d (player, season) groups from game logs.",
        len(game_groups),
    )

    # Compute season scores per format
    for fmt in formats:
        fmt_name = fmt["name"]
        rules = fmt["rules"]
        fmt_id = fmt["id"]

        rows_out = []
        for (nflverse_id, season_year), game_logs in game_groups.items():
            meta = player_meta[(nflverse_id, season_year)]
            position = meta["position"] or ""

            total_pts = sum(
                ScoringFormat.compute_points(g, rules, position)
                for g in game_logs
            )
            games = len(game_logs)
            ppg = round(total_pts / games, 3) if games > 0 else 0.0

            penalties = penalty_totals.get((nflverse_id, season_year), {})
            rows_out.append({
                "nflverse_id": nflverse_id,
                "season_year": season_year,
                "player_name": meta["player_name"],
                "position": position,
                "nfl_team": meta["nfl_team"],
                "format_id": fmt_id,
                "format_name": fmt_name,
                "games_played": games,
                "fantasy_points_total": round(total_pts, 3),
                "fantasy_ppg": ppg,
                "fumbles_lost": penalties.get("fumbles_lost", 0),
                "interceptions_thrown": penalties.get("interceptions_thrown", 0),
            })

        logger.info(
            "  [%s] Computed %d player-seasons.",
            fmt_name, len(rows_out),
        )

        if dry_run:
            # Print top 15 by PPG for each format
            top = sorted(rows_out, key=lambda r: -r["fantasy_ppg"])[:15]
            print(f"\n  Top 15 PPG — {fmt_name}:")
            for i, r in enumerate(top, 1):
                print(
                    f"    {i:>2}. {r['player_name']:<28} {r['position']:<3} "
                    f"{r['season_year']}  {r['fantasy_ppg']:>6.2f} ppg  "
                    f"({r['games_played']} games)"
                )
            continue

        # Write to DB
        with get_session(db_path) as session:
            for r in rows_out:
                existing = (
                    session.query(NFLPlayerSeasonScore)
                    .filter(
                        NFLPlayerSeasonScore.nflverse_id == r["nflverse_id"],
                        NFLPlayerSeasonScore.season_year == r["season_year"],
                        NFLPlayerSeasonScore.format_id == r["format_id"],
                    )
                    .first()
                )
                if existing is None:
                    existing = NFLPlayerSeasonScore(
                        nflverse_id=r["nflverse_id"],
                        season_year=r["season_year"],
                        format_id=r["format_id"],
                    )
                    session.add(existing)
                existing.player_name = r["player_name"]
                existing.position = r["position"]
                existing.nfl_team = r["nfl_team"]
                existing.format_name = r["format_name"]
                existing.games_played = r["games_played"]
                existing.fantasy_points_total = r["fantasy_points_total"]
                existing.fantasy_ppg = r["fantasy_ppg"]
                existing.fumbles_lost = r["fumbles_lost"]
                existing.interceptions_thrown = r["interceptions_thrown"]

        logger.info("  [%s] Wrote %d rows.", fmt_name, len(rows_out))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute season fantasy scores per scoring format."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--formats", type=str, nargs="+",
        help="Specific format names to compute (default: all formats in DB).",
    )
    parser.add_argument(
        "--years", type=int, nargs="+",
        help="Specific seasons to compute (default: all years in game_logs).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print top players per format without writing to DB.",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    init_db(db_path)

    compute(
        db_path=db_path,
        format_names=args.formats,
        years=args.years,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        logger.info("Done.")


if __name__ == "__main__":
    main()
