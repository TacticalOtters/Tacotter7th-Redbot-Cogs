from typing import Optional

import discord
from redbot.core import commands


class VCOwnerCommandsMixin:
    """
    Voice Channel Owner Commands mixin.

    Adds hybrid commands for users who OWN a temporary voice channel:
    - /voicechannelhandling transfer <user>
    - /voicechannelhandling limit <number>
    - /voicechannelhandling invite [public]

    Requirements on the main cog class:
      - Methods:
        - get_temp_channels(guild) -> List[int]
      - Config entries:
        - self.config.guild(guild).owner_channels()
      - Helper methods:
        - _set_owner_channel(guild, user_id, channel_id)
        - _clear_owner_by_channel(guild, channel_id)
    """

    # ----------------------------------------------------------
    # Group: /voicechannelhandling
    # ----------------------------------------------------------

    @commands.hybrid_group(
        name="voicechannelhandling",
        invoke_without_command=True,
    )
    @commands.guild_only()
    async def vch_owner_group(self, ctx: commands.Context):
        """
        Voice channel owner command group.

        Use subcommands:
        - /voicechannelhandling transfer <user>
        - /voicechannelhandling limit <number>
        - /voicechannelhandling invite [public]
        """
        await ctx.send(
            "Available owner commands:\n"
            "- `/voicechannelhandling transfer <user>`\n"
            "- `/voicechannelhandling limit <number>`\n"
            "- `/voicechannelhandling invite [public]`",
            ephemeral=True,
        )

    # ----------------------------------------------------------
    # /voicechannelhandling transfer
    # ----------------------------------------------------------

    @vch_owner_group.command(name="transfer")
    @commands.guild_only()
    async def vch_owner_transfer(
        self,
        ctx: commands.Context,
        new_owner: discord.Member,
    ):
        """
        Transfer ownership of the current temporary voice channel
        to another user in the same channel.
        """
        channel = await self._get_owned_temp_channel_for_ctx(ctx)
        if channel is None:
            return

        guild = ctx.guild
        assert guild is not None

        if new_owner.bot:
            await ctx.send(
                "You cannot transfer ownership to a bot.",
                ephemeral=True,
            )
            return

        if not new_owner.voice or new_owner.voice.channel != channel:
            await ctx.send(
                "The target user must be in the same voice channel as you.",
                ephemeral=True,
            )
            return

        # Update permission overwrites
        overwrites = channel.overwrites.copy()
        if ctx.author in overwrites:
            del overwrites[ctx.author]

        overwrites[new_owner] = discord.PermissionOverwrite(
            manage_channels=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )

        name_template = await self.get_name_template(guild)
        counter = await self.get_next_counter(guild)
        new_name = self._render_channel_name(name_template, new_owner, counter)

        try:
            await channel.edit(
                name=new_name,
                overwrites=overwrites,
                reason="Temporary voice channel ownership transfer.",
            )
        except discord.HTTPException:
            await ctx.send(
                "Failed to update channel permissions or rename the channel.",
                ephemeral=True,
            )
            return

        # Update mapping
        await self._clear_owner_by_channel(guild, channel.id)
        await self._set_owner_channel(guild, new_owner.id, channel.id)

        await ctx.send(
            f"Ownership of this temporary voice channel has been transferred to {new_owner.mention}.",
            ephemeral=True,
        )

    # ----------------------------------------------------------
    # /voicechannelhandling limit
    # ----------------------------------------------------------

    @vch_owner_group.command(name="limit")
    @commands.guild_only()
    async def vch_owner_limit(
        self,
        ctx: commands.Context,
        limit: int,
    ):
        """
        Limit the number of users allowed in your temporary voice channel.

        - 0 means no limit.
        - Values are clamped to a maximum of 99.
        """
        channel = await self._get_owned_temp_channel_for_ctx(ctx)
        if channel is None:
            return

        if limit < 0:
            limit = 0
        if limit > 99:
            limit = 99

        try:
            await channel.edit(
                user_limit=limit,
                reason="Temporary voice channel user limit changed by owner.",
            )
        except discord.HTTPException:
            await ctx.send(
                "Failed to change the user limit for this channel.",
                ephemeral=True,
            )
            return

        limit_text = "unlimited" if limit == 0 else str(limit)
        await ctx.send(
            f"User limit for this channel is now set to {limit_text}.",
            ephemeral=True,
        )

    # ----------------------------------------------------------
    # /voicechannelhandling invite
    # ----------------------------------------------------------

    @vch_owner_group.command(name="invite")
    @commands.guild_only()
    async def vch_owner_invite(
        self,
        ctx: commands.Context,
        public: Optional[bool] = False,
    ):
        """
        Generate a 30-minute invite for your temporary voice channel.

        Default behavior:
        - Invite link is DM'ed to you.
        - This command's response is ephemeral.

        If `public` is true:
        - The bot will post the invite link publicly in the channel
          where the command is used.
        """
        channel = await self._get_owned_temp_channel_for_ctx(ctx)
        if channel is None:
            return

        # Create a 30-minute invite (1800 seconds)
        try:
            invite = await channel.create_invite(
                max_age=1800,
                max_uses=0,
                unique=True,
                reason="Temporary voice channel invite requested by owner.",
            )
        except discord.HTTPException:
            await ctx.send(
                "Failed to create an invite for this channel.",
                ephemeral=True,
            )
            return

        if public:
            # Public message in the current text channel.
            await ctx.send(
                f"Invite link for this temporary voice channel (valid 30 minutes): {invite.url}"
            )
        else:
            # DM + ephemeral confirmation.
            try:
                await ctx.author.send(
                    f"Here is your temporary voice channel invite (valid 30 minutes): {invite.url}"
                )
                await ctx.send(
                    "I have sent you a DM with an invite link for this channel.",
                    ephemeral=True,
                )
            except discord.Forbidden:
                await ctx.send(
                    "I could not DM you the invite link. Please check your privacy settings.",
                    ephemeral=True,
                )

    # ----------------------------------------------------------
    # Internal helper: resolve owned temp VC
    # ----------------------------------------------------------

    async def _get_owned_temp_channel_for_ctx(
        self,
        ctx: commands.Context,
    ) -> Optional[discord.VoiceChannel]:
        """
        Resolve and validate the temporary voice channel owned by the invoking user.

        Conditions:
        - Caller must be in a voice channel.
        - The channel must be tracked as a temp channel.
        - The caller must be the recorded owner of that temp channel.
        """
        guild = ctx.guild
        if guild is None:
            return None

        author = ctx.author
        if not isinstance(author, discord.Member):
            return None

        if not author.voice or not isinstance(author.voice.channel, discord.VoiceChannel):
            await ctx.send(
                "You must be connected to a temporary voice channel to use this command.",
                ephemeral=True,
            )
            return None

        channel = author.voice.channel

        # Check this is a managed temp channel
        temp_channels = await self.get_temp_channels(guild)
        if channel.id not in temp_channels:
            await ctx.send(
                "This command can only be used inside a managed temporary voice channel.",
                ephemeral=True,
            )
            return None

        # Verify ownership from config
        owners = await self.config.guild(guild).owner_channels()
        owner_id: Optional[int] = None
        for uid_str, cid in owners.items():
            if cid == channel.id:
                try:
                    owner_id = int(uid_str)
                except ValueError:
                    continue
                break

        if owner_id != author.id:
            await ctx.send(
                "You are not the owner of this temporary voice channel.",
                ephemeral=True,
            )
            return None

        return channel
