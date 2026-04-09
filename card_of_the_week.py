#!/usr/bin/env python3
"""
Card of the Week bot for MTG Cube Discord.
Picks a random card from the cube, fetches winrate and combo data,
and posts an embed to Discord via webhook.
"""

import json
import os
import random
import re
import sys
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CUBE_ID = "tm1"
CUBE_RECORDS_ID = "60ba7b55a2494110485dc479"
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

CUBE_JSON_URL = f"https://cubecobra.com/cube/api/cubeJSON/{CUBE_ID}"
CUBE_RECORDS_URL = f"https://cubecobra.com/cube/records/{CUBE_RECORDS_ID}?view=winrate-analytics"
COMBOS_URL = "https://cubecobra.com/cube/api/getcombos"

HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}

# Minimum number of decks a card must appear in to show winrate.
# Set to 0 to always show (even with tiny sample sizes).
MIN_DECKS_FOR_WINRATE = 2

# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_cube_cards():
    """Return list of card dicts from the Cube Cobra cube JSON endpoint."""
    resp = requests.get(CUBE_JSON_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Cards live under data["cards"]["mainboard"]
    cards = data.get("cards", {}).get("mainboard", [])
    if not cards:
        raise ValueError("No mainboard cards found in cube JSON response.")
    return cards


def fetch_winrate_data():
    """
    Scrape the records page HTML and extract the winrate JSON blob.
    Returns a dict keyed by oracle_id -> {decks, matchWins, matchLosses, ...}
    """
    resp = requests.get(CUBE_RECORDS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # The winrate data is a JSON object whose keys are oracle UUIDs.
    # We find it by looking for the pattern "cardAnalytics":{...} or
    # a large block of UUID-keyed objects in the embedded JS state.
    # Strategy: find the first occurrence of a UUID-keyed JSON object.
    uuid_pattern = re.compile(
        r'(\{"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"\s*:\s*\{"decks")',
    )
    match = uuid_pattern.search(html)
    if not match:
        print("Warning: could not locate winrate data in page source. Winrate will be skipped.")
        return {}

    start = match.start()
    # Walk forward to find the matching closing brace for this top-level object.
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    try:
        winrate_data = json.loads(html[start:end])
        print(f"Loaded winrate data for {len(winrate_data)} cards.")
        return winrate_data
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse winrate JSON: {e}")
        return {}


def fetch_combos(oracle_ids: list[str]) -> list[dict]:
    """POST all oracle IDs to Cube Cobra and return list of combo dicts."""
    payload = {"oracles": oracle_ids}
    resp = requests.post(COMBOS_URL, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # Response is either a list of combos or {"combos": [...]}
    combos = data if isinstance(data, list) else data.get("combos", [])
    return combos


# ── Card selection ─────────────────────────────────────────────────────────────

def pick_random_card(cards: list[dict]) -> dict:
    """Pick a random card, skipping basic lands."""
    eligible = [
        c for c in cards
        if c.get("details", {}).get("type", "").lower() not in ("basic land", "")
        and "Basic Land" not in c.get("details", {}).get("type", "")
    ]
    if not eligible:
        eligible = cards  # fallback: pick from everything
    return random.choice(eligible)


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_winrate(oracle_id: str, winrate_data: dict) -> str | None:
    stats = winrate_data.get(oracle_id)
    if not stats:
        return None
    decks = stats.get("decks", 0)
    if decks < MIN_DECKS_FOR_WINRATE:
        return None
    mw = stats.get("matchWins", 0)
    ml = stats.get("matchLosses", 0)
    total_matches = mw + ml
    if total_matches == 0:
        return None
    match_wr = round(100 * mw / total_matches, 1)
    gw = stats.get("gameWins", 0)
    gl = stats.get("gameLosses", 0)
    total_games = gw + gl
    game_wr = round(100 * gw / total_games, 1) if total_games > 0 else 0
    trophies = stats.get("trophies", 0)
    trophy_str = f" 🏆 {trophies} trophy{'s' if trophies != 1 else ''}"
    return (f"Match: {match_wr}% ({mw}W–{ml}L) | "
            f"Game: {game_wr}% ({gw}W–{gl}L) | "
            f"{decks} deck{'s' if decks != 1 else ''}{trophy_str}")


def format_combos(oracle_id: str, combos: list[dict], all_cards: list[dict]) -> list[str]:
    results = []
    for combo in combos:
        uses = combo.get("uses", [])
        piece_oracle_ids = [u["card"]["oracleId"] for u in uses if "card" in u]
        if oracle_id not in piece_oracle_ids:
            continue
        piece_names = [u["card"]["name"] for u in uses if "card" in u]
        produces = combo.get("produces", [])
        result = ", ".join(p["feature"]["name"] for p in produces) if produces else combo.get("description", "Unknown effect")
        results.append(f"**{' + '.join(piece_names)}** → {result}")
    return results


# ── Discord posting ───────────────────────────────────────────────────────────

def post_to_discord(card: dict, winrate_str: str | None, combo_lines: list[str]):
    details = card.get("details", {})
    name = details.get("name", "Unknown Card")
    image_url = details.get("image_normal") or details.get("image_small", "")
    scryfall_uri = details.get("scryfall_uri", "")

    desc_parts = []

    if winrate_str:
        desc_parts.append(f"📊 **Winrate:** {winrate_str}")
    else:
        desc_parts.append("📊 **Winrate:** Not enough data yet")

    if combo_lines:
        desc_parts.append("")
        desc_parts.append(f"⚡ **Combos in this cube ({len(combo_lines)}):**")
        for line in combo_lines[:5]:
            desc_parts.append(f"• {line}")
        if len(combo_lines) > 5:
            desc_parts.append(f"*…and {len(combo_lines) - 5} more*")

    description = "\n".join(desc_parts)

    embed = {
        "title": f"🃏 {name}",
        "description": description,
        "color": 0x5865F2,
        "image": {"url": image_url},
    }
    if scryfall_uri:
        embed["url"] = scryfall_uri

    payload = {
        "content": "🧝 **SCROLL of the week!!** 🧙\nSPEAK WIZARD! LOVETH thee this incantation, or dost thee HATETH it??",
        "embeds": [embed],
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()
    print(f"Posted '{name}' to Discord. Status: {resp.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching cube card list…")
    cards = fetch_cube_cards()
    print(f"Found {len(cards)} mainboard cards.")

    print("Fetching winrate data…")
    winrate_data = fetch_winrate_data()

    print("Picking a random card…")
    card = pick_random_card(cards)
    details = card.get("details", {})
    name = details.get("name", "Unknown")
    oracle_id = details.get("oracle_id", "")
    print(f"Selected: {name} (oracle_id: {oracle_id})")

    print("Fetching combo data…")
    all_oracle_ids = [
        c.get("details", {}).get("oracle_id", "")
        for c in cards
        if c.get("details", {}).get("oracle_id")
    ]
    combos = fetch_combos(all_oracle_ids)
    print(f"Found {len(combos)} combos in cube.")

    winrate_str = format_winrate(oracle_id, winrate_data)
    combo_lines = format_combos(oracle_id, combos, cards)
    print(f"Winrate: {winrate_str or 'N/A'}")
    print(f"Combos involving this card: {len(combo_lines)}")

    print("Posting to Discord…")
    post_to_discord(card, winrate_str, combo_lines)
    print("Done!")


if __name__ == "__main__":
    main()
