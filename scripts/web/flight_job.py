#!/usr/bin/env python3
"""One explicitly launched job. Never runs on boot or retries a flight."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

from script_runs import ROOT, save



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    options = parser.parse_args()
    state_path = options.folder / (options.run_id+".json")
    state = json.loads(state_path.read_text())
    state.update(pid=os.getpid(), state="starting")
    save(state_path, state)
    code = 1
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from flight_2m.mission import main as flight
        sys.argv = [str(ROOT / "scripts/flight_2m.py"), "--execute"]
        if not state["record_video"]:
            sys.argv.append("--no-video")
        state.update(state="running", message="Flight script running")
        save(state_path, state)
        print("Starting the approved 2 m flight script.", flush=True)
        code = flight()
    except KeyboardInterrupt:
        code = 130
        print("Launch cancelled before flight execution.", flush=True)
    except Exception as error:
        print(f"Runner stopped: {error}", flush=True)
    finally:
        state.update(state="completed" if code==0 else "failed", exit_code=code,
                     finished_at=time.time(), message="Script exited; inspect output for flight result")
        save(state_path, state)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
