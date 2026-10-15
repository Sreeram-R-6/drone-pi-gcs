"""Lifecycle for manual camera clips, independent of flight mission commands."""
from pathlib import Path
import threading
import time

from .recorder import VideoRecorder


class RecordingConflict(RuntimeError):
    pass


class ManualRecording:
    def __init__(self, folder, camera="/dev/video0", recorder_factory=VideoRecorder):
        self.folder = Path(folder)
        self.camera = camera
        self.factory = recorder_factory
        self.lock = threading.Lock()
        self.state = "idle"
        self.error = ""
        self.video = None
        self.started_at = None
        self.worker = None
        self.closed = threading.Event()

    def status(self):
        with self.lock:
            if self.state == "recording" and not self.video.check():
                self.video.stop()
                self.state = "error"
                self.error = "Camera recording stopped unexpectedly. Check the recording log."
            return self._snapshot()

    def _snapshot(self):
        return {"state": self.state, "error": self.error,
                "camera": self.camera, "camera_available": Path(self.camera).exists(),
                "name": self.video.path.name if self.video and self.video.path else None,
                "started_at": self.started_at,
                "elapsed_seconds": max(0, int(time.time() - self.started_at))
                if self.started_at and self.state == "recording" else 0}

    def start(self):
        with self.lock:
            if self.state in {"starting", "recording", "stopping"}:
                raise RecordingConflict("A manual recording is already active.")
            if self.folder.is_symlink():
                raise RuntimeError("Manual recording folder cannot be a symlink.")
            self.video = self.factory(camera=self.camera, output_dir=self.folder, prefix="manual")
            self.state, self.error, self.started_at = "starting", "", None
            self.worker = threading.Thread(target=self._start, daemon=True)
            self.worker.start()
            return self._snapshot()

    def _start(self):
        try:
            self.video.start()
        except Exception as error:
            try:
                self.video.stop()
            except Exception:
                pass
            with self.lock:
                self.state, self.error = "error", str(error)
        else:
            with self.lock:
                self.state, self.started_at = "recording", time.time()
            threading.Thread(target=self._watch, args=(self.video,), daemon=True).start()

    def _watch(self, video):
        # Release the camera lock after a recorder crash even with no browser open.
        while not self.closed.wait(1):
            with self.lock:
                if self.video is not video or self.state != "recording":
                    return
            self.status()

    def stop(self):
        with self.lock:
            if self.state == "starting":
                raise RecordingConflict("Camera is still starting. Try again in a moment.")
            if self.state == "stopping":
                return self._snapshot()
            if self.state != "recording":
                return self._snapshot()
            self.state = "stopping"
            self.worker = threading.Thread(target=self._stop, daemon=True)
            self.worker.start()
            return self._snapshot()

    def _stop(self):
        try:
            success = self.video.stop()
            with self.lock:
                self.state = "idle" if success else "error"
                self.error = "" if success else "Video saved, but recording cleanup reported an error."
        except Exception as error:
            with self.lock:
                self.state, self.error = "error", str(error)

    def close(self):
        # Called after HTTP shutdown. Wait for bounded startup/stop before exit.
        self.closed.set()
        if self.worker:
            self.worker.join(timeout=22)
        if self.video:
            self.video.stop()
