# Drone websites at startup

The dashboard at **http://localhost:8070** links to the video gallery (8080),
MAVProxy / Pi terminal (8071), OLED mirror (8090), and QR receiver (5000). It shows HTTP availability,
systemd process state, and whether each service is enabled at boot. Support
status also covers the OLED front panel and MAVProxy router. Status refreshes
every ten seconds. The overview is responsive on phones and includes a bottom
navigation bar. Flight launching is confined to the explicit Scripts page.

## Pi terminal and MAVProxy

Open **http://localhost:8071** or **http://100.112.205.54:8071**. Choose
**Open MAVProxy** to sign in to the Pi and start an interactive MAVProxy console,
or **Open Pi terminal** for a normal login shell. Enter the Pi account password
when SSH prompts; password typing displays no characters. Passwords are not
stored in the application or written to terminal logs.

The browser terminal is a real pseudo-terminal attached to SSH on this Pi,
with bundled xterm.js rendering, keyboard input, colours, resize, and mobile
Ctrl+C/Tab/Esc/arrow keys. The existing SSH server authenticates the account;
its host key is verified against this Pi's public SSH host keys. Browser
sessions are isolated by an HttpOnly cookie and per-session action token.

MAVProxy runs as the Pi user through the shared TCP 5760 hub, backed by the existing router's UDP 14552 output.
It never opens the FC serial port a second time, sends no GCS heartbeat, and
never automatically arms. Its own console HOME is separate from the normal
Pi shell so unrelated MAVProxy startup scripts are not loaded. Exit MAVProxy
with Ctrl+C or its exit command to return to the Pi shell. Disconnect closes
that browser's SSH session; the main MAVProxy router and OLED keep running.
Closing a browser tab alone keeps its session alive for reconnection.

The installer disables/stops `drone-gcs-web.service` when installing the new
`drone-mavproxy-web.service`. Legacy GCS source is retained for reference;
the dashboard and boot no longer use it. Port 8071 keeps its existing Tailscale
forwarding. The shell has the normal permissions of your Pi account; this is
real terminal access, not a simulated command viewer.

## Run the current flight script

Open **http://localhost:8070/scripts** or
**http://100.112.205.54:8070/scripts** over Tailscale. Choose whether to record
video, tap **Review & run script**, review the sequence, confirm that you are
with the aircraft and ready to recover control, then tap **Arm & fly**.
The current sequence is 3 s preflight plus a 3 s recheck, GUIDED, normal arm,
2 m rangefinder-confirmed takeoff, 2 m forward, then RTL/land. Existing script
checks apply; `opt_qua` is not a launch threshold. No fence is uploaded by
this version. The script can move and spin the aircraft's motors.

Only this allowlisted script is executable; there is no arbitrary shell or
file-path input. The launch review creates a one-use ticket that expires in
60 seconds. Same-origin and per-process-token checks protect action requests.
Concurrent/replayed launches are rejected. Visiting a page, reconnecting,
starting the web service, and rebooting never launch a flight.

The browser GCS has been replaced with a real Pi terminal at port 8071.
MAVProxy consoles and flight scripts use independent TCP connections to the hub.
The `mission.lock` prevents duplicate autonomous missions; consoles do not take it.
Each local console gets a distinct source system (200–239). The normal Pi shell
remains available after MAVProxy exits. Coordinate commands between operators.

The page displays live output and run status. **Stop & request LAND** sends a
single interrupt to that job; the script requests LAND while it owns GUIDED
and respects pilot/FC mode changes. This is not a guaranteed landing command
after a lost link. Repeated Stop requests do not interrupt landing cleanup.

Jobs run as the normal user and persist independently of the browser. Output
and state are kept under `video/.script-runs/`. Dashboard-only restarts leave
an already launched job running and recover its status; jobs never retry
automatically. If a job disappears without a completion record, another launch
is blocked pending inspection of aircraft and job state.

Updating just the dashboard and terminal web services:

```bash
sudo python3 scripts/web/install_services.py --user user \
  --only drone-web-dashboard.service drone-mavproxy-web.service
```

Install from the repository root:

```bash
sudo python3 scripts/web/install_services.py --user user
```

Replace `user` with the account that owns the checkout. The installer validates
and installs these units in `/etc/systemd/system`, enables them for boot, and
starts them immediately:

| Service | Website |
| --- | --- |
| `drone-mavproxy-web.service` | http://localhost:8071 |
| `drone-web-dashboard.service` | http://localhost:8070 |
| `drone-video-gallery.service` | http://localhost:8080 |
| `drone-qr-web.service` | http://localhost:5000 |

The existing `drone-oled-web.service` provides http://localhost:8090.
OLED and MAVProxy keep their current units/configuration; the installer does
not restart them. The dashboard wants the existing OLED web unit available.
These websites start when the system boots, without an interactive login.
They are background services; the installer does not open a browser window.

Keep the checkout at its installed location. Services execute its Python files
as the chosen normal user. No Python packages need installing. The xterm.js browser assets are bundled locally.
The video gallery writes browser playback cache only under `video/`.

The installer transfers manually launched copies of this checkout's gallery,
dashboard and QR server to systemd. It checks their owner and exact script path
before stopping them. `--replace-gallery-pid PID` can also explicitly identify
the old gallery process.
Installation preserves timestamped backups of existing copies of these three
web units. Re-running the installer updates and restarts only these web units.

## Manage services

```bash
systemctl status drone-web-dashboard drone-video-gallery drone-qr-web
journalctl -u drone-web-dashboard -u drone-video-gallery -u drone-qr-web -n 50
sudo systemctl restart drone-web-dashboard drone-video-gallery drone-qr-web
```

Disable these three websites at boot and stop them:

```bash
sudo systemctl disable --now drone-web-dashboard drone-video-gallery drone-qr-web
```

Add more local websites to `scripts/web/sites.json`, then restart the dashboard.
Each item specifies its local port and systemd unit. The dashboard shows status
for configured sites; it does not discover arbitrary network services or
install startup units for newly listed websites.

## QR website

`qr_receiver.py` provides the existing QR application's `POST /qr` JSON API
without requiring Flask. The legacy `drone_project/qr_server.py` is preserved.
The new receiver runs on localhost and keeps the latest value in memory;
it resets after restart. A vision producer on this Pi can POST
`{"data":"QR value"}` to `http://127.0.0.1:5000/qr`.

## View from another computer

To access the websites directly through this Pi's Tailscale address:

```bash
sudo python3 scripts/web/enable_tailscale.py
```

Open `http://100.112.205.54:8070` from a device connected to your tailnet.
The dashboard links use the hostname/IP you opened, so videos, OLED and QR
open on the Pi instead of your viewing device's localhost. The four ports
are forwarded using persistent Tailscale Serve TCP forwarding. They remain
available on localhost as well. Tailscale network access rules apply.
This does not enable public Tailscale Funnel access.

Alternatively, without Tailscale forwarding:

On the viewing computer, open a tunnel to the Pi:

```bash
ssh -N -L 8070:127.0.0.1:8070 -L 8071:127.0.0.1:8071 -L 8080:127.0.0.1:8080 \
  -L 8090:127.0.0.1:8090 -L 5000:127.0.0.1:5000 user@PI_ADDRESS
```

Open http://localhost:8070 on that computer. All dashboard links then work
through the forwarded ports. Existing OLED network settings are preserved;
the three newly installed websites bind only to localhost.

## Tests

```bash
python3 scripts/test_web_services.py
python3 scripts/test_video_gallery.py
drone_project/pi_flight/.venv/bin/python scripts/test_script_runs.py
```

Tests run local HTTP servers and generated media only, without a camera or
aircraft connection. Boot enablement is verified with systemd; validation
does not require rebooting the Pi.

## Recovery and persistence

Managed websites start at boot and restart after unexpected process exit.
Terminal web service restarts end its SSH sessions; sign in again to open a new
terminal. No motor tests or flight commands run automatically. Tailscale Serve
`--bg` forwarding survives reboot. The serial router and OLED are separate services.

## Terminal tabs and shortcuts

On port 8071, **+ New terminal** adds an independent tab (up to eight per browser).
Choose **Open Pi terminal** or **Open MAVProxy**, then authenticate with SSH.
**Reconnect** opens a disconnected tab in its previous mode; **Restart terminal**
ends and reopens only the selected SSH session. **Close tab** ends and removes it.
Reloading the page restores tabs and output; a web service restart ends sessions.
Multiple MAVProxy consoles, Pi shells and one autonomous flight can coexist.
Telemetry is broadcast to each connection; coordinate aircraft commands.

The basic command menu includes Pi status, services/logs, MAVProxy status,
parameters, normal arm/disarm, GUIDED, LAND and RTL. Shortcuts insert text without
Enter so the operator can review the current prompt. Clear an unfinished command
before inserting another; use shortcuts after SSH authentication, not at the
password prompt. If MAVProxy has exited to bash, select Pi shell commands.

The motor-test builder prepares `long DO_MOTOR_TEST motor 0 percent seconds 1 0 0`.
It tests one motor in controller test order, with throttle 1–15% and duration 1–3s.
Remove propellers and keep the aircraft on the ground. Confirm the dialog, review
the command at a MAVProxy prompt, then press Enter. It does not force-arm or
change safety parameters; the flight controller may reject the test. No motor or
flight commands are run by the website tests.


## Codex on the Scripts page

The bottom of `/scripts` embeds a compact Codex terminal from the same Pi/IP at
port 8071. Click **Open Codex**, enter the Pi password at the SSH prompt, and use
the interactive CLI. Codex opens with `--cd /home/user/all_drone --no-alt-screen`;
its existing account, configuration and approval settings apply. No prompt or
flight command is submitted automatically. Reconnect, restart and disconnect
operate on this terminal only. Leaving the page keeps its session available.
The embedded view reuses an existing Codex tab or creates its own tab, preserving
other Pi/MAVProxy terminals. **Open full terminal** opens the main terminal page.
Only the dashboard on the matching hostname/IP and port 8070 can embed this view.


## Terminal controls and force arming

The terminal toolbar provides keyboard focus, reviewed paste (including large
Unicode text), clear view, text sizes 10–24px, and expand/collapse. Tabs support
arrow/Home/End keys. Storage restrictions do not prevent opening a terminal.
Polling pauses in hidden pages; input failures are reported and never retried.
Restarted/removed sessions discard stale pending output and queued input.

**Force arm…** is enabled only for a connected MAVProxy tab. A simple confirmation
dialog explains that props must be removed and the aircraft must be on the
ground; no typed phrase or checkbox is required. It inserts, without Enter:
`long COMPONENT_ARM_DISARM 1 21196 0 0 0 0 0`.
This attempts to bypass arming checks and can start motors; review it at the
MAVProxy prompt before pressing Enter. It does not modify persistent parameters.
See https://ardupilot.org/dev/docs/mavlink-arming-and-disarming.html.
The installed legacy `arm throttle force` uses a different value, so the shortcut
uses the documented COMMAND_LONG directly. Tests never transmit this to an FC.


For browser regression tests, install `playwright` in a development environment,
run `python3 -m playwright install chromium`, then run
`python3 -m unittest discover -s scripts -p test_terminal_browser.py`.
The browser tests use an inert PTY echo process instead of SSH/MAVLink; they cover
mobile layout, Codex embedding, storage restrictions, tabs, reconnect/restart,
Unicode paste, font/expand controls, connection recovery and dangerous-command
confirmation. Backend tests run with
`drone_project/pi_flight/.venv/bin/python scripts/test_pi_terminal.py`, including
force-arm packet validation against a mocked MAVLink sender.


Frontend interaction tests also run without a browser using
`node scripts/test_terminal_ui.cjs`. They load the real terminal JavaScript with
an in-memory DOM and inert API, including force-arm confirmation/cancellation,
stale sessions, motor-test limits, tabs, reconnect/restart, Unicode paste,
connection recovery, fonts/expand, and restricted storage. They do not validate
pixel rendering; use the optional Chromium tests above for visual/layout checks.

## Ensure startup and recovery

Run `sudo python3 scripts/web/ensure_startup.py` after installing the existing
services. It enables and starts the dashboard, terminal server, video gallery,
QR receiver, OLED panel/mirror, telemetry router, SSH and Tailscale. Active
processes are preserved. Systemd drop-ins remove retry-rate limits for the
router/OLED/SSH/Tailscale and use a 3-second retry delay; web units already have
persistent recovery. Configuration is verified with `systemd-analyze verify`.
Tailscale's existing persistent forwarding supplies the same five website ports.
Interactive SSH/Codex/MAVProxy sessions still require login; recordings and
flights require explicit operator actions. This setup does not reboot the Pi.


## Concurrent MAVLink clients and remote ground stations

`drone-mavlink-hub.service` listens on `tcp:127.0.0.1:5760` and consumes the
existing router's dedicated UDP 14552 output. The UART stays exclusively owned
by `mavproxy.service`; OLED keeps its independent read/write UDP 14553 link.
The flight script and local flight configuration now default to TCP 5760.
MAVProxy consoles use unique source systems 200–239 and separate log files;
they no longer acquire the autonomous mission lock or compete for UDP 14550.
Legacy UDP 14550 remains available for a single legacy client.

Run `sudo python3 scripts/web/install_mavlink_hub.py` to install/enable the hub
and add persistent Tailscale TCP forwarding without restarting the UART router
or existing terminals. New consoles use the hub. Consoles opened before migration must reconnect to use the shared TCP link.
The initial migration restarted the terminal website to release its obsolete console lock.
Use `sudo python3 scripts/web/ensure_startup.py` to verify all startup services.

On another Tailscale-connected device, configure a MAVLink TCP connection to
`100.112.205.54:5760` (pymavlink `tcp:100.112.205.54:5760`). This endpoint permits
both telemetry reads and MAVLink writes and is not exposed to the public internet.
Use distinct GCS source IDs for external clients; local flight scripts use 254,
OLED 253 and the UART router 252. Tailnet access rules still apply.

The hub reassembles each writer's frames independently and preserves signatures.
Slow clients are disconnected instead of blocking other telemetry readers. It
never queues commands for replay after an upstream outage. Network/serial faults,
link bandwidth and conflicting operator commands can still interrupt operation;
concurrent connectivity does not arbitrate simultaneous aircraft-control commands.
