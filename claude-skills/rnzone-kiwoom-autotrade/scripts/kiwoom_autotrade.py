#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RN존 듀얼전략 → 키움증권 REST API 실주문 실행기.

동작:
1. rnzone_report.py --json 을 호출해 오늘의 "신규 진입 대상"(신규 투자자 기준) 신호를
   구조화 데이터로 받는다. 사람이 읽는 리포트와 완전히 같은 계산 로직을 사용한다
   (rnzone_report.py 내부 entries 리스트가 유일한 출처 — 이 스크립트는 신호를 다시
   계산하지 않는다).
2. 각 신호에 대해 거래소를 확인하고, 지정가 매수 주문을 계획한다.
3. 기본은 무조건 DRY-RUN(계획만 출력, 실주문 없음). 실주문을 내려면 --live 플래그와
   환경변수 KIWOOM_LIVE_TRADING=YES 를 모두 설정해야 한다(이중 안전장치).
4. 같은 날 같은 종목/단계 주문은 로컬 상태 파일(executed_signals.json)로 중복 제출을 막는다.
5. 모든 시도(성공/실패/드라이런)를 order_log.jsonl에 기록한다.

⚠️ 매수(신규 진입) 자동화만 지원한다. 목표가(+20%)/본절가/기간청산 매도 자동화는 아직
연결되어 있지 않다 — 실제 체결 수량·평단은 반드시 증권사 계좌 조회로 재확인할 것.
매도는 아직 사람이 직접 처리해야 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RNZONE_SCRIPT = (SCRIPT_DIR / ".." / ".." / "rnzone-report" / "scripts" / "rnzone_report.py").resolve()
STATE_PATH = SCRIPT_DIR / "executed_signals.json"
LOG_PATH = SCRIPT_DIR / "order_log.jsonl"

sys.path.insert(0, str(SCRIPT_DIR))
import kiwoom_broker as broker  # noqa: E402
from kiwoom import KiwoomError  # noqa: E402


def fetch_signals(timeout: int = 180) -> dict:
    if not RNZONE_SCRIPT.exists():
        raise SystemExit(
            f"rnzone_report.py를 찾을 수 없습니다: {RNZONE_SCRIPT}\n"
            "rnzone-report 스킬과 이 스킬이 같은 상위 폴더(claude-skills/ 또는 ~/.claude/skills/)에 "
            "나란히 있어야 합니다."
        )
    result = subprocess.run(
        [sys.executable, str(RNZONE_SCRIPT), "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SystemExit(f"rnzone_report.py 실행 실패:\n{result.stderr}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"rnzone_report.py --json 출력 파싱 실패: {exc}\n---\n{result.stdout}")
    if not payload.get("ok"):
        raise SystemExit(f"rnzone_report.py 신호 생성 실패: {payload}")
    if not payload.get("new_investor_mode"):
        raise SystemExit(
            "이 실행기는 NEW_INVESTOR_MODE=True(신규 투자자) 리포트만 지원합니다. "
            "rnzone_report.py 상단의 NEW_INVESTOR_MODE 상수를 True로 두세요."
        )
    return payload


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _log(event: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _signal_key(data_date: str, sym: str, stage: int) -> str:
    return f"{data_date}:{sym}:{stage}"


def plan_order(sym: str, amount_usd: float, entry_price: float, max_order_usd: float | None) -> dict:
    exchange = broker.resolve_exchange(sym)
    price = round(entry_price, 4)
    qty = broker.compute_quantity(amount_usd, price)
    orderable = broker.get_orderable_quantity(sym, exchange, price)
    capped_reasons = []
    if orderable is not None and orderable < qty:
        capped_reasons.append(f"주문가능수량({orderable}) < 계획수량({qty})")
        qty = orderable
    if max_order_usd is not None:
        max_qty = broker.compute_quantity(max_order_usd, price)
        if max_qty < qty:
            capped_reasons.append(f"KIWOOM_MAX_ORDER_USD(${max_order_usd:g}) 한도 적용")
            qty = max_qty
    return {
        "sym": sym,
        "exchange": exchange,
        "price": price,
        "planned_amount_usd": round(amount_usd, 2),
        "qty": qty,
        "orderable_qty": orderable,
        "capped_reason": "; ".join(capped_reasons) or None,
    }


def execute(payload: dict, *, live: bool, max_order_usd: float | None) -> None:
    data_date = payload["data_date"]
    state = _load_state()
    entries = payload.get("entries", [])

    print(f"=== RN존 → 키움 자동매매 실행 계획 ({data_date}, {'🔴 LIVE' if live else '🟡 DRY-RUN'}) ===")
    if not entries:
        print("오늘 신규 진입 신호가 없습니다. 실행할 주문이 없습니다.")
        return

    for entry in entries:
        legs = [(entry["sym"], entry["amount_usd"], entry["entry_price_usd"], entry["stage"], False)]
        wp = entry.get("weekly_pay")
        if wp and wp.get("est_price_usd"):
            legs.append((wp["sym"], wp["amount_usd"], wp["est_price_usd"], entry["stage"], True))
        elif wp:
            print(f"  ⚠️ {wp['sym']} 주배당 가격 추정 불가 — 주배당 주문은 건너뜁니다 (수동 확인 필요)")

        for sym, amount_usd, price, stage, is_wp in legs:
            key = _signal_key(data_date, sym, stage)
            if key in state:
                print(f"  - {sym}: 이미 처리됨({state[key]}) — 건너뜀")
                continue
            time.sleep(broker.REQUEST_DELAY_SECONDS)
            try:
                plan = plan_order(sym, amount_usd, price, max_order_usd)
            except KiwoomError as exc:
                print(f"  ✗ {sym}: 주문 계획 실패 — {exc}")
                _log({"sym": sym, "stage": stage, "status": "plan_failed", "error": str(exc), "live": live})
                continue

            note = f" ({plan['capped_reason']})" if plan["capped_reason"] else ""
            kind = "주배당 적립" if is_wp else "트레이딩 진입"
            print(
                f"  - [{kind}] {sym} [{plan['exchange']}] 지정가 {plan['price']}$ x {plan['qty']}주 "
                f"(예산 ${plan['planned_amount_usd']}){note}"
            )

            if plan["qty"] < 1:
                print("    → 수량 0, 주문하지 않음")
                _log({**plan, "stage": stage, "status": "skipped_zero_qty", "live": live})
                continue

            if not live:
                _log({**plan, "stage": stage, "status": "dry_run", "live": False})
                continue

            try:
                response = broker.place_limit_buy(sym, plan["exchange"], plan["qty"], plan["price"])
            except KiwoomError as exc:
                print(f"    ✗ 주문 실패: {exc}")
                _log({**plan, "stage": stage, "status": "order_failed", "error": str(exc), "live": True})
                continue

            order_no = response.get("ord_no")
            print(f"    ✓ 주문 접수: 주문번호 {order_no}")
            _log({**plan, "stage": stage, "status": "order_placed", "order_no": order_no, "live": True,
                  "response": response})
            state[key] = datetime.now(timezone.utc).isoformat()
            _save_state(state)
            time.sleep(0.3)


def main() -> None:
    parser = argparse.ArgumentParser(description="RN존 → 키움 자동매매 실행기")
    parser.add_argument("--live", action="store_true", help="실제 주문을 제출한다 (기본은 dry-run)")
    parser.add_argument(
        "--max-order-usd", type=float, default=None,
        help="주문 1건당 최대 금액(USD) 상한. 환경변수 KIWOOM_MAX_ORDER_USD로도 설정 가능",
    )
    args = parser.parse_args()

    live = bool(args.live) and os.getenv("KIWOOM_LIVE_TRADING", "").strip().upper() == "YES"
    if args.live and not live:
        print(
            "⚠️ --live 플래그는 켜졌지만 환경변수 KIWOOM_LIVE_TRADING=YES 가 없어 "
            "DRY-RUN으로 강제 전환합니다 (이중 안전장치)."
        )

    max_order_usd = args.max_order_usd
    if max_order_usd is None and os.getenv("KIWOOM_MAX_ORDER_USD"):
        try:
            max_order_usd = float(os.environ["KIWOOM_MAX_ORDER_USD"])
        except ValueError:
            raise SystemExit("KIWOOM_MAX_ORDER_USD 값이 숫자가 아닙니다.")

    payload = fetch_signals()
    execute(payload, live=live, max_order_usd=max_order_usd)


if __name__ == "__main__":
    main()
