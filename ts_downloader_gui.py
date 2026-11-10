"""
TS 영상 다운로더 - GUI 버전 (tkinter)
HLS(m3u8/TS), MPEG-DASH(mpd), 직접 MP4/WebM 다운로드를 창으로 사용할 수 있게 한 프로그램입니다.
DRM 보호 콘텐츠는 지원하지 않습니다.
"""
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

import hls_download as core
import media_dl

APP_TITLE = "TS 영상 다운로더"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("720x560")
        self.minsize(600, 480)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.variants = []
        self.gen = 0            # 초기화할 때마다 증가 -> 이전 작업의 메시지는 무시
        self._auto_start = False
        self._page_url = None
        self._auto_ref = None   # 프로그램이 자동으로 채운 Referer 값
        self._setting_url = False
        self._build()
        n = media_dl.cleanup_temp(min_age=3600)
        if n:
            self._log(f"이전 실행에서 남은 임시 폴더 {n}개를 정리했습니다.")
        self.after(100, self._poll)

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="페이지 또는 영상 주소\n(m3u8/mpd/mp4/ts)").grid(row=0, column=0, sticky="w", **pad)
        self.url = tk.StringVar()
        self.url.trace_add("write", self._on_url_edit)
        ttk.Entry(frm, textvariable=self.url).grid(row=0, column=1, sticky="ew", **pad)
        self.btn_find = ttk.Button(frm, text="페이지에서 찾기", command=self.find_in_page)
        self.btn_find.grid(row=0, column=2, sticky="ew", **pad)

        ttk.Label(frm, text="찾은 영상").grid(row=1, column=0, sticky="w", **pad)
        self.found = ttk.Combobox(frm, state="readonly", values=[])
        self.found.grid(row=1, column=1, columnspan=2, sticky="ew", **pad)
        self.found.bind("<<ComboboxSelected>>", self._pick_found)

        ttk.Label(frm, text="Referer (선택)").grid(row=3, column=0, sticky="w", **pad)
        self.referer = tk.StringVar()
        ttk.Entry(frm, textvariable=self.referer).grid(row=3, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="Cookie (선택)").grid(row=4, column=0, sticky="w", **pad)
        self.cookie = tk.StringVar()
        ttk.Entry(frm, textvariable=self.cookie).grid(row=4, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="화질").grid(row=5, column=0, sticky="w", **pad)
        self.quality = ttk.Combobox(frm, state="readonly", values=["최고 화질 (자동)"])
        self.quality.current(0)
        self.quality.grid(row=5, column=1, sticky="ew", **pad)
        self.btn_q = ttk.Button(frm, text="화질 불러오기", command=self.load_qualities)
        self.btn_q.grid(row=5, column=2, sticky="ew", **pad)

        ttk.Label(frm, text="저장 위치").grid(row=6, column=0, sticky="w", **pad)
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(default_dir):
            default_dir = os.path.expanduser("~")
        self._default_out = os.path.join(default_dir, "video.mp4")
        self.out = tk.StringVar(value=self._default_out)
        ttk.Entry(frm, textvariable=self.out).grid(row=6, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="찾아보기...", command=self.browse).grid(row=6, column=2, sticky="ew", **pad)

        opt = ttk.Frame(frm)
        opt.grid(row=7, column=0, columnspan=3, sticky="w", **pad)
        self.fmt = tk.StringVar(value="mp4")
        ttk.Radiobutton(opt, text="MP4로 저장 (ffmpeg 필요)", variable=self.fmt, value="mp4",
                        command=self._fix_ext).pack(side="left")
        ttk.Radiobutton(opt, text="TS 그대로 저장", variable=self.fmt, value="ts",
                        command=self._fix_ext).pack(side="left", padx=12)
        ttk.Label(opt, text="동시 다운로드").pack(side="left", padx=(20, 4))
        self.workers = tk.IntVar(value=8)
        ttk.Spinbox(opt, from_=1, to=32, width=4, textvariable=self.workers).pack(side="left")

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=3, sticky="ew", **pad)
        self.btn_start = ttk.Button(btns, text="다운로드 시작", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="취소", command=self.do_cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=8)
        self.btn_reset = ttk.Button(btns, text="초기화", command=self.reset)
        self.btn_reset.pack(side="left")
        self.btn_open = ttk.Button(btns, text="저장 폴더 열기", command=self.open_folder)
        self.btn_open.pack(side="right")

        self.pbar = ttk.Progressbar(frm, maximum=100)
        self.pbar.grid(row=9, column=0, columnspan=3, sticky="ew", **pad)
        self.status = tk.StringVar(value="대기 중")
        ttk.Label(frm, textvariable=self.status).grid(row=10, column=0, columnspan=3, sticky="w", **pad)

        logf = ttk.Frame(frm)
        logf.grid(row=11, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(11, weight=1)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._check_ffmpeg()

    def _check_ffmpeg(self):
        if not core.find_ffmpeg():
            self._log("※ ffmpeg를 찾지 못했습니다. MP4 저장을 하려면 ffmpeg.exe를 이 프로그램과 "
                      "같은 폴더에 두거나 PATH에 등록하세요. 지금은 TS 저장만 가능합니다.")
            self.fmt.set("ts")
            self._fix_ext()

    def _on_url_edit(self, *_):
        """사용자가 주소를 새로 입력하면 이전 영상의 찾은 목록/화질/자동 Referer를 비움"""
        if self._setting_url:
            return
        self.found.set("")
        self.found.configure(values=[])
        self.variants = []
        self.quality.configure(values=["최고 화질 (자동)"])
        self.quality.current(0)
        if self._auto_ref and self.referer.get() == self._auto_ref:
            self.referer.set("")
        self._auto_ref = None
        self._page_url = None

    def reset(self):
        """프로그램을 처음 실행한 상태로 되돌림"""
        running = self.worker is not None and self.worker.is_alive()
        if running:
            self.cancel.set()  # 이전 작업은 백그라운드에서 스스로 정리하고 종료
        self.gen += 1
        self.cancel = threading.Event()
        self.worker = None
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self._auto_start = False
        self._setting_url = True
        self.url.set("")
        self._setting_url = False
        self._on_url_edit()
        self.referer.set("")
        self.cookie.set("")
        part = self.out.get().strip() + ".part"
        for f in (part, part + ".url"):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass
        self.out.set(self._default_out)
        self.fmt.set("mp4")
        self.workers.set(8)
        self.pbar["value"] = 0
        self.status.set("대기 중")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._set_busy(False)
        n = media_dl.cleanup_temp(min_age=0 if not running else 60)
        self._log("초기화했습니다." + (" 진행 중이던 작업은 중지했습니다." if running else "")
                  + (f" 임시 폴더 {n}개 정리." if n else ""))
        self._check_ffmpeg()

    def _fix_ext(self):
        p = self.out.get()
        base, _ = os.path.splitext(p)
        self.out.set(base + "." + self.fmt.get())

    def browse(self):
        ext = self.fmt.get()
        p = filedialog.asksaveasfilename(
            defaultextension="." + ext,
            initialfile=os.path.basename(self.out.get()),
            initialdir=os.path.dirname(self.out.get()) or None,
            filetypes=[(ext.upper() + " 파일", "*." + ext), ("모든 파일", "*.*")])
        if p:
            self.out.set(p)

    def open_folder(self):
        d = os.path.dirname(self.out.get())
        if os.path.isdir(d) and hasattr(os, "startfile"):
            os.startfile(d)

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------ 작업 스레드 <-> UI
    def _poster(self):
        """현재 세대(gen)로 표시된 메시지 전송 함수"""
        g = self.gen
        return lambda kind, val: self.q.put((g, kind, val))

    def _poll(self):
        try:
            while True:
                g, kind, val = self.q.get_nowait()
                if g != self.gen:
                    continue  # 초기화 이전 작업의 메시지는 버림
                if kind == "log":
                    self._log(val)
                elif kind == "progress":
                    done, total, el = val
                    self.pbar["value"] = done * 100 / total
                    self.status.set(f"{done * 100 // total}%  ({done}/{total})  {el:.0f}초" if total != 1000
                                    else f"{done // 10}%  {el:.0f}초")
                elif kind == "status":
                    self.status.set(val)
                elif kind == "variants":
                    self._set_variants(val)
                elif kind == "found":
                    self._set_found(val)
                elif kind == "done":
                    self._finish(*val)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _set_busy(self, busy):
        st = "disabled" if busy else "normal"
        self.btn_start.configure(state=st)
        self.btn_q.configure(state=st)
        self.btn_find.configure(state=st)
        self.btn_cancel.configure(state="normal" if busy else "disabled")

    def _fetcher(self):
        headers = {"User-Agent": core.DEFAULT_UA}
        ref = self.referer.get().strip()
        if ref:
            headers["Referer"] = ref
            m = re.match(r"(https?://[^/]+)", ref)
            if m:
                headers["Origin"] = m.group(1)
        ck = self.cookie.get().strip()
        if ck:
            headers["Cookie"] = ck
        return core.Fetcher(headers)

    def _check_url(self):
        url = self.url.get().strip()
        if not re.match(r"https?://", url):
            messagebox.showwarning(APP_TITLE, "http:// 또는 https:// 로 시작하는 영상 주소를 입력하세요.")
            return None
        return url

    # ------------------------------------------------------------ 페이지에서 m3u8 찾기
    def find_in_page(self):
        url = self._check_url()
        if not url:
            return
        if core.is_media_url(url):
            self.status.set("이미 영상 주소입니다. 바로 다운로드하면 됩니다.")
            return
        self._set_busy(True)
        self.status.set("페이지에서 영상 주소 찾는 중...")
        self._page_url = url
        fetcher = self._fetcher()
        post = self._poster()

        def run():
            try:
                c = media_dl.find_media_in_page(url, fetcher, log=lambda m: post("log", m))
            except BaseException as e:  # noqa: BLE001
                post("log", f"찾기 오류: {e}")
                c = []
            post("found", c)

        threading.Thread(target=run, daemon=True).start()

    def _set_found(self, c):
        self._set_busy(False)
        self.found.configure(values=c)
        if not c:
            self._auto_start = False
            self.status.set("페이지에서 m3u8 주소를 찾지 못했습니다")
            self._log("m3u8 주소를 찾지 못했습니다. 주소가 스크립트로 숨겨져 있거나 로그인/DRM이 필요한 페이지일 수 있습니다. "
                      "브라우저 F12 > Network 탭에서 'm3u8'로 검색해 직접 입력해 주세요.")
            return
        for u in c:
            self._log("발견: " + u)
        self.found.current(0)
        self._pick_found()
        if getattr(self, "_auto_start", False):
            self._auto_start = False
            self.after(50, self.start)
        self.status.set(f"영상 주소 {len(c)}개 발견 · 첫 번째를 선택했습니다")

    def _pick_found(self, _evt=None):
        u = self.found.get()
        if u:
            if self._page_url and (not self.referer.get().strip() or self.referer.get() == self._auto_ref):
                self.referer.set(self._page_url)
                self._auto_ref = self._page_url
            self._setting_url = True
            self.url.set(u)
            self._setting_url = False
            self.variants = []
            self.quality.configure(values=["최고 화질 (자동)"])
            self.quality.current(0)

    # ------------------------------------------------------------ 화질 불러오기
    def load_qualities(self):
        url = self._check_url()
        if not url:
            return
        self._set_busy(True)
        self.status.set("화질 목록 불러오는 중...")
        fetcher = self._fetcher()
        post = self._poster()

        def run():
            try:
                u, _ = media_dl.resolve_page(url, fetcher, log=lambda m: post("log", m))
                v = media_dl.list_qualities(u, fetcher)
                post("variants", v)
            except BaseException as e:  # noqa: BLE001
                post("log", f"화질 목록 오류: {e}")
                post("variants", [])

        threading.Thread(target=run, daemon=True).start()

    def _set_variants(self, v):
        self.variants = v
        vals = ["최고 화질 (자동)"] + [f"{x['resolution']}  ·  {x['bandwidth'] // 1000} kbps" for x in v]
        self.quality.configure(values=vals)
        self.quality.current(0)
        self.status.set(f"화질 {len(v)}개 발견" if v else "화질 선택이 없는 영상입니다 (그대로 받으면 됩니다)")
        self._set_busy(False)

    # ------------------------------------------------------------ 다운로드
    def start(self):
        url = self._check_url()
        if not url:
            return
        # 페이지 주소/확장자 없는 주소는 다운로드 단계에서 내용을 보고 자동 판별합니다
        out = self.out.get().strip()
        if not out:
            messagebox.showwarning(APP_TITLE, "저장 위치를 지정하세요.")
            return
        mp4 = self.fmt.get() == "mp4"
        if mp4 and not core.find_ffmpeg():
            messagebox.showwarning(APP_TITLE, "ffmpeg가 없어 MP4로 저장할 수 없습니다. TS 저장을 선택하세요.")
            return
        if os.path.exists(out) and not messagebox.askyesno(APP_TITLE, "같은 이름의 파일이 있습니다. 덮어쓸까요?"):
            return

        qi = self.quality.current()
        args = SimpleNamespace(url=url, output=out, mp4=mp4, workers=max(1, int(self.workers.get() or 8)),
                               quality=(qi - 1) if qi > 0 else None, resolution=None, list_qualities=False)
        self.cancel.clear()
        self.pbar["value"] = 0
        self._set_busy(True)
        self.status.set("다운로드 준비 중...")
        self._log(f"시작: {url}")
        self.worker = threading.Thread(target=self._run, args=(args, self._fetcher(), self._poster(), self.cancel),
                                       daemon=True)
        self.worker.start()

    def _run(self, args, fetcher, post, cancel):
        try:
            out = media_dl.download_any(
                args, fetcher, log=lambda m: post("log", m),
                on_progress=lambda d, t, s: post("progress", (d, t, time.time() - s)),
                cancel=cancel)
            post("done", (True, out))
        except SystemExit as e:
            post("done", (False, str(e)))
        except BaseException as e:  # noqa: BLE001
            post("done", (False, str(e)))

    def do_cancel(self):
        self.cancel.set()
        self.status.set("취소하는 중...")

    def _finish(self, ok, msg):
        self._set_busy(False)
        if ok:
            self.status.set("완료")
            self._log(f"완료: {msg}")
            messagebox.showinfo(APP_TITLE, f"다운로드 완료\n{msg}")
        else:
            self.status.set("실패")
            self._log(f"오류: {msg}")
            messagebox.showerror(APP_TITLE, msg)


if __name__ == "__main__":
    App().mainloop()
