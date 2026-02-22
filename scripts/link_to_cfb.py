"""
Link NFL players in nfl_player_seasons_raw to cfb-prospect-db player IDs.

Strategy:
  1. For each unique (player_name, draft_year, position) in nfl_player_seasons_raw,
     attempt to fuzzy-match the name against cfb-prospect-db players with a
     matching position and draft_year (from nfl_draft_picks).
  2. Exact matches (score 100) are stored as method='exact'.
  3. High-confidence fuzzy matches (score ≥ 90) are stored as method='fuzzy'.
  4. Ambiguous matches (top two candidates within 5 points) are written to
     qa_name_collisions for manual review.
  5. Players with no match at any threshold are written to qa_missing_draft.
  6. The draft_year field on NFLPlayerSeason rows is also back-filled from the
     matched draft pick data.

After running this script:
  - cfb_link table contains all confident matches
  - qa_name_collisions contains players requiring manual verification
  - qa_missing_draft contains unmatched players
  - draft_year is populated on NFLPlayerSeason rows for matched players

Usage:
    python scripts/link_to_cfb.py
    python scripts/link_to_cfb.py --threshold 85    # stricter matching
    python scripts/link_to_cfb.py --dry-run         # report only, no writes

Dependencies:
    cfb-prospect-db must be populated (run populate_db.py and populate_nfl.py first).
    CFB_DB_PATH must be set in .env.
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path, get_cfb_db_path
from ffnfl.database import (
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

_HIGH_CONFIDENCE = 90   # auto-accept above this score
_LOW_CONFIDENCE  = 75   # reject below this score


def _load_cfb_candidates(cfb_db_path: str) -> dict[str, list[dict]]:
    """
    Load all WR/RB/TE players from cfb-prospect-db who have a draft pick record.

    Returns a dict keyed by position, each containing a list of:
        {player_id, full_name, draft_year, position}
    """
    # Import cfb-prospect-db models dynamically (different repo)
    cfb_root = Path(cfb_db_path).parent
    if str(cfb_root) not in sys.path:
        sys.path.insert(0, str(cfb_root))

    from ffdb.database import NFLDraftPick, Player
    from ffdb.database import get_session as cfb_session

    candidates: dict[str, list[dict]] = defaultdict(list)
    with cfb_session(cfb_db_path) as s:
        rows = (
            s.query(Player, NFLDraftPick)
            .join(NFLDraftPick, NFLDraftPick.player_id == Player.id)
            .filter(NFLDraftPick.position_drafted.in_(["WR", "RB", "TE"]))
            .all()
        )
        for player, pick in rows:
            pos = pick.position_drafted or player.position
            if not pos:
                continue
            candidates[pos].append({
                "player_id": player.id,
                "full_name": player.full_name,
                "draft_year": pick.draft_year,
                "position": pos,
            })

    total = sum(len(v) for v in candidates.values())
    logger.info("Loaded %d drafted skill players from cfb-prospect-db.", total)
    return candidates


def _fuzzy_match(
    name: str,
    draft_year: Optional[int],
    position: str,
    candidates: list[dict],
    threshold: int,
) -> list[tuple[dict, float]]:
    """
    Fuzzy-match a player name against candidates, filtered by position and
    optionally draft_year (±1 year tolerance for early/late declares).

    Returns list of (candidate_dict, score) sorted descending by score.
    """
    from rapidfuzz import fuzz, process

    # Filter to same position and approximate draft year (±1 year tolerance).
    # Do NOT relax to all years — that would match old draft classes against
    # recent CFB prospects and produce garbage links.
    filtered = [
        c for c in candidates
        if c["position"] == position
        and (draft_year is None or abs((c["draft_year"] or 0) - (draft_year or 0)) <= 1)
    ]
    if not filtered:
        return []

    names = [c["full_name"] for c in filtered]
    results = process.extract(name, names, scorer=fuzz.WRatio, limit=3)

    matched = []
    for match_name, score, idx in results:
        if score >= threshold:
            matched.append((filtered[idx], float(score)))

    return sorted(matched, key=lambda x: -x[1])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link NFL players to cfb-prospect-db player IDs."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--cfb-db", type=str, default=None)
    parser.add_argument(
        "--threshold", type=int, default=_LOW_CONFIDENCE,
        help=f"Minimum fuzzy score to consider a match (default: {_LOW_CONFIDENCE}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report matches without writing to DB.",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    cfb_db_path = args.cfb_db or get_cfb_db_path()
    init_db(db_path)

    # Load cfb-prospect-db candidates
    cfb_candidates = _load_cfb_candidates(cfb_db_path)
    all_candidates: list[dict] = [c for v in cfb_candidates.values() for c in v]

    # Load unique NFL players — deduplicate by (name, position).
    # Track earliest season_year per player as estimated draft_year when
    # draft_year hasn't been populated yet (avoids year-unfiltered fuzzy matches
    # against wrong draft classes).
    unique: dict[tuple, dict] = {}
    with get_session(db_path) as session:
        for r in session.query(NFLPlayerSeason).all():
            key = (r.player_name, r.position)
            if key not in unique:
                unique[key] = {
                    "player_name": r.player_name,
                    "position": r.position,
                    "draft_year": r.draft_year,
                    "fantasy_ppg": r.fantasy_ppg,
                    "season_year": r.season_year,
                    "min_season_year": r.season_year,
                }
            else:
                # Keep the earliest season year seen — best proxy for draft year
                if r.season_year and (unique[key]["min_season_year"] is None
                                      or r.season_year < unique[key]["min_season_year"]):
                    unique[key]["min_season_year"] = r.season_year
                # Prefer non-null draft_year if we have it
                if r.draft_year and unique[key]["draft_year"] is None:
                    unique[key]["draft_year"] = r.draft_year

    logger.info("Attempting to link %d unique (player, position) combos...",
                len(unique))

    exact_count = fuzzy_count = ambiguous_count = missing_count = 0
    links = []
    collisions = []
    missing = []

    pos_candidates = dict(cfb_candidates)

    for (name, pos), info in unique.items():
        if not pos or pos not in ("WR", "RB", "TE"):
            continue
        candidates_for_pos = pos_candidates.get(pos, [])

        # Use draft_year if populated; otherwise fall back to earliest season year.
        # This ensures year-based filtering works even for un-linked players.
        effective_draft_year = info["draft_year"] or info.get("min_season_year")
        matches = _fuzzy_match(name, effective_draft_year, pos, candidates_for_pos, args.threshold)

        if not matches:
            missing_count += 1
            missing.append(info)
            continue

        top_score = matches[0][1]
        second_score = matches[1][1] if len(matches) > 1 else 0

        # Ambiguous: top two within 5 points and both above threshold
        if second_score >= args.threshold and (top_score - second_score) < 5:
            ambiguous_count += 1
            collisions.append({
                "nfl_player_name": name,
                "draft_year": info["draft_year"],
                "position": pos,
                "candidate_1_name": matches[0][0]["full_name"],
                "candidate_1_id": matches[0][0]["player_id"],
                "candidate_1_score": top_score,
                "candidate_2_name": matches[1][0]["full_name"],
                "candidate_2_id": matches[1][0]["player_id"],
                "candidate_2_score": second_score,
            })
            continue

        best, score = matches[0]
        method = "exact" if score == 100.0 else "fuzzy"
        if method == "exact":
            exact_count += 1
        else:
            fuzzy_count += 1

        # Use the NFLDraftPick year from the matched CFB record as the authoritative
        # draft_year — this is what back-fills draft_year on NFLPlayerSeason rows.
        resolved_draft_year = info["draft_year"] or best.get("draft_year")
        links.append({
            "nfl_player_name": name,
            "draft_year": resolved_draft_year,
            "position": pos,
            "cfb_player_id": best["player_id"],
            "cfb_full_name": best["full_name"],
            "match_score": score,
            "match_method": method,
        })

    logger.info(
        "Results: %d exact, %d fuzzy, %d ambiguous (QA), %d missing",
        exact_count, fuzzy_count, ambiguous_count, missing_count,
    )

    if args.dry_run:
        print(f"\nTop fuzzy matches:")
        fuzzy_only = [l for l in links if l["match_method"] == "fuzzy"]
        fuzzy_only.sort(key=lambda x: x["match_score"])
        for l in fuzzy_only[:20]:
            print(f"  {l['nfl_player_name']!r:30} -> {l['cfb_full_name']!r:30} "
                  f"(score={l['match_score']:.0f}, draft={l['draft_year']})")
        print(f"\nQA - Ambiguous ({len(collisions)} total):")
        for c in collisions[:10]:
            print(f"  {c['nfl_player_name']!r}: {c['candidate_1_name']!r} "
                  f"({c['candidate_1_score']:.0f}) vs {c['candidate_2_name']!r} "
                  f"({c['candidate_2_score']:.0f})")
        return

    # Write links to DB
    with get_session(db_path) as session:
        for l in links:
            existing = (
                session.query(CFBLink)
                .filter(
                    CFBLink.nfl_player_name == l["nfl_player_name"],
                    CFBLink.position == l["position"],
                )
                .first()
            )
            if existing is None:
                existing = CFBLink(
                    nfl_player_name=l["nfl_player_name"],
                    position=l["position"],
                )
                session.add(existing)
            existing.draft_year = l["draft_year"]
            existing.cfb_player_id = l["cfb_player_id"]
            existing.cfb_full_name = l["cfb_full_name"]
            existing.match_score = l["match_score"]
            existing.match_method = l["match_method"]

        for c in collisions:
            existing = (
                session.query(QANameCollision)
                .filter(
                    QANameCollision.nfl_player_name == c["nfl_player_name"],
                    QANameCollision.draft_year == c["draft_year"],
                )
                .first()
            )
            if existing is None:
                existing = QANameCollision(**c)
                session.add(existing)

        for m in missing:
            existing = (
                session.query(QAMissingDraft)
                .filter(
                    QAMissingDraft.nfl_player_name == m["player_name"],
                    QAMissingDraft.season_year == m.get("season_year"),
                )
                .first()
            )
            if existing is None:
                existing = QAMissingDraft(
                    nfl_player_name=m["player_name"],
                    position=m["position"],
                    season_year=m.get("season_year"),
                    estimated_draft_year=m.get("draft_year"),
                    fantasy_ppg=m.get("fantasy_ppg"),
                )
                session.add(existing)

    logger.info(
        "Wrote %d links, %d collisions (QA), %d missing (QA) to DB.",
        len(links), len(collisions), len(missing),
    )

    # Back-fill draft_year on NFLPlayerSeason rows
    with get_session(db_path) as session:
        link_map = {
            (l.nfl_player_name, l.position): (l.cfb_player_id, l.draft_year)
            for l in session.query(CFBLink).all()
        }
        updated = 0
        for row in session.query(NFLPlayerSeason).filter(
            NFLPlayerSeason.draft_year.is_(None)
        ).all():
            key = (row.player_name, row.position)
            if key in link_map:
                _, dy = link_map[key]
                if dy:
                    row.draft_year = dy
                    updated += 1
        logger.info("Back-filled draft_year on %d NFLPlayerSeason rows.", updated)

    logger.info("Done.")


if __name__ == "__main__":
    main()
