# MTG Cube Discord Automation

Automated weekly Discord posts for the **tm1** cube on Cube Cobra, plus **Cube Clash**, a real-time browser sandbox for playtesting trophy decks against each other.

Everything runs on free/cheap infrastructure:

- **GitHub Actions** — runs the three weekly bot scripts on a cron schedule
- **Railway** — hosts the Cube Clash Node.js server
- **Discord bot token** — posts to a single channel
- **Cube Cobra + Scryfall** — data sources (cube list, records, combos, decklists, card images)

---

## How the pieces fit together

```
                 GitHub Actions (cron, Mon/Wed/Fri 6pm UTC)
                 ┌──────────────────────────────────────────┐
Cube Cobra ───▶  │ card_of_the_week.py ──▶ Discord           │
(cube JSON,      │ p1p1_bot.py         ──▶ Discord + CubeCobra poll
 records,        │ trophy_battle.py    ──▶ Discord           │
 combos,         │                     ──▶ Cube Clash (Railway)
 decklists)      │ scrape_trophies.py  ──▶ trophy_decks.json (manual)
                 └──────────────────────────────────────────┘
Scryfall ────▶ card images used by trophy_battle.py and Cube Clash
```

Two repositories:

| Repo | Purpose |
|---|---|
| `mbrunlieb/cube-card-of-the-week` | All bot scripts, workflow, history/registry JSON, deck photos |
| `mbrunlieb/cube-clash` | Cube Clash web app (Express + Socket.io + React) |

---

## Weekly schedule

All jobs run at **6:00 PM UTC** (1 PM CDT / 12 PM CST). Defined in `.github/workflows/card_of_the_week.yml`.

| Day | Job | Script | Writes back to repo? |
|---|---|---|---|
| Monday | Card of the Week | `card_of_the_week.py` | `history.json` |
| Wednesday | P1P1 | `p1p1_bot.py` | no |
| Friday | Trophy Battle | `trophy_battle.py` | `trophy_battle_history.json` |
| Manual only | Scrape Trophies | `scrape_trophies.py` | `trophy_decks.json` |

Any job can be run on demand from **GitHub → Actions → "Card of the Week" → Run workflow**, then pick the job from the dropdown.

---

## Repo 1: `cube-card-of-the-week`

### Monday — Card of the Week (`card_of_the_week.py`)

1. Fetches the cube's mainboard from `https://cubecobra.com/cube/api/cubeJSON/tm1`.
2. Picks a random card not yet in `history.json`. Lands are skipped unless tagged `spotlight` in Cube Cobra. When every eligible card has been featured, history resets and a "fresh cycle" note is added to the post.
3. Scrapes winrate data from the records page (`?view=winrate-analytics`) by locating the embedded JSON blob keyed by oracle ID. Winrate is only shown if the card appears in ≥2 decks (`MIN_DECKS_FOR_WINRATE`).
4. POSTs all oracle IDs to `/cube/api/getcombos` and lists combos involving the chosen card (max 5 shown).
5. Posts an embed (card image, winrate, combos) with a 5-option "how do you feel about this card" poll (24h).
6. Appends the oracle ID to `history.json` and the workflow commits it.

### Wednesday — P1P1 (`p1p1_bot.py`)

1. Fetches the cube list and samples 16 random cards (`PACK_SIZE`).
2. POSTs the pack to `https://cubecobra.com/tool/api/createp1p1frompack` using the stored **session cookie** (`CUBECOBRA_SESSION`). This is the only script that needs a login.
3. Posts the pack image (`/cube/p1p1packimage/{pack_id}`) and voting link to Discord.

> ⚠️ This is the fragile one. If the session cookie has expired, the job logs an error from Cube Cobra and posts nothing. See *Manual tasks → Refresh the Cube Cobra session cookie*.

### Friday — Trophy Battle (`trophy_battle.py`)

1. Loads `trophy_decks.json` and keeps only entries that have an `image`.
2. Picks a random pair of decks that (a) aren't the same drafter and (b) haven't been matched before per `trophy_battle_history.json`. Resets history when all pairs are used.
3. Fetches each decklist by scraping the Cube Cobra deck page (`/cube/deck/{draft_id}?seat={seat}`), flattening the seat's mainboard indices and resolving names (Scryfall fallback by ID when needed). Double-faced cards keep only the front-face name.
4. Fetches Scryfall images (front and back faces) and POSTs both decks to Cube Clash at `{CLASH_URL}/api/set-decks` with the shared secret. This wipes any in-progress Clash games.
5. Posts a head-to-head poll (36h), then a second message with both deck photos and decklists as `.txt` attachments, plus the Cube Clash link.
6. Appends the pair to `trophy_battle_history.json` and the workflow commits it.

### Manual — Scrape Trophies (`scrape_trophies.py`)

Scrapes the trophy archive page and adds any new trophy winners to `trophy_decks.json` with `"image": null`. Existing entries are never overwritten. It also records each drafter's `seat` index, which `trophy_battle.py` needs to pull the correct decklist. Decks with `image: null` are ignored by Trophy Battle until you add a photo.

### Files

| File | What it is | Who edits it |
|---|---|---|
| `card_of_the_week.py` | Monday script | you |
| `p1p1_bot.py` | Wednesday script | you |
| `trophy_battle.py` | Friday script | you |
| `scrape_trophies.py` | Manual trophy scraper | you |
| `.github/workflows/card_of_the_week.yml` | All schedules and manual triggers | you |
| `history.json` | Oracle IDs already featured as Card of the Week | bot |
| `trophy_battle_history.json` | Deck pairs already battled | bot |
| `trophy_decks.json` | Registry of trophy decks (drafter, event, date, draft ID, seat, image path) | scraper + you |
| `deck_images/` | Photos of trophy decks, referenced from `trophy_decks.json` | you |

### GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | Value | Used by |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token | all three posters |
| `DISCORD_CHANNEL_ID` | Target channel ID | all three posters |
| `CUBECOBRA_SESSION` | Cube Cobra `connect.sid` cookie value | P1P1 |
| `CLASH_URL` | Cube Clash base URL, e.g. `https://cube-clash-production.up.railway.app` | Trophy Battle |
| `CLASH_SECRET` | Shared secret; must match Railway's `CLASH_SECRET` | Trophy Battle |

### Cube Cobra IDs

- Cube vanity ID: `tm1`
- Cube internal ID / records ID: `60ba7b55a2494110485dc479`

---

## Repo 2: `cube-clash`

A shared two-player board with no rules enforcement. Hosted on Railway.

### Tech

- `server.js` — Express + Socket.io. Holds the current week's decks and all active games **in memory only**.
- `public/index.html` — loads React, ReactDOM, Babel standalone and Socket.io from CDN.
- `public/app.jsx` — the entire frontend.
- `railway.toml` — deployment config. Server listens on `process.env.PORT`.

### Flow

1. `trophy_battle.py` POSTs to `/api/set-decks` with the secret → server stores decks and clears games.
2. Lobby (`/api/lobby`) shows the week label, both decks, and open games.
3. A player picks a seat → a game is created (`POST /api/games`) → they land in the **decklist editor** (pre-filled, editable, fetches Scryfall images client-side) → "Ready to Play" emits `update_deck` then `join_game`.
4. Second player joins from the lobby's "Waiting for opponent" list as seat B.
5. All actions go through the `game_action` socket event; the server mutates state and broadcasts `game_state` to both players.

### Environment variable (Railway → service → Variables)

- `CLASH_SECRET` — must match the GitHub secret of the same name.

### Features quick reference

- Drag to move, double-click to tap, right-click for the full menu (zones, flip, clone, counters, give to opponent).
- Hand ribbon at bottom; graveyard/exile viewers for both players; library search (shuffle-on-close) and view-top-N.
- Custom tokens, freehand drawing overlay, separate battlefield/hand size sliders, chat + action log.
- Restart sends both players back to the deck editor; Concede ends the game; Quit returns to lobby.

---

## Manual tasks

### Refresh the Cube Cobra session cookie (when P1P1 stops posting)

1. Log in to cubecobra.com in your browser.
2. Open DevTools → **Application** (Chrome) / **Storage** (Firefox) → Cookies → `https://cubecobra.com`.
3. Copy the value of `connect.sid`.
4. GitHub → repo → Settings → Secrets and variables → Actions → edit `CUBECOBRA_SESSION` → paste.
5. Optionally re-run the `p1p1` job manually to confirm.

### Add a new trophy deck

1. Actions → Run workflow → job `scrape-trophies`. New winners are appended to `trophy_decks.json` with `image: null`. The job log lists which entries still need images.
2. Take a photo of the deck, name it `YYYY_Month_DrafterInitial.jpg` (e.g. `2026_April_BenH.jpg`), and add it to `deck_images/`.
3. Edit the entry in `trophy_decks.json`: set `"image": "deck_images/2026_April_BenH.jpg"`.
4. Commit and push. The deck is now eligible for Friday battles.

> Drafter names are matched as exact strings for the "no same-drafter matchups" rule. If one person appears under two Cube Cobra names (e.g. `TannerGold` and `ButterFriend`), they can be matched against themselves unless you normalize the `drafter` field.

### Reset history (start a fresh cycle)

- **Card of the Week:** replace `history.json` contents with `{"chosen": []}` and push.
- **Trophy Battle:** replace `trophy_battle_history.json` contents with `{"matchups": []}` and push.

The bots rebuild the full structure (`last_updated`, `total_*`, etc.) on their next run. Deleting the files outright also works — both scripts handle a missing file.

### Reload decks into Cube Clash (after a Railway restart)

Cube Clash keeps decks in memory, so any restart or redeploy clears them and the lobby shows "No decks set for this week." Fix: Actions → Run workflow → job `trophy-battle`. Note this also posts a new battle to Discord and appends to history.

### Relaunch Cube Clash on Railway from scratch

1. Railway → New Project → **Deploy from GitHub repo** → `mbrunlieb/cube-clash`.
2. Service → Variables → add `CLASH_SECRET` (same value as the GitHub secret).
3. Service → Settings → Networking → **Generate Domain**.
4. If the domain differs from before, update the `CLASH_URL` GitHub secret.
5. Run the `trophy-battle` job to push decks. Visit the URL; the lobby should show this week's matchup.

### Run any job on demand

Actions → "Card of the Week" workflow → Run workflow → choose the job. Uses the `workflow_dispatch` input in the YAML.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| P1P1 job logs `Error creating P1P1 poll: 401/403` or `success=false` | Session cookie expired | Refresh `CUBECOBRA_SESSION` |
| Card of the Week shows "Not enough data yet" for everything | Records page HTML changed and the winrate regex didn't match | Check `fetch_winrate_data()` pattern against the page source |
| Trophy Battle: "could not find cards array in deck page" | Cube Cobra deck page structure changed | Inspect the page source; adjust `fetch_decklist()` regexes |
| Trophy Battle: "Not enough trophy decks with images" | Fewer than 2 entries in `trophy_decks.json` have an image | Add deck photos |
| Cube Clash lobby says "No decks set for this week" | Railway restarted | Re-run `trophy-battle` |
| Cube Clash `/api/set-decks` returns 401 | `CLASH_SECRET` mismatch between GitHub and Railway | Make them identical |
| Workflow "Commit updated history" step fails | Branch protection or permissions | Ensure job has `permissions: contents: write` (it does) and `main` allows bot pushes |
