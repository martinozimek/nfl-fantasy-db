"""
SQLAlchemy ORM models for the nfl-fantasy-db.

Tables
------
nfl_player_seasons_raw
    Raw per-season NFL stats ingested from nflverse player_stats CSV.
    One row per (player_name, season_year, nfl_team, position).

nfl_b2s
    Derived Best Two Seasons (B2S) labels.
    One row per (player_name, position, draft_year).
    WR/RB: average PPR points/game of best 2 seasons (≥8 games each).
    TE:    best single season PPG (≥8 games).

nfl_big_board
    Consensus pre-draft big board rankings.
    One row per (player_name, draft_year).

scoring_formats
    Named fantasy scoring templates (PPR, half-PPR, standard, custom).
    Rules stored as a JSON string. See ScoringFormat.make_rules().

nfl_player_game_logs
    Weekly (per-game) raw counting stats from nflverse player_stats.
    Includes all fields needed to compute custom scoring formats:
    passing, rushing, receiving, 2-pt conversions, fumbles, special teams TDs.

nfl_player_season_scores
    Computed season fantasy points per (player, season, scoring format).
    Derived from nfl_player_game_logs by scripts/compute_scores.py.

cfb_link
    Fuzzy-matched link from NFL player name → cfb-prospect-db player_id.
    Enables joining NFL outcomes back to college features.

qa_name_collisions
    QA: players whose NFL name matched multiple cfb-prospect-db records.

qa_missing_draft
    QA: players with NFL seasons but no draft record matched.
"""

import json
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class NFLPlayerSeason(Base):
    """
    Raw per-season NFL statistics for a single player.
    Source: nflverse player_stats CSV (fantasy_points_ppr aggregated by season).
    """

    __tablename__ = "nfl_player_seasons_raw"
    __table_args__ = (
        UniqueConstraint(
            "player_name", "season_year", "nfl_team", "position",
            name="uq_nfl_player_season",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identity
    player_name = Column(String, nullable=False)
    nflverse_id = Column(String)          # nflverse gsis_id (e.g. "00-0036335")
    position = Column(String)             # WR, RB, TE
    nfl_team = Column(String)
    season_year = Column(Integer, nullable=False)

    # Counting stats
    games_played = Column(Integer)
    targets = Column(Integer)
    receptions = Column(Integer)
    rec_yards = Column(Integer)
    rec_tds = Column(Integer)
    rush_attempts = Column(Integer)
    rush_yards = Column(Integer)
    rush_tds = Column(Integer)

    # Fantasy
    fantasy_points_ppr = Column(Float)    # total season PPR points
    fantasy_ppg = Column(Float)           # PPR points per game

    # Derived / linking
    draft_year = Column(Integer)          # populated by link_to_cfb.py
    source = Column(String, default="nflverse")

    def __repr__(self) -> str:
        return (
            f"<NFLPlayerSeason {self.player_name!r} {self.season_year} "
            f"{self.nfl_team} {self.fantasy_ppg:.1f}ppg>"
        )


class NFLB2S(Base):
    """
    Derived Best Two Seasons (B2S) training labels.

    B2S definition (matches ZAP):
      WR/RB: average PPR points/game of the player's best 2 seasons
             within their first 3 NFL years, ≥8 games per season.
      TE:    best single season PPG (first 3 NFL years, ≥8 games).

    Players with fewer than 1 qualifying season score 0 on the B2S metric.
    """

    __tablename__ = "nfl_b2s"
    __table_args__ = (
        UniqueConstraint("player_name", "position", "draft_year", name="uq_b2s"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    player_name = Column(String, nullable=False)
    position = Column(String)             # WR, RB, TE
    draft_year = Column(Integer)

    # B2S scores
    b2s_score = Column(Float)            # WR/RB: avg of best-2 PPG; TE: best-1 PPG
    top_season_ppg = Column(Float)       # best single-season PPG (all positions)
    year1_ppg = Column(Float)            # first NFL season PPG (transparency)
    year2_ppg = Column(Float)            # second NFL season PPG
    year3_ppg = Column(Float)            # third NFL season PPG
    qualifying_seasons = Column(Integer) # how many seasons met ≥8 game threshold

    def __repr__(self) -> str:
        return (
            f"<NFLB2S {self.player_name!r} {self.draft_year} "
            f"{self.position} b2s={self.b2s_score}>"
        )


class NFLBigBoard(Base):
    """
    Consensus pre-draft big board ranking for a prospect class.
    Source: NFL Mock Draft Database (or similar consensus aggregator).
    """

    __tablename__ = "nfl_big_board"
    __table_args__ = (
        UniqueConstraint("player_name", "draft_year", "source", name="uq_bigboard"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    player_name = Column(String, nullable=False)
    draft_year = Column(Integer, nullable=False)
    position = Column(String)
    consensus_rank = Column(Integer)      # overall consensus rank (1 = top prospect)
    position_rank = Column(Integer)       # rank within position group
    source = Column(String)              # e.g. "nflmockdraftdatabase"
    notes = Column(Text)

    def __repr__(self) -> str:
        return (
            f"<NFLBigBoard {self.player_name!r} {self.draft_year} "
            f"rank={self.consensus_rank}>"
        )


class CFBLink(Base):
    """
    Fuzzy-matched link from NFL player name → cfb-prospect-db player ID.

    Created by scripts/link_to_cfb.py. Used to join NFL outcomes back to
    college feature data for regression training.
    """

    __tablename__ = "cfb_link"
    __table_args__ = (
        # Player plays one position — unique per (name, position), not by draft_year
        # (draft_year may be None initially and updated later without creating duplicates)
        UniqueConstraint("nfl_player_name", "position", name="uq_cfb_link"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    nfl_player_name = Column(String, nullable=False)
    draft_year = Column(Integer)
    position = Column(String)

    cfb_player_id = Column(Integer)       # Player.id in cfb-prospect-db
    cfb_full_name = Column(String)        # matched name in cfb-prospect-db
    match_score = Column(Float)           # rapidfuzz WRatio score (0–100)
    match_method = Column(String)         # "exact", "fuzzy", "manual"
    verified = Column(Boolean, default=False)  # manually verified flag

    def __repr__(self) -> str:
        return (
            f"<CFBLink {self.nfl_player_name!r} → "
            f"{self.cfb_full_name!r} score={self.match_score}>"
        )


class ScoringFormat(Base):
    """
    Named fantasy scoring format. Formats are binned by the two settings that
    actually vary across leagues: reception points and TE reception bonus.

    Rules JSON keys (all numeric):

      Scoring axes (vary by league — defines the format name):
        reception             points per reception (0=std, 0.5=half-PPR, 1.0=PPR)
        te_rec_bonus          extra points per TE reception on top of reception
                              (0=none, 0.5=te_premium_0.5, 1.0=te_premium_1.0)

      Standard everywhere (fixed across all typical leagues):
        pass_yards_per_point  yards per 1 pt (default 25)
        pass_td               points per passing TD (default 6)
        pass_2pt              points per passing 2-pt conversion (default 2)
        rush_yards_per_point  yards per 1 pt (default 10)
        rush_td               points per rushing TD (default 6)
        rush_2pt              points per rushing 2-pt conversion (default 2)
        rec_yards_per_point   yards per 1 pt (default 10)
        rec_td                points per receiving TD (default 6)
        rec_2pt               points per receiving 2-pt conversion (default 2)
        special_teams_td      points per special teams TD (default 6)

    INT and fumble-lost penalties are NOT embedded in formats because they
    vary too much by league. Instead, raw counts are stored per season in
    NFLPlayerSeasonScore.fumbles_lost and .interceptions_thrown so each
    user can apply their own multiplier at query time:

        adjusted_ppg = fantasy_ppg
                       + fumbles_lost      / games_played * your_fumble_penalty
                       + interceptions_thrown / games_played * your_int_penalty
    """

    __tablename__ = "scoring_formats"
    __table_args__ = (UniqueConstraint("name", name="uq_scoring_format_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)    # "standard", "ppr", "te_premium_1.0", etc.
    description = Column(Text)
    rules = Column(Text, nullable=False)     # JSON string

    def parse_rules(self) -> dict:
        return json.loads(self.rules)

    @staticmethod
    def compute_points(stats: dict, rules: dict, position: str) -> float:
        """
        Apply *rules* to a *stats* dict and return base fantasy points.

        INT and fumble-lost penalties are deliberately excluded — they are
        tracked as raw season counts in NFLPlayerSeasonScore for league-specific
        adjustment. *stats* keys match NFLPlayerGameLog column names.
        """
        r = rules
        pts = 0.0

        # Passing (INT penalty excluded — tracked separately)
        pts += (stats.get("pass_yards") or 0) / r.get("pass_yards_per_point", 25)
        pts += (stats.get("pass_tds") or 0)   * r.get("pass_td", 6)
        pts += (stats.get("pass_2pt") or 0)   * r.get("pass_2pt", 2)

        # Rushing
        pts += (stats.get("rush_yards") or 0) / r.get("rush_yards_per_point", 10)
        pts += (stats.get("rush_tds") or 0)   * r.get("rush_td", 6)
        pts += (stats.get("rush_2pt") or 0)   * r.get("rush_2pt", 2)

        # Receiving
        recs = stats.get("receptions") or 0
        pts += recs                            * r.get("reception", 1.0)
        pts += (stats.get("rec_yards") or 0)  / r.get("rec_yards_per_point", 10)
        pts += (stats.get("rec_tds") or 0)    * r.get("rec_td", 6)
        pts += (stats.get("rec_2pt") or 0)    * r.get("rec_2pt", 2)

        # TE reception bonus (the main differentiating axis for TE-premium formats)
        if position == "TE":
            pts += recs * r.get("te_rec_bonus", 0.0)

        # Special teams TD (standard 6 pts everywhere)
        pts += (stats.get("special_teams_tds") or 0) * r.get("special_teams_td", 6)

        return round(pts, 3)

    def __repr__(self) -> str:
        return f"<ScoringFormat name={self.name!r}>"


class NFLPlayerGameLog(Base):
    """
    Per-game (weekly) raw counting stats from nflverse player_stats.

    One row per (nflverse_id, season_year, week). Covers all counting
    stats needed to compute custom fantasy scoring formats. Populated by
    scripts/populate_game_logs.py.
    """

    __tablename__ = "nfl_player_game_logs"
    __table_args__ = (
        UniqueConstraint(
            "nflverse_id", "season_year", "week",
            name="uq_game_log",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identity
    player_name = Column(String, nullable=False)
    nflverse_id = Column(String, nullable=False)
    position = Column(String)
    nfl_team = Column(String)
    season_year = Column(Integer, nullable=False)
    week = Column(Integer, nullable=False)

    # Passing
    completions = Column(Integer)
    pass_attempts = Column(Integer)
    pass_yards = Column(Integer)
    pass_tds = Column(Integer)
    interceptions = Column(Integer)
    pass_2pt = Column(Integer)           # passing_2pt_conversions in nflverse
    sack_fumbles_lost = Column(Integer)

    # Rushing
    rush_attempts = Column(Integer)
    rush_yards = Column(Integer)
    rush_tds = Column(Integer)
    rush_2pt = Column(Integer)           # rushing_2pt_conversions in nflverse
    rushing_fumbles_lost = Column(Integer)

    # Receiving
    targets = Column(Integer)
    receptions = Column(Integer)
    rec_yards = Column(Integer)
    rec_tds = Column(Integer)
    rec_2pt = Column(Integer)            # receiving_2pt_conversions in nflverse
    receiving_fumbles_lost = Column(Integer)

    # Miscellaneous
    special_teams_tds = Column(Integer)

    # nflverse pre-computed scores (for spot-checking)
    fantasy_points_std = Column(Float)   # nflverse standard (no PPR)
    fantasy_points_ppr = Column(Float)   # nflverse full PPR

    def __repr__(self) -> str:
        return (
            f"<NFLPlayerGameLog {self.player_name!r} "
            f"{self.season_year} wk{self.week}>"
        )


class NFLPlayerSeasonScore(Base):
    """
    Computed season fantasy points per (player, season, scoring format).

    Derived from nfl_player_game_logs by scripts/compute_scores.py.
    One row per (nflverse_id, season_year, format_id).

    For players who changed teams mid-season, nfl_team shows the team
    with the most games played that season.
    """

    __tablename__ = "nfl_player_season_scores"
    __table_args__ = (
        UniqueConstraint(
            "nflverse_id", "season_year", "format_id",
            name="uq_season_score",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    player_name = Column(String, nullable=False)
    nflverse_id = Column(String)
    position = Column(String)
    nfl_team = Column(String)              # team with most games this season
    season_year = Column(Integer, nullable=False)
    format_id = Column(Integer, ForeignKey("scoring_formats.id"), nullable=False)
    format_name = Column(String)           # denormalized for easy querying

    games_played = Column(Integer)
    fantasy_points_total = Column(Float)   # base points — does NOT include INT/fumble penalties
    fantasy_ppg = Column(Float)            # base PPG — does NOT include INT/fumble penalties

    # Raw penalty counts — apply your league's multiplier at query time:
    #   adjusted_ppg = fantasy_ppg
    #                  + fumbles_lost       / games_played * your_fumble_penalty
    #                  + interceptions_thrown / games_played * your_int_penalty
    fumbles_lost = Column(Integer)         # total fumbles lost (rush + rec + sack)
    interceptions_thrown = Column(Integer) # interceptions (near-zero for WR/RB/TE)

    def __repr__(self) -> str:
        return (
            f"<NFLPlayerSeasonScore {self.player_name!r} "
            f"{self.season_year} fmt={self.format_name} "
            f"ppg={self.fantasy_ppg}>"
        )


class QANameCollision(Base):
    """
    QA: NFL players whose name matched multiple cfb-prospect-db records.
    Requires manual review before training data is used.
    """

    __tablename__ = "qa_name_collisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nfl_player_name = Column(String)
    draft_year = Column(Integer)
    position = Column(String)
    candidate_1_name = Column(String)
    candidate_1_id = Column(Integer)
    candidate_1_score = Column(Float)
    candidate_2_name = Column(String)
    candidate_2_id = Column(Integer)
    candidate_2_score = Column(Float)
    resolved = Column(Boolean, default=False)
    notes = Column(Text)


class QAMissingDraft(Base):
    """
    QA: NFL players with season data but no matched draft record in cfb-prospect-db.
    """

    __tablename__ = "qa_missing_draft"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nfl_player_name = Column(String)
    position = Column(String)
    season_year = Column(Integer)
    estimated_draft_year = Column(Integer)
    fantasy_ppg = Column(Float)
    notes = Column(Text)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _migrate(engine) -> None:
    """
    Add any missing columns to existing tables (forward-only SQLite migration).
    Safe to run on a fresh DB or an existing one.
    """
    from sqlalchemy import text

    migrations = [
        # Added when INT/fumble were factored out of scoring formats
        ("nfl_player_season_scores", "fumbles_lost",          "INTEGER"),
        ("nfl_player_season_scores", "interceptions_thrown",  "INTEGER"),
    ]
    with engine.connect() as conn:
        for table, col, col_type in migrations:
            result = conn.execute(text(f"PRAGMA table_info({table})"))
            existing = {row[1] for row in result}
            if col not in existing:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                ))
        conn.commit()


def init_db(db_path: str) -> None:
    """Create all tables if they don't exist, then apply forward migrations."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    _migrate(engine)
    engine.dispose()


def _make_engine(db_path: str):
    return create_engine(
        f"sqlite:///{db_path}",
        echo=False,
        connect_args={"check_same_thread": False},
    )


@contextmanager
def get_session(db_path: str) -> Generator[Session, None, None]:
    """Context manager that yields a SQLAlchemy Session and auto-commits/rolls back."""
    engine = _make_engine(db_path)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
