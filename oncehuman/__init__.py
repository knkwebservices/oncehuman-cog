from .oncehuman import OnceHuman


async def setup(bot):
    await bot.add_cog(OnceHuman(bot))
