#!/usr/bin/env python3
"""
Card of the Week bot for MTG Cube Discord.
Picks a random card from the cube, fetches winrate and combo data,
and posts an embed to Discord via webhook.
Tracks history to avoid repeats until all cards have been featured.
"""

import json
import os
import random
import re
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CUBE_ID = "tm1"
CUBE_RECORDS_ID = "60ba7b55a2494110485dc479"
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
HISTORY_FILE = "history.json"

CUBE_JSON_URL = f"https://cubecobra.com/cube/api/cubeJSON/{CUBE_ID}"
CUBE_RECORDS_URL = f"https://cubecobra.com/cube/records/{CUBE_RECORDS_ID}?view=winrate-analytics"
COMBOS_URL = "https://cubecobra.com/cube/api/getcombos"

HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}
DISCORD_API = "https://discord.com/api/v10"

# Minimum number of decks a card must appear in to show winrate.
MIN_DECKS_FOR_WINRATE = 2

# ── History tracking ──────────────────────────────────────────────────────────

def load_history() -> list[str]:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r") as f:
        data = json.load(f)
    return data.get("chosen", [])


def save_history(history: list[str], card_name: str):
    data = {
        "chosen": history,
        "last_updated": datetime.utcnow().isoformat(),
        "last_card": card_name,
        "total_chosen": len(history),
    }
    with open(HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"History saved: {len(history)} cards chosen so far.")


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_cube_cards():
    """Return list of card dicts from the Cube Cobra cube JSON endpoint."""
    resp = requests.get(CUBE_JSON_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    cards = data.get("cards", {}).get("mainboard", [])
    if not cards:
        raise ValueError("No mainboard cards found in cube JSON response.")
    return cards


def fetch_winrate_data():
    """
    Scrape the records page HTML and extract the winrate JSON blob.
    Returns a dict keyed by oracle_id -> {decks, matchWins, matchLosses, ...}
    """
    resp = requests.get(CUBE_RECORDS_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    html = resp.text

    uuid_pattern = re.compile(
        r'(\{"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"\s*:\s*\{"decks")',
    )
    match = uuid_pattern.search(html)
    if not match:
        print("Warning: could not locate winrate data in page source.")
        return {}

    start = match.start()
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
    combos = data if isinstance(data, list) else data.get("combos", [])
    return combos


# ── Card selection ─────────────────────────────────────────────────────────────

def pick_random_card(cards: list[dict], history: list[str]) -> tuple[dict, bool]:
    """
    Pick a random eligible card not in history.
    Excludes lands unless tagged 'spotlight'.
    Returns (card, history_was_reset).
    """
    def is_eligible(c):
        type_line = c.get("details", {}).get("type", "")
        tags = [t.lower() for t in c.get("tags", [])]
        if "Land" in type_line:
            return "spotlight" in tags
        return True

    eligible = [c for c in cards if is_eligible(c)]
    if not eligible:
        eligible = cards

    unseen = [
        c for c in eligible
        if c.get("details", {}).get("oracle_id", "") not in history
    ]

    history_reset = False
    if not unseen:
        print("All eligible cards have been featured! Resetting history.")
        unseen = eligible
        history_reset = True

    return random.choice(unseen), history_reset


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
    trophy_str = f" 🏆 {trophies} {'trophy' if trophies == 1 else 'trophies'}"
    return (f"Match: {match_wr}% ({mw}W–{ml}L) | "
            f"Game: {game_wr}% ({gw}W–{gl}L) | "
            f"{decks} deck{'s' if decks != 1 else ''} | {trophy_str}")


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

def post_to_discord(card: dict, winrate_str: str | None, combo_lines: list[str], history_reset: bool):
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
        desc_parts.append(f"👹 **Combos in our cube ({len(combo_lines)}):**")
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

    intro = "🧝 **SCROLL of the day!!** 🧙\n ~ ~ __**SPEAK WIZARD!!**__ ~ ~"
    if history_reset:
        intro += "\n*Every card has been featured — starting a fresh cycle!* 🔄"

    poll = {
        "question": {"text": f"LOVETH thee {name} or dost thou HATETH it??"},
        "answers": [
            {"poll_media": {"text": "I will PIVOT HARD anytime I see this card", "emoji": {"name": "💎"}}},
            {"poll_media": {"text": "Alright, I am pretty happy to take this P1P1", "emoji": {"name": "🚬"}}},
            {"poll_media": {"text": "MID-PACK ass behavior on display", "emoji": {"name": "🥣"}}},
            {"poll_media": {"text": "Eh, maybe on the wheel?", "emoji": {"name": "🎡"}}},
            {"poll_media": {"text": "Can we please cut this?", "emoji": {"name": "🚱"}}},
        ],
        "duration": 36,
        "allow_multiselect": False,
    }

    bot_headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "content": intro,
        "embeds": [embed],
        "poll": poll,
    }

    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages"
    resp = requests.post(url, json=payload, headers=bot_headers, timeout=15)
    if not resp.ok:
        print(f"Discord error response: {resp.text}")
    resp.raise_for_status()
    print(f"Posted '{name}' to Discord. Status: {resp.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading history…")
    history = load_history()
    print(f"Cards previously chosen: {len(history)}")

    print("Fetching cube card list…")
    cards = fetch_cube_cards()
    print(f"Found {len(cards)} mainboard cards.")

    print("Fetching winrate data…")
    try:
        winrate_data = fetch_winrate_data()
    except Exception as e:
        print(f"Warning: could not fetch winrate data: {e}")
        winrate_data = {}

    print("Picking a random card…")
    card, history_reset = pick_random_card(cards, history)
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
    post_to_discord(card, winrate_str, combo_lines, history_reset)

    if history_reset:
        history = [oracle_id]
    else:
        history.append(oracle_id)
    save_history(history, name)

    print("Done!")


if __name__ == "__main__":
    main()
