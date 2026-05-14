#!/usr/bin/env python3
"""
Wednesday P1P1 bot for MTG Cube Discord.
Generates a random pack from the cube, creates a P1P1 poll on Cube Cobra,
and posts the pack image and link to Discord.
"""

import json
import os
import random
import time

import requests

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
CUBECOBRA_SESSION = os.environ["CUBECOBRA_SESSION"]

CUBE_ID = "tm1"
CUBE_INTERNAL_ID = "60ba7b55a2494110485dc479"
CUBE_JSON_URL = f"https://cubecobra.com/cube/api/cubeJSON/{CUBE_ID}"
CREATE_P1P1_URL = "https://cubecobra.com/tool/api/createp1p1frompack"

DISCORD_API = "https://discord.com/api/v10"
HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}

PACK_SIZE = 16

# ── Fetch cube cards ──────────────────────────────────────────────────────────

def fetch_cube_cards() -> list[dict]:
    """Fetch the cube card list from Cube Cobra."""
    resp = requests.get(CUBE_JSON_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    cards = data.get("cards", {}).get("mainboard", [])
    if not cards:
        raise ValueError("No mainboard cards found.")
    print(f"Fetched {len(cards)} cards from cube.")
    return cards

# ── Generate pack ─────────────────────────────────────────────────────────────

def generate_pack(cards: list[dict]) -> list[dict]:
    """Pick PACK_SIZE random cards to form a pack."""
    pack = random.sample(cards, min(PACK_SIZE, len(cards)))
    print(f"Generated pack of {len(pack)} cards:")
    for c in pack:
        print(f"  - {c.get('details', {}).get('name', 'Unknown')}")
    return pack

# ── Create P1P1 poll on Cube Cobra ────────────────────────────────────────────

def create_p1p1_poll(pack: list[dict]) -> str | None:
    """
    POST the pack to Cube Cobra to create a P1P1 poll.
    Returns the poll URL, or None if it fails.
    """
    seed = str(int(time.time() * 1000))

    cards_payload = []
    for card in pack:
        card_id = card.get("cardID") or card.get("details", {}).get("scryfall_id", "")
        index = card.get("index", 0)
        cards_payload.append({"cardID": card_id, "index": index})

    payload = {
        "cubeId": CUBE_INTERNAL_ID,
        "seed": seed,
        "cards": cards_payload,
    }

    resp = requests.post(
        CREATE_P1P1_URL,
        json=payload,
        headers={
            **HEADERS,
            "Content-Type": "application/json",
            "Cookie": f"connect.sid={CUBECOBRA_SESSION}",
            "Referer": f"https://cubecobra.com/cube/playtest/{CUBE_ID}",
            "Origin": "https://cubecobra.com",
        },
        timeout=30,
    )

    if not resp.ok:
        print(f"Error creating P1P1 poll: {resp.status_code} {resp.text[:200]}")
        return None

    data = resp.json()
    if not data.get("success"):
        print(f"Cube Cobra returned success=false: {data}")
        return None

    pack_id = data.get("pack", {}).get("id")
    if not pack_id:
        print(f"No pack ID in response: {data}")
        return None

    poll_url = f"https://cubecobra.com/cube/p1p1/{pack_id}"
    print(f"Created P1P1 poll: {poll_url}")
    return poll_url

# ── Post to Discord ───────────────────────────────────────────────────────────

def post_to_discord(poll_url: str, pack_id: str):
    """Post the P1P1 poll link and pack image to Discord."""
    image_url = f"https://cubecobra.com/cube/p1p1packimage/{pack_id}"

    embed = {
        "title": "SCROLLS!",
        "color": 0x5865F2,
        "url": poll_url,
        "image": {"url": image_url},
    }

    content = (
        "📜 **SCROLLS for SALE!!** 📜\n"
        "...howls the vedalken street urchin. You pat your pockets, but damn it you only have enough coin for a single scroll... what will you choose??\n\n"
        f"🗳️ Vote here: {poll_url}"
    )

    payload = {
        "content": content,
        "embeds": [embed],
    }

    bot_headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        json=payload,
        headers=bot_headers,
        timeout=15,
    )

    if not resp.ok:
        print(f"Discord error: {resp.text}")
    resp.raise_for_status()
    print(f"Posted to Discord. Status: {resp.status_code}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Fetching cube cards...")
    cards = fetch_cube_cards()

    print("Generating pack...")
    pack = generate_pack(cards)

    print("Creating P1P1 poll on Cube Cobra...")
    poll_url = create_p1p1_poll(pack)

    if not poll_url:
        print("Failed to create poll. Exiting.")
        return

    pack_id = poll_url.split("/")[-1]

    print("Posting to Discord...")
    post_to_discord(poll_url, pack_id)

    print("Done!")


if __name__ == "__main__":
    main()
