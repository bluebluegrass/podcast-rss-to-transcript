#!/usr/bin/env python3
"""List, resolve, and download podcast episodes from RSS/Atom feeds."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable


ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus")


@dataclass
class Episode:
    index: int
    title: str
    guid: str
    published: str
    audio_url: str
    link: str


def fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "codex-podcast-rss-transcribe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def text_or_empty(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def pick_audio_url(item: ET.Element) -> str:
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = (enclosure.get("url") or "").strip()
        if url:
            return url
    for el in item.findall("link"):
        candidate = (el.text or "").strip()
        if candidate.lower().endswith(AUDIO_EXTENSIONS):
            return candidate
    return ""


def parse_rss_items(root: ET.Element) -> list[Episode]:
    channel = root.find("channel")
    if channel is None:
        return []
    episodes: list[Episode] = []
    for idx, item in enumerate(channel.findall("item"), start=1):
        title = text_or_empty(item.find("title"))
        guid = text_or_empty(item.find("guid")) or title
        published = text_or_empty(item.find("pubDate"))
        audio_url = pick_audio_url(item)
        link = text_or_empty(item.find("link"))
        episodes.append(Episode(idx, title, guid, published, audio_url, link))
    return episodes


def parse_atom_items(root: ET.Element) -> list[Episode]:
    episodes: list[Episode] = []
    for idx, entry in enumerate(root.findall("atom:entry", ATOM_NS), start=1):
        title = text_or_empty(entry.find("atom:title", ATOM_NS))
        guid = text_or_empty(entry.find("atom:id", ATOM_NS)) or title
        published = text_or_empty(entry.find("atom:published", ATOM_NS)) or text_or_empty(
            entry.find("atom:updated", ATOM_NS)
        )
        audio_url = ""
        link = ""
        for node in entry.findall("atom:link", ATOM_NS):
            href = (node.get("href") or "").strip()
            rel = (node.get("rel") or "").strip()
            if not link and href and (rel in ("", "alternate")):
                link = href
            if href and (rel == "enclosure" or href.lower().endswith(AUDIO_EXTENSIONS)):
                audio_url = href
        episodes.append(Episode(idx, title, guid, published, audio_url, link))
    return episodes


def load_episodes(feed_url: str) -> list[Episode]:
    raw = fetch_bytes(feed_url)
    root = ET.fromstring(raw)
    tag = root.tag.lower()
    if tag.endswith("rss"):
        episodes = parse_rss_items(root)
    elif tag.endswith("feed"):
        episodes = parse_atom_items(root)
    else:
        raise ValueError(f"Unsupported feed root element: {root.tag}")
    if not episodes:
        raise ValueError("No episodes found in feed.")
    return episodes


def contains_casefold(haystack: str, needle: str) -> bool:
    return needle.casefold() in haystack.casefold()


def select_episode(
    episodes: Iterable[Episode],
    *,
    episode_index: int | None,
    episode_guid: str | None,
    title: str | None,
    match_mode: str,
    latest: bool,
) -> Episode:
    items = list(episodes)
    if latest and not any([episode_index, episode_guid, title]):
        return items[0]
    if episode_index is not None:
        for ep in items:
            if ep.index == episode_index:
                return ep
        raise ValueError(f"Episode index {episode_index} not found.")
    if episode_guid:
        for ep in items:
            if ep.guid == episode_guid:
                return ep
        raise ValueError("No episode matched the provided guid.")
    if title:
        if match_mode == "exact":
            matches = [ep for ep in items if ep.title == title]
        else:
            matches = [ep for ep in items if contains_casefold(ep.title, title)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError("No episode matched the provided title filter.")
        raise ValueError(f"Title matched {len(matches)} episodes; use guid or index for disambiguation.")
    raise ValueError("Provide one selector: --episode-index, --episode-guid, --title, or --latest.")


def safe_filename(s: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]+", "_", s).strip()
    return cleaned[:120] or "episode"


def infer_extension(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXTENSIONS:
        return ext
    return ".mp3"


def cmd_list(args: argparse.Namespace) -> int:
    episodes = load_episodes(args.feed_url)
    if args.json:
        print(json.dumps([asdict(ep) for ep in episodes[: args.limit]], indent=2, ensure_ascii=False))
        return 0
    print("index\ttitle\tpublished\tguid\taudio_url")
    for ep in episodes[: args.limit]:
        print(f"{ep.index}\t{ep.title}\t{ep.published}\t{ep.guid}\t{ep.audio_url}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    episodes = load_episodes(args.feed_url)
    selected = select_episode(
        episodes,
        episode_index=args.episode_index,
        episode_guid=args.episode_guid,
        title=args.title,
        match_mode=args.match_mode,
        latest=args.latest,
    )
    print(json.dumps(asdict(selected), indent=2, ensure_ascii=False))
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    episodes = load_episodes(args.feed_url)
    selected = select_episode(
        episodes,
        episode_index=args.episode_index,
        episode_guid=args.episode_guid,
        title=args.title,
        match_mode=args.match_mode,
        latest=args.latest,
    )
    if not selected.audio_url:
        raise ValueError("Selected episode has no enclosure/audio URL in the feed.")
    out_path = Path(args.out) if args.out else Path(args.out_dir) / (
        safe_filename(selected.title) + infer_extension(selected.audio_url)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = fetch_bytes(selected.audio_url, timeout=args.timeout)
    out_path.write_bytes(audio_bytes)
    print(json.dumps({"saved_to": str(out_path), "episode": asdict(selected)}, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_feed(p: argparse.ArgumentParser) -> None:
        p.add_argument("--feed-url", required=True, help="Podcast RSS/Atom feed URL")

    def add_selector(p: argparse.ArgumentParser) -> None:
        p.add_argument("--episode-index", type=int, help="1-based index from feed order")
        p.add_argument("--episode-guid", help="Episode guid/id")
        p.add_argument("--title", help="Episode title filter")
        p.add_argument("--match-mode", choices=("contains", "exact"), default="contains")
        p.add_argument("--latest", action="store_true", help="Pick the first feed item (usually newest)")

    p_list = sub.add_parser("list", help="List episodes in feed")
    add_feed(p_list)
    p_list.add_argument("--limit", type=int, default=25)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_resolve = sub.add_parser("resolve", help="Resolve one episode to structured JSON")
    add_feed(p_resolve)
    add_selector(p_resolve)
    p_resolve.set_defaults(func=cmd_resolve)

    p_download = sub.add_parser("download", help="Download selected episode audio")
    add_feed(p_download)
    add_selector(p_download)
    p_download.add_argument("--out", help="Exact output file path")
    p_download.add_argument("--out-dir", default="output/podcast-audio", help="Output directory when --out is omitted")
    p_download.add_argument("--timeout", type=int, default=60)
    p_download.set_defaults(func=cmd_download)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:  # pragma: no cover
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
