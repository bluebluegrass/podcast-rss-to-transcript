from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .feed_discovery import discover_feed_for_episode


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBAPP_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = WEBAPP_ROOT / "frontend"
OUTPUT_DIR = WEBAPP_ROOT / "output"
RSS_SCRIPT = REPO_ROOT / "scripts" / "podcast_rss_episode.py"
LOCAL_TRANSCRIBE_CLI = REPO_ROOT / "scripts" / "transcribe_diarize.py"
SKILL_TRANSCRIBE_CLI = Path.home() / ".codex" / "skills" / "transcribe" / "scripts" / "transcribe_diarize.py"
TRANSCRIBE_CLI = LOCAL_TRANSCRIBE_CLI if LOCAL_TRANSCRIBE_CLI.exists() else SKILL_TRANSCRIBE_CLI
READABILITY_MODEL = "gpt-4o-mini"


class TranscribeRequest(BaseModel):
    feed_url: str | None = Field(default=None, min_length=10, max_length=2000)
    podcast_title: str | None = Field(default=None, min_length=2, max_length=300)
    episode_title: str = Field(min_length=2, max_length=300)
    include_speakers: bool = False
    format_readable: bool = True

    @model_validator(mode="after")
    def validate_source_input(self) -> "TranscribeRequest":
        has_feed = bool(self.feed_url and self.feed_url.strip())
        has_podcast = bool(self.podcast_title and self.podcast_title.strip())
        if has_feed == has_podcast:
            raise ValueError("Provide exactly one of feed_url or podcast_title")
        return self


class TranscribeResponse(BaseModel):
    job_id: str
    episode_title: str
    published: str
    guid: str
    mode: str
    resolved_feed_url: str
    podcast_title_resolved: str
    discovery_method: str
    warnings: list[str]
    readability_formatted: bool
    transcript_text: str
    transcript_markdown: str
    suggested_filename: str


app = FastAPI(title="Podcast RSS Transcript App", version="1.2.0")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _run_command(cmd: list[str], timeout_seconds: int = 1800) -> str:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "Command failed").strip()
        raise RuntimeError(msg)
    return (proc.stdout or "").strip()


def _extract_json(output: str) -> dict:
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        start = output.find("{")
        end = output.rfind("}")
        if start >= 0 and end > start:
            return json.loads(output[start : end + 1])
        raise


def _format_diarized_json(raw_json: str) -> str:
    obj = json.loads(raw_json)
    segments = obj.get("segments", [])
    if not segments:
        return str(obj.get("text", "")).strip()

    turns: list[str] = []
    current_speaker: str | None = None
    current_parts: list[str] = []

    for seg in segments:
        speaker = str(seg.get("speaker") or "Unknown").strip()
        text = " ".join(str(seg.get("text") or "").split())
        if not text:
            continue

        if speaker == current_speaker:
            current_parts.append(text)
            continue

        if current_speaker is not None and current_parts:
            turns.append(f"Speaker {current_speaker}: {' '.join(current_parts)}")

        current_speaker = speaker
        current_parts = [text]

    if current_speaker is not None and current_parts:
        turns.append(f"Speaker {current_speaker}: {' '.join(current_parts)}")

    return "\n\n".join(turns).strip()


def _format_transcript_readable(transcript_text: str, include_speakers: bool) -> tuple[str, bool]:
    text = transcript_text.strip()
    if not text:
        return transcript_text, False

    try:
        from openai import OpenAI
    except Exception:
        return transcript_text, False

    system_prompt = (
        "You are a transcript formatter. Rewrite transcript text into clean, human-readable markdown. "
        "Preserve factual meaning. Do not summarize. Do not invent content. "
        "Keep speaker tags exactly if present. "
        "Break long blocks into short paragraphs (2-4 sentences). "
        "Fix punctuation and capitalization. "
        "Remove only obvious accidental duplicate phrases. "
        "Output markdown only."
    )

    if include_speakers:
        user_prompt = (
            "Format this transcript while preserving speaker labels like 'Speaker A:' per turn. "
            "Keep chronological order and wording intent.\n\n"
            f"Transcript:\n{text}"
        )
    else:
        user_prompt = (
            "Format this transcript into readable paragraphs while preserving meaning and chronology.\n\n"
            f"Transcript:\n{text}"
        )

    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=READABILITY_MODEL,
            temperature=0.1,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        formatted = (resp.choices[0].message.content or "").strip()
    except Exception:
        return transcript_text, False

    if not formatted:
        return transcript_text, False

    if len(formatted) < max(120, int(len(text) * 0.35)):
        return transcript_text, False

    return formatted, True


def _sanitize_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_", " ") else "-" for ch in value)
    cleaned = "-".join(cleaned.split())
    return cleaned[:120].strip("-") or "transcript"


def _resolve_episode(feed_url: str, title: str) -> dict:
    output = _run_command(
        [
            "python3",
            str(RSS_SCRIPT),
            "resolve",
            "--feed-url",
            feed_url,
            "--title",
            title,
            "--match-mode",
            "contains",
        ],
        timeout_seconds=120,
    )
    return _extract_json(output)


def _download_episode(feed_url: str, guid: str, audio_out: Path) -> None:
    _run_command(
        [
            "python3",
            str(RSS_SCRIPT),
            "download",
            "--feed-url",
            feed_url,
            "--episode-guid",
            guid,
            "--out",
            str(audio_out),
        ],
        timeout_seconds=600,
    )


def _normalize_audio_for_transcription(input_audio: Path, output_audio: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return input_audio

    # Normalize container/codec so upstream transcription API gets a predictable audio stream.
    _run_command(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(input_audio),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(output_audio),
        ],
        timeout_seconds=900,
    )

    if not output_audio.exists() or output_audio.stat().st_size == 0:
        raise RuntimeError("Audio normalization failed: generated file is empty")

    return output_audio


def _transcribe_audio(audio_path: Path, out_path: Path, include_speakers: bool) -> str:
    cmd = [
        "python3",
        str(TRANSCRIBE_CLI),
        str(audio_path),
        "--response-format",
        "diarized_json" if include_speakers else "text",
        "--out",
        str(out_path),
    ]
    if include_speakers:
        cmd.extend(["--model", "gpt-4o-transcribe-diarize"])

    try:
        _run_command(cmd, timeout_seconds=2400)
    except RuntimeError as exc:
        msg = str(exc)
        if "Audio file might be corrupted or unsupported" in msg:
            raise RuntimeError(
                "Downloaded audio could not be decoded by transcription API. "
                "Try another episode/feed, or disable speaker detection."
            ) from exc
        raise

    return out_path.read_text(encoding="utf-8")


def _to_user_error_message(exc: Exception) -> str:
    raw = str(exc).strip()
    if not raw:
        return "Unexpected error while processing transcript request"

    if "Audio file might be corrupted or unsupported" in raw:
        return "Downloaded audio could not be decoded by transcription API. Try another episode or feed URL."

    if "No podcast feed candidates found" in raw:
        return "Could not find a podcast feed for that podcast title. Try a more specific title or provide RSS URL directly."

    if "Found podcast feeds but no matching episode title was found" in raw:
        return "Podcast was found, but the episode title did not match. Try exact episode title text or provide RSS URL directly."

    if "Provide exactly one of feed_url or podcast_title" in raw:
        return "Provide either RSS feed URL or podcast title (not both)."

    if "Traceback" in raw:
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if lines:
            return lines[-1]

    return raw


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "rss_script_exists": RSS_SCRIPT.exists(),
        "transcribe_cli_exists": TRANSCRIBE_CLI.exists(),
        "openai_key_set": bool(os.getenv("OPENAI_API_KEY")),
        "readability_model": READABILITY_MODEL,
        "ffmpeg_exists": bool(shutil.which("ffmpeg")),
        "discovery_provider": "itunes_search_with_cache",
    }


@app.post("/api/transcribe", response_model=TranscribeResponse)
def transcribe(req: TranscribeRequest) -> TranscribeResponse:
    if not RSS_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"RSS script not found: {RSS_SCRIPT}")
    if not TRANSCRIBE_CLI.exists():
        raise HTTPException(status_code=500, detail=f"Transcribe CLI not found: {TRANSCRIBE_CLI}")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is not set on server")

    job_id = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        warnings: list[str] = []

        if req.feed_url:
            resolved_feed_url = req.feed_url
            podcast_title_resolved = ""
            discovery_method = "rss_direct"
            episode = _resolve_episode(resolved_feed_url, req.episode_title)
        else:
            discovery = discover_feed_for_episode(
                podcast_title=req.podcast_title or "",
                episode_title=req.episode_title,
                resolve_episode_fn=_resolve_episode,
            )
            resolved_feed_url = str(discovery["feed_url"])
            podcast_title_resolved = str(discovery.get("podcast_title_resolved") or "")
            discovery_method = str(discovery.get("discovery_method") or "itunes_search")
            warnings.extend(discovery.get("warnings") or [])
            episode = discovery["episode"]
        guid = str(episode.get("guid") or "")
        if not guid:
            raise RuntimeError("Episode GUID missing from resolve output")

        downloaded_audio = job_dir / "episode.raw"
        _download_episode(resolved_feed_url, guid, downloaded_audio)

        normalized_audio = _normalize_audio_for_transcription(
            downloaded_audio,
            job_dir / "episode.normalized.mp3",
        )

        raw_transcript_path = job_dir / ("transcript.diarized.json" if req.include_speakers else "transcript.txt")
        raw_content = _transcribe_audio(normalized_audio, raw_transcript_path, req.include_speakers)

        transcript_text = _format_diarized_json(raw_content) if req.include_speakers else raw_content.strip()
        readability_formatted = False
        if req.format_readable:
            transcript_text, readability_formatted = _format_transcript_readable(
                transcript_text,
                req.include_speakers,
            )

        transcript_markdown = (
            f"# {episode.get('title', req.episode_title)}\n\n"
            f"- Published: {episode.get('published', '')}\n"
            f"- GUID: {guid}\n"
            f"- Feed: {resolved_feed_url}\n"
            f"- Discovery Method: {discovery_method}\n"
            f"- Podcast Resolved: {podcast_title_resolved or 'N/A'}\n"
            f"- Speaker Detection: {'On' if req.include_speakers else 'Off'}\n"
            f"- Readability Formatting: {'On' if readability_formatted else 'Off'}\n"
            f"- Warnings: {'; '.join(warnings) if warnings else 'None'}\n\n"
            f"## Transcript\n\n{transcript_text}\n"
        )

        suggested_filename = f"{_sanitize_filename(str(episode.get('title', req.episode_title)))}.md"
        (job_dir / suggested_filename).write_text(transcript_markdown, encoding="utf-8")

        return TranscribeResponse(
            job_id=job_id,
            episode_title=str(episode.get("title", req.episode_title)),
            published=str(episode.get("published", "")),
            guid=guid,
            mode="speaker-tagged" if req.include_speakers else "plain-text",
            resolved_feed_url=resolved_feed_url,
            podcast_title_resolved=podcast_title_resolved,
            discovery_method=discovery_method,
            warnings=warnings,
            readability_formatted=readability_formatted,
            transcript_text=transcript_text,
            transcript_markdown=transcript_markdown,
            suggested_filename=suggested_filename,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Processing timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_to_user_error_message(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
