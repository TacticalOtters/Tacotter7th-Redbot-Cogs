from typing import Optional

import discord
from redbot.core import commands


class VCCCommandsMixin:
    """
    Voice Channel Commands (VCC) mixin.

    This class is mixed into VoiceChannelHandling to provide hybrid commands
    for configuring and inspecting the temporary voice channel system.

    Requirements:
    - The final cog class must define:
        - config (redbot Config)
        - get_creation_channel_id(guild)
        - set_creation_channel(guild, channel_id)
        - get_name_template(guild)
        - set_name_template(guild, template)
        - get_delete_delay(guild)
        - set_delete_delay(guild, delay)
        - get_temp_category(guild)
        - set_temp_category(guild, category_id)
        - _write_guild_json(guild_id, data: dict)
        - _read_guild_json(guild_id) -> Optional[dict]
    """

    @commands.hybrid_group(name="vch", invoke_without_command=True)
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def vch(self, ctx: commands.Context):
        """
        Voice channel handling control group.

        If used without a subcommand, shows current settings for this server.
        """
        guild = ctx.guild
        if guild is None:
            return

        creation_channel_id = await self.get_creation_channel_id(guild)
        name_template = await self.get_name_template(guild)
        delete_delay = await self.get_delete_delay(guild)
        temp_category_id = await self.get_temp_category(guild)

        creation_channel = (
            guild.get_channel(creation_channel_id)
            if creation_channel_id is not None
            else None
        )
        temp_category = (
            guild.get_channel(temp_category_id)
            if temp_category_id is not None
            else None
        )

        # Try to read JSON DB if it exists (best effort).
        db_data = self._read_guild_json(guild.id)
        has_json = db_data is not None

        description_lines = [
            f"Creation room: {creation_channel.mention if isinstance(creation_channel, discord.VoiceChannel) else 'Not set'}",
            f"Name template: `{name_template}`",
            f"Delete delay: {delete_delay} seconds",
            f"Temp category: {temp_category.name if isinstance(temp_category, discord.CategoryChannel) else 'Not set'}",
            f"JSON DB file: `{guild.id}.json` ({'present' if has_json else 'missing'})",
        ]

        await ctx.send(
            "VoiceChannelHandling settings for this server:\n"
            + "\n".join(f"- {line}" for line in description_lines)
        )

    # ------------------------------------------------------------------
    # vch setcreator
    # ------------------------------------------------------------------

    @vch.command(name="setcreator")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def vch_set_creator(
        self,
        ctx: commands.Context,
        creator_room: discord.VoiceChannel,
    ):
        """
        Set the lobby voice channel where users join to spawn a temp channel.
        """
        guild = ctx.guild
        if guild is None:
            return

        await self.set_creation_channel(guild, creator_room.id)

        # Update JSON DB if it exists.
        db_data = self._read_guild_json(guild.id) or {}
        db_data.update(
            {
                "guild_id": guild.id,
                "voicecreateroom_id": creator_room.id,
                "name_template": await self.get_name_template(guild),
                "delete_delay": await self.get_delete_delay(guild),
                "temp_category_id": await self.get_temp_category(guild),
            }
        )
        self._write_guild_json(guild.id, db_data)

        await ctx.send(f"Creation lobby channel set to {creator_room.mention}.")

    # ------------------------------------------------------------------
    # vch setname
    # ------------------------------------------------------------------

    @vch.command(name="setname")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def vch_set_name(
        self,
        ctx: commands.Context,
        *,
        template: str,
    ):
        """
        Set the name template for temporary voice channels.

        Available placeholders:
        - {user}    -> joining user's display name
        - {id}      -> joining user's ID
        - {tag}     -> joining user's tag (Name#1234)
        - {counter} -> auto-incrementing channel counter (1, 2, 3, ...)

        Examples:
            {user}'s squad
            VC-{id}
            Table #{counter} for {user} ({tag})
        """
        guild = ctx.guild
        if guild is None:
            return

        # Try a dry-run render, just to catch bad placeholders early.
        try:
            tag = str(ctx.author)
            # Use counter=1 as a dummy for validation
            test_name = template.format(
                user=ctx.author.display_name,
                id=ctx.author.id,
                tag=tag,
                counter=1,
            )
        except Exception:
            await ctx.send(
                "Template is invalid. You can use `{user}`, `{id}`, `{tag}`, and `{counter}` placeholders."
            )
            return

        await self.set_name_template(guild, template)

        db_data = self._read_guild_json(guild.id) or {}
        db_data.update(
            {
                "guild_id": guild.id,
                "voicecreateroom_id": await self.get_creation_channel_id(guild),
                "name_template": template,
                "delete_delay": await self.get_delete_delay(guild),
                "temp_category_id": await self.get_temp_category(guild),
            }
        )
        self._write_guild_json(guild.id, db_data)

        await ctx.send(
            "Name template updated.\n"
            f"Example with you: `{test_name}`"
        )

    # ------------------------------------------------------------------
    # vch setdelay
    # ------------------------------------------------------------------

    @vch.command(name="setdelay")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def vch_set_delay(
        self,
        ctx: commands.Context,
        seconds: int,
    ):
        """
        Set the delete delay (in seconds) for empty temporary channels.

        Minimum is 3 seconds to avoid spammy create/delete cycles.
        """
        guild = ctx.guild
        if guild is None:
            return

        if seconds < 3:
            seconds = 3

        await self.set_delete_delay(guild, seconds)

        db_data = self._read_guild_json(guild.id) or {}
        db_data.update(
            {
                "guild_id": guild.id,
                "voicecreateroom_id": await self.get_creation_channel_id(guild),
                "name_template": await self.get_name_template(guild),
                "delete_delay": seconds,
                "temp_category_id": await self.get_temp_category(guild),
            }
        )
        self._write_guild_json(guild.id, db_data)

        await ctx.send(f"Delete delay set to {seconds} seconds.")

    # ------------------------------------------------------------------
    # vch setcategory
    # ------------------------------------------------------------------

    @vch.command(name="setcategory")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def vch_set_category(
        self,
        ctx: commands.Context,
        category: Optional[discord.CategoryChannel] = None,
    ):
        """
        Set the category for temporary channels.

        If no category is provided, temp channels will use the creation
        lobby's category instead.
        """
        guild = ctx.guild
        if guild is None:
            return

        if category is not None:
            target_category_id = category.id
        else:
            # Fall back to creation channel's category.
            creation_id = await self.get_creation_channel_id(guild)
            creation_channel = (
                guild.get_channel(creation_id)
                if creation_id is not None
                else None
            )
            target_category_id = (
                creation_channel.category_id
                if isinstance(creation_channel, discord.VoiceChannel)
                else None
            )

        await self.set_temp_category(guild, target_category_id)

        db_data = self._read_guild_json(guild.id) or {}
        db_data.update(
            {
                "guild_id": guild.id,
                "voicecreateroom_id": await self.get_creation_channel_id(guild),
                "name_template": await self.get_name_template(guild),
                "delete_delay": await self.get_delete_delay(guild),
                "temp_category_id": target_category_id,
            }
        )
        self._write_guild_json(guild.id, db_data)

        if target_category_id is None:
            await ctx.send(
                "Temp channel category cleared. "
                "New temp channels will follow the creation lobby's category."
            )
        else:
            cat_obj = guild.get_channel(target_category_id)
            cat_name = cat_obj.name if isinstance(cat_obj, discord.CategoryChannel) else "Unknown"
            await ctx.send(
                f"Temp channel category set to `{cat_name}`."
            )
