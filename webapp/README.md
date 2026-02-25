# Web App (MVP)

Simple web interface for RSS-to-transcript.

## Features

- Input `RSS Feed URL` and `Episode Title`
- Generate transcript directly in browser
- Optional speaker detection mode (diarized -> speaker-tagged text)
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

## API

- `GET /api/health`
- `POST /api/transcribe`

Request body:

```json
{
  "feed_url": "https://feeds.example.com/podcast.rss",
  "episode_title": "Episode title",
  "include_speakers": false
}
```
