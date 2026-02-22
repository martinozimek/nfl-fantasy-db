"""
ffnfl — NFL Fantasy Outcomes Database
======================================

Stores historical NFL player-season fantasy statistics and derived
Best Two Seasons (B2S) training labels for use in the prospect-model.

Data source: nflverse player_stats CSV (free, open-source).
Covers: WR / RB / TE, draft classes 2011–2022, NFL seasons 2011–2024.

Quick-start
-----------
Run the pipeline in order:

    # 1. Ingest raw season stats from nflverse (downloads ~30 MB CSV)
    python scripts/populate_nfl_seasons.py

    # 2. Link NFL players to cfb-prospect-db IDs (fuzzy name matching)
    python scripts/link_to_cfb.py

    # 3. Compute B2S training labels (requires draft_year to be linked)
    python scripts/compute_b2s.py

    # 4. QA report — flag collisions, missing links, sanity checks
    python scripts/qa_report.py

    # Refresh any source at any time
    python scripts/refresh.py               # smart incremental refresh
    python scripts/refresh.py --check       # check for new nflverse data (exit 1 if stale)
    python scripts/refresh.py --full        # force re-ingest everything

Importing ORM models directly
------------------------------
    from ffnfl import NFLPlayerSeason, NFLB2S, CFBLink
    from ffnfl.database import get_session
    from config import get_db_path

    with get_session(get_db_path()) as session:
        rows = session.query(NFLB2S).filter(
            NFLB2S.position == "WR",
            NFLB2S.draft_year == 2021,
        ).order_by(NFLB2S.b2s_score.desc()).all()

        for r in rows:
            print(r.player_name, r.b2s_score)

B2S definition (matches ZAP model)
-----------------------------------
    WR / RB: average PPR points/game of the player's best 2 seasons
             within their first 3 NFL years, where each season has ≥8 games played.
    TE:      best single-season PPG within first 3 NFL years (≥8 games).

    "First 3 NFL years" = draft_year, draft_year+1, draft_year+2.
    Players with no qualifying seasons receive b2s_score = 0.0.

Database path
-------------
    Set NFL_DB_PATH in a .env file at the repo root (see .env.example).
    Set CFB_DB_PATH to point at your cfb-prospect-db file (used by link_to_cfb.py).
"""

from ffnfl.database import (
    NFLPlayerSeason,
    NFLB2S,
    NFLBigBoard,
    ScoringFormat,
    NFLPlayerGameLog,
    NFLPlayerSeasonScore,
    CFBLink,
    QANameCollision,
    QAMissingDraft,
)

__all__ = [
    # Raw season data
    "NFLPlayerSeason",
    # Derived training labels
    "NFLB2S",
    # Pre-draft market proxy
    "NFLBigBoard",
    # Scoring system
    "ScoringFormat",
    "NFLPlayerGameLog",
    "NFLPlayerSeasonScore",
    # Cross-database linking
    "CFBLink",
    # QA tables
    "QANameCollision",
    "QAMissingDraft",
]
