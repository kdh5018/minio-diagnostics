# 개선 작업 계획서 (0.4.0)

- 작성: 2026-11-16, 기준 커밋 `f067c0c` (0.3.1 배포 후). 상태 갱신: 2026-11-16, 0.4.0 소스 커밋 `639d60a`
- 범위: 0.3.1 사용·검증(10월 12일 샘플, 2026-10-14 R3 자료, Rocky 8/9/10 컨테이너)에서 드러난 개선점
- 원칙
  - 공식 판정(mc 보고서)과 이 도구의 자체 점검을 구분한다.
  - 결측·미지원 값을 정상으로 바꾸지 않는다.
  - 종료 코드 0~4(배포본 5 포함)의 의미를 유지한다.
  - 자원 제한과 입력 결합 안전장치(시각 차 120초, 대상 일치)를 완화하지 않는다.
  - 원본 본문·호스트명·라이선스 값은 출력하지 않는다.
  - 신규 진단 수집, MinIO API 접속, 서버 변경은 하지 않는다.
  - 실자료 fixture 기대값을 바꾸지 않는다. 민감 원본·생성 보고서는 저장소·배포물에 넣지 않는다.
- 상태 값: `완료`(구현·테스트·배포물 검증 끝), `보류`(이유 기재), `미검증`(구현했으나 실제 환경 검증 못 함),
  `범위 밖`(이번 작업에서 하지 않기로 정해진 것)

## 상태 요약

| 번호 | 제목 | 우선순위 | 구분 | 상태 |
|---|---|---|---|---|
| 1 | 보고서의 '확인된 정보'·'한계'를 사람이 읽는 표로 | P1 | 이번 구현 | 완료 |
| 2 | THP 자체 점검 기준을 공식 권장값과 일치 | P0 | 이번 구현 | 완료 |
| 3 | R2에서도 `--lang`·`--fail-on-priority`·`--mask basic` 지원 | P1 | 이번 구현 | 완료 |
| 4 | 입력 결합 거부(시각 차·대상 불일치) 메시지 구체화 | P1 | 이번 구현 | 완료 |
| 5 | 입력 파일 권한·종류 안내 | P1 | 이번 구현 | 완료 |
| 6 | 수집 오류 유형을 고정 분류로 집계 | P2 | 이번 구현 | 완료 |
| 7 | `sys.errors` 결측 시 오류 수 처리 정리 | P1 | 이번 구현 | 완료 |
| 8 | `run-report.sh` 사용성(도움말·오타 안내·결과 보기) | P2 | 이번 구현 | 완료 |
| 9 | 빌드 zstd 의존 완화·라이선스 원문 검증·재현성 | P2 | 이번 구현 | 완료 |
| 10 | 배포 대상 추가를 '표 한 줄 추가'로 | P3 | 이번 구현 | 완료 |
| 11 | README 재구성, 배포본 README-ko 갱신 | P2 | 이번 구현 | 완료 |
| 12 | 실행 후 한 줄 결과 요약 출력 | P2 | 이번 구현 | 완료 |
| 13 | 0.4.0 빌드와 깨끗한 환경·실자료 검증 | P0 | 이번 구현 | 완료 |
| H1 | 기본 출력 폴더 이름 통일 | P3 | 보류 | 보류 |
| H2 | aarch64·Ubuntu 실제 빌드 | P3 | 보류 | 보류 |
| H3 | 실제 RHEL·AlmaLinux·실서버 실행 | P2 | 범위 밖 | 범위 밖 |
| H4 | 저장소 공개 설정 정리 | - | 범위 밖 | 범위 밖 |
| H5 | 결합 시각 허용치(120초) 완화 | - | 하지 않음 | 하지 않음 |
| H6 | 영어 보고서(`--lang en`) | P3 | 보류 | 보류 |
| H7 | 수집 시점 간 비교 | P3 | 보류 | 보류 |
| H8 | SUBNET 결과와 대조 | - | 범위 밖 | 범위 밖 |
| H9 | 인증서 신뢰체인·SAN·만료 임박 정책 | P3 | 보류 | 보류 |
| H10 | 메트릭·로그 운영 분석 | - | 범위 밖 | 범위 밖 |

## 이번 구현

### 1. 보고서의 '확인된 정보'·'한계'를 사람이 읽는 표로 (P1)

- 문제(근거): 0.3.1 HTML·Markdown 하단에 확인된 정보·한계·입력 결합 정보가 Python dict/JSON 원문
  (`{'status': ...}`, `<pre>{&quot;...`)으로 그대로 나왔다. 영문 코드(`provided`, `missing_or_unrecognized`)도 번역 없이 노출됐다.
- 변경 내용
  - R1·R2 공통 블록(`info_blocks`)으로 확인된 정보, 입력 결합 확인, 자체 해설 요약·규칙 목록, 입력 버전, 한계(범위 표·메모·완전성·마스킹 한계)를 표·목록으로 출력.
  - R2는 원본 관측 정보(서버·드라이브 기록, 시스템 설정 관측, 수집 오류, 수집 범위)를 같은 방식으로 추가.
  - 상태·코드는 한글 뒤에 원래 코드를 괄호로 둔다(예: `제공됨 (provided)`). 결측은 `미제공 / 미평가`.
  - 공식 요약 줄을 "공식 판정(mc 보고서): 통과 N · 오류 N · 경고 N (총 N)"으로, 분석 상태와 공식 판정이 별개임을 명시.
  - JSON 보고서 구조는 바꾸지 않음(HTML·Markdown 표현만 변경).
- 영향 범위: `aistordoctor.py`(`info_blocks`, `blocks_html`, `blocks_markdown`, `markdown`, `html_report`), `r2_health.py`(`observation_blocks`).
- 완료 기준: HTML·Markdown에 `{'`, `{&quot;`, `<pre>{`가 없고 표 머리글이 나온다. 원본 호스트명·오류 원문이 없다.
  `verify_browser.cjs` 통과.
- 테스트: `test_r2.R2Tests.test_markdown_and_html_have_no_internal_structures`

### 2. THP 자체 점검 기준을 공식 권장값과 일치 (P0)

- 문제(근거): 0.3.1의 `r2.thp.mode`는 "never 통과, always/madvise 경고"였다. 공식 `sys.mem.thp` 검사 Resolution의 권장값은
  `enabled=madvise`, `defrag=defer+madvise`(또는 madvise), `max_ptes_none=0`이라 공식 권장대로 설정한 서버를 도구가 경고하는 모순이 있었다.
  R3 자료에서 공식 경고 원인은 `max_ptes_none 0→511`·`enabled madvise→always`였는데 도구 기준과 어긋났다.
- 변경 내용: 세 값을 공식 권장값과 비교. 하나라도 다르면 경고, 세 값이 모두 관측되고 일치하면 통과, 하나라도 없으면 미평가(결측을 일치로 보지 않음).
  규칙 버전 `r2-system-2`, 조건 문구에 "자체 점검, 공식 판정이 우선" 명시.
- 영향 범위: `r2_enrichment.py`(`THP_OFFICIAL_RECOMMENDED`, `thp_result`, `system_rules`). JSON의 해당 규칙 결과·사유·버전.
- 완료 기준: 권장값 일치 → pass, 불일치 → warning, 결측 → 미평가. 공식 상태(`official_status`)는 비워 둠.
- 테스트: `test_r2.R2Tests.test_thp_compared_with_official_recommendation`

### 3. R2에서도 `--lang`·`--fail-on-priority`·`--mask basic` 지원 (P1)

- 문제(근거): 0.3.1 `run-report.sh`는 `--health`와 함께 쓰면 세 옵션을 실행 전에 거부(종료 코드 2)했다.
  사용자가 실행 방식마다 옵션 차이를 기억해야 했다.
- 변경 내용: R2 진입점에 `--lang ko`, `--mask strict|basic`, `--fail-on-priority P1~P4` 추가. 마스킹 수준은 공식 Markdown 처리에도 전달.
  `run-report.sh`의 검증·전달을 두 방식 공통으로. 잘못된 값은 여전히 종료 코드 2.
  종료 코드 우선순위는 4(부분 분석) > 1(조건 충족) > 0으로, 부분 분석을 1로 가리지 않음.
- 영향 범위: `r2_health.py`(`main`, `report`), `run-report.sh`. 기존 테스트 `test_r1_r2_option_differences`를 의도적으로 `test_r1_r2_common_options`로 교체.
- 완료 기준: R2에서 세 옵션이 분석기에 전달되고, 잘못된 값은 분석 전 거부.
- 테스트: `test_r2.R2Tests.test_common_options_mask_lang_fail_on_priority`, `test_shell_release.RunReportTests.test_r1_r2_common_options`

### 4. 입력 결합 거부(시각 차·대상 불일치) 메시지 구체화 (P1)

- 문제(근거): R3 `local-input/` 결합은 Markdown 07:11:00과 원본 07:08:18의 차이 161초로 거부됐는데,
  메시지는 일반 입력 오류뿐이라 원인과 조치를 알 수 없었다.
- 변경 내용: 결합 거부 시 시각 차(초)와 허용 120초, Markdown·gzip 수집 시각(KST 병기), 대상 수(Markdown/gzip/공통)만 표시.
  "로컬에서 다시 만든 Markdown의 Date는 생성 시각" 안내와 조치(같은 수집 파일 사용 또는 따로 분석)를 함께 출력.
  허용치 120초와 거부 동작은 바꾸지 않음(121초 거부, 120초 허용 경계 테스트).
- 영향 범위: `r2_enrichment.py`(`link`의 `conflict_detail`, `TIME_TOLERANCE_SECONDS`), `r2_health.py`(`LinkConflict`, `conflict_lines`).
- 완료 기준: 161초 사례에서 "161초", "허용 120초"와 조치가 나오고 호스트명은 나오지 않음. 출력 폴더를 만들지 않음.
- 테스트: `test_r2.R2Tests.test_time_conflict_message_is_specific_and_safe`, `test_target_conflict_message_counts_only`

### 5. 입력 파일 권한·종류 안내 (P1)

- 문제(근거): R3 원본은 소유자 전용(0600, uid 1000)이라 다른 계정으로 실행하면 종료 코드 2만 나오고 이유가 보이지 않았다.
  `.md`와 `.json.gz`를 바꿔 지정해도 분석기 깊은 곳의 일반 오류만 나왔다.
- 변경 내용
  - `run-report.sh`: 읽기 불가 시 소유자·UID·권한과 현재 사용자, "소유자 계정으로 실행하거나 읽을 수 있는 사본" 안내.
    상위 디렉터리 진입 불가도 구분. 원본 권한을 바꾸라는 안내(chmod)는 하지 않음.
  - 파일 앞부분 바이트로 종류 확인: `--health`는 gzip이어야 하고 `--official-report`는 gzip이 아니어야 함.
  - 분석기: 입력 오류 원인(권한·없음·디렉터리·인코딩·알려진 형식 오류)을 고정 문구로 안내. 경로·원문은 출력하지 않음.
- 영향 범위: `run-report.sh`(`check_input`), `aistordoctor.py`(`describe_input_error`, `INPUT_ERROR_HINTS`), `r2_health.py`.
- 완료 기준: 0600 타 소유 파일에서 소유자·권한·조치가 나오고 종료 코드 2. 원본 권한 불변.
- 테스트: `test_shell_release.RunReportTests.test_input_kind_mismatch_and_permission_guidance`(root 실행 시 권한 부분 건너뜀),
  `test_analyzer_input_error_details_reach_user`, `test_r2.R2Tests.test_input_error_guidance_without_raw_text`

### 6. 수집 오류 유형을 고정 분류로 집계 (P2)

- 문제(근거): 0.3.1은 수집 오류를 영역별 건수로만 보여 줘 R3의 "CPU governor 파일 없음 3 + NIC ring 미지원 3"을
  원본을 따로 열어 확인해야 했다. 원문을 그대로 내보내면 경로·장치명이 노출된다.
- 변경 내용: 오류 문구를 고정 패턴으로 "대상 / 종류"(예: `CPU 주파수 정책(cpufreq/scaling_governor) / 파일 없음`, `NIC ring 파라미터 / 미지원`)로만 집계.
  모르는 문구는 `미분류 대상 / 기타`. 원문·경로·장치명은 저장하지 않고 "원인 확정 아님"을 명시.
- 영향 범위: `r2_enrichment.py`(`classify_error`, `extract`의 `error_type_counts`), `r2_health.py`(관측 표).
- 완료 기준: 10월 12일·R3 자료에서 CPU 3·NIC 3 분류. 원문 단어가 출력에 없음.
- 테스트: `test_r2.R2Tests.test_collection_error_types_fixed_labels_only`

### 7. `sys.errors` 결측 시 오류 수 처리 정리 (P1)

- 문제(근거): `sys.errors`가 없거나 형식이 다른 행이 있으면 `reported_error_count`가 `None`이 되어,
  실제로 오류 행이 관측됐는데도 규칙이 미평가로만 남고 보고서에 오류 수가 보이지 않았다.
- 변경 내용: 기존 계약(`reported_error_count`는 불완전하면 `None`)을 유지하고 `observed_error_rows`(관측 하한),
  `count_basis`(complete/lower_bound), `incomplete_reasons`를 추가. 하한이 1 이상이면 경고, 0이면 미평가(0을 통과로 바꾸지 않음).
  보고서에는 "최소 N (전체 수 미확정)"으로 표시하고 부분 분석 사유에 추가.
- 영향 범위: `r2_enrichment.py`(`extract`, `system_rules`), `r2_health.py`. JSON 스키마 `1.3-r2`(필드 추가).
- 완료 기준: 결측 시 None 유지, 하한 표시, 부분 분석(종료 코드 4) 유지.
- 테스트: `test_r2.R2Tests.test_sys_errors_missing_reports_lower_bound_not_zero`

### 8. `run-report.sh` 사용성 (P2)

- 문제(근거): 도움말에 예시·결과 보는 법이 없었고, `--output-dir`·`-o` 같은 흔한 오타에 "알 수 없는 인자"만 나왔다.
  서버에 브라우저가 없을 때 HTML을 어떻게 보는지 안내가 없었다.
- 변경 내용: 한국어 도움말에 공통 옵션·예시·결과 보기·종료 코드 정리. 흔한 오타에 "혹시 X 옵션을 의미했습니까?" 안내(실행하지 않음).
  위치 인자만 준 경우 입력 지정 방법 안내. 결과 경로에 "(브라우저로 열기)"와 PC 복사 안내. 종료 코드 2 시 위 메시지를 보라고 안내.
- 영향 범위: `run-report.sh`.
- 완료 기준: 도움말·오타·위치 인자 테스트 통과. 종료 코드 의미 불변.
- 테스트: `test_help_is_korean_with_examples_and_html_guidance`, `test_typo_options_get_hint_without_running`

### 9. 빌드 zstd 의존 완화·라이선스 원문 검증·재현성 (P2)

- 문제(근거): 제3자 라이선스 원문을 `.tar.zst`에서 꺼낼 때 `zstd` 명령 또는 Python 3.14만 지원했다.
  `--license-dir`로 풀어 둔 폴더를 쓰면 원문 검증 없이 "미검증"으로만 기록됐다. 같은 입력으로 다시 빌드했을 때 같은 결과가 나오는지 시험이 없었다.
- 변경 내용
  - zstd 해제: `zstd` → `unzstd` → `zstdcat` → Python `zstandard` 모듈 → Python 3.14+ `compression.zstd` 순서.
  - full 배포판별 `python/licenses/` 20개 파일의 SHA-256을 고정(`LICENSE_MANIFEST`). 추출·폴더 지정 모두 파일 집합과 해시를 대조.
    다르면 빌드 거부. 시험용 `--allow-unverified-licenses`를 쓰면 `RELEASE-INFO`에 `THIRD_PARTY_LICENSE_VERIFIED=no`와 `UNVERIFIED` 기록.
  - 같은 입력·같은 `SOURCE_DATE_EPOCH`로 두 번 빌드해 바이트 단위 일치 확인.
- 영향 범위: `build-release.sh`, `test_shell_release.ReleasePackagingTests`. 배포본 `RELEASE-INFO`에 `THIRD_PARTY_LICENSE_VERIFIED` 추가.
- 완료 기준: 실제 0.4.0 빌드 `THIRD_PARTY_LICENSE_VERIFIED=yes`, 두 번 빌드 SHA-256 동일.
- 테스트: `test_license_dir_checked_against_pinned_hashes`, `test_build_is_reproducible`

### 10. 배포 대상 추가를 '표 한 줄 추가'로 (P3)

- 문제(근거): 대상 정의가 `case` 블록이라 추가할 때 코드 구조를 고쳐야 했고, `--list-targets`는 ID만 보여 줬다.
- 변경 내용: `TARGET_TABLE`(한 줄 = 한 대상: ID·설명·OS ID·주 버전·최소 glibc·triple·런타임 SHA·full 배포판 SHA)로 바꾸고,
  `--list-targets`가 세부 정보와 라이선스 대조 목록 유무를 출력. triple과 CPU 불일치는 거부.
- 대상 추가 점검 목록(aarch64·Ubuntu 등)
  1. python-build-standalone 같은 릴리스의 `install_only_stripped`·`pgo+lto-full` 파일 SHA-256을 공식 SHA256SUMS로 확인.
  2. `TARGET_TABLE`에 한 줄 추가, 새 full 배포판이면 `LICENSE_MANIFEST`에 파일별 SHA-256 추가.
  3. 최소 glibc는 런타임 요구 기호(빌드 출력) 이상이고 대상 OS 최저 버전 이하인지 확인.
  4. `run-report.sh` 사전 점검이 OS ID·CPU를 `RELEASE-INFO`에서 읽는지 확인(코드 수정 불필요).
  5. 해당 OS·CPU의 깨끗한 환경(Python 없음, 네트워크 차단, 비root)에서 사전 점검·단독·결합 실행.
- 영향 범위: `build-release.sh`.
- 완료 기준: 복사본에 표 한 줄만 추가해 새 대상 배포물이 만들어지는 테스트 통과. 실제 다른 OS·CPU 빌드는 하지 않음(H2).
- 테스트: `test_add_target_is_one_table_row`

### 11. README 재구성, 배포본 README-ko 갱신 (P2)

- 문제(근거): README가 날짜별 작업 기록 위주라 현재 상태와 사용법을 찾기 어려웠다. 배포본 README-ko에는
  "종료 코드 1은 R1 전용", 옛 THP 기준 등 0.4.0과 맞지 않는 설명이 생긴다.
- 변경 내용: README 첫머리에 현재 상태 요약·사용자용 빠른 시작·문서 안내를 두고, 기존 기록은 "변경 이력"으로 보존.
  README-ko에 공통 옵션, 입력 종류·결합 거부·권한 안내, 결과 보기, 종료 코드 1 공통, THP·수집 오류 분류 해석 주의를 추가.
- 영향 범위: `README.md`, `build-release.sh`(README-ko 본문).
- 완료 기준: 0.4.0 배포본 README-ko에 새 안내가 있고 옛 설명이 없음.

### 12. 실행 후 한 줄 결과 요약 출력 (P2)

- 문제(근거): 실행이 끝나면 저장 경로만 나와 결과를 보려면 HTML을 열어야 했다.
- 변경 내용: "요약: 공식 판정 통과 31 · 오류 2 · 경고 2 (총 35) / 자체 점검 통과 4 · 경고 2 · 미평가 1 / 분석 상태 …" 한 줄 출력.
  공식 판정과 자체 점검을 나눠 표시.
- 영향 범위: `aistordoctor.py`(`result_summary_line`), `r2_health.py`.
- 완료 기준: R1·R2 실행 출력에 요약 줄.

### 13. 0.4.0 빌드와 깨끗한 환경·실자료 검증 (P0)

- 변경 내용: 패키지 0.4.0, R1 `0.4.0-r1`, R2 `0.4.0-r2`, 스키마 `1.3-r2`. 0.3.0·0.3.1 배포물은 보존.
- 검증: Rocky 8·9·10 최소 이미지(Python 없음, `--network none`, 비root)에서 사전 점검·10월 12일 샘플 단독·결합,
  R3 자료(읽기 전용, 작업 전후 SHA-256 비교) 단독·결합과 161초 결합 거부 메시지, 동봉 런타임 테스트, 브라우저 검증.
- 완료 기준: 위 실행의 종료 코드가 0.3.1과 같고(0/4/2), 원본 해시 불변, push 후 원격 SHA 일치.

## 보류·범위 밖

| 번호 | 항목 | 구분 | 이유 |
|---|---|---|---|
| H1 | 기본 출력 폴더 통일(소스 `~/minio-airgap-diag-output`, 배포본 `~/aistor-diag-output`) | 보류 | 기존 사용자의 결과 위치·`CURRENT.json`이 바뀌는 호환성 변경이라 별도 결정 필요. 지금은 `--out-dir`로 지정 가능 |
| H2 | aarch64·Ubuntu 실제 빌드 | 보류 | 요청·실검증 환경이 없음. 표 한 줄 추가 구조와 점검 목록(10번)만 준비 |
| H3 | 실제 RHEL·AlmaLinux·실서버(204 등) 실행 | 범위 밖 | 서버 접속·변경 금지. Rocky 최소 이미지 컨테이너로 대신 검증 |
| H4 | 저장소 공개 설정 | 범위 밖 | 지시에 따라 건드리지 않음. `LICENSE`(사내·고객사 전용)와의 정합은 별도 결정 |
| H5 | 결합 시각 허용치 120초 완화 | 하지 않음 | 안전장치 완화 금지. 대신 거부 메시지를 구체화(4번) |
| H6 | 영어 보고서 `--lang en` | 보류 | 번역 문구 검수 계획 없음. 현재 `ko`만 허용하고 그 밖의 값은 종료 코드 2 |
| H7 | 수집 시점 간 비교 | 보류 | 서버·드라이브 별칭이 입력 안에서만 유효해 안정 대상 ID 설계가 먼저 필요 |
| H8 | SUBNET 결과와 대조 | 범위 밖 | SUBNET 업로드 금지, 검사 집합(33개)이 mc(35개)와 다름 |
| H9 | 인증서 신뢰체인·SAN·만료 임박 정책 | 보류 | 정책 기준(임박 일수 등) 미정. 현재 유효기간만 점검 |
| H10 | 메트릭·로그 운영 분석 | 범위 밖 | 신규 수집이 필요한 기능 |

## 검증 결과

2026-11-16, 소스 커밋 `639d60a`(dirty=no) 기준. 이번 구현 1~13번은 모두 `완료`입니다.

### 배포물

- `dist/aistor-diag-analyzer-0.4.0-el8-x86_64.tar.gz` 25,186,796바이트,
  SHA-256 `2ba5b1688e7a574db2079e52c5d0d192ca64c5d3091eaf1b993efde25309be28`
- 같은 입력으로 다른 폴더에 다시 빌드해 SHA-256이 같음을 확인(9번 재현성).
- `RELEASE-INFO`: `PACKAGE_VERSION=0.4.0`, `THIRD_PARTY_LICENSE_VERIFIED=yes`(원문 20개 고정 SHA-256 일치).
- 0.3.0·0.3.1 배포물과 `.sha256`은 그대로 둠.

### 테스트

- 개발 환경(WSL, Python 3.14): 119개 실행, 모두 통과. 실자료·배포물 환경 변수를 모두 지정해 건너뜀 0개.
- Rocky 10 컨테이너(동봉 런타임): 119개 통과, 건너뜀 0개.
- Rocky 8·9 컨테이너: 119개 통과, 건너뜀 8개. 최소 이미지에 `find`·`xargs`가 없어 빌드 스크립트 시험만 건너뜀.

### 깨끗한 환경 (Rocky 8.9·9.3·10.2 컨테이너)

조건: Python 패키지 없음, `--network none`, 비root(UID 1000·1500), 입력은 읽기 전용 마운트, 배포본 폴더 쓰기 금지.

| 실행 | Rocky 8.9 (glibc 2.28) | Rocky 9.3 (2.34) | Rocky 10.2 (2.39) |
|---|---|---|---|
| `--check --verify-bundle` | 0 | 0 | 0 |
| 10월 12일 공식 Markdown 단독 | 0 | 0 | 0 |
| 10월 12일 결합(`--allow-partial`) | 4 | 4 | 4 |
| R3 collection 단독 | 0 | 0 | 0 |
| R3 local-input 단독 | 0 | 0 | 0 |
| R3 결합(`--allow-partial`) | 4 | 4 | 4 |
| R3 local-input + collection gzip 결합 | 2 (거부) | 2 (거부) | 2 (거부) |

- 결합 거부 메시지: "수집 시각 차이: 161초 (허용 120초 이내만 결합)", 양쪽 수집 시각(KST)과 조치를 표시(4번). 120초 허용치는 그대로.
- 결합 결과: 서버 3, 드라이브 12, 수집 오류 행 6(CPU 정보 3, 네트워크 정보 3), 오류 수 근거 `complete`,
  오류 유형 "CPU 주파수 정책 / 파일 없음" 3, "NIC ring 파라미터 / 미지원" 3(6번), THP 자체 점검 경고(2번).
- 요약 줄: "공식 판정 통과 31 · 오류 2 · 경고 2 (총 35) / 자체 점검 통과 4 · 경고 2 · 미평가 1 / 분석 상태 partial"(12번).
  공식 판정 비통과는 `minio.cluster.min`(오류), `sys.mem.iommu`(오류), `minio.net.tls.valid`(경고), `sys.mem.thp`(경고)입니다.
- 모든 결과: 세대 폴더 0700, 파일 0600, `manifest.json` 해시 일치.
- 권한 안내(5번): UID 1500으로 0600 파일을 읽으면 종료 코드 2, 파일 소유자·권한·현재 사용자와 조치 표시,
  "원본 파일의 권한은 바꾸지 마십시오" 안내.

### 원본 보존

- R3 자료: 실행 전후 418개 파일 SHA-256 동일, 항목 수 430개 그대로, 새 파일 없음.
- 10월 12일 샘플 2개: 실행 전후 SHA-256 동일.

### 브라우저 (`verify_browser.cjs`)

- Chrome 157.0.8103.40, `file:`, 0.4.0 결합 HTML(`tool_version` `0.4.0-r2`, 항목 7개).
- 필터 18개 조합 기대값 일치, 검색·키보드 펼침·인쇄 배치 통과, 데스크톱·모바일 가로 넘침 없음.
- 악성 마크업 합성 입력(`<script>`, `<img onerror>`, `<iframe>`, `javascript:` 링크): 실행·삽입 없음.
- 외부 요청 0, 페이지 오류 0, 콘솔 오류 0. 생성 보고서·스크린샷은 확인 후 삭제.

### 검증 중 추가로 고친 것

- R2 결합 거부 상세 줄의 들여쓰기가 "  -   · …"로 겹쳐 출력되던 문제(`3a56b7a`).
- 계정 이름이 없는 UID로 실행하면 현재 사용자 표시가 두 줄로 갈라지던 문제(`013afe2`, 시험 `639d60a`).

### 남은 한계

- 실제 RHEL·AlmaLinux·실서버 실행은 하지 않음(H3). Rocky 최소 이미지 컨테이너 기준입니다.
- Rocky 8·9 최소 이미지에서는 빌드 스크립트 시험 8개를 건너뜀(실행 도구 부재, 개발 환경·Rocky 10에서는 통과).
- 컨테이너 안에서 이름 없는 소유자는 `stat`이 주는 대로 "UNKNOWN(UID 1000)"으로 표시됨(동작에는 영향 없음).
- H1~H10은 위 표의 이유대로 보류·범위 밖입니다.
