"""Discord front-end — a gateway bot exposing a read-only /ask command.

Gateway (outbound WebSocket) so no public endpoint is needed: the bot dials out
to Discord and receives /ask interactions over the socket, while reaching the
local stack directly. Run as the long-lived OrbStack container (see compose).
"""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from . import agent, config

intents = discord.Intents.default()  # no privileged intents — slash commands only


class AskBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Guild-scoped sync is instant; global sync can take ~an hour to propagate.
        if config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(config.DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


client = AskBot()


@client.tree.command(name="ask", description="Ask about the observability stack (read-only)")
@app_commands.describe(question="What do you want to know?")
async def ask(interaction: discord.Interaction, question: str):
    if config.ALLOWED_USER_IDS and str(interaction.user.id) not in config.ALLOWED_USER_IDS:
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    # The agent loop takes >3s, so defer immediately, then follow up.
    await interaction.response.defer(thinking=True)
    try:
        answer = await asyncio.to_thread(agent.ask, question)
    except Exception as e:  # never leave the interaction hanging
        answer = f"Error: {type(e).__name__}: {e}"
    # Discord hard-caps messages at 2000 chars.
    await interaction.followup.send(answer[:1900] or "(no answer)")


def main():
    if not config.DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set (see .env).")
    client.run(config.DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
