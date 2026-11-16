# 변경 이력

[README](../README.md)에서 옮긴 날짜별 기록입니다. 개선 항목별 상태는 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)를 보십시오.

최근 기록이 아래에 있습니다. 각 기록은 당시 상태 그대로 두고, 뒤에 바뀐 점만 "→"로 덧붙였습니다.

### 2026-11-16 현황: Python 없는 서버용 배포본

위 기록은 그대로 두고 이번 변경만 추가합니다. 분석 엔진(`analyze.py`·R1·R2)은 바꾸지 않았습니다.

#### 배포본 실행 (`run-report.sh`)

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
  → 0.4.0에서 두 실행 방식 공통 옵션으로 바뀌었습니다.
- 분석기의 종료 코드 0~4를 그대로 돌려줍니다. 4(부분 분석)로 저장된 경우에도 결과 경로를 안내합니다.
  `--force`는 사용자가 지정한 경우에만 전달합니다.
- 사전 점검: OS(ID·주 버전)·CPU 아키텍처·glibc·동봉 런타임 실행(noexec 포함)·입력 파일·출력 폴더 권한(0700, 소유자)·
  심볼릭 링크를 확인합니다. 지원하지 않는 환경은 설치를 시도하지 않고 종료 코드 **5**로 안내합니다.
- `--use-system-python`은 검증된 시스템 python3(3.9 이상)를 쓸 때만 선택합니다. 기본은 동봉 런타임입니다.

#### 배포본 빌드 (`build-release.sh`, 개발 환경 전용)

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

#### 이번 검증 결과 (2026-11-16)

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

#### 남은 항목

- P4: 2026-10-14 R3 자료를 받지 못해 미완료입니다. 다른 자료로 대체하지 않았습니다.
  → 같은 날 후속 작업에서 사용 승인을 받아 검증했습니다(아래 "2026-11-16 후속" 절).
- 라이선스: 프로젝트 라이선스와 동봉 런타임의 제3자 고지 정리가 미결입니다(`LICENSE` 파일 없음,
  `RELEASE-INFO`의 `LICENSE_STATUS=pending`). 배포 전 결정이 필요합니다.
  → 같은 날 사내·고객사 전용으로 확정했습니다(아래 "2026-11-16 후속" 절).

### 2026-11-16 후속: 라이선스 확정, 0.3.1 배포본, R3 실자료 검증

위 기록은 그대로 두고 이번 변경만 추가합니다. 분석 엔진(`analyze.py`·R1·R2)은 바꾸지 않았습니다.

#### 라이선스

- 저장소 최상위 `LICENSE`: Copyright (c) 2026 WNYTech. All rights reserved. 사내 및 WNYTech가 제공한 고객사에서
  제공 목적으로만 사용하며, 그 목적 외의 무단 복제·배포를 금지합니다. 오픈소스 라이선스가 아닙니다.
- 배포본에는 `LICENSE`, `licenses/PYTHON-LICENSE.txt`, `licenses/third-party/LICENSE.*.txt`(20개),
  `licenses/NOTICE-ko.txt`가 들어갑니다. `RELEASE-INFO`는 `LICENSE_STATUS=proprietary`입니다.
- 제3자 고지는 stripped 런타임에 들어 있지 않아, 같은 빌드의 full 배포판
  (`cpython-3.12.16+20261110-x86_64-unknown-linux-gnu-pgo+lto-full.tar.zst`,
  SHA-256 `a1b98760371cfd145a1533ea75c89437cdaf538e84c5e84c469eda9065a251af`)의 `python/licenses/` 원문을 그대로 씁니다.
  CPython·OpenSSL 3·zlib·libffi·SQLite·Expat·bzip2·liblzma·mpdecimal 원문이 없으면 빌드가 실패합니다.
- 빌드 환경에서 `.tar.zst`를 풀려면 `zstd` 또는 Python 3.14 이상이 필요합니다(없으면 `--license-dir`).
  → 0.4.0에서 해제 도구 선택지를 넓히고 원문 파일별 SHA-256 대조를 추가했습니다.

#### 0.3.1 배포물

- `aistor-diag-analyzer-0.3.1-el8-x86_64.tar.gz`, 25,180,589 bytes,
  SHA-256 `b7a34e012924653888c401aa23fb0396cb4be33506322884178941820f6e1ee8`, 소스 커밋 `76972c8` (dirty=no).
  0.3.0 배포물은 지우지 않았습니다.
- 테스트: 104개(기존 83 + 쉘·패키징 21). WSL Python 3.14.4에서 샘플·배포물 지정 시 104개 통과(건너뜀 0),
  지정하지 않으면 건너뜀 8. Rocky 8.9·9.3 컨테이너(동봉 런타임) 104개 중 건너뜀 5(최소 이미지에 `find`·`xargs`가 없어
  빌드 테스트 제외), Rocky 10.2 건너뜀 0.
- 깨끗한 환경: Rocky 8.9·9.3·10.2 최소 이미지, Python·python RPM 없음, `--network none`, 비root.
  세 환경 모두 `--check --verify-bundle` 0, 10월 12일 샘플 단독 0·결합 4, R3 단독 0·결합 4.
  결과 세대 0700, 파일 0600, manifest SHA-256 일치.
  R3 원본은 소유자 전용(0600)이라 해당 실행은 소유자 uid(1000, 비root)로 했습니다. 다른 uid로는 읽지 못해
  종료 코드 2(입력 오류)로 거부되는 것도 확인했습니다. 원본 권한은 바꾸지 않았습니다.

#### P4: R3 실자료 검증 (2026-10-14 수집)

| 실행 | 종료 코드 | 공식 요약(통과/오류/경고) | 비고 |
|---|---|---|---|
| `collection/` 공식 Markdown 단독 | 0 | 31/2/2 (35개) | `success` |
| `local-input/` 공식 Markdown 단독 | 0 | 31/2/2 (35개) | `success` |
| `collection/` Markdown + gzip 결합(`--allow-partial`) | 4 | 31/2/2 | `partial`, 서버 3, 드라이브 12, 수집 오류 6(CPU 3, 네트워크 3) |
| `local-input/` Markdown + gzip 결합(추가 확인) | 2 | - | 입력 연결 검사 거부: Markdown 시각 07:11:00과 원본 07:08:18의 차이 161초가 허용 120초 초과 |

- 사용자 설명과 대조: mc 35개 통과 31·오류 2·경고 2 일치. 원본 수집 오류 CPU governor 파일 없음 3건 + NIC ring
  미지원 3건 일치(도구는 오류 문구를 내보내지 않으므로 영역별 건수로 확인하고, 문구 종류는 원본을 읽기 전용으로 분류).
- 공식 오류·경고 검사: `minio.cluster.min`·`sys.mem.iommu`(오류), `minio.net.tls.valid`·`sys.mem.thp`(경고).
- 두 Markdown 차이: 보고서 날짜(07:08:22 대 07:11:00)와 THP 표 한 행
  (`pool1.server2` 항목이 `collection/`에서는 `max_ptes_none 0→511`, `local-input/`에서는 `enabled madvise→always`).
  요약 수치와 검사 목록은 같습니다.
- SUBNET 결과(33개, 통과 29·실패 2·경고 2)는 검사 집합이 달라 대조하지 않았습니다.
- 원본 보존: R3 폴더 전체 418개 파일의 SHA-256이 작업 전후 동일하고, 폴더 안에 새 파일이 생기지 않았습니다.
  입력 3개 파일은 폴더 자체의 `SHA256SUMS`와도 일치합니다. 실자료 fixture 기대값은 바꾸지 않았습니다.
  생성 보고서는 컨테이너 임시 메모리와 저장소 밖 임시 폴더에만 만들었고 확인 후 삭제했습니다.

#### 남은 항목 (후속)

- GitHub 저장소가 공개 상태인데 `LICENSE`는 비공개(사내·고객사 전용)입니다. 공개 여부는 별도 결정 사항입니다.
- 실제 RHEL·AlmaLinux와 실서버(Rocky 9.4) 실행은 하지 않았습니다. 컨테이너는 Rocky 최소 이미지 기준입니다.
- aarch64·Ubuntu 대상은 정의만 예시로 두었고 빌드·검증하지 않았습니다.

### 2026-11-16 0.4.1 고객 관점 수정

- MD 엔티티 제거(CommonMark 이스케이프), 결합 IOMMU `None` 제거, 영문 코드에 한국어 라벨,
  공개 커널·부트 경로와 공식 문서 URL 원문 표시(호스트·IP·일련번호·`host:port`는 계속 가림),
  절대 경로 표시, `.output.lock` 제거(디렉터리 잠금), 인증서 남은 시간 기준 시각 표시, README-ko 입력·결과 옮기기 안내.
- 배포물: `aistor-diag-analyzer-0.4.1-el8-x86_64.tar.gz` 25,193,631바이트,
  SHA-256 `8899f458eaef753c0bca9c5ddfbe7bad4f47a27b91a828c71b335b0bf0d9401d`(소스 `c9333ee`).
- 테스트 137개, Rocky 8·9·10 컨테이너·실자료 검증. 항목별 상태는 계획서의 "0.4.1 고객 관점 수정".

### 2026-11-16 0.4.0 개선

계획·항목별 상태는 [`docs/IMPROVEMENT_PLAN.md`](IMPROVEMENT_PLAN.md)에 있습니다.

- 보고서: '확인된 정보'·'한계'를 사람이 읽는 표로 바꾸고, 공식 판정 줄과 분석 상태를 구분해 표시합니다.
- THP 자체 점검을 공식 `sys.mem.thp` 권장값 기준으로 바꿨습니다(이전 never 통과 기준은 0.3.x까지).
- `--lang`·`--fail-on-priority`·`--mask basic`을 R1·R2 공통으로 지원합니다.
- 결합 거부 시 시각 차(초)·허용치·양쪽 수집 시각·조치를 표시합니다. 120초 허용치는 그대로입니다.
- 입력 파일 권한·종류 안내, 수집 오류 고정 분류 집계, `sys.errors` 결측 시 최소값 표시, 실행 후 한 줄 요약.
- `run-report.sh`: 도움말·오타 안내·결과 보기 안내. `build-release.sh`: zstd 해제 경로 확대, 라이선스 원문 SHA-256 대조,
  재현 가능한 빌드, 대상 추가를 표 한 줄로.
- 배포물: `aistor-diag-analyzer-0.4.0-el8-x86_64.tar.gz` 25,186,796바이트,
  SHA-256 `2ba5b1688e7a574db2079e52c5d0d192ca64c5d3091eaf1b993efde25309be28`(소스 `639d60a`). 0.3.0·0.3.1은 유지.
- 검증: 테스트 119개 통과(개발 환경·Rocky 10 건너뜀 0, Rocky 8·9 빌드 시험 8개 건너뜀),
  Rocky 8.9·9.3·10.2 컨테이너(Python 없음, 네트워크 차단, 비root)에서 10월 12일 샘플·R3 자료 실행,
  원본 해시 전후 동일, Chrome 브라우저 점검 통과. 상세는 계획서의 "검증 결과".
- 실제 RHEL·AlmaLinux·실서버 실행, aarch64·Ubuntu 빌드는 하지 않았습니다.

### 2026-11-16 0.4.2 코드·문서 간소화

동작은 바꾸지 않고 코드 중복을 줄이고 README를 짧게 했습니다(README 412→49줄, 상세는 docs/로 이동, 배포본 README-ko 124→63줄).
108개 결과 비교에서 간소화 전과 버전 문자열 외 차이 0, Rocky 8·9·10 컨테이너 재검증 통과. 상세는 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)의 "0.4.2".

### 2026-11-16 0.4.3 남은 항목 수정

0.4.2에서 발견한 5건을 고쳤습니다: 결합 R2의 `--as-of`를 공식 부분에도 적용, CURRENT.json 하드링크 점검을 R1·R2 공용으로,
10초 처리 제한을 해석 반복마다 확인, 빌드 내려받기는 해시 확인 후에만 캐시에 저장, 시스템 Python 최소 버전을 한 곳에서만 정의.
`--as-of` 없이 실행한 결과는 0.4.2와 버전 문자열 외 동일, Rocky 8·9·10 재검증 통과. 상세는 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)의 "0.4.3".

### 2026-11-16 0.4.4 단수형 Summary·표시 문제 수정

201 서버 새 수집처럼 Summary가 `1 warning`·`1 error` 단수형이면 입력 오류(종료 코드 2)로 거부되던 문제를 고쳤습니다.
R2 결합에서 Markdown 해석 오류를 공식 Markdown으로 지목, 상대 경로 입력을 절대 경로로 표시, MD 표의 불필요한 백슬래시 제거,
입력·출력 충돌 거부 시 원인 표시. 판정·종료 코드·120초 결합 허용치는 그대로. 상세는 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)의 "0.4.4".

### 2026-11-16 0.5.0 HTML 보고서 화면 개편

HTML 보고서를 요약 대시보드, 서버×드라이브 격자, 검사 카드, THP·IOMMU·rlimit 비교표, 수집 오류 막대·히트맵, 고정 목차·심각도 필터 구성으로 바꿨습니다.
판정·숫자·Markdown·JSON·종료 코드는 0.4.4와 같습니다. 상세는 [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md)의 "0.5.0".
