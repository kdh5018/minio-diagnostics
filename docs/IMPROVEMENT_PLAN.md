# 개선 작업 계획서 (0.4.0~0.4.4)

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

## 0.4.1 고객 관점 수정 (2026-11-16)

- 근거: 0.4.0 배포본 고객 입장 시험 기록(TEST_LOG 8절)과 그 과정에서 찾은 R3 `host:port` 노출.
- 소스 커밋: `dfbe9dd`(수정), `9f0fa7e`(회귀 테스트 18개), `c9333ee`(README 버전). 배포물 소스는 `c9333ee`.
- 바꾸지 않은 것: 120초 결합 허용치, 자원 제한, 종료 코드 의미, 실자료 판정 기대값(공식 31/2/2, 자체 4/2/1, 수집 오류 6).

| 번호 | 고객이 겪은 문제 | 변경 | 회귀 테스트 | 상태 |
|---|---|---|---|---|
| F1 | MD에 `&#95;`·`&lt;` 같은 HTML 엔티티가 그대로 보임 | MD는 CommonMark 문맥별 백슬래시 이스케이프로 바꾸고, 근거 원문은 펜스 코드 블록으로. 코드 스팬은 원문 유지. HTML은 기존처럼 HTML 이스케이프 | `Fix041Tests` 엔티티·악성 입력·펜스·규칙 단위, R2 엔티티, 실자료 | 완료 |
| F2 | 결합 IOMMU 항목이 `자체 점검 결과: None / explicit_flags_unobserved_or_ambiguous` | `미평가`와 한국어 사유(코드 병기) | `test_unevaluated_rule_shows_korean_not_none`, 실자료 | 완료 |
| F3 | 공개 시스템 경로·문서 URL까지 `identifier-N`으로 가려 조치 방법을 읽을 수 없음, `--mask basic`도 같음 | 표준 커널·부트 경로(`/proc/cmdline`, `/etc/default/grub`, THP sysfs 등)와 공식 문서 URL(docs.min.io, kernel.org, golang)만 허용 목록으로 원문 표시. 경로 뒤에 IP·도메인·`@`가 붙으면 가림. `/dev`, `/sys/class/net`, `/sys/block`, 사용자 경로는 계속 가림 | 공개 경로 원문·식별자 가림(strict·basic), 실자료 | 완료 |
| F3a | R3 `pool1.server1:9000` 같은 `호스트:포트`가 가려지지 않음(0.4.0) | `호스트:포트` 형태를 식별자로 가림(마스킹 범위 확대) | `test_r3_style_server_names_masked_everywhere`, 컨테이너 R3 HTML·MD·JSON 노출 0 | 완료 |
| F4 | `unclassified`, `unsupported`, `no_registered_explanation` 등 영문 코드 노출 | 한국어 라벨 + 괄호 안 코드. HTML 필터 값(`value`)은 그대로 두고 보이는 글자만 한국어 | `test_codes_have_korean_labels`, R2 필터 단언 | 완료 |
| F5 | "출력:" 줄에 `../` 상대 경로가 그대로 보임 | 출력·결과 폴더를 정규화된 절대 경로로 표시 | 셸 `..` 경로 시험, R1 `main` 절대 경로 | 완료 |
| F6 | 결과 상위 폴더에 `.output.lock`이 남고 설명이 없음 | 디렉터리 자체 잠금(flock)으로 바꿔 파일을 만들지 않음. 디렉터리 잠금이 안 되는 파일 시스템(NFS 등)에서만 `.output.lock` 사용. README-ko에 설명 추가 | 잠금 파일 미생성·동시 실행 거부·대체 경로 | 완료 |
| F7 | 인증서 남은 시간이 수집 시각 기준인데 기준이 표시되지 않음 | 기준 시각(수집 시각 또는 `--as-of`)을 줄 제목에 표시하고, 보고서 생성 시각 기준 줄(만료 시 "이미 만료됨") 추가. JSON 스키마는 그대로 | 만료·미만료·`--as-of` | 완료 |
| F8 | (참고) 샘플과 R3의 요약 숫자가 같음 | 서로 다른 입력임을 확인(결합 시험의 대상 공통 0개). 버그 아님 | 해당 없음 | 조치 불필요 |
| F9 | README에 입력 파일 둘 곳과 결과를 PC로 옮기는 방법이 없음 | README-ko에 "입력 파일 두기"(scp), 결과 scp/sftp·PowerShell 예시, 실행 후 화면에 scp 예시 한 줄 | 셸 시험(scp 안내) | 완료 |
| F10 | 컨테이너에서 이름 없는 계정이 `UNKNOWN(UID 1000)`으로 표시 | "이름 없는 계정(UID n)"으로 표시 | 셸 `stat` 대체 시험, 컨테이너 3종 | 완료 |

### 0.4.1 검증 결과

- 배포물: `dist/aistor-diag-analyzer-0.4.1-el8-x86_64.tar.gz` 25,193,631바이트,
  SHA-256 `8899f458eaef753c0bca9c5ddfbe7bad4f47a27b91a828c71b335b0bf0d9401d`(소스 `c9333ee`, dirty=no). 0.3.0·0.3.1·0.4.0 해시 그대로.
- 테스트: 137개(기존 119 + 신규 18). 개발 환경(Python 3.14, 배포물 포함) 전부 통과·건너뜀 0.
  동봉 런타임으로 Rocky 10.2 전부 통과·건너뜀 0, Rocky 8.9·9.3 통과·빌드 시험 8개 건너뜀(`find`·`xargs` 없음, 0.4.0과 같음).
- 깨끗한 환경: Rocky 8.9·9.3·10.2 최소 컨테이너(Python 없음, `--network none`, 읽기 전용 루트, UID 1000 계정 이름 없음).
  - `--check --verify-bundle` 0. 샘플·R3 단독 0, 결합 `--allow-partial` 4, 결합 기본 4, `--mask basic` 결합 4.
  - 모든 결과 공식 31/2/2, 결합 자체 점검 4/2/1, 수집 오류 6. 세대 폴더 700, 파일 600.
  - MD 엔티티 0, `None` 0, `/proc/cmdline`·docs.min.io 원문 표시, `pool1.server`·`svc.cluster.local` 노출 0(HTML·MD·JSON), `.output.lock` 없음.
  - R3 `local-input` Markdown + 수집 gzip 결합: 종료 코드 2, "수집 시각 차이: 161초 (허용 120초 이내만 결합)".
  - `--out-dir ../rel-out`: 절대 경로 표시, `/../` 0. 권한 없는 입력: "소유자 이름 없는 계정(UID 1000)".
- 렌더링 교차 확인: markdown-it(CommonMark+표)로 실자료 6종(샘플·R3 × 단독 strict/basic·결합)과 악성 입력 18종을 렌더링해
  HTML 블록·인라인 HTML·링크·이미지 토큰 0개.
- 브라우저(`verify_browser.cjs`): Chrome 157, 0.4.1 결합 HTML. 필터 18조합 일치, 검색·키보드·인쇄 통과,
  악성 마크업 실행·삽입 없음, 외부 요청·페이지 오류·콘솔 오류 0.
- 원본 보존: 10월 12일 샘플 2개, R3 418개 파일 SHA-256 전후 동일(R3 항목 430개 그대로).
- 남은 한계: 실제 RHEL·AlmaLinux·실서버 실행은 하지 않음. 디렉터리 잠금이 안 되는 NFS 대체 경로는 단위 테스트로만 확인.
  Markdown 뷰어별 표시는 CommonMark 렌더러 하나로만 교차 확인.

## 0.4.2 코드·문서 간소화 (동작 동일)

- 코드: ponytail 스킬(github.com/DietrichGebert/ponytail) 기준으로 중복 제거. 판정·마스킹·종료 코드·120초 허용치·자원 제한·출력 권한은 그대로.
  - aistordoctor.py 1069→1061, r2_health.py 489→479, r2_enrichment.py 206→215(공용 도우미 이동), analyze.py 10→10,
    run-report.sh 630→617, build-release.sh 619→609(코드) / 548(README-ko 축약 포함).
- 문서: README 412→49줄. 상세 설명은 `docs/USAGE.md`·`docs/DEVELOPMENT.md`·`docs/HISTORY.md`로 옮김(삭제 없음). 배포본 README-ko 124→63줄.
- 동등성: 샘플·R3 × R1/R2 × strict/basic, 원본 단독, `--as-of`, `--fail-on-priority`, 부분 분석 거부, 결합 거부, 잘못된 입력,
  run-report.sh 도움말·오류·점검 등 108개 결과(HTML·MD·JSON·manifest·표준 출력·종료 코드)를 실행 ID·시각만 정규화해 비교.
  간소화 전(`2a275c7`)과 차이 0, 버전 올림 후에는 버전 문자열만 다름.
- 배포물: `dist/aistor-diag-analyzer-0.4.2-el8-x86_64.tar.gz` 25,190,037바이트,
  SHA-256 `0f3dd51bc6742ab1e547f2b26e9457263c2dc89d8e5fa6c68285e871edffcbf9`(소스 `c9162d5`, dirty=no). 0.3.0~0.4.1 해시 그대로.
- 검증: 테스트 137개 개발 환경 통과·건너뜀 0. Rocky 8.9·9.3·10.2 컨테이너(Python 없음, 네트워크 차단, UID 1000)에서
  0.4.1과 같은 항목 전부 같은 결과(31/2/2, 4/2/1, 수집 오류 6, 결합 거부 161초 종료 코드 2, 700/600, `.output.lock` 없음).
  컨테이너 MD 5종은 0.4.1과 실행 ID·시각·버전 외 동일. 원본 해시 샘플 2개·R3 418개 전후 동일.
- 간소화 중 발견했으나 고치지 않은 점(동작 변경이라 별도 판단 필요):
  결합 R2의 `--as-of`가 공식 부분 R1 모델에 전달되지 않음, R2에는 R1의 CURRENT.json 하드링크(samefile) 점검이 없음,
  공식 보고서 구간 파싱 루프가 10초 시한 점검 전에 시한을 넘길 수 있음, build-release.sh 라이선스 다운로드 실패 시 `.part` 처리,
  SYSTEM_PYTHON_MIN 3.9가 RELEASE-INFO와 run-report.sh에 중복.
  → 0.4.3에서 5건 모두 수정(아래).

## 0.4.3 남은 항목 수정 (2026-11-16)

0.4.2 간소화 중 발견한 5건. 각각 먼저 재현 테스트를 만들어 실패를 확인한 뒤 고쳤습니다(신규 테스트 6개, 수정 전 6개 모두 실패).

| # | 문제 | 재현 | 수정 | 테스트 |
|---|---|---|---|---|
| 1 | 결합 R2의 `--as-of`가 공식 부분에 전달되지 않음 | 재현(공식 부분 기준 시각 출처가 `collection`) | `r2_health.py`가 공식 부분 모델에도 같은 기준 시각을 넘김. `--as-of` 미사용 결과는 그대로 | `test_r2.py` `test_combined_as_of_reaches_official_part` |
| 2 | R2에 R1의 CURRENT.json 하드링크 점검 없음 | 부분 재현: 명시 점검이 없고 문구가 R1과 다름. 저장 단계 보호로 종료 코드 3·입력 무변경은 이미 지켜짐 | `aistordoctor.refuse_input_output_alias()` 공용 함수로 R1·R2 모두 같은 점검, 같은 종료 코드 3·같은 문구(`OUTPUT_FAILURE_MESSAGE`) | `test_r2.py` `test_input_hardlinked_as_current_json_refused_like_r1`(공식·원본 입력 각각, R1 비교 포함) |
| 3 | 공식 Markdown 구간 해석 루프가 10초 시한을 넘겨도 계속 돎 | 재현(시한 경과 후에도 다음 단계 호출) | `check_deadline()`를 모든 반복 안에서 호출. 섹션 끝 계산은 이분 탐색으로(결과 동일). 10초·종료 코드 의미 그대로 | `run_tests.py` `Fix043Tests` 2개 |
| 4 | `build-release.sh` 내려받기 실패 시 `.part`가 캐시에 남거나 최종 이름으로 옮겨짐 | 재현(실패 시 `.part` 잔류, 해시 불일치 파일이 최종 이름으로 남음, 라이선스 원본도 같음) | 캐시 폴더 안 임시 파일에 받고 고정 SHA-256이 맞을 때만 `mv`. 실패·불일치는 임시 파일 삭제 후 빌드 중단, 중단 시 EXIT trap이 삭제 | `test_shell_release.py` `test_failed_or_wrong_download_never_reaches_cache`(curl 대역으로 실패·불일치·정상) |
| 5 | 시스템 Python 최소 버전 3.9가 두 곳에 따로 적힘 | 재현(원본 값을 바꿔도 RELEASE-INFO는 3.9) | `run-report.sh`의 `DEFAULT_SYSTEM_PYTHON_MIN` 한 곳만 원본. 빌드가 읽어 RELEASE-INFO·README-ko 생성 | `test_shell_release.py` `test_system_python_minimum_has_one_source`(값을 바꿔 빌드하면 두 곳 모두 따라감) |

- 커밋: `95deaeb`(수정 5건+테스트), `c3f0c68`(0.4.3 버전 올림).
- 배포물: `dist/aistor-diag-analyzer-0.4.3-el8-x86_64.tar.gz` 25,191,594바이트,
  SHA-256 `50195691136de399e8616dc78f3d8df0b5aa5aea48e82e8c10a589e025f0d78e`(소스 `c3f0c68`, dirty=no). 0.3.0~0.4.2 해시 그대로.
- 테스트: 143개(137+6). 개발 환경 0.4.3 배포물 지정 시 건너뜀 0.
- 0.4.2 대비 108개 결과 비교: 107개는 버전 문자열 외 동일. 결합 `--as-of` 실행 1건만 의도대로 3개 값이 바뀜
  (분석 기준 시각·출처 `collection`→`explicit`, 인증서 남은 시간 752,393→784,602초). 판정 개수는 같음.
- Rocky 8.9·9.3·10.2 컨테이너(Python 없음, 네트워크 차단, UID 1000): 31/2/2, 4/2/1, 수집 오류 6, 결합 거부 161초 종료 코드 2,
  700/600, 테스트 통과(Rocky 8·9는 빌드 도구 필요 시험 10개 건너뜀, Rocky 10 건너뜀 0). 컨테이너 MD 5종은 0.4.2와 실행 ID·시각·버전 외 동일.
  원본 해시 샘플 2개·R3 418개 전후 동일.
- 하지 않은 것: 실서버 재시험(이후 Rack 담당), 실제 네트워크 다운로드 실패(대역으로만 시험).

## 0.4.4 단수형 Summary·표시 문제 수정 (2026-11-16)

201 서버 새 수집(`* Results: 32 passed, 2 errors, 1 warning`)이 종료 코드 2로 거부된 문제와 0.4.3 고객 테스트에서 발견한 표시 문제.
새 테스트는 0.4.3 코드에서 모두 실패하는 것을 먼저 확인했습니다(합성 fixture만 사용, 고객 원본은 저장소에 넣지 않음).

| # | 문제 | 수정 위치 | 테스트 |
|---|---|---|---|
| 1 | Summary 정규식이 복수형만 인식(`1 error`·`1 warning` 거부) | `aistordoctor.py` `parse_report()` Summary 정규식 `errors?`·`warnings?\b`, 안내 문구 | `test_r2.py` `Fix044Tests.test_singular_and_plural_summary_standalone_and_combined`(단수·복수·둘 다 1·`1 passed`, R1 단독·R2 결합 main 포함), `test_unrecognized_summary_still_refused` |
| 2 | R2 결합에서 Markdown 해석 실패를 `원본 gzip(--health)` 탓으로 표시 | `r2_health.py` `report()`가 Markdown을 먼저 해석하고 실패 시 `OfficialInputError`, `main()` 라벨 선택. `aistordoctor.model()`·`r2_enrichment.link()`는 해석 결과를 받아 재사용 | `test_markdown_parse_failure_names_markdown_input`(반대 경우 포함) |
| a | 상대 경로 입력이 `../`째 표시 | `run-report.sh` `absolute_path()`(기존 출력 경로 정리 함수를 입력에도 적용) | `test_shell_release.py` `test_relative_input_paths_shown_as_clean_absolute_path` |
| b | MD 표 칸에 `\--as-of 지정`, `3\.4` 같은 백슬래시 | `aistordoctor.md_text()`: 표 칸은 줄머리 이스케이프 생략(인라인 이스케이프 유지) | `test_markdown_table_cell_has_no_line_start_backslash` |
| c | 입력·출력 충돌 거부에 원인 없음 | `aistordoctor.InputOutputCollision`·`output_failure_lines()`, R1·R2 `main()`. 첫 줄 문구·종료 코드 3 그대로, 경로는 출력하지 않음 | `test_collision_message_states_cause` |

- 기존 `test_arguments_passed_verbatim_and_force_only_when_requested`는 입력을 절대 경로로 전달하게 된 점만 반영.
- 커밋 `a0a4932`(수정), `9aeed4f`(0.4.4). 배포물 `dist/aistor-diag-analyzer-0.4.4-el8-x86_64.tar.gz` 25,191,749바이트,
  SHA-256 `4726d41f2da3c8c57b12cb13e2bb07f349702c27cd7dfeafe76f731ce8f18d64`(소스 `9aeed4f`, dirty=no). 0.3.0~0.4.3 해시 그대로.
- 테스트 149개(59/57/33). git archive 깨끗한 사본 + 0.4.4 배포물 지정, 건너뜀 0.
- 0.4.3 대비 108개 결과: HTML·JSON·표준 출력·종료 코드 동일. MD 7개에서 표 칸 줄머리 백슬래시만 사라짐.
- Rocky 8.9·9.3·10.2: 31/2/2, 4/2/1, 수집 오류 6, 결합 거부 161초 종료 코드 2, 원본 해시 2/2·418/418 동일.
