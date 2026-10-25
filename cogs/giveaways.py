import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import format_duration, is_staff, make_embed, parse_duration, respond, staff_only

log = logging.getLogger("bot.giveaways")

STATUS_LABELS = {"active": "active", "paused": "paused", "ended": "ended", "cancelled": "cancelled"}
STATUS_EMOJIS = {"active": "🎉", "paused": "⏸️", "ended": "🏁", "cancelled": "🚫"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
USERS_ONLY = discord.AllowedMentions(users=True, roles=False, everyone=False)


def ts(seconds: int, style: str = "R") -> str:
    return discord.utils.format_dt(datetime.fromtimestamp(seconds, tz=timezone.utc), style)


def mentions(user_ids: list[int]) -> str:
    return ", ".join(f"<@{uid}>" for uid in user_ids)


def build_embed(row, count: int) -> discord.Embed:
    """Construit l'embed d'un giveaway à partir de sa ligne en base (ou d'un dict équivalent)."""
    status = row["status"]
    color = {
        "active": config.EMBED_COLOR,
        "paused": config.WARNING_COLOR,
        "ended": config.GIVEAWAY_ENDED_COLOR,
        "cancelled": config.ERROR_COLOR,
    }[status]

    lines = []
    if row["description"]:
        lines += [row["description"], ""]
    if status == "active":
        lines.append(f"🕒 **Ends:** {ts(row['ends_at'])} ({ts(row['ends_at'], 'f')})")
    elif status == "paused":
        lines.append(f"⏸️ **Paused** — {format_duration(row['remaining'] or 0)} will remain when resumed")
    elif status == "ended":
        lines.append(f"🏁 **Ended** on {ts(row['ends_at'], 'f')}")
    else:
        lines.append("🚫 **Giveaway cancelled**")
    lines.append(f"👑 **Hosted by:** <@{row['host_id']}>")
    lines.append(f"🏆 **Winners:** {row['winner_count']}")
    if row["required_role_id"]:
        lines.append(f"🔒 **Requirement:** must have the role <@&{row['required_role_id']}>")
    lines.append(f"👥 **Entries:** {count}")

    if status == "ended":
        winners = json.loads(row["winners"])
        lines.append("")
        lines.append(f"🎊 **Winner(s):** {mentions(winners)}" if winners
                     else "😕 **No valid entries**")
    elif status == "active":
        lines += ["", "Click the button below to enter!"]

    embed = discord.Embed(title=f"{STATUS_EMOJIS[status]} {row['prize']}",
                          description="\n".join(lines), color=color)
    if row["image_name"]:
        embed.set_image(url=f"attachment://{row['image_name']}")
    embed.set_footer(text=f"Giveaway #{row['id']}")
    return embed


# --- Vues ---------------------------------------------------------------------
class GiveawayView(discord.ui.View):
    """Bouton persistant : une seule instance enregistrée sert tous les giveaways."""

    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.join.disabled = disabled

    @discord.ui.button(label="Enter", emoji="🎉", style=discord.ButtonStyle.primary,
                       custom_id="giveaway:join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Giveaways" = interaction.client.get_cog("Giveaways")
        await cog.handle_join(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaways", giveaway_id: int) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Leave giveaway", emoji="🚪", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        db = self.cog.bot.db
        row = await db.fetchone("SELECT status FROM giveaways WHERE id = ?", (self.giveaway_id,))
        if row is None or row["status"] != "active":
            await interaction.response.edit_message(content="⏹️ This giveaway is no longer active.", view=None)
            return
        await db.execute("DELETE FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?",
                         (self.giveaway_id, interaction.user.id))
        self.cog.mark_dirty(self.giveaway_id)
        await interaction.response.edit_message(content="✅ You left the giveaway.", view=None)


# --- Cog ----------------------------------------------------------------------
class Giveaways(commands.Cog):
    giveaway = app_commands.Group(name="giveaway", description="Giveaway management", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()   # sérialise les changements d'état (fin, pause, annulation…)
        self._dirty: set[int] = set()  # giveaways dont l'embed doit être rafraîchi

    async def cog_load(self) -> None:
        self.bot.add_view(GiveawayView())
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    def mark_dirty(self, giveaway_id: int) -> None:
        self._dirty.add(giveaway_id)

    # --- boucle de fond : fin automatique + rafraîchissement du compteur ---
    @tasks.loop(seconds=5)
    async def tick(self) -> None:
        due = await self.bot.db.fetchall(
            "SELECT id FROM giveaways WHERE status = 'active' AND ends_at <= ?", (int(time.time()),))
        for row in due:
            try:
                await self.finish(row["id"])
            except Exception:
                log.exception("Failed to end giveaway %s", row["id"])
        dirty, self._dirty = self._dirty, set()
        for giveaway_id in dirty:
            try:
                await self.refresh(giveaway_id)
            except Exception:
                log.exception("Failed to refresh giveaway %s", giveaway_id)

    @tick.before_loop
    async def before_tick(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.bot.db.execute(
            "UPDATE giveaways SET status = 'cancelled' "
            "WHERE message_id = ? AND status IN ('active', 'paused')", (payload.message_id,))

    # --- briques internes ---
    async def count_entries(self, giveaway_id: int) -> int:
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM giveaway_entries WHERE giveaway_id = ?", (giveaway_id,))
        return row["c"]

    async def refresh(self, giveaway_id: int) -> None:
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        if row is None or not row["message_id"]:
            return
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel_or_thread(row["channel_id"]) if guild else None
        if channel is None:
            return
        embed = build_embed(row, await self.count_entries(giveaway_id))
        view = GiveawayView(disabled=row["status"] != "active")
        try:
            await channel.get_partial_message(row["message_id"]).edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden):
            log.warning("Unable to edit the message of giveaway %s", giveaway_id)
        except discord.HTTPException:
            log.exception("HTTP error while editing giveaway %s", giveaway_id)

    async def draw(self, row, count: int) -> list[int]:
        """Tire `count` gagnants parmi les participants encore éligibles (jamais déjà tirés)."""
        guild = self.bot.get_guild(row["guild_id"])
        if guild is None:
            return []
        drawn = set(json.loads(row["drawn"]))
        entries = await self.bot.db.fetchall(
            "SELECT user_id FROM giveaway_entries WHERE giveaway_id = ?", (row["id"],))
        pool = []
        for entry in entries:
            uid = entry["user_id"]
            member = guild.get_member(uid)
            if uid in drawn or member is None or member.bot:
                continue
            if row["required_role_id"] and not any(r.id == row["required_role_id"] for r in member.roles):
                continue
            pool.append(uid)
        return random.sample(pool, min(count, len(pool)))

    async def announce(self, row, winners: list[int], *, reroll: bool = False) -> None:
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel_or_thread(row["channel_id"]) if guild else None
        if channel is None:
            return
        prize = row["prize"]
        if winners:
            head = "🔁 New draw" if reroll else "🎉 Congratulations"
            text = (f"{head}: {mentions(winners)}!\n"
                    f"Prize: **{prize}** — contact <@{row['host_id']}> to claim it.")
        else:
            text = f"😕 No valid entries for **{prize}**."
        reference = discord.MessageReference(
            message_id=row["message_id"], channel_id=row["channel_id"], fail_if_not_exists=False)
        try:
            await channel.send(text, reference=reference, allowed_mentions=USERS_ONLY)
        except discord.HTTPException:
            log.warning("Unable to announce the winners of giveaway %s", row["id"])

    async def set_state(self, giveaway_id: int, allowed: tuple[str, ...], **values) -> bool:
        """Change l'état d'un giveaway si son statut actuel est dans `allowed`."""
        async with self._lock:
            row = await self.bot.db.fetchone("SELECT status FROM giveaways WHERE id = ?", (giveaway_id,))
            if row is None or row["status"] not in allowed:
                return False
            assignments = ", ".join(f"{key} = ?" for key in values)
            await self.bot.db.execute(
                f"UPDATE giveaways SET {assignments} WHERE id = ?", (*values.values(), giveaway_id))
            return True

    async def finish(self, giveaway_id: int) -> Optional[list[int]]:
        """Termine un giveaway (tirage, mise à jour de l'embed, annonce). None si déjà terminé."""
        async with self._lock:
            row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
            if row is None or row["status"] not in ("active", "paused"):
                return None
            winners = await self.draw(row, row["winner_count"])
            await self.bot.db.execute(
                "UPDATE giveaways SET status = 'ended', ends_at = ?, remaining = NULL, "
                "winners = ?, drawn = ? WHERE id = ?",
                (int(time.time()), json.dumps(winners), json.dumps(winners), giveaway_id))
        await self.refresh(giveaway_id)
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
        await self.announce(row, winners)
        return winners

    # --- participation ---
    async def handle_join(self, interaction: discord.Interaction) -> None:
        row = await self.bot.db.fetchone(
            "SELECT * FROM giveaways WHERE message_id = ?", (interaction.message.id,))
        if row is None:
            await respond(interaction, "❌ This giveaway could not be found.")
            return
        if row["status"] != "active":
            await respond(interaction, f"❌ This giveaway is not active (status: {STATUS_LABELS[row['status']]}).")
            return
        member = interaction.user
        role_id = row["required_role_id"]
        if role_id and not any(r.id == role_id for r in member.roles):
            await respond(interaction, f"🔒 You need the role <@&{role_id}> to enter.")
            return

        existing = await self.bot.db.fetchone(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id = ? AND user_id = ?", (row["id"], member.id))
        if existing:
            await respond(interaction, "✅ You're already entered in this giveaway.", view=LeaveView(self, row["id"]))
            return
        await self.bot.db.execute(
            "INSERT INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)", (row["id"], member.id))
        self.mark_dirty(row["id"])
        await respond(interaction, "🎉 You're in, good luck!")

    # --- sélection d'un giveaway pour les sous-commandes ---
    async def resolve(self, interaction: discord.Interaction, ref: Optional[str],
                      states: tuple[str, ...]):
        db, guild = self.bot.db, interaction.guild
        if ref:
            ref = ref.strip().lstrip("#")
            row = None
            if ref.isdigit():
                row = await db.fetchone(
                    "SELECT * FROM giveaways WHERE guild_id = ? AND (id = ? OR message_id = ?)",
                    (guild.id, int(ref), int(ref)))
            if row is None:
                await respond(interaction, "❌ Giveaway not found. Use the autocomplete of the `giveaway` option.")
                return None
            if row["status"] not in states:
                await respond(interaction, f"❌ This giveaway's status is {STATUS_LABELS[row['status']]}: action not possible.")
                return None
            return row

        marks = ", ".join("?" * len(states))
        row = await db.fetchone(
            f"SELECT * FROM giveaways WHERE guild_id = ? AND channel_id = ? AND status IN ({marks}) "
            "ORDER BY id DESC LIMIT 1", (guild.id, interaction.channel_id, *states))
        if row is None:
            wanted = " / ".join(STATUS_LABELS[s] for s in states)
            await respond(interaction, f"❌ No giveaway ({wanted}) in this channel. "
                                       "Specify which one with the `giveaway` option.")
        return row

    # --- commandes ---
    @giveaway.command(name="start", description="Start a giveaway in this channel")
    @app_commands.describe(
        prize="What can be won",
        duration="Duration: 30s, 10m, 2h, 3d, 1h30m…",
        winners="Number of winners (default 1)",
        condition="Role required to enter",
        image="Custom giveaway image",
        description="Extra text shown in the giveaway")
    @staff_only()
    async def start_cmd(self, interaction: discord.Interaction,
                        prize: app_commands.Range[str, 1, 200],
                        duration: str,
                        winners: app_commands.Range[int, 1, config.GIVEAWAY_MAX_WINNERS] = 1,
                        condition: Optional[discord.Role] = None,
                        image: Optional[discord.Attachment] = None,
                        description: Optional[app_commands.Range[str, 1, 1000]] = None) -> None:
        guild, channel = interaction.guild, interaction.channel
        seconds = parse_duration(duration)
        if not seconds or seconds < config.GIVEAWAY_MIN_DURATION or seconds > config.GIVEAWAY_MAX_DURATION:
            await respond(interaction,
                          f"❌ Invalid duration. Examples: `30s`, `10m`, `2h`, `3d`, `1h30m` "
                          f"(from {format_duration(config.GIVEAWAY_MIN_DURATION)} to "
                          f"{format_duration(config.GIVEAWAY_MAX_DURATION)}).")
            return
        if condition is not None and condition.is_default():
            await respond(interaction, "❌ @everyone can't be used as a requirement: leave the option empty.")
            return
        if image is not None:
            suffix = Path(image.filename).suffix.lower()
            if not (image.content_type or "").startswith("image/") or suffix not in IMAGE_EXTENSIONS:
                await respond(interaction, "❌ The file must be an image (png, jpg, gif or webp).")
                return
            if image.size > config.GIVEAWAY_MAX_IMAGE_BYTES:
                await respond(interaction, "❌ Image too large (8 MB maximum).")
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await respond(interaction, "❌ Run this command in a text channel.")
            return
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links
                and (image is None or perms.attach_files)):
            await respond(interaction, "❌ I'm missing permissions here (send messages, "
                                       "embed links, attach files).")
            return
        active = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM giveaways WHERE guild_id = ? AND status IN ('active', 'paused')",
            (guild.id,))
        if active["c"] >= config.GIVEAWAY_MAX_ACTIVE:
            await respond(interaction, f"❌ Limit of {config.GIVEAWAY_MAX_ACTIVE} active giveaways reached.")
            return

        await interaction.response.defer(ephemeral=True)
        # L'image est ré-envoyée avec le message : l'URL d'une pièce jointe de commande expire.
        file, image_name = None, None
        if image is not None:
            image_name = f"giveaway{Path(image.filename).suffix.lower()}"
            try:
                file = await image.to_file(filename=image_name)
            except discord.HTTPException:
                await respond(interaction, "❌ Unable to fetch the image.")
                return

        ends_at = int(time.time()) + seconds
        giveaway_id = await self.bot.db.execute(
            "INSERT INTO giveaways (guild_id, channel_id, host_id, prize, description, image_name, "
            "winner_count, required_role_id, ends_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild.id, channel.id, interaction.user.id, prize, description, image_name, winners,
             condition.id if condition else None, ends_at, int(time.time())))
        row = {"id": giveaway_id, "status": "active", "prize": prize, "description": description,
               "image_name": image_name, "winner_count": winners, "host_id": interaction.user.id,
               "required_role_id": condition.id if condition else None, "ends_at": ends_at,
               "remaining": None, "winners": "[]"}
        try:
            message = await channel.send(embed=build_embed(row, 0), view=GiveawayView(), file=file)
        except discord.HTTPException:
            await self.bot.db.execute("DELETE FROM giveaways WHERE id = ?", (giveaway_id,))
            await respond(interaction, "❌ Unable to send the giveaway in this channel.")
            return
        await self.bot.db.execute("UPDATE giveaways SET message_id = ? WHERE id = ?",
                                  (message.id, giveaway_id))
        await respond(interaction, f"✅ Giveaway #{giveaway_id} started: {message.jump_url}")

    @giveaway.command(name="end", description="End a giveaway immediately and draw the winners")
    @app_commands.describe(giveaway="Giveaway to end (default: the one in this channel)")
    @staff_only()
    async def end_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None) -> None:
        row = await self.resolve(interaction, giveaway, ("active", "paused"))
        if row is None:
            return
        await interaction.response.defer(ephemeral=True)
        if await self.finish(row["id"]) is None:
            await respond(interaction, "❌ This giveaway has already ended.")
            return
        await respond(interaction, f"✅ Giveaway #{row['id']} (**{row['prize']}**) ended.")

    @giveaway.command(name="reroll", description="Draw new winners for an ended giveaway")
    @app_commands.describe(giveaway="Target giveaway (default: the last ended one in this channel)",
                           winners="Number of new winners (default: the original number)")
    @staff_only()
    async def reroll_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None,
                         winners: Optional[app_commands.Range[int, 1, config.GIVEAWAY_MAX_WINNERS]] = None) -> None:
        row = await self.resolve(interaction, giveaway, ("ended",))
        if row is None:
            return
        await interaction.response.defer(ephemeral=True)
        async with self._lock:
            row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (row["id"],))
            new = await self.draw(row, winners or row["winner_count"])
            if not new:
                await respond(interaction, "❌ No other eligible entrant (all have already been drawn "
                                           "or no longer meet the requirement).")
                return
            drawn = json.loads(row["drawn"]) + new
            await self.bot.db.execute("UPDATE giveaways SET winners = ?, drawn = ? WHERE id = ?",
                                      (json.dumps(new), json.dumps(drawn), row["id"]))
        await self.refresh(row["id"])
        row = await self.bot.db.fetchone("SELECT * FROM giveaways WHERE id = ?", (row["id"],))
        await self.announce(row, new, reroll=True)
        await respond(interaction, f"✅ {len(new)} new winner(s) drawn for giveaway #{row['id']}.")

    @giveaway.command(name="cancel", description="Cancel a giveaway (no winner)")
    @app_commands.describe(giveaway="Giveaway to cancel (default: the one in this channel)")
    @staff_only()
    async def cancel_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None) -> None:
        row = await self.resolve(interaction, giveaway, ("active", "paused"))
        if row is None:
            return
        await interaction.response.defer(ephemeral=True)
        if not await self.set_state(row["id"], ("active", "paused"), status="cancelled", remaining=None):
            await respond(interaction, "❌ This giveaway is no longer running.")
            return
        await self.refresh(row["id"])
        await respond(interaction, f"🚫 Giveaway #{row['id']} (**{row['prize']}**) cancelled.")

    @giveaway.command(name="pause", description="Pause a giveaway (the remaining time is kept)")
    @app_commands.describe(giveaway="Giveaway to pause (default: the one in this channel)")
    @staff_only()
    async def pause_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None) -> None:
        row = await self.resolve(interaction, giveaway, ("active",))
        if row is None:
            return
        await interaction.response.defer(ephemeral=True)
        remaining = max(row["ends_at"] - int(time.time()), 1)
        if not await self.set_state(row["id"], ("active",), status="paused", remaining=remaining):
            await respond(interaction, "❌ This giveaway is no longer active.")
            return
        await self.refresh(row["id"])
        await respond(interaction, f"⏸️ Giveaway #{row['id']} paused ({format_duration(remaining)} remaining).")

    @giveaway.command(name="resume", description="Resume a paused giveaway")
    @app_commands.describe(giveaway="Giveaway to resume (default: the one in this channel)")
    @staff_only()
    async def resume_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None) -> None:
        row = await self.resolve(interaction, giveaway, ("paused",))
        if row is None:
            return
        await interaction.response.defer(ephemeral=True)
        remaining = row["remaining"] or 1
        if not await self.set_state(row["id"], ("paused",), status="active",
                                    ends_at=int(time.time()) + remaining, remaining=None):
            await respond(interaction, "❌ This giveaway is no longer paused.")
            return
        await self.refresh(row["id"])
        await respond(interaction, f"▶️ Giveaway #{row['id']} resumed: ends {ts(int(time.time()) + remaining)}.")

    @giveaway.command(name="list", description="Show the running giveaways on the server")
    async def list_cmd(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT g.*, (SELECT COUNT(*) FROM giveaway_entries e WHERE e.giveaway_id = g.id) AS entries "
            "FROM giveaways g WHERE g.guild_id = ? AND g.status IN ('active', 'paused') "
            "ORDER BY g.ends_at LIMIT 25", (interaction.guild.id,))
        if not rows:
            await respond(interaction, "No running giveaways.")
            return
        lines = []
        for row in rows:
            url = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
            when = (f"ends {ts(row['ends_at'])}" if row["status"] == "active"
                    else f"paused ({format_duration(row['remaining'] or 0)} remaining)")
            prize = row["prize"] if len(row["prize"]) <= 60 else row["prize"][:57] + "..."
            lines.append(f"{STATUS_EMOJIS[row['status']]} **#{row['id']}** · {prize}\n"
                         f"└ <#{row['channel_id']}> · {when} · 👥 {row['entries']} · [View]({url})")
        await respond(interaction, embed=make_embed(f"🎁 Running giveaways ({len(rows)})", "\n\n".join(lines)))

    @giveaway.command(name="info", description="Show a giveaway's information")
    @app_commands.describe(giveaway="Giveaway to view (default: the latest in this channel)")
    async def info_cmd(self, interaction: discord.Interaction, giveaway: Optional[str] = None) -> None:
        row = await self.resolve(interaction, giveaway, tuple(STATUS_LABELS))
        if row is None:
            return
        count = await self.count_entries(row["id"])
        url = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
        embed = make_embed(f"{STATUS_EMOJIS[row['status']]} {row['prize']}")
        if row["description"]:
            embed.description = row["description"]
        embed.add_field(name="Status", value=STATUS_LABELS[row["status"]].capitalize())
        embed.add_field(name="Host", value=f"<@{row['host_id']}>")
        embed.add_field(name="Channel", value=f"<#{row['channel_id']}>")
        embed.add_field(name="Winners", value=str(row["winner_count"]))
        embed.add_field(name="Entries", value=str(count))
        embed.add_field(name="Requirement",
                        value=f"<@&{row['required_role_id']}>" if row["required_role_id"] else "None")
        if row["status"] == "active":
            embed.add_field(name="Ends", value=f"{ts(row['ends_at'])}\n{ts(row['ends_at'], 'f')}")
        elif row["status"] == "paused":
            embed.add_field(name="Time remaining", value=format_duration(row["remaining"] or 0))
        elif row["status"] == "ended":
            embed.add_field(name="Ended", value=ts(row["ends_at"], "f"))
            winners = json.loads(row["winners"])
            embed.add_field(name="Winner(s)", value=mentions(winners) if winners else "None", inline=False)
        embed.add_field(name="Message", value=f"[Go to giveaway]({url})", inline=False)
        embed.set_footer(text=f"Giveaway #{row['id']} • created on {datetime.fromtimestamp(row['created_at']).strftime('%Y-%m-%d')}")
        await respond(interaction, embed=embed)

    # --- autocomplétion de l'option `giveaway` ---
    async def _autocomplete(self, interaction: discord.Interaction, current: str,
                            states: tuple[str, ...], *, staff: bool) -> list[app_commands.Choice[str]]:
        if interaction.guild is None or (staff and not await is_staff(self.bot, interaction.user)):
            return []
        marks = ", ".join("?" * len(states))
        rows = await self.bot.db.fetchall(
            f"SELECT id, prize, status FROM giveaways WHERE guild_id = ? AND status IN ({marks}) "
            "ORDER BY id DESC LIMIT 100", (interaction.guild.id, *states))
        needle = current.lower().lstrip("#")
        choices = []
        for row in rows:
            label = f"#{row['id']} · {row['prize']} ({STATUS_LABELS[row['status']]})"
            if needle and needle not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=str(row["id"])))
            if len(choices) == 25:
                break
        return choices

    @end_cmd.autocomplete("giveaway")
    @cancel_cmd.autocomplete("giveaway")
    async def ac_open(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ("active", "paused"), staff=True)

    @pause_cmd.autocomplete("giveaway")
    async def ac_active(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ("active",), staff=True)

    @resume_cmd.autocomplete("giveaway")
    async def ac_paused(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ("paused",), staff=True)

    @reroll_cmd.autocomplete("giveaway")
    async def ac_ended(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ("ended",), staff=True)

    @info_cmd.autocomplete("giveaway")
    async def ac_any(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, tuple(STATUS_LABELS), staff=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaways(bot))
