"""/adjutant — the discoverability surface.

One ephemeral panel routing into the views/panels that otherwise only
surface through their own top-level command: Config (config.py's panel
view), Server Link (server.py's status panel), and Incidents. Setup/Ranks/
Diagnostics are placeholders pending a follow-up pass that wires them to
adjutant/cogs/setup.py — this cog deliberately never imports setup.py (see
the TODO on each placeholder button), so it reaches every other cog purely
through `bot.get_cog(...)` + `type(cog).command.callback(cog, interaction)`,
the same pattern the test suite uses to call a command's body directly.
That also means this file needs zero cross-cog imports at all, sidestepping
any circular-import risk with the cogs it routes to.
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
    "`/server` — each shows what's going on when run bare, and acts when you give it arguments. "
    "Configuration, the server link, and the incident log live behind the buttons below; setup "
    "and diagnostics are on their way there too."
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
        # TODO(team-lead): wire to setup.py's wizard entry point (the
        # /setup command's callback, or whatever it's refactored into) —
        # this cog stays out of setup.py while that file is being edited
        # elsewhere, per the current work split.
        await interaction.response.send_message(
            "Setup isn't wired to a button yet — run `/setup` for now.", ephemeral=True
        )

    @discord.ui.button(label="Ranks", style=discord.ButtonStyle.secondary, row=1)
    async def ranks_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # TODO(team-lead): wire to setup.py's rank-ladder editor. The
        # underlying logic already exists and is ready to call —
        # RolesCog.ladder_add / RolesCog.ladder_remove in cogs/roles.py —
        # they just lost their slash-command wrapper in this pass and are
        # waiting for a button/modal (here, or in setup.py) to drive them.
        await interaction.response.send_message(
            "Ladder editing isn't wired to a button yet — an admin can use `/rank` to view the current ladder.",
            ephemeral=True,
        )

    @discord.ui.button(label="Diagnostics", style=discord.ButtonStyle.secondary, row=1)
    async def diagnostics_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # TODO(team-lead): wire to setup.py's preflight/diagnostics check
        # (or adjutant/selftest.py's harness, if that's the intended
        # target instead).
        await interaction.response.send_message("Diagnostics isn't wired to a button yet.", ephemeral=True)


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
