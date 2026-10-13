# MinIO 진단 분석기

MinIO AIStor의 진단 Markdown을 읽어 보고서를 만드는 오프라인 분석기입니다.
현재는 **R1 구현 후보**이며, 전체 기능 개발과 운영 환경의 최종 인수는 완료되지 않았습니다.

## 현재 기능

- 공식 요약과 원본에 포함된 개별 검사 결과 읽기
- 공식 상태와 분석기의 해설용 우선순위 구분
- HTML·Markdown·JSON 보고서와 SHA-256 manifest 생성
- 실행마다 새 보고서 세대를 만들고 소유자 전용 접근권한 적용
- 기존 보고서를 보존하면서 `CURRENT.json`으로 최신 세대 지정
- 비밀값·식별자 마스킹 적용. 공유 전 수동 검토 필요

MinIO에 접속하거나 보고서 안의 명령을 실행하지 않습니다.
통과 건수만으로 개별 검사 결과를 만들어내지 않습니다.
health gzip 입력, 자체 health 규칙, 시점 비교, 메트릭·로그 분석은 아직 지원하지 않습니다.

## 실행 환경

Python 표준 라이브러리만 사용하므로 별도 패키지 설치가 필요하지 않습니다.
Linux/WSL의 Python 3.11·3.14에서 검증했습니다.

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
