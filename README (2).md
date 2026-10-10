# MTG Cube Discord Automation

Automated weekly Discord posts for the **tm1** cube on Cube Cobra, plus **Cube Clash**, a real-time browser sandbox for playing trophy decks against each other.

| Day | Post | What happens |
|---|---|---|
| Monday | **Scroll of the Week** | A random cube card, with its win rate and combos, plus a "how do you feel about this card" poll |
| Wednesday | **Scrolls for Sale** | A random 16-card pack with a link to pick from on Cube Cobra |
| Friday | **Clash of the Wise** | Two trophy decks, a head-to-head poll, decklists, and a link to play them in Cube Clash |

Everything runs on free or cheap infrastructure:

- **GitHub Actions** runs the weekly scripts on a cron schedule.
- **Railway** (Hobby plan) hosts the Cube Clash server.
- **A Discord bot token** posts to one channel.
- **Cube Cobra + Scryfall** supply the data (cube list, records, combos, decks, card images).

---

## How the pieces fit together

```
                GitHub Actions  (cron: Mon / Wed / Fri, 6 PM UTC)
                ┌─────────────────────────────────────────────────────┐
Cube Cobra ───▶ │ card_of_the_week.py  ──▶ Discord                     │
 cube list      │ p1p1_bot.py          ──▶ Discord (+ Cube Cobra poll) │
 records        │ trophy_battle.py     ──▶ Discord                     │
 deck pages     │    ├─ scrape_trophies.py runs first (new winners)    │
 combos         │    ├─ builds a deck image if there is no photo       │
                │    ├──▶ Cube Clash on Railway (set-decks)            │
Scryfall ─────▶ │    └──▶ current_week.json committed to the repo      │
 card images    └─────────────────────────────────────────────────────┘
                                   │
 Cube Clash (Railway) ◀── reloads current_week.json after any restart
```

| Repo | Purpose |
|---|---|
| `mbrunlieb/cube-card-of-the-week` | Bot scripts, the workflow, history/registry JSON, deck photos |
| `mbrunlieb/cube-clash` | The Cube Clash web app (Express + Socket.io + React) |

---

## Where each file goes

Putting a file in the wrong place has caused real failures (a workflow file pasted into a `.py` file broke a Monday run). Check this table before committing.

**`cube-card-of-the-week`**

| File | Location |
|---|---|
| `card_of_the_week.py`, `p1p1_bot.py`, `trophy_battle.py`, `scrape_trophies.py` | repo root |
| `card_of_the_week.yml` | `.github/workflows/card_of_the_week.yml` |
| `history.json`, `trophy_battle_history.json`, `trophy_decks.json`, `current_week.json` | repo root (bots write these) |
| deck photos | `deck_images/` |

**`cube-clash`**

| File | Location |
|---|---|
| `server.js`, `package.json`, `railway.toml` | repo root |
| `app.jsx`, `index.html` | `public/` |

After editing `app.jsx`, hard-reload the browser (Ctrl+Shift+R). It is cached.

---

## Weekly schedule

All jobs run at **6:00 PM UTC** (1 PM CDT / 12 PM CST). They are defined in `.github/workflows/card_of_the_week.yml`.

| Day | Job | Script | Writes back to the repo |
|---|---|---|---|
| Monday | `card-of-the-week` | `card_of_the_week.py` | `history.json` |
| Wednesday | `p1p1` | `p1p1_bot.py` | nothing |
| Friday | `trophy-battle` | `trophy_battle.py` (runs `scrape_trophies.py` first) | `trophy_battle_history.json`, `trophy_decks.json`, `current_week.json` |

### Running a job by hand

GitHub → **Actions** → "Card of the Week" → **Run workflow**, then choose a job from the dropdown.

| Job | What it does | Posts to Discord? | Writes to repo? |
|---|---|---|---|
| `card-of-the-week` | The Monday job | yes | `history.json` |
| `p1p1` | The Wednesday job | yes | no |
| `trophy-battle` | The Friday job | yes | history, decks, `current_week.json` |
| `scrape-trophies` | Only the trophy scraper | no | `trophy_decks.json` |
| `preview-deck-image` | Builds one deck's grid image and posts just that image. The **drafter** box picks a deck by name; blank gives a random deck without a photo | image only | no |
| `push-clash-only` | Picks a random matchup, pushes it to Cube Clash, and writes `current_week.json`. No Discord post, no history entry | no | `current_week.json` |
| `probe-winrate` | Computes win rates and prints one card's numbers. The **drafter** box is the card name (blank = Uro) | no | no |

> `push-clash-only` overwrites `current_week.json` with its random test matchup. Until the next Friday run, a Clash restart will reload that test matchup.

---

## Repo 1: `cube-card-of-the-week`

### Monday: Scroll of the Week (`card_of_the_week.py`)

1. Fetches the cube's mainboard from `https://cubecobra.com/cube/api/cubeJSON/tm1`.
2. Picks a random card that is not in `history.json`. Lands are skipped unless tagged `spotlight` in Cube Cobra. When every eligible card has been used, history resets and the post notes a fresh cycle.
3. **Computes win rates itself** (see below).
4. POSTs all oracle IDs to `/cube/api/getcombos` and lists combos that involve the card (up to 5).
5. Posts an embed (card image, win rate, combos) with a 5-option poll that runs 24 hours.
6. Appends the oracle ID to `history.json`; the workflow commits it.

**How win rates are computed.** Cube Cobra used to embed a ready-made per-card win-rate table in the records page. It no longer does, so the script rebuilds it:

1. The records page (`/cube/records/<id>`) embeds every record in `window.reactProps`: players, match results per round, trophy winners, and a `draft` ID.
2. For each record, the script fetches the draft's deck page, which contains every seat's deck.
3. It matches each player to a seat. A seat's `name` is the deck's color label (such as `WUR Artifacts` or `BG`), **not** the drafter's name, so matching works like this:
   - Logged-in drafters match by Cube Cobra user ID or username (the seat's `owner`).
   - Everyone else matches by position, since a record lists its players in seat order. This is only used when the player count equals the seat count. Otherwise those players are skipped rather than guessed.
4. For each card it totals the decks it appeared in, plus those decks' match and game results and trophies.

Rules worth knowing:
- A drawn match counts as played, not won. This matches Cube Cobra's own page. The post shows draws only when there are any (for example `13W–9L–6D`).
- A win rate shows only if the card was in at least 2 decks (`MIN_DECKS_FOR_WINRATE`).
- Each Monday run fetches every draft page (about 11 pages today, half a second apart), so the win-rate step takes a short while. It recomputes from scratch each time.

**Checking win rates:** run `probe-winrate`. For Uro, Cube Cobra's own analytics page showed **9 decks and 28 matches**, and the script gives the same. In the log, every record line should end with all players matched, and the `owner check` should read `N/N`.

### Wednesday: Scrolls for Sale (`p1p1_bot.py`)

1. Fetches the cube list and samples 16 random cards (`PACK_SIZE`).
2. POSTs the pack to `https://cubecobra.com/tool/api/createp1p1frompack` using the stored **session cookie** (`CUBECOBRA_SESSION`). This is the only script that needs a Cube Cobra login.
3. Posts the pack image (`/cube/p1p1packimage/{pack_id}`) and a voting link.

> **This is the one fragile piece.** The cookie expires periodically. When it does, the job logs an error from Cube Cobra and posts nothing. See *Refresh the Cube Cobra session cookie* below.

### Friday: Clash of the Wise (`trophy_battle.py`)

1. Runs `scrape_trophies.py` first (failures here do not stop the battle), adding any new trophy winners to `trophy_decks.json`.
2. Treats every deck with a Cube Cobra draft ID as eligible. **A paper-deck photo is optional.**
3. Picks a random pair that are different decks, are not the same drafter name, and have not been matched before. History resets when every pair has been used.
4. Fetches each decklist from the Cube Cobra deck page (`/cube/deck/{draft_id}?seat={seat}`). Double-faced cards keep their front-face name.
5. Looks up Scryfall data for each card. Card data already fetched during the decklist step is reused, so Scryfall is only asked by name for anything missing.
6. Pushes both decks to Cube Clash (`{CLASH_URL}/api/set-decks`), and saves the same payload to `current_week.json`.
7. Posts the poll (36 hours), then a second message with both deck images, both decklists as `.txt` files, and the Cube Clash link.
8. Appends the pair to `trophy_battle_history.json`. The workflow commits that file, `trophy_decks.json`, and `current_week.json`.

**Deck images.** If a deck has a photo in `deck_images/`, that is used. Otherwise the script builds a grid image from the Scryfall card images: 8 across, sorted by mana value with lands last, with a header. The post notes when a grid image was auto-generated. If a registered photo cannot be downloaded, it falls back to the grid.

**Matchup IDs.** Decks are identified in the history file as `{draft_id}_seat{N}`. (They used to be identified by image path, which broke for decks without photos.)

### Manual or automatic: scrape trophies (`scrape_trophies.py`)

Scrapes the trophy archive (`?view=trophy-archive`) and adds new trophy winners to `trophy_decks.json` with `"image": null`. Existing entries are never overwritten. It records each drafter's `seat` index, which is needed to pull the right decklist. It runs automatically at the start of every Friday job; the `scrape-trophies` job exists for running it alone.

### Files

| File | What it is | Who edits it |
|---|---|---|
| `card_of_the_week.py` | Monday script | you |
| `p1p1_bot.py` | Wednesday script | you |
| `trophy_battle.py` | Friday script (also has the preview and clash-only modes) | you |
| `scrape_trophies.py` | Trophy scraper | you |
| `.github/workflows/card_of_the_week.yml` | All schedules and manual jobs | you |
| `history.json` | Oracle IDs already used for Scroll of the Week | bot |
| `trophy_battle_history.json` | Deck pairs already used | bot |
| `trophy_decks.json` | Trophy deck registry (drafter, event, date, draft ID, seat, optional photo path) | scraper, and you for photos |
| `current_week.json` | This week's Clash matchup, kept so Clash can recover from a restart | bot |
| `deck_images/` | Optional photos of paper decks | you |

### GitHub secrets (Settings → Secrets and variables → Actions)

| Secret | Value | Used by |
|---|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token | all three posts |
| `DISCORD_CHANNEL_ID` | Target channel ID | all three posts |
| `CUBECOBRA_SESSION` | Cube Cobra `connect.sid` cookie value | Scrolls for Sale |
| `CLASH_URL` | `https://cube-clash-production.up.railway.app` | Clash of the Wise |
| `CLASH_SECRET` | Shared secret, must equal Railway's `CLASH_SECRET` | Clash of the Wise |

### Cube Cobra IDs

- Cube vanity ID: `tm1`
- Internal cube / records ID: `60ba7b55a2494110485dc479`

---

## Repo 2: `cube-clash`

A shared two-player board with no rules enforcement. Hosted on Railway.

### Tech

- `server.js`: Express + Socket.io. Holds this week's decks and all live games **in memory**.
- `public/index.html`: loads React, ReactDOM, Babel and Socket.io from a CDN.
- `public/app.jsx`: the entire frontend.
- `railway.toml`: deployment config. The server listens on `process.env.PORT`.

### Flow

1. `trophy_battle.py` POSTs to `/api/set-decks` with the secret. The server stores the decks and clears all games.
2. The lobby (`/api/lobby`) shows the week label, both decks, and open games.
3. A player picks a seat. A game is created and they land in the **decklist editor** (pre-filled and editable). "Ready to Play" sends `update_deck` then `join_game`.
4. The second player joins from the lobby's "Waiting for opponent" list.
5. All moves go through the `game_action` socket event. The server updates state and sends `game_state` to both players.

### Self-healing after a restart

The server keeps decks in memory, so a Railway restart used to leave the lobby empty. Now:

- On startup, the server loads `current_week.json` from the bot repo's raw GitHub URL.
- If the lobby is opened while no decks are set, it tries again, at most once a minute.
- The URL can be overridden with the optional `CURRENT_WEEK_URL` variable in Railway.
- Until the first Friday run creates `current_week.json`, the server logs a 404 for it. That is expected.

### Games clean up after themselves

- When the last player leaves a game (including **Quit**), the game is removed from the lobby after 15 seconds. The short delay means a page refresh does not delete a game.
- A game nobody joined, or one emptied by a restart, is removed after 10 minutes.

### Features

- Drag cards anywhere on the battlefield, up to the right edge. The hover preview hides while you drag.
- Double-click to tap; right-click for the full menu (zones, flip, clone, counters, give to opponent).
- Hand ribbon; graveyard and exile viewers for both players; library search and view top N.
- Custom tokens, separate battlefield and hand size sliders, chat and action log.
- **Shared drawing:** one canvas covers both halves of the board. Strokes sync to your opponent and are mirrored top-to-bottom to match their view. Late joiners see existing drawings, and Clear wipes both screens.
- Restart sends both players back to the deck editor; Concede ends the game; Quit returns to the lobby.

### Railway environment variables

- `CLASH_SECRET` (required): must match the GitHub secret.
- `CURRENT_WEEK_URL` (optional): where to load `current_week.json` from.

---

## Manual tasks

### Refresh the Cube Cobra session cookie (when Scrolls for Sale stops posting)

1. Log in to cubecobra.com in your browser.
2. DevTools → **Application** (Chrome) or **Storage** (Firefox) → Cookies → `https://cubecobra.com`.
3. Copy the value of `connect.sid`.
4. GitHub → repo → Settings → Secrets and variables → Actions → edit `CUBECOBRA_SESSION` → paste.
5. Re-run the `p1p1` job to confirm.

### Add a paper-deck photo (optional)

A deck needs no photo to be used, because Friday builds a grid image. To use your own photo instead:

1. Name it `YYYY_Month_DrafterInitial.jpg` (for example `2026_April_BenH.jpg`) and add it to `deck_images/`.
2. Edit the deck's entry in `trophy_decks.json` and set `"image": "deck_images/2026_April_BenH.jpg"`.
3. Commit and push.

To see what the auto-generated image looks like for a deck, run `preview-deck-image`.

### Reset history (start a fresh cycle)

- **Scroll of the Week:** set `history.json` to `{"chosen": []}`.
- **Clash of the Wise:** set `trophy_battle_history.json` to `{"matchups": []}`.

The scripts rebuild the other fields on their next run. Deleting the files also works.

### Test Cube Clash without posting to Discord

Run `push-clash-only`, then open the Clash URL. To test the self-healing, restart the service in Railway (Deployments → ⋮ → Restart) and reload the lobby after about 30 seconds. The same decks should be back.

### Redeploy or relaunch Cube Clash on Railway

If the service just went offline (for example a trial ended), **redeploy the existing service**. It keeps its domain and variables, and nothing else needs recreating. Check Settings → Networking (the domain matches `CLASH_URL`) and Variables (`CLASH_SECRET` is set).

To start from scratch:

1. Railway → New Project → **Deploy from GitHub repo** → `mbrunlieb/cube-clash`.
2. Variables → add `CLASH_SECRET`, equal to the GitHub secret.
3. Settings → Networking → **Generate Domain**.
4. If the domain changed, update the `CLASH_URL` GitHub secret.
5. The lobby fills itself from `current_week.json`, or run `push-clash-only`.

### Quirk: one person, two Cube Cobra names

Cube Cobra keeps old usernames in old drafts (for example `TannerGold` and `ButterFriend` are the same person). The "different drafter" rule compares names exactly, so those can be paired. This is accepted, since the point is the decks and not who played them.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Scrolls for Sale posts nothing, or the log shows 401/403 or `success=false` | Cube Cobra session cookie expired | Refresh `CUBECOBRA_SESSION` |
| Scroll of the Week shows no win rate | Cube Cobra changed its records page again, or the card is in fewer than 2 decks | Run `probe-winrate` and read the log (see below) |
| Win-rate numbers look too low | Players not matched to seats | In the `probe-winrate` log, each record line should show every player matched and `owner check N/N` |
| `could not locate records in the records page` | `window.reactProps` or its `records` field moved | View the page source (Ctrl+U) and search for `Team cube draft` to find where the records are now |
| Clash of the Wise: `could not find cards array in deck page` | Deck page structure changed | Inspect the page source and adjust `fetch_decklist()` |
| Grid image is all gray tiles | Scryfall lookup failed | Check the log for `Scryfall ... error` lines; it prints the status and response |
| Cube Clash lobby says "No decks set for this week" | No `current_week.json` yet, or GitHub unreachable | Run `push-clash-only` or the `trophy-battle` job |
| `/api/set-decks` returns 401 | `CLASH_SECRET` differs between GitHub and Railway | Make them identical |
| A workflow's "Commit updated history" step fails | Branch protection or permissions | The job needs `permissions: contents: write`, and `main` must allow bot pushes |
| A scheduled run fails with a Python `SyntaxError` mentioning YAML text | The workflow file was pasted into a `.py` file | Restore the script from Git history and put the YAML in `.github/workflows/` |
| The preview or Clash job is missing from the Run workflow dropdown | The YAML on `main` is old or in the wrong folder | Confirm `.github/workflows/card_of_the_week.yml` has the job in its `options:` list |

### What the `probe-winrate` log tells you

- `Found N records.` means the records page parsed.
- One line per record: players matched, player and seat counts, and the owner check.
- `Resolving N card printings via Scryfall` is normal (cards not in the current cube list).
- `Computed winrate data for N cards from N decks.` The deck count should be close to the total on Cube Cobra's records page.

---

## Known gaps and ideas

- **The Cube Cobra session cookie** is the one thing that will keep breaking. Options: log in automatically with a stored username and password, or send a Discord alert the same day it fails.
- Monday refetches every draft page each week. If the record count grows a lot, cache the computed win rates in the repo.
- The data computed for win rates could drive extra posts (top and bottom performers, win rate by color, a drafter leaderboard).
- Slash commands (such as an on-demand deck image) need a bot process that stays online to receive them, and the weekly GitHub Actions scripts cannot do that. This would need a host (Railway is the obvious one).
