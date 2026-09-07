#!/usr/bin/env python3
"""
Trophy Battle bot for MTG Cube Discord.
Picks two random trophy decks, posts their images (paper-deck photo if one exists,
otherwise an auto-generated grid built from Scryfall card images) and decklists,
and runs a poll asking which deck is stronger.
Tracks matchup history to avoid repeats and prevents same-drafter matchups.
"""

import argparse
import json
import os
import random
import re
import time
from datetime import datetime

import requests
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = os.environ["DISCORD_CHANNEL_ID"]
TROPHY_DECKS_FILE = "trophy_decks.json"
TROPHY_HISTORY_FILE = "trophy_battle_history.json"
CURRENT_WEEK_FILE = "current_week.json"  # snapshot of this week's Clash decks; server reloads it on restart

DISCORD_API = "https://discord.com/api/v10"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/mbrunlieb/cube-card-of-the-week/main"
HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0"}
# Scryfall requires both User-Agent and Accept headers or it may reject the request.
SCRYFALL_HEADERS = {"User-Agent": "CubeCardOfTheWeekBot/1.0 (github.com/mbrunlieb/cube-card-of-the-week)", "Accept": "application/json"}

# Card data captured during decklist fetching (keyed by name) so we don't
# have to re-query Scryfall by name later.
SCRYFALL_CACHE: dict[str, dict] = {}


def scryfall_entry(card: dict) -> dict | None:
    """Turn a Scryfall card object into our {front, back, cmc, type_line} entry."""
    faces = card.get("card_faces") or []
    front = (
        card.get("image_uris", {}).get("normal")
        or (faces[0].get("image_uris", {}).get("normal") if faces else None)
    )
    if not front:
        return None
    return {
        "front": front,
        "back": faces[1].get("image_uris", {}).get("normal") if len(faces) >= 2 else None,
        "cmc": card.get("cmc", 0),
        "type_line": card.get("type_line") or (faces[0].get("type_line", "") if faces else ""),
    }


def cache_card(card: dict):
    entry = scryfall_entry(card)
    name = card.get("name", "")
    if entry and name:
        SCRYFALL_CACHE[name] = entry
        if "//" in name:
            SCRYFALL_CACHE[name.split("//")[0].strip()] = entry

# ── History tracking ──────────────────────────────────────────────────────────

def load_trophy_decks() -> list[dict]:
    if not os.path.exists(TROPHY_DECKS_FILE):
        raise FileNotFoundError(f"{TROPHY_DECKS_FILE} not found!")
    with open(TROPHY_DECKS_FILE, "r") as f:
        decks = json.load(f)
    ready = [d for d in decks if d.get("cubecobra_draft_id")]
    with_photo = sum(1 for d in ready if d.get("image"))
    print(f"Loaded {len(decks)} total trophy decks, {len(ready)} usable "
          f"({with_photo} with photos, {len(ready) - with_photo} will use generated images).")
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
    """Stable identifier for a deck. Prefers draft ID + seat so decks without photos work."""
    if deck.get("cubecobra_draft_id"):
        return f"{deck['cubecobra_draft_id']}_seat{deck.get('seat', 0)}"
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
    # Cards may have name directly, via details, or only via cardID (Scryfall ID)
    card_names = []
    card_ids_to_lookup = []

    for idx in indices:
        if idx < len(cards):
            card = cards[idx]
            name = (card.get("details") or {}).get("name") or card.get("name")
            if name:
                card_names.append(name)
            else:
                card_id = card.get("cardID") or (card.get("details") or {}).get("scryfall_id")
                if card_id:
                    card_ids_to_lookup.append(card_id)

    # Look up any missing names via Scryfall collection API
    if card_ids_to_lookup:
        print(f"Looking up {len(card_ids_to_lookup)} card names via Scryfall...")
        for i in range(0, len(card_ids_to_lookup), 75):
            chunk = card_ids_to_lookup[i:i + 75]
            try:
                resp = requests.post(
                    "https://api.scryfall.com/cards/collection",
                    json={"identifiers": [{"id": cid} for cid in chunk]},
                    headers=SCRYFALL_HEADERS,
                    timeout=15,
                )
                if not resp.ok:
                    print(f"Scryfall ID lookup error {resp.status_code}: {resp.text[:300]}")
                for c in resp.json().get("data", []):
                    card_names.append(c["name"])
                    cache_card(c)
                time.sleep(0.1)
            except Exception as e:
                print(f"Warning: Scryfall lookup failed: {e}")

    if not card_names:
        print(f"Warning: could not resolve any card names for seat {seat}.")
        return None

    print(f"Found {len(card_names)} cards for seat {seat}.")
    # Use only the first face name for double-faced cards
    clean_names = [name.split("//")[0].strip() for name in card_names]
    lines = [f"1 {name}" for name in sorted(clean_names)]
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

def fetch_scryfall_images(card_names: list[str]) -> dict[str, dict]:
    """
    Return a dict of name -> {"front", "back", "cmc", "type_line"} for the given cards.
    Uses data already cached from the decklist ID lookup; only queries Scryfall
    by name for cards that aren't cached.
    """
    image_map = {}
    missing = []
    for name in card_names:
        entry = SCRYFALL_CACHE.get(name) or SCRYFALL_CACHE.get(name.split("//")[0].strip())
        if entry:
            image_map[name] = entry
        else:
            missing.append(name)

    print(f"{len(image_map)} cards already cached from ID lookup; {len(missing)} need a name lookup.")

    for i in range(0, len(missing), 75):
        chunk = missing[i:i + 75]
        try:
            resp = requests.post(
                "https://api.scryfall.com/cards/collection",
                json={"identifiers": [{"name": n} for n in chunk]},
                headers=SCRYFALL_HEADERS,
                timeout=30,
            )
            if not resp.ok:
                print(f"Scryfall name lookup error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
            data = resp.json()
            for card in data.get("data", []):
                cache_card(card)
                name = card.get("name", "")
                entry = SCRYFALL_CACHE.get(name)
                if entry:
                    image_map[name] = entry
                    if "//" in name:
                        image_map[name.split("//")[0].strip()] = entry
            for nf in data.get("not_found", []):
                print(f"Scryfall could not find: {nf}")
            time.sleep(0.1)
        except Exception as e:
            print(f"Warning: Scryfall name lookup failed for chunk: {e}")

    print(f"Fetched images for {len(image_map)}/{len(card_names)} cards from Scryfall.")
    return image_map


def parse_names(decklist: str | None) -> list[str]:
    """Turn a '1 Card Name' decklist into a list of card names."""
    if not decklist:
        return []
    names = []
    for line in decklist.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            names.append(parts[1])
    return names


def push_decks_to_clash(deck_a: dict, deck_b: dict, decklist_a: str | None, decklist_b: str | None, image_map: dict):
    """
    Push this week's decks to the Cube Clash server, and save the same payload
    to current_week.json so the server can reload it after a restart.
    """
    clash_url = os.environ.get("CLASH_URL")
    clash_secret = os.environ.get("CLASH_SECRET")

    names_a = parse_names(decklist_a)
    names_b = parse_names(decklist_b)

    def build_cards(names: list[str]) -> list[dict]:
        cards = []
        for name in names:
            entry = image_map.get(name) or image_map.get(name.split("//")[0].strip())
            cards.append({
                "name": name,
                "imageUrl": entry["front"] if entry else None,
                "imageUrlBack": entry["back"] if entry else None,
            })
        return cards

    payload = {
        "weekLabel": f"{deck_a['drafter']} vs {deck_b['drafter']} — {deck_a['event'].strip()}",
        "deckA": {
            "name": deck_a["event"],
            "drafter": deck_a["drafter"],
            "cards": build_cards(names_a),
        },
        "deckB": {
            "name": deck_b["event"],
            "drafter": deck_b["drafter"],
            "cards": build_cards(names_b),
        },
    }

    # Snapshot to the repo (committed by the workflow) so Clash can self-heal on restart.
    with open(CURRENT_WEEK_FILE, "w") as f:
        json.dump({**payload, "generated_at": datetime.utcnow().isoformat()}, f, indent=2)
    print(f"Saved {CURRENT_WEEK_FILE}.")

    if not clash_url or not clash_secret:
        print("Warning: CLASH_URL or CLASH_SECRET not set, skipping Cube Clash push.")
        return False

    try:
        resp = requests.post(f"{clash_url}/api/set-decks", json={**payload, "secret": clash_secret}, timeout=15)
        resp.raise_for_status()
        print(f"Pushed decks to Cube Clash. Status: {resp.status_code}")
        return True
    except Exception as e:
        print(f"Warning: could not push decks to Cube Clash: {e} (server will pick up {CURRENT_WEEK_FILE} on its next restart)")
        return False

# ── Deck image generation ─────────────────────────────────────────────────────
# Used as a fallback for trophy decks that don't have a paper-deck photo yet.

GRID_COLUMNS = 8
CARD_W, CARD_H = 292, 408          # Scryfall "normal" is 488x680; 60% keeps Discord uploads ~1MB
CARD_GAP = 8
HEADER_H = 90
BG_COLOR = (24, 24, 28)
TEXT_COLOR = (235, 235, 235)
SUBTEXT_COLOR = (170, 170, 180)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _sort_key(name: str, image_map: dict):
    entry = image_map.get(name) or image_map.get(name.split("//")[0].strip()) or {}
    is_land = "Land" in (entry.get("type_line") or "")
    return (is_land, entry.get("cmc", 0), name.lower())


def generate_deck_image(deck: dict, decklist: str | None, image_map: dict) -> bytes | None:
    """Build a grid image of the deck from Scryfall card images. Returns JPEG bytes or None."""
    names = parse_names(decklist)
    if not names:
        print(f"Cannot generate image for {deck['drafter']}: no decklist.")
        return None

    names = sorted(names, key=lambda n: _sort_key(n, image_map))
    cols = min(GRID_COLUMNS, len(names))
    rows = (len(names) + cols - 1) // cols
    width = cols * CARD_W + (cols + 1) * CARD_GAP
    height = HEADER_H + rows * CARD_H + (rows + 1) * CARD_GAP

    canvas = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    draw.text((CARD_GAP * 2, 14), f"{deck['drafter']}'s Trophy Deck", fill=TEXT_COLOR, font=_font(34))
    draw.text((CARD_GAP * 2, 56), f"{deck['event'].strip()}  |  {len(names)} cards", fill=SUBTEXT_COLOR, font=_font(20))

    fetched = 0
    for i, name in enumerate(names):
        entry = image_map.get(name) or image_map.get(name.split("//")[0].strip())
        x = CARD_GAP + (i % cols) * (CARD_W + CARD_GAP)
        y = HEADER_H + CARD_GAP + (i // cols) * (CARD_H + CARD_GAP)
        card_img = None
        if entry and entry.get("front"):
            try:
                r = requests.get(entry["front"], headers={"User-Agent": SCRYFALL_HEADERS["User-Agent"]}, timeout=15)
                r.raise_for_status()
                card_img = Image.open(BytesIO(r.content)).convert("RGB").resize((CARD_W, CARD_H), Image.LANCZOS)
                fetched += 1
                time.sleep(0.08)  # be polite to Scryfall
            except Exception as e:
                print(f"Warning: could not fetch image for {name}: {e}")
        if card_img is None:
            # Placeholder tile with the card name
            card_img = Image.new("RGB", (CARD_W, CARD_H), (60, 60, 68))
            d = ImageDraw.Draw(card_img)
            d.text((12, 12), name, fill=TEXT_COLOR, font=_font(18))
        canvas.paste(card_img, (x, y))

    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=85, optimize=True)
    print(f"Generated deck image for {deck['drafter']}: {fetched}/{len(names)} card images, {buf.tell() // 1024} KB")
    return buf.getvalue()


def get_deck_image_bytes(deck: dict, decklist: str | None, image_map: dict) -> tuple[bytes | None, str, bool]:
    """
    Return (image_bytes, extension, generated) for a deck.
    Uses the photo from the repo when one is registered; otherwise generates a grid.
    """
    if deck.get("image"):
        url = f"{GITHUB_RAW_BASE}/{deck['image']}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.content, deck["image"].split(".")[-1], False
        except Exception as e:
            print(f"Warning: could not fetch photo for {deck['drafter']} ({e}); falling back to generated image.")
    return generate_deck_image(deck, decklist, image_map), "jpg", True


# ── Discord posting ───────────────────────────────────────────────────────────

def post_to_discord(deck_a: dict, deck_b: dict, decklist_a: str | None, decklist_b: str | None,
                    history_reset: bool, clash_url: str | None = None,
                    image_a: tuple | None = None, image_b: tuple | None = None):
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
    files = {}
    form_content = f"🅰️ **Deck A — {deck_a['drafter']}**     _VS_     🅱️ **Deck B — {deck_b['drafter']}**"

    generated_any = False
    for slot, label, deck, img in (("files[0]", "a", deck_a, image_a), ("files[1]", "b", deck_b, image_b)):
        if not img or not img[0]:
            print(f"Warning: no image available for Deck {label.upper()}.")
            continue
        data, ext, generated = img
        generated_any = generated_any or generated
        mime_ext = "jpeg" if ext.lower() in ("jpg", "jpeg") else ext.lower()
        files[slot] = (f"deck_{label}_{deck['drafter']}.{ext}", data, f"image/{mime_ext}")

    if generated_any:
        form_content += "\n-# Grid images are auto-generated from the decklist where no paper-deck photo exists."

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
        print("Not enough trophy decks with draft IDs to run a battle. Exiting.")
        return

    print("Loading matchup history...")
    history = load_matchup_history()
    print(f"Past matchups: {len(history)}")

    print("Picking matchup...")
    deck_a, deck_b, history_reset = pick_matchup(decks, history)
    print(f"Matchup: {deck_a['drafter']} vs {deck_b['drafter']}")

    print("Fetching decklists...")
    decklist_a, decklist_b = fetch_both_decklists(deck_a, deck_b)

    all_names = list(set(parse_names(decklist_a) + parse_names(decklist_b)))
    print(f"Fetching Scryfall images for {len(all_names)} unique cards...")
    image_map = fetch_scryfall_images(all_names)

    clash_url = os.environ.get("CLASH_URL")
    print("Pushing decks to Cube Clash...")
    push_decks_to_clash(deck_a, deck_b, decklist_a, decklist_b, image_map)

    print("Preparing deck images...")
    image_a = get_deck_image_bytes(deck_a, decklist_a, image_map)
    image_b = get_deck_image_bytes(deck_b, decklist_b, image_map)

    print("Posting to Discord...")
    post_to_discord(deck_a, deck_b, decklist_a, decklist_b, history_reset,
                    clash_url=clash_url, image_a=image_a, image_b=image_b)

    pair = sorted([deck_id(deck_a), deck_id(deck_b)])
    if history_reset:
        history = [pair]
    else:
        history.append(pair)
    save_matchup_history(history, deck_a, deck_b)

    print("Done!")


def preview_deck_image(drafter: str | None = None):
    """
    Test helper: generate (and post to Discord) the grid image for one deck.
    Does NOT run a poll, push to Cube Clash, or touch history.
    Picks by drafter name if given, otherwise a random deck without a photo.
    """
    decks = load_trophy_decks()
    if drafter:
        candidates = [d for d in decks if d["drafter"].lower() == drafter.lower()]
        if not candidates:
            print(f"No deck found for drafter '{drafter}'. Known drafters: "
                  + ", ".join(sorted({d['drafter'] for d in decks})))
            return
    else:
        candidates = [d for d in decks if not d.get("image")] or decks
    deck = random.choice(candidates)
    print(f"Previewing generated image for {deck['drafter']} — {deck['event'].strip()}")

    decklist = fetch_decklist(deck["cubecobra_draft_id"], deck.get("seat", 0))
    if not decklist:
        print("Could not fetch decklist; aborting preview.")
        return

    names = parse_names(decklist)
    image_map = fetch_scryfall_images(names)
    data = generate_deck_image(deck, decklist, image_map)
    if not data:
        print("Image generation failed.")
        return

    resp = requests.post(
        f"{DISCORD_API}/channels/{DISCORD_CHANNEL_ID}/messages",
        headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
        data={"payload_json": json.dumps({"content": f"🧪 **Deck image preview** — {deck['drafter']} ({deck['event'].strip()})"})},
        files={"files[0]": (f"preview_{deck['drafter']}.jpg", data, "image/jpeg")},
        timeout=30,
    )
    if not resp.ok:
        print(f"Discord error: {resp.text}")
    resp.raise_for_status()
    print(f"Posted preview. Status: {resp.status_code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-image", nargs="?", const="", metavar="DRAFTER",
                        help="Only generate and post a deck grid image (optionally for a specific drafter).")
    args = parser.parse_args()
    if args.preview_image is not None:
        preview_deck_image(args.preview_image or None)
    else:
        main()
