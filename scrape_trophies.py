#!/usr/bin/env python3
"""
Trophy scraper for MTG Cube Discord.
Scrapes the Cube Cobra trophy archive and updates trophy_decks.json
with any new trophy decks found. Existing entries are preserved.
New entries without images are added with image: null as placeholders.
"""

import json
import os
import re
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CUBE_ID = "tm1"
CUBE_RECORDS_ID = "60ba7b55a2494110485dc479"
TROPHY_DECKS_FILE = "trophy_decks.json"

TROPHY_ARCHIVE_URL = f"https://cubecobra.com/cube/records/{CUBE_RECORDS_ID}?view=trophy-archive"
HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}


# ── Scraping ──────────────────────────────────────────────────────────────────

def fetch_trophy_archive() -> list[dict]:
    """
    Scrape the trophy archive page and extract all trophy deck entries.
    Returns list of dicts with drafter, event, date, cubecobra_draft_id.
    """
    resp = requests.get(TROPHY_ARCHIVE_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    html = resp.text

    # The records are embedded as a JSON array in the page source
    # Find it by looking for the pattern [{"id":"...","cube":"...
    pattern = re.compile(r'\[\{"id":"[0-9a-f\-]+","cube":"[0-9a-f\-]+"')
    match = pattern.search(html)

    if not match:
        print("Warning: could not find trophy data in page source.")
        return []

    start = match.start()
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start=start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        records = json.loads(html[start:end])
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse trophy archive JSON: {e}")
        return []

    entries = []
    for record in records:
        trophy_players = record.get("trophy", [])
        if not trophy_players:
            continue

        draft_id = record.get("draft", "")
        event_name = record.get("name", "Unknown Event")
        date_ms = record.get("date") or record.get("dateCreated", 0)
        date_str = datetime.utcfromtimestamp(date_ms / 1000).strftime("%Y-%m-%d") if date_ms else "Unknown"

        for player in trophy_players:
            entries.append({
                "drafter": player,
                "event": event_name,
                "date": date_str,
                "cubecobra_draft_id": draft_id,
                "image": None,
            })

    print(f"Found {len(entries)} trophy deck entries in archive.")
    return entries

# ── Merging ───────────────────────────────────────────────────────────────────

def load_existing(filepath: str) -> list[dict]:
    if not os.path.exists(filepath):
        return []
    with open(filepath, "r") as f:
        return json.load(f)


def merge_entries(existing: list[dict], scraped: list[dict]) -> tuple[list[dict], int]:
    """
    Merge scraped entries into existing list.
    Matches on cubecobra_draft_id + drafter to avoid duplicates.
    Preserves image paths from existing entries.
    Returns (merged_list, new_count).
    """
    # Build lookup of existing entries
    existing_keys = {
        (e.get("cubecobra_draft_id", ""), e.get("drafter", "")): i
        for i, e in enumerate(existing)
    }

    merged = list(existing)
    new_count = 0

    for entry in scraped:
        key = (entry.get("cubecobra_draft_id", ""), entry.get("drafter", ""))
        if key not in existing_keys:
            merged.append(entry)
            new_count += 1
            print(f"  + New entry: {entry['drafter']} — {entry['event']} ({entry['date']})")

    return merged, new_count


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching trophy archive from Cube Cobra…")
    scraped = fetch_trophy_archive()

    if not scraped:
        print("No trophy data found. Exiting.")
        return

    print(f"Loading existing {TROPHY_DECKS_FILE}…")
    existing = load_existing(TROPHY_DECKS_FILE)
    print(f"Existing entries: {len(existing)}")

    print("Merging entries…")
    merged, new_count = merge_entries(existing, scraped)

    print(f"Saving {TROPHY_DECKS_FILE}…")
    with open(TROPHY_DECKS_FILE, "w") as f:
        json.dump(merged, f, indent=2)

    print(f"Done! {new_count} new entries added. Total: {len(merged)} trophy decks.")

    # Summary of entries missing images
    missing_images = [e for e in merged if not e.get("image")]
    if missing_images:
        print(f"\n{len(missing_images)} entries still need images:")
        for e in missing_images:
            print(f"  - {e['drafter']} — {e['event']} ({e['date']})")


if __name__ == "__main__":
    main()
