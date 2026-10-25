import json
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import (ensure_infrastructure, make_embed, parse_color, respond)

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def can_send(channel: discord.abc.GuildChannel, guild: discord.Guild) -> bool:
    perms = channel.permissions_for(guild.me)
    return perms.view_channel and perms.send_messages


# --- Embed personnalisé -------------------------------------------------------
class EmbedModal(discord.ui.Modal, title="Create an embed"):
    titre = discord.ui.TextInput(label="Title", max_length=256, required=False)
    contenu = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph,
                                   max_length=4000)
    couleur = discord.ui.TextInput(label="Color (hex, e.g. #5865F2)", max_length=7, required=False)
    image = discord.ui.TextInput(label="Image URL (optional)", max_length=500, required=False)
    footer = discord.ui.TextInput(label="Footer (optional)", max_length=200, required=False)

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(description=self.contenu.value,
                              color=parse_color(self.couleur.value))
        if self.titre.value:
            embed.title = self.titre.value
        if self.image.value.startswith(("http://", "https://")):
            embed.set_image(url=self.image.value)
        if self.footer.value:
            embed.set_footer(text=self.footer.value)
        try:
            await self.channel.send(embed=embed)
        except discord.HTTPException:
            await respond(interaction, "❌ Unable to send the embed (check the image URL and my permissions).")
            return
        await respond(interaction, f"✅ Embed sent in {self.channel.mention}.")


# --- Sondages -----------------------------------------------------------------
def build_poll_embed(question: str, options: list[str], counts: list[int], author: discord.abc.User) -> discord.Embed:
    total = sum(counts)
    lines = []
    for emoji, option, count in zip(NUMBER_EMOJIS, options, counts):
        percent = (count / total * 100) if total else 0
        filled = round(percent / 10)
        bar = "█" * filled + "░" * (10 - filled)
        lines.append(f"{emoji} **{option}**\n`{bar}` {count} vote(s) · {percent:.0f}%")
    embed = make_embed(f"📊 {question}", "\n\n".join(lines))
    embed.set_footer(text=f"Poll by {author.display_name} • {total} vote(s) • "
                          "click your choice again to remove your vote")
    return embed


class PollButton(discord.ui.Button):
    def __init__(self, index: int, label: Optional[str] = None) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, emoji=NUMBER_EMOJIS[index],
                         label=(label or f"Option {index + 1}")[:80], custom_id=f"poll:{index}")
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "ServerManagement" = interaction.client.get_cog("ServerManagement")
        await cog.handle_vote(interaction, self.index)


class PollView(discord.ui.View):
    def __init__(self, options: Optional[list[str]] = None) -> None:
        super().__init__(timeout=None)
        if options is None:  # vue enregistrée une fois pour tous les sondages
            options = [None] * config.POLL_MAX_OPTIONS
        for index, option in enumerate(options):
            self.add_item(PollButton(index, option))


# --- Cog ----------------------------------------------------------------------
class ServerManagement(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(PollView())

    @app_commands.command(name="setup", description="Automatically configure the bot's systems")
    @app_commands.describe(staff_role="Staff role (created automatically if it doesn't exist)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction, staff_role: Optional[discord.Role] = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            infra = await ensure_infrastructure(self.bot, interaction.guild, staff_role=staff_role)
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I'm missing the “Manage Channels” and/or “Manage Roles” permissions.", ephemeral=True)
            return
        embed = make_embed("✅ Setup complete", color=config.SUCCESS_COLOR)
        embed.add_field(name="Staff role", value=infra["staff_role"].mention, inline=False)
        embed.add_field(name="Ticket category", value=infra["category"].name, inline=False)
        embed.add_field(name="Ticket logs", value=infra["ticket_log"].mention)
        embed.add_field(name="Moderation logs", value=infra["mod_log"].mention)
        embed.add_field(
            name="Next step",
            value="Go to your tickets welcome channel and run `/ticket setup` to post the panel there.",
            inline=False)
        if infra["created"]:
            embed.add_field(name="Automatically created", value="\n".join(infra["created"]), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show server information")
    @app_commands.guild_only()
    async def serverinfo_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        bots = sum(1 for member in guild.members if member.bot)
        embed = make_embed(f"📋 {guild.name}")
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "D"))
        embed.add_field(name="ID", value=f"`{guild.id}`")
        embed.add_field(name="Members", value=f"{guild.member_count} ({bots} bots)")
        embed.add_field(name="Channels", value=(
            f"💬 {len(guild.text_channels)} · 🔊 {len(guild.voice_channels)} · 📁 {len(guild.categories)}"))
        embed.add_field(name="Roles", value=str(len(guild.roles) - 1))
        embed.add_field(name="Boosts", value=f"Tier {guild.premium_tier} ({guild.premium_subscription_count})")
        embed.add_field(name="Emojis", value=str(len(guild.emojis)))
        embed.add_field(name="Verification", value=str(guild.verification_level).capitalize())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show a member's information")
    @app_commands.describe(user="Member (you by default)")
    @app_commands.guild_only()
    async def userinfo_cmd(self, interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
        member = user or interaction.user
        roles = [role.mention for role in reversed(member.roles) if not role.is_default()]
        shown = " ".join(roles[:15]) + (f" … (+{len(roles) - 15})" if len(roles) > 15 else "")
        row = await self.bot.db.fetchone(
            "SELECT COUNT(*) AS c FROM warnings WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, member.id))

        embed = make_embed(f"👤 {member.display_name}", color=member.color.value or config.EMBED_COLOR)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member} {'🤖' if member.bot else ''}\n`{member.id}`")
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "D"))
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "D"))
        embed.add_field(name="Top role", value=member.top_role.mention)
        embed.add_field(name="Warnings", value=str(row["c"]))
        if member.is_timed_out():
            embed.add_field(name="Timeout until", value=discord.utils.format_dt(member.timed_out_until, "R"))
        embed.add_field(name=f"Roles ({len(roles)})", value=shown or "None", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="role", description="Add or remove a role from a member")
    @app_commands.describe(user="Target member", role="Role to add / remove")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    async def role_cmd(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role) -> None:
        guild = interaction.guild
        if role.is_default() or role.managed:
            await respond(interaction, "❌ This role can't be assigned manually.")
            return
        if interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
            await respond(interaction, "❌ This role is higher than or equal to your highest role.")
            return
        if role >= guild.me.top_role:
            await respond(interaction, "❌ This role is higher than or equal to my highest role.")
            return
        audit = f"/role by {interaction.user}"
        if role in user.roles:
            await user.remove_roles(role, reason=audit)
            await interaction.response.send_message(
                f"➖ Role {role.mention} removed from {user.mention}.", allowed_mentions=discord.AllowedMentions.none())
        else:
            await user.add_roles(role, reason=audit)
            await interaction.response.send_message(
                f"➕ Role {role.mention} added to {user.mention}.", allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="nick", description="Change a member's nickname")
    @app_commands.describe(user="Target member", nickname="New nickname (leave empty to reset)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_nicknames=True)
    @app_commands.checks.has_permissions(manage_nicknames=True)
    async def nick_cmd(self, interaction: discord.Interaction, user: discord.Member,
                       nickname: Optional[str] = None) -> None:
        guild = interaction.guild
        if user.id == guild.owner_id:
            await respond(interaction, "❌ Can't change the server owner's nickname.")
            return
        if user.id != interaction.user.id and interaction.user.id != guild.owner_id \
                and user.top_role >= interaction.user.top_role:
            await respond(interaction, "❌ This member's role is higher than or equal to yours.")
            return
        if user.top_role >= guild.me.top_role and user.id != guild.me.id:
            await respond(interaction, "❌ This member's role is higher than or equal to mine.")
            return
        if nickname and len(nickname) > 32:
            await respond(interaction, "❌ A nickname can't exceed 32 characters.")
            return
        await user.edit(nick=nickname or None, reason=f"/nick by {interaction.user}")
        if nickname:
            await interaction.response.send_message(f"✏️ {user.mention}'s nickname changed to **{nickname}**.",
                                                    allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(f"✏️ {user.mention}'s nickname has been reset.",
                                                    allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="embed", description="Create a custom embed")
    @app_commands.describe(channel="Channel to send the embed in (current channel by default)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def embed_cmd(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel) or not can_send(channel, interaction.guild):
            await respond(interaction, "❌ I can't send messages in this channel.")
            return
        await interaction.response.send_modal(EmbedModal(channel))

    @app_commands.command(name="say", description="Make the bot send a message")
    @app_commands.describe(text="Message to send (use \\n for a line break)",
                           channel="Destination channel (current channel by default)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def say_cmd(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 2000],
                      channel: Optional[discord.TextChannel] = None) -> None:
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel) or not can_send(channel, interaction.guild):
            await respond(interaction, "❌ I can't send messages in this channel.")
            return
        await channel.send(text.replace("\\n", "\n"), allowed_mentions=discord.AllowedMentions.none())
        await respond(interaction, f"✅ Message sent in {channel.mention}.")

    @app_commands.command(name="announce", description="Post an announcement with an embed")
    @app_commands.describe(title="Announcement title", message="Content (use \\n for a line break)",
                           channel="Announcement channel (current channel by default)",
                           mention="Role to mention (@everyone allowed)")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def announce_cmd(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 256],
                           message: app_commands.Range[str, 1, 4000],
                           channel: Optional[discord.TextChannel] = None,
                           mention: Optional[discord.Role] = None) -> None:
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel) or not can_send(channel, interaction.guild):
            await respond(interaction, "❌ I can't send messages in this channel.")
            return
        embed = make_embed(f"📢 {title}", message.replace("\\n", "\n"))
        embed.set_footer(text=f"Announcement by {interaction.user.display_name}",
                         icon_url=interaction.user.display_avatar.url)
        if mention is None:
            await channel.send(embed=embed)
        elif mention.is_default():
            await channel.send("@everyone", embed=embed, allowed_mentions=discord.AllowedMentions(everyone=True))
        else:
            await channel.send(mention.mention, embed=embed,
                               allowed_mentions=discord.AllowedMentions(roles=[mention]))
        await respond(interaction, f"✅ Announcement posted in {channel.mention}.")

    @app_commands.command(name="poll", description="Create a poll with buttons")
    @app_commands.describe(question="Poll question", option1="First option", option2="Second option",
                           option3="Third option", option4="Fourth option", option5="Fifth option")
    @app_commands.guild_only()
    async def poll_cmd(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 250],
                       option1: app_commands.Range[str, 1, 80], option2: app_commands.Range[str, 1, 80],
                       option3: Optional[str] = None, option4: Optional[str] = None,
                       option5: Optional[str] = None) -> None:
        options = [o.strip()[:80] for o in (option1, option2, option3, option4, option5) if o and o.strip()]
        embed = build_poll_embed(question, options, [0] * len(options), interaction.user)
        await interaction.response.send_message(embed=embed, view=PollView(options))
        message = await interaction.original_response()
        await self.bot.db.execute(
            "INSERT INTO polls (message_id, guild_id, channel_id, author_id, question, options) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message.id, interaction.guild_id, interaction.channel_id, interaction.user.id, question,
             json.dumps(options)))

    async def handle_vote(self, interaction: discord.Interaction, index: int) -> None:
        poll = await self.bot.db.fetchone("SELECT * FROM polls WHERE message_id = ?", (interaction.message.id,))
        if poll is None:
            await respond(interaction, "❌ This poll could not be found.")
            return
        options = json.loads(poll["options"])
        if index >= len(options):
            await respond(interaction, "❌ Invalid option.")
            return

        existing = await self.bot.db.fetchone(
            "SELECT option_idx FROM poll_votes WHERE message_id = ? AND user_id = ?",
            (poll["message_id"], interaction.user.id))
        if existing and existing["option_idx"] == index:
            await self.bot.db.execute(
                "DELETE FROM poll_votes WHERE message_id = ? AND user_id = ?",
                (poll["message_id"], interaction.user.id))
            feedback = "🗑️ Your vote has been removed."
        else:
            await self.bot.db.execute(
                "INSERT INTO poll_votes (message_id, user_id, option_idx) VALUES (?, ?, ?) "
                "ON CONFLICT(message_id, user_id) DO UPDATE SET option_idx = excluded.option_idx",
                (poll["message_id"], interaction.user.id, index))
            feedback = f"✅ Vote recorded for **{options[index]}**."

        rows = await self.bot.db.fetchall(
            "SELECT option_idx, COUNT(*) AS c FROM poll_votes WHERE message_id = ? GROUP BY option_idx",
            (poll["message_id"],))
        counts = [0] * len(options)
        for row in rows:
            if row["option_idx"] < len(counts):
                counts[row["option_idx"]] = row["c"]

        author = interaction.guild.get_member(poll["author_id"]) or interaction.user
        await interaction.response.edit_message(embed=build_poll_embed(poll["question"], options, counts, author))
        await interaction.followup.send(feedback, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerManagement(bot))
