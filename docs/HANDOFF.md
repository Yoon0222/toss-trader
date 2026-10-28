# 인수인계

Last updated: 2026-10-26

- 개발 경로: C:/Users/SEPC/Documents/PythonProject/toss-trader
- 화면: http://127.0.0.1:8000/ (API 문서 /docs)
- 현 UI는 FastAPI 정적 파일로 제공. Next.js는 아직 도입하지 않았다.
- 샘플 데이터 제거 완료. app/market.py 공식 읽기 전용 연결. 실제 키 미설정으로 현재 미연결 상태.
- 실행 로그는 브라우저 메모리, 새로고침하면 초기화. 관심/설정 역시 화면 세션에만 유지.
- 검증: docs/QA_REPORT.md. 실전 거래 차단 유지.

- 최신 검증: 32 통과, 1 생략, 1 경고. .env에 TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 및 계좌 조회용 TOSS_ACCOUNT_SEQ 설정 후 재시작 필요.

- 미국별도가상계좌: app/us_paper_runner.py, US10/26~10/30. 데이터 data/us-paper-validation-20261026. NYtimezone/tzdata. 실제US시세/달력조회확인, 정규장체결전. 98tests. 상세 docs/US_PAPER_WEEK.md.
