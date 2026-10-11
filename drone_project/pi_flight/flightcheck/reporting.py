"""Flush evidence continuously, including failures and state transitions."""
import dataclasses
import datetime as dt
import json
from pathlib import Path
import uuid


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Report:
    def __init__(self, base):
        self.path = Path(base) / (dt.datetime.now(dt.timezone.utc).strftime(
            "%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
        self.path.mkdir(parents=True)
        self.events = []

    def event(self, kind, **data):
        row = {"utc": utc(), "kind": kind, **data}
        self.events.append(row)
        with (self.path / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(row, default=str, allow_nan=False) + "\n")

    def save(self, name, data):
        with (self.path / name).open("w") as stream:
            json.dump(data, stream, indent=2, default=str, allow_nan=False)

    def checklist(self, checks, phase):
        lines = [f"{phase}: STATE    CHECK — MEASURED / REQUIREMENT"]
        for check in checks:
            lines.append(f"{check.state:8} {check.name} — {check.measured} / "
                         f"{check.requirement}; {check.explanation}")
        rendered = "\n".join(lines)
        print(rendered, flush=True)
        with (self.path / "checklist.log").open("a") as stream:
            stream.write(utc() + "\n" + rendered + "\n")
        self.save(f"{phase}.json", [dataclasses.asdict(c) for c in checks])
