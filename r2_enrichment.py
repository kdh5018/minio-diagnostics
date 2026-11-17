"""Allowlisted system observations, source coverage and non-forced input linking."""
from collections import Counter
import re

import aistordoctor as a

TIME_TOLERANCE_SECONDS = 120  # 결합 안전장치. 완화하지 않는다.
SYSTEM_SECTIONS = ('cpus', 'partitions', 'osinfo', 'meminfo', 'procinfo', 'netinfo', 'errors', 'services', 'config')


def endpoint(value):
    """Internal equality only. Never exported, logged or used for connections."""
    if not isinstance(value, str):
        return None
    value = re.sub(r'^https?://', '', value.strip().strip('`'), flags=re.I).rstrip('/')
    return value.lower() if re.fullmatch(r'(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}', value) else None


def server_targets(body):
    """Internal endpoint -> server alias map; endpoints never leave this module."""
    servers = body.get('minio', {}).get('info', {}).get('servers', [])
    return {endpoint(s.get('endpoint')): 'server-%04d' % (i + 1) for i, s in enumerate(servers) if isinstance(s, dict) and endpoint(s.get('endpoint'))}


def rule(rid, version, result, reason, title, category, required, condition, path, basis):
    """One custom-rule result record (shared by R2 health and system rules)."""
    return {'id': rid, 'rule_id': rid, 'rule_version': version, 'source_check_id': None,
            'origin': 'custom_rule', 'official_status': None, 'official_status_reason': 'not_applicable',
            'rule_result': result, 'assessment': 'assessable' if result else 'insufficient_data', 'reason': reason,
            'title': title, 'category': category, 'required_inputs': required, 'condition': condition,
            'source_locator': {'input': 'health-001', 'json_path': path}, 'basis': basis}


def active_thp(value):
    if not isinstance(value, str):
        return None
    words = value.split()
    if not words or any(w.strip('[]') not in ('always', 'madvise', 'never', 'defer', 'defer+madvise') for w in words):
        return None
    selected = re.findall(r'\[(always|madvise|never|defer|defer\+madvise)\]', value)
    return selected[0] if len(selected) == 1 else value if value in ('always', 'madvise', 'never') else None


# 공식 sys.mem.thp 검사 Resolution에 적힌 권장값. 공식 판정 자체는 공식 Markdown 결과가 우선한다.
THP_OFFICIAL_RECOMMENDED = {'thp_enabled': ('madvise',), 'thp_defrag': ('defer+madvise', 'madvise'), 'thp_max_ptes_none': (0,)}

# 수집 오류 유형 분류. 고정 라벨만 내보내며 원문·경로·장치명은 저장하지 않는다. 원인 확정이 아니다.
ERROR_SUBJECTS = (('CPU 주파수 정책(cpufreq/scaling_governor)', re.compile(r'scaling_governor|cpufreq', re.I)),
                  ('NIC ring 파라미터', re.compile(r'ring param', re.I)))
ERROR_KINDS = (('파일 없음', re.compile(r'no such file|not found|does not exist', re.I)),
               ('미지원', re.compile(r'not supported|unsupported|not implemented', re.I)),
               ('권한 거부', re.compile(r'permission denied|operation not permitted|access denied', re.I)),
               ('시간 초과', re.compile(r'timeout|timed out|deadline', re.I)))


def classify_error(value):
    texts = [value] if isinstance(value, str) else [x for x in value if isinstance(x, str)] if isinstance(value, list) else []
    text = ' '.join(texts)[:4096]
    subject = next((label for label, rx in ERROR_SUBJECTS if rx.search(text)), '미분류 대상')
    kind = next((label for label, rx in ERROR_KINDS if rx.search(text)), '기타')
    return subject + ' / ' + kind


def extract(body, normalized, number):
    sys = body.get('sys') if isinstance(body.get('sys'), dict) else {}
    targets = server_targets(body)
    configs = sys.get('config')
    configurations = []
    if isinstance(configs, list):
        for index, row in enumerate(configs):
            if not isinstance(row, dict):
                continue
            config = row.get('config') if isinstance(row.get('config'), dict) else {}
            thp = config.get('thp-config') if isinstance(config.get('thp-config'), dict) else {}
            cmd = config.get('proc-cmdline')
            tokens = [word for item in cmd if isinstance(item, str) for word in item.split()] if isinstance(cmd, list) else []
            iommu = {}
            for token in tokens:
                matched = re.fullmatch(r'(intel_iommu|amd_iommu|iommu)=(on|off|pt|soft|0|1)', token.lower())
                if matched:
                    key, value = matched.groups()
                    value = {'0': 'off', '1': 'on'}.get(value, value)
                    iommu[key] = value if key not in iommu or iommu[key] == value else None
            configurations.append({'id': 'config-%04d' % (index + 1), 'server_id': targets.get(endpoint(row.get('addr'))),
                'thp_enabled': active_thp(thp.get('enabled')), 'thp_defrag': active_thp(thp.get('defrag')),
                'thp_max_ptes_none': number(int(thp['max_ptes_none'])) if isinstance(thp.get('max_ptes_none'), str) and re.fullmatch(r'[0-9]{1,16}', thp['max_ptes_none']) else None,
                'rlimit_max': number(config.get('rlimit-max')), 'iommu_flags': iommu, 'proc_cmdline_observed': isinstance(cmd, list),
                'source_locator': {'input': 'health-001', 'json_path': '$.sys.config[%d].config' % index},
                'target_scope': 'reported endpoint; physical-host identity not established'})
    coverage = {}
    errors = 0
    types = Counter()
    incomplete = []
    if not isinstance(sys.get('errors'), list):
        incomplete.append('sys_errors_missing_or_not_list')
    for section in SYSTEM_SECTIONS:
        records = sys.get(section)
        if not isinstance(records, list):
            coverage[section] = {'status': 'missing_or_unrecognized', 'records': None, 'reported_errors': None,
                                 'malformed_records': None, 'error_types': {}}
            continue
        section_errors = malformed = 0
        section_types = Counter()
        for row in records:
            if not isinstance(row, dict):
                malformed += 1
                continue
            if bool(row.get('error')) or bool(row.get('errors')):
                section_errors += 1
                section_types[classify_error(row.get('error') or row.get('errors'))] += 1
        if malformed:
            incomplete.append('malformed_records:' + section)
        errors += section_errors
        types.update(section_types)
        coverage[section] = {'status': 'provided', 'records': len(records), 'reported_errors': section_errors,
                             'malformed_records': malformed, 'error_types': dict(sorted(section_types.items()))}
    # reported_error_count는 기존 계약(불완전하면 None)을 유지하고, 관측 하한은 별도 필드로 둔다.
    collection = {'reported_error_count': None if incomplete else errors,
                  'observed_error_rows': errors, 'count_basis': 'lower_bound' if incomplete else 'complete',
                  'incomplete_reasons': incomplete, 'error_type_counts': dict(sorted(types.items())),
                  'error_type_scope': 'fixed labels from pattern matching; cause not established; raw text excluded',
                  'sections': coverage, 'error_text_excluded': True,
                  'scope': 'direct error/errors fields in recognized sys arrays; absence is not proof of full collection'}
    groups = Counter((d['pool_index'], d['set_index']) for d in normalized['drives'] if d['pool_index'] is not None and d['set_index'] is not None)
    topology = {'groups': [{'pool_index': p, 'set_index': s, 'observed_drive_records': count} for (p, s), count in sorted(groups.items())],
                'unindexed_drive_records': sum(d['pool_index'] is None or d['set_index'] is None for d in normalized['drives']),
                'physical_failure_domains': None, 'quorum': None, 'reason': 'indices do not prove complete sets, physical hosts or quorum'}
    return configurations, collection, topology


def link(body, official_text, stamp, parsed=None):
    if official_text is None:
        return {'status': 'health_only', 'reason': 'optional Markdown not provided', 'same_collection_proven': False}
    _, collected, checks, _ = parsed or a.parse_report(official_text)
    found = set()
    for check in checks:
        for block in check['evidence_blocks']:
            if block.get('type') != 'table':
                continue
            columns = [i for i, h in enumerate(block.get('headers', [])) if h.strip().lower() in ('server', 'endpoint')]
            for row in block.get('rows', []):
                for i in columns:
                    if i < len(row) and endpoint(row[i]):
                        found.add(endpoint(row[i]))
    targets = set(server_targets(body))
    raw_time = stamp(body.get('timestamp'))
    delta = (collected - raw_time).total_seconds() if collected and raw_time else None
    targets_match = bool(found and targets) and found == targets
    time_matches = delta is not None and abs(delta) <= TIME_TOLERANCE_SECONDS
    target_conflict = bool(found and targets and found != targets)
    time_conflict = delta is not None and abs(delta) > TIME_TOLERANCE_SECONDS
    result = {'status': 'conflict' if target_conflict or time_conflict else 'consistent_observations' if targets_match and time_matches else 'insufficient_evidence',
              'targets_match': targets_match, 'time_delta_seconds': delta, 'time_tolerance_seconds': TIME_TOLERANCE_SECONDS,
              'same_collection_proven': False, 'deployment_identity_proven': False,
              'reason': 'targets and bounded timestamps only; no collection/deployment identifier proof; no cross-filling'}
    if result['status'] == 'conflict':
        # 안내 메시지용 세부(대상은 개수만, 주소는 포함하지 않음). 저장 결과에는 넣지 않는다.
        result['conflict_detail'] = {'time_conflict': time_conflict, 'target_conflict': target_conflict,
            'markdown_collected_at': collected.isoformat() if collected else None,
            'health_collected_at': raw_time.isoformat() if raw_time else None,
            'markdown_target_count': len(found), 'health_target_count': len(targets),
            'shared_target_count': len(found & targets)}
    return result


def thp_result(configurations):
    # 관측값을 공식 Resolution 권장값과 비교한다. 결측값은 일치로 간주하지 않는다.
    if not configurations:
        return None, 'thp_config_missing'
    mismatch, complete = False, True
    for config in configurations:
        for key, allowed in THP_OFFICIAL_RECOMMENDED.items():
            value = config.get(key)
            if value is None:
                complete = False
            elif value not in allowed:
                mismatch = True
    if mismatch:
        return 'warning', 'observed_value_differs_from_official_recommendation'
    if complete:
        return 'pass', 'all_observed_values_match_official_recommendation'
    return None, 'thp_values_missing_or_ambiguous'


def system_rules(n):
    configurations = n['system_configurations']
    rules = []
    def add(rid, title, result, reason, required, condition, path, version='r2-system-1'):
        rules.append(rule(rid, version, result, reason, title, '설정', required, condition, path, n['collected_at']))
    result, reason = thp_result(configurations)
    add('r2.thp.mode', 'THP 설정과 공식 권장값 비교(자체 점검)', result, reason,
        ['sys.config.thp-config.enabled', 'sys.config.thp-config.defrag', 'sys.config.thp-config.max_ptes_none'],
        '자체 점검: 공식 sys.mem.thp 검사 Resolution의 권장값(enabled=madvise, defrag=defer+madvise 또는 madvise, max_ptes_none=0)과 관측값을 비교. '
        '하나라도 다르면 경고, 세 값이 모두 관측·일치하면 통과, 값이 없으면 미평가. 공식 판정은 공식 보고서 결과가 우선하며 실제 지연·장애를 확정하거나 자동 변경하지 않음',
        '$.sys.config', 'r2-system-2')
    flags = [c['iommu_flags'] for c in configurations]
    result = None
    if flags and all(x and all(v is not None for v in x.values()) for x in flags):
        result = 'pass' if len({tuple(sorted(x.items())) for x in flags}) == 1 else 'warning'
    if result:
        reason = 'explicit_flags_only'
    elif not configurations or not all(c.get('proc_cmdline_observed') for c in configurations):
        reason = 'proc_cmdline_missing'
    elif not any(flags):
        reason = 'explicit_flags_absent_in_cmdline'
    else:
        reason = 'explicit_flags_unobserved_or_ambiguous'  # 일부 서버만 명시·값 충돌
    add('r2.iommu.consistency', '명시적 IOMMU 부팅 옵션 일치', result, reason, ['sys.config.proc-cmdline'],
        '모든 서버에 iommu·intel_iommu·amd_iommu 옵션이 명시된 경우에만 서버 간 일치를 비교. 부팅 옵션 기록이 있어도 옵션이 없으면 미평가로 두며 '
        '부재를 off로 해석하지 않음. 공식 sys.mem.iommu 검사의 오류 판정과 별개이며 하드웨어 지원·I/O 장애는 판정하지 않음', '$.sys.config')
    collection = n['collection']
    count, observed = collection['reported_error_count'], collection.get('observed_error_rows')
    if count:
        result, reason = 'warning', 'recognized_error_fields_only'
    elif count == 0:
        result, reason = 'pass', 'recognized_error_fields_only'
    elif observed:
        result, reason = 'warning', 'lower_bound_error_rows_observed'
    else:
        result, reason = None, 'error_coverage_unavailable'
    add('r2.collection.errors', '제공된 수집 오류 기록', result, reason, ['sys.errors'],
        '알려진 sys 배열의 비어 있지 않은 error/errors만 집계. sys.errors 부재나 형식 이상 행이 있으면 관측 행 수는 최소값이며, 이때 0건이면 미평가. '
        '기록 부재는 전체 수집 성공을 증명하지 않음', '$.sys', 'r2-system-2')
    return rules
