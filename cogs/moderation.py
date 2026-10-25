import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import (format_duration, hierarchy_error, log_mod, make_embed, parse_duration,
                   respond, try_dm)

MAX_TIMEOUT = 28 * 24 * 3600  # limite Discord : 28 jours


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._history: dict[tuple[int, int], deque] = defaultdict(deque)
        self._cooldown: dict[tuple[int, int], float] = {}

    async def cog_load(self) -> None:
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()

    @tasks.loop(minutes=10)
    async def cleanup(self) -> None:
        now = time.monotonic()
        for key in [k for k, dq in self._history.items() if not dq or now - dq[-1][0] > config.SPAM_WINDOW]:
            del self._history[key]
        for key in [k for k, until in self._cooldown.items() if until < now]:
            del self._cooldown[key]

    # --- avertissements & sanctions progressives ---
    async def issue_warning(self, guild: discord.Guild, member: discord.Member,
                            moderator: discord.abc.User, reason: str) -> tuple[int, Optional[str]]:
        await self.bot.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild.id, member.id, moderator.id, reason, int(time.time())))
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM warnings WHERE guild_id = ? AND user_id = ?", (guild.id, member.id))
        count = row["c"]

        embed = make_embed("⚠️ Warning", color=config.WARNING_COLOR)
        embed.description = f"You received a warning on **{guild.name}**."
        embed.add_field(name="Reason", value=reason[:1000], inline=False)
        embed.add_field(name="Total warnings", value=str(count))
        await try_dm(member, embed=embed)

        sanction = None
        rule = config.PROGRESSIVE_SANCTIONS.get(count)
        if rule:
            kind, seconds = rule
            audit = f"Progressive sanction ({count} warnings)"
            try:
                if kind == "timeout":
                    await member.timeout(timedelta(seconds=min(seconds, MAX_TIMEOUT)), reason=audit)
                    sanction = f"Timeout of {format_duration(seconds)}"
                elif kind == "kick":
                    await try_dm(member, content=f"You were kicked from **{guild.name}** ({audit}).")
                    await member.kick(reason=audit)
                    sanction = "Kick"
                elif kind == "ban":
                    await try_dm(member, content=f"You were banned from **{guild.name}** ({audit}).")
                    await member.ban(reason=audit)
                    sanction = "Ban"
            except discord.HTTPException:
                sanction = f"{kind} planned but could not be applied (permissions)"
        return count, sanction

    # --- commandes ---
    @app_commands.command(name="ban", description="Ban a user from the server")
    @app_commands.describe(user="User to ban", reason="Reason for the ban",
                           delete_days="Days of messages to delete (0 to 7)")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban_cmd(self, interaction: discord.Interaction, user: discord.User,
                      reason: str = "No reason provided",
                      delete_days: app_commands.Range[int, 0, 7] = 0) -> None:
        guild = interaction.guild
        member = guild.get_member(user.id)
        if member is not None:
            error = hierarchy_error(interaction, member)
            if error:
                await respond(interaction, f"❌ {error}")
                return
            await try_dm(user, content=f"You were banned from **{guild.name}**.\nReason: {reason}")
        await guild.ban(user, reason=f"{interaction.user} : {reason}",
                        delete_message_seconds=delete_days * 86400)
        await interaction.response.send_message(f"🔨 **{user}** has been banned.\nReason: {reason}")
        await log_mod(self.bot, guild, "Ban", user, interaction.user, reason, color=config.ERROR_COLOR)

    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.describe(user="Member to kick", reason="Reason for the kick")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick_cmd(self, interaction: discord.Interaction, user: discord.Member,
                       reason: str = "No reason provided") -> None:
        error = hierarchy_error(interaction, user)
        if error:
            await respond(interaction, f"❌ {error}")
            return
        await try_dm(user, content=f"You were kicked from **{interaction.guild.name}**.\nReason: {reason}")
        await user.kick(reason=f"{interaction.user} : {reason}")
        await interaction.response.send_message(f"👢 **{user}** has been kicked.\nReason: {reason}")
        await log_mod(self.bot, interaction.guild, "Kick", user, interaction.user, reason)

    @app_commands.command(name="timeout", description="Put a member in timeout (temporary mute)")
    @app_commands.describe(user="Target member", duration="E.g. 10m, 2h, 1d, 1h30m — “0” to remove the timeout",
                           reason="Reason for the timeout")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout_cmd(self, interaction: discord.Interaction, user: discord.Member,
                          duration: str = "10m", reason: str = "No reason provided") -> None:
        error = hierarchy_error(interaction, user)
        if error:
            await respond(interaction, f"❌ {error}")
            return
        seconds = parse_duration(duration)
        if seconds is None:
            await respond(interaction, "❌ Invalid duration. Examples: `10m`, `2h`, `1d`, `1h30m`.")
            return
        if seconds > MAX_TIMEOUT:
            await respond(interaction, "❌ A timeout can't exceed 28 days.")
            return

        audit = f"{interaction.user} : {reason}"
        if seconds == 0:
            await user.timeout(None, reason=audit)
            await interaction.response.send_message(f"🔊 **{user}**'s timeout has been removed.")
            await log_mod(self.bot, interaction.guild, "Timeout removed", user, interaction.user, reason,
                          color=config.SUCCESS_COLOR)
            return

        await user.timeout(timedelta(seconds=seconds), reason=audit)
        await try_dm(user, content=f"You were timed out on **{interaction.guild.name}** "
                                   f"for {format_duration(seconds)}.\nReason: {reason}")
        await interaction.response.send_message(
            f"🔇 **{user}** is in timeout for {format_duration(seconds)}.\nReason: {reason}")
        await log_mod(self.bot, interaction.guild, "Timeout", user, interaction.user, reason,
                      extra=f"Duration: {format_duration(seconds)}")

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(user="Member to warn", reason="Reason for the warning")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn_cmd(self, interaction: discord.Interaction, user: discord.Member,
                       reason: str = "No reason provided") -> None:
        error = hierarchy_error(interaction, user)
        if error:
            await respond(interaction, f"❌ {error}")
            return
        await interaction.response.defer()
        count, sanction = await self.issue_warning(interaction.guild, user, interaction.user, reason)
        text = f"⚠️ **{user}** has received a warning ({count} total).\nReason: {reason}"
        if sanction:
            text += f"\n🔨 Automatic sanction: **{sanction}**"
        await interaction.followup.send(text)
        await log_mod(self.bot, interaction.guild, "Warning", user, interaction.user, reason,
                      extra=f"Total: {count}" + (f"\nAutomatic sanction: {sanction}" if sanction else ""))

    @app_commands.command(name="warnings", description="Show a member's warnings")
    @app_commands.describe(user="Target member")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_cmd(self, interaction: discord.Interaction, user: discord.Member) -> None:
        rows = await self.bot.db.fetchall(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (interaction.guild_id, user.id))
        embed = make_embed(f"⚠️ Warnings for {user.display_name}", color=config.WARNING_COLOR)
        embed.set_thumbnail(url=user.display_avatar.url)
        if not rows:
            embed.description = "No warnings. 🎉"
        else:
            embed.description = f"**{len(rows)}** warning(s) in total."
            for row in rows[:10]:
                embed.add_field(
                    name=f"#{row['id']} — {discord.utils.format_dt(datetime.fromtimestamp(row['created_at'], tz=timezone.utc), 'd')}",
                    value=f"{row['reason'][:300]}\nBy <@{row['moderator_id']}>", inline=False)
            if len(rows) > 10:
                embed.set_footer(text=f"Only the 10 most recent are shown ({len(rows)} total).")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clear", description="Delete messages in the channel")
    @app_commands.describe(amount="Number of messages to delete (1 to 100)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def clear_cmd(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await respond(interaction, "❌ Can't delete messages in this type of channel.")
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await channel.purge(limit=amount, reason=f"/clear by {interaction.user}")
        await interaction.followup.send(f"🧹 {len(deleted)} message(s) deleted.", ephemeral=True)

    @app_commands.command(name="slowmode", description="Set the channel's slowmode")
    @app_commands.describe(seconds="Delay between messages in seconds (0 to disable, max 21600)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode_cmd(self, interaction: discord.Interaction,
                           seconds: app_commands.Range[int, 0, 21600]) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await respond(interaction, "❌ Slowmode is only available in text channels.")
            return
        await channel.edit(slowmode_delay=seconds, reason=f"/slowmode by {interaction.user}")
        if seconds == 0:
            await interaction.response.send_message("🐇 Slowmode disabled.")
        else:
            await interaction.response.send_message(f"🐢 Slowmode enabled: {format_duration(seconds)} between messages.")

    @app_commands.command(name="lock", description="Lock the current channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock_cmd(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await respond(interaction, "❌ Only text channels can be locked.")
            return
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                      reason=f"/lock by {interaction.user}")
        await interaction.response.send_message("🔒 Channel locked.")

    @app_commands.command(name="unlock", description="Unlock the current channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock_cmd(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await respond(interaction, "❌ Only text channels can be unlocked.")
            return
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                      reason=f"/unlock by {interaction.user}")
        await interaction.response.send_message("🔓 Channel unlocked.")

    @app_commands.command(name="purge", description="Delete a user's recent messages in this channel")
    @app_commands.describe(user="User whose messages will be deleted",
                           scan="Number of recent messages to scan (10 to 1000, default 100)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge_cmd(self, interaction: discord.Interaction, user: discord.User,
                        scan: app_commands.Range[int, 10, 1000] = 100) -> None:
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await respond(interaction, "❌ Can't delete messages in this type of channel.")
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await channel.purge(limit=scan, check=lambda m: m.author.id == user.id,
                                      reason=f"/purge {user} by {interaction.user}")
        await interaction.followup.send(
            f"🧹 {len(deleted)} message(s) from **{user}** deleted (out of the last {scan}).", ephemeral=True)
        if deleted:
            await log_mod(self.bot, interaction.guild, "Message purge", user, interaction.user,
                          f"{len(deleted)} message(s) deleted in {channel.mention}")

    # --- anti-spam ---
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        member = message.author
        perms = member.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        key = (message.guild.id, member.id)
        now = time.monotonic()

        # Spam en cours de traitement : on supprime simplement les messages qui continuent d'arriver.
        if self._cooldown.get(key, 0) > now:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return

        history = self._history[key]
        history.append((now, message))
        while history and now - history[0][0] > config.SPAM_WINDOW:
            history.popleft()

        reasons = []
        if len(history) >= config.SPAM_MAX_MESSAGES:
            reasons.append(f"{len(history)} messages in {config.SPAM_WINDOW}s")
        if message.content and sum(1 for _, m in history if m.content == message.content) >= config.SPAM_MAX_DUPLICATES:
            reasons.append("repeated message")
        if len(message.mentions) + len(message.role_mentions) >= config.SPAM_MAX_MENTIONS:
            reasons.append("mass mentions")
        if not reasons:
            return

        spam_messages = [m for _, m in history]
        history.clear()
        self._cooldown[key] = now + config.SPAM_WINDOW

        for spam in spam_messages:
            try:
                await spam.delete()
            except discord.HTTPException:
                pass

        reason = f"Automatic spam detection ({', '.join(reasons)})"
        count, sanction = await self.issue_warning(message.guild, member, self.bot.user, reason)
        if sanction is None:
            try:
                await member.timeout(timedelta(seconds=config.SPAM_TIMEOUT), reason=reason)
                sanction = f"Timeout of {format_duration(config.SPAM_TIMEOUT)}"
            except discord.HTTPException:
                pass

        try:
            await message.channel.send(
                f"🚫 {member.mention}, spam is not tolerated ({count} warning(s)).", delete_after=8)
        except discord.HTTPException:
            pass
        await log_mod(self.bot, message.guild, "Spam detected", member, self.bot.user, reason,
                      extra=f"Total warnings: {count}" + (f"\nSanction: {sanction}" if sanction else ""))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
