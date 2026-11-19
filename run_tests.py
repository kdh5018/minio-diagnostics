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
import multiprocessing
import os
import re
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


def concurrent_writer(root, ready, release):
    original = a.write_exclusive
    waiting = False
    def paused_write(path, data):
        nonlocal waiting
        original(path, data)
        if not waiting:
            waiting = True
            ready.set()
            if not release.wait(10):
                raise RuntimeError('synthetic synchronization timeout')
    with patch.object(a, 'write_exclusive', side_effect=paused_write):
        a.save(a.model(SYNTHETIC), root, force=True)


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
        # 0.5.11: 기본은 한도 없음. 값을 넣으면 이전처럼 거부한다.
        self.assertIsNone(a.MAX_BYTES)
        with patch.object(a, 'MAX_BYTES', 1024), self.assertRaises(a.InputError):
            a.model('x' * 1025)

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

    def test_structured_evidence_and_source_lines(self):
        text = SYNTHETIC.replace('host=test.svc.cluster.local:9000 password=canary-secret',
                                '| Setting | Value |\n|---|---|\n| mode | always |\n\n- observed item')
        blocks = a.model(text)['findings'][0]['evidence_blocks']
        table = next(b for b in blocks if b['type'] == 'table')
        code = next(b for b in blocks if b['type'] == 'code')
        self.assertEqual(table['section'], 'info')
        self.assertEqual(table['headers'], ['Setting', 'Value'])
        self.assertEqual(table['rows'], [['mode', 'always']])
        self.assertEqual(table['source_locator']['line'], text.splitlines().index('| Setting | Value |') + 1)
        self.assertEqual(code['section'], 'resolution')
        self.assertTrue(code['closed'])
        self.assertIn('fake.check', code['text'])
        self.assertTrue(any(b['type'] == 'list' for b in blocks))
        self.assertIn('<table>', a.html_report(a.model(text)))
        self.assertIn('| Setting | Value |', a.markdown(a.model(text)))

    def test_fenced_info_marker_is_inert(self):
        report = a.model(SYNTHETIC.replace('# this is text, never executed', '**Info:**\n```python\n## Summary'))
        blocks = report['findings'][0]['evidence_blocks']
        self.assertEqual(len(report['findings']), 1)
        self.assertTrue(all(b['section'] == 'resolution' for b in blocks if b['type'] == 'code'))
        self.assertEqual(report['run_status'], 'success')

    def test_stable_ids_do_not_depend_on_row_order(self):
        row = '| 2 | error | other.check | Second check | Second observation |'
        text = SYNTHETIC.replace('1 errors', '2 errors').replace('## Failed checks', row + '\n## Failed checks')
        text += '\n### 2. Second check (error)\n**Info:**\nsecond fact\n'
        first_row = '| 1 | error | unknown.check | Unknown check | Observed fact |'
        swapped = text.replace(first_row + '\n' + row, row + '\n' + first_row)
        first = a.model(text)
        second = a.model(swapped)
        self.assertEqual({f['source_check_id']: f['id'] for f in first['findings']},
                         {f['source_check_id']: f['id'] for f in second['findings']})
        self.assertEqual({f['id'] for f in first['findings']}, {c['finding_id'] for c in first['check_results']})

    def test_metadata_missing_values_and_version_consistency(self):
        report = a.model(SYNTHETIC)
        for value in report['source_versions'].values():
            self.assertIsNone(value['value'])
            self.assertTrue(value['reason'])
        finding = report['findings'][0]
        self.assertEqual(finding['assessment'], 'unsupported')
        self.assertIsNone(finding['approval_required'])
        self.assertEqual(finding['affected'], [])
        self.assertEqual(sum(report['analysis_summary']['priority_counts'].values()), len(report['findings']))
        for body in (a.markdown(report), a.html_report(report)):
            self.assertIn(a.VERSION, body)
            self.assertIn(a.SCHEMA_VERSION, body)
        generation = a.save(report, self.protected_root())
        manifest = json.loads((generation / 'manifest.json').read_text())
        self.assertEqual(manifest['run_id'], generation.name)
        self.assertEqual(manifest['requested_formats'], ['html', 'markdown', 'json'])
        self.assertEqual(manifest['parser_version'], a.PARSER_VERSION)
        self.assertEqual(manifest['policy_version'], report['policy']['version'])

    def test_individually_passed_check_does_not_request_change(self):
        text = SYNTHETIC.replace('0 passed, 1 errors', '1 passed, 0 errors')
        text = text.replace('| error |', '| passed |').replace('(error)', '(passed)')
        text = text.replace('unknown.check', 'minio.cluster.min')
        report = a.model(text)
        finding = report['findings'][0]
        self.assertFalse(finding['approval_required'])
        self.assertEqual(finding['priority'], 'unclassified')
        self.assertEqual(finding['verification_steps'], [])
        self.assertEqual(report['official_summary']['passed'], 1)
        self.assertEqual(len(report['check_results']), 1)
        self.assertIn('변경 승인: 불필요', a.html_report(report))

    def test_processing_and_block_limits(self):
        with patch.object(a, 'MAX_PROCESS_SECONDS', 0), self.assertRaises(a.InputError):
            a.model(SYNTHETIC)
        with patch.object(a, 'MAX_BLOCKS', 1), self.assertRaises(a.InputError):
            a.model(SYNTHETIC)

    def test_output_limit_keeps_previous_generation(self):
        root = self.protected_root()
        report = a.model(SYNTHETIC)
        a.save(report, root)
        before = (root / 'CURRENT.json').read_bytes()
        with patch.object(a, 'MAX_OUTPUT_BYTES', 1), self.assertRaises(a.OutputError):
            a.save(report, root, force=True)
        self.assertEqual((root / 'CURRENT.json').read_bytes(), before)

    def test_each_write_failure_preserves_previous_set(self):
        for target in ('aistor-diag-report.json', 'aistor-diag-report.md', 'aistor-diag-report.html', 'manifest.json', 'pointer'):
            with self.subTest(stage=target):
                root = self.protected_root()
                report = a.model(SYNTHETIC)
                old = a.save(report, root)
                before = (root / 'CURRENT.json').read_bytes()
                hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir()}
                original = a.write_exclusive
                def failing_write(path, data):
                    if path.name == target or target == 'pointer' and path.name.startswith('CURRENT-'):
                        raise OSError('synthetic write failure')
                    original(path, data)
                with patch.object(a, 'write_exclusive', side_effect=failing_write), self.assertRaises(a.OutputError):
                    a.save(report, root, force=True)
                self.assertEqual((root / 'CURRENT.json').read_bytes(), before)
                self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir()}, hashes)
                for path in root.glob('run-*/*'):
                    a.require_private(path)

    def test_corrupt_write_and_manifest_are_not_committed(self):
        for target in ('aistor-diag-report.html', 'manifest.json'):
            with self.subTest(target=target):
                root = self.protected_root()
                report = a.model(SYNTHETIC)
                a.save(report, root)
                before = (root / 'CURRENT.json').read_bytes()
                original = a.write_exclusive
                def corrupting_write(path, data):
                    if path.name == target:
                        data = data.replace('validated', 'invalid') if target == 'manifest.json' else data + 'tampered'
                    original(path, data)
                with patch.object(a, 'write_exclusive', side_effect=corrupting_write), self.assertRaises(a.OutputError):
                    a.save(report, root, force=True)
                self.assertEqual((root / 'CURRENT.json').read_bytes(), before)

    def test_fsync_failure_preserves_previous_pointer(self):
        root = self.protected_root()
        report = a.model(SYNTHETIC)
        a.save(report, root)
        before = (root / 'CURRENT.json').read_bytes()
        with patch.object(a.os, 'fsync', side_effect=OSError('synthetic fsync failure')), self.assertRaises(a.OutputError):
            a.save(report, root, force=True)
        self.assertEqual((root / 'CURRENT.json').read_bytes(), before)

    def test_insecure_and_hardlinked_lock_refused(self):
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                root = self.protected_root()
                lock = root / '.output.lock'
                a.write_exclusive(lock, '')
                if hardlink:
                    os.link(lock, root / 'lock-alias')
                else:
                    lock.chmod(0o644)
                with self.assertRaises(a.OutputError):
                    a.save(a.model(SYNTHETIC), root)
                self.assertFalse((root / 'CURRENT.json').exists())

    def test_real_concurrent_writers_conflict_without_overwrite(self):
        root = self.protected_root()
        old = a.save(a.model(SYNTHETIC), root)
        old_hash = hashlib.sha256((old / 'aistor-diag-report.json').read_bytes()).hexdigest()
        ctx = multiprocessing.get_context('spawn')
        ready, release = ctx.Event(), ctx.Event()
        process = ctx.Process(target=concurrent_writer, args=(root, ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10), 'concurrent writer did not acquire lock')
            with self.assertRaises(a.OutputError):
                a.save(a.model(SYNTHETIC), root, force=True)
        finally:
            release.set()
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(hashlib.sha256((old / 'aistor-diag-report.json').read_bytes()).hexdigest(), old_hash)
        pointer = json.loads((root / 'CURRENT.json').read_text())
        self.assertNotEqual(pointer['generation'], old.name)
        self.assertTrue((root / pointer['generation'] / 'manifest.json').is_file())

    def test_malicious_variants_mask_all_structured_outputs(self):
        attacks = ['<img src="https://example.com/tracker" onerror="alert(1)">',
                   '<iframe src="file:///etc/passwd"></iframe>', '</script><script>alert(1)</script>',
                   '[open](JaVaScRiPt:alert(1))', '![load](data:text/html,canary)',
                   '[[file:///etc/passwd]]', 'password="canary with spaces"', 'Authorization: Bearer canary-bearer']
        for attack in attacks:
            with self.subTest(attack=attack):
                report = a.model(SYNTHETIC.replace('Observed fact', attack))
                for body in (a.markdown(report), a.html_report(report), json.dumps(report)):
                    self.assertNotIn('canary with spaces', body)
                    self.assertNotIn('canary-bearer', body)
                html_body = a.html_report(report)
                self.assertNotIn('<img ', html_body)
                self.assertNotIn('<iframe ', html_body)
                self.assertNotIn('<script>alert(1)</script>', html_body)

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


TLS_MD = SYNTHETIC.replace('unknown.check', 'minio.net.tls.valid').replace(
    'host=test.svc.cluster.local:9000 password=canary-secret',
    'Certificate valid from 2026-01-01T00:00:00Z until 2026-10-21T01:56:42Z')


def unescaped_html_risks(md):
    """Independent check: outside code spans/fences, nothing may start HTML, an entity or a link."""
    risks, fence = [], None
    for line in md.split('\n'):
        opener = re.match(r'^(`{3,})', line)
        if fence:
            if line.strip() == fence:
                fence = None
            continue
        if opener:
            fence = opener[1]
            continue
        plain = re.sub(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)', '', line)
        risks += re.findall(r'(?<!\\)<[A-Za-z/!?][^\n]{0,20}', plain)
        risks += re.findall(r'(?<!\\)&(?:#\w+|[A-Za-z]\w*);', plain)
        risks += re.findall(r'(?<!\\)\]\(', plain)
    return risks


class Fix041Tests(unittest.TestCase):
    private_root = R1Tests.private_root
    protected_root = R1Tests.protected_root
    """0.4.1 customer-view regressions (TEST_LOG 8절)."""

    ENTITIES = ('&lt;', '&gt;', '&amp;', '&quot;', '&#')

    def test_markdown_has_no_html_entities(self):
        text = SYNTHETIC.replace('Observed fact', 'Cluster has < recommended (4) servers').replace(
            'host=test.svc.cluster.local:9000 password=canary-secret',
            'max_ptes_none [8 nodes per server pool] "quoted" it\'s `cat /proc/cmdline` a|b * c')
        md = a.markdown(a.model(text))
        for entity in self.ENTITIES:
            self.assertNotIn(entity, md)
        self.assertIn('max_ptes_none [8 nodes per server pool] "quoted" it\'s `cat /proc/cmdline`', md)
        self.assertIn('Cluster has < recommended (4) servers', md)

    def test_markdown_hostile_input_stays_inert(self):
        attacks = ['<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '[open](javascript:alert(1))',
                   '![load](data:text/html,x)', '&lt;b&gt; &#60;script&#62;', '<!-- hidden -->', '[ref]: javascript:alert(1)',
                   '`unclosed <script>', '``a ` b`` <iframe src=x>', '<http://evil.example/>', 'a\\<b> \\[x](y)']
        for attack in attacks:
            with self.subTest(attack=attack):
                report = a.model(SYNTHETIC.replace('Observed fact', attack).replace('host=test.svc', attack + ' host=test.svc'))
                md = a.markdown(report)
                self.assertEqual(unescaped_html_risks(md), [])
                rendered = a.html_report(report)
                for tag in ('<script>alert', '<img ', '<iframe ', '<!-- hidden'):
                    self.assertNotIn(tag, rendered)

    def test_markdown_code_blocks_are_fenced_and_unbreakable(self):
        text = SYNTHETIC.replace('# this is text, never executed', '```` <script>x</script>')
        md = a.markdown(a.model(text))
        self.assertIn('`````\n```` <script>x</script>', md)
        self.assertEqual(unescaped_html_risks(md), [])

    def test_md_line_rules(self):
        self.assertEqual(a.md_text('max_ptes_none'), 'max_ptes_none')
        self.assertEqual(a.md_text('_x_ *y*'), '\\_x\\_ \\*y\\*')
        self.assertEqual(a.md_text('# heading'), '\\# heading')
        self.assertEqual(a.md_text('1. item'), '1\\. item')
        self.assertEqual(a.md_text('a|b', cell=True), 'a\\|b')
        self.assertEqual(a.md_text('`a|b`', cell=True), '`a\\|b`')
        self.assertEqual(a.md_text('x\ny', cell=True), 'x y')
        self.assertEqual(a.md_text('[a](b)'), '[a\\](b)')
        self.assertEqual(a.md_text('C:\\dir\\file'), 'C:\\dir\\file')

    def test_public_reference_paths_and_urls_readable_in_both_levels(self):
        public = ['/proc/cmdline', '/etc/default/grub', '/etc/kernel/cmdline',
                  '/sys/kernel/mm/transparent_hugepage/enabled', '/sys/kernel/mm/transparent_hugepage/defrag',
                  '/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none',
                  'https://docs.min.io/enterprise/aistor-object-store/reference/aistor-server/requirements/',
                  'https://www.kernel.org/doc/html/latest/admin-guide/kernel-parameters.html?highlight=iommu',
                  'https://tip.golang.org/doc/gc-guide#Linux_transparent_huge_pages']
        for level in ('strict', 'basic'):
            m = a.Masker(level)
            for value in public:
                with self.subTest(level=level, value=value):
                    self.assertEqual(m.string('see `cat %s`.' % value), 'see `cat %s`.' % value)

    def test_identifiers_still_masked_in_both_levels(self):
        private = ['pool1.server1:9000', 'minio-1:9000', '10.20.30.40', '10.20.30.40:9000', 'node1.example.com',
                   'store-0.store-hl.ns.svc.cluster.local:9000', '/dev/disk/by-id/nvme-SAMSUNG_SERIAL123',
                   '/sys/class/net/eth0/address', '/sys/block/nvme0n1/device/serial', '/home/customer/data',
                   'https://subnet.min.io/cluster/abc', 'https://docs.min.io/x?host=10.1.2.3',
                   'https://docs.min.io/x/customer.internal/y', 'https://user@docs.min.io/x', 'fe80::1']
        for level in ('strict', 'basic'):
            m = a.Masker(level)
            for value in private:
                with self.subTest(level=level, value=value):
                    out = m.string('value %s end' % value)
                    self.assertNotIn(value, out)
                    self.assertIn('identifier-', out)

    def test_r3_style_server_names_masked_everywhere(self):
        text = SYNTHETIC.replace('host=test.svc.cluster.local:9000 password=canary-secret',
                                 '| Server | Value |\n|---|---|\n| pool1.server1:9000 | x |\n| pool1.server2:9000 | y |')
        for level in ('strict', 'basic'):
            report = a.model(text, level)
            for body in (json.dumps(report), a.markdown(report), a.html_report(report)):
                self.assertNotIn('pool1.server', body)

    def test_codes_have_korean_labels(self):
        report = a.model(SYNTHETIC)
        md, rendered = a.markdown(report), a.html_report(report)
        self.assertIn('우선순위: 미분류 (unclassified)', md)
        self.assertIn('평가 범위: 해설 미등록 (unsupported) / 이 도구에 등록된 해설 없음 (no_registered_explanation)', md)
        self.assertIn('공식 상태: 오류 (error)', md)
        self.assertIn('근거 구간: 정보(Info) (info)', md)
        self.assertIn('환경: 미지정 (unspecified)', md)
        self.assertNotRegex(md, r'우선순위: unclassified|평가 범위: unsupported')
        self.assertIn('<option value="unclassified">미분류 (unclassified)</option>', rendered)
        self.assertIn('data-priority="unclassified"', rendered)
        self.assertIn('<option value="error">오류 (error)</option>', rendered)
        known = a.model(SYNTHETIC.replace('unknown.check', 'sys.mem.iommu'))
        self.assertIn('평가 가능 (assessable) / 공식 보고서 결과만 반영(이 도구가 다시 판정하지 않음) (official_import_only)', a.markdown(known))

    def test_certificate_time_shows_collection_and_render_basis(self):
        report = a.model(TLS_MD)
        self.assertEqual(report['facts']['certificate_remaining_seconds'], 752393)
        with patch.object(a, 'now_utc', return_value=a.dt.datetime(2026, 11, 16, 0, 0, tzinfo=a.dt.timezone.utc)):
            md, rendered = a.markdown(report), a.html_report(report)
        for body in (md, rendered):
            self.assertIn('인증서 남은 시간(수집 시각 기준)', body)
            self.assertIn('752393초', body)
            self.assertIn('인증서 남은 시간(보고서 생성 시각 기준)', body)
            self.assertIn('이미 만료됨', body)
            self.assertIn('2026-11-16 09:00:00', body)
        with patch.object(a, 'now_utc', return_value=a.dt.datetime(2026, 10, 20, 1, 56, 42, tzinfo=a.dt.timezone.utc)):
            md = a.markdown(report)
        self.assertIn('86400초 (약 1.0일) (생성 시각: 한국 시각 2026-10-20 10:56:42)', md)
        explicit = a.model(TLS_MD, as_of='2026-10-20T01:56:42Z')
        self.assertIn('인증서 남은 시간(--as-of 지정 시각 기준)', a.markdown(explicit))

    def test_no_lock_file_left_and_directory_lock_conflicts(self):
        import fcntl
        root = self.protected_root()
        a.save(a.model(SYNTHETIC), root)
        self.assertFalse((root / a.LEGACY_LOCK).exists())
        fd = os.open(root, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(a.OutputError):
                a.save(a.model(SYNTHETIC), root, force=True)
        finally:
            os.close(fd)
        a.save(a.model(SYNTHETIC), root, force=True)
        self.assertFalse((root / a.LEGACY_LOCK).exists())

    def test_lock_file_fallback_when_directory_lock_unsupported(self):
        import errno
        import fcntl
        import stat as st
        root = self.protected_root()
        original = fcntl.flock
        def flock(fd, op):
            if st.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.ENOLCK, 'synthetic: no directory locks')
            return original(fd, op)
        with patch.object(fcntl, 'flock', side_effect=flock):
            a.save(a.model(SYNTHETIC), root)
        lock = root / a.LEGACY_LOCK
        self.assertTrue(lock.is_file())
        self.assertEqual(st.S_IMODE(lock.stat().st_mode), 0o600)

    def test_printed_result_path_is_absolute_and_normalized(self):
        root = self.private_root()
        (root / 'work').mkdir(mode=0o700)
        source = root / 'report.md'
        source.write_text(SYNTHETIC, encoding='utf-8')
        cwd = os.getcwd()
        out = StringIO()
        try:
            os.chdir(root / 'work')
            with contextlib.redirect_stdout(out):
                rc = a.main(['--official-report', '../report.md', '--out-dir', '../work/../result'])
        finally:
            os.chdir(cwd)
        self.assertEqual(rc, 0)
        line = next(x for x in out.getvalue().splitlines() if x.startswith('결과 세트 확정: '))
        printed = line.split(': ', 1)[1]
        self.assertTrue(os.path.isabs(printed))
        self.assertNotIn('..', printed)
        self.assertTrue(printed.startswith(str((root / 'result').resolve()) + '/run-'))

    def test_real_fixture_markdown_readable_and_masked(self):
        if not SAMPLE.is_file():
            self.skipTest('approved real fixture not available; not a pass')
        text = SAMPLE.read_text(encoding='utf-8')
        for level in ('strict', 'basic'):
            report = a.model(text, level)
            self.assertEqual(report['official_summary'], {'passed': 31, 'errors': 2, 'warnings': 2})
            md = a.markdown(report)
            for entity in self.ENTITIES:
                self.assertNotIn(entity, md)
            self.assertEqual(unescaped_html_risks(md), [])
            for public in ('/proc/cmdline', '/etc/default/grub', '/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none',
                           'https://docs.min.io/enterprise/aistor-object-store/reference/aistor-server/requirements/'):
                self.assertIn(public, md)
            for body in (md, a.html_report(report), json.dumps(report)):
                self.assertNotIn('svc.cluster.local', body)
                self.assertNotIn('primary-object-store-pool', body)


class Fix043Tests(unittest.TestCase):
    """0.4.3: 0.4.2 간소화 중 발견한 남은 항목."""

    @patch.object(a, 'MAX_PROCESS_SECONDS', 10)
    def test_parse_deadline_checked_inside_loops(self):
        # 시한을 넘긴 뒤에는 다음 반복에서 바로 멈춰야 한다(근거 블록 파싱 등 이후 작업을 하지 않음).
        clock = [0.0]
        real_cells, blocks = a.cells, []

        def late_cells(line):
            clock[0] = a.MAX_PROCESS_SECONDS + 1.0
            return real_cells(line)

        def counting_blocks(*args, **kwargs):
            blocks.append(1)
            return []

        with patch.object(a.time, 'monotonic', lambda: clock[0]), patch.object(a, 'cells', late_cells), \
                patch.object(a, 'evidence_blocks', counting_blocks):
            with self.assertRaises(a.InputError) as caught:
                a.parse_report(SYNTHETIC)
        self.assertIn('processing time limit exceeded', str(caught.exception))
        self.assertEqual(blocks, [])
        self.assertEqual(a.MAX_PROCESS_SECONDS, 10)

    def test_parse_result_unchanged_for_many_sections(self):
        # 구간 끝 계산을 바꿔도 결과가 같아야 한다: 상세 뒤에 다른 ## 구간이 오면 그 앞에서 끊는다.
        text = SYNTHETIC + '\n## Appendix\nnot part of the detail\n'
        summary, collected, checks, notes = a.parse_report(text)
        self.assertNotIn('not part of the detail', checks[0]['detail'])
        self.assertEqual(a.parse_report(SYNTHETIC)[2][0]['detail'], checks[0]['detail'].rstrip('\n'))


# 0.5.6: 0.5.4 보안 점검에서 새던 형태를 합성 값으로 재현한다. 모두 가짜 값이다.
CANARIES_056 = {
    'json_accesskey': ('"accessKey": "AKCANARY0001xyz"', 'AKCANARY0001xyz'),
    'json_secret_key': ('"secret_key":"skCanary0002Value"', 'skCanary0002Value'),
    'root_user': ('MINIO_ROOT_USER=canaryadmin03', 'canaryadmin03'),
    'root_password': ('MINIO_ROOT_PASSWORD=canaryPw0004', 'canaryPw0004'),
    'co_kr_host': ('connect node1.canary-five.co.kr failed', 'canary-five'),
    'io_host': ('endpoint api.canarysix.io down', 'canarysix'),
    'short_host': ('server canaryseven is offline', 'canaryseven'),
    'jwt': ('license eyJhbGciOiJFUzM4NCJ9.eyJzdWIiOiJjYW5hcnk4In0.c2lnbmF0dXJlOA', 'eyJzdWIiOiJjYW5hcnk4In0'),
    'cert_cn': ('subject=CN=canarynine.corp', 'canarynine'),
    'cert_san': ('X509v3 SAN: DNS:canaryten.internal.kr', 'canaryten'),
    'api_key': ('api_key=canaryEleven11', 'canaryEleven11'),
    'amz_credential': ('X-Amz-Credential=CANARY12AKID/20261119/us-east-1/s3/aws4_request', 'CANARY12AKID'),
    'subnet_api_key': ('SUBNET_API_KEY canary13subnetkey', 'canary13subnetkey'),
    'email': ('contact canary14@customer.kr', 'canary14'),
    'mac': ('eth0 ether 02:42:ac:11:00:0f', '02:42:ac:11:00:0f'),
    'uuid': ('deploymentID 7c3e9f10-1b2a-4c5d-8e6f-0a1b2c3d4e5f', '7c3e9f10-1b2a-4c5d-8e6f-0a1b2c3d4e5f'),
    'ipv4': ('peer 10.20.30.41 timeout', '10.20.30.41'),
    'ipv6': ('peer fd00:abcd::17 timeout', 'fd00:abcd::17'),
    'url': ('see https://canary19.customer.kr/console', 'canary19'),
    'bearer': ('Authorization: Bearer canary20bearertoken', 'canary20bearertoken'),
}
PEM_056 = '-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUCanaryPem056\n-----END CERTIFICATE-----'


def report_with_detail(detail):
    return SYNTHETIC.replace('host=test.svc.cluster.local:9000 password=canary-secret', detail)


class Fix056Tests(unittest.TestCase):
    """0.5.6: 마스킹 누락·ReDoS·PEM 입력·www 자동 링크."""

    def bodies(self, text, level='strict'):
        r = a.model(text, level)
        return {'json': json.dumps(r, ensure_ascii=False), 'md': a.markdown(r), 'html': a.html_report(r)}

    def test_detail_reaches_every_renderer(self):
        # 이 전제가 깨지면 아래 누락 테스트가 아무것도 증명하지 못한다.
        for name, body in self.bodies(report_with_detail('marker-visible-zz')).items():
            self.assertIn('marker-visible-zz', body, name)

    def test_canaries_masked_in_all_formats_and_levels(self):
        for level in ('strict', 'basic'):
            for name, (line, needle) in CANARIES_056.items():
                if level == 'basic' and name == 'short_host':
                    continue  # 포트 없는 짧은 이름은 strict 전용
                with self.subTest(level=level, canary=name):
                    for fmt, body in self.bodies(report_with_detail(line), level).items():
                        self.assertNotIn(needle, body, fmt)

    def test_canaries_masked_when_combined_on_one_line(self):
        line = ' | '.join(v[0] for v in CANARIES_056.values())
        for fmt, body in self.bodies(report_with_detail(line)).items():
            for name, (_, needle) in CANARIES_056.items():
                self.assertNotIn(needle, body, (fmt, name))

    def test_pem_block_masked_and_report_produced(self):
        for level in ('strict', 'basic'):
            r = a.model(report_with_detail('cert follows\n' + PEM_056 + '\ntail-visible'), level)
            for body in (json.dumps(r), a.markdown(r), a.html_report(r)):
                self.assertNotIn('CanaryPem056', body)
                self.assertNotIn('BEGIN CERTIFICATE', body)
                self.assertIn('tail-visible', body)
        unclosed = a.model(report_with_detail('-----BEGIN CERTIFICATE-----\nMIIBCanaryOpen056'))
        self.assertNotIn('CanaryOpen056', json.dumps(unclosed))
        key = a.model(report_with_detail('-----BEGIN PRIVATE KEY-----\nMIIECanaryKey056\n-----END PRIVATE KEY-----'))
        self.assertNotIn('CanaryKey056', json.dumps(key))

    def test_masking_is_linear_on_long_lines(self):
        # 0.5.4에서는 64KB 한 줄에 100초 이상 걸렸다.
        for unit in ('a.', 'a-', 'a.:', 'a@', 'eyJ.', 'a:', '/a', '1.', 'aB3+', 'server ', '"accessKey":'):
            text = unit * (65536 // len(unit))
            m = a.Masker('strict')
            started = a.time.monotonic()
            m.string(text)
            m.string(text + '.')
            self.assertLess(a.time.monotonic() - started, 3.0, unit)
        started = a.time.monotonic()
        a.model(report_with_detail('a.' * 32000))
        self.assertLess(a.time.monotonic() - started, 3.0)

    @patch.object(a, 'MAX_PROCESS_SECONDS', 10)
    def test_deadline_enforced_during_masking(self):
        m = a.Masker('strict', deadline=a.time.monotonic() - 1)
        with self.assertRaises(a.InputError):
            m.string('plain')
        m = a.Masker('strict', deadline=a.time.monotonic() - 1)
        with self.assertRaises(a.InputError):
            for _ in range(1024):
                m.tick()
        # model(): 파싱이 끝난 뒤 시간이 지나면 마스킹 단계에서 멈춘다.
        clock = [0.0]
        real_string = a.Masker.string

        def late_string(self, value):
            clock[0] = a.MAX_PROCESS_SECONDS + 1.0
            return real_string(self, value)

        with patch.object(a.time, 'monotonic', lambda: clock[0]), patch.object(a.Masker, 'string', late_string):
            with self.assertRaises(a.InputError) as caught:
                a.model(SYNTHETIC)
        self.assertIn('processing time limit exceeded', str(caught.exception))

    def test_www_autolink_escaped_in_markdown(self):
        self.assertEqual(a.md_line('see www.example.test now'), 'see www\\.example.test now')
        self.assertEqual(a.md_line('WWW.example.test'), 'WWW\\.example.test')
        self.assertEqual(a.md_line('(www.x.test)'), '(www\\.x.test)')
        for kept in ('https://www.kernel.org/doc', 'awww.x', 'a.www.x', 'wwwx'):
            self.assertNotIn('www\\.', a.md_line(kept), kept)

    def test_public_references_still_readable(self):
        text = report_with_detail('see https://docs.min.io/enterprise/aistor-object-store/ and node pool per drive')
        body = self.bodies(text)['md']
        self.assertIn('docs.min.io', body)
        self.assertIn('node pool per drive', body)

    def test_default_output_dir_matches_shell_entry(self):
        if 'AISTOR_DIAG_OUTPUT_DIR' not in os.environ:
            self.assertEqual(a.DEFAULT_OUTPUT_DIR, Path.home() / 'aistor-diag-output')
        if not (ROOT / 'run-report.sh').is_file():
            self.skipTest('run-report.sh not in this layout')
        shell = (ROOT / 'run-report.sh').read_text(encoding='utf-8')
        self.assertIn('OUT_DIR=$HOME/aistor-diag-output', shell)


class Fix057Tests(unittest.TestCase):
    """0.5.7: Shield 0.5.6 재점검 N1(콜론 뒤 IPv4로 보고서 전체 실패)·N2(과마스킹)·N3(누락). 모두 합성 값."""

    N1_LINES = ('tls peer X509v3 SAN: DNS:x.corp, IP Address:10.44.33.22', 'addr:10.1.2.3', 'host:10.0.0.5:9000',
                'DNS:x.corp, IP:10.9.9.9', 'inet addr:10.1.2.3  Bcast:10.1.2.255', 'peer:10.0.0.5', 'Address:192.168.0.1:9000',
                'IP Address:10.44.33.22, IP Address:10.44.33.23')

    def bodies(self, line, level='strict'):
        r = a.model(report_with_detail(line), level)
        return {'json': json.dumps(r, ensure_ascii=False), 'md': a.markdown(r), 'html': a.html_report(r)}

    def test_ipv4_after_colon_does_not_fail_report(self):
        for level in ('strict', 'basic'):
            for line in self.N1_LINES:
                with self.subTest(level=level, line=line):
                    for fmt, body in self.bodies(line, level).items():
                        for ip in ('10.44.33.2', '10.1.2.', '10.0.0.5', '10.9.9.9', '192.168.0.1'):
                            self.assertNotIn(ip, body, fmt)

    def test_ipv4_after_colon_cli_writes_report(self):
        try:
            root = Path(tempfile.mkdtemp(dir=a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')))
            a.secure_dir(root)
        except a.OutputError:
            self.skipTest('protected-storage integration blocked: backend owner/mode cannot be verified')
        source = root / 'ipcolon.md'
        source.write_text(report_with_detail(self.N1_LINES[0]), encoding='utf-8')
        out = root / 'out'
        with contextlib.redirect_stdout(StringIO()), contextlib.redirect_stderr(StringIO()):
            rc = a.main(['--official-report', str(source), '--out-dir', str(out)])
        self.assertIn(rc, (0, 4))
        self.assertTrue(list(out.glob('*/aistor-diag-report.html')))

    def test_host_port_still_masked(self):
        for line, needle in (('endpoint minio-7.example:9000', 'minio-7'), ('host:node3:9000 down', 'node3')):
            for fmt, body in self.bodies(line).items():
                self.assertNotIn(needle, body, (line, fmt))

    def test_overmasking_fixed(self):
        for line, kept in (('license: Enterprise', 'Enterprise'), ('drive.id mismatch', 'drive.id'), ('status.ok', 'status.ok'),
                           ('pool.id 3', 'pool.id'), ('Hostname resolution failed', 'resolution')):
            with self.subTest(line=line):
                self.assertIn(kept, self.bodies(line)['json'])
        md = self.bodies('subject (CN=minio) end')['md']
        self.assertRegex(md, r'\(CN=identifier-\d{3}\) end')
        self.assertNotIn('minio)', md)
        # 라이선스 키 값 자체는 계속 가린다.
        self.assertNotIn('canaryLic057', self.bodies('license: canaryLic057')['json'])

    def test_missed_shapes_now_masked(self):
        cases = (('--secret-key canaryN3a', 'canaryN3a'), ('--access-key=canaryN3b', 'canaryN3b'),
                 ('access key: canaryN3c', 'canaryN3c'), ('secret key = canaryN3d', 'canaryN3d'),
                 ('pass=canaryN3e', 'canaryN3e'), ('pwd: canaryN3f', 'canaryN3f'), ('accessKey => canaryN3g', 'canaryN3g'),
                 ('key AKIAIOSFODNN7CANARY', 'AKIAIOSFODNN7CANARY'),
                 ('minio://canaryuser:canarypw@10.1.1.1/x', 'canaryuser'), ('minio://canaryuser:canarypw@10.1.1.1/x', 'canarypw'),
                 ('qz_node14.customer.co.kr down', 'qz_node14'), ('mac 5254.00ab.cd24', '5254.00ab.cd24'))
        for level in ('strict', 'basic'):
            for line, needle in cases:
                with self.subTest(level=level, line=line):
                    for fmt, body in self.bodies(line, level).items():
                        self.assertNotIn(needle, body, fmt)

    def test_ordinary_words_not_treated_as_secrets(self):
        for line in ('bypass=1', 'passed: 3', 'compass=x', 'my_var.field', 'self.aliases.get', 'foo-secret-free text'):
            with self.subTest(line=line):
                self.assertNotIn('[REDACTED]', self.bodies(line)['json'])

    def test_new_patterns_linear(self):
        for unit in ('-a', '--a ', 'a key ', 'x://', 'x://a:', 'AKIA', 'abcd.', 'a=>', 'a_', 'Address:1', 'a:1.', 'a key: '):
            text = unit * (65536 // len(unit))
            started = a.time.monotonic()
            a.Masker('strict').string(text)
            self.assertLess(a.time.monotonic() - started, 3.0, unit)


class Fix058Tests(unittest.TestCase):
    """0.5.8: Shield 0.5.7 재점검 M1(앞 단어 수에 따라 자격증명 잔존)·M2(이름표 콜론 뒤 IPv6)·M3. 모두 합성 값."""

    # (키, 값 앞에 붙일 구분자 목록). 공백 구분은 환경변수형·CLI 플래그 키만 자격증명으로 본다.
    KEYS = (('secret_key', (': ', '=', ' => ')), ('MINIO_ROOT_PASSWORD', ('=', ': ', ' ')), ('--access-key', ('=', ' ')),
            ('access_key', ('=', ': ')), ('accessKey', (' => ', ': ')), ('secret key', (': ', ' = ')), ('pwd', (': ', '=')))
    PREFIXES = ('', 'for ', 'auth error for ', 'mc: <ERROR> Unable to init. ', 'env ', 'args: ', 'the ', 'error for the ')

    def bodies(self, line, level='strict'):
        r = a.model(report_with_detail(line), level)
        return {'json': json.dumps(r, ensure_ascii=False), 'md': a.markdown(r), 'html': a.html_report(r)}

    def test_credentials_masked_regardless_of_leading_words(self):
        n = 0
        for key, seps in self.KEYS:
            for sep in seps:
                for prefix in self.PREFIXES:
                    n += 1
                    value = 'QZm1v%03dx' % n
                    line = '%s%s%s%s is wrong' % (prefix, key, sep, value)
                    for level in ('strict', 'basic'):
                        with self.subTest(line=line, level=level):
                            for fmt, body in self.bodies(line, level).items():
                                self.assertNotIn(value, body, fmt)

    def test_leak_check_catches_embedded_credentials(self):
        # 사후 검증(fail-closed)도 앞 단어와 무관하게 남은 자격증명을 잡아야 한다.
        m = a.Masker('strict')
        for line in ('env MINIO_ROOT_PASSWORD=QZ6x', 'auth error for secret_key: QZ4x', 'for secret_key: QZ2x',
                     'mc: <ERROR> Unable to init. secret_key: QZ11x', 'args: --access-key=QZAK02abc', 'the access_key=QZ10x is wrong'):
            with self.subTest(line=line):
                self.assertIn('secret', m.leaks(line))
                with self.assertRaises(a.OutputError):
                    m.verify({'x': line})
                self.assertEqual(m.leaks(m.string(line)), [])

    def test_ipv6_after_label_colon_masked(self):
        for line in ('X509v3 SAN: DNS:a.corp, IP Address:fd00:abcd::1234', 'peer IP:fd00:abcd::1234 ok',
                     'addr:2001:db8::7 up', 'IP Address:fd00:abcd::1234, IP Address:10.44.33.22'):
            for level in ('strict', 'basic'):
                with self.subTest(line=line, level=level):
                    for fmt, body in self.bodies(line, level).items():
                        for needle in ('fd00', '2001:db8', '10.44.33'):
                            self.assertNotIn(needle, body, fmt)
        self.assertIn('ipv6', a.Masker('strict').leaks('IP Address:fd00:abcd::1234'))
        # IPv6가 아닌 콜론 값(시각, 포트)은 그대로 둔다.
        self.assertIn('started 12:30:45 ok', self.bodies('started 12:30:45 ok')['json'])

    def test_m3_rare_combination_and_pass_count(self):
        for fmt, body in self.bodies('/10.44.33.22 failed access key: QZm3a').items():
            self.assertNotIn('QZm3a', body, fmt)
            self.assertNotIn('10.44.33', body, fmt)
        self.assertIn('pass: 12 checks', self.bodies('pass: 12 checks')['md'])
        for line, value in (('pass: QZm3pw', 'QZm3pw'), ('pass=1234', '1234'), ('pass: 5678', '5678')):
            with self.subTest(line=line):
                self.assertNotIn(value, self.bodies(line)['json'])

    def test_secret_search_linear(self):
        for unit in ('CN=', 'a ', 'k=', 'for ', 'env X_PASSWORD=', 'IP:fd00::1 ', 'a b=', 'x: y '):
            text = unit * (262144 // len(unit))
            started = a.time.monotonic()
            a.Masker('strict').string(text)
            self.assertLess(a.time.monotonic() - started, 3.0, unit)



class Fix059Tests(unittest.TestCase):
    """0.5.9: Shield 0.5.8 재점검 R1(16진수 글자로 끝나는 이름표 뒤 IPv6)·R2(mc alias 위치 인자 키)·R3. 모두 합성 값."""

    LABELS = ('src', 'remote', 'source', 'interface', 'id', 'peer_id', 'bind', 'grid', 'advertise', 'ipv6', 'ip6', 'IPv6',
              'node', 'IP Address', 'addr')

    def bodies(self, line, level='strict'):
        r = a.model(report_with_detail(line), level)
        return {'json': json.dumps(r, ensure_ascii=False), 'md': a.markdown(r), 'html': a.html_report(r)}

    def assert_hidden(self, line, needles, levels=('strict', 'basic')):
        for level in levels:
            for fmt, body in self.bodies(line, level).items():
                for needle in needles:
                    self.assertNotIn(needle, body, (line, level, fmt))

    def test_ipv6_after_any_label_masked(self):
        for label in self.LABELS:
            for line in ('%s:fd00:abcd::1234 ok' % label, 'peer %s:2001:db8:0:1::7, next' % label):
                with self.subTest(line=line):
                    self.assert_hidden(line, ('fd00', '2001:db8'))
                    self.assertIn('ipv6', a.Masker('strict').leaks(line))
        # IPv6가 아닌 콜론 값은 그대로 둔다.
        for kept in ('started 12:30:45 ok', 'sha256:abcdef'):
            self.assertIn(kept, self.bodies(kept)['json'])

    def test_mc_alias_positional_keys_masked(self):
        cases = ('mc alias set qzalias http://10.0.0.1:9000 QZAKr2a01 QZSKr2b01',
                 'run: mc alias set qzalias https://minio.qz.co.kr:9000 "QZAKr2a02" "QZSKr2b02" --api S3v4',
                 'mc config host add qzalias http://10.0.0.1:9000 QZAKr2a03 QZSKr2b03',
                 'mc --insecure alias set qzalias http://10.0.0.1:9000 QZAKr2a04 QZSKr2b04',
                 'mc.exe alias set qzalias http://x:9000 QZAKr2a05 QZSKr2b05')
        for line in cases:
            with self.subTest(line=line):
                self.assert_hidden(line, ('QZAKr2', 'QZSKr2'))
                self.assertIn('secret', a.Masker('strict').leaks(line))
        # 문서 예시 자리표시자는 그대로 둔다.
        self.assertIn('<ACCESS_KEY>', self.bodies('mc alias set myminio http://x:9000 <ACCESS_KEY> <SECRET_KEY>')['json'])

    def test_r3_shapes_masked(self):
        for line, needle in (('"password": "QZr3a01', 'QZr3a01'), ("'secret': 'QZr3a02 trailing", 'QZr3a02'),
                             ('password: "QZr3a03 more', 'QZr3a03'), ('pass: 1234 ok', '1234'), ('PASS: 9999 chars', '9999'),
                             ('mac 52:54:00:aa:bb:c1-1 up', '52:54:00'), ('52-54-00-aa-bb-c2-2 down', '52-54-00')):
            with self.subTest(line=line):
                self.assert_hidden(line, (needle,))
        self.assertIn('pass: 12 checks', self.bodies('pass: 12 checks')['md'])
        self.assertEqual(a.Masker('strict').leaks(a.Masker('strict').string('"password": "QZr3a01')), [])

    def test_new_rules_linear(self):
        for unit in ('id:fd00::1 ', 'a:', 'mc alias set a b ', 'mc -q ', 'aa-bb-cc-dd-ee-ff-1 ', '"password": "x ', 'x_:'):
            text = unit * (262144 // len(unit))
            started = a.time.monotonic()
            a.Masker('strict').string(text)
            self.assertLess(a.time.monotonic() - started, 3.0, unit)


class Fix0510Tests(Fix059Tests):
    """0.5.10: Shield 0.5.9 재점검 S1(mc 명령 변형의 위치 인자 키)·S2·S3·R2 보고서 버전. 모두 합성 값."""

    K1, K2 = 'QZAKs1x01', 'QZSKs1y02'

    def s1_cases(self):
        k1, k2, cont = self.K1, self.K2, chr(92) + chr(10)
        return (
            'mcli alias set m http://h:9000 %s %s' % (k1, k2),
            'mc alias set --api S3v4 m http://h:9000 %s %s' % (k1, k2),
            'mc --config-dir /x alias set m http://h:9000 %s %s' % (k1, k2),
            'mc -C /x alias set m http://h:9000 %s %s' % (k1, k2),
            'mc alias set m http://h:9000 %s  %s %s' % (cont, k1, k2),
            'mc alias set m %s http://h:9000 %s %s %s --api s3v4' % (cont, k1, cont, k2),
            "mc alias set m http://h:9000 %s '$Abc%s'" % (k1, k2),
            'mc alias set m http://h:9000 %s "${Abc%s}"' % (k1, k2),
            'mc alias set m http://h:9000 %s "<Abc%s>"' % (k1, k2),
            'mc alias set m http://h:9000 %s XX_YY%s_KEY' % (k1, k2),
            'mc admin user add m %s %s' % (k1, k2),
            'mcli admin user add m %s %s' % (k1, k2),
            '/usr/local/bin/mcli --insecure alias set m https://h:9000 %s %s' % (k1, k2),
            '| `MCLI ALIAS SET m http://h:9000 "%s" "%s"` | x |' % (k1, k2),
            'mc  alias  set  --path  auto  m  http://h:9000  %s  %s' % (k1, k2),
        )

    def test_s1_mc_variants_masked(self):
        for line in self.s1_cases():
            with self.subTest(line=line):
                self.assert_hidden(line, (self.K1[:6], self.K2[:6]))
                self.assertIn('secret', a.Masker('strict').leaks(line))
                masked = a.Masker('strict').string(line)
                self.assertEqual(a.Masker('strict').leaks(masked), [], masked)

    def test_s1_placeholders_and_other_mc_lines_kept(self):
        for line in ('mc alias set m http://h:9000 <ACCESS_KEY> <SECRET_KEY>',
                     'mc alias set m http://h:9000 <access-key> <secret-key>',
                     'mc ls m/bucket', 'mc admin info m', 'mcli alias list'):
            with self.subTest(line=line):
                out = a.Masker('basic').string(line)
                self.assertNotIn('[REDACTED]', out)
                self.assertEqual(out.split()[-2:], line.split()[-2:])
                self.assertNotIn('secret', a.Masker('strict').leaks(line))

    def test_s2_s3_shapes_masked(self):
        for line, needle in (('52-54-00-aa-bb-cc-10 down', '52-54-00'), ('nic 52:54:00:aa:bb:cd-ab up', '52:54:00'),
                             ('"password": "Qz Pw34x', 'Pw34x'), ('peer 0:2001:db8:0:0:0:0:0:7 up', '2001:db8')):
            with self.subTest(line=line):
                self.assert_hidden(line, (needle,))
        for kept in ('started 12:30:45 ok', 'sha256:abcdef'):
            self.assertIn(kept, self.bodies(kept)['json'])

    def test_r2_report_version_follows_masker(self):
        import r2_health as h
        self.assertEqual(h.VERSION, a.VERSION.rsplit('-', 1)[0] + '-r2')

    def test_new_rules_linear(self):
        for unit in ('mc alias set a b ', 'mcli -C x alias set a ', 'mc --api ', 'a:', '"password": "x y '):
            text = unit * (262144 // len(unit))
            started = a.time.monotonic()
            a.Masker('strict').string(text)
            self.assertLess(a.time.monotonic() - started, 3.0, unit)


class Fix0511Tests(unittest.TestCase):
    """0.5.11: 입력 크기·처리 시간·출력 합계 한도를 기본에서 없앴다. 합성 값만 쓴다."""

    def big_report(self, mib):
        # 2 MiB·20,000줄을 넘는 상세(서버별 관측 줄 반복)를 만든다.
        line = '| node-%06d.qz.example.com:9000 | drive /data%d ok | password=QZbig%06d |'
        rows, size, i = [], 0, 0
        while size < mib * 1024 * 1024:
            row = line % (i, i % 16, i)
            rows.append(row)
            size += len(row) + 1
            i += 1
        return report_with_detail('| Server | Value | Note |' + chr(10) + '|---|---|---|' + chr(10) + chr(10).join(rows))

    def test_defaults_have_no_size_or_time_limit(self):
        for name in ('MAX_BYTES', 'MAX_LINES', 'MAX_BLOCKS', 'MAX_OUTPUT_BYTES', 'MAX_PROCESS_SECONDS'):
            self.assertIsNone(getattr(a, name), name)
        self.assertEqual(a.MAX_CHECKS, 1000)

    def test_large_report_saved_through_cli(self):
        text = self.big_report(3)
        self.assertGreater(len(text.encode('utf-8')), 2 * 1024 * 1024)
        self.assertGreater(text.count(chr(10)), 20000)
        root = R1Tests.private_root(self)
        src = root / 'big.md'
        src.write_text(text, encoding='utf-8')
        try:
            a.secure_dir(root)
            protected = True
        except a.OutputError:
            protected = False
        with contextlib.redirect_stderr(StringIO()), contextlib.redirect_stdout(StringIO()):
            rc = a.main(['--official-report', str(src), '--out-dir', str(root / 'reports')])
        if not protected:
            self.skipTest('protected-storage integration blocked: backend owner/mode cannot be verified')
        self.assertEqual(rc, 0)
        pointer = json.loads((root / 'reports/CURRENT.json').read_text(encoding='utf-8'))
        body = (root / 'reports' / pointer['generation'] / 'aistor-diag-report.md').read_text(encoding='utf-8')
        self.assertNotIn('QZbig', body)
        self.assertNotIn('qz.example.com', body)

    def test_limits_still_apply_when_set(self):
        with patch.object(a, 'MAX_LINES', 10), self.assertRaises(a.InputError):
            a.model(self.big_report(0.01))
        with patch.object(a, 'MAX_PROCESS_SECONDS', 0), self.assertRaises(a.InputError):
            a.model(SYNTHETIC)

if __name__ == '__main__':
    unittest.main(verbosity=2)
