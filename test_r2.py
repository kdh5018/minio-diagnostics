"""R2 real fixture and synthetic bounds/security tests. No source copies or cleanup."""
import ast
import copy
import gzip
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import aistordoctor as a
import r2_health as h

ROOT = Path(__file__).resolve().parent
REAL = Path(os.environ.get('AISTOR_DIAG_TEST_HEALTH', str(ROOT / 'fixtures/health.json.gz')))
MD = Path(os.environ.get('AISTOR_DIAG_TEST_REPORT', str(ROOT / 'fixtures/official-report.md')))
SYNTHETIC = {'version': '3', 'timestamp': '2026-10-12T08:56:45Z', 'minio': {'info': {
    'servers': [{'version': '2026-08-07T18:34:35Z', 'endpoint': 'secret.internal:9000',
                 'minio_env_vars': {'MINIO_ROOT_PASSWORD': 'canary-secret'},
                 'license': {'key': 'canary-license'},
                 'drives': [{'state': 'ok', 'totalspace': 100, 'usedspace': 20, 'availspace': 80,
                             'endpoint': '/private/data', 'uuid': 'canary-uuid'}]}],
    'tls': {'tls_enabled': True, 'certs': [{'not_before': '2026-01-01T00:00:00Z', 'not_after': '2027-01-01T00:00:00Z', 'subject': 'canary-subject'}]}}}}


def packed(body, header=True):
    text = (json.dumps({'version': '3'}) + '\n' if header else '') + json.dumps(body)
    return gzip.compress(text.encode())


class R2Tests(unittest.TestCase):
    def test_real_fixture(self):
        if not REAL.is_file():
            self.skipTest("approved real health fixture unavailable; not a pass")
        body, count = h.read(REAL)
        r = h.report(body, count)
        self.assertEqual(count, 2)
        self.assertEqual(len(r['facts']['health']['servers']), 3)
        self.assertEqual(len(r['facts']['health']['drives']), 12)
        self.assertEqual(r['run_status'], 'success')
        self.assertIsNone(r['official_summary'])
        self.assertTrue(all(c['origin'] == 'custom_rule' and c['official_status'] is None for c in r['check_results']))

    def test_paired_official_preserved(self):
        if not REAL.is_file() or not MD.is_file():
            self.skipTest("approved paired fixtures unavailable; not a pass")
        body, count = h.read(REAL)
        r = h.report(body, count, MD.read_text())
        self.assertEqual(r['official_summary'], {'passed': 31, 'errors': 2, 'warnings': 2})
        self.assertEqual(len([c for c in r['check_results'] if c['origin'] == 'official_import']), 4)
        self.assertEqual(r['input_link']['status'], 'independent_inputs')

    def test_single_and_concatenated_json(self):
        for header in (True, False):
            body, count = h.decode(packed(SYNTHETIC, header))
            self.assertEqual(count, 2 if header else 1)
            self.assertEqual(h.report(body, count)['run_status'], 'success')

    def test_unknown_version_not_gating(self):
        body = copy.deepcopy(SYNTHETIC)
        body['version'] = '999'
        r = h.report(*h.decode(packed(body)))
        self.assertEqual(r['run_status'], 'success')
        self.assertEqual(r['input_metadata']['health']['format_version'], '999')

    def test_missing_version_not_gating(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['version']
        self.assertEqual(h.report(body)['run_status'], 'success')

    def test_no_raw_sensitive_fields_in_any_output(self):
        r = h.report(SYNTHETIC)
        for body in (json.dumps(r), a.html_report(h.view(r)), a.markdown(h.view(r))):
            for canary in ('canary-secret', 'canary-license', 'canary-uuid', 'canary-subject', 'secret.internal', '/private/data', 'MINIO_ROOT_PASSWORD'):
                self.assertNotIn(canary, body)

    def test_missing_state_is_not_pass(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['minio']['info']['servers'][0]['drives'][0]['state']
        r = h.report(body)
        check = next(c for c in r['check_results'] if c['rule_id'] == 'r2.drive.state')
        self.assertIsNone(check['rule_result'])
        self.assertEqual(r['run_status'], 'partial')

    def test_offline_and_invalid_capacity(self):
        body = copy.deepcopy(SYNTHETIC)
        drive = body['minio']['info']['servers'][0]['drives'][0]
        drive.update(state='offline', usedspace=101)
        checks = {c['rule_id']: c for c in h.report(body)['check_results']}
        self.assertEqual(checks['r2.drive.state']['rule_result'], 'fail')
        self.assertEqual(checks['r2.capacity.bounds']['rule_result'], 'fail')

    def test_expired_certificate(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['tls']['certs'][0]['not_after'] = '2026-10-01T00:00:00Z'
        check = next(c for c in h.report(body)['check_results'] if c['rule_id'] == 'r2.tls.validity')
        self.assertEqual(check['rule_result'], 'fail')

    def test_version_difference(self):
        body = copy.deepcopy(SYNTHETIC)
        other = copy.deepcopy(body['minio']['info']['servers'][0])
        other['version'] = '2026-08-08T18:34:35Z'
        body['minio']['info']['servers'].append(other)
        check = next(c for c in h.report(body)['check_results'] if c['rule_id'] == 'r2.version.consistency')
        self.assertEqual(check['rule_result'], 'warning')

    def test_unsupported_structure_rejected(self):
        with self.assertRaises(a.InputError):
            h.report({'version': '999', 'other': {}})

    def test_truncated_gzip_rejected(self):
        with self.assertRaises(a.InputError):
            h.decode(packed(SYNTHETIC)[:-8])

    def test_compressed_and_expanded_limits(self):
        for constant in ('MAX_COMPRESSED', 'MAX_EXPANDED'):
            with patch.object(h, constant, 5), self.assertRaises(a.InputError):
                h.decode(packed(SYNTHETIC))

    def test_depth_and_string_limits(self):
        with patch.object(h, 'MAX_DEPTH', 2), self.assertRaises(a.InputError):
            h.decode(packed(SYNTHETIC))
        with patch.object(h, 'MAX_STRING', 3), self.assertRaises(a.InputError):
            h.decode(packed(SYNTHETIC))

    def test_duplicate_nan_and_extra_documents_rejected(self):
        for text in ('{"version":3,"version":4}', '{"value":NaN}', '{} {} {}'):
            with self.subTest(text=text), self.assertRaises(a.InputError):
                h.decode(gzip.compress(text.encode()))

    def test_time_and_node_limits(self):
        for constant in ('SECONDS', 'MAX_NODES'):
            with patch.object(h, constant, 0), self.assertRaises(a.InputError):
                h.decode(packed(SYNTHETIC))

    def test_missing_and_explicit_time(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['timestamp']
        self.assertEqual(h.report(body)['run_status'], 'partial')
        self.assertEqual(h.report(body, as_of='2026-10-12T00:00:00Z')['run_status'], 'success')
        with self.assertRaises(a.InputError):
            h.report(body, as_of='2026-10-12')

    def test_unknown_version_value_not_exported_as_secret(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['servers'][0]['version'] = 'canary-secret'
        self.assertNotIn('canary-secret', json.dumps(h.report(body)))

    def test_no_network_or_shell_imports(self):
        tree = ast.parse(Path(h.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(n.name.split('.')[0] in {'socket', 'urllib', 'http', 'requests', 'subprocess'} for n in node.names))


if __name__ == '__main__':
    unittest.main(verbosity=2)
