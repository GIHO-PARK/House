#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RN존 (지수·섹터 레버리지 + 본주) → 키움증권 REST API 자동매매 실행기.

하루 한 번, 미국 정규장이 열린 직후 실행한다(예: 한국시간 23:45). 키움 미국주식
지정가는 당일만 유효하므로 매일 다시 건다. 주문을 넣은 뒤에는 PC가 꺼져 있어도
키움 서버에서 체결된다.

한 번 실행할 때 하는 일:
1. rnzone_report.py --json 으로 종목별 1·2·3차 매수선과 차수별 주문금액(plan)을 받는다.
   사람이 읽는 리포트와 같은 계산이다 — 이 스크립트는 신호를 다시 계산하지 않는다.
2. 키움 계좌의 잔고·미체결·예수금을 읽고, 로컬 장부(positions.json)와 맞춘다.
   어제 건 주문이 체결됐으면 장부에 차수를 올리고, 잔고에서 사라졌으면 청산으로 기록한다.
3. 주문을 만든다(우선순위 순서):
   ① 매도 — 기간청산(레버 42일·본주 63일, 종가 -3% 지정가) / 2차 이후 본절(평단) / 목표(평단 +20%)
   ② 본주 체결분의 주배당 ETF 적립
   ③ 보유 종목의 다음 차수(2차·3차) 매수
   ④ 신규 1차 매수 — 매수 대기 종목 중 매수선에 가까운 순. 동시 보유 7종목(대기 주문 포함),
      반도체·기술 계열 2종목 상한, 남는 현금으로 그 종목의 2차까지 감당될 때만.
4. 기본은 DRY-RUN(계획만 출력). 실주문은 --live 와 환경변수 KIWOOM_LIVE_TRADING=YES 둘 다 필요.
5. 모든 시도는 order_log.jsonl 에 기록한다.

비상금(RESERVE_KRW)은 주문에 쓰지 않는다. 미수가 생기지 않도록 주문 직전 키움
'미수불가 주문가능수량'으로 한 번 더 확인한다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RNZONE_SCRIPT = (SCRIPT_DIR / ".." / ".." / "rnzone-report" / "scripts" / "rnzone_report.py").resolve()
STATE_PATH = SCRIPT_DIR / "positions.json"
LOG_PATH = SCRIPT_DIR / "order_log.jsonl"

TIMEOUT_SELL_DISCOUNT = 0.03   # 기간청산은 직전 종가보다 3% 낮은 지정가(사실상 즉시 체결)
WP_BUY_PREMIUM = 0.01          # 주배당 ETF는 직전 종가 +1% 지정가로 적립
CIRCUIT_BREAKER = -0.30        # 트레이딩 평가손이 운용금의 -30% 이하면 신규 1차 진입 중단
PRICE_TOL = 0.005              # 같은 가격 주문으로 볼 허용 오차($)


# ── 장부 ─────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            state.setdefault("positions", {})
            state.setdefault("pending", {})
            state.setdefault("closed", [])
            return state
        except Exception:
            pass
    return {"positions": {}, "pending": {}, "closed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def log_event(event: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── 신호 ─────────────────────────────────────────────────

def fetch_signals(timeout: int = 300) -> dict:
    if not RNZONE_SCRIPT.exists():
        raise SystemExit(f"rnzone_report.py를 찾을 수 없습니다: {RNZONE_SCRIPT}")
    print("[1/3] 오늘의 신호 계산 중 (rnzone_report.py 실행, 야후 파이낸스 조회로 1~2분 소요)...", flush=True)
    t0 = time.monotonic()
    result = subprocess.run([sys.executable, str(RNZONE_SCRIPT), "--json"],
                            capture_output=True, text=True, timeout=timeout)
    print(f"[1/3] 신호 계산 완료 ({time.monotonic() - t0:.1f}초)", flush=True)
    if result.returncode != 0:
        raise SystemExit(f"rnzone_report.py 실행 실패:\n{result.stderr}")
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"rnzone_report.py --json 출력 파싱 실패: {exc}\n---\n{result.stdout}")
    if not payload.get("ok"):
        raise SystemExit(f"rnzone_report.py 신호 생성 실패: {payload}")
    if "plan" not in payload:
        raise SystemExit("rnzone_report.py가 plan을 내보내지 않습니다 — 두 스킬을 같은 버전으로 맞추세요.")
    return payload


# ── 주문 계획 (순수 함수: 계좌·장부·신호 → 주문 목록) ─────────────

def _held_days(dates: list, entry_date: str, data_date: str) -> int:
    return sum(1 for d in dates if entry_date < d <= data_date)


def _ceil_cent(x: float) -> float:
    return math.ceil(x * 100 - 1e-9) / 100


def build_actions(payload: dict, account: dict, state: dict, max_order_usd: float | None = None) -> dict:
    """주문 목록과 갱신된 장부를 만든다. 계좌·주문 API는 호출하지 않는다.

    account = {"cash_usd", "cash_krw", "krw_order": bool,
               "holdings": {sym: {qty, sellable, avg, now}},
               "open_orders": [{sym, side, price, remaining, ord_no}]}
    """
    fx = payload["fx_krw_per_usd"]
    data_date = payload["data_date"]
    dates = payload.get("trading_dates", [])
    plan = {p["sym"]: p for p in payload["plan"]}
    holdings = account["holdings"]
    positions = {k: dict(v) for k, v in state.get("positions", {}).items()}
    pending = state.get("pending", {})
    closed = list(state.get("closed", []))
    notes, actions = [], []

    # 1) 장부 맞추기 — 어제 걸어둔 주문의 체결/청산 반영
    new_fills = []
    for sym, rec in list(positions.items()):
        h = holdings.get(sym)
        if not h:
            closed.append({"sym": sym, "date": data_date, "entry_date": rec["entry_date"],
                           "tranches": rec["tranches"], "avg": rec.get("avg")})
            notes.append(f"🏁 {sym} 청산 확인 (잔고 0) — 장부에서 제거")
            del positions[sym]
            continue
        p = pending.get(sym)
        if h["qty"] > rec.get("qty", 0) and p and p.get("tranche", 0) > max(rec["tranches"]):
            rec["tranches"].append(p["tranche"])
            new_fills.append((sym, p["tranche"]))
            notes.append(f"✅ {sym} {p['tranche']}차 체결 확인 ({rec.get('qty', 0)} → {h['qty']}주)")
        rec["qty"], rec["avg"] = h["qty"], h["avg"]
    for sym, p in pending.items():
        if p.get("tranche") == 1 and sym not in positions and holdings.get(sym, {}).get("qty", 0) > 0:
            h = holdings[sym]
            positions[sym] = {k: p[k] for k in ("category", "group", "b1", "lines", "amount_usd",
                                                 "hold_limit_days", "weekly_pay") if k in p}
            positions[sym].update({"entry_date": data_date, "tranches": [1], "qty": h["qty"], "avg": h["avg"]})
            new_fills.append((sym, 1))
            notes.append(f"✅ {sym} 1차 체결 확인 ({h['qty']}주 @ ${h['avg']:.2f}) — 장부 등록")
    managed = set(positions) | set(plan)
    unmanaged = sorted(s for s in holdings if s not in managed and s not in wp_syms)
    if unmanaged:
        notes.append("ℹ️ 장부 밖 보유 종목(자동매매가 건드리지 않음): " + ", ".join(unmanaged))

    # 예산: 비상금을 뺀 현금. 원화주문을 안 쓰면 달러 주문가능금액이 상한.
    # 같은 날 다시 실행하면 우리 대기 매수주문이 주문가능금액을 이미 묶고 있으므로 되돌려 더한다
    # (아래 6)에서 같은 주문은 유지·재차감되고, 나머지는 취소된다).
    wp_syms = {p["weekly_pay"]["sym"] for p in plan.values() if p.get("weekly_pay")}
    ours = set(positions) | set(plan) | wp_syms
    held_back = sum(o["price"] * o["remaining"] for o in account.get("open_orders", [])
                    if o["side"] == "buy" and o["sym"] in ours)
    cash_usd = account["cash_usd"] + held_back
    total_krw = cash_usd * fx + account["cash_krw"]
    budget = (total_krw - payload["reserve_krw"]) / fx
    if not account.get("krw_order"):
        budget = min(budget, cash_usd)
    budget = max(0.0, budget)
    start_budget = budget

    def capped(amount):
        return min(amount, max_order_usd) if max_order_usd else amount

    new_pending = {}

    # 2) 매도 — 보유 종목마다 하나
    for sym, rec in positions.items():
        h = holdings[sym]
        held = _held_days(dates, rec["entry_date"], data_date)
        rec["held_days"] = held
        close = plan.get(sym, {}).get("close") or h.get("now") or rec["avg"]
        qty = h.get("sellable") or h["qty"]
        if held >= rec["hold_limit_days"]:
            price, why = round(close * (1 - TIMEOUT_SELL_DISCOUNT), 2), f"기간청산 {held}/{rec['hold_limit_days']}일"
        elif max(rec["tranches"]) >= 2:
            price, why = _ceil_cent(rec["avg"]), "본절(2차 이후 평단)"
        else:
            price, why = _ceil_cent(rec["avg"] * 1.2), "목표 +20%"
        rec["timeout"] = held >= rec["hold_limit_days"]
        if qty > 0:
            actions.append({"type": "sell", "sym": sym, "qty": qty, "price": price, "why": why})

    # 3) 주배당 적립 — 어제 체결된 본주 차수만큼
    for sym, tranche in new_fills:
        wp = positions[sym].get("weekly_pay") if sym in positions else None
        if not wp or not wp.get("close"):
            continue
        amt = capped(wp["amount_usd"][str(tranche)])
        price = round(wp["close"] * (1 + WP_BUY_PREMIUM), 2)
        qty = math.floor(min(amt, budget) / price)
        if qty >= 1:
            budget -= qty * price
            actions.append({"type": "buy", "sym": wp["sym"], "qty": qty, "price": price,
                            "why": f"주배당 적립 ({sym} {tranche}차 연동)"})
        else:
            notes.append(f"⚠️ {wp['sym']} 적립 건너뜀 — 현금 부족")

    # 4) 보유 종목의 다음 차수 매수 (신규 진입보다 먼저, 2차를 3차보다 먼저)
    for sym, rec in sorted(positions.items(), key=lambda kv: max(kv[1]["tranches"])):
        nxt = max(rec["tranches"]) + 1
        if rec.get("timeout") or nxt > 3 or not rec["lines"].get(str(nxt)):
            continue
        price = round(rec["lines"][str(nxt)], 2)
        amt = capped(rec["amount_usd"][str(nxt)])
        qty = math.floor(min(amt, budget) / price)
        if qty < 1:
            notes.append(f"⚠️ {sym} {nxt}차 주문 못 함 — 현금 부족 (필요 ${amt:,.0f}, 남은 ${budget:,.0f})")
            continue
        if budget < amt:
            notes.append(f"⚠️ {sym} {nxt}차 일부만 주문 — 현금 부족 ({qty}주, 필요 ${amt:,.0f})")
        budget -= qty * price
        actions.append({"type": "buy", "sym": sym, "qty": qty, "price": price, "why": f"{nxt}차 매수"})
        new_pending[sym] = {"tranche": nxt, "date": data_date}

    # 5) 신규 1차 — 매수 대기 종목, 매수선에 가까운 순
    pnl = sum((holdings[s].get("now", 0) - r["avg"]) * holdings[s]["qty"] for s, r in positions.items()
              if holdings[s].get("now"))
    operating = (payload["capital_krw"] - payload["reserve_krw"]) / fx
    if operating > 0 and pnl / operating <= CIRCUIT_BREAKER:
        notes.append(f"🛑 회로차단기: 트레이딩 평가손 {pnl / operating:.0%} — 신규 1차 진입 중단")
        cands = []
    else:
        cands = [p for p in plan.values() if p["waiting"] and p["sym"] not in positions]
        for p in [p for p in cands if p["sym"] in holdings]:
            notes.append(f"⏸️ {p['sym']} 1차 보류 — 장부 밖에서 이미 보유 중")
        cands = [p for p in cands if p["sym"] not in holdings]
        cands.sort(key=lambda p: p["close"] / p["lines"]["1"])
    slots = payload["max_concurrent"] - len(positions)
    group_count = {}
    for rec in positions.values():
        if rec.get("group"):
            group_count[rec["group"]] = group_count.get(rec["group"], 0) + 1
    for p in cands:
        sym = p["sym"]
        if slots <= 0:
            notes.append(f"⏸️ {sym} 1차 보류 — 동시 보유 {payload['max_concurrent']}종목 상한")
            continue
        g = p.get("group")
        if g and group_count.get(g, 0) >= payload["max_per_group"]:
            notes.append(f"⏸️ {sym} 1차 보류 — {g} 계열 {payload['max_per_group']}종목 상한")
            continue
        price = round(p["lines"]["1"], 2)
        amt1 = capped(p["amount_usd"]["1"])
        # 체결되면 다음 날 2차까지 넣을 수 있어야 들어간다. 대기 주문이 묶는 돈은 1차 금액뿐이라
        # 1차만 예산에서 뺀다(여러 종목이 같은 날 한꺼번에 체결되면 2차가 일부 밀릴 수 있음).
        need = amt1 + p["amount_usd"]["2"]
        if budget < need:
            notes.append(f"⏸️ {sym} 1차 보류 — 2차까지 필요 ${need:,.0f} > 남은 현금 ${budget:,.0f}")
            continue
        qty = math.floor(amt1 / price)
        if qty < 1:
            continue
        budget -= qty * price
        slots -= 1
        if g:
            group_count[g] = group_count.get(g, 0) + 1
        dist = (price / p["close"] - 1) * 100
        actions.append({"type": "buy", "sym": sym, "qty": qty, "price": price,
                        "why": f"1차 매수 (현재가 대비 {dist:+.1f}%)"})
        new_pending[sym] = {"tranche": 1, "date": data_date,
                            **{k: p[k] for k in ("category", "group", "b1", "lines", "amount_usd",
                                                 "hold_limit_days", "weekly_pay") if k in p}}

    # 6) 이미 걸려 있는 같은 주문은 건너뛰고, 우리 종목의 다른 가격 주문은 취소
    final, cancels = [], []
    open_orders = [o for o in account.get("open_orders", []) if o["sym"] in ours]
    used = set()
    for a in actions:
        dup = next((i for i, o in enumerate(open_orders) if i not in used and o["sym"] == a["sym"]
                    and o["side"] == a["type"] and abs(o["price"] - a["price"]) <= PRICE_TOL), None)
        if dup is not None:
            used.add(dup)
            notes.append(f"↩️ {a['sym']} {a['why']} — 같은 주문이 이미 걸려 있음")
            continue
        final.append(a)
    for i, o in enumerate(open_orders):
        if i not in used:
            cancels.append({"type": "cancel", "sym": o["sym"], "ord_no": o["ord_no"],
                            "why": f"이전 {o['side']} ${o['price']} 주문 정리"})

    return {"actions": cancels + final, "notes": notes,
            "state": {"positions": positions, "pending": new_pending, "closed": closed[-200:],
                      "last_run": {"data_date": data_date, "at": datetime.now(timezone.utc).isoformat()}},
            "budget_start": start_budget, "budget_left": budget}


# ── 실행 ─────────────────────────────────────────────────

def read_account() -> dict:
    import kiwoom_broker as broker
    print("[2/3] 키움 계좌 조회 중 (예수금 → 잔고 → 미체결)...", flush=True)
    cash = broker.get_cash()
    time.sleep(broker.REQUEST_DELAY_SECONDS)
    holdings = broker.get_holdings()
    time.sleep(broker.REQUEST_DELAY_SECONDS)
    open_orders = broker.get_open_orders()
    return {"cash_usd": cash["usd"], "cash_krw": cash["krw"],
            "krw_order": os.getenv("KIWOOM_KRW_ORDER", "").strip().upper() == "YES",
            "holdings": holdings, "open_orders": open_orders}


def execute(actions: list, *, live: bool) -> list:
    """주문 목록을 제출한다. 실패한 매수 종목 목록을 돌려준다(장부에서 대기 해제용)."""
    failed = []
    broker = None
    if live:
        import kiwoom_broker as broker
        from kiwoom import KiwoomError
    if actions:
        print(f"[3/3] 주문 {'제출' if live else '계획'} 시작 ({len(actions)}건)", flush=True)
    for a in actions:
        label = (f"[{a['type']}] {a['sym']} " +
                 (f"주문번호 {a['ord_no']}" if a["type"] == "cancel" else f"{a['qty']}주 @ ${a['price']:,.2f}") +
                 f" — {a['why']}")
        if not live:
            print(f"  · {label}", flush=True)
            log_event({**a, "status": "dry_run"})
            continue
        print(f"  ... {a['sym']} 처리 중 ({a['type']})", flush=True)
        try:
            stex = broker.resolve_exchange(a["sym"])
            time.sleep(broker.REQUEST_DELAY_SECONDS)
            if a["type"] == "cancel":
                resp = broker.cancel_order(a["ord_no"], stex, a["sym"])
            elif a["type"] == "sell":
                resp = broker.place_limit_sell(a["sym"], stex, a["qty"], a["price"])
            else:
                allowed = broker.get_orderable_quantity(a["sym"], stex, a["price"])
                time.sleep(broker.REQUEST_DELAY_SECONDS)
                if allowed is not None and allowed < a["qty"]:
                    print(f"  ⚠️ {a['sym']} 미수불가 주문가능 {allowed}주 < 계획 {a['qty']}주 — 줄여서 주문")
                    a["qty"] = allowed
                if a["qty"] < 1:
                    print(f"  ✗ {label} — 주문가능수량 0, 건너뜀")
                    log_event({**a, "status": "skipped_zero_qty"})
                    failed.append(a["sym"])
                    continue
                resp = broker.place_limit_buy(a["sym"], stex, a["qty"], a["price"])
            print(f"  ✓ {label} (주문번호 {resp.get('ord_no')})", flush=True)
            log_event({**a, "status": "placed", "ord_no": resp.get("ord_no")})
        except (KiwoomError, ValueError) as exc:
            print(f"  ✗ {label} — {exc}", flush=True)
            log_event({**a, "status": "failed", "error": str(exc)})
            if a["type"] == "buy":
                failed.append(a["sym"])
        time.sleep(0.3)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser(description="RN존 → 키움 자동매매 실행기 (매수·매도)")
    parser.add_argument("--live", action="store_true", help="실제 주문을 제출한다 (기본은 dry-run)")
    parser.add_argument("--max-order-usd", type=float, default=None,
                        help="주문 1건당 최대 금액(USD). 환경변수 KIWOOM_MAX_ORDER_USD로도 설정 가능")
    parser.add_argument("--payload-json", help="(테스트용) rnzone_report.py --json 출력 파일을 대신 사용")
    parser.add_argument("--account-json", help="(테스트용) 키움 조회 대신 쓸 가상 계좌 JSON — dry-run 전용")
    args = parser.parse_args()

    live = bool(args.live) and os.getenv("KIWOOM_LIVE_TRADING", "").strip().upper() == "YES"
    if args.live and not live:
        print("⚠️ --live 는 켜졌지만 KIWOOM_LIVE_TRADING=YES 가 없어 DRY-RUN으로 실행합니다 (이중 안전장치).")
    if live and args.account_json:
        raise SystemExit("--account-json 은 dry-run 전용입니다.")

    max_order_usd = args.max_order_usd
    if max_order_usd is None and os.getenv("KIWOOM_MAX_ORDER_USD"):
        try:
            max_order_usd = float(os.environ["KIWOOM_MAX_ORDER_USD"])
        except ValueError:
            raise SystemExit("KIWOOM_MAX_ORDER_USD 값이 숫자가 아닙니다.")

    payload = (json.loads(Path(args.payload_json).read_text(encoding="utf-8"))
               if args.payload_json else fetch_signals())
    if args.account_json:
        account = json.loads(Path(args.account_json).read_text(encoding="utf-8"))
    else:
        try:
            account = read_account()
        except Exception as exc:  # noqa: BLE001 — 원인별 안내만 하고 종료
            msg = str(exc)
            print(f"\n⚠️ 키움 계좌 조회 실패: {msg}", flush=True)
            if "8001" in msg or "앱키" in msg:
                print("→ App Key/Secret을 확인하세요(운영 키인지, 오타 없는지). 프로그램의 '설정'에서 다시 저장할 수 있습니다.")
            elif "IP" in msg.upper():
                print("→ 이 PC의 공인 IP가 키움 포털(openapi.kiwoom.com)에 등록돼 있는지 확인하세요. "
                      "집 인터넷 IP가 바뀌면 다시 등록해야 합니다.")
            elif isinstance(exc, ModuleNotFoundError):
                print("→ 키움 패키지가 없습니다. install.bat 을 다시 실행하세요.")
            log_event({"type": "account_read", "status": "failed", "error": msg})
            raise SystemExit(2)
    state = load_state()
    result = build_actions(payload, account, state, max_order_usd)

    fx = payload["fx_krw_per_usd"]
    print(f"=== RN존 자동매매 ({payload['data_date']} 종가 기준, {'🔴 LIVE' if live else '🟡 DRY-RUN'}) ===")
    print(f"현금: ${account['cash_usd']:,.2f} + {account['cash_krw']:,.0f}원 · 비상금 "
          f"{payload['reserve_krw']:,.0f}원 제외 주문 가능 ${result['budget_start']:,.2f} (환율 {fx:,.1f})")
    positions = result["state"]["positions"]
    if positions:
        print("보유(장부):")
        for sym, r in positions.items():
            print(f"  - {sym}: {r['qty']}주, 평단 ${r['avg']:,.2f}, {'/'.join(map(str, r['tranches']))}차 체결, "
                  f"{r.get('held_days', 0)}/{r['hold_limit_days']}일")
    for n in result["notes"]:
        print(n)
    if not result["actions"]:
        print("오늘 낼 주문 없음.")
    else:
        print(f"주문 {len(result['actions'])}건:")
    failed = execute(result["actions"], live=live)
    print(f"주문 후 남은 예산(계획 기준): ${result['budget_left']:,.2f}")

    if live:
        new_state = result["state"]
        for sym in failed:
            new_state["pending"].pop(sym, None)
        save_state(new_state)
    else:
        print("(dry-run: 장부 positions.json 은 바꾸지 않았습니다)")


if __name__ == "__main__":
    main()
