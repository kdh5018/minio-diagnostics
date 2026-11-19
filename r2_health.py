"""R2 limited offline adapter. No server access, collectors or raw-data export."""
import argparse
from collections import Counter
import copy
import gzip
import io
import json
from pathlib import Path
import re
import sys
import time
import zlib

import aistordoctor as a
import r2_enrichment as enrichment

# 0.5.10: 패키지 버전을 따른다. R2 보고서도 aistordoctor의 마스킹·유출 검사를 쓰므로 버전이 같아야 추적할 수 있다.
VERSION = a.VERSION.rsplit('-', 1)[0] + '-r2'
SCHEMA_VERSION = '1.5-r2'
# 0.5.11: 클러스터 크기에 비례하는 한도(JSON 요소 수·서버 수·드라이브 수·처리 시간)는 기본에서 없앤다(None = 한도 없음).
# 서버 1대가 압축 해제 약 33 KB·요소 약 3,300개라 이전 MAX_NODES(200,000)는 서버 약 60대에서 막혔다.
# 남기는 것: 압축 폭탄 방어(압축 해제 1 GiB, 서버 약 3만 대 분량), 중첩 깊이, 값 하나의 길이, 인증서 수.
MAX_COMPRESSED = 1024 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = None
MAX_STRING = 65536
MAX_SERVERS = None
MAX_DRIVES = None
SECONDS = None


def invalid(*args):
    raise a.InputError('invalid JSON value')


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise a.InputError('duplicate JSON key')
        result[key] = value
    return result


def decode(data):
    started = time.monotonic()
    if len(data) > MAX_COMPRESSED or data[:2] != b'\x1f\x8b':
        raise a.InputError('gzip signature or compressed limit')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            expanded = stream.read(MAX_EXPANDED + 1)
        if len(expanded) > MAX_EXPANDED:
            raise a.InputError('expanded size limit')
        text = expanded.decode('utf-8-sig')
        depth = 0
        structural_units = string_span = atom_span = 0
        quoted = escaped = False
        for index, char in enumerate(text):
            if index % 65536 == 0 and a.over_limit(time.monotonic() - started, SECONDS):
                raise a.InputError('processing time limit')
            if quoted:
                string_span += 1
                # One Unicode character may use a pair of six-character surrogate escapes.
                if string_span > 12 * MAX_STRING + 2:
                    raise a.InputError('serialized string limit before decoding')
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
                string_span = atom_span = 0
            elif char in '[{':
                depth += 1
                structural_units += 1
                atom_span = 0
                if depth > MAX_DEPTH:
                    raise a.InputError('nesting limit')
            elif char in ']}':
                depth -= 1
                atom_span = 0
            elif char in ',:':
                structural_units += 1
                atom_span = 0
            elif char.isspace():
                atom_span = 0
            else:
                atom_span += 1
                if atom_span > 6 * MAX_STRING + 2:
                    raise a.InputError('scalar token limit before decoding')
            # Conservative lexical allocation guard; exact node count follows decode.
            if MAX_NODES is not None and structural_units > 2 * MAX_NODES:
                raise a.InputError('structural token limit before decoding')
        decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=invalid)
        documents, pos = [], 0
        while pos < len(text):
            while pos < len(text) and text[pos].isspace():
                pos += 1
            if pos == len(text):
                break
            value, pos = decoder.raw_decode(text, pos)
            documents.append(value)
            if len(documents) > 2:
                raise a.InputError('ambiguous document count')
        if len(documents) not in (1, 2) or not all(isinstance(x, dict) for x in documents):
            raise a.InputError('unsupported document structure')
        stack, count = list(documents), 0
        while stack:
            value = stack.pop()
            count += 1
            if a.over_limit(count, MAX_NODES) or a.over_limit(time.monotonic() - started, SECONDS):
                raise a.InputError('resource limit')
            if isinstance(value, dict):
                stack.extend(value.keys())
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str) and len(value) > MAX_STRING:
                raise a.InputError('string limit')
        body = documents[-1]
        if len(documents) == 2 and any(k in documents[0] for k in ('minio', 'sys')):
            raise a.InputError('unsupported header structure')
        return body, len(documents)
    except (OSError, EOFError, UnicodeError, ValueError, RecursionError, zlib.error) as exc:
        raise a.InputError('invalid or limited diagnostic input') from exc


def read(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_COMPRESSED + 1)
    return decode(data)


def number(value):
    return value if type(value) is int and 0 <= value <= 2 ** 63 - 1 else None


def stamp(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    return a.aware_time(value)


def release(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    candidate = value.removeprefix('RELEASE.')
    if re.fullmatch(r'v?[0-9]+(?:\.[0-9]+){1,3}', candidate) or stamp(candidate):
        return value
    return None


def normalize(body):
    info = body.get('minio', {}).get('info') if isinstance(body.get('minio'), dict) else None
    if not isinstance(info, dict) or not isinstance(info.get('servers'), list):
        raise a.InputError('minimum server-array structure unavailable')
    if a.over_limit(len(info['servers']), MAX_SERVERS):
        raise a.InputError('server limit')
    servers, drives, notes = [], [], []
    for index, server in enumerate(info['servers']):
        if not isinstance(server, dict):
            notes.append('invalid server record')
            continue
        sid = 'server-%04d' % (index + 1)
        locator = {'input': 'health-001', 'json_path': '$.minio.info.servers[%d]' % index}
        servers.append({'id': sid, 'version': release(server.get('version')), 'source_locator': locator})
        records = server.get('drives')
        if not isinstance(records, list):
            notes.append('drive array missing')
            continue
        for position, drive in enumerate(records):
            if MAX_DRIVES is not None and len(drives) >= MAX_DRIVES:
                raise a.InputError('drive limit')
            if not isinstance(drive, dict):
                notes.append('invalid drive record')
                continue
            state = drive.get('state')
            state = state if state in ('ok', 'offline') else None
            fields = {name: number(drive.get(name)) for name in
                      ('totalspace', 'usedspace', 'availspace', 'pool_index', 'set_index', 'disk_index')}
            drives.append(dict(fields, id='%s-drive-%04d' % (sid, position + 1), server_id=sid, state=state,
                               source_locator={'input': 'health-001', 'json_path': locator['json_path'] + '.drives[%d]' % position}))
    tls = info.get('tls') if isinstance(info.get('tls'), dict) else {}
    certs = []
    raw_certs = tls.get('certs')
    if isinstance(raw_certs, list):
        for index, cert in enumerate(raw_certs[:1000]):
            if not isinstance(cert, dict):
                notes.append('invalid certificate record')
                continue
            before, after = stamp(cert.get('not_before')), stamp(cert.get('not_after'))
            certs.append({'id': 'certificate-%04d' % (index + 1),
                          'not_before': before.isoformat() if before else None,
                          'not_after': after.isoformat() if after else None,
                          'source_locator': {'input': 'health-001', 'json_path': '$.minio.info.tls.certs[%d]' % index}})
        if len(raw_certs) > 1000:
            raise a.InputError('certificate limit')
    collected = stamp(body.get('timestamp'))
    normalized = {'servers': servers, 'drives': drives, 'certificates': certs,
                  'tls_enabled': tls.get('tls_enabled') if type(tls.get('tls_enabled')) is bool else None,
                  'collected_at': collected.isoformat() if collected else None,
                  'record_identity_scope': 'input-local aliases; not stable cross-snapshot identities',
                  'capacity_unit': 'raw source counters; byte unit not independently verified',
                  'excluded': ['environment_variables', 'unselected_configuration_values', 'license_data', 'raw_errors', 'endpoints', 'deployment_identifiers']}
    configurations, collection, topology = enrichment.extract(body, normalized, number)
    normalized.update(system_configurations=configurations, collection=collection, topology=topology)
    return normalized, sorted(set(notes))


CATALOG = {
    'r2.drive.state': ('드라이브 관측 상태', '가용성', ['drives[].state'], 'ok/offline만 평가; 다른 상태는 판정 불가'),
    'r2.capacity.bounds': ('용량 카운터 범위', '용량', ['totalspace', 'usedspace', 'availspace'], '비음수 정수, used/available 각각 total 이하; 임계 점유율·추세는 미평가'),
    'r2.version.consistency': ('서버 버전 일치', '설정', ['servers[].version'], '관측된 서버 버전 문자열의 일치; 특정 버전을 요구하지 않음'),
    'r2.tls.validity': ('인증서 수집 시점 유효기간', '보안', ['timestamp', 'not_before', 'not_after'], '원본 수집 시점에서 유효기간만 평가; 만료 임박 정책·신뢰체인·SAN은 미평가(공식 mc 보고서의 TLS 검사와는 별개 판정)')}


def evaluate(n, as_of=None):
    basis = stamp(as_of) if as_of else stamp(n['collected_at'])
    if as_of and basis is None:
        raise a.InputError('timezone-aware as-of required')
    drives, servers, certs = n['drives'], n['servers'], n['certificates']
    results = []
    for rid, (title, category, required, condition) in CATALOG.items():
        result, reason = None, 'required_input_missing_or_unrecognized'
        try:
            if rid == 'r2.drive.state' and drives:
                states = [d['state'] for d in drives]
                if 'offline' in states:
                    result, reason = 'fail', 'explicit_offline_record'
                elif all(s == 'ok' for s in states):
                    result, reason = 'pass', 'all_provided_states_ok'
            elif rid == 'r2.capacity.bounds' and drives:
                values = [(d['totalspace'], d['usedspace'], d['availspace']) for d in drives]
                if all(all(x is not None for x in row) for row in values):
                    result = 'pass' if all(u <= t and f <= t for t, u, f in values) else 'fail'
                    reason = 'provided_counter_bounds_only'
            elif rid == 'r2.version.consistency' and servers:
                versions = [s['version'] for s in servers]
                if all(versions):
                    result = 'pass' if len(set(versions)) == 1 else 'warning'
                    reason = 'provided_version_string_comparison'
            elif rid == 'r2.tls.validity' and certs and basis and n['tls_enabled'] is True:
                bounds = [(stamp(c['not_before']), stamp(c['not_after'])) for c in certs]
                if all(b and e for b, e in bounds):
                    result = 'pass' if all(b <= basis < e for b, e in bounds) else 'fail'
                    reason = 'collection_or_explicit_as_of_basis'
        except (ValueError, TypeError, KeyError):
            reason = 'isolated_rule_error'
        results.append(enrichment.rule(rid, 'r2-initial-1', result, reason, title, category, required, condition,
                                       '$.minio.info', basis.isoformat() if basis else None))
    return results


class OfficialInputError(a.InputError):
    """공식 Markdown(--official-report) 해석 실패. 원래 예외는 __cause__에 둔다(원인 안내는 그쪽을 따른다)."""


MARKDOWN_ERRORS = (a.InputError, UnicodeError, ValueError, TypeError, KeyError)


class LinkConflict(a.InputError):
    """Combined-input refusal. Carries counts and timestamps only (no endpoints or raw text)."""
    def __init__(self, detail):
        super().__init__('health/Markdown evidence conflict; no combined report')
        self.detail = detail


def conflict_lines(detail):
    lines = ['공식 Markdown과 원본 gzip이 같은 수집으로 확인되지 않아 결합 보고서를 만들지 않았습니다(안전장치, 완화 불가).']
    if detail.get('time_conflict'):
        lines.append('수집 시각 차이: %s초 (허용 %s초 이내만 결합)' % (int(abs(detail['delta'])), enrichment.TIME_TOLERANCE_SECONDS)
                     if detail.get('delta') is not None else '수집 시각 차이가 허용 범위를 넘었습니다.')
        lines.append('  · 공식 Markdown 시각: %s' % (a.with_kst(detail.get('markdown_collected_at')) or a.MISSING))
        lines.append('  · 원본 gzip 시각: %s' % (a.with_kst(detail.get('health_collected_at')) or a.MISSING))
        lines.append('  · 로컬에서 원본 gzip으로 다시 만든 Markdown은 Date가 생성 시각이라 수집 시각과 다를 수 있습니다.')
    if detail.get('target_conflict'):
        lines.append('서버 대상 불일치: Markdown %s개 / gzip %s개 / 공통 %s개 (주소는 출력하지 않음)' % (
            detail.get('markdown_target_count'), detail.get('health_target_count'), detail.get('shared_target_count')))
    lines += ['조치: 같은 mc 수집 실행에서 함께 받은 diag-report-*.md와 diag-data-*.json.gz를 지정하거나,',
              '      각 파일을 따로 분석하십시오(--official-report만 또는 --health만).']
    return lines


# 0.5.5: 자체 점검 결과(check_results·custom_rule_*)는 내부용이며 보고서에 표시하지 않는다는 JSON 표지.
CUSTOM_RULE_DISPLAY = 'internal_only_not_displayed'


def report(body, documents=1, official_text=None, as_of=None, mask='strict'):
    n, notes = normalize(body)
    checks = evaluate(n, as_of) + enrichment.system_rules(n)
    parsed = None
    if official_text is not None:
        # 0.4.4: 공식 Markdown 해석은 결합 판단 전에 따로 끝내고, 실패하면 Markdown 입력 오류로 표시한다.
        try:
            parsed = a.parse_report(official_text)
        except MARKDOWN_ERRORS as exc:
            raise OfficialInputError('official markdown') from exc
    pairing = enrichment.link(body, official_text, stamp, parsed)
    if pairing['status'] == 'conflict':
        raise LinkConflict(dict(pairing.pop('conflict_detail'), delta=pairing['time_delta_seconds']))
    if official_text is not None:
        try:
            r = a.model(official_text, mask, as_of, parsed)  # 0.4.3: 공식 부분도 같은 기준 시각(R1과 동일)
        except a.OutputError:
            raise
        except MARKDOWN_ERRORS as exc:
            raise OfficialInputError('official markdown') from exc
    else:
        r = {'schema_version': a.SCHEMA_VERSION, 'official_summary': None, 'findings': [], 'check_results': [],
             'parser_errors': [], 'policy': {'version': 'r2-initial-1', 'profile': 'unspecified'},
             'input_metadata': {}, 'source_versions': {}, 'facts': {}, 'coverage': {}, 'completeness': {},
             'masking': {'level': mask, 'policy_version': 'r2-allowlist-1', 'limitations': 'R2 원본은 허용 필드만 추출하며 공유 전 수동 검토가 필요합니다.'}}
    r['policy'] = {'version': 'r2-initial-1', 'profile': 'unspecified', 'official_explanations': 'r1-1',
                   'resolution_ko_version': a.RESOLUTION_KO_VERSION}  # 0.5.4
    r['tool_version'] = VERSION
    r['schema_version'] = SCHEMA_VERSION
    r['input_metadata']['parser_version'] = 'r2-structural-2'
    r['input_metadata']['health'] = {'id': 'health-001', 'documents': documents, 'collected_at': n['collected_at'],
                                    'format_version': str(body.get('version')) if type(body.get('version')) in (str, int) and re.fullmatch(r'[0-9]{1,8}', str(body['version'])) else None,
                                    'format_policy': 'structure first; version metadata does not gate execution'}
    r['source_versions']['aistor'] = {'value': sorted({s['version'] for s in n['servers'] if s['version']}), 'reason': 'observed_server_metadata; no required product version'}
    r['source_versions']['mc'] = {'value': None, 'reason': 'not_identified; not_required_for_file_analysis'}
    r['facts']['health'] = n
    r['coverage'].update(health='allowlisted_minio_info_subset', topology='indices_only; no quorum or failure-domain inference',
                         configuration='allowlisted_thp_iommu_rlimit', collection_errors='recognized_sys_arrays_only', comparison='not_supported', operational_analysis='not_supported')
    r['input_link'] = pairing
    if pairing['status'] == 'insufficient_evidence':
        notes.append('input correspondence evidence insufficient')
    if n['collection']['observed_error_rows']:
        notes.append('reported collector errors; source coverage incomplete')
    if n['collection']['count_basis'] != 'complete':
        notes.append('collection error count incomplete')
    r['parser_errors'].extend(notes)
    r['check_results'].extend(checks)
    # 0.5.5(스키마 1.5-r2): 자체 점검 결과는 findings(화면 카드·우선순위·--fail-on-priority 기준)에 넣지 않는다.
    # check_results와 custom_rule_*는 내부용으로 남기며 보고서 화면·Markdown에는 표시하지 않는다.
    r['analysis_summary'] = dict(r.get('analysis_summary') or {},
                                 custom_rule_counts=dict(Counter(c['rule_result'] or 'not_evaluated' for c in checks)),
                                 custom_rule_catalog={c['rule_id']: {'version': c['rule_version'], 'required_inputs': c['required_inputs'], 'condition': c['condition']} for c in checks},
                                 custom_rule_display=CUSTOM_RULE_DISPLAY)
    partial = bool(r['parser_errors']) or any(c['rule_result'] is None for c in checks if c['rule_id'] != 'r2.version.consistency')
    r['run_status'] = 'partial' if partial else 'success'
    r['completeness'] = {'source_coverage': 'explicit_allowlist_subset', 'requested_scope_satisfied': not partial and all(c['rule_result'] is not None for c in checks),
                         'limitations': '범위 밖 필드는 미지원입니다. success는 수집 완전성·시스템 정상 판정이 아닙니다.'}
    return r


def view(r):
    """Presentation copy: official cards get per-server raw values (0.5.5). Never written to JSON."""
    v = copy.deepcopy(r)
    v['findings'] = a.official_cards(v['findings'])
    n = v['facts'].get('health')
    if isinstance(n, dict):
        for f in v['findings']:
            obs = server_observations(f.get('source_check_id'), n)
            if obs:
                f['server_observations'] = obs
    return v


def iommu_text(c):
    flags = ', '.join('%s=%s' % (k, v if v is not None else '모호') for k, v in sorted(c['iommu_flags'].items()))
    return flags or ('명시 옵션 없음' if c.get('proc_cmdline_observed', True) else '부팅 옵션 기록 없음')


def server_observations(check_id, n):
    """0.5.5: per-server values from the gzip for the official THP/IOMMU card. Facts only, no verdict or rule text."""
    configs = n.get('system_configurations') or []
    if not configs:
        return None
    who = lambda c: c.get('server_id') or c['id'] + ' (대상 미확인)'
    if check_id == 'sys.mem.thp':
        return {'headers': ['서버', 'THP enabled', 'THP defrag', 'max_ptes_none'],
                'rows': [[who(c)] + [c.get(k) for k, _ in THP_COLUMNS] for c in configs]}
    if check_id == 'sys.mem.iommu':
        return {'headers': ['서버', '명시적 IOMMU 부팅 옵션'], 'rows': [[who(c), iommu_text(c)] for c in configs]}
    return None


SECTION_LABELS = {'cpus': 'CPU 정보', 'netinfo': '네트워크 정보', 'partitions': '파티션', 'osinfo': 'OS 정보',
                  'meminfo': '메모리', 'procinfo': '프로세스', 'errors': '시스템 오류', 'services': '서비스', 'config': '설정'}
INCOMPLETE_LABELS = {'sys_errors_missing_or_not_list': 'sys.errors 배열 없음 또는 형식 다름'}


def error_count_text(collection):
    if collection.get('reported_error_count') is not None:
        return collection['reported_error_count']
    if collection.get('observed_error_rows'):
        return '최소 %s (전체 수 미확정)' % collection['observed_error_rows']
    return None


def gib(value):
    # 0.5.2(B6): 2^30 이상 정수에만 바이트로 볼 때의 GiB를 덧붙인다(원값·JSON은 그대로).
    if type(value) is int and value >= 2 ** 30:
        return '%d (바이트로 보면 %.1f GiB)' % (value, value / 2 ** 30)
    return value


def observation_blocks(r):
    """Presentation only: allowlisted observations as label/value tables, never new diagnoses."""
    n = r.get('facts', {}).get('health')
    if not isinstance(n, dict):
        return []
    blocks = [('h', 2, '원본 관측 정보 (공식 판정과 별도)'),
              ('kv', '원본 수집 시각: ', a.with_kst(n.get('collected_at'))),
              ('p', '서버·드라이브 수는 제공된 기록 수입니다. 물리 서버·장애 도메인·quorum을 뜻하지 않습니다.'),
              ('h', 3, '서버 기록'),
              ('table', ['대상 별칭', '관측 버전', '원본 JSON 위치'],
               [(s['id'], s['version'], s['source_locator']['json_path']) for s in n['servers']]),
              ('h', 3, '드라이브 기록'),
              ('p', '용량은 원본 카운터입니다. 단위는 검증하지 않았으며, 괄호의 GiB는 값을 바이트로 볼 때의 환산입니다. 점유율·성능은 판정하지 않습니다.'),
              ('table', ['드라이브 별칭', '서버 별칭', '관측 상태', 'totalspace', 'usedspace', 'availspace', '원본 JSON 위치'],
               [(d['id'], d['server_id'], d['state'], gib(d['totalspace']), gib(d['usedspace']), gib(d['availspace']), d['source_locator']['json_path'])
                for d in n['drives']])]
    # 0.5.2(A3): 자체 점검 r2.tls.validity가 본 인증서(별칭·유효기간만, 주체·SAN은 내보내지 않음).
    certs = n.get('certificates') or []
    blocks.append(('h', 3, '인증서 기록(원본 gzip)'))
    blocks.append(('table', ['인증서 별칭', '유효 시작', '만료', '원본 JSON 위치'],
                   [(c['id'], a.with_kst(c['not_before']), a.with_kst(c['not_after']), c['source_locator']['json_path']) for c in certs])
                  if certs else ('p', '인증서 기록 없음(TLS 꺼짐 또는 미제공).'))
    configs = n.get('system_configurations') or []
    if configs:
        blocks += [('h', 3, '시스템 설정 관측(THP·IOMMU·rlimit)'),
                   ('p', '공식 sys.mem.thp Resolution의 THP 권장값: enabled=madvise, defrag=defer+madvise 또는 madvise, max_ptes_none=0 (판정은 공식 검사 결과만).'),
                   ('table', ['설정 별칭', '서버 별칭', 'THP enabled', 'THP defrag', 'max_ptes_none', 'rlimit-max', '명시적 IOMMU 옵션'],
                    [(c['id'], c['server_id'], c['thp_enabled'], c['thp_defrag'], c['thp_max_ptes_none'], c['rlimit_max'],
                      ', '.join('%s=%s' % (k, v if v is not None else '모호') for k, v in sorted(c['iommu_flags'].items())) or '명시 없음')
                     for c in configs])]
    collection = n['collection']
    blocks += [('h', 3, '수집되지 않은 항목 (원본 gzip의 오류 기록, 공식 검사와 별도)'),
               ('kv', '지원 필드에서 오류가 기록된 행 수: ', error_count_text(collection)),
               ('p', '오류 본문·내부 주소는 내보내지 않습니다. 기록이 없다는 것은 전체 수집 성공의 증거가 아닙니다.')]
    reasons = collection.get('incomplete_reasons') or []
    if reasons:
        blocks.append(('p', '전체 오류 수를 확정하지 못한 이유: ' + ', '.join(
            INCOMPLETE_LABELS.get(x) or ('형식 이상 행: ' + SECTION_LABELS.get(x.split(':', 1)[-1], x)) for x in reasons)))
    blocks.append(('table', ['수집 영역', '제공 기록 수', '오류 기록 행 수', '입력 지원 상태', '원본 JSON 위치'],
                   [(SECTION_LABELS.get(k, k), v['records'], v['reported_errors'],
                     '제공' if v['status'] == 'provided' else '미제공 / 미지원', '$.sys.' + k)
                    for k, v in collection['sections'].items()]))
    typed = [(SECTION_LABELS.get(k, k), label, count) for k, v in collection['sections'].items() for label, count in (v.get('error_types') or {}).items()]
    if typed:
        blocks += [('p', '오류 유형(고정 분류, 원인 확정 아님):'),
                   ('table', ['수집 영역', '대상 / 유형', '행 수'], typed)]
    link = r.get('input_link') or {}
    blocks.append(('details', '수집 범위·미지원·결합 제한', [
        ('table', ['항목', '값'], [('수집 오류 집계 범위', '인식된 sys 배열의 error/errors 필드만 집계(부재가 전체 수집 성공을 증명하지 않음)'),
                                  ('입력 결합 상태', a.label(link.get('status'))),
                                  ('토폴로지', 'pool·set 인덱스만 표시, quorum·장애 도메인 미추론')])]))
    return blocks


def observations_html(r):
    blocks = observation_blocks(r)
    return '<section id="raw-observations">' + a.blocks_html(blocks) + '</section>' if blocks else ''


# --- 0.5.0 도식 화면(표시 전용) ----------------------------------------------------
# 이미 보고서에 실리는 허용 필드(별칭·상태·카운터·고정 분류)만 그린다. 새 판정을 만들지 않는다.
STATE_UI = {'ok': ('passed', '✔', 'ok'), 'offline': ('error', '✖', 'offline')}
THP_COLUMNS = (('thp_enabled', 'THP enabled'), ('thp_defrag', 'THP defrag'), ('thp_max_ptes_none', 'max_ptes_none'))


def usage(d):
    total, used = d.get('totalspace'), d.get('usedspace')
    if not isinstance(total, int) or not isinstance(used, int) or total <= 0:
        return None
    return used * 100.0 / total


def drive_cell(d):
    tone, icon, word = STATE_UI.get(d.get('state'), ('unknown', '?', '상태 미확인'))
    pct = usage(d)
    if pct is None:
        meter, text = '', '사용률 계산 불가(카운터 없음)'
    elif pct > 100:
        meter, text = '<span class="meter"><span class="fill u-hi" style="width:100%"></span></span>', '카운터 범위 벗어남(used > total)'
    else:
        band = 'hi' if pct >= 90 else 'mid' if pct >= 70 else 'lo'
        meter = '<span class="meter" role="img" aria-label="사용 %.1f%%"><span class="fill u-%s" style="width:%.1f%%"></span></span>' % (pct, band, pct)
        text = '사용 %.1f%%' % pct
    index = ' · '.join('%s %s' % (name, d.get(key)) for name, key in (('pool', 'pool_index'), ('set', 'set_index'), ('disk', 'disk_index'))
                       if d.get(key) is not None) or '인덱스 없음'
    return ('<li class="drv tone-%s"><span class="drv-id">%s</span><span class="drv-st"><span aria-hidden="true">%s</span> %s</span>'
            '<span class="drv-ix">%s</span>%s<span class="drv-u">%s</span></li>') % (
        tone, a.esc('drive-' + d['id'].rsplit('-drive-', 1)[-1]), icon, a.esc(word), a.esc(index), meter, a.esc(text))


def drive_grid_html(n):
    by_server = {}
    for d in n['drives']:
        by_server.setdefault(d['server_id'], []).append(d)
    blocks = []
    for s in n['servers']:
        drives = by_server.get(s['id'], [])
        states = Counter(d.get('state') or 'unknown' for d in drives)
        summary = ' · '.join(x for x in ('✔ ok %d' % states['ok'] if states['ok'] else '', '✖ offline %d' % states['offline'] if states['offline'] else '',
                                         '? 미확인 %d' % states['unknown'] if states['unknown'] else '') if x) or '드라이브 기록 없음'
        tone = 'error' if states['offline'] else 'unknown' if states['unknown'] or not drives else 'passed'
        blocks.append('<div class="srv tone-b-%s"><div class="srv-h"><strong>%s</strong><span class="muted">버전 %s</span><span>드라이브 %d개 · %s</span></div><ul class="drives">%s</ul></div>' % (
            tone, a.esc(s['id']), a.esc(s['version'] or a.MISSING), len(drives), a.esc(summary), ''.join(drive_cell(d) for d in drives)))
    groups = n.get('topology', {}).get('groups') or []
    topo = '<p class="note">pool·set 인덱스별 드라이브 기록: %s (quorum·장애 도메인은 추론하지 않음)</p>' % a.esc(
        ', '.join('pool %s / set %s: %s개' % (g['pool_index'], g['set_index'], g['observed_drive_records']) for g in groups) or '인덱스 없음')
    states = Counter(d.get('state') or 'unknown' for d in n['drives'])
    legend = ('<p class="legend"><span class="badge sev-passed">✔ ok</span> <span class="badge sev-error">✖ offline</span> '
              '<span class="badge sev-unknown">? 상태 미확인</span> · 막대: 사용률 '
              '<span class="key u-lo"></span>70% 미만 <span class="key u-mid"></span>70~90% <span class="key u-hi"></span>90% 이상 '
              '(표시용 구간이며 판정 아님)</p>')
    return ('<section id="topology" aria-labelledby="topology-h"><h2 id="topology-h">서버 × 드라이브</h2>'
            '<p class="note">서버 %d개 · 드라이브 %d개 기록 (ok %d · offline %d · 미확인 %d). 원본 카운터 기준 비율이며 단위·성능·물리 배치를 확정하지 않습니다.</p>'
            '<p class="note">참고: "server-0001" 등은 원본 gzip 수집 순서로 이 도구가 붙인 번호이고, "identifier-001" 등은 공식 mc 보고서 안의 식별자를 마스킹하며 '
            '등장 순서로 붙인 번호입니다. 서버 수·대상이 일치하더라도 두 번호가 반드시 같은 서버를 가리키는 순서라고 보장하지 않습니다.</p>%s'
            '<div class="srvgrid">%s</div>%s</section>') % (
        len(n['servers']), len(n['drives']), states['ok'], states['offline'], states['unknown'], legend, ''.join(blocks), topo)


def majority(values):
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None
    value, count = Counter(present).most_common(1)[0]
    return value if count > len(present) / 2 else None


def config_cell(value, ok=None, differs=False):
    if value is None:
        return '<td class="c-na"><span aria-hidden="true">—</span> 미관측</td>'
    if ok is True:
        return '<td class="c-ok"><span aria-hidden="true">✔</span> %s</td>' % a.esc(value)
    if ok is False:
        return '<td class="c-bad"><span aria-hidden="true">✖</span> <strong>%s</strong> <small>권장과 다름</small></td>' % a.esc(value)
    if differs:
        return '<td class="c-warn"><span aria-hidden="true">▲</span> <strong>%s</strong> <small>다른 서버와 다름</small></td>' % a.esc(value)
    return '<td>%s</td>' % a.esc(value)


def config_table_html(n):
    configs = n.get('system_configurations') or []
    if not configs:
        return ('config', '시스템 설정', '<section id="config"><h2>시스템 설정 비교 (THP·IOMMU·rlimit)</h2><p>설정 기록이 없습니다(미관측).</p></section>')
    iommu = [', '.join('%s=%s' % (k, v if v is not None else '모호') for k, v in sorted(c['iommu_flags'].items())) or None for c in configs]
    rlimits = [c['rlimit_max'] for c in configs]
    common_iommu, common_rlimit = majority(iommu), majority(rlimits)
    head = ''.join('<th scope="col">%s</th>' % a.esc(x) for x in ('서버', 'THP enabled', 'THP defrag', 'max_ptes_none', '명시적 IOMMU 옵션', 'rlimit-max'))
    rec = enrichment.THP_OFFICIAL_RECOMMENDED
    recommended = ('<tr class="rec"><th scope="row">권장값</th>%s<td>서버 간 동일<br><small>공식 sys.mem.iommu 기준: iommu=pt 또는 intel_iommu·amd_iommu=off (판정은 검사 결과 참고)</small></td><td>기준 없음<br><small>표시만</small></td></tr>' %
                   ''.join('<td>%s<br><small>공식 sys.mem.thp Resolution</small></td>' % a.esc(' 또는 '.join(str(v) for v in rec[key])) for key, _ in THP_COLUMNS))
    rows = []
    mismatches = 0
    for c, flags, rlimit in zip(configs, iommu, rlimits):
        cells = []
        for key, _ in THP_COLUMNS:
            value = c.get(key)
            ok = None if value is None else value in rec[key]
            mismatches += ok is False
            cells.append(config_cell(value, ok))
        cells.append(config_cell(flags, differs=common_iommu is not None and flags != common_iommu) if flags else
                     '<td class="c-na"><span aria-hidden="true">—</span> 명시 없음</td>')
        cells.append(config_cell(rlimit, differs=common_rlimit is not None and rlimit != common_rlimit))
        rows.append('<tr><th scope="row">%s</th>%s</tr>' % (a.esc(c['server_id'] or c['id'] + ' (대상 미확인)'), ''.join(cells)))
    return ('config', '시스템 설정',
            '<section id="config" aria-labelledby="config-h"><h2 id="config-h">시스템 설정 비교 (THP·IOMMU·rlimit)</h2>'
            '<p class="note">권장값과 다른 칸 %d개. <span class="c-bad">✖ 권장과 다름</span> · <span class="c-warn">▲ 다른 서버와 다름</span> · '
            '<span class="c-ok">✔ 권장과 같음</span> · — 미관측. 공식 판정은 공식 보고서 결과가 우선하며 이 표는 자동 변경을 뜻하지 않습니다.</p>'
            '<div class="table-scroll"><table class="cmp"><thead><tr>%s</tr></thead><tbody>%s%s</tbody></table></div></section>' % (
                mismatches, head, recommended, ''.join(rows)))


def collection_html(n):
    collection = n['collection']
    sections = collection['sections']
    peak = max([v['reported_errors'] for v in sections.values() if isinstance(v.get('reported_errors'), int)] + [1])
    bars = []
    for key, v in sections.items():
        name = SECTION_LABELS.get(key, key)
        if v['status'] != 'provided':
            bars.append('<li class="cbar na"><span class="cbar-k">%s</span><span class="cbar-t">미제공 / 미지원</span></li>' % a.esc(name))
            continue
        errs = v['reported_errors']
        bars.append('<li class="cbar"><span class="cbar-k">%s</span><span class="cbar-track"><span class="cbar-fill%s" style="width:%.1f%%"></span></span>'
                    '<span class="cbar-t">오류 기록 행 %d / 기록 %d</span></li>' % (
                        a.esc(name), ' zero' if not errs else '', errs * 100.0 / peak if errs else 0, errs, v['records']))
    types = collection.get('error_type_counts') or {}
    subjects, kinds = [], []
    for key in types:
        subject, _, kind = key.partition(' / ')
        subjects += [subject] if subject not in subjects else []
        kinds += [kind] if kind not in kinds else []
    heat = ''
    if types:
        top = max(types.values())
        rows = ''
        for subject in subjects:
            cells = ''
            for kind in kinds:
                count = types.get(subject + ' / ' + kind, 0)
                level = 0 if not count else min(4, 1 + int(3 * count / top))
                cells += '<td class="heat h%d">%s</td>' % (level, count if count else '·')
            rows += '<tr><th scope="row">%s</th>%s</tr>' % (a.esc(subject), cells)
        heat = ('<h3>대상 × 유형 (고정 분류, 원인 확정 아님)</h3><div class="table-scroll"><table class="heatmap"><thead><tr><th>대상 ↓ · 유형 →</th>%s</tr></thead><tbody>%s</tbody></table></div>'
                % (''.join('<th scope="col">%s</th>' % a.esc(k) for k in kinds), rows))
    reasons = collection.get('incomplete_reasons') or []
    caveat = ' 전체 수 미확정: ' + ', '.join(INCOMPLETE_LABELS.get(x) or ('형식 이상 행: ' + SECTION_LABELS.get(x.split(':', 1)[-1], x)) for x in reasons) + '.' if reasons else ''
    count = error_count_text(collection)
    # 0.5.5: 등급(경고·정보) 없이 부록의 '수집되지 않은 항목' 안내로만 둔다.
    return ('<section id="uncollected"><details class="appendix"><summary>부록: 수집되지 않은 항목 (영역·대상·유형 집계)</summary>'
            '<p class="note">원본 gzip에서 오류가 기록돼 값이 수집되지 않은 행: <strong>%s</strong>. 이 도구는 이 값으로 판정하지 않으며 설정 변경 대상도 아닙니다. '
            '오류 본문·내부 주소는 내보내지 않습니다. 기록이 없다는 것은 전체 수집 성공의 증거가 아닙니다.%s</p>'
            '<h3>수집 영역별</h3><ul class="cbars">%s</ul>%s</details></section>') % (
        a.esc(a.MISSING if count is None else count), a.esc(caveat), ''.join(bars), heat)


R2_CSS = '''<style>
.srvgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.srv{background:var(--card);border:1px solid var(--line);border-top:5px solid var(--unknown);border-radius:10px;padding:10px 12px}
.srv.tone-b-passed{border-top-color:var(--passed)}.srv.tone-b-error{border-top-color:var(--error)}
.srv-h{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline;margin-bottom:8px;font-size:14px}
.drives{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:6px}
.drv{display:flex;flex-direction:column;gap:2px;border:1px solid var(--line);border-left:4px solid var(--unknown);border-radius:6px;padding:6px 8px;font-size:12px;background:var(--card);color:var(--fg)}
.drv.tone-passed{border-left-color:var(--passed)}.drv.tone-error{border-left-color:var(--error);background:var(--error-bg)}
.drv-id{font-weight:700}.drv-st{font-weight:600}.drv.tone-passed .drv-st{color:var(--passed)}.drv.tone-error .drv-st{color:var(--error)}.drv-ix,.drv-u{color:var(--muted)}
.meter{display:block;height:8px;border-radius:4px;background:var(--bar);overflow:hidden}.fill{display:block;height:100%}
.u-lo{background:var(--accent)}.u-mid{background:var(--warning)}.u-hi{background:var(--error)}
.key{display:inline-block;width:12px;height:8px;border-radius:2px;margin:0 3px 0 6px}.legend{font-size:13px;color:var(--muted)}
.cmp td,.cmp th{min-width:110px}.cmp .rec td,.cmp .rec th{background:var(--bar)}
.c-ok{color:var(--passed)}.c-bad{background:var(--error-bg);color:var(--error)}.c-warn{background:var(--warning-bg);color:var(--warning)}.c-na{color:var(--muted)}
td.c-bad strong,td.c-warn strong{color:inherit}
.cbars{list-style:none;padding:0;margin:0;display:grid;gap:6px;max-width:760px}
.cbar{display:grid;grid-template-columns:130px minmax(80px,1fr) 150px;gap:10px;align-items:center;font-size:14px}
.cbar.na{color:var(--muted)}.cbar.na .cbar-t{grid-column:2/4}
.cbar-track{height:14px;background:var(--bar);border-radius:4px;overflow:hidden}.cbar-fill{display:block;height:100%;background:var(--muted)}
.heatmap td{text-align:center;min-width:90px;font-weight:700}.heat.h0{color:var(--muted);font-weight:400}
.heat.h1{background:color-mix(in srgb,var(--muted) 18%,var(--card))}.heat.h2{background:color-mix(in srgb,var(--muted) 35%,var(--card))}
.heat.h3{background:color-mix(in srgb,var(--muted) 55%,var(--card))}.heat.h4{background:color-mix(in srgb,var(--muted) 75%,var(--card));color:#fff}
@media (max-width:600px){.cbar{grid-template-columns:1fr}}
</style>'''


def health_dashboard(r, n):
    states = Counter(d.get('state') or 'unknown' for d in n['drives'])
    count = error_count_text(n['collection'])
    link = (r.get('input_link') or {}).get('status')
    return ('<div class="panel"><h3>원본 관측 (gzip)</h3><div class="tiles">%s%s%s</div><p class="note">입력 결합: %s</p></div>') % (
        a.tile('info', '서버 기록', len(n['servers'])),
        a.tile('info', '드라이브 기록', len(n['drives']),
               '관측 상태 ok %d · offline %d · 미확인 %d' % (states['ok'], states['offline'], states['unknown'])),
        a.tile('info', '수집되지 않은 행', count, '부록 참고(판정 아님)' if count else ''), a.esc(a.plain(a.label(link))))


def html_report(r):
    v = view(r)
    n = r.get('facts', {}).get('health')
    sections, extra, raw = [], '', []
    if isinstance(n, dict):
        extra = health_dashboard(r, n)
        sections = [('topology', '서버 × 드라이브', drive_grid_html(n)), config_table_html(n)]
        raw = [('uncollected', '수집되지 않은 항목', collection_html(n)),
               ('raw', '원본 관측 표', '<section id="raw"><details class="appendix"><summary>원본 관측 표 전체 (서버·드라이브·설정·수집 영역)</summary>%s</details></section>'
                % observations_html(r))]
    page = a.html_page(v, sections=sections, dashboard_extra=extra,
                       categories=a.R1_CATEGORIES + ('용량',), before_appendix=raw)
    return page.replace('</style>', '</style>' + R2_CSS, 1)


def markdown_report(r):
    return a.markdown(view(r), observation_blocks(r))


def main(argv=None):
    parser = argparse.ArgumentParser(description='R2 offline health subset analyzer')
    parser.add_argument('--health', type=Path, required=True)
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--official-report', type=Path)
    parser.add_argument('--out-dir', type=Path, default=a.DEFAULT_OUTPUT_DIR)
    parser.add_argument('--mask', choices=('basic', 'strict'), default='strict')
    parser.add_argument('--lang', choices=('ko',), default='ko')
    parser.add_argument('--fail-on-priority', choices=('P1', 'P2', 'P3', 'P4'))
    parser.add_argument('--as-of')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args(argv)
    markdown_stage = False
    try:
        body, count = read(args.health)
        text = None
        if args.official_report:
            markdown_stage = True
            with args.official_report.open('rb') as stream:
                raw = stream.read() if a.MAX_BYTES is None else stream.read(a.MAX_BYTES + 1)
            if a.over_limit(len(raw), a.MAX_BYTES):
                raise a.InputError('Markdown limit')
            text = raw.decode('utf-8-sig')
            markdown_stage = False
        r = report(body, count, text, args.as_of, args.mask)  # Markdown 해석 실패는 OfficialInputError
    except LinkConflict as exc:
        print('R2 입력 오류: 결합 거부', file=sys.stderr)
        for line in conflict_lines(exc.detail):
            print(('  ' if line.startswith(' ') else '  - ') + line, file=sys.stderr)
        return 2
    except (a.InputError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print('R2 입력 오류: 형식·제한·필수 구조를 확인하십시오.', file=sys.stderr)
        for line in a.describe_input_error(exc, '공식 Markdown(--official-report)' if markdown_stage or isinstance(exc, OfficialInputError) else '원본 gzip(--health)'):
            print('  - ' + line, file=sys.stderr)
        return 2
    except a.OutputError:
        print('R2 마스킹 실패: 결과를 저장하지 않았습니다.', file=sys.stderr)
        return 3
    if r['run_status'] == 'partial' and not args.allow_partial:
        print('R2 부분 분석: --allow-partial 없이 결과를 확정하지 않습니다.', file=sys.stderr)
        return 4
    try:
        a.refuse_input_output_alias(args.out_dir, ('원본 gzip(--health)', args.health),
                                    ('공식 Markdown(--official-report)', args.official_report))
    except (a.OutputError, OSError) as exc:
        print('\n'.join(a.output_failure_lines(exc)), file=sys.stderr)
        return 3
    try:
        generation = a.save(r, args.out_dir, args.force, renderers={
            'markdown': markdown_report, 'html': html_report})
        print('R2 결과 세트 확정: ' + str(generation.resolve()))
        print(a.result_summary_line(r))
    except (a.OutputError, OSError):
        print('R2 저장 실패: 기존 확정 세트를 보존합니다.', file=sys.stderr)
        return 3
    if r['run_status'] == 'partial':
        return 4
    # R1과 같은 의미: 지정한 우선순위의 항목이 있으면 1 (부분 분석 4가 우선).
    if args.fail_on_priority and any(f['priority'] == args.fail_on_priority for f in a.official_cards(r['findings'])):
        return 1
    return 0
