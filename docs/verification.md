# Phase 1 검증 결과

검증일: 2026-10-26. 로컬 Windows / Python 3.11.7, 전용 가상환경.

- 최종 pytest: **17 passed, 1 skipped, 1 warning** (2.75초).
- 설정 기본값·모든 실전 모드 차단·비밀값 마스킹 검증.
- 주문 입력 6개 경계값, 동시 중복 요청, 매수·매도 잔고와 보유량, 중복키 충돌 검증.
- 오래된 시세·미래 시세·비정상 가격, 현금·보유량 부족, 금액 한도, 정지 상태 검증.
- SQLite 비동기 DB 저장 후 연결 재생성, 중복 주문 유일 제약 검증.
- API liveness, 상태 조회, Redis 연결 불가 시 readiness 503 검증.
- Alembic upgrade head → downgrade base → upgrade head: 각 종료코드 0. 초기 6개 테이블과 alembic_version 생성 확인.
- 1 skipped: 실제 PostgreSQL·Redis 서비스 연결 시험. SERVICE_INTEGRATION=1일 때 실행하며 CI 서비스 설정을 제공.
- 1 warning: Starlette TestClient의 httpx 사용 중단 예정 안내. 현재 테스트는 정상 통과. 추후 httpx2 전환 검토.

## 미검증 항목

Docker가 설치되지 않아 Python 3.12 컨테이너 빌드, Compose 실행, 실제 PostgreSQL·Redis 연결은 로컬에서 검증하지 못했습니다. 제공한 GitHub Actions도 아직 원격 실행하지 않았습니다. SQLite 검증을 PostgreSQL 검증으로 간주하지 않습니다. 토스 실계좌 인증·시세·주문은 시험하지 않았습니다.

Windows 제한 환경에서 asyncio의 내부 socketpair 생성이 멈춰, 같은 테스트를 승인된 제한 밖 실행으로 검증했습니다. 실제 외부 주문은 호출하지 않았습니다.

requirements-dev.lock은 검증한 패키지 버전, requirements.lock은 실행 의존성 버전을 고정합니다. 이 버전들을 Python 3.12/Linux에 설치할 수 있는지는 컨테이너·CI 실행 시 추가 검증해야 합니다.
