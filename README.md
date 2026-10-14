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
