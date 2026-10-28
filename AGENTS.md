# 주식 자동매매 프로젝트

실제 개발 루트는 이 파일이 있는 toss-trader다. Python 3.12 컨테이너, FastAPI, SQLAlchemy, PostgreSQL, Redis를 사용한다. 현재 Phase 1이며 README와 docs/verification.md의 구현·미검증 범위를 따른다.

- 기본 모드 PAPER_TRADING. 명시적 실전 승인 없이 실제 계좌 주문을 전송하지 않는다. 현재 TossBroker는 실전 호출이 차단되어 있다.
- 공식 토스 문서에서 확인한 기능만 구현한다. LLM이 주문 API를 직접 호출하지 않도록 한다.
- 금액은 Decimal/Numeric, 시각은 timezone-aware. 거래 일정은 Asia/Seoul과 거래소 달력을 사용한다.
- 핵심 거래 로직 변경은 관련 pytest를 실행한다. DB 변경은 모델·Alembic·제약·호환성과 임시 DB 되돌리기를 검토한다.
- SQLite/모의 결과와 PostgreSQL/실전 결과를 구분한다. 비밀값을 코드·로그·보고서에 남기지 않는다.

## 이식한 스킬

.agents/skills/의 worklog, qa-report, run-app, web-design-guidelines, supabase-postgres-best-practices를 해당 작업에 사용한다. Postgres 스킬은 자체 PostgreSQL에 적용하고 Supabase 도입을 뜻하지 않는다.

## 역할 정의

.codex/agents/에 qa-trader, db-reviewer, design-reviewer, recorder, deploy-status, release의 TOML 정의가 있다. .claude/agents/에도 같은 역할을 제공한다.
이 파일들은 역할 지침이다. 현재 세션에 새 도구나 실행 중 에이전트가 등록되었다고 가정하지 않는다. 역할 작업을 맡길 때 해당 정의를 읽어 사용한다. 병렬 에이전트 실행은 사용자가 요청했거나 적용 지침이 허용할 때 수행한다.

피넛 전용 모바일 SDK, 계좌·키, Notion ID, Supabase ref, 배포 도메인, 자동 외부 쓰기 설정은 이 프로젝트로 가져오지 않는다.

## 사용자 지정 검토 방식

개발 작업마다 6개 역할(qa-trader, db-reviewer, design-reviewer, recorder, deploy-status, release)을 모두 실행한다. 동시 슬롯이 부족하면 순차 실행하고, 관련 변경이 없는 역할은 해당 없음과 확인 범위를 보고한다. 이것은 외부 배포·실전 주문 승인으로 간주하지 않는다.
