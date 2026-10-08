# -*- coding: utf-8 -*-
"""RN존 자동매매 — 창 프로그램.

바탕화면 아이콘(설치.bat 이 만든다)으로 실행한다. 명령어 없이:
- 오늘 주문 계획 보기(dry-run, 주문 안 나감)
- 지금 실주문 실행(확인 창을 거친 뒤)
- 매일 자동실행 켜기/끄기(작업 스케줄러, 절전 중이면 깨워서 실행)
- 보유 현황(장부)·마지막 실행 결과 확인
- 설정(키움 App Key/Secret, 주문 1건 상한) 저장
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

APP_DIR = Path(__file__).resolve().parent
SCRIPTS = APP_DIR / "scripts"
ENV_PATH = APP_DIR / ".env"
LOG_PATH = APP_DIR / "daily_run.log"
POSITIONS_PATH = SCRIPTS / "positions.json"
RUN_BAT = APP_DIR / "run_daily.bat"
TASK_NAME = "RNZone_Kiwoom_AutoTrade"
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
        "if($t){$i=$t|Get-ScheduledTaskInfo; if($i.NextRunTime){$i.NextRunTime.ToString('yyyy-MM-dd HH:mm')}else{'예약됨'}}"
    )
    return out.splitlines()[-1] if code == 0 and out else None


def register_task() -> tuple[bool, str]:
    script = (
        f"$a=New-ScheduledTaskAction -Execute '{RUN_BAT}' -WorkingDirectory '{APP_DIR}';"
        "$t=New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday "
        f"-At '{RUN_TIME}';"
        "$s=New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30);"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null"
    )
    code, out = powershell(script)
    # 절전 중 깨우기 허용(전원 옵션 '절전 모드 해제 타이머 허용'). 실패해도 안내만 한다.
    powershell("powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;"
               "powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1;"
               "powercfg /SETACTIVE SCHEME_CURRENT")
    return code == 0, out


def unregister_task() -> tuple[bool, str]:
    code, out = powershell(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    return code == 0, out


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
        if not ENV_PATH.exists():
            self.after(300, self.first_run)

    # 상태 갱신
    def refresh(self) -> None:
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
        if not LOG_PATH.exists():
            return "마지막 실행: 기록 없음"
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines):
            if "run finished" in line:
                ok = "exit=0" in line
                stamp = line.replace("=====", "").replace("run finished", "").strip()
                return f"마지막 실행: {stamp} — {'정상 종료' if ok else '⚠️ 오류 — 로그 확인 필요'}"
        return "마지막 실행: 기록 없음"

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
