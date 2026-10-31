# -*- coding: utf-8 -*-
"""PPT / PPTX → PDF 转换器（GUI 的导入前处理）。

底层转换引擎只吃 PDF，所以拖进来的 .pptx / .ppt 会先在这里转成 PDF 再进队列。

探测顺序（哪个可用用哪个，都不动 PowerPoint 的可见窗口）：

1. **LibreOffice headless**（免费、跨平台、无界面）：
   ``soffice --headless --convert-to pdf``，先查 PATH 再查常见安装位置；
2. **PowerPoint COM**（Windows + pywin32）：``Presentations.Open → SaveAs(..., 32)``，
   32 即 ppSaveAsPDF，窗口全程隐藏。

结果缓存在 ``%TEMP%/HandoutMaker2/ppt_cache/<md5(绝对路径+修改时间)>.pdf``：
同一份 PPT 重复导入秒回，不重复转换。两个后端都没有 → ``ConvertError``，
GUI 把这个文件标成「失败」并给出三条自救路径。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

CACHE_DIR = os.path.join(tempfile.gettempdir(), "HandoutMaker2", "ppt_cache")

_SOFFICE_HINTS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


class ConvertError(RuntimeError):
    """PPT→PDF 失败（带用户可操作的解决提示）。"""


def _cache_path(src: str) -> str:
    key_src = os.path.abspath(src) + "|" + str(int(os.path.getmtime(src)))
    digest = hashlib.md5(key_src.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".pdf")


def _find_soffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    for hint in _SOFFICE_HINTS:
        if os.path.isfile(hint):
            return hint
    return None


def _via_soffice(src: str, dest: str) -> str:
    exe = _find_soffice()
    if not exe:
        raise ConvertError("未找到 LibreOffice（soffice）")
    cmd = [exe, "--headless", "--convert-to", "pdf", "--outdir", CACHE_DIR, src]
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=600, creationflags=creationflags,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConvertError("LibreOffice 转换超时（>10 分钟）") from exc
    if proc.returncode != 0:
        raise ConvertError(f"LibreOffice 退出码 {proc.returncode}")
    produced = os.path.join(CACHE_DIR, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if not os.path.isfile(produced):
        raise ConvertError("LibreOffice 没有产出 PDF（文件可能损坏或有密码保护）")
    os.replace(produced, dest)
    return dest


def _via_com(src: str, dest: str) -> str:
    try:
        import win32com.client  # noqa: PLC0415 —— 仅 Windows + pywin32 需要
    except ImportError as exc:
        raise ConvertError("未安装 pywin32（pip install pywin32）") from exc
    if sys.platform != "win32":
        raise ConvertError("PowerPoint COM 只在 Windows 上可用")

    app = None
    pres = None
    try:
        app = win32com.client.DispatchEx("PowerPoint.Application")
    except BaseException as exc:  # noqa: BLE001
        raise ConvertError(f"PowerPoint 启动失败：{exc}") from exc
    try:
        try:
            pres = app.Presentations.Open(src, ReadOnly=True, WithWindow=False)
        except TypeError:  # 老版本 PowerPoint 不接受 WithWindow 关键字
            pres = app.Presentations.Open(src, ReadOnly=True)
        pres.SaveAs(dest, 32)  # 32 = ppSaveAsPDF
    except BaseException as exc:  # noqa: BLE001
        raise ConvertError(f"PowerPoint 导出 PDF 失败：{exc}") from exc
    finally:
        try:
            if pres is not None:
                pres.Close()
        except BaseException:  # noqa: BLE001
            pass
        try:
            if app is not None:
                app.Quit()
        except BaseException:  # noqa: BLE001
            pass
    return dest


def to_pdf(src: str) -> str:
    """把 .pptx/.ppt 转成 PDF 并返回缓存路径；失败抛 ConvertError。"""
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"找不到文件：{src}")
    if not src.lower().endswith((".pptx", ".ppt")):
        raise ConvertError(f"不是 PPT 文件：{os.path.basename(src)}")

    dest = _cache_path(src)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return dest
    os.makedirs(CACHE_DIR, exist_ok=True)

    errors = []
    for backend, fn in (("LibreOffice", _via_soffice), ("PowerPoint COM", _via_com)):
        try:
            pdf = fn(src, dest)
            if pdf and os.path.isfile(pdf) and os.path.getsize(pdf) > 0:
                return pdf
        except ConvertError as exc:
            errors.append(f"{backend}: {exc}")
    raise ConvertError(
        "无法把 PPT 转成 PDF。可选方案：① 安装免费的 LibreOffice；"
        "② pip install pywin32 后由 PowerPoint 导出；"
        "③ 在 PowerPoint 里手动「另存为 PDF」再拖进来。"
        "（细节：" + "；".join(errors) + "）")
