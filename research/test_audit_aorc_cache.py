"""Failure-path checks; no network or real source data required."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import audit_aorc_cache as audit


class AuditFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        (self.cache / 'payload.bin').write_bytes(b'actual')
        self.row = {'url': 'https://example.invalid/test', 'status': 200,
                    'name': 'payload.bin', 'sha256': hashlib.sha256(b'actual').hexdigest()}

    def write_record(self):
        (self.cache / 'requests.jsonl').write_text(json.dumps(self.row) + '\n')

    def test_corrupted_payload_rejected(self):
        self.row['sha256'] = hashlib.sha256(b'expected').hexdigest()
        self.write_record()
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            audit.Cache([self.cache]).read(self.row['url'])

    def test_error_responses_rejected(self):
        for patch in ({'status': 404}, {'status': 200, 'error': 'aborted'}):
            with self.subTest(patch=patch):
                self.row.update(patch)
                self.write_record()
                with self.assertRaisesRegex(ValueError, 'No manifested payloads'):
                    audit.Cache([self.cache])

    def test_failure_removes_stale_success(self):
        self.row['status'] = 404
        self.write_record()
        output = self.root / 'report'
        output.mkdir()
        (output / 'audit_results.json').write_text('{"status": "old success"}')
        result = subprocess.run([
            sys.executable, audit.__file__, '--geojson', str(self.root / 'unused.geojson'),
            '--cache-dir', str(self.cache), '--output', str(output), '--cache-only'],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((output / 'audit_results.json').exists())
        status = json.loads((output / 'audit_status.json').read_text())
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['network_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
