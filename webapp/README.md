# Web App (MVP)

Simple web interface for RSS-to-transcript with two input modes.

## Features

- Input by `RSS Feed URL + Episode Title`
- Input by `Podcast Title + Episode Title` (backend feed discovery)
- Generate transcript directly in browser
- Optional speaker detection mode (diarized -> speaker-tagged text)
- Optional readability formatting pass
- Copy all transcript content
- Download transcript as `.md`

## Run

From repository root:

```bash
cd "/Users/simona/Documents/Vibe Projects/podcast-rss-to-trsncript"
source ~/.zshrc
uvicorn webapp.backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Open:

- http://127.0.0.1:8000

## Environment requirements

- `OPENAI_API_KEY` set in the shell that starts `uvicorn`
- Existing scripts available:
  - `scripts/podcast_rss_episode.py`
  - `~/.codex/skills/transcribe/scripts/transcribe_diarize.py`
- `ffmpeg` recommended for robust audio normalization

## API

- `GET /api/health`
- `POST /api/transcribe`

### Request mode A (direct RSS)

```json
{
  "feed_url": "https://feeds.example.com/podcast.rss",
  "episode_title": "Episode title",
  "include_speakers": false,
  "format_readable": true
}
```

### Request mode B (podcast-title discovery)

```json
{
  "podcast_title": "How to Be Anything",
  "episode_title": "9. How to Be a Doctor in the Arctic Circle",
  "include_speakers": false,
  "format_readable": true
}
```

Rules:

- Provide exactly one of `feed_url` or `podcast_title`
- `episode_title` is always required

Response includes:

- `resolved_feed_url`
- `podcast_title_resolved`
- `discovery_method` (`rss_direct`, `itunes_search`, or `cache`)
- `warnings` for low-confidence title matches
