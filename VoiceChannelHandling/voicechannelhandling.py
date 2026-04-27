import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.data_manager import cog_data_path

from .VCC.commands_mixin import VCCCommandsMixin
from .VCC.VCOwnerCommand import VCOwnerCommandsMixin


log = logging.getLogger("red.VoiceChannelHandling")


class VoiceChannelHandling(VCCCommandsMixin, VCOwnerCommandsMixin, commands.Cog):
    """Temporary voice channel handling cog.

    This cog:
    - Creates a temporary voice channel when a user joins the configured creator room.
    - Moves the user into their temp channel.
    - Reuses the user's existing temp channel if it still exists.
    - Deletes temp channels after a configurable delay when no human members remain.
    - Tracks state in Red Config and mirrors a per-guild JSON snapshot.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        self._data_path: Path = cog_data_path(self)
        self._data_path.mkdir(parents=True, exist_ok=True)

        self.config = Config.get_conf(
            self,
            identifier=0x5643485F4D41494E_01,
            force_registration=True,
        )

        default_guild = {
            "creation_channel_id": None,
            "name_template": "{user}'s channel",
            "delete_delay": 10,
            "temp_category_id": None,
            "temp_channels": [],
            "counter": 1,
            "owner_channels": {},
        }

        self.config.register_guild(**default_guild)

        self._delete_tasks: Dict[int, asyncio.Task] = {}
        self._guild_locks: Dict[int, asyncio.Lock] = {}

    def cog_unload(self):
        for task in self._delete_tasks.values():
            if not task.done():
                task.cancel()

        self._delete_tasks.clear()
        self._guild_locks.clear()

    # ==========================================================
    # Lock helpers
    # ==========================================================

    def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    # ==========================================================
    # JSON DB helpers
    # ==========================================================

    def _guild_db_path(self, guild_id: int) -> Path:
        return self._data_path / f"{guild_id}.json"

    def _write_guild_json(self, guild_id: int, data: dict) -> None:
        """Atomic JSON write so a crash does not leave a half-written file."""
        path = self._guild_db_path(guild_id)
        tmp_path = path.with_suffix(".json.tmp")

        with tmp_path.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)

        os.replace(tmp_path, path)

    def _read_guild_json(self, guild_id: int) -> Optional[dict]:
        path = self._guild_db_path(guild_id)
        if not path.is_file():
            return None

        try:
            with path.open("r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            log.exception("Failed to read VCH JSON DB for guild %s", guild_id)
            return None

    async def _write_guild_snapshot(self, guild: discord.Guild) -> None:
        conf = self.config.guild(guild)

        data = {
            "guild_id": guild.id,
            "voicecreateroom_id": await conf.creation_channel_id(),
            "creation_channel_id": await conf.creation_channel_id(),
            "name_template": await conf.name_template(),
            "delete_delay": await conf.delete_delay(),
            "temp_category_id": await conf.temp_category_id(),
            "temp_channels": await conf.temp_channels(),
            "owner_channels": await conf.owner_channels(),
            "counter": await conf.counter(),
        }

        self._write_guild_json(guild.id, data)

    # ==========================================================
    # Public helper API for VCC command modules
    # ==========================================================

    async def set_creation_channel(self, guild: discord.Guild, channel_id: Optional[int]) -> None:
        await self.config.guild(guild).creation_channel_id.set(channel_id)
        await self._write_guild_snapshot(guild)

    async def get_creation_channel_id(self, guild: discord.Guild) -> Optional[int]:
        return await self.config.guild(guild).creation_channel_id()

    async def set_name_template(self, guild: discord.Guild, template: str) -> None:
        await self.config.guild(guild).name_template.set(template)
        await self._write_guild_snapshot(guild)

    async def get_name_template(self, guild: discord.Guild) -> str:
        return await self.config.guild(guild).name_template()

    async def set_delete_delay(self, guild: discord.Guild, delay: int) -> None:
        delay = max(3, int(delay))
        await self.config.guild(guild).delete_delay.set(delay)
        await self._write_guild_snapshot(guild)

    async def get_delete_delay(self, guild: discord.Guild) -> int:
        delay = await self.config.guild(guild).delete_delay()
        return max(3, int(delay or 10))

    async def set_temp_category(self, guild: discord.Guild, category_id: Optional[int]) -> None:
        await self.config.guild(guild).temp_category_id.set(category_id)
        await self._write_guild_snapshot(guild)

    async def get_temp_category(self, guild: discord.Guild) -> Optional[int]:
        return await self.config.guild(guild).temp_category_id()

    async def get_temp_channels(self, guild: discord.Guild) -> List[int]:
        channels = await self.config.guild(guild).temp_channels()
        return list(dict.fromkeys(channels))

    async def _get_next_counter_unlocked(self, guild: discord.Guild) -> int:
        """Increment counter without taking the guild lock.

        Only call this when:
        - You are already inside the guild lock, OR
        - You do not need locking.
        """
        conf = self.config.guild(guild)
        current = await conf.counter()

        if current is None or current < 1:
            current = 1

        await conf.counter.set(current + 1)
        return current

    async def get_next_counter(self, guild: discord.Guild) -> int:
        """Get the next counter value safely.

        This public wrapper takes the guild lock. Do not call this from code that
        already holds the same guild lock, because asyncio.Lock is not re-entrant.
        """
        async with self._get_guild_lock(guild.id):
            return await self._get_next_counter_unlocked(guild)

    # ==========================================================
    # Setup command
    # ==========================================================

    @commands.hybrid_command(name="setupvch", description="Configure temporary voice channel handling.")
    @app_commands.guild_only()
    @app_commands.describe(
        creator_room="Voice channel users join to create a temp channel.",
        delete_delay="Seconds before empty temp channels are deleted.",
        category="Optional category where temp channels are created.",
        name_template="Template: {user}, {id}, {tag}, {counter}.",
    )
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def setupvch(
        self,
        ctx: commands.Context,
        creator_room: discord.VoiceChannel,
        delete_delay: int,
        category: Optional[discord.CategoryChannel] = None,
        *,
        name_template: str = "{user}'s channel",
    ):
        guild = ctx.guild
        if guild is None:
            return

        delete_delay = max(3, int(delete_delay))
        target_category_id = category.id if category else creator_room.category_id

        await self.config.guild(guild).creation_channel_id.set(creator_room.id)
        await self.config.guild(guild).name_template.set(name_template)
        await self.config.guild(guild).delete_delay.set(delete_delay)
        await self.config.guild(guild).temp_category_id.set(target_category_id)

        await self._write_guild_snapshot(guild)

        temp_cat_text = "None"
        if target_category_id is not None:
            cat_obj = guild.get_channel(target_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                temp_cat_text = cat_obj.name

        await ctx.send(
            "VoiceChannelHandling configured.\n"
            f"- Creation room: {creator_room.mention}\n"
            f"- Name template: `{name_template}`\n"
            f"- Delete delay: `{delete_delay}` seconds\n"
            f"- Temp category: `{temp_cat_text}`\n"
            f"- JSON DB file: `{guild.id}.json`"
        )

    # ==========================================================
    # Voice state listener
    # ==========================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        if before.channel == after.channel:
            return

        guild = member.guild
        if guild is None:
            return

        guild_conf = self.config.guild(guild)
        creation_channel_id = await guild_conf.creation_channel_id()

        if creation_channel_id is None:
            return

        temp_channels = set(await guild_conf.temp_channels())

        # Leaving a tracked temp channel: schedule deletion if no human members remain.
        # Music bots do not keep temp channels alive. Bot-only channel = empty.
        if before.channel and before.channel.id in temp_channels:
            if isinstance(before.channel, discord.VoiceChannel) and not self._has_human_members(before.channel):
                await self._schedule_delete_temp_channel(before.channel)

        # Joining a tracked temp channel: cancel pending deletion.
        if after.channel and after.channel.id in temp_channels:
            self._cancel_delete_task(after.channel.id)

        # Joining the creator room: create/reuse temp channel.
        if after.channel and after.channel.id == creation_channel_id:
            if isinstance(after.channel, discord.VoiceChannel):
                await self._handle_creation_join(member, after.channel)

    # ==========================================================
    # Creation logic
    # ==========================================================

    async def _handle_creation_join(
        self,
        member: discord.Member,
        base_channel: discord.VoiceChannel,
    ) -> None:
        guild = base_channel.guild

        async with self._get_guild_lock(guild.id):
            existing_id = await self._get_owner_channel_id(guild, member.id)

            if existing_id is not None:
                existing_channel = guild.get_channel(existing_id)

                if isinstance(existing_channel, discord.VoiceChannel):
                    self._cancel_delete_task(existing_channel.id)

                    try:
                        await member.move_to(
                            existing_channel,
                            reason="Moved member back to their existing temporary voice channel.",
                        )
                    except discord.Forbidden:
                        log.warning(
                            "Missing permission to move user %s to existing temp channel %s in guild %s",
                            member.id,
                            existing_id,
                            guild.id,
                        )
                    except discord.HTTPException:
                        log.exception(
                            "Failed to move user %s to existing temp channel %s in guild %s",
                            member.id,
                            existing_id,
                            guild.id,
                        )

                    return

            me = guild.me
            if me is None:
                log.warning("Could not resolve bot member in guild %s", guild.id)
                return

            perms = me.guild_permissions
            if not perms.manage_channels:
                log.warning("Missing Manage Channels in guild %s", guild.id)
                return

            if not perms.move_members:
                log.warning("Missing Move Members in guild %s", guild.id)
                return

            guild_conf = self.config.guild(guild)

            name_template = await guild_conf.name_template()

            # IMPORTANT:
            # We are already inside the guild lock here.
            # Do not call get_next_counter(), because that would try to acquire
            # the same asyncio.Lock again and deadlock.
            counter = await self._get_next_counter_unlocked(guild)

            channel_name = self._render_channel_name(name_template, member, counter)

            overwrites = base_channel.overwrites.copy()
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            )

            category = await self._resolve_temp_category(guild, base_channel)

            try:
                log.info("Creating temp voice channel for user %s in guild %s", member.id, guild.id)

                new_channel = await guild.create_voice_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Temporary voice channel created for {member} ({member.id}).",
                )

                log.info(
                    "Created temp voice channel %s for user %s in guild %s",
                    new_channel.id,
                    member.id,
                    guild.id,
                )

            except discord.Forbidden:
                log.warning("Missing permission to create temp voice channel in guild %s", guild.id)
                return
            except discord.HTTPException:
                log.exception("Failed to create temp voice channel in guild %s", guild.id)
                return

            await self._add_temp_channel(guild, new_channel.id)
            await self._set_owner_channel(guild, member.id, new_channel.id)
            await self._write_guild_snapshot(guild)

            try:
                await member.move_to(
                    new_channel,
                    reason="Moved member into their temporary voice channel.",
                )
            except discord.Forbidden:
                log.warning(
                    "Missing permission to move user %s into temp channel %s in guild %s",
                    member.id,
                    new_channel.id,
                    guild.id,
                )
                await self._schedule_delete_temp_channel(new_channel)
                return
            except discord.HTTPException:
                log.exception(
                    "Failed to move user %s into temp channel %s in guild %s",
                    member.id,
                    new_channel.id,
                    guild.id,
                )
                await self._schedule_delete_temp_channel(new_channel)
                return

            if not self._has_human_members(new_channel):
                await self._schedule_delete_temp_channel(new_channel)

    async def _resolve_temp_category(
        self,
        guild: discord.Guild,
        base_channel: discord.VoiceChannel,
    ) -> Optional[discord.CategoryChannel]:
        temp_category_id = await self.config.guild(guild).temp_category_id()

        if temp_category_id is not None:
            cat_obj = guild.get_channel(temp_category_id)
            if isinstance(cat_obj, discord.CategoryChannel):
                return cat_obj

        return base_channel.category

    def _render_channel_name(self, template: str, member: discord.Member, counter: int) -> str:
        tag = str(member)

        try:
            rendered = template.format(
                user=member.display_name,
                id=member.id,
                tag=tag,
                counter=counter,
            )
        except Exception:
            rendered = f"{member.display_name}'s channel"

        return self._sanitize_channel_name(rendered, member)

    @staticmethod
    def _sanitize_channel_name(name: str, member: discord.Member) -> str:
        name = name.replace("\n", " ").replace("\r", " ").strip()

        if not name:
            name = f"{member.display_name}'s channel"

        return name[:100]

    @staticmethod
    def _has_human_members(channel: discord.VoiceChannel) -> bool:
        """Return True when at least one non-bot member is inside the channel."""
        return any(not member.bot for member in channel.members)

    # ==========================================================
    # Deletion logic
    # ==========================================================

    async def _schedule_delete_temp_channel(self, channel: discord.VoiceChannel) -> None:
        if channel.id in self._delete_tasks:
            return

        if self._has_human_members(channel):
            return

        task = asyncio.create_task(
            self._delete_temp_channel_after_delay(channel.guild.id, channel.id)
        )

        self._delete_tasks[channel.id] = task

    async def _delete_temp_channel_after_delay(self, guild_id: int, channel_id: int) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            delay = await self.get_delete_delay(guild)
            await asyncio.sleep(delay)

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            channel = guild.get_channel(channel_id)

            if not isinstance(channel, discord.VoiceChannel):
                await self._remove_temp_channel(guild, channel_id)
                await self._clear_owner_by_channel(guild, channel_id)
                await self._write_guild_snapshot(guild)
                return

            if self._has_human_members(channel):
                return

            try:
                await channel.delete(reason="Temporary voice channel has no human members.")
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning("Missing permission to delete temp channel %s in guild %s", channel_id, guild_id)
                return
            except discord.HTTPException:
                log.exception("Failed to delete temp channel %s in guild %s", channel_id, guild_id)
                return

            await self._remove_temp_channel(guild, channel_id)
            await self._clear_owner_by_channel(guild, channel_id)
            await self._write_guild_snapshot(guild)

        except asyncio.CancelledError:
            raise
        finally:
            self._delete_tasks.pop(channel_id, None)

    def _cancel_delete_task(self, channel_id: int) -> None:
        task = self._delete_tasks.pop(channel_id, None)

        if task and not task.done():
            task.cancel()

    # ==========================================================
    # Config helpers
    # ==========================================================

    async def _add_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).temp_channels() as channels:
            if channel_id not in channels:
                channels.append(channel_id)

    async def _remove_temp_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).temp_channels() as channels:
            while channel_id in channels:
                channels.remove(channel_id)

    async def _set_owner_channel(self, guild: discord.Guild, user_id: int, channel_id: int) -> None:
        async with self.config.guild(guild).owner_channels() as owners:
            owners[str(user_id)] = channel_id

    async def _get_owner_channel_id(self, guild: discord.Guild, user_id: int) -> Optional[int]:
        owners = await self.config.guild(guild).owner_channels()
        chan_id = owners.get(str(user_id))

        if chan_id is None:
            return None

        try:
            chan_id = int(chan_id)
        except (TypeError, ValueError):
            async with self.config.guild(guild).owner_channels() as owners_mut:
                owners_mut.pop(str(user_id), None)
            await self._write_guild_snapshot(guild)
            return None

        channel = guild.get_channel(chan_id)

        if not isinstance(channel, discord.VoiceChannel):
            async with self.config.guild(guild).owner_channels() as owners_mut:
                owners_mut.pop(str(user_id), None)

            await self._remove_temp_channel(guild, chan_id)
            await self._write_guild_snapshot(guild)
            return None

        return chan_id

    async def _clear_owner_by_channel(self, guild: discord.Guild, channel_id: int) -> None:
        async with self.config.guild(guild).owner_channels() as owners:
            to_delete = [
                uid for uid, cid in owners.items()
                if str(cid) == str(channel_id)
            ]

            for uid in to_delete:
                owners.pop(uid, None)
