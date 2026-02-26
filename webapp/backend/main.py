from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
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
JOBS_DB = OUTPUT_DIR / "jobs.sqlite3"
RSS_SCRIPT = REPO_ROOT / "scripts" / "podcast_rss_episode.py"
LOCAL_TRANSCRIBE_CLI = REPO_ROOT / "scripts" / "transcribe_diarize.py"
SKILL_TRANSCRIBE_CLI = Path.home() / ".codex" / "skills" / "transcribe" / "scripts" / "transcribe_diarize.py"
TRANSCRIBE_CLI = LOCAL_TRANSCRIBE_CLI if LOCAL_TRANSCRIBE_CLI.exists() else SKILL_TRANSCRIBE_CLI
READABILITY_MODEL = "gpt-4o-mini"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


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


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    progress_stage: str
    progress_percent: int


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    progress_stage: str
    progress_percent: int
    error: str | None
    created_at: str
    updated_at: str
    result: TranscribeResponse | None


app = FastAPI(title="Podcast RSS Transcript App", version="1.3.0")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

_job_queue: queue.Queue[str] = queue.Queue()
_worker_thread: threading.Thread | None = None
_db_lock = threading.Lock()
_active_job_lock = threading.Lock()
_active_job_id: str | None = None
_UNSET = object()


def _utcnow() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(JOBS_DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_jobs_db() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        with _db_connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    progress_stage TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()


def _insert_job(job_id: str, req: TranscribeRequest) -> None:
    now = _utcnow()
    payload_json = json.dumps(req.model_dump(), ensure_ascii=False)
    with _db_lock:
        with _db_connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, status, progress_stage, progress_percent,
                    payload_json, result_json, error_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (job_id, STATUS_QUEUED, "Queued", 0, payload_json, now, now),
            )
            conn.commit()


def _update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress_stage: str | None = None,
    progress_percent: int | None = None,
    error_text: object = _UNSET,
    result_json: object = _UNSET,
) -> None:
    fields: list[str] = []
    values: list[object] = []

    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if progress_stage is not None:
        fields.append("progress_stage = ?")
        values.append(progress_stage)
    if progress_percent is not None:
        fields.append("progress_percent = ?")
        values.append(max(0, min(100, int(progress_percent))))
    if error_text is not _UNSET:
        fields.append("error_text = ?")
        values.append(error_text)
    if result_json is not _UNSET:
        fields.append("result_json = ?")
        values.append(result_json)

    fields.append("updated_at = ?")
    values.append(_utcnow())
    values.append(job_id)

    with _db_lock:
        with _db_connect() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", tuple(values))
            conn.commit()


def _get_job_row(job_id: str) -> dict | None:
    with _db_lock:
        with _db_connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def _set_active_job_id(job_id: str | None) -> None:
    global _active_job_id
    with _active_job_lock:
        _active_job_id = job_id


def _get_active_job_id() -> str | None:
    with _active_job_lock:
        return _active_job_id


def _ensure_worker_started() -> None:
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(target=_job_worker_loop, name="transcribe-job-worker", daemon=True)
    _worker_thread.start()


def _validate_runtime_dependencies() -> None:
    if not RSS_SCRIPT.exists():
        raise HTTPException(status_code=500, detail=f"RSS script not found: {RSS_SCRIPT}")
    if not TRANSCRIBE_CLI.exists():
        raise HTTPException(status_code=500, detail=f"Transcribe CLI not found: {TRANSCRIBE_CLI}")
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is not set on server")


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


def _create_job_id() -> str:
    return f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"


def _run_transcription_pipeline(
    req: TranscribeRequest,
    job_id: str,
    progress_callback: Callable[[str, int], None] | None = None,
) -> TranscribeResponse:
    def progress(stage: str, pct: int) -> None:
        if progress_callback is not None:
            progress_callback(stage, pct)

    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []

    progress("Resolving episode", 8)
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
    progress("Downloading audio", 28)
    _download_episode(resolved_feed_url, guid, downloaded_audio)

    progress("Normalizing audio", 42)
    normalized_audio = _normalize_audio_for_transcription(
        downloaded_audio,
        job_dir / "episode.normalized.mp3",
    )

    raw_transcript_path = job_dir / ("transcript.diarized.json" if req.include_speakers else "transcript.txt")
    progress("Transcribing audio", 70)
    raw_content = _transcribe_audio(normalized_audio, raw_transcript_path, req.include_speakers)

    transcript_text = _format_diarized_json(raw_content) if req.include_speakers else raw_content.strip()
    readability_formatted = False

    if req.format_readable:
        progress("Formatting transcript", 88)
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

    progress("Completed", 100)

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


def _job_worker_loop() -> None:
    while True:
        job_id = _job_queue.get()
        _set_active_job_id(job_id)
        try:
            row = _get_job_row(job_id)
            if not row:
                continue

            req_payload = json.loads(row["payload_json"])
            req = TranscribeRequest.model_validate(req_payload)

            _update_job(
                job_id,
                status=STATUS_RUNNING,
                progress_stage="Started",
                progress_percent=2,
                error_text=None,
            )

            result = _run_transcription_pipeline(
                req,
                job_id,
                progress_callback=lambda stage, pct: _update_job(
                    job_id,
                    progress_stage=stage,
                    progress_percent=pct,
                ),
            )

            _update_job(
                job_id,
                status=STATUS_COMPLETED,
                progress_stage="Completed",
                progress_percent=100,
                error_text=None,
                result_json=json.dumps(result.model_dump(), ensure_ascii=False),
            )
        except Exception as exc:
            _update_job(
                job_id,
                status=STATUS_FAILED,
                progress_stage="Failed",
                progress_percent=100,
                error_text=_to_user_error_message(exc),
            )
        finally:
            _set_active_job_id(None)
            _job_queue.task_done()


def _row_to_job_status(row: dict) -> JobStatusResponse:
    result: TranscribeResponse | None = None
    raw_result = row.get("result_json")
    if raw_result:
        try:
            result = TranscribeResponse.model_validate(json.loads(raw_result))
        except Exception:
            result = None

    return JobStatusResponse(
        job_id=str(row["id"]),
        status=str(row["status"]),
        progress_stage=str(row.get("progress_stage") or ""),
        progress_percent=int(row.get("progress_percent") or 0),
        error=(str(row["error_text"]) if row.get("error_text") else None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        result=result,
    )


@app.on_event("startup")
def _on_startup() -> None:
    _init_jobs_db()
    _ensure_worker_started()


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
        "worker_alive": bool(_worker_thread and _worker_thread.is_alive()),
        "queued_jobs": _job_queue.qsize(),
        "active_job_id": _get_active_job_id(),
    }


@app.post("/api/jobs", response_model=JobCreateResponse)
def create_job(req: TranscribeRequest) -> JobCreateResponse:
    _validate_runtime_dependencies()
    _init_jobs_db()
    _ensure_worker_started()

    job_id = _create_job_id()
    _insert_job(job_id, req)
    _job_queue.put(job_id)

    return JobCreateResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        progress_stage="Queued",
        progress_percent=0,
    )


@app.get("/api/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str) -> JobStatusResponse:
    row = _get_job_row(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return _row_to_job_status(row)


@app.get("/api/jobs/{job_id}/download")
def download_job_markdown(job_id: str) -> FileResponse:
    row = _get_job_row(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if row.get("status") != STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail="Transcript is not ready yet")

    raw_result = row.get("result_json")
    if not raw_result:
        raise HTTPException(status_code=500, detail="Completed job is missing transcript metadata")

    try:
        result = TranscribeResponse.model_validate(json.loads(raw_result))
    except Exception:
        raise HTTPException(status_code=500, detail="Stored transcript metadata is invalid")

    file_path = OUTPUT_DIR / job_id / result.suggested_filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Transcript markdown file not found")

    return FileResponse(
        file_path,
        media_type="text/markdown; charset=utf-8",
        filename=result.suggested_filename,
    )


@app.post("/api/transcribe", response_model=TranscribeResponse)
def transcribe(req: TranscribeRequest) -> TranscribeResponse:
    _validate_runtime_dependencies()
    _init_jobs_db()

    job_id = _create_job_id()
    try:
        return _run_transcription_pipeline(req, job_id)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Processing timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_to_user_error_message(exc))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")
