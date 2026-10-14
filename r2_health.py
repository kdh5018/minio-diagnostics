"""R2 limited offline adapter. No server access, collectors or raw-data export."""
import argparse
from collections import Counter
import copy
import datetime as dt
import gzip
import io
import json
from pathlib import Path
import re
import sys
import time
import zlib

import aistordoctor as a

VERSION = '0.2.0-r2-candidate'
MAX_COMPRESSED = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 200000
MAX_STRING = 65536
MAX_SERVERS = 1000
MAX_DRIVES = 100000
SECONDS = 10


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
        quoted = escaped = False
        for index, char in enumerate(text):
            if index % 65536 == 0 and time.monotonic() - started > SECONDS:
                raise a.InputError('processing time limit')
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > MAX_DEPTH:
                    raise a.InputError('nesting limit')
            elif char in ']}':
                depth -= 1
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
            if count > MAX_NODES or time.monotonic() - started > SECONDS:
                raise a.InputError('resource limit')
            if isinstance(value, dict):
                stack.extend(value.keys())
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str) and len(value) > MAX_STRING:
                raise a.InputError('string limit')
        body = documents[-1]
        if len(documents) == 2 and set(documents[0]) != {'version'}:
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
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def release(value):
    if not isinstance(value, str):
        return None
    candidate = value.removeprefix('RELEASE.')
    if re.fullmatch(r'v?[0-9]+(?:\.[0-9]+){1,3}', candidate) or stamp(candidate):
        return value
    return None


def normalize(body):
    info = body.get('minio', {}).get('info') if isinstance(body.get('minio'), dict) else None
    if not isinstance(info, dict) or not isinstance(info.get('servers'), list):
        raise a.InputError('minimum server-array structure unavailable')
    if len(info['servers']) > MAX_SERVERS:
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
            if len(drives) >= MAX_DRIVES:
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
                  'excluded': ['environment_variables', 'configuration_values', 'license_data', 'raw_errors', 'endpoints', 'deployment_identifiers']}
    return normalized, sorted(set(notes))


CATALOG = {
    'r2.drive.state': ('드라이브 관측 상태', '가용성', ['drives[].state'], 'ok/offline만 평가; 다른 상태는 판정 불가'),
    'r2.capacity.bounds': ('용량 카운터 범위', '용량', ['totalspace', 'usedspace', 'availspace'], '비음수 정수, used/available 각각 total 이하; 임계 점유율·추세는 미평가'),
    'r2.version.consistency': ('서버 버전 일치', '설정', ['servers[].version'], '관측된 서버 버전 문자열의 일치; 특정 버전을 요구하지 않음'),
    'r2.tls.validity': ('인증서 수집 시점 유효기간', '보안', ['timestamp', 'not_before', 'not_after'], '원본 수집 시점에서 유효기간만 평가; 만료 임박 정책·신뢰체인·SAN은 미평가')}


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
        results.append({'id': rid, 'rule_id': rid, 'rule_version': 'r2-initial-1', 'source_check_id': None,
                        'origin': 'custom_rule', 'official_status': None, 'official_status_reason': 'not_applicable',
                        'rule_result': result, 'assessment': 'assessable' if result else 'insufficient_data',
                        'reason': reason, 'title': title, 'category': category, 'required_inputs': required,
                        'condition': condition, 'source_locator': {'input': 'health-001', 'json_path': '$.minio.info'},
                        'basis': basis.isoformat() if basis else None})
    return results


def report(body, documents=1, official_text=None, as_of=None):
    n, notes = normalize(body)
    checks = evaluate(n, as_of)
    if official_text is not None:
        r = a.model(official_text)
    else:
        r = {'schema_version': '1.1-r1', 'official_summary': None, 'findings': [], 'check_results': [],
             'parser_errors': [], 'policy': {'version': 'r2-initial-1', 'profile': 'unspecified'},
             'input_metadata': {}, 'source_versions': {}, 'facts': {}, 'coverage': {}, 'completeness': {},
             'masking': {'level': 'strict', 'policy_version': 'r2-allowlist-1', 'limitations': 'R2 원본은 허용 필드만 추출하며 공유 전 수동 검토가 필요합니다.'}}
    r['policy'] = {'version': 'r2-initial-1', 'profile': 'unspecified', 'official_explanations': 'r1-1'}
    r['tool_version'] = VERSION
    r['schema_version'] = '1.2-r2'
    r['input_metadata']['parser_version'] = 'r2-structural-1'
    r['input_metadata']['health'] = {'id': 'health-001', 'documents': documents, 'collected_at': n['collected_at'],
                                    'format_version': str(body.get('version')) if type(body.get('version')) in (str, int) and re.fullmatch(r'[0-9]{1,8}', str(body['version'])) else None,
                                    'format_policy': 'structure first; version metadata does not gate execution'}
    r['source_versions']['aistor'] = {'value': sorted({s['version'] for s in n['servers'] if s['version']}), 'reason': 'observed_server_metadata; no required product version'}
    r['source_versions']['mc'] = {'value': None, 'reason': 'not_identified; not_required_for_file_analysis'}
    r['facts']['health'] = n
    r['coverage'].update(health='allowlisted_minio_info_subset', topology='indices_only; no quorum or failure-domain inference',
                         configuration='not_normalized_yet', collection_errors='not_evaluated', comparison='not_supported', operational_analysis='not_supported')
    r['input_link'] = {'status': 'independent_inputs' if official_text else 'health_only',
                       'reason': 'no forced field filling or same-deployment assertion; correspondence verification remains pending'}
    r['parser_errors'].extend(notes)
    r['check_results'].extend(checks)
    for c in checks:
        if c['rule_result'] == 'pass':
            continue
        r['findings'].append(dict(c, assessment_reason=c['reason'], priority='P2' if c['rule_result'] else 'unclassified',
            observed='자체 점검 결과: ' + str(c['rule_result']) + ' / ' + c['reason'], detail=c['condition'],
            evidence_blocks=[], recommendation='원본 관측과 실제 대상을 읽기 전용으로 대조하십시오. 자동 조치는 수행하지 않습니다.',
            impact='현재 서비스 상태·데이터 안전성·원인을 확정하지 않습니다.', approval_required=True if c['rule_result'] else None,
            verification_steps=['필요한 입력과 의미를 확인한 뒤 동일 조건으로 재점검합니다.'], limitations=[c['condition']]))
    r['analysis_summary'] = {'custom_rule_counts': dict(Counter(c['rule_result'] or 'not_evaluated' for c in checks)),
                             'custom_rule_catalog': CATALOG}
    partial = bool(r['parser_errors']) or any(c['rule_result'] is None for c in checks)
    r['run_status'] = 'partial' if partial else 'success'
    r['completeness'] = {'source_coverage': 'explicit_allowlist_subset', 'requested_scope_satisfied': not partial,
                         'limitations': '범위 밖 필드는 미지원입니다. success는 수집 완전성·시스템 정상 판정이 아닙니다.'}
    return r


def view(r):
    # Presentation labels never alter the serialized official_status=null contract.
    v = copy.deepcopy(r)
    for f in v['findings']:
        if f['origin'] == 'custom_rule':
            f['official_status'] = '해당 없음 / 자체 결과: ' + str(f['rule_result'])
    v['facts']['custom_check_results'] = r['check_results']
    return v


def main(argv=None):
    parser = argparse.ArgumentParser(description='R2 offline health subset analyzer')
    parser.add_argument('--health', type=Path, required=True)
    parser.add_argument('--official-report', type=Path)
    parser.add_argument('--out-dir', type=Path, default=a.DEFAULT_OUTPUT_DIR)
    parser.add_argument('--mask', choices=('strict',), default='strict')
    parser.add_argument('--as-of')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args(argv)
    try:
        body, count = read(args.health)
        text = None
        if args.official_report:
            with args.official_report.open('rb') as stream:
                raw = stream.read(a.MAX_BYTES + 1)
            if len(raw) > a.MAX_BYTES:
                raise a.InputError('Markdown limit')
            text = raw.decode('utf-8-sig')
        r = report(body, count, text, args.as_of)
    except (a.InputError, OSError, UnicodeError, ValueError, TypeError, KeyError):
        print('R2 입력 오류: 형식·제한·필수 구조를 확인하십시오.', file=sys.stderr)
        return 2
    except a.OutputError:
        print('R2 마스킹 실패: 결과를 저장하지 않았습니다.', file=sys.stderr)
        return 3
    if r['run_status'] == 'partial' and not args.allow_partial:
        print('R2 부분 분석: --allow-partial 없이 결과를 확정하지 않습니다.', file=sys.stderr)
        return 4
    try:
        for source in (args.health, args.official_report):
            if source and (source.resolve() == args.out_dir.resolve() or source.resolve() == (args.out_dir / 'CURRENT.json').resolve()):
                raise a.OutputError('input/output collision')
        generation = a.save(r, args.out_dir, args.force, renderers={
            'markdown': lambda x: a.markdown(view(x)), 'html': lambda x: a.html_report(view(x))})
        print('R2 결과 세트 확정: ' + str(generation))
    except (a.OutputError, OSError):
        print('R2 저장 실패: 기존 확정 세트를 보존합니다.', file=sys.stderr)
        return 3
    return 4 if r['run_status'] == 'partial' else 0
