from .voicechannelhandling import VoiceChannelHandling

async def setup(bot):
    """VCH entry point."""
    await bot.add_cog(VoiceChannelHandling(bot))
