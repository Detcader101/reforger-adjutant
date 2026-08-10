"""/adjutant — the discoverability surface.

One ephemeral panel routing into the views/panels that otherwise only
surface through their own top-level command: Config (config.py's panel
view), Server Link (server.py's status panel), and Incidents reach other
cogs purely through `bot.get_cog(...)` + `type(cog).command.callback(cog,
interaction)` — the same pattern the test suite uses to call a command's
body directly — so this module needs zero module-level cross-cog imports,
sidestepping any circular-import risk.

Setup, Ranks, and Diagnostics are the one exception: they import straight
from adjutant/cogs/setup.py (SetupView, RankLadderView, _preflight_embed),
lazily inside each button callback rather than at module scope, so the
import only happens when the button is actually clicked and the hub can
still load even if setup.py can't. adjutant/cogs/admin.py's setup-related
fallbacks (preflight/ranks/setup-quick/teardown) follow the same lazy-
import pattern for the same reason.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import view_util, voice
from .roles import decline_admin_only, is_guild_admin

log = logging.getLogger(__name__)

_INTRO = (
    "At your service. Quick commands you'll use most: `/team`, `/event`, `/map`, `/rank`, "
    "`/server`, and `/setup` — each shows what's going on when run bare, and acts when you give "
    "it arguments. Configuration, setup, ranks, diagnostics, the server link, and the incident "
    "log all live behind the buttons below too."
)


class HubView(view_util.ErrorHandledView):
    """Locked to whoever ran /adjutant. Every button re-checks its own
    gating at click time rather than trusting that the panel's invoker is
    still who's clicking — same rule as every other view in the bot."""

    def __init__(self, bot: commands.Bot, invoker_id: int, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                voice.decline("This panel belongs to whoever ran `/adjutant`."), ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _missing_cog(self, interaction: discord.Interaction, feature: str) -> None:
        await interaction.response.send_message(
            voice.broken(f"{feature} isn't loaded right now.", "Try again shortly, or flag an admin."),
            ephemeral=True,
        )

    @discord.ui.button(label="Config", style=discord.ButtonStyle.primary, row=0)
    async def config_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="adjutant hub: config")
            return
        cog = self.bot.get_cog("ConfigCog")
        if cog is None:
            await self._missing_cog(interaction, "Config")
            return
        await cog.panel(interaction)

    @discord.ui.button(label="Server Link", style=discord.ButtonStyle.primary, row=0)
    async def server_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self.bot.get_cog("ServerLinkCog")
        if cog is None:
            await self._missing_cog(interaction, "Server link")
            return
        await type(cog).server.callback(cog, interaction)

    @discord.ui.button(label="Incidents", style=discord.ButtonStyle.secondary, row=0)
    async def incidents_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog = self.bot.get_cog("AdminCog")
        if cog is None:
            await self._missing_cog(interaction, "Incidents")
            return
        await type(cog).incidents.callback(cog, interaction)

    @discord.ui.button(label="Setup", style=discord.ButtonStyle.secondary, row=1)
    async def setup_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="adjutant hub: setup")
            return
        # Imported here rather than at module scope: hub.py deliberately owns
        # no cog imports, so the hub can load even if a feature cog can't.
        from .setup import SetupView

        view = SetupView(self.bot, guild, member.id)
        await interaction.response.send_message(
            embed=voice.embed(
                "Setup",
                "Pick a template and the features you want, then Finish. "
                "Everything here can be changed later from Config.",
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Ranks", style=discord.ButtonStyle.secondary, row=1)
    async def ranks_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="adjutant hub: ranks")
            return
        from .setup import RankLadderView, ladder_embed

        view = RankLadderView(self.bot, guild, member.id)
        await interaction.response.send_message(
            embed=await ladder_embed(self.bot, guild), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Diagnostics", style=discord.ButtonStyle.secondary, row=1)
    async def diagnostics_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(member, discord.Member) or not is_guild_admin(member):
            await decline_admin_only(interaction, detail="adjutant hub: diagnostics")
            return
        from .admin import minimal_mode
        from .setup import _preflight_embed

        assert self.bot.db is not None
        embed = _preflight_embed(guild, minimal=await minimal_mode(self.bot.db, guild.id))
        await interaction.response.send_message(embed=embed, ephemeral=True)


class HubCog(commands.Cog, name="HubCog"):
    """/adjutant — the discoverability surface described in this module's
    docstring."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="adjutant", description="Open the adjutant's control panel.")
    async def adjutant(self, interaction: discord.Interaction) -> None:
        embed = voice.embed("Adjutant", _INTRO)
        view = HubView(self.bot, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return
        await view_util.handle_app_command_error(interaction, error, log)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HubCog(bot))
