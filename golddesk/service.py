"""Keep the local gold futures workbench running in the current user session."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "golddesk" / "data" / "service"


def main():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "supervisor.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A golddesk supervisor is already running.")
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()

        stop = threading.Event()
        child = None

        def shutdown(signum, frame):
            stop.set()
            if child is not None and child.poll() is None:
                child.terminate()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        try:
            while not stop.is_set():
                log_path = STATE / "server.stdout.log"
                with log_path.open("a", encoding="utf-8") as log:
                    child = subprocess.Popen(
                        [sys.executable, "-u", "-m", "golddesk.server", "--port", "8785"],
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    (STATE / "server.pid").write_text(str(child.pid))
                    code = child.wait()
                child = None
                (STATE / "server.pid").unlink(missing_ok=True)
                if not stop.is_set():
                    print("Golddesk exited with code {}; restarting in 5 seconds.".format(code), flush=True)
                    stop.wait(5)
        finally:
            (STATE / "server.pid").unlink(missing_ok=True)
            lock.seek(0)
            lock.truncate()


if __name__ == "__main__":
    main()
