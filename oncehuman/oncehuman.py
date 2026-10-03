"""
OnceHuman — live lookups from oncehumandb.com, as a Red-DiscordBot cog.
Ported from the `oncehuman` cog in TS6 Roadie (a TeamSpeak bot) to work the
same way on Discord.

Command:
    [p]oh <name>    look something up in the Once Human database
"""

import asyncio

import aiohttp
import discord
from redbot.core import commands
from redbot.core.bot import Red

from .gamedb import Entry, GameDb, LookupError, SearchHit, rank_hits

SECTIONS = [
    "weapons",
    "armor",
    "armor-sets",
    "mods",
    "attachments",
    "items",
    "deviations",
    "memetics",
    "identities",
    "recipes",
    "cradle-overrides",
    "overrides",
]
PREFER = [
    "weapon",
    "armor",
    "deviation",
    "mod",
    "item",
    "attachment",
    "armor-set",
    "recipe",
    "identity",
    "memetic",
    "cradle-override",
]
CREDIT = "Data from Once Human DB (oncehumandb.com)"
BASE_URL = "https://www.oncehumandb.com"


class OnceHuman(commands.Cog):
    """Live lookups from the Once Human Database (oncehumandb.com)."""

    __version__ = "1.0.0"
    __author__ = "Rob Keck (knkwebservices)"

    def __init__(self, bot: Red):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self.db = GameDb(self.session, base=BASE_URL, sections=SECTIONS)

    def cog_unload(self):
        asyncio.create_task(self.session.close())

    @commands.command(aliases=["oncehuman"])
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def oh(self, ctx: commands.Context, *, query: str):
        """Look something up in the Once Human database.

        Example: `[p]oh Doombringer`
        """
        query = query.strip().replace("\n", " ")[:80]
        if len(query) < 2:
            await ctx.send(f"Usage: `{ctx.clean_prefix}oh <name>`, like `{ctx.clean_prefix}oh Doombringer`.")
            return

        async with ctx.typing():
            try:
                hits = rank_hits(await self.db.search(query), query, PREFER)
            except LookupError as e:
                await ctx.send(f"I couldn't search the Once Human database right now ({e}). Try again in a bit.")
                return

            if not hits:
                await ctx.send(f'Nothing in the Once Human database matches "{query}". Try a shorter or different name.')
                return

            best = hits[0]
            others = []
            for h in hits[1:]:
                if h.name.lower() != best.name.lower() and h.name not in others:
                    others.append(h.name)
                if len(others) >= 4:
                    break

            try:
                entry = await self.db.entry(best.path)
            except LookupError:
                # the search itself worked, so the name, its short line and the link are still worth giving
                embed = discord.Embed(
                    title=f"{best.name} ({best.kind})",
                    description=best.detail or None,
                    url=self.db.base + best.path,
                    color=await ctx.embed_color(),
                )
                embed.set_footer(text=CREDIT)
                await ctx.send(embed=embed)
                return

        embed = self._build_embed(entry, kind=best.kind, others=others, color=await ctx.embed_color(), prefix=ctx.clean_prefix)
        await ctx.send(embed=embed)

    @staticmethod
    def _build_embed(entry: Entry, *, kind: str, others: list, color, prefix: str) -> discord.Embed:
        embed = discord.Embed(
            title=f"{entry.name} ({kind})" if kind else entry.name,
            description=entry.description,
            url=entry.url,
            color=color,
        )
        for name, value in entry.props[:8]:
            embed.add_field(name=name[:256], value=value[:1024], inline=True)
        for q, a in entry.faq[:2]:
            embed.add_field(name=q[:256], value=a[:1024], inline=False)
        if others:
            embed.add_field(name="Also found", value=f"{', '.join(others)} (`{prefix}oh <name>`)", inline=False)
        embed.set_footer(text=CREDIT)
        return embed
