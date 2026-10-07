"""Web access: search (DuckDuckGo, no key needed — or your own SearXNG) and page reading.

Set AMO_SEARXNG_URL=http://localhost:8888 to use a self-hosted SearXNG instead of DuckDuckGo.
"""

from __future__ import annotations

import html
import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .config import settings

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def search(query: str, max_results: int = 6) -> list[dict[str, str]]:
    """Top web results: [{title, url, snippet}]."""
    query = query.strip()
    if not query:
        return []
    return _cached(f"s:{query}:{max_results}", 600, lambda: _search(query, max_results))


def _search(query: str, max_results: int) -> list[dict[str, str]]:
    if settings.searxng_url:
        r = httpx.get(f"{settings.searxng_url.rstrip('/')}/search",
                      params={"q": query, "format": "json"}, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
                for x in r.json().get("results", [])[:max_results]]

    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=20,
                   headers={"User-Agent": UA}, follow_redirects=True)
    r.raise_for_status()
    results = []
    for block in r.text.split('class="result results_links')[1:]:
        a = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S)
        if not a:
            continue
        href = html.unescape(a.group(1))
        if "uddg=" in href:  # DuckDuckGo redirect link → real URL
            href = unquote(parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)["uddg"][0])
        if "duckduckgo.com/y.js" in href:  # ads
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, flags=re.S)
        results.append({"title": _strip_tags(a.group(2)), "url": href,
                        "snippet": _strip_tags(snip.group(1)) if snip else ""})
        if len(results) >= max_results:
            break
    return results


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "iframe"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data)


def read(url: str, max_chars: int = 6000) -> dict[str, str]:
    """Fetch a web page and return its readable text (trimmed)."""
    if not re.match(r"^https?://", url):
        url = "https://" + url

    def fetch():
        r = httpx.get(url, timeout=25, headers={"User-Agent": UA}, follow_redirects=True)
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "html"):
            return {"url": str(r.url), "title": "", "text": r.text[:max_chars]}
        p = _Text()
        p.feed(r.text)
        text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.out))
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return {"url": str(r.url), "title": p.title.strip(), "text": text[:max_chars]}

    return _cached(f"r:{url}:{max_chars}", 900, fetch)
