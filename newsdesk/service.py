"""Keep the oil workbench running in the current user's session.

Run detached from an authorized terminal; this does not install a login service.
Only one supervisor may run for this workspace. Logs never include environment
variables, and the server inherits the caller's existing private configuration.
"""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from newsdesk.private_config import load_private_aihub_config


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "newsdesk" / "data" / "service"


def main():
    load_private_aihub_config()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A newsdesk supervisor is already running.")
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        stop = threading.Event()
        children = {}
        restart_at = {}
        terminated_children = set()
        child_specs = {
            "oil": ([sys.executable, "-u", "-m", "newsdesk.server", "--port", "8765"], STATE / "server.pid"),
        }

        def launch(name, command, pid_path):
            try:
                child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL)
            except OSError as error:
                print("{} server could not start: {}. Retrying in 5 seconds.".format(name, error), flush=True)
                restart_at[name] = time.monotonic() + 5
                return
            children[name] = child
            pid_path.write_text(str(child.pid))
            print("Started {} server with pid {}.".format(name, child.pid), flush=True)

        def terminate_child(name, child):
            if name in terminated_children or child.poll() is not None:
                return
            child.terminate()
            terminated_children.add(name)

        def shutdown(signum, frame):
            stop.set()
            for name, child in children.items():
                terminate_child(name, child)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            for name, (command, pid_path) in child_specs.items():
                launch(name, command, pid_path)
            while not stop.is_set():
                now = time.monotonic()
                for name, (command, pid_path) in child_specs.items():
                    child = children.get(name)
                    if child is None:
                        if now >= restart_at.get(name, 0):
                            launch(name, command, pid_path)
                        continue
                    code = child.poll()
                    if code is None:
                        continue
                    children.pop(name, None)
                    pid_path.unlink(missing_ok=True)
                    if not stop.is_set():
                        print("{} server exited with code {}; restarting in 5 seconds.".format(name, code), flush=True)
                        restart_at[name] = now + 5
                stop.wait(0.5)
        finally:
            for name, child in list(children.items()):
                terminate_child(name, child)
                child.wait()
                child_specs[name][1].unlink(missing_ok=True)
            for _, pid_path in child_specs.values():
                pid_path.unlink(missing_ok=True)
            lock.seek(0)
            lock.truncate()


if __name__ == "__main__":
    main()
