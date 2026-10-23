# -*- coding: utf-8 -*-
"""RN존 자동매매 — 창 프로그램.

바탕화면 아이콘(설치.bat 이 만든다)으로 실행한다. 명령어 없이:
- 오늘 주문 계획 보기(dry-run, 주문 안 나감)
- 지금 실주문 실행(확인 창을 거친 뒤)
- 매일 자동실행 켜기/끄기(작업 스케줄러, 절전 중이면 깨워서 실행)
- 보유 현황(장부)·마지막 실행 결과 확인
- 설정(키움 App Key/Secret, 주문 1건 상한) 저장
- 업데이트: GitHub의 최신 코드를 받아 교체(.env·장부·로그·.venv는 그대로 둔다)
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import io
import tkinter as tk
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

APP_DIR = Path(__file__).resolve().parent
SCRIPTS = APP_DIR / "scripts"
ENV_PATH = APP_DIR / ".env"
LOG_PATH = APP_DIR / "daily_run.log"
ORDER_LOG_PATH = APP_DIR / "scripts" / "order_log.jsonl"
POSITIONS_PATH = SCRIPTS / "positions.json"
RUN_BAT = APP_DIR / "run_daily.bat"
TASK_NAME = "RNZone_Kiwoom_AutoTrade"
# 업데이트 출처(공개 저장소). 받은 버전은 version.json에 기록한다.
REPO = "GIHO-PARK/House"
BRANCH = "claude/install-lppak6"
VERSION_PATH = APP_DIR / "version.json"
UPDATE_DIRS = ("claude-skills/rnzone-kiwoom-autotrade/", "claude-skills/rnzone-report/")
# 사용자 데이터 — 업데이트로 절대 덮어쓰지 않는다
KEEP_NAMES = {".env", "positions.json", "order_log.jsonl", "daily_run.log", "exchange_cache.json",
              "discovered.json", "executed_signals.json", "version.json"}
RUN_TIME = "23:45"
DEFAULT_MAX_ORDER_USD = "7000"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ── 설정(.env) ───────────────────────────────────────────

def read_env() -> dict:
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env(values: dict) -> None:
    lines = [f"{k}={v}" for k, v in values.items() if v not in (None, "")]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def python_exe(windowless: bool = False) -> str:
    venv = APP_DIR / ".venv" / "Scripts" / ("pythonw.exe" if windowless else "python.exe")
    if venv.exists():
        return str(venv)
    return sys.executable


# ── 작업 스케줄러 ────────────────────────────────────────

def powershell(script: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def task_status() -> str | None:
    """등록돼 있으면 다음 실행 시각 문자열, 없으면 None."""
    if os.name != "nt":
        return None
    code, out = powershell(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        "if($t){$i=$t|Get-ScheduledTaskInfo; $n=if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-dd HH:mm')}else{'예약됨'};"
        "$l=if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000){' · 스케줄러 마지막 실행 '+$i.LastRunTime.ToString('MM/dd HH:mm')+' (결과 코드 '+$i.LastTaskResult+')'}else{' · 스케줄러 실행 이력 없음'};$n+$l}"
    )
    if code != 0 or not out:
        return None
    line = out.splitlines()[-1]
    for raw, meaning in TASK_RESULT_TEXT.items():
        line = line.replace(f"(결과 코드 {raw})", f"(결과 코드 {raw} — {meaning})")
    return line


# 작업 스케줄러 '마지막 실행 결과' 코드 → 사람이 읽는 설명
TASK_RESULT_TEXT = {
    "0": "정상 종료",
    "1": "오류 종료, 로그 확인",
    "2": "계좌 조회 실패, 로그 확인",
    "267009": "실행 중",
    "267011": "아직 실행 안 됨",
    "2147942667": "폴더 경로 오류",
    "2147942402": "실행 파일 없음",
}


def _ps_quote(text) -> str:
    """PowerShell 작은따옴표 문자열 리터럴."""
    return "'" + str(text).replace("'", "''") + "'"


def task_paths() -> tuple[str, str] | None:
    """등록된 작업이 실행하는 (명령+인수, 작업 폴더). 작업이 없으면 None."""
    if os.name != "nt":
        return None
    code, out = powershell(
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue;"
        "if($t){$a=$t.Actions[0]; 'EXE='+$a.Execute+' '+$a.Arguments; 'DIR='+$a.WorkingDirectory}"
    )
    if code != 0 or "DIR=" not in out:
        return None
    vals = dict(line.split("=", 1) for line in out.splitlines() if line[:4] in ("EXE=", "DIR="))
    return vals.get("EXE", "").strip(), vals.get("DIR", "").strip()


def task_points_here() -> bool:
    """작업이 지금 이 폴더의 run_daily.bat을 가리키는지. 폴더를 옮기거나 다시 설치하면 예전 경로가 남아
    작업이 '디렉터리 이름이 잘못됨'(결과 코드 2147942667 = 0x8007010B)으로 실패한다."""
    paths = task_paths()
    if not paths:
        return True
    exe, wd = paths
    try:
        same_dir = Path(wd).resolve() == APP_DIR
    except OSError:
        same_dir = False
    return same_dir and str(RUN_BAT).lower() in exe.lower() and RUN_BAT.exists()


def register_task() -> tuple[bool, str]:
    # cmd.exe로 감싸 실행: 경로에 공백·한글이 있어도 안전하고, 작업 폴더는 항상 지금 이 프로그램 폴더.
    arg = '/c "' + str(RUN_BAT) + '"'
    script = (
        f"$a=New-ScheduledTaskAction -Execute 'cmd.exe' "
        f"-Argument {_ps_quote(arg)} -WorkingDirectory {_ps_quote(APP_DIR)};"
        "$t=New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday "
        f"-At '{RUN_TIME}';"
        "$s=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    code, out = powershell(script)
    apply_power_settings()
    return code == 0, out


# 시스템 무인 절전 시간 제한(숨은 전원 옵션). 예약 작업이 PC를 깨워도 사람이 없으면 Windows는
# 기본 2분 뒤 다시 재운다 — 작업이 시작되기 전에 잠들어 그날 주문을 놓친 적이 있어 30분으로 늘린다.
UNATTEND_SLEEP = "7bc4a2f9-d8fc-4469-b07b-33eb785aaca0"
UNATTEND_SECONDS = 1800


def apply_power_settings() -> None:
    """절전 해제 타이머 허용 + 무인 깨어남 유지 시간. 실패해도 조용히 넘어간다."""
    if os.name != "nt":
        return
    powershell("powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;"
               "powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;"
               f"powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP {UNATTEND_SLEEP} {UNATTEND_SECONDS};"
               f"powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP {UNATTEND_SLEEP} {UNATTEND_SECONDS};"
               "powercfg /SETACTIVE SCHEME_CURRENT")


def unregister_task() -> tuple[bool, str]:
    code, out = powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    return code == 0, out


# ── 업데이트 ─────────────────────────────────────────────

def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "RNZoneTrader",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def local_version() -> dict:
    try:
        return json.loads(VERSION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_update() -> dict:
    """{latest: {sha, date, message}, changes: [메시지 첫 줄...], up_to_date: bool}"""
    head = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"))
    latest = {"sha": head["sha"], "date": head["commit"]["committer"]["date"][:16].replace("T", " "),
              "message": head["commit"]["message"].splitlines()[0]}
    mine = local_version().get("sha")
    if mine == latest["sha"]:
        return {"latest": latest, "changes": [], "up_to_date": True}
    changes = []
    try:
        if mine:
            cmp = json.loads(_get(f"https://api.github.com/repos/{REPO}/compare/{mine}...{latest['sha']}"))
            changes = [c["commit"]["message"].splitlines()[0] for c in cmp.get("commits", [])][::-1]
        else:
            recent = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits?sha={BRANCH}&per_page=5"))
            changes = [c["commit"]["message"].splitlines()[0] for c in recent]
    except Exception:
        changes = [latest["message"]]
    return {"latest": latest, "changes": changes, "up_to_date": False}


def apply_update(latest: dict) -> list:
    """해당 버전 ZIP을 받아 두 스킬 폴더의 코드만 교체한다. 바뀐 파일 목록을 돌려준다."""
    data = _get(f"https://codeload.github.com/{REPO}/zip/{latest['sha']}", timeout=120)
    root = APP_DIR.parent.parent  # .../House (claude-skills 의 상위)
    changed = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            if not rel.startswith(UPDATE_DIRS):
                continue
            parts = rel.split("/")
            if parts[-1] in KEEP_NAMES or ".venv" in parts or "__pycache__" in parts:
                continue
            target = root / rel
            new = z.read(info)
            if target.exists() and target.read_bytes() == new:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new)
            changed.append(rel.split("claude-skills/", 1)[-1])
    VERSION_PATH.write_text(json.dumps({**latest, "installed_at": f"{datetime.now():%Y-%m-%d %H:%M}"},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    return changed


# ── 화면 ─────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RN존 자동매매")
        self.geometry("880x640")
        self.minsize(720, 520)
        self.lines: queue.Queue = queue.Queue()
        self.running = False

        style = ttk.Style(self)
        style.configure("Big.TButton", padding=(10, 6))
        style.configure("Live.TButton", padding=(10, 6), foreground="#b42318")
        style.configure("Head.TLabel", font=("Malgun Gothic", 11, "bold"))

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        self.lbl_task = ttk.Label(top, text="", style="Head.TLabel")
        self.lbl_task.pack(anchor="w")
        self.lbl_last = ttk.Label(top, text="")
        self.lbl_last.pack(anchor="w", pady=(2, 0))

        bar = ttk.Frame(self, padding=(12, 6))
        bar.pack(fill="x")
        self.btn_dry = ttk.Button(bar, text="오늘 주문 계획 보기", style="Big.TButton",
                                  command=lambda: self.run_trader(live=False))
        self.btn_live = ttk.Button(bar, text="지금 실주문 실행", style="Live.TButton",
                                   command=self.confirm_live)
        self.btn_task = ttk.Button(bar, text="자동실행 켜기", style="Big.TButton", command=self.toggle_task)
        self.btn_cfg = ttk.Button(bar, text="설정", style="Big.TButton", command=self.open_settings)
        self.btn_log = ttk.Button(bar, text="로그 파일 열기", style="Big.TButton", command=self.open_log)
        for b in (self.btn_dry, self.btn_live, self.btn_task, self.btn_cfg, self.btn_log):
            b.pack(side="left", padx=(0, 6))

        bar2 = ttk.Frame(self, padding=(12, 0, 12, 4))
        bar2.pack(fill="x")
        self.btn_update = ttk.Button(bar2, text="업데이트 확인", command=self.update_check)
        self.btn_update.pack(side="left", padx=(0, 6))
        ttk.Button(bar2, text="실행 결과 복사", command=self.copy_output).pack(side="left", padx=(0, 6))
        self.lbl_ver = ttk.Label(bar2, text="")
        self.lbl_ver.pack(side="left", padx=(6, 0))

        pane = ttk.PanedWindow(self, orient="vertical")
        pane.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        holdf = ttk.LabelFrame(pane, text="보유 현황 (자동매매 장부)", padding=6)
        cols = ("sym", "qty", "avg", "tranche", "days", "entry")
        self.tree = ttk.Treeview(holdf, columns=cols, show="headings", height=5)
        for c, label, w in (("sym", "종목", 90), ("qty", "수량", 70), ("avg", "평단($)", 90),
                            ("tranche", "체결 차수", 90), ("days", "보유일 / 기간청산", 130),
                            ("entry", "진입일", 100)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True)
        pane.add(holdf, weight=1)

        outf = ttk.LabelFrame(pane, text="실행 결과", padding=6)
        self.out = scrolledtext.ScrolledText(outf, height=16, font=("Consolas", 10), wrap="word")
        self.out.pack(fill="both", expand=True)
        pane.add(outf, weight=3)

        self.refresh()
        self.after(150, self.drain)
        self.after(1200, lambda: self.update_check(silent=True))
        if not ENV_PATH.exists():
            self.after(300, self.first_run)

    # 상태 갱신
    def refresh(self) -> None:
        nxt = task_status()
        if nxt and not getattr(self, "_power_checked", False):
            # 이미 켜 둔 자동실행에도 새 전원 설정이 적용되도록 프로그램을 열 때 한 번 맞춘다.
            self._power_checked = True
            apply_power_settings()
        if nxt and not task_points_here():
            # 예전 설치 폴더를 가리키는 작업 → 이 폴더로 자동 재등록
            ok, out = register_task()
            self.write(("🔧 자동실행 작업이 예전 폴더를 가리키고 있어 이 폴더로 다시 등록했습니다.\n"
                      f"   실행 파일: {RUN_BAT}\n") if ok else f"⚠️ 자동실행 재등록 실패: {out}\n")
            nxt = task_status()
        if nxt:
            self.lbl_task.config(text=f"✅ 자동실행 켜짐 — 다음 실행 {nxt} (월~금 {RUN_TIME}, 절전 중이면 깨워서 실행)")
            self.btn_task.config(text="자동실행 끄기")
        else:
            self.lbl_task.config(text="⏸️ 자동실행 꺼짐 — '자동실행 켜기'를 누르면 월~금 밤 11:45에 자동으로 주문합니다")
            self.btn_task.config(text="자동실행 켜기")
        self.lbl_last.config(text=self.last_run_text())
        for row in self.tree.get_children():
            self.tree.delete(row)
        try:
            positions = json.loads(POSITIONS_PATH.read_text(encoding="utf-8")).get("positions", {})
        except Exception:
            positions = {}
        for sym, r in positions.items():
            self.tree.insert("", "end", values=(
                sym, r.get("qty"), f"{r.get('avg', 0):,.2f}",
                "/".join(f"{t}차" for t in r.get("tranches", [])),
                f"{r.get('held_days', 0)} / {r.get('hold_limit_days', '-')}일", r.get("entry_date", "")))
        if not positions:
            self.tree.insert("", "end", values=("(보유 없음)", "", "", "", "", ""))

    @staticmethod
    def last_run_text() -> str:
        """마지막 '실주문' 실행 결과(주문 접수/실패 건수)와, 와야 할 자동실행이 빠졌는지 알려준다."""
        from datetime import timedelta
        events = []
        if ORDER_LOG_PATH.exists():
            for line in ORDER_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-500:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("status") in ("placed", "failed", "skipped_zero_qty") or e.get("type") == "account_read":
                    try:
                        e["_at"] = datetime.fromisoformat(e["ts"]).astimezone().replace(tzinfo=None)
                    except (KeyError, ValueError):
                        continue
                    events.append(e)
        # 가장 최근에 '와야 했던' 자동실행 시각(월~금 23:45)
        now = datetime.now()
        hh, mm = (int(x) for x in RUN_TIME.split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if due > now:
            due -= timedelta(days=1)
        while due.weekday() >= 5:
            due -= timedelta(days=1)
        if not events:
            return "마지막 실주문: 기록 없음"
        last = max(e["_at"] for e in events)
        batch = [e for e in events if (last - e["_at"]).total_seconds() < 1800]
        placed = sum(e.get("status") == "placed" for e in batch)
        failed = sum(e.get("status") in ("failed", "skipped_zero_qty") or e.get("type") == "account_read" for e in batch)
        text = f"마지막 실주문: {last:%m/%d %H:%M} — 접수 {placed}건" + (f", ⚠️ 실패 {failed}건(로그 확인)" if failed else "")
        if last < due - timedelta(minutes=5):
            day = "월화수목금토일"[due.weekday()]
            text += f"   ⚠️ {due:%m/%d}({day}) 밤 자동실행 기록이 없습니다 — PC가 꺼져 있었거나 실행되지 않았습니다"
        return text

    # 실행
    def confirm_live(self) -> None:
        env = read_env()
        cap = env.get("KIWOOM_MAX_ORDER_USD", DEFAULT_MAX_ORDER_USD)
        if not messagebox.askyesno(
                "실주문 확인",
                "키움 실계좌로 지금 바로 주문을 냅니다.\n\n"
                f"· 주문 1건 상한: ${cap}\n"
                "· 미국 정규장 시간(한국시간 밤 10:30/11:30 이후)에 실행해야 주문이 들어갑니다.\n\n"
                "먼저 '오늘 주문 계획 보기'로 내용을 확인하셨나요? 진행할까요?"):
            return
        self.run_trader(live=True)

    def run_trader(self, live: bool) -> None:
        if self.running:
            messagebox.showinfo("실행 중", "이미 실행 중입니다. 끝날 때까지 기다려 주세요.")
            return
        self.running = True
        for b in (self.btn_dry, self.btn_live):
            b.state(["disabled"])
        self.out.delete("1.0", "end")
        title = "🔴 실주문 실행" if live else "🟡 주문 계획 보기 (주문은 나가지 않음)"
        self.write(f"{title} — {datetime.now():%Y-%m-%d %H:%M}\n신호 계산과 계좌 조회에 1~3분 걸립니다...\n\n")
        threading.Thread(target=self._worker, args=(live,), daemon=True).start()

    def _worker(self, live: bool) -> None:
        env = dict(os.environ)
        env.update(read_env())
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("KIWOOM_MODE", "real")
        env.setdefault("KIWOOM_MAX_ORDER_USD", DEFAULT_MAX_ORDER_USD)
        cmd = [python_exe(), "-u", str(SCRIPTS / "kiwoom_autotrade.py")]
        if live:
            env["KIWOOM_LIVE_TRADING"] = "YES"
            cmd.append("--live")
        log = None
        try:
            if live:
                log = LOG_PATH.open("a", encoding="utf-8")
                log.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} run started (manual) =====\n")
            proc = subprocess.Popen(cmd, cwd=str(APP_DIR), env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", creationflags=NO_WINDOW)
            for line in proc.stdout:
                self.lines.put(line)
                if log:
                    log.write(line)
            code = proc.wait()
            if log:
                log.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} run finished (exit={code}) =====\n")
            self.lines.put(f"\n{'✅ 완료' if code == 0 else f'⚠️ 오류로 종료 (코드 {code})'}\n")
        except Exception as exc:  # noqa: BLE001 — 화면에 그대로 보여준다
            self.lines.put(f"\n⚠️ 실행 실패: {exc}\n")
        finally:
            if log:
                log.close()
            self.lines.put(None)

    def drain(self) -> None:
        try:
            while True:
                item = self.lines.get_nowait()
                if item is None:
                    self.running = False
                    for b in (self.btn_dry, self.btn_live):
                        b.state(["!disabled"])
                    self.refresh()
                else:
                    self.write(item)
        except queue.Empty:
            pass
        self.after(150, self.drain)

    def write(self, text: str) -> None:
        self.out.insert("end", text)
        self.out.see("end")

    # 자동실행
    def toggle_task(self) -> None:
        if os.name != "nt":
            messagebox.showwarning("지원 안 함", "자동실행 등록은 Windows에서만 됩니다.")
            return
        if task_status():
            if messagebox.askyesno("자동실행 끄기", "매일 자동 주문을 멈출까요?"):
                ok, out = unregister_task()
                messagebox.showinfo("자동실행", "꺼졌습니다." if ok else f"끄지 못했습니다:\n{out}")
        else:
            if not ENV_PATH.exists():
                messagebox.showwarning("설정 필요", "먼저 '설정'에서 키움 정보를 저장해 주세요.")
                return
            if not messagebox.askyesno(
                    "자동실행 켜기",
                    f"월~금 밤 {RUN_TIME}에 이 PC가 자동으로 실주문을 냅니다.\n\n"
                    "· PC는 꺼지면 안 되고 절전·로그인 상태면 됩니다(절전 중이면 깨워서 실행).\n"
                    "· 첫 실주문을 직접 확인한 뒤에 켜는 것을 권장합니다.\n\n켤까요?"):
                return
            ok, out = register_task()
            messagebox.showinfo("자동실행", "켜졌습니다." if ok else f"등록하지 못했습니다:\n{out}")
        self.refresh()

    # 설정
    def first_run(self) -> None:
        messagebox.showinfo("처음 실행", "키움 REST API 운영(real) App Key와 Secret을 입력해 주세요.\n"
                                       "openapi.kiwoom.com 에서 발급받고, 이 PC의 공인 IP를 등록해 두어야 합니다.")
        self.open_settings()

    def open_settings(self) -> None:
        env = read_env()
        win = tk.Toplevel(self)
        win.title("설정")
        win.transient(self)
        win.grab_set()
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        fields = [("APP_KEY", "App Key (운영)", False),
                  ("APP_SECRET", "App Secret (운영)", True),
                  ("KIWOOM_MAX_ORDER_USD", "주문 1건 상한 ($)", False)]
        entries = {}
        for i, (key, label, secret) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=4)
            e = ttk.Entry(frm, width=48, show="•" if secret else "")
            e.insert(0, env.get(key, DEFAULT_MAX_ORDER_USD if key == "KIWOOM_MAX_ORDER_USD" else ""))
            e.grid(row=i, column=1, sticky="we", pady=4, padx=(8, 0))
            entries[key] = e
        krw = tk.BooleanVar(value=env.get("KIWOOM_KRW_ORDER", "").upper() == "YES")
        ttk.Checkbutton(frm, text="원화주문 사용 (달러로 미리 환전했다면 끄세요)",
                        variable=krw).grid(row=len(fields), column=1, sticky="w", pady=(6, 0))
        ttk.Label(frm, foreground="#555", wraplength=460, justify="left",
                  text="· 처음 몇 주는 상한을 500 정도로 낮게 두고 확인한 뒤 7000으로 올리세요.\n"
                       "· kiwoomcli setup 으로 이미 인증했다면 Key/Secret은 비워 두세요.\n"
                       "· 이 정보는 이 폴더의 .env 파일에만 저장됩니다.").grid(
            row=len(fields) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def save() -> None:
            cap = entries["KIWOOM_MAX_ORDER_USD"].get().strip()
            try:
                if float(cap) <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("확인", "주문 1건 상한은 0보다 큰 숫자로 입력해 주세요.", parent=win)
                return
            values = dict(env)
            values["KIWOOM_MODE"] = "real"
            for key, e in entries.items():
                values[key] = e.get().strip()
            values["KIWOOM_KRW_ORDER"] = "YES" if krw.get() else ""
            write_env(values)
            win.destroy()
            messagebox.showinfo("설정", "저장했습니다. '오늘 주문 계획 보기'로 연결을 확인해 보세요.")

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="취소", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="저장", command=save).pack(side="right", padx=(0, 6))

    # 업데이트
    def update_check(self, silent: bool = False) -> None:
        mine = local_version()
        self.lbl_ver.config(text=f"현재 버전: {mine.get('date', '확인 안 됨')} · 확인 중...")
        self.btn_update.state(["disabled"])

        def work() -> None:
            try:
                info = check_update()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)  # exc는 except 블록이 끝나면 사라지므로 미리 문자열로 잡아 둔다
                self.after(0, lambda: self._update_checked(None, silent, msg))
                return
            self.after(0, lambda: self._update_checked(info, silent, None))
        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, info: dict | None, silent: bool, error: str | None) -> None:
        self.btn_update.state(["!disabled"])
        mine = local_version()
        if error:
            self.lbl_ver.config(text=f"현재 버전: {mine.get('date', '확인 안 됨')} · 업데이트 확인 실패")
            if not silent:
                messagebox.showwarning("업데이트", f"최신 버전을 확인하지 못했습니다(인터넷 연결 확인):\n{error}")
            return
        if info["up_to_date"]:
            self.lbl_ver.config(text=f"✅ 최신 버전 ({mine.get('date')})")
            self.btn_update.config(text="업데이트 확인")
            if not silent:
                messagebox.showinfo("업데이트", "이미 최신 버전입니다.")
            return
        self.lbl_ver.config(text=f"🆕 새 버전 있음 ({info['latest']['date']} UTC) — 왼쪽 버튼을 눌러 업데이트")
        self.btn_update.config(text="🆕 업데이트 설치")
        if silent:
            return
        if self.running:
            messagebox.showinfo("업데이트", "실행이 끝난 뒤에 업데이트하세요.")
            return
        changes = "\n".join(f"· {c}" for c in info["changes"][:12]) or f"· {info['latest']['message']}"
        if not messagebox.askyesno("업데이트 설치",
                                   f"새 버전({info['latest']['date']} UTC)의 변경 내용:\n\n{changes}\n\n"
                                   "설정(.env)·보유 장부·로그는 그대로 둡니다. 설치할까요?"):
            return
        try:
            changed = apply_update(info["latest"])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("업데이트", f"설치하지 못했습니다:\n{exc}")
            return
        files = "\n".join(changed[:15]) + (f"\n외 {len(changed) - 15}개" if len(changed) > 15 else "")
        messagebox.showinfo("업데이트 완료", f"바뀐 파일 {len(changed)}개:\n{files or '(없음)'}\n\n프로그램을 다시 엽니다.")
        subprocess.Popen([python_exe(windowless=True), str(Path(__file__).resolve())], cwd=str(APP_DIR),
                         creationflags=NO_WINDOW)
        self.destroy()

    def copy_output(self) -> None:
        text = self.out.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("복사", "실행 결과를 복사했습니다. 대화창에 붙여넣기(Ctrl+V) 하세요.")

    def open_log(self) -> None:
        if not LOG_PATH.exists():
            messagebox.showinfo("로그", "아직 실행 기록이 없습니다.")
            return
        if os.name == "nt":
            os.startfile(LOG_PATH)  # noqa: S606 — 사용자 PC의 메모장으로 연다
        else:
            messagebox.showinfo("로그", str(LOG_PATH))


if __name__ == "__main__":
    App().mainloop()
