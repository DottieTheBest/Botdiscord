import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database
from utils import NotStaff, respond

log = logging.getLogger("bot")

COGS = (
    "cogs.tickets",
    "cogs.moderation",
    "cogs.server",
    "cogs.chat",
    "cogs.giveaways",
)


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # privilégié : /userinfo, /serverinfo, rôles
        intents.message_content = True  # privilégié : transcriptions, anti-spam
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.db = Database(config.DB_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.tree.on_error = self.on_tree_error
        for cog in COGS:
            await self.load_extension(cog)

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=int(config.DEV_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("%d commands synced to the test server.", len(synced))
        else:
            synced = await self.tree.sync()
            log.info("%d commands synced globally.", len(synced))

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s servers)", self.user, len(self.guilds))

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            original = error.original
        else:
            original = error

        if isinstance(error, NotStaff):
            message = "🚫 This command is restricted to staff."
        elif isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            message = f"🚫 You are missing the permission: {perms}."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(f"`{p}`" for p in error.missing_permissions)
            message = f"🚫 I am missing the permission: {perms}."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "🚫 This command only works in a server."
        elif isinstance(original, discord.Forbidden):
            message = "🚫 Discord refused this action: check my permissions and my role's position."
        else:
            log.exception("Error in command %s", interaction.command, exc_info=error)
            message = "❌ An unexpected error occurred."

        try:
            await respond(interaction, message)
        except discord.HTTPException:
            pass


def main() -> None:
    if not config.TOKEN:
        sys.exit("DISCORD_TOKEN is missing: copy .env.example to .env and set your token.")
    Bot().run(config.TOKEN)


if __name__ == "__main__":
    main()
