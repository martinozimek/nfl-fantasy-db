"""
Compute Best Two Seasons (B2S) training labels from nfl_player_seasons_raw.

B2S definition (matches ZAP model):
  WR / RB: average PPR points/game of the player's best 2 seasons within
           their first 3 NFL years, where each season has ≥8 games played.
  TE:      best single-season PPG within first 3 NFL years, ≥8 games.

"First 3 NFL years" is defined relative to draft_year:
  year 1 = draft_year season, year 2 = draft_year+1, year 3 = draft_year+2.

Players whose draft_year has not been populated (via link_to_cfb.py) are
skipped. Players with zero qualifying seasons receive b2s_score = 0.0.

Usage:
    python scripts/compute_b2s.py           # default: all players with draft_year set
    python scripts/compute_b2s.py --dry-run # print results without writing to DB
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import NFLB2S, NFLPlayerSeason, get_session, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Minimum games to count a season as "qualifying" for B2S
_MIN_GAMES = 8

# TE uses only best single season; WR/RB use best-2 average
_TE_POSITIONS = {"TE"}
_WR_RB_POSITIONS = {"WR", "RB"}


def compute_b2s_for_player(
    player_name: str,
    position: str,
    draft_year: int,
    sessions_data: list[dict],
) -> dict:
    """
    Compute B2S metrics for a single player.

    Parameters
    ----------
    player_name:   Player name string.
    position:      'WR', 'RB', or 'TE'.
    draft_year:    NFL draft year (first NFL season is draft_year).
    sessions_data: List of dicts with keys: season_year, games_played, fantasy_ppg.

    Returns
    -------
    dict with b2s_score, top_season_ppg, year1_ppg, year2_ppg, year3_ppg,
    qualifying_seasons.
    """
    # Restrict to first 3 NFL years
    window = {draft_year, draft_year + 1, draft_year + 2}
    year_ppg: dict[int, float] = {}
    for s in sessions_data:
        yr = s.get("season_year")
        games = s.get("games_played") or 0
        ppg = s.get("fantasy_ppg") or 0.0
        if yr in window and games >= _MIN_GAMES and ppg > 0:
            year_ppg[yr] = ppg

    qualifying = sorted(year_ppg.values(), reverse=True)
    qualifying_count = len(qualifying)

    year1 = year_ppg.get(draft_year)
    year2 = year_ppg.get(draft_year + 1)
    year3 = year_ppg.get(draft_year + 2)
    top_ppg = qualifying[0] if qualifying else None

    if position in _TE_POSITIONS:
        # TE: best single season
        b2s = top_ppg
    else:
        # WR/RB: average of best 2
        if len(qualifying) >= 2:
            b2s = round((qualifying[0] + qualifying[1]) / 2, 3)
        elif len(qualifying) == 1:
            b2s = qualifying[0]  # only 1 qualifying season — use it
        else:
            b2s = 0.0

    return {
        "player_name": player_name,
        "position": position,
        "draft_year": draft_year,
        "b2s_score": round(b2s, 3) if b2s else 0.0,
        "top_season_ppg": round(top_ppg, 3) if top_ppg else None,
        "year1_ppg": round(year1, 3) if year1 else None,
        "year2_ppg": round(year2, 3) if year2 else None,
        "year3_ppg": round(year3, 3) if year3 else None,
        "qualifying_seasons": qualifying_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute B2S training labels from nfl_player_seasons_raw."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print computed B2S scores without writing to DB.",
    )
    parser.add_argument(
        "--min-draft-year", type=int, default=2011,
        help="Earliest draft year to compute (default: 2011).",
    )
    parser.add_argument(
        "--max-draft-year", type=int, default=2022,
        help="Latest draft year to compute (default: 2022 — ensures 3 full seasons by 2024).",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    init_db(db_path)

    logger.info("Computing B2S for draft classes %d-%d...",
                args.min_draft_year, args.max_draft_year)

    with get_session(db_path) as session:
        # Load all rows that have a draft_year set
        rows = (
            session.query(NFLPlayerSeason)
            .filter(
                NFLPlayerSeason.draft_year.isnot(None),
                NFLPlayerSeason.draft_year >= args.min_draft_year,
                NFLPlayerSeason.draft_year <= args.max_draft_year,
            )
            .all()
        )

    # Group by (player_name, position, draft_year)
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        key = (r.player_name, r.position, r.draft_year)
        groups[key].append({
            "season_year": r.season_year,
            "games_played": r.games_played,
            "fantasy_ppg": r.fantasy_ppg,
        })

    results = []
    for (name, pos, dy), seasons in groups.items():
        result = compute_b2s_for_player(name, pos, dy, seasons)
        results.append(result)

    results.sort(key=lambda x: (x["draft_year"], -(x["b2s_score"] or 0)))

    if args.dry_run:
        print(f"\n{'Name':<30} {'Pos':<4} {'Draft':>5} {'B2S':>6} {'Y1':>5} {'Y2':>5} {'Y3':>5}")
        print("-" * 65)
        for r in results[:50]:
            print(
                f"{r['player_name']:<30} {r['position']:<4} "
                f"{r['draft_year']:>5} {r['b2s_score']:>6.2f} "
                f"{(r['year1_ppg'] or 0):>5.1f} "
                f"{(r['year2_ppg'] or 0):>5.1f} "
                f"{(r['year3_ppg'] or 0):>5.1f}"
            )
        logger.info("Dry run: %d B2S rows computed (not written).", len(results))
        return

    # Write to DB
    with get_session(db_path) as session:
        for r in results:
            existing = (
                session.query(NFLB2S)
                .filter(
                    NFLB2S.player_name == r["player_name"],
                    NFLB2S.position == r["position"],
                    NFLB2S.draft_year == r["draft_year"],
                )
                .first()
            )
            if existing is None:
                existing = NFLB2S(
                    player_name=r["player_name"],
                    position=r["position"],
                    draft_year=r["draft_year"],
                )
                session.add(existing)

            existing.b2s_score = r["b2s_score"]
            existing.top_season_ppg = r["top_season_ppg"]
            existing.year1_ppg = r["year1_ppg"]
            existing.year2_ppg = r["year2_ppg"]
            existing.year3_ppg = r["year3_ppg"]
            existing.qualifying_seasons = r["qualifying_seasons"]

    logger.info("Upserted %d B2S rows.", len(results))
    logger.info("Done.")


if __name__ == "__main__":
    main()
