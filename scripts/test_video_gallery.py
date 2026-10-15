"""Gallery HTTP and media tests; no camera or aircraft access."""
import http.client
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from video_gallery import GalleryServer
from flight_2m.manual_recording import ManualRecording
from flight_2m.recorder import VideoRecorder


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = GalleryServer(("127.0.0.1", 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def get(self, path, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, headers=headers or {}, body=body)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def post(self, action, authenticated=True):
        headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                   "X-Recording-Token": self.server.recording_token} if authenticated else {}
        return self.get("/api/recording/"+action, "POST", headers)

    def delete(self, name, library="flight", authenticated=True):
        headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                   "X-Recording-Token": self.server.recording_token,
                   "Content-Type": "application/json"} if authenticated else {}
        return self.get("/api/videos/delete", "POST", headers, json.dumps({"name": name, "library": library}))

    def wait_recording(self, state):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = json.loads(self.get("/api/recording")[2])
            if status["state"] == state:
                return status
            if status["state"] == "error" and state != "error":
                self.fail(status["error"])
            time.sleep(.05)
        self.fail(f"Recording did not reach {state}")

    def test_page_and_empty_library(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Flight recordings", body)
        self.assertEqual(json.loads(self.get("/api/videos")[2]), {"videos": []})

    def test_list_filters_logs_cache_and_symlinks(self):
        (self.root / "flight.mkv").write_bytes(b"video")
        (self.root / "flight.log").write_text("private log")
        (self.root / "outside.mp4").symlink_to("/etc/passwd")
        rows = json.loads(self.get("/api/videos")[2])["videos"]
        self.assertEqual([row["name"] for row in rows], ["flight.mkv"])

    def test_range_and_head(self):
        (self.root / "test.mp4").write_bytes(b"0123456789")
        status, headers, body = self.get("/stream?name=test.mp4", headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        status, headers, body = self.get("/stream?name=test.mp4", "HEAD")
        self.assertEqual((status, body, headers["Content-Length"]), (200, b"", "10"))
        self.assertEqual(self.get("/stream?name=test.mp4", headers={"Range": "bytes=-3"})[2], b"789")

    def test_invalid_ranges(self):
        (self.root / "test.mp4").write_bytes(b"0123456789")
        for requested in ("bytes=20-30", "bytes=5-2", "bytes=", "bytes=0-1,3-4", "bytes=-0"):
            with self.subTest(requested=requested):
                self.assertEqual(self.get("/stream?name=test.mp4", headers={"Range": requested})[0], 416)

    def test_path_traversal_and_symlink_rejected(self):
        (self.root / "outside.mp4").symlink_to("/etc/passwd")
        for name in ("../README.md", "/etc/passwd", "outside.mp4", ".web-cache/test.mp4"):
            self.assertEqual(self.get("/download?" + urlencode({"name": name}))[0], 404)

    def test_download_with_special_filename(self):
        name = 'flight #1 "test".mp4'
        (self.root / name).write_bytes(b"clip")
        status, headers, body = self.get("/download?" + urlencode({"name": name}))
        self.assertEqual((status, body), (200, b"clip"))
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

    def test_delete_clip_and_cached_copy_from_correct_library(self):
        manual = self.root / "manual"
        manual.mkdir()
        source = manual / "same.mkv"
        source.write_bytes(b"manual")
        (self.root / "same.mkv").write_bytes(b"flight")
        stat = source.stat()
        key = hashlib.sha256(f"{source}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
        cache = self.root / ".web-cache"
        cache.mkdir()
        cached = cache / (key+".mp4")
        cached.write_bytes(b"copy")
        self.assertEqual(self.delete("same.mkv", "manual")[0], 200)
        self.assertFalse(source.exists())
        self.assertFalse(cached.exists())
        self.assertEqual((self.root / "same.mkv").read_bytes(), b"flight")
        self.assertEqual(self.delete("same.mkv", "manual")[0], 404)

    def test_delete_requires_token_and_rejects_paths(self):
        source = self.root / "keep.mkv"
        source.write_bytes(b"preserve")
        self.assertEqual(self.delete(source.name, authenticated=False)[0], 403)
        for name, library in (("../keep.mkv", "manual"), ("keep.mkv", "../"), (".gitkeep", "flight")):
            self.assertEqual(self.delete(name, library)[0], 404)
        self.assertEqual(source.read_bytes(), b"preserve")

    def test_active_flight_clip_cannot_be_deleted(self):
        source = self.root / "active.mkv"
        source.write_bytes(b"recording")
        locks = self.root / ".camera-locks"
        locks.mkdir()
        with (locks / "camera.lock").open("w+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            json.dump({"path": str(source)}, handle)
            handle.flush()
            self.assertEqual(self.delete(source.name)[0], 409)
        self.assertTrue(source.exists())
        self.assertEqual(self.delete(source.name)[0], 200)

    def test_manual_library_is_separate(self):
        (self.root / "flight.mkv").write_bytes(b"flight")
        (self.root / "manual").mkdir()
        (self.root / "manual" / "manual.mkv").write_bytes(b"manual")
        flight = json.loads(self.get("/api/videos")[2])["videos"]
        manual = json.loads(self.get("/api/videos?library=manual")[2])["videos"]
        self.assertEqual([row["name"] for row in flight], ["flight.mkv"])
        self.assertEqual([row["name"] for row in manual], ["manual.mkv"])
        self.assertEqual(self.get(manual[0]["download"])[2], b"manual")
        self.assertEqual(self.get("/download?library=manual&name=../flight.mkv")[0], 404)
        self.assertEqual(self.get("/api/videos?library=../")[0], 404)

    def test_recording_controls_require_token_and_same_origin(self):
        self.assertEqual(self.post("start", authenticated=False)[0], 403)
        self.assertEqual(self.get("/api/recording/start", "POST", {
            "Origin": "http://other.example", "X-Recording-Token": self.server.recording_token})[0], 403)
        self.assertEqual(json.loads(self.get("/api/recording")[2])["state"], "idle")

    def test_camera_start_failure_reported_without_affecting_flight_files(self):
        class Unavailable:
            path = None
            def __init__(self, **kwargs):
                pass
            def start(self):
                raise RuntimeError("Camera busy with flight recording")
            def stop(self):
                return True
        self.server.recording = ManualRecording(self.root / "manual", recorder_factory=Unavailable)
        (self.root / "flight.mkv").write_bytes(b"preserve")
        self.assertEqual(self.post("start")[0], 202)
        self.assertIn("Camera busy", self.wait_recording("error")["error"])
        self.assertEqual((self.root / "flight.mkv").read_bytes(), b"preserve")

    def test_camera_lock_shared_between_manual_and_flight(self):
        first = VideoRecorder(output_dir=self.root, lock_dir=self.root / "locks")
        second = VideoRecorder(output_dir=self.root / "manual", prefix="manual", lock_dir=self.root / "locks")
        try:
            with patch.object(VideoRecorder, "_start"):
                first.start()
                with self.assertRaisesRegex(RuntimeError, "Camera is busy"):
                    second.start()
                first.stop()
                second.start()
        finally:
            first.stop()
            second.stop()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools missing")
    def test_manual_start_stop_and_playback(self):
        class SyntheticRecorder(VideoRecorder):
            def command(self, executable):
                command = super().command(executable)
                return [executable, "-hide_banner", "-nostdin", "-n", "-loglevel", "warning",
                        "-re", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10"] + command[command.index("-an"):]
        def factory(**kwargs):
            return SyntheticRecorder(**kwargs, lock_dir=self.root / "locks")
        self.server.recording = ManualRecording(self.root / "manual", recorder_factory=factory)
        self.assertEqual(self.post("start")[0], 202)
        status = self.wait_recording("recording")
        self.assertTrue(status["name"].startswith("manual_"))
        self.assertEqual(self.post("start")[0], 409)
        name = status["name"]
        query = urlencode({"library": "manual", "name": name})
        self.assertEqual(self.get("/api/play?"+query)[0], 409)
        self.assertEqual(self.delete(name, "manual")[0], 409)
        self.assertEqual(self.post("stop")[0], 202)
        self.wait_recording("idle")
        self.assertIsNotNone(self.server.recording.video.process.poll())
        self.assertEqual(self.get("/api/play?"+query)[0], 200)
        status, headers, body = self.get("/stream?"+query)
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        self.assertIn(b"ftyp", body[:32])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools missing")
    def test_mkv_prepared_as_playable_mp4(self):
        source = self.root / "flight.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "testsrc2=size=320x240:rate=10", "-t", "1", "-c:v", "libx264",
                        "-threads", "1", str(source)], check=True)
        status, headers, data = self.get("/api/play?name=flight.mkv")
        self.assertEqual(status, 200, data)
        url = json.loads(data)["url"]
        status, headers, data = self.get(url)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertIn(b"ftyp", data[:32])
        cache = self.server.playable(source)
        before = cache.stat().st_mtime_ns
        self.assertEqual(self.get("/api/play?name=flight.mkv")[0], 200)
        self.assertEqual(cache.stat().st_mtime_ns, before)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(cache)],
                               check=True, capture_output=True, text=True)
        self.assertIn("h264", probe.stdout)


if __name__ == "__main__":
    unittest.main()
