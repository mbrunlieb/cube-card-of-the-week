#!/usr/bin/env python3
"""
Clear all messages from the Discord channel the bots post to, WITHOUT deleting the
channel (so its ID, permissions and settings stay the same).

Safe by default: unless CONFIRM is exactly "DELETE" this only COUNTS what it would remove.
Pinned messages are always skipped.

Optional date limits (YYYY-MM-DD, midnight US Central time):
  DELETE_AFTER   delete only messages sent ON or AFTER this date (keeps everything older)
  DELETE_BEFORE  delete only messages sent BEFORE this date (keeps everything newer)
Use both to delete a window. With neither, every non-pinned message is deleted.

Only timestamps and counts are logged, never message text or authors (the Actions logs
of a public repo are public).

The bot needs "Manage Messages" and "Read Message History" in the channel.

Discord only allows bulk deletion of messages younger than 14 days; older messages
have to be deleted one at a time (rate limited, so this can take a few minutes).
"""

import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

DISCORD_API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
BULK_MAX_AGE_MS = 13 * 24 * 60 * 60 * 1000  # stay safely inside Discord's 14-day bulk limit


def snowflake_ms(snowflake: str) -> int:
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


LOCAL_TZ = ZoneInfo("America/Chicago")


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")


def parse_date_ms(value: str, name: str) -> int | None:
    """Parse YYYY-MM-DD as midnight US Central time -> epoch milliseconds."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(f"ERROR: {name} must look like 2026-05-20 (YYYY-MM-DD), got '{value}'.")
        sys.exit(1)
    return int(d.replace(tzinfo=LOCAL_TZ).timestamp() * 1000)


class Discord:
    def __init__(self, token: str, channel_id: str):
        self.channel_id = channel_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (cube-card-of-the-week, 1.0)",
        })

    def request(self, method: str, path: str, **kwargs):
        """Make a request, waiting out rate limits. Returns the final response."""
        url = f"{DISCORD_API}{path}"
        for _ in range(8):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code != 429:
                return resp
            try:
                wait = float(resp.json().get("retry_after", 1.0))
            except Exception:
                wait = 1.0
            print(f"  Rate limited, waiting {wait:.1f}s…")
            time.sleep(wait + 0.2)
        return resp

    def fetch_all_messages(self) -> list[dict]:
        """Return every message in the channel (newest first)."""
        messages: list[dict] = []
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            resp = self.request("GET", f"/channels/{self.channel_id}/messages", params=params)
            if not resp.ok:
                print(f"ERROR fetching messages: {resp.status_code} {resp.text[:300]}")
                if resp.status_code in (401, 403):
                    print("Check that the bot token is valid and the bot has View Channel, "
                          "Read Message History and Manage Messages in this channel.")
                sys.exit(1)
            batch = resp.json()
            if not batch:
                break
            messages.extend(batch)
            before = batch[-1]["id"]
            if len(batch) < 100:
                break
        return messages

    def bulk_delete(self, ids: list[str]) -> int:
        resp = self.request("POST", f"/channels/{self.channel_id}/messages/bulk-delete", json={"messages": ids})
        if resp.ok:
            return len(ids)
        print(f"  Bulk delete failed ({resp.status_code}): {resp.text[:200]}; falling back to one-by-one")
        return sum(self.delete_one(i) for i in ids)

    def delete_one(self, message_id: str) -> int:
        resp = self.request("DELETE", f"/channels/{self.channel_id}/messages/{message_id}")
        time.sleep(0.35)  # stay under the per-channel delete rate limit
        if resp.status_code in (200, 204, 404):  # 404 = already gone
            return 1
        print(f"  Could not delete {message_id}: {resp.status_code} {resp.text[:160]}")
        return 0


def main():
    token = os.environ["DISCORD_BOT_TOKEN"]
    channel_id = os.environ["DISCORD_CHANNEL_ID"]
    confirmed = os.environ.get("CONFIRM", "").strip() == "DELETE"
    after_ms = parse_date_ms(os.environ.get("DELETE_AFTER", ""), "delete_after")
    before_ms = parse_date_ms(os.environ.get("DELETE_BEFORE", ""), "delete_before")
    if after_ms is not None and before_ms is not None and after_ms >= before_ms:
        print("ERROR: delete_after must be an earlier date than delete_before.")
        sys.exit(1)

    discord = Discord(token, channel_id)

    # Show exactly which channel this will act on, so it can be checked BEFORE confirming.
    info = discord.request("GET", f"/channels/{channel_id}")
    if not info.ok:
        print(f"ERROR: could not look up channel {channel_id}: {info.status_code} {info.text[:200]}")
        sys.exit(1)
    ch = info.json()
    print("=" * 60)
    print(f"TARGET CHANNEL: #{ch.get('name', '(unnamed)')}   (channel ID {channel_id}, server ID {ch.get('guild_id', 'n/a')})")
    print("Only this one channel is read or modified. Nothing else in the server is touched.")
    print("=" * 60)

    expected = os.environ.get("EXPECTED_CHANNEL", "").strip().lstrip("#")
    if expected and expected != ch.get("name"):
        print(f"ABORTING: expected channel #{expected} but DISCORD_CHANNEL_ID is #{ch.get('name')}.")
        sys.exit(1)

    print("Reading the channel…")
    messages = discord.fetch_all_messages()
    pinned = [m for m in messages if m.get("pinned")]
    unpinned = [m for m in messages if not m.get("pinned")]

    def in_range(m: dict) -> bool:
        t = snowflake_ms(m["id"])
        return (after_ms is None or t >= after_ms) and (before_ms is None or t < before_ms)

    targets = [m for m in unpinned if in_range(m)]
    kept_by_date = [m for m in unpinned if not in_range(m)]

    # Messages per month (US Central), so a cutoff date is easy to choose. Counts only, no content.
    by_month = Counter(fmt_ms(snowflake_ms(m["id"]))[:7] for m in unpinned)
    print("\nMessages in the channel by month (excluding pinned):")
    for month in sorted(by_month):
        print(f"  {month}: {by_month[month]}")

    print("\nDate limits: " + (
        f"delete messages sent on or after {fmt_ms(after_ms)}" if after_ms is not None else "no start date")
        + "; " + (f"before {fmt_ms(before_ms)}" if before_ms is not None else "no end date"))

    def span(msgs):
        if not msgs:
            return "none"
        times = [snowflake_ms(m["id"]) for m in msgs]
        return f"{fmt_ms(min(times))}  to  {fmt_ms(max(times))}"

    print(f"  To delete ({len(targets)}): {span(targets)}")
    print(f"  Kept because of the date limits ({len(kept_by_date)}): {span(kept_by_date)}")
    print(f"  Kept because pinned ({len(pinned)})")

    now_ms = int(time.time() * 1000)
    recent = [m["id"] for m in targets if now_ms - snowflake_ms(m["id"]) < BULK_MAX_AGE_MS]
    old = [m["id"] for m in targets if now_ms - snowflake_ms(m["id"]) >= BULK_MAX_AGE_MS]

    print(f"\nFound {len(messages)} messages: {len(targets)} to delete "
          f"({len(recent)} recent, {len(old)} older than ~2 weeks), "
          f"{len(kept_by_date)} kept by date, {len(pinned)} pinned (kept).")

    if not confirmed:
        print(f"\nDRY RUN: nothing was deleted. This would act on #{ch.get('name')}.")
        print("If that is NOT the channel you meant, stop and fix DISCORD_CHANNEL_ID first.")
        print("To delete these messages, run again with the confirm box set to DELETE.")
        return
    if not targets:
        print("Nothing to delete.")
        return

    deleted = 0

    # Recent messages: bulk delete in chunks of up to 100 (Discord needs at least 2 per call).
    for i in range(0, len(recent), 100):
        chunk = recent[i:i + 100]
        deleted += discord.bulk_delete(chunk) if len(chunk) >= 2 else sum(discord.delete_one(c) for c in chunk)
        print(f"  Deleted {deleted}/{len(targets)}…")

    # Older messages: one at a time.
    for n, message_id in enumerate(old, 1):
        deleted += discord.delete_one(message_id)
        if n % 25 == 0 or n == len(old):
            print(f"  Deleted {deleted}/{len(targets)}…")

    print(f"\nDone. Deleted {deleted} of {len(targets)} messages; "
          f"{len(kept_by_date)} kept by date, {len(pinned)} pinned kept.")
    if deleted < len(targets):
        print(f"{len(targets) - deleted} messages could not be deleted (see the errors above).")


if __name__ == "__main__":
    main()
