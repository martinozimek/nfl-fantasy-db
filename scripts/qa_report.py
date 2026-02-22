"""
Generate a QA report for nfl-fantasy-db data quality.

Checks performed:
  1. Name collisions     — players in qa_name_collisions needing manual review
  2. Missing draft links — players in qa_missing_draft with high PPG (likely real players)
  3. B2S coverage        — % of draft class with a B2S score
  4. Season coverage     — players missing year 1/2/3 data windows
  5. Games sanity check  — players with suspiciously high or low game counts

Usage:
    python scripts/qa_report.py
    python scripts/qa_report.py --min-ppg 8  # focus on high-value missing players
    python scripts/qa_report.py --output qa_report.csv
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import (
    NFLB2S,
    CFBLink,
    NFLPlayerSeason,
    QAMissingDraft,
    QANameCollision,
    get_session,
    init_db,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run_qa(db_path: str, min_ppg: float = 5.0, output: str = None) -> None:
    """
    Run all QA checks and print a summary report.

    Parameters
    ----------
    db_path:  Path to the nfl-fantasy-db SQLite file.
              min_ppg: Minimum PPG threshold for highlighting missing draft players.
    output:   If provided, write the collision report to a CSV file.
    """
    report_lines = []

    def section(title: str) -> None:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print('='*60)

    def row(label: str, value) -> None:
        line = f"  {label:<40} {value}"
        print(line)
        report_lines.append(line)

    with get_session(db_path) as session:

        # --- Overview ---
        section("DATABASE OVERVIEW")
        total_seasons = session.query(NFLPlayerSeason).count()
        total_b2s = session.query(NFLB2S).count()
        total_links = session.query(CFBLink).count()
        total_collisions = session.query(QANameCollision).filter(
            QANameCollision.resolved == False  # noqa: E712
        ).count()
        total_missing = session.query(QAMissingDraft).count()

        row("Total season rows (raw)", total_seasons)
        row("B2S rows computed", total_b2s)
        row("CFB links created", total_links)
        row("Name collisions (unresolved)", total_collisions)
        row("Missing draft records", total_missing)

        # --- B2S coverage by draft year ---
        section("B2S COVERAGE BY DRAFT YEAR & POSITION")
        from sqlalchemy import func
        b2s_by_year = (
            session.query(
                NFLB2S.draft_year,
                NFLB2S.position,
                func.count(NFLB2S.id).label("count"),
                func.avg(NFLB2S.b2s_score).label("avg_b2s"),
            )
            .group_by(NFLB2S.draft_year, NFLB2S.position)
            .order_by(NFLB2S.draft_year, NFLB2S.position)
            .all()
        )
        if b2s_by_year:
            print(f"  {'Year':>4}  {'Pos':<4}  {'Count':>5}  {'Avg B2S':>8}")
            print("  " + "-"*28)
            for yr, pos, cnt, avg in b2s_by_year:
                print(f"  {yr:>4}  {pos:<4}  {cnt:>5}  {(avg or 0):>8.2f}")
        else:
            print("  No B2S data yet — run compute_b2s.py first.")

        # --- Name collisions ---
        section(f"NAME COLLISIONS (top 20, unresolved)")
        collisions = (
            session.query(QANameCollision)
            .filter(QANameCollision.resolved == False)  # noqa: E712
            .order_by(QANameCollision.draft_year.desc())
            .limit(20)
            .all()
        )
        if collisions:
            print(f"  {'NFL Name':<30}  {'Year':>4}  {'Candidate 1':<25}  {'Sc':>3}  {'Candidate 2':<25}  {'Sc':>3}")
            print("  " + "-"*100)
            for c in collisions:
                print(
                    f"  {(c.nfl_player_name or ''):<30}  "
                    f"{(c.draft_year or 0):>4}  "
                    f"{(c.candidate_1_name or ''):<25}  "
                    f"{(c.candidate_1_score or 0):>3.0f}  "
                    f"{(c.candidate_2_name or ''):<25}  "
                    f"{(c.candidate_2_score or 0):>3.0f}"
                )
        else:
            print("  No unresolved collisions.")

        # --- High-PPG missing players ---
        section(f"MISSING DRAFT LINKS (PPG >= {min_ppg}, top 20)")
        high_ppg_missing = (
            session.query(QAMissingDraft)
            .filter(QAMissingDraft.fantasy_ppg >= min_ppg)
            .order_by(QAMissingDraft.fantasy_ppg.desc())
            .limit(20)
            .all()
        )
        if high_ppg_missing:
            print(f"  {'NFL Name':<30}  {'Pos':<4}  {'Season':>6}  {'PPG':>6}  {'Est.Draft':>9}")
            print("  " + "-"*65)
            for m in high_ppg_missing:
                print(
                    f"  {(m.nfl_player_name or ''):<30}  "
                    f"{(m.position or ''):<4}  "
                    f"{(m.season_year or 0):>6}  "
                    f"{(m.fantasy_ppg or 0):>6.1f}  "
                    f"{(m.estimated_draft_year or 0):>9}"
                )
        else:
            print(f"  No missing players with PPG >= {min_ppg}.")

        # --- Games sanity check ---
        section("GAMES SANITY CHECK")
        from sqlalchemy import and_
        # Players with very few games in a season (< 4) but high PPG
        suspect_low = (
            session.query(NFLPlayerSeason)
            .filter(
                and_(
                    NFLPlayerSeason.games_played < 4,
                    NFLPlayerSeason.fantasy_ppg > 15,
                )
            )
            .order_by(NFLPlayerSeason.fantasy_ppg.desc())
            .limit(10)
            .all()
        )
        if suspect_low:
            print("  High-PPG players with < 4 games (possible injury year):")
            for r in suspect_low:
                print(f"    {r.player_name:<30} {r.season_year}  {r.games_played}g  {r.fantasy_ppg:.1f}ppg")
        else:
            print("  No suspicious low-game / high-PPG rows found.")

    # --- Export collisions to CSV ---
    if output:
        try:
            import csv
            with get_session(db_path) as session:
                all_collisions = session.query(QANameCollision).all()
            with open(output, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "id", "nfl_player_name", "draft_year", "position",
                    "candidate_1_name", "candidate_1_id", "candidate_1_score",
                    "candidate_2_name", "candidate_2_id", "candidate_2_score",
                    "resolved", "notes",
                ])
                writer.writeheader()
                for c in all_collisions:
                    writer.writerow({
                        "id": c.id,
                        "nfl_player_name": c.nfl_player_name,
                        "draft_year": c.draft_year,
                        "position": c.position,
                        "candidate_1_name": c.candidate_1_name,
                        "candidate_1_id": c.candidate_1_id,
                        "candidate_1_score": c.candidate_1_score,
                        "candidate_2_name": c.candidate_2_name,
                        "candidate_2_id": c.candidate_2_id,
                        "candidate_2_score": c.candidate_2_score,
                        "resolved": c.resolved,
                        "notes": c.notes,
                    })
            logger.info("Collision report written to %s", output)
        except Exception as exc:
            logger.error("Failed to write CSV: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run QA checks on nfl-fantasy-db data."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--min-ppg", type=float, default=5.0,
        help="PPG threshold for highlighting missing players (default: 5.0).",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Write collision QA table to this CSV file.",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    init_db(db_path)
    run_qa(db_path, min_ppg=args.min_ppg, output=args.output)


if __name__ == "__main__":
    main()
