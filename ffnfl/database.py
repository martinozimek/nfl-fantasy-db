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

cfb_link
    Fuzzy-matched link from NFL player name → cfb-prospect-db player_id.
    Enables joining NFL outcomes back to college features.

qa_name_collisions
    QA: players whose NFL name matched multiple cfb-prospect-db records.

qa_missing_draft
    QA: players with NFL seasons but no draft record matched.
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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
        UniqueConstraint("nfl_player_name", "draft_year", name="uq_cfb_link"),
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

def init_db(db_path: str) -> None:
    """Create all tables if they don't exist (idempotent)."""
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
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
