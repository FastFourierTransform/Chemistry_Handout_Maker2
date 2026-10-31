# -*- coding: utf-8 -*-
"""工作线程：串行批量转换 + 实时日志/逐页进度 + 暂停/终止。

与 batch.py 的主循环逐行对齐（同一条 .part / skip-existing / 体检 / 报表语义），
差别只在两点：

1. 底层脚本的一切 stdout 被 ``_LogCapture`` 接住 → 按行拆解 → 塞进线程安全队列；
   **主线程的 QTimer 定期排空队列**并把行事件变成 Qt 信号喂给日志面板/进度条。
   信号绝不在 print 的调用栈里发射 —— 否则任何线程往 stdout 打一行字都会
   在信号槽之间无限递归（冒烟测试实测：STATUS_STACK_OVERFLOW 硬崩）。
2. 循环由 GUI 的「暂停 / 终止」控制：

   * 暂停：协作式，**当前文件跑完后**停在队列里（智谱限流是并发维度，
     串行链路中途强停反而干净）；暂停期间点「继续」即刻恢复；
   * 终止：置 cancel 标志后，底层下一次 print 触发 ``_LogCapture.write()``
     在转换线程的调用栈里抛 ``RequestCancelled`` —— 底层脚本自己的
     finally（清理临时图片）都会走完，跟 Ctrl+C 同一条退出路径，
     半成品 .part 原地保留供排查（与 batch.py 的契约一致）。
     生效延迟 = 当前这一次 API 请求的剩余时间（每页开始时必有 print，
     实测通常几秒到几十秒）。

线程纪律（都是实测踩过的坑）：

* 每个 Qt 信号只允许**一个线程**发射 —— 同一 QObject 的信号被两个线程并发
  emit 会直接崩 Qt。所以：log_line / page_progress 只由主线程（排空队列时）
  发射；file_updated / state_changed / batch_finished 只由工作线程发射；
  pause/resume/cancel 只置标志，不发任何信号。
"""

from __future__ import annotations

import contextlib
import io
import os
import queue
import re
import threading
import time

from PySide6.QtCore import QThread, Signal

import gui.core.bridge as bridge
from gui.converters import ppt_to_pdf
from gui.core.models import ApiProfile, ConvertOptions, FileTask, TaskStatus


class RequestCancelled(Exception):
    """协作式终止信号：由日志捕获器在底层下一次 print 时抛出。"""


_PAGE_RE = re.compile(bridge.PAGE_PROGRESS_RE)


def _fmt_secs(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _level_of(line: str) -> str:
    if any(k in line for k in ("[错误]", "Traceback", "失败", "异常",
                               "Error:", "error:")):
        return "error"
    if "[警告]" in line or "!!" in line:      # 核心脚本用 !! 标记重试/截断/降级
        return "warn"
    if any(k in line for k in ("[成功]", "全部通过", "解析完成", "[通过]")):
        return "ok"
    return "info"


class _LogCapture(io.TextIOBase):
    """把底层脚本的 stdout 变成**队列事件**（绝不发射 Qt 信号，见模块 docstring）。

    事件元组：
        ("line", 级别, 文本)
        ("page", uid, 当前页, 该文件总页)   ← uid 在入队时就带好，排空可能滞后

    * 按行拆解（write 可能一次只给半行）；
    * 逐页进度正则解析（“正在处理第 3 页（共 15 页）…”）；
    * 抹掉 API Key，日志可放心截图/转发；
    * 终止开关：cancel 且当前线程是转换线程时，直接在底层 print 的
      调用栈里抛 RequestCancelled；
    * write 加锁：sys.stdout 是全局的，主线程偶尔的 print 也会进这里，
      两个线程并发写同一个缓冲会撕坏半行。
    """

    def __init__(self, on_event, cancel_check, secrets=(), uid_provider=None,
                 on_page=None):
        self._buf = ""
        self._lock = threading.Lock()
        self._on_event = on_event          # 只做 queue.put，纯 Python，无 Qt
        self._on_page = on_page            # 解析到页进度时回调（工作线程上下文）
        self._cancel_check = cancel_check
        self._secrets = [s for s in secrets if s]
        self._uid = uid_provider or (lambda: -1)

    def write(self, s):
        if self._cancel_check and self._cancel_check():
            raise RequestCancelled("用户终止")
        if not s:
            return 0
        with self._lock:
            self._buf += s
            if "\n" in self._buf:
                lines = self._buf.split("\n")
                self._buf = lines.pop()
            else:
                lines = []
        for line in lines:
            self._emit(line.rstrip("\r"))
        return len(s)

    def flush(self):
        return None

    def isatty(self):
        return False

    @property
    def encoding(self):
        return "utf-8"

    def _emit(self, line):
        for secret in self._secrets:
            line = line.replace(secret, "***")
        m = _PAGE_RE.search(line)
        if m:
            last = int(m.group(2) or m.group(1))
            total = int(m.group(3)) if m.group(3) else 0
            if self._on_page is not None:
                self._on_page(last, total)
            self._on_event(("page", self._uid(), last, total))
        self._on_event(("line", _level_of(line), line))


class ConversionWorker(QThread):
    """串行转换批次。日志/进度走队列 + 主线程排空，其余信号工作线程直发。"""

    # 主线程（排空队列时）发射：
    log_line = Signal(str, str)          # (级别, 文本) → 日志面板
    page_progress = Signal(int, int, int)  # (uid, 当前页, 该文件总页)
    # 工作线程发射：
    file_updated = Signal(dict)          # {uid,status,message,output,seconds,pages,pdf}
    state_changed = Signal(str)          # idle / running / paused / finished
    batch_finished = Signal(dict)        # 汇总 {total,ok,skip,fail,cancel,elapsed,dry_run,report}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[FileTask] = []
        self._profile: ApiProfile | None = None
        self._options = ConvertOptions()
        self._dry_run = False
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._cur_uid = -1
        self._events: queue.Queue | None = None
        # PySide6 的 QThread 不注册为 Python threading.Thread（连 ident 属性都
        # 没有），所以「是否在转换线程里」只能用 OS 线程号判断，不能用
        # threading.current_thread() is self —— 后者恒为 False，取消检查会
        # 永远不触发（冒烟测试实测抓到的坑）。
        self._runner_ident: int | None = None

    # ------------------------------------------------------------- 控制面
    def configure(self, tasks, profile, options, dry_run=False):
        self._tasks = list(tasks)
        self._profile = profile
        self._options = options
        self._dry_run = bool(dry_run)
        # 标志在 configure 时复位：start() 之后立刻点「终止」也不会被 run() 清掉
        self._cancel.clear()
        self._pause.clear()

    def pause(self):
        """暂停：当前文件跑完后停在队列里。

        只置标志、不发信号 —— 这些方法在 GUI 线程被调用，而信号只能由
        工作线程单线程发射（同一 QObject 的信号被两个线程并发 emit 会
        直接崩 Qt）。「已暂停」的确认由工作线程真正停在暂停点时自己发。
        """
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def cancel(self):
        """终止：底层下一次 print 时生效（当前请求结束后）。只置标志。"""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _cancel_check(self) -> bool:
        # 只允许在转换线程自己的 print 调用栈里抛异常；
        # 其他线程（如 GUI 线程）误打 stdout 只被捕获、不会被炸掉。
        return (self._cancel.is_set()
                and self._runner_ident is not None
                and threading.get_ident() == self._runner_ident)

    def _on_page_event(self, page: int, total: int):
        """捕获器在工作线程解析到「正在处理第 N 页」时回调。

        子类/测试可覆写（必须在工作线程上下文中，做不了 Qt 操作）。
        """
        return None

    # ------------------------------------------------- 队列（工作线程写）
    def _log(self, level: str, text: str):
        if self._events is not None:
            self._events.put(("line", level, text))

    # ------------------------------------------- 队列排空（**主线程**调用）
    def drain_events(self, max_items: int = 2000):
        """把捕获队列里的事件转成 Qt 信号。只允许主线程调用。"""
        events = self._events
        if events is None:
            return
        for _ in range(max_items):
            try:
                ev = events.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "line":
                self.log_line.emit(ev[1], ev[2])
            elif ev[0] == "page":
                self.page_progress.emit(ev[1], ev[2], ev[3])

    # ------------------------------------------------------------- 主流程
    def run(self):
        tasks, profile, options = self._tasks, self._profile, self._options
        self._runner_ident = threading.get_ident()
        self._events = queue.Queue()

        try:
            G, api_key, base_url, models, proxy = bridge.resolve_profile(profile)
            args = bridge.build_args(profile, options)
            secrets = [profile.api_key.strip()] if profile.api_key.strip() else []

            capture = _LogCapture(
                on_event=self._events.put,
                cancel_check=self._cancel_check,
                secrets=secrets,
                uid_provider=lambda: self._cur_uid,
                on_page=self._on_page_event,
            )
            started = time.time()
            # redirect_stdout 会把 sys.stdout 全局换成 capture：GUI 线程本来就不
            # print（日志都走队列），底层脚本的 print 全数入网。
            with contextlib.redirect_stdout(capture):
                self._run_batch(tasks, profile, options, args,
                                G, api_key, base_url, models, proxy, started)
        except BaseException as exc:  # noqa: BLE001 —— 绝不让未预期异常静默吞掉批次
            self._log("error", f"[错误] 批量循环异常：{type(exc).__name__}: {exc}")
            self.batch_finished.emit({
                "error": f"{type(exc).__name__}: {exc}",
                "total": len(self._tasks), "ok": 0, "skip": 0, "fail": 0,
                "cancel": 0, "elapsed": 0.0, "total_pages": 0,
            })
            self.state_changed.emit("finished")

    def _run_batch(self, tasks, profile, options, args,
                   G, api_key, base_url, models, proxy, started):
        self.state_changed.emit("running")

        # 按**用户给的源文件**排输出路径：PPT 的 pdf 是 %TEMP% 里的 <md5>.pdf，
        # 用它排路径会让产物落到临时目录、还叫 md5.md。转换本身仍用缓存 PDF。
        jobs = bridge.plan_jobs([t.source for t in tasks], options.outdir or "")
        total_pages = 0
        for t, job in zip(tasks, jobs):
            pages = t.pages or bridge.count_pages(t.pdf or job.source) or 0
            t.pages = pages
            total_pages += pages
            self.file_updated.emit({
                "uid": t.uid, "status": getattr(t.status, "value", str(t.status)),
                "message": t.message, "output": t.output,
                "seconds": t.seconds, "pages": pages,
            })

        self._log(
            "info", f"共 {len(jobs)} 个文件待处理（合计 {total_pages or '?'} 页）｜"
                    f"引擎 {profile.engine}｜模型链 {', '.join(models)}")
        if options.outdir:
            os.makedirs(options.outdir, exist_ok=True)

        # ---- 预演：只列计划，不调 API、不写文件 ----------------------------
        if self._dry_run:
            for t, job in zip(tasks, jobs):
                head = f"  {job.source}" + (f"  ({t.pages} 页)" if t.pages else "")
                self._log("info", head)
                self._log("info", f"       -> {job.dest}")
            self._log("ok", "预演结束：没有调用 API、没有写任何文件。")
            self.batch_finished.emit(self._summarize(jobs, [], time.time() - started,
                                                     total_pages, dry_run=True))
            self.state_changed.emit("finished")
            return

        # ---- 启动前自检（可选，大 batch 前值得开）---------------------------
        if options.precheck:
            self._log("info", "启动前自检：先发一张探针图做 API 连通性检查 ...")
            ok, msg = bridge.precheck(profile)
            self._log("ok" if ok else "error", msg)
            if not ok:
                self._log(
                    "error", "[错误] 自检未通过，整批不启动"
                             "（确认 Key/网络后再跑，或关掉「启动前自检」）。")
                self.batch_finished.emit({
                    "error": "precheck_failed", "elapsed": 0.0,
                    **self._summarize(jobs, [], 0.0, total_pages),
                })
                self.state_changed.emit("finished")
                return

        # ---- 主循环：串行，一个一个来（与 batch.py main 同构）---------------
        results = []
        for idx, (task, job) in enumerate(zip(tasks, jobs), 1):
            if self._cancel.is_set():
                break

            # 暂停点：当前文件跑完后在这里等
            if self._pause.is_set():
                self._log("warn", "[暂停] 当前文件已跑完，已暂停，等待继续 ...")
                self.state_changed.emit("paused")
                while self._pause.is_set() and not self._cancel.is_set():
                    time.sleep(0.2)
                if self._cancel.is_set():
                    break
                self._log("info", "[继续] 恢复转换 ...")
                self.state_changed.emit("running")

            entry = {"source": job.source, "output": job.dest, "status": "ok",
                     "seconds": 0.0, "pdf_pages": task.pages or None,
                     "verify": None, "error": None}

            self._log(
                "info", f"[{idx}/{len(jobs)}] {job.source}"
                        + (f"（{task.pages} 页）" if task.pages else ""))

            if options.skip_existing and bridge.looks_complete(job.dest):
                entry["status"] = "skip"
                results.append(entry)
                self._log("warn", f"  已存在且完整，跳过：{job.dest}")
                self.file_updated.emit({
                    "uid": task.uid, "status": TaskStatus.SKIPPED.value,
                    "message": "已存在产物，跳过", "output": job.dest,
                    "seconds": 0.0, "pages": task.pages,
                })
                continue

            self.file_updated.emit({
                "uid": task.uid, "status": TaskStatus.RUNNING.value,
                "message": f"[{idx}/{len(jobs)}] 转换中 ...", "output": "",
                "seconds": 0.0, "pages": task.pages,
            })
            self._cur_uid = task.uid

            t0 = time.time()
            try:
                os.makedirs(os.path.dirname(os.path.abspath(job.dest)) or ".", exist_ok=True)
                # 先写 .part，整份跑完才改名：半成品永远不会冒充成品
                bridge.convert_one(task.pdf or job.source, job.part, args,
                                   api_key, models, base_url, G, proxy)
                os.replace(job.part, job.dest)
            except RequestCancelled:
                entry["status"] = "cancel"
                entry["seconds"] = time.time() - t0
                results.append(entry)
                note = f"（半成品保留在 {job.part}）" if os.path.exists(job.part) else ""
                self._log("warn", f"  [终止] 已取消：{job.source}{note}")
                self.file_updated.emit({
                    "uid": task.uid, "status": TaskStatus.CANCELLED.value,
                    "message": "已取消", "output": "",
                    "seconds": entry["seconds"], "pages": task.pages,
                })
                break
            except BaseException as exc:  # noqa: BLE001 —— 文件级隔离，绝不拖垮整批
                entry["status"] = "fail"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["seconds"] = time.time() - t0
                results.append(entry)
                self._log("error", f"  [失败] {entry['error']}")
                if os.path.exists(job.part):
                    self._log(
                        "warn", f"  半成品保留在：{job.part}"
                                f"（若已转完只是改名失败，手动改成 .md 即可；重跑会覆盖它）")
                self.file_updated.emit({
                    "uid": task.uid, "status": TaskStatus.FAILED.value,
                    "message": f"{type(exc).__name__}: {exc}", "output": "",
                    "seconds": entry["seconds"], "pages": task.pages,
                })
                if options.stop_on_error:
                    self._log("error", "已勾选「失败即停」，停止整批。")
                    break
                continue

            entry["seconds"] = time.time() - t0
            # 该文件的页进度直接补满（page 事件属于捕获队列，这里只发文件级信号）
            self._events.put(("page", self._cur_uid, task.pages or 0, task.pages or 0))

            message = f"完成（{_fmt_secs(entry['seconds'])}）"
            if options.verify:
                html_path = None
                if options.html_preview:
                    base = options.outdir or os.path.dirname(job.dest) or "."
                    preview_dir = os.path.join(base, "preview")
                    html_path = job.preview(preview_dir)
                    os.makedirs(preview_dir, exist_ok=True)
                try:
                    passed, bad, _log = bridge.verify_output(job.dest, html_path)
                    entry["verify"] = (passed, bad)
                    if bad:
                        self._log(
                            "warn", f"  体检：{len(bad)} 项不合格 -> {'；'.join(bad)}")
                        message += f"；体检 {passed}/{passed + len(bad)}"
                    else:
                        self._log(
                            "ok", f"  体检：{passed} 项全部通过"
                                  + (f"，预览 {html_path}" if html_path else ""))
                        message += f"；体检 {passed} 项通过"
                except BaseException as exc:  # noqa: BLE001 —— 体检挂了不该否定已落盘的产物
                    entry["verify"] = (0, [f"体检脚本异常：{type(exc).__name__}: {exc}"])
                    self._log("warn", f"  体检异常（产物已落盘）：{exc}")

            self._log("ok", f"  [成功] {job.dest}（{_fmt_secs(entry['seconds'])}）")
            self.file_updated.emit({
                "uid": task.uid, "status": TaskStatus.SUCCESS.value,
                "message": message, "output": job.dest,
                "seconds": entry["seconds"], "pages": task.pages,
            })
            results.append(entry)

        elapsed = time.time() - started
        if args.report:
            try:
                bridge.emit_report(args, jobs, results, elapsed, total_pages)
            except BaseException as exc:  # noqa: BLE001
                self._log("warn", f"[警告] 报表写不出去：{exc}")

        # 复用 batch 的汇总打印（含失败清单 + 可直接重跑的命令）。
        # 不用 capture 当 out：cancel 标志仍置位时 write 会再抛异常；
        # 打进 StringIO 再按行灌日志队列，格式与 CLI 完全一致。
        try:
            buf = io.StringIO()
            bridge._batch._print_summary(jobs, results, elapsed, args, out=buf)
            for line in buf.getvalue().splitlines():
                self._log(_level_of(line), line)
        except BaseException:  # noqa: BLE001
            pass

        summary = self._summarize(jobs, results, elapsed, total_pages)
        self._log(
            "info",
            f"本轮结束：成功 {summary['ok']} / 跳过 {summary['skip']} / "
            f"失败 {summary['fail']} / 取消 {summary['cancel']}，用时 {_fmt_secs(elapsed)}")
        self.batch_finished.emit(summary)
        self.state_changed.emit("finished")

    @staticmethod
    def _summarize(jobs, results, elapsed, total_pages, dry_run=False):
        def count(status):
            return sum(1 for r in results if r["status"] == status)
        return {
            "total": len(jobs), "ok": count("ok"), "skip": count("skip"),
            "fail": count("fail"), "cancel": count("cancel"),
            "elapsed": round(elapsed, 1), "total_pages": total_pages,
            "dry_run": dry_run,
        }


# ---------------------------------------------------------------------------
# 辅助工作线程：PPT→PDF 预处理 / API 连通性探测
# ---------------------------------------------------------------------------
class PrepWorker(QThread):
    """把 .pptx/.ppt 转成 PDF 缓存（LibreOffice → PowerPoint COM 自动探测）。"""

    prepared = Signal(int, str, str)   # (uid, pdf路径, 错误信息——两者恰好一个为空)

    def __init__(self, tasks, parent=None):
        super().__init__(parent)
        self._tasks = list(tasks)

    def run(self):
        for task in self._tasks:
            try:
                pdf = ppt_to_pdf.to_pdf(task.source)
                self.prepared.emit(task.uid, pdf, "")
            except BaseException as exc:  # noqa: BLE001 —— 失败只影响这一个文件
                self.prepared.emit(task.uid, "", f"{type(exc).__name__}: {exc}")


class CheckWorker(QThread):
    """后台跑一次 API 连通性自检（真发一张探针小图，几秒出结果）。"""

    finished_ok = Signal(bool, str)

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self._profile = profile

    def run(self):
        ok, msg = bridge.precheck(self._profile)
        self.finished_ok.emit(ok, msg)
