# podcast-rss-transcribe

Resolve podcast episodes from RSS/Atom feeds, download a specific episode audio file, and hand it to transcription.

## What this skill includes

- `SKILL.md`: Codex skill definition and workflow
- `agents/openai.yaml`: UI metadata for Codex
- `scripts/podcast_rss_episode.py`: episode listing/resolving/downloading CLI

## Requirements

- Python 3.9+
- Network access for RSS and audio download
- `OPENAI_API_KEY` (only required when you run transcription)
- Optional: `ffmpeg` if you want speed changes (e.g., 2x/3x) before transcription

## Install for local use

Option 1 (copy folder):

```bash
mkdir -p "$HOME/.codex/skills"
cp -R podcast-rss-transcribe "$HOME/.codex/skills/"
```

Option 2 (symlink during development):

```bash
mkdir -p "$HOME/.codex/skills"
ln -s "$(pwd)/podcast-rss-transcribe" "$HOME/.codex/skills/podcast-rss-transcribe"
```

## Publish for others

1. Push this folder to a GitHub repository.
2. Share the repo URL and the skill path (if not at repo root).
3. Other users can install it using Codex skill install flow from GitHub repo source.

## CLI usage

List episodes:

```bash
python3 scripts/podcast_rss_episode.py list \
  --feed-url "https://example.com/podcast.rss" \
  --limit 25
```

Resolve one episode:

```bash
python3 scripts/podcast_rss_episode.py resolve \
  --feed-url "https://example.com/podcast.rss" \
  --title "Episode title"
```

Download one episode by guid:

```bash
python3 scripts/podcast_rss_episode.py download \
  --feed-url "https://example.com/podcast.rss" \
  --episode-guid "episode-guid" \
  --out-dir "output/podcast-audio"
```

## Transcription handoff example

```bash
python3 "$HOME/.codex/skills/transcribe/scripts/transcribe_diarize.py" \
  "output/podcast-audio/episode.mp3" \
  --response-format text \
  --out "output/transcribe/episode.md"
```

## Notes

- Prefer `--episode-guid` for stable matching.
- If title matching is ambiguous, use `--episode-index` or `--episode-guid`.
- For best transcript quality, avoid very high speed-up factors.
