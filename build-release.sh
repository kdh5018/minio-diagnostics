#!/usr/bin/env bash
# 런타임 포함 폴더형 배포물(tar.gz)을 만든다. 개발 환경 전용 스크립트다.
#
#   ./build-release.sh --os el8 --arch x86_64
#
# - 분석기 소스(analyze.py 등)와 run-report.sh, 대상별 Python 런타임을 묶는다.
# - 런타임은 python-build-standalone(install_only_stripped)을 사용하고 SHA-256을 고정한다.
#   인터넷이 없는 빌드 환경에서는 --runtime-archive로 미리 받은 파일을 지정한다.
# - 런타임 제3자 라이선스 원문은 같은 릴리스의 full 배포판(python/licenses/)에서 가져온다.
#   stripped 배포판에는 CPython 라이선스만 들어 있기 때문이다. SHA-256을 고정하며,
#   미리 받은 파일은 --license-archive, 이미 풀어 둔 폴더는 --license-dir로 지정한다.
#   어느 경우든 원문 파일마다 고정된 SHA-256(LICENSE_MANIFEST)과 대조한다.
# - 고객·테스트 서버에서 실행하는 스크립트가 아니다. 서버에는 결과 tar.gz만 반입한다.
# - 진단 원본·보고서·.git·인증정보·테스트 잔여물·백업·내부 계약서는 포함하지 않으며,
#   묶기 전에 금지 목록 검사를 다시 수행한다.
#
# 대상 추가 방법: 아래 TARGET_TABLE에 한 줄을 추가한다(다른 코드는 고치지 않는다).
#   필요한 값은 런타임 triple, 해당 런타임 파일과 full 배포판의 SHA-256,
#   지원 OS ID·주 버전, 최소 glibc다. full 배포판이 새 것이면 LICENSE_MANIFEST에
#   그 배포판의 python/licenses/ 파일별 SHA-256도 추가한다. 점검 목록은 docs/IMPROVEMENT_PLAN.md.

set -euo pipefail
umask 022

readonly PACKAGE_NAME=aistor-diag-analyzer
readonly PACKAGE_VERSION=0.5.8
readonly PBS_RELEASE=20261110
readonly PBS_PYTHON=3.12.16
readonly APP_FILES=(analyze.py aistordoctor.py r2_health.py r2_enrichment.py)
# 배포물에 반드시 들어가야 하는 런타임 제3자 라이선스 원문(full 배포판 python/licenses/).
readonly REQUIRED_RUNTIME_LICENSES=(LICENSE.cpython.txt LICENSE.openssl-3.txt LICENSE.zlib.txt
    LICENSE.libffi.txt LICENSE.sqlite.txt LICENSE.expat.txt LICENSE.bzip2.txt LICENSE.liblzma.txt
    LICENSE.mpdecimal.txt)

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
  --license-archive <파일>  미리 받은 full 배포판(.tar.zst)에서 제3자 라이선스 원문 추출
  --license-dir <디렉터리>  이미 풀어 둔 python/licenses 폴더 사용(파일별 고정 SHA-256과 대조)
  --allow-unverified-licenses
                            라이선스 원문이 고정 SHA-256과 다르거나 대조 목록이 없어도 빌드
                            (시험용. RELEASE-INFO에 THIRD_PARTY_LICENSE_VERIFIED=no로 기록)
  --allow-dirty             커밋되지 않은 분석기 변경이 있어도 빌드(배포 정보에 dirty 기록)
  --skip-runtime-exec       빌드 환경에서 런타임 실행 점검 생략(다른 CPU 대상 빌드용)
  --list-targets            지원 대상 목록(ID·설명·OS·glibc·triple·라이선스 대조 목록 여부)
  --scan-only <디렉터리>    금지 항목 검사만 실행
USAGE
}

# ---------------------------------------------------------------- 대상 정의
# 한 줄 = 한 대상. 열(| 구분): 대상ID|설명|OS ID(공백 구분)|OS 주 버전|최소 glibc|런타임 triple|
#   런타임(install_only_stripped) SHA-256|full 배포판(라이선스 원문 출처) SHA-256
# el8-x86_64: 런타임은 glibc 2.17 기준 빌드이므로 EL8(glibc 2.28) 이상에서 동작한다.
# 미검증 예시(추가 전 SHA-256 확인·실환경 검증 필요, docs/IMPROVEMENT_PLAN.md 점검 목록 참고):
#   el8-aarch64|RHEL·Rocky Linux·AlmaLinux 8/9/10 (aarch64)|rhel rocky almalinux|8 9 10|2.28|aarch64-unknown-linux-gnu|<확인 필요>|<확인 필요>
#   ubuntu22-x86_64|Ubuntu 22.04/24.04 (x86_64)|ubuntu|22 24|2.35|x86_64-unknown-linux-gnu|6a9e62da...|a1b98760...
TARGET_TABLE='
el8-x86_64|RHEL·Rocky Linux·AlmaLinux 8/9/10 (x86_64)|rhel rocky almalinux|8 9 10|2.28|x86_64-unknown-linux-gnu|6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39|a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af
'

# full 배포판별 python/licenses/ 원문 파일 SHA-256. 형식: <full 배포판 SHA-256> <파일 SHA-256>  <파일명>
LICENSE_MANIFEST='
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 3bdff06e69991c94664f2ef5c5f8096f60b7dbec071756ea6cc26b445e06ec5b  LICENSE.bdb.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 1f38bbc7caacafd65169276d759c0d88c991b753b643ce35d0e45ea1971dd441  LICENSE.bzip2.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 86e61415828a8b5b06ec8d024e6f086ce155a8b85fd0c419c0ba4dc004e74fdd  LICENSE.cpython.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 122f2c27000472a201d337b9b31f7eb2b52d091b02857061a8880371612d9534  LICENSE.expat.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 2daec087a88e7c9b8082557cdeebad5bbb8155a4137472f0b22e269cd99d0c1e  LICENSE.libX11.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 56abe29bb1d9806a9e04fa9f80fed2c0f18027594df3f098148d814aef6bddfa  LICENSE.libXau.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 29cea33c32bbc9785142386377915612a2fa786482c46843383384aded2e09b1  LICENSE.libedit.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af deaf3a42effb551a5b140fa9afefed183a27f1341c6d1bf430d106a5e6931fc0  LICENSE.libffi.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 9a4062de0a2c388a98cf35a35d348b62fa97c838a71c3c28ee1a2d7d0a565b02  LICENSE.liblzma.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 122ee1f7e258f2c3c0e538a75c037684f420454bf3850ddc74ce750bbf5fe86b  LICENSE.libuuid.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af c5ffbfeaa501071ceeb97b7de2c0d703fdaa35de01c0fb6cbac1c28453a3e9fd  LICENSE.libxcb.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 669512af7219f58be03a398766d7c9da11a3b3df9d3f05cb74c5ceca25c8da3b  LICENSE.mpdecimal.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 87a4c4442337b8968ef956031c406b74f9cb7149b7ba87311bdaba534816201c  LICENSE.ncurses.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a  LICENSE.openssl-3.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 38bef3d28b24f145ea293bd3b6eb4b20396982abc8303128fb493986ea5bc719  LICENSE.sqlite.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af c0a69a2bfd757361ec7e6143973b103c90409316b49e9c88db26ad6388e79f16  LICENSE.tcl.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 3ac5cdd0bef6c43ce34c6a7ced452081d9e5a0bf94082b9f9147d23ec9e214f5  LICENSE.tix.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 6c9f0d975b41afaa34d22f55bb8986ce69e5cb7ad327cb2b28820cd425edf5ee  LICENSE.zlib-ng.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af e32ff4e00d9d94930537635291da39e7e612703334bf6fde8c7f1686fe8a45a2  LICENSE.zlib.txt
a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af 7055266497633c9025b777c78eb7235af13922117480ed5c674677adc381c9d8  LICENSE.zstd.txt
'

target_rows() { printf '%s\n' "$TARGET_TABLE" | grep -v '^[[:space:]]*$' | grep -v '^#'; }
KNOWN_TARGETS=$(target_rows | cut -d'|' -f1 | tr '\n' ' ' | sed 's/ $//')

list_targets() {
    local id desc ids majors glibc triple rsha lsha manifest
    while IFS='|' read -r id desc ids majors glibc triple rsha lsha; do
        if printf '%s\n' "$LICENSE_MANIFEST" | grep -q "^$lsha "; then manifest=있음; else manifest=없음; fi
        printf '%s\n  설명: %s\n  OS: %s (주 버전 %s), 최소 glibc %s\n  런타임: %s, SHA-256 %s\n  라이선스 원문 출처 SHA-256: %s (파일별 대조 목록 %s)\n' \
            "$id" "$desc" "$ids" "$majors" "$glibc" "$triple" "$rsha" "$lsha" "$manifest"
    done < <(target_rows)
}

target_config() {
    TARGET_ID=$1-$2
    TARGET_OS_BASE=$1
    TARGET_ARCH=$2
    local row
    row=$(target_rows | awk -F'|' -v id="$TARGET_ID" '$1 == id' | head -n 1)
    [ -n "$row" ] || die "지원하지 않는 대상입니다: $TARGET_ID (가능: $KNOWN_TARGETS)"
    IFS='|' read -r _ TARGET_DESC TARGET_OS_IDS TARGET_OS_MAJORS MIN_GLIBC PBS_TRIPLE \
        RUNTIME_SHA256_PINNED LICENSE_SHA256_PINNED <<<"$row"
    [ "${TARGET_ARCH}" = "${PBS_TRIPLE%%-*}" ] || die "대상 표 오류: $TARGET_ID 의 triple($PBS_TRIPLE)이 CPU와 다릅니다."
    RUNTIME_FILE=cpython-${PBS_PYTHON}+${PBS_RELEASE}-${PBS_TRIPLE}-install_only_stripped.tar.gz
    RUNTIME_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${RUNTIME_FILE//+/%2B}
    LICENSE_FILE=cpython-${PBS_PYTHON}+${PBS_RELEASE}-${PBS_TRIPLE}-pgo+lto-full.tar.zst
    LICENSE_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${LICENSE_FILE//+/%2B}
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
LICENSE_ARCHIVE=
LICENSE_DIR=
ALLOW_DIRTY=0
SKIP_RUNTIME_EXEC=0
ALLOW_UNVERIFIED_LICENSES=0

while [ $# -gt 0 ]; do
    case $1 in
        --os) OS_ARG=${2:?}; shift 2 ;;
        --arch) ARCH_ARG=${2:?}; shift 2 ;;
        --out-dir) OUT_DIR=${2:?}; shift 2 ;;
        --cache-dir) CACHE_DIR=${2:?}; shift 2 ;;
        --runtime-archive) RUNTIME_ARCHIVE=${2:?}; shift 2 ;;
        --runtime-sha256) RUNTIME_SHA256_OVERRIDE=${2:?}; shift 2 ;;
        --license-archive) LICENSE_ARCHIVE=${2:?}; shift 2 ;;
        --license-dir) LICENSE_DIR=${2:?}; shift 2 ;;
        --allow-dirty) ALLOW_DIRTY=1; shift ;;
        --skip-runtime-exec) SKIP_RUNTIME_EXEC=1; shift ;;
        --allow-unverified-licenses) ALLOW_UNVERIFIED_LICENSES=1; shift ;;
        --list-targets) list_targets; exit 0 ;;
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
    for f in "${APP_FILES[@]}" run-report.sh LICENSE; do
        # 추적되지 않는(허용 목록 누락 포함) 파일도 커밋되지 않은 변경으로 본다.
        git -C "$ROOT" ls-files --error-unmatch -- "$f" >/dev/null 2>&1 || SOURCE_DIRTY=yes
    done
    if [ -n "$(git -C "$ROOT" status --porcelain -- "${APP_FILES[@]}" run-report.sh LICENSE)" ]; then
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
# 시스템 python3 최소 버전의 원본은 run-report.sh 한 곳이다(RELEASE-INFO·README-ko는 여기서 만든다).
SYSTEM_PYTHON_MIN=$(sed -n 's/^readonly DEFAULT_SYSTEM_PYTHON_MIN=\([0-9][0-9.]*\)$/\1/p' "$ROOT/run-report.sh")
[ -n "$SYSTEM_PYTHON_MIN" ] || die "run-report.sh에서 DEFAULT_SYSTEM_PYTHON_MIN을 찾지 못했습니다."

LICENSE_WORK=$(mktemp -d "${TMPDIR:-/tmp}/aistor-lic.XXXXXX")
WORK=$(mktemp -d "${TMPDIR:-/tmp}/aistor-build.XXXXXX")
FETCH_TMP=
trap 'rm -rf -- "$WORK" "$LICENSE_WORK"; [ -z "$FETCH_TMP" ] || rm -f -- "$FETCH_TMP"' EXIT

# ---------------------------------------------------------------- 런타임 확보
# 받는 동안은 캐시 폴더 안 임시 파일에 쓰고, 고정 SHA-256이 맞을 때만 최종 이름으로 옮긴다(같은 파일 시스템 mv).
# 실패·불일치면 임시 파일을 지우고 멈춘다. 중간에 끊겨도 EXIT trap이 지운다.
fetch() {  # fetch <url> <캐시 파일> <고정 SHA-256> <표시 접두어> <curl·wget이 없을 때 안내>
    local got ok=0
    FETCH_TMP=$(mktemp "$2.part.XXXXXX")
    echo "${4}내려받기(개발 환경): $1"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 -o "$FETCH_TMP" "$1" && ok=1
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$FETCH_TMP" "$1" && ok=1
    else
        die "curl·wget이 없습니다. $5"
    fi
    [ "$ok" -eq 1 ] || die "${4}내려받기 실패: $1 (캐시에 남기지 않음)"
    got=$(sha256sum -- "$FETCH_TMP" | cut -d' ' -f1)
    [ "$got" = "$3" ] || die "${4}내려받은 파일 SHA-256 불일치: $got (기대 $3, 캐시에 남기지 않음)"
    mv -f -- "$FETCH_TMP" "$2"
    FETCH_TMP=
}
RUNTIME_SHA256=${RUNTIME_SHA256_OVERRIDE:-$RUNTIME_SHA256_PINNED}
case $RUNTIME_SHA256 in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) die "대상 $TARGET_ID 의 런타임 SHA-256이 정해지지 않았습니다." ;;
esac
if [ -z "$RUNTIME_ARCHIVE" ]; then
    mkdir -p -- "$CACHE_DIR"
    RUNTIME_ARCHIVE=$CACHE_DIR/$RUNTIME_FILE
    if [ ! -f "$RUNTIME_ARCHIVE" ]; then
        fetch "$RUNTIME_URL" "$RUNTIME_ARCHIVE" "$RUNTIME_SHA256" '런타임 ' '--runtime-archive로 미리 받은 파일을 지정하십시오.'
    fi
fi
[ -f "$RUNTIME_ARCHIVE" ] || die "런타임 파일이 없습니다: $RUNTIME_ARCHIVE"
actual=$(sha256sum -- "$RUNTIME_ARCHIVE" | cut -d' ' -f1)
[ "$actual" = "$RUNTIME_SHA256" ] || die "런타임 SHA-256 불일치: $actual (기대 $RUNTIME_SHA256)"
echo "런타임 확인: $(basename -- "$RUNTIME_ARCHIVE") sha256=$actual"

[ -f "$ROOT/LICENSE" ] || die "저장소 최상위 LICENSE 파일이 없습니다."

# ---------------------------------------------------------------- 제3자 라이선스 원문 확보
# zstd 해제: zstd → unzstd → zstdcat → Python zstandard 모듈 → Python 3.14+ compression.zstd 순서로 찾는다.
PY_ZSTD_EXTRACT='
import sys, tarfile
try:
    from compression import zstd
    opener = lambda p: zstd.open(p)
except ImportError:
    import zstandard
    opener = lambda p: zstandard.ZstdDecompressor().stream_reader(open(p, "rb"))
with opener(sys.argv[1]) as f, tarfile.open(fileobj=f, mode="r|") as t:
    for m in t:
        rest = m.name[len("python/licenses/"):]
        if m.isfile() and m.name.startswith("python/licenses/") and "/" not in rest:
            t.extract(m, sys.argv[2], filter="data")
'
extract_licenses() {  # extract_licenses <.tar.zst> <대상 디렉터리>
    local tool
    for tool in zstd unzstd zstdcat; do
        if command -v "$tool" >/dev/null 2>&1; then
            case $tool in zstd|unzstd) "$tool" -dc -- "$1" ;; zstdcat) zstdcat -- "$1" ;; esac |
                tar -xf - -C "$2" --wildcards 'python/licenses/LICENSE.*'
            echo "zstd 해제 도구: $tool"
            return 0
        fi
    done
    if command -v python3 >/dev/null 2>&1; then
        if python3 -I -c 'import compression.zstd' >/dev/null 2>&1 || python3 -I -c 'import zstandard' >/dev/null 2>&1; then
            python3 -I -c "$PY_ZSTD_EXTRACT" "$1" "$2"
            echo "zstd 해제 도구: python3"
            return 0
        fi
    fi
    die "zstd 해제 도구가 없습니다. 다음 중 하나가 필요합니다(개발 환경): zstd·unzstd·zstdcat 명령, Python zstandard 모듈, Python 3.14 이상. 또는 --license-dir로 풀어 둔 python/licenses 폴더를 지정하십시오."
}

# 원문 폴더의 LICENSE.*.txt 집합과 파일별 SHA-256이 고정 목록과 정확히 같은지 대조한다.
verify_licenses() {  # verify_licenses <디렉터리> <full 배포판 SHA-256>
    local dir=$1 expected actual
    expected=$(printf '%s\n' "$LICENSE_MANIFEST" | awk -v k="$2" '$1 == k {print $2 "  " $3}' | LC_ALL=C sort -k2)
    if [ -z "$expected" ]; then
        err "라이선스 원문 대조 목록이 없습니다(대상 $TARGET_ID, full 배포판 SHA-256 $2)."
        return 1
    fi
    actual=$(cd -- "$dir" && find . -maxdepth 1 -type f -name 'LICENSE.*.txt' -printf '%f\0' | LC_ALL=C sort -z | xargs -0 -r sha256sum | LC_ALL=C sort -k2)
    if [ "$expected" != "$actual" ]; then
        err "라이선스 원문 SHA-256이 고정 목록과 다릅니다(파일 누락·추가·내용 변경):"
        diff <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") | grep '^[<>]' | sed 's/^</  고정:/; s/^>/  실제:/' >&2 || true
        return 1
    fi
    return 0
}

case ${LICENSE_SHA256_PINNED:-} in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) [ -n "$LICENSE_DIR" ] && [ "$ALLOW_UNVERIFIED_LICENSES" -eq 1 ] ||
        die "대상 $TARGET_ID 의 full 배포판 SHA-256이 정해지지 않았습니다." ;;
esac
if [ -n "$LICENSE_DIR" ]; then
    [ -d "$LICENSE_DIR" ] || die "--license-dir 디렉터리가 없습니다: $LICENSE_DIR"
    LICENSE_SRC=$LICENSE_DIR
    LICENSE_ORIGIN="directory (reference: $LICENSE_FILE sha256=${LICENSE_SHA256_PINNED:-none})"
else
    if [ -z "$LICENSE_ARCHIVE" ]; then
        mkdir -p -- "$CACHE_DIR"
        LICENSE_ARCHIVE=$CACHE_DIR/$LICENSE_FILE
        if [ ! -f "$LICENSE_ARCHIVE" ]; then
            fetch "$LICENSE_URL" "$LICENSE_ARCHIVE" "$LICENSE_SHA256_PINNED" '' '미리 받은 파일을 옵션으로 지정하십시오.'
        fi
    fi
    [ -f "$LICENSE_ARCHIVE" ] || die "라이선스 원문 출처 파일이 없습니다: $LICENSE_ARCHIVE"
    actual=$(sha256sum -- "$LICENSE_ARCHIVE" | cut -d' ' -f1)
    [ "$actual" = "$LICENSE_SHA256_PINNED" ] || die "full 배포판 SHA-256 불일치: $actual (기대 $LICENSE_SHA256_PINNED)"
    extract_licenses "$LICENSE_ARCHIVE" "$LICENSE_WORK"
    LICENSE_SRC=$LICENSE_WORK/python/licenses
    LICENSE_ORIGIN="$LICENSE_FILE sha256=$LICENSE_SHA256_PINNED"
fi
for f in "${REQUIRED_RUNTIME_LICENSES[@]}"; do
    [ -s "$LICENSE_SRC/$f" ] || die "필수 제3자 라이선스 원문이 없습니다: $f"
done
if verify_licenses "$LICENSE_SRC" "${LICENSE_SHA256_PINNED:-none}"; then
    LICENSE_VERIFIED=yes
elif [ "$ALLOW_UNVERIFIED_LICENSES" -eq 1 ]; then
    LICENSE_VERIFIED=no
    LICENSE_ORIGIN="UNVERIFIED (per-file SHA-256 not matched; --allow-unverified-licenses) $LICENSE_ORIGIN"
    err "경고: 라이선스 원문을 검증하지 않고 빌드합니다(시험용). RELEASE-INFO에 기록됩니다."
else
    die "라이선스 원문 검증 실패. 고정된 full 배포판에서 추출하거나(--license-archive) 시험용이면 --allow-unverified-licenses를 지정하십시오."
fi
echo "제3자 라이선스 원문: $(cd -- "$LICENSE_SRC" && ls LICENSE.*.txt | wc -l)개 (검증=$LICENSE_VERIFIED, $LICENSE_ORIGIN)"

# ---------------------------------------------------------------- 스테이징
BUNDLE=$PACKAGE_NAME-$PACKAGE_VERSION-$TARGET_ID
STAGE=$WORK/$BUNDLE
mkdir -p -- "$STAGE/app" "$STAGE/runtime" "$STAGE/licenses/third-party"

install -m 0644 -- "$ROOT/LICENSE" "$STAGE/LICENSE"
for f in "$LICENSE_SRC"/LICENSE.*.txt; do
    install -m 0644 -- "$f" "$STAGE/licenses/third-party/$(basename -- "$f")"
done

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
# 0.5.1: 런타임에 딸려 온 바이트코드 캐시는 넣지 않는다. 아래에서 모든 파일 시각을 SOURCE_DATE_EPOCH로
# 맞추면 캐시가 원본보다 오래된 것으로 보여, 동봉 Python을 -B 없이 직접 실행할 때 다시 써져
# --verify-bundle이 체크섬 불일치로 실패했다. 분석기는 항상 -B로 실행하므로 캐시가 없어도 된다.
find "$PYROOT" -depth -type d -name '__pycache__' -exec rm -rf -- {} +
find "$PYROOT" -type f -name '*.pyc' -exec rm -f -- {} +

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
SYSTEM_PYTHON_MIN=$SYSTEM_PYTHON_MIN
R1_TOOL_VERSION=$R1_VERSION
R2_TOOL_VERSION=$R2_VERSION
SOURCE_COMMIT=$SOURCE_COMMIT
SOURCE_DIRTY=$SOURCE_DIRTY
BUILD_DATE=$BUILD_DATE
LICENSE_STATUS=proprietary
LICENSE=Proprietary - Copyright (c) 2026 WNYTech. All rights reserved. (internal and customer use only)
THIRD_PARTY_LICENSE_SOURCE=python-build-standalone $PBS_RELEASE $LICENSE_ORIGIN
THIRD_PARTY_LICENSE_VERIFIED=$LICENSE_VERIFIED
INFO

cat >"$STAGE/licenses/NOTICE-ko.txt" <<NOTICE
라이선스 및 제3자 고지
======================

1. 분석기(app/, run-report.sh 및 배포 안내 문서)
   Copyright (c) 2026 WNYTech. All rights reserved.
   WNYTech 사내 및 WNYTech가 제공한 고객사에서 제공 목적으로만 사용할 수 있으며,
   그 목적 외의 무단 복제·배포를 금지합니다. 전문: 배포물 최상위 LICENSE

2. 동봉 Python 런타임(runtime/python)
   CPython $PBS_PYTHON, python-build-standalone $PBS_RELEASE 배포판($PBS_TRIPLE).
   Python Software Foundation License Version 2 등 CPython 라이선스를 따릅니다.
   - PYTHON-LICENSE.txt         : 런타임에 들어 있는 CPython 라이선스 원문
   - third-party/LICENSE.*.txt  : 런타임 배포판(python/licenses/)에 들어 있는 원문 그대로

   런타임에 정적으로 포함된 주요 구성요소와 원문 파일:
   - CPython                     third-party/LICENSE.cpython.txt
   - OpenSSL 3                   third-party/LICENSE.openssl-3.txt
   - zlib / zlib-ng              third-party/LICENSE.zlib.txt, LICENSE.zlib-ng.txt
   - libffi                      third-party/LICENSE.libffi.txt
   - SQLite                      third-party/LICENSE.sqlite.txt
   - Expat                       third-party/LICENSE.expat.txt
   - bzip2                       third-party/LICENSE.bzip2.txt
   - XZ Utils(liblzma)           third-party/LICENSE.liblzma.txt
   - mpdecimal                   third-party/LICENSE.mpdecimal.txt
   - libuuid, ncurses, libedit, Berkeley DB, zstd 등 나머지 파일도 원문 그대로 포함

   Tcl/Tk·Tix·X11(libX11·libXau·libxcb) 관련 모듈은 배포물에서 제거했지만,
   원 배포판의 고지를 보존하기 위해 해당 원문도 함께 둡니다.
   출처: $LICENSE_ORIGIN
NOTICE

cat >"$STAGE/README-ko.txt" <<README
MinIO AIStor 폐쇄망 진단 보고서 도구 $PACKAGE_VERSION ($TARGET_ID)
======================================================================

이미 수집된 진단 파일을 읽어 HTML·Markdown·JSON 보고서를 만듭니다. 설치 없이 실행하며
Python 런타임이 동봉되어 있습니다. 인터넷·SUBNET·MinIO 서버에 접속하거나 서버 설정을 바꾸지 않습니다.
지원: $TARGET_DESC, glibc $MIN_GLIBC 이상, 일반 사용자 권한(그 밖의 환경은 실행 전 거부, 종료 코드 5).

1. 준비
-------
  sha256sum -c $BUNDLE.tar.gz.sha256
  tar -xzf $BUNDLE.tar.gz && cd $BUNDLE        # 실행 가능한 위치(예: 홈)에 해제, noexec 위치 불가
  - 압축 해제에 tar·gzip이 필요합니다(최소 설치 이미지에는 없을 수 있음). 없으면 다른 서버에서 풀어
    폴더째 복사하십시오. 읽기 전용 위치에 둔 폴더에서도 실행됩니다.
  - ./run-report.sh가 "Permission denied"(종료 126)로 바로 끝나면 해제 위치가 noexec이거나, SELinux
    제한 사용자(user_u)에 user_exec_content=off인 경우입니다(감사 로그에 남지 않을 수 있음).
    홈 등 실행 가능한 위치에 다시 해제하거나 실행이 허용된 계정을 쓰십시오.
  - SELinux enforcing(targeted 기본 정책)에서 별도 설정 없이 동작합니다. 정책·boolean을 바꾸지 마십시오.
  - 저장 위치는 권한 700을 지킬 수 있는 파일시스템이어야 합니다. Windows 공유 폴더처럼 권한이
    적용되지 않는 곳은 저장을 거부합니다(종료 코드 3).

  입력 파일은 일반 사용자 홈 아래 한 폴더에 둡니다. 원본은 읽기만 합니다.
    PC에서: scp diag-report-*.md diag-data-*.json.gz <계정>@<서버>:~/aistor-diag-input/
  - diag-report-<이름>_<시각>.md     공식 Markdown(--official-report)
  - diag-data-<이름>_<시각>.json.gz  원본 gzip(--health). 같은 수집이면 <시각>이 같습니다.

2. 실행
-------
  ./run-report.sh --check --verify-bundle                       # 사전 점검만
  ./run-report.sh --official-report "/경로/diag-report-....md" \
                  --health "/경로/diag-data-....json.gz" --allow-partial
  (--health 없이 공식 Markdown 단독, --official-report 없이 원본 gzip 단독도 가능)

  주요 옵션: --mask strict|basic(기본 strict), --fail-on-priority P1~P4, --as-of <시간대 포함 시각>,
  --lang ko, --out-dir <디렉터리>(기본 ~/aistor-diag-output), --force(기존 결과 보존, 새 세대 추가),
  --use-system-python(검증된 python3 $SYSTEM_PYTHON_MIN 이상). 전체: ./run-report.sh --help
  - 두 입력의 수집 시각 차가 120초를 넘거나 서버 대상이 다르면 결합을 거부합니다(종료 코드 2).
    로컬에서 다시 만든 Markdown은 Date가 생성 시각이라 거부될 수 있으니 같은 수집 파일을 쓰십시오.
  - 입력을 읽을 권한이 없으면 소유자 계정으로 실행하거나 읽을 수 있는 사본을 지정하십시오(원본 권한 변경 금지).
  - 공백·한글 경로는 따옴표로 감싸고, 심볼릭 링크 대신 실제 경로를 지정하십시오.

3. 결과
-------
  <저장 위치>/run-<id>/ 아래 aistor-diag-report.html(브라우저로 열기)·.md·.json, manifest.json(SHA-256).
  CURRENT.json은 최신 세대 포인터입니다. 폴더 700, 파일 600으로 저장됩니다.
  서버에 브라우저가 없으면 화면에 표시된 절대 경로의 run-<id> 폴더만 PC로 복사합니다.
      scp -r <계정>@<서버>:/home/<계정>/aistor-diag-output/run-<id> .
  (Windows 10 이상 PowerShell에 scp·sftp 기본 포함, WinSCP 등 승인 도구도 가능)
  .output.lock: 0.4.0까지 만들던 빈 잠금 파일입니다. 0.4.1부터는 디렉터리 잠금을 지원하지 않는
  일부 NFS에서만 만듭니다. 실행 중이 아닐 때 지워도 되며 옮길 필요가 없습니다.

4. 종료 코드
------------
  0 처리 성공(시스템 정상 판정 아님)   1 --fail-on-priority 조건 충족
  2 입력 오류(원인·조치 표시)           3 마스킹·보호 저장 실패(기존 결과 보존)
  4 부분 분석(--allow-partial이면 제한 보고서 저장)   5 지원하지 않는 실행 환경

5. 해석 시 주의
---------------
- 보고서의 판정·숫자는 mc 공식 판정만 씁니다. 원본 gzip 관측값은 판정 없이 근거로만 보입니다. 미제공·수집되지 않은 항목은 정상이 아닙니다.
- 인증서 남은 시간은 "수집 시각 기준"과 "보고서 생성 시각 기준"으로 표시합니다. 특정 시각은 --as-of로 봅니다.
- 서버 주소·IP·호스트명은 identifier-NNN으로 가립니다(표준 커널 경로·공개 문서 주소는 그대로).
  마스킹을 적용해도 외부 공유 전에는 직접 검토하십시오.
- 수집 오류 유형은 고정 분류이며 원인을 확정하지 않습니다. 행 수가 "최소 N"일 수 있습니다.

6. 배포물 정보
--------------
  RELEASE-INFO(버전·런타임·소스 커밋), SHA256SUMS(--verify-bundle로 검증),
  LICENSE(사내·고객사 전용), licenses/(런타임 제3자 고지).
  업데이트는 새 폴더에 해제하고 이전 폴더와 결과는 보존하십시오.
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
