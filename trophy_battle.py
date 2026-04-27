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

    # Extract the flat cards array
    cards_pattern = re.compile(r'"cards"\s*:\s*(\[.*?\])\s*,\s*"seats"', re.DOTALL)
    cards_match = cards_pattern.search(html)
    if not cards_match:
        print(f"Warning: could not find cards array in deck page.")
        return None

    try:
        cards = json.loads(cards_match.group(1))
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse cards JSON: {e}")
        return None

    # Extract the seats array using bracket counting
    seats_marker = html.find('"seats":[')
    if seats_marker == -1:
        seats_marker = html.find('"seats" :[')
    if seats_marker == -1:
        print(f"Warning: could not find seats array in deck page.")
        return None

    start = seats_marker + len('"seats":')
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
        seats = json.loads(html[start:end])
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse seats JSON: {e}")
        return None

    if seat >= len(seats):
        print(f"Warning: seat {seat} not found (only {len(seats)} seats).")
        return None

    # Flatten the nested mainboard index arrays
    mainboard = seats[seat].get("mainboard", [])
    indices = []
    for pile in mainboard:
        for row in pile:
            if isinstance(row, list):
                indices.extend(row)
            elif isinstance(row, int):
                indices.append(row)

    if not indices:
        print(f"Warning: no card indices found in mainboard for seat {seat}.")
        return None

    # Look up each index in the cards array
    card_names = []
    for idx in indices:
        if idx < len(cards):
            name = cards[idx].get("details", {}).get("name") or cards[idx].get("name")
            if name:
                card_names.append(name)

    if not card_names:
        print(f"Warning: could not resolve any card names for seat {seat}.")
        return None

    print(f"Found {len(card_names)} cards for seat {seat}.")
    lines = [f"1 {name}" for name in sorted(card_names)]
    return "\n".join(lines)


def fetch_both_decklists(deck_a: dict, deck_b: dict) -> tuple[str | None, str | None]:
    """Fetch decklists for both decks and return as a tuple."""
    decklist_a = None
    decklist_b = None

    draft_id_a = deck_a.get("cubecobra_draft_id")
    seat_a = deck_a.get("seat", 0)
    if draft_id_a:
        print(f"Fetching decklist for {deck_a['drafter']} (seat {seat_a})...")
        decklist_a = fetch_decklist(draft_id_a, seat_a)
        if decklist_a:
            print(f"Decklist ready: {len(decklist_a.splitlines())} cards")
        else:
            print(f"Could not fetch decklist for {deck_a['drafter']}.")

    draft_id_b = deck_b.get("cubecobra_draft_id")
    seat_b = deck_b.get("seat", 0)
    if draft_id_b:
        print(f"Fetching decklist for {deck_b['drafter']} (seat {seat_b})...")
        decklist_b = fetch_decklist(draft_id_b, seat_b)
        if decklist_b:
            print(f"Decklist ready: {len(decklist_b.splitlines())} cards")
        else:
            print(f"Could not fetch decklist for {deck_b['drafter']}.")

    return decklist_a, decklist_b


# ── Cube Clash integration ────────────────────────────────────────────────────

def push_decks_to_clash(deck_a: dict, deck_b: dict, decklist_a: str | None, decklist_b: str | None):
    """Push this week's decks to the Cube Clash server."""
    clash_url = os.environ.get("CLASH_URL")
    clash_secret = os.environ.get("CLASH_SECRET")
    print(f"DEBUG: CLASH_URL present={bool(clash_url)}, CLASH_SECRET present={bool(clash_secret)}")
    if not clash_url or not clash_secret:
        print("Warning: CLASH_URL or CLASH_SECRET not set, skipping Cube Clash update.")
        return False

    def parse_decklist(decklist: str | None) -> list[dict]:
        if not decklist:
            return []
        cards = []
        for line in decklist.strip().splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) == 2:
                name = parts[1]
                cards.append({"name": name, "imageUrl": None})
        return cards

    payload = {
        "secret": clash_secret,
        "weekLabel": f"{deck_a['drafter']} vs {deck_b['drafter']} — {deck_a['event']}",
        "deckA": {
            "name": deck_a["event"],
            "drafter": deck_a["drafter"],
            "cards": parse_decklist(decklist_a),
        },
        "deckB": {
            "name": deck_b["event"],
            "drafter": deck_b["drafter"],
            "cards": parse_decklist(decklist_b),
        },
    }

    try:
        resp = requests.post(f"{clash_url}/api/set-decks", json=payload, timeout=15)
        resp.raise_for_status()
        print(f"Pushed decks to Cube Clash. Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"Warning: could not push decks to Cube Clash: {e}")
        return False


# ── Discord posting ───────────────────────────────────────────────────────────

def post_to_discord(deck_a: dict, deck_b: dict, decklist_a: str | None, decklist_b: str | None, history_reset: bool, clash_url: str | None = None):
    def format_deck_info(deck: dict, label: str) -> str:
        return f"**{label}: {deck['drafter']}'s Trophy Deck**\n📅 {deck['event']}"

    content_lines = [
        "⚔️ **CLASH OF THE WISE!!** ⚔️",
        "Two grimoires of unparalleled power lay before ye...",
        "",
        format_deck_info(deck_a, "Deck A"),
        "",
        format_deck_info(deck_b, "Deck B"),
    ]

    if history_reset:
        content_lines.append("\n*All matchups have been featured — starting a fresh cycle!* 🔄")

    if clash_url:
        content_lines.append("")
        content_lines.append(f"⚔️ **Play these decks:** {clash_url}")

    content = "\n".join(content_lines)

    poll = {
        "question": {"text": "...whiche spellbook do ye choose?"},
        "answers": [
            {"poll_media": {"text": f"Deck A — {deck_a['drafter']}", "emoji": {"name": "🅰️"}}},
            {"poll_media": {"text": f"Deck B — {deck_b['drafter']}", "emoji": {"name": "🅱️"}}},
        ],
        "duration": 36,
        "allow_multiselect": False,
    }

    bot_headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "content": content,
        "poll": poll,
    }

    url = f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages"
    resp = requests.post(url, json=payload, headers=bot_headers, timeout=15)
    if not resp.ok:
        print(f"Discord error response: {resp.text}")
    resp.raise_for_status()
    print(f"Posted trophy battle. Status: {resp.status_code}")

    # Post deck images and decklists as file attachments
    image_url_a = f"{GITHUB_RAW_BASE}/{deck_a['image']}"
    image_url_b = f"{GITHUB_RAW_BASE}/{deck_b['image']}"

    files = {}
    form_content = f"🅰️ **Deck A — {deck_a['drafter']}**     _VS_     🅱️ **Deck B — {deck_b['drafter']}**"

    try:
        img_a = requests.get(image_url_a, timeout=15)
        img_a.raise_for_status()
        ext_a = deck_a['image'].split('.')[-1]
        files["files[0]"] = (f"deck_a_{deck_a['drafter']}.{ext_a}", img_a.content, f"image/{ext_a}")
    except Exception as e:
        print(f"Warning: could not fetch Deck A image: {e}")

    try:
        img_b = requests.get(image_url_b, timeout=15)
        img_b.raise_for_status()
        ext_b = deck_b['image'].split('.')[-1]
        files["files[1]"] = (f"deck_b_{deck_b['drafter']}.{ext_b}", img_b.content, f"image/{ext_b}")
    except Exception as e:
        print(f"Warning: could not fetch Deck B image: {e}")

    if decklist_a:
        files["files[2]"] = (f"Deck_A_{deck_a['drafter']}_decklist.txt", decklist_a.encode("utf-8"), "text/plain")
    if decklist_b:
        files["files[3]"] = (f"Deck_B_{deck_b['drafter']}_decklist.txt", decklist_b.encode("utf-8"), "text/plain")

    if files:
        file_headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
        file_resp = requests.post(
            f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=file_headers,
            data={"payload_json": json.dumps({"content": form_content})},
            files=files,
            timeout=30,
        )
        if not file_resp.ok:
            print(f"Discord file upload error: {file_resp.text}")
        else:
            print(f"Posted images and decklists. Status: {file_resp.status_code}")


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

    print("Fetching decklists...")
    decklist_a, decklist_b = fetch_both_decklists(deck_a, deck_b)

    clash_url = os.environ.get("CLASH_URL")
    print("Pushing decks to Cube Clash...")
    push_decks_to_clash(deck_a, deck_b, decklist_a, decklist_b)

    print("Posting to Discord...")
    post_to_discord(deck_a, deck_b, decklist_a, decklist_b, history_reset, clash_url=clash_url)

    pair = sorted([deck_id(deck_a), deck_id(deck_b)])
    if history_reset:
        history = [pair]
    else:
        history.append(pair)
    save_matchup_history(history, deck_a, deck_b)

    print("Done!")


if __name__ == "__main__":
    main()
