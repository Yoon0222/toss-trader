---
name: qa-report
description: 주식 프로젝트의 QA 결과를 로컬 문서에 누적하고 재현 가능한 미해결 항목을 정리한다.
---

# QA 결과 기록

피넛 qa-report의 근거 중심 누적 방식을 이식했다. 저장 위치는 docs/QA_REPORT.md다.
기능명·날짜·PASS/FAIL/PARTIAL, 수용 기준별 근거, 실행 명령과 결과, 실패 재현·파일:라인, 미검증 사유를 기록한다.
미해결 항목은 High/Med/Low로 구분하고 실제 수정 확인 후에만 해결 표시한다. 기존 기록은 보존한다.
SQLite 통과를 PostgreSQL 통과로 바꾸어 적지 않는다. 모의 체결을 실전 주문 검증으로 적지 않는다.
피넛의 Notion ID·Supabase 프로젝트를 사용하지 않는다. 보고서 작성은 실전 거래나 외부 전송 권한을 부여하지 않는다.
