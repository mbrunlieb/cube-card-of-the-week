#!/usr/bin/env python3
"""
Trophy Battle bot for MTG Cube Discord.
Picks two random trophy decks, posts their images and decklists,
and runs a poll asking which deck is stronger.
Tracks matchup history to avoid repeats and prevents same-drafter matchups.
"""

import json
import os
import random
import re
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
TROPHY_DECKS_FILE = "trophy_decks.json"
TROPHY_HISTORY_FILE = "trophy_battle_history.json"

DISCORD_API = "https://discord.com/api/v10"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/mbrunlieb/cube-card-of-the-week/main"
HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}

# ── History tracking ──────────────────────────────────────────────────────────

def load_trophy_decks() -> list[dict]:
    if not os.path.exists(TROPHY_DECKS_FILE):
        raise FileNotFoundError(f"{TROPHY_DECKS_FILE} not found!")
    with open(TROPHY_DECKS_FILE, "r") as f:
        decks = json.load(f)
    ready = [d for d in decks if d.get("image")]
    print(f"Loaded {len(decks)} total trophy decks, {len(ready)} have images.")
    return ready


def load_matchup_history() -> list[list[str]]:
    if not os.path.exists(TROPHY_HISTORY_FILE):
        return []
    with open(TROPHY_HISTORY_FILE, "r") as f:
        data = json.load(f)
    return data.get("matchups", [])


def save_matchup_history(history: list[list[str]], deck_a: dict, deck_b: dict):
    data = {
        "matchups": history,
        "last_updated": datetime.utcnow().isoformat(),
        "last_matchup": f"{deck_a['drafter']} vs {deck_b['drafter']}",
        "total_matchups": len(history),
    }
    with open(TROPHY_HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"History saved: {len(history)} matchups so far.")


def deck_id(deck: dict) -> str:
    return deck["image"].replace("/", "_").replace(".", "_")


def pick_matchup(decks: list[dict], history: list[list[str]]) -> tuple[dict, dict, bool]:
    used_pairs = [tuple(sorted(p)) for p in history]

    unseen = []
    for i in range(len(decks)):
        for j in range(i + 1, len(decks)):
            if decks[i]["drafter"] == decks[j]["drafter"]:
                continue
            pair = tuple(sorted([deck_id(decks[i]), deck_id(decks[j])]))
            if pair not in used_pairs:
                unseen.append((decks[i], decks[j]))

    history_reset = False
    if not unseen:
        print("All matchups have been used! Resetting history.")
        unseen = [
            (decks[i], decks[j])
            for i in range(len(decks))
            for j in range(i + 1, len(decks))
            if decks[i]["drafter"] != decks[j]["drafter"]
        ]
        history_reset = True

    deck_a, deck_b = random.choice(unseen)
    return deck_a, deck_b, history_reset


# ── Decklist fetching ─────────────────────────────────────────────────────────

def fetch_decklist(draft_id: str, seat: int) -> str | None:
    url = f"https://cubecobra.com/cube/deck/{draft_id}?seat={seat}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"Warning: could not fetch deck page: {e}")
        return None

    pattern = re.compile(r'"mainboard"\s*:\s*(\[.*?\])\s*,\s*"sideboard"', re.DOTALL)
    match = pattern.search(html)

    if not match:
        print(f"Warning: could not find mainboard data for seat {seat}.")
        return None

    try:
        cards = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse mainboard JSON: {e}")
        return None

    card_names = []
    for card in cards:
        name = None
        if isinstance(card, dict):
            name = card.get("details", {}).get("name") or card.get("name")
        if name:
            card_names.append(name)

    if not card_names:
        print(f"Warning: no card names found for seat {seat}.")
        return None

    lines = [f"1 {name}" for name in sorted(card_names)]
    return "\n".join(lines)


# ── Discord posting ───────────────────────────────────────────────────────────

def post_to_discord(deck_a: dict, deck_b: dict, history_reset: bool):
    def format_deck_info(deck: dict, label: str) -> str:
        return f"**{label}: {deck['drafter']}'s Trophy Deck**\n📅 {deck['event']}"

    content_lines = [
        "🏆 **TROPHY BATTLE!!** 🏆",
        "Two undefeated decks enter. Only one can be crowned the greatest.",
        "",
        format_deck_info(deck_a, "Deck A"),
        "",
        format_deck_info(deck_b, "Deck B"),
    ]

    if history_reset:
        content_lines.append("\n*All matchups have been featured — starting a fresh cycle!* 🔄")

    content = "\n".join(content_lines)

    image_url_a = f"{GITHUB_RAW_BASE}/{deck_a['image']}"
    image_url_b = f"{GITHUB_RAW_BASE}/{deck_b['image']}"

    poll = {
        "question": {"text": "Twoe shimmering grimores lay before ye on a silken bed: whiche do ye choose?"},
        "answers": [
            {"poll_media": {"text": f"Deck A — {deck_a['drafter']}", "emoji": {"name": "🅰️"}}},
            {"poll_media": {"text": f"Deck B — {deck_b['drafter']}", "emoji": {"name": "🅱️"}}},
        ],
        "duration": 36,
        "allow_multiselect": False,
    }

    embeds = [
        {
            "title": f"🅰️ {deck_a['drafter']} — {deck_a['event']}",
            "color": 0xFFD700,
            "image": {"url": image_url_a},
        },
        {
            "title": f"🅱️ {deck_b['drafter']} — {deck_b['event']}",
            "color": 0xC0C0C0,
            "image": {"url": image_url_b},
        },
    ]

    bot_headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "content": content,
        "embeds": embeds,
        "poll": poll,
    }

    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages"
    resp = requests.post(url, json=payload, headers=bot_headers, timeout=15)
    if not resp.ok:
        print(f"Discord error response: {resp.text}")
    resp.raise_for_status()
    print(f"Posted trophy battle. Status: {resp.status_code}")

    # Post decklists as file attachments in a follow-up message
    decklist_parts = []
    for label, deck in [("Deck_A", deck_a), ("Deck_B", deck_b)]:
        draft_id = deck.get("cubecobra_draft_id")
        seat = deck.get("seat", 0)
        if draft_id:
            print(f"Fetching decklist for {deck['drafter']} (seat {seat})...")
            decklist = fetch_decklist(draft_id, seat)
            if decklist:
                filename = f"{label}_{deck['drafter']}_decklist.txt"
                decklist_parts.append((filename, decklist))
            else:
                print(f"Could not fetch decklist for {deck['drafter']}.")

    if decklist_parts:
        file_headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        files = {}
        for i, (filename, text) in enumerate(decklist_parts):
            files[f"files[{i}]"] = (filename, text.encode("utf-8"), "text/plain")
        form_data = {"content": "📋 **Decklists** (for Cockatrice and friends!)"}
        file_resp = requests.post(
            f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=file_headers,
            data={"payload_json": json.dumps(form_data)},
            files=files,
            timeout=15,
        )
        if not file_resp.ok:
            print(f"Discord file upload error: {file_resp.text}")
        else:
            print(f"Posted decklists. Status: {file_resp.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading trophy decks...")
    decks = load_trophy_decks()

    if len(decks) < 2:
        print("Not enough trophy decks with images to run a battle. Exiting.")
        return

    print("Loading matchup history...")
    history = load_matchup_history()
    print(f"Past matchups: {len(history)}")

    print("Picking matchup...")
    deck_a, deck_b, history_reset = pick_matchup(decks, history)
    print(f"Matchup: {deck_a['drafter']} vs {deck_b['drafter']}")

    print("Posting to Discord...")
    post_to_discord(deck_a, deck_b, history_reset)

    pair = sorted([deck_id(deck_a), deck_id(deck_b)])
    if history_reset:
        history = [pair]
    else:
        history.append(pair)
    save_matchup_history(history, deck_a, deck_b)

    print("Done!")


if __name__ == "__main__":
    main()
