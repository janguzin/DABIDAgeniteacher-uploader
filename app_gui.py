# app_gui.py — Geniteacher Uploader GUI (uploader.py는 수정 없이 그대로 사용)
import os, sys, threading, queue
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime

import uploader  # ← 같은 폴더의 uploader.py 그대로 사용

# ------- 로그 리다이렉트 -------
log_q = queue.Queue()

class TextRedirector:
    def __init__(self, text_widget):
        self.text_widget = text_widget
    def write(self, msg):
        if msg:
            log_q.put(msg)
    def flush(self):
        pass

def pump_logs(text_widget):
    try:
        while True:
            msg = log_q.get_nowait()
            text_widget.insert(tk.END, msg)
            text_widget.see(tk.END)
    except queue.Empty:
        pass
    text_widget.after(100, pump_logs, text_widget)

# ------- 업로더 실행(별도 스레드) -------
def run_uploader(folder_path, user, pw, run_btn):
    try:
        run_btn.config(state=tk.DISABLED)

        # (선택) GUI 입력값을 ENV로 주입 → uploader.try_login_if_needed가 그대로 활용
        if user:
            os.environ["GENI_ID"] = user
        if pw:
            os.environ["GENI_PW"] = pw

        # 경로 정리
        folder = Path(folder_path.strip().strip('"').strip("'").rstrip("\\/")).expanduser().resolve()
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 업로드 시작: {folder}\n")

        # 핵심: 기존 코드 그대로 호출
        uploader.run(folder)

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 모든 작업 완료!\n")
        messagebox.showinfo("완료", "업로드가 완료되었습니다.")
    except Exception as e:
        print(f"\n[에러] {e}\n")
        messagebox.showerror("오류", str(e))
    finally:
        run_btn.config(state=tk.NORMAL)

# ------- GUI -------
def main():
    root = tk.Tk()
    root.title("Geniteacher Uploader")

    frm = tk.Frame(root, padx=12, pady=12)
    frm.pack(fill="both", expand=True)

    # 폴더 경로
    tk.Label(frm, text="업로드할 폴더 경로").grid(row=0, column=0, sticky="w")
    ent_folder = tk.Entry(frm, width=60)
    ent_folder.grid(row=1, column=0, columnspan=2, sticky="we", pady=4)
    def choose_dir():
        d = filedialog.askdirectory()
        if d:
            ent_folder.delete(0, tk.END); ent_folder.insert(0, d)
    tk.Button(frm, text="폴더 선택", command=choose_dir).grid(row=1, column=2, padx=6)

    # 로그인(선택 입력) — 비워두면 기존 세션으로 진행
    tk.Label(frm, text="지니티처 아이디(선택)").grid(row=2, column=0, sticky="w", pady=(10, 0))
    ent_id = tk.Entry(frm, width=30); ent_id.grid(row=3, column=0, sticky="w")

    tk.Label(frm, text="지니티처 비밀번호(선택)").grid(row=2, column=1, sticky="w", pady=(10, 0))
    ent_pw = tk.Entry(frm, width=30, show="•"); ent_pw.grid(row=3, column=1, sticky="w")

    # 실행 버튼(백그라운드 스레드에서 run())
    run_btn = tk.Button(
        frm, text="실행", width=14,
        command=lambda: threading.Thread(
            target=run_uploader,
            args=(ent_folder.get(), ent_id.get(), ent_pw.get(), run_btn),
            daemon=True
        ).start()
    )
    run_btn.grid(row=3, column=2, padx=6)

    # 로그 창
    tk.Label(frm, text="로그").grid(row=4, column=0, sticky="w", pady=(12, 0))
    txt = tk.Text(frm, height=18, width=90)
    txt.grid(row=5, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(5, weight=1); frm.columnconfigure(0, weight=1); frm.columnconfigure(1, weight=1)

    # stdout/stderr → 로그창
    sys.stdout = TextRedirector(txt)
    sys.stderr = TextRedirector(txt)
    pump_logs(txt)

    root.minsize(760, 480)
    root.mainloop()

if __name__ == "__main__":
    main()
