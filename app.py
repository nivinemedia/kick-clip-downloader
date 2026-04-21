import os
import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL


app = Flask(__name__)

KICK_CLIP_URL_RE = re.compile(
    r"^https?://(?:www\.)?kick\.com/[\w-]+(?:/clips/|/?\?(?:[^#]+&)?clip=)(clip_[\w-]+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
MEDIA_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".ts",
    ".m3u8",
}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


class DownloadError(RuntimeError):
    """Raised when the clip cannot be downloaded or converted."""


def sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("._-")
    return cleaned[:90] or "kick-clip"


def parse_clip_url(raw_url: str) -> tuple[str, str]:
    candidate = (raw_url or "").strip()
    match = KICK_CLIP_URL_RE.match(candidate)
    if not match:
        raise DownloadError(
            "Use um link publico de clip da Kick no formato "
            "https://kick.com/<canal>/clips/clip_<id>."
        )
    return candidate, match.group(1)


def build_download_name(info: dict, clip_id: str) -> str:
    channel = sanitize_filename(info.get("channel") or "kick")
    title = sanitize_filename(info.get("title") or clip_id)
    filename = f"{channel}-{title}-{clip_id}"
    return f"{filename[:140].strip('._-')}.mp4"


def locate_downloaded_file(work_dir: Path, info: dict) -> Path:
    requested_downloads = info.get("requested_downloads") or []
    for item in requested_downloads:
        filepath = item.get("filepath")
        if filepath:
            candidate = Path(filepath)
            if candidate.exists():
                return candidate

    files = [
        path
        for path in work_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in MEDIA_EXTENSIONS
        and not path.name.endswith(".part")
        and path.stat().st_size > 0
    ]
    if not files:
        raise DownloadError("Nao encontrei o arquivo bruto baixado pela Kick.")
    return max(files, key=lambda path: path.stat().st_size)


def download_clip_source(url: str, work_dir: Path) -> tuple[dict, Path]:
    ydl_options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "cachedir": False,
        "concurrent_fragment_downloads": 4,
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": "https://kick.com/",
        },
        "outtmpl": {
            "default": str(work_dir / "%(id)s.%(ext)s"),
        },
    }

    try:
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # pragma: no cover - depends on external service
        raise DownloadError(f"Nao foi possivel baixar o clip da Kick: {exc}") from exc

    return info, locate_downloaded_file(work_dir, info)


def ensure_ffmpeg() -> str:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise DownloadError("ffmpeg nao foi encontrado no sistema.")
    return ffmpeg_path


def run_ffmpeg(command: list[str]) -> None:
    command = [
        ensure_ffmpeg(),
        *command,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise DownloadError(result.stderr.strip() or "Falha ao processar o arquivo com ffmpeg.")


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


def remux_to_mp4(source_path: Path, output_path: Path) -> None:
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
        ]
    )


def transcode_to_h264_mp4(source_path: Path, output_path: Path) -> None:
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
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


def prepare_output_file(source_path: Path, output_path: Path) -> Path:
    media_info = probe_media(source_path)
    if is_fast_path_compatible(media_info, source_path):
        if source_path.suffix.lower() == ".mp4":
            return source_path

        try:
            remux_to_mp4(source_path, output_path)
            return output_path
        except DownloadError:
            pass

    transcode_to_h264_mp4(source_path, output_path)
    return output_path


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))})


@app.post("/api/download")
def download_clip():
    payload = request.get_json(silent=True) or {}
    work_dir = None

    try:
        url, clip_id = parse_clip_url(payload.get("url"))
        work_dir = Path(tempfile.mkdtemp(prefix=f"kick-clip-{clip_id}-"))

        info, source_file = download_clip_source(url, work_dir)
        output_name = build_download_name(info, clip_id)
        output_path = work_dir / output_name

        file_to_send = prepare_output_file(source_file, output_path)
        response = send_file(
            file_to_send,
            mimetype="video/mp4",
            as_attachment=True,
            download_name=output_name,
            max_age=0,
        )
        response.call_on_close(lambda: shutil.rmtree(work_dir, ignore_errors=True))
        return response
    except DownloadError as exc:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": str(exc)}), 400
    except Exception:  # pragma: no cover - defensive fallback
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({"error": "Ocorreu um erro inesperado ao processar o clip."}), 500


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "7860")),
        debug=debug,
        use_reloader=debug,
    )
