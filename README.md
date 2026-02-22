# nfl-fantasy-db

Historical NFL player-season fantasy statistics and derived Best Two Seasons (B2S) training labels. One component of the three-repo fantasy football prospect modeling pipeline.

```
FF\
├── cfb-prospect-db\   ← college features
├── nfl-fantasy-db\    ← this repo (NFL outcomes / training labels)
└── prospect-model\    ← regression model (later)
```

## What's in here

| Table | Contents |
|---|---|
| `nfl_player_seasons_raw` | One row per player per NFL season (WR/RB/TE, 2011–2024) |
| `nfl_b2s` | Derived B2S training labels — the regression target |
| `nfl_big_board` | Consensus pre-draft rankings (future) |
| `cfb_link` | Fuzzy-matched link: NFL player name → cfb-prospect-db player ID |
| `qa_name_collisions` | Ambiguous name matches needing manual review |
| `qa_missing_draft` | Players with season data but no matched draft record |

## Setup

```bash
pip install -r requirements.txt

# Copy and fill in your .env
cp .env.example .env
# Set NFL_DB_PATH and CFB_DB_PATH in .env
```

## Pipeline (run in order)

```bash
# 1. Download and ingest nflverse player stats (WR/RB/TE, 2011–2024)
python scripts/populate_nfl_seasons.py

# 2. Fuzzy-match NFL players to cfb-prospect-db player IDs
#    (populates cfb_link and back-fills draft_year on season rows)
python scripts/link_to_cfb.py

# 3. Compute B2S training labels from linked season data
python scripts/compute_b2s.py

# 4. Run QA report to check data quality
python scripts/qa_report.py
```

## Refresh

```bash
# Check if nflverse has released updated data (exit 1 if stale)
python scripts/refresh.py --check

# Smart incremental refresh (only re-ingest if new data detected)
python scripts/refresh.py

# Force full re-ingest of everything
python scripts/refresh.py --full

# Refresh a single source
python scripts/refresh.py --source seasons --years 2024
python scripts/refresh.py --source b2s
```

## B2S definition

B2S (Best Two Seasons) is the regression target — the same metric used as a directional reference from ZAP:

- **WR / RB**: average PPR points/game of the player's best 2 seasons within their first 3 NFL years, where each season has ≥8 games played.
- **TE**: best single-season PPG within first 3 NFL years, ≥8 games.
- Players with no qualifying seasons receive `b2s_score = 0.0`.

"First 3 NFL years" is defined as `draft_year`, `draft_year+1`, `draft_year+2`.

## Python usage

```python
from ffnfl import NFLB2S, NFLPlayerSeason, CFBLink
from ffnfl.database import get_session
from config import get_db_path

db = get_db_path()

# Top WR B2S scores for the 2021 draft class
with get_session(db) as session:
    rows = (
        session.query(NFLB2S)
        .filter(NFLB2S.position == "WR", NFLB2S.draft_year == 2021)
        .order_by(NFLB2S.b2s_score.desc())
        .all()
    )
    for r in rows[:10]:
        print(f"{r.player_name}: {r.b2s_score:.1f} ppg")

# Raw season stats for a player
with get_session(db) as session:
    seasons = (
        session.query(NFLPlayerSeason)
        .filter(NFLPlayerSeason.player_name == "Justin Jefferson")
        .order_by(NFLPlayerSeason.season_year)
        .all()
    )
    for s in seasons:
        print(f"{s.season_year}: {s.games_played}g, {s.fantasy_ppg:.1f} ppg")

# Players linked to cfb-prospect-db
with get_session(db) as session:
    links = (
        session.query(CFBLink)
        .filter(CFBLink.match_score >= 90)
        .all()
    )
    print(f"{len(links)} high-confidence links")
```

## Script flags

### populate_nfl_seasons.py
```
--years 2011 2012 ...   NFL seasons to ingest (default: 2011–2024)
--positions WR RB TE    Positions to include (default: WR RB TE)
--db PATH               Override NFL_DB_PATH from .env
```

### link_to_cfb.py
```
--threshold 85          Minimum fuzzy match score (default: 75)
--dry-run               Report matches without writing to DB
--db / --cfb-db PATH    Override DB paths from .env
```

### compute_b2s.py
```
--min-draft-year 2011   Earliest draft class to compute (default: 2011)
--max-draft-year 2022   Latest draft class (default: 2022 — 3 full seasons by 2024)
--dry-run               Print scores without writing
```

### qa_report.py
```
--min-ppg 8.0           PPG threshold for highlighting missing players (default: 5.0)
--output collisions.csv Write collision table to CSV for manual review
```

## Data scope

- **Training window**: draft classes 2011–2022 (≥3 NFL seasons complete by end of 2024)
- **Positions**: WR, RB, TE
- **Estimated players**: ~1,200–1,500 total
- **Source**: [nflverse player_stats](https://github.com/nflverse/nflverse-data) — free, open-source

## Project structure

```
nfl-fantasy-db\
├── ffnfl\
│   ├── __init__.py        ORM model exports + documentation
│   └── database.py        SQLAlchemy ORM models + session helpers
├── scripts\
│   ├── populate_nfl_seasons.py   Ingest nflverse player stats
│   ├── link_to_cfb.py            Fuzzy-match to cfb-prospect-db
│   ├── compute_b2s.py            Derive B2S training labels
│   ├── qa_report.py              Data quality report
│   └── refresh.py                Master refresh orchestrator
├── config.py              .env loader (NFL_DB_PATH, CFB_DB_PATH)
├── requirements.txt
├── .env.example
└── .gitignore
```
