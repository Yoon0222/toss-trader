---
name: run-app
description: 주식 FastAPI 개발 서버를 모의 모드로 실행하거나 상태를 확인한다.
---

# 로컬 실행

피넛 run-app의 실행·검증 흐름을 FastAPI에 맞게 이식했다.
프로젝트 루트의 .venv/Scripts/python.exe(Windows) 또는 .venv/bin/python을 사용한다.
TRADING_MODE=PAPER_TRADING, LIVE_ENABLED=false, LIVE_APPROVED=false로 실행한다.
명령: python -m uvicorn app.main:app --host 127.0.0.1 --port 8000.
백그라운드는 숨김 창과 work/ 로그를 사용하고 실행한 PID를 기록한다. 포트 점유 시 프로세스를 확인하고 프로젝트 소유 서버만 재시작한다.
/health/live와 /api/status 응답을 확인한다. /health/ready의 DB·Redis 연결 실패는 별도로 보고한다.
Docker가 있다면 README의 Compose 실행 경로를 따른다. 없으면 API 실행과 서비스 통합 실행의 차이를 명시한다.
실전 모드를 활성화하거나 주문 API를 호출하지 않는다. .env 비밀값을 출력하지 않는다.
