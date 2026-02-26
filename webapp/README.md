# Web App

Simple web interface for podcast transcription with long-audio reliability.

## Features

- Input by `RSS Feed URL + Episode Title`
- Input by `Podcast Title + Episode Title` (backend feed discovery)
- Async jobs (`create -> poll -> download`) for long episodes
- Chunked transcription for large audio (auto split)
- Retry per chunk for transient API/network failures
- Optional speaker detection
- Optional readability formatting
- Copy all transcript content
- Download transcript as `.md`

## Run

From repository root:

```bash
cd "/Users/simona/Documents/Vibe Projects/podcast-rss-to-trsncript"
source ~/.zshrc
uvicorn webapp.backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Open: [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Environment

Required:

- `OPENAI_API_KEY`
- `ffmpeg` (normalization + chunking)
- `scripts/podcast_rss_episode.py`
- `scripts/transcribe_diarize.py`

Optional tuning env vars:

- `TRANSCRIBE_CHUNK_SECONDS` (default `600`)
- `MAX_EPISODE_DURATION_SECONDS` (default `10800`)
- `CHUNK_TRANSCRIBE_RETRIES` (default `4`)
- `JOB_RETENTION_DAYS` (default `7`)

## API

- `GET /api/health`
- `POST /api/jobs`
- `GET /api/jobs/{job_id}`
- `GET /api/jobs/{job_id}/download`

Backward compatible endpoint (synchronous):

- `POST /api/transcribe`

## Request payload

Provide exactly one of `feed_url` or `podcast_title`:

```json
{
  "feed_url": "https://feeds.example.com/podcast.rss",
  "episode_title": "Episode title",
  "include_speakers": false,
  "format_readable": true
}
```

or

```json
{
  "podcast_title": "How to Be Anything",
  "episode_title": "9. How to Be a Doctor in the Arctic Circle",
  "include_speakers": false,
  "format_readable": true
}
```
