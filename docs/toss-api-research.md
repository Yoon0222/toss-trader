# 토스증권 공식 API 조사

확인일: 2026-10-26 (Asia/Seoul). 문서 확인은 공개 자료 기준이며 계좌 로그인·키 발급·실제 API 호출은 수행하지 않았습니다. OpenAPI info.version: 1.2.24, 스펙 버전 3.1.0.

공식 출처:
- https://developers.tossinvest.com/llms.txt
- https://openapi.tossinvest.com/openapi-docs/overview.md
- https://openapi.tossinvest.com/openapi-docs/faq.md
- https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- https://openapi.tossinvest.com/openapi-docs/latest/asyncapi.json

## 확인된 기능

| 항목 | 조사 결과 |
|---|---|
| 신청·키 발급 | 토스증권 WTS 로그인 → 설정 → Open API에서 client_id/client_secret 발급. 개인별 가입·자격 조건의 상세 심사 여부는 미확인 |
| 인증 | POST /oauth2/token, OAuth2 Client Credentials, form-urlencoded. Bearer 토큰. 클라이언트당 유효 토큰 1개, 재발급 시 이전 토큰 즉시 무효화. refresh token 없음 |
| IP | WTS 허용 IP 관리에 호출 IP 등록, 미등록 IP 차단. 운영 서버의 고정 공인 IP 확보 필요 |
| REST | https://openapi.tossinvest.com |
| 시세 | 국내·미국 현재가, 호가, 당일 최근 체결, 1분·일봉. 현재가 다건 최대 200종목. 과거 틱 전체 이력 제공은 확인되지 않음 |
| 실시간 | wss://openapi-ws.tossinvest.com/ws/v1, Bearer handshake. 국내 체결·호가·본인 주문 이벤트 |
| WS 한도 | 계정당 동시 2연결, 연결당 codes 합산 100구독(채널별 별도 계산). 180초 무송신 시 종료, 60초 ping 권장. 재접속 시 누락된 주문 이벤트는 재전송되지 않아 REST 대사 필요 |
| 주문 | 지정가·시장가 주문, 정정·취소. 계좌 토큰에 X-Tossinvest-Account 필요. 실제 주문 가능 여부는 주문 시점 계좌·시장·주문 조건에 따름 |
| 계좌·잔고 | GET /api/v1/accounts, 보유 자산, 매수 가능 금액·매도 가능 수량·수수료 조회 |
| 체결 | 주문 목록·상세. REST 목록은 주문별 합산이며 체결 이벤트 건별 내역과 다름. 실시간 주문 이벤트 활용 필요 |
| 달력 | 국내 KRX·NXT 장 운영 캘린더 제공. 휴장·세션 변경 판단에 활용 가능 |
| 모의투자 | 조사한 공식 문서에서 별도 모의투자 서버 제공을 확인하지 못함. 미지원으로 단정하지 않으며 자체 Paper Engine을 준비 |
| 실전 전제 | 정상 계좌, 발급된 클라이언트, 허용 IP, 유효 토큰, accountSeq, 계좌 주문 권한·제약 확인. 상세 개인 자격은 WTS에서 추가 확인 |

## REST 요청 한도

overview 기준 클라이언트 × 그룹별 TPS: AUTH 5, ACCOUNT 1, ASSET 5, STOCK 5, STOCK_ALL 1, MARKET_INFO 3, MARKET_DATA 15, MARKET_DATA_CHART 20, ORDER 10, ORDER_HISTORY 5, ORDER_INFO 6(09:00~09:10에는 3). 매수·매도를 합산합니다. 수치는 변경될 수 있으므로 X-RateLimit-* 및 Retry-After를 실행 시 반영해야 합니다. 이 표는 주요 그룹만 발췌한 조사 요약입니다.

## 구현 상태와 제약

Phase 1의 TossBroker는 네트워크 요청 없이 예외를 발생시킵니다. 위 경로는 조사 기록이며 연동 완료를 의미하지 않습니다. 뉴스·공시 원문 수집과 AI 분석 API는 이번 조사에서 확인한 브로커 기능으로 가정하지 않습니다. 별도 허가된 뉴스 제공처·공시 데이터 원천을 선정해야 합니다. 전 시장 스캐너는 구독 한도가 있으므로 REST 후보 축소 → 선택 종목 WS 구독 방식이 필요합니다. AI의 주문 호출 권한은 부여하지 않습니다.
