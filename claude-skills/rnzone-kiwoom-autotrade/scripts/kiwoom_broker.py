#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""키움증권 REST API 미국주식 주문 래퍼.

공식 키움 패키지(PyPI: kwcli, import: kiwoom) 위에서 동작한다. 인증·토큰
갱신·연속조회는 그 패키지의 get_client()가 전담하고, 이 모듈은 RN존 자동매매에
필요한 최소 기능(거래소 확인, 주문가능수량, 예수금, 잔고, 미체결, 지정가 매수·매도, 취소)만 감싼다.

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

    client = get_client()
    last_error: Exception | None = None
    for i, stex_tp in enumerate(EXCHANGE_CANDIDATES):
        if i > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
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
    client = get_client()
    response = client.fetch_page(
        api_id="ust31490",
        path="/api/us/ordr",
        body={"stk_cd": stk_cd, "uv": f"{price:.4f}", "stex_tp": stex_tp},
    )
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
    return response.body


def place_limit_sell(stk_cd: str, stex_tp: str, qty: int, price: float) -> dict:
    """ust20001 — 미국주식 매도 주문(지정가 전용, trde_tp=00)."""
    if qty < 1:
        raise ValueError("qty는 1 이상이어야 합니다.")
    client = get_client()
    response = client.fetch_page(
        api_id="ust20001",
        path="/api/us/ordr",
        body={
            "stk_cd": stk_cd,
            "stex_tp": stex_tp,
            "ord_qty": str(qty),
            "trde_tp": "00",
            "ord_uv": format_order_price(price),
        },
    )
    return response.body


def cancel_order(orig_ord_no: str, stex_tp: str, stk_cd: str) -> dict:
    """ust20003 — 미국주식 주문 취소(미체결 잔량 전부)."""
    client = get_client()
    response = client.fetch_page(
        api_id="ust20003",
        path="/api/us/ordr",
        body={"orig_ord_no": orig_ord_no, "stex_tp": stex_tp, "stk_cd": stk_cd},
    )
    return response.body


def _num(raw, default: float = 0.0) -> float:
    """키움 응답 숫자 문자열("+000123.45", "1,234" 등)을 float로 변환."""
    if raw in (None, ""):
        return default
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _all_rows(api_id: str, path: str, body: dict, list_key: str = "result_list") -> tuple[list, dict]:
    """연속조회를 끝까지 따라가 결과 리스트를 모은다. (rows, 첫 페이지 body) 반환."""
    client = get_client()
    rows: list = []
    first: dict | None = None
    for response in client.iterate_pages(api_id=api_id, path=path, body=body, max_pages=0,
                                         page_delay_seconds=REQUEST_DELAY_SECONDS):
        if first is None:
            first = response.body
        rows.extend(r for r in (response.body.get(list_key) or []) if isinstance(r, dict))
    return rows, first or {}


def get_holdings() -> dict:
    """ust21070 — 미국주식 원장 잔고. {종목: {qty, sellable, avg, now}}"""
    rows, _ = _all_rows("ust21070", "/api/us/acnt", {"stex_tp": "", "stk_cd": ""})
    out = {}
    for r in rows:
        sym = (r.get("stk_cd") or "").strip().upper()
        qty = int(_num(r.get("poss_qty")))
        if not sym or qty <= 0:
            continue
        out[sym] = {
            "qty": qty,
            "sellable": int(_num(r.get("sell_alowq"), qty)),
            "avg": _num(r.get("frgn_stk_book_uv")),
            "now": _num(r.get("now_pric")),
        }
    return out


def get_open_orders() -> list:
    """ust21050 — 오늘 미체결 주문. [{sym, side(buy/sell), price, remaining, ord_no}]"""
    rows, _ = _all_rows("ust21050", "/api/us/acnt",
                        {"ord_dt": "", "slby_tp": "0", "stex_tp": "", "stk_cd": ""})
    out = []
    for r in rows:
        remaining = int(_num(r.get("ord_remnq")))
        if remaining <= 0:
            continue
        side_name = str(r.get("slby_tp_nm") or "")
        side_code = str(r.get("slby_tp") or "").strip()
        side = "sell" if ("매도" in side_name or side_code == "1") else "buy"
        out.append({
            "sym": (r.get("stk_cd") or "").strip().upper(),
            "side": side,
            "price": _num(r.get("ord_uv")),
            "remaining": remaining,
            "ord_no": str(r.get("ord_no") or "").strip(),
        })
    return out


def get_cash() -> dict:
    """ust21110 — {usd: 외화(USD) 주문가능금액, krw: 원화예수금}"""
    client = get_client()
    body = client.fetch_page(api_id="ust21110", path="/api/us/acnt", body={}).body
    usd = 0.0
    for row in body.get("result_list", []) or []:
        if row.get("crnc_code") == "USD":
            usd = _num(row.get("fc_ord_alowa"))
    return {"usd": usd, "krw": _num(body.get("krw_entra"))}


def compute_quantity(amount_usd: float, price: float) -> int:
    if price <= 0:
        return 0
    return math.floor(amount_usd / price)
