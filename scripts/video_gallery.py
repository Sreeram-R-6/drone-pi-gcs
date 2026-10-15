#!/usr/bin/env python3
"""Serve the local video gallery. Uses only Python's standard library."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

from flight_2m.manual_recording import ManualRecording, RecordingConflict

ROOT = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).with_name("video_gallery") / "index.html"
EXTENSIONS = {".mkv", ".mp4", ".webm", ".mov"}


class GalleryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, video_dir, camera="/dev/video0"):
        self.video_dir = Path(video_dir).resolve()
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.conversion_lock = threading.Lock()
        self.recording = ManualRecording(self.video_dir / "manual", camera)
        self.recording_token = secrets.token_urlsafe(32)
        super().__init__(address, Handler)

    def server_close(self):
        super().server_close()
        self.recording.close()

    def folder(self, library):
        if library not in {"flight", "manual"}:
            raise FileNotFoundError("Unknown video library")
        folder = self.video_dir if library == "flight" else self.video_dir / "manual"
        if folder.is_symlink():
            raise FileNotFoundError("Invalid video directory")
        return folder

    def source(self, name, library="flight"):
        if not name or Path(name).name != name or name.startswith("."):
            raise FileNotFoundError("Unknown video")
        path = self.folder(library) / name
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise FileNotFoundError("Unknown video")
        return path

    def playable(self, source):
        if source.suffix.lower() in {".mp4", ".webm"}:
            return source
        stat = source.stat()
        key = hashlib.sha256(f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
        cache = self.video_dir / ".web-cache"
        with self.conversion_lock:
            cache.mkdir(exist_ok=True)
            if cache.is_symlink():
                raise RuntimeError("Invalid video cache directory")
            target = cache / (key + ".mp4")
            if target.is_file() and not target.is_symlink():
                return target
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg is None:
                raise RuntimeError("Install FFmpeg to play MKV recordings: sudo apt install ffmpeg")
            temporary = cache / (key + "." + uuid.uuid4().hex + ".mp4")
            try:
                result = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                     "-i", str(source), "-map", "0:v:0", "-c:v", "copy",
                     "-movflags", "+faststart", str(temporary)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120,
                )
                if result.returncode or not temporary.exists() or not temporary.stat().st_size:
                    raise RuntimeError("Video is not ready for playback. Wait for recording to finish, then retry. You can still download the original.")
                temporary.replace(target)
                return target
            finally:
                temporary.unlink(missing_ok=True)

    def delete_clip(self, name, library):
        # Serializes deletion with browser-copy preparation and keeps requests
        # restricted to an existing video in one of the two known libraries.
        with self.conversion_lock, self.recording.lock:
            source = self.source(name, library)
            current = self.recording._snapshot()
            if library == "manual" and current["name"] == name and current["state"] in {"starting", "recording", "stopping"}:
                raise RecordingConflict("Stop this recording before deleting it.")
            for path in (self.video_dir / ".camera-locks").glob("*.lock"):
                if path.is_symlink():
                    continue
                with path.open("r") as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        try:
                            info = json.load(handle)
                        except (ValueError, OSError):
                            raise RecordingConflict("A camera recording is active. Try deleting after it stops.") from None
                        if info.get("path") == str(source):
                            raise RecordingConflict("Stop this recording before deleting it.")
            stat = source.stat()
            key = hashlib.sha256(f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
            source.unlink()
            cache = self.video_dir / ".web-cache"
            if not cache.is_symlink():
                (cache / (key + ".mp4")).unlink(missing_ok=True)
            return {"deleted": name, "library": library}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        if urlsplit(self.path).path not in {"/api/recording", "/api/videos"}:
            super().log_message(format, *args)

    def do_POST(self):
        # A per-process token and Origin check protect camera actions, including
        # requests arriving through the existing Tailscale TCP forwarding.
        expected_origin = "http://" + self.headers.get("Host", "")
        if (self.headers.get("Origin") != expected_origin or
                not secrets.compare_digest(self.headers.get("X-Recording-Token", ""),
                                           self.server.recording_token)):
            self.json({"error": "Reload this page before using clip controls."}, 403)
            return
        try:
            if self.path == "/api/recording/start":
                self.json(self.server.recording.start(), 202)
            elif self.path == "/api/recording/stop":
                self.json(self.server.recording.stop(), 202)
            elif self.path == "/api/videos/delete":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Invalid delete request")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not isinstance(data.get("library"), str):
                    raise ValueError("Select a clip to delete")
                self.json(self.server.delete_clip(data["name"], data["library"]))
            else:
                self.json({"error": "Not found"}, 404)
        except RecordingConflict as error:
            self.json({"error": str(error)}, 409)
        except FileNotFoundError:
            self.json({"error": "Video not found"}, 404)
        except (ValueError, UnicodeDecodeError) as error:
            self.json({"error": str(error)}, 400)
        except (OSError, RuntimeError) as error:
            self.json({"error": str(error)}, 503)

    def do_HEAD(self):
        self.do_GET()

    def send_data(self, data, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, value, status=200):
        self.send_data(json.dumps(value).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                page = PAGE.read_text().replace("__RECORDING_TOKEN__", self.server.recording_token)
                self.send_data(page.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/recording":
                self.json(self.server.recording.status())
            elif url.path == "/api/videos":
                library = query.get("library", ["flight"])[0]
                rows = []
                folder = self.server.folder(library)
                for path in folder.iterdir() if folder.exists() else []:
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                        continue
                    stat = path.stat()
                    rows.append({"name": path.name, "size": stat.st_size,
                                 "modified": stat.st_mtime,
                                 "download": "/download?" + urlencode({"name": path.name, "library": library})})
                self.json({"videos": sorted(rows, key=lambda row: row["modified"], reverse=True)})
            elif url.path in {"/api/play", "/stream", "/download"}:
                name = query.get("name", [""])[0]
                library = query.get("library", ["flight"])[0]
                source = self.server.source(name, library)
                current = self.server.recording.status()
                if library == "manual" and current["name"] == name and current["state"] in {"starting", "recording", "stopping"}:
                    self.json({"error": "Stop the manual recording before playback or download."}, 409)
                    return
                if url.path == "/api/play":
                    self.server.playable(source)
                    self.json({"url": "/stream?" + urlencode({"name": name, "library": library})})
                elif url.path == "/download":
                    self.stream(source, download=True)
                else:
                    self.stream(self.server.playable(source))
            else:
                self.json({"error": "Not found"}, 404)
        except FileNotFoundError:
            self.json({"error": "Video not found"}, 404)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            self.json({"error": str(error)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.json({"error": "Unable to read video files"}, 500)

    def stream(self, path, download=False):
        # One open descriptor keeps length/range handling consistent during reads.
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start, end = 0, size - 1
            requested = self.headers.get("Range")
            if requested:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                valid = bool(match and any(match.groups()))
                if valid:
                    first, last = match.groups()
                    if first:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    else:
                        start = max(0, size - int(last))
                    valid = 0 <= start <= end < size
                if not valid:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            self.send_response(206 if requested else 200)
            content_type = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}.get(path.suffix.lower(), "video/quicktime")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                from urllib.parse import quote
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == "HEAD":
                return
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--video-dir", type=Path, default=ROOT / "video")
    parser.add_argument("--camera", default="/dev/video0", help="USB camera used for manual recordings")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("--port must be 1..65535")
    def stop_service(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_service)
    with GalleryServer(("127.0.0.1", options.port), options.video_dir, options.camera) as server:
        print(f"Video gallery: http://localhost:{server.server_port}", flush=True)
        print(f"Recordings: {server.video_dir}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
