#!/usr/bin/env python3
"""
Trophy Battle bot for MTG Cube Discord.
Picks two random trophy decks, posts their images, and runs a poll
asking which deck is stronger. Tracks matchup history to avoid repeats.
"""

import json
import os
import random
from datetime import datetime

import requests

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
TROPHY_DECKS_FILE = "trophy_decks.json"
TROPHY_HISTORY_FILE = "trophy_battle_history.json"

DISCORD_API = "https://discord.com/api/v10"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/mbrunlieb/cube-card-of-the-week/main"

# ── History tracking ──────────────────────────────────────────────────────────

def load_trophy_decks() -> list[dict]:
    """Load trophy decks from JSON file, returning only those with images."""
    if not os.path.exists(TROPHY_DECKS_FILE):
        raise FileNotFoundError(f"{TROPHY_DECKS_FILE} not found!")
    with open(TROPHY_DECKS_FILE, "r") as f:
        decks = json.load(f)
    ready = [d for d in decks if d.get("image")]
    print(f"Loaded {len(decks)} total trophy decks, {len(ready)} have images.")
    return ready


def load_matchup_history() -> list[list[str]]:
    """Load list of past matchups. Each matchup is a sorted pair of deck IDs."""
    if not os.path.exists(TROPHY_HISTORY_FILE):
        return []
    with open(TROPHY_HISTORY_FILE, "r") as f:
        data = json.load(f)
    return data.get("matchups", [])


def save_matchup_history(history: list[list[str]], deck_a: dict, deck_b: dict):
    """Save updated matchup history."""
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
    """Generate a stable unique ID for a deck from its image path."""
    return deck["image"].replace("/", "_").replace(".", "_")


def pick_matchup(decks: list[dict], history: list[list[str]]) -> tuple[dict, dict, bool]:
    """
    Pick two decks that haven't been matched up before.
    Returns (deck_a, deck_b, history_was_reset).
    """
    used_pairs = [tuple(sorted(p)) for p in history]

    # Build all possible unseen matchups
    unseen = []
    for i in range(len(decks)):
        for j in range(i + 1, len(decks)):
            pair = tuple(sorted([deck_id(decks[i]), deck_id(decks[j])]))
            if pair not in used_pairs:
                unseen.append((decks[i], decks[j]))

    history_reset = False
    if not unseen:
        print("All matchups have been used! Resetting history.")
        unseen = [(decks[i], decks[j]) for i in range(len(decks)) for j in range(i + 1, len(decks))]
        history_reset = True

    deck_a, deck_b = random.choice(unseen)
    return deck_a, deck_b, history_reset


# ── Discord posting ───────────────────────────────────────────────────────────

def post_to_discord(deck_a: dict, deck_b: dict, history_reset: bool):
    """Post the trophy battle to Discord with both deck images and a poll."""

    def format_deck_info(deck: dict, label: str) -> str:
        lines = [f"**{label}: {deck['drafter']}'s Trophy Deck**"]
        lines.append(f"📅 {deck['event']} — {deck['date']}")
        return "\n".join(lines)

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

    # Build image URLs from GitHub raw
    image_url_a = f"{GITHUB_RAW_BASE}/{deck_a['image']}"
    image_url_b = f"{GITHUB_RAW_BASE}/{deck_b['image']}"

    poll = {
        "question": {"text": "Which deck is stronger?"},
        "answers": [
            {"poll_media": {"text": f"Deck A — {deck_a['drafter']}", "emoji": {"name": "🅰️"}}},
            {"poll_media": {"text": f"Deck B — {deck_b['drafter']}", "emoji": {"name": "🅱️"}}},
            {"poll_media": {"text": "Too close to call!", "emoji": {"name": "⚖️"}}},
        ],
        "duration": 36,
        "allow_multiselect": False,
    }

    # Post Deck A image
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
    print(f"Posted trophy battle: {deck_a['drafter']} vs {deck_b['drafter']}. Status: {resp.status_code}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading trophy decks…")
    decks = load_trophy_decks()

    if len(decks) < 2:
        print("Not enough trophy decks with images to run a battle. Exiting.")
        return

    print("Loading matchup history…")
    history = load_matchup_history()
    print(f"Past matchups: {len(history)}")

    print("Picking matchup…")
    deck_a, deck_b, history_reset = pick_matchup(decks, history)
    print(f"Matchup: {deck_a['drafter']} vs {deck_b['drafter']}")

    print("Posting to Discord…")
    post_to_discord(deck_a, deck_b, history_reset)

    # Update history
    pair = sorted([deck_id(deck_a), deck_id(deck_b)])
    if history_reset:
        history = [pair]
    else:
        history.append(pair)
    save_matchup_history(history, deck_a, deck_b)

    print("Done!")


if __name__ == "__main__":
    main()
