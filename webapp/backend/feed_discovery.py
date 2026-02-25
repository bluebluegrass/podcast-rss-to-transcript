from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Callable


ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
CACHE_TTL_SECONDS = 3600

# key -> {expires_at, feed_url, podcast_title_resolved}
_CACHE: dict[str, dict] = {}


@dataclass
class FeedCandidate:
    podcast_title: str
    feed_url: str
    score: float


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _score_title_similarity(query: str, candidate: str) -> float:
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    return SequenceMatcher(None, q, c).ratio()


def _cache_key(podcast_title: str) -> str:
    return _normalize(podcast_title)


def _get_cached_feed(podcast_title: str) -> dict | None:
    key = _cache_key(podcast_title)
    row = _CACHE.get(key)
    if not row:
        return None
    if row.get("expires_at", 0) < time.time():
        _CACHE.pop(key, None)
        return None
    return row


def _set_cached_feed(podcast_title: str, feed_url: str, podcast_title_resolved: str) -> None:
    _CACHE[_cache_key(podcast_title)] = {
        "expires_at": time.time() + CACHE_TTL_SECONDS,
        "feed_url": feed_url,
        "podcast_title_resolved": podcast_title_resolved,
    }


def search_podcast_candidates(podcast_title: str, limit: int = 10, timeout_seconds: int = 12) -> list[FeedCandidate]:
    params = urllib.parse.urlencode(
        {
            "media": "podcast",
            "entity": "podcast",
            "term": podcast_title,
            "limit": str(limit),
        }
    )
    url = f"{ITUNES_SEARCH_URL}?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": "podcast-rss-to-transcript/1.0"})
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))

    results = data.get("results", []) if isinstance(data, dict) else []
    candidates: list[FeedCandidate] = []

    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("collectionName") or "").strip()
        feed_url = str(item.get("feedUrl") or "").strip()
        if not title or not feed_url:
            continue
        candidates.append(
            FeedCandidate(
                podcast_title=title,
                feed_url=feed_url,
                score=_score_title_similarity(podcast_title, title),
            )
        )

    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates


def discover_feed_for_episode(
    podcast_title: str,
    episode_title: str,
    resolve_episode_fn: Callable[[str, str], dict],
) -> dict:
    cached = _get_cached_feed(podcast_title)
    if cached:
        try:
            episode = resolve_episode_fn(cached["feed_url"], episode_title)
            return {
                "feed_url": cached["feed_url"],
                "podcast_title_resolved": cached.get("podcast_title_resolved", podcast_title),
                "discovery_method": "cache",
                "warnings": [],
                "episode": episode,
            }
        except Exception:
            # Cache may be stale or mismatch for this episode title.
            pass

    candidates = search_podcast_candidates(podcast_title)
    if not candidates:
        raise RuntimeError("No podcast feed candidates found for this podcast title.")

    warnings: list[str] = []
    best_score = candidates[0].score
    if best_score < 0.45:
        warnings.append("Podcast title match confidence is low. Verify results carefully.")

    checked_titles: list[str] = []
    for candidate in candidates[:8]:
        checked_titles.append(candidate.podcast_title)
        try:
            episode = resolve_episode_fn(candidate.feed_url, episode_title)
            _set_cached_feed(podcast_title, candidate.feed_url, candidate.podcast_title)
            return {
                "feed_url": candidate.feed_url,
                "podcast_title_resolved": candidate.podcast_title,
                "discovery_method": "itunes_search",
                "warnings": warnings,
                "episode": episode,
            }
        except Exception:
            continue

    sample = ", ".join(checked_titles[:3])
    if sample:
        raise RuntimeError(
            "Found podcast feeds but no matching episode title was found. "
            f"Tried feeds such as: {sample}. Please provide exact episode title or RSS URL."
        )

    raise RuntimeError("Found podcast feeds but could not resolve target episode.")
