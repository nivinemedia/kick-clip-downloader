import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL


app = Flask(__name__)

KICK_CLIP_URL_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/[\w-]+(?:/clips/|/?\?(?:[^#]+&)?clip=)(clip_[\w-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
KICK_VOD_URL_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/[\w-]+/videos/(?P<id>[\da-f]{8}-(?:[\da-f]{4}-){3}[\da-f]{12})(?:[/?#].*)?$",
    re.IGNORECASE,
)
YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:(?:www|m|music)\.)?youtube\.com/(?:watch\?.*v=|shorts/|live/|embed/)[\w-]+.*$|^https?://youtu\.be/[\w-]+.*$",
    re.IGNORECASE,
)
YOUTUBE_ID_RE = re.compile(
    r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&#/]|$)",
    re.IGNORECASE,
)

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".ts",
    ".m3u8",
}
AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".flac",
    ".webm",
}
DOWNLOAD_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
AUDIO_LABEL_PREFIXES = {
    "none": "",
    "sfx": "[SFX] ",
    "music": "[MUSIC] ",
}
YOUTUBE_TRANSIENT_ERROR_MARKERS = (
    "unexpected_eof_while_reading",
    "eof occurred in violation of protocol",
    "unable to download api page",
    "ssl:",
    "transporterror",
    "connection reset",
    "remote end closed connection",
    "timed out",
    "temporarily unavailable",
    "bad handshake",
)
DOWNLOAD_JOBS: dict[str, dict] = {}
DOWNLOAD_JOBS_LOCK = threading.Lock()
JOB_TTL_SECONDS = 60 * 60


class DownloadError(RuntimeError):
    """Raised when the requested media cannot be downloaded or converted."""


def current_timestamp() -> float:
    return time.time()


def cleanup_job_directory(job: dict | None) -> None:
    if not job:
        return
    work_dir = job.get("work_dir")
    if work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
        job["work_dir"] = None


def cleanup_expired_jobs() -> None:
    expired_jobs: list[str] = []
    current_time = current_timestamp()

    with DOWNLOAD_JOBS_LOCK:
        for job_id, job in DOWNLOAD_JOBS.items():
            if current_time - job.get("updated_at", current_time) > JOB_TTL_SECONDS:
                expired_jobs.append(job_id)

    for job_id in expired_jobs:
        release_job(job_id)


def create_job(
    status: str = "queued",
    progress: int = 0,
    message: str = "Preparando o download...",
) -> dict:
    cleanup_expired_jobs()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "status": status,
        "progress": progress,
        "message": message,
        "created_at": current_timestamp(),
        "updated_at": current_timestamp(),
        "work_dir": None,
        "output_path": None,
        "output_name": None,
        "mimetype": None,
        "error": None,
    }
    with DOWNLOAD_JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = job
    return job


def get_job(job_id: str) -> dict | None:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def update_job(job_id: str, **fields) -> dict | None:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return None

        if "progress" in fields:
            current_progress = int(job.get("progress", 0))
            fields["progress"] = max(current_progress, int(fields["progress"]))

        job.update(fields)
        job["updated_at"] = current_timestamp()
        return dict(job)


def set_job_progress(job_id: str, progress: int, message: str | None = None) -> None:
    payload = {"progress": progress}
    if message is not None:
        payload["message"] = message
    update_job(job_id, **payload)


def release_job(job_id: str) -> None:
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.pop(job_id, None)
    cleanup_job_directory(job)


def serialize_job(job: dict) -> dict:
    response = {
        "id": job["id"],
        "status": job["status"],
        "progress": int(job.get("progress", 0)),
        "message": job.get("message"),
        "error": job.get("error"),
    }
    if job.get("status") == "done":
        response["download_url"] = f"/api/jobs/{job['id']}/file"
        response["filename"] = job.get("output_name")
    return response


def sanitize_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("._-")
    return cleaned[:90] or "midia"


def sanitize_display_filename(value: str, fallback: str) -> str:
    candidate = (value or fallback or "").strip()
    candidate = re.sub(r"[\x00-\x1f]", "", candidate)
    candidate = candidate.replace("/", " - ").replace("\\", " - ")
    candidate = re.sub(r'[<>:"|?*]+', "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    return (candidate[:180] or fallback or "midia").strip()


def extract_youtube_id(url: str) -> str:
    match = YOUTUBE_ID_RE.search(url or "")
    if match:
        return match.group(1)
    return "youtube"


def parse_media_url(raw_url: str) -> dict:
    candidate = (raw_url or "").strip()

    clip_match = KICK_CLIP_URL_RE.match(candidate)
    if clip_match:
        return {
            "platform": "kick",
            "kind": "clip",
            "url": candidate,
            "id": clip_match.group(1),
        }

    vod_match = KICK_VOD_URL_RE.match(candidate)
    if vod_match:
        return {
            "platform": "kick",
            "kind": "vod",
            "url": candidate,
            "id": vod_match.group("id"),
        }

    if YOUTUBE_URL_RE.match(candidate):
        return {
            "platform": "youtube",
            "kind": "youtube",
            "url": candidate,
            "id": extract_youtube_id(candidate),
        }

    raise DownloadError(
        "Use um link publico de clip ou VOD da Kick, ou um video do YouTube. "
        "Exemplos: https://kick.com/<canal>/clips/clip_<id>, "
        "https://kick.com/<canal>/videos/<id> ou https://www.youtube.com/watch?v=<id>."
    )


def format_seconds_for_filename(value: float) -> str:
    total_seconds = max(int(value), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def format_seconds_for_display(value: float) -> str:
    total_seconds = max(int(value), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def parse_timecode(raw_value: str, label: str) -> float:
    candidate = (raw_value or "").strip()
    if not candidate:
        raise DownloadError(f"Informe o {label} do recorte do VOD.")

    if re.fullmatch(r"\d+(?:\.\d+)?", candidate):
        return float(candidate)

    parts = candidate.split(":")
    if not 1 <= len(parts) <= 3:
        raise DownloadError(f"Formato invalido para {label}. Use HH:MM:SS, MM:SS ou segundos.")

    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise DownloadError(f"Formato invalido para {label}. Use HH:MM:SS, MM:SS ou segundos.") from exc

    if any(number < 0 for number in numbers):
        raise DownloadError(f"O {label} nao pode ser negativo.")

    total = 0.0
    for number in numbers:
        total = total * 60 + number
    return total


def parse_vod_range(payload: dict, source: dict) -> tuple[float | None, float | None]:
    if source["platform"] != "kick" or source["kind"] != "vod":
        return None, None

    start_raw = payload.get("start_time")
    end_raw = payload.get("end_time")
    if not start_raw or not end_raw:
        raise DownloadError("Para baixar VOD por tempo, informe inicio e fim do trecho.")

    start_time = parse_timecode(start_raw, "inicio")
    end_time = parse_timecode(end_raw, "fim")

    if end_time <= start_time:
        raise DownloadError("O fim do trecho precisa ser maior do que o inicio.")

    return start_time, end_time


def parse_youtube_preferences(payload: dict) -> dict:
    download_mode = (payload.get("download_mode") or "video").strip().lower()
    if download_mode not in {"video", "audio"}:
        raise DownloadError("Escolha se voce quer baixar video + audio ou so audio do YouTube.")

    if download_mode == "video":
        video_format = (payload.get("video_format") or "mp4").strip().lower()
        if video_format != "mp4":
            raise DownloadError("No YouTube com video, o formato disponivel aqui e MP4 em H.264.")
        return {
            "download_mode": "video",
            "video_format": "mp4",
            "audio_format": None,
            "audio_label": "none",
        }

    audio_format = (payload.get("audio_format") or "mp3").strip().lower()
    if audio_format not in {"mp3", "wav"}:
        raise DownloadError("No YouTube com audio, escolha MP3 ou WAV.")

    audio_label = (payload.get("audio_label") or "none").strip().lower()
    if audio_label not in AUDIO_LABEL_PREFIXES:
        raise DownloadError("Escolha um rotulo valido para o audio do YouTube.")

    return {
        "download_mode": "audio",
        "video_format": None,
        "audio_format": audio_format,
        "audio_label": audio_label,
    }


def merge_ydl_options(base: dict, extra: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def get_youtube_ydl_fallbacks() -> list[dict]:
    common = {
        "source_address": "0.0.0.0",
        "socket_timeout": 10,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
    }
    return [
        common,
        merge_ydl_options(
            common,
            {
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android"],
                    }
                },
            },
        ),
        merge_ydl_options(
            common,
            {
                "extractor_args": {
                    "youtube": {
                        "player_client": ["ios"],
                    }
                },
            },
        ),
        merge_ydl_options(
            common,
            {
                "extractor_args": {
                    "youtube": {
                        "player_client": ["web"],
                    }
                },
            },
        ),
    ]


def is_transient_youtube_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in YOUTUBE_TRANSIENT_ERROR_MARKERS)


def build_youtube_error_message(action: str, exc: Exception) -> str:
    if is_transient_youtube_error(exc):
        return (
            f"Nao foi possivel {action} do YouTube agora. "
            "O YouTube respondeu de forma instavel. Tente novamente em alguns segundos."
        )
    return f"Nao foi possivel {action} do YouTube: {exc}"


def extract_youtube_info_with_fallbacks(source_url: str, base_options: dict, download: bool) -> dict:
    last_error = None

    for index, extra in enumerate(get_youtube_ydl_fallbacks()):
        ydl_options = merge_ydl_options(base_options, extra)
        attempts = 2 if index == 0 else 1

        for attempt in range(1, attempts + 1):
            try:
                with YoutubeDL(ydl_options) as ydl:
                    return ydl.extract_info(source_url, download=download)
            except Exception as exc:  # pragma: no cover - depends on external service
                last_error = exc
                if attempt < attempts and is_transient_youtube_error(exc):
                    time.sleep(min(attempt, 1))
                    continue
                break

    if last_error is None:
        raise RuntimeError("Falha desconhecida ao ler o YouTube.")
    raise last_error


def get_youtube_video_selector() -> str:
    return (
        "bestvideo[vcodec~='^(avc1|h264)'][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[vcodec~='^(avc1|h264)'][ext=mp4]+bestaudio/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo*+bestaudio/best"
    )


def build_download_progress_hook(job_id: str, message: str, progress_start: int, progress_end: int):
    def hook(progress: dict) -> None:
        status = progress.get("status")
        if status == "downloading":
            downloaded = progress.get("downloaded_bytes") or 0
            total = progress.get("total_bytes") or progress.get("total_bytes_estimate") or 0
            if total > 0:
                ratio = min(downloaded / total, 1)
                target_progress = progress_start + int((progress_end - progress_start) * ratio)
            else:
                target_progress = progress_start + max((progress_end - progress_start) // 5, 1)
            set_job_progress(job_id, target_progress, message)
        elif status == "finished":
            set_job_progress(job_id, progress_end, "Finalizando os arquivos baixados...")

    return hook


def get_headers_for_platform(platform: str) -> dict:
    if platform == "kick":
        return {"Referer": "https://kick.com/"}
    if platform == "youtube":
        return {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.youtube.com/",
        }
    return {}


def build_ydl_options(source: dict, work_dir: Path | None = None) -> dict:
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": False,
        "retries": 3,
        "fragment_retries": 3,
        "cachedir": False,
        "concurrent_fragment_downloads": 4,
        "http_headers": get_headers_for_platform(source["platform"]),
    }
    if work_dir is not None:
        ydl_options["outtmpl"] = {
            "default": str(work_dir / "%(title)s [%(id)s].%(ext)s"),
        }
    return ydl_options


def fetch_media_metadata(source: dict) -> dict:
    options = build_ydl_options(source)

    if source["platform"] == "youtube":
        try:
            info = extract_youtube_info_with_fallbacks(source["url"], options, download=False)
        except Exception as exc:  # pragma: no cover - depends on external service
            raise DownloadError(build_youtube_error_message("ler os dados", exc)) from exc
    else:
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(source["url"], download=False)
        except Exception as exc:  # pragma: no cover - depends on external service
            raise DownloadError(f"Nao foi possivel ler os dados do Kick: {exc}") from exc

    if info.get("_type") == "playlist":
        raise DownloadError("Esse link parece ser uma playlist. Cole o link de um unico video.")

    return info


def locate_downloaded_file(work_dir: Path, info: dict) -> Path:
    candidate_paths: list[Path] = []

    for key in ("filepath", "_filename"):
        filepath = info.get(key)
        if filepath:
            candidate_paths.append(Path(filepath))

    for key in ("requested_downloads", "requested_formats"):
        for item in info.get(key) or []:
            filepath = item.get("filepath")
            if filepath:
                candidate_paths.append(Path(filepath))

    for candidate in candidate_paths:
        if candidate.exists() and candidate.is_file():
            return candidate

    files = [
        path
        for path in work_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in DOWNLOAD_EXTENSIONS
        and not path.name.endswith(".part")
        and path.stat().st_size > 0
    ]
    if not files:
        raise DownloadError("Nao encontrei o arquivo bruto baixado.")
    return max(files, key=lambda path: (path.stat().st_mtime, path.stat().st_size))


def download_kick_source(
    source: dict,
    work_dir: Path,
    start_time: float | None = None,
    end_time: float | None = None,
    job_id: str | None = None,
) -> tuple[dict, Path]:
    ydl_options = build_ydl_options(source, work_dir)
    if job_id:
        ydl_options["progress_hooks"] = [
            build_download_progress_hook(job_id, "Baixando da Kick...", 10, 72)
        ]

    if start_time is not None and end_time is not None:
        ydl_options.update(
            {
                "download_ranges": lambda *_: (
                    {
                        "start_time": start_time,
                        "end_time": end_time,
                    },
                ),
                "force_keyframes_at_cuts": True,
                "external_downloader": {
                    "m3u8": "ffmpeg",
                    "https": "ffmpeg",
                    "http": "ffmpeg",
                },
            }
        )

    try:
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(source["url"], download=True)
    except Exception as exc:  # pragma: no cover - depends on external service
        raise DownloadError(f"Nao foi possivel baixar a midia da Kick: {exc}") from exc

    return info, locate_downloaded_file(work_dir, info)


def download_youtube_source(
    source: dict,
    work_dir: Path,
    preferences: dict,
    job_id: str | None = None,
) -> tuple[dict, Path]:
    base_options = build_ydl_options(source, work_dir)
    if job_id:
        base_options["progress_hooks"] = [
            build_download_progress_hook(job_id, "Baixando do YouTube...", 10, 72)
        ]
    if preferences["download_mode"] == "video":
        base_options.update(
            {
                "format": get_youtube_video_selector(),
                "merge_output_format": "mp4",
            }
        )
    else:
        base_options.update(
            {
                "format": "bestaudio/best",
            }
        )

    try:
        info = extract_youtube_info_with_fallbacks(source["url"], base_options, download=True)
    except Exception as exc:  # pragma: no cover - depends on external service
        raise DownloadError(build_youtube_error_message("baixar a midia", exc)) from exc

    return info, locate_downloaded_file(work_dir, info)


def ensure_ffmpeg() -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise DownloadError("ffmpeg nao foi encontrado no sistema.")
    return ffmpeg_path


def run_ffmpeg(
    command: list[str],
    job_id: str | None = None,
    message: str | None = None,
    progress_start: int | None = None,
    progress_end: int | None = None,
    duration_seconds: float | None = None,
) -> None:
    tracked_progress = (
        job_id is not None
        and progress_start is not None
        and progress_end is not None
        and duration_seconds is not None
        and duration_seconds > 0
    )

    if not tracked_progress:
        full_command = [ensure_ffmpeg(), *command]
        result = subprocess.run(full_command, capture_output=True, text=True)
        if result.returncode != 0:
            raise DownloadError(result.stderr.strip() or "Falha ao processar o arquivo com ffmpeg.")
        if job_id and progress_end is not None:
            set_job_progress(job_id, progress_end, message)
        return

    full_command = [ensure_ffmpeg(), "-progress", "pipe:1", "-nostats", *command]
    process = subprocess.Popen(
        full_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    out_time_seconds = 0.0
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.strip()
        if line.startswith("out_time_us="):
            out_time_seconds = float(line.split("=", 1)[1]) / 1_000_000
        elif line.startswith("out_time_ms="):
            out_time_seconds = float(line.split("=", 1)[1]) / 1_000_000
        else:
            continue

        ratio = min(max(out_time_seconds / duration_seconds, 0), 1)
        target_progress = progress_start + int((progress_end - progress_start) * ratio)
        set_job_progress(job_id, target_progress, message)

    stderr_output = process.stderr.read() if process.stderr is not None else ""
    process.wait()
    if process.returncode != 0:
        raise DownloadError(stderr_output.strip() or "Falha ao processar o arquivo com ffmpeg.")
    set_job_progress(job_id, progress_end, message)


def probe_media(source_path: Path) -> dict:
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        raise DownloadError("ffprobe nao foi encontrado no sistema.")

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(source_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise DownloadError("Nao foi possivel analisar o arquivo baixado.")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError("Nao foi possivel interpretar os dados do arquivo baixado.") from exc


def get_media_duration(media_info: dict) -> float:
    try:
        return float((media_info.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def is_fast_path_compatible(media_info: dict, source_path: Path) -> bool:
    format_name = (media_info.get("format") or {}).get("format_name") or ""
    streams = media_info.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)

    if not video_stream or video_stream.get("codec_name") != "h264":
        return False

    if audio_stream and audio_stream.get("codec_name") not in {"aac", "mp3"}:
        return False

    if source_path.suffix.lower() == ".mp4" and "mp4" in format_name:
        return True

    return audio_stream is None or audio_stream.get("codec_name") in {"aac", "mp3"}


def remux_to_mp4(source_path: Path, output_path: Path, job_id: str | None = None, duration_seconds: float | None = None) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        job_id=job_id,
        message="Empacotando o video em MP4...",
        progress_start=78,
        progress_end=94,
        duration_seconds=duration_seconds,
    )


def transcode_to_h264_mp4(source_path: Path, output_path: Path, job_id: str | None = None, duration_seconds: float | None = None) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ],
        job_id=job_id,
        message="Convertendo para MP4 H.264...",
        progress_start=78,
        progress_end=96,
        duration_seconds=duration_seconds,
    )


def transcode_to_mp3(source_path: Path, output_path: Path, job_id: str | None = None, duration_seconds: float | None = None) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "0",
            str(output_path),
        ],
        job_id=job_id,
        message="Convertendo audio para MP3...",
        progress_start=78,
        progress_end=96,
        duration_seconds=duration_seconds,
    )


def transcode_to_wav(source_path: Path, output_path: Path, job_id: str | None = None, duration_seconds: float | None = None) -> None:
    run_ffmpeg(
        [
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        job_id=job_id,
        message="Convertendo audio para WAV...",
        progress_start=78,
        progress_end=96,
        duration_seconds=duration_seconds,
    )


def prepare_video_output_file(source_path: Path, output_path: Path, job_id: str | None = None) -> Path:
    media_info = probe_media(source_path)
    duration_seconds = get_media_duration(media_info)
    if is_fast_path_compatible(media_info, source_path):
        if source_path.suffix.lower() == ".mp4":
            if job_id:
                set_job_progress(job_id, 96, "Arquivo pronto para finalizar.")
            return source_path

        try:
            remux_to_mp4(source_path, output_path, job_id=job_id, duration_seconds=duration_seconds)
            return output_path
        except DownloadError:
            pass

    transcode_to_h264_mp4(source_path, output_path, job_id=job_id, duration_seconds=duration_seconds)
    return output_path


def prepare_audio_output_file(
    source_path: Path,
    output_path: Path,
    audio_format: str,
    job_id: str | None = None,
) -> Path:
    media_info = probe_media(source_path)
    duration_seconds = get_media_duration(media_info)

    if audio_format == "mp3":
        if source_path.suffix.lower() == ".mp3":
            shutil.copy2(source_path, output_path)
            if job_id:
                set_job_progress(job_id, 96, "Arquivo pronto para finalizar.")
            return output_path
        transcode_to_mp3(source_path, output_path, job_id=job_id, duration_seconds=duration_seconds)
        return output_path

    if audio_format == "wav":
        transcode_to_wav(source_path, output_path, job_id=job_id, duration_seconds=duration_seconds)
        return output_path

    raise DownloadError("Formato de audio nao suportado.")


def build_kick_output_name(
    info: dict,
    source: dict,
    start_time: float | None = None,
    end_time: float | None = None,
) -> str:
    title = sanitize_display_filename(info.get("title") or source["id"], source["id"])

    if source["kind"] == "clip":
        return f"{sanitize_display_filename(f'[CLIP] {title}', title)}.mp4"

    channel = sanitize_display_filename(info.get("channel") or "Kick", "Kick")
    base_name = f"{channel} - {title}"
    if start_time is not None and end_time is not None:
        base_name = (
            f"{base_name} - "
            f"{format_seconds_for_filename(start_time)}-to-{format_seconds_for_filename(end_time)}"
        )
    return f"{sanitize_display_filename(base_name, source['id'])}.mp4"


def build_youtube_output_name(info: dict, preferences: dict) -> str:
    title = sanitize_display_filename(info.get("title") or "YouTube", "YouTube")

    if preferences["download_mode"] == "video":
        return f"{title}.mp4"

    prefix = AUDIO_LABEL_PREFIXES[preferences["audio_label"]]
    base_name = sanitize_display_filename(f"{prefix}{title}", title)
    return f"{base_name}.{preferences['audio_format']}"


def build_media_info(source: dict, info: dict) -> dict:
    title = info.get("title") or source["id"]
    channel = info.get("channel") or info.get("uploader") or source["platform"].title()
    duration = int(info.get("duration") or 0)

    if source["platform"] == "kick" and source["kind"] == "clip":
        return {
            "platform": "kick",
            "kind": "clip",
            "id": source["id"],
            "title": title,
            "channel": channel,
            "duration": duration,
            "duration_label": format_seconds_for_display(duration) if duration else None,
            "download_prefix": "[CLIP]",
        }

    if source["platform"] == "kick" and source["kind"] == "vod":
        if duration <= 0:
            raise DownloadError("Nao foi possivel identificar a duracao desse VOD.")
        return {
            "platform": "kick",
            "kind": "vod",
            "id": source["id"],
            "title": title,
            "channel": channel,
            "duration": duration,
            "duration_label": format_seconds_for_display(duration),
        }

    return {
        "platform": "youtube",
        "kind": "youtube",
        "id": source["id"],
        "title": title,
        "channel": channel,
        "duration": duration,
        "duration_label": format_seconds_for_display(duration) if duration else None,
        "video_format": "mp4",
        "audio_formats": ["mp3", "wav"],
        "audio_labels": ["none", "sfx", "music"],
    }


def get_mimetype_for_extension(extension: str) -> str:
    if extension == ".mp4":
        return "video/mp4"
    if extension == ".mp3":
        return "audio/mpeg"
    if extension == ".wav":
        return "audio/wav"
    return "application/octet-stream"


def build_download_task(payload: dict) -> dict:
    source = parse_media_url(payload.get("url"))
    task = {"source": source}

    if source["platform"] == "kick":
        start_time, end_time = parse_vod_range(payload, source)
        task["start_time"] = start_time
        task["end_time"] = end_time
    else:
        task["preferences"] = parse_youtube_preferences(payload)

    return task


def get_initial_job_message(task: dict) -> str:
    source = task["source"]
    if source["platform"] == "kick" and source["kind"] == "vod":
        return "Conectando ao VOD da Kick..."
    if source["platform"] == "kick":
        return "Conectando ao clip da Kick..."
    return "Conectando ao YouTube..."


def run_download_job(job_id: str, task: dict) -> None:
    work_dir = None

    try:
        source = task["source"]
        update_job(job_id, status="running", progress=6, message=get_initial_job_message(task))
        work_dir = Path(tempfile.mkdtemp(prefix=f"{source['platform']}-{source['id']}-"))
        update_job(job_id, work_dir=str(work_dir), progress=9)

        if source["platform"] == "kick":
            start_time = task["start_time"]
            end_time = task["end_time"]
            set_job_progress(job_id, 12, "Baixando da Kick...")
            info, source_file = download_kick_source(
                source,
                work_dir,
                start_time,
                end_time,
                job_id=job_id,
            )
            output_name = build_kick_output_name(info, source, start_time, end_time)
            output_path = work_dir / output_name
            file_to_send = prepare_video_output_file(source_file, output_path, job_id=job_id)
            mimetype = get_mimetype_for_extension(".mp4")
        else:
            preferences = task["preferences"]
            set_job_progress(job_id, 12, "Buscando a melhor qualidade no YouTube...")
            info, source_file = download_youtube_source(
                source,
                work_dir,
                preferences,
                job_id=job_id,
            )
            output_name = build_youtube_output_name(info, preferences)
            output_path = work_dir / output_name

            if preferences["download_mode"] == "video":
                file_to_send = prepare_video_output_file(source_file, output_path, job_id=job_id)
            else:
                file_to_send = prepare_audio_output_file(
                    source_file,
                    output_path,
                    preferences["audio_format"],
                    job_id=job_id,
                )
            mimetype = get_mimetype_for_extension(Path(output_name).suffix.lower())

        update_job(
            job_id,
            status="done",
            progress=100,
            message="Arquivo pronto para baixar.",
            output_path=str(file_to_send),
            output_name=output_name,
            mimetype=mimetype,
        )
    except DownloadError as exc:
        cleanup_job_directory({"work_dir": str(work_dir)} if work_dir else None)
        update_job(
            job_id,
            status="error",
            message=str(exc),
            error=str(exc),
            work_dir=None,
        )
    except Exception:
        cleanup_job_directory({"work_dir": str(work_dir)} if work_dir else None)
        update_job(
            job_id,
            status="error",
            message="Ocorreu um erro inesperado ao processar a midia.",
            error="Ocorreu um erro inesperado ao processar a midia.",
            work_dir=None,
        )


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))})


@app.post("/api/media-info")
def media_info():
    payload = request.get_json(silent=True) or {}

    try:
        source = parse_media_url(payload.get("url"))
        info = fetch_media_metadata(source)
        return jsonify(build_media_info(source, info))
    except DownloadError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # pragma: no cover - defensive fallback
        return jsonify({"error": "Ocorreu um erro inesperado ao ler a midia."}), 500


@app.post("/api/vod-info")
def vod_info():
    payload = request.get_json(silent=True) or {}

    try:
        source = parse_media_url(payload.get("url"))
        if source["platform"] != "kick" or source["kind"] != "vod":
            raise DownloadError("Cole um link completo de VOD da Kick para liberar o recorte.")
        info = fetch_media_metadata(source)
        return jsonify(build_media_info(source, info))
    except DownloadError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:  # pragma: no cover - defensive fallback
        return jsonify({"error": "Ocorreu um erro inesperado ao ler o VOD."}), 500


@app.post("/api/download")
def download_media():
    payload = request.get_json(silent=True) or {}
    try:
        task = build_download_task(payload)
    except DownloadError as exc:
        return jsonify({"error": str(exc)}), 400

    job = create_job(
        status="running",
        progress=4,
        message=get_initial_job_message(task),
    )
    threading.Thread(target=run_download_job, args=(job["id"], task), daemon=True).start()
    return jsonify(serialize_job(job)), 202


@app.get("/api/jobs/<job_id>")
def get_download_job(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Esse download nao existe mais."}), 404
    return jsonify(serialize_job(job))


@app.get("/api/jobs/<job_id>/file")
def download_job_file(job_id: str):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Esse download nao existe mais."}), 404

    if job.get("status") != "done" or not job.get("output_path"):
        return jsonify({"error": "O arquivo ainda nao ficou pronto."}), 409

    output_path = Path(job["output_path"])
    if not output_path.exists():
        release_job(job_id)
        return jsonify({"error": "O arquivo nao esta mais disponivel."}), 404

    update_job(job_id, message="Arquivo pronto para baixar.")

    return send_file(
        output_path,
        mimetype=job.get("mimetype") or "application/octet-stream",
        as_attachment=True,
        download_name=job.get("output_name") or output_path.name,
        max_age=0,
    )


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "7860")),
        debug=debug,
        use_reloader=debug,
    )
