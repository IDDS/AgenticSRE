"""Contract checks for the read-only multimodal file-volume audit."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'audit_multimodal_volume.py'


class VolumeAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='volume-audit-test-')
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name in ('logs', 'metrics', 'traces'):
            (self.root / name).mkdir()

    def run_audit(self, output, **roots):
        command = [sys.executable, str(SCRIPT)]
        for name in ('logs', 'metrics', 'traces'):
            command.extend(['--' + name, str(roots.get(name, self.root / name))])
        command.extend(['--output', str(output)])
        return subprocess.run(command, capture_output=True, text=True, timeout=15)

    def test_hash_deduplication_and_exclusions(self):
        (self.root / 'logs' / 'a').write_bytes(b'abc')
        os.link(self.root / 'logs' / 'a', self.root / 'logs' / 'hardlink')
        (self.root / 'metrics' / 'same-content').write_bytes(b'abc')
        (self.root / 'traces' / 'different').write_bytes(b'defgh')
        (self.root / 'traces' / 'link').symlink_to(self.root / 'logs' / 'a')
        output = self.root / 'result.json'
        process = self.run_audit(output)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(output.read_text())
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['unique_content_bytes'], 8)
        self.assertEqual(result['modalities']['logs']['hardlink_aliases'], 1)
        self.assertEqual(result['modalities']['metrics']['content_duplicates'], 1)
        self.assertEqual(result['modalities']['traces']['symlinks_skipped'], 1)
        self.assertTrue(result['all_declared_modalities_nonempty'])
        self.assertFalse(result['meets_50_decimal_gb'])
        self.assertFalse(result['meets_50_gib'])

    def test_empty_modality_cannot_be_presented_as_nonempty(self):
        (self.root / 'logs' / 'a').write_bytes(b'a')
        (self.root / 'metrics' / 'b').write_bytes(b'b')
        (self.root / 'traces' / 'empty').write_bytes(b'')
        output = self.root / 'result.json'
        result = self.run_audit(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(output.read_text())['all_declared_modalities_nonempty'])

    def test_overlap_and_output_inside_source_are_rejected(self):
        output = self.root / 'result.json'
        self.assertNotEqual(self.run_audit(output, metrics=self.root / 'logs').returncode, 0)
        self.assertFalse(output.exists())
        inside = self.root / 'logs' / 'result.json'
        self.assertNotEqual(self.run_audit(inside).returncode, 0)
        self.assertFalse(inside.exists())

    def test_unreadable_file_makes_threshold_result_unknown(self):
        for name in ('logs', 'metrics', 'traces'):
            (self.root / name / 'good').write_bytes(name.encode())
        (self.root / 'logs' / 'unreadable').write_bytes(b'private')
        spec = importlib.util.spec_from_file_location('volume_audit', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_open = os.open

        def guarded_open(path, flags, *args, **kwargs):
            if Path(path).name == 'unreadable':
                raise PermissionError('denied')
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(module.os, 'open', side_effect=guarded_open):
            result = module.scan({x: self.root / x for x in ('logs', 'metrics', 'traces')})
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['meets_50_decimal_gb'])
        self.assertIsNone(result['meets_50_gib'])
        self.assertEqual(result['errors'][0]['type'], 'PermissionError')

    def test_existing_output_is_not_overwritten(self):
        output = self.root / 'result.json'
        output.write_text('existing')
        self.assertNotEqual(self.run_audit(output).returncode, 0)
        self.assertEqual(output.read_text(), 'existing')


if __name__ == '__main__':
    unittest.main()
