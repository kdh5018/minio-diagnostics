#!/usr/bin/env bash
# 런타임 포함 폴더형 배포물(tar.gz)을 만든다. 개발 환경 전용 스크립트다.
#
#   ./build-release.sh --os el8 --arch x86_64
#
# - 분석기 소스(analyze.py 등)와 run-report.sh, 대상별 Python 런타임을 묶는다.
# - 런타임은 python-build-standalone(install_only_stripped)을 사용하고 SHA-256을 고정한다.
#   인터넷이 없는 빌드 환경에서는 --runtime-archive로 미리 받은 파일을 지정한다.
# - 고객·테스트 서버에서 실행하는 스크립트가 아니다. 서버에는 결과 tar.gz만 반입한다.
# - 진단 원본·보고서·.git·인증정보·테스트 잔여물·백업·내부 계약서는 포함하지 않으며,
#   묶기 전에 금지 목록 검사를 다시 수행한다.
#
# 대상 추가 방법: target_config()에 "<os>-<arch>)" 블록 하나를 추가한다.
#   필요한 값은 런타임 triple, 해당 런타임 파일의 SHA-256, 지원 OS ID·주 버전, 최소 glibc다.

set -euo pipefail
umask 022

readonly PACKAGE_NAME=aistor-diag-analyzer
readonly PACKAGE_VERSION=0.3.0
readonly PBS_RELEASE=20261110
readonly PBS_PYTHON=3.12.16
readonly APP_FILES=(analyze.py aistordoctor.py r2_health.py r2_enrichment.py)

ROOT=$(cd -P -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd -P)

err() { printf '%s\n' "$*" >&2; }
die() { err "빌드 오류: $*"; exit 1; }

usage() {
    cat <<'USAGE'
사용법: build-release.sh --os <os> --arch <arch> [옵션]

  --os <os>                 대상 OS 기준(현재: el8 = RHEL·Rocky·Alma 8 이상)
  --arch <arch>             대상 CPU(현재: x86_64)
  --out-dir <디렉터리>      결과 저장 위치(기본: ./dist)
  --cache-dir <디렉터리>    런타임 내려받기 캐시(기본: ./.cache/runtime)
  --runtime-archive <파일>  미리 받은 런타임 파일 사용(내려받지 않음)
  --runtime-sha256 <값>     런타임 파일 SHA-256 재지정(시험용; 기본은 대상별 고정값)
  --allow-dirty             커밋되지 않은 분석기 변경이 있어도 빌드(배포 정보에 dirty 기록)
  --skip-runtime-exec       빌드 환경에서 런타임 실행 점검 생략(다른 CPU 대상 빌드용)
  --list-targets            지원 대상 목록
  --scan-only <디렉터리>    금지 항목 검사만 실행
USAGE
}

# ---------------------------------------------------------------- 대상 정의
KNOWN_TARGETS="el8-x86_64"

target_config() {
    TARGET_ID=$1-$2
    TARGET_OS_BASE=$1
    TARGET_ARCH=$2
    case $TARGET_ID in
        el8-x86_64)
            # RHEL 계열 8 이상. 런타임은 glibc 2.17 기준 빌드이므로 EL8(glibc 2.28)에서 동작한다.
            TARGET_DESC="RHEL·Rocky Linux·AlmaLinux 8/9/10 (x86_64)"
            TARGET_OS_IDS="rhel rocky almalinux"
            TARGET_OS_MAJORS="8 9 10"
            MIN_GLIBC=2.28
            PBS_TRIPLE=x86_64-unknown-linux-gnu
            RUNTIME_SHA256_PINNED=6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39
            ;;
        # 예시(미검증·미빌드). 요청이 오면 SHA-256을 확인해 채우고 실제 환경에서 검증한다.
        # el8-aarch64)
        #     TARGET_DESC="RHEL·Rocky Linux·AlmaLinux 8/9/10 (aarch64)"
        #     TARGET_OS_IDS="rhel rocky almalinux"; TARGET_OS_MAJORS="8 9 10"; MIN_GLIBC=2.28
        #     PBS_TRIPLE=aarch64-unknown-linux-gnu; RUNTIME_SHA256_PINNED=<확인 필요> ;;
        # ubuntu22-x86_64)
        #     TARGET_DESC="Ubuntu 22.04/24.04 (x86_64)"
        #     TARGET_OS_IDS="ubuntu"; TARGET_OS_MAJORS="22 24"; MIN_GLIBC=2.35
        #     PBS_TRIPLE=x86_64-unknown-linux-gnu; RUNTIME_SHA256_PINNED=6a9e62da... ;;
        *)
            die "지원하지 않는 대상입니다: $TARGET_ID (가능: $KNOWN_TARGETS)"
            ;;
    esac
    RUNTIME_FILE=cpython-${PBS_PYTHON}+${PBS_RELEASE}-${PBS_TRIPLE}-install_only_stripped.tar.gz
    RUNTIME_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${RUNTIME_FILE//+/%2B}
}

# ---------------------------------------------------------------- 금지 항목 검사
# 18.4절 "절대 포함 금지": .git, 개발용 GitHub CLI·인증정보, 진단 원본, 실제 생성 보고서,
# 테스트 잔여물, 백업, 내부 운영 계약서, 개인·내부 경로가 들어간 로그.
scan_forbidden() {
    local dir=$1 bad=0 hits
    hits=$(cd -- "$dir" && find . \( \
        -name .git -o -name .gitignore -o -name .tools -o -name .cache -o -name .github \
        -o -name gh -o -name 'hosts.yml' -o -name '.netrc' -o -name '.git-credentials' \
        -o -name '*.json.gz' -o -name '*diag-report*' -o -name '*diag-data*' \
        -o -name 'CURRENT.json' -o -name '.output.lock' -o -name 'manifest.json' -o -name 'aistor-diag-report.*' \
        -o -name 'R1_CONTRACT*' -o -name 'R2_CONTRACT*' -o -name '*CONTRACT*.json' \
        -o -name fixtures -o -name test-artifacts -o -name 'output-r1' -o -name 'run-[0-9a-f]*' \
        -o -name '*.bak' -o -name '*.orig' -o -name '*.swp' -o -name '*~' -o -name '*backup*' \
        -o -name '*.log' -o -name 'test_*.py' -o -name 'run_tests.py' -o -name 'verify_browser.cjs' \
        -o -name 'router_progress.py' \
        \) -print) || return 2
    if [ -n "$hits" ]; then
        err "금지 항목 발견:"
        err "$hits"
        bad=1
    fi
    # 분석기 쪽(런타임 제외)에는 바이트코드 캐시도 넣지 않는다.
    hits=$(cd -- "$dir" && find . -path ./runtime -prune -o \( -name '__pycache__' -o -name '*.pyc' \) -print) || return 2
    if [ -n "$hits" ]; then
        err "분석기 바이트코드 캐시 발견:"
        err "$hits"
        bad=1
    fi
    # 배포물 텍스트(런타임 제외)에 개인·내부 경로나 토큰 형식이 없어야 한다.
    hits=$(cd -- "$dir" && grep -rIlE --exclude-dir=runtime \
        '/home/user|/mnt/c/|C:\\\\Users|hermes|obsidian|ghp_[A-Za-z0-9]|github_pat_|gho_[A-Za-z0-9]' . 2>/dev/null) || true
    if [ -n "$hits" ]; then
        err "내부 경로·인증정보 형식 문자열 발견:"
        err "$hits"
        bad=1
    fi
    return "$bad"
}

# ---------------------------------------------------------------- 인자
OS_ARG=
ARCH_ARG=
OUT_DIR=$ROOT/dist
CACHE_DIR=$ROOT/.cache/runtime
RUNTIME_ARCHIVE=
RUNTIME_SHA256_OVERRIDE=
ALLOW_DIRTY=0
SKIP_RUNTIME_EXEC=0

while [ $# -gt 0 ]; do
    case $1 in
        --os) OS_ARG=${2:?}; shift 2 ;;
        --arch) ARCH_ARG=${2:?}; shift 2 ;;
        --out-dir) OUT_DIR=${2:?}; shift 2 ;;
        --cache-dir) CACHE_DIR=${2:?}; shift 2 ;;
        --runtime-archive) RUNTIME_ARCHIVE=${2:?}; shift 2 ;;
        --runtime-sha256) RUNTIME_SHA256_OVERRIDE=${2:?}; shift 2 ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        --skip-runtime-exec) SKIP_RUNTIME_EXEC=1; shift ;;
        --list-targets) printf '%s\n' $KNOWN_TARGETS; exit 0 ;;
        --scan-only)
            [ -d "${2:-}" ] || die "--scan-only 디렉터리가 없습니다."
            if scan_forbidden "$2"; then echo "금지 항목 없음"; exit 0; fi
            exit 1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "알 수 없는 인자: $1" ;;
    esac
done
[ -n "$OS_ARG" ] && [ -n "$ARCH_ARG" ] || { usage >&2; die "--os와 --arch를 지정하십시오."; }
target_config "$OS_ARG" "$ARCH_ARG"

for tool in tar gzip sha256sum find grep; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool 명령이 필요합니다(개발 환경)."
done

# ---------------------------------------------------------------- 소스 상태
SOURCE_COMMIT=unknown
SOURCE_DIRTY=unknown
SOURCE_EPOCH=$(date +%s)
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    SOURCE_COMMIT=$(git -C "$ROOT" rev-parse HEAD)
    SOURCE_EPOCH=$(git -C "$ROOT" log -1 --format=%ct HEAD)
    SOURCE_DIRTY=no
    for f in "${APP_FILES[@]}" run-report.sh; do
        # 추적되지 않는(허용 목록 누락 포함) 파일도 커밋되지 않은 변경으로 본다.
        git -C "$ROOT" ls-files --error-unmatch -- "$f" >/dev/null 2>&1 || SOURCE_DIRTY=yes
    done
    if [ -n "$(git -C "$ROOT" status --porcelain -- "${APP_FILES[@]}" run-report.sh)" ]; then
        SOURCE_DIRTY=yes
    fi
fi
if [ "$SOURCE_DIRTY" != no ] && [ "$ALLOW_DIRTY" -eq 0 ]; then
    die "분석기·진입점에 커밋되지 않은 변경이 있거나 Git 상태를 확인할 수 없습니다. 커밋 후 빌드하거나 --allow-dirty를 지정하십시오."
fi
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$SOURCE_EPOCH}
BUILD_DATE=$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ)
R1_VERSION=$(sed -n "s/^VERSION = '\(.*\)'.*/\1/p" "$ROOT/aistordoctor.py" | head -n 1)
R2_VERSION=$(sed -n "s/^VERSION = '\(.*\)'.*/\1/p" "$ROOT/r2_health.py" | head -n 1)

# ---------------------------------------------------------------- 런타임 확보
RUNTIME_SHA256=${RUNTIME_SHA256_OVERRIDE:-$RUNTIME_SHA256_PINNED}
case $RUNTIME_SHA256 in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) die "대상 $TARGET_ID 의 런타임 SHA-256이 정해지지 않았습니다." ;;
esac
if [ -z "$RUNTIME_ARCHIVE" ]; then
    mkdir -p -- "$CACHE_DIR"
    RUNTIME_ARCHIVE=$CACHE_DIR/$RUNTIME_FILE
    if [ ! -f "$RUNTIME_ARCHIVE" ]; then
        echo "런타임 내려받기(개발 환경): $RUNTIME_URL"
        tmp_dl=$RUNTIME_ARCHIVE.part
        if command -v curl >/dev/null 2>&1; then
            curl -fL --retry 3 -o "$tmp_dl" "$RUNTIME_URL"
        elif command -v wget >/dev/null 2>&1; then
            wget -O "$tmp_dl" "$RUNTIME_URL"
        else
            die "curl·wget이 없습니다. --runtime-archive로 미리 받은 파일을 지정하십시오."
        fi
        mv -f -- "$tmp_dl" "$RUNTIME_ARCHIVE"
    fi
fi
[ -f "$RUNTIME_ARCHIVE" ] || die "런타임 파일이 없습니다: $RUNTIME_ARCHIVE"
actual=$(sha256sum -- "$RUNTIME_ARCHIVE" | cut -d' ' -f1)
[ "$actual" = "$RUNTIME_SHA256" ] || die "런타임 SHA-256 불일치: $actual (기대 $RUNTIME_SHA256)"
echo "런타임 확인: $(basename -- "$RUNTIME_ARCHIVE") sha256=$actual"

# ---------------------------------------------------------------- 스테이징
BUNDLE=$PACKAGE_NAME-$PACKAGE_VERSION-$TARGET_ID
WORK=$(mktemp -d "${TMPDIR:-/tmp}/aistor-build.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
STAGE=$WORK/$BUNDLE
mkdir -p -- "$STAGE/app" "$STAGE/runtime" "$STAGE/licenses"

install -m 0755 -- "$ROOT/run-report.sh" "$STAGE/run-report.sh"
for f in "${APP_FILES[@]}"; do
    install -m 0644 -- "$ROOT/$f" "$STAGE/app/$f"
done

tar -xzf "$RUNTIME_ARCHIVE" -C "$STAGE/runtime"
PYROOT=$STAGE/runtime/python
[ -d "$PYROOT" ] || die "런타임 압축 구조가 예상과 다릅니다(python/ 없음)."
PYLIB=$(find "$PYROOT/lib" -maxdepth 1 -type d -name 'python3.*' | head -n 1)
[ -n "$PYLIB" ] || die "런타임 표준 라이브러리 위치를 찾지 못했습니다."

# 분석에 필요 없는 개발·GUI·패키지 설치 도구를 제거한다(실행 중 설치 경로 차단).
if [ -f "$PYLIB/LICENSE.txt" ]; then
    install -m 0644 -- "$PYLIB/LICENSE.txt" "$STAGE/licenses/PYTHON-LICENSE.txt"
fi
rm -rf -- "$PYROOT/include" "$PYROOT/share" "$PYROOT/lib/pkgconfig" \
    "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" "$PYLIB/ensurepip" \
    "$PYLIB/lib2to3" "$PYLIB/venv" "$PYLIB/pydoc_data"
rm -rf -- "$PYLIB"/config-3.* "$PYLIB"/site-packages/pip* "$PYLIB"/site-packages/setuptools* \
    "$PYLIB"/lib-dynload/_tkinter* "$PYLIB"/lib-dynload/_test* "$PYLIB"/lib-dynload/xxlimited* \
    "$PYROOT"/lib/libtcl* "$PYROOT"/lib/libtk* "$PYROOT"/lib/tcl* "$PYROOT"/lib/tk* "$PYROOT"/lib/itcl* \
    "$PYROOT"/lib/thread[0-9]* "$PYROOT"/bin/pip* "$PYROOT"/bin/idle* "$PYROOT"/bin/2to3* \
    "$PYROOT"/bin/pydoc* "$PYROOT"/bin/python3*-config
[ -x "$PYROOT/bin/python3" ] || die "런타임 실행 파일(bin/python3)이 없습니다."

# 런타임이 요구하는 glibc 기호 버전이 대상 최소 glibc 이하인지 확인한다.
max_glibc=$(find "$PYROOT" -type f \( -name '*.so*' -o -path '*/bin/python3*' \) -print0 |
    xargs -0 grep -aoh 'GLIBC_2\.[0-9][0-9]*' 2>/dev/null | sed 's/GLIBC_//' | sort -t. -k1,1n -k2,2n -u | tail -n 1) || true
if [ -n "$max_glibc" ]; then
    lo=$(printf '%s\n%s\n' "$max_glibc" "$MIN_GLIBC" | sort -t. -k1,1n -k2,2n | head -n 1)
    [ "$lo" = "$max_glibc" ] || die "런타임이 glibc $max_glibc 기호를 요구합니다(대상 최소 $MIN_GLIBC)."
    echo "런타임 요구 glibc 최대 기호: $max_glibc (대상 최소 $MIN_GLIBC)"
else
    echo "런타임 glibc 기호를 확인하지 못했습니다(시험 런타임일 수 있음)."
fi

if [ "$SKIP_RUNTIME_EXEC" -eq 0 ] && [ "$(uname -m)" = "$TARGET_ARCH" ]; then
    got=$("$PYROOT/bin/python3" -E -s -B -c 'import sys, argparse, fcntl, gzip, hashlib, ipaddress, json, uuid, zlib; print("%d.%d.%d" % sys.version_info[:3])')
    [ "$got" = "$PBS_PYTHON" ] || die "런타임 버전 불일치: $got (기대 $PBS_PYTHON)"
    echo "런타임 실행 점검: Python $got"
fi

cat >"$STAGE/RELEASE-INFO" <<INFO
# aistor-diag-analyzer 배포 정보 (KEY=VALUE, run-report.sh가 읽음)
PACKAGE_NAME=$PACKAGE_NAME
PACKAGE_VERSION=$PACKAGE_VERSION
TARGET_ID=$TARGET_ID
TARGET_DESC=$TARGET_DESC
TARGET_ARCH=$TARGET_ARCH
TARGET_OS_IDS=$TARGET_OS_IDS
TARGET_OS_MAJORS=$TARGET_OS_MAJORS
MIN_GLIBC=$MIN_GLIBC
PYTHON_VERSION=$PBS_PYTHON
RUNTIME_SOURCE=python-build-standalone $PBS_RELEASE ($PBS_TRIPLE, install_only_stripped)
RUNTIME_ARCHIVE_SHA256=$RUNTIME_SHA256
SYSTEM_PYTHON_MIN=3.9
R1_TOOL_VERSION=$R1_VERSION
R2_TOOL_VERSION=$R2_VERSION
SOURCE_COMMIT=$SOURCE_COMMIT
SOURCE_DIRTY=$SOURCE_DIRTY
BUILD_DATE=$BUILD_DATE
LICENSE_STATUS=pending
INFO

cat >"$STAGE/licenses/NOTICE-ko.txt" <<NOTICE
라이선스 고지 (미확정)

- 분석기 소스(app/, run-report.sh)의 배포 라이선스는 아직 결정되지 않았습니다.
  외부 배포 전에 라이선스 정책을 확정해야 합니다.
- 동봉 런타임: CPython $PBS_PYTHON, python-build-standalone $PBS_RELEASE 배포본.
  CPython 라이선스 전문은 PYTHON-LICENSE.txt에 있습니다.
  이 런타임은 OpenSSL·zlib·SQLite·libffi 등 제3자 구성요소를 포함합니다.
  각 구성요소의 라이선스 고지 정리는 외부 배포 전 확인 항목으로 남아 있습니다.
NOTICE

cat >"$STAGE/README-ko.txt" <<README
MinIO AIStor 폐쇄망 진단 보고서 도구 $PACKAGE_VERSION ($TARGET_ID)
======================================================================

이 폴더는 설치하지 않고 바로 실행하는 배포물입니다.
Python 런타임이 포함되어 있으므로 서버에 Python·pip·패키지를 설치할 필요가 없습니다.
실행 중 인터넷·SUBNET·MinIO 서버에 접속하지 않으며, 서버 설정을 바꾸지 않습니다.
이미 수집된 진단 파일을 읽어 보고서만 만듭니다.

지원 환경
---------
- $TARGET_DESC
- glibc $MIN_GLIBC 이상, 일반 사용자 권한
- 그 밖의 OS·CPU에서는 실행 전에 거부하며 아무것도 설치하지 않습니다.

1. 반입과 압축 해제
-------------------
  sha256sum -c $BUNDLE.tar.gz.sha256
  tar -xzf $BUNDLE.tar.gz
  cd $BUNDLE

  - 실행 권한이 있는 위치(예: 홈 디렉터리)에 압축 해제하십시오.
    noexec로 마운트된 위치에서는 동봉 런타임을 실행할 수 없습니다.
  - 압축 해제한 폴더는 읽기 전용이어도 됩니다. 결과는 별도 위치에 저장됩니다.

2. 실행
-------
  # 사전 점검만(분석 실행 안 함)
  ./run-report.sh --check --verify-bundle

  # 공식 Markdown 단독
  ./run-report.sh --official-report "/경로/diag-report-....md"

  # 원본 gzip 결합(제한 보고서 저장 허용)
  ./run-report.sh --official-report "/경로/diag-report-....md" \\
                  --health "/경로/diag-data-....json.gz" --allow-partial

  - 기본 저장 위치: ~/aistor-diag-output  (변경: --out-dir <디렉터리>)
  - 같은 위치에 기존 결과가 있으면 --force가 필요합니다.
    --force는 이전 결과를 지우거나 덮어쓰지 않고 새 세대를 추가합니다.
  - 공백·한글이 들어간 경로는 따옴표로 감싸 지정하십시오.
  - 입력·출력 경로에 심볼릭 링크는 쓰지 말고 실제 경로를 지정하십시오.
  - 검증된 시스템 python3(3.9 이상)를 쓰려면 --use-system-python 을 지정합니다.
  - 전체 옵션: ./run-report.sh --help

3. 결과
-------
  <저장 위치>/run-<id>/aistor-diag-report.html   브라우저로 직접 열기(웹 서버 불필요)
  <저장 위치>/run-<id>/aistor-diag-report.md
  <저장 위치>/run-<id>/aistor-diag-report.json
  <저장 위치>/run-<id>/manifest.json             파일별 SHA-256
  <저장 위치>/CURRENT.json                       최신 결과 세대 포인터

  결과 폴더는 소유자 전용 권한(디렉터리 700, 파일 600)으로 저장됩니다.
  보고서는 마스킹을 적용하지만, 외부 공유 전에는 반드시 직접 검토하십시오.

4. 종료 코드
------------
  0  요청 범위 처리 성공(시스템이 정상이라는 판정은 아님)
  1  (R1) --fail-on-priority 조건에 해당하는 항목 있음
  2  입력 오류(파일 없음·형식·인코딩·크기 제한)
  3  마스킹·보호 저장 실패, 출력 위치 문제(기존 결과는 보존)
  4  부분 분석. --allow-partial을 지정했다면 제한 보고서가 저장되고 경로가 표시됨
  5  지원하지 않는 실행 환경(분석 미실행, 설치 시도 없음)

5. 해석 시 주의
---------------
- 공식 판정(official_status)과 이 도구의 자체 점검·해설은 서로 다른 것입니다.
- 미평가·미제공·수집 오류 항목은 정상 판정이 아닙니다.
- 부분 분석(4)은 자료가 부족하다는 뜻이며 오류를 숨기지 않습니다.

6. 배포물 정보
--------------
- 버전·대상·런타임·소스 커밋: RELEASE-INFO
- 배포물 내부 파일 체크섬: SHA256SUMS (./run-report.sh --verify-bundle 로 검증)
- 라이선스: licenses/ (분석기 라이선스는 미확정)
- 업데이트 시 새 버전을 다른 폴더에 압축 해제하고, 이전 버전 폴더와 결과는 그대로 보존하십시오.
README

# ---------------------------------------------------------------- 검사·체크섬·묶기
if ! scan_forbidden "$STAGE"; then
    die "금지 항목이 있어 배포물을 만들지 않았습니다."
fi

(cd -- "$STAGE" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z |
    xargs -0 sha256sum | sed 's#  \./#  #') >"$STAGE/SHA256SUMS"

find "$STAGE" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
mkdir -p -- "$OUT_DIR"
TARBALL=$OUT_DIR/$BUNDLE.tar.gz
[ ! -e "$TARBALL" ] || die "같은 이름의 배포물이 이미 있습니다(덮어쓰지 않음): $TARBALL"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH" \
    --mode='u+rwX,go+rX,go-w' -C "$WORK" -cf - "$BUNDLE" | gzip -n -9 >"$TARBALL.part"
mv -- "$TARBALL.part" "$TARBALL"
(cd -- "$OUT_DIR" && sha256sum -- "$BUNDLE.tar.gz" >"$BUNDLE.tar.gz.sha256")

echo "배포물: $TARBALL"
echo "크기: $(wc -c <"$TARBALL") bytes"
echo "SHA-256: $(cut -d' ' -f1 "$TARBALL.sha256")"
echo "소스 커밋: $SOURCE_COMMIT (dirty=$SOURCE_DIRTY)"
