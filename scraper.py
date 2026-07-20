"""
scraper.py — two-phase fetch pipeline with SQLite cache and outlet RSS text.

Phase 1 (HTTP, 10 workers):
  - Resolve Google News redirect URLs
  - Check DB: skip URLs already cached today
  - Try outlet's own RSS feed for body text (bypasses paywalls)
  - Fall back to trafilatura, then RSS summary, then title

Phase 2 (LLM, 3 workers):
  - Run analyse() only on articles not already in DB
  - Save results to DB

Returns all of today's articles from DB (cached + newly fetched).
"""

import re
import threading
from difflib import SequenceMatcher
import socket
import feedparser
import requests
import trafilatura
import trafilatura.settings as _traf_settings
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from llm import analyse, normalise
from db import get_today_urls, get_today_articles, save_articles, init_db

logger = logging.getLogger(__name__)

# Malaysia timezone
_MYT = ZoneInfo("Asia/Kuala_Lumpur")

# ── Trafilatura config: 8-second download timeout ─────────────────────────────
_TRAF_CONFIG = _traf_settings.use_config()
_TRAF_CONFIG.set("DEFAULT", "DOWNLOAD_TIMEOUT", "8")

# Outlet RSS fetch timeout (seconds)
_FEED_TIMEOUT = 8

# Minimum title similarity ratio to consider two articles duplicates.
# 0.75 catches "X says Y" vs "X: Y" variants while keeping genuinely different stories.
_DEDUP_THRESHOLD = 0.75


# Shared session with large pool — stops urllib3 "pool full" warnings
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# RSS feed URLs
MALAYSIA_RSS = "https://news.google.com/rss/search?q=Malaysia&hl=en-MY&gl=MY&ceid=MY:en&num=100"
GLOBAL_RSS   = "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"

# Malaysian outlet RSS feeds — used to get article body text without scraping
OUTLET_RSS = {
    "bernama.com":               "https://www.bernama.com/en/rss/news.php",
    "freemalaysiatoday.com":     "https://www.freemalaysiatoday.com/feed/",
    "malaymail.com":             "https://www.malaymail.com/feed",
    "themalaysianinsight.com":   "https://www.themalaysianinsight.com/feed",
    "malaysiakini.com":          "https://www.malaysiakini.com/rss",
    "thestar.com.my":            "https://www.thestar.com.my/rss/news/nation",
    "nst.com.my":                "https://www.nst.com.my/rss/news",
    "sinchew.com.my":            "https://www.sinchew.com.my/rss",
}

# Cache parsed outlet feeds within a single run (avoid re-fetching same feed)
# Protected by a lock so concurrent Phase-1 workers don't corrupt it.
_outlet_feed_cache: dict[str, list] = {}
_outlet_cache_lock = threading.Lock()


def _get_outlet_feed(domain: str) -> list:
    """Parse and cache an outlet's RSS feed. Returns list of entries."""
    with _outlet_cache_lock:
        if domain in _outlet_feed_cache:
            return _outlet_feed_cache[domain]

    feed_url = OUTLET_RSS.get(domain, "")
    if not feed_url:
        return []
    try:
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_FEED_TIMEOUT)
        try:
            feed = feedparser.parse(feed_url)
        finally:
            socket.setdefaulttimeout(old_timeout)
        entries = feed.entries or []
        with _outlet_cache_lock:
            _outlet_feed_cache[domain] = entries
        logger.debug("Loaded %d entries from %s feed", len(entries), domain)
        return entries
    except Exception as e:
        logger.debug("Could not load outlet feed for %s: %s", domain, e)
        with _outlet_cache_lock:
            _outlet_feed_cache[domain] = []
        return []


def _text_from_outlet_feed(url: str) -> str:
    """
    Try to find this URL's article in the outlet's own RSS feed and
    return its body text from <content:encoded> or <description>.
    Matches on URL similarity rather than exact equality (Google redirect
    URLs differ from canonical outlet URLs).
    """
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    entries = _get_outlet_feed(domain)
    if not entries:
        return ""

    path = parsed.path.rstrip("/")
    for entry in entries:
        entry_url = entry.get("link", "")
        if not entry_url:
            continue
        entry_path = urlparse(entry_url).path.rstrip("/")
        if entry_path == path or entry_path.endswith(path) or path.endswith(entry_path):
            text = (
                entry.get("content", [{}])[0].get("value", "")
                or entry.get("content_detail", {}).get("value", "")
                or entry.get("summary", "")
            )
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 100:
                logger.debug("Got outlet text for %s (%d chars)", url, len(text))
                return text
    return ""


def _is_today(entry, region: str = "MY") -> bool:
    """
    Return True if the entry is recent enough to include.
    - MY:     published today in Malaysian time (MYT, UTC+8)
    - GLOBAL: published within the last 2 days in MYT
    """
    max_age = 1 if region == "MY" else 2  # days

    for field in ("published", "updated"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            pub_myt = parsedate_to_datetime(raw).astimezone(_MYT)
            cutoff  = datetime.now(_MYT).date() - timedelta(days=max_age - 1)
            return pub_myt.date() >= cutoff
        except Exception:
            continue

    logger.debug("No parseable date on '%s', keeping it", entry.get("title", "?"))
    return True


def get_real_url(google_url: str) -> str:
    try:
        resp = _session.get(
            google_url,
            headers={"User-Agent": "Mozilla/5.0"},
            allow_redirects=True,
            timeout=5,
        )
        return resp.url
    except requests.RequestException as e:
        logger.warning("URL resolution failed for %s: %s", google_url, e)
        return google_url


def extract_text(url: str) -> str:
    """trafilatura extraction with 8s download timeout."""
    try:
        downloaded = trafilatura.fetch_url(url, config=_TRAF_CONFIG)
        if downloaded:
            text = trafilatura.extract(downloaded)
            if text:
                return text
    except Exception as e:
        logger.debug("trafilatura failed for %s: %s", url, e)
    return ""


def _fetch_one(entry) -> dict | None:
    """
    Phase 1 worker: resolve URL, get text, return partial article dict.
    Returns None on error.
    """
    try:
        real_link = get_real_url(entry.link)

        # Text priority:
        #   1. Outlet's own RSS feed (best — full text, no paywall)
        #   2. trafilatura (works on open sites)
        #   3. RSS <description> snippet
        #   4. Title only
        text = (
            _text_from_outlet_feed(real_link)
            or extract_text(real_link)
            or (entry.get("summary") or "").strip()
            or entry.title
        )

        # Publish time displayed in Malaysian time (MYT)
        pub_str = ""
        for field in ("published", "updated"):
            raw = entry.get(field)
            if raw:
                try:
                    pub_str = parsedate_to_datetime(raw).astimezone(_MYT).strftime("%I:%M %p")
                    break
                except Exception:
                    pass

        return {
            "title":     entry.title,
            "url":       real_link,
            "text":      text[:600],
            "published": pub_str,
        }
    except Exception as e:
        logger.error("Failed to fetch '%s': %s", getattr(entry, "title", "?"), e)
        return None


def _dedup_by_title(
    articles: list[dict],
    existing_titles: list[str] = None,
    threshold: float = _DEDUP_THRESHOLD,
) -> list[dict]:
    """
    Remove near-duplicate articles using title similarity.
    Checks against both newly kept articles and already-existing DB titles.
    Keeps the first article seen for each cluster of similar titles.
    """
    kept: list[dict] = []
    existing_lower = [t.lower() for t in (existing_titles or [])]

    for article in articles:
        title = article["title"].lower()
        is_dup = False

        for ext in existing_lower:
            if SequenceMatcher(None, title, ext).ratio() >= threshold:
                logger.debug("Dedup drop against DB: '%s'", article["title"])
                is_dup = True
                break

        if not is_dup:
            for existing in kept:
                if SequenceMatcher(None, title, existing["title"].lower()).ratio() >= threshold:
                    logger.debug("Dedup drop in-batch: '%s'", article["title"])
                    is_dup = True
                    break

        if not is_dup:
            kept.append(article)

    removed = len(articles) - len(kept)
    if removed:
        logger.info(
            "Dedup: removed %d duplicate article(s) (threshold=%.0f%%)",
            removed, threshold * 100,
        )
    return kept


# Maximum number of new articles to send through the LLM per run.
# Keeps cold-load time reasonable on slower hardware.
_MAX_LLM_ARTICLES = 15


def get_news(
    query: str = "Malaysia",
    region: str = "MY",
    fetch_workers: int = 10,
    llm_workers: int = 3,
    today_only: bool = True,
) -> list[dict]:
    """
    Fetch all of today's news for the given query (Malaysian time).

    Returns a merged list of:
      - Articles already in the DB for today (instant, no LLM)
      - Newly fetched articles (HTTP + LLM, then saved to DB)
    """
    init_db()

    # Clear per-run outlet feed cache so Refresh fetches fresh article text
    with _outlet_cache_lock:
        _outlet_feed_cache.clear()

    # ── RSS feed — with timeout so a slow Google response doesn't hang forever ──
    rss_url = GLOBAL_RSS if region == "GLOBAL" else MALAYSIA_RSS
    try:
        rss_resp = _session.get(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        rss_resp.raise_for_status()
        feed = feedparser.parse(rss_resp.text)
    except Exception as e:
        logger.error("RSS fetch failed (%s): %s — falling back to DB cache", rss_url, e)
        return get_today_articles(region)

    candidates = feed.entries
    if today_only:
        before = len(candidates)
        candidates = [e for e in candidates if _is_today(e, region)]
        logger.info(
            "Date filter (MYT): %d/%d entries are from today", len(candidates), before
        )

    if not candidates:
        logger.warning("No entries after date filter — returning cached articles")
        return get_today_articles(region)

    cached_urls = get_today_urls(region)
    logger.info(
        "Phase 1: fetching %d candidates (%d already cached today)",
        len(candidates), len(cached_urls),
    )

    # ── Phase 1: parallel HTTP fetch ──────────────────────────────────────────
    fetched = []
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        futures = {pool.submit(_fetch_one, e): e for e in candidates}
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            if result["url"] in cached_urls:
                logger.debug("Skipping cached URL: %s", result["url"])
                continue
            fetched.append(result)

    logger.info("Phase 1 done: %d new articles to process. Deduplicating…", len(fetched))

    today_articles = get_today_articles(region)
    db_titles = [a["title"] for a in today_articles]
    fetched = _dedup_by_title(fetched, existing_titles=db_titles)

    if len(fetched) > _MAX_LLM_ARTICLES:
        logger.info("Capping LLM batch: %d → %d articles", len(fetched), _MAX_LLM_ARTICLES)
        fetched = fetched[:_MAX_LLM_ARTICLES]

    logger.info(
        "Phase 2: LLM analysis on %d unique articles (%d workers)",
        len(fetched), llm_workers,
    )

    if not fetched:
        logger.info("All articles already cached — serving from DB")
        return get_today_articles(region)

    # ── Phase 2: LLM analysis ─────────────────────────────────────────────────
    existing = get_today_articles(region)
    seen_categories = sorted({a["category"] for a in existing})

    analysed = []

    def _analyse_one(article: dict) -> dict:
        result = analyse(article["title"], article["text"])
        return {**article, "category": result["category"], "summary": result["summary"]}

    with ThreadPoolExecutor(max_workers=llm_workers) as pool:
        futures = {pool.submit(_analyse_one, art): art for art in fetched}
        for future in as_completed(futures):
            article = future.result()
            if article is None:
                continue
            seen_categories_sorted = sorted(seen_categories)
            normed = normalise(article["category"], seen_categories_sorted)
            if normed not in seen_categories:
                seen_categories.append(normed)
            article["category"] = normed
            analysed.append(article)

    # ── Persist to DB ─────────────────────────────────────────────────────────
    if analysed:
        save_articles(analysed, region=region)

    # ── Merge DB (cached + new) and restore RSS order ─────────────────────────
    all_articles = get_today_articles(region)
    order = {e.title: i for i, e in enumerate(candidates)}
    all_articles.sort(key=lambda a: order.get(a["title"], 999))

    logger.info("Done — %d total articles for today (MYT)", len(all_articles))
    return all_articles