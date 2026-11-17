# 사용 안내·분석 범위

[README](../README.md)에서 옮긴 상세 설명입니다. 빌드·테스트는 [DEVELOPMENT.md](DEVELOPMENT.md), 날짜별 기록은 [HISTORY.md](HISTORY.md)를 보십시오.

## 현재 상태 요약 (2026-11-16)

| 항목 | 내용 |
|---|---|
| 최신 배포본 | 0.5.2 (`el8-x86_64`: RHEL·Rocky·AlmaLinux 8/9/10, x86_64). 런타임 동봉, 서버에 Python 설치 불필요 |
| 분석기 버전 | R1 `0.5.2-r1`(공식 Markdown), R2 `0.5.2-r2`(원본 gzip 결합, JSON 스키마 `1.3-r2`) |
| 검증 | Rocky 8·9·10 최소 컨테이너(Python 없음, 네트워크 차단, 비root), 2026-10-12 샘플, 2026-10-14 R3 실자료 |
| 라이선스 | 사내·고객사 전용(`LICENSE`). 런타임 제3자 고지는 배포본 `licenses/` |
| 개선 계획·상태 | [`docs/IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md) |
| 운영 최종 인수 | 미완료. 실제 RHEL·AlmaLinux·실서버 실행, aarch64·Ubuntu 빌드는 하지 않음 |

공식 판정(mc 보고서의 통과·오류·경고)과 이 도구의 자체 점검·해설은 별개입니다.
미평가·미제공·수집 오류 항목은 정상 판정이 아니며, 종료 코드 0은 시스템이 정상이라는 뜻이 아닙니다.

## 현재 기능

- 공식 요약과 원본에 포함된 개별 검사 결과 읽기
- 공식 상태와 분석기의 해설용 우선순위 구분
- HTML·Markdown·JSON 보고서와 SHA-256 manifest 생성
- HTML은 요약 대시보드·서버×드라이브 격자·검사 카드·설정 비교표·수집 오류 히트맵으로 표시(JS 없이도 전부 보이고 인쇄 시 전부 펼침)
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
THP 자체 점검은 공식 `sys.mem.thp` Resolution 권장값(enabled=madvise, defrag=defer+madvise 또는 madvise, max_ptes_none=0)과
관측값을 비교합니다(0.4.0부터, 이전 0.3.x는 never 통과 기준). 공식 판정이 우선하며 실제 지연·장애나 자동 조치를 의미하지 않습니다.
수집 오류는 고정 분류("대상 / 종류")로만 집계하고 원문은 내보내지 않습니다. `sys.errors`가 없으면 오류 행 수는 최소값으로 표시합니다.
IOMMU 옵션 부재를 off로 간주하지 않습니다. pool/set 인덱스만으로 quorum·물리 장애 도메인을 계산하지 않습니다.
인증서는 수집 시점 또는 명시한 `--as-of`에서 유효기간만 점검합니다. 신뢰체인·SAN·만료 임박 정책은 미지원입니다.
Markdown 결합 시 알려진 Server/Endpoint 표와 수집 시각(허용 차이 120초)을 비교합니다.
명백한 대상·시각 충돌은 거부합니다(종료 코드 2, 시각 차·대상 수와 조치를 표시). 일치해도 같은 배포·실행을 증명했다고 주장하거나 서로의 결측값을 채우지 않습니다.

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

