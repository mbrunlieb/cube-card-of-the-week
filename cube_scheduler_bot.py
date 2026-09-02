"""
cube_scheduler_bot.py — Cube Night Scheduler

Posts a Discord poll on the 1st of each month for the following month's
weekend dates, assigns a role to voters on the winning date, sends a
1-week ping, and manages a live confirmation counter with buttons.

Environment variables required:
  DISCORD_BOT_TOKEN             — bot token
  DISCORD_SCHEDULING_CHANNEL_ID — #cube-scheduling channel ID
  DISCORD_GUILD_ID              — server (guild) ID
"""

import os
import calendar
import re
from datetime import datetime, timedelta, timezone, time as time_t

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN             = os.environ["DISCORD_BOT_TOKEN"]
SCHEDULING_CHANNEL_ID = int(os.environ["DISCORD_SCHEDULING_CHANNEL_ID"])
GUILD_ID              = int(os.environ["DISCORD_GUILD_ID"])

CUBE_ROLE_NAME    = "🎲 Next Cube"
POLL_DURATION_DAYS = 7
# Runs daily at 6 PM UTC (1 PM CDT) — matches your existing bot schedule
DAILY_RUN_TIME    = time_t(hour=18, minute=0, tzinfo=timezone.utc)

# ── Intents ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True         # required for role assignment + voter lookup
intents.message_content = True # required for reading channel history on recovery

# ── Confirmation View (persistent across restarts) ────────────────────────────

class ConfirmationView(discord.ui.View):
    """
    Confirm / Drop / Join buttons on the cube night announcement.
    timeout=None + stable custom_ids = survives bot restarts.
    """

    def __init__(self, bot_ref=None):
        super().__init__(timeout=None)
        self.bot_ref = bot_ref  # set on the registered instance in setup_hook

    def _bot(self, interaction: discord.Interaction) -> "CubeBot":
        # Works whether called from the registered view or the send-time view
        return self.bot_ref or interaction.client  # type: ignore[return-value]

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success,
                       custom_id="cube_confirm")
    async def confirm(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        bot = self._bot(interaction)
        member = interaction.user
        role = await bot.get_cube_role(interaction.guild)

        if role and role not in member.roles:
            await member.add_roles(role)

        bot.state["confirmed_ids"].add(member.id)
        bot.state["dropped_ids"].discard(member.id)

        await bot.update_confirmation_embed(interaction.channel)
        await interaction.response.send_message(
            "✅ You're confirmed for the next Cube! See you there 🎲", ephemeral=True
        )

    @discord.ui.button(label="❌ Drop", style=discord.ButtonStyle.danger,
                       custom_id="cube_drop")
    async def drop(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        bot = self._bot(interaction)
        member = interaction.user
        role = await bot.get_cube_role(interaction.guild)

        if role and role in member.roles:
            await member.remove_roles(role)

        bot.state["confirmed_ids"].discard(member.id)
        bot.state["dropped_ids"].add(member.id)

        await bot.update_confirmation_embed(interaction.channel)
        await interaction.response.send_message(
            "You've dropped from the next Cube. Hope to see you next time! 👋",
            ephemeral=True,
        )

    @discord.ui.button(label="➕ Join", style=discord.ButtonStyle.primary,
                       custom_id="cube_join")
    async def join(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        bot = self._bot(interaction)
        member = interaction.user
        role = await bot.get_cube_role(interaction.guild)

        if role and role in member.roles:
            await interaction.response.send_message(
                "You're already signed up for the next Cube!", ephemeral=True
            )
            return

        if role:
            await member.add_roles(role)

        bot.state["confirmed_ids"].add(member.id)
        bot.state["dropped_ids"].discard(member.id)

        await bot.update_confirmation_embed(interaction.channel)
        await interaction.response.send_message(
            "✅ You've joined the next Cube! See you there 🎲", ephemeral=True
        )


# ── Bot ───────────────────────────────────────────────────────────────────────

class CubeBot(commands.Bot):

    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        # In-memory state; persisted to/recovered from the channel topic
        self.state: dict = {
            "poll_message_id":       None,   # int | None
            "poll_month":            None,   # "YYYY-MM" string
            "cube_date":             None,   # datetime | None  (the winning date)
            "cube_date_label":       None,   # "Saturday, October 18" display string
            "confirmation_message_id": None, # int | None
            "confirmed_ids":         set(),  # set[int] of member IDs
            "dropped_ids":           set(),  # set[int]
            "week_ping_sent":        False,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def setup_hook(self):
        # Register the persistent view BEFORE syncing commands
        self.add_view(ConfirmationView(bot_ref=self))
        self.daily_task.start()

    async def on_ready(self):
        print(f"✅  Logged in as {self.user} ({self.user.id})")
        # Sync guild-specific slash commands (instant, no propagation delay)
        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)
        await self.recover_state()

    # ── State persistence (channel topic as key-value store) ─────────────────

    async def save_state(self):
        """Write current state into the channel topic for crash-safe persistence."""
        channel = self.get_channel(SCHEDULING_CHANNEL_ID)
        if not channel:
            return
        date_str  = (self.state["cube_date"].strftime("%Y-%m-%d")
                     if self.state["cube_date"] else "none")
        topic = (
            f"poll:{self.state['poll_message_id'] or 'none'} "
            f"month:{self.state['poll_month'] or 'none'} "
            f"date:{date_str} "
            f"label:{self.state['cube_date_label'] or 'none'} "
            f"conf:{self.state['confirmation_message_id'] or 'none'} "
            f"ping:{self.state['week_ping_sent']}"
        )
        try:
            await channel.edit(topic=topic)
        except discord.Forbidden:
            print("⚠️  Missing MANAGE_CHANNELS — can't persist state to topic.")

    async def recover_state(self):
        """On startup, rebuild in-memory state from the channel topic + role."""
        channel = self.get_channel(SCHEDULING_CHANNEL_ID)
        if not channel or not channel.topic:
            print("ℹ️  No saved state found — starting fresh.")
            return
        try:
            parts: dict[str, str] = {}
            for segment in channel.topic.split():
                k, _, v = segment.partition(":")
                parts[k] = v

            def _int_or_none(key: str):
                v = parts.get(key, "none")
                return int(v) if v != "none" else None

            self.state["poll_message_id"]       = _int_or_none("poll")
            self.state["poll_month"]             = parts.get("month") if parts.get("month") != "none" else None
            self.state["confirmation_message_id"] = _int_or_none("conf")
            self.state["week_ping_sent"]          = parts.get("ping") == "True"

            date_raw = parts.get("date", "none")
            if date_raw != "none":
                self.state["cube_date"] = datetime.strptime(
                    date_raw, "%Y-%m-%d"
                ).replace(tzinfo=timezone.utc)

            label_raw = parts.get("label", "none")
            self.state["cube_date_label"] = label_raw if label_raw != "none" else None

            # Confirmed IDs = current role holders
            guild = self.get_guild(GUILD_ID)
            if guild:
                role = discord.utils.get(guild.roles, name=CUBE_ROLE_NAME)
                if role:
                    self.state["confirmed_ids"] = {m.id for m in role.members}

            print(f"📋  Recovered state: {self.state}")
        except Exception as exc:
            print(f"⚠️  State recovery failed ({exc}) — starting fresh.")

    # ── Role helpers ──────────────────────────────────────────────────────────

    async def get_cube_role(self, guild: discord.Guild) -> discord.Role:
        role = discord.utils.get(guild.roles, name=CUBE_ROLE_NAME)
        if not role:
            role = await guild.create_role(
                name=CUBE_ROLE_NAME,
                color=discord.Color.gold(),
                mentionable=True,
                reason="Auto-created by Cube Scheduler Bot",
            )
            print(f"✅  Created role: {CUBE_ROLE_NAME}")
        return role

    async def clear_cube_role(self, guild: discord.Guild):
        """Strip the role from every current holder."""
        role = discord.utils.get(guild.roles, name=CUBE_ROLE_NAME)
        if role:
            for member in list(role.members):
                try:
                    await member.remove_roles(role)
                except discord.Forbidden:
                    pass

    # ── Date helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def get_next_month_weekends() -> list[datetime]:
        """Return all Saturdays and Sundays for the month following today."""
        now = datetime.now(timezone.utc)
        year, month = now.year, now.month + 1
        if month > 12:
            month, year = 1, year + 1
        _, num_days = calendar.monthrange(year, month)
        return [
            datetime(year, month, day, 18, 0, tzinfo=timezone.utc)
            for day in range(1, num_days + 1)
            if datetime(year, month, day).weekday() in (5, 6)  # Sat=5 Sun=6
        ]

    @staticmethod
    def get_current_month_weekends(min_days_ahead: int = 0) -> list[datetime]:
        """Remaining Sat/Sun of the CURRENT month, at least min_days_ahead out
        (so no option can fall before the poll closes)."""
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=min_days_ahead)
        _, num_days = calendar.monthrange(now.year, now.month)
        return [
            datetime(now.year, now.month, day, 18, 0, tzinfo=timezone.utc)
            for day in range(1, num_days + 1)
            if datetime(now.year, now.month, day).weekday() in (5, 6)
            and datetime(now.year, now.month, day, 18, 0, tzinfo=timezone.utc) > cutoff
        ]

    @staticmethod
    def date_label(dt: datetime) -> str:
        """'Saturday, October 4' — Linux-safe, strips leading zero on day."""
        return dt.strftime("%-d %B %Y")  # used internally; display label separate

    # ── Poll posting ──────────────────────────────────────────────────────────

    async def post_monthly_poll(self, current_month: bool = False,
                                duration_days: int | None = None) -> bool:
        channel = self.get_channel(SCHEDULING_CHANNEL_ID)
        if not channel:
            print("❌  Scheduling channel not found.")
            return False

        duration = duration_days or POLL_DURATION_DAYS
        if current_month:
            # Only offer dates that land AFTER the poll closes (+1 day buffer)
            dates = self.get_current_month_weekends(min_days_ahead=duration + 1)
        else:
            dates = self.get_next_month_weekends()
        if not dates:
            print("⚠️  No eligible weekend dates found.")
            return False

        month_label = dates[0].strftime("%B %Y")   # "October 2025"
        poll_month  = dates[0].strftime("%Y-%m")   # "2025-10"

        poll = discord.Poll(
            f"🎲 When can you cube in {month_label}?",
            duration=timedelta(days=duration),
            multiple=True,
        )
        for d in dates:
            emoji = "🟩" if d.weekday() == 5 else "🟦"   # green=Sat, blue=Sun
            poll.add_answer(text=d.strftime("%A, %B %-d"), emoji=emoji)

        msg = await channel.send(
            content=(
                f"# 🗓️  {month_label} Cube\n"
                f"Vote for the dates that work for you — poll closes in "
                f"{duration} day{'s' if duration != 1 else ''}. "
                f"Everyone who selects the winning cube date gets the **{CUBE_ROLE_NAME}** role "
                f"and a confirmation ping a week before the cube."
            ),
            poll=poll,
        )
        self.state["poll_message_id"] = msg.id
        self.state["poll_month"]      = poll_month
        await self.save_state()
        print(f"✅  Posted poll for {month_label} (msg {msg.id})")
        return True

    # ── Poll result handling ──────────────────────────────────────────────────

    async def handle_poll_result(self, poll_message: discord.Message):
        channel = self.get_channel(SCHEDULING_CHANNEL_ID)
        guild   = self.get_guild(GUILD_ID)
        poll    = poll_message.poll

        # Find winning answer
        if not poll.answers:
            return
        best = max(poll.answers, key=lambda a: a.vote_count)
        if best.vote_count == 0:
            await channel.send(
                "⚠️  No votes were cast — no cube scheduled this month."
            )
            self.state["poll_message_id"] = None
            await self.save_state()
            return

        winning_label = best.text  # e.g. "Saturday, October 18"

        # Reconstruct the actual datetime from the winning label + poll_month
        cube_date: datetime | None = None
        if self.state["poll_month"]:
            try:
                year, month = map(int, self.state["poll_month"].split("-"))
                # Parse day from e.g. "Saturday, October 18" → "October 18 2025"
                day_part = winning_label.split(", ", 1)[1]      # "October 18"
                cube_date = datetime.strptime(
                    f"{day_part} {year}", "%B %d %Y"
                ).replace(hour=18, tzinfo=timezone.utc)
            except Exception as exc:
                print(f"⚠️  Could not parse date from '{winning_label}': {exc}")

        self.state["cube_date"]       = cube_date
        self.state["cube_date_label"] = winning_label
        self.state["poll_message_id"] = None

        # Gather voters
        members_to_assign: list[discord.Member] = []
        async for user in best.voters():
            member = guild.get_member(user.id)
            if not member:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.NotFound:
                    continue
            if member:
                members_to_assign.append(member)

        # Reset + reassign role
        await self.clear_cube_role(guild)
        role = await self.get_cube_role(guild)
        for member in members_to_assign:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass

        self.state["confirmed_ids"] = {m.id for m in members_to_assign}
        self.state["dropped_ids"]   = set()

        # Post confirmation message
        embed = self._build_embed(winning_label, len(members_to_assign), role)
        view  = ConfirmationView(bot_ref=self)
        msg   = await channel.send(
            content=(
                f"🏆  **The votes are in!** {role.mention} — your cube date is set.\n"
                f"Confirm your spot, drop if plans change, or join if you missed the poll!"
            ),
            embed=embed,
            view=view,
        )
        self.state["confirmation_message_id"] = msg.id
        await self.save_state()
        print(f"✅  Cube night: {winning_label} | {len(members_to_assign)} players")

    # ── Embed builder ─────────────────────────────────────────────────────────

    def _build_embed(self, date_label: str, count: int,
                     role: discord.Role | None = None) -> discord.Embed:
        embed = discord.Embed(
            title=f"📅  Next Cube — {date_label}",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="✅  Confirmed Players",
            value=f"**{count}** player{'s' if count != 1 else ''}",
            inline=True,
        )
        if role:
            embed.add_field(name="Role", value=role.mention, inline=True)
        embed.add_field(
            name="How to update your status",
            value=(
                "**✅ Confirm** — lock in your spot\n"
                "**❌ Drop** — let us know you can't make it\n"
                "**➕ Join** — weren't in the poll? Jump in!"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Status and role updated automatically • Counter refreshes on each click"
        )
        return embed

    async def update_confirmation_embed(self, channel: discord.TextChannel):
        """Edit the live confirmation message to reflect the current confirmed count."""
        msg_id = self.state.get("confirmation_message_id")
        if not msg_id:
            return
        try:
            msg   = await channel.fetch_message(msg_id)
            guild = channel.guild
            role  = discord.utils.get(guild.roles, name=CUBE_ROLE_NAME)
            count = len(self.state["confirmed_ids"])
            label = self.state.get("cube_date_label") or ""
            new_embed = self._build_embed(label, count, role)
            await msg.edit(embed=new_embed)
        except Exception as exc:
            print(f"⚠️  Could not update embed: {exc}")

    # ── 1-week ping ───────────────────────────────────────────────────────────

    async def send_week_ping(self):
        channel = self.get_channel(SCHEDULING_CHANNEL_ID)
        guild   = self.get_guild(GUILD_ID)
        role    = discord.utils.get(guild.roles, name=CUBE_ROLE_NAME)
        if not role:
            return
        label = self.state.get("cube_date_label") or "the next Cube"
        count = len(self.state["confirmed_ids"])
        days_until = 7
        if self.state.get("cube_date"):
            days_until = max(0, (self.state["cube_date"] - datetime.now(timezone.utc)).days)
        if days_until >= 6:
            when = "is one week away"
        elif days_until <= 1:
            when = "is almost here"
        else:
            when = f"is only {days_until} days away"
        await channel.send(
            f"{role.mention} — **{label} {when}!** 🎲\n\n"
            f"Can you still make it? Use the buttons above to confirm or drop. "
            f"Current count: **{count} confirmed**."
        )
        self.state["week_ping_sent"] = True
        await self.save_state()
        print("✅  Sent 1-week ping")

    # ── Daily scheduled task ──────────────────────────────────────────────────

    @tasks.loop(time=DAILY_RUN_TIME)
    async def daily_task(self):
        now = datetime.now(timezone.utc)
        print(f"🕐  Daily task: {now.strftime('%Y-%m-%d %H:%M UTC')}")

        # ── 1st of the month: post the scheduling poll ──
        if now.day == 1:
            no_active_poll    = not self.state["poll_message_id"]
            no_active_conf    = not self.state["confirmation_message_id"]
            # Only post if we haven't scheduled this coming month yet
            next_month        = (now.month % 12) + 1
            date_already_set  = (
                self.state["cube_date"] is not None
                and self.state["cube_date"].month == next_month
            )
            if no_active_poll and no_active_conf and not date_already_set:
                await self.post_monthly_poll()

        # ── Check if an active poll has expired ──
        if self.state["poll_message_id"]:
            channel = self.get_channel(SCHEDULING_CHANNEL_ID)
            try:
                msg = await channel.fetch_message(self.state["poll_message_id"])
                if msg.poll and msg.poll.is_finalised():
                    await self.handle_poll_result(msg)
            except discord.NotFound:
                print("⚠️  Poll message not found — clearing.")
                self.state["poll_message_id"] = None
                await self.save_state()

        # ── 1-week ping ──
        if (
            self.state["cube_date"]
            and not self.state["week_ping_sent"]
            and self.state["confirmation_message_id"]
        ):
            days_until = (self.state["cube_date"] - now).days
            if days_until <= 7:
                await self.send_week_ping()

        # ── Clean up the day after cube night ──
        if self.state["cube_date"]:
            if now > self.state["cube_date"] + timedelta(days=1):
                guild = self.get_guild(GUILD_ID)
                if guild:
                    await self.clear_cube_role(guild)
                self.state.update({
                    "poll_message_id":        None,
                    "poll_month":             None,
                    "cube_date":              None,
                    "cube_date_label":        None,
                    "confirmation_message_id": None,
                    "confirmed_ids":          set(),
                    "dropped_ids":            set(),
                    "week_ping_sent":         False,
                })
                await self.save_state()
                print("🧹  Cleaned up after cube day.")
                # Immediately start the next cycle — the 1st-of-month post is
                # usually blocked by the active confirmation, so without this
                # the following month would be skipped entirely.
                await self.post_monthly_poll()

    @daily_task.before_loop
    async def before_daily(self):
        await self.wait_until_ready()


# ── Admin Slash Commands ──────────────────────────────────────────────────────
# Guild-only commands sync instantly; no hour-long global propagation wait.

bot = CubeBot()
guild_obj = discord.Object(id=GUILD_ID)


@bot.tree.command(name="cube-poll", description="Force-post the scheduling poll for next month",
                  guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_poll(interaction: discord.Interaction):
    if bot.state["poll_message_id"] or bot.state["confirmation_message_id"]:
        await interaction.response.send_message(
            "❌  A scheduling cycle is already active. Run `/cube-reset` first "
            "if you want to start over.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    ok = await bot.post_monthly_poll()
    await interaction.followup.send(
        "✅  Scheduling poll posted!" if ok
        else "⚠️  Couldn't post the poll — check the logs.", ephemeral=True
    )


@bot.tree.command(name="cube-poll-now",
                  description="Post a poll for the CURRENT month's remaining weekends",
                  guild=guild_obj)
@app_commands.describe(duration_days="How many days the poll stays open (default 3)")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_poll_now(interaction: discord.Interaction, duration_days: int = 3):
    if bot.state["poll_message_id"] or bot.state["confirmation_message_id"]:
        await interaction.response.send_message(
            "❌  A scheduling cycle is already active. Run `/cube-reset` first "
            "if you want to start over.", ephemeral=True
        )
        return
    if not 1 <= duration_days <= 7:
        await interaction.response.send_message(
            "❌  duration_days must be between 1 and 7.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    ok = await bot.post_monthly_poll(current_month=True, duration_days=duration_days)
    await interaction.followup.send(
        "✅  Current-month poll posted!" if ok
        else ("⚠️  No eligible weekend dates left this month "
              "(options must fall after the poll closes). "
              "Try a shorter duration_days."), ephemeral=True
    )


@bot.tree.command(name="cube-ping", description="Manually send the 1-week confirmation ping",
                  guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_ping(interaction: discord.Interaction):
    if not bot.state.get("cube_date"):
        await interaction.response.send_message(
            "❌  No cube is currently scheduled.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    await bot.send_week_ping()
    await interaction.followup.send("✅  Ping sent!", ephemeral=True)


@bot.tree.command(name="cube-status", description="Show the current scheduling state (admin)",
                  guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_status(interaction: discord.Interaction):
    s = bot.state
    embed = discord.Embed(title="📋  Cube Scheduler State", color=discord.Color.blurple())
    embed.add_field(name="Active Poll ID",   value=s["poll_message_id"] or "—",      inline=False)
    embed.add_field(name="Poll Month",       value=s["poll_month"] or "—",            inline=True)
    embed.add_field(name="Cube Date",        value=s["cube_date_label"] or "—",       inline=True)
    embed.add_field(name="Confirmation ID",  value=s["confirmation_message_id"] or "—", inline=False)
    embed.add_field(name="Confirmed",        value=len(s["confirmed_ids"]),            inline=True)
    embed.add_field(name="Dropped",          value=len(s["dropped_ids"]),              inline=True)
    embed.add_field(name="Week Ping Sent",   value=str(s["week_ping_sent"]),           inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="cube-reset", description="Reset the current scheduling cycle (admin)",
                  guild=guild_obj)
@app_commands.checks.has_permissions(administrator=True)
async def cmd_reset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await bot.clear_cube_role(guild)
    bot.state.update({
        "poll_message_id":        None,
        "poll_month":             None,
        "cube_date":              None,
        "cube_date_label":        None,
        "confirmation_message_id": None,
        "confirmed_ids":          set(),
        "dropped_ids":            set(),
        "week_ping_sent":         False,
    })
    await bot.save_state()
    await interaction.followup.send("✅  Scheduling cycle reset.", ephemeral=True)


# ── Entry Point ───────────────────────────────────────────────────────────────

bot.run(BOT_TOKEN)
