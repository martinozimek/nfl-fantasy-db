"""
Seed the scoring_formats table with standard fantasy formats.

Formats are named and binned by the two settings that actually vary across
leagues: reception points and TE reception bonus. INT and fumble penalties
are NOT stored in formats — they are tracked as raw counts in
NFLPlayerSeasonScore so each user can apply their own multiplier.

Formats included:
  standard        No reception points, no TE bonus
  half_ppr        0.5 points per reception
  ppr             1 point per reception (full PPR)
  te_premium_0.5  Full PPR + 0.5 extra per TE reception (1.5 total for TEs)
  te_premium_1.0  Full PPR + 1.0 extra per TE reception (2.0 total for TEs)

All other scoring axes are fixed at standard values everywhere:
  Pass: 1 pt / 25 yds, 6 pts TD, 2 pts 2-pt
  Rush: 1 pt / 10 yds, 6 pts TD, 2 pts 2-pt
  Rec:  1 pt / 10 yds, 6 pts TD, 2 pts 2-pt
  Special teams TD: 6 pts

Usage:
    python scripts/setup_scoring_formats.py
    python scripts/setup_scoring_formats.py --list   # print current formats
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import ScoringFormat, get_session, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Format definitions
# ---------------------------------------------------------------------------

def _rules(
    *,
    pass_yards_per_point: float = 25,
    pass_td: float = 6,
    pass_2pt: float = 2,
    rush_yards_per_point: float = 10,
    rush_td: float = 6,
    rush_2pt: float = 2,
    reception: float = 1.0,
    rec_yards_per_point: float = 10,
    rec_td: float = 6,
    rec_2pt: float = 2,
    te_rec_bonus: float = 0.0,
    special_teams_td: float = 6,
) -> str:
    """
    Build a scoring rules JSON string.

    INT and fumble-lost penalties are intentionally absent — they are stored
    as raw counts in NFLPlayerSeasonScore for league-specific adjustment.
    """
    return json.dumps({
        "pass_yards_per_point": pass_yards_per_point,
        "pass_td":              pass_td,
        "pass_2pt":             pass_2pt,
        "rush_yards_per_point": rush_yards_per_point,
        "rush_td":              rush_td,
        "rush_2pt":             rush_2pt,
        "reception":            reception,
        "rec_yards_per_point":  rec_yards_per_point,
        "rec_td":               rec_td,
        "rec_2pt":              rec_2pt,
        "te_rec_bonus":         te_rec_bonus,
        "special_teams_td":     special_teams_td,
    })


FORMATS = [
    {
        "name": "standard",
        "description": (
            "Classic standard scoring. No reception points, no TE bonus. "
            "Pass: 1/25 yds, 6 TD. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 1/10 yds, 6 TD. "
            "INT and fumble penalties applied at query time."
        ),
        "rules": _rules(reception=0.0),
    },
    {
        "name": "half_ppr",
        "description": (
            "Half-PPR: 0.5 points per reception, no TE bonus. "
            "Pass: 1/25 yds, 6 TD. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 0.5/rec, 1/10 yds, 6 TD. "
            "INT and fumble penalties applied at query time."
        ),
        "rules": _rules(reception=0.5),
    },
    {
        "name": "ppr",
        "description": (
            "Full PPR: 1 point per reception, no TE bonus. "
            "Pass: 1/25 yds, 6 TD. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 1/rec, 1/10 yds, 6 TD. "
            "INT and fumble penalties applied at query time."
        ),
        "rules": _rules(reception=1.0),
    },
    {
        "name": "te_premium_0.5",
        "description": (
            "Full PPR + 0.5 TE reception bonus (TEs earn 1.5 pts/rec). "
            "All other rules same as PPR. "
            "INT and fumble penalties applied at query time."
        ),
        "rules": _rules(reception=1.0, te_rec_bonus=0.5),
    },
    {
        "name": "te_premium_1.0",
        "description": (
            "Full PPR + 1.0 TE reception bonus (TEs earn 2.0 pts/rec). "
            "All other rules same as PPR. "
            "INT and fumble penalties applied at query time."
        ),
        "rules": _rules(reception=1.0, te_rec_bonus=1.0),
    },
]

# Old format names that should be removed if they exist (renamed in redesign)
_OBSOLETE_NAMES = {"te_premium", "dynasty"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed scoring_formats table with standard and TE-premium formats."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--list", action="store_true",
        help="Print current formats in DB and exit.",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    init_db(db_path)

    if args.list:
        with get_session(db_path) as session:
            rows = session.query(ScoringFormat).order_by(ScoringFormat.id).all()
        if not rows:
            print("No scoring formats in DB. Run without --list to seed.")
        else:
            print(f"\n{'ID':<4} {'Name':<20} Description")
            print("-" * 80)
            for r in rows:
                print(f"{r.id:<4} {r.name:<20} {r.description[:60] if r.description else ''}")
            print()
        return

    with get_session(db_path) as session:
        # Remove obsolete format names from previous schema versions
        removed = 0
        for old_name in _OBSOLETE_NAMES:
            old = session.query(ScoringFormat).filter(ScoringFormat.name == old_name).first()
            if old:
                session.delete(old)
                removed += 1
                logger.info("  Removed obsolete format: %s", old_name)
        if removed:
            logger.info("  Removed %d obsolete format(s).", removed)

        # Upsert current formats
        for fmt in FORMATS:
            existing = (
                session.query(ScoringFormat)
                .filter(ScoringFormat.name == fmt["name"])
                .first()
            )
            if existing is None:
                existing = ScoringFormat(name=fmt["name"])
                session.add(existing)
                logger.info("  Created format: %s", fmt["name"])
            else:
                logger.info("  Updated format: %s", fmt["name"])
            existing.description = fmt["description"]
            existing.rules = fmt["rules"]

    logger.info("Seeded %d scoring formats.", len(FORMATS))

    # Print summary
    with get_session(db_path) as session:
        for r in session.query(ScoringFormat).order_by(ScoringFormat.id).all():
            rules = r.parse_rules()
            print(
                f"  {r.name:<20} rec={rules['reception']:.1f}  "
                f"te_bonus={rules['te_rec_bonus']:.1f}"
            )


if __name__ == "__main__":
    main()
