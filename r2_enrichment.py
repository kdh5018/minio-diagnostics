"""Allowlisted system observations, source coverage and non-forced input linking."""
from collections import Counter
import re

import aistordoctor as a

SYSTEM_SECTIONS = ('cpus', 'partitions', 'osinfo', 'meminfo', 'procinfo', 'netinfo', 'errors', 'services', 'config')


def endpoint(value):
    """Internal equality only. Never exported, logged or used for connections."""
    if not isinstance(value, str):
        return None
    value = re.sub(r'^https?://', '', value.strip().strip('`'), flags=re.I).rstrip('/')
    return value.lower() if re.fullmatch(r'(?:[A-Za-z0-9_.-]+|\[[0-9A-Fa-f:]+\]):[0-9]{1,5}', value) else None


def active_thp(value):
    if not isinstance(value, str):
        return None
    words = value.split()
    if not words or any(w.strip('[]') not in ('always', 'madvise', 'never', 'defer', 'defer+madvise') for w in words):
        return None
    selected = re.findall(r'\[(always|madvise|never|defer|defer\+madvise)\]', value)
    return selected[0] if len(selected) == 1 else value if value in ('always', 'madvise', 'never') else None


def extract(body, normalized, number):
    sys = body.get('sys') if isinstance(body.get('sys'), dict) else {}
    raw_servers = body.get('minio', {}).get('info', {}).get('servers', [])
    targets = {endpoint(s.get('endpoint')): 'server-%04d' % (i + 1) for i, s in enumerate(raw_servers) if isinstance(s, dict) and endpoint(s.get('endpoint'))}
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
                'rlimit_max': number(config.get('rlimit-max')), 'iommu_flags': iommu,
                'source_locator': {'input': 'health-001', 'json_path': '$.sys.config[%d].config' % index},
                'target_scope': 'reported endpoint; physical-host identity not established'})
    coverage = {}
    errors = 0
    malformed = False
    for section in SYSTEM_SECTIONS:
        records = sys.get(section)
        if not isinstance(records, list):
            coverage[section] = {'status': 'missing_or_unrecognized', 'records': None, 'reported_errors': None}
            continue
        section_errors = 0
        for row in records:
            if not isinstance(row, dict):
                malformed = True
                continue
            if bool(row.get('error')) or bool(row.get('errors')):
                section_errors += 1
        errors += section_errors
        coverage[section] = {'status': 'provided', 'records': len(records), 'reported_errors': section_errors}
    collection = {'reported_error_count': errors if isinstance(sys.get('errors'), list) and not malformed else None,
                  'sections': coverage, 'error_text_excluded': True,
                  'scope': 'direct error/errors fields in recognized sys arrays; absence is not proof of full collection'}
    groups = Counter((d['pool_index'], d['set_index']) for d in normalized['drives'] if d['pool_index'] is not None and d['set_index'] is not None)
    topology = {'groups': [{'pool_index': p, 'set_index': s, 'observed_drive_records': count} for (p, s), count in sorted(groups.items())],
                'unindexed_drive_records': sum(d['pool_index'] is None or d['set_index'] is None for d in normalized['drives']),
                'physical_failure_domains': None, 'quorum': None, 'reason': 'indices do not prove complete sets, physical hosts or quorum'}
    return configurations, collection, topology


def link(body, official_text, stamp):
    if official_text is None:
        return {'status': 'health_only', 'reason': 'optional Markdown not provided', 'same_collection_proven': False}
    _, collected, checks, _ = a.parse_report(official_text)
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
    servers = body.get('minio', {}).get('info', {}).get('servers', [])
    targets = {endpoint(s.get('endpoint')) for s in servers if isinstance(s, dict) and endpoint(s.get('endpoint'))}
    raw_time = stamp(body.get('timestamp'))
    delta = (collected - raw_time).total_seconds() if collected and raw_time else None
    targets_match = bool(found and targets) and found == targets
    time_matches = delta is not None and abs(delta) <= 120
    conflict = bool(found and targets and found != targets) or delta is not None and abs(delta) > 120
    return {'status': 'conflict' if conflict else 'consistent_observations' if targets_match and time_matches else 'insufficient_evidence',
            'targets_match': targets_match, 'time_delta_seconds': delta, 'time_tolerance_seconds': 120,
            'same_collection_proven': False, 'deployment_identity_proven': False,
            'reason': 'targets and bounded timestamps only; no collection/deployment identifier proof; no cross-filling'}


def system_rules(n):
    configurations = n['system_configurations']
    rules = []
    def add(rid, title, result, reason, required, condition, path):
        rules.append({'id': rid, 'rule_id': rid, 'rule_version': 'r2-system-1', 'source_check_id': None,
            'origin': 'custom_rule', 'official_status': None, 'official_status_reason': 'not_applicable',
            'rule_result': result, 'assessment': 'assessable' if result else 'insufficient_data', 'reason': reason,
            'title': title, 'category': '설정', 'required_inputs': required, 'condition': condition,
            'source_locator': {'input': 'health-001', 'json_path': path}, 'basis': n['collected_at']})
    modes = [c['thp_enabled'] for c in configurations]
    result = ('pass' if all(x == 'never' for x in modes) else 'warning') if modes and all(modes) else None
    add('r2.thp.mode', 'THP 활성 설정 관측', result, 'observed_mode_only' if result else 'active_mode_missing', ['sys.config.thp-config.enabled'],
        '도구의 초기 정책: never는 통과, always/madvise는 검토 경고. 실제 지연·장애를 확정하거나 자동 변경하지 않음', '$.sys.config')
    flags = [c['iommu_flags'] for c in configurations]
    result = None
    if flags and all(x and all(v is not None for v in x.values()) for x in flags):
        result = 'pass' if len({tuple(sorted(x.items())) for x in flags}) == 1 else 'warning'
    add('r2.iommu.consistency', '명시적 IOMMU 부팅 옵션 일치', result, 'explicit_flags_only' if result else 'explicit_flags_unobserved_or_ambiguous', ['sys.config.proc-cmdline'],
        '명시된 옵션만 비교. 옵션 부재를 off로 해석하지 않으며 하드웨어 지원·I/O 장애는 판정하지 않음', '$.sys.config')
    count = n['collection']['reported_error_count']
    result = 'warning' if count else 'pass' if count == 0 else None
    add('r2.collection.errors', '제공된 수집 오류 기록', result, 'recognized_error_fields_only' if result else 'error_coverage_unavailable', ['sys.errors'],
        '알려진 sys 배열의 비어 있지 않은 error/errors만 집계. 기록 부재는 전체 수집 성공을 증명하지 않음', '$.sys')
    return rules
