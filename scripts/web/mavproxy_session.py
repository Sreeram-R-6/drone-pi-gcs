#!/usr/bin/env python3
"""Interactive MAVProxy client on the existing router output; never owns UART."""
from pathlib import Path
import os
import contextlib
import signal
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'drone_project/pi_flight'))
from flightcheck.telemetry import process_lock


def command(source_system=251):
    venv=ROOT/'drone_project/pi_flight/.venv/bin'
    return [str(venv/'python'),str(venv/'mavproxy.py'),'--master=tcp:127.0.0.1:5760',
            '--mavversion=2.0','--dialect=ardupilotmega',f'--source-system={source_system}',
            '--heartbeat-rate=0','--streamrate=-1',
            '--default-modules=link,log,output,param,arm,mode,rc,cmdlong',
            '--logfile='+str(ROOT/f'video/.terminal/mavproxy-{source_system}.tlog')]


@contextlib.contextmanager
def console_identity():
    for source in range(200,240):
        stack=contextlib.ExitStack()
        try:stack.enter_context(process_lock(f'console-source-{source}.lock'))
        except RuntimeError as error:
            stack.close()
            if 'Duplicate owner' in str(error):continue
            raise
        try:
            yield source
            return
        finally:stack.close()
    raise RuntimeError('All console identities are in use')


def main():
    try:
        with console_identity() as source:
            print(f'MAVProxy on the Pi · shared TCP 5760 · source system {source}',flush=True)
            print('Telemetry and commands are shared with other clients. Coordinate vehicle commands.',flush=True)
            # Keep unrelated ~/.mavinit.scr startup commands out of this console.
            home=ROOT/'video/.terminal/console-home';home.mkdir(parents=True,exist_ok=True)
            environment=dict(os.environ,HOME=str(home))
            environment.pop('MAVPROXY_HOME',None)
            process=subprocess.Popen(command(source),env=environment)
            def stop(signum,frame):
                if process.poll() is None:process.send_signal(signal.SIGTERM)
            signal.signal(signal.SIGHUP,stop);signal.signal(signal.SIGTERM,stop)
            while True:
                try:return process.wait()
                except KeyboardInterrupt:
                    if process.poll() is None:process.send_signal(signal.SIGINT)
    except RuntimeError as error:
        print(f'MAVProxy unavailable: {error}',flush=True)
        return 1


if __name__=='__main__':raise SystemExit(main())
