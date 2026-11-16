#!/usr/bin/env bash
# MinIO AIStor 폐쇄망 진단 보고서 실행 진입점.
#
# - 기존 Python 분석기(analyze.py)를 그대로 실행한다. 분석 로직을 쉘로 다시 구현하지 않는다.
# - 스크립트 위치를 기준으로 분석기와 런타임을 찾으므로 어느 디렉터리에서 실행해도 된다.
# - 네트워크 접속, 패키지 설치, 진단 수집, 서버 설정 변경을 하지 않는다.
# - eval을 사용하지 않으며, 모든 경로는 배열 인자로 그대로 전달한다.
# - 분석기 종료 코드 0~4를 바꾸지 않고 그대로 돌려준다.
#
# 쉘 진입점 자체의 종료 코드:
#   2  사용법·입력 파일 오류(분석기의 입력 오류와 같은 의미)
#   3  출력 위치 문제(분석기의 저장 실패와 같은 의미, 기존 결과는 건드리지 않음)
#   5  지원하지 않는 실행 환경(OS·CPU·glibc·런타임). 분석을 실행하지 않음

set -u
set -o pipefail
umask 077

readonly EXIT_USAGE=2
readonly EXIT_OUTPUT=3
readonly EXIT_ENV=5
# 소스(저장소) 실행 및 --use-system-python 에서 허용하는 시스템 python3 최소 버전.
readonly DEFAULT_SYSTEM_PYTHON_MIN=3.9

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
die() {
    local code=$1
    shift
    printf '%s\n' "$@" >&2
    exit "$code"
}

usage() {
    cat <<'USAGE'
사용법: run-report.sh [옵션]

MinIO AIStor 진단 결과 파일을 이 서버 안에서만 읽어 한국어 보고서(HTML·Markdown·JSON)를 만듭니다.
네트워크 접속·패키지 설치·진단 수집·서버 설정 변경을 하지 않습니다.

입력(둘 중 하나 이상):
  --official-report <파일>   공식 진단 Markdown(diag-report-*.md)
  --health <파일>            원본 진단 gzip(diag-data-*.json.gz). 지정하면 R2 분석
                             두 파일을 함께 주면 같은 수집(대상 일치, 시각 차이 120초 이내)일 때만 결합합니다.

출력:
  --out-dir <디렉터리>       보고서 저장 위치(기본: ~/aistor-diag-output, 소유자 전용 700)
  --force                    기존 결과가 있을 때 새 세대를 추가(이전 결과는 삭제하지 않음)
  --allow-partial            자료 부족 항목을 미평가로 남긴 제한 보고서 저장을 허용

분석 옵션(R1·R2 공통):
  --mask strict|basic        마스킹 수준(기본 strict). 원본 gzip은 항상 허용 필드만 추출
  --as-of <ISO8601 시각>     인증서 유효기간 판정 기준 시각(시간대 필수, 예: 2026-10-12T09:00:00+09:00)
  --lang ko                  보고서 언어(현재 ko만 지원)
  --fail-on-priority P1..P4  해당 우선순위 항목이 있으면 종료 코드 1(부분 분석 4가 우선)

실행 환경:
  --check                    실행 환경·입력·출력 위치만 점검하고 분석하지 않음
  --verify-bundle            배포물 내부 SHA256SUMS 검증 후 진행
  --use-system-python        동봉 런타임 대신 검증된 시스템 python3 사용
  --allow-unverified-os      검증 목록에 없는 OS 배포판에서 실행(CPU·glibc 점검은 유지)
  --version                  배포물 버전 정보 출력
  -h, --help                 이 도움말

예시:
  ./run-report.sh --official-report diag-report-xxx.md --out-dir ~/report-out
  ./run-report.sh --health diag-data-xxx.json.gz --out-dir ~/report-out --allow-partial
  ./run-report.sh --official-report diag-report-xxx.md --health diag-data-xxx.json.gz --out-dir ~/report-out --allow-partial
  ./run-report.sh --check --official-report diag-report-xxx.md

결과 보기:
  완료 후 표시되는 "HTML 보고서" 경로를 브라우저로 여십시오. 서버에 브라우저가 없으면
  결과 폴더를 승인된 방법으로 PC에 옮겨 여십시오(외부 접속이 필요 없는 단일 HTML 파일).

종료 코드:
  0 처리 성공(시스템 정상 판정 아님)  1 --fail-on-priority 조건 충족  2 입력 오류
  3 마스킹·저장 실패  4 부분 분석  5 지원하지 않는 실행 환경
USAGE
}

# 자주 틀리는 옵션 이름 안내(자동으로 바꾸지 않는다).
suggest_option() {
    case ${1%%=*} in
        --output-dir|--outdir|--output|--out|-o) printf '%s' '--out-dir' ;;
        --report|--md|--markdown|--official|--official-md) printf '%s' '--official-report' ;;
        --gz|--data|--health-data|--diag-data|--json) printf '%s' '--health' ;;
        --partial|--allow_partial) printf '%s' '--allow-partial' ;;
        --asof|--as_of|--at) printf '%s' '--as-of' ;;
        --fail-on|--priority) printf '%s' '--fail-on-priority' ;;
        --masking) printf '%s' '--mask' ;;
        -f|--overwrite) printf '%s' '--force' ;;
        -v|-V) printf '%s' '--version' ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------- 위치 확인
script_source=${BASH_SOURCE[0]}
if resolved=$(readlink -f -- "$script_source" 2>/dev/null) && [ -n "$resolved" ]; then
    script_source=$resolved
fi
SCRIPT_DIR=$(cd -P -- "$(dirname -- "$script_source")" && pwd -P) ||
    die "$EXIT_ENV" "오류: 스크립트 위치를 확인할 수 없습니다."

if [ -f "$SCRIPT_DIR/app/analyze.py" ]; then
    LAYOUT=bundle
    APP_DIR=$SCRIPT_DIR/app
elif [ -f "$SCRIPT_DIR/analyze.py" ]; then
    LAYOUT=source
    APP_DIR=$SCRIPT_DIR
else
    die "$EXIT_ENV" "오류: 분석기(analyze.py)를 찾을 수 없습니다: $SCRIPT_DIR" \
        "배포물을 압축 해제한 폴더 안의 run-report.sh를 실행하십시오."
fi
INFO_FILE=$SCRIPT_DIR/RELEASE-INFO
BUNDLED_PY=$SCRIPT_DIR/runtime/python/bin/python3

# RELEASE-INFO는 KEY=VALUE 텍스트다. source/eval 하지 않고 줄 단위로만 읽는다.
R_PACKAGE_VERSION=
R_TARGET_ID=
R_TARGET_ARCH=
R_TARGET_OS_IDS=
R_TARGET_OS_MAJORS=
R_MIN_GLIBC=
R_PYTHON_VERSION=
R_SYSTEM_PYTHON_MIN=
if [ -f "$INFO_FILE" ]; then
    while IFS='=' read -r key value || [ -n "$key" ]; do
        case $key in
            PACKAGE_VERSION) R_PACKAGE_VERSION=$value ;;
            TARGET_ID) R_TARGET_ID=$value ;;
            TARGET_ARCH) R_TARGET_ARCH=$value ;;
            TARGET_OS_IDS) R_TARGET_OS_IDS=$value ;;
            TARGET_OS_MAJORS) R_TARGET_OS_MAJORS=$value ;;
            MIN_GLIBC) R_MIN_GLIBC=$value ;;
            PYTHON_VERSION) R_PYTHON_VERSION=$value ;;
            SYSTEM_PYTHON_MIN) R_SYSTEM_PYTHON_MIN=$value ;;
        esac
    done <"$INFO_FILE"
elif [ "$LAYOUT" = bundle ]; then
    die "$EXIT_ENV" "오류: 배포 정보 파일(RELEASE-INFO)이 없습니다. 배포물이 손상되었을 수 있습니다."
fi
SYSTEM_PYTHON_MIN=${R_SYSTEM_PYTHON_MIN:-$DEFAULT_SYSTEM_PYTHON_MIN}

# ---------------------------------------------------------------- 인자 해석
REPORT=
HEALTH=
OUT_DIR=
OUT_DIR_SET=0
MASK=
AS_OF=
LANG_OPT=
FAIL_ON=
ALLOW_PARTIAL=0
FORCE=0
CHECK_ONLY=0
VERIFY_BUNDLE=0
USE_SYSTEM_PY=0
ALLOW_UNVERIFIED_OS=0

need_value() {
    # $1 옵션 이름, $2 남은 인자 수
    if [ "$2" -lt 2 ]; then
        die "$EXIT_USAGE" "사용법 오류: $1 옵션에 값이 필요합니다." "도움말: run-report.sh --help"
    fi
}

while [ $# -gt 0 ]; do
    case $1 in
        --official-report) need_value "$1" $#; REPORT=$2; shift 2 ;;
        --official-report=*) REPORT=${1#*=}; shift ;;
        --health) need_value "$1" $#; HEALTH=$2; shift 2 ;;
        --health=*) HEALTH=${1#*=}; shift ;;
        --out-dir) need_value "$1" $#; OUT_DIR=$2; OUT_DIR_SET=1; shift 2 ;;
        --out-dir=*) OUT_DIR=${1#*=}; OUT_DIR_SET=1; shift ;;
        --mask) need_value "$1" $#; MASK=$2; shift 2 ;;
        --mask=*) MASK=${1#*=}; shift ;;
        --as-of) need_value "$1" $#; AS_OF=$2; shift 2 ;;
        --as-of=*) AS_OF=${1#*=}; shift ;;
        --lang) need_value "$1" $#; LANG_OPT=$2; shift 2 ;;
        --lang=*) LANG_OPT=${1#*=}; shift ;;
        --fail-on-priority) need_value "$1" $#; FAIL_ON=$2; shift 2 ;;
        --fail-on-priority=*) FAIL_ON=${1#*=}; shift ;;
        --allow-partial) ALLOW_PARTIAL=1; shift ;;
        --force) FORCE=1; shift ;;
        --check) CHECK_ONLY=1; shift ;;
        --verify-bundle) VERIFY_BUNDLE=1; shift ;;
        --use-system-python) USE_SYSTEM_PY=1; shift ;;
        --allow-unverified-os) ALLOW_UNVERIFIED_OS=1; shift ;;
        --version)
            say "aistor-diag-analyzer ${R_PACKAGE_VERSION:-source} (${R_TARGET_ID:-$LAYOUT})"
            exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *)
            if hint=$(suggest_option "$1"); then
                die "$EXIT_USAGE" "사용법 오류: 알 수 없는 인자입니다: $1" "혹시 $hint 옵션을 의미했습니까?" "도움말: run-report.sh --help"
            fi
            case $1 in
                -*) die "$EXIT_USAGE" "사용법 오류: 알 수 없는 인자입니다: $1" "도움말: run-report.sh --help" ;;
                *) die "$EXIT_USAGE" "사용법 오류: 알 수 없는 인자입니다: $1" \
                    "입력 파일은 옵션과 함께 지정하십시오: --official-report <md> 또는 --health <gz>" "도움말: run-report.sh --help" ;;
            esac ;;
    esac
done

if [ "$CHECK_ONLY" -eq 0 ] && [ -z "$REPORT" ] && [ -z "$HEALTH" ]; then
    die "$EXIT_USAGE" "사용법 오류: --official-report 또는 --health 중 하나 이상을 지정하십시오." \
        "도움말: run-report.sh --help"
fi

MODE=R1
[ -z "$HEALTH" ] || MODE=R2
# 0.4.0부터 --lang, --fail-on-priority, --mask는 R1·R2 공통이다.
case $LANG_OPT in ''|ko) ;; *) die "$EXIT_USAGE" "사용법 오류: --lang은 ko만 지원합니다." ;; esac
case $FAIL_ON in ''|P1|P2|P3|P4) ;; *) die "$EXIT_USAGE" "사용법 오류: --fail-on-priority는 P1·P2·P3·P4 중 하나입니다." ;; esac
case $MASK in ''|strict|basic) ;; *) die "$EXIT_USAGE" "사용법 오류: --mask는 strict 또는 basic입니다." ;; esac
[ -n "$MASK" ] || MASK=strict

if [ "$OUT_DIR_SET" -eq 1 ] && [ -z "$OUT_DIR" ]; then
    die "$EXIT_USAGE" "사용법 오류: --out-dir 값이 비어 있습니다."
fi
if [ -z "$OUT_DIR" ]; then
    [ -n "${HOME:-}" ] || die "$EXIT_OUTPUT" "출력 오류: HOME이 설정되지 않았습니다. --out-dir로 저장 위치를 지정하십시오."
    OUT_DIR=$HOME/aistor-diag-output
fi
case $OUT_DIR in
    /*) ;;
    *) OUT_DIR=$PWD/$OUT_DIR ;;
esac
# 0.4.1: ../ 와 ./ 가 섞인 경로를 실제 절대 경로로 정리해 표시한다(가리키는 위치는 그대로).
# 상위 디렉터리가 있으면 그 물리 경로(cd -P)를 쓰고, 마지막 이름은 그대로 둔다(심볼릭 링크 거부 검사를 유지).
normalize_out_dir() {
    local path=$1 parent base resolved
    while [ "${#path}" -gt 1 ] && [ "${path%/}" != "$path" ]; do path=${path%/}; done
    base=$(basename -- "$path")
    parent=$(dirname -- "$path")
    case $base in
        .|..)
            if resolved=$(cd -P -- "$path" 2>/dev/null && pwd -P); then printf '%s' "$resolved"; return; fi ;;
        *)
            if resolved=$(cd -P -- "$parent" 2>/dev/null && pwd -P); then
                [ "$resolved" = / ] && resolved=
                printf '%s/%s' "$resolved" "$base"
                return
            fi ;;
    esac
    printf '%s' "$1"
}
OUT_DIR=$(normalize_out_dir "$OUT_DIR")

# ---------------------------------------------------------------- 사전 점검
CHECK_LINES=()
note() { CHECK_LINES+=("$1"); }

version_ge() {
    # version_ge A B : A >= B (숫자.숫자[.숫자])
    local IFS=.
    local -a a b
    read -r -a a <<<"$1"
    read -r -a b <<<"$2"
    local i x y
    for i in 0 1 2; do
        x=${a[i]:-0}
        y=${b[i]:-0}
        case $x in ''|*[!0-9]*) return 1 ;; esac
        case $y in ''|*[!0-9]*) return 1 ;; esac
        if [ "$((10#$x))" -gt "$((10#$y))" ]; then return 0; fi
        if [ "$((10#$x))" -lt "$((10#$y))" ]; then return 1; fi
    done
    return 0
}

word_in() {
    # word_in WORD "LIST"
    local w
    for w in $2; do
        [ "$w" = "$1" ] && return 0
    done
    return 1
}

read_os_release() {
    OS_ID=
    OS_VERSION_ID=
    OS_PRETTY=
    local file=/etc/os-release
    [ -r "$file" ] || file=/usr/lib/os-release
    [ -r "$file" ] || return 1
    local key value
    while IFS='=' read -r key value || [ -n "$key" ]; do
        value=${value%\"}
        value=${value#\"}
        value=${value%\'}
        value=${value#\'}
        case $key in
            ID) OS_ID=$value ;;
            VERSION_ID) OS_VERSION_ID=$value ;;
            PRETTY_NAME) OS_PRETTY=$value ;;
        esac
    done <"$file"
    return 0
}

glibc_version() {
    local out=
    if command -v getconf >/dev/null 2>&1; then
        out=$(getconf GNU_LIBC_VERSION 2>/dev/null) || out=
        out=${out#glibc }
    fi
    if [ -z "$out" ] && command -v ldd >/dev/null 2>&1; then
        out=$(ldd --version 2>/dev/null | head -n 1) || out=
        out=${out##* }
    fi
    case $out in
        [0-9]*.[0-9]*) printf '%s' "$out" ;;
        *) return 1 ;;
    esac
}

check_platform() {
    local kernel arch
    kernel=$(uname -s 2>/dev/null) || kernel=unknown
    arch=$(uname -m 2>/dev/null) || arch=unknown
    if [ "$kernel" != Linux ]; then
        die "$EXIT_ENV" "지원하지 않는 환경: Linux가 아닙니다($kernel). 분석을 실행하지 않았습니다." \
            "지원 대상 Linux 서버에서 실행하십시오."
    fi
    if [ "$LAYOUT" = bundle ]; then
        if [ "$arch" != "$R_TARGET_ARCH" ]; then
            die "$EXIT_ENV" "지원하지 않는 환경: CPU 아키텍처 $arch 은(는) 이 배포물($R_TARGET_ID, $R_TARGET_ARCH) 대상이 아닙니다." \
                "분석을 실행하지 않았고 아무것도 설치하지 않았습니다. 해당 아키텍처용 배포물을 요청하십시오."
        fi
        if ! read_os_release; then
            [ "$ALLOW_UNVERIFIED_OS" -eq 1 ] ||
                die "$EXIT_ENV" "지원하지 않는 환경: /etc/os-release를 읽을 수 없어 OS를 확인할 수 없습니다." \
                    "검증되지 않은 OS에서 실행하려면 운영자 책임으로 --allow-unverified-os를 지정하십시오."
        fi
        local major=${OS_VERSION_ID%%.*}
        if word_in "$OS_ID" "$R_TARGET_OS_IDS" && word_in "$major" "$R_TARGET_OS_MAJORS"; then
            note "OS: ${OS_PRETTY:-$OS_ID $OS_VERSION_ID} (지원 대상)"
        elif [ "$ALLOW_UNVERIFIED_OS" -eq 1 ]; then
            note "OS: ${OS_PRETTY:-${OS_ID:-unknown}} (검증 목록 외, --allow-unverified-os 지정)"
        else
            die "$EXIT_ENV" "지원하지 않는 환경: ${OS_PRETTY:-${OS_ID:-unknown} ${OS_VERSION_ID}}" \
                "이 배포물의 지원 대상: ID [$R_TARGET_OS_IDS], 주 버전 [$R_TARGET_OS_MAJORS], $R_TARGET_ARCH" \
                "분석을 실행하지 않았고 아무것도 설치하지 않았습니다." \
                "운영자 책임으로 시험하려면 --allow-unverified-os를 지정할 수 있습니다(CPU·glibc 점검은 유지)."
        fi
        local glibc
        if ! glibc=$(glibc_version); then
            die "$EXIT_ENV" "지원하지 않는 환경: glibc 버전을 확인할 수 없습니다(getconf·ldd 없음)." \
                "이 배포물은 glibc $R_MIN_GLIBC 이상이 필요합니다."
        fi
        if ! version_ge "$glibc" "$R_MIN_GLIBC"; then
            die "$EXIT_ENV" "지원하지 않는 환경: glibc $glibc < 필요 버전 $R_MIN_GLIBC." \
                "분석을 실행하지 않았습니다. 지원 대상 OS에서 실행하십시오."
        fi
        note "CPU: $arch, glibc: $glibc (필요 $R_MIN_GLIBC 이상)"
    else
        note "CPU: $arch (소스 실행, OS 목록 점검 생략)"
    fi
}

PY=
select_runtime() {
    local selftest='import sys, argparse, fcntl, gzip, hashlib, ipaddress, json, uuid, zlib
print("%d.%d.%d" % sys.version_info[:3])'
    local ver
    if [ "$USE_SYSTEM_PY" -eq 0 ] && [ "$LAYOUT" = bundle ]; then
        if [ ! -f "$BUNDLED_PY" ]; then
            die "$EXIT_ENV" "오류: 동봉 런타임이 없습니다: $BUNDLED_PY" \
                "배포물이 완전히 압축 해제되었는지 확인하십시오. 다른 설치는 시도하지 않습니다."
        fi
        if ! ver=$("$BUNDLED_PY" -E -s -B -c "$selftest" 2>/dev/null); then
            die "$EXIT_ENV" "오류: 동봉 런타임을 실행할 수 없습니다: $BUNDLED_PY" \
                "압축 해제 위치가 noexec로 마운트되었거나 실행 권한이 없을 수 있습니다." \
                "실행 가능한 위치(예: 홈 디렉터리)에 다시 압축 해제하십시오. 다른 설치는 시도하지 않습니다."
        fi
        if [ -n "$R_PYTHON_VERSION" ] && [ "$ver" != "$R_PYTHON_VERSION" ]; then
            die "$EXIT_ENV" "오류: 동봉 런타임 버전($ver)이 배포 정보($R_PYTHON_VERSION)와 다릅니다. 배포물을 다시 반입하십시오."
        fi
        PY=$BUNDLED_PY
        note "런타임: 동봉 Python $ver"
        return 0
    fi
    local candidate
    candidate=$(command -v python3 2>/dev/null) || candidate=
    if [ -z "$candidate" ]; then
        if [ "$LAYOUT" = bundle ]; then
            die "$EXIT_ENV" "오류: --use-system-python을 지정했지만 시스템 python3가 없습니다." \
                "옵션을 빼면 동봉 런타임을 사용합니다."
        fi
        die "$EXIT_ENV" "오류: 소스 실행에는 python3 $SYSTEM_PYTHON_MIN 이상이 필요합니다." \
            "Python이 없는 서버에서는 런타임 포함 배포물을 사용하십시오. 자동 설치는 하지 않습니다."
    fi
    if ! ver=$("$candidate" -E -s -B -c "$selftest" 2>/dev/null); then
        die "$EXIT_ENV" "오류: 시스템 python3($candidate)에 필요한 표준 모듈이 없거나 실행에 실패했습니다." \
            "동봉 런타임(배포물) 사용을 권장합니다."
    fi
    if ! version_ge "$ver" "$SYSTEM_PYTHON_MIN"; then
        die "$EXIT_ENV" "오류: 시스템 python3 $ver 은(는) 검증 범위($SYSTEM_PYTHON_MIN 이상)가 아닙니다." \
            "동봉 런타임을 사용하십시오(--use-system-python을 빼고 실행)."
    fi
    PY=$candidate
    note "런타임: 시스템 Python $ver ($candidate)"
}

verify_bundle() {
    [ "$LAYOUT" = bundle ] || die "$EXIT_USAGE" "사용법 오류: --verify-bundle은 배포물에서만 사용할 수 있습니다."
    [ -f "$SCRIPT_DIR/SHA256SUMS" ] || die "$EXIT_ENV" "오류: SHA256SUMS가 없습니다. 배포물이 손상되었을 수 있습니다."
    command -v sha256sum >/dev/null 2>&1 || die "$EXIT_ENV" "오류: sha256sum 명령이 없어 배포물을 검증할 수 없습니다."
    if ! (cd -- "$SCRIPT_DIR" && sha256sum --quiet --strict -c SHA256SUMS) >&2; then
        die "$EXIT_ENV" "오류: 배포물 체크섬이 일치하지 않습니다. 원래 배포물을 다시 반입하십시오."
    fi
    note "배포물 체크섬: 일치"
}

file_owner_mode() {
    # 소유자 계정·UID와 권한(8진수)만 보여 준다. 파일 내용은 읽지 않는다.
    # 계정 이름이 없는 UID(컨테이너 등)는 stat이 UNKNOWN으로 표시하므로 한국어로 바꾼다.
    local info name uid mode
    if info=$(stat -c '%U %u %a' -- "$1" 2>/dev/null); then
        read -r name uid mode <<<"$info"
        [ "$name" != UNKNOWN ] || name='이름 없는 계정'
        printf '소유자 %s(UID %s), 권한 %s' "$name" "$uid" "$mode"
    else
        printf '%s' '소유자·권한 확인 불가'
    fi
}

current_user() {
    # 계정 이름이 없는 UID(컨테이너 등)에서는 id -un이 실패하면서 숫자를 출력하므로 결과를 버린다.
    local name uid
    uid=$(id -u)
    name=$(id -un 2>/dev/null) || name=
    if [ -n "$name" ] && [ "$name" != "$uid" ]; then printf '%s(UID %s)' "$name" "$uid"; else printf 'UID %s(계정 이름 없음)' "$uid"; fi
}

blocked_parent() {
    # 경로의 상위 디렉터리 중 현재 사용자가 들어갈 수 없는(x 권한 없음) 첫 디렉터리를 출력한다.
    local dir
    dir=$(dirname -- "$1")
    local -a chain=()
    while :; do
        chain=("$dir" "${chain[@]}")
        if [ "$dir" = / ] || [ "$dir" = . ]; then break; fi
        dir=$(dirname -- "$dir")
    done
    for dir in "${chain[@]}"; do
        if [ -d "$dir" ] && [ ! -x "$dir" ]; then
            printf '%s' "$dir"
            return 0
        fi
    done
    return 1
}

check_input() {
    # check_input 라벨 경로 종류(md|gz)
    local label=$1 path=$2 kind=$3
    if [ -L "$path" ]; then
        local real
        real=$(readlink -f -- "$path" 2>/dev/null) || real='(확인 불가)'
        die "$EXIT_USAGE" "입력 오류: $label 이(가) 심볼릭 링크입니다: $path" \
            "실제 파일 경로를 직접 지정하십시오: $real"
    fi
    if [ ! -e "$path" ]; then
        local blocked
        if blocked=$(blocked_parent "$path"); then
            die "$EXIT_USAGE" "입력 오류: $label 파일의 상위 디렉터리에 들어갈 권한이 없습니다: $blocked ($(file_owner_mode "$blocked"))" \
                "현재 사용자: $(current_user)" \
                "디렉터리 소유자 계정으로 실행하거나, 읽을 수 있는 위치에 받은 사본을 지정하십시오. 이 도구는 권한을 바꾸지 않습니다."
        fi
        die "$EXIT_USAGE" "입력 오류: $label 파일이 없습니다: $path" "경로와 파일 이름(대소문자·확장자)을 확인하십시오."
    fi
    [ -f "$path" ] || die "$EXIT_USAGE" "입력 오류: $label 은(는) 일반 파일이 아닙니다: $path"
    if [ ! -r "$path" ]; then
        die "$EXIT_USAGE" "입력 오류: $label 파일을 읽을 권한이 없습니다: $path" \
            "  파일: $(file_owner_mode "$path") / 현재 사용자: $(current_user)" \
            "  파일 소유자 계정으로 실행하거나, 소유자에게 읽기 가능한 사본을 받아 지정하십시오." \
            "  원본 파일의 권한은 바꾸지 마십시오(이 도구도 바꾸지 않습니다)."
    fi
    local magic=
    if command -v od >/dev/null 2>&1; then
        magic=$(od -An -tx1 -N2 -- "$path" 2>/dev/null | tr -d ' \n') || magic=
    fi
    if [ -n "$magic" ]; then
        if [ "$kind" = gz ] && [ "$magic" != 1f8b ]; then
            die "$EXIT_USAGE" "입력 오류: $label 이(가) gzip 파일이 아닙니다: $path" \
                "--health에는 diag-data-*.json.gz를, 공식 Markdown(diag-report-*.md)은 --official-report로 지정하십시오."
        fi
        if [ "$kind" = md ] && [ "$magic" = 1f8b ]; then
            die "$EXIT_USAGE" "입력 오류: $label 에 gzip 파일을 지정했습니다: $path" \
                "원본 gzip(diag-data-*.json.gz)은 --health로, 공식 Markdown(diag-report-*.md)은 --official-report로 지정하십시오."
        fi
    fi
    note "입력($label): $path"
}

check_output() {
    local dir=$OUT_DIR
    if [ -L "$dir" ]; then
        die "$EXIT_OUTPUT" "출력 오류: 저장 위치가 심볼릭 링크입니다: $dir" \
            "실제 디렉터리 경로를 지정하십시오. 기존 파일은 변경하지 않았습니다."
    fi
    if [ -e "$dir" ]; then
        [ -d "$dir" ] || die "$EXIT_OUTPUT" "출력 오류: 저장 위치가 디렉터리가 아닙니다: $dir"
        local owner mode
        owner=$(stat -c %u -- "$dir" 2>/dev/null) || owner=
        mode=$(stat -c %a -- "$dir" 2>/dev/null) || mode=
        if [ "$owner" != "$(id -u)" ]; then
            die "$EXIT_OUTPUT" "출력 오류: 저장 위치의 소유자가 현재 사용자가 아닙니다: $dir" \
                "본인 소유의 새 경로를 --out-dir로 지정하십시오. 권한은 자동으로 변경하지 않습니다."
        fi
        case $mode in
            700|[0-7]700) ;;
            *) die "$EXIT_OUTPUT" "출력 오류: 저장 위치 권한이 소유자 전용(700)이 아닙니다: $dir (현재 $mode)" \
                "보고서 보호를 위해 소유자 전용 디렉터리가 필요합니다. 권한은 자동으로 변경하지 않습니다." \
                "직접 chmod 700 을 적용하거나, 존재하지 않는 새 경로를 --out-dir로 지정하십시오." ;;
        esac
        [ -w "$dir" ] && [ -x "$dir" ] || die "$EXIT_OUTPUT" "출력 오류: 저장 위치에 쓸 수 없습니다: $dir"
        if [ -e "$dir/CURRENT.json" ] && [ "$FORCE" -eq 0 ] && [ "$CHECK_ONLY" -eq 0 ]; then
            die "$EXIT_OUTPUT" "출력 안내: 이 위치에 기존 결과가 있습니다: $dir" \
                "새 결과 세대를 추가하려면 --force를 지정하십시오(이전 결과는 삭제·덮어쓰기 되지 않음)." \
                "또는 다른 --out-dir을 지정하십시오."
        fi
        note "출력: $dir (기존 디렉터리, 소유자 전용)"
    else
        local parent=$dir
        while [ ! -e "$parent" ]; do
            parent=$(dirname -- "$parent")
        done
        [ -d "$parent" ] || die "$EXIT_OUTPUT" "출력 오류: 상위 경로가 디렉터리가 아닙니다: $parent"
        [ -w "$parent" ] && [ -x "$parent" ] ||
            die "$EXIT_OUTPUT" "출력 오류: 저장 위치를 만들 권한이 없습니다(쓰기 불가: $parent)." \
                "쓰기 가능한 위치를 --out-dir로 지정하십시오(예: 홈 디렉터리 아래)."
        note "출력: $dir (새로 생성 예정, 권한 700)"
    fi
}

check_platform
[ "$VERIFY_BUNDLE" -eq 0 ] || verify_bundle
select_runtime
[ -z "$REPORT" ] || check_input "공식 Markdown" "$REPORT" md
[ -z "$HEALTH" ] || check_input "원본 gzip" "$HEALTH" gz
check_output

if [ "$CHECK_ONLY" -eq 1 ]; then
    say "사전 점검 통과 (분석은 실행하지 않음)"
    printf '  - %s\n' "${CHECK_LINES[@]}"
    exit 0
fi

# ---------------------------------------------------------------- 분석 실행
# -E: PYTHON* 환경변수 무시, -s: 사용자 site 무시, -B: .pyc를 쓰지 않음(읽기 전용 배포 위치 지원)
# -X utf8: 로케일과 무관하게 UTF-8 경로·출력 처리
args=("$APP_DIR/analyze.py")
[ -z "$REPORT" ] || args+=("--official-report=$REPORT")
[ -z "$HEALTH" ] || args+=("--health=$HEALTH")
args+=("--out-dir=$OUT_DIR" "--mask=$MASK")
[ -z "$AS_OF" ] || args+=("--as-of=$AS_OF")
[ "$ALLOW_PARTIAL" -eq 0 ] || args+=(--allow-partial)
[ "$FORCE" -eq 0 ] || args+=(--force)
[ -z "$LANG_OPT" ] || args+=("--lang=$LANG_OPT")
[ -z "$FAIL_ON" ] || args+=("--fail-on-priority=$FAIL_ON")

say "[$MODE] 분석 시작 (네트워크·설치 없음)"
printf '  - %s\n' "${CHECK_LINES[@]}"

output=$("$PY" -E -s -B -X utf8 "${args[@]}")
rc=$?
[ -z "$output" ] || printf '%s\n' "$output"

GENERATION=
while IFS= read -r line; do
    case $line in
        '결과 세트 확정: '*) GENERATION=${line#'결과 세트 확정: '} ;;
        'R2 결과 세트 확정: '*) GENERATION=${line#'R2 결과 세트 확정: '} ;;
    esac
done <<<"$output"

show_paths() {
    say "  결과 폴더: $GENERATION"
    say "  HTML 보고서(브라우저로 열기): $GENERATION/aistor-diag-report.html"
    say "  Markdown: $GENERATION/aistor-diag-report.md"
    say "  JSON: $GENERATION/aistor-diag-report.json"
    say "  manifest: $GENERATION/manifest.json"
    say "  최신 결과 포인터: $OUT_DIR/CURRENT.json"
    say "  서버에 브라우저가 없으면 결과 폴더를 승인된 방법으로 PC에 옮겨 HTML을 여십시오(외부 접속 불필요)."
    say "    예(PC에서 실행): scp -r <계정>@<서버>:$GENERATION ."
}

case $rc in
    0)
        say "완료(종료 코드 0): 요청 범위 처리 성공. 시스템 정상 판정은 아닙니다."
        [ -z "$GENERATION" ] || show_paths ;;
    1)
        say "완료(종료 코드 1): 보고서를 저장했고 --fail-on-priority $FAIL_ON 조건에 해당하는 항목이 있습니다."
        [ -z "$GENERATION" ] || show_paths ;;
    2)
        err "실패(종료 코드 2): 입력 오류. 위 안내(분석기 메시지)를 확인하십시오. 결과를 저장하지 않았습니다." ;;
    3)
        err "실패(종료 코드 3): 마스킹 검증 또는 보호 저장 실패. 기존 확정 결과는 보존됩니다."
        err "  저장 위치($OUT_DIR)의 소유자·권한(700)·충돌 여부를 확인하십시오." ;;
    4)
        if [ -n "$GENERATION" ]; then
            say "부분 분석(종료 코드 4): 자료 부족 항목을 미평가로 남긴 제한 보고서를 저장했습니다."
            say "  미평가·미제공 항목은 정상 판정이 아닙니다. 보고서의 제한 사항을 확인하십시오."
            show_paths
        else
            err "부분 분석(종료 코드 4): 결과를 확정하지 않아 저장하지 않았습니다."
            err "  제한 보고서를 저장하려면 --allow-partial을 추가하십시오."
        fi ;;
    *)
        err "실패(종료 코드 $rc): 분석기가 예상하지 못한 코드로 종료했습니다. 위 오류 메시지를 확인하십시오." ;;
esac
exit "$rc"
