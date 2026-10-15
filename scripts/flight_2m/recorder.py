"""USB camera recording in a separate FFmpeg process, with bounded cleanup."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from uuid import uuid4

DEFAULT_VIDEO_DIR = Path(__file__).resolve().parents[2] / "video"
CAMERA_LOCK_DIR = DEFAULT_VIDEO_DIR / ".camera-locks"


def add_video_arguments(parser):
    parser.add_argument("--camera", default="/dev/video0", help="USB camera V4L2 device")
    parser.add_argument("--video-dir", type=Path, default=DEFAULT_VIDEO_DIR)
    parser.add_argument("--video-size", default="640x480", help="camera resolution, e.g. 1280x720")
    parser.add_argument("--video-fps", type=int, default=30)


class VideoRecorder:
    def __init__(self, camera="/dev/video0", output_dir=DEFAULT_VIDEO_DIR,
                 size="640x480", fps=30, prefix="flight", lock_dir=CAMERA_LOCK_DIR):
        if not re.fullmatch(r"[1-9]\d*x[1-9]\d*", size):
            raise ValueError("Video size must be WIDTHxHEIGHT")
        if not 1 <= fps <= 120:
            raise ValueError("Video FPS must be 1..120")
        if not camera.startswith("/dev/"):
            raise ValueError("Camera must be a local /dev/ V4L2 device")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", prefix):
            raise ValueError("Invalid recording filename prefix")
        self.camera, self.output_dir = camera, Path(output_dir).resolve()
        self.size, self.fps = size, fps
        self.process = None
        self.path = None
        self.log_path = None
        self.log = None
        self.failed = False
        self.prefix = prefix
        self.lock_dir = Path(lock_dir)
        self.camera_lock = None

    @classmethod
    def from_options(cls, options):
        return cls(options.camera, options.video_dir, options.video_size, options.video_fps)

    def command(self, executable):
        return [executable, "-hide_banner", "-nostdin", "-n", "-loglevel", "warning",
                "-f", "v4l2", "-framerate", str(self.fps), "-video_size", self.size,
                "-i", self.camera, "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-threads", "2", "-pix_fmt", "yuv420p", "-flush_packets", "1",
                "-f", "matroska", str(self.path)]

    def start(self, timeout=10):
        if self.process is not None or self.camera_lock is not None:
            raise RuntimeError("Recorder already started")
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(Path(self.camera).resolve()).encode()).hexdigest()
        handle = (self.lock_dir / (key + ".lock")).open("a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("Camera is busy with another recording. Stop that recording first.") from None
        self.camera_lock = handle
        try:
            self._start(timeout)
        except BaseException:
            self.stop()
            raise

    def _start(self, timeout):
        if self.process is not None:
            raise RuntimeError("Recorder already started")
        executable = shutil.which("ffmpeg")
        if not executable:
            raise RuntimeError("FFmpeg is required for USB recording: sudo apt install ffmpeg")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        self.path = self.output_dir / f"{self.prefix}_{stamp}_{uuid4().hex[:8]}.mkv"
        self.log_path = self.path.with_suffix(".log")
        self.camera_lock.seek(0)
        self.camera_lock.truncate()
        json.dump({"path": str(self.path), "pid": os.getpid()}, self.camera_lock)
        self.camera_lock.flush()
        self.log = self.log_path.open("x")
        self.process = subprocess.Popen(
            self.command(executable), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=self.log, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Camera recording failed; see {self.log_path}")
            # Require actual output, not just a live FFmpeg process.
            if self.path.exists() and self.path.stat().st_size > 1024:
                print(f"Recording USB camera to {self.path}", flush=True)
                return
            time.sleep(0.1)
        raise RuntimeError(f"Camera produced no video within {timeout}s; see {self.log_path}")

    def check(self):
        """Report capture failure once; flight control continues independently."""
        if self.process is not None and self.process.poll() is not None and not self.failed:
            self.failed = True
            print(f"VIDEO ERROR: recording stopped unexpectedly; see {self.log_path}",
                  file=sys.stderr, flush=True)
        return not self.failed

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                # SIGINT lets FFmpeg flush the Matroska trailer before exiting.
                for method, timeout in ((lambda: self.process.send_signal(signal.SIGINT), 5),
                                        (self.process.terminate, 2), (self.process.kill, 2)):
                    try:
                        method()
                        self.process.wait(timeout=timeout)
                        break
                    except subprocess.TimeoutExpired:
                        self.failed = True
                    except ProcessLookupError:
                        break
            elif self.process.returncode not in (0, 255, -signal.SIGINT):
                self.failed = True
        if self.log is not None:
            self.log.close()
            self.log = None
        if self.camera_lock is not None:
            self.camera_lock.close()
            self.camera_lock = None
        if self.path is not None:
            print(f"Video file: {self.path}", flush=True)
        if self.failed:
            print(f"VIDEO ERROR: recording ended with an error or forced cleanup; see {self.log_path}",
                  file=sys.stderr, flush=True)
        return not self.failed


def main():
    parser = argparse.ArgumentParser(description="Record USB video only; never connects to the drone.")
    add_video_arguments(parser)
    parser.add_argument("--duration", type=float, default=10, help="recording duration in seconds")
    options = parser.parse_args()
    if not 0 < options.duration < float("inf"):
        parser.error("--duration must be positive and finite")
    recorder = None
    result = 0
    try:
        recorder = VideoRecorder.from_options(options)
        recorder.start()
        deadline = time.monotonic() + options.duration
        while time.monotonic() < deadline:
            if not recorder.check():
                result = 1
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        result = 130
    except (RuntimeError, ValueError, OSError) as error:
        print(f"Video error: {error}", file=sys.stderr)
        result = 1
    finally:
        if recorder is not None and not recorder.stop():
            result = result or 1
    return result
