"""MinIO AIStor R1 offline report analyzer; original implementation, stdlib only.
R2 health and R3/R4 analysis are deliberately not implemented in R1.
"""
import argparse
import bisect
from collections import Counter
import datetime as dt
import errno
import functools
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

VERSION = '0.5.10-r1'
SCHEMA_VERSION = '1.2-r1'
PARSER_VERSION = 'official-markdown-r1-2'
POLICY_VERSION = 'r1-1'
DEFAULT_OUTPUT_DIR = Path(os.environ.get('AISTOR_DIAG_OUTPUT_DIR', str(Path.home() / 'aistor-diag-output')))
# 0.5.11: 입력 크기·줄 수·근거 블록 수·출력 합계·처리 시간 한도를 기본에서 없앤다(None = 한도 없음).
# 대형 클러스터나 느린 서버에서 보고서를 못 만드는 일을 막기 위해서다. 값을 넣으면 이전처럼 그 한도를 건다.
# 검사 항목 수(MAX_CHECKS)는 클러스터 크기와 무관한 공식 검사 목록이라 남긴다.
MAX_BYTES = None
MAX_LINES = None
MAX_CHECKS = 1000
MAX_BLOCKS = None
MAX_OUTPUT_BYTES = None
MAX_PROCESS_SECONDS = None
KST = dt.timezone(dt.timedelta(hours=9))


class InputError(ValueError):
    pass


class OutputError(ValueError):
    pass


OUTPUT_FAILURE_MESSAGE = '출력 실패: 경로·충돌·접근권한을 확인하십시오. 기존 확정 결과를 보존합니다.'


class InputOutputCollision(OutputError):
    """입력이 출력 위치와 겹침. 원인 설명(경로·내용 없이 입력 이름만)을 함께 가진다."""
    def __init__(self, label, reason):
        super().__init__('input/output collision')
        self.lines = ['원인: %s 파일이 %s입니다. 저장하면 입력이 바뀌므로 저장하지 않았습니다.' % (label, reason),
                      '조치: 입력 파일을 다른 위치에 복사해 지정하거나, 다른 --out-dir을 지정하십시오.']


def output_failure_lines(exc):
    """출력 실패 시 stderr 줄. 첫 줄은 항상 OUTPUT_FAILURE_MESSAGE, 원인을 아는 경우 뒤에 덧붙인다."""
    return [OUTPUT_FAILURE_MESSAGE] + ['  - ' + line for line in getattr(exc, 'lines', [])]


def refuse_input_output_alias(out_dir, *sources):
    """R1·R2 공용: 입력이 출력 폴더 자체이거나 그 CURRENT.json(경로·하드링크 포함)이면 거부한다.

    sources: (표시 이름, 경로) 쌍. 경로가 None이면 건너뛴다.
    """
    pointer = out_dir / 'CURRENT.json'
    for label, source in sources:
        if not source:
            continue
        if out_dir.resolve() == source.resolve():
            raise InputOutputCollision(label, '출력 폴더(--out-dir) 자체')
        if pointer.resolve() == source.resolve():
            raise InputOutputCollision(label, '출력 폴더의 최신 결과 포인터(CURRENT.json)와 같은 경로')
        if pointer.exists() and os.path.samefile(pointer, source):
            raise InputOutputCollision(label, '출력 폴더의 최신 결과 포인터(CURRENT.json)와 같은 파일(하드링크)')


def over_limit(value, limit):
    return limit is not None and value > limit


def deadline_after(started, seconds):
    return None if seconds is None else started + seconds


def check_deadline(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise InputError('processing time limit exceeded')


def literal_union(words):
    """0.5.11: 문자열 중 하나라도 들어 있는지 한 번에 찾는 정규식(접두어 트리). 만들 수 없으면 None."""
    if not words:
        return None
    trie = {}
    for word in words:
        node = trie
        for char in word:
            if node.get('') is True:
                break  # 더 짧은 원본이 이미 끝났다. 존재 여부만 보므로 긴 쪽은 필요 없다.
            node = node.setdefault(char, {})
        else:
            node.clear()
            node[''] = True

    def build(node):
        if node.get('') is True:
            return ''
        parts = []
        for char in sorted(node):
            child, text = node[char], re.escape(char)
            while len(child) == 1 and child.get('') is not True:
                (char, child), = child.items()
                text += re.escape(char)
            parts.append(text + build(child))
        return parts[0] if len(parts) == 1 else '(?:' + '|'.join(parts) + ')'

    try:
        return re.compile(build(trie))
    except (RecursionError, re.error, OverflowError):
        return None


# 입력 오류 안내. 키는 이 도구가 만든 고정 문구이며, 입력 내용·경로·원본 오류 본문은 출력하지 않는다.
INPUT_ERROR_HINTS = {
    'recognized summary is required': '공식 진단 Markdown의 Summary 줄(Results: N passed, N error(s), N warning(s))을 찾지 못했습니다. mc가 만든 diag-report-*.md인지 확인하십시오.',
    'required check table missing': '오류·경고 수가 있는데 검사 표(Check/Status 열)를 찾지 못했습니다. 잘리거나 편집된 Markdown일 수 있습니다.',
    'input limit exceeded': '공식 Markdown 크기·줄 수 제한을 넘었습니다(기본은 한도 없음).',
    'check limit exceeded': '검사 항목 수 제한(1,000개)을 넘었습니다.',
    'detail limit exceeded': '상세 검사 절 수 제한(1,000개)을 넘었습니다.',
    'block limit exceeded': '근거 블록 수 제한을 넘었습니다(기본은 한도 없음).',
    'processing time limit exceeded': '처리 시간 제한을 넘었습니다(기본은 한도 없음).',
    'as-of must include timezone': '--as-of에는 시간대가 필요합니다. 예: 2026-10-12T09:00:00+09:00 또는 2026-10-12T00:00:00Z',
    'timezone-aware as-of required': '--as-of에는 시간대가 필요합니다. 예: 2026-10-12T09:00:00+09:00 또는 2026-10-12T00:00:00Z',
    'Markdown limit': '공식 Markdown 크기 제한을 넘었습니다(기본은 한도 없음).',
    'gzip signature or compressed limit': 'gzip 파일이 아니거나 압축 파일 크기 제한(1 GiB)을 넘었습니다. 공식 Markdown은 --official-report로 지정하십시오.',
    'expanded size limit': '압축 해제 크기 제한(1 GiB, 압축 폭탄 방어)을 넘었습니다.',
    'nesting limit': 'JSON 중첩 깊이 제한(64단계)을 넘었습니다.',
    'serialized string limit before decoding': 'JSON 문자열 길이 제한을 넘었습니다.',
    'scalar token limit before decoding': 'JSON 값 길이 제한을 넘었습니다.',
    'structural token limit before decoding': 'JSON 요소 수 제한을 넘었습니다.',
    'resource limit': 'JSON 요소 수 또는 처리 시간 제한을 넘었습니다(기본은 한도 없음).',
    'string limit': 'JSON 문자열 길이 제한(65,536자)을 넘었습니다.',
    'processing time limit': '처리 시간 제한을 넘었습니다(기본은 한도 없음).',
    'ambiguous document count': 'gzip 안의 JSON 문서가 3개 이상입니다(지원: 본문 1개 또는 머리말+본문 2개).',
    'unsupported document structure': 'gzip 안의 JSON 구조가 지원 형식(객체 1~2개)이 아닙니다.',
    'unsupported header structure': 'gzip 첫 JSON 문서가 머리말이 아니라 본문으로 보입니다(지원하지 않는 구조).',
    'duplicate JSON key': 'JSON에 중복 키가 있어 해석을 거부했습니다.',
    'invalid JSON value': 'JSON에 NaN·Infinity 같은 허용되지 않는 값이 있습니다.',
    'minimum server-array structure unavailable': '원본 gzip에 MinIO 서버 정보 배열(minio.info.servers)이 없습니다. mc 진단 원본(diag-data-*.json.gz)인지 확인하십시오.',
    'server limit': '서버 기록 수 제한을 넘었습니다(기본은 한도 없음).',
    'drive limit': '드라이브 기록 수 제한을 넘었습니다(기본은 한도 없음).',
    'certificate limit': '인증서 기록 수 제한(1,000개)을 넘었습니다.',
    'invalid or limited diagnostic input': 'gzip 압축 또는 JSON 해석에 실패했습니다. 파일이 잘렸거나 손상되었을 수 있습니다.',
}


def describe_input_error(exc, label='입력 파일'):
    """Return Korean guidance lines for an input failure without echoing input content or paths."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, PermissionError) or isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM):
            return [label + ' 파일을 읽을 권한이 없습니다.',
                    '파일 소유자 계정으로 실행하거나, 소유자에게 읽기 가능한 사본을 받아 지정하십시오. 이 도구는 파일 권한을 바꾸지 않습니다.']
        if isinstance(exc, FileNotFoundError):
            return [label + ' 파일이 없습니다. 경로와 파일 이름을 확인하십시오.']
        if isinstance(exc, IsADirectoryError):
            return [label + ' 경로가 파일이 아니라 디렉터리입니다.']
        if isinstance(exc, UnicodeError):
            return [label + '이(가) UTF-8 텍스트가 아닙니다. 원본 gzip(diag-data-*.json.gz)은 --health로 지정하십시오.']
        message = exc.args[0] if isinstance(exc, InputError) and exc.args and isinstance(exc.args[0], str) else None
        if message in INPUT_ERROR_HINTS:
            return [label + ': ' + INPUT_ERROR_HINTS[message]]
        exc = exc.__cause__ or exc.__context__
    return [label + ': 형식·인코딩·크기 제한을 확인하십시오.']


# ---------------------------------------------------------------- 사람이 읽는 표시(공통)
MISSING = '미제공 / 미평가'
STATUS_KO = {'success': '요청 범위 처리 완료(시스템 정상 판정 아님)', 'partial': '부분 분석(자료 부족 항목은 미평가로 남김)'}
CODE_KO = {
    # coverage
    'not_available_from_aggregate': '공식 Markdown에는 통과 검사 상세가 없어 복원하지 않음',
    'not_supported_in_r1': '이 실행에서 사용하지 않음(공식 Markdown 단독)',
    'not_supported': '지원하지 않음',
    'allowlisted_minio_info_subset': '허용 목록의 MinIO 서버·드라이브·TLS 정보만 추출',
    'indices_only; no quorum or failure-domain inference': 'pool·set 인덱스만 표시, quorum·장애 도메인은 추론하지 않음',
    'allowlisted_thp_iommu_rlimit': 'THP·IOMMU·rlimit 설정만 추출',
    'recognized_sys_arrays_only': '인식된 sys 배열의 error/errors 필드만 집계',
    # parser notes
    'summary/detail mismatch': '공식 요약 수치와 상세 검사 수가 다름',
    'check detail not uniquely linked': '검사 표의 행과 상세 절을 1:1로 연결하지 못함',
    'unmatched detail preserved': '검사 표에 없는 상세 절을 그대로 보존함',
    'duplicate check ID': '같은 검사 ID가 중복됨',
    'duplicate summary': 'Summary 줄이 두 번 이상 있음',
    'check status conflict': '검사 표와 상세 절의 상태가 다름',
    'unclosed fence': '닫히지 않은 코드 블록이 있음',
    'unrecognized official status': '알 수 없는 공식 상태 값이 있음',
    'invalid certificate timestamp': '인증서 시각 형식이 올바르지 않음',
    'input correspondence evidence insufficient': '두 입력이 같은 대상·시점인지 확인할 증거가 부족함',
    'reported collector errors; source coverage incomplete': '원본에 수집 오류 기록이 있어 수집 범위가 불완전함',
    'collection error count incomplete': 'sys.errors 부재 또는 형식 이상 행 때문에 수집 오류 전체 수를 확정하지 못함',
    'drive array missing': '드라이브 배열이 없는 서버 기록이 있음',
    'invalid server record': '형식이 올바르지 않은 서버 기록이 있음',
    'invalid drive record': '형식이 올바르지 않은 드라이브 기록이 있음',
    'invalid certificate record': '형식이 올바르지 않은 인증서 기록이 있음',
    # input link
    'health_only': '원본 gzip 단독(공식 Markdown 미지정)',
    'consistent_observations': '대상·시각이 허용 범위에서 일치(같은 수집 실행 증명은 아님)',
    'insufficient_evidence': '대응 증거 부족(제한 결과)',
    'conflict': '충돌(결합 거부)',
}
COVERAGE_LABELS = {'passed_details': '통과 검사 상세', 'health': '원본 gzip(R2)', 'comparison': '스냅숏 비교',
                   'operational_analysis': '운영 분석', 'topology': '토폴로지', 'configuration': '시스템 설정',
                   'collection_errors': '수집 오류'}


def summary_text(summary):
    if not isinstance(summary, dict):
        return '공식 Markdown 미지정(공식 판정 없음)'
    total = sum(v for v in summary.values() if isinstance(v, int))
    return '통과 %s · 오류 %s · 경고 %s (총 %s)' % (summary.get('passed'), summary.get('errors'), summary.get('warnings'), total)


def aware_time(value):
    """ISO 8601 time with a timezone (Z accepted), else None."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def with_kst(value):
    if not value:
        return None
    parsed = aware_time(value)
    if parsed is None:
        return str(value)
    return '%s (한국 시각 %s)' % (value, parsed.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S'))


def duration_text(seconds):
    if not isinstance(seconds, int):
        return None
    sign = '-' if seconds < 0 else ''
    return '%s초 (약 %s%.1f일)' % (seconds, sign, abs(seconds) / 86400)


# 0.4.1: 보고서에 보이는 영문 코드에 한국어 설명을 붙인다(JSON 값·HTML 필터 값은 그대로).
LABEL_KO = {
    'unclassified': '미분류', 'assessable': '평가 가능', 'unsupported': '해설 미등록', 'insufficient_data': '자료 부족',
    'official_import_only': '공식 보고서 결과만 반영(이 도구가 다시 판정하지 않음)', 'no_registered_explanation': '이 도구에 등록된 해설 없음',
    'error': '오류', 'warning': '경고', 'passed': '통과', 'unspecified': '미지정',
    'unlabelled': '구분 없음', 'info': '정보(Info)', 'resolution': '조치 안내(Resolution)',
    'not_available_in_supported_markdown_contract': '공식 Markdown에 버전 정보가 없음',
    'observed_server_metadata; no required product version': '서버 기록에서 관측한 값(특정 버전을 요구하지 않음)',
    'not_identified; not_required_for_file_analysis': '확인 불가(파일 분석에는 필요 없음)',
    'required_input_missing_or_unrecognized': '필요한 입력이 없거나 인식하지 못함',
    'explicit_offline_record': '오프라인 기록이 명시됨', 'all_provided_states_ok': '제공된 상태가 모두 ok',
    'provided_counter_bounds_only': '제공된 용량 카운터 범위만 확인', 'provided_version_string_comparison': '제공된 버전 문자열만 비교',
    'collection_or_explicit_as_of_basis': '수집 시각 또는 --as-of 기준', 'isolated_rule_error': '이 규칙만 평가 중 오류(다른 규칙은 계속)',
    'thp_config_missing': 'THP 설정 기록 없음', 'observed_value_differs_from_official_recommendation': '관측값이 공식 권장값과 다름',
    'all_observed_values_match_official_recommendation': '관측값이 모두 공식 권장값과 같음',
    'thp_values_missing_or_ambiguous': 'THP 값 일부가 없거나 모호함', 'explicit_flags_only': '명시된 옵션만 비교',
    'explicit_flags_unobserved_or_ambiguous': '부팅 옵션에서 iommu·intel_iommu·amd_iommu 값을 찾지 못했거나 서버 일부만 있거나 값이 충돌함(옵션 부재는 판정하지 않음)',
    'explicit_flags_absent_in_cmdline': '부팅 옵션(proc-cmdline)은 수집됐으나 iommu·intel_iommu·amd_iommu 옵션이 없음',
    'proc_cmdline_missing': '일부 또는 전체 서버에서 부팅 옵션 기록이 없음',
    'certificate_expiry_not_available': '공식 Markdown에 인증서 만료 정보 없음(공식 TLS 검사가 통과하면 상세가 실리지 않음)',
    'analysis_time_unavailable': '분석 기준 시각 없음',
    'recognized_error_fields_only': '인식된 오류 필드만 집계', 'lower_bound_error_rows_observed': '최소 오류 행 수만 확인',
    'error_coverage_unavailable': '오류 집계 범위를 확인할 수 없음',
}


def label(code):
    """Korean text for a report code, keeping the code in parentheses for traceability."""
    if code is None:
        return MISSING
    text = LABEL_KO.get(str(code)) or CODE_KO.get(str(code)) or RESULT_KO.get(str(code))
    return '%s (%s)' % (text, code) if text else str(code)


# 0.5.2(C2): HTML 카드·요약에서는 괄호 속 내부 코드를 떼고 한국어만 보인다(부록 표·필터·Markdown·JSON은 그대로).
CODE_SUFFIX = re.compile(r' \((?:[a-z][a-z0-9]*_[a-z0-9_]*|[a-z]+)\)')


def plain(text):
    return CODE_SUFFIX.sub('', str(text))


def priority_text(priority):
    return priority if priority in ('P1', 'P2', 'P3', 'P4') else label(priority)


def result_text(result):
    return RESULT_KO.get(str(result), str(result)) if result is not None else '미평가'


APPROVAL_KO = {True: '필요', False: '불필요', None: '미정'}


def status_text(finding):
    """Visible status. Custom rules have no official status; their own result is shown in Korean."""
    if finding.get('origin') == 'custom_rule':
        return '해당 없음 / 자체 결과: ' + result_text(finding.get('rule_result'))
    return label(finding.get('official_status'))


def assessment_text(finding):
    return label(finding.get('assessment')) + ' / ' + label(finding.get('assessment_reason'))


def now_utc():
    # 보고서 생성 시각. 테스트에서 고정할 수 있도록 함수로 둔다.
    return dt.datetime.now(dt.timezone.utc)


RESULT_KO = {'pass': '통과', 'warning': '경고', 'fail': '실패', 'not_evaluated': '미평가', 'info': '정보', 'assessable': '평가 가능',
             'unsupported': '해설 미등록', 'insufficient_data': '자료 부족', 'unclassified': '미분류'}


def render_time_remaining(expires_at):
    """Remaining time measured at report generation, so an already expired certificate is not read as valid."""
    expiry = aware_time(expires_at) if expires_at else None
    if expiry is None:
        return None
    now = now_utc()
    seconds = int((expiry - now).total_seconds())
    when = now.astimezone(KST).strftime('%Y-%m-%d %H:%M:%S')
    if seconds <= 0:
        return '이미 만료됨: 만료 후 %s 경과 (생성 시각: 한국 시각 %s)' % (duration_text(-seconds), when)
    return '%s (생성 시각: 한국 시각 %s)' % (duration_text(seconds), when)


def counts_rows(prefix, counts):
    return [(prefix, priority_text(k) if prefix == '우선순위' else RESULT_KO.get(str(k), str(k)), v)
            for k, v in sorted((counts or {}).items(), key=lambda kv: str(kv[0]))]


def info_blocks(report):
    """Human-readable tables for metadata, facts, summaries and limits (no raw structures)."""
    meta = report.get('input_metadata') or {}
    facts = report.get('facts') or {}
    blocks = [('h', 2, '확인된 정보')]
    rows = [('입력 형식', '공식 Markdown' if meta.get('format') == 'official_markdown' else meta.get('format') or '원본 gzip 단독'),
            ('공식 Markdown 수집 시각', with_kst(meta.get('collected_at'))),
            ('분석 기준 시각', with_kst(meta.get('as_of'))),
            ('기준 시각 출처', {'explicit': '--as-of 지정', 'collection': '수집 시각'}.get(meta.get('as_of_origin'), meta.get('as_of_origin'))),
            ('해석기 버전', meta.get('parser_version'))]
    health = meta.get('health')
    if isinstance(health, dict):
        rows += [('원본 gzip 수집 시각', with_kst(health.get('collected_at'))),
                 ('원본 gzip JSON 문서 수', health.get('documents')),
                 ('원본 형식 버전(실행을 막지 않음)', health.get('format_version'))]
    basis_name = '--as-of 지정 시각' if meta.get('as_of_origin') == 'explicit' else '수집 시각'
    rows += [('인증서 만료 시각(공식 Markdown 기준)', facts.get('certificate_expires_kst') or facts.get('certificate_expires_at')),
             ('인증서 남은 시간(%s 기준)' % basis_name, duration_text(facts.get('certificate_remaining_seconds'))),
             ('인증서 남은 시간(보고서 생성 시각 기준)', render_time_remaining(facts.get('certificate_expires_at'))),
             ('인증서 시각 미확정 사유', facts.get('certificate_time_reason'))]
    # 0.5.2(A3·D6): 사유가 없으면 행을 빼고(label(None)이 '미제공'을 돌려주므로 먼저 거름), 있으면 한국어로 보인다.
    rows = [(k, label(v) if k == '인증서 시각 미확정 사유' else v) for k, v in rows if not (k == '인증서 시각 미확정 사유' and v is None)]
    rows.append(('스키마·해설 정책 버전', '%s / %s' % (report.get('schema_version'), (report.get('policy') or {}).get('version'))))
    blocks.append(('table', ['항목', '값'], rows))
    link = report.get('input_link')
    if isinstance(link, dict):
        blocks += [('h', 2, '입력 결합 확인'),
                   ('table', ['항목', '값'], [
                       ('결합 상태', label(link.get('status'))),
                       ('서버 대상 일치', {True: '일치', False: '불일치 또는 확인 불가'}.get(link.get('targets_match'), MISSING) if 'targets_match' in link else '해당 없음'),
                       ('수집 시각 차이(초)', (round(link['time_delta_seconds'], 1) if isinstance(link.get('time_delta_seconds'), (int, float)) else None) if 'time_delta_seconds' in link else '해당 없음'),
                       ('허용 시각 차이(초)', link.get('time_tolerance_seconds') if 'time_tolerance_seconds' in link else '해당 없음'),
                       ('같은 수집 실행 증명', '아니오(증명하지 않음)'),
                       ('같은 배포 증명', '아니오(증명하지 않음)')])]
    summary = report.get('analysis_summary') or {}
    blocks.append(('h', 2, '이 도구의 해설 요약'))
    rows = []
    # 0.5.5: 자체 점검 결과·규칙표는 보고서에 싣지 않는다(JSON의 내부용 필드로만 남음).
    for key, heading in (('priority_counts', '우선순위'), ('category_counts', '영역'), ('assessment_counts', '평가 범위')):
        rows += counts_rows(heading, summary.get(key))
    blocks.append(('table', ['구분', '값', '개수'], rows) if rows else ('p', '집계할 항목이 없습니다.'))
    blocks += [('h', 2, '입력 버전'),
               ('table', ['구성요소', '값', '사유'],
                [(name, ', '.join(v['value']) if isinstance(v.get('value'), list) else v.get('value'), label(v.get('reason')))
                 for name, v in (report.get('source_versions') or {}).items() if isinstance(v, dict)])]
    blocks.append(('h', 2, '한계'))
    blocks.append(('table', ['범위', '상태'], [(COVERAGE_LABELS.get(k, k), label(v)) for k, v in (report.get('coverage') or {}).items()]))
    notes = report.get('parser_errors') or []
    blocks.append(('p', '해석·결합 주의 사항:'))
    blocks.append(('ul', [label(n) for n in notes] or ['없음']))
    completeness = report.get('completeness') or {}
    if 'requested_scope_satisfied' in completeness:
        blocks.append(('p', '요청 범위 충족: ' + ('예' if completeness['requested_scope_satisfied'] else '아니오(미평가·미제공 항목 있음)')))
    if completeness.get('limitations'):
        blocks.append(('p', completeness['limitations']))
    blocks.append(('p', (report.get('masking') or {}).get('limitations', '')))
    return blocks


def esc(x):
    return html.escape(str(x), quote=True)


def blocks_html(blocks):
    cell = lambda x: MISSING if x is None else esc(x)
    out = []
    for block in blocks:
        kind = block[0]
        if kind == 'h':
            out.append('<h%d>%s</h%d>' % (block[1], esc(block[2]), block[1]))
        elif kind == 'p':
            out.append('<p>%s</p>' % esc(block[1]))
        elif kind == 'kv':
            out.append('<p>%s<strong>%s</strong></p>' % (esc(block[1]), cell(block[2])))
        elif kind == 'ul':
            out.append('<ul>' + ''.join('<li>%s</li>' % esc(x) for x in block[1]) + '</ul>')
        elif kind == 'table':
            head = '<tr>' + ''.join('<th>' + esc(x) + '</th>' for x in block[1]) + '</tr>'
            body = ''.join('<tr>' + ''.join('<td>' + cell(x) + '</td>' for x in row) + '</tr>' for row in block[2])
            out.append('<div class="table-scroll"><table>' + head + body + '</table></div>')
        elif kind == 'details':
            out.append('<details><summary>%s</summary>%s</details>' % (esc(block[1]), blocks_html(block[2])))
    return ''.join(out)


MD_PUNCT = frozenset('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')
MD_ENTITY = re.compile(r'&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});')


def md_line(line, cell=False, line_start=True):
    """Escape one line by CommonMark/GFM rules: inert text without HTML entities.

    Code spans (matched backtick runs) are kept verbatim because renderers never interpret
    their content. Outside them only characters that could start HTML, entities, links,
    emphasis or block structure are backslash-escaped, so plain readers (less, notepad)
    still see the original text.
    """
    out, i, n = [], 0, len(line)
    tildes = line.count('~') >= 2
    if line_start:
        line = line.lstrip(' \t')
        n = len(line)
        ordered = re.match(r'(\d{1,9})([.)])', line)
        if ordered:
            out.append(ordered[1] + '\\' + ordered[2])
            i = ordered.end()
        elif line[:1] in ('#', '>', '+', '-', '='):
            out.append('\\' + line[0])
            i = 1
    while i < n:
        ch = line[i]
        if ch == '`':
            j = i
            while j < n and line[j] == '`':
                j += 1
            run, k, close = j - i, j, -1
            while k < n:
                if line[k] != '`':
                    k += 1
                    continue
                m = k
                while m < n and line[m] == '`':
                    m += 1
                if m - k == run:
                    close = k
                    break
                k = m
            if close >= 0:
                span = line[i:close + run]
                out.append(span.replace('|', '\\|') if cell else span)
                i = close + run
            else:
                out.append('\\`' * run)
                i = j
            continue
        nxt = line[i + 1] if i + 1 < n else ''
        prev = line[i - 1] if i else ''
        if ch == '\\':
            out.append('\\\\' if not nxt or nxt in MD_PUNCT else '\\')
        elif ch == '<':
            out.append('\\<' if nxt and (nxt in '/!?' or (nxt.isascii() and nxt.isalpha())) else '<')
        elif ch == '&':
            out.append('\\&' if MD_ENTITY.match(line, i) else '&')
        elif ch == ']':
            out.append('\\]' if nxt and nxt in '([:' else ']')
        elif ch == '[':
            out.append('\\[' if nxt == '^' else '[')
        elif ch == '*':
            out.append('\\*')
        elif ch == '_':
            out.append('_' if prev.isalnum() and nxt.isalnum() else '\\_')
        elif ch == '~' and tildes:
            out.append('\\~')
        elif ch == '|' and cell:
            out.append('\\|')
        elif ch in 'wW' and line[i:i + 4].lower() == 'www.' and not (prev and (prev.isalnum() or prev in '/.:@-_')):
            # 0.5.6: GFM 확장 자동 링크(www.)가 생기지 않도록 점을 이스케이프한다. 표시 글자는 같다.
            out.append(line[i:i + 3] + '\\.')
            i += 4
            continue
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def md_text(value, *, cell=False, line_start=True):
    """Markdown-escape arbitrary text. Lines are escaped independently (code spans never cross lines)."""
    lines = str(value).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    # 0.4.4: 표 칸 안은 줄머리 블록 구조(목록·제목·인용)가 생기지 않으므로 줄머리 이스케이프를 하지 않는다
    # (예: '\--as-of 지정'). 인라인 이스케이프(HTML·링크·강조·|)는 그대로 적용한다.
    rendered = [md_line(line, cell, (line_start or index > 0) and not cell) for index, line in enumerate(lines)]
    return ' '.join(rendered) if cell else '\n'.join(rendered)


def md_inline(value):
    """Escaped text that follows a label on the same line."""
    return md_text(value, line_start=False)


def md_code_block(text):
    """Fenced block whose fence is longer than any backtick run inside, so content cannot close it."""
    longest = max((len(run) for run in re.findall(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    return fence + '\n' + text + '\n' + fence


def blocks_markdown(blocks):
    cell = lambda x: MISSING if x is None else md_text(x, cell=True)
    out = []
    for block in blocks:
        kind = block[0]
        if kind == 'h':
            out += ['#' * block[1] + ' ' + md_inline(block[2]), '']
        elif kind == 'p':
            out += [md_text(block[1]).replace('\n', '\n\n'), '']
        elif kind == 'kv':
            out += [md_text(block[1]) + '**' + cell(block[2]) + '**', '']
        elif kind == 'ul':
            out += ['- ' + cell(x) for x in block[1]] + ['']
        elif kind == 'table':
            out.append('| ' + ' | '.join(cell(x) for x in block[1]) + ' |')
            out.append('| ' + ' | '.join('---' for _ in block[1]) + ' |')
            out += ['| ' + ' | '.join(cell(x) for x in row) + ' |' for row in block[2]]
            out.append('')
        elif kind == 'details':
            out += ['#### ' + md_inline(block[1]), ''] + blocks_markdown(block[2]).split('\n')
    return '\n'.join(out)


def cells(line):
    return [x.strip().replace('\\|', '|') for x in re.split(r'(?<!\\)\|', line.strip().strip('|'))]


LIST_ITEM = re.compile(r'^\s*(?:[-*+] |\d+[.)] )')


def closes_fence(fence, line):
    """A closing fence uses the opening character at least as many times."""
    return re.fullmatch(r'\s*' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*', line)


def evidence_blocks(lines, start_line):
    """Normalize the supported Markdown blocks without interpreting their content."""
    blocks, section, position = [], 'unlabelled', 0
    while position < len(lines):
        line = lines[position]
        marker = re.fullmatch(r'\s*(?:\*\*)?(Info|Resolution):?(?:\*\*)?:?\s*', line, re.I)
        if marker:
            section = marker[1].lower()
            position += 1
            continue
        if not line.strip():
            position += 1
            continue
        begin = position
        fence = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if fence:
            position += 1
            content = []
            while position < len(lines) and not closes_fence(fence[1], lines[position]):
                content.append(lines[position])
                position += 1
            closed = position < len(lines)
            position += int(closed)
            block = {'type': 'code', 'language': fence[2].strip(), 'text': '\n'.join(content), 'closed': closed}
        elif line.strip().startswith('|'):
            table = []
            while position < len(lines) and lines[position].strip().startswith('|'):
                table.append(cells(lines[position]))
                position += 1
            has_header = len(table) > 1 and all(re.fullmatch(r':?-+:?', cell) for cell in table[1])
            block = {'type': 'table', 'headers': table[0] if has_header else [],
                     'rows': table[2:] if has_header else table}
        else:
            kind = 'list' if LIST_ITEM.match(line) else 'text'
            content = [line]
            position += 1
            while position < len(lines) and lines[position].strip():
                next_line = lines[position]
                if re.match(r'^\s*(?:`{3,}|~{3,}|\||(?:\*\*)?(?:Info|Resolution):)', next_line, re.I):
                    break
                next_kind = 'list' if LIST_ITEM.match(next_line) else 'text'
                if next_kind != kind:
                    break
                content.append(next_line)
                position += 1
            block = {'type': kind, 'text': '\n'.join(content)}
        blocks.append(dict(block, section=section, source_locator={
            'input': 'input-001', 'line': start_line + begin, 'end_line': start_line + position - 1}))
        if over_limit(len(blocks), MAX_BLOCKS):
            raise InputError('block limit exceeded')
    return blocks


def parse_report(text):
    deadline = deadline_after(time.monotonic(), MAX_PROCESS_SECONDS)
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    raw_lines = text.splitlines()
    if over_limit(len(text.encode('utf-8')), MAX_BYTES) or over_limit(len(raw_lines), MAX_LINES):
        raise InputError('input limit exceeded')
    visible, fenced = [], False
    for n, line in enumerate(raw_lines, 1):
        check_deadline(deadline)
        opener = re.match(r'^\s*(`{3,}|~{3,})', line)
        if opener:
            if not fenced:
                fence = opener[1]
                fenced = True
            elif closes_fence(fence, line):
                fenced = False
            continue
        if not fenced:
            visible.append((n, line))
    summary = None
    collected = None
    checks, notes = [], []
    rows, headers = [], None
    details, current = [], None
    section = ''
    for n, line in visible:
        check_deadline(deadline)
        if re.match(r'^##\s+', line):
            section = line[3:].strip().lower()
            headers = None
            current = None
        if section == 'summary':
            # 0.4.4: mc는 개수가 1이면 단수형(1 error, 1 warning)으로 쓴다.
            match = re.search(r'Results:\s*(\d+)\s+passed,\s*(\d+)\s+errors?,\s*(\d+)\s+warnings?\b', line, re.I)
            if match:
                if summary is not None:
                    notes.append('duplicate summary')
                summary = dict(zip(('passed', 'errors', 'warnings'), map(int, match.groups())))
            if 'Date:' in line:
                raw = line.split('Date:', 1)[1].strip()
                try:
                    collected = dt.datetime.strptime(raw, '%a, %d %b %Y %H:%M:%S UTC').replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    collected = aware_time(raw)
        if section in ('errors', 'checks', 'failed checks summary') and line.strip().startswith('|'):
            row = cells(line)
            if 'Check' in row and 'Status' in row:
                headers = row
            elif headers and len(row) == len(headers) and not all(re.fullmatch(r':?-+:?', x) for x in row):
                item = dict(zip(headers, row))
                if item.get('Check'):
                    rows.append((n, item))
        if section == 'failed checks':
            match = re.match(r'^###\s+(\d+)\.\s+(.+?)\s+\(([^)]+)\)\s*$', line)
            if match:
                current = {'index': match[1], 'name': match[2], 'status': match[3], 'lines': [], 'line': n}
                details.append(current)
            elif current:
                current['lines'].append(line)
    if summary is None:
        raise InputError('recognized summary is required')
    if len(rows) > MAX_CHECKS or sum(summary.values()) > 100000:
        raise InputError('check limit exceeded')
    if (summary['errors'] or summary['warnings']) and not rows:
        raise InputError('required check table missing')
    # 상세는 다음 상세 또는 다음 ## 구간 앞에서 끝난다. 구간 줄 목록은 한 번만 만든다(상세마다 전체를 다시 훑지 않음).
    section_lines = [n for n, line in visible if re.match(r'^##\s+', line)]
    for i, detail in enumerate(details):
        check_deadline(deadline)
        end = details[i + 1]['line'] - 1 if i + 1 < len(details) else len(raw_lines)
        later = bisect.bisect_right(section_lines, detail['line'])
        if later < len(section_lines):
            end = min(end, section_lines[later] - 1)
        detail['lines'] = raw_lines[detail['line']:end]
    if len(details) > MAX_CHECKS:
        raise InputError('detail limit exceeded')
    used = set()
    for n, row in rows:
        check_deadline(deadline)
        matches = [i for i, d in enumerate(details) if d['index'] == row.get('Index') and d['name'] == row.get('Name')]
        detail = details[matches[0]] if len(matches) == 1 else None
        if detail:
            used.add(matches[0])
            if detail['status'] != row['Status']:
                notes.append('check status conflict')
        else:
            notes.append('check detail not uniquely linked')
        checks.append({'source_check_id': row['Check'], 'official_status': row['Status'], 'title': row.get('Name', row['Check']),
                       'observed': row.get('Description', ''), 'detail': '\n'.join(detail['lines']) if detail else '',
                       'evidence_blocks': evidence_blocks(detail['lines'], detail['line'] + 1) if detail else [],
                       'source_locator': {'input': 'input-001', 'line': n}})
    for i, detail in enumerate(details):
        check_deadline(deadline)
        if i not in used:
            notes.append('unmatched detail preserved')
            checks.append({'source_check_id': None, 'official_status': detail['status'], 'title': detail['name'],
                           'observed': '', 'detail': '\n'.join(detail['lines']),
                           'evidence_blocks': evidence_blocks(detail['lines'], detail['line'] + 1),
                           'source_locator': {'input': 'input-001', 'line': detail['line']}})
    ids = [c['source_check_id'] for c in checks if c['source_check_id']]
    if len(ids) != len(set(ids)):
        notes.append('duplicate check ID')
    if sum(c['official_status'] == 'error' for c in checks) != summary['errors'] or sum(c['official_status'] == 'warning' for c in checks) != summary['warnings']:
        notes.append('summary/detail mismatch')
    if fenced:
        notes.append('unclosed fence')
    if any(c['official_status'] not in ('passed', 'error', 'warning') for c in checks):
        notes.append('unrecognized official status')
    if over_limit(sum(len(c['evidence_blocks']) for c in checks), MAX_BLOCKS):
        raise InputError('block limit exceeded')
    check_deadline(deadline)
    return summary, collected, checks, sorted(set(notes))


# --- 마스킹(0.5.6) --------------------------------------------------------------
# 0.5.6 보안 점검(F1·F2·F3) 반영:
# - 모든 패턴은 토큰 시작에서만 시작하고(왼쪽 경계) 반복 횟수에 상한을 둬 입력 길이에 선형으로 동작한다(ReDoS 방지).
# - strict/basic 공통: 지원 목록 밖 도메인(.kr·.io 등 모든 2글자 국가 도메인과 주요 일반 도메인), JSON 형식 키,
#   자격증명·라이선스 키, JWT, 인증서 주체(CN·O·OU·DNS), 이메일, MAC, UUID, 인증서 PEM 블록을 가린다.
# - strict 전용: 포트 없는 짧은 호스트명(server·node·host 등 바로 뒤 이름), 이름 필드 값.
# - 마스킹 뒤 출력 전체를 같은 탐지 규칙으로 다시 검사해 하나라도 남으면 보고서를 만들지 않는다(fail-closed).
MASK_GTLDS = frozenset(
    'com net org edu gov mil int arpa info biz name pro io ai app dev cloud tech online site xyz me tv cc co '
    'local localdomain localhost internal intranet lan corp home svc cluster test example invalid onion '
    'asia mobi jobs aero coop museum travel cat tel post global group company systems network services solutions '
    'center digital world today live store shop page blog wiki email host hosting server'.split())
# 2글자 확장자 중 파일 이름으로 흔한 것(국가 도메인과 겹침)은 호스트로 보지 않는다.
MASK_FILE_EXT = frozenset('md sh py pl rs js ts gz xz bz so ko cs hs rb ps'.split())
MASK_PUBLIC_HOSTS = frozenset('docs.min.io min.io www.kernel.org kernel.org docs.kernel.org tip.golang.org go.dev golang.org'.split())
MASK_SECRET_WORDS = ('secretkey', 'accesskey', 'apikey', 'password', 'passwd', 'passphrase', 'token', 'authorization',
                     'credential', 'rootuser', 'license', 'secret', 'privatekey', 'sessionkey', 'signature', 'cookie')
MASK_HOST_STOPWORDS = frozenset(
    'a an the and or of to in on for with by from at as per via into out up down off over under not no is are was were be been '
    'being has have had do does did can cannot could will would should may might must shall this that these those it its each '
    'all any some every both either neither other same such which who whom whose what when where why how than then there '
    'here only also just more most less least many much few one two three four five six seven eight nine ten count counts '
    'list name names pool pools set sets side status state info information error errors warning warnings failed failure '
    'offline online unreachable reachable available unavailable healthy unhealthy ok time timeout version versions config '
    'configuration process processes memory cpu cpus drive drives disk disks number numbers id ids type types level levels '
    'address addresses port ports total value values check checks result results details detail table data group groups '
    'role roles mode modes size sizes uptime local remote pod pods network networks storage capacity health '
    'recommended required minimum maximum min max default current expected actual configured '
    # 0.5.7(N2): 'Hostname resolution failed' 같은 문장의 일반 낱말
    'resolution lookup resolve resolving verification validation mismatch unknown missing'.split())
# 0.5.7(N2): 라이선스 값 중 플랜·상태 이름은 비밀이 아니므로 그대로 둔다(키·서명 값은 계속 가림).
MASK_LICENSE_PLANS = frozenset('enterprise enterprise-lite enterprise-plus standard free trial community agpl agplv3 '
                               'none expired valid invalid unlicensed'.split())
# 0.5.7(N3): 짧은 비밀번호 키는 부분 일치(bypass 등)를 피하려고 정확히 일치할 때만 본다.
MASK_SECRET_EXACT = frozenset('pass pwd pw dbpass dbpwd userpass'.split())
# 0.5.9(R3): 'pass: 12 checks'처럼 숫자 뒤에 오면 집계로 보는 낱말.
MASK_PASS_COUNT_WORDS = frozenset('check checks test tests item items case cases drive drives disk disks node nodes '
                                  'server servers object objects bucket buckets pool pools set sets file files of out total'.split())
# 0.5.9(R2): 공식 문서의 mc alias 예시 자리표시자(ACCESS_KEY, <SECRET_KEY>, $MINIO_ROOT_USER)는 원문으로 둔다.
# 0.5.10(S1): 자리표시자를 좁힌다. '$이름'·'${이름}'은 대문자 이름이고 작은따옴표 밖일 때만, '<…>'은 대문자나
# 소문자 한 가지로만 된 이름일 때만, 맨 낱말은 문서에 나오는 목록일 때만 원문으로 둔다('XX_YY_KEY'·'<Abc>'는 가림).
MASK_MC_PLACEHOLDER = re.compile(r'"?(?:\$[A-Z_][A-Z0-9_]{0,63}|\$\{[A-Z_][A-Z0-9_]{0,63}\})"?'
                                 r'''|["']?<(?:[A-Z][A-Z0-9_-]{0,63}|[a-z][a-z0-9_-]{0,63})>["']?'''
                                 r'''|["']?(?:ACCESS_KEY|SECRET_KEY|ACCESS_KEY_ID|SECRET_ACCESS_KEY|ACCESSKEY|SECRETKEY|ACCESS|SECRET'''
                                 r'''|USERNAME|PASSWORD|USER|MINIO_ROOT_USER|MINIO_ROOT_PASSWORD|ROOT_USER|ROOT_PASSWORD)["']?''')
# 0.5.10(S1): mc 명령에서 다음 낱말을 값으로 받는 옵션. 별칭·URL·키 자리를 셀 때 값까지 건너뛴다.
MASK_MC_VALUE_FLAGS = frozenset('--config-dir -c --api --path --lookup --limit-upload --limit-download --custom-header -h '
                                '--resolve --enc-c --enc-kms --enc-s3 --conn-read-deadline --conn-write-deadline '
                                '--policy --access-key --secret-key --name --description --expiry'.split())


class Masker:
    """Best-effort redaction, followed by leak checks. No mapping file is emitted."""
    # 키=값 형태의 자격증명. 키는 토큰 시작에서만 잡고(따옴표로 감싼 JSON 키 포함) 값 길이에 상한을 둔다.
    # 0.5.7(N3): CLI 플래그(--secret-key X, -p=X)도 키로 본다. 'foo-secret'처럼 낱말 중간의 '-'는 키 시작이 아니다.
    # 'access key: X'처럼 공백 낀 키와 'accessKey => X' 구분자도 받는다.
    # 0.5.8(M1): 키+구분자와 값을 나눠 찾는다. 자격증명이 아닌 '단어 단어' 쌍이 값 자리의 진짜 키(env KEY=X)를
    # 집어삼키지 않도록, 키가 아니면 값을 소비하지 않고 다음 낱말부터 다시 찾는다(secret_spans).
    # 마스킹과 사후 검증이 같은 반복을 쓴다.
    secret_head = re.compile(r'''(?<![A-Za-z0-9_.])(?<![A-Za-z0-9_.]-)(["']?)([A-Za-z][A-Za-z0-9_.-]{0,63}(?:[ \t](?:key|token|secret|password)(?![A-Za-z0-9_]))?)\1'''
                             r'''(\s*(?:=>|[:=])\s*|[ \t]+)''')
    # 0.5.9(R3): 닫는 따옴표가 없는 값("password": "값)도 여는 따옴표부터 공백 전까지 값으로 본다.
    secret_value = re.compile(r'''"(?:\\.|[^"\\\n]){0,4096}"|'(?:\\.|[^'\\\n]){0,4096}'|[^\s|,;"']{1,4096}'''
                              r'''|"[^"\n|]{1,4096}|'[^'\n|]{1,4096}''')
    # 0.5.10(S2): 닫는 따옴표가 없으면 공백을 넘어 줄 끝(표 칸 끝)까지 값으로 본다('"password": "Qz Pw34x').
    # 0.5.9(R2): 'mc alias set <별칭> <URL> <접근 키> <비밀 키>'와 'mc config host add …'의 위치 인자 키.
    # 0.5.10(S1): 정규식 하나로 묶지 않고 명령 이름부터 낱말 단위로 읽는다(mc_key_spans). 'mcli'·경로·'.exe',
    # 값을 받는 옵션('--api S3v4', '-C /x'), 줄 끝 '\' 이어짐, 'mc admin user add'를 함께 다룬다.
    # 낱말 8개(옵션과 그 값) 안에 하위 명령(alias set|add, config host add, admin user add)이 있을 때만 후보로 본다('mc ls' 등은
    # 정규식에서 바로 거른다). 낱말과 구분자는 겹치는 글자가 없어 미리보기 한 번이 최대 8낱말로 끝난다.
    mc_cmd = re.compile(r'(?i)(?<![\w.-])mc(?:li)?(?:\.exe)?(?=(?:(?:[ \t]|\\\r?\n)+(?:[^\s`\\]|\\(?!\r?\n))+){0,8}?'
                        r'(?:[ \t]|\\\r?\n)+(?:alias(?:[ \t]|\\\r?\n)+(?:set|add)|config(?:[ \t]|\\\r?\n)+host(?:[ \t]|\\\r?\n)+add'
                        r'|admin(?:[ \t]|\\\r?\n)+user(?:[ \t]|\\\r?\n)+add)(?=[ \t]|\\\r?\n))')
    mc_tok = re.compile(r'''"[^"\n]{0,4096}"|'[^'\n]{0,4096}'|(?:[^\s`\\]|\\(?!\r?\n)){1,4096}''')
    # 낱말 구분은 공백·탭·줄 끝 '\' 이어짐. 낱말 n개까지를 정규식 한 번으로 잡는다(클래스 본문 내포식이라 pattern을 바깥 반복으로 넘긴다).
    mc_run = {n: re.compile(r'(?:(?:[ \t]|\\\r?\n)+(?:%s)){0,%d}' % (p, n)) for p in (mc_tok.pattern,) for n in range(1, 41)}
    mc_anchor = re.compile(r'[A-Za-z][A-Za-z0-9+.-]{0,20}://\S*|identifier-\d{3,}')
    pass_follow = re.compile(r'[ \t]+([A-Za-z]+)')
    pem_begin = re.compile(r'-----BEGIN ([A-Z0-9 ]{1,40})-----')
    pem_end = re.compile(r'-----END [A-Z0-9 ]{1,40}-----')
    bearer = re.compile(r'(?i)(?<![A-Za-z0-9_])Bearer[ \t]+[^\s"\'<>|,;]{1,4096}')
    jwt = re.compile(r'(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,8192}\.[A-Za-z0-9_-]{4,8192}\.[A-Za-z0-9_-]{0,8192}')
    encoded = re.compile(r'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,8192}={0,2}(?![A-Za-z0-9+/=])')
    email = re.compile(r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,126}')
    # 0.5.7(N2): 값에 괄호를 넣지 않는다('(CN=minio)'의 닫는 괄호 보존).
    subject = re.compile(r'(?<![A-Za-z0-9_])((?:CN|OU|O|emailAddress)[ \t]*=[ \t]*|(?:CN|DNS|URI)[ \t]*:[ \t]*)([^,/\n\r"\'|<>+;()]{1,253})')
    # 0.5.9(R3): 뒤에 '-1'처럼 짧은 꼬리(인터페이스 번호 등)가 붙어도 MAC으로 본다.
    # 0.5.10(S2): '-10'·'-ab'처럼 두 자리 꼬리도 MAC으로 본다. 2자리 16진수 묶음이 두 개 이상 더 이어질 때(EUI-64 등)만 MAC이 아니다.
    mac = re.compile(r'(?<![0-9A-Fa-f:.-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}'
                     r'(?![0-9A-Fa-f:.])(?!-[0-9A-Fa-f]{2}-[0-9A-Fa-f]{2}(?![0-9A-Za-z]))')
    # 0.5.7(N3): 점 표기 MAC(Cisco 형식 5254.00ab.cd24), AWS 형식 액세스 키, URL 사용자 정보(scheme://user:pass@).
    mac_dot = re.compile(r'(?<![0-9A-Za-z.])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Za-z.])')
    aws_key = re.compile(r'(?<![A-Za-z0-9])(?:AKIA|ASIA|AIDA|AROA|AGPA|ANPA|ANVA|AIPA)[A-Z0-9]{12,20}(?![A-Za-z0-9])')
    userinfo = re.compile(r'(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{1,20}://)([^\s/@:\[\]]{1,256})(?::([^\s/@]{1,256}))?@')
    uuid_re = re.compile(r'(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f-])')
    host = re.compile(r'(?<![A-Za-z0-9_.-])[A-Za-z0-9][A-Za-z0-9_-]{0,62}(?:\.[A-Za-z0-9_-]{1,63}){1,126}')
    identifiers = re.compile(r'(?P<url>https?://[^\s<>\]\)]{1,8192})'
                             r'|(?P<ip4>(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![0-9]))'
                             # 0.5.7(N1): 'IP Address:10.44.33.22'의 'Address:10'을 호스트:포트로 잡지 않는다(뒤가 '.숫자'면 IPv4).
                             r'|(?P<hp>(?<![\w.:/-])[A-Za-z][\w-]{0,62}(?:\.[\w-]{1,63}){0,126}:\d{2,5}(?!\d)(?!\.\d))'
                             r'|(?P<path>(?<![\w.@/\\-])(?:[A-Za-z]:\\|/)(?:[\w.@-]{1,255}[/\\]){1,64}[\w.@-]{1,255})', re.I)
    short_host = re.compile(r'(?i)(?<![A-Za-z0-9_-])(servers?|hosts?|hostname|nodes?|peers?|endpoints?|instances?|machine)'
                            r'([ \t]*[:=][ \t]*|[ \t]+)(["\']?)([A-Za-z][A-Za-z0-9-]{0,62})(?![A-Za-z0-9_.@-])')
    name_field = re.compile(r'(?i)(?<![A-Za-z0-9_])((?:bucket|object|policy|cluster|deployment)(?:[_ -]?name)?[ \t]*[:=][ \t]*)([^\s|,;]{1,4096})')
    ipv4_leak = re.compile(r'(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])')
    token = re.compile(r'[A-Za-z0-9_.-]{1,512}')
    # 0.4.1: 고객 조치 안내에 필요한 표준 커널·부팅 설정 경로와 공개 문서 URL만 원문으로 둔다.
    # 호스트명·IP·장치 식별자가 들어갈 수 있는 경로(/dev, /sys/class/net, /sys/block 등)는 계속 가린다.
    public_path = re.compile(r'/proc/(?:cmdline|meminfo|cpuinfo|sys/(?:kernel|vm|fs)(?:/[a-z0-9_]+)+)'
                             r'|/sys/kernel/mm/transparent_hugepage(?:/[a-z_]+)*'
                             r'|/etc/default/grub|/etc/kernel/cmdline|/etc/sysctl\.conf|/etc/security/limits\.conf'
                             r'|/boot/grub2/grub\.cfg|/boot/grub2/grubenv')
    public_url = re.compile(r'https://(?:docs\.min\.io|min\.io|www\.kernel\.org|kernel\.org|docs\.kernel\.org|tip\.golang\.org|go\.dev|golang\.org)'
                            r'(?:/[A-Za-z0-9._~%+-]+)*/?(?:\?highlight=[A-Za-z0-9_+-]{1,40})?(?:#[A-Za-z0-9._-]{1,80})?')
    trailing = '.,;:`\'"'
    placeholder = re.compile(r'identifier-\d{3,}|\[(?:REDACTED|MASKED|JWT REMOVED|ENCODED VALUE REMOVED|PEM BLOCK REMOVED|PRIVATE KEY REMOVED)\]')

    # 0.5.8(M2): 'IP Address:fd00:abcd::1234'처럼 'IP:'·'Address:' 같은 이름표 콜론 바로 뒤 IPv6도 잡는다.
    # 0.5.9(R1): 이름표 끝 글자와 무관하게(src:·id:·ipv6:·peer_id:) 'X:' 바로 뒤에서도 시작한다('::' 뒤는 제외).
    # 주소 전체가 이미 일치한 자리는 sub가 건너뛰므로 주소 중간에서 다시 시작하지 않는다. 일치해도 ipaddress로
    # IPv6가 아니면 그대로 둔다(시각 12:30:45 등). 후보 하나는 45자 안으로 묶여 선형이다.
    # 0.5.10: 이름표('src:', '0:')를 일치 안에 넣고 낱말 경계에서만 시작한다. 콜론 뒤 모든 자리에서 다시 시작하던
    # 0.5.9 방식보다 빠르다('a:a:a:…' 1MB 최악 입력). 어디까지가 이름표인지는 ipv6_part가 정한다.
    ipv6 = re.compile(r'(?<![\w:])(?:\w{1,64}:)?(?:[0-9a-f]{0,4}:){2,8}[0-9a-f:]{1,4}(?:%[\w-]{1,32})?(?![\w:])', re.I)

    def __init__(self, level, public_check_ids=(), *, public_tokens=(), deadline=None):
        self.level = level
        self.aliases = {}
        self.public_check_ids = frozenset(public_check_ids)
        # 본문에 그대로 둘 공개 토큰(공식 검사 ID 등). 호스트 판정에서만 쓴다.
        self.public_tokens = frozenset(t.lower() for t in tuple(public_check_ids) + tuple(public_tokens))
        self.deadline = deadline
        self._ticks = 0

    def tick(self):
        if self.deadline is not None:
            self._ticks += 1
            if self._ticks % 256 == 0:
                check_deadline(self.deadline)

    # -- 판정 함수: 마스킹과 사후 검증이 같은 기준을 쓴다 ----------------------------
    def host_sensitive(self, value):
        host = value.rstrip('.').lower()
        if host in self.public_tokens or host in MASK_PUBLIC_HOSTS:
            return False
        labels = host.split('.')
        tld = labels[-1]
        if not tld.isascii() or not tld.isalpha() or tld in MASK_FILE_EXT:
            return False
        if '_' in host:
            # 0.5.7(N3): 밑줄 든 이름(qz_node14.customer.co.kr)은 라벨 3개 이상인 실제 도메인 형태만 호스트로 본다
            # (snake_case 필드 이름 오탐 방지). 밑줄은 TLD·도메인 라벨에는 올 수 없다.
            return len(labels) >= 3 and '_' not in labels[-2] + tld and (len(tld) == 2 or tld in MASK_GTLDS)
        if len(tld) == 2 and len(labels) == 2 and labels[0] in MASK_HOST_STOPWORDS:
            return False  # 0.5.7(N2): drive.id·status.ok·pool.id 같은 필드 이름
        return len(tld) == 2 or tld in MASK_GTLDS

    @staticmethod
    @functools.lru_cache(maxsize=4096)
    def secret_word(key):
        """키 이름에 자격증명 낱말이 있는지(구분자와 무관한 부분). 0.5.8: 후보마다 부르므로 캐시한다."""
        norm = re.sub(r'[^a-z0-9]', '', key.lower())
        return any(word in norm for word in MASK_SECRET_WORDS) or norm in MASK_SECRET_EXACT or norm.endswith('pwd')

    @staticmethod
    def secret_key(key, separator, flag=False):
        if not Masker.secret_word(key):
            return False
        if separator.strip() or flag:
            return True  # 0.5.7: CLI 플래그(--secret-key X)는 공백 구분도 값으로 본다.
        # 공백 구분(KEY value)은 환경변수 형태(대문자·밑줄) 키만 자격증명으로 본다.
        return bool(re.fullmatch(r'[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+', key))

    def secret_match(self, text, start, quote, key, separator, value, value_end):
        """키=값이 가려야 할 자격증명인지. 마스킹과 사후 검증이 같이 쓴다."""
        flag = not quote and start > 0 and text[start - 1] == '-'
        if not self.secret_key(key, separator, flag) or self.is_placeholder(value):
            return False
        norm = re.sub(r'[^a-z0-9]', '', key.lower())
        # 0.5.7(N2): 'license: Enterprise' 같은 플랜·상태 이름은 남긴다.
        if 'license' in norm and value.strip('"\'').lower() in MASK_LICENSE_PLANS:
            return False
        # 0.5.8(M3): 'pass: 12 checks'처럼 숫자 뒤에 낱말이 이어지는 집계 문장은 비밀번호로 보지 않는다.
        # 0.5.9(R3): 뒤 낱말이 집계 명사일 때만 예외로 한다('pass: 1234 ok'·'PASS: 9999 chars'는 계속 가림).
        if norm == 'pass' and ':' in separator and value.isdigit():
            follow = self.pass_follow.match(text, value_end, value_end + 24)
            if follow and follow.group(1).lower() in MASK_PASS_COUNT_WORDS:
                return False
        return True

    def secret_spans(self, text):
        """가려야 할 (키 시작, 값 끝, 따옴표, 키, 구분자) 목록.

        키가 아닌 후보('auth error', 'env MINIO_...')는 값을 소비하지 않고 다음 낱말부터 다시 본다.
        후보 하나의 검사 길이는 키 64자+구분자로 묶이고, 값은 자격증명 키일 때만 읽으므로 입력에 선형이다.
        """
        pos, spans = 0, []
        while True:
            head = self.secret_head.search(text, pos)
            if not head:
                return spans
            self.tick()
            quote, key, separator = head.groups()
            value = self.secret_word(key) and self.secret_value.match(text, head.end())
            if value and self.secret_match(text, head.start(), quote, key, separator, value.group(0), value.end()):
                spans.append((head.start(), value.end(), quote, key, separator))
                pos = value.end()
            else:
                pos = head.start() + 1  # lookbehind가 낱말 중간 시작을 막으므로 다음 후보는 다음 낱말이다.

    def is_placeholder(self, value):
        return bool(self.placeholder.fullmatch(value.strip('"\'').rstrip(self.trailing)))

    def is_public(self, value):
        core = value.rstrip(self.trailing)
        if self.public_path.fullmatch(core):
            return True
        if self.public_url.fullmatch(core):
            rest = core.split('/', 3)[3] if core.count('/') >= 3 else ''
            # 경로·조각 안에 주소·도메인 형태가 끼어 있으면 공개 URL로 보지 않는다.
            return not (self.ipv4_leak.search(rest) or '@' in rest
                        or any(self.host_sensitive(m.group(0)) for m in self.host.finditer(rest)))
        return False

    def alias_value(self, value):
        self.tick()
        if self.is_public(value) or self.is_placeholder(value):
            return value
        if value not in self.aliases:
            self.aliases[value] = 'identifier-%03d' % (len(self.aliases) + 1)
        return self.aliases[value]

    def alias(self, match):
        return self.alias_value(match.group(0))

    # -- 단계별 치환 ------------------------------------------------------------
    def strip_pem(self, text):
        if '-----' not in text:
            return text
        out, pos = [], 0
        while True:
            begin = self.pem_begin.search(text, pos)
            if not begin:
                break
            out.append(text[pos:begin.start()])
            end = self.pem_end.search(text, begin.end())
            out.append('[PRIVATE KEY REMOVED]' if 'PRIVATE KEY' in begin.group(1) else '[PEM BLOCK REMOVED]')
            if not end:
                pos = len(text)  # 닫히지 않은 블록은 끝까지 버린다(fail-closed).
                break
            pos = end.end()
        out.append(text[pos:])
        return self.pem_end.sub('[PEM BLOCK REMOVED]', ''.join(out))

    def mc_key_hidden(self, value):
        if self.is_placeholder(value):
            return True
        if value.startswith("'") and '$' in value:
            return False  # 작은따옴표 안의 '$'는 셸이 풀지 않으므로 실제 값이다.
        return bool(MASK_MC_PLACEHOLDER.fullmatch(value))

    def mc_tokens(self, text, pos, limit=24):
        """pos 뒤를 공백·줄 끝 '\' 이어짐으로 나눈 낱말 (시작, 끝, 값) 목록. 줄바꿈·백틱에서 멈춘다."""
        run = self.mc_run[limit].match(text, pos)  # 공백으로 이어진 낱말 limit개까지를 한 번에 잡는다.
        # 이스케이프된 줄바꿈 표기('\', 글자 그대로의 '\n')는 낱말로 세지 않는다.
        return [(tok.start(), tok.end(), tok.group(0)) for tok in self.mc_tok.finditer(text, pos, run.end())
                if tok.group(0) not in ('\\', '\\n', '\\r\\n')]

    @staticmethod
    def mc_skip_options(tokens, i, limit=8):
        """i부터 옵션을 건너뛴 다음 자리. 값을 받는 옵션은 다음 낱말까지 건너뛴다."""
        for _ in range(limit):
            if i >= len(tokens) or not tokens[i][2].startswith('-') or tokens[i][2] == '-':
                break
            flag = tokens[i][2]
            i += 2 if '=' not in flag and flag.lower() in MASK_MC_VALUE_FLAGS else 1
        return i

    def mc_positionals(self, tokens, i):
        out = []
        while i < len(tokens) and len(out) < 8:
            j = self.mc_skip_options(tokens, i)
            if j > i:
                i = j
                continue
            out.append(tokens[i])
            i += 1
        return out

    def mc_key_spans(self, text):
        """mc·mcli 명령줄에서 위치 인자로 준 키 낱말 (시작, 끝, 값) 목록. 마스킹과 사후 검증이 같이 쓴다.

        첫 낱말을 정규식으로 거른 뒤 하위 명령이 보일 만큼(최대 20개) 읽고, 맞을 때만 키까지(최대 40개) 읽는다.
        명령 이름 하나당 읽는 양이 묶여 있고, 키를 찾은 명령은 마지막 키 뒤부터 다음 명령을 찾으므로 입력에 선형이다.
        """
        spans = []
        if 'mc' not in text.lower():
            return spans
        pos = 0
        while True:
            cmd = self.mc_cmd.search(text, pos)
            if not cmd:
                return spans
            pos = cmd.end()
            self.tick()
            # 옵션 없는 흔한 형태(하위 명령 3 + 별칭·URL·키 2 + 여유)를 한 번에 읽는다. 앞이나 사이에 옵션이 있으면
            # 옵션 몫만큼 더 읽는다(최대 40개).
            tokens = self.mc_tokens(text, pos, 11)
            i = self.mc_skip_options(tokens, 0)
            if len(tokens) == 11 and (i or any(t[2].startswith('-') for t in tokens[3:])):
                tokens = self.mc_tokens(text, pos, 40)
                i = self.mc_skip_options(tokens, 0)
            words = [t[2].lower() for t in tokens[i:i + 3]]
            if words[:2] in (['alias', 'set'], ['alias', 'add']):
                args, by_url = self.mc_positionals(tokens, i + 2), True
            elif words == ['config', 'host', 'add']:
                args, by_url = self.mc_positionals(tokens, i + 3), True
            elif words == ['admin', 'user', 'add']:
                args, by_url = self.mc_positionals(tokens, i + 3), False
            else:
                continue
            at = 0
            if by_url:
                # URL(또는 이미 가린 URL) 뒤 두 낱말이 키다. URL 모양이 없으면 '별칭 URL' 다음 자리로 본다.
                at = next((k for k, t in enumerate(args) if self.mc_anchor.fullmatch(t[2].strip('"\''))), 1)
            keys = args[at + 1:at + 3]
            spans.extend(keys)
            if keys:
                pos = keys[-1][1]

    def mc_secret(self, text):
        """mc 명령줄의 위치 인자 키가 아직 가려지지 않았는지(사후 검증)."""
        return any(not self.mc_key_hidden(value) for _, _, value in self.mc_key_spans(text))

    def redact_mc_keys(self, text):
        out, pos = [], 0
        for start, end, value in sorted(set(self.mc_key_spans(text))):
            if start < pos or self.mc_key_hidden(value):
                continue
            out.append(text[pos:start])
            out.append('[REDACTED]')
            pos = end
        out.append(text[pos:])
        return ''.join(out)

    def redact_secrets(self, text):
        out, pos = [], 0
        for start, end, quote, key, separator in self.secret_spans(text):
            out.append(text[pos:start])
            out.append(quote + key + quote + separator + '[REDACTED]')
            pos = end
        out.append(text[pos:])
        return ''.join(out)

    def _subject_sub(self, match):
        value = match.group(2).rstrip(' \t.')
        if not value or self.is_placeholder(value):
            return match.group(0)
        return match.group(1) + self.alias_value(value) + match.group(2)[len(value):]

    @staticmethod
    def _encoded_sub(match):
        value = match.group(0)
        if any(c.isdigit() for c in value) and any(c.isupper() for c in value) and any(c.islower() for c in value):
            return '[ENCODED VALUE REMOVED]'
        return value

    def _host_sub(self, match):
        self.tick()
        return self.alias_value(match.group(0)) if self.host_sensitive(match.group(0)) else match.group(0)

    def _short_host_sub(self, match):
        self.tick()
        name = match.group(4)
        if name.lower() in MASK_HOST_STOPWORDS or self.is_placeholder(name):
            return match.group(0)
        return match.group(1) + match.group(2) + match.group(3) + self.alias_value(name)

    @staticmethod
    def is_ipv6(value):
        try:
            return ipaddress.ip_address(value.split('%', 1)[0]).version == 6
        except ValueError:
            return False

    def ipv6_part(self, value):
        """value 안에서 가릴 IPv6의 시작 위치. 없으면 -1.

        이름표('src:')가 붙어 있으면 첫 콜론 뒤부터 본다.
        0.5.10(S3): '0:2001:db8:0:0:0:0:0:7'·'src:0:2001:…'처럼 숫자 묶음이 이름표와 주소 사이에 끼면 둘째 콜론 뒤까지 본다.
        """
        cut = 0
        for _ in range(3):
            if self.is_ipv6(value[cut:]):
                return cut
            cut = value.find(':', cut) + 1
            if not 0 < cut < len(value):
                break
        return -1

    def _ipv6_sub(self, match):
        value = match.group(0)
        cut = self.ipv6_part(value)
        return value if cut < 0 else value[:cut] + self.alias_value(value[cut:])

    def string(self, text):
        if self.deadline is not None:
            check_deadline(self.deadline)
        text = self.strip_pem(text)
        text = self.bearer.sub('Bearer [REDACTED]', text)
        text = self.userinfo.sub(lambda m: m.group(1) + '[REDACTED]' + (':[REDACTED]' if m.group(3) is not None else '') + '@', text)
        text = self.aws_key.sub('[REDACTED]', text)
        text = self.redact_mc_keys(text)
        text = self.redact_secrets(text)
        text = self.jwt.sub('[JWT REMOVED]', text)
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)
        text = self.email.sub(self.alias, text)
        text = self.subject.sub(self._subject_sub, text)
        text = self.identifiers.sub(self.alias, text)
        text = self.host.sub(self._host_sub, text)
        text = self.mac.sub(self.alias, text)
        text = self.mac_dot.sub(self.alias, text)
        text = self.uuid_re.sub(self.alias, text)
        text = self.ipv6.sub(self._ipv6_sub, text)
        text = self.encoded.sub(self._encoded_sub, text)
        if self.level == 'strict':
            text = self.short_host.sub(self._short_host_sub, text)
            text = self.name_field.sub(lambda m: m.group(1) + '[MASKED]', text)
        if self.deadline is not None:
            check_deadline(self.deadline)
        return text

    def _known_tokens(self, text):
        """2차 치환: 한 곳에서 가린 이름(짧은 호스트명 등)이 다른 문장에 맨 이름으로 남지 않게 한다."""
        def sub(match):
            value = match.group(0)
            core = value.rstrip('.')
            alias = self.aliases.get(core)
            return alias + value[len(core):] if alias else value
        return self.token.sub(sub, text)

    def _walk(self, value, fn):
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, list):
            return [self._walk(x, fn) for x in value]
        if isinstance(value, dict):
            return {k: v if k == 'source_check_id' and isinstance(v, str) and v in self.public_check_ids
                    else self._walk(v, fn) for k, v in value.items()}
        return value

    def apply(self, value, *, for_verification=False):
        if for_verification:
            if isinstance(value, dict):
                return {k: '[PUBLIC CHECK ID]' if k == 'source_check_id' and isinstance(v, str) and v in self.public_check_ids
                        else self.apply(v, for_verification=True) for k, v in value.items()}
            if isinstance(value, list):
                return [self.apply(x, for_verification=True) for x in value]
            return value
        masked = self._walk(value, self.string)
        if any(self.token.fullmatch(original) for original in self.aliases):
            masked = self._walk(masked, self._known_tokens)
        return masked

    # -- 사후 검증: 남으면 안 되는 형태 -------------------------------------------
    def _alias_index(self):
        # 별칭은 늘기만 하므로 개수가 바뀔 때만 다시 만든다.
        count = len(self.aliases)
        cached = getattr(self, '_alias_cache', None)
        if cached is None or cached[0] != count:
            tokens = frozenset(x for x in self.aliases if self.token.fullmatch(x))
            others = [x for x in self.aliases if x not in tokens]
            cached = (count, tokens, others, literal_union(others))
            self._alias_cache = cached
        return cached[1:]

    def leaks(self, text):
        """Return the kinds of sensitive shapes still present in already-masked text."""
        found = []
        if '-----BEGIN ' in text or '-----END ' in text:
            found.append('pem')
        # 0.5.11: 별칭 원본을 하나씩 대조하면 문자열 수 x 별칭 수라 큰 클러스터에서 제곱으로 느려진다.
        # 같은 판정(낱말 모양 원본은 낱말 단위, 나머지는 부분 문자열)을 한 번에 찾는 색인으로 한다.
        token_originals, other_originals, other_union = self._alias_index()
        if token_originals and not token_originals.isdisjoint(m.group(0).rstrip('.') for m in self.token.finditer(text)):
            found.append('alias')
        elif other_originals and (other_union.search(text) if other_union is not None
                                  else any(original in text for original in other_originals)):
            found.append('alias')
        if self.secret_spans(text):
            found.append('secret')
        if self.mc_secret(text):
            found.append('secret')  # 0.5.9(R2), 0.5.10(S1)
        if self.aws_key.search(text) or any(not self.is_placeholder(m.group(2)) for m in self.userinfo.finditer(text)):
            found.append('secret')
        if self.mac_dot.search(text):
            found.append('mac')
        if self.jwt.search(text):
            found.append('jwt')
        if self.email.search(text):
            found.append('email')
        if self.mac.search(text):
            found.append('mac')
        if self.uuid_re.search(text):
            found.append('uuid')
        if self.ipv4_leak.search(text):
            found.append('ipv4')
        if any(self.ipv6_part(m.group(0)) >= 0 for m in self.ipv6.finditer(text)):
            found.append('ipv6')  # 0.5.8(M2)
        if any(self.host_sensitive(m.group(0)) and not self.is_public(m.group(0)) for m in self.host.finditer(text)):
            found.append('host')
        for match in self.subject.finditer(text):
            value = match.group(2).rstrip(' \t.')
            if value and not self.is_placeholder(value):
                found.append('subject')
                break
        return found

    def verify(self, value):
        problems = set()
        def scan(item):
            if isinstance(item, str):
                problems.update(self.leaks(item))
            elif isinstance(item, list):
                for x in item:
                    scan(x)
            elif isinstance(item, dict):
                for x in item.values():
                    scan(x)
        scan(self.apply(value, for_verification=True))
        if problems & {'secret', 'jwt'}:
            raise OutputError('secret verification failed')
        if problems:
            raise OutputError('redaction verification failed')


# --- 한글 해결방안(0.5.4) ------------------------------------------------------
# mc 공식 검사 ID별 정적 사전. 근거는 mc RELEASE.2026-07-24T01-15-12Z 바이너리에 내장된 공식 조치 안내(docs/<검사 ID>.md)이며,
# 보고서의 Resolution 원문과 같은 글이다. 실행 중 번역·네트워크 사용은 하지 않는다.
# 대상 OS가 RHEL·Rocky·AlmaLinux(el 계열)이므로 원문의 Debian 계열 명령은 el 계열 명령으로 바꾸고, 원문에 없는 조치·수치는 넣지 않는다.
# 입력과 무관한 고정 문구이므로 마스킹 대상이 아니다(마스킹·검증이 끝난 뒤 붙인다).
RESOLUTION_KO_VERSION = 'ko-res-1'
RESOLUTION_KO_BASIS = 'mc 공식 조치 안내(Resolution) 번역, el 계열(RHEL·Rocky·AlmaLinux) 명령으로 보완'
RESOLUTION_KO_FOOTER = ('설정·하드웨어 변경은 영향을 확인한 뒤 별도 승인을 받아 수행하십시오. 이 도구는 아무것도 변경하지 않습니다. '
                        '조치 후 진단을 다시 수집해 이 검사 결과를 확인하십시오. 근거 원문은 아래 "공식 조치 안내(Resolution, 마스킹)"에 있습니다.')
RESOLUTION_KO_MISSING = '한글 해결방안 미등록 — 아래 공식 원문 참고'


def _code(text):
    return ('code', text)


_REPL_CHECK = ('MinIO 서버 로그에서 복제 오류를 확인하고, 원본(source) 사이트에서 다른 사이트로 연결되는지 점검하십시오.',
               '문서를 참고하거나 SUBNET 티켓으로 안내를 받으십시오.')
_REPL_PENDING = ('복제가 자동으로 동기화합니다. 동기화되지 않으면 문서를 참고하거나 SUBNET 티켓으로 안내를 받으십시오.',)
_INODES = ('OS 도구로 inode 사용량이 시스템 한도를 넘게 늘어난 파일 경로를 찾으십시오(예: df -i 로 드라이브별 inode 사용률 확인).',
           'EXT4 드라이브의 inode 고갈은 파일시스템 특성이라 장기적으로는 보통 XFS로 다시 포맷해야 합니다. '
           '파일시스템 전환은 MinIO 드라이브 교체 절차(https://docs.min.io/enterprise/aistor-object-store/operations/failure-and-recovery/recover-after-drive-failure/)를 기준으로 하십시오.')

RESOLUTION_KO = {
    'minio.cluster.min': (
        '운영 권장 기준인 "server pool당 최소 8노드"를 충족하는 구성으로 이전(확장)하는 계획을 세우십시오.',
        '검사 메시지의 "권장 4대"는 이 검사의 판정 기준이고, 8노드는 공식 조치 안내의 운영 권고입니다.',
        '참고 문서: https://docs.min.io/enterprise/aistor-object-store/reference/aistor-server/requirements/'),
    'minio.cluster.node.tolerance': (
        '각 erasure set에서 Erasure Coding 패리티를 노드당 드라이브 수 이상으로 높이십시오.',
        '참고 문서: https://docs.min.io/enterprise/aistor-object-store/operations/core-concepts/erasure-coding/ (또는 SUBNET 이슈로 안내 요청)'),
    'minio.config.threshold': (
        '설정값을 확인해 지원 범위 안의 값으로 바꾸십시오. 문서를 참고하거나 SUBNET 티켓으로 안내를 받으십시오.',),
    'minio.buckets.max': (
        '애플리케이션 구조와 로직을 점검해 버킷이 과도하게 만들어지지 않도록 하십시오.',
        '최상위 버킷을 많이 만드는 대신 prefix(접두사)를 쓰는 구조를 검토하십시오.'),
    'minio.buckets.quota': (
        '버킷 사용량을 검토해 불필요한 데이터를 정리하거나, 증가가 정상이면 버킷 quota 한도를 늘리십시오.',
        '용량 계획 지원은 SUBNET 이슈로 엔지니어링과 상의하십시오.'),
    'minio.license.consistency': (
        '모든 서버에 같은 라이선스가 설치되어 있도록 맞추십시오.',),
    'minio.license.expiry': (
        '구독을 갱신하고 SUBNET에서 새 라이선스를 내려받아 클러스터에 설치하십시오.',
        '문의 사항은 SUBNET 이슈로 엔지니어링과 상의하십시오.'),
    'minio.net.latency': (
        '노드 간 네트워크를 점검하고 스위치·라우터 등 연결 장비와 관련 소프트웨어가 정상 동작하는지 확인하십시오.',),
    'minio.net.tls.algo': (
        'MinIO TLS 인증서를 ECDSA 또는 Ed25519 알고리즘으로 생성하면 연결당 성능이 좋아집니다.',
        '권장 알고리즘·암호 스위트: https://docs.min.io/enterprise/aistor-object-store/installation/linux/network-encryption/#recommendations-for-generating-certificates'),
    'minio.net.tls.status': (
        '배포에 TLS 암호화를 활성화하십시오.',
        '참고 문서: https://docs.min.io/enterprise/aistor-object-store/installation/linux/network-encryption/'),
    'minio.net.tls.valid': (
        'TLS 인증서를 유효한(정상 동작이 확인된) 인증서·키로 교체하십시오.',
        '교체 전후 인증서의 유효 기간은 다음처럼 읽기 전용으로 확인할 수 있습니다.',
        _code('openssl x509 -noout -subject -dates -in <인증서 파일>')),
    'minio.process.args': (
        '/etc/default/minio 파일의 MINIO_OPTS 값이 pool 안 모든 MinIO 노드에서 같은지 확인하십시오.',
        '환경 파일 없이 MinIO를 시작하는 경우에는 시작 스크립트·실행 인자가 모든 노드에서 같은지 확인하십시오.'),
    'minio.process.rlimit': (
        'OS의 rlimit-max(MinIO 프로세스의 열린 파일 수 상한) 설정을 최소 65000 이상 허용하도록 바꾸십시오.',
        'el 계열에서 systemd로 실행하면 서비스 유닛의 LimitNOFILE 값으로 정합니다(systemctl edit minio 에서 [Service] 아래 LimitNOFILE= 지정 → '
        'systemctl daemon-reload → MinIO 재시작). systemd 밖에서 실행하면 /etc/security/limits.conf 의 nofile 값을 확인하십시오.',
        '적용 값 확인: cat /proc/<MinIO PID>/limits 의 "Max open files" 줄. 컨테이너(쿠버네티스)로 실행하면 노드·컨테이너 런타임의 제한 설정을 확인하십시오.'),
    'minio.process.user': (
        'systemctl로 관리하는 배포는 minio 서버 프로세스를 실행하는 User를 root가 아닌 계정으로 바꾸십시오.',
        '현재 값은 systemctl cat minio 출력의 User= 줄로 확인합니다. '
        '참고 절차(Deploy MinIO): https://docs.min.io/enterprise/aistor-object-store/installation/linux/install/'),
    'minio.version.consistency': (
        '표시된 노드의 MinIO 서버 버전을 올려 모든 노드의 버전을 같게 맞추십시오.',
        '참고 절차: https://docs.min.io/enterprise/aistor-object-store/upgrade-aistor-server/upgrade-aistor-linux/'),
    **{'minio.repl.' + k: _REPL_CHECK for k in (
        'bkt.cfg.mismatch', 'bkt.cors_cfg.mismatch', 'bkt.olock.mismatch', 'bkt.policy.mismatch', 'bkt.quota_cfg.mismatch',
        'bkt.sse_cfg.mismatch', 'bkt.tags.mismatch', 'bkt.targets.offline', 'bkt.ver_cfg.mismatch', 'sites.offline')},
    **{'minio.repl.' + k: _REPL_PENDING for k in (
        'bkts.pending', 'group.policy.mismatch', 'groups.pending', 'policies.pending', 'user.policy.mismatch', 'users.pending')},
    'minio.repl.errors': (
        'MinIO 서버 로그에서 오류 상세를 확인하십시오. 문서를 참고하거나 SUBNET 티켓으로 안내를 받으십시오.',),
    'minio.repl.retries': (
        'MinIO 서버 로그에서 복제 오류가 있는지 확인하십시오. 문서를 참고하거나 SUBNET 티켓으로 안내를 받으십시오.',),
    'minio.repl.ilm.pending': (
        '사이트 복제 설정을 수정해 ILM 만료(Expiry) 규칙 복제를 활성화하십시오.',
        _code('mc admin replicate update ALIAS --enable-ilm-expiry-replication')),
    'sys.cpu.consistency': (
        '표시된 노드의 CPU를 확인하고, 가능할 때 pool 안 모든 노드가 같은 제조사·모델의 CPU를 쓰도록 교체하십시오.',),
    'sys.cpu.cores.min': (
        '표시된 노드의 CPU를 확인하고, 가능할 때 pool 안 모든 노드가 최소 8코어 이상인 같은 CPU를 쓰도록 교체하십시오.',),
    'sys.cpu.extensions': (
        '해당 pool 노드의 CPU를 다음 성능 기능을 지원하는 CPU로 교체하는 계획을 검토하십시오.',
        'ARM: asimd, aes / 그 외(x86 등): avx, avx2, aes, bmi1, bmi2, movbe',
        '지원 여부는 lscpu 출력의 Flags 항목에서 확인할 수 있습니다.'),
    'sys.cpu.governor': (
        '표시된 노드의 CPU 설정을 확인해 performance(또는 그와 비슷한) CPU governor를 쓰도록 하십시오.',
        'el 계열 확인: cpupower frequency-info (kernel-tools 패키지). 설정 예: cpupower frequency-set -g performance '
        '또는 tuned 프로필(throughput-performance 등). tuned 프로필은 다른 커널 설정도 함께 바꾸므로 영향을 확인한 뒤 적용하십시오.'),
    'sys.cpu.hyperthreading': (
        '성능을 위해 CPU 하이퍼스레딩(SMT)을 끄십시오.',
        _code('echo off | sudo tee /sys/devices/system/cpu/smt/control'),
        '위 명령은 즉시 적용되지만 재부팅하면 원래대로 돌아갑니다. 영구 적용은 BIOS(펌웨어) 설정 등 운영 표준 방법으로 정하십시오.',
        '문의 사항은 SUBNET 이슈로 엔지니어링과 상의하십시오.'),
    'sys.drive.consistency': (
        '아키텍처를 검토해 최소한 pool 안 모든 노드의 드라이브 개수와 용량이 같도록 하십시오.',
        '제조사가 서로 다른 드라이브 SKU의 성능·동작 특성이 같거나 비슷하다고 보면 이 경고는 무시해도 됩니다. '
        '그렇지 않으면 드라이브 교체 절차(https://docs.min.io/enterprise/aistor-object-store/operations/failure-and-recovery/recover-after-drive-failure/)로 '
        '드라이브 단위 교체를 계획해 pool 전체 구성을 맞추십시오.'),
    'sys.drive.mount': (
        '/etc/fstab 에서 /dev/sdb1, /dev/nvme0n1p1 같은 장치 경로(재부팅 후 바뀔 수 있음) 대신 고정 식별자를 쓰십시오.',
        'blkid /dev/sdX 로 UUID를 확인한 뒤 장치 경로를 UUID=<uuid> 로 바꿉니다. LABEL=, PARTUUID=, /dev/disk/by-id/ 도 쓸 수 있습니다.',
        '운영 중인 배포는 유지보수 시간에 한 노드씩 바꾸십시오. el 계열은 fstab 수정 후 findmnt --verify 로 문법을 확인하고 systemctl daemon-reload 로 반영합니다. '
        '문의 사항은 SUBNET 이슈로 상의하십시오.'),
    'sys.drive.status': (
        'hdparm, smartctl 같은 OS 드라이브 도구로 표시된 노드의 고장 드라이브를 찾고, MinIO 드라이브 교체 절차'
        '(https://docs.min.io/enterprise/aistor-object-store/operations/failure-and-recovery/recover-after-drive-failure/)에 따라 교체 일정을 잡으십시오.',
        'el 계열: smartctl 은 smartmontools 패키지, hdparm 은 hdparm 패키지에 있습니다(예: smartctl -a /dev/<장치>).',
        '소프트웨어 복구 도구로 단기 해결이 되더라도 해당 하드웨어의 교체 계획은 세워야 합니다.'),
    'sys.drive.usage': (
        '단기: mc du, mc ls --versions 로 객체를 직접 지울 수 있는 버킷·prefix를 찾아 공간을 확보하십시오(예: 오래된 noncurrent 객체 버전 삭제).',
        '장기: 첫 pool 용량의 최소 2~3배인 새 server pool로 클러스터 확장을 계획하십시오. 계획·구현 지원은 SUBNET 이슈로 받을 수 있습니다.'),
    'sys.fs.consistency': (
        '표시된 노드의 /etc/fstab 에서 모든 MinIO 드라이브의 마운트 옵션이 같은지 확인하십시오.',
        '현재 적용된 옵션 확인: findmnt -o TARGET,FSTYPE,OPTIONS'),
    'sys.fs.inodes.free': _INODES,
    'sys.fs.inodes.usage': _INODES,
    'sys.fs.supported': (
        '신규 배포: 표시된 드라이브를 xfs(Windows는 ntfs)로 다시 포맷하십시오.',
        '운영 중인 배포: 한 번에 드라이브 하나씩 다시 포맷하고 healing이 끝나기를 기다리는 방식으로 모든 드라이브를 권장 파일시스템으로 바꾸십시오. '
        '진행 전에 SUBNET 이슈로 엔지니어링과 상의하십시오.',
        '현재 파일시스템 확인: lsblk -f 또는 df -T'),
    'sys.fs.xfs.errors': (
        'MinIO는 모든 드라이브에서 XFS retry-on-error(오류 시 재시도) 동작을 끄도록 강력히 권장합니다.',
        '설정 방법은 MinIO 문서(https://docs.min.io/enterprise/aistor-object-store/reference/aistor-server/requirements/storage/#xfs-configuration-and-guidance)와 '
        '커널 문서(https://docs.kernel.org/admin-guide/xfs.html#error-handling)를 따르십시오.'),
    'sys.mem.consistency': (
        '표시된 노드의 하드웨어 구성을 바꿔 pool 안 모든 노드의 시스템 메모리 양을 같게 맞추십시오.',),
    'sys.mem.iommu': (
        '부팅 옵션에 iommu=pt(IOMMU passthrough)가 있어야 합니다. 확인: cat /proc/cmdline 출력에 iommu=pt 가 있는지 봅니다.',
        'el 계열(RHEL·Rocky·AlmaLinux 8/9/10) 적용: 공식 원문의 update-grub 은 Debian·Ubuntu 명령이므로 쓰지 않고 grubby 로 모든 커널 항목에 추가합니다(root 권한).',
        _code('grubby --update-kernel=ALL --args="iommu=pt"\ngrubby --info=ALL | grep -E "^(kernel|args)="'),
        '/etc/default/grub 의 GRUB_CMDLINE_LINUX 에도 iommu=pt 가 들어갔는지 확인하고, 없으면 추가하십시오. '
        'grub2-mkconfig 로 GRUB 설정을 다시 만드는 경우 이 값이 쓰이며, 출력 경로는 el 버전·부팅 방식(BIOS/UEFI)에 따라 다릅니다(예: BIOS는 /boot/grub2/grub.cfg).',
        '공식 원문의 EFI 방식(커널 명령줄 파일을 쓰는 경우): /etc/kernel/cmdline 에 iommu=pt 를 추가합니다.',
        '대안(공식 원문): iommu=pt 대신 CPU별 IOMMU를 끕니다. Intel은 intel_iommu=off, AMD는 amd_iommu=off.',
        '부팅 옵션은 재부팅해야 적용됩니다. 재부팅 후 cat /proc/cmdline 에 값이 있는지 확인하십시오. '
        '재부팅에 따른 서비스 영향(동시에 재부팅하는 노드 수)을 검토해 일정을 정하고, 쿠버네티스 등 컨테이너 환경이면 파드가 아니라 각 노드(호스트)에 적용합니다.'),
    'sys.mem.swap': (
        '표시된 노드의 swap을 끄십시오.',
        '일시 적용(현재 부팅에만, 재부팅하면 다시 켜짐):',
        _code('swapoff -a'),
        '영구 적용: /etc/fstab 에서 swap 항목을 지워(또는 주석 처리) 재부팅 후에도 꺼지게 합니다. 확인: swapon --show 출력이 비어 있으면 꺼진 상태입니다.'),
    'sys.mem.thp': (
        '권장값: enabled=madvise, defrag=defer+madvise 또는 madvise, khugepaged/max_ptes_none=0',
        '현재 값 확인:',
        _code('cat /sys/kernel/mm/transparent_hugepage/enabled\ncat /sys/kernel/mm/transparent_hugepage/defrag\n'
              'cat /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none'),
        '일시 적용(즉시 반영, 재부팅하면 원래 값으로 돌아감, root 권한, 공식 원문 명령):',
        _code('echo "madvise" > /sys/kernel/mm/transparent_hugepage/enabled\n'
              'echo "defer+madvise" > /sys/kernel/mm/transparent_hugepage/defrag\n'
              'echo "0" > /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none'),
        '영구 적용(재부팅 후에도 유지): el 계열은 tuned 사용자 프로필로 세 값을 함께 지정할 수 있습니다. '
        '<현재 프로필>에는 tuned-adm active 로 확인한 이름을 넣습니다.',
        _code('mkdir -p /etc/tuned/minio-thp\ncat > /etc/tuned/minio-thp/tuned.conf <<\'EOF\'\n[main]\ninclude=<현재 프로필>\n\n[sysfs]\n'
              '/sys/kernel/mm/transparent_hugepage/enabled=madvise\n/sys/kernel/mm/transparent_hugepage/defrag=defer+madvise\n'
              '/sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none=0\nEOF\ntuned-adm profile minio-thp'),
        'tuned를 쓰지 않는 서버는 부팅 때 같은 세 값을 쓰는 방법(예: systemd 서비스)을 운영 표준에 따라 정하십시오. '
        'enabled 값만은 커널 부팅 옵션(grubby --update-kernel=ALL --args="transparent_hugepage=madvise")으로도 지정할 수 있습니다.',
        '적용 후 재부팅하고 위 확인 명령으로 세 값이 유지되는지 확인하십시오. 쿠버네티스 등 컨테이너 환경이면 파드가 아니라 각 노드(호스트)에 적용합니다.',
        '참고 문서: https://tip.golang.org/doc/gc-guide#Linux_transparent_huge_pages'),
    'sys.net.eth.buffer': (
        'NIC의 rx/tx 링 버퍼가 하드웨어 최대치보다 낮게 설정되지 않도록 하십시오.',
        '확인: ethtool -g <인터페이스> 에서 "Pre-set maximums"와 "Current hardware settings"를 비교합니다.',
        '일시 적용 예(공식 원문 스크립트, jq 필요. jq가 없으면 인터페이스 이름을 직접 지정):',
        _code('for i in $(ip -p -j a | jq -r .[].ifname); do\n    sudo ethtool -G $i rx <max-rx> tx <max-tx>\ndone'),
        'ethtool -G 값은 재부팅 후 유지되지 않습니다. el 계열 NetworkManager 환경의 영구 적용 예: '
        'nmcli connection modify <연결 이름> ethtool.ring-rx <max-rx> ethtool.ring-tx <max-tx> 후 연결을 다시 활성화(nmcli connection up <연결 이름>).'),
    'sys.os.config': (
        '목록에 표시된 서비스를 server pool의 모든 노드에서 꺼서 MinIO가 계산·저장 자원을 다른 서비스와 경쟁하지 않게 하십시오.',
        'el 계열: systemctl disable --now <서비스 이름>'),
    'sys.os.consistency': (
        '표시된 노드의 호스트 OS를 갱신해 pool 안 모든 노드의 구성이 같도록 하십시오.',),
    'sys.os.kernel.min': (
        '모든 MinIO 노드를 지원 기간이 끝나지 않은(non-EOL) 같은 LTS 커널로 올리십시오.',
        'el 계열은 배포판이 제공·지원하는 커널(dnf 로 kernel 패키지 갱신 후 재부팅)을 모든 노드에 같은 버전으로 맞춥니다. 확인: uname -r'),
    'sys.os.kernel.unstable': (
        '커널을 6.1.66-1 이상으로 올리십시오(공식 원문의 근거는 Debian 버그 1057843).',
        '가장 이른 기회에 드라이브를 XFS로 전환하는 계획을 세우십시오.',
        'el 계열 커널은 버전 번호 체계가 달라 6.1.66-1 과 직접 비교되지 않습니다. el 서버에서 이 검사가 나오면 공식 원문과 SUBNET으로 해당 여부를 확인하십시오.'),
    'sys.os.ntp': (
        'pool 안 모든 서버에서 NTP(시간 동기화)가 켜져 있고 동기화되어 있는지 확인하십시오.',
        'el 계열(chrony): timedatectl 의 "System clock synchronized: yes", chronyc tracking 으로 확인합니다. 꺼져 있으면 systemctl enable --now chronyd'),
}


def resolution_ko(check_id, passed=False):
    """Static Korean resolution for an official check (0.5.4). Passed checks need no action, so none is attached."""
    if passed:
        return {'status': 'not_applicable', 'reason': 'official_check_passed', 'blocks': []}
    entry = RESOLUTION_KO.get(check_id)
    if entry is None:
        return {'status': 'not_registered', 'reason': 'no_korean_resolution_for_check', 'blocks': [], 'message': RESOLUTION_KO_MISSING}
    blocks = [{'type': 'code', 'text': b[1]} if isinstance(b, tuple) else {'type': 'text', 'text': b} for b in entry]
    return {'status': 'registered', 'basis': RESOLUTION_KO_BASIS, 'blocks': blocks, 'note': RESOLUTION_KO_FOOTER}


def attach_resolution_ko(report):
    report.setdefault('policy', {})['resolution_ko_version'] = RESOLUTION_KO_VERSION
    for f in report['findings']:
        if f.get('origin') == 'custom_rule':
            f['resolution_ko'] = {'status': 'not_applicable', 'reason': 'custom_rule_not_official_check', 'blocks': []}
        else:
            f['resolution_ko'] = resolution_ko(f.get('source_check_id'), passed=f.get('official_status') == 'passed')
    return report


def resolution_ko_markdown(res):
    if not res or res.get('status') == 'not_applicable':
        return []
    out = ['### 해결방안(한글)', '']
    if res['status'] != 'registered':
        return out + [md_text(res.get('message') or RESOLUTION_KO_MISSING), '']
    for block in res['blocks']:
        out.extend([md_code_block(block['text']) if block['type'] == 'code' else md_text(block['text']), ''])
    return out + ['> ' + md_inline(res['note']), '']


def resolution_ko_html(res):
    if not res or res.get('status') == 'not_applicable':
        return ''
    head = '<h4>해결방안(한글) <small>(공식 조치 안내 번역·el 계열 보완, 변경은 별도 승인)</small></h4>'
    if res['status'] != 'registered':
        return '<div class="box ko-res missing">%s<p>%s</p></div>' % (head, esc(res.get('message') or RESOLUTION_KO_MISSING))
    body = ''.join('<pre>%s</pre>' % esc(b['text']) if b['type'] == 'code' else '<p>%s</p>' % esc(b['text']) for b in res['blocks'])
    return '<div class="box ko-res">%s%s<p class="muted">%s</p></div>' % (head, body, esc(res['note']))


def model(text, mask='strict', as_of=None, parsed=None):
    started = time.monotonic()
    summary, collected, checks, notes = parsed or parse_report(text)
    basis = aware_time(as_of) if as_of else collected
    if as_of and basis is None:
        raise InputError('as-of must include timezone')
    explain = {
        'minio.cluster.min': ('가용성', 'P3', '공식 메시지의 권장 서버 수는 검사 기준이고, 조치 안내(Resolution)의 server pool당 노드 수는 운영 권고라 숫자가 다를 수 있습니다. 물리 호스트·Erasure Set·패리티 없이 장애 허용 수를 계산할 수 없습니다.'),
        'sys.mem.iommu': ('설정', 'unclassified', '부팅 옵션의 검사 불일치이며 실제 I/O 장애를 확정하지 않습니다. 공식 조치 안내의 update-grub은 Debian·Ubuntu 계열 명령입니다. RHEL·Rocky 등 el 계열은 grubby 또는 grub2-mkconfig로 부팅 옵션을 바꾸며, 재부팅 후 /proc/cmdline 내용으로 적용을 확인합니다. 호스트·배포 방식과 영향을 확인하고 변경은 별도 승인하십시오.'),
        'sys.mem.thp': ('설정', 'P2', '공식 근거 표는 서버마다 권장과 다른 항목 하나만 보여 줄 수 있으며 어느 항목이 표시될지는 수집마다 달라질 수 있습니다. 표에 없는 항목이 권장값과 같다는 뜻은 아닙니다. 원본 gzip을 함께 분석하면 ‘시스템 설정 비교’ 표에서 enabled·defrag·max_ptes_none 세 값을 서버별로 확인할 수 있습니다. 공식 조치 안내의 echo 명령은 재부팅 후 유지되지 않으므로 영구 적용 방법은 운영 표준에 따라 별도로 정하십시오. 변경 영향과 호스트 적용 상태를 확인하십시오.'),
        'minio.net.tls.valid': ('보안', 'P2', '상세 만료 원인을 확인하십시오. Secret 이름·TLS 종료 위치는 미확인입니다. 교체 후 실제 제공 인증서를 재검증하십시오.')}
    findings, identifiers = [], Counter()
    expiry = None
    certificate_locator = None
    for check in checks:
        category, priority, recommendation = explain.get(check['source_check_id'], ('미분류', 'unclassified', '등록되지 않은 검사는 원본을 보존하며 영향과 우선순위를 추정하지 않습니다.'))
        passed = check['official_status'] == 'passed'
        if passed:
            priority = 'unclassified'
            recommendation = '원본의 개별 검사 통과를 보존합니다. 이를 현재 서비스 정상 또는 변경 필요 판정으로 확대하지 않습니다.'
        key = check['source_check_id'] or 'unlinked:' + check['title']
        identifier = 'official-' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
        identifiers[identifier] += 1
        if identifiers[identifier] > 1:
            identifier += '-' + str(identifiers[identifier])
        known = check['source_check_id'] in explain
        entry = dict(check, id=identifier, origin='official_import', rule_result=None,
                     assessment='assessable' if known else 'unsupported',
                     assessment_reason='official_import_only' if known else 'no_registered_explanation',
                     priority=priority, category=category, recommendation=recommendation,
                     affected=[], affected_reason='targets_not_normalized_in_r1',
                     impact='실제 서비스 영향은 이 보고서만으로 확정하지 않습니다.',
                     approval_required=False if passed else True if known else None,
                     approval_reason='no_change_recommended_for_passed_import' if passed else
                         'changes_require_separate_approval' if known else 'no_action_plan_for_unknown_check',
                     verification_steps=['원본 관측과 실제 대상·설정을 읽기 전용으로 대조합니다.',
                                         '변경이 별도로 승인·수행된 경우 동일 검사와 실제 적용 상태를 재확인합니다.'] if known and not passed else [],
                     refs=[{'type': 'official_report', 'source_locator': check['source_locator'],
                            'check_version': None, 'reason': 'source_check_version_unknown'}],
                     basis='official_report', limitations=['통과 상세가 제공되지 않은 검사는 복원하지 않습니다.', '현재 서비스 상태를 실시간 확인한 결과가 아닙니다.'])
        if check['source_check_id'] == 'minio.net.tls.valid':
            certificate_locator = check['source_locator']
            times = re.findall(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', check['detail'])
            if len(times) == 2:
                try:
                    expiry = dt.datetime.fromisoformat(times[1].replace('Z', '+00:00'))
                except ValueError:
                    notes.append('invalid certificate timestamp')
        findings.append(entry)
    report = {'schema_version': SCHEMA_VERSION, 'tool_version': VERSION,
              'input_metadata': {'id': 'input-001', 'format': 'official_markdown', 'collected_at': collected.isoformat() if collected else None,
                                 'as_of': basis.isoformat() if basis else None, 'as_of_origin': 'explicit' if as_of else 'collection',
                                 'as_of_reason': None if basis else 'collection_time_missing_or_invalid',
                                 'parser_version': PARSER_VERSION, 'timezone': 'Asia/Seoul'},
              'source_versions': {name: {'value': None, 'reason': 'not_available_in_supported_markdown_contract'} for name in ('aistor', 'mc')},
              'analysis_summary': {name + '_counts': dict(sorted(Counter(f[name] for f in findings).items()))
                                   for name in ('priority', 'category', 'assessment')},
              'policy': {'version': POLICY_VERSION, 'profile': 'unspecified',
                         'scope': 'local_explanations_not_vendor_severity_or_custom_health_rules'},
              'official_summary': summary, 'findings': findings,
              'check_results': [{'finding_id': f['id'], 'source_check_id': f['source_check_id'], 'origin': 'official_import',
                                 'official_status': f['official_status'], 'rule_result': None, 'source_locator': f['source_locator']} for f in findings],
              'facts': {'certificate_expires_at': expiry.isoformat() if expiry else None,
                        'certificate_expires_kst': expiry.astimezone(KST).isoformat() if expiry else None,
                        'certificate_remaining_seconds': int((expiry - basis).total_seconds()) if expiry and basis else None,
                        'certificate_source_locator': certificate_locator,
                        'certificate_time_reason': None if expiry and basis else
                            'analysis_time_unavailable' if expiry else 'certificate_expiry_not_available'},
              'coverage': {'passed_details': 'not_available_from_aggregate', 'health': 'not_supported_in_r1', 'comparison': 'not_supported_in_r1', 'operational_analysis': 'not_supported_in_r1'},
              'parser_errors': notes, 'run_status': 'partial' if notes else 'success',
              'completeness': {'source_coverage': 'official_summary_and_provided_details', 'requested_scope_satisfied': not notes},
              'masking': {'level': mask, 'policy_version': 'r1-1', 'limitations': '자동 마스킹의 완전성을 보장하지 않습니다. 공유 전 수동 검토가 필요합니다.'}}
    # 0.5.6: 처리 시간 제한을 마스킹 도중에도 적용한다(이전에는 마스킹이 끝난 뒤에만 확인).
    masker = Masker(mask, public_check_ids=explain, deadline=deadline_after(started, MAX_PROCESS_SECONDS))
    report = masker.apply(report)
    masker.verify(report)
    # 0.5.4: 한글 해결방안은 입력과 무관한 고정 문구라 마스킹·검증 뒤에 붙인다(공식 판정·수치는 그대로).
    attach_resolution_ko(report)
    if over_limit(time.monotonic() - started, MAX_PROCESS_SECONDS):
        raise InputError('processing time limit exceeded')
    return report


def evidence_markdown(block):
    if block['type'] == 'code':
        return [md_code_block(block.get('text', ''))]
    if block['type'] == 'table':
        if block['headers'] and all(len(row) == len(block['headers']) for row in block['rows']):
            return (['| ' + ' | '.join(md_text(cell, cell=True) for cell in block['headers']) + ' |',
                     '| ' + ' | '.join('---' for _ in block['headers']) + ' |'] +
                    ['| ' + ' | '.join(md_text(cell, cell=True) for cell in row) + ' |' for row in block['rows']])
        rows = ([block['headers']] if block['headers'] else []) + list(block['rows'])
        return ['- ' + md_inline(' · '.join(str(cell) for cell in row)) for row in rows]
    if block['type'] == 'list':
        out = []
        for line in str(block.get('text', '')).split('\n'):
            item = re.match(r'^\s*(?:[-*+]|(\d{1,9})[.)])\s+(.*)$', line)
            if item:
                out.append((item[1] + '. ' if item[1] else '- ') + md_inline(item[2]))
            elif line.strip():
                out.append('  ' + md_inline(line.strip()))
        return out
    return [md_text(block.get('text', '')).replace('\n', '\n\n')]


# 0.5.5: 보고서의 판정·숫자는 mc 공식 판정만 쓴다. 이 도구의 자체 점검 판정은 싣지 않는다.
OFFICIAL_ONLY_NOTE = '판정·숫자는 mc 공식 판정만 표시합니다. 원본 gzip 관측값은 판정 없이 공식 검사의 근거로만 보입니다.'


def official_cards(findings):
    """0.5.5: only official checks become cards. Own-rule records never reach the report, the priority tiles or the exit code."""
    return [f for f in findings if f.get('origin') != 'custom_rule']


def server_observations_markdown(obs):
    """Per-server raw values attached to an official card (0.5.5, display only, no verdict)."""
    if not obs or not obs.get('rows'):
        return []
    return ['### 서버별 관측값(원본 gzip, 판정 아님)', '', blocks_markdown([('table', obs['headers'], obs['rows'])]), '']


def markdown(report, extra_blocks=None):
    out = ['# MinIO AIStor 폐쇄망 진단 보고서', '',
           '도구 버전: ' + md_inline(report['tool_version']) + ' / 스키마: ' + md_inline(report['schema_version']),
           '해설 정책: ' + md_inline(report['policy']['version']) + ' / 환경: ' + md_inline(label(report['policy']['profile'])), '',
           '## 공식 요약', '', '공식 판정(mc 보고서): ' + md_inline(summary_text(report['official_summary'])), '',
           '분석 상태: ' + md_inline(report['run_status']) + ' — ' + STATUS_KO.get(report['run_status'], ''), '',
           OFFICIAL_ONLY_NOTE + ' 통과 상세 부재는 정상 검사 복원을 허용하지 않습니다.', '']
    for f in official_cards(report['findings']):
        out.extend(['## ' + md_inline(f['title']), '',
                    '공식 상태: ' + md_inline(status_text(f)) + '  ',
                    '우선순위: ' + md_inline(priority_text(f['priority'])) + '  ',
                    '평가 범위: ' + md_inline(assessment_text(f)), '',
                    md_text(f['observed']), '', md_text(f['recommendation']), '', md_text(f['impact']), '',
                    '변경 승인: ' + APPROVAL_KO[f['approval_required']], '',
                    *resolution_ko_markdown(f.get('resolution_ko')),
                    *server_observations_markdown(f.get('server_observations')),
                    '### 재확인 절차', '', *['- ' + md_inline(step) for step in f['verification_steps']], '',
                    '### 원본 근거(마스킹)', ''])
        for block in f['evidence_blocks']:
            out.append('근거 구간: ' + md_inline(label(block['section'])) + ' / 줄 ' + str(block['source_locator']['line']))
            out.extend([''] + evidence_markdown(block))
            out.append('')
        if not f['evidence_blocks']:
            out.extend([md_text(f['detail']).replace('\n', '\n\n'), ''])
    out.append(blocks_markdown((extra_blocks or []) + info_blocks(report)))
    return '\n'.join(out)


# --- HTML 화면(0.5.0) ---------------------------------------------------------
# 표시 계층만 담당한다. 판정·마스킹·Markdown/JSON 출력은 바꾸지 않는다.
# 외부 자원 없이 인라인 CSS/SVG와 약간의 순수 JS만 쓰며, JS가 꺼져도 모든 내용이 보인다.
SEVERITY_UI = {'error': ('✖', '오류'), 'warning': ('▲', '경고'), 'passed': ('✔', '통과'), 'unknown': ('?', '미평가'), 'info': ('ⓘ', '정보')}
SEVERITY_ORDER = {'error': 0, 'warning': 1, 'unknown': 2, 'info': 3, 'passed': 4}
PRIORITY_ORDER = {'P1': 0, 'P2': 1, 'P3': 2, 'P4': 3}


def severity(finding):
    """Display bucket of an official card (0.5.5: cards are official checks only)."""
    status = finding.get('official_status')
    return status if status in ('error', 'warning', 'passed') else 'unknown'


def badge(sev, text=None, cls='sev'):
    icon, word = SEVERITY_UI[sev]
    return '<span class="badge %s-%s"><span aria-hidden="true">%s</span> %s</span>' % (cls, sev, icon, esc(text or word))


def stack_bar(parts, label_text):
    """Proportion bar; every segment also has a text label next to it."""
    total = sum(v for _, v, _ in parts if isinstance(v, int) and v > 0)
    if not total:
        return ''
    segs = ''.join('<span class="seg sev-%s" style="flex:%d" title="%s %d"></span>' % (sev, v, esc(name), v)
                   for sev, v, name in parts if isinstance(v, int) and v > 0)
    return '<div class="stack" role="img" aria-label="%s">%s</div>' % (esc(label_text), segs)


def tile(sev, name, value, sub=''):
    icon = SEVERITY_UI[sev][0] if sev in SEVERITY_UI else ''
    value = MISSING if value is None else value
    return ('<div class="tile tone-%s"><div class="tile-k"><span aria-hidden="true">%s</span> %s</div>'
            '<div class="tile-v">%s</div>%s</div>') % (sev, icon, esc(name), esc(value), '<div class="tile-s">%s</div>' % esc(sub) if sub else '')


# 0.5.2(B1): 우선순위 산정 기준 설명(규칙 자체는 그대로).
PRIORITY_BASIS = ('<p class="note">산정 기준: 공식 검사 카드 1장을 1건으로 셉니다. 검사별로 이 도구에 등록된 고정값이며 '
                  '등록되지 않은 검사는 미분류입니다. 원본 gzip 관측값은 우선순위에 넣지 않습니다.</p>')


def conclusion_text(report):
    """One cautious line. Never states that the service is healthy."""
    parts, issues = [], 0
    summary = report.get('official_summary')
    if isinstance(summary, dict):
        parts.append('공식 검사(mc) 오류 %s건·경고 %s건' % (summary.get('errors'), summary.get('warnings')))
        issues += sum(v for k, v in summary.items() if k in ('errors', 'warnings') and isinstance(v, int))
    head = ', '.join(parts) or '공식 판정 자료 없음'
    # 0.5.2(D7): 오류가 있으면 그것부터 보라고 덧붙인다. 0.5.5: 공식 판정만 센다.
    urgent = summary.get('errors') if isinstance(summary, dict) and isinstance(summary.get('errors'), int) else 0
    verdict = ('이 있어 확인이 필요합니다(오류 먼저).' if urgent else '이 있어 확인이 필요합니다.') if issues else ('으로, 보고된 오류·경고는 없습니다.' if parts else '입니다.')
    return head + verdict + ' 수집 시점의 기록을 분석한 결과이며 현재 서비스 상태를 확정하지 않습니다.'


def dashboard_html(report, extra=''):
    summary = report.get('official_summary')
    findings = report.get('findings') or []
    blocks = []
    if isinstance(summary, dict):
        blocks.append('<div class="panel"><h3>공식 판정 (mc 보고서)</h3><div class="tiles">%s%s%s</div>%s<p class="note">%s</p></div>' % (
            tile('passed', '통과', summary.get('passed')), tile('error', '오류', summary.get('errors')), tile('warning', '경고', summary.get('warnings')),
            stack_bar([('passed', summary.get('passed'), '통과'), ('error', summary.get('errors'), '오류'), ('warning', summary.get('warnings'), '경고')],
                      '공식 판정 비율: ' + summary_text(summary)),
            '공식 판정(mc 보고서) ' + summary_text(summary)))
    else:
        blocks.append('<div class="panel"><h3>공식 판정 (mc 보고서)</h3><p class="note">%s</p></div>' % esc(summary_text(summary)))
    # 0.5.5: 자체 점검 결과 타일은 없다. 우선순위는 공식 카드(통과 제외)만 센다.
    open_cards = [f for f in official_cards(findings) if severity(f) != 'passed']
    priorities = Counter(f.get('priority') for f in open_cards)
    blocks.append('<div class="panel"><h3>해설 우선순위 (공식 카드 기준, 통과 제외, 이 도구 기준·공식 심각도 아님)</h3><div class="tiles prio">%s</div>%s</div>' % (''.join(
        '<div class="tile tone-p%s"><div class="tile-k">%s</div><div class="tile-v">%d</div></div>' % (
            key[-1] if key in PRIORITY_ORDER else 'u', esc(key if key in PRIORITY_ORDER else '미분류'), priorities.get(key, 0))
        for key in ('P1', 'P2', 'P3', 'P4', 'unclassified')), PRIORITY_BASIS))
    return ('<section id="dashboard" aria-labelledby="dashboard-h"><h2 id="dashboard-h">요약</h2>'
            '<p class="conclusion"><span aria-hidden="true">ⓘ</span> %s</p><div class="panels">%s%s</div></section>') % (
        esc(conclusion_text(report)), ''.join(blocks), extra)


def evidence_html(blocks):
    out = []
    for block in blocks:
        out.append('<p class="loc">근거 구간: %s / 줄 %s</p>' % (esc(label(block['section'])), esc(block['source_locator']['line'])))
        if block['type'] == 'table':
            out.append(blocks_html([('table', block['headers'], block['rows'])]))
        else:
            out.append('<pre>%s</pre>' % esc(block['text']))
    return ''.join(out)


STANDARD_IMPACT = frozenset(('실제 서비스 영향은 이 보고서만으로 확정하지 않습니다.', '현재 서비스 상태·데이터 안전성·원인을 확정하지 않습니다.'))


def server_observations_html(obs):
    """0.5.5: per-server raw values (gzip) under an official card. Facts only: no badge, no own verdict, no rule text."""
    if not obs or not obs.get('rows'):
        return ''
    head = ''.join('<th scope="col">%s</th>' % esc(h) for h in obs['headers'])
    rows = ''.join('<tr><th scope="row">%s</th>%s</tr>' % (esc(r[0]), ''.join('<td>%s</td>' % esc(MISSING if v is None else v) for v in r[1:]))
                   for r in obs['rows'])
    return ('<div class="box obs-box"><h4>서버별 관측값(원본 gzip) <small>(판정 아님, 수집 시점 값)</small></h4>'
            '<div class="table-scroll"><table class="obs"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div></div>') % (head, rows)


def finding_card(f):
    sev = severity(f)
    verdict = '<div class="box official"><h4>%s 공식 판정 <small>(mc 보고서)</small></h4><p>%s</p></div>' % (
        badge(sev, '공식: ' + plain(status_text(f))), esc(f['observed']))
    steps = ''.join('<li>%s</li>' % esc(step) for step in f['verification_steps'])
    resolution = [b for b in f['evidence_blocks'] if b.get('section') == 'resolution']
    other = [b for b in f['evidence_blocks'] if b.get('section') != 'resolution']
    evidence = ''
    if resolution:
        # 0.5.4: 한글 해결방안을 위에 두고 영문 원문은 접어서 남긴다(인쇄 시에는 펼쳐진다).
        evidence += '<details class="res-en"><summary>공식 조치 안내(Resolution, 마스킹, 영문 원문)</summary>%s</details>' % evidence_html(resolution)
    if other or not f['evidence_blocks']:
        evidence += '<details><summary>마스킹된 근거</summary>%s</details>' % (evidence_html(other) or '<pre>%s</pre>' % esc(f['detail']))
    meta = '<span class="chip">우선순위 %s</span><span class="chip">영역 %s</span>' % (esc(plain(priority_text(f['priority']))), esc(f['category']))
    # 0.5.4 한글 해결방안, 0.5.5 서버별 관측값(판정 없음)을 공식 판정 아래에 둔다.
    verdict += resolution_ko_html(f.get('resolution_ko')) + server_observations_html(f.get('server_observations'))
    # 0.5.2(C1): 카드마다 같은 표준 면책 문장은 생략하고 검사 결과 머리말에 한 번만 둔다(JSON·Markdown은 그대로).
    impact = '' if f['impact'] in STANDARD_IMPACT else '<p>%s</p>' % esc(f['impact'])
    return ('<article class="card sev-%s" data-sev="%s" data-status="%s" data-priority="%s" data-category="%s">'
            '<header class="card-h">%s<h3>%s</h3><div class="chips">%s</div></header>%s'
            '<div class="box ours"><h4>이 도구의 해설 <small>(공식 판정과 별개)</small></h4><p>%s</p>%s'
            '<p class="muted">평가 범위: <span title="%s">%s</span> / 변경 승인: %s</p></div>%s%s</article>') % (
        sev, sev, esc(f['official_status']), esc(f['priority']), esc(f['category']),
        badge(sev), esc(f['title']), meta, verdict, esc(f['recommendation']), impact,
        esc('%s / %s' % (f.get('assessment'), f.get('assessment_reason'))), esc(plain(assessment_text(f))), esc(APPROVAL_KO[f['approval_required']]),
        '<div class="box steps"><h4>조치·재확인 절차</h4><ol>%s</ol></div>' % steps if steps else '', evidence)


HTML_CSS = '''
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--muted:#57606a;--line:#d0d7de;--accent:#0b5cad;--bar:#e3e7ec;
--error:#b42318;--error-bg:#fdecea;--warning:#8a5a00;--warning-bg:#fff3d1;--passed:#1a7f37;--passed-bg:#e7f5ea;--unknown:#57606a;--unknown-bg:#eceff2;
--p1:#b42318;--p2:#c2410c;--p3:#8a5a00;--p4:#0b5cad;--pu:#57606a}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#161b22;--fg:#e6edf3;--muted:#9da7b3;--line:#30363d;--accent:#58a6ff;--bar:#262c34;
--error:#ff7b72;--error-bg:#3b1517;--warning:#e3b341;--warning-bg:#33280c;--passed:#56d364;--passed-bg:#10301a;--unknown:#9da7b3;--unknown-bg:#21262d;
--p1:#ff7b72;--p2:#ffa657;--p3:#e3b341;--p4:#58a6ff;--pu:#9da7b3}}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:12px}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic","Apple SD Gothic Neo",sans-serif;line-height:1.55;overflow-wrap:anywhere}
.skip{position:absolute;left:-999px}.skip:focus{left:8px;top:8px;background:var(--card);padding:8px;z-index:9}
.top{padding:20px 24px 12px;border-bottom:1px solid var(--line);background:var(--card)}
.top h1{margin:0 0 4px;font-size:24px}.meta{margin:0;color:var(--muted);font-size:13px}
.layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:24px;max-width:1320px;margin:auto;padding:16px 24px 48px}
.toc{position:sticky;top:12px;align-self:start;font-size:14px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.toc strong{display:block;margin-bottom:6px}.toc ol{list-style:none;margin:0;padding:0}.toc a{display:block;padding:5px 8px;border-radius:6px;color:var(--fg);text-decoration:none}
.toc a:hover,.toc a:focus-visible{background:var(--bar)}
main{min-width:0}section{margin:0 0 28px}h2{font-size:20px;margin:8px 0 12px;padding-bottom:6px;border-bottom:2px solid var(--line)}
h3{font-size:16px;margin:0 0 8px}h4{font-size:14px;margin:0 0 6px}small,.muted,.note{color:var(--muted)}.note{font-size:13px;margin:8px 0 0}
.conclusion{background:var(--card);border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:8px;padding:12px 14px;margin:0 0 14px;font-weight:600}
.panels{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(78px,1fr));gap:8px}
.tile{border-radius:8px;padding:8px 10px;background:var(--unknown-bg);border:1px solid var(--line)}
.tile-k{font-size:13px;font-weight:600}.tile-v{font-size:26px;font-weight:700;line-height:1.2}.tile-s{font-size:12px;color:var(--muted)}
.tone-error{background:var(--error-bg);color:var(--error)}.tone-warning{background:var(--warning-bg);color:var(--warning)}
.tone-passed{background:var(--passed-bg);color:var(--passed)}.tone-unknown,.tone-info{background:var(--unknown-bg);color:var(--fg)}
.tone-p1{color:var(--p1)}.tone-p2{color:var(--p2)}.tone-p3{color:var(--p3)}.tone-p4{color:var(--p4)}.tone-pu{color:var(--pu)}
.prio .tile{background:var(--card)}
.stack{display:flex;height:12px;border-radius:6px;overflow:hidden;margin-top:10px;background:var(--bar)}
.seg.sev-info{background:var(--accent)}.badge.sev-info{color:var(--accent);background:var(--unknown-bg)}.card.sev-info{border-left-color:var(--accent)}
.obs-box{background:var(--card)}table.obs{font-size:13px;margin:4px 0}.counts{font-size:14px;margin:0 0 10px}
.seg.sev-error{background:var(--error)}.seg.sev-warning{background:var(--warning)}.seg.sev-passed{background:var(--passed)}.seg.sev-unknown{background:var(--unknown)}
.badge{display:inline-flex;align-items:center;gap:4px;font-size:12px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid currentColor;white-space:nowrap}
.sev-error.badge,.badge.sev-error{color:var(--error);background:var(--error-bg)}.badge.sev-warning{color:var(--warning);background:var(--warning-bg)}
.badge.sev-passed{color:var(--passed);background:var(--passed-bg)}.badge.sev-unknown{color:var(--unknown);background:var(--unknown-bg)}
.chip{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--bar)}.chips{display:flex;flex-wrap:wrap;gap:6px}
.card{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--unknown);border-radius:10px;padding:14px 16px;margin:0 0 14px}
.card.sev-error{border-left-color:var(--error)}.card.sev-warning{border-left-color:var(--warning)}.card.sev-passed{border-left-color:var(--passed)}
.card-h{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;margin-bottom:10px}.card-h h3{margin:0;flex:1 1 260px}
.box{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0}.box p{margin:4px 0}
.box.official{background:var(--bar)}.box.ko-res{border-left:4px solid var(--accent)}.box.ko-res.missing{border-left-color:var(--unknown)}.box.ko-res pre{white-space:pre-wrap}.steps ol{margin:4px 0;padding-left:22px}
details{margin:8px 0}summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bar);padding:8px;border-radius:6px}
.loc{font-size:12px;color:var(--muted);margin:6px 0 2px}
.table-scroll{overflow-x:auto}table{border-collapse:collapse;font-size:14px}th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{background:var(--bar)}
.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:0 0 14px}
.filters[hidden]{display:none}.sevbtns{display:flex;flex-wrap:wrap;gap:6px}
.sevbtns button{font:inherit;font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.sevbtns button[aria-pressed="true"]{background:var(--fg);color:var(--card);border-color:var(--fg)}
label{display:flex;align-items:center;gap:6px;font-size:14px}input,select{font:inherit;padding:5px 8px;min-width:0;max-width:100%;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px}
#shown{font-size:13px;color:var(--muted)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.appendix>summary{font-size:18px;padding:8px 0}
@media (max-width:860px){.layout{grid-template-columns:1fr;padding:12px}.panels{grid-template-columns:1fr}.toc{position:sticky;top:0;z-index:5;padding:6px 8px;min-width:0;max-width:100%}
.toc strong{display:none}.toc ol{display:flex;gap:4px;overflow-x:auto;white-space:nowrap}}
@media print{:root{color-scheme:light;--bg:#fff;--card:#fff;--fg:#000;--muted:#444;--line:#999;--bar:#eee}
*{print-color-adjust:exact;-webkit-print-color-adjust:exact}.toc,.filters,.skip{display:none!important}.layout{display:block;padding:0}
article[hidden]{display:block!important}details::details-content{content-visibility:visible!important;display:block!important}
details>*{display:block}.card,.panel,.srv,tr{break-inside:avoid}a{color:inherit}}
'''

HTML_SCRIPT = '''<script>'use strict';(function(){
const $=function(id){return document.getElementById(id);};const bar=$('filters');if(!bar)return;bar.hidden=false;
const cards=Array.prototype.slice.call(document.querySelectorAll('article.card'));const buttons=Array.prototype.slice.call(bar.querySelectorAll('button[data-sev]'));let sev='';
function apply(){const q=$('q').value.toLowerCase(),s=$('s').value,p=$('p').value,c=$('c').value;let shown=0;
cards.forEach(function(a){const ok=a.textContent.toLowerCase().includes(q)&&(!s||a.dataset.status===s)&&(!p||a.dataset.priority===p)&&(!c||a.dataset.category===c)&&(!sev||a.dataset.sev===sev);a.hidden=!ok;if(ok)shown++;});
$('shown').textContent=shown+'건 표시 / 전체 '+cards.length+'건';}
buttons.forEach(function(b){b.addEventListener('click',function(){sev=b.dataset.sev;buttons.forEach(function(x){x.setAttribute('aria-pressed',String(x===b));});apply();});});
$('q').addEventListener('input',apply);['s','p','c'].forEach(function(id){$(id).addEventListener('change',apply);});
let reopen=[];window.addEventListener('beforeprint',function(){cards.forEach(function(a){a.hidden=false;});reopen=Array.prototype.slice.call(document.querySelectorAll('details:not([open])'));reopen.forEach(function(d){d.open=true;});});
window.addEventListener('afterprint',function(){reopen.forEach(function(d){d.open=false;});reopen=[];apply();});apply();})();</script>'''

R1_CATEGORIES = ('가용성', '설정', '보안', '미분류')


def filters_html(cards_sev, status_options=(), categories=R1_CATEGORIES):
    # 0.5.3: 공식 버튼은 공식 카드만 센다(mc 보고서 숫자와 같은 기준). 0.5.5: 자체 점검 버튼은 없다.
    counts = Counter(cards_sev)
    buttons = ['<button type="button" data-sev="" aria-pressed="true">목록 전체 %d</button>' % len(cards_sev)]
    for sev in ('error', 'warning', 'passed', 'unknown'):
        if sev != 'unknown' or counts.get('unknown'):
            icon, word = SEVERITY_UI[sev]
            # 0.5.2(D2): 통과 카드가 없으면 대시보드의 통과 수와 어긋나 보이지 않게 이유를 붙인다.
            note = ' (공식 통과 상세는 원본에 없음)' if sev == 'passed' and not counts.get(sev) else ''
            buttons.append('<button type="button" data-sev="%s" aria-pressed="false"><span aria-hidden="true">%s</span> 공식 %s %d%s</button>' % (sev, icon, word, counts.get(sev, 0), note))
    extra = ''.join('<option value="%s">%s</option>' % (esc(value), esc(text)) for value, text in status_options)
    return ('<div class="filters" id="filters" hidden><div class="sevbtns" role="group" aria-label="심각도">%s</div>'
            '<label>검색 <input id="q" type="search"></label>'
            '<label>공식 결과 <select id="s"><option value="">전체</option><option value="error">오류 (error)</option><option value="warning">경고 (warning)</option><option value="passed">통과 (passed)</option>%s</select></label>'
            '<label>우선순위 <select id="p"><option value="">전체</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option><option value="unclassified">미분류 (unclassified)</option></select></label>'
            '<label>영역 <select id="c"><option value="">전체</option>%s</select></label><span id="shown" aria-live="polite"></span></div>') % (
        ''.join(buttons), extra, ''.join('<option>%s</option>' % esc(c) for c in categories))


def counts_html(report, cards):
    """Always visible (JS off too): official card counts next to the mc summary (0.5.5: official only)."""
    keys = Counter(severity(f) for f in cards)
    summary = report.get('official_summary')
    official = '공식 판정 카드: 오류 %d · 경고 %d' % (keys.get('error', 0), keys.get('warning', 0))
    if isinstance(summary, dict):
        official += ' (mc 보고서 요약: 통과 %s · 오류 %s · 경고 %s)' % (summary.get('passed'), summary.get('errors'), summary.get('warnings'))
    return '<p class="counts">%s</p>' % esc(official)


def html_page(report, *, sections=(), dashboard_extra='', status_options=(), categories=R1_CATEGORIES, before_appendix=()):
    """Single self-contained page: dashboard, optional visual sections, check cards, collapsed appendix."""
    shown = sorted(official_cards(report['findings']), key=lambda f: (SEVERITY_ORDER[severity(f)], PRIORITY_ORDER.get(f.get('priority'), 9)))
    cards = ''.join(finding_card(f) for f in shown)
    checks = ('<section id="checks" aria-labelledby="checks-h"><h2 id="checks-h">검사 결과 (오류·경고 우선)</h2>'
              '<p class="note">%s 통과 상세는 복원하지 않습니다. 모든 해설은 수집 시점 기록에 근거하며 현재 서비스 상태·실제 영향·원인을 확정하지 않습니다(카드마다 반복하지 않음).</p>'
              '%s%s%s</section>') % (esc(OFFICIAL_ONLY_NOTE), counts_html(report, shown), filters_html([severity(f) for f in shown], status_options, categories),
                                     cards or '<p>표시할 검사 상세가 없습니다.</p>')
    appendix = ('<section id="appendix"><details class="appendix"><summary>부록: 확인된 정보·입력 결합·한계</summary>%s</details></section>'
                % blocks_html(info_blocks(report)))
    body = [('dashboard', '요약', dashboard_html(report, dashboard_extra))] + list(sections) + \
           [('checks', '검사 결과', checks)] + list(before_appendix) + [('appendix', '부록', appendix)]
    toc = ''.join('<li><a href="#%s">%s</a></li>' % (esc(sid), esc(title)) for sid, title, _ in body)
    # 0.5.2(C4): 머리줄은 도구 버전과 분석 상태만. 스키마·해설 정책 버전은 부록 '확인된 정보' 표에 둔다.
    meta = '도구 %s · 분석 상태: %s' % (report['tool_version'], STATUS_KO.get(report['run_status'], report['run_status']))
    return ('<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
            '<title>MinIO AIStor 진단 보고서</title><style>' + HTML_CSS + '</style></head><body>'
            '<a class="skip" href="#main">본문으로 건너뛰기</a>'
            '<header class="top"><h1>MinIO AIStor 폐쇄망 진단 보고서</h1><p class="meta">' + esc(meta) + '</p></header>'
            '<div class="layout"><nav class="toc" aria-label="목차"><strong>목차</strong><ol>' + toc + '</ol></nav>'
            '<main id="main">' + ''.join(html for _, _, html in body) + '</main></div>' + HTML_SCRIPT + '</body></html>')


def html_report(report, extra_html=''):
    sections = [('extra', '추가 정보', '<section id="extra">%s</section>' % extra_html)] if extra_html else []
    return html_page(report, sections=sections)


def require_private(path, directory=False):
    info = Path(path).lstat()
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not kind_ok or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise OutputError('verified owner-only POSIX permissions required')
    if not directory and info.st_nlink != 1:
        raise OutputError('hard-linked output refused')


def secure_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise OutputError('symlink output directory refused')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'posix':
        raise OutputError('ACL verification is not supported on this OS in R1')
    require_private(path, directory=True)
    return path


def write_exclusive(path, data):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
        require_private(path)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if Path(path).read_text(encoding='utf-8') != data:
        raise OutputError('output read-back failed')


LEGACY_LOCK = '.output.lock'


def lock_output(root):
    """Exclusive, non-blocking lock for one output directory; returns the fd to close.

    0.4.1: the lock is taken on the output directory itself, so no file is left behind.
    A lock file is used only when the filesystem refuses directory locks (for example
    some NFS mounts). An existing lock file (0.4.0 or fallback) must stay owner-only.
    """
    import fcntl
    legacy = root / LEGACY_LOCK
    if legacy.exists() or legacy.is_symlink():
        require_private(legacy)
    fd = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise
    fd = os.open(legacy, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        require_private(legacy)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def save(report, out_dir, force=False, *, renderers=None):
    started = time.monotonic()
    root = secure_dir(out_dir)
    pointer = root / 'CURRENT.json'
    if pointer.is_symlink():
        raise OutputError('symlink pointer refused')
    if pointer.exists():
        require_private(pointer)
    if pointer.exists() and not force:
        raise OutputError('existing result refused; --force required')
    generation = root / ('run-' + uuid.uuid4().hex)
    try:
        fd = lock_output(root)
    except OSError as exc:
        raise OutputError('output directory busy or lock unavailable; previous committed set preserved') from exc
    try:
        if pointer.exists() and not force:
            raise OutputError('concurrent result collision')
        generation.mkdir(mode=0o700)
        require_private(generation, directory=True)
        renderers = renderers or {'markdown': markdown, 'html': html_report}
        outputs = {'aistor-diag-report.json': json.dumps(report, ensure_ascii=False, separators=(',', ':')) + '\n',
                   'aistor-diag-report.md': renderers['markdown'](report), 'aistor-diag-report.html': renderers['html'](report)}
        if over_limit(sum(len(body.encode('utf-8')) for body in outputs.values()), MAX_OUTPUT_BYTES):
            raise OutputError('output size limit exceeded')
        if over_limit(time.monotonic() - started, MAX_PROCESS_SECONDS):
            raise OutputError('rendering time limit exceeded')
        hashes = {}
        for name, body in outputs.items():
            write_exclusive(generation / name, body)
            hashes[name] = hashlib.sha256(body.encode('utf-8')).hexdigest()
        json.loads((generation / 'aistor-diag-report.json').read_text(encoding='utf-8'))
        manifest = {'run_id': generation.name, 'requested_formats': ['html', 'markdown', 'json'],
                    'tool_version': report['tool_version'], 'schema_version': report['schema_version'],
                    'parser_version': report['input_metadata']['parser_version'],
                    'policy_version': report['policy']['version'], 'masking_policy_version': report['masking']['policy_version'],
                    'run_status': report['run_status'],
                    'completeness': report['completeness'], 'files': hashes, 'commit': 'validated'}
        write_exclusive(generation / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        if json.loads((generation / 'manifest.json').read_text(encoding='utf-8')) != manifest:
            raise OutputError('manifest verification failed')
        for name, expected in hashes.items():
            require_private(generation / name)
            if hashlib.sha256((generation / name).read_bytes()).hexdigest() != expected:
                raise OutputError('checksum verification failed')
        marker = root / ('CURRENT-' + uuid.uuid4().hex + '.tmp')
        write_exclusive(marker, json.dumps({'generation': generation.name, 'manifest': 'manifest.json'}) + '\n')
        require_private(marker)
        if pointer.exists():
            require_private(pointer)
        require_private(generation / 'manifest.json')
        require_private(root, directory=True)
        os.replace(marker, pointer)
        return generation
    except (OSError, ValueError) as exc:
        raise OutputError('output failed; previous committed set preserved; new files remain restricted') from exc
    finally:
        os.close(fd)


def result_summary_line(report):
    # 0.5.5: 실행 끝 요약도 공식 판정만 보인다.
    return '요약: 공식 판정 ' + summary_text(report.get('official_summary')) + ' / 분석 상태 ' + str(report.get('run_status'))


def main(argv=None, *, default_out_dir=None):
    parser = argparse.ArgumentParser(description='AIStor R1 offline Markdown analyzer')
    parser.add_argument('--official-report', type=Path)
    parser.add_argument('--out-dir', type=Path, default=default_out_dir)
    parser.add_argument('--mask', choices=('basic', 'strict'), default='strict')
    parser.add_argument('--lang', choices=('ko',), default='ko')
    parser.add_argument('--as-of')
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--fail-on-priority', choices=('P1', 'P2', 'P3', 'P4'))
    parser.add_argument('--check-env', action='store_true')
    parser.add_argument('--version', action='version', version=VERSION)
    args = parser.parse_args(argv)
    if args.check_env:
        print(json.dumps({'python': sys.version.split()[0], 'platform': sys.platform, 'network_required': False, 'external_dependencies': False, 'output_permissions': 'POSIX owner-only required'}, ensure_ascii=False))
        return 0
    if not args.official_report:
        parser.error('--official-report is required')
    try:
        if over_limit(args.official_report.stat().st_size, MAX_BYTES):
            raise InputError('input limit exceeded')
        with args.official_report.open('rb') as stream:
            data = stream.read() if MAX_BYTES is None else stream.read(MAX_BYTES + 1)
        if over_limit(len(data), MAX_BYTES):
            raise InputError('input limit exceeded')
        text = data.decode('utf-8-sig')
        report = model(text, args.mask, args.as_of)
    except (InputError, OSError, UnicodeError) as exc:
        print('입력 오류: 필수 형식·인코딩·경로·제한을 확인하십시오.', file=sys.stderr)
        for line in describe_input_error(exc, '공식 Markdown(--official-report)'):
            print('  - ' + line, file=sys.stderr)
        return 2
    except OutputError:
        print('마스킹 검증 실패: 결과를 출력하지 않았습니다.', file=sys.stderr)
        return 3
    partial = report['run_status'] == 'partial'
    if partial and not args.allow_partial:
        print('부분 분석: 결과 미확정. --allow-partial로 제한 결과 저장을 명시하십시오.', file=sys.stderr)
        return 4
    if args.out_dir:
        try:
            refuse_input_output_alias(args.out_dir, ('공식 Markdown(--official-report)', args.official_report))
            generation = save(report, args.out_dir, args.force)
            print('결과 세트 확정: ' + str(generation.resolve()))
            print(result_summary_line(report))
        except (OutputError, OSError) as exc:
            print('\n'.join(output_failure_lines(exc)), file=sys.stderr)
            return 3
    else:
        print(json.dumps({'official_summary': report['official_summary'], 'run_status': report['run_status'], 'coverage': report['coverage']}, ensure_ascii=False))
    if partial:
        return 4
    if args.fail_on_priority and any(f['priority'] == args.fail_on_priority for f in official_cards(report['findings'])):
        return 1
    return 0
