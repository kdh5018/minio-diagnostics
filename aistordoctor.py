"""MinIO AIStor R1 offline report analyzer; original implementation, stdlib only.
R2 health and R3/R4 analysis are deliberately not implemented in R1.
"""
import argparse
from collections import Counter
import datetime as dt
import errno
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

VERSION = '0.4.2-r1'
SCHEMA_VERSION = '1.1-r1'
PARSER_VERSION = 'official-markdown-r1-2'
POLICY_VERSION = 'r1-1'
DEFAULT_OUTPUT_DIR = Path(os.environ.get('AISTOR_DIAG_OUTPUT_DIR', str(Path.home() / 'minio-airgap-diag-output')))
MAX_BYTES = 2 * 1024 * 1024
MAX_LINES = 20000
MAX_CHECKS = 1000
MAX_BLOCKS = 2000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_PROCESS_SECONDS = 10
KST = dt.timezone(dt.timedelta(hours=9))


class InputError(ValueError):
    pass


class OutputError(ValueError):
    pass


# 입력 오류 안내. 키는 이 도구가 만든 고정 문구이며, 입력 내용·경로·원본 오류 본문은 출력하지 않는다.
INPUT_ERROR_HINTS = {
    'recognized summary is required': '공식 진단 Markdown의 Summary 줄(Results: N passed, N errors, N warnings)을 찾지 못했습니다. mc가 만든 diag-report-*.md인지 확인하십시오.',
    'required check table missing': '오류·경고 수가 있는데 검사 표(Check/Status 열)를 찾지 못했습니다. 잘리거나 편집된 Markdown일 수 있습니다.',
    'input limit exceeded': '공식 Markdown 크기·줄 수 제한(2 MiB, 20,000줄)을 넘었습니다.',
    'check limit exceeded': '검사 항목 수 제한(1,000개)을 넘었습니다.',
    'detail limit exceeded': '상세 검사 절 수 제한(1,000개)을 넘었습니다.',
    'block limit exceeded': '근거 블록 수 제한(2,000개)을 넘었습니다.',
    'processing time limit exceeded': '처리 시간 제한(10초)을 넘었습니다.',
    'as-of must include timezone': '--as-of에는 시간대가 필요합니다. 예: 2026-10-12T09:00:00+09:00 또는 2026-10-12T00:00:00Z',
    'timezone-aware as-of required': '--as-of에는 시간대가 필요합니다. 예: 2026-10-12T09:00:00+09:00 또는 2026-10-12T00:00:00Z',
    'Markdown limit': '공식 Markdown 크기 제한(2 MiB)을 넘었습니다.',
    'gzip signature or compressed limit': 'gzip 파일이 아니거나 압축 크기 제한(32 MiB)을 넘었습니다. 공식 Markdown은 --official-report로 지정하십시오.',
    'expanded size limit': '압축 해제 크기 제한(64 MiB)을 넘었습니다.',
    'nesting limit': 'JSON 중첩 깊이 제한(64단계)을 넘었습니다.',
    'serialized string limit before decoding': 'JSON 문자열 길이 제한을 넘었습니다.',
    'scalar token limit before decoding': 'JSON 값 길이 제한을 넘었습니다.',
    'structural token limit before decoding': 'JSON 요소 수 제한을 넘었습니다.',
    'resource limit': 'JSON 요소 수 또는 처리 시간 제한을 넘었습니다.',
    'string limit': 'JSON 문자열 길이 제한(65,536자)을 넘었습니다.',
    'processing time limit': '처리 시간 제한(10초)을 넘었습니다.',
    'ambiguous document count': 'gzip 안의 JSON 문서가 3개 이상입니다(지원: 본문 1개 또는 머리말+본문 2개).',
    'unsupported document structure': 'gzip 안의 JSON 구조가 지원 형식(객체 1~2개)이 아닙니다.',
    'unsupported header structure': 'gzip 첫 JSON 문서가 머리말이 아니라 본문으로 보입니다(지원하지 않는 구조).',
    'duplicate JSON key': 'JSON에 중복 키가 있어 해석을 거부했습니다.',
    'invalid JSON value': 'JSON에 NaN·Infinity 같은 허용되지 않는 값이 있습니다.',
    'minimum server-array structure unavailable': '원본 gzip에 MinIO 서버 정보 배열(minio.info.servers)이 없습니다. mc 진단 원본(diag-data-*.json.gz)인지 확인하십시오.',
    'server limit': '서버 기록 수 제한(1,000개)을 넘었습니다.',
    'drive limit': '드라이브 기록 수 제한(100,000개)을 넘었습니다.',
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
    'official_import_only': '공식 보고서 결과만 반영(자체 재판정 없음)', 'no_registered_explanation': '이 도구에 등록된 해설 없음',
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
    'explicit_flags_unobserved_or_ambiguous': '명시적 IOMMU 옵션이 관측되지 않았거나 모호함',
    'recognized_error_fields_only': '인식된 오류 필드만 집계', 'lower_bound_error_rows_observed': '최소 오류 행 수만 확인',
    'error_coverage_unavailable': '오류 집계 범위를 확인할 수 없음',
}


def label(code):
    """Korean text for a report code, keeping the code in parentheses for traceability."""
    if code is None:
        return MISSING
    text = LABEL_KO.get(str(code)) or CODE_KO.get(str(code)) or RESULT_KO.get(str(code))
    return '%s (%s)' % (text, code) if text else str(code)


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


RESULT_KO = {'pass': '통과', 'warning': '경고', 'fail': '실패', 'not_evaluated': '미평가', 'assessable': '평가 가능',
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
    rows += [('인증서 만료 시각', facts.get('certificate_expires_kst') or facts.get('certificate_expires_at')),
             ('인증서 남은 시간(%s 기준)' % basis_name, duration_text(facts.get('certificate_remaining_seconds'))),
             ('인증서 남은 시간(보고서 생성 시각 기준)', render_time_remaining(facts.get('certificate_expires_at'))),
             ('인증서 시각 미확정 사유', facts.get('certificate_time_reason'))]
    blocks.append(('table', ['항목', '값'], [r for r in rows if not (r[0] == '인증서 시각 미확정 사유' and r[1] is None)]))
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
    blocks.append(('h', 2, '자체 해설 요약'))
    rows = []
    for key, heading in (('priority_counts', '우선순위'), ('category_counts', '영역'), ('assessment_counts', '평가 범위'),
                       ('custom_rule_counts', '자체 점검 결과')):
        rows += counts_rows(heading, summary.get(key))
    blocks.append(('table', ['구분', '값', '개수'], rows) if rows else ('p', '집계할 항목이 없습니다.'))
    catalog = summary.get('custom_rule_catalog')
    if isinstance(catalog, dict) and catalog:
        blocks.append(('table', ['자체 점검 규칙', '규칙 버전', '필요 입력', '판정 조건'],
                       [(rid, c.get('version'), ', '.join(c.get('required_inputs') or []), c.get('condition')) for rid, c in catalog.items()]))
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
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def md_text(value, *, cell=False, line_start=True):
    """Markdown-escape arbitrary text. Lines are escaped independently (code spans never cross lines)."""
    lines = str(value).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    rendered = [md_line(line, cell, line_start or index > 0) for index, line in enumerate(lines)]
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
        if len(blocks) > MAX_BLOCKS:
            raise InputError('block limit exceeded')
    return blocks


def parse_report(text):
    deadline = time.monotonic() + MAX_PROCESS_SECONDS
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    raw_lines = text.splitlines()
    if len(text.encode('utf-8')) > MAX_BYTES or len(raw_lines) > MAX_LINES:
        raise InputError('input limit exceeded')
    visible, fenced = [], False
    for n, line in enumerate(raw_lines, 1):
        if time.monotonic() > deadline:
            raise InputError('processing time limit exceeded')
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
        if re.match(r'^##\s+', line):
            section = line[3:].strip().lower()
            headers = None
            current = None
        if section == 'summary':
            match = re.search(r'Results:\s*(\d+)\s+passed,\s*(\d+)\s+errors,\s*(\d+)\s+warnings', line, re.I)
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
    for i, detail in enumerate(details):
        end = details[i + 1]['line'] - 1 if i + 1 < len(details) else len(raw_lines)
        later_sections = [n - 1 for n, line in visible if n > detail['line'] and re.match(r'^##\s+', line)]
        if later_sections:
            end = min(end, later_sections[0])
        detail['lines'] = raw_lines[detail['line']:end]
    if len(details) > MAX_CHECKS:
        raise InputError('detail limit exceeded')
    used = set()
    for n, row in rows:
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
    if sum(len(c['evidence_blocks']) for c in checks) > MAX_BLOCKS:
        raise InputError('block limit exceeded')
    if time.monotonic() > deadline:
        raise InputError('processing time limit exceeded')
    return summary, collected, checks, sorted(set(notes))


class Masker:
    """Best-effort redaction, followed by leak checks. No mapping file is emitted."""
    secret = re.compile(r'''(?i)((?:secret[_ -]?key|access[_ -]?key|password|passwd|token|authorization)\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s|,;]+)''')
    private = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
    identifiers = re.compile(r'https?://[^\s<>\]\)]+|\b(?:[\w-]+\.)+(?:local|internal|svc|lan|com|net|org)(?::\d+)?\b|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|(?<![\w.:/-])[A-Za-z][\w-]*(?:\.[\w-]+)*:\d{2,5}\b|(?:[A-Za-z]:\\|/)(?:[\w.@-]+[/\\])+[\w.@-]+', re.I)
    # 0.4.1: 고객 조치 안내에 필요한 표준 커널·부팅 설정 경로와 공개 문서 URL만 원문으로 둔다.
    # 호스트명·IP·장치 식별자가 들어갈 수 있는 경로(/dev, /sys/class/net, /sys/block 등)는 계속 가린다.
    public_path = re.compile(r'/proc/(?:cmdline|meminfo|cpuinfo|sys/(?:kernel|vm|fs)(?:/[a-z0-9_]+)+)'
                             r'|/sys/kernel/mm/transparent_hugepage(?:/[a-z_]+)*'
                             r'|/etc/default/grub|/etc/kernel/cmdline|/etc/sysctl\.conf|/etc/security/limits\.conf'
                             r'|/boot/grub2/grub\.cfg|/boot/grub2/grubenv')
    public_url = re.compile(r'https://(?:docs\.min\.io|min\.io|www\.kernel\.org|kernel\.org|docs\.kernel\.org|tip\.golang\.org|go\.dev|golang\.org)'
                            r'(?:/[A-Za-z0-9._~%+-]+)*/?(?:\?highlight=[A-Za-z0-9_+-]{1,40})?(?:#[A-Za-z0-9._-]{1,80})?')
    trailing = '.,;:`\'"'

    ipv6 = re.compile(r'(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{1,4}(?:%[\w-]+)?(?![\w:])', re.I)

    def __init__(self, level, public_check_ids=()):
        self.level = level
        self.aliases = {}
        self.public_check_ids = frozenset(public_check_ids)

    def is_public(self, value):
        core = value.rstrip(self.trailing)
        if self.public_path.fullmatch(core):
            return True
        if self.public_url.fullmatch(core):
            rest = core.split('/', 3)[3] if core.count('/') >= 3 else ''
            # 경로·조각 안에 주소·도메인 형태가 끼어 있으면 공개 URL로 보지 않는다.
            return not re.search(r'\d{1,3}(?:\.\d{1,3}){3}|(?:[\w-]+\.)+(?:local|internal|svc|lan|com|net|org)\b|@', rest, re.I)
        return False

    def alias(self, match):
        value = match.group(0)
        if self.is_public(value):
            return value
        if value not in self.aliases:
            self.aliases[value] = 'identifier-%03d' % (len(self.aliases) + 1)
        return self.aliases[value]

    def string(self, text):
        text = self.private.sub('[PRIVATE KEY REMOVED]', text)
        text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
        text = self.secret.sub(lambda m: m.group(1) + '[REDACTED]', text)
        text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
        text = ''.join(c for c in text if c in '\n\t' or ord(c) >= 32 and ord(c) != 127)
        text = self.identifiers.sub(self.alias, text)
        def ipv6_alias(match):
            try:
                address = ipaddress.ip_address(match.group(0).split('%', 1)[0])
            except ValueError:
                return match.group(0)
            return self.alias(match) if address.version == 6 else match.group(0)
        text = self.ipv6.sub(ipv6_alias, text)
        if self.level == 'strict':
            text = re.sub(r'(?i)((?:bucket|object|policy|cluster|deployment)(?:[_ -]?name)?\s*[:=]\s*)([^\s|,;]+)', lambda m: m.group(1) + '[MASKED]', text)
        return text

    def apply(self, value, *, for_verification=False):
        if isinstance(value, str):
            return value if for_verification else self.string(value)
        if isinstance(value, list):
            return [self.apply(x, for_verification=for_verification) for x in value]
        if isinstance(value, dict):
            return {k: ('[PUBLIC CHECK ID]' if for_verification else v)
                    if k == 'source_check_id' and isinstance(v, str) and v in self.public_check_ids
                    else self.apply(v, for_verification=for_verification) for k, v in value.items()}
        return value

    def verify(self, value):
        dumped = json.dumps(self.apply(value, for_verification=True), ensure_ascii=False)
        if any(original in dumped for original in self.aliases) or '-----BEGIN ' in dumped:
            raise OutputError('redaction verification failed')
        for match in self.secret.finditer(dumped):
            if not match.group(2).startswith('[REDACTED]'):
                raise OutputError('secret verification failed')


def model(text, mask='strict', as_of=None):
    started = time.monotonic()
    summary, collected, checks, notes = parse_report(text)
    basis = aware_time(as_of) if as_of else collected
    if as_of and basis is None:
        raise InputError('as-of must include timezone')
    explain = {
        'minio.cluster.min': ('가용성', 'P3', '검사 기준과 운영 권고를 구분하십시오. 물리 호스트·Erasure Set·패리티 없이 장애 허용 수를 계산할 수 없습니다.'),
        'sys.mem.iommu': ('설정', 'unclassified', '부팅 옵션의 검사 불일치이며 실제 I/O 장애를 확정하지 않습니다. 호스트·배포 방식과 영향을 확인하고 변경은 별도 승인하십시오.'),
        'sys.mem.thp': ('설정', 'P2', '보고서에 관측된 설정만 사용합니다. defrag와 max_ptes_none의 실제 값은 미확인입니다. 변경 영향과 호스트 적용 상태를 확인하십시오.'),
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
    masker = Masker(mask, public_check_ids=explain)
    report = masker.apply(report)
    masker.verify(report)
    if time.monotonic() - started > MAX_PROCESS_SECONDS:
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


def markdown(report, extra_blocks=None):
    out = ['# MinIO AIStor 폐쇄망 진단 보고서', '',
           '도구 버전: ' + md_inline(report['tool_version']) + ' / 스키마: ' + md_inline(report['schema_version']),
           '해설 정책: ' + md_inline(report['policy']['version']) + ' / 환경: ' + md_inline(label(report['policy']['profile'])), '',
           '## 공식 요약', '', '공식 판정(mc 보고서): ' + md_inline(summary_text(report['official_summary'])), '',
           '분석 상태: ' + md_inline(report['run_status']) + ' — ' + STATUS_KO.get(report['run_status'], ''), '',
           '공식 판정과 이 도구의 자체 점검은 별개입니다. 통과 상세 부재는 정상 검사 복원을 허용하지 않습니다.', '']
    for f in report['findings']:
        out.extend(['## ' + md_inline(f['title']), '',
                    '공식 상태: ' + md_inline(status_text(f)) + '  ',
                    '우선순위: ' + md_inline(priority_text(f['priority'])) + '  ',
                    '평가 범위: ' + md_inline(assessment_text(f)), '',
                    md_text(f['observed']), '', md_text(f['recommendation']), '', md_text(f['impact']), '',
                    '변경 승인: ' + APPROVAL_KO[f['approval_required']], '',
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


def html_report(report, extra_html=''):
    articles = []
    for f in report['findings']:
        evidence = []
        for block in f['evidence_blocks']:
            evidence.append('<p>근거 구간: %s / 줄 %s</p>' % (esc(label(block['section'])), block['source_locator']['line']))
            if block['type'] == 'table':
                evidence.append(blocks_html([('table', block['headers'], block['rows'])]))
            else:
                evidence.append('<pre>%s</pre>' % esc(block['text']))
        steps = ''.join('<li>%s</li>' % esc(step) for step in f['verification_steps'])
        articles.append('<article data-status="%s" data-priority="%s" data-category="%s"><h2>%s</h2><p>공식: %s / 우선순위: %s / %s</p><p>%s</p><p>%s</p><p>%s</p><p>평가 범위: %s / 변경 승인: %s</p><ul>%s</ul><details><summary>마스킹된 근거</summary>%s</details></article>' %
                        (*tuple(esc(x) for x in (f['official_status'], f['priority'], f['category'], f['title'], status_text(f),
                                               priority_text(f['priority']), f['category'], f['observed'], f['recommendation'], f['impact'],
                                               assessment_text(f), APPROVAL_KO[f['approval_required']])),
                         steps, ''.join(evidence) or '<pre>%s</pre>' % esc(f['detail'])))
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>MinIO AIStor 진단 보고서</title><style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px;overflow-wrap:anywhere}
h1{font-size:28px}h2{font-size:20px}article{border:1px solid #bbb;border-radius:8px;padding:16px;margin:16px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere}.filters{display:flex;flex-wrap:wrap;gap:12px}
label{display:flex;align-items:center;gap:6px}input,select{padding:8px;min-width:0;max-width:100%%}
summary{cursor:pointer}summary:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #167d60}
.table-scroll{overflow-x:auto}table{border-collapse:collapse}th,td{border:1px solid #bbb;padding:8px;text-align:left}
@media(max-width:480px){body{padding:16px}h1{font-size:24px}}
@media print{.filters{display:none}article{break-inside:avoid}}
</style><h1>MinIO AIStor 폐쇄망 진단 보고서</h1><p>도구·스키마·정책: %s</p><p>상태: %s</p><p>%s</p>
<p>통과 상세는 복원하지 않습니다. 현재 서비스 상태의 실시간 검증이 아닙니다.</p>
<div class="filters"><label>검색 <input id="q" type="search"></label>
<label>공식 상태 <select id="s"><option value="">전체</option><option value="error">오류 (error)</option><option value="warning">경고 (warning)</option><option value="passed">통과 (passed)</option></select></label>
<label>우선순위 <select id="p"><option value="">전체</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option><option value="unclassified">미분류 (unclassified)</option></select></label>
<label>영역 <select id="c"><option value="">전체</option><option>가용성</option><option>설정</option><option>보안</option><option>미분류</option></select></label></div>
%s%s%s
<script>'use strict';function filter(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value,p=document.getElementById('p').value,c=document.getElementById('c').value;document.querySelectorAll('article').forEach(a=>{a.hidden=!(a.textContent.toLowerCase().includes(q)&&(!s||a.dataset.status===s)&&(!p||a.dataset.priority===p)&&(!c||a.dataset.category===c));});}document.getElementById('q').addEventListener('input',filter);document.getElementById('s').addEventListener('change',filter);document.getElementById('p').addEventListener('change',filter);document.getElementById('c').addEventListener('change',filter);</script></html>''' % (
        esc(report['tool_version'] + ' / ' + report['schema_version'] + ' / ' + report['policy']['version']),
        esc(report['run_status'] + ' — ' + STATUS_KO.get(report['run_status'], '')), esc('공식 판정(mc 보고서) ' + summary_text(report['official_summary'])), ''.join(articles),
        extra_html, blocks_html(info_blocks(report)))


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
        if sum(len(body.encode('utf-8')) for body in outputs.values()) > MAX_OUTPUT_BYTES:
            raise OutputError('output size limit exceeded')
        if time.monotonic() - started > MAX_PROCESS_SECONDS:
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
    custom = (report.get('analysis_summary') or {}).get('custom_rule_counts')
    line = '요약: 공식 판정 ' + summary_text(report.get('official_summary'))
    if isinstance(custom, dict):
        names = {'pass': '통과', 'warning': '경고', 'not_evaluated': '미평가'}
        line += ' / 자체 점검 ' + ' · '.join('%s %s' % (names.get(k, k), v) for k, v in sorted(custom.items(), key=lambda kv: (list(names).index(kv[0]) if kv[0] in names else 9, str(kv[0]))))
    return line + ' / 분석 상태 ' + str(report.get('run_status'))


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
        if args.official_report.stat().st_size > MAX_BYTES:
            raise InputError('input limit exceeded')
        with args.official_report.open('rb') as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
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
            # Output sets cannot alias or reside within the source file.
            if args.out_dir.resolve() == args.official_report.resolve() or (args.out_dir / 'CURRENT.json').resolve() == args.official_report.resolve() or ((args.out_dir / 'CURRENT.json').exists() and os.path.samefile(args.out_dir / 'CURRENT.json', args.official_report)):
                raise OutputError('input/output collision')
            generation = save(report, args.out_dir, args.force)
            print('결과 세트 확정: ' + str(generation.resolve()))
            print(result_summary_line(report))
        except (OutputError, OSError):
            print('출력 실패: 경로·충돌·접근권한을 확인하십시오. 기존 확정 결과를 보존합니다.', file=sys.stderr)
            return 3
    else:
        print(json.dumps({'official_summary': report['official_summary'], 'run_status': report['run_status'], 'coverage': report['coverage']}, ensure_ascii=False))
    if partial:
        return 4
    if args.fail_on_priority and any(f['priority'] == args.fail_on_priority for f in report['findings']):
        return 1
    return 0
