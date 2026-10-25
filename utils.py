import re
import unicodedata
from typing import Optional

import discord
from discord import app_commands

import config


class NotStaff(app_commands.CheckFailure):
    """Levée quand un membre non-staff utilise une commande réservée au staff."""


# --- Embeds / réponses -------------------------------------------------------
def make_embed(title: Optional[str] = None, description: Optional[str] = None, *,
               color: int = config.EMBED_COLOR) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = discord.utils.utcnow()
    return embed


async def respond(interaction: discord.Interaction, content: Optional[str] = None, *,
                  embed: Optional[discord.Embed] = None, view: Optional[discord.ui.View] = None,
                  ephemeral: bool = True) -> None:
    """Répond à une interaction, qu'elle ait déjà été acquittée ou non."""
    kwargs = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def try_dm(user: discord.abc.User, **kwargs) -> bool:
    try:
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# --- Permissions -------------------------------------------------------------
async def is_staff(bot, member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    cfg = await bot.db.get_config(member.guild.id)
    staff_role_id = cfg.get("staff_role_id")
    return bool(staff_role_id) and any(role.id == staff_role_id for role in member.roles)


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and await is_staff(interaction.client, interaction.user):
            return True
        raise NotStaff("This command is restricted to staff.")

    return app_commands.check(predicate)


def hierarchy_error(interaction: discord.Interaction, target: discord.Member) -> Optional[str]:
    """Renvoie un message d'erreur si l'action sur `target` est interdite, sinon None."""
    guild = interaction.guild
    if target.id == interaction.user.id:
        return "You can't do that to yourself."
    if target.id == guild.owner_id:
        return "You can't act on the server owner."
    if target.id == guild.me.id:
        return "I can't do that to myself."
    if interaction.user.id != guild.owner_id and target.top_role >= interaction.user.top_role:
        return "This member's role is higher than or equal to yours."
    if target.top_role >= guild.me.top_role:
        return "This member's role is higher than or equal to mine: I can't act on them."
    return None


# --- Parsing / formatage -----------------------------------------------------
_DURATION_PART = re.compile(r"(\d+)([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> Optional[int]:
    """'10m', '2h', '1d', '1h30m' -> secondes. '0'/'off' -> 0. Nombre seul = minutes."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    if text in {"0", "off"}:
        return 0
    if text.isdigit():
        return int(text) * 60
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        return None
    return sum(int(n) * _UNITS[u] for n, u in parts)


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0 s"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    chunks = []
    if days:
        chunks.append(f"{days} d")
    if hours:
        chunks.append(f"{hours} h")
    if minutes:
        chunks.append(f"{minutes} min")
    if secs and not days:
        chunks.append(f"{secs} s")
    return " ".join(chunks)


def parse_color(text: Optional[str], default: int = config.EMBED_COLOR) -> int:
    if not text:
        return default
    text = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return int(text, 16)
    return default


def slugify(text: str, max_len: int = 90) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:max_len].strip("-") or "ticket"


# --- Logs --------------------------------------------------------------------
async def send_log(bot, guild: discord.Guild, key: str, *, embed: discord.Embed,
                   file: Optional[discord.File] = None) -> bool:
    """Envoie un embed dans le salon de logs configuré (key: 'mod_log_channel_id' ou 'ticket_log_channel_id')."""
    cfg = await bot.db.get_config(guild.id)
    channel_id = cfg.get(key)
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        await channel.send(embed=embed, file=file) if file else await channel.send(embed=embed)
        return True
    except discord.HTTPException:
        return False


async def log_mod(bot, guild: discord.Guild, action: str, target, moderator, reason: str, *,
                  extra: Optional[str] = None, color: int = config.WARNING_COLOR) -> None:
    embed = make_embed(f"🛡️ {action}", color=color)
    embed.add_field(name="Member", value=f"{target.mention}\n`{target.id}`")
    embed.add_field(name="Moderator", value=f"{moderator.mention}\n`{moderator.id}`")
    if extra:
        embed.add_field(name="Details", value=extra, inline=False)
    embed.add_field(name="Reason", value=reason[:1000], inline=False)
    await send_log(bot, guild, "mod_log_channel_id", embed=embed)


# --- Installation automatique -----------------------------------------------
def _private_overwrites(guild: discord.Guild, staff_role: discord.Role) -> dict:
    return {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, attach_files=True),
    }


async def _ensure_log_channel(guild, channel_id, name, staff_role, created) -> discord.TextChannel:
    existing = guild.get_channel(channel_id) if channel_id else None
    if isinstance(existing, discord.TextChannel):
        return existing
    channel = await guild.create_text_channel(
        name, overwrites=_private_overwrites(guild, staff_role), reason="Automatic setup")
    created.append(f"Channel {channel.mention}")
    return channel


async def ensure_infrastructure(bot, guild: discord.Guild, *, staff_role: Optional[discord.Role] = None,
                                category: Optional[discord.CategoryChannel] = None) -> dict:
    """Crée (si absents) rôle staff, catégorie des tickets et salons de logs, puis enregistre la config."""
    cfg = await bot.db.get_config(guild.id)
    created: list[str] = []

    role = staff_role
    if role is None and cfg.get("staff_role_id"):
        role = guild.get_role(cfg["staff_role_id"])
    if role is None:
        role = await guild.create_role(
            name="Staff", colour=discord.Colour(config.EMBED_COLOR), mentionable=True,
            reason="Automatic setup")
        created.append(f"Role {role.mention}")

    if category is None and cfg.get("ticket_category_id"):
        existing = guild.get_channel(cfg["ticket_category_id"])
        if isinstance(existing, discord.CategoryChannel):
            category = existing
    if category is None:
        category = await guild.create_category(
            "🎫 Tickets", overwrites=_private_overwrites(guild, role), reason="Automatic setup")
        created.append(f"Category **{category.name}**")

    ticket_log = await _ensure_log_channel(
        guild, cfg.get("ticket_log_channel_id"), "ticket-logs", role, created)
    mod_log = await _ensure_log_channel(
        guild, cfg.get("mod_log_channel_id"), "mod-logs", role, created)

    await bot.db.set_config(
        guild.id, staff_role_id=role.id, ticket_category_id=category.id,
        ticket_log_channel_id=ticket_log.id, mod_log_channel_id=mod_log.id)
    return {"staff_role": role, "category": category, "ticket_log": ticket_log,
            "mod_log": mod_log, "created": created}
