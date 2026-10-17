"""Allowlisted flight jobs, one-time launch tickets and persistent run output."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "drone_project/pi_flight/.venv/bin/python"


def save(path, value):
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class RunConflict(RuntimeError):
    pass


class ScriptRuns:
    def __init__(self, folder=None, spawn=subprocess.Popen):
        self.folder = Path(folder or ROOT / "video/.script-runs")
        self.spawn = spawn
        self.lock = threading.Lock()
        self.tickets = {}

    def prepare(self, script, record_video):
        if script != "flight_2m" or type(record_video) is not bool:
            raise ValueError("Select the supported 2 m flight script")
        with self.lock:
            token = secrets.token_urlsafe(32)
            self.tickets = {key: value for key, value in self.tickets.items() if value["expires"] > time.monotonic()}
            self.tickets[token] = {"expires": time.monotonic()+60, "record_video": record_video}
            return {"ticket": token, "expires_in": 60, "script": script,
                    "record_video": record_video,
                    "sequence": "3 s preflight + 3 s recheck → GUIDED → arm → 1.5 m range-feedback flight (2 m ceiling) → 2 m forward → launch coordinates → LAND"}

    @contextmanager
    def ownership(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        with (self.folder / ".launch.lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def status(self):
        with self.lock, self.ownership():
            return self._status()

    def _status(self):
        latest = self.folder / "latest.json"
        if not latest.exists():
            return {"state": "idle", "lines": [], "run_id": None}
        run_id = json.loads(latest.read_text())["run_id"]
        if not isinstance(run_id, str) or not len(run_id)==32 or any(c not in "0123456789abcdef" for c in run_id):
            raise RuntimeError("Invalid saved run")
        state = json.loads((self.folder / (run_id+".json")).read_text())
        if state["state"] in {"starting", "running", "stopping"}:
            pid = state.get("pid")
            if (pid and not self.matches(pid, run_id)) or (not pid and time.time()-state["started_at"]>30):
                state["state"] = "interrupted"
                state["message"] = "Runner exited without completion confirmation. Check the aircraft and output before another run."
            if (self.folder / (run_id+".stop")).exists() and state["state"] in {"starting", "running"}:
                state["state"] = "stopping"
        log = self.folder / (run_id+".log")
        if log.exists():
            with log.open("rb") as handle:
                handle.seek(0, 2)
                handle.seek(max(0, handle.tell()-65536))
                state["lines"] = handle.read().decode(errors="replace").splitlines()[-400:]
        else:
            state["lines"] = []
        return state

    def matches(self, pid, run_id):
        try:
            process = Path(f"/proc/{int(pid)}")
            command = (process / "cmdline").read_bytes().split(b"\0")
            return process.stat().st_uid == os.getuid() and str(ROOT / "scripts/web/flight_job.py").encode() in command and run_id.encode() in command
        except (OSError, ValueError):
            return False

    def launch(self, ticket, confirmed):
        with self.lock, self.ownership():
            preparation = self.tickets.pop(ticket, None)
            if not confirmed or preparation is None or preparation["expires"] < time.monotonic():
                raise ValueError("Launch confirmation expired. Review the flight again.")
            if self._status()["state"] in {"starting", "running", "stopping", "interrupted"}:
                raise RunConflict("A flight is active or needs inspection before another launch.")
            run_id = uuid.uuid4().hex
            state = {"run_id": run_id, "state": "starting", "pid": None,
                     "record_video": preparation["record_video"], "started_at": time.time(),
                     "exit_code": None, "message": "Starting flight runner"}
            state_path = self.folder / (run_id+".json")
            save(state_path, state)
            save(self.folder / "latest.json", {"run_id": run_id})
            command = [str(PYTHON), "-u", str(ROOT / "scripts/web/flight_job.py"),
                       "--folder", str(self.folder), "--run-id", run_id]
            try:
                with (self.folder / (run_id+".log")).open("xb") as output:
                    process = self.spawn(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    # Child records its own PID/state and completion atomically.
                    state["pid"] = process.pid
                    threading.Thread(target=process.wait,daemon=True).start()
            except OSError as error:
                state.update(state="failed", message=str(error))
                save(state_path, state)
                raise RuntimeError("Unable to start flight runner") from error
            return {"run_id": run_id, "state": "starting"}

    def stop(self):
        with self.lock, self.ownership():
            state = self._status()
            if state["state"] not in {"running", "starting", "stopping"}:
                raise RunConflict("No active flight job to stop")
            if state["state"] == "stopping":
                return state
            if not state.get("pid") or not self.matches(state["pid"], state["run_id"]):
                raise RunConflict("Runner is still starting. Retry in a moment.")
            marker = self.folder / (state["run_id"]+".stop")
            marker.touch(exist_ok=False)
            os.kill(state["pid"], signal.SIGINT)
            return {"state": "stopping", "run_id": state["run_id"]}
