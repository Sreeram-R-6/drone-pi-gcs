#!/usr/bin/env python3
"""One named session, exactly two panes, no respawn. Default is fully offline."""
import argparse
from pathlib import Path
import shlex
import subprocess


def main():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--offline", action="store_true")
    modes.add_argument("--check-once", action="store_true")
    modes.add_argument("--wait-for-fcu", action="store_true")
    p.add_argument("--timeout", type=float)
    p.add_argument("--flight", action="store_true")
    p.add_argument("--detach", action="store_true", help="create without attaching (offline verification)")
    args = p.parse_args()
    if args.wait_for_fcu and args.timeout is None:
        p.error("--wait-for-fcu requires --timeout")
    if args.timeout is not None and not 0 < args.timeout <= 300:
        p.error("timeout must be in (0, 300]")
    base = Path(__file__).resolve().parent
    session = "pi-flight"
    def tmux(*command, check=True):
        return subprocess.run(["tmux", *command], check=check, capture_output=True, text=True)
    exists = tmux("has-session", "-t", "="+session, check=False).returncode == 0
    if not exists:
        mode = "--check-once" if args.check_once else "--wait-for-fcu" if args.wait_for_fcu else "--offline"
        flags = [mode] + (["--timeout", str(args.timeout)] if args.timeout else [])
        def command(script, extra=()):
            invocation = shlex.join(["bash", str(base/script), *flags, *extra])
            # MAVProxy deliberately ignores SIGHUP. Keep a shell parent which
            # translates tmux/session shutdown into a child SIGTERM so it cannot
            # become an orphan serial owner.
            return ("child=; trap 'if [ -n \"$child\" ]; then kill -TERM \"$child\" 2>/dev/null; "
                    "wait \"$child\"; fi; exit' HUP TERM INT; " + invocation +
                    " & child=$!; wait \"$child\"; code=$?; trap - HUP TERM INT; "
                    "printf '\\nProcess exited (%s). Inert shell; no restart.\\n' \"$code\"; "
                    "exec bash --noprofile --norc")
        pane1 = tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", session,
                     "-n", "bringup", command("launch_mavproxy.sh")).stdout.strip()
        pane2 = tmux("split-window", "-h", "-P", "-F", "#{pane_id}", "-t", pane1,
                     command("launch_mission.sh", ["--flight"] if args.flight else [])).stdout.strip()
        tmux("select-pane", "-t", pane1, "-T", "MAVProxy — serial owner")
        tmux("select-pane", "-t", pane2, "-T", "Preflight — loopback only")
        tmux("set-option", "-t", session, "pane-border-status", "top")
        tmux("set-option", "-t", session, "pane-border-format", "#{pane_title}")
    else:
        print("Existing pi-flight session: reattaching without launching additional owners")
    if not args.detach:
        subprocess.run(["tmux", "attach-session", "-t", "="+session], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
