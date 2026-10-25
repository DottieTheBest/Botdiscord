import asyncio
import html
import io
import time
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import (ensure_infrastructure, is_staff, make_embed, respond, send_log, slugify)


# --- Transcription -----------------------------------------------------------
_TRANSCRIPT_CSS = """
body{background:#313338;color:#dbdee1;font-family:Arial,Helvetica,sans-serif;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 4px}.meta{color:#949ba4;font-size:13px;margin-bottom:20px}
.msg{display:flex;gap:12px;margin:0 0 14px}.msg img{width:40px;height:40px;border-radius:50%}
.head{font-weight:bold;color:#fff}.time{color:#949ba4;font-size:12px;font-weight:normal;margin-left:6px}
.att,.emb{margin-top:4px;font-size:14px}.emb{border-left:4px solid #5865F2;padding:4px 10px;background:#2b2d31}
a{color:#00a8fc}
"""


async def build_transcript(channel: discord.TextChannel) -> bytes:
    rows = []
    count = 0
    async for msg in channel.history(limit=config.TRANSCRIPT_MAX_MESSAGES, oldest_first=True):
        count += 1
        stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        body = html.escape(msg.content).replace("\n", "<br>")
        attachments = "".join(
            f'<div class="att">📎 <a href="{html.escape(a.url)}">{html.escape(a.filename)}</a></div>'
            for a in msg.attachments)
        embeds = "".join(
            '<div class="emb"><b>{}</b> {}</div>'.format(
                html.escape(e.title or ""), html.escape(e.description or "").replace("\n", "<br>"))
            for e in msg.embeds)
        avatar = html.escape(msg.author.display_avatar.replace(size=64).url)
        rows.append(
            f'<div class="msg"><img src="{avatar}" alt="">'
            f'<div><div class="head">{html.escape(msg.author.display_name)}'
            f'<span class="time">{stamp} UTC</span></div>{body}{attachments}{embeds}</div></div>')

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    document = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>Transcript — {html.escape(channel.name)}</title><style>{_TRANSCRIPT_CSS}</style></head><body>"
        f"<h1>#{html.escape(channel.name)}</h1>"
        f"<div class='meta'>{html.escape(channel.guild.name)} · {count} message(s) · generated on {generated}</div>"
        + "".join(rows) + "</body></html>")
    return document.encode("utf-8")


def transcript_file(data: bytes, channel_name: str) -> discord.File:
    return discord.File(io.BytesIO(data), filename=f"transcript-{channel_name}.html")


def ticket_embed(owner_id: int, key: str, subject: Optional[str], claimed_by: Optional[int] = None) -> discord.Embed:
    label, emoji, _ = config.ticket_category(key)
    embed = make_embed(
        f"{emoji} Ticket — {label}",
        "Thanks for opening a ticket! Please explain your request in detail, "
        "a staff member will get back to you as soon as possible.")
    if subject:
        embed.add_field(name="📝 Subject", value=subject[:1000], inline=False)
    embed.add_field(name="Opened by", value=f"<@{owner_id}>")
    embed.add_field(name="Category", value=label)
    embed.add_field(name="🙋 Claimed by",
                    value=f"<@{claimed_by}>" if claimed_by else "Nobody yet")
    return embed


# --- Vues persistantes -------------------------------------------------------
class TicketCategorySelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=label, value=key, emoji=emoji, description=desc)
            for key, (label, emoji, desc) in config.TICKET_CATEGORIES.items()
        ]
        super().__init__(custom_id="ticket:panel", placeholder="Choose a category…",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.open_ticket(interaction, self.values[0])
        try:  # réinitialise la sélection affichée dans le panneau
            await interaction.message.edit(view=TicketPanelView())
        except discord.HTTPException:
            pass


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect())


class ConfirmCloseView(discord.ui.View):
    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=60)
        self.cog = cog

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🔒 Closing…", view=None)
        await self.cog.close_ticket(interaction.channel, interaction.user, None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Closing cancelled.", view=None)


class TicketControlView(discord.ui.View):
    def __init__(self, claimed: bool = False) -> None:
        super().__init__(timeout=None)
        self.claim_btn.disabled = claimed

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, emoji="🔒",
                       custom_id="ticket:close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        row = await cog.ticket_row(interaction)
        if row is None:
            return
        if not await cog.can_manage(interaction, row):
            await respond(interaction, "🚫 Only the ticket creator or staff can close it.")
            return
        await respond(interaction, "Do you really want to close this ticket?", view=ConfirmCloseView(cog))

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, emoji="🙋",
                       custom_id="ticket:claim")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.claim(interaction)


# --- Cog ---------------------------------------------------------------------
class Tickets(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Ticket system", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())

    # --- helpers ---
    async def ticket_row(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (interaction.channel_id,))
        if row is None:
            await respond(interaction, "❌ This command only works inside an open ticket.")
        return row

    async def can_manage(self, interaction: discord.Interaction, row) -> bool:
        return interaction.user.id == row["owner_id"] or await is_staff(self.bot, interaction.user)

    async def staff_check(self, interaction: discord.Interaction) -> bool:
        if await is_staff(self.bot, interaction.user):
            return True
        await respond(interaction, "🚫 This action is restricted to staff.")
        return False

    # --- ouverture ---
    async def open_ticket(self, interaction: discord.Interaction, key: str, subject: Optional[str] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        guild, user = interaction.guild, interaction.user
        cfg = await self.bot.db.get_config(guild.id)

        category = guild.get_channel(cfg["ticket_category_id"]) if cfg["ticket_category_id"] else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.followup.send(
                "❌ The ticket system isn't configured. An administrator needs to run `/ticket setup`.",
                ephemeral=True)
            return

        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND owner_id = ? AND closed_at IS NULL",
            (guild.id, user.id))
        if row["c"] >= config.MAX_OPEN_TICKETS_PER_USER:
            await interaction.followup.send(
                f"❌ You already have {row['c']} open ticket(s). Close one before creating a new one.",
                ephemeral=True)
            return

        label = config.ticket_category(key)[0]
        staff_role = guild.get_role(cfg["staff_role_id"]) if cfg["staff_role_id"] else None
        member_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: member_perms,
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
                attach_files=True, embed_links=True),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True)

        name = f"{key}-{slugify(user.display_name, 20)}"
        try:
            channel = await guild.create_text_channel(
                name, category=category, overwrites=overwrites,
                topic=f"{label} ticket from {user} (ID: {user.id})",
                reason=f"Ticket opened by {user}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I can't create the channel: I'm missing the “Manage Channels” permission.",
                ephemeral=True)
            return

        await self.bot.db.execute(
            "INSERT INTO tickets (channel_id, guild_id, owner_id, category, subject, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (channel.id, guild.id, user.id, key, subject, int(time.time())))

        mentions = user.mention + (f" {staff_role.mention}" if staff_role else "")
        await channel.send(content=mentions, embed=ticket_embed(user.id, key, subject),
                           view=TicketControlView())
        await interaction.followup.send(f"✅ Your ticket has been created: {channel.mention}", ephemeral=True)

    # --- claim ---
    async def claim(self, interaction: discord.Interaction) -> None:
        row = await self.ticket_row(interaction)
        if row is None:
            return
        if not await self.staff_check(interaction):
            return
        if row["claimed_by"]:
            await respond(interaction, f"ℹ️ This ticket is already claimed by <@{row['claimed_by']}>.")
            return
        await self.bot.db.execute(
            "UPDATE tickets SET claimed_by = ? WHERE channel_id = ?", (interaction.user.id, interaction.channel_id))
        embed = ticket_embed(row["owner_id"], row["category"], row["subject"], interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=TicketControlView(claimed=True))
        await interaction.followup.send(f"🙋 {interaction.user.mention} has claimed this ticket.")

    # --- fermeture ---
    async def close_ticket(self, channel: discord.TextChannel, closer: discord.abc.User,
                           reason: Optional[str]) -> None:
        row = await self.bot.db.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ? AND closed_at IS NULL", (channel.id,))
        if row is None:
            return
        await self.bot.db.execute(
            "UPDATE tickets SET closed_at = ? WHERE channel_id = ?", (int(time.time()), channel.id))

        guild = channel.guild
        data = await build_transcript(channel)
        label = config.ticket_category(row["category"])[0]

        embed = make_embed("🔒 Ticket closed", color=config.ERROR_COLOR)
        embed.add_field(name="Ticket", value=f"`#{channel.name}`")
        embed.add_field(name="Category", value=label)
        embed.add_field(name="Opened by", value=f"<@{row['owner_id']}>")
        embed.add_field(name="Closed by", value=closer.mention)
        if row["claimed_by"]:
            embed.add_field(name="Claimed by", value=f"<@{row['claimed_by']}>")
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)

        await send_log(self.bot, guild, "ticket_log_channel_id", embed=embed,
                       file=transcript_file(data, channel.name))

        owner = guild.get_member(row["owner_id"])
        if owner is not None:
            try:
                await owner.send(
                    content=f"Your ticket **#{channel.name}** on **{guild.name}** has been closed. "
                            "Here is the conversation transcript.",
                    file=transcript_file(data, channel.name))
            except discord.HTTPException:
                pass

        try:
            await channel.send("🔒 Ticket closed. Deleting the channel in 5 seconds…")
        except discord.HTTPException:
            pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {closer}")
        except discord.HTTPException:
            pass

    # --- commandes /ticket ---
    @ticket.command(name="create", description="Open a private ticket")
    @app_commands.describe(category="Ticket type (Support by default)", subject="Summary of your request")
    @app_commands.choices(category=[
        app_commands.Choice(name=label, value=key)
        for key, (label, _emoji, _desc) in config.TICKET_CATEGORIES.items()])
    async def ticket_create(self, interaction: discord.Interaction,
                            category: Optional[app_commands.Choice[str]] = None,
                            subject: Optional[str] = None) -> None:
        await self.open_ticket(interaction, category.value if category else "support", subject)

    @ticket.command(name="setup", description="Set up the ticket system in this channel")
    @app_commands.describe(staff_role="Staff role (created automatically if it doesn't exist)",
                           category="Discord category where tickets are created (created if missing)")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction,
                           staff_role: Optional[discord.Role] = None,
                           category: Optional[discord.CategoryChannel] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            infra = await ensure_infrastructure(
                self.bot, interaction.guild, staff_role=staff_role, category=category)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I'm missing the “Manage Channels” and/or “Manage Roles” permissions.",
                ephemeral=True)
            return

        lines = "\n".join(
            f"{emoji} **{label}** — {desc}" for label, emoji, desc in config.TICKET_CATEGORIES.values())
        panel = make_embed(
            "🎫 Need help?",
            "Pick a category from the menu below to open a private ticket "
            f"with the team.\n\n{lines}")
        try:
            await interaction.channel.send(embed=panel, view=TicketPanelView())
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I can't send messages in this channel.", ephemeral=True)
            return

        summary = [
            f"✅ Ticket panel installed in {interaction.channel.mention}.",
            f"• Staff role: {infra['staff_role'].mention}",
            f"• Ticket category: **{infra['category'].name}**",
            f"• Ticket logs: {infra['ticket_log'].mention}",
        ]
        if infra["created"]:
            summary.append("Automatically created: " + ", ".join(infra["created"]))
        await interaction.followup.send("\n".join(summary), ephemeral=True)

    # --- commandes de gestion du ticket ---
    @app_commands.command(name="close", description="Close the current ticket")
    @app_commands.describe(reason="Reason for closing")
    @app_commands.guild_only()
    async def close_cmd(self, interaction: discord.Interaction, reason: Optional[str] = None) -> None:
        row = await self.ticket_row(interaction)
        if row is None:
            return
        if not await self.can_manage(interaction, row):
            await respond(interaction, "🚫 Only the ticket creator or staff can close it.")
            return
        await interaction.response.send_message("🔒 Closing the ticket…")
        await self.close_ticket(interaction.channel, interaction.user, reason)

    @app_commands.command(name="add", description="Add a user to the current ticket")
    @app_commands.describe(user="Member to add")
    @app_commands.guild_only()
    async def add_cmd(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if await self.ticket_row(interaction) is None or not await self.staff_check(interaction):
            return
        await interaction.channel.set_permissions(
            user, view_channel=True, send_messages=True, read_message_history=True,
            attach_files=True, embed_links=True, reason=f"Added to the ticket by {interaction.user}")
        await interaction.response.send_message(f"✅ {user.mention} has been added to the ticket.")

    @app_commands.command(name="remove", description="Remove a user from the current ticket")
    @app_commands.describe(user="Member to remove")
    @app_commands.guild_only()
    async def remove_cmd(self, interaction: discord.Interaction, user: discord.Member) -> None:
        row = await self.ticket_row(interaction)
        if row is None or not await self.staff_check(interaction):
            return
        if user.id == row["owner_id"]:
            await respond(interaction, "❌ You can't remove the ticket creator. Use `/close`.")
            return
        await interaction.channel.set_permissions(
            user, overwrite=None, reason=f"Removed from the ticket by {interaction.user}")
        await interaction.response.send_message(f"✅ {user.mention} has been removed from the ticket.")

    @app_commands.command(name="rename", description="Rename the current ticket")
    @app_commands.describe(name="New channel name")
    @app_commands.guild_only()
    async def rename_cmd(self, interaction: discord.Interaction, name: str) -> None:
        if await self.ticket_row(interaction) is None or not await self.staff_check(interaction):
            return
        new_name = slugify(name, 90)
        await interaction.response.defer()
        try:  # Discord limite à 2 renommages / 10 min : on évite d'attendre indéfiniment
            await asyncio.wait_for(
                interaction.channel.edit(name=new_name, reason=f"Renamed by {interaction.user}"), timeout=15)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "⏳ Discord limits channel renames (2 every 10 minutes). Please try again later.",
                ephemeral=True)
            return
        await interaction.followup.send(f"✏️ Ticket renamed to **#{new_name}**.")

    @app_commands.command(name="transcript", description="Generate a transcript of the ticket")
    @app_commands.guild_only()
    async def transcript_cmd(self, interaction: discord.Interaction) -> None:
        row = await self.ticket_row(interaction)
        if row is None:
            return
        if not await self.can_manage(interaction, row):
            await respond(interaction, "🚫 Only the ticket creator or staff can generate the transcript.")
            return
        await interaction.response.defer()
        data = await build_transcript(interaction.channel)
        await interaction.followup.send(
            "📄 Transcript generated (open the file in your browser).",
            file=transcript_file(data, interaction.channel.name))

    # --- nettoyage ---
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.bot.db.execute(
            "UPDATE tickets SET closed_at = ? WHERE channel_id = ? AND closed_at IS NULL",
            (int(time.time()), channel.id))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
