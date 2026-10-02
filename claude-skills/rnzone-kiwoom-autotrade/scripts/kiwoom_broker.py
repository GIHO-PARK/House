#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""키움증권 REST API 미국주식 주문 래퍼.

공식 키움 패키지(PyPI: kwcli, import: kiwoom) 위에서 동작한다. 인증·토큰
갱신·연속조회는 그 패키지의 get_client()가 전담하고, 이 모듈은 RN존 자동매매에
필요한 최소 기능(거래소 확인, 주문가능수량, 예수금, 지정가 매수)만 감싼다.

설치: pip install kwcli  (Python 3.13+ 필요 — 공식 저장소 요구사항과 동일)
인증: `kiwoomcli setup` 권장, 또는 환경변수
      KIWOOM_MODE=real|demo, APP_KEY/APP_SECRET(운영), APP_KEY_MOCK/APP_SECRET_MOCK(모의)
참고: https://github.com/Kiwoom-Securities/Kiwoom-REST-API
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from kiwoom import KiwoomError, get_client
from kiwoom.core.errors import APIError

# 키움 거래소구분: NA=AMEX, ND=NASDAQ, NY=NYSE. 종목조회 API는 거래소를 미리
# 알아야 하므로, 세 거래소를 순서대로 시도해 처음 성공하는 곳을 사용한다.
EXCHANGE_CANDIDATES = ("ND", "NY", "NA")
_EXCHANGE_CACHE_PATH = Path(__file__).with_name("exchange_cache.json")

# 키움 REST API는 API ID별로 초당 요청 수를 제한한다(예: usa10100 = 초당 5회).
# 종목별 연속 호출(거래소 확인·주문가능수량 등) 사이에 짧게 대기해 429(1700)를 피한다.
REQUEST_DELAY_SECONDS = 0.25


def _load_exchange_cache() -> dict:
    if _EXCHANGE_CACHE_PATH.exists():
        try:
            return json.loads(_EXCHANGE_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_exchange_cache(cache: dict) -> None:
    try:
        _EXCHANGE_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass


def resolve_exchange(stk_cd: str) -> str:
    """종목코드의 거래소구분(NA/ND/NY)을 확인한다. 결과는 로컬 캐시에 저장한다."""
    cache = _load_exchange_cache()
    if stk_cd in cache:
        return cache[stk_cd]

    print(f"      [거래소 확인] {stk_cd}: 캐시 없음, usa10100 조회 시작", flush=True)
    client = get_client()
    last_error: Exception | None = None
    for i, stex_tp in enumerate(EXCHANGE_CANDIDATES):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        print(f"      [거래소 확인] {stk_cd}: {stex_tp} 시도", flush=True)
        try:
            response = client.fetch_page(
                api_id="usa10100",
                path="/api/us/stkinfo",
                body={"stex_tp": stex_tp, "stk_cd": stk_cd},
            )
        except APIError as exc:
            last_error = exc
            continue
        if response.body.get("stk_cd"):
            cache[stk_cd] = stex_tp
            _save_exchange_cache(cache)
            return stex_tp
    raise KiwoomError(
        f"{stk_cd} 종목의 거래소를 확인할 수 없습니다 (NASDAQ/NYSE/AMEX 모두 실패): {last_error}"
    )


def get_orderable_quantity(stk_cd: str, stex_tp: str, price: float) -> int | None:
    """ust31490 — 해당 가격 기준 실제 주문 가능 수량.

    ``ord_alowq_100``/``ord_alowq_50``은 종목이 속한 증거금율 구간(100%/50%)에
    맞는 필드만 값이 채워지고 나머지는 0으로 내려온다 — 종목마다 어느 구간인지
    다르므로 하나만 읽으면 자금이 있어도 0으로 오판할 수 있다. 이 자동매매는
    신용(미수)을 절대 쓰지 않으므로, 증거금 구간과 무관하게 항상 유효한
    ``min_ord_alowq``(미수불가/현금 기준 주문가능수량)를 사용한다.
    """
    print(f"      [주문가능수량] {stk_cd}: ust31490 조회 시작", flush=True)
    client = get_client()
    response = client.fetch_page(
        api_id="ust31490",
        path="/api/us/ordr",
        body={"stk_cd": stk_cd, "uv": f"{price:.4f}", "stex_tp": stex_tp},
    )
    print(f"      [주문가능수량] {stk_cd}: 조회 완료", flush=True)
    raw = response.body.get("min_ord_alowq")
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def get_deposit_usd() -> float | None:
    """ust21110 — 해외주식 외화(USD) 주문가능금액을 조회한다."""
    client = get_client()
    response = client.fetch_page(api_id="ust21110", path="/api/us/acnt", body={})
    for row in response.body.get("result_list", []) or []:
        if row.get("crnc_code") == "USD":
            raw = row.get("fc_ord_alowa")
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return None


def format_order_price(price: float) -> str:
    """키움 미국주식 주문/정정 단가 형식: $1 미만은 소수점 4자리, $1 이상은
    소수점 2자리까지만 허용된다(그 이상을 보내면 return_code 1517로 거부됨).
    조회 API(ust31490 등)에는 이 제약이 없다 — 주문/정정/취소에만 적용."""
    if price < 1:
        return f"{price:.4f}"
    return f"{price:.2f}"


def place_limit_buy(stk_cd: str, stex_tp: str, qty: int, price: float) -> dict:
    """ust20000 — 미국주식 매수 주문(지정가 전용, trde_tp=00).

    RN존 스킬 자체가 "얇은 ETF는 지정가 주문 필수"라고 명시하므로 이 실행기는
    시장가(trde_tp=03 등)를 지원하지 않는다.
    """
    if qty < 1:
        raise ValueError("qty는 1 이상이어야 합니다.")
    print(f"      [주문 전송] {stk_cd}: ust20000 호출 시작", flush=True)
    client = get_client()
    response = client.fetch_page(
        api_id="ust20000",
        path="/api/us/ordr",
        body={
            "stex_tp": stex_tp,
            "stk_cd": stk_cd,
            "ord_qty": str(qty),
            "trde_tp": "00",
            "ord_uv": format_order_price(price),
        },
    )
    print(f"      [주문 전송] {stk_cd}: 응답 수신", flush=True)
    return response.body


def compute_quantity(amount_usd: float, price: float) -> int:
    if price <= 0:
        return 0
    return math.floor(amount_usd / price)
