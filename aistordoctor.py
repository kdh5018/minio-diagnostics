"""MinIO AIStor R1 offline report analyzer; original implementation, stdlib only.
R2 health and R3/R4 analysis are deliberately not implemented in R1.
"""
import argparse
import datetime as dt
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

VERSION = '0.1.0-r1'
DEFAULT_OUTPUT_DIR = Path(os.environ.get('AISTOR_DIAG_OUTPUT_DIR', str(Path.home() / 'minio-airgap-diag-output')))
MAX_BYTES = 2 * 1024 * 1024
MAX_LINES = 20000
MAX_CHECKS = 1000
KST = dt.timezone(dt.timedelta(hours=9))


class InputError(ValueError):
    pass


class OutputError(ValueError):
    pass


def cells(line):
    return [x.strip().replace('\\|', '|') for x in re.split(r'(?<!\\)\|', line.strip().strip('|'))]


def parse_report(text):
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if len(text.encode('utf-8')) > MAX_BYTES or len(text.splitlines()) > MAX_LINES:
        raise InputError('input limit exceeded')
    visible, fenced = [], False
    for n, line in enumerate(text.splitlines(), 1):
        if re.match(r'^\s*(`{3,}|~{3,})', line):
            if not fenced:
                fence = re.match(r'^\s*(`{3,}|~{3,})', line).group(1)
                fenced = True
            elif line.strip().startswith(fence) and len(line.strip()) >= len(fence):
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
                    try:
                        collected = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
                        if collected.tzinfo is None:
                            collected = None
                    except ValueError:
                        collected = None
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
    raw_lines = text.splitlines()
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
                       'source_locator': {'input': 'input-001', 'line': n}})
    for i, detail in enumerate(details):
        if i not in used:
            notes.append('unmatched detail preserved')
            checks.append({'source_check_id': None, 'official_status': detail['status'], 'title': detail['name'],
                           'observed': '', 'detail': '\n'.join(detail['lines']),
                           'source_locator': {'input': 'input-001', 'line': detail['line']}})
    ids = [c['source_check_id'] for c in checks if c['source_check_id']]
    if len(ids) != len(set(ids)):
        notes.append('duplicate check ID')
    if sum(c['official_status'] == 'error' for c in checks) != summary['errors'] or sum(c['official_status'] == 'warning' for c in checks) != summary['warnings']:
        notes.append('summary/detail mismatch')
    if fenced:
        notes.append('unclosed fence')
    return summary, collected, checks, sorted(set(notes))


class Masker:
    """Best-effort redaction, followed by leak checks. No mapping file is emitted."""
    secret = re.compile(r'''(?i)((?:secret[_ -]?key|access[_ -]?key|password|passwd|token|authorization)\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s|,;]+)''')
    private = re.compile(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', re.S)
    identifiers = re.compile(r'https?://[^\s<>\]\)]+|\b(?:[\w-]+\.)+(?:local|internal|svc|lan|com|net|org)(?::\d+)?\b|\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|(?:[A-Za-z]:\\|/)(?:[\w.@-]+[/\\])+[\w.@-]+', re.I)
    ipv6 = re.compile(r'(?<![\w:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f:]{1,4}(?:%[\w-]+)?(?![\w:])', re.I)

    def __init__(self, level, public_check_ids=()):
        self.level = level
        self.aliases = {}
        self.public_check_ids = frozenset(public_check_ids)

    def alias(self, match):
        value = match.group(0)
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
    summary, collected, checks, notes = parse_report(text)
    if as_of:
        try:
            basis = dt.datetime.fromisoformat(as_of.replace('Z', '+00:00'))
            if basis.tzinfo is None:
                raise ValueError()
        except ValueError:
            raise InputError('as-of must include timezone') from None
    else:
        basis = collected
    explain = {
        'minio.cluster.min': ('가용성', 'P3', '검사 기준과 운영 권고를 구분하십시오. 물리 호스트·Erasure Set·패리티 없이 장애 허용 수를 계산할 수 없습니다.'),
        'sys.mem.iommu': ('설정', 'unclassified', '부팅 옵션의 검사 불일치이며 실제 I/O 장애를 확정하지 않습니다. 호스트·배포 방식과 영향을 확인하고 변경은 별도 승인하십시오.'),
        'sys.mem.thp': ('설정', 'P2', '보고서에 관측된 설정만 사용합니다. defrag와 max_ptes_none의 실제 값은 미확인입니다. 변경 영향과 호스트 적용 상태를 확인하십시오.'),
        'minio.net.tls.valid': ('보안', 'P2', '상세 만료 원인을 확인하십시오. Secret 이름·TLS 종료 위치는 미확인입니다. 교체 후 실제 제공 인증서를 재검증하십시오.')}
    findings = []
    expiry = None
    for i, check in enumerate(checks, 1):
        category, priority, recommendation = explain.get(check['source_check_id'], ('미분류', 'unclassified', '등록되지 않은 검사는 원본을 보존하며 영향과 우선순위를 추정하지 않습니다.'))
        if check['official_status'] == 'passed':
            priority = 'unclassified'
        entry = dict(check, id='official-%03d' % i, origin='official_import', rule_result=None,
                     assessment='assessable', priority=priority, category=category, recommendation=recommendation,
                     basis='official_report', limitations=['통과 상세가 제공되지 않은 검사는 복원하지 않습니다.', '현재 서비스 상태를 실시간 확인한 결과가 아닙니다.'])
        if check['source_check_id'] == 'minio.net.tls.valid':
            times = re.findall(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', check['detail'])
            if len(times) == 2:
                try:
                    expiry = dt.datetime.fromisoformat(times[1].replace('Z', '+00:00'))
                except ValueError:
                    notes.append('invalid certificate timestamp')
        findings.append(entry)
    report = {'schema_version': '1.0-r1', 'tool_version': VERSION,
              'input_metadata': {'id': 'input-001', 'format': 'official_markdown', 'collected_at': collected.isoformat() if collected else None,
                                 'as_of': basis.isoformat() if basis else None, 'as_of_origin': 'explicit' if as_of else 'collection', 'timezone': 'Asia/Seoul'},
              'official_summary': summary, 'findings': findings,
              'check_results': [{'source_check_id': c['source_check_id'], 'origin': 'official_import', 'official_status': c['official_status'], 'rule_result': None, 'source_locator': c['source_locator']} for c in checks],
              'facts': {'certificate_expires_at': expiry.isoformat() if expiry else None,
                        'certificate_expires_kst': expiry.astimezone(KST).isoformat() if expiry else None,
                        'certificate_remaining_seconds': int((expiry - basis).total_seconds()) if expiry and basis else None},
              'coverage': {'passed_details': 'not_available_from_aggregate', 'health': 'not_supported_in_r1', 'comparison': 'not_supported_in_r1', 'operational_analysis': 'not_supported_in_r1'},
              'parser_errors': notes, 'run_status': 'partial' if notes else 'success',
              'completeness': {'source_coverage': 'official_summary_and_provided_details', 'requested_scope_satisfied': not notes},
              'masking': {'level': mask, 'policy_version': 'r1-1', 'limitations': '자동 마스킹의 완전성을 보장하지 않습니다. 공유 전 수동 검토가 필요합니다.'}}
    masker = Masker(mask, public_check_ids=explain)
    report = masker.apply(report)
    masker.verify(report)
    return report


def markdown(report):
    def literal(value):
        # All user-origin text remains inert even in HTML-capable Markdown viewers.
        return html.escape(str(value)).replace('`', '&#96;').replace('[', '&#91;').replace(']', '&#93;').replace('*', '&#42;').replace('_', '&#95;').replace('|', '&#124;').replace('!', '&#33;')
    out = ['# MinIO AIStor 폐쇄망 진단 보고서', '', '## 공식 요약', '', json.dumps(report['official_summary'], ensure_ascii=False), '',
           '분석 상태: ' + report['run_status'], '', '통과 상세 부재는 정상 검사 복원을 허용하지 않습니다.', '']
    for f in report['findings']:
        out.extend(['## ' + literal(f['title']), '', '공식 상태: ' + literal(f['official_status']), '우선순위: ' + f['priority'], '', literal(f['observed']), '', literal(f['recommendation']), '', '### 원본 근거(마스킹)', '', literal(f['detail']).replace('\n', '\n\n'), ''])
    out.extend(['## 확인된 정보', '', literal(report['input_metadata']), literal(report['facts']), '', '## 한계', '', literal(report['coverage']), literal(report['parser_errors']), literal(report['masking']['limitations']), ''])
    return '\n'.join(out)


def html_report(report):
    esc = lambda x: html.escape(str(x), quote=True)
    articles = []
    for f in report['findings']:
        articles.append('<article data-status="%s" data-priority="%s" data-category="%s"><h2>%s</h2><p>공식: %s / 우선순위: %s / %s</p><p>%s</p><p>%s</p><details><summary>마스킹된 근거</summary><pre>%s</pre></details></article>' %
                        tuple(esc(x) for x in (f['official_status'], f['priority'], f['category'], f['title'], f['official_status'], f['priority'], f['category'], f['observed'], f['recommendation'], f['detail'])))
    return '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>MinIO AIStor 진단 보고서</title><style>body{font-family:system-ui,sans-serif;max-width:1100px;margin:auto;padding:24px}article{border:1px solid #bbb;border-radius:8px;padding:16px;margin:16px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}input,select{padding:8px} @media print{.filters{display:none}article{break-inside:avoid}}</style><h1>MinIO AIStor 폐쇄망 진단 보고서</h1><p>상태: %s</p><p>공식 요약: %s</p><p>통과 상세는 복원하지 않습니다. 현재 서비스 상태의 실시간 검증이 아닙니다.</p><div class="filters"><label>검색 <input id="q" type="search"></label><label>공식 상태 <select id="s"><option value="">전체</option><option>error</option><option>warning</option><option>passed</option></select></label><label>우선순위 <select id="p"><option value="">전체</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option><option>unclassified</option></select></label><label>영역 <select id="c"><option value="">전체</option><option>가용성</option><option>설정</option><option>보안</option><option>미분류</option></select></label></div>%s<h2>분석 한계</h2><pre>%s</pre><p>%s</p><script>'use strict';function filter(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value,p=document.getElementById('p').value,c=document.getElementById('c').value;document.querySelectorAll('article').forEach(a=>{a.hidden=!(a.textContent.toLowerCase().includes(q)&&(!s||a.dataset.status===s)&&(!p||a.dataset.priority===p)&&(!c||a.dataset.category===c));});}document.getElementById('q').addEventListener('input',filter);document.getElementById('s').addEventListener('change',filter);document.getElementById('p').addEventListener('change',filter);document.getElementById('c').addEventListener('change',filter);</script></html>''' % (esc(report['run_status']), esc(json.dumps(report['official_summary'], ensure_ascii=False)), ''.join(articles), esc(json.dumps({'input_metadata': report['input_metadata'], 'coverage': report['coverage'], 'notes': report['parser_errors'], 'facts': report['facts']}, ensure_ascii=False, indent=2)), esc(report['masking']['limitations']))


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


def save(report, out_dir, force=False):
    root = secure_dir(out_dir)
    pointer = root / 'CURRENT.json'
    if pointer.is_symlink():
        raise OutputError('symlink pointer refused')
    if pointer.exists() and not force:
        raise OutputError('existing result refused; --force required')
    # fcntl is stdlib on the current Linux environment; the persistent lock file
    # is never deleted, preventing lock-inode races and respecting no-delete policy.
    import fcntl
    fd = os.open(root / '.output.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    generation = root / ('run-' + uuid.uuid4().hex)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if pointer.exists() and not force:
            raise OutputError('concurrent result collision')
        generation.mkdir(mode=0o700)
        require_private(generation, directory=True)
        outputs = {'aistor-diag-report.json': json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                   'aistor-diag-report.md': markdown(report), 'aistor-diag-report.html': html_report(report)}
        hashes = {}
        for name, body in outputs.items():
            write_exclusive(generation / name, body)
            hashes[name] = hashlib.sha256(body.encode('utf-8')).hexdigest()
        json.loads((generation / 'aistor-diag-report.json').read_text(encoding='utf-8'))
        manifest = {'tool_version': VERSION, 'schema_version': report['schema_version'], 'run_status': report['run_status'],
                    'completeness': report['completeness'], 'files': hashes, 'commit': 'validated'}
        write_exclusive(generation / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        marker = root / ('CURRENT-' + uuid.uuid4().hex + '.tmp')
        write_exclusive(marker, json.dumps({'generation': generation.name, 'manifest': 'manifest.json'}) + '\n')
        os.replace(marker, pointer)
        return generation
    except (OSError, ValueError) as exc:
        raise OutputError('output failed; previous committed set preserved; new files remain restricted') from exc
    finally:
        os.close(fd)


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
        text = args.official_report.read_text(encoding='utf-8-sig')
        report = model(text, args.mask, args.as_of)
    except (InputError, OSError, UnicodeError):
        print('입력 오류: 필수 형식·인코딩·경로·제한을 확인하십시오.', file=sys.stderr)
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
            print('결과 세트 확정: ' + str(generation))
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
