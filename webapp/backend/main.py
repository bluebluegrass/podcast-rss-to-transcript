from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBAPP_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = WEBAPP_ROOT / "frontend"
OUTPUT_DIR = WEBAPP_ROOT / "output"
RSS_SCRIPT = REPO_ROOT / "scripts" / "podcast_rss_episode.py"
TRANSCRIBE_CLI = Path.home() / ".codex" / "skills" / "transcribe" / "scripts" / "transcribe_diarize.py"
READABILITY_MODEL = "gpt-4o-mini"


class TranscribeRequest(BaseModel):
    feed_url: str = Field(min_length=10, max_length=2000)
    episode_title: str = Field(min_length=2, max_length=300)
    include_speakers: bool = False
    format_readable: bool = True


class TranscribeResponse(BaseModel):
    job_id: str
    episode_title: str
    published: str
    guid: str
    mode: str
    readability_formatted: bool
    transcript_text: str
    transcript_markdown: str
    suggested_filename: str


app = FastAPI(title="Podcast RSS Transcript App", version="1.1.0")
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

    # Guardrail: if formatter output is suspiciously short, keep original to avoid accidental summarization.
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

    _run_command(cmd, timeout_seconds=2400)
    return out_path.read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "rss_script_exists": RSS_SCRIPT.exists(),
        "transcribe_cli_exists": TRANSCRIBE_CLI.exists(),
        "openai_key_set": bool(os.getenv("OPENAI_API_KEY")),
        "readability_model": READABILITY_MODEL,
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
        episode = _resolve_episode(req.feed_url, req.episode_title)
        guid = str(episode.get("guid") or "")
        if not guid:
            raise RuntimeError("Episode GUID missing from resolve output")

        audio_path = job_dir / "episode.mp3"
        _download_episode(req.feed_url, guid, audio_path)

        raw_transcript_path = job_dir / ("transcript.diarized.json" if req.include_speakers else "transcript.txt")
        raw_content = _transcribe_audio(audio_path, raw_transcript_path, req.include_speakers)

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
            f"- Feed: {req.feed_url}\n"
            f"- Speaker Detection: {'On' if req.include_speakers else 'Off'}\n"
            f"- Readability Formatting: {'On' if readability_formatted else 'Off'}\n\n"
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
            readability_formatted=readability_formatted,
            transcript_text=transcript_text,
            transcript_markdown=transcript_markdown,
            suggested_filename=suggested_filename,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Processing timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
