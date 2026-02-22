"""
Seed the scoring_formats table with standard fantasy formats and the
user's dynasty league settings.

Formats included:
  standard      Classic scoring: no PPR, -2 INT
  half_ppr      0.5 points per reception
  ppr           1 point per reception (full PPR)
  te_premium    Full PPR + 0.5 extra per TE reception (1.5 total for TEs)
  dynasty       User's custom dynasty league (see below)

Dynasty league settings (user-defined):
  Passing:   1 pt / 25 yds, 6 pts TD, 2 pts 2-pt, -3 INT
  Rushing:   1 pt / 10 yds, 6 pts TD, 2 pts 2-pt
  Receiving: 1 pt / reception (full PPR), 1 pt / 10 yds, 6 pts TD, 2 pts 2-pt
  TE bonus:  +1 pt per reception (2.0 pts total = TE premium)
  Misc:      -2 fumble lost, 6 pts special teams TD, 6 pts fumble recovery TD

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
    interception: float = -2,
    rush_yards_per_point: float = 10,
    rush_td: float = 6,
    rush_2pt: float = 2,
    reception: float = 1.0,
    rec_yards_per_point: float = 10,
    rec_td: float = 6,
    rec_2pt: float = 2,
    te_rec_bonus: float = 0.0,
    fumble_lost: float = -2,
    special_teams_td: float = 6,
    fumble_recovery_td: float = 6,
) -> str:
    return json.dumps({
        "pass_yards_per_point": pass_yards_per_point,
        "pass_td": pass_td,
        "pass_2pt": pass_2pt,
        "interception": interception,
        "rush_yards_per_point": rush_yards_per_point,
        "rush_td": rush_td,
        "rush_2pt": rush_2pt,
        "reception": reception,
        "rec_yards_per_point": rec_yards_per_point,
        "rec_td": rec_td,
        "rec_2pt": rec_2pt,
        "te_rec_bonus": te_rec_bonus,
        "fumble_lost": fumble_lost,
        "special_teams_td": special_teams_td,
        "fumble_recovery_td": fumble_recovery_td,
    })


FORMATS = [
    {
        "name": "standard",
        "description": (
            "Classic standard scoring. No reception points. "
            "Pass: 1/25 yds, 6 TD, -2 INT. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 1/10 yds, 6 TD. "
            "Fumble lost: -2."
        ),
        "rules": _rules(reception=0.0),
    },
    {
        "name": "half_ppr",
        "description": (
            "Half-PPR: 0.5 points per reception. "
            "Pass: 1/25 yds, 6 TD, -2 INT. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 0.5/rec, 1/10 yds, 6 TD. "
            "Fumble lost: -2."
        ),
        "rules": _rules(reception=0.5),
    },
    {
        "name": "ppr",
        "description": (
            "Full PPR: 1 point per reception. "
            "Pass: 1/25 yds, 6 TD, -2 INT. "
            "Rush: 1/10 yds, 6 TD. "
            "Rec: 1/rec, 1/10 yds, 6 TD. "
            "Fumble lost: -2."
        ),
        "rules": _rules(reception=1.0),
    },
    {
        "name": "te_premium",
        "description": (
            "Full PPR + 0.5 TE reception bonus (TEs earn 1.5 pts/rec). "
            "All other rules same as PPR. Fumble lost: -2."
        ),
        "rules": _rules(reception=1.0, te_rec_bonus=0.5),
    },
    {
        "name": "dynasty",
        "description": (
            "Custom dynasty league: "
            "Pass: 1/25 yds, 6 TD, 2 pts 2-pt, -3 INT. "
            "Rush: 1/10 yds, 6 TD, 2 pts 2-pt. "
            "Rec: 1/rec (PPR), 1/10 yds, 6 TD, 2 pts 2-pt. "
            "TE: +1 per reception (2.0 total = TE premium). "
            "Fumble lost: -2. Special teams TD: 6. Fumble recovery TD: 6."
        ),
        "rules": _rules(
            interception=-3,
            reception=1.0,
            te_rec_bonus=1.0,
        ),
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed scoring_formats table with standard and dynasty formats."
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
            print(f"\n{'ID':<4} {'Name':<16} Description")
            print("-" * 80)
            for r in rows:
                print(f"{r.id:<4} {r.name:<16} {r.description[:60] if r.description else ''}")
            print()
        return

    with get_session(db_path) as session:
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
                f"  {r.name:<16} rec={rules['reception']:.1f}  "
                f"te_bonus={rules['te_rec_bonus']:.1f}  "
                f"INT={rules['interception']}  "
                f"fumble={rules['fumble_lost']}"
            )


if __name__ == "__main__":
    main()
