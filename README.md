# MinIO 진단 분석기

MinIO AIStor의 진단 Markdown과 원본 diagnostics gzip을 읽는 오프라인 분석기입니다.
R1은 `0.1.1-r1`, R2는 `0.2.2-r2-candidate` 구현 후보입니다. 운영 최종 인수는 완료되지 않았습니다.
분석 환경에 AIStor·mc 설치나 서버 접속은 필요 없습니다. 버전 정보는 가능한 경우 자동 추출하며 필수 사용자 입력이 아닙니다.

## 현재 기능

- 공식 요약과 원본에 포함된 개별 검사 결과 읽기
- 공식 상태와 분석기의 해설용 우선순위 구분
- HTML·Markdown·JSON 보고서와 SHA-256 manifest 생성
- 실행마다 새 보고서 세대를 만들고 소유자 전용 접근권한 적용
- 기존 보고서를 보존하면서 `CURRENT.json`으로 최신 세대 지정
- 비밀값·식별자 마스킹 적용. 공유 전 수동 검토 필요
- Info·Resolution의 표·목록·코드를 구조화하고 원본 줄 번호 보존
- 고유한 공식 검사 ID에 기반한 항목 식별자와 결과 연결
- 입력 버전·대상·시각을 모를 때 추정하지 않고 결측 이유 표시

MinIO에 접속하거나 보고서 안의 명령을 실행하지 않습니다.
통과 건수만으로 개별 검사 결과를 만들어내지 않습니다.
R2는 gzip의 단일 JSON 또는 헤더+진단 JSON을 지원합니다. 형식 버전이 낯설어도 알려진 구조를 분석합니다.
드라이브 상태·용량 카운터·버전 일치·인증서 유효기간·THP·명시적 IOMMU 옵션·제공된 수집 오류를 점검합니다.
시점 비교와 메트릭·로그 운영 분석은 아직 지원하지 않습니다.

## 실행 환경

Python 표준 라이브러리만 사용하므로 별도 패키지 설치가 필요하지 않습니다.
R1은 Linux/WSL의 Python 3.11·3.14 검증 기록이 있습니다. R2는 현재 Docker Python 3.11에서만 실행 검증했습니다.

보고서 저장에는 소유자 전용 권한을 적용할 수 있는 POSIX 파일시스템이 필요합니다.
Windows 마운트 경로 대신 Linux 홈 아래의 보호 디렉터리를 사용하세요.
Windows 네이티브 환경의 보고서 저장은 지원하지 않습니다.

```bash
python3 analyze.py --check-env
python3 analyze.py --official-report /path/to/approved-report.md --mask strict
```

기본 저장 경로는 `~/minio-airgap-diag-output`입니다.
`AISTOR_DIAG_OUTPUT_DIR` 환경변수 또는 `--out-dir` 옵션으로 변경할 수 있으며,
`--out-dir`이 우선합니다.

결과는 `run-<uuid>/`에 저장됩니다. 기존 결과가 있을 때 `--force`를 추가하면
이전 세대는 보존하고, 새 파일 검증 후 `CURRENT.json`만 갱신합니다.
종료 코드 `0`은 분석 처리 성공을 뜻하며, 운영 시스템이 정상이라는 판정은 아닙니다.

생성된 `aistor-diag-report.html`을 브라우저에서 열면 됩니다.
화면에 필요한 요소가 내장되어 있어 웹 서버는 필요하지 않습니다.
Windows에서는 다음 WSL 경로로 열 수 있습니다.

`\\wsl.localhost\<distribution>\home\<user>\minio-airgap-diag-output\<generation>\aistor-diag-report.html`.

## R2 실행과 범위

```bash
python3 analyze.py --health /path/to/approved-data.json.gz --mask strict --force --allow-partial
python3 analyze.py --health /path/to/approved-data.json.gz --official-report /path/to/approved-report.md --mask strict --force --allow-partial
```

`--allow-partial`은 자료 부족 항목을 미평가로 남기고 제한 보고서를 저장하겠다는 명시입니다.
부분 분석은 저장에 성공해도 종료 코드 4입니다. 2는 입력 오류, 3은 저장·보호 실패입니다.
서버 버전만 없다는 이유로 지원되는 나머지 항목의 처리를 차단하지 않습니다.
원본 환경변수·라이선스·주소·에러 본문은 내보내지 않고 허용된 관측값과 JSON 위치만 보존합니다.
서버/드라이브 별칭은 해당 입력 안에서만 유효하며, 다른 시점과의 안정적인 대상 ID가 아닙니다.
용량 단위는 독립 검증하지 않았으므로 원본 카운터 범위만 비교하고 점유율·추세를 판정하지 않습니다.
THP 초기 정책은 never 통과, always/madvise 검토 경고입니다. 실제 지연·장애나 자동 조치를 의미하지 않습니다.
IOMMU 옵션 부재를 off로 간주하지 않습니다. pool/set 인덱스만으로 quorum·물리 장애 도메인을 계산하지 않습니다.
인증서는 수집 시점 또는 명시한 `--as-of`에서 유효기간만 점검합니다. 신뢰체인·SAN·만료 임박 정책은 미지원입니다.
Markdown 결합 시 알려진 Server/Endpoint 표와 수집 시각(허용 차이 120초)을 비교합니다.
명백한 대상·시각 충돌은 거부합니다. 일치해도 같은 배포·실행을 증명했다고 주장하거나 서로의 결측값을 채우지 않습니다.

## 테스트

```bash
python3 run_tests.py
AISTOR_DIAG_TEST_REPORT=/path/to/approved-report.md python3 run_tests.py
```

승인된 실제 자료가 없으면 실제 자료용 테스트 2개는 건너뛰며, 통과로 간주하지 않습니다.
자료는 환경변수로 지정하거나 로컬 `fixtures/official-report.md`에 둘 수 있습니다.

합성 테스트 결과는 보호 출력 경로 아래에 보존합니다.
합성 저장 단위 테스트는 권한 판정 부분만 모의 처리하므로 저장소 보안을 검증한 것으로
해석하면 안 됩니다. 실제 분석기의 권한 검사는 그대로 유지됩니다.

`0.1.1-r1`에서는 WSL Python 3.14, 기존 실행기 Python 3.11,
Hermes Docker Python 3.11에서 각각 **44개 통과·실패 0개·보류 0개**를 확인했습니다.
저장 단계별 실패 주입과 실제 프로세스 간 동시 저장 충돌 검사를 포함합니다.
상세 R1 범위와 남은 인수 조건은 [R1 검증 현황](R1_ACCEPTANCE.md)을 참고하세요.
R2 인수 점검 후 Docker에서는 승인된 실제 자료와 합성 사례로 78개 테스트가 통과했습니다.
JSON 객체를 만들기 전에도 구조 토큰·긴 문자열·긴 스칼라를 제한하며, 디코딩 후 정확한 노드·문자열 한도를 재검사합니다.
형식 헤더와 제품 버전이 모두 없어도 알려진 본문 구조의 다른 점검을 차단하지 않는 회귀 사례를 포함합니다.
R2의 WSL·Windows Chrome 실행은 아직 확인하지 않았으며, R1의 통과 기록을 대신 적용하지 않습니다.

```bash
AISTOR_DIAG_TEST_HEALTH=/path/to/approved-data.json.gz AISTOR_DIAG_TEST_REPORT=/path/to/approved-report.md python3 -m unittest -v run_tests test_r2
```

실제 자료 테스트는 지정된 기준 샘플(서버 3개·드라이브 기록 12개·공식 요약 31/2/2)에 대한 회귀입니다.
자료가 없으면 해당 테스트는 건너뛰며 통과로 취급하지 않습니다. 다른 버전·규모 자료의 지원 완료 근거가 아닙니다.

### 선택적 브라우저 검증

기존 Node.js·Playwright·Chrome 검증 환경이 있는 경우 다음 스크립트로
실제 보고서와 악성 입력으로 만든 합성 보고서를 검사할 수 있습니다.
분석기 실행에는 이 도구들이 필요하지 않습니다.

```text
node verify_browser.cjs <실제-보고서.html> <기존-보호-검증-디렉터리> <합성-악성입력-보고서.html>
```

스크립트는 검증 디렉터리에 화면 이미지와 `result.json`을 저장합니다.
HTTP 요청을 차단하면서 시도 여부를 기록하고, 필터·검색·키보드 펼치기·인쇄 표시·
모바일 가로 넘침·HTML 주입을 확인합니다. 공개 저장소에 실제 보고서를 올리지 않습니다.

## 파일 전용 보고서 요구사항 확인 및 화면 보완

기존 R1/R2 구현을 재사용했습니다. 별도 분석기나 새로운 장애 추론 규칙은 추가하지 않았습니다.

- 구현·기존 기능 유지: 공식 Markdown 단독 입력, 선택적 gzip 결합, 헤더 JSON+진단 JSON 읽기,
  공식 상태·Info·Resolution·근거 보존, 검색·상태·영역 필터, 근거 펼치기,
  단일 파일 HTML, Python 표준 라이브러리 실행, 보호 세대 저장·manifest·체크섬.
- 이번 보완: HTML에 서버·드라이브 관측 표와 수집 영역별 오류 기록 수를 별도 표시합니다.
  공식 오류·경고와 원본 수집 오류는 합산하지 않습니다. 결측값은 미제공/미평가로 표시합니다.
  추가 표는 관측값 표시만 수행하고 기존 판정 모델을 변경하지 않습니다.
- 제한: 지원되는 sys 배열의 직접 error/errors 필드에 오류가 기록된 **행 수**를 집계합니다.
  한 행의 여러 오류 메시지를 각각 센 총 오류 수가 아니며, 오류 본문은 출력하지 않습니다.
  서버·드라이브 별칭과 기록 수는 물리 장애 도메인을 의미하지 않습니다.
- 기존 자체 R2 규칙은 공식 결과와 분리된 표시를 유지합니다. THP·TLS 등의 자체 정책은
  공식 mc 판정을 대체하지 않습니다. 새로운 진단 규칙은 이번 보완 범위에 포함하지 않습니다.
- SUBNET 분석의 재현은 목표가 아닙니다. mc와 SUBNET의 검사 집합·개수를 덮어쓰지 않습니다.

### 실제로 실행한 명령 형식

승인된 파일을 지정하고 프로젝트 디렉터리에서 실행합니다. REPORT와 HEALTH는 같은 수집의
공식 Markdown 및 원본 gzip 경로로 바꾸십시오. 아래 변수 자리에는 사용자의 실제 파일 경로를
지정해야 합니다. 라이브 서버·mc·인터넷·웹 서버는 필요하지 않습니다.

```bash
REPORT='/승인된/공식/보고서.md'
HEALTH='/승인된/원본/진단.json.gz'
OUT='/home/user/minio-airgap-diag-output'

# 공식 보고서 단독: 검증 샘플에서는 종료 코드 0
PYTHONDONTWRITEBYTECODE=1 python3 analyze.py \
  --official-report "$REPORT" --out-dir "$OUT" --mask strict --force

# 원본 결합: 제한 보고서는 저장 후 종료 코드 4일 수 있음
PYTHONDONTWRITEBYTECODE=1 python3 analyze.py \
  --official-report "$REPORT" --out-dir "$OUT" --mask strict --force \
  --health "$HEALTH" --allow-partial

# 원본을 저장소에 복사하지 않고 실제 자료 테스트
PYTHONDONTWRITEBYTECODE=1 \
  AISTOR_DIAG_TEST_REPORT="$REPORT" AISTOR_DIAG_TEST_HEALTH="$HEALTH" \
  python3 -m unittest run_tests test_r2
```

`--force`는 이전 세대를 삭제하지 않고 최신 포인터만 새 검증 세대로 갱신합니다.
CLI가 출력한 새 run 디렉터리의 `aistor-diag-report.html`을 직접 여십시오.
WSL 배포 이름은 자신의 환경에서 확인하고 다음 경로의 `<distribution>`에 넣습니다.

```text
\\wsl.localhost\<distribution>\home\user\minio-airgap-diag-output\run-<uuid>\aistor-diag-report.html
```

### 이번 검증의 범위와 남은 조건

- 현재 Docker Python 3.11에서 변경 전 78개, 보완 후 83개 테스트 통과. 실패·오류·건너뜀 없음.
- 이전에 제공된 2026-10-12 실제 자료로 단독·결합 CLI를 실행했습니다. 새 세대 두 개의
  HTML·Markdown·JSON·manifest를 다시 읽고 공식 요약 31/2/2와 모든 공식 finding·check_results가
  단독 공식 입력 모델과 동일함을 확인했습니다.
- 해당 원본에서 CPU 영역 오류 기록 3행, 네트워크 영역 오류 기록 3행을 별도로 표시했습니다.
- 새 세대 디렉터리 0700, 파일 0600, 소유자와 manifest SHA-256를 확인했습니다.
  실행 전 원본 두 파일과 기존 보고서 48개 파일의 해시를 기록하고 생성 후 동일함을 확인했습니다.
- 네트워크 연결·DNS 함수를 차단한 파일 읽기·보고서 렌더링 검사에서 연결 시도 0건을 확인했습니다.
  HTML 외부 자산 태그 없음과 JavaScript 구문 검사도 확인했습니다. 이는 브라우저의 실제 요청
  관찰이나 OS 수준 완전한 네트워크 격리 시험을 대신하지 않습니다.
- 요청된 2026-10-14 R3 자료 경로는 현재 Docker에 마운트되어 있지 않았습니다.
  그 자료의 신규 검증·SUBNET 비교·원본 보존 검증은 미완료입니다. 과거 샘플을 R3 검증으로 간주하지 않습니다.
- Windows Chrome·Playwright·WSL 실행 연결이 현재 컨테이너에 없어 최신 HTML의 file:// 열람,
  필터·검색·펼치기·외부 요청 0건 브라우저 검증은 미실시입니다. 기존 R1 기록을 대신 적용하지 않습니다.
- 전체 요청의 인수 완료 조건은 R3 파일의 읽기 전용 접근과 Windows 브라우저 검증을 확보하는 것입니다.
  패키지 설치나 보호 권한 우회로 이를 대체하지 않습니다.

## 2026-11-16 현황: Python 없는 서버용 배포본

위 기록은 그대로 두고 이번 변경만 추가합니다. 분석 엔진(`analyze.py`·R1·R2)은 바꾸지 않았습니다.

### 배포본 실행 (`run-report.sh`)

고객 서버에 Python이나 패키지를 설치하지 않고, 런타임을 동봉한 폴더형 tar.gz를 풀어 실행합니다.

```bash
tar xzf aistor-diag-analyzer-<버전>-el8-x86_64.tar.gz
cd aistor-diag-analyzer-<버전>-el8-x86_64
./run-report.sh --check --verify-bundle
./run-report.sh --official-report /경로/공식보고서.md
./run-report.sh --official-report /경로/공식보고서.md --health /경로/원본.json.gz --allow-partial
```

- 스크립트 위치 기준으로 동작해 어느 디렉터리에서 호출해도 됩니다. 공백·한글 경로를 지원하고 `eval`을 쓰지 않습니다.
- 기본 출력 위치는 `~/aistor-diag-output`이며 `--out-dir`로 바꿉니다. 상대 경로는 호출한 위치 기준입니다.
- R2(`--health`)에서는 `--fail-on-priority`·`--lang`·`--mask basic`을 실행 전에 거부합니다(종료 코드 2).
- 분석기의 종료 코드 0~4를 그대로 돌려줍니다. 4(부분 분석)로 저장된 경우에도 결과 경로를 안내합니다.
  `--force`는 사용자가 지정한 경우에만 전달합니다.
- 사전 점검: OS(ID·주 버전)·CPU 아키텍처·glibc·동봉 런타임 실행(noexec 포함)·입력 파일·출력 폴더 권한(0700, 소유자)·
  심볼릭 링크를 확인합니다. 지원하지 않는 환경은 설치를 시도하지 않고 종료 코드 **5**로 안내합니다.
- `--use-system-python`은 검증된 시스템 python3(3.9 이상)를 쓸 때만 선택합니다. 기본은 동봉 런타임입니다.

### 배포본 빌드 (`build-release.sh`, 개발 환경 전용)

```bash
./build-release.sh --list-targets
./build-release.sh --os el8 --arch x86_64      # 결과: dist/
```

- 대상은 OS·아키텍처 인자로 고릅니다. 현재 활성 대상은 `el8-x86_64`(RHEL·Rocky·AlmaLinux 8/9/10, glibc 2.28 이상) 하나입니다.
  aarch64·Ubuntu는 스크립트 안의 예시 블록을 채워 추가합니다(미빌드·미검증).
- 런타임: python-build-standalone 20261110, CPython 3.12.16 (`install_only_stripped`), SHA-256 고정 검증.
  런타임 요구 glibc 최대 기호는 2.17입니다. 테스트·IDLE·tkinter·pip 등은 제거합니다.
- 커밋되지 않은 변경이 있으면 빌드를 거부합니다. tar.gz는 정렬·소유자 0·고정 mtime으로 결정적으로 만듭니다.
- 배포본에는 `run-report.sh`, `app/`(분석기 4개 파일), `runtime/`, `RELEASE-INFO`, `README-ko.txt`,
  `SHA256SUMS`, `licenses/`만 들어갑니다. `.git`, 인증정보, 진단 원본, 생성 보고서, 테스트·백업·로그,
  내부 계약서, 내부 경로 문자열이 있으면 빌드가 실패합니다.

### 이번 검증 결과 (2026-11-16)

- 테스트: 기존 83개 + 신규 `test_shell_release.py` 19개 = 102개.
  - WSL Python 3.14.4, 10월 12일 샘플과 배포본 지정 시: 102개 통과, 건너뜀 0.
  - 동봉 런타임(3.12.16)으로 Rocky 8.9·9.3 컨테이너: 102개 중 99개 통과, 3개 건너뜀
    (최소 이미지에 `find`·`xargs`가 없어 빌드 테스트 3개 제외). Rocky 10.2: 102개 통과, 건너뜀 0.
- 배포물: `aistor-diag-analyzer-0.3.0-el8-x86_64.tar.gz`, 25,158,944 bytes,
  SHA-256 `18522fe1620fb8ecbc92e83fad331c2646e338854b7f00e16037edd4ea1a1b1d`, 소스 커밋 `6215c29` (dirty=no).
  배포물은 저장소에 넣지 않습니다(`dist/`는 추적 제외).
- 깨끗한 환경: Docker `rockylinux:8-minimal`(8.9, glibc 2.28)·`9-minimal`(9.3, glibc 2.34)·
  `rockylinux/rockylinux:10-minimal`(10.2, glibc 2.39). `python3`·`python`·platform-python·python RPM 없음,
  `--network none`, 비root uid 1500. 이미지에는 tar·gzip만 추가했습니다(실서버 기본 포함 도구).
  - 세 환경 모두 `--check --verify-bundle` 0, 공식 단독 0, 원본 결합 `--allow-partial` 4(결과 경로 안내).
  - 생성 세대 0700, HTML·MD·JSON·manifest 0600, manifest SHA-256 일치.
  - 공식 요약 31/2/2, 서버 3, 드라이브 12, 수집 오류 기록 6행. 입력 경로는 공백·한글 포함 읽기 전용 마운트.
- 원본 보존: 10월 12일 샘플 두 파일의 SHA-256이 작업 전후 동일합니다.
- 브라우저(P5): Windows Chrome 157 + Playwright로 동봉 런타임이 만든 최신 결합 HTML을 `file://`로 열어
  `verify_browser.cjs`를 실행했습니다. 필터 18개, 검색, 키보드 펼치기, 인쇄 표시, 악성 마크업 합성 보고서,
  데스크톱·모바일 가로 넘침 없음, 외부 요청·페이지 오류·콘솔 오류 0건. 생성 보고서와 화면 이미지는 확인 후 삭제했습니다.

### 남은 항목

- P4: 2026-10-14 R3 자료를 받지 못해 미완료입니다. 다른 자료로 대체하지 않았습니다.
- 라이선스: 프로젝트 라이선스와 동봉 런타임의 제3자 고지 정리가 미결입니다(`LICENSE` 파일 없음,
  `RELEASE-INFO`의 `LICENSE_STATUS=pending`). 배포 전 결정이 필요합니다.
- 실제 RHEL·AlmaLinux와 실서버(Rocky 9.4) 실행은 하지 않았습니다. 컨테이너는 Rocky 최소 이미지 기준입니다.
- aarch64·Ubuntu 대상은 정의만 예시로 두었고 빌드·검증하지 않았습니다.

## Git 관리 범위

분석기 코드·테스트·공개 사용 안내만 추적합니다.
진단 원본, 생성 보고서, 로컬 운영 계약서, Router 연동 유틸리티,
인증정보, 로그, 백업은 로컬에 남깁니다.

`.gitignore`는 허용 목록 방식입니다. 커밋 전에 변경 내용과 허용 목록을 확인하세요.
자동 마스킹을 적용했더라도 실제 진단 자료와 토큰은 커밋하지 않습니다.
분석기 실행에는 GitHub 인증이 필요하지 않습니다.

## 커밋 메시지 규칙

`유형: 변경 내용` 형식으로 한글 한 줄을 작성합니다.

- `기능`: 기능 추가
- `수정`: 오류 수정
- `문서`: README 등 문서 변경
- `테스트`: 테스트 추가·보완
- `관리`: Git 설정 등 유지보수

예: `문서: README 한글화 및 커밋 규칙 추가`

제목에는 실제 변경만 짧게 적고, 서로 다른 목적의 변경은 별도 커밋으로 나눕니다.
