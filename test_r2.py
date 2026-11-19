"""R2 real fixture and synthetic bounds/security tests. No source copies or cleanup."""
import ast
from collections import Counter
import copy
import gzip
import json
import os
import re
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

PREVIEW_MD = PAIRED_MD.replace('0 passed, 1 errors, 0 warnings', '32 passed, 1 error, 1 warning').replace(
    '| secret.internal:9000 | x |',
    '| secret.internal:9000 | x |\n| other.internal:9000 | x |\n| third.internal:9000 | x |')


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

    def test_large_cluster_not_limited_by_default(self):
        # 0.5.11: 서버 1,200대·드라이브 24,000개·JSON 요소 20만 개 이상도 기본 한도에 걸리지 않는다.
        for name in ('SECONDS', 'MAX_NODES', 'MAX_SERVERS', 'MAX_DRIVES'):
            self.assertIsNone(getattr(h, name), name)
        self.assertEqual(h.MAX_EXPANDED, 256 * 1024 * 1024)  # 압축 폭탄 방어는 남긴다(0.5.12: 256 MiB).
        body = copy.deepcopy(SYNTHETIC)
        server = body['minio']['info']['servers'][0]
        body['minio']['info']['servers'] = [dict(server, endpoint='node-%04d.qz.example.com:9000' % n,
                                                 drives=[dict(server['drives'][0], endpoint='/data%d' % d, uuid='u-%d-%d' % (n, d))
                                                         for d in range(20)]) for n in range(1200)]
        decoded, count = h.decode(packed(body))
        self.assertEqual(len(decoded['minio']['info']['servers']), 1200)
        normalized, _ = h.normalize(decoded)
        self.assertEqual(len(normalized['servers']), 1200)
        self.assertEqual(len(normalized['drives']), 24000)
        r = h.report(decoded, count)
        page = json.dumps(r, ensure_ascii=False)
        self.assertNotIn('qz.example.com', page)
        self.assertNotIn('canary-secret', page)

    def test_gzip_bomb_still_rejected(self):
        bomb = gzip.compress(b' ' * (2 * 1024 * 1024))
        with patch.object(h, 'MAX_EXPANDED', 1024 * 1024), self.assertRaises(a.InputError):
            h.decode(bomb)

    def test_0512_bomb_limits_are_256_mib(self):
        # 0.5.12: 압축 파일·압축 해제 상한 256 MiB. 시간 한도는 다시 넣지 않는다.
        self.assertEqual(h.MAX_COMPRESSED, 256 * 1024 * 1024)
        self.assertEqual(h.MAX_EXPANDED, 256 * 1024 * 1024)
        self.assertIsNone(h.SECONDS)
        self.assertIsNone(a.MAX_PROCESS_SECONDS)

    def test_0512_expanded_over_256_mib_rejected_by_default(self):
        bomb = gzip.compress(b' ' * (256 * 1024 * 1024 + 1), compresslevel=1)
        self.assertLess(len(bomb), 2 * 1024 * 1024)
        with self.assertRaises(a.InputError) as caught:
            h.decode(bomb)
        del bomb
        # decode는 안쪽 상한 오류를 일반 입력 오류로 감싼다(예외 종류는 그대로). 안내는 안쪽 원인을 보인다.
        self.assertEqual(caught.exception.__cause__.args[0], 'expanded size limit')
        lines = a.describe_input_error(caught.exception, '원본 gzip')
        self.assertIn('256 MiB', lines[0])
        self.assertIn('분석 서버에서 실행', lines[0])

    def test_0512_compressed_over_256_mib_rejected_by_default(self):
        data = b'\x1f\x8b' + bytes(256 * 1024 * 1024 - 1)
        with self.assertRaises(a.InputError) as caught:
            h.decode(data)
        del data
        self.assertEqual(caught.exception.args[0], 'gzip signature or compressed limit')
        lines = a.describe_input_error(caught.exception, '원본 gzip')
        self.assertIn('256 MiB', lines[0])
        self.assertIn('분석 서버에서 실행', lines[0])

    def test_0512_no_time_limit_even_when_clock_jumps(self):
        # 시계가 호출마다 1시간씩 흘러도(아주 느린 서버) 시간 때문에 거부하지 않는다.
        import itertools
        clock = itertools.count(0, 3600)
        with patch('time.monotonic', side_effect=lambda: float(next(clock))):
            decoded, count = h.decode(packed(SYNTHETIC))
            r = h.report(decoded, count)
        self.assertNotIn('canary-secret', json.dumps(r, ensure_ascii=False))
        self.assertNotIn('secret.internal', json.dumps(r, ensure_ascii=False))

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
        self.assertIsNone(next(c for c in r['check_results'] if c.get('rule_id') == 'r2.iommu.consistency')['rule_result'])
        self.assertNotIn('canary-secret', json.dumps(r))

    def test_collection_errors_redacted(self):
        body = copy.deepcopy(SYNTHETIC)
        body['sys']['errors'][0]['error'] = 'canary-password https://secret.internal'
        r = h.report(body)
        self.assertEqual(r['facts']['health']['collection']['reported_error_count'], 1)
        self.assertNotIn('canary-password', json.dumps(r))
        self.assertEqual(next(c for c in r['check_results'] if c['rule_id'] == 'r2.collection.errors')['rule_result'], 'info')  # 0.5.3

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
        # 0.5.5 의도된 변경: 자체 점검 카드가 없으므로 자체 결과 필터 값도 없다.
        self.assertNotIn('자체 결과', rendered)
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
        for label in ('원본 관측 정보', '서버 기록', '드라이브 기록', '수집되지 않은 항목', '수집 범위·미지원·결합 제한'):
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
            self.assertNotIn('자체 점검 결과', text)  # 0.5.5: 자체 판정 표시 없음
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
        self.assertEqual((rule['rule_result'], rule['reason']), ('info', 'lower_bound_error_rows_observed'))  # 0.5.3
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
        self.assertEqual(rc, 0, err)  # 0.5.5: 공식 카드만 셈 — gzip 단독은 공식 카드 0장
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
        iommu = next(c for c in r['check_results'] if c.get('rule_id') == 'r2.iommu.consistency')
        self.assertIsNone(iommu['rule_result'])  # 내부 JSON에는 남는다
        self.assertFalse(any(f['id'] == 'r2.iommu.consistency' for f in r['findings']))
        md, rendered = h.markdown_report(r), h.html_report(r)
        for text in (md, rendered):
            self.assertNotIn('None', text)
            self.assertNotIn('자체 점검 결과', text)
            self.assertNotIn('자체 결과', text)

    def test_r2_markdown_has_no_entities_and_filters_keep_values(self):
        body = copy.deepcopy(SYNTHETIC)
        body['minio']['info']['servers'][0]['drives'][0]['usedspace'] = 101
        r = h.report(body, 1, PAIRED_MD)
        md, rendered = h.markdown_report(r), h.html_report(r)
        for entity in ('&lt;', '&gt;', '&quot;', '&#'):
            self.assertNotIn(entity, md)
        self.assertNotIn('자체 결과', rendered)  # 0.5.5: 자체 카드·필터 없음
        self.assertNotIn('자체 점검 결과', md)
        self.assertIn('data-status="error"', rendered)  # 공식 카드 필터 값은 유지
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


def html_body(page):
    """Markup outside the inline <style>/<script> blocks."""
    return re.sub(r'<style>.*?</style>|<script>.*?</script>', '', page, flags=re.S)


def preview_body():
    """합성 3서버 × 2드라이브: offline 1개, 사용률 3구간, THP·IOMMU·rlimit 불일치, 수집 오류 3종."""
    body = copy.deepcopy(SYNTHETIC)
    hosts = ['secret.internal:9000', 'other.internal:9000', 'third.internal:9000']
    servers = []
    for i, host in enumerate(hosts):
        server = copy.deepcopy(body['minio']['info']['servers'][0])
        server['endpoint'] = host
        server['drives'] = [dict(server['drives'][0], usedspace=u, availspace=100 - u, pool_index=0, set_index=0, disk_index=i * 2 + j)
                            for j, u in enumerate(((10, 75), (95, 20), (30, 40))[i])]
        servers.append(server)
    servers[2]['drives'][1]['state'] = 'offline'
    body['minio']['info']['servers'] = servers
    thp = [{'enabled': 'always [madvise] never', 'defrag': '[madvise]', 'max_ptes_none': '0'},
           {'enabled': '[always] madvise never', 'defrag': '[madvise]', 'max_ptes_none': '511'},
           {'enabled': 'always [madvise] never', 'defrag': '[madvise]', 'max_ptes_none': '0'}]
    body['sys']['config'] = [{'addr': host, 'config': {'proc-cmdline': ['intel_iommu=off' if i == 2 else 'intel_iommu=on'],
                                                       'thp-config': thp[i], 'rlimit-max': 4096 if i == 1 else 1048576}}
                             for i, host in enumerate(hosts)]
    body['sys']['cpus'] = [{'error': 'open /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor: no such file or directory canary-cpu'}] * 3
    body['sys']['netinfo'] = [{'error': 'Error getting ring parameters for canary-eth0: operation not supported'}] * 2 + [{}]
    return body


class Html050Tests(unittest.TestCase):
    """0.5.0 도식 화면: 표시 계층만 바뀌고 판정·마스킹·Markdown은 그대로여야 한다."""

    def setUp(self):
        self.r = h.report(preview_body(), 1, PREVIEW_MD)
        self.page = h.html_report(self.r)

    def test_single_file_without_external_resources(self):
        for page in (self.page, a.html_report(a.model(PAIRED_MD))):
            body = html_body(page)
            for needle in ('<link', '<img', '<iframe', '<object', '<embed', 'src=', '@import', 'url(', '@font-face', 'innerHTML'):
                self.assertNotIn(needle, page)
            self.assertNotRegex(body, r'href="(?!#)')  # 목차 앵커만 허용
            self.assertIn("default-src 'none'", page)

    def test_dashboard_tiles_and_cautious_conclusion(self):
        page = self.page
        self.assertIn('id="dashboard"', page)
        self.assertIn('공식 판정(mc 보고서) 통과 32 · 오류 1 · 경고 1 (총 34)', page)
        self.assertRegex(page, r'tile tone-passed"><div class="tile-k"><span aria-hidden="true">✔</span> 통과</div><div class="tile-v">32<')
        conclusion = re.search(r'<p class="conclusion">(.*?)</p>', page).group(1)
        self.assertIn('확인이 필요합니다', conclusion)
        self.assertIn('현재 서비스 상태를 확정하지 않습니다', conclusion)
        self.assertNotRegex(conclusion, r'정상(입니다|이다|으로 판정)')
        for prio in ('P1', 'P2', 'P3', 'P4', '미분류'):
            self.assertIn('<div class="tile-k">%s</div>' % prio, page)
        clean = a.html_report(a.model(PAIRED_MD.replace('0 passed, 1 errors', '1 passed, 0 errors').replace('| error |', '| passed |').replace('(error)', '(passed)')))
        conclusion = re.search(r'<p class="conclusion">(.*?)</p>', clean).group(1)
        self.assertIn('보고된 오류·경고는 없습니다', conclusion)
        self.assertIn('확정하지 않습니다', conclusion)

    def test_drive_grid_shows_state_and_usage_with_text(self):
        page = self.page
        self.assertEqual(page.count('<li class="drv '), 6)
        self.assertIn('<span aria-hidden="true">✖</span> offline', page)
        self.assertIn('style="width:95.0%"', page)
        self.assertIn('fill u-hi', page)
        self.assertIn('fill u-mid', page)
        self.assertIn('fill u-lo', page)
        self.assertIn('사용 95.0%', page)
        self.assertIn('표시용 구간이며 판정 아님', page)
        self.assertNotIn('secret.internal', page)

    def test_config_table_highlights_mismatches(self):
        page = self.page
        self.assertIn('<td class="c-bad"><span aria-hidden="true">✖</span> <strong>511</strong> <small>권장과 다름</small></td>', page)
        self.assertIn('<td class="c-bad"><span aria-hidden="true">✖</span> <strong>always</strong> <small>권장과 다름</small></td>', page)
        self.assertIn('<strong>intel_iommu=off</strong> <small>다른 서버와 다름</small>', page)
        self.assertIn('<strong>4096</strong> <small>다른 서버와 다름</small>', page)
        self.assertIn('권장값과 다른 칸 2개', page)

    def test_collection_errors_chart_and_heatmap(self):
        page = self.page
        self.assertIn('오류 기록 행 3 / 기록 3', page)  # 0.5.5: 판정 기호 없음
        self.assertIn('오류 기록 행 2 / 기록 3', page)
        self.assertNotIn('✖ 오류 행', page)
        self.assertIn('<th scope="row">CPU 주파수 정책(cpufreq/scaling_governor)</th><td class="heat h4">3</td>', page)
        self.assertIn('<table class="heatmap">', page)
        self.assertNotIn('canary', page)

    def test_cards_separate_official_and_own_commentary(self):
        page = self.page
        cards = a.official_cards(self.r['findings'])
        self.assertEqual(page.count('<article '), len(cards))
        self.assertTrue(all(f.get('origin') != 'custom_rule' for f in cards))
        self.assertIn('공식 판정 <small>(mc 보고서)</small>', page)
        self.assertNotIn('자체 점검 판정', page)  # 0.5.5: 자체 판정 블록 없음
        self.assertNotIn('data-sev="own-', page)
        sev = re.findall(r'<article class="card sev-(\w+)" data-sev="\1"', page)
        self.assertEqual(len(sev), len(cards))
        order = {'error': 0, 'warning': 1, 'unknown': 2, 'info': 3, 'passed': 4}
        keys = [order[s] for s in sev]
        self.assertEqual(keys, sorted(keys))  # 오류·경고 우선

    def test_works_without_js_and_expands_for_print(self):
        page = self.page
        self.assertIn('<div class="filters" id="filters" hidden>', page)  # JS가 없으면 동작하지 않는 필터는 숨김
        for label in ('전체 ', '오류 ', '경고 ', '통과 '):
            self.assertIn(label, re.search(r'<div class="sevbtns".*?</div>', page).group(0))
        for section in ('dashboard', 'topology', 'config', 'uncollected', 'checks', 'raw', 'appendix'):
            self.assertIn('<a href="#%s">' % section, page)
            self.assertIn('id="%s"' % section, page)
        self.assertIn('<details class="appendix"><summary>부록: 확인된 정보·입력 결합·한계</summary>', page)
        self.assertIn('@media print', page)
        self.assertIn('details::details-content{content-visibility:visible!important;display:block!important}', page)
        self.assertIn('article[hidden]{display:block!important}', page)
        self.assertIn("addEventListener('beforeprint'", page)
        self.assertIn('@media (prefers-color-scheme:dark)', page)

    def test_rendering_is_display_only(self):
        before = copy.deepcopy(self.r)
        md = h.markdown_report(self.r)
        h.html_report(self.r)
        self.assertEqual(self.r, before)
        self.assertEqual(md, h.markdown_report(self.r))
        for level in ('strict', 'basic'):
            r = h.report(preview_body(), 1, PREVIEW_MD, mask=level)
            page = h.html_report(r)
            for leaked in ('secret.internal', 'other.internal', 'canary', '/private/data'):
                self.assertNotIn(leaked, page)

    def test_hostile_values_escaped_in_visual_sections(self):
        r = copy.deepcopy(self.r)
        n = r['facts']['health']
        n['servers'][0]['version'] = '<script>alert(1)</script>'
        n['drives'][0]['id'] = 'x-drive-<img src=x>'
        n['system_configurations'][0]['rlimit_max'] = '<b>9</b>'
        n['collection']['error_type_counts'] = {'<i>s</i> / <u>k</u>': 1}
        page = h.html_report(r)
        for raw in ('<script>alert', '<img', '<b>9', '<i>s', '<u>k'):
            self.assertNotIn(raw, page)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', page)


def replay_md(results, checks):
    """실제 수집본과 같은 형태(Summary 줄·검사 ID·상태)만 흉내 낸 합성 Markdown. 값은 모두 가짜."""
    hosts = ['secret.internal:9000', 'other.internal:9000', 'third.internal:9000']
    rows = ''.join('| %d | %s | %s | %s | Observed |\n' % (i + 1, s, cid, name) for i, (cid, name, s) in enumerate(checks))
    info = '**Info:**\n| Server | Value |\n|---|---|\n' + ''.join('| %s | x |\n' % host for host in hosts)
    details = ''.join('### %d. %s (%s)\n%s' % (i + 1, name, s, info) for i, (cid, name, s) in enumerate(checks))
    return ('# MinIO Health Diagnostics Report\n## Summary\n* Date: Mon, 12 Oct 2026 08:56:49 UTC\n* Results: %s\n'
            '## Errors\n| Index | Status | Check | Name | Description |\n|---|---|---|---|---|\n%s## Failed checks\n%s'
            % (results, rows, details))


class Replay050Tests(unittest.TestCase):
    """0.5.0: 11/16(단수형 1 warning)·10/15 수집본 형태를 합성으로 재현해 결합 실행 결과를 고정한다."""
    CASES = {
        '1116': ('32 passed, 2 errors, 1 warning',
                 [('minio.cluster.min', 'Cluster Size', 'error'), ('sys.mem.iommu', 'IOMMU', 'error'),
                  ('sys.mem.thp', 'THP', 'warning')], (32, 2, 1)),
        '1015': ('31 passed, 2 errors, 2 warnings',
                 [('minio.cluster.min', 'Cluster Size', 'error'), ('sys.mem.iommu', 'IOMMU', 'error'),
                  ('minio.net.tls.valid', 'TLS Certs Validity', 'warning'), ('sys.mem.thp', 'THP', 'warning')], (31, 2, 2)),
    }

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        os.chmod(self.root, 0o700)
        self.addCleanup(self._tmp.cleanup)

    def test_combined_run_saves_partial_report_with_rc4(self):
        import contextlib, io
        gz = self.root / 'in.json.gz'
        gz.write_bytes(packed(preview_body()))
        for label, (line, checks, (p, e, w)) in self.CASES.items():
            with self.subTest(case=label):
                md, out = self.root / ('%s.md' % label), self.root / ('out-%s' % label)
                md.write_text(replay_md(line, checks), encoding='utf-8')
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = h.main(['--official-report', str(md), '--health', str(gz), '--out-dir', str(out), '--allow-partial'])
                self.assertEqual(rc, 4, stderr.getvalue())  # 수집 오류가 있는 부분 분석
                generation = json.loads((out / 'CURRENT.json').read_text())['generation']
                saved = out / generation
                report = json.loads((saved / 'aistor-diag-report.json').read_text())
                self.assertEqual(report['run_status'], 'partial')
                self.assertEqual(report['official_summary'], {'passed': p, 'errors': e, 'warnings': w})
                self.assertEqual(report['input_link']['status'], 'consistent_observations')
                self.assertEqual(sorted(c['source_check_id'] for c in report['check_results'] if c['origin'] != 'custom_rule'),
                                 sorted(cid for cid, _, _ in checks))
                page = (saved / 'aistor-diag-report.html').read_text(encoding='utf-8')
                self.assertIn('공식 판정(mc 보고서) 통과 %d · 오류 %d · 경고 %d' % (p, e, w), page)
                self.assertIn('통과 %d · 오류 %d · 경고 %d' % (p, e, w), (saved / 'aistor-diag-report.md').read_text(encoding='utf-8'))
                self.assertNotIn('secret.internal', page)




def body_201():
    """201(11/16) 형태 합성: 3서버 모두 ok·같은 버전·유효 인증서, cmdline에 IOMMU 옵션 없음, THP always/madvise/511, CPU·NIC 수집 오류."""
    body = preview_body()
    for server in body['minio']['info']['servers']:
        for drive in server['drives']:
            drive.update(state='ok', totalspace=10737418240, usedspace=1073741824, availspace=9663676416)
    thp = {'enabled': '[always] madvise never', 'defrag': 'always defer defer+madvise [madvise] never', 'max_ptes_none': '511'}
    for config in body['sys']['config']:
        config['config'] = {'proc-cmdline': ['BOOT_IMAGE=(hd0,gpt2)/vmlinuz', 'ro', 'crashkernel=auto', 'quiet'],
                            'thp-config': dict(thp), 'rlimit-max': 1048576}
    body['sys']['cpus'] = [{'error': 'open /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor: no such file or directory canary-cpu'}] * 3
    body['sys']['netinfo'] = [{'error': 'Error getting ring parameters for canary-eth0: operation not supported'}] * 3
    return body


def card_texts(page):
    return re.findall(r'<article class="card.*?</article>', page, flags=re.S)


class Review054Tests(unittest.TestCase):
    """0.5.4: 공식 오류·경고 카드에 한글 해결방안(정적 사전)을 공식 판정 바로 아래에 두고, 영문 원문은 접어서 남긴다."""

    def setUp(self):
        line, checks, _ = Replay050Tests.CASES['1116']
        md = replay_md(line, checks).replace('### 3. THP (warning)\n', '### 3. THP (warning)\n**Resolution:**\nSet THP to madvise.\n**Info:**\n', 1)
        self.r = h.report(body_201(), 1, md)
        self.page, self.md = h.html_report(self.r), h.markdown_report(self.r)
        self.cards = card_texts(self.page)

    def official(self, cid):
        return next(f for f in self.r['findings'] if f.get('source_check_id') == cid)

    def test_official_buttons_unchanged(self):
        bar = re.search(r'<div class="sevbtns".*?</div>', self.page).group(0)
        b = {k: int(n) for k, n in re.findall(r'data-sev="([^"]*)"[^>]*>(?:<span[^>]*>[^<]*</span>)?[^<]*?(\d+)', bar)}
        self.assertEqual(self.r['official_summary'], {'passed': 32, 'errors': 2, 'warnings': 1})
        self.assertEqual((b['error'], b['warning']), (2, 1))
        self.assertEqual(len(re.findall(r'data-sev="warning"', self.page)), 2)  # 상자는 data-sev를 늘리지 않음
        self.assertEqual(len(self.cards), 3)  # 0.5.5: 공식 카드 3장만

    def test_korean_box_on_error_and_warning_cards_after_official_verdict(self):
        for cid in ('minio.cluster.min', 'sys.mem.iommu', 'sys.mem.thp'):
            f = self.official(cid)
            self.assertEqual(f['resolution_ko']['status'], 'registered')
            card = next(c for c in self.cards if 'data-status="%s"' % f['official_status'] in c and esc_title(f['title']) in c)
            self.assertEqual(card.count('해결방안(한글)'), 1)
            self.assertLess(card.index('공식 판정 <small>'), card.index('해결방안(한글)'))
            self.assertLess(card.index('해결방안(한글)'), card.index('이 도구의 해설'))
        iommu = next(c for c in self.cards if '명시적 IOMMU 부팅 옵션' in c)
        self.assertIn('grubby --update-kernel=ALL --args=&quot;iommu=pt&quot;', iommu)
        self.assertIn('cat /proc/cmdline', iommu)
        self.assertLess(iommu.index('해결방안(한글)'), iommu.index('서버별 관측값(원본 gzip)'))
        thp = next(c for c in self.cards if '<th scope="col">THP enabled</th>' in c)
        self.assertIn('일시 적용', thp)
        self.assertIn('영구 적용', thp)
        self.assertIn('tuned-adm profile minio-thp', thp)
        self.assertLess(thp.index('해결방안(한글)'), thp.index('서버별 관측값(원본 gzip)'))
        self.assertFalse(any('설정 변경 대상이 아니라' in c for c in self.cards))  # 자체 전용 카드 없음

    def test_english_resolution_collapsed(self):
        thp = next(c for c in self.cards if '<th scope="col">THP enabled</th>' in c)
        self.assertIn('<details class="res-en"><summary>공식 조치 안내(Resolution, 마스킹, 영문 원문)</summary>', thp)
        self.assertIn('Set THP to madvise.', thp)
        self.assertNotIn('<details open', self.page)
        self.assertLess(thp.index('해결방안(한글)'), thp.index('영문 원문'))

    def test_unregistered_and_passed_and_custom(self):
        self.assertEqual(a.resolution_ko('no.such.check')['status'], 'not_registered')
        self.assertEqual(a.resolution_ko('sys.mem.thp', passed=True)['status'], 'not_applicable')
        md = replay_md('0 passed, 1 errors, 0 warnings', [('no.such.check', 'Unknown', 'error')])
        page = a.html_report(a.model(md))
        self.assertIn('한글 해결방안 미등록 — 아래 공식 원문 참고', page)
        self.assertIn('한글 해결방안 미등록 — 아래 공식 원문 참고', a.markdown(a.model(md)))
        self.assertFalse(any(f.get('origin') == 'custom_rule' for f in self.r['findings']))  # 0.5.5: findings는 공식만

    def test_dictionary_covers_known_checks(self):
        known = {cid for case in Replay050Tests.CASES.values() for cid, _, _ in case[1]}
        known |= {'minio.process.rlimit', 'sys.drive.status', 'sys.fs.supported'}  # 201 수집본에서 실패로 관측된 검사
        self.assertFalse(known - set(a.RESOLUTION_KO))
        self.assertEqual(len(a.RESOLUTION_KO), 58)  # mc RELEASE.2026-07-24T01-15-12Z 내장 공식 안내 전체
        for cid, entry in a.RESOLUTION_KO.items():
            text = ' '.join(b if isinstance(b, str) else b[1] for b in entry)
            self.assertNotIn('update-grub', text.replace('update-grub 은', ''), cid)  # el 계열 명령만 안내
            self.assertTrue(re.search(r'[가-힣]', text), cid)

    def test_json_markdown_schema(self):
        self.assertEqual(self.r['schema_version'], '1.5-r2')
        self.assertEqual(a.SCHEMA_VERSION, '1.2-r1')
        self.assertEqual(self.r['policy']['resolution_ko_version'], a.RESOLUTION_KO_VERSION)
        f = self.official('sys.mem.iommu')
        self.assertTrue(any(b['type'] == 'code' and 'grubby' in b['text'] for b in f['resolution_ko']['blocks']))
        self.assertEqual(self.md.count('### 해결방안(한글)'), 3)
        self.assertIn('grubby --update-kernel=ALL', self.md)
        json.dumps(self.r, ensure_ascii=False)
        r1 = a.model(replay_md('32 passed, 2 errors, 1 warning', Replay050Tests.CASES['1116'][1]))
        self.assertEqual(r1['schema_version'], '1.2-r1')
        self.assertEqual(sum(f['resolution_ko']['status'] == 'registered' for f in r1['findings']), 3)


def esc_title(title):
    import html as _html
    return _html.escape(title, quote=True)


class Review053Tests(unittest.TestCase):
    """0.5.3: 검사 결과 목록의 공식 숫자는 mc 요약과 같고, 자체 점검은 따로 센다."""

    def setUp(self):
        line, checks, _ = Replay050Tests.CASES['1116']
        self.r = h.report(body_201(), 1, replay_md(line, checks))
        self.page = h.html_report(self.r)
        self.cards = card_texts(self.page)

    def buttons(self, page=None):
        bar = re.search(r'<div class="sevbtns".*?</div>', page or self.page).group(0)
        return {k: int(n) for k, n in re.findall(r'data-sev="([^"]*)"[^>]*>(?:<span[^>]*>[^<]*</span>)?[^<]*?(\d+)', bar)}

    def test_official_buttons_match_mc_summary(self):
        b = self.buttons()
        s = self.r['official_summary']
        self.assertEqual((b['error'], b['warning']), (s['errors'], s['warnings']))  # 2 · 1
        self.assertFalse([k for k in b if k.startswith('own-')])  # 0.5.5: 자체 버튼 없음
        self.assertIn('공식 판정 카드: 오류 2 · 경고 1 (mc 보고서 요약: 통과 32 · 오류 2 · 경고 1)', self.page)
        self.assertNotIn('자체 점검(이 도구', self.page)
        self.assertEqual(len(re.findall(r'data-sev="warning"', self.page)), 2)  # 버튼 1 + 카드 1

    def test_thp_iommu_merged_into_official_cards(self):
        thp = [c for c in self.cards if 'Transparent Huge Pages' in c or 'THP' in re.search(r'<h3>(.*?)</h3>', c).group(1)]
        self.assertEqual(len(thp), 1)
        self.assertIn('data-sev="warning"', thp[0])
        self.assertIn('서버별 관측값(원본 gzip)', thp[0])
        self.assertIn('<tr><th scope="row">server-0001</th><td>always</td><td>madvise</td><td>511</td></tr>', thp[0])
        self.assertNotIn('자체', thp[0])  # 배지·자체 결과·조건 문구 없음
        iommu = [c for c in self.cards if '명시적 IOMMU 부팅 옵션' in c]
        self.assertEqual(len(iommu), 1)
        self.assertIn('data-sev="error"', iommu[0])
        self.assertIn('<td>명시 옵션 없음</td>', iommu[0])
        self.assertNotIn('자체', iommu[0])
        self.assertNotIn('미평가', iommu[0])

    def test_collection_errors_info_not_warning(self):
        # 0.5.5: 수집 오류는 카드가 아니라 부록 '수집되지 않은 항목'(등급 없음)에만 둔다.
        self.assertFalse(any('설정 변경 대상이 아니라' in c for c in self.cards))
        self.assertFalse(any(f.get('rule_id') == 'r2.collection.errors' for f in self.r['findings']))
        c = next(c for c in self.r['check_results'] if c.get('rule_id') == 'r2.collection.errors')
        self.assertEqual(c['rule_result'], 'info')  # 내부 JSON 값은 유지
        self.assertIn('id="uncollected"', self.page)
        self.assertIn('부록: 수집되지 않은 항목', self.page)
        sect = re.search(r'<section id="uncollected">.*?</section>', self.page, flags=re.S).group(0)
        for word in ('정보 등급', '경고', '✖'):
            self.assertNotIn(word, sect)

    def test_dashboard_priority_and_appendix_consistent(self):
        tiles = dict(re.findall(r'<div class="tile-k">(P\d|미분류)</div><div class="tile-v">(\d+)</div>', self.page))
        self.assertEqual(tiles, {'P1': '0', 'P2': '1', 'P3': '1', 'P4': '0', '미분류': '1'})
        self.assertEqual(sum(map(int, tiles.values())), 3)  # 공식 오류 2 + 경고 1
        md = h.markdown_report(self.r)
        self.assertNotIn('r2.collection.errors', md)  # 자체 규칙표 삭제
        self.assertNotIn('자체 점검', self.page)
        self.assertNotIn('정보 1', a.result_summary_line(self.r))

    def test_own_card_kept_when_no_official_card(self):
        # 0.5.5: 공식 THP 카드가 없어도 자체 카드는 만들지 않는다.
        r = h.report(body_201(), 1, PREVIEW_MD)
        page = h.html_report(r)
        self.assertFalse(any(f.get('origin') == 'custom_rule' for f in r['findings']))
        self.assertEqual(page.count('<article '), len(a.official_cards(r['findings'])))
        self.assertNotIn('자체 점검', page)

    def test_10_15_shape_counts(self):
        line, checks, _ = Replay050Tests.CASES['1015']
        r = h.report(body_201(), 1, replay_md(line, checks))
        b = self.buttons(h.html_report(r))
        s = r['official_summary']
        self.assertEqual((b['error'], b['warning']), (s['errors'], s['warnings']))


class Review052Tests(unittest.TestCase):
    """0.5.2 교차검증 확정 항목: 표시만 바뀌고 판정·숫자·카드 수는 0.5.1과 같아야 한다."""

    def setUp(self):
        line, checks, _ = Replay050Tests.CASES['1116']
        self.r = h.report(body_201(), 1, replay_md(line, checks))
        self.page, self.md = h.html_report(self.r), h.markdown_report(self.r)
        self.find = {f['id']: f for f in self.r['findings']}
        self.rules = {c['rule_id']: c for c in self.r['check_results'] if c.get('rule_id')}

    # -- 판정 고정 -------------------------------------------------------------
    def test_judgment_counts_unchanged(self):
        r = self.r
        self.assertEqual(r['official_summary'], {'passed': 32, 'errors': 2, 'warnings': 1})
        # 0.5.3: 수집 오류 기록은 경고가 아니라 정보. 자체 경고는 THP 1건만.
        self.assertEqual(r['analysis_summary']['custom_rule_counts'], {'pass': 4, 'warning': 1, 'info': 1, 'not_evaluated': 1})
        results = {c['rule_id']: c['rule_result'] for c in r['check_results'] if c['origin'] == 'custom_rule'}
        self.assertEqual(results, {'r2.drive.state': 'pass', 'r2.capacity.bounds': 'pass', 'r2.version.consistency': 'pass',
                                   'r2.tls.validity': 'pass', 'r2.thp.mode': 'warning', 'r2.collection.errors': 'info',
                                   'r2.iommu.consistency': None})
        cards = card_texts(self.page)
        self.assertEqual(len(cards), 3)  # 0.5.5: 공식 카드 3장만
        prio = Counter(re.search(r'data-priority="([^"]*)"', c).group(1) for c in cards)
        self.assertEqual(prio, {'P2': 1, 'P3': 1, 'unclassified': 1})
        for key, n in (('P2', 1), ('P3', 1), ('미분류', 1)):
            self.assertIn('<div class="tile-k">%s</div><div class="tile-v">%d</div>' % (key, n), self.page)

    def test_iommu_reason_split_keeps_result(self):
        iommu = self.rules['r2.iommu.consistency']  # 0.5.5: 내부 JSON(check_results)에만 남음
        self.assertIsNone(iommu['rule_result'])
        self.assertEqual(iommu['assessment'], 'insufficient_data')
        self.assertEqual(iommu['reason'], 'explicit_flags_absent_in_cmdline')
        self.assertNotIn('r2.iommu.consistency', self.find)
        self.assertIn('<td>명시 옵션 없음</td>', self.page)
        self.assertIn('옵션이 없으면 미평가로 두며', self.r['analysis_summary']['custom_rule_catalog']['r2.iommu.consistency']['condition'])

    def test_iommu_reason_variants(self):
        def run(cmdlines):
            body = body_201()
            for config, cmd in zip(body['sys']['config'], cmdlines):
                if cmd is None:
                    config['config'].pop('proc-cmdline')
                else:
                    config['config']['proc-cmdline'] = cmd
            r = h.report(body, 1, PREVIEW_MD)
            self.assertFalse(any(f['id'] == 'r2.iommu.consistency' for f in r['findings']))
            c = next(c for c in r['check_results'] if c.get('rule_id') == 'r2.iommu.consistency')
            return c['rule_result'], c['reason']
        self.assertEqual(run([['ro'], None, ['ro']]), (None, 'proc_cmdline_missing'))
        self.assertEqual(run([['intel_iommu=on'], ['ro'], ['ro']]), (None, 'explicit_flags_unobserved_or_ambiguous'))
        self.assertEqual(run([['iommu=pt']] * 3)[0], 'pass')
        self.assertEqual(run([['iommu=pt'], ['iommu=pt'], ['iommu=on']]), ('warning', 'explicit_flags_only'))
        for code in ('proc_cmdline_missing', 'explicit_flags_absent_in_cmdline', 'explicit_flags_unobserved_or_ambiguous'):
            self.assertNotEqual(a.label(code), code + ' (%s)' % code)

    # -- 문구(A1·A2·D4·B3·B4) -----------------------------------------------------
    def test_official_explanations(self):
        thp, cluster, iommu = (self.find_official(cid)['recommendation'] for cid in ('sys.mem.thp', 'minio.cluster.min', 'sys.mem.iommu'))
        self.assertIn('어느 항목이 표시될지는 수집마다 달라질 수 있습니다', thp)
        self.assertIn('echo 명령은 재부팅 후 유지되지 않으므로', thp)
        self.assertIn('server pool당 노드 수는 운영 권고', cluster)
        self.assertIn('grubby 또는 grub2-mkconfig', iommu)
        self.assertIn('재부팅 후 /proc/cmdline 내용으로', iommu)  # 경로 뒤 조사가 붙으면 마스킹됨

    def find_official(self, cid):
        return next(f for f in self.r['findings'] if f.get('source_check_id') == cid or f['id'] == cid or cid in str(f.get('check_id')))

    # -- 인증서(A3·D6) ------------------------------------------------------------
    def test_certificate_display(self):
        for text in (self.page, self.md):
            self.assertIn('인증서 기록(원본 gzip)', text)
            self.assertIn('certificate-0001', text)
            self.assertIn('인증서 만료 시각(공식 Markdown 기준)', text)
            self.assertNotIn('r2.tls.validity', text)  # 0.5.5: 자체 규칙 언급 없음
            self.assertNotIn('별개 판정', text)
            self.assertNotIn('canary-subject', text)
        body = body_201()
        body['minio']['info']['tls'] = {'tls_enabled': False}
        r = h.report(body, 1, PREVIEW_MD)
        page = h.html_report(r)
        self.assertIn('인증서 기록 없음(TLS 꺼짐 또는 미제공).', page)
        self.assertNotIn('미평가로 남습니다', page)

    def test_certificate_reason_row_korean_or_absent(self):
        rows = dict(row for block in a.info_blocks(self.r) if block[0] == 'table' for row in block[2] if len(row) == 2)
        reason = rows.get('인증서 시각 미확정 사유')
        if reason is not None:
            self.assertNotRegex(reason, r'^[a-z_]+$')
            self.assertNotEqual(reason, a.MISSING)

    # -- B1·B2·D7 -----------------------------------------------------------------
    def test_priority_basis_and_conclusion(self):
        self.assertIn('산정 기준: 공식 검사 카드 1장을 1건으로 셉니다. 검사별로 이 도구에 등록된 고정값', self.page)
        self.assertIn('원본 gzip 관측값은 우선순위에 넣지 않습니다', self.page)
        conclusion = re.search(r'<p class="conclusion">(.*?)</p>', self.page).group(1)
        self.assertIn('확인이 필요합니다(오류 먼저).', conclusion)
        self.assertIn('현재 서비스 상태를 확정하지 않습니다', conclusion)

    def test_collection_errors_not_change_approval(self):
        # 0.5.5: 자체 규칙은 카드가 없어 승인 표시도 없다. 공식 카드의 승인 표시는 그대로.
        for rid in ('r2.collection.errors', 'r2.thp.mode', 'r2.iommu.consistency'):
            self.assertNotIn(rid, self.find)
            self.assertIn(rid, self.rules)
        self.assertFalse(any('설정 변경 대상이 아니라' in c for c in card_texts(self.page)))

    # -- B5·B6·D3 -----------------------------------------------------------------
    def test_alias_note_gib_and_iommu_recommendation(self):
        self.assertIn('두 번호가 반드시 같은 서버를 가리키는 순서라고 보장하지 않습니다', self.page)
        for text in (self.page, self.md):
            self.assertIn('10737418240 (바이트로 보면 10.0 GiB)', text)
            self.assertIn('괄호의 GiB는 값을 바이트로 볼 때의 환산', text)
        self.assertEqual(self.r['facts']['health']['drives'][0]['totalspace'], 10737418240)
        self.assertIn('공식 sys.mem.iommu 기준: iommu=pt 또는 intel_iommu·amd_iommu=off', self.page)

    # -- C1~C4 --------------------------------------------------------------------
    def test_standard_disclaimer_once(self):
        body = html_body(self.page)
        for sentence in a.STANDARD_IMPACT:
            self.assertNotIn(sentence, body)
        self.assertEqual(body.count('카드마다 반복하지 않음'), 1)
        self.assertIn('실제 서비스 영향은 이 보고서만으로 확정하지 않습니다.', self.md)  # Markdown은 그대로
        self.assertEqual(self.md.count('같은 수집 실행 증명 |'), 1)

    def test_cards_hide_internal_codes(self):
        for card in card_texts(self.page):
            visible = re.sub(r'<[^>]+>', ' ', card)
            self.assertNotRegex(visible, r' \([a-z]+_[a-z0-9_]+\)')
        self.assertIn('title="assessable / official_import_only"', self.page)
        dash = re.search(r'<section id="dashboard".*?</section>', self.page, flags=re.S).group(0)
        self.assertNotIn('(consistent_observations)', dash)
        self.assertIn('(consistent_observations)', self.md)  # 부록·Markdown은 코드 유지

    def test_custom_cards_show_observed_values(self):
        # 0.5.5: THP·IOMMU 공식 카드에 서버별 관측값 표만(판정 없음).
        cards = card_texts(self.page)
        obs = [c for c in cards if '서버별 관측값(원본 gzip)' in c]
        self.assertEqual(len(obs), 2)
        joined = ''.join(obs)
        self.assertIn('<tr><th scope="row">server-0001</th><td>always</td><td>madvise</td><td>511</td></tr>', joined)
        self.assertIn('<tr><th scope="row">server-0003</th><td>명시 옵션 없음</td></tr>', joined)
        self.assertNotIn('자체', joined)
        self.assertNotIn('canary', joined)
        self.assertIn('### 서버별 관측값(원본 gzip, 판정 아님)', self.md)
        self.assertIn('| server-0001 | always | madvise | 511 |', self.md)
        dumped = json.dumps(self.r, ensure_ascii=False)
        self.assertNotIn('observed_values', dumped)  # JSON에는 넣지 않음
        self.assertNotIn('server_observations', dumped)

    def test_header_meta_and_appendix_versions(self):
        self.assertIn('도구 %s · 분석 상태: 부분 분석' % h.VERSION, self.page)
        self.assertNotIn('도구·스키마·정책', self.page)
        self.assertIn('스키마·해설 정책 버전', self.page)
        self.assertIn(h.SCHEMA_VERSION, self.page)

    # -- D1·D2 --------------------------------------------------------------------
    def test_rule_table_result_and_filter_text(self):
        self.assertNotIn('자체 점검 규칙', self.md)  # 0.5.5: 부록 자체 규칙표 삭제
        self.assertNotIn('r2.tls.validity', self.md)
        self.assertIn('목록 전체 3', self.page)
        self.assertIn('공식 통과 0 (공식 통과 상세는 원본에 없음)</button>', self.page)


class Review055Tests(unittest.TestCase):
    """0.5.5: 화면·Markdown의 판정·숫자는 mc 공식 판정만. 자체 점검은 내부 JSON에만 남는다."""

    def setUp(self):
        line, checks, _ = Replay050Tests.CASES['1116']
        self.r = h.report(body_201(), 1, replay_md(line, checks))
        self.page, self.md = h.html_report(self.r), h.markdown_report(self.r)

    def test_no_own_verdicts_badges_or_cards(self):
        visible = re.sub(r'<script.*?</script>|<style.*?</style>', '', self.page, flags=re.S)
        for text in (visible, self.md):
            for word in ('자체 점검', '자체 결과', '자체 판정', 'own-', '보조 정보'):
                self.assertNotIn(word, text)
        self.assertNotRegex(self.page, r'data-sev="own-|\bown-(info|warning|error|verdict|aux)')
        for rid in ('r2.drive.state', 'r2.capacity.bounds', 'r2.version.consistency', 'r2.tls.validity', 'r2.collection.errors'):
            self.assertNotIn(rid, visible)
        self.assertEqual(self.page.count('<article '), 3)
        self.assertIn('id="uncollected"', self.page)
        self.assertIn('<li class="drv ', self.page)  # 드라이브 격자 유지

    def test_json_keeps_custom_internal_only(self):
        self.assertEqual(self.r['schema_version'], '1.5-r2')
        self.assertTrue(all(f.get('origin') != 'custom_rule' for f in self.r['findings']))
        s = self.r['analysis_summary']
        self.assertEqual(s['custom_rule_display'], 'internal_only_not_displayed')
        self.assertEqual(s['custom_rule_counts'], {'pass': 4, 'warning': 1, 'info': 1, 'not_evaluated': 1})
        self.assertEqual(s['priority_counts'], {'P2': 1, 'P3': 1, 'unclassified': 1})
        self.assertEqual(len([c for c in self.r['check_results'] if c.get('origin') == 'custom_rule']), 7)

    def test_fail_on_priority_counts_official_only(self):
        import contextlib, io, tempfile
        parent = a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')
        work = Path(tempfile.mkdtemp(prefix='r2-055-', dir=parent))
        (work / 'h.json.gz').write_bytes(packed(SYNTHETIC))
        (work / 'r.md').write_text(PAIRED_MD, encoding='utf-8')
        def run(*argv):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return h.main(list(argv))
        r = h.report(copy.deepcopy(SYNTHETIC), 1, PAIRED_MD)
        self.assertEqual(r['run_status'], 'success')
        self.assertIn(('r2.thp.mode', 'warning'), [(c.get('rule_id'), c['rule_result']) for c in r['check_results']])
        self.assertEqual([f['priority'] for f in r['findings']], ['unclassified'])  # 공식 카드 1장(미분류)
        # 자체 THP 경고(예전 P2)는 세지 않는다 → P2 기준이어도 0
        base = ('--health', str(work / 'h.json.gz'), '--official-report', str(work / 'r.md'))
        self.assertEqual(run(*base, '--out-dir', str(work / 'o1'), '--fail-on-priority', 'P2'), 0)
        # gzip 단독: 공식 카드 0장
        self.assertEqual(run('--health', str(work / 'h.json.gz'), '--out-dir', str(work / 'o2'), '--fail-on-priority', 'P2'), 0)
        # 공식 P2 카드는 그대로 1 (R1 경로, 공식 THP 경고 = P2)
        line, checks, _ = Replay050Tests.CASES['1116']
        (work / 'r2.md').write_text(replay_md(line, checks), encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = a.main(['--official-report', str(work / 'r2.md'), '--out-dir', str(work / 'o3'), '--fail-on-priority', 'P2'])
        self.assertEqual(rc, 1)

    def test_thp_iommu_observation_tables_without_verdict(self):
        cards = card_texts(self.page)
        thp = next(c for c in cards if '<th scope="col">THP enabled</th>' in c)
        self.assertIn('<th scope="col">max_ptes_none</th>', thp)
        self.assertIn('판정 아님', thp)
        for word in ('경고 /', '미평가', '조건', '권장과 다름'):
            self.assertNotIn(word, re.search(r'<div class="box obs-box">.*?</table>', thp, flags=re.S).group(0))


if __name__ == '__main__':
    unittest.main(verbosity=2)
