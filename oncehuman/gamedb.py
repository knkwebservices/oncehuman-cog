"""
Shared live-lookup engine for Once Human DB (oncehumandb.com) style sites.

Ported from TS6 Roadie's `util/gamedb.ts` (the TeamSpeak-bot version of this
same cog). The site has no public API, and its robots.txt allows bots
(only /api/ is off-limits, which this never touches): the cog hits the same
search page and entry pages a person would, reads the structured data
(JSON-LD) the pages carry for search engines, caches what it gets, and
never asks more than once a second.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import quote

import aiohttp

FETCH_TIMEOUT = aiohttp.ClientTimeout(total=8)
MIN_GAP = 1.0  # seconds between requests to the same site
CACHE_TTL = 60 * 60  # seconds
CACHE_MAX = 300
USER_AGENT = "TGSC-RedBot (Red-DiscordBot cog; https://github.com/knkwebservices)"

_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_LINK_RE = re.compile(r'<a\b[^>]*\bhref="(/([a-z0-9-]+)/[^"#?]+)"[^>]*>([\s\S]*?)</a>', re.IGNORECASE)
_LDJSON_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>([\s\S]*?)</script>', re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b[^>]*>([\s\S]*?)</h1>", re.IGNORECASE)

_SKIP_TYPES = {"WebSite", "Organization", "VideoGame", "BreadcrumbList", "FAQPage"}


class LookupError(Exception):
    """Raised for anything that stops a search/entry fetch from completing."""


@dataclass
class SearchHit:
    kind: str
    name: str
    detail: str
    path: str


@dataclass
class Entry:
    name: str
    url: str
    description: Optional[str] = None
    props: List[Tuple[str, str]] = field(default_factory=list)
    faq: List[Tuple[str, str]] = field(default_factory=list)


def _decode(s: str) -> str:
    s = _COMMENT_RE.sub("", s)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _text_parts(fragment: str) -> List[str]:
    fragment = _COMMENT_RE.sub("", fragment)
    parts = re.split(r"<[^>]+>", fragment)
    return [p for p in (_decode(p) for p in parts) if p]


def parse_search(html_text: str, sections: List[str]) -> List[SearchHit]:
    """Search results are links like <a href="/items/x"><span>item</span><span>Name</span><p>detail</p></a>."""
    out: List[SearchHit] = []
    seen = set()
    for m in _LINK_RE.finditer(html_text):
        path, section, inner = m.group(1), m.group(2), m.group(3)
        if section.lower() not in sections:
            continue
        if "/category/" in path:
            continue
        parts = _text_parts(inner)
        if len(parts) < 2:
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(SearchHit(kind=parts[0].lower(), name=parts[1], detail=" ".join(parts[2:]), path=path))
    return out


def parse_entry(html_text: str, url: str) -> Entry:
    """Read an entry page: its JSON-LD (name, description, properties, FAQ), falling back to meta tags."""
    blocks = []
    for m in _LDJSON_RE.finditer(html_text):
        try:
            v = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        for b in v if isinstance(v, list) else [v]:
            if isinstance(b, dict):
                blocks.append(b)

    def s(v) -> Optional[str]:
        return _decode(v) if isinstance(v, str) and v.strip() else None

    main = next(
        (b for b in blocks if isinstance(b.get("@type"), str) and b["@type"] not in _SKIP_TYPES and s(b.get("name"))),
        None,
    )

    props: List[Tuple[str, str]] = []
    me = main.get("mainEntity") if main else None
    add = me.get("additionalProperty") if isinstance(me, dict) else None
    if add is None and main:
        add = main.get("additionalProperty")
    if isinstance(add, list):
        for p in add:
            if not isinstance(p, dict):
                continue
            n = s(p.get("name"))
            v = p.get("value")
            val = str(v) if isinstance(v, (int, float)) else s(v)
            if n and val:
                props.append((n, val))

    faq: List[Tuple[str, str]] = []
    for b in blocks:
        if b.get("@type") != "FAQPage":
            continue
        lst = b.get("mainEntity")
        if not isinstance(lst, list):
            continue
        for q in lst:
            if not isinstance(q, dict):
                continue
            question = s(q.get("name"))
            accepted = q.get("acceptedAnswer")
            answer = s(accepted.get("text")) if isinstance(accepted, dict) else None
            if question and answer:
                faq.append((question, answer))

    def meta(name: str) -> Optional[str]:
        m = re.search(rf'<meta[^>]+(?:name|property)="{name}"[^>]+content="([^"]*)"', html_text, re.IGNORECASE)
        if not m:
            m = re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:name|property)="{name}"', html_text, re.IGNORECASE)
        return s(m.group(1)) if m else None

    h1 = _H1_RE.search(html_text)
    name = (s(main.get("name")) if main else None) or (s(h1.group(1)) if h1 else None) or meta("og:title")
    if not name:
        raise LookupError("That page didn't look like a database entry.")

    description = (s(main.get("description")) if main else None) or meta("description")
    return Entry(name=name, url=url, description=description, props=props, faq=faq)


def rank_hits(hits: List[SearchHit], query: str, prefer: List[str]) -> List[SearchHit]:
    """The best hit for what someone typed: an exact name first, then names starting with it, then the rest;
    within each, the kinds listed first in `prefer` win, then the site's own order."""
    q = query.strip().lower()

    def level(h: SearchHit) -> int:
        n = h.name.lower()
        if n == q:
            return 0
        if n.startswith(q):
            return 1
        if q in n:
            return 2
        return 3

    def kind_rank(h: SearchHit) -> int:
        try:
            return prefer.index(h.kind)
        except ValueError:
            return len(prefer)

    indexed = list(enumerate(hits))
    indexed.sort(key=lambda pair: (level(pair[1]), kind_rank(pair[1]), pair[0]))
    return [h for _, h in indexed]


class GameDb:
    """One site: search + entry fetches, with a cache and a one-request-per-second limit."""

    def __init__(self, session: aiohttp.ClientSession, base: str, sections: List[str]):
        self.session = session
        self.base = base.rstrip("/")
        self.sections = sections
        self._cache: dict = {}
        self._cache_order: List[str] = []
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _get(self, path: str) -> str:
        async with self._lock:
            wait = self._last + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            host = re.sub(r"^www\.", "", self.base.split("//", 1)[-1])
            try:
                async with self.session.get(
                    self.base + path,
                    timeout=FETCH_TIMEOUT,
                    headers={"User-Agent": USER_AGENT},
                ) as resp:
                    if resp.status == 404:
                        raise LookupError("not found")
                    if resp.status != 200:
                        raise LookupError(f"{host} answered HTTP {resp.status}")
                    return await resp.text()
            except asyncio.TimeoutError:
                raise LookupError(f"{host} did not answer in time")
            except aiohttp.ClientError:
                raise LookupError(f"could not reach {host}")

    def _cache_get(self, key: str):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        return None

    def _cache_set(self, key: str, value) -> None:
        self._cache[key] = (time.monotonic(), value)
        self._cache_order.append(key)
        if len(self._cache_order) > CACHE_MAX:
            old = self._cache_order.pop(0)
            self._cache.pop(old, None)

    async def search(self, query: str) -> List[SearchHit]:
        q = query.strip().lower()
        key = f"s:{q}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        text = await self._get(f"/search?q={quote(q)}")
        value = parse_search(text, self.sections)
        self._cache_set(key, value)
        return value

    async def entry(self, path: str) -> Entry:
        key = f"e:{path}"
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        text = await self._get(path)
        value = parse_entry(text, self.base + path)
        self._cache_set(key, value)
        return value
