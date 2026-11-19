import contextlib
import fcntl
import io
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from newsdesk import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        configuration = patch.object(service, 'load_private_aihub_config')
        configuration.start()
        self.addCleanup(configuration.stop)

    def test_starts_both_servers_and_forwards_shutdown(self):
        stop = threading.Event()
        stop.is_set = Mock(side_effect=[False, True])
        handlers = {}
        oil = Mock(pid=101)
        oil.poll.return_value = None
        oil.wait.return_value = -15
        gold = Mock(pid=102)
        gold.poll.return_value = None
        gold.wait.return_value = -15

        def stop_children(seconds):
            self.assertEqual(seconds, 0.5)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return True

        with tempfile.TemporaryDirectory() as tmp, patch.object(service, 'STATE', Path(tmp)), \
                patch.object(service.threading, 'Event', return_value=stop), \
                patch.object(service.signal, 'signal', side_effect=lambda sig, fn: handlers.update({sig: fn})), \
                patch.object(stop, 'wait', side_effect=stop_children), \
                patch.object(service.subprocess, 'Popen', side_effect=[oil, gold]) as spawn, \
                contextlib.redirect_stdout(io.StringIO()):
            service.main()
            self.assertEqual(spawn.call_count, 2)
            stop.wait.assert_called_once_with(0.5)
            oil.terminate.assert_called_once()
            gold.terminate.assert_called_once()
            self.assertFalse((Path(tmp) / 'server.pid').exists())
            self.assertFalse((Path(tmp) / 'gold-server.pid').exists())
            self.assertEqual((Path(tmp) / 'supervisor.lock').read_text(), '')

    def test_active_supervisor_prevents_duplicate_server(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, 'STATE', Path(tmp)), \
                patch.object(service.subprocess, 'Popen') as spawn:
            with (Path(tmp) / 'supervisor.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(SystemExit):
                    service.main()
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
