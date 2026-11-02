"""Find the JSON endpoint behind agoradex.app/traders without a browser.

Downloads the page and its JavaScript bundles, collects URL-like strings that
mention traders/leaderboards, then calls each candidate and keeps the ones whose
response contains a list of wallet objects. Best-effort: frontends change, and
an endpoint called only from server-side code won't appear in the bundles.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import requests

from .leaderboard import guess_mapping

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36")
SCRIPT_RE = re.compile(r'<script[^>]+src="([^"]+\.js[^"]*)"')
CHUNK_RE = re.compile(r'"(static/chunks/[^"]+\.js)"')
ABS_URL_RE = re.compile(r'["\'`](https?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^"\'`\s<>]*)?)["\'`]')
PATH_RE = re.compile(r'["\'`](/[A-Za-z0-9_\-/.]*(?:trader|leaderboard|wallets|top)[A-Za-z0-9_\-/.]*)'
                     r'[?"\'`$]')
KEYWORDS = ("trader", "leaderboard", "/api", "api.", "indexer", "wallets")
SKIP_HOSTS = ("x.com", "twitter.com", "github.com", "google", "w3.org", "reactjs.org",
              "nextjs.org", "walletconnect", "mozilla.org", "telegram", "discord")


def _fetch(s: requests.Session, url: str, timeout: float) -> str | None:
    try:
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.debug("fetch %s failed: %s", url, e)
        return None


def collect_candidates(page_url: str, timeout: float = 20, max_scripts: int = 120
                       ) -> tuple[list[str], list[str]]:
    """Returns (absolute URLs, relative paths) found in the page's bundles."""
    s = requests.Session()
    s.headers["user-agent"] = UA
    html = _fetch(s, page_url, timeout)
    if html is None:
        raise RuntimeError(f"could not load {page_url}")
    scripts = list(dict.fromkeys(urljoin(page_url, m) for m in SCRIPT_RE.findall(html)))
    abs_urls: set[str] = set()
    paths: set[str] = set()
    sources = [html]
    seen: set[str] = set()
    i = 0
    while i < len(scripts) and i < max_scripts:
        src = scripts[i]
        i += 1
        if src in seen:
            continue
        seen.add(src)
        js = _fetch(s, src, timeout)
        if not js:
            continue
        sources.append(js)
        # Next.js build manifests reference lazily loaded chunks.
        for chunk in CHUNK_RE.findall(js):
            u = urljoin(page_url, "/_next/" + chunk)
            if u not in seen:
                scripts.append(u)
    for text in sources:
        for u in ABS_URL_RE.findall(text):
            host = urlsplit(u).netloc.lower()
            if any(k in host for k in SKIP_HOSTS):
                continue
            if any(k in u.lower() for k in KEYWORDS):
                abs_urls.add(u.rstrip("/"))
        paths.update(PATH_RE.findall(text))
    log.info("scanned %d bundles: %d URLs, %d paths", len(sources) - 1, len(abs_urls), len(paths))
    return sorted(abs_urls), sorted(paths)


def candidate_requests(page_url: str, abs_urls: list[str], paths: list[str]) -> list[str]:
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(page_url))
    bases = {origin} | {"{0.scheme}://{0.netloc}{0.path}".format(urlsplit(u)).rstrip("/")
                        for u in abs_urls if "api" in u.lower() or "indexer" in u.lower()}
    out: list[str] = []
    for u in abs_urls:
        if "trader" in u.lower() or "leaderboard" in u.lower():
            out.append(u)
    for p in paths:
        if "${" in p or p.startswith("//"):
            continue
        for b in bases:
            out.append(b + p)
    # page route itself is not an API
    out = [u for u in dict.fromkeys(out) if urlsplit(u).path.rstrip("/") not in ("/traders", "")]
    return out[:80]


def probe(urls: list[str], timeout: float = 15) -> list[dict]:
    s = requests.Session()
    s.headers.update({"user-agent": UA, "accept": "application/json",
                      "referer": "https://www.agoradex.app/traders",
                      "origin": "https://www.agoradex.app"})
    hits = []
    for u in urls:
        for params in ({"sort": "pnl", "limit": 100}, {}):
            try:
                r = s.get(u, params=params, timeout=timeout)
                if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                    continue
                guess = guess_mapping(r.json())
            except (requests.RequestException, ValueError):
                continue
            if guess:
                hits.append({"url": u, "params": params, **guess})
                break
    hits.sort(key=lambda h: (-("pnl" in h["fields"]), -h["count"]))
    return hits


def discover(page_url: str = "https://www.agoradex.app/traders") -> tuple[list[dict], list[str]]:
    abs_urls, paths = collect_candidates(page_url)
    urls = candidate_requests(page_url, abs_urls, paths)
    log.info("probing %d candidate endpoints", len(urls))
    return probe(urls), urls
