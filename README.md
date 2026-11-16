# MinIO 진단 분석기

MinIO AIStor 진단 Markdown(mc 보고서)과 원본 diagnostics gzip을 읽어 HTML·Markdown·JSON 보고서를 만드는 오프라인 분석기입니다.
서버 접속·인터넷·설치 없이 동작하며, 배포본(`el8-x86_64`: RHEL·Rocky·AlmaLinux 8/9/10)은 Python 런타임을 동봉합니다.
공식 판정과 이 도구의 자체 점검은 별개이며, 종료 코드 0은 시스템이 정상이라는 뜻이 아닙니다.

## 빠른 시작

```bash
sha256sum -c aistor-diag-analyzer-0.4.2-el8-x86_64.tar.gz.sha256
tar -xzf aistor-diag-analyzer-0.4.2-el8-x86_64.tar.gz && cd aistor-diag-analyzer-0.4.2-el8-x86_64
./run-report.sh --check --verify-bundle                                   # 사전 점검
./run-report.sh --official-report /경로/diag-report-....md \
                --health /경로/diag-data-....json.gz --allow-partial     # 실행(--health 생략 시 공식 Markdown 단독)
```

결과는 `~/aistor-diag-output/run-<id>/aistor-diag-report.html`입니다. 서버에 브라우저가 없으면 안내되는 scp 명령으로 PC에 복사해 엽니다.

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 처리 성공(시스템 정상 판정 아님) |
| 1 | `--fail-on-priority` 조건 충족 |
| 2 | 입력 오류(권한·형식·결합 거부 포함, 원인·조치 표시) |
| 3 | 저장·보호 실패 |
| 4 | 부분 분석(제한 보고서 저장됨) |
| 5 | 지원하지 않는 실행 환경 |

## 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--official-report <md>` / `--health <json.gz>` | 입력. 두 입력은 같은 수집이어야 하며 시각 차 120초 초과·대상 불일치면 거부 |
| `--allow-partial` | 자료 부족 항목을 미평가로 두고 저장(종료 코드 4) |
| `--out-dir <dir>` / `--force` | 출력 위치(기본 `~/aistor-diag-output`) / 기존 결과가 있을 때 새 세대 추가 |
| `--mask strict\|basic` | 마스킹 수준(기본 strict). 공유 전 수동 검토 필요 |
| `--fail-on-priority P1~P4`, `--as-of <시각>`, `--lang ko` | 우선순위 기준 종료, 판정 기준 시각, 언어 |
| `--check`, `--verify-bundle`, `--help` | 사전 점검만, 배포물 체크섬 확인, 전체 옵션·예시 |

입력 파일을 읽을 수 없으면 소유자 계정으로 실행하거나 읽을 수 있는 사본을 쓰십시오. 원본 권한은 바꾸지 않습니다.

## 문서

- [docs/USAGE.md](docs/USAGE.md): 현재 상태, 소스 실행, 분석 범위·제약
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md): 테스트, 빌드, Git 관리·커밋 규칙
- [docs/HISTORY.md](docs/HISTORY.md): 날짜별 변경·검증 이력
- [docs/IMPROVEMENT_PLAN.md](docs/IMPROVEMENT_PLAN.md): 개선 항목별 상태, [R1_ACCEPTANCE.md](R1_ACCEPTANCE.md): R1 인수 기준
- 라이선스: 사내·고객사 전용(`LICENSE`), 런타임 제3자 고지는 배포본 `licenses/`
