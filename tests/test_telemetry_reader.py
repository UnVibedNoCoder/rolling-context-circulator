"""Synchronize integration evidence reads without ignoring completed corrupt records."""
import fcntl
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_relay


class TelemetryReaderTests(unittest.TestCase):
    def test_reader_waits_for_writer_lock_before_parsing_complete_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            harness = SimpleNamespace(state=Path(directory), fail=self.fail)
            entering_read_lock = threading.Event()
            finished = threading.Event()
            result = {}
            real_flock = fcntl.flock

            def flock(fd, operation):
                if operation == fcntl.LOCK_SH:
                    entering_read_lock.set()
                return real_flock(fd, operation)

            def read():
                try:
                    result['events'] = test_relay.RelayTests.events(harness)
                except Exception as exc:
                    result['error'] = exc
                finally:
                    finished.set()

            with path.open('w') as writer, patch('test_relay.fcntl.flock', side_effect=flock):
                real_flock(writer.fileno(), fcntl.LOCK_EX)
                writer.write('{"event":"relay_request_')
                writer.flush()
                reader = threading.Thread(target=read)
                reader.start()
                try:
                    self.assertTrue(entering_read_lock.wait(2))
                    self.assertFalse(finished.is_set())
                    writer.write('completed"}\n')
                    writer.flush()
                finally:
                    real_flock(writer.fileno(), fcntl.LOCK_UN)
                    reader.join(3)
                self.assertFalse(reader.is_alive())
            self.assertNotIn('error', result)
            self.assertEqual(result['events'], [{'event': 'relay_request_completed'}])

    def test_completed_malformed_record_still_fails_instead_of_being_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'telemetry.jsonl'
            path.write_text('{invalid completed record}\n')
            harness = SimpleNamespace(state=Path(directory), fail=self.fail)
            with self.assertRaises(json.JSONDecodeError):
                test_relay.RelayTests.events(harness)


if __name__ == '__main__':
    unittest.main()
