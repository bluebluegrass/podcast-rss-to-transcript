from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
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

# OpenAI audio upload limit is ~25MB; keep headroom for robustness.
MAX_TRANSCRIBE_BYTES = int(os.getenv("MAX_TRANSCRIBE_BYTES", str(24 * 1024 * 1024)))
DEFAULT_CHUNK_SECONDS = int(os.getenv("TRANSCRIBE_CHUNK_SECONDS", "600"))
MAX_EPISODE_DURATION_SECONDS = int(os.getenv("MAX_EPISODE_DURATION_SECONDS", str(3 * 60 * 60)))
MAX_NORMALIZED_AUDIO_BYTES = int(os.getenv("MAX_NORMALIZED_AUDIO_BYTES", str(1024 * 1024 * 1024)))
CHUNK_TRANSCRIBE_RETRIES = int(os.getenv("CHUNK_TRANSCRIBE_RETRIES", "4"))
JOB_RETENTION_DAYS = int(os.getenv("JOB_RETENTION_DAYS", "7"))


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
    audio_duration_seconds: float | None = None
    chunk_count: int = 1
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS


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


app = FastAPI(title="Podcast RSS Transcript App", version="1.4.0")
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


def _recover_stale_jobs() -> int:
    now = _utcnow()
    with _db_lock:
        with _db_connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?)",
                (STATUS_QUEUED, STATUS_RUNNING),
            ).fetchall()
            recovered = len(rows)
            if recovered:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        progress_stage = ?,
                        progress_percent = ?,
                        error_text = ?,
                        updated_at = ?
                    WHERE status IN (?, ?)
                    """,
                    (
                        STATUS_FAILED,
                        "Failed",
                        100,
                        "Job interrupted by service restart. Please submit again.",
                        now,
                        STATUS_QUEUED,
                        STATUS_RUNNING,
                    ),
                )
                conn.commit()
    return recovered


def _cleanup_old_jobs() -> int:
    if JOB_RETENTION_DAYS <= 0:
        return 0

    cutoff = (datetime.utcnow() - timedelta(days=JOB_RETENTION_DAYS)).isoformat(timespec="seconds") + "Z"
    deleted_ids: list[str] = []

    with _db_lock:
        with _db_connect() as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN (?, ?) AND updated_at < ?",
                (STATUS_COMPLETED, STATUS_FAILED, cutoff),
            ).fetchall()
            deleted_ids = [str(row["id"]) for row in rows]
            if deleted_ids:
                conn.executemany("DELETE FROM jobs WHERE id = ?", [(jid,) for jid in deleted_ids])
                conn.commit()

    for job_id in deleted_ids:
        shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)

    return len(deleted_ids)


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
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=500, detail="ffmpeg is not installed on server")


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
        timeout_seconds=900,
    )


def _normalize_audio_for_transcription(input_audio: Path, output_audio: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return input_audio

    # Normalize to predictable audio stream and lower bitrate, reducing chunk size variability.
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
        timeout_seconds=1800,
    )

    if not output_audio.exists() or output_audio.stat().st_size == 0:
        raise RuntimeError("Audio normalization failed: generated file is empty")

    if output_audio.stat().st_size > MAX_NORMALIZED_AUDIO_BYTES:
        raise RuntimeError("Audio file is too large after normalization")

    return output_audio


def _probe_audio_duration_seconds(audio_path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    try:
        raw = _run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            timeout_seconds=60,
        )
        value = float(raw.strip())
        if value <= 0:
            return None
        return value
    except Exception:
        return None


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

    _run_command(cmd, timeout_seconds=2700)
    return out_path.read_text(encoding="utf-8")


def _is_retryable_error(message: str) -> bool:
    lowered = message.lower()
    indicators = (
        "rate limit",
        "timeout",
        "timed out",
        "temporarily",
        "connection reset",
        "connection aborted",
        "internal server error",
        "502",
        "503",
        "504",
        "api_connection_error",
    )
    return any(token in lowered for token in indicators)


def _transcribe_audio_with_retry(audio_path: Path, out_path: Path, include_speakers: bool) -> str:
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path.read_text(encoding="utf-8")

    attempt = 1
    while True:
        try:
            return _transcribe_audio(audio_path, out_path, include_speakers)
        except Exception as exc:
            message = str(exc)
            if "Audio file might be corrupted or unsupported" in message:
                raise RuntimeError(
                    "Downloaded audio could not be decoded by transcription API. "
                    "Try another episode/feed, or disable speaker detection."
                ) from exc

            if attempt >= CHUNK_TRANSCRIBE_RETRIES or not _is_retryable_error(message):
                raise RuntimeError(message) from exc

            delay = min(30, 2**attempt)
            time.sleep(delay)
            attempt += 1


def _split_audio_into_chunks(normalized_audio: Path, chunks_dir: Path) -> list[Path]:
    if normalized_audio.stat().st_size <= MAX_TRANSCRIBE_BYTES:
        return [normalized_audio]

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to split long audio")

    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk_%04d.mp3"

    def segment_command(copy_codec: bool) -> list[str]:
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            str(normalized_audio),
            "-f",
            "segment",
            "-segment_time",
            str(DEFAULT_CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
        ]
        if copy_codec:
            cmd.extend(["-c", "copy"])
        else:
            cmd.extend(["-ac", "1", "-ar", "16000", "-codec:a", "libmp3lame", "-b:a", "64k"])
        cmd.append(str(pattern))
        return cmd

    # First try stream copy for speed; fallback to re-encode segmentation.
    try:
        _run_command(segment_command(copy_codec=True), timeout_seconds=1800)
    except Exception:
        for existing in chunks_dir.glob("chunk_*.mp3"):
            existing.unlink(missing_ok=True)
        _run_command(segment_command(copy_codec=False), timeout_seconds=2400)

    chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
    if not chunks:
        raise RuntimeError("Failed to split long audio into chunks")

    oversized = [chunk for chunk in chunks if chunk.stat().st_size > MAX_TRANSCRIBE_BYTES]
    if oversized:
        raise RuntimeError("Generated audio chunk exceeds API upload limit; reduce chunk duration")

    return chunks


def _drop_boundary_overlap(prev_text: str, next_text: str) -> str:
    prev_words = prev_text.split()
    next_words = next_text.split()
    if not prev_words or not next_words:
        return next_text

    max_overlap = min(45, len(prev_words), len(next_words))
    for overlap in range(max_overlap, 7, -1):
        tail = " ".join(prev_words[-overlap:]).casefold()
        head = " ".join(next_words[:overlap]).casefold()
        if tail == head:
            return " ".join(next_words[overlap:]).strip()

    return next_text


def _merge_plain_text_chunks(chunk_texts: list[str]) -> str:
    merged: list[str] = []
    for text in chunk_texts:
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            continue
        if not merged:
            merged.append(cleaned)
            continue

        trimmed = _drop_boundary_overlap(merged[-1], cleaned)
        if trimmed:
            merged.append(trimmed)

    return "\n\n".join(merged).strip()


def _merge_chunk_outputs(raw_parts: list[str], include_speakers: bool) -> str:
    if include_speakers:
        turns = [_format_diarized_json(raw).strip() for raw in raw_parts if raw.strip()]
        return "\n\n".join([t for t in turns if t]).strip()

    return _merge_plain_text_chunks(raw_parts)


def _transcribe_long_audio(
    normalized_audio: Path,
    job_dir: Path,
    include_speakers: bool,
    progress_callback: Callable[[str, int], None],
) -> tuple[str, int, float | None, list[str]]:
    duration_seconds = _probe_audio_duration_seconds(normalized_audio)
    warnings: list[str] = []

    if duration_seconds and duration_seconds > MAX_EPISODE_DURATION_SECONDS:
        raise RuntimeError(
            f"Episode too long ({int(duration_seconds // 60)} min). "
            f"Current limit is {int(MAX_EPISODE_DURATION_SECONDS // 60)} min."
        )

    progress_callback("Preparing audio chunks", 50)
    chunks_dir = job_dir / "chunks"
    chunk_files = _split_audio_into_chunks(normalized_audio, chunks_dir)

    if len(chunk_files) > 1 and include_speakers:
        warnings.append("Long-audio speaker labels are best-effort and may reset between chunks.")

    chunk_outputs_dir = job_dir / "chunk_transcripts"
    chunk_outputs_dir.mkdir(parents=True, exist_ok=True)

    raw_parts: list[str] = []
    total_chunks = len(chunk_files)

    for index, chunk_path in enumerate(chunk_files, start=1):
        progress_start = 56
        progress_end = 86
        pct = progress_start + int(((index - 1) / max(1, total_chunks)) * (progress_end - progress_start))
        progress_callback(f"Transcribing chunk {index}/{total_chunks}", pct)

        out_suffix = "json" if include_speakers else "txt"
        chunk_out = chunk_outputs_dir / f"chunk_{index:04d}.{out_suffix}"
        raw = _transcribe_audio_with_retry(chunk_path, chunk_out, include_speakers)
        raw_parts.append(raw)

    progress_callback("Stitching chunks", 88)
    transcript_text = _merge_chunk_outputs(raw_parts, include_speakers)

    if not transcript_text.strip():
        raise RuntimeError("Transcript generation returned empty output")

    return transcript_text, total_chunks, duration_seconds, warnings


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

    if "Episode too long" in raw:
        return raw

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
    progress("Downloading audio", 24)
    _download_episode(resolved_feed_url, guid, downloaded_audio)

    progress("Normalizing audio", 40)
    normalized_audio = _normalize_audio_for_transcription(
        downloaded_audio,
        job_dir / "episode.normalized.mp3",
    )

    transcript_text, chunk_count, duration_seconds, chunk_warnings = _transcribe_long_audio(
        normalized_audio,
        job_dir,
        req.include_speakers,
        progress_callback=progress,
    )
    warnings.extend(chunk_warnings)

    readability_formatted = False
    if req.format_readable:
        progress("Formatting transcript", 93)
        transcript_text, readability_formatted = _format_transcript_readable(
            transcript_text,
            req.include_speakers,
        )

    duration_label = (
        f"{duration_seconds:.1f} seconds"
        if isinstance(duration_seconds, (int, float)) and duration_seconds > 0
        else "N/A"
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
        f"- Audio Duration: {duration_label}\n"
        f"- Chunk Size (sec): {DEFAULT_CHUNK_SECONDS}\n"
        f"- Chunk Count: {chunk_count}\n"
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
        audio_duration_seconds=(float(duration_seconds) if duration_seconds is not None else None),
        chunk_count=chunk_count,
        chunk_seconds=DEFAULT_CHUNK_SECONDS,
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
    _recover_stale_jobs()
    _cleanup_old_jobs()
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
        "ffprobe_exists": bool(shutil.which("ffprobe")),
        "discovery_provider": "itunes_search_with_cache",
        "worker_alive": bool(_worker_thread and _worker_thread.is_alive()),
        "queued_jobs": _job_queue.qsize(),
        "active_job_id": _get_active_job_id(),
        "chunk_seconds": DEFAULT_CHUNK_SECONDS,
        "max_episode_seconds": MAX_EPISODE_DURATION_SECONDS,
        "chunk_retry_attempts": CHUNK_TRANSCRIBE_RETRIES,
        "job_retention_days": JOB_RETENTION_DAYS,
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
