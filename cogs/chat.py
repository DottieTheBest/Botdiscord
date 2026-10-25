import unicodedata
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import respond

# --- Tailles : titres Markdown de Discord ---------------------------------------
# clé: (libellé affiché, préfixe de ligne)
SIZES = {
    "tiny": ("Tiny", "-# "),
    "normal": ("Normal", ""),
    "small": ("Small", "### "),
    "medium": ("Medium", "## "),
    "large": ("Large", "# "),
}

# --- Polices : Markdown ou lettres Unicode -------------------------------------
# Un vrai message Discord ne peut pas utiliser Arial ou Comic Sans : on remplace les lettres
# par leurs équivalents « stylisés » Unicode, qui s'affichent partout et se copient comme du texte.
MARKDOWN_STYLES = {
    "bold": ("Bold", "**{}**"),
    "italic": ("Italic", "*{}*"),
    "bold_italic": ("Bold italic", "***{}***"),
    "underline": ("Underline", "__{}__"),
    "strikethrough": ("Strikethrough", "~~{}~~"),
    "code": ("Code", "`{}`"),
}

_DOUBLE_STRUCK_EXCEPTIONS = {"C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "Z": "ℤ"}


def _shift(text: str, upper: int, lower: int, digit: Optional[int] = None,
           exceptions: Optional[dict] = None) -> str:
    out = []
    for char in unicodedata.normalize("NFD", text):  # é -> e + accent : l'accent est conservé
        if exceptions and char in exceptions:
            out.append(exceptions[char])
        elif "A" <= char <= "Z":
            out.append(chr(upper + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            out.append(chr(lower + ord(char) - ord("a")))
        elif digit is not None and "0" <= char <= "9":
            out.append(chr(digit + ord(char) - ord("0")))
        else:
            out.append(char)
    return unicodedata.normalize("NFC", "".join(out))


UNICODE_STYLES = {
    "cursive": ("Cursive (𝓢𝓬𝓻𝓲𝓹𝓽)", lambda t: _shift(t, 0x1D4D0, 0x1D4EA)),
    "gothic": ("Gothic (𝕲𝖔𝖙𝖍𝖎𝖈)", lambda t: _shift(t, 0x1D56C, 0x1D586)),
    "double_struck": ("Double-struck (𝔻𝕠𝕦𝕓𝕝𝕖)", lambda t: _shift(t, 0x1D538, 0x1D552, 0x1D7D8, _DOUBLE_STRUCK_EXCEPTIONS)),
    "typewriter": ("Typewriter (𝚃𝚢𝚙𝚎)", lambda t: _shift(t, 0x1D670, 0x1D68A, 0x1D7F6)),
    "sans_bold": ("Sans-serif bold (𝗕𝗼𝗹𝗱)", lambda t: _shift(t, 0x1D5D4, 0x1D5EE, 0x1D7EC)),
}

FONT_CHOICES = (
    [("normal", "Normal")]
    + [(key, label) for key, (label, _) in MARKDOWN_STYLES.items()]
    + [(key, label) for key, (label, _) in UNICODE_STYLES.items()]
)

CHAT_MAX_CHARS = 900
DISCORD_MESSAGE_LIMIT = 2000


def format_text(text: str, size: str = "normal", font: str = "normal") -> str:
    """Applique le style (police) puis la taille, ligne par ligne."""
    prefix = SIZES.get(size, SIZES["normal"])[1]
    lines = []
    for line in text.replace("\\n", "\n").split("\n"):
        line = line.strip()
        if line:
            if font in UNICODE_STYLES:
                line = UNICODE_STYLES[font][1](line)
            elif font in MARKDOWN_STYLES:
                line = MARKDOWN_STYLES[font][1].format(line)
            line = prefix + line
        lines.append(line)
    return "\n".join(lines).strip()


def discord_length(text: str) -> int:
    """Longueur comptée en unités UTF-16 (estimation prudente de ce que Discord limite à 2000)."""
    return len(text.encode("utf-16-le")) // 2


class Chat(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="chat", description="The bot sends your text with the chosen size and style")
    @app_commands.describe(text="Text to write (use \\n for a line break)",
                           size="Text size", font="Writing style")
    @app_commands.choices(
        size=[app_commands.Choice(name=label, value=key) for key, (label, _) in SIZES.items()],
        font=[app_commands.Choice(name=label, value=key) for key, label in FONT_CHOICES])
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 5.0)
    async def chat_cmd(self, interaction: discord.Interaction,
                       text: app_commands.Range[str, 1, CHAT_MAX_CHARS],
                       size: Optional[app_commands.Choice[str]] = None,
                       font: Optional[app_commands.Choice[str]] = None) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable) \
                or not channel.permissions_for(interaction.guild.me).send_messages:
            await respond(interaction, "❌ I can't send messages in this channel.")
            return

        message = format_text(text, size.value if size else "normal", font.value if font else "normal")
        if not message:
            await respond(interaction, "❌ The text is empty.")
            return
        if discord_length(message) > DISCORD_MESSAGE_LIMIT:
            await respond(interaction, "❌ The text is too long once styled. Please shorten it.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await interaction.followup.send("❌ Unable to send the message in this channel.", ephemeral=True)
            return
        await interaction.followup.send("✅ Message sent.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Chat(bot))
