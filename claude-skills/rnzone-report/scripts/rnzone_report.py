#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RN존 듀얼전략 리포트
- 다리1 트레이딩: 레버리지 ETF(거래대금 100만$/일 이상 자동 편입) 자체 차트의 RN존, 기간청산 42일
  + 신규 상장 레버리지 ETF 매일 자동 발굴(야후 검색 API) → 발견 즉시 유니버스 후보에 편입·캐시 저장
- 다리1' 본주 트레이딩: 듀얼코어 6종목 본주 차트, 기간청산 63일
- 다리2 주배당 적립: 본주 신호 발생 시 WeeklyPay ETF 매수(영구 보유), 분배금은 섹터 모멘텀 1위에 재투자
- 모든 보유 포지션에 매도 시점(목표가/본절가/기간 D-day) 표시
표준 라이브러리만 사용. 실행: python rnzone_report.py (1~2분 소요)
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── 기법 파라미터 ──────────────────────────────────────────
TOUCH = 0.04       # 상단선 터치 인정(영상 공식 4%)
BUYZ = 0.02        # 매수존 범위
SELL = 0.20        # 목표 수익률(평단 대비)
HOLD_LEV = 42      # 기간청산 - 레버리지(거래일)
HOLD_STK = 63      # 기간청산 - 본주(거래일)
LIQ_MIN = 1_000_000  # 레버리지 후보 최소 유동성($/일)
TOP_N = 25           # 유니버스 = 거래대금 상위 25 (+보유 중 종목은 랭킹 밖이어도 유지)
NEAR = 0.03        # 터치 임박 판정
MAX_CONCURRENT = 7  # 동시 진입 상한(경고용)
MAX_CANDS = 60     # 레버리지 후보 상한(폭주 방지)
NEW_INVESTOR_MODE = True  # 실보유 없는 신규 투자자용: 과거 평단 대신 오늘 종가를 진입가로 재계산,
                          # 이미 청산됐어야 할 매도/본절/기간청산 안내는 표시하지 않음
CAPITAL_KRW = 50_000_000   # 총 투입 자본
TRADE_RATIO = 0.7          # 트레이딩(레버리지+본주) 배분 비율 — 나머지는 주배당 적립
FX_FALLBACK = 1400.0       # 환율 조회 실패 시 참고 환율($/원)

LV = [1, 2, 3, 5, 7.5, 10, 15, 20, 30, 50, 75, 100, 150, 200, 300,
      500, 750, 1000, 1500, 2000]

# 듀얼코어 6종목: (본주, 주배당ETF, 실측베타)
DUAL = [("AMD", "AMDW", 1.18), ("ARM", "ARMW", 1.20), ("PLTR", "PLTW", 1.16),
        ("GOOGL", "GOOW", 1.20), ("TSLA", "TSLW", 1.18), ("NVDA", "NVDW", 1.17)]

# 레버리지 ETF 기본 후보(여기에 매일 자동 발굴분이 합쳐짐)
LEVS = ["SSO", "QLD", "UPRO", "SPXL", "TQQQ", "SOXL", "TECL", "TNA", "FAS", "USD",
        "LABU", "TSLL", "TSLT", "NVDL", "NVDU", "AMDL", "CONL", "MSTX", "MSTU",
        "GGLL", "FBL", "AAPU", "MSFU", "AMZU", "PLTU", "SMCX", "AVGX", "NFXL",
        "BITX", "ETHU", "ROBN", "ARMG", "BABX"]

SECTORS = {"반도체": ["NVDW", "AMDW", "ARMW"],
           "소프트웨어": ["GOOW", "PLTW"],
           "모빌리티": ["TSLW"]}

# ── 신규 레버리지 ETF 자동 발굴 설정 ───────────────────────
DISC_QUERIES = ["2X Long", "Daily Bull", "2X Shares", "Daily Target 2X",
                "GraniteShares 2x", "Leverage Shares 2X", "Defiance Daily Target",
                "Direxion Daily Bull", "Tradr 2X"]
LEV_PAT = re.compile(r"\b(2X|3X|BULL|LEVERAGED|ULTRA)\b", re.I)
BAD_PAT = re.compile(r"\b(BEAR|SHORT|INVERSE|VIX)\b|-1X", re.I)
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discovered.json")


def above(p):
    for v in LV:
        if v > p:
            return v
    return None


def below(x):
    r = None
    for v in LV:
        if v < x:
            r = v
    return r


def fetch(sym, rng="2y"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
    last = None
    for a in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.load(r)["chart"]["result"][0]
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def discover_levs():
    """야후 검색으로 미국 상장 롱 레버리지 ETF 발굴(인버스 제외). {심볼: 이름}"""
    found = {}
    for q in DISC_QUERIES:
        try:
            url = ("https://query1.finance.yahoo.com/v1/finance/search?q="
                   + urllib.parse.quote(q) + "&quotesCount=25&newsCount=0")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
            for x in d.get("quotes", []):
                sym = (x.get("symbol") or "").upper()
                name = x.get("longname") or x.get("shortname") or ""
                if not sym or "." in sym or "-" in sym or len(sym) > 6:
                    continue  # 해외 상장(.TO 등)·비정상 심볼 제외
                if LEV_PAT.search(name) and not BAD_PAT.search(name):
                    found[sym] = name
        except Exception:
            pass
        time.sleep(0.3)
    return found


def load_merge_cache(found):
    """과거 발굴분과 합쳐 저장(한 번 발견한 종목은 계속 추적)"""
    cached = {}
    try:
        if os.path.exists(CACHE):
            with open(CACHE, encoding="utf-8") as f:
                cached = json.load(f)
    except Exception:
        cached = {}
    merged = dict(cached)
    merged.update(found)
    try:
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return merged


def bars_vol(res):
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    bars, vols = [], []
    for i in range(len(ts)):
        if q["high"][i] is not None and q["low"][i] is not None and q["close"][i] is not None:
            bars.append((ts[i], q["high"][i], q["low"][i], q["close"][i]))
            vols.append(q["close"][i] * (q["volume"][i] or 0))
    return bars, vols


def run_machine(bars, hold):
    """3단 상태머신. 최종 상태 + 마지막 봉 이벤트 + 매도 시점 정보 반환"""
    st, armed, b1, buy_i = 0, None, None, None
    f1 = f2 = f3 = None
    last = len(bars) - 1
    ev = {}
    for i, (t, h, l, c) in enumerate(bars):
        e = {}
        A = above(c)
        if A and h >= A * (1 - TOUCH) and (st == 0 or (st == 1 and A != armed)):
            armed, b1 = A, below(A)
            if b1 is not None:
                st = 1
                e["cond"] = {"armed": A, "b1": b1}
        if st == 1 and b1 and l <= b1 * (1 + BUYZ):
            st, f1, buy_i = 2, b1 * (1 + BUYZ), i
            e["b1"] = {"b1": b1}
        if st == 2 and b1 and l <= b1 * 0.80:
            st, f2 = 3, b1 * 0.80
            e["b2"] = True
        if st == 3 and b1:
            l3 = below(b1)
            if l3 and l <= l3 * (1 + BUYZ):
                st, f3 = 4, l3 * (1 + BUYZ)
                e["b3"] = True
        sh = (100 / f1 if f1 else 0) + (200 / f2 if f2 else 0) + (300 / f3 if f3 else 0)
        cost = (100 if f1 else 0) + (200 if f2 else 0) + (300 if f3 else 0)
        avg = cost / sh if sh > 0 else None
        if st >= 2 and avg and h >= avg * (1 + SELL):
            e["sell"] = True
            st, armed, b1, f1, f2, f3, buy_i = 0, None, None, None, None, None, None
        if st >= 3 and avg and h >= avg:
            e["be"] = True
            st, armed, b1, f1, f2, f3, buy_i = 0, None, None, None, None, None, None
        if st >= 2 and buy_i is not None and i - buy_i >= hold:
            e["timeout"] = True
            st, armed, b1, f1, f2, f3, buy_i = 0, None, None, None, None, None, None
        if i == last:
            ev = e
    sh = (100 / f1 if f1 else 0) + (200 / f2 if f2 else 0) + (300 / f3 if f3 else 0)
    cost = (100 if f1 else 0) + (200 if f2 else 0) + (300 if f3 else 0)
    avg = cost / sh if sh > 0 else None
    held = (last - buy_i) if buy_i is not None else None
    nxt = None
    if st == 2 and b1:
        nxt = ("2차선", b1 * 0.80)
    elif st == 3 and b1:
        l3 = below(b1)
        if l3:
            nxt = ("3차선", l3)
    return {"stage": st, "armed": armed, "b1": b1, "avg": avg, "held": held,
            "close": bars[-1][3], "last_ts": bars[-1][0], "events": ev,
            "next": nxt, "hold": hold}


def fp(v):
    return "?" if v is None else f"{v:,.2f}"


def fl(v):
    return "?" if v is None else f"{v:g}"


def fkrw(v):
    return f"{v/10000:,.0f}만원"


def fetch_fx():
    """USD/KRW 환율(야후 KRW=X). 실패 시 FX_FALLBACK 사용."""
    try:
        res = fetch("KRW=X", "5d")
        bars, _ = bars_vol(res)
        if bars:
            return bars[-1][3], True
    except Exception:
        pass
    return FX_FALLBACK, False


def main():
    json_mode = "--json" in sys.argv[1:]

    # 0) 환율 + 신규 레버리지 ETF 발굴 + 캐시 병합
    fx, fx_ok = fetch_fx()
    trade_budget_krw = CAPITAL_KRW * TRADE_RATIO
    wp_budget_krw = CAPITAL_KRW * (1 - TRADE_RATIO)
    slot_krw = trade_budget_krw / MAX_CONCURRENT
    slot_usd = slot_krw / fx
    wp_cap_krw = wp_budget_krw / len(DUAL)
    wp_cap_usd = wp_cap_krw / fx

    disc_today = discover_levs()
    disc_all = load_merge_cache(disc_today)
    new_syms = sorted(s for s in disc_all if s not in LEVS)
    candidates = (LEVS + new_syms)[:MAX_CANDS]

    lev_rows, lev_thin, lev_young, fails = [], [], [], []
    for sym in candidates:
        try:
            res = fetch(sym)
            bars, vols = bars_vol(res)
            adv = sum(vols[-21:]) / min(21, len(vols)) if len(vols) >= 10 else None
            if adv is None or adv < LIQ_MIN:
                lev_thin.append((sym, adv))
                continue
            if len(bars) < 30:
                lev_young.append(sym)  # 상장 30거래일 미만 — 신호 없이 관찰만
                continue
            st = run_machine(bars, HOLD_LEV)
            st["sym"] = sym
            st["adv"] = adv
            st["new"] = sym in new_syms
            lev_rows.append(st)
        except Exception as e:
            fails.append((sym, str(e)[:50]))
        time.sleep(0.2)

    # 유니버스 확정: 거래대금 상위 TOP_N + 보유 중(stage>=2)은 랭킹 밖이어도 유지
    lev_rows.sort(key=lambda x: -x["adv"])
    top = lev_rows[:TOP_N]
    kept = [r for r in lev_rows[TOP_N:] if r["stage"] >= 2]
    for r in kept:
        r["keep"] = True
    n_below = len([r for r in lev_rows[TOP_N:] if r["stage"] < 2])
    lev_rows = top + kept

    stk_rows = []
    wp_ret = {}
    for s, wp, beta in DUAL:
        try:
            res = fetch(s)
            bars, _ = bars_vol(res)
            st = run_machine(bars, HOLD_STK)
            st.update({"sym": s, "wp": wp, "beta": beta, "wp_px": None})
            try:
                wres = fetch(wp, "6mo")
                wb, _ = bars_vol(wres)
                if wb:
                    st["wp_px"] = wb[-1][3]
                    if len(wb) > 60:
                        wp_ret[wp] = wb[-1][3] / wb[-61][3] - 1
            except Exception:
                pass
            stk_rows.append(st)
        except Exception as e:
            fails.append((s, str(e)[:50]))
        time.sleep(0.2)

    if not lev_rows and not stk_rows:
        if json_mode:
            print(json.dumps({"ok": False, "error": "fetch_failed"}, ensure_ascii=False))
        else:
            print("⚠️ 전 종목 조회 실패 — 네트워크/야후 문제. 잠시 후 재시도하세요.")
        return

    all_ts = [r["last_ts"] for r in lev_rows + stk_rows]
    data_date = datetime.fromtimestamp(max(all_ts), tz=timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    stale = (now.timestamp() - max(all_ts)) > 1.6 * 86400
    today = now.strftime("%m/%d")

    sec_mom = []
    for sec, names in SECTORS.items():
        vals = [wp_ret[n] for n in names if n in wp_ret]
        if vals:
            sec_mom.append((sum(vals) / len(vals), sec, names))
    sec_mom.sort(reverse=True)

    def est_wp_value(r, target):
        if r["wp_px"] is None or target is None:
            return None
        drop = target / r["close"] - 1
        return r["wp_px"] * (1 + r["beta"] * drop)

    def est_wp(r, target):
        val = est_wp_value(r, target)
        if val is None:
            return "-"
        drop = target / r["close"] - 1
        tag = " *참고치" if drop < -0.25 else ""
        return f"≈{val:,.2f}${tag}"

    todo, holding, near, waiting, idle, entries = [], [], [], [], [], []
    n_open = 0

    def classify(r, is_stk):
        nonlocal n_open
        sym = r["sym"]
        tag = f"{sym}(본주)" if is_stk else (f"🆕{sym}" if r.get("new") else sym)
        hold = r["hold"]
        ev = r["events"]
        if ev.get("cond"):
            ec = ev["cond"]
            x = f"🔔 **{tag} 조건성립** (상단 {fl(ec['armed'])}$) → 매수선 **{fl(ec['b1'])}$** 대기"
            if is_stk:
                x += f" [주배당 {r['wp']} 적립 준비, {est_wp(r, ec['b1'])}]"
            todo.append(x)
        if ev.get("b1"):
            eb = ev["b1"]["b1"]
            tgt = eb * (1 + BUYZ) * (1 + SELL) if eb else None
            amt = slot_usd * (100 / 600)
            x = (f"✅ **{tag} 1차 매수** {fl(eb)}$ → 목표 ~{fp(tgt)}$(+20%) / {hold}일 한도 "
                 f"/ 진입금액 ${fp(amt)}(약 {fkrw(amt * fx)})")
            if is_stk:
                wamt = wp_cap_usd * (100 / 600)
                x += f" + **{r['wp']} 적립 1차** ({est_wp(r, eb)}, 약 {fkrw(wamt * fx)})"
            todo.append(x)
        if ev.get("b2"):
            amt = slot_usd * (200 / 600)
            x = f"✅✅ **{tag} 2차 매수** (1차가 -20%) / 추가 진입금액 ${fp(amt)}(약 {fkrw(amt * fx)})"
            if is_stk:
                wamt = wp_cap_usd * (200 / 600)
                x += f" + {r['wp']} 적립 2차 (약 {fkrw(wamt * fx)})"
            todo.append(x)
        if ev.get("b3"):
            amt = slot_usd * (300 / 600)
            x = f"✅✅✅ **{tag} 3차 매수** (아래 RN선) / 추가 진입금액 ${fp(amt)}(약 {fkrw(amt * fx)})"
            if is_stk:
                wamt = wp_cap_usd * (300 / 600)
                x += f" + {r['wp']} 적립 3차 (약 {fkrw(wamt * fx)})"
            todo.append(x)
        if ev.get("sell") and not NEW_INVESTOR_MODE:
            todo.append(f"💰 **{tag} 목표 도달(+20%)** → 전량 매도")
        if ev.get("be") and not NEW_INVESTOR_MODE:
            todo.append(f"⚖️ **{tag} 본절 도달** → 본절 매도")
        if ev.get("timeout") and not NEW_INVESTOR_MODE:
            todo.append(f"⏰ **{tag} 기간청산({hold}일)** → 청산")

        st = r["stage"]
        c = r["close"]
        if st >= 2:
            n_open += 1
            stage_label = '1차' if st == 2 else '2차' if st == 3 else '3차'
            nx = f"{r['next'][0]} {fp(r['next'][1])}$" if r["next"] else "-"
            if NEW_INVESTOR_MODE:
                # 실보유 없음 → 과거 평단 대신 오늘 종가를 진입가로, 기간청산도 오늘부터 재계산
                entry = c
                target = entry * (1 + SELL)
                weight = {2: 100 / 600, 3: 300 / 600}.get(st, 1.0)
                amt = slot_usd * weight
                wp_note = f" (참고: {r['wp']} {est_wp(r, entry)})" if is_stk else ""
                x = (f"| {tag} | {stage_label}까지 도달(오늘 일괄 진입) | {fp(entry)}$(오늘){wp_note} | "
                     f"**{fp(target)}$** | D-{hold} (0/{hold}, 오늘 진입 기준) | {nx} | "
                     f"{fp(amt)}$(약 {fkrw(amt * fx)}) |")
                holding.append(x)
                entry_action = {
                    "sym": sym,
                    "is_stk": is_stk,
                    "stage": st,
                    "entry_price_usd": round(entry, 4),
                    "target_price_usd": round(target, 4),
                    "amount_usd": round(amt, 2),
                    "hold_limit_days": hold,
                }
                if is_stk:
                    wp_amt = wp_cap_usd * weight
                    wp_px = est_wp_value(r, entry)
                    entry_action["weekly_pay"] = {
                        "sym": r["wp"],
                        "amount_usd": round(wp_amt, 2),
                        "est_price_usd": round(wp_px, 4) if wp_px is not None else None,
                    }
                entries.append(entry_action)
            else:
                avg = r["avg"]
                pnl = (c / avg - 1) * 100
                be = fp(avg) + "$" if st >= 3 else "-"
                dd = hold - r["held"]
                holding.append(f"| {tag} | {stage_label} | "
                               f"{fp(avg)}$ | {fp(c)}$ | {pnl:+.1f}% | **{fp(avg * 1.2)}$** | {be} | "
                               f"D-{dd} ({r['held']}/{hold}) | {nx} |")
        elif st == 1 and r["armed"] and c <= r["armed"]:
            gap = (r["b1"] * (1 + BUYZ) / c - 1) * 100
            amt = slot_usd * (100 / 600)
            x = (f"| {tag} | {fp(c)}$ | {fl(r['armed'])}$ | **{fl(r['b1'])}$** | {gap:+.1f}% | "
                 f"{fp(amt)}$(약 {fkrw(amt * fx)}) |")
            if is_stk:
                x += f" {est_wp(r, r['b1'])} ({r['wp']}) |"
            waiting.append((gap, x, is_stk))
        else:
            A = above(c)
            if A:
                gap = (A * (1 - TOUCH) / c - 1) * 100
                if 0 <= gap <= NEAR * 100:
                    near.append((gap, f"| {tag} | {fp(c)}$ | {fl(A)}$ | {fl(A * (1 - TOUCH))}$ | "
                                      f"{gap:+.1f}% | 터치 시 매수선 {fl(below(A))}$ |"))
                else:
                    idle.append(tag)

    for r in stk_rows:
        classify(r, True)
    for r in sorted(lev_rows, key=lambda x: -x["adv"]):
        classify(r, False)

    if json_mode:
        payload = {
            "ok": True,
            "generated_at": now.isoformat(),
            "data_date": data_date,
            "stale": stale,
            "new_investor_mode": NEW_INVESTOR_MODE,
            "fx_krw_per_usd": fx,
            "fx_ok": fx_ok,
            "capital_krw": CAPITAL_KRW,
            "trade_ratio": TRADE_RATIO,
            "max_concurrent": MAX_CONCURRENT,
            "n_open": n_open,
            "entries": entries,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return

    out = [f"# 📊 RN존 듀얼전략 리포트 ({today} / 미국 {data_date} 기준)"]
    if stale:
        out.append("> ⚠️ 휴장 또는 데이터 지연 — 직전 거래일 기준입니다.")
    out.append("")

    out.append(f"## 💰 자본 배분 (총 {fkrw(CAPITAL_KRW)} 기준)")
    out.append(f"- 환율: 1$ ≈ {fx:,.0f}원 (야후 KRW=X{'' if fx_ok else ', 조회 실패 → 참고환율 사용'})")
    out.append(f"- 트레이딩(레버리지+본주, {TRADE_RATIO*100:.0f}%): {fkrw(trade_budget_krw)} → "
               f"동시 진입 {MAX_CONCURRENT}슬롯, 슬롯당 {fkrw(slot_krw)}(${fp(slot_usd)}) — 1:2:3 비중 분할")
    out.append(f"- 주배당 적립({(1-TRADE_RATIO)*100:.0f}%): {fkrw(wp_budget_krw)} → "
               f"종목당 상한 {fkrw(wp_cap_krw)}(${fp(wp_cap_usd)}) (듀얼코어 {len(DUAL)}종목 균등 배분)")
    out.append("")

    out.append("## 📌 오늘 할 일")
    out.extend(["- " + t for t in todo] if todo else ["- 새 신호 없음 — 기존 주문·보유 유지"])
    out.append("")

    if NEW_INVESTOR_MODE:
        out.append(f"## 🆕 신규 진입 대상 (오늘 진입 시) — 동시 진입 {n_open}/{MAX_CONCURRENT}"
                   + (" ⚠️ 상한 초과!" if n_open > MAX_CONCURRENT else ""))
        out.append("> 실보유 없는 신규 투자자 기준 — 과거 평단 대신 **오늘 종가를 진입가**로 계산했습니다. "
                   "이미 몇 차수까지 조건이 성립된 종목은 해당 차수 비중을 오늘 한 번에 진입하는 것으로 간주합니다.")
        if holding:
            out.append("| 종목 | 도달 차수 | 진입가 | 목표가(+20%) | 기간청산 한도 | 다음 매수선 | 진입금액 |")
            out.append("|---|---|---|---|---|---|---|")
            out.extend(holding)
        else:
            out.append("- 지금 진입 조건을 만족하는 종목 없음 — 아래 '매수 대기' 목록 참고")
    else:
        out.append(f"## 💼 보유 중 (트레이딩) — 동시 진입 {n_open}/{MAX_CONCURRENT}"
                   + (" ⚠️ 상한 초과!" if n_open > MAX_CONCURRENT else ""))
        if holding:
            out.append("| 종목 | 단계 | 평단 | 현재가 | 손익 | 목표가 | 본절가 | 기간청산 | 다음 매수선 |")
            out.append("|---|---|---|---|---|---|---|---|---|")
            out.extend(holding)
        else:
            out.append("- 보유 포지션 없음")
    out.append("")

    out.append("## 💰 주배당 적립 (분배금 재투자 지시)")
    if sec_mom:
        top = sec_mom[0]
        out.append(f"- **이번 주 재투자 대상: {top[1]} 섹터** ({' · '.join(top[2])}, 균등 매수) — "
                   f"60일 모멘텀 {top[0] * 100:+.1f}%")
        for m, sec, names in sec_mom[1:]:
            out.append(f"  - {sec}: {m * 100:+.1f}%")
    else:
        out.append("- 섹터 모멘텀 계산 불가(주배당 ETF 데이터 부족)")
    out.append("")

    if near:
        out.append(f"## 🔥 터치 임박 (터치존까지 {NEAR * 100:.0f}% 이내)")
        out.append("| 종목 | 현재가 | 상단선 | 터치존 | 남은 거리 | 비고 |")
        out.append("|---|---|---|---|---|---|")
        out.extend(l for _, l in sorted(near))
        out.append("")

    out.append("## ⚡ 매수 대기 (매수선까지 가까운 순)")
    if waiting:
        out.append("| 종목 | 현재가 | 상단선 | 매수선 | 매수존까지 | 1차 진입금액 | 주배당 예상가 |")
        out.append("|---|---|---|---|---|---|---|")
        out.extend(l for _, l, _ in sorted(waiting, key=lambda x: -x[0]))
    else:
        out.append("- 해당 없음")
    out.append("")

    if idle:
        out.append("😴 대기: " + ", ".join(idle))
        out.append("")

    n_top = len([r for r in lev_rows if not r.get("keep")])
    out.append(f"## 🧭 유니버스 점검 — 거래대금 상위 {n_top}종 + 보유 유지 {len(kept)}종 "
               f"(매일 재평가 + 신규 자동 발굴)")
    out.append("편입: " + ", ".join(
        ("🆕" if r.get("new") else "") + f"{r['sym']}({r['adv']/1e6:.0f}M)"
        for r in lev_rows if not r.get("keep")))
    if kept:
        out.append("📌 보유 유지(랭킹 밖, 사이클 종료 시까지): " + ", ".join(
            f"{r['sym']}({r['adv']/1e6:.0f}M)" for r in kept))
    if n_below:
        out.append(f"랭킹 밖 감시 후보: {n_below}종 (신호 미표시, 상위 진입 시 자동 편입)")
    if new_syms:
        newly_in = [r["sym"] for r in lev_rows if r.get("new")]
        newly_out = [s for s in new_syms if s not in newly_in]
        line = f"자동 발굴 누적 {len(new_syms)}종"
        if newly_in:
            line += f" — 이번에 편입: {', '.join(newly_in)}"
        if newly_out:
            line += f" (기준 미달 대기: {', '.join(newly_out[:15])})"
        out.append(line)
    if lev_young:
        out.append("👶 신규 관찰(상장 30거래일 미만, 신호 제외): " + ", ".join(lev_young))
    thin_base = [s for s, _ in lev_thin if s in LEVS]
    if thin_base:
        out.append("기준 미달: " + ", ".join(thin_base))
    if fails:
        out.append("⚠️ 조회 실패: " + ", ".join(s for s, _ in fails))
    out.append("")

    out.append("---")
    out.append(f"*규칙: 3단 매수(1:2:3) · 트레이딩 매도 = 목표 +20% / 2차 후 본절 / 기간청산(레버 {HOLD_LEV}일·본주 {HOLD_STK}일) · "
               f"주배당은 영구 보유·분배금 섹터모멘텀 재투자 · 동시 진입 {MAX_CONCURRENT}종 상한 · "
               f"종목당 주배당 상한 {fkrw(wp_cap_krw)}(자본 {fkrw(CAPITAL_KRW)} 기준, {TRADE_RATIO*100:.0f}:{(1-TRADE_RATIO)*100:.0f} 배분) · "
               f"가격 손절 없음(출구는 기간·자격·회로차단기) · 분기 자격점검*")
    out.append(f"*데이터: 야후 파이낸스 일봉 ({data_date}) · 이 리포트는 자동 계산 결과이며 투자 판단의 참고용입니다.*")

    print("\n".join(out))


if __name__ == "__main__":
    main()
