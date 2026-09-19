# accumbot — 매집 구간 추종 자동매매

영상에서 말한 방식을 규칙으로 옮긴 것입니다.

> 기관은 큰 주문을 한 번에 못 넣어서 **같은 가격대에서 며칠에 걸쳐 나눠 매수**한다.
> 그 흔적이 보이면(가격은 평평한데 거래량·자금 흐름은 늘어남) **따라 산다.**

봇이 하는 일: 신호 판정 → 시장가 진입 → **손절가·익절가**를 정해 두고 가격을 보며 시장가 청산.
기본은 **모의(paper) 모드**이고, 실거래는 설정을 바꾸고 API 키를 넣어야만 켜집니다.

> **먼저 읽어 주세요.**
> - 이 전략이 돈을 벌어 준다는 근거는 없습니다. 아래 백테스트는 거래 수가 적어 통계적으로 의미가 약합니다.
> - 영상의 "5년에 1800억" 같은 주장은 확인할 수 없는 광고성 문구입니다.
> - 실거래 전에 반드시 모의 모드로 몇 주 돌려 보고, 잃어도 되는 돈만 쓰세요.

## 전략 규칙 (모두 봉 마감 기준, 미래 참조 없음)

| 단계 | 규칙 | 기본값 |
|---|---|---|
| ① 매집 구간 | 직전 N봉의 (고점−저점)/저점 이 W 이하 | N=15, W=8% |
| ② 같은 가격 | 구간 종가 중 구간 VWAP ±b 안에 든 비율이 r 이상 | b=3%, r=70% |
| ③ 매집 증거 | 구간 평균 거래량 ≥ 이전 60봉 평균 × v, 그리고 OBV(또는 ADL)가 구간 시작보다 끝에서 높음 | v=1.0, OBV |
| ④ 진입 신호 | 종가 > 구간 고점 × (1+0.5%) 이고 거래량 ≥ 20봉 평균 × 1.0 | |
| ⑤ 손절가 | 구간 저점 − 0.5×ATR(14). 손절 거리가 12% 넘으면 진입 안 함 | |
| ⑥ 익절가 | 진입가 + 2R (R = 진입가 − 손절가) | |
| ⑦ 보조 청산 | +1R 도달 시 손절을 진입가로, 20봉 지나면 청산, (선택) ATR 트레일링 | |
| 포지션 크기 | 자본 × 1% ÷ (진입가 − 손절가), 한 종목 최대 자본의 30% | |

프리셋: `stock`(기본, 위 값), `crypto`(구간 폭 15%·밴드 5% 등 코인용 완화), `loose`(신호 많이, 품질 낮음).

## 설치 (윈도우 PowerShell 기준)

```powershell
cd trading
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q          # 11개 테스트 통과 확인
```

## 사용법

```powershell
# 백테스트 — 코인 (업비트 일봉, 최근 1400일)
python -m accumbot backtest --source upbit --symbol BTC/KRW --days 1400 --preset crypto

# 백테스트 — 국내주식 (야후 파이낸스 일봉, 수수료+세금 0.25%, 1주 단위)
python -m accumbot backtest --source yfinance --symbol 005930.KS --start 2018-01-01 --fee-bps 25 --lot 1

# 지금 신호가 있는 종목 스캔
python -m accumbot scan --source upbit --symbols BTC/KRW ETH/KRW XRP/KRW --preset crypto
python -m accumbot scan --source yfinance --symbols 005930.KS 000660.KS 035420.KS

# 파라미터 민감도 표 (과최적화 주의 문구 포함)
python -m accumbot sweep --source yfinance --symbols 005930.KS 000660.KS 035420.KS --start 2016-01-01 --fee-bps 25 --lot 1

# 모의 매매 (config.example.yaml 을 config.yaml 로 복사해 고친 뒤)
python -m accumbot run --config config.yaml              # 한 번 점검
python -m accumbot run --config config.yaml --loop 300   # 5분마다 점검 (일봉이면 하루 한 번도 충분)
python -m accumbot status --config config.yaml
```

일봉 기준이면 **봉이 마감된 직후**(업비트 일봉은 한국시간 09:00, 국내주식은 15:30 이후) 한 번 실행하면 됩니다.
윈도우 작업 스케줄러나 cron 에 `python -m accumbot run --config config.yaml` 을 걸어 두세요.
텔레그램 알림을 받으려면 환경변수 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 를 넣습니다.

## 실거래 (업비트, 코인만)

1. 업비트에서 API 키 발급 — **자산조회·주문 권한만**, 출금 권한은 끄고, **IP 허용 목록**에 봇이 도는 PC의 IP 를 넣습니다.
2. 환경변수로 키를 넣습니다 (파일에 적지 마세요).
   ```powershell
   $env:UPBIT_ACCESS_KEY="..."; $env:UPBIT_SECRET_KEY="..."
   ```
3. `config.yaml` 에서 `mode: live` 로 바꾸고 실행합니다. 실행 시 경고가 뜹니다.

동작 방식:
- 시장가 매수는 업비트 `ord_type=price`(금액 지정), 매도는 `ord_type=market`(수량 지정) 을 씁니다.
- **업비트에는 서버 쪽 손절/익절 주문이 없습니다.** 봇이 가격을 보고 있다가 손절가·익절가에 닿으면 시장가로 팝니다.
  그래서 봇이 꺼져 있으면 손절도 안 나갑니다. 실거래는 봇을 계속 켜 두거나(`--loop 60`), 짧은 간격으로 스케줄해야 합니다.
- 최소 주문 금액 5,000원, 수수료 0.05%(2026-09 기준).

국내주식은 **모의·스캔 전용**입니다(야후 파이낸스 데이터). 증권사 API(한국투자증권 등) 주문 연결은 들어 있지 않습니다.

## 백테스트 결과 (2026-09-19, 참고용)

수수료·슬리피지 포함, 자본 1천만 원, 1회 위험 1%. 거래 수가 적어 **우연일 수 있는 수준**입니다.

| 대상 | 기간 | 거래 | 승률 | 평균 R | 손익비 | 총수익률 |
|---|---|---|---|---|---|---|
| BTC/KRW (crypto 프리셋) | 2022-11 ~ 2026-09 | 12 | 42% | +0.45 | 2.39 | +5.4% |
| ETH/KRW (crypto) | 같은 기간 | 5 | 40% | +0.27 | 2.28 | +1.4% |
| XRP/KRW (crypto) | 같은 기간 | 2 | 50% | +0.99 | — | +2.0% |
| SOL/KRW (crypto) | 같은 기간 | 1 | 0% | −0.01 | 0 | −0.0% |
| DOGE/KRW (crypto) | 같은 기간 | 2 | 50% | +0.39 | — | +0.8% |
| 삼성전자 005930 (stock) | 2018-01 ~ 2026-09 | 8 | 88% | +0.55 | 18.5 | +4.3% |
| SK하이닉스 000660 (stock) | 같은 기간 | 1 | 0% | −0.17 | 0 | −0.2% |
| NAVER 035420 (stock) | 같은 기간 | 2 | 0% | −0.71 | 0 | −1.3% |
| 현대차 005380 (stock) | 같은 기간 | 3 | 67% | −0.02 | 1.01 | 0.0% |
| LG화학 051910 (stock) | 같은 기간 | 1 | 0% | −0.93 | 0 | −0.9% |
| 셀트리온 068270 (stock) | 같은 기간 | 2 | 0% | −0.55 | 0 | −0.9% |

국내주식 9종목 × 2016~2026 민감도 표(`sweep`)에서는 구간 폭 8~12%·돌파 거래량 1.0× 조합이 거래 22~49건, 평균 R +0.1~+0.2, 손익비 1.3~2.1 이었지만 **9종목 중 3종목만 플러스**였습니다. 돌파 거래량을 1.5×, 2.0× 로 올리면 성과가 나빠졌습니다.

읽는 법: 총수익률이 작은 것은 1회 위험을 자본의 1%로 잡았기 때문입니다. 평균 R 이 +0.2~0.3 이면 "손실 1 걸고 평균 0.2~0.3 남긴다"는 뜻이고, 이 정도 거래 수로는 진짜 우위인지 판단할 수 없습니다.
`sweep` 으로 파라미터를 넓게 바꿔 보고, 여러 종목·여러 기간에서 고르게 플러스인지 확인한 뒤에만 실거래를 고려하세요.

## 파일

```
trading/
  accumbot/
    indicators.py  ATR · OBV · ADL · 구간 통계
    strategy.py    규칙 ①~⑦ → signal / stop
    backtest.py    다음 봉 시가 체결, 봉 내 손절·익절, 수수료, 지표
    data.py        yfinance · 업비트(ccxt) · CSV 로더
    broker.py      PaperBroker · UpbitBroker · 상태 파일
    engine.py      실시간 점검 루프 (모의/실거래)
    notify.py      텔레그램 알림(선택)
    sweep.py       프리셋 · 민감도 표
    cli.py         명령줄
  tests/           합성 데이터 테스트 11개
  config.example.yaml
  requirements.txt
```
