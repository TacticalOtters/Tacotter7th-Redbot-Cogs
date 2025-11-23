import asyncio
import json
from pathlib import Path
from typing import Dict, Optional, List

# ==========================================================
# Discord and red bot imports
# ==========================================================

import discord
from redbot.core import commands, Config
from redbot.core.data_manager import cog_data_path

# ==========================================================
# Module imports
# ==========================================================

from .VCC.commands_mixin import VCCCommandsMixin
from .VCC.VCOwnerCommand import VCOwnerCommandsMixin


class VoiceChannelHandling(VCCCommandsMixin, VCOwnerCommandsMixin, commands.Cog):
    """Temporary voice channel handling cog.

    Responsibilities:
    - Listen to voice state updates.
    - Create a temporary voice channel when a user joins the configured
      'creation lobby' voice channel.
    - Copy base permissions from the creation lobby and grant the joining user
      management permissions for their temp channel.
    - Track temporary channels per guild.
    - Delete a temporary channel if it is empty and stays empty for a delay.
    - Provide a JSON-based per-guild configuration file.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Data path for JSON "DB" files, one per guild: <guild_id>.json
        self._data_path: Path = cog_data_path(self)
        self._data_path.mkdir(parents=True, exist_ok=True)

        # Config: Red will store this as JSON per guild.
        self.config = Config.get_conf(
            self,
            identifier=0x5643485F4D41494E_01,  # random unique int for this cog
            force_registration=True,
        )

        default_guild = {
            # ID of the "creation lobby" voice channel.
            "creation_channel_id": None,
            # Template for new channel name: use {user} and/or {id}.
            "name_template": "{user}'s channel",
            # Deletion delay for empty temp channels (seconds).
            "delete_delay": 10,
            # Category ID where temp channels will be created (optional).
            "temp_category_id": None,
            # List of channel IDs considered temporary for this guild.
            "temp_channels": [],
            # Next counter value for {counter} placeholder.
            "counter": 1,
            "owner_channels": {}, # key: str(user_id), value: channel_id
        }

        self.config.register_guild(**default_guild)

        # In-memory tracking of scheduled deletion tasks.
        # Key: channel_id, Value: asyncio.Task
        self._delete_tasks: Dict[int, asyncio.Task] = {}

    # ==========================================================
    # JSON DB helpers
    # ==========================================================

    def _guild_db_path(self, guild_id: int) -> Path:
        """Return the path to the JSON DB file for a guild."""
        return self._data_path / f"{guild_id}.json"

    def _write_guild_json(self, guild_id: int, data: dict) -> None:
        """Write the JSON DB file for a guild."""
        path = self._guild_db_path(guild_id)
        with path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

    def _read_guild_json(self, guild_id: int) -> Optional[dict]:
        """Read the JSON DB file for a guild, if it exists."""
        path = self._guild_db_path(guild_id)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return None

    # ==========================================================
    # Public helper API for command modules (VCC) to use later
    # ==========================================================

    async def set_creation_channel(self, guild: discord.Guild, channel_id: Optional[int]) -> None:
        """Set or clear the creation lobby voice channel for this guild."""
        await self.config.guild(guild).creation_channel_id.set(channel_id)

    async def get_creation_channel_id(self, guild: discord.Guild) -> Optional[int]:
        """Get the configured creation lobby voice channel ID for this guild."""
        return await self.config.guild(guild).creation_channel_id()

    async def set_name_template(self, guild: discord.Guild, template: str) -> None:
        """Set the channel name template for this guild."""
        await self.config.guild(guild).name_template.set(template)

    async def get_name_template(self, guild: discord.Guild) -> str:
        """Get the channel name template for this guild."""
        return await self.config.guild(guild).name_template()

    async def set_delete_delay(self, guild: discord.Guild, delay: int) -> None:
        """Set delete delay (seconds) for empty temp channels."""
        await self.config.guild(guild).delete_delay.set(delay)

    async def get_delete_delay(self, guild: discord.Guild) -> int:
        """Get delete delay (seconds) for empty temp channels."""
        return await self.config.guild(guild).delete_delay()

    async def set_temp_category(self, guild: discord.Guild, category_id: Optional[int]) -> None:
        """Set the category ID where temp channels will be created."""
        await self.config.guild(guild).temp_category_id.set(category_id)

    async def get_temp_category(self, guild: discord.Guild) -> Optional[int]:
        """Get the category ID where temp channels will be created."""
        return await self.config.guild(guild).temp_category_id()

    async def get_temp_channels(self, guild: discord.Guild) -> List[int]:
        """Get the list of tracked temp channel IDs for this guild."""
        return await self.config.guild(guild).temp_channels()
    
    async def get_next_counter(self, guild: discord.Guild) -> int:
        """Get the next counter value for this guild and increment it."""
        conf = self.config.guild(guild)
        current = await conf.counter()
        if current is None or current < 1:
            current = 1
        await conf.counter.set(current + 1)
        return current

    # ==========================================================
    # Hybrid command: SetupVCH (creates JSON DB + config)
    # ==========================================================

    @commands.hybrid_command(name="setupvch")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def setupvch(
        self,
        ctx: commands.Context,
        creator_room: discord.VoiceChannel,
        delete_delay: int,
        category: Optional[discord.CategoryChannel] = None,
        *,
        name_template: str = "{user}'s channel",
    ):
        """Initial setup for VoiceChannelHandling.

        This will:
        - Store settings in Red's config.
        - Create a JSON DB file named <guild_id>.json with:
          - voicecreateroom_id
          - name_template
          - delete_delay
          - temp_category_id
        """

        guild = ctx.guild
        if guild is None:
            return

        # Basic sanity for delete delay.
        if delete_delay < 3:
            delete_delay = 3

        # Choose temp category: explicit argument or creator room's category.
        if category is not None:
            target_category_id = category.id
        else:
            target_category_id = creator_room.category_id

        # Save to Red Config.
        await self.set_creation_channel(guild, creator_room.id)
        await self.set_name_template(guild, name_template)
        await self.set_delete_delay(guild, delete_delay)
        await self.set_temp_category(guild, target_category_id)

        # Build JSON payload.
        db_data = {
            "guild_id": guild.id,
            "voicecreateroom_id": creator_room.id,
            "name_template": name_template,
            "delete_delay": delete_delay,
            "temp_category_id": target_category_id,
        }

        # Write JSON DB file: <data folder>/<guild_id>.json
        self._write_guild_json(guild.id, db_data)

        # Feedback message
        temp_cat_text = "None"
        if target_category_id is not None:
            cat_obj = guild.get_channel(target_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                temp_cat_text = cat_obj.name

        await ctx.send(
            "VoiceChannelHandling configured for this server.\n"
            f"- Creation room: {creator_room.mention}\n"
            f"- Name template: `{name_template}`\n"
            f"- Delete delay: {delete_delay} seconds\n"
            f"- Temp category: {temp_cat_text}\n"
            f"- JSON DB file: `{guild.id}.json`"
        )

    # ==========================================================
    # Voice state handling
    # ==========================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        """Main listener for handling temp channel lifecycle."""

        # Ignore bots by default; change this if you want them to trigger channels.
        if member.bot:
            return

        guild = member.guild
        if guild is None:
            return

        guild_conf = self.config.guild(guild)
        creation_channel_id = await guild_conf.creation_channel_id()
        if creation_channel_id is None:
            # Cog is not configured for this guild.
            return

        temp_channels = await guild_conf.temp_channels()
        temp_channels_set = set(temp_channels)

        # 1) Handle leaving a temp channel: schedule deletion if empty.
        if before.channel and before.channel.id in temp_channels_set:
            if len(before.channel.members) == 0:
                await self._schedule_delete_temp_channel(before.channel)

        # 2) Handle joining an existing temp channel: cancel deletion.
        if after.channel and after.channel.id in temp_channels_set:
            self._cancel_delete_task(after.channel.id)

        # 3) Handle joining the creation lobby: create a new temp channel.
        if after.channel and after.channel.id == creation_channel_id:
            await self._handle_creation_join(member, after.channel)

    # ==========================================================
    # Temp channel lifecycle helpers
    # ==========================================================

    async def _handle_creation_join(
        self,
        member: discord.Member,
        base_channel: discord.VoiceChannel,
    ) -> None:
        """Create or reuse a temp channel for `member` and move them into it."""
        guild = base_channel.guild
        guild_conf = self.config.guild(guild)

        # 1) Check if this user already has a temp channel
        existing_id = await self._get_owner_channel_id(guild, member.id)
        if existing_id is not None:
            existing_channel = guild.get_channel(existing_id)
            if isinstance(existing_channel, discord.VoiceChannel):
                # Cancel pending delete for that channel, if any
                self._cancel_delete_task(existing_channel.id)

                # Just move them back into their existing channel
                try:
                    await member.move_to(existing_channel)
                except discord.HTTPException:
                    pass
                return
            # If mapping was stale, we cleaned it in _get_owner_channel_id

        # 2) No existing channel: create a new one
        name_template = await guild_conf.name_template()
        counter = await self.get_next_counter(guild)
        channel_name = self._render_channel_name(name_template, member, counter)

        overwrites = base_channel.overwrites.copy()
        overwrites[member] = discord.PermissionOverwrite(
            manage_channels=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )

        me = guild.me
        if not me.guild_permissions.manage_channels or not me.guild_permissions.move_members:
            return

        temp_category_id = await guild_conf.temp_category_id()
        category = None
        if temp_category_id is not None:
            cat_obj = guild.get_channel(temp_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                category = cat_obj
        if category is None:
            category = base_channel.category

        new_channel = await guild.create_voice_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Temporary voice channel created by VoiceChannelHandling cog.",
        )

        await self._add_temp_channel(guild, new_channel.id)
        await self._set_owner_channel(guild, member.id, new_channel.id)

        try:
            await member.move_to(new_channel)
        except discord.HTTPException:
            pass

    def _render_channel_name(self, template: str, member: discord.Member, counter: int) -> str:
        """Render channel name, falling back to a safe default if needed.

        Available placeholders:
        - {user}: member.display_name
        - {id}: member.id
        - {tag}: str(member) (usually Name#1234)
        - {counter}: auto-incrementing integer per guild
        """
        tag = str(member)
        try:
            return template.format(
                user=member.display_name,
                id=member.id,
                tag=tag,
                counter=counter,
            )
        except Exception:
            return f"{member.display_name}'s channel"

    async def _schedule_delete_temp_channel(self, channel: discord.VoiceChannel) -> None:
        """Schedule deletion of a temp channel if it remains empty."""
        if channel.id in self._delete_tasks:
            # Already scheduled.
            return

        # Only schedule if it is actually empty at the time of scheduling.
        if len(channel.members) != 0:
            return

        task = asyncio.create_task(self._delete_temp_channel_after_delay(channel))
        self._delete_tasks[channel.id] = task

    async def _delete_temp_channel_after_delay(self, channel: discord.VoiceChannel) -> None:
        """Wait a configured delay, then delete the channel if still empty."""
        guild = channel.guild
        if guild is None:
            return

        delay = await self.get_delete_delay(guild)

        try:
            await asyncio.sleep(delay)

            # Channel might already be gone.
            guild = channel.guild
            if guild is None:
                return

            # Re-check if channel is still empty.
            if len(channel.members) == 0:
                channel_id = channel.id

                try:
                    await channel.delete(reason="Temporary voice channel empty.")
                except discord.NotFound:
                    # Already deleted.
                    pass

                # Remove from config list.
                await self._remove_temp_channel(guild, channel_id)
                await self._clear_owner_by_channel(guild, channel_id)

        finally:
            # Clean up task reference.
            self._delete_tasks.pop(getattr(channel, "id", None), None)

    def _cancel_delete_task(self, channel_id: int) -> None:
        """Cancel a pending scheduled deletion for the given channel."""
        task = self._delete_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()

    # ==========================================================
    # Config helpers for temp channel list
    # ==========================================================

    async def _add_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        """Add a channel to the guild's temp channel tracking list."""
        async with self.config.guild(guild).temp_channels() as channels:
            if channel_id not in channels:
                channels.append(channel_id)

    async def _remove_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        """Remove a channel from the guild's temp channel tracking list."""
        async with self.config.guild(guild).temp_channels() as channels:
            if channel_id in channels:
                channels.remove(channel_id)
    
    async def _set_owner_channel(self, guild: discord.Guild, user_id: int, channel_id: int) -> None:
        """Set or update the temp channel owned by a specific user."""
        async with self.config.guild(guild).owner_channels() as owners:
            owners[str(user_id)] = channel_id

    async def _get_owner_channel_id(self, guild: discord.Guild, user_id: int) -> Optional[int]:
        """Get the temp channel ID owned by this user, if any and still valid."""
        owners = await self.config.guild(guild).owner_channels()
        chan_id = owners.get(str(user_id))
        if chan_id is None:
            return None

        channel = guild.get_channel(chan_id)
        if not isinstance(channel, discord.VoiceChannel):
            # Channel no longer exists, clean up mapping
            async with self.config.guild(guild).owner_channels() as owners_mut:
                owners_mut.pop(str(user_id), None)
            return None

        return chan_id

    async def _clear_owner_by_channel(self, guild: discord.Guild, channel_id: int) -> None:
        """Remove any owner entries pointing to a specific channel."""
        async with self.config.guild(guild).owner_channels() as owners:
            to_delete = [uid for uid, cid in owners.items() if cid == channel_id]
            for uid in to_delete:
                owners.pop(uid, None)
