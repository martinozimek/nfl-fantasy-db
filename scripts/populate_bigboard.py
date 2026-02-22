"""
Populate nfl_big_board from nflmockdraftdatabase.com consensus big board pages.

Strategy:
  - For each year, fetch the consensus big board HTML page.
  - nflmockdraftdatabase.com is a Rails + React SPA that embeds its data as JSON
    in a `data-react-props` attribute — no Selenium required, plain requests works.
  - Extract WR/RB/TE players with their overall consensus rank and position rank.
  - Upsert into nfl_big_board with source='nflmockdraftdatabase'.

URL pattern:
  https://www.nflmockdraftdatabase.com/big-boards/{year}/consensus-big-board-{year}-nfl-draft

The 'consensus_rank' stored is the player's overall consensus draft position
(e.g., 6 = analysts expected this player to go 6th overall). This serves as a
pre-draft market expectation signal, distinct from actual post-draft pick number.

Usage:
    python scripts/populate_bigboard.py
    python scripts/populate_bigboard.py --years 2023 2024 2025
    python scripts/populate_bigboard.py --dry-run
    python scripts/populate_bigboard.py --start-year 2016 --end-year 2026

Dependencies:
    pip install requests beautifulsoup4
"""

import argparse
import html as html_module
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import get_db_path
from ffnfl.database import NFLBigBoard, get_session, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_BASE_URL = (
    "https://www.nflmockdraftdatabase.com/big-boards/{year}/consensus-big-board-{year}-nfl-draft"
)
_POSITIONS = {"WR", "RB", "TE"}
_SOURCE = "nflmockdraftdatabase"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_REQUEST_DELAY = 2.0   # seconds between requests — be a polite guest


def _fetch_board(year: int) -> Optional[list[dict]]:
    """
    Fetch and parse the consensus big board for *year*.

    Returns a list of dicts with keys:
        player_name, position, consensus_rank, draft_year
    for WR/RB/TE players only, or None on failure.
    """
    url = _BASE_URL.format(year=year)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
    except requests.RequestException as exc:
        logger.warning("  [%d] Network error: %s", year, exc)
        return None

    if resp.status_code == 404:
        logger.info("  [%d] No big board found (404).", year)
        return None
    if resp.status_code != 200:
        logger.warning("  [%d] Unexpected status %d.", year, resp.status_code)
        return None

    soup = BeautifulSoup(resp.content, "html.parser")

    # Data is embedded as React props on the component div.
    react_div = soup.find("div", attrs={"data-react-class": True})
    if react_div is None:
        logger.warning("  [%d] No React props div found — page structure may have changed.", year)
        return None

    raw_props = react_div.get("data-react-props", "")
    if not raw_props:
        logger.warning("  [%d] Empty data-react-props attribute.", year)
        return None

    # HTML entities are escaped in the attribute value.
    try:
        data = json.loads(html_module.unescape(raw_props))
    except json.JSONDecodeError as exc:
        logger.warning("  [%d] JSON parse error: %s", year, exc)
        return None

    # Locate the selections list — may be under 'mock' or 'big_board' key.
    selections = None
    for top_key in ("mock", "big_board", "board"):
        section = data.get(top_key, {})
        if isinstance(section, dict):
            selections = section.get("selections") or section.get("players")
        elif isinstance(section, list):
            selections = section
        if selections:
            break

    # Fallback: look for any top-level list that contains dicts with 'player' key
    if not selections:
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and "player" in v[0]:
                selections = v
                break

    if not selections:
        logger.warning(
            "  [%d] Could not locate selections list. Top-level keys: %s",
            year, list(data.keys()),
        )
        return None

    rows = []
    pos_counters: dict[str, int] = {}

    for entry in selections:
        # Support both flat and nested player structures
        player = entry.get("player") or entry
        raw_pos = (
            player.get("position")
            or entry.get("position")
            or ""
        ).upper().strip()

        if raw_pos not in _POSITIONS:
            continue

        name = (
            player.get("name")
            or player.get("full_name")
            or entry.get("player_name")
            or ""
        ).strip()
        if not name:
            continue

        # Overall consensus rank — prefer consensus sub-dict, fall back to outer pick
        consensus_sub = entry.get("consensus") or {}
        overall_rank = (
            consensus_sub.get("pick")
            or entry.get("pick")
            or entry.get("rank")
            or entry.get("consensus_pick")
        )
        if overall_rank is None:
            continue

        try:
            overall_rank = int(overall_rank)
        except (ValueError, TypeError):
            continue

        pos_counters[raw_pos] = pos_counters.get(raw_pos, 0) + 1
        rows.append({
            "player_name": name,
            "position": raw_pos,
            "draft_year": year,
            "consensus_rank": overall_rank,
            "position_rank": pos_counters[raw_pos],
        })

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate nfl_big_board from nflmockdraftdatabase.com."
    )
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument(
        "--start-year", type=int, default=2015,
        help="First draft year to fetch (default: 2015).",
    )
    parser.add_argument(
        "--end-year", type=int, default=2026,
        help="Last draft year to fetch (default: 2026).",
    )
    parser.add_argument(
        "--years", type=int, nargs="+",
        help="Explicit list of years to fetch (overrides --start-year / --end-year).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and parse without writing to DB.",
    )
    args = parser.parse_args()

    db_path = args.db or get_db_path()
    if not args.dry_run:
        init_db(db_path)

    years = args.years or list(range(args.start_year, args.end_year + 1))

    total_written = 0

    for year in years:
        logger.info("Fetching consensus big board for %d...", year)
        rows = _fetch_board(year)

        if rows is None:
            time.sleep(_REQUEST_DELAY)
            continue

        by_pos: dict[str, list] = {}
        for r in rows:
            by_pos.setdefault(r["position"], []).append(r)
        summary = ", ".join(
            f"{pos}:{len(v)}" for pos, v in sorted(by_pos.items())
        )
        logger.info("  [%d] Parsed %d skill players (%s).", year, len(rows), summary)

        if args.dry_run:
            for r in rows[:10]:
                print(
                    f"  #{r['consensus_rank']:>3}  {r['position']:<3}  "
                    f"{r['player_name']:<30}  pos_rank={r['position_rank']}"
                )
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
        else:
            with get_session(db_path) as session:
                for r in rows:
                    existing = (
                        session.query(NFLBigBoard)
                        .filter(
                            NFLBigBoard.player_name == r["player_name"],
                            NFLBigBoard.draft_year == r["draft_year"],
                            NFLBigBoard.source == _SOURCE,
                        )
                        .first()
                    )
                    if existing is None:
                        existing = NFLBigBoard(
                            player_name=r["player_name"],
                            draft_year=r["draft_year"],
                            source=_SOURCE,
                        )
                        session.add(existing)
                    existing.position = r["position"]
                    existing.consensus_rank = r["consensus_rank"]
                    existing.position_rank = r["position_rank"]

            total_written += len(rows)
            logger.info("  [%d] Upserted %d rows.", year, len(rows))

        time.sleep(_REQUEST_DELAY)

    if not args.dry_run:
        logger.info("Done. Total rows written: %d.", total_written)


if __name__ == "__main__":
    main()
