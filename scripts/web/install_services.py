#!/usr/bin/env python3
"""Install only the local web services; does not restart OLED or MAVProxy."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
ASSETS = Path(__file__).parent
UNITS = ("drone-mavlink-hub.service", "drone-mavproxy-web.service", "drone-video-gallery.service", "drone-qr-web.service", "drone-web-dashboard.service")


def render(template, root, user):
    if any(character in str(root) for character in ('"', '\\', '\n', '\r')):
        raise ValueError("Unsupported checkout path")
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError("Web services must run as a normal user")
    # systemd expands percent specifiers even inside quoted arguments.
    return template.replace("@ROOT@", str(root).replace("%", "%%")).replace("@USER@", account.pw_name)


def replace_manual_gallery(pid, root, user):
    process = Path(f"/proc/{pid}")
    if not process.exists():
        return
    args = (process / "cmdline").read_bytes().split(b"\0")
    uid = pwd.getpwnam(user).pw_uid
    scripts = [arg.decode() for arg in args if arg.endswith(b"video_gallery.py")]
    cwd = (process / "cwd").resolve()
    if process.stat().st_uid != uid or len(scripts) != 1 or (cwd / scripts[0]).resolve() != root / "scripts/video_gallery.py":
        raise RuntimeError("Refusing to stop PID: not this checkout's manual gallery")
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not process.exists():
            return
        time.sleep(.1)
    raise RuntimeError("Manual gallery did not exit; stop it before installation")


def stop_manual_web_servers(root, user):
    """Transfer this checkout's manually launched web processes to systemd."""
    uid = pwd.getpwnam(user).pw_uid
    scripts = {root / "scripts/video_gallery.py", root / "scripts/web_dashboard.py",
               root / "scripts/web/qr_receiver.py", root / "scripts/gcs_web.py",root / "scripts/pi_terminal.py"}
    stopped = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            # Already-managed services are handled by systemctl restart below.
            if "/system.slice/drone-" in (process / "cgroup").read_text():
                continue
            args = (process / "cmdline").read_bytes().split(b"\0")
            if not args or "python" not in Path(os.fsdecode(args[0])).name:
                continue
            cwd = (process / "cwd").resolve()
            if any((cwd / os.fsdecode(arg)).resolve() in scripts for arg in args[1:] if arg.endswith(b".py")):
                os.kill(int(process.name), signal.SIGTERM)
                stopped.append(process)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    for process in stopped:
        for _ in range(30):
            if not process.exists():
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"Manual web process {process.name} did not exit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER", ""))
    parser.add_argument("--replace-gallery-pid", type=int)
    parser.add_argument("--only", nargs="+", choices=UNITS, help="Update only these selected web services")
    args = parser.parse_args()
    selected = tuple(args.only or UNITS)
    if os.geteuid() != 0:
        parser.error("Run with sudo to install systemd startup services")
    if not args.user:
        parser.error("Specify --user for the service account")
    flight_root = ROOT / "drone_project/pi_flight"
    if not (flight_root / ".venv/bin/python").is_file() or not (flight_root / "config.toml").is_file():
        parser.error("Run scripts/setup_pi.sh first: flight virtual environment and config are required")
    with tempfile.TemporaryDirectory(prefix="drone-web-units-") as folder:
        staged = []
        for unit in selected:
            path = Path(folder) / unit
            path.write_text(render((ASSETS / (unit + ".in")).read_text(), ROOT, args.user))
            staged.append(path)
        subprocess.run(["systemd-analyze", "verify", *map(str, staged)], check=True)
        if args.replace_gallery_pid:
            replace_manual_gallery(args.replace_gallery_pid, ROOT, args.user)
        if not args.only:
            stop_manual_web_servers(ROOT, args.user)
        (ROOT / "video").mkdir(exist_ok=True)
        for folder in ("logs", "fc_logs"):
            destination = ROOT / "drone_project/pi_flight" / folder
            destination.mkdir(exist_ok=True)
            shutil.chown(destination, user=args.user)
        target = Path("/etc/systemd/system")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for source in staged:
            destination = target / source.name
            if destination.exists():
                shutil.copy2(destination, target / (source.name + ".before-" + stamp))
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    if 'drone-mavproxy-web.service' in selected:
        subprocess.run(['systemctl','disable','--now','drone-gcs-web.service'],check=False)
    subprocess.run(["systemctl", "enable", *selected], check=True)
    subprocess.run(["systemctl", "restart", *selected], check=True)
    for unit in selected:
        subprocess.run(["systemctl", "is-active", "--quiet", unit], check=True)
        subprocess.run(["systemctl", "is-enabled", "--quiet", unit], check=True)
    ports={"drone-web-dashboard.service":8070,"drone-mavproxy-web.service":8071,
           "drone-video-gallery.service":8080,"drone-qr-web.service":5000}
    for port in (ports[unit] for unit in selected if unit in ports):
        for attempt in range(30):
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError(f"Website on port {port} did not become ready; inspect journalctl")
    print("Installed, running and enabled at boot:")
    print("Dashboard http://localhost:8070")
    print("MAVProxy  http://localhost:8071")
    print("Videos    http://localhost:8080")
    print("QR        http://localhost:5000")
    print("OLED      http://localhost:8090 (existing service preserved)")


if __name__ == "__main__":
    main()
