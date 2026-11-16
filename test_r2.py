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
    'tls': {'tls_enabled': True, 'certs': [{'not_before': '2026-01-01T00:00:00Z', 'not_after': '2027-01-01T00:00:00Z', 'subject': 'canary-subject'}]}}}, 'sys': {'config': [{'addr': 'secret.internal:9000', 'config': {'proc-cmdline': ['intel_iommu=on'], 'thp-config': {'enabled': 'always madvise [never]'}}}], 'errors': [{'addr': 'secret.internal:9000'}]}}


PAIRED_MD = '''# MinIO Health Diagnostics Report
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
| Server | Value |
|---|---|
| secret.internal:9000 | x |
'''


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
        self.assertIn(r['run_status'], ('success', 'partial'))
        self.assertIsNone(r['official_summary'])
        self.assertTrue(all(c['origin'] == 'custom_rule' and c['official_status'] is None for c in r['check_results']))

    def test_paired_official_preserved(self):
        if not REAL.is_file() or not MD.is_file():
            self.skipTest("approved paired fixtures unavailable; not a pass")
        body, count = h.read(REAL)
        r = h.report(body, count, MD.read_text())
        self.assertEqual(r['official_summary'], {'passed': 31, 'errors': 2, 'warnings': 2})
        self.assertEqual(len([c for c in r['check_results'] if c['origin'] == 'official_import']), 4)
        self.assertEqual(r['input_link']['status'], 'consistent_observations')
        self.assertFalse(r['input_link']['same_collection_proven'])

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

    def test_system_thp_and_iommu(self):
        body = copy.deepcopy(SYNTHETIC)
        config = body['sys']['config'][0]['config']
        config['thp-config']['enabled'] = '[always] madvise never'
        results = {c['rule_id']: c for c in h.report(body)['check_results']}
        self.assertEqual(results['r2.thp.mode']['rule_result'], 'warning')
        self.assertEqual(results['r2.iommu.consistency']['rule_result'], 'pass')

    def test_absent_iommu_not_off(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['config'][0]['config']['proc-cmdline'] = ['other=canary-secret']
        r = h.report(body)
        self.assertEqual(r['facts']['health']['system_configurations'][0]['iommu_flags'], {})
        self.assertIsNone(next(c for c in r['check_results'] if c['rule_id'] == 'r2.iommu.consistency')['rule_result'])
        self.assertNotIn('canary-secret', json.dumps(r))

    def test_collection_errors_redacted(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['errors'][0]['error'] = 'canary-password https://secret.internal'
        r = h.report(body)
        self.assertEqual(r['facts']['health']['collection']['reported_error_count'], 1)
        self.assertNotIn('canary-password', json.dumps(r))
        self.assertEqual(next(c for c in r['check_results'] if c['rule_id'] == 'r2.collection.errors')['rule_result'], 'warning')

    def test_topology_not_quorum(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['servers'][0]['drives'][0].update(pool_index=0, set_index=1)
        topo = h.report(body)['facts']['health']['topology']
        self.assertEqual(topo['groups'][0]['observed_drive_records'], 1)
        self.assertIsNone(topo['quorum'])
        self.assertIsNone(topo['physical_failure_domains'])

    def test_missing_server_version_does_not_block(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['minio']['info']['servers'][0]['version']
        r = h.report(body)
        self.assertEqual(r['run_status'], 'success')
        self.assertFalse(r['completeness']['requested_scope_satisfied'])
        self.assertIsNone(next(c for c in r['check_results'] if c['rule_id'] == 'r2.version.consistency')['rule_result'])

    def test_header_extension_not_version_gate(self):
        text = json.dumps({'version': '999', 'newMetadata': 'canary-secret'}) + '\n' + json.dumps(SYNTHETIC)
        r = h.report(*h.decode(gzip.compress(text.encode())))
        self.assertEqual(r['run_status'], 'success')
        self.assertNotIn('canary-secret', json.dumps(r))

    def test_pair_time_conflict_rejected(self):
        if not REAL.is_file() or not MD.is_file():
            self.skipTest('approved paired fixtures unavailable; not a pass')
        body, count = h.read(REAL)
        body['timestamp'] = '2026-10-01T08:56:49Z'
        with self.assertRaises(a.InputError):
            h.report(body, count, MD.read_text())

    def test_pair_targets_conflict_rejected(self):
        if not REAL.is_file() or not MD.is_file():
            self.skipTest('approved paired fixtures unavailable; not a pass')
        body, count = h.read(REAL)
        body['minio']['info']['servers'][0]['endpoint'] = 'different.internal:9000'
        with self.assertRaises(a.InputError):
            h.report(body, count, MD.read_text())

    def test_thp_unknown_not_defaulted(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['config'][0]['config']['thp-config']['enabled'] = 'canary-secret [never]'
        r = h.report(body)
        self.assertIsNone(r['facts']['health']['system_configurations'][0]['thp_enabled'])
        self.assertNotIn('canary-secret', json.dumps(r))

    def test_r2_filter_labels_and_units(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['servers'][0]['drives'][0]['usedspace'] = 101
        r = h.report(body)
        rendered = h.html_report(r)
        self.assertIn('<option>용량</option>', rendered)
        # 0.4.1 의도된 변경: 필터 값은 그대로, 보이는 글자는 한국어(fail → 실패).
        self.assertIn('<option value="해당 없음 / 자체 결과: fail">해당 없음 / 자체 결과: 실패</option>', rendered)
        self.assertTrue(all(c['official_status'] is None for c in r['check_results']))
        self.assertIn('raw source counters', r['facts']['health']['capacity_unit'])

    def test_preparse_node_limit_before_decoder(self):
        payload = gzip.compress(json.dumps({'values': [0] * 100}).encode())
        with patch.object(h, 'MAX_NODES', 5), patch.object(h.json, 'JSONDecoder', side_effect=AssertionError('decoder must not run')), self.assertRaises(a.InputError):
            h.decode(payload)

    def test_preparse_long_string_before_decoder(self):
        payload = gzip.compress(json.dumps({'x': 'z' * 100}).encode())
        with patch.object(h, 'MAX_STRING', 2), patch.object(h.json, 'JSONDecoder', side_effect=AssertionError('decoder must not run')), self.assertRaises(a.InputError):
            h.decode(payload)

    def test_unicode_surrogate_escape_within_decoded_limit(self):
        payload = gzip.compress(b'{"x":"\\ud83d\\ude00"}')
        with patch.object(h, 'MAX_STRING', 1):
            body, count = h.decode(payload)
        self.assertEqual(body, {'x': '\U0001f600'})
        self.assertEqual(count, 1)

    def test_preparse_long_atom_before_decoder(self):
        payload = gzip.compress(('{"x":0.' + '0' * 100 + '1}').encode())
        with patch.object(h, 'MAX_STRING', 2), patch.object(h.json, 'JSONDecoder', side_effect=AssertionError('decoder must not run')), self.assertRaises(a.InputError):
            h.decode(payload)

    def test_missing_header_and_producer_versions_not_gating(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['version']
        del body['minio']['info']['servers'][0]['version']
        data = gzip.compress(('{}\n' + json.dumps(body)).encode())
        r = h.report(*h.decode(data))
        self.assertEqual(r['run_status'], 'success')
        self.assertIsNone(r['input_metadata']['health']['format_version'])
        self.assertIsNone(next(c for c in r['check_results'] if c['rule_id'] == 'r2.version.consistency')['rule_result'])

    def test_observation_tables_do_not_change_diagnoses(self):
        report = h.report(copy.deepcopy(SYNTHETIC))
        before = copy.deepcopy(report)
        rendered = h.html_report(report)
        self.assertEqual(report, before)
        for label in ('원본 관측 정보', '서버 기록', '드라이브 기록', '원본 수집 오류', '수집 범위·미지원·결합 제한'):
            self.assertIn(label, rendered)
        self.assertEqual(rendered.count('<article '), len(report['findings']))

    def test_six_collection_errors_shown_separately(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['cpus'] = [{'error': 'canary-cpu-secret'} for _ in range(3)]
        body['sys']['netinfo'] = [{'error': 'canary-nic-secret'} for _ in range(3)]
        report = h.report(body)
        rendered = h.html_report(report)
        self.assertEqual(report['facts']['health']['collection']['reported_error_count'], 6)
        self.assertIn('오류가 기록된 행 수: <strong>6</strong>', rendered)
        self.assertIn('<td>CPU 정보</td><td>3</td><td>3</td>', rendered)
        self.assertIn('<td>네트워크 정보</td><td>3</td><td>3</td>', rendered)
        self.assertNotIn('canary-cpu-secret', rendered)
        self.assertNotIn('canary-nic-secret', rendered)
        self.assertIsNone(report['official_summary'])

    def test_missing_observations_not_zero(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['minio']['info']['servers'][0]['drives'][0]['totalspace']
        del body['sys']['errors']
        report = h.report(body)
        self.assertIsNone(report['facts']['health']['collection']['reported_error_count'])
        rendered = h.observations_html(report)
        self.assertIn('오류가 기록된 행 수: <strong>미제공 / 미평가</strong>', rendered)
        self.assertIn('미제공 / 미지원', rendered)

    def test_observation_table_escapes_untrusted_values(self):
        report = h.report(SYNTHETIC)
        report['facts']['health']['servers'][0]['id'] = '<img src=x onerror=alert(1)>'
        rendered = h.observations_html(report)
        self.assertNotIn('<img', rendered)
        self.assertIn('&lt;img', rendered)

    def test_actual_collection_counts_when_fixture_present(self):
        if not REAL.is_file():
            self.skipTest('approved actual health fixture unavailable; not a pass')
        report = h.report(*h.read(REAL))
        collection = report['facts']['health']['collection']
        self.assertEqual(collection['reported_error_count'], 6)
        self.assertEqual(collection['sections']['cpus']['reported_errors'], 3)
        self.assertEqual(collection['sections']['netinfo']['reported_errors'], 3)
        self.assertIn('오류가 기록된 행 수: <strong>6</strong>', h.html_report(report))

    # -- 0.4.0 개선 회귀 테스트 ------------------------------------------------
    def workdir(self):
        import tempfile
        parent = a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')
        return Path(tempfile.mkdtemp(prefix='r2-040-', dir=parent))

    def run_main(self, *argv):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = h.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_markdown_and_html_have_no_internal_structures(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['cpus'] = [{'error': 'open /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor: no such file or directory canary-cpu'}]
        r = h.report(body, 1, PAIRED_MD)
        md, rendered = h.markdown_report(r), h.html_report(r)
        for text in (md, rendered):
            self.assertNotIn("{'", text)
            self.assertNotIn('{&quot;', text)
            self.assertNotIn('{&#x27;', text)
            self.assertNotIn('canary', text)
            self.assertNotIn('secret.internal', text)
            self.assertIn('통과 0 · 오류 1 · 경고 0 (총 1)', text)
            self.assertIn('원본 관측 정보', text)
            self.assertIn('입력 결합 확인', text)
            self.assertIn('자체 점검 결과', text)
        self.assertNotIn('<pre>{', rendered)
        self.assertIn('| 항목 | 값 |', md)
        self.assertIn('| 수집 영역 | 제공 기록 수 | 오류 기록 행 수 | 입력 지원 상태 | 원본 JSON 위치 |', md)
        r1 = a.markdown(a.model(PAIRED_MD))
        self.assertIn('## 한계', r1)
        self.assertIn('| 범위 | 상태 |', r1)
        self.assertNotIn("{'", r1)

    def test_thp_compared_with_official_recommendation(self):
        def thp(**values):
            body = copy.deepcopy(SYNTHETIC)
            body['sys']['config'][0]['config']['thp-config'] = values
            return next(c for c in h.report(body)['check_results'] if c['rule_id'] == 'r2.thp.mode')
        ok = thp(enabled='always [madvise] never', defrag='always defer [defer+madvise] madvise never', max_ptes_none='0')
        self.assertEqual(ok['rule_result'], 'pass')
        self.assertEqual(ok['rule_version'], 'r2-system-2')
        self.assertIn('공식 sys.mem.thp', ok['condition'])
        self.assertIsNone(ok['official_status'])
        self.assertEqual(thp(enabled='always [madvise] never', defrag='[madvise]', max_ptes_none='511')['rule_result'], 'warning')
        self.assertEqual(thp(enabled='[never]', defrag='[madvise]', max_ptes_none='0')['rule_result'], 'warning')
        self.assertEqual(thp(enabled='[always] madvise never')['rule_result'], 'warning')
        missing = thp(enabled='always [madvise] never')
        self.assertIsNone(missing['rule_result'])
        self.assertEqual(missing['assessment'], 'insufficient_data')

    def test_collection_error_types_fixed_labels_only(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['cpus'] = [{'error': 'open /sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor: no such file or directory' % i} for i in range(3)]
        body['sys']['netinfo'] = [{'error': 'Error getting ring parameters for canary-eth0: operation not supported'} for _ in range(3)]
        body['sys']['partitions'] = [{'error': 'canary-host-secret permission denied'}, {'errors': ['weird canary']}]
        r = h.report(body)
        types = r['facts']['health']['collection']['error_type_counts']
        self.assertEqual(types, {'CPU 주파수 정책(cpufreq/scaling_governor) / 파일 없음': 3, 'NIC ring 파라미터 / 미지원': 3,
                                 '미분류 대상 / 권한 거부': 1, '미분류 대상 / 기타': 1})
        dumped = json.dumps(r, ensure_ascii=False) + h.html_report(r) + h.markdown_report(r)
        for leaked in ('canary', '/sys/devices', 'scaling_governor:'):
            self.assertNotIn(leaked, dumped)
        self.assertIn('원인 확정 아님', h.html_report(r))

    def test_sys_errors_missing_reports_lower_bound_not_zero(self):
        body = copy.deepcopy(SYNTHETIC)
        del body['sys']['errors']
        body['sys']['cpus'] = [{'error': 'x'}, {'error': 'y'}, 'not-a-record']
        r = h.report(body)
        collection = r['facts']['health']['collection']
        self.assertIsNone(collection['reported_error_count'])
        self.assertEqual(collection['observed_error_rows'], 2)
        self.assertEqual(collection['count_basis'], 'lower_bound')
        self.assertEqual(collection['incomplete_reasons'], ['sys_errors_missing_or_not_list', 'malformed_records:cpus'])
        rule = next(c for c in r['check_results'] if c['rule_id'] == 'r2.collection.errors')
        self.assertEqual((rule['rule_result'], rule['reason']), ('warning', 'lower_bound_error_rows_observed'))
        self.assertEqual(r['run_status'], 'partial')
        self.assertIn('collection error count incomplete', r['parser_errors'])
        self.assertIn('오류가 기록된 행 수: <strong>최소 2 (전체 수 미확정)</strong>', h.observations_html(r))
        body['sys']['errors'] = 'broken'
        body['sys']['cpus'] = []
        r = h.report(body)
        self.assertIsNone(next(c for c in r['check_results'] if c['rule_id'] == 'r2.collection.errors')['rule_result'])
        self.assertIn('오류가 기록된 행 수: <strong>미제공 / 미평가</strong>', h.observations_html(r))

    def test_common_options_mask_lang_fail_on_priority(self):
        work = self.workdir()
        health = work / 'h.json.gz'
        health.write_bytes(packed(SYNTHETIC))
        rc, out, err = self.run_main('--health', str(health), '--out-dir', str(work / 'o1'), '--lang', 'ko', '--mask', 'basic', '--fail-on-priority', 'P2')
        self.assertEqual(rc, 1, err)
        self.assertIn('요약: 공식 판정 공식 Markdown 미지정', out)
        current = json.loads((work / 'o1/CURRENT.json').read_text())
        saved = json.loads((work / 'o1' / current['generation'] / 'aistor-diag-report.json').read_text())
        self.assertEqual(saved['masking']['level'], 'basic')
        self.assertEqual(saved['schema_version'], h.SCHEMA_VERSION)
        rc, _, err = self.run_main('--health', str(health), '--out-dir', str(work / 'o2'), '--fail-on-priority', 'P1')
        self.assertEqual(rc, 0, err)
        body = copy.deepcopy(SYNTHETIC)
        del body['sys']['errors']
        health.write_bytes(packed(body))
        rc, _, _ = self.run_main('--health', str(health), '--out-dir', str(work / 'o3'), '--allow-partial', '--fail-on-priority', 'P2')
        self.assertEqual(rc, 4)

    def test_time_conflict_message_is_specific_and_safe(self):
        work = self.workdir()
        body = copy.deepcopy(SYNTHETIC)
        body['timestamp'] = '2026-10-12T08:54:08Z'
        (work / 'h.json.gz').write_bytes(packed(body))
        (work / 'r.md').write_text(PAIRED_MD, encoding='utf-8')
        rc, out, err = self.run_main('--health', str(work / 'h.json.gz'), '--official-report', str(work / 'r.md'), '--out-dir', str(work / 'o'))
        self.assertEqual(rc, 2)
        self.assertIn('161초', err)
        self.assertIn('허용 120초', err)
        self.assertIn('생성 시각', err)
        self.assertIn('따로 분석', err)
        self.assertNotIn('secret.internal', err + out)
        self.assertFalse((work / 'o').exists())
        body['timestamp'] = '2026-10-12T08:54:48Z'  # 121초: 거부
        with self.assertRaises(a.InputError):
            h.report(body, 1, PAIRED_MD)
        body['timestamp'] = '2026-10-12T08:54:49Z'  # 정확히 120초: 허용
        self.assertEqual(h.report(body, 1, PAIRED_MD)['input_link']['status'], 'consistent_observations')

    def test_target_conflict_message_counts_only(self):
        work = self.workdir()
        (work / 'h.json.gz').write_bytes(packed(SYNTHETIC))
        (work / 'r.md').write_text(PAIRED_MD.replace('secret.internal:9000', 'other.internal:9000'), encoding='utf-8')
        rc, out, err = self.run_main('--health', str(work / 'h.json.gz'), '--official-report', str(work / 'r.md'), '--out-dir', str(work / 'o'))
        self.assertEqual(rc, 2)
        self.assertIn('서버 대상 불일치: Markdown 1개 / gzip 1개 / 공통 0개', err)
        self.assertNotIn('other.internal', err + out)
        self.assertNotIn('secret.internal', err + out)

    def test_input_error_guidance_without_raw_text(self):
        work = self.workdir()
        bad = work / 'not-gzip.md'
        bad.write_text('canary-secret', encoding='utf-8')
        rc, _, err = self.run_main('--health', str(bad), '--out-dir', str(work / 'o'))
        self.assertEqual(rc, 2)
        self.assertIn('원본 gzip(--health)', err)
        self.assertIn('gzip 파일이 아니거나', err)
        self.assertNotIn('canary-secret', err)
        rc, _, err = self.run_main('--health', str(work / 'missing.gz'), '--out-dir', str(work / 'o'))
        self.assertEqual(rc, 2)
        self.assertIn('파일이 없습니다', err)
        self.assertNotIn('missing.gz', err)
        lines = a.describe_input_error(PermissionError(13, 'Permission denied: /secret/canary'), 'X')
        self.assertIn('읽을 권한이 없습니다', lines[0])
        self.assertNotIn('canary', ' '.join(lines))
        self.assertNotIn('chmod', ' '.join(lines))

    def test_no_network_or_shell_imports(self):
        tree = ast.parse(Path(h.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(n.name.split('.')[0] in {'socket', 'urllib', 'http', 'requests', 'subprocess'} for n in node.names))


    # -- 0.4.1 고객 관점 회귀 테스트 --------------------------------------------
    def test_unevaluated_rule_shows_korean_not_none(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['config'][0]['config']['proc-cmdline'] = []
        r = h.report(body, 1, PAIRED_MD)
        iommu = next(f for f in r['findings'] if f['id'] == 'r2.iommu.consistency')
        self.assertIsNone(iommu['rule_result'])
        md, rendered = h.markdown_report(r), h.html_report(r)
        for text in (md, rendered, iommu['observed']):
            self.assertNotIn('None', text)
            self.assertIn('자체 점검 결과: 미평가 / 명시적 IOMMU 옵션이 관측되지 않았거나 모호함', text)
        self.assertIn('공식 상태: 해당 없음 / 자체 결과: 미평가', md)

    def test_r2_markdown_has_no_entities_and_filters_keep_values(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['servers'][0]['drives'][0]['usedspace'] = 101
        r = h.report(body, 1, PAIRED_MD)
        md, rendered = h.markdown_report(r), h.html_report(r)
        for entity in ('&lt;', '&gt;', '&quot;', '&#'):
            self.assertNotIn(entity, md)
        self.assertIn('<option value="해당 없음 / 자체 결과: fail">해당 없음 / 자체 결과: 실패</option>', rendered)
        self.assertIn('data-status="해당 없음 / 자체 결과: fail"', rendered)
        self.assertIn('자체 점검 결과: 실패 / 제공된 용량 카운터 범위만 확인 (provided_counter_bounds_only)', md)
        self.assertNotIn('secret.internal', md + rendered)

    def test_real_combined_readable_when_fixture_present(self):
        if not (REAL.is_file() and MD.is_file()):
            self.skipTest('approved actual fixtures unavailable; not a pass')
        body, count = h.read(REAL)
        r = h.report(body, count, MD.read_text(encoding='utf-8'))
        self.assertEqual(r['official_summary'], {'passed': 31, 'errors': 2, 'warnings': 2})
        self.assertEqual(r['analysis_summary']['custom_rule_counts'], {'pass': 4, 'warning': 2, 'not_evaluated': 1})
        self.assertEqual(r['facts']['health']['collection']['reported_error_count'], 6)
        md, rendered = h.markdown_report(r), h.html_report(r)
        for entity in ('&lt;', '&gt;', '&quot;', '&#'):
            self.assertNotIn(entity, md)
        for text in (md, rendered):
            self.assertNotIn('결과: None', text)
            self.assertIn('/proc/cmdline', text)
            self.assertNotIn('svc.cluster.local', text)

    # -- 0.4.3 남은 항목 회귀 테스트 --------------------------------------------
    TLS_PAIRED_MD = PAIRED_MD.replace('unknown.check', 'minio.net.tls.valid').replace(
        '**Info:**', 'Certificate valid from 2026-01-01T00:00:00Z until 2026-10-21T01:56:42Z\n**Info:**')

    def test_combined_as_of_reaches_official_part(self):
        # 0.4.3: 결합 실행의 --as-of가 공식 보고서 부분(인증서 남은 시간·기준 시각)에도 적용된다.
        explicit = h.report(copy.deepcopy(SYNTHETIC), 1, self.TLS_PAIRED_MD, '2026-10-20T01:56:42Z')
        meta, facts = explicit['input_metadata'], explicit['facts']
        self.assertEqual(meta['as_of_origin'], 'explicit')
        self.assertEqual(meta['as_of'], '2026-10-20T01:56:42+00:00')
        self.assertEqual(facts['certificate_remaining_seconds'], 86400)
        self.assertIn('인증서 남은 시간(--as-of 지정 시각 기준)', h.markdown_report(explicit))
        # --as-of 없음: 지금과 같이 수집 시각 기준.
        default = h.report(copy.deepcopy(SYNTHETIC), 1, self.TLS_PAIRED_MD)
        self.assertEqual(default['input_metadata']['as_of_origin'], 'collection')
        self.assertEqual(default['facts']['certificate_remaining_seconds'],
                         int((a.aware_time('2026-10-21T01:56:42Z') - a.aware_time('2026-10-12T08:56:49Z')).total_seconds()))

    def test_input_hardlinked_as_current_json_refused_like_r1(self):
        # 0.4.3: R1과 같은 공용 점검. 입력이 출력 폴더의 CURRENT.json과 같은 파일(하드링크)이면 종료 코드 3.
        for kind in ('official', 'health'):
            with self.subTest(kind=kind):
                root = self.workdir()
                md, gz, out = root / 'in.md', root / 'in.json.gz', a.secure_dir(root / 'out')
                md.write_text(PAIRED_MD, encoding='utf-8')
                gz.write_bytes(packed(SYNTHETIC))
                os.link(md if kind == 'official' else gz, out / 'CURRENT.json')
                before = (md.read_bytes(), gz.read_bytes())
                rc, stdout, err = self.run_main('--health', str(gz), '--official-report', str(md),
                                                '--out-dir', str(out), '--allow-partial', '--force')
                self.assertEqual(rc, 3, stdout + err)
                self.assertIn(a.OUTPUT_FAILURE_MESSAGE, err)
                self.assertEqual((md.read_bytes(), gz.read_bytes()), before)
                self.assertEqual([p.name for p in out.iterdir()], ['CURRENT.json'])
                # R1도 같은 공용 함수·메시지를 쓴다.
                if kind == 'official':
                    import contextlib, io
                    e1 = io.StringIO()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(e1):
                        self.assertEqual(a.main(['--official-report', str(md), '--out-dir', str(out), '--force']), 3)
                    self.assertIn(a.OUTPUT_FAILURE_MESSAGE, e1.getvalue())


def summary_md(results, statuses):
    """Summary 줄과 상세 행 수가 맞는 합성 공식 Markdown(실제 고객 원본 아님)."""
    rows = ''.join('| %d | %s | c%d.check | Check %d | Observed |\n' % (i + 1, s, i, i) for i, s in enumerate(statuses))
    details = ''.join('### %d. Check %d (%s)\n**Info:**\n| Server | Value |\n|---|---|\n| secret.internal:9000 | x |\n'
                      % (i + 1, i, s) for i, s in enumerate(statuses))
    return ('# MinIO Health Diagnostics Report\n## Summary\n* Date: Mon, 12 Oct 2026 08:56:49 UTC\n* Results: %s\n'
            '## Errors\n| Index | Status | Check | Name | Description |\n|---|---|---|---|---|\n%s## Failed checks\n%s'
            % (results, rows, details))


class Fix044Tests(unittest.TestCase):
    """0.4.4: 201 서버 새 수집(단수형 Summary)과 0.4.3 고객 테스트에서 발견한 문제."""
    CASES = [  # (Summary 줄, 상세 상태들, 기대 요약)
        ('32 passed, 2 errors, 1 warning', ['error', 'error', 'warning'], (32, 2, 1)),  # 201 서버 수집과 같은 형태
        ('0 passed, 1 error, 0 warnings', ['error'], (0, 1, 0)),
        ('1 passed, 0 errors, 1 warning', ['warning'], (1, 0, 1)),
        ('1 passed, 1 error, 1 warning', ['error', 'warning'], (1, 1, 1)),
        ('31 passed, 2 errors, 2 warnings', ['error', 'error', 'warning', 'warning'], (31, 2, 2)),
    ]

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o700)
        self.addCleanup(self._tmp.cleanup)

    def run_main(self, main, *argv):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_singular_and_plural_summary_standalone_and_combined(self):
        gz = self.root / 'in.json.gz'
        gz.write_bytes(packed(SYNTHETIC))
        for n, (line, statuses, expected) in enumerate(self.CASES):
            with self.subTest(summary=line):
                text = summary_md(line, statuses)
                want = dict(zip(('passed', 'errors', 'warnings'), expected))
                standalone = a.model(text)
                self.assertEqual(standalone['official_summary'], want)
                self.assertNotIn('summary/detail mismatch', standalone['parser_errors'])
                combined = h.report(copy.deepcopy(SYNTHETIC), 1, text)
                self.assertEqual(combined['official_summary'], want)
                self.assertEqual(combined['input_link']['status'], 'consistent_observations')
                md = self.root / ('in-%d.md' % n)
                md.write_text(text, encoding='utf-8')
                rc, _, err = self.run_main(a.main, '--official-report', str(md), '--out-dir', str(self.root / ('r1-%d' % n)))
                self.assertEqual(rc, 0, err)
                rc, _, err = self.run_main(h.main, '--official-report', str(md), '--health', str(gz),
                                           '--out-dir', str(self.root / ('r2-%d' % n)), '--allow-partial')
                self.assertIn(rc, (0, 4), err)
                saved = json.loads((self.root / ('r2-%d' % n) / 'CURRENT.json').read_text())
                report = json.loads((self.root / ('r2-%d' % n) / saved['generation'] / 'aistor-diag-report.json').read_text()) \
                    if 'generation' in saved else None
                if report is not None:
                    self.assertEqual(report['official_summary'], want)

    def test_unrecognized_summary_still_refused(self):
        for line in ('1 passed, 1 errorz, 0 warnings', '1 passed, 1 error, 0 warningsx', '1 passed'):
            with self.subTest(summary=line), self.assertRaises(a.InputError):
                a.model(summary_md(line, ['error']))

    def test_markdown_parse_failure_names_markdown_input(self):
        gz, md = self.root / 'in.json.gz', self.root / 'in.md'
        gz.write_bytes(packed(SYNTHETIC))
        md.write_text(summary_md('1 passed', ['error']), encoding='utf-8')  # Summary 줄 인식 불가
        rc, _, err = self.run_main(h.main, '--official-report', str(md), '--health', str(gz),
                                   '--out-dir', str(self.root / 'out'), '--allow-partial')
        self.assertEqual(rc, 2)
        self.assertIn('공식 Markdown(--official-report)', err)
        self.assertIn('Summary 줄', err)
        self.assertNotIn('원본 gzip(--health)', err)
        # 반대로 원본 gzip이 잘못되면 원본 gzip을 지목한다.
        md.write_text(PAIRED_MD, encoding='utf-8')
        gz.write_bytes(b'not gzip')
        rc, _, err = self.run_main(h.main, '--official-report', str(md), '--health', str(gz),
                                   '--out-dir', str(self.root / 'out'), '--allow-partial')
        self.assertEqual(rc, 2)
        self.assertIn('원본 gzip(--health)', err)
        self.assertNotIn('공식 Markdown(--official-report)', err)

    def test_markdown_table_cell_has_no_line_start_backslash(self):
        r = h.report(copy.deepcopy(SYNTHETIC), 1, PAIRED_MD, '2026-10-20T01:56:42Z')
        text = h.markdown_report(r)
        self.assertIn('| 기준 시각 출처 | --as-of 지정 |', text)
        self.assertNotIn('\\--as-of', text)
        self.assertNotIn('\\--as-of', a.markdown(a.model(PAIRED_MD, as_of='2026-10-20T01:56:42Z')))
        # 표 칸의 인라인 이스케이프(HTML·링크·|)는 그대로.
        cell = a.md_text('- <b>x</b> | [y](javascript:z)', cell=True)
        self.assertTrue(cell.startswith('- '))
        self.assertEqual(cell, '- \\<b>x\\</b> \\| [y\\](javascript:z)')

    def test_collision_message_states_cause(self):
        md, gz = self.root / 'in.md', self.root / 'in.json.gz'
        md.write_text(PAIRED_MD, encoding='utf-8')
        gz.write_bytes(packed(SYNTHETIC))
        for kind, label in (('official', '공식 Markdown(--official-report)'), ('health', '원본 gzip(--health)')):
            with self.subTest(kind=kind):
                out = a.secure_dir(self.root / ('out-' + kind))
                os.link(md if kind == 'official' else gz, out / 'CURRENT.json')
                rc, _, err = self.run_main(h.main, '--health', str(gz), '--official-report', str(md),
                                           '--out-dir', str(out), '--allow-partial', '--force')
                self.assertEqual(rc, 3)
                self.assertTrue(err.startswith(a.OUTPUT_FAILURE_MESSAGE), err)
                self.assertIn('원인: ' + label + ' 파일이 출력 폴더의 최신 결과 포인터(CURRENT.json)와 같은 파일(하드링크)', err)
                self.assertIn('조치:', err)
                self.assertNotIn(str(self.root), err)  # 경로는 출력하지 않음
        rc, _, err = self.run_main(a.main, '--official-report', str(md), '--out-dir', str(self.root / 'out-official'), '--force')
        self.assertEqual(rc, 3)
        self.assertIn('원인: 공식 Markdown(--official-report) 파일이 출력 폴더의 최신 결과 포인터(CURRENT.json)와 같은 파일(하드링크)', err)
        # 입력이 출력 폴더 자체인 경우
        rc, _, err = self.run_main(a.main, '--official-report', str(md), '--out-dir', str(md), '--force')
        self.assertEqual(rc, 3)
        self.assertIn('출력 폴더(--out-dir) 자체', err)


if __name__ == '__main__':
    unittest.main(verbosity=2)
