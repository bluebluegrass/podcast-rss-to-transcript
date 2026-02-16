---
name: podcast-rss-transcribe
description: Resolve podcast episodes from RSS/Atom feeds and prepare one specific episode for transcription. Use when a user asks to transcribe a podcast episode from a feed URL, select an episode by title/index/guid, download episode audio, or bridge podcast feed discovery into an audio transcription workflow.
---

# Podcast RSS Episode Transcription

Use this skill to turn a podcast feed URL into one local audio file for transcription.

## Workflow
1. Collect inputs: feed URL and episode selector (title, guid/id, index, or latest).
2. List episodes from the feed.
3. Resolve exactly one episode.
4. Download the selected episode audio locally.
5. Hand off local audio to the `transcribe` skill.

## Resolve and Download Episode
Use the bundled script:

```bash
python3 scripts/podcast_rss_episode.py list \
  --feed-url "https://example.com/podcast.rss" \
  --limit 30
```

Pick an episode with one selector:
- `--episode-guid` for stable id matching.
- `--episode-index` for quick selection from the list.
- `--title` for fuzzy lookup (use `--match-mode exact` when needed).
- `--latest` to pick the first feed item.

Resolve before download when ambiguity is possible:

```bash
python3 scripts/podcast_rss_episode.py resolve \
  --feed-url "https://example.com/podcast.rss" \
  --title "Episode title text"
```

Download audio:

```bash
python3 scripts/podcast_rss_episode.py download \
  --feed-url "https://example.com/podcast.rss" \
  --episode-guid "episode-guid-here" \
  --out-dir "output/podcast-audio"
```

## Transcribe Downloaded Audio
After download, invoke the `transcribe` skill workflow on the saved file path.

Fast text transcript:

```bash
python3 "$TRANSCRIBE_CLI" \
  "output/podcast-audio/episode.mp3" \
  --response-format text \
  --out "output/transcribe/podcast-episode.txt"
```

Use diarization only when speaker labeling is requested.

## Decision Rules
- Prefer `guid` matching over title matching when both are available.
- If title matching returns multiple episodes, stop and disambiguate with index or guid.
- Fail fast if the feed item has no audio enclosure URL.
- Save downloaded audio under `output/podcast-audio/` unless user gives a path.
- Keep transcription output under `output/transcribe/`.
