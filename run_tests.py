#!/usr/bin/env python3
"""Dedicated stdlib suite. Synthetic fixtures are not production evidence.
No existing files are deleted. Synthetic test directories are retained.
Protected-storage integration tests are skipped with an explicit reason if the
backend cannot enforce permissions. Synthetic writer unit tests mock only the
permission predicate and never establish security or production acceptance.
"""
import contextlib
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aistordoctor as a

ROOT = Path(__file__).resolve().parent
SAMPLE = Path(os.environ.get('AISTOR_DIAG_TEST_REPORT', str(ROOT / 'fixtures/official-report.md')))
SYNTHETIC = '''# MinIO Health Diagnostics Report
## Summary
* Date: Mon, 12 Oct 2026 08:56:49 UTC
* Results: 0 passed, 1 errors, 0 warnings
## Errors
| Index | Status | Check | Name | Description |
|---|---|---|---|---|
| 1 | error | unknown.check | Unknown check | Observed fact |
## Failed checks
### 1. Unknown check (error)
**Info:**
host=test.svc.cluster.local:9000 password=canary-secret
**Resolution:**
```
# this is text, never executed
| 999 | error | fake.check | Fake | fake |
```
'''


class R1Tests(unittest.TestCase):
    def private_root(self):
        parent = a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')
        return Path(tempfile.mkdtemp(dir=parent))

    def protected_root(self):
        root = self.private_root()
        try:
            a.secure_dir(root)
        except a.OutputError:
            self.skipTest('protected-storage integration blocked: backend owner/mode cannot be verified')
        return root

    def test_actual_sample(self):
        if not SAMPLE.is_file():
            self.skipTest('approved real fixture not available; not a pass')
        r = a.model(SAMPLE.read_text(encoding='utf-8'))
        self.assertEqual(r['official_summary'], {'passed': 31, 'errors': 2, 'warnings': 2})
        self.assertEqual(len(r['findings']), 4)
        self.assertEqual(r['run_status'], 'success')
        self.assertEqual(r['facts']['certificate_remaining_seconds'], 752393)
        self.assertEqual(r['facts']['certificate_expires_kst'], '2026-10-21T10:56:42+09:00')
        self.assertTrue(all(c['official_status'] != 'passed' for c in r['check_results']))
        self.assertNotIn('primary-object-store', json.dumps(r))

    def test_fence_preserved_not_parsed(self):
        r = a.model(SYNTHETIC)
        self.assertEqual(len(r['findings']), 1)
        self.assertIn('fake.check', r['findings'][0]['detail'])
        self.assertIn('never executed', r['findings'][0]['detail'])

    def test_bom_crlf(self):
        self.assertEqual(a.model(SYNTHETIC), a.model('\ufeff' + SYNTHETIC.replace('\n', '\r\n')))

    def test_unknown_preserved(self):
        f = a.model(SYNTHETIC)['findings'][0]
        self.assertEqual(f['source_check_id'], 'unknown.check')
        self.assertEqual(f['priority'], 'unclassified')
        self.assertIsNone(f['rule_result'])

    def test_all_formats_mask(self):
        r = a.model(SYNTHETIC)
        for body in (json.dumps(r), a.markdown(r), a.html_report(r)):
            self.assertNotIn('canary-secret', body)
            self.assertNotIn('test.svc.cluster.local', body)

    def test_ipv6_not_time(self):
        m = a.Masker('strict')
        self.assertNotIn('2001:db8::1', m.string('host 2001:db8::1'))
        self.assertEqual(m.string('2026-10-21T01:56:42+00:00'), '2026-10-21T01:56:42+00:00')

    def test_public_check_ids_preserved_without_host_leak(self):
        report = a.model(SYNTHETIC.replace('unknown.check', 'minio.net.tls.valid'))
        self.assertEqual(report['check_results'][0]['source_check_id'], 'minio.net.tls.valid')
        self.assertEqual(report['findings'][0]['source_check_id'], 'minio.net.tls.valid')
        masker = a.Masker('strict', public_check_ids={'minio.net.tls.valid'})
        masked = masker.apply({'source_check_id': 'minio.net.tls.valid', 'detail': 'host=minio.net password=canary'})
        self.assertNotIn('minio.net', masked['detail'])
        self.assertNotIn('canary', masked['detail'])
        masker.verify(masked)
        with self.assertRaises(a.OutputError):
            masker.verify({**masked, 'detail': 'host=minio.net'})

    def test_partial(self):
        r = a.model(SYNTHETIC.replace('1 errors', '2 errors'))
        self.assertEqual(r['run_status'], 'partial')
        self.assertIn('summary/detail mismatch', r['parser_errors'])

    def test_missing_summary(self):
        with self.assertRaises(a.InputError):
            a.model('arbitrary markdown')

    def test_missing_check_table(self):
        with self.assertRaises(a.InputError):
            a.model('# Report\n## Summary\n* Results: 0 passed, 1 errors, 0 warnings\n')

    def test_as_of(self):
        with self.assertRaises(a.InputError):
            a.model(SYNTHETIC, as_of='2026-10-13')
        r = a.model(SYNTHETIC, as_of='2026-10-13T09:00:00+09:00')
        self.assertEqual(r['input_metadata']['as_of_origin'], 'explicit')
        self.assertEqual(r['input_metadata']['collected_at'], '2026-10-12T08:56:49+00:00')

    def test_missing_time_not_invented(self):
        r = a.model(SYNTHETIC.replace('* Date: Mon, 12 Oct 2026 08:56:49 UTC\n', ''))
        self.assertIsNone(r['input_metadata']['collected_at'])
        self.assertIsNone(r['input_metadata']['as_of'])

    def test_hostile_html_markdown(self):
        r = a.model(SYNTHETIC.replace('Observed fact', '<script>alert(1)</script> ![x](javascript:evil)'))
        self.assertNotIn('<script>alert(1)</script>', a.html_report(r))
        self.assertNotIn('<script>alert(1)</script>', a.markdown(r))
        self.assertNotIn('![x](', a.markdown(r))
        self.assertNotIn('innerHTML', a.html_report(r))

    def test_atomic_commit_manifest(self):
        root = self.protected_root()
        r = a.model(SYNTHETIC)
        gen = a.save(r, root)
        pointer = json.loads((root / 'CURRENT.json').read_text())
        self.assertEqual(pointer['generation'], gen.name)
        manifest = json.loads((gen / 'manifest.json').read_text())
        for name, expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((gen / name).read_bytes()).hexdigest(), expected)
        self.assertEqual(os.stat(gen).st_mode & 0o777, 0o700)

    def test_collision(self):
        root = self.protected_root()
        a.save(a.model(SYNTHETIC), root)
        with self.assertRaises(a.OutputError):
            a.save(a.model(SYNTHETIC), root)

    def test_cli_default_output(self):
        root = self.protected_root()
        source = root / 'input.md'
        source.write_text(SYNTHETIC, encoding='utf-8')
        with contextlib.redirect_stdout(StringIO()):
            code = a.main(['--official-report', str(source)], default_out_dir=root / 'reports')
        self.assertEqual(code, 0)
        self.assertTrue((root / 'reports/CURRENT.json').is_file())

    def test_cli_explicit_output_overrides_default(self):
        root = self.protected_root()
        source = root / 'input.md'
        source.write_text(SYNTHETIC, encoding='utf-8')
        with contextlib.redirect_stdout(StringIO()):
            code = a.main(['--official-report', str(source), '--out-dir', str(root / 'explicit')], default_out_dir=root / 'unused')
        self.assertEqual(code, 0)
        self.assertTrue((root / 'explicit/CURRENT.json').is_file())
        self.assertFalse((root / 'unused').exists())

    def test_failed_force_keeps_pointer(self):
        root = self.protected_root()
        a.save(a.model(SYNTHETIC), root)
        before = (root / 'CURRENT.json').read_bytes()
        with patch.object(a.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(a.OutputError):
                a.save(a.model(SYNTHETIC), root, force=True)
        self.assertEqual((root / 'CURRENT.json').read_bytes(), before)

    def test_successful_force_keeps_old_set(self):
        root = self.protected_root()
        old = a.save(a.model(SYNTHETIC), root)
        new = a.save(a.model(SYNTHETIC), root, force=True)
        self.assertNotEqual(old, new)
        self.assertTrue((old / 'aistor-diag-report.json').is_file())

    def test_insecure_root(self):
        root = self.private_root()
        root.chmod(0o755)
        with self.assertRaises(a.OutputError):
            a.save(a.model(SYNTHETIC), root)

    def test_input_limit(self):
        with self.assertRaises(a.InputError):
            a.model('x' * (a.MAX_BYTES + 1))

    def test_cli_codes_partial_and_error(self):
        root = self.private_root()
        bad = root / 'partial.md'
        bad.write_text(SYNTHETIC.replace('1 errors', '2 errors'), encoding='utf-8')
        try:
            a.secure_dir(root)
            protected = True
        except a.OutputError:
            protected = False
        with contextlib.redirect_stderr(StringIO()), contextlib.redirect_stdout(StringIO()):
            self.assertEqual(a.main(['--official-report', str(bad)]), 4)
            self.assertEqual(a.main(['--official-report', str(root / 'absent')]), 2)
            self.assertEqual(a.main(['--official-report', str(bad), '--out-dir', str(root / 'reports'), '--allow-partial']), 4 if protected else 3)
        self.assertEqual((root / 'reports/CURRENT.json').is_file(), protected)

    def test_quoted_secret_redaction(self):
        r = a.model(SYNTHETIC.replace('canary-secret', '"canary with spaces"'))
        self.assertNotIn('with spaces', json.dumps(r))

    def test_invalid_certificate_time_is_partial(self):
        text = SYNTHETIC.replace('unknown.check', 'minio.net.tls.valid') + '\n| 2026-07-23T01:56:42Z | 2026-99-21T01:56:42Z |\n'
        r = a.model(text)
        self.assertEqual(r['run_status'], 'partial')
        self.assertIsNone(r['facts']['certificate_remaining_seconds'])
        self.assertIn('invalid certificate timestamp', r['parser_errors'])

    def test_bearer_redaction(self):
        r = a.model(SYNTHETIC + '\nAuthorization: Bearer synthetic-bearer-canary\n')
        self.assertNotIn('synthetic-bearer-canary', json.dumps(r))

    def test_facts_in_all_renderers(self):
        if not SAMPLE.is_file():
            self.skipTest('real fixture unavailable')
        r = a.model(SAMPLE.read_text(encoding='utf-8'))
        for body in (a.markdown(r), a.html_report(r)):
            self.assertIn('2026-10-21T10:56:42+09:00', body)
            self.assertIn('752393', body)

    def test_no_analyzer_network_or_shell_imports(self):
        import ast
        tree = ast.parse((ROOT / 'aistordoctor.py').read_text())
        forbidden = {'socket', 'urllib', 'http', 'requests', 'subprocess'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(x.name.split('.')[0] in forbidden for x in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or '').split('.')[0], forbidden)


class SyntheticWriterUnitTests(unittest.TestCase):
    """Permission predicate mocked ONLY for synthetic transaction mechanics.
    These tests do not validate filesystem confidentiality or host/browser use.
    """
    def setUp(self):
        self.root = R1Tests.private_root(self)
        self.guard = patch.object(a, 'require_private', return_value=None)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.report = a.model(SYNTHETIC)

    def test_unit_manifest_and_readback(self):
        gen = a.save(self.report, self.root)
        marker = json.loads((self.root / 'CURRENT.json').read_text())
        self.assertEqual(marker['generation'], gen.name)
        manifest = json.loads((gen / 'manifest.json').read_text())
        for name, expected in manifest['files'].items():
            self.assertEqual(hashlib.sha256((gen / name).read_bytes()).hexdigest(), expected)

    def test_unit_collision_refused(self):
        a.save(self.report, self.root)
        with self.assertRaises(a.OutputError):
            a.save(self.report, self.root)

    def test_unit_failed_commit_preserves_previous(self):
        a.save(self.report, self.root)
        before = (self.root / 'CURRENT.json').read_bytes()
        with patch.object(a.os, 'replace', side_effect=OSError('synthetic commit failure')):
            with self.assertRaises(a.OutputError):
                a.save(self.report, self.root, force=True)
        self.assertEqual((self.root / 'CURRENT.json').read_bytes(), before)

    def test_unit_force_retains_previous(self):
        old = a.save(self.report, self.root)
        new = a.save(self.report, self.root, force=True)
        self.assertNotEqual(old, new)
        self.assertTrue((old / 'manifest.json').is_file())


if __name__ == '__main__':
    unittest.main(verbosity=2)
