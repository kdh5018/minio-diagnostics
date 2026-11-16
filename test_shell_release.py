#!/usr/bin/env python3
"""run-report.sh and build-release.sh regression tests.

Synthetic inputs only. These tests exercise the shell entrypoint and packaging
contract (paths, cwd, read-only deployment, exit codes, option mapping,
environment refusal, forbidden content). They are not production-data evidence.
Linux + bash only; other platforms skip with an explicit reason.
Synthetic test directories are retained under the protected test-artifacts root.
"""
import hashlib
import io
import os
from pathlib import Path
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest

import aistordoctor as a

ROOT = Path(__file__).resolve().parent
RUN = ROOT / 'run-report.sh'
BUILD = ROOT / 'build-release.sh'
APP_FILES = ('analyze.py', 'aistordoctor.py', 'r2_health.py', 'r2_enrichment.py')
BASH = shutil.which('bash')
LINUX = platform.system() == 'Linux' and BASH is not None
PYVER = '%d.%d.%d' % sys.version_info[:3]
RELEASE = os.environ.get('AISTOR_DIAG_TEST_RELEASE')

# Same shape as run_tests.SYNTHETIC (not imported to avoid re-running its test classes).
SYNTHETIC = '''# MinIO Health Diagnostics Report
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
host=test.svc.cluster.local:9000 password=canary-secret
**Resolution:**
```
# this is text, never executed
```
'''
PARTIAL = SYNTHETIC.replace('1 errors', '2 errors')
REPORT_FILES = ('aistor-diag-report.html', 'aistor-diag-report.md', 'aistor-diag-report.json', 'manifest.json')


def host_os():
    values = {}
    for name in ('/etc/os-release', '/usr/lib/os-release'):
        try:
            with open(name, encoding='utf-8') as stream:
                for line in stream:
                    key, sep, value = line.rstrip('\n').partition('=')
                    if sep:
                        values[key] = value.strip('"\'')
            break
        except OSError:
            continue
    return values.get('ID', 'unknown'), values.get('VERSION_ID', '0').split('.')[0]


def tree_snapshot(path):
    out = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            out[os.path.relpath(full, path)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


def set_tree_writable(path, writable):
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            full = os.path.join(dirpath, name)
            mode = os.lstat(full).st_mode
            if stat.S_ISLNK(mode):
                continue
            os.chmod(full, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))
        mode = os.lstat(dirpath).st_mode
        os.chmod(dirpath, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))


@unittest.skipUnless(LINUX, 'run-report.sh tests need Linux and bash')
class ShellEntrypointTests(unittest.TestCase):
    def setUp(self):
        parent = a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')
        self.base = Path(tempfile.mkdtemp(prefix='shell-', dir=parent))
        self.home = self.base / '홈 디렉터리'
        self.home.mkdir(mode=0o700)
        self.args_log = self.base / 'stub-args.bin'

    def make_bundle(self, runtime='real', name='배포 폴더 (공백)', **info):
        bundle = self.base / name
        (bundle / 'app').mkdir(parents=True)
        shutil.copyfile(RUN, bundle / 'run-report.sh')
        os.chmod(bundle / 'run-report.sh', 0o755)
        for f in APP_FILES:
            shutil.copyfile(ROOT / f, bundle / 'app' / f)
        os_id, major = host_os()
        values = {'PACKAGE_VERSION': 'test', 'TARGET_ID': 'test-host', 'TARGET_ARCH': platform.machine(),
                  'TARGET_OS_IDS': os_id, 'TARGET_OS_MAJORS': major, 'MIN_GLIBC': '2.17', 'PYTHON_VERSION': PYVER}
        values.update(info)
        (bundle / 'RELEASE-INFO').write_text(''.join('%s=%s\n' % kv for kv in values.items()), encoding='utf-8')
        if runtime:
            py = bundle / 'runtime/python/bin/python3'
            py.parent.mkdir(parents=True)
            if runtime == 'real':
                py.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable), encoding='utf-8')
            else:
                py.write_text(self.stub_script(values['PYTHON_VERSION']), encoding='utf-8')
            os.chmod(py, 0o755)
        return bundle

    def stub_script(self, version):
        return ('#!/bin/sh\n'
                'for x in "$@"; do if [ "$x" = "-c" ]; then echo %s; exit 0; fi; done\n'
                ': > %s\n'
                'for x in "$@"; do printf "%%s\\0" "$x" >> %s; done\n'
                'if [ -n "${STUB_GEN:-}" ]; then echo "결과 세트 확정: $STUB_GEN"; fi\n'
                'exit "${STUB_RC:-0}"\n') % (shlex.quote(version), shlex.quote(str(self.args_log)), shlex.quote(str(self.args_log)))

    def stub_args(self):
        if not self.args_log.exists():
            return None
        return self.args_log.read_bytes().decode('utf-8').split('\0')[:-1]

    def run_sh(self, bundle, *args, cwd=None, env=None, direct=False):
        e = dict(os.environ)
        e['HOME'] = str(self.home)
        for key in ('STUB_RC', 'STUB_GEN'):
            e.pop(key, None)
        e.update(env or {})
        script = str(bundle / 'run-report.sh')
        cmd = [script, *args] if direct else [BASH, script, *args]
        return subprocess.run(cmd, cwd=str(cwd or self.base), env=e, capture_output=True, text=True, timeout=180)

    def write_input(self, rel, text=SYNTHETIC):
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def generation_of(self, out_dir):
        pointer = Path(out_dir) / 'CURRENT.json'
        import json
        return Path(out_dir) / json.loads(pointer.read_text(encoding='utf-8'))['generation']

    def assert_report_set(self, generation):
        for name in REPORT_FILES:
            self.assertTrue((generation / name).is_file(), name)
            self.assertEqual(stat.S_IMODE((generation / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o700)

    # -- 공백·한글 경로, 임의 cwd -------------------------------------------
    def test_space_korean_paths_from_root_cwd(self):
        bundle = self.make_bundle()
        report = self.write_input('입력 자료 1/공식 보고서 (10월).md')
        out = self.base / '출력 위치 2'
        p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', str(out), cwd='/', direct=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        generation = self.generation_of(out)
        self.assert_report_set(generation)
        self.assertIn(str(generation / 'aistor-diag-report.html'), p.stdout)
        self.assertIn('시스템 정상 판정은 아닙니다', p.stdout)

    def test_relative_paths_resolve_from_caller_cwd(self):
        bundle = self.make_bundle()
        work = self.base / '작업 위치 a b'
        self.write_input('작업 위치 a b/보고서 1.md')
        p = self.run_sh(bundle, '--official-report=보고서 1.md', '--out-dir', '상대 출력', cwd=work)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assert_report_set(self.generation_of(work / '상대 출력'))
        self.assertFalse((bundle / '상대 출력').exists())

    def test_invoked_through_symlink_finds_bundle(self):
        bundle = self.make_bundle()
        link_dir = self.base / '링크 위치'
        link_dir.mkdir()
        (link_dir / 'run').symlink_to(bundle / 'run-report.sh')
        p = subprocess.run([str(link_dir / 'run'), '--check'], cwd='/', capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(self.home)), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('사전 점검 통과', p.stdout)

    # -- 읽기 전용 배포 위치 ---------------------------------------------------
    def test_read_only_bundle_location(self):
        bundle = self.make_bundle()
        report = self.write_input('입력/보고서.md')
        before = tree_snapshot(bundle)
        set_tree_writable(bundle, False)
        try:
            frozen = tree_snapshot(bundle)
            p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', str(self.base / 'out'), cwd=bundle)
            after = tree_snapshot(bundle)
        finally:
            set_tree_writable(bundle, True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(frozen, after)
        self.assertEqual(set(before), set(after))
        self.assertFalse(any('__pycache__' in k for k in after))
        self.assert_report_set(self.generation_of(self.base / 'out'))

    # -- 종료 코드 보존 --------------------------------------------------------
    def test_exit_codes_0_to_4_preserved_with_stub(self):
        bundle = self.make_bundle(runtime='stub')
        report = self.write_input('in/r.md')
        gen = str(self.base / 'out/run-stub')
        for rc in (0, 1, 2, 3, 4, 9):
            for with_gen in (True, False):
                env = {'STUB_RC': str(rc)}
                if with_gen:
                    env['STUB_GEN'] = gen
                p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', str(self.base / 'out'), env=env)
                self.assertEqual(p.returncode, rc, (rc, with_gen, p.stderr))
                if rc == 4 and with_gen:
                    self.assertIn(gen + '/aistor-diag-report.html', p.stdout)
                    self.assertIn('부분 분석(종료 코드 4)', p.stdout)
                if rc == 4 and not with_gen:
                    self.assertIn('--allow-partial', p.stderr)

    def test_real_exit_codes_partial_force_and_input_errors(self):
        bundle = self.make_bundle()
        partial = self.write_input('입력 자료/부분.md', PARTIAL)
        out = self.base / '결과 위치'
        p = self.run_sh(bundle, '--official-report', str(partial), '--out-dir', str(out))
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertFalse((out / 'CURRENT.json').exists())
        self.assertIn('--allow-partial', p.stderr)

        p = self.run_sh(bundle, '--official-report', str(partial), '--out-dir', str(out), '--allow-partial')
        self.assertEqual(p.returncode, 4, p.stderr)
        first = self.generation_of(out)
        self.assert_report_set(first)
        self.assertIn(str(first / 'aistor-diag-report.html'), p.stdout)
        pointer = (out / 'CURRENT.json').read_bytes()

        p = self.run_sh(bundle, '--official-report', str(partial), '--out-dir', str(out), '--allow-partial')
        self.assertEqual(p.returncode, 3)
        self.assertIn('--force', p.stderr)
        self.assertEqual((out / 'CURRENT.json').read_bytes(), pointer)

        p = self.run_sh(bundle, '--official-report', str(partial), '--out-dir', str(out), '--allow-partial', '--force')
        self.assertEqual(p.returncode, 4, p.stderr)
        second = self.generation_of(out)
        self.assertNotEqual(first, second)
        self.assert_report_set(first)
        self.assert_report_set(second)

        bad = self.write_input('입력 자료/형식 오류.md', 'arbitrary markdown')
        p = self.run_sh(bundle, '--official-report', str(bad), '--out-dir', str(self.base / 'other'))
        self.assertEqual(p.returncode, 2)
        p = self.run_sh(bundle, '--official-report', str(self.base / '없는 파일.md'), '--out-dir', str(self.base / 'other'))
        self.assertEqual(p.returncode, 2)
        self.assertIn('파일이 없습니다', p.stderr)

    # -- 인자 전달·--force·eval 금지 ------------------------------------------
    def test_arguments_passed_verbatim_and_force_only_when_requested(self):
        bundle = self.make_bundle(runtime='stub')
        tricky = self.write_input('in/$(touch PWNED) ; `touch PWNED2` "q" \'s\' 공백.md')
        dash = self.write_input('in/-dash.md')
        p = self.run_sh(bundle, '--official-report', str(tricky), '--out-dir', str(self.base / 'o u t'))
        self.assertEqual(p.returncode, 0, p.stderr)
        args = self.stub_args()
        self.assertEqual(args[:4], ['-E', '-s', '-B', '-X'])
        self.assertEqual(args[5], str(bundle / 'app/analyze.py'))
        self.assertIn('--official-report=' + str(tricky), args)
        self.assertIn('--out-dir=' + str(self.base / 'o u t'), args)
        self.assertIn('--mask=strict', args)
        self.assertNotIn('--force', args)
        self.assertNotIn('--allow-partial', args)
        for d in (self.base, self.base / 'in', ROOT):
            self.assertFalse((d / 'PWNED').exists())
            self.assertFalse((d / 'PWNED2').exists())
        p = self.run_sh(bundle, '--official-report', '-dash.md', '--force', '--allow-partial', cwd=dash.parent)
        self.assertEqual(p.returncode, 0, p.stderr)
        args = self.stub_args()
        self.assertIn('--official-report=-dash.md', args)
        self.assertIn('--force', args)
        self.assertIn('--allow-partial', args)

    def test_default_and_relative_output_dir(self):
        bundle = self.make_bundle(runtime='stub')
        report = self.write_input('in/r.md')
        p = self.run_sh(bundle, '--official-report', str(report))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('--out-dir=' + str(self.home / 'aistor-diag-output'), self.stub_args())
        p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', 'rel out', cwd=self.base / 'in')
        self.assertIn('--out-dir=' + str(self.base / 'in/rel out'), self.stub_args())

    def test_r1_r2_option_differences(self):
        bundle = self.make_bundle(runtime='stub')
        report = self.write_input('in/r.md')
        health = self.write_input('in/h.json.gz', 'not used by stub')
        for extra in (['--fail-on-priority', 'P1'], ['--lang', 'ko'], ['--mask', 'basic']):
            if self.args_log.exists():
                self.args_log.unlink()
            p = self.run_sh(bundle, '--official-report', str(report), '--health', str(health), *extra)
            self.assertEqual(p.returncode, 2, extra)
            self.assertIsNone(self.stub_args(), extra)
        p = self.run_sh(bundle, '--official-report', str(report), '--health', str(health), '--as-of', '2026-10-12T00:00:00Z')
        self.assertEqual(p.returncode, 0, p.stderr)
        args = self.stub_args()
        self.assertIn('--health=' + str(health), args)
        self.assertIn('--as-of=2026-10-12T00:00:00Z', args)
        self.assertFalse(any(x.startswith(('--lang', '--fail-on-priority')) for x in args))
        p = self.run_sh(bundle, '--official-report', str(report), '--lang', 'ko', '--fail-on-priority', 'P2', '--mask', 'basic')
        self.assertEqual(p.returncode, 0, p.stderr)
        args = self.stub_args()
        self.assertIn('--lang=ko', args)
        self.assertIn('--fail-on-priority=P2', args)
        self.assertIn('--mask=basic', args)
        self.assertEqual(self.run_sh(bundle).returncode, 2)
        self.assertEqual(self.run_sh(bundle, '--official-report').returncode, 2)
        self.assertEqual(self.run_sh(bundle, '--unknown', str(report)).returncode, 2)
        self.assertEqual(self.run_sh(bundle, '--official-report', str(report), '--fail-on-priority', 'P9').returncode, 2)

    # -- 실행 환경 사전 점검 ---------------------------------------------------
    def test_unsupported_environment_refused_without_running(self):
        report = self.write_input('in/r.md')
        cases = [({'TARGET_ARCH': 'sparc64'}, [], 'CPU'),
                 ({'TARGET_OS_IDS': 'no-such-os'}, [], '지원하지 않는 환경'),
                 ({'MIN_GLIBC': '99.0'}, ['--allow-unverified-os'], 'glibc')]
        for index, (info, extra, message) in enumerate(cases):
            if self.args_log.exists():
                self.args_log.unlink()
            bundle = self.make_bundle(runtime='stub', name='bundle-%d' % index, **info)
            p = self.run_sh(bundle, '--official-report', str(report), *extra)
            self.assertEqual(p.returncode, 5, info)
            self.assertIn(message, p.stderr)
            self.assertIn('아무것도 설치하지 않았습니다' if 'glibc' not in message else 'glibc', p.stderr)
            self.assertIsNone(self.stub_args())
        bundle = self.make_bundle(runtime='stub', name='bundle-allow', TARGET_OS_IDS='no-such-os')
        p = self.run_sh(bundle, '--official-report', str(report), '--allow-unverified-os')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('검증 목록 외', p.stdout)

    def test_missing_or_unusable_runtime_refused(self):
        report = self.write_input('in/r.md')
        bundle = self.make_bundle(runtime=None, name='no-runtime')
        p = self.run_sh(bundle, '--official-report', str(report))
        self.assertEqual(p.returncode, 5)
        self.assertIn('동봉 런타임이 없습니다', p.stderr)
        bundle = self.make_bundle(runtime='stub', name='no-exec')
        os.chmod(bundle / 'runtime/python/bin/python3', 0o644)
        p = self.run_sh(bundle, '--official-report', str(report))
        self.assertEqual(p.returncode, 5)
        self.assertIn('noexec', p.stderr)
        bundle = self.make_bundle(runtime='stub', name='wrong-version', PYTHON_VERSION='0.0.1')
        os.chmod(bundle / 'runtime/python/bin/python3', 0o755)
        (bundle / 'runtime/python/bin/python3').write_text(self.stub_script('3.12.16'), encoding='utf-8')
        p = self.run_sh(bundle, '--official-report', str(report))
        self.assertEqual(p.returncode, 5)

    def test_system_python_option_requires_verified_version(self):
        bundle = self.make_bundle(runtime='stub')
        report = self.write_input('in/r.md')
        good = self.base / 'path-good'
        good.mkdir()
        (good / 'python3').symlink_to(sys.executable)
        old = self.base / 'path-old'
        old.mkdir()
        (old / 'python3').write_text('#!/bin/sh\necho 3.6.8\n', encoding='utf-8')
        os.chmod(old / 'python3', 0o755)
        base_path = '/usr/bin:/bin'
        p = self.run_sh(bundle, '--check', '--use-system-python', env={'PATH': str(good) + ':' + base_path})
        if sys.version_info >= (3, 9):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn('시스템 Python ' + PYVER, p.stdout)
        p = self.run_sh(bundle, '--official-report', str(report), '--use-system-python', env={'PATH': str(old) + ':' + base_path})
        self.assertEqual(p.returncode, 5)
        self.assertIn('검증 범위', p.stderr)
        p = self.run_sh(bundle, '--check', env={'PATH': str(old) + ':' + base_path})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('동봉 Python', p.stdout)

    def test_symlink_and_insecure_output_refused_without_changes(self):
        bundle = self.make_bundle(runtime='stub')
        report = self.write_input('in/r.md')
        link = self.base / 'in/link.md'
        link.symlink_to(report)
        p = self.run_sh(bundle, '--official-report', str(link))
        self.assertEqual(p.returncode, 2)
        self.assertIn('심볼릭 링크', p.stderr)
        real_out = self.base / 'real-out'
        real_out.mkdir(mode=0o700)
        (self.base / 'out-link').symlink_to(real_out)
        p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', str(self.base / 'out-link'))
        self.assertEqual(p.returncode, 3)
        loose = self.base / 'loose-out'
        loose.mkdir()
        os.chmod(loose, 0o755)
        p = self.run_sh(bundle, '--official-report', str(report), '--out-dir', str(loose))
        self.assertEqual(p.returncode, 3)
        self.assertIn('자동으로 변경하지 않습니다', p.stderr)
        self.assertEqual(stat.S_IMODE(loose.stat().st_mode), 0o755)
        self.assertIsNone(self.stub_args())

    def test_verify_bundle_detects_tampering(self):
        bundle = self.make_bundle(runtime='stub')
        files = sorted(p for p in bundle.rglob('*') if p.is_file())
        lines = ['%s  %s\n' % (hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(bundle).as_posix()) for p in files]
        (bundle / 'SHA256SUMS').write_text(''.join(lines), encoding='utf-8')
        p = self.run_sh(bundle, '--check', '--verify-bundle')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('체크섬: 일치', p.stdout)
        with open(bundle / 'app/r2_enrichment.py', 'a', encoding='utf-8') as stream:
            stream.write('\n# tampered\n')
        p = self.run_sh(bundle, '--check', '--verify-bundle')
        self.assertEqual(p.returncode, 5)

    def test_source_layout_uses_system_python(self):
        good = self.base / 'path-good'
        good.mkdir()
        (good / 'python3').symlink_to(sys.executable)
        p = subprocess.run([BASH, str(RUN), '--check', '--out-dir', str(self.base / 'src-out')], cwd='/', capture_output=True,
                           text=True, env=dict(os.environ, HOME=str(self.home), PATH=str(good) + ':/usr/bin:/bin'), timeout=60)
        if sys.version_info < (3, 9):
            self.skipTest('host python below supported source minimum')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('소스 실행', p.stdout)


@unittest.skipUnless(LINUX and all(shutil.which(t) for t in ('tar', 'gzip', 'sha256sum', 'find', 'grep', 'xargs', 'install')),
                     'build-release.sh tests need Linux bash, tar, gzip, sha256sum, findutils, grep, install')
class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        parent = a.secure_dir(a.DEFAULT_OUTPUT_DIR / 'test-artifacts')
        self.base = Path(tempfile.mkdtemp(prefix='release-', dir=parent))

    def fake_runtime(self):
        archive = self.base / 'fake-runtime.tar.gz'
        members = {
            'python/bin/python3': (b'#!/bin/sh\necho fake\n', 0o755),
            'python/bin/pip3': (b'#!/bin/sh\n', 0o755),
            'python/include/python3.12/Python.h': (b'', 0o644),
            'python/lib/python3.12/LICENSE.txt': (b'PSF LICENSE (fake)\n', 0o644),
            'python/lib/python3.12/os.py': (b'# fake\n', 0o644),
            'python/lib/python3.12/test/test_fake.py': (b'# fake\n', 0o644),
            'python/lib/python3.12/site-packages/pip/__init__.py': (b'', 0o644),
            'python/lib/python3.12/ensurepip/__init__.py': (b'', 0o644),
        }
        with tarfile.open(archive, 'w:gz') as tar:
            for name, (data, mode) in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = mode
                tar.addfile(info, io.BytesIO(data))
        return archive, hashlib.sha256(archive.read_bytes()).hexdigest()

    def build(self, *extra, sha=None):
        archive, digest = self.fake_runtime()
        out = self.base / 'dist'
        cmd = [BASH, str(BUILD), '--os', 'el8', '--arch', 'x86_64', '--runtime-archive', str(archive),
               '--runtime-sha256', sha or digest, '--allow-dirty', '--skip-runtime-exec', '--out-dir', str(out), *extra]
        env = dict(os.environ, TMPDIR=str(self.base))
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300), out

    def test_build_bundle_contents_and_forbidden_items(self):
        p, out = self.build()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tarballs = list(out.glob('*.tar.gz'))
        self.assertEqual(len(tarballs), 1)
        tarball = tarballs[0]
        self.assertTrue(tarball.name.endswith('-el8-x86_64.tar.gz'))
        expected = (out / (tarball.name + '.sha256')).read_text(encoding='utf-8').split()[0]
        self.assertEqual(hashlib.sha256(tarball.read_bytes()).hexdigest(), expected)
        verify_release_tarball(self, tarball, fake_runtime=True)
        again, _ = self.build()
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(hashlib.sha256(tarball.read_bytes()).hexdigest(), expected)

    def test_wrong_runtime_checksum_and_unknown_target_rejected(self):
        p, out = self.build(sha='0' * 64)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('SHA-256', p.stderr)
        self.assertFalse(list(out.glob('*.tar.gz')) if out.exists() else [])
        p = subprocess.run([BASH, str(BUILD), '--os', 'ubuntu22', '--arch', 'x86_64'], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('지원하지 않는 대상', p.stderr)

    def test_scan_only_flags_forbidden_content(self):
        bad = self.base / 'bad'
        (bad / '.git').mkdir(parents=True)
        (bad / 'CURRENT.json').write_text('{}', encoding='utf-8')
        (bad / 'diag-data-x.json.gz').write_bytes(b'x')
        (bad / 'note.txt').write_text('copied from /home/user/secret\n', encoding='utf-8')
        p = subprocess.run([BASH, str(BUILD), '--scan-only', str(bad)], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 1)
        for name in ('.git', 'CURRENT.json', 'diag-data-x.json.gz', 'note.txt'):
            self.assertIn(name, p.stderr)
        clean = self.base / 'clean'
        (clean / 'app').mkdir(parents=True)
        (clean / 'app/analyze.py').write_text('# ok\n', encoding='utf-8')
        p = subprocess.run([BASH, str(BUILD), '--scan-only', str(clean)], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)


def verify_release_tarball(test, tarball, fake_runtime=False):
    with tarfile.open(tarball, 'r:gz') as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        tops = {n.split('/', 1)[0] for n in names}
        test.assertEqual(len(tops), 1)
        top = tops.pop()
        test.assertTrue(top.startswith('aistor-diag-analyzer-'))
        rel = {n[len(top) + 1:]: m for n, m in zip(names, members) if n != top}
        for required in ('run-report.sh', 'RELEASE-INFO', 'README-ko.txt', 'SHA256SUMS', 'licenses/NOTICE-ko.txt',
                         'licenses/PYTHON-LICENSE.txt', 'runtime/python/bin/python3') + tuple('app/' + f for f in APP_FILES):
            test.assertIn(required, rel)
        test.assertTrue(rel['run-report.sh'].mode & 0o111)
        test.assertTrue(rel['runtime/python/bin/python3'].mode & 0o111)
        for m in members:
            if m.issym() or m.islnk():
                # Links are allowed only inside the runtime and must stay inside it.
                test.assertTrue(m.name.startswith(top + '/runtime/'), m.name)
                target = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname)) if m.issym() else m.linkname
                test.assertFalse(m.linkname.startswith('/'), m.name)
                test.assertTrue(target.startswith(top + '/runtime/'), (m.name, m.linkname))
            test.assertEqual((m.uid, m.gid), (0, 0))
            test.assertFalse(m.mode & 0o022, m.name)
        forbidden_parts = {'.git', '.tools', '.cache', 'fixtures', 'test-artifacts', 'output-r1', 'CURRENT.json',
                           'run_tests.py', 'test_r2.py', 'test_shell_release.py', 'verify_browser.cjs', 'README.md',
                           'R1_ACCEPTANCE.md', 'R1_CONTRACT.json', 'R2_CONTRACT.json', 'build-release.sh', 'LICENSE',
                           'include', 'ensurepip', 'idlelib', 'tkinter'}
        for path in rel:
            parts = path.split('/')
            test.assertFalse(forbidden_parts.intersection(parts), path)
            test.assertFalse(path.endswith(('.json.gz', '.bak', '.orig', '.log')), path)
            test.assertFalse('diag-report' in path or 'diag-data' in path, path)
            if not path.startswith('runtime/'):
                test.assertNotIn('__pycache__', parts, path)
            test.assertFalse(path.startswith('runtime/python/bin/pip'), path)
            test.assertNotIn('site-packages/pip', path)
            if path.startswith('runtime/python/lib/python3.') and '/test/' in path + '/':
                test.assertNotEqual(parts[4:5], ['test'], path)
        sums = tar.extractfile(top + '/SHA256SUMS').read().decode('utf-8').splitlines()
        listed = {}
        for line in sums:
            digest, name = line.split('  ', 1)
            listed[name] = digest
        files = {p: m for p, m in rel.items() if m.isfile() and p != 'SHA256SUMS'}
        test.assertEqual(set(listed), set(files))
        for path, m in files.items():
            test.assertEqual(hashlib.sha256(tar.extractfile(m).read()).hexdigest(), listed[path], path)
        info = tar.extractfile(top + '/RELEASE-INFO').read().decode('utf-8')
        for key in ('TARGET_ARCH=', 'TARGET_OS_IDS=', 'TARGET_OS_MAJORS=', 'MIN_GLIBC=', 'PYTHON_VERSION=',
                    'SOURCE_COMMIT=', 'LICENSE_STATUS=pending'):
            test.assertIn(key, info)
        for f in APP_FILES:
            test.assertEqual(tar.extractfile(top + '/app/' + f).read(), (ROOT / f).read_bytes(), f)
        test.assertEqual(tar.extractfile(top + '/run-report.sh').read(), RUN.read_bytes())
        if not fake_runtime:
            test.assertIn('runtime/python/lib/libpython3.12.so.1.0', rel)


@unittest.skipUnless(RELEASE, 'AISTOR_DIAG_TEST_RELEASE not set; built release artifact not checked (not a pass)')
class BuiltReleaseArtifactTests(unittest.TestCase):
    def test_release_artifact(self):
        tarball = Path(RELEASE)
        sha_file = tarball.with_name(tarball.name + '.sha256')
        expected = sha_file.read_text(encoding='utf-8').split()
        self.assertEqual(expected[1], tarball.name)
        self.assertEqual(hashlib.sha256(tarball.read_bytes()).hexdigest(), expected[0])
        verify_release_tarball(self, tarball)


if __name__ == '__main__':
    unittest.main()
