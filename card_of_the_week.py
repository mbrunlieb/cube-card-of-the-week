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
import sys
import time
from collections import defaultdict
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


# ── Win rate data ─────────────────────────────────────────────────────────────
# Cube Cobra no longer embeds a precomputed per-card winrate blob in the records
# page. It embeds the raw records instead (players, match results, draft IDs), so
# we compute per-card stats ourselves:
#   records page  -> who played whom and the results, plus each record's draft ID
#   deck pages    -> every seat's deck for that draft
#   combine       -> for each card, the decks it was in and how those decks did

SCRYFALL_HEADERS = {
    "User-Agent": "CubeCardOfTheWeekBot/1.0 (github.com/mbrunlieb/cube-card-of-the-week)",
    "Accept": "application/json",
}


def _fix_js_literals(text: str) -> str:
    """Cube Cobra's embedded props are a JS literal, not strict JSON: 'undefined' -> null."""
    return re.sub(r'([:,\[]\s*)undefined(?=\s*[,}\]])', r"\1null", text)


def extract_react_props(html: str) -> dict | None:
    """Pull the window.reactProps object out of a Cube Cobra page."""
    i = html.find("window.reactProps")
    if i == -1:
        return None
    start = html.find("{", i)
    end = html.find("</script>", start)
    chunk = _fix_js_literals(html[start:end if end != -1 else None])
    try:
        obj, _ = json.JSONDecoder().raw_decode(chunk)
        return obj
    except json.JSONDecodeError as e:
        print(f"Warning: could not parse reactProps: {e}")
        return None


def find_records(obj, depth: int = 0) -> list[dict] | None:
    """Find the list of records (dicts with 'matches') anywhere in the props."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        recs = obj.get("records")
        if isinstance(recs, list) and recs and isinstance(recs[0], dict) and "matches" in recs[0]:
            return recs
        for v in obj.values():
            found = find_records(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj[:50]:
            found = find_records(v, depth + 1)
            if found:
                return found
    return None


def fetch_draft(draft_id: str) -> tuple[list[dict], list[dict]] | None:
    """Fetch a draft's deck page and return (cards, seats)."""
    url = f"https://cubecobra.com/cube/deck/{draft_id}?seat=0"
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    html = _fix_js_literals(resp.text)

    seats_m = re.search(r'"seats"\s*:\s*\[', html)
    if not seats_m:
        print(f"  Warning: no seats array on deck page for draft {draft_id}")
        return None
    decoder = json.JSONDecoder()
    seats, _ = decoder.raw_decode(html, seats_m.end() - 1)

    cards_pos = -1
    for m in re.finditer(r'"cards"\s*:\s*\[', html[:seats_m.start()]):
        cards_pos = m.end() - 1  # keep the last one before "seats"
    if cards_pos == -1:
        print(f"  Warning: no cards array on deck page for draft {draft_id}")
        return None
    cards, _ = decoder.raw_decode(html, cards_pos)
    return cards, seats


def _flatten_ints(x):
    if isinstance(x, bool):
        return
    if isinstance(x, int):
        yield x
    elif isinstance(x, list):
        for item in x:
            yield from _flatten_ints(item)


def _norm(name) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def seat_identity(seat: dict) -> dict:
    """
    A seat's `name` is the DECK's label (e.g. 'WUR Artifacts', 'BG'), not the player's name.
    The only real player identity is `owner`, present only when the drafter was a Cube Cobra user.
    """
    owner = seat.get("owner")
    if isinstance(owner, dict):
        return {"owner_id": owner.get("id") or owner.get("_id"), "owner_name": _norm(owner.get("username"))}
    return {"owner_id": owner if isinstance(owner, str) else None, "owner_name": ""}


def match_players_to_seats(players: list[dict], seats: list[dict]) -> dict[str, int]:
    """
    Map record player names -> seat index.
      1. Logged-in drafters: match record userId / username to the seat owner.
      2. Everyone else: record players are listed in seat order, so when the player
         and seat counts agree, player i is the drafter of seat i.
    """
    idents = [seat_identity(s) for s in seats]
    result: dict[str, int] = {}
    used: set[int] = set()

    for p in players:  # 1a: user ID
        uid = p.get("userId")
        if not uid:
            continue
        for i, ident in enumerate(idents):
            if i not in used and ident["owner_id"] and str(ident["owner_id"]) == str(uid):
                result[p["name"]] = i
                used.add(i)
                break
    for p in players:  # 1b: Cube Cobra username
        if p["name"] in result:
            continue
        n = _norm(p["name"])
        for i, ident in enumerate(idents):
            if i not in used and n and n == ident["owner_name"]:
                result[p["name"]] = i
                used.add(i)
                break

    if len(players) == len(seats):  # 2: seat order
        for i, p in enumerate(players):
            if p["name"] not in result and i not in used:
                result[p["name"]] = i
                used.add(i)
    return result


def owner_consistency(players: list[dict], seats: list[dict], mapping: dict[str, int]) -> tuple[int, int]:
    """Sanity check: for seats with a known owner, did the mapped player match that owner?"""
    ok = total = 0
    for p in players:
        i = mapping.get(p["name"])
        if i is None:
            continue
        ident = seat_identity(seats[i])
        if not (ident["owner_id"] or ident["owner_name"]):
            continue
        total += 1
        if (p.get("userId") and str(p["userId"]) == str(ident["owner_id"])) or _norm(p["name"]) == ident["owner_name"]:
            ok += 1
    return ok, total


def player_results(record: dict) -> dict[str, dict]:
    """Per-player match/game results from a record's rounds."""
    out = defaultdict(lambda: {"mw": 0, "ml": 0, "md": 0, "gw": 0, "gl": 0})
    for rnd in record.get("matches", []):
        for m in rnd.get("matches", []):
            p1, p2 = m.get("p1"), m.get("p2")
            r = m.get("results") or []
            if not p1 or not p2 or len(r) < 2:
                continue  # byes / incomplete
            a, b = r[0], r[1]
            for me, opp, g_for, g_against in ((p1, p2, a, b), (p2, p1, b, a)):
                out[me]["gw"] += g_for
                out[me]["gl"] += g_against
                if g_for > g_against:
                    out[me]["mw"] += 1
                elif g_for < g_against:
                    out[me]["ml"] += 1
                else:
                    out[me]["md"] += 1
    return out


def scryfall_oracle_ids(card_ids: list[str]) -> dict[str, str]:
    """Resolve Scryfall card IDs -> oracle IDs."""
    resolved = {}
    for i in range(0, len(card_ids), 75):
        chunk = card_ids[i:i + 75]
        try:
            resp = requests.post(
                "https://api.scryfall.com/cards/collection",
                json={"identifiers": [{"id": cid} for cid in chunk]},
                headers=SCRYFALL_HEADERS, timeout=30,
            )
            if not resp.ok:
                print(f"  Scryfall lookup error {resp.status_code}: {resp.text[:200]}")
                continue
            for c in resp.json().get("data", []):
                if c.get("oracle_id"):
                    resolved[c["id"]] = c["oracle_id"]
            time.sleep(0.1)
        except Exception as e:
            print(f"  Warning: Scryfall oracle lookup failed: {e}")
    return resolved


def fetch_winrate_data(cube_cards: list[dict] | None = None, verbose: bool = False) -> dict:
    """
    Compute per-card stats from the cube's records.
    Returns {oracle_id: {decks, matchWins, matchLosses, matchDraws, gameWins, gameLosses, trophies}}.
    """
    resp = requests.get(CUBE_RECORDS_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    props = extract_react_props(resp.text)
    records = find_records(props) if props else None
    if not records:
        print("Warning: could not locate records in the records page.")
        return {}
    print(f"Found {len(records)} records.")

    # cardID (printing) -> oracle_id, from the cube list we already have
    cube_map = {}
    for c in cube_cards or []:
        cid, oid = c.get("cardID"), (c.get("details") or {}).get("oracle_id")
        if cid and oid:
            cube_map[cid] = oid

    # Phase 1: gather every deck (as card IDs) with its owner's results
    decks = []  # {"oracles": set, "unresolved": set(cardIDs), "res": {...}, "trophy": bool}
    for rec in records:
        draft_id = rec.get("draft")
        label = (rec.get("name") or "").strip()
        if not draft_id:
            print(f"  Skipping '{label}': no draft ID")
            continue
        try:
            draft = fetch_draft(draft_id)
        except Exception as e:
            print(f"  Warning: could not fetch draft for '{label}': {e}")
            continue
        time.sleep(0.5)
        if not draft:
            continue
        cards, seats = draft
        if verbose and not decks:
            print(f"  Seat keys (first draft): {sorted(seats[0].keys()) if seats else 'none'}")
        mapping = match_players_to_seats(rec.get("players", []), seats)
        results = player_results(rec)
        trophy_names = set(rec.get("trophy") or [])
        unmatched = [p["name"] for p in rec.get("players", []) if p["name"] not in mapping]
        ok, total = owner_consistency(rec.get("players", []), seats, mapping)
        print(f"  {label}: {len(mapping)}/{len(rec.get('players', []))} players matched "
              f"({len(rec.get('players', []))} players, {len(seats)} seats; owner check {ok}/{total})"
              + (f" UNMATCHED: {unmatched}" if unmatched else ""))
        if verbose and total and ok < total:
            print("    WARNING: some seat owners don't match the player assigned to that seat")

        for pname, seat_idx in mapping.items():
            oracles, unresolved = set(), set()
            for idx in set(_flatten_ints(seats[seat_idx].get("mainboard", []))):
                if idx >= len(cards):
                    continue
                card = cards[idx]
                cid = card.get("cardID") or (card.get("details") or {}).get("scryfall_id")
                oid = (card.get("details") or {}).get("oracle_id") or cube_map.get(cid)
                if oid:
                    oracles.add(oid)
                elif cid:
                    unresolved.add(cid)
            decks.append({"oracles": oracles, "unresolved": unresolved,
                          "res": results.get(pname, {"mw": 0, "ml": 0, "md": 0, "gw": 0, "gl": 0}),
                          "trophy": pname in trophy_names})

    # Phase 2: resolve any printings not in the current cube list
    all_unresolved = sorted({cid for d in decks for cid in d["unresolved"]})
    if all_unresolved:
        print(f"Resolving {len(all_unresolved)} card printings via Scryfall…")
        resolved = scryfall_oracle_ids(all_unresolved)
        for d in decks:
            d["oracles"] |= {resolved[c] for c in d["unresolved"] if c in resolved}

    # Phase 3: aggregate per card
    stats: dict[str, dict] = {}
    for d in decks:
        for oid in d["oracles"]:
            st = stats.setdefault(oid, {"decks": 0, "matchWins": 0, "matchLosses": 0, "matchDraws": 0,
                                        "gameWins": 0, "gameLosses": 0, "trophies": 0})
            st["decks"] += 1
            st["matchWins"] += d["res"]["mw"]
            st["matchLosses"] += d["res"]["ml"]
            st["matchDraws"] += d["res"]["md"]
            st["gameWins"] += d["res"]["gw"]
            st["gameLosses"] += d["res"]["gl"]
            st["trophies"] += 1 if d["trophy"] else 0
    print(f"Computed winrate data for {len(stats)} cards from {len(decks)} decks.")
    return stats


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
    md = stats.get("matchDraws", 0)
    total_matches = mw + ml + md  # a drawn match counts as played, not won (same as Cube Cobra)
    if total_matches == 0:
        return None
    match_wr = round(100 * mw / total_matches, 1)
    record_str = f"{mw}W–{ml}L" + (f"–{md}D" if md else "")
    gw = stats.get("gameWins", 0)
    gl = stats.get("gameLosses", 0)
    total_games = gw + gl
    game_wr = round(100 * gw / total_games, 1) if total_games > 0 else 0
    trophies = stats.get("trophies", 0)
    trophy_str = f" 🏆 {trophies} {'trophy' if trophies == 1 else 'trophies'}"
    return (f"Match: {match_wr}% ({record_str}) | "
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

    intro = "🧝 **SCROLL of the week!!** 🧙"
    if history_reset:
        intro += "\n*Every card has been featured — starting a fresh cycle!* 🔄"

    poll = {
        "question": {"text": f"How do you feel about {name}? You can be honest, no one will be mad at you."},
        "answers": [
            {"poll_media": {"text": "Oh god thank you (top 25 cube card)", "emoji": {"name": "💎"}}},
            {"poll_media": {"text": "Yes. (Bomb)", "emoji": {"name": "🚬"}}},
            {"poll_media": {"text": "OK! (Solid playable)", "emoji": {"name": "🥣"}}},
            {"poll_media": {"text": "Ugh, fine. (Filler)", "emoji": {"name": "🎡"}}},
            {"poll_media": {"text": "Why is this in the cube? (Mike, cut this)", "emoji": {"name": "🚱"}}},
        ],
        "duration": 24,
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
        winrate_data = fetch_winrate_data(cards)
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


def probe_winrate(card_name: str):
    """Diagnostic: compute winrate data and print the numbers for one card (no Discord post)."""
    cards = fetch_cube_cards()
    data = fetch_winrate_data(cards, verbose=True)
    target = next((c for c in cards if (c.get("details") or {}).get("name", "").lower() == card_name.lower()), None)
    if not target:
        print(f"'{card_name}' not found in the cube list.")
        return
    oid = target["details"]["oracle_id"]
    st = data.get(oid)
    print(f"\n=== {card_name} ===")
    if not st:
        print("No data for this card.")
    else:
        total = st["matchWins"] + st["matchLosses"] + st["matchDraws"]
        print(f"decks={st['decks']}  matches={total}  record={st['matchWins']}W-{st['matchLosses']}L-{st['matchDraws']}D  "
              f"games={st['gameWins']}-{st['gameLosses']}  trophies={st['trophies']}")
    print(f"Discord string would be: {format_winrate(oid, data)}")
    print("Cube Cobra's own page showed Uro, Titan of Nature's Wrath at 9 decks / 28 matches.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe-winrate":
        probe_winrate(" ".join(sys.argv[2:]) or "Uro, Titan of Nature's Wrath")
    else:
        main()
