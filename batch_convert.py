# -*- coding: utf-8 -*-
"""批量转换入口：一条命令把一整个目录 / 一批 PDF 依次转成讲义 Markdown。

单文件入口是 `pdf_to_chemistry_handout_glm.py`（渲染 -> 调 GLM -> 归一化 -> 落盘），
本脚本只是它的**批量外壳**：转换逻辑一行都没有重写，直接调用
`convert_pdf_to_handout()` / `convert_selected_pages()`，所以单文件跑与批量跑的
产物完全一致（同一套提示词、同一套后处理、同一套落盘节奏）。

用法：

    python batch_convert.py 课件目录                  # 目录下所有 PDF
    python batch_convert.py 课件目录 -r               # 递归子目录
    python batch_convert.py a.pdf b.pdf c.pdf         # 指定若干文件
    python batch_convert.py "image/*.pdf"             # glob（PowerShell 下记得加引号）
    python batch_convert.py --list-file list.txt      # 从清单文件读路径（一行一个）
    python batch_convert.py 课件目录 --dry-run        # 只看会转哪些、输出到哪
    python batch_convert.py 课件目录 -o out --skip-existing --verify --report r.json
    python batch_convert.py 课件目录 --verify-only    # 只体检已有产物，不花额度

刻意为之的七个设计（都是"批量"与"单文件"的真正差别）：

1. **串行，永远串行**。智谱的限流是并发维度（同一时刻处理中的请求数），并行跑多个
   PDF 只会一起撞 1302 然后集体退避 —— 所以这里**没有** `--jobs`。批量靠"少动手"
   省时间，不靠并发。
2. **先写 `.part`，成功才改名**。单文件时"中途崩了也留下半份可用讲义"是优点，
   但在批量里会让 `--skip-existing` 把半成品当成已完成。所以批量一律先写
   `<输出>.md.part`，整份跑完才 `os.replace` 成正式名；失败时 .part 原地保留供排查，
   但绝不被当作成品。
3. **一个文件失败不拖垮整批**（默认；要"出错即停"用 `--stop-on-error`）。文件级的
   异常全部捕获，末尾给失败清单 + 一条可直接复制粘贴的重跑命令。
4. **体检内置**。`--verify` 直接复用 `verify_handout.py` 的检查函数（import 进同一
   进程，不是起子进程），逐份核对表格结构 / 化学记号 / 符号；`--html-preview DIR`
   顺便产出带 KaTeX 的预览页。
5. **输出名冲突自动消歧**。同名 PDF 散在不同子目录、又都输出到同一个 `-o` 时，
   用父目录名区分，**不会静默互相覆盖**。输入顺序按自然序排（`讲义2` 在 `讲义10` 前）。
6. **输入展开替你把坑踩平**：文件 / 目录 / glob / 清单文件都收，自动去重、自然排序，
   并且**字面路径优先于通配符展开** —— `[2]碳酸钠+碳酸氢钠.pdf` 这种名字里的 `[2]`
   会被 glob 当成字符类（匹配一个字符 "2"），先展开就会"查无此文件"而把整份课件
   静默漏掉（本仓库的示例课件正好叫这个名字，实测踩过）。
   **目录名里的 `[]` 同样中招**：`...\\[1]物质及其变化\\pdf` 这种路径，一旦拼上 `*.pdf`
   再交给 glob，`[1]` 就被当成"匹配字符 1"，于是 PDF 明明躺在里面却报"目录里没有 PDF"。
   所以目录一律用文件系统 API 直接列（见 `_iter_pdf_paths`），glob 也先把**磁盘上真实
   存在**的字面前缀剥掉再展开（见 `_glob_safe`）—— 路径是不是通配符，文件系统说了算。
7. **默认跳过 `*_handout_glm.pdf`**（本工具自己的产物）。产物 PDF 常常和源课件放在
   同一个目录 —— 本仓库就是 —— 扫描时把它们再转一遍只会得到
   `xxx_handout_glm_handout_glm.md`。要连产物一起转用 `--include-outputs`，
   要手工排除用 `--exclude PATTERN`。

退出码：0 全部成功（含跳过） / 1 有失败 / 2 用法或输入有问题。
"""

import argparse
import contextlib
import fnmatch
import glob as _glob
import io
import json
import os
import re
import shutil
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdf_to_chemistry_handout_glm as G  # noqa: E402

# 输出名与单文件入口保持一致：<输入名>_handout_glm.md（README 第 5 节的契约）
OUT_SUFFIX = "_handout_glm"
PART_SUFFIX = ".part"


# ---------------------------------------------------------------------------
# 1. 输入展开：文件 / 目录 / glob / 清单文件，统一成有序去重的 PDF 列表
# ---------------------------------------------------------------------------
def _natural_key(text):
    """自然序：让 讲义2.pdf 排在 讲义10.pdf 前面（纯字典序会反过来）。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(text))]


def _is_glob(pattern):
    return any(ch in pattern for ch in "*?[")


def _iter_pdf_paths(root, recursive=False):
    """列目录下的所有 .pdf —— 用文件系统 API，**绝不把目录名交给 glob**。

    为什么不能用 `glob.glob(os.path.join(root, "*.pdf"))`：glob 会把整条模式串都过一遍
    通配符语义，所以目录名里的方括号也被当成字符类。`...\\[1]物质及其变化\\pdf\\*.pdf`
    里的 `[1]` 会去匹配"一个字符 1"，而磁盘上那条目录叫 `[1]物质及其变化`，于是 glob
    一个结果都不返回 —— 用户看到的就是"目录内未找到 PDF"，哪怕 12 个 PDF 就躺在里面。
    （同理还有 `[1]物质的组成和性质分类.pdf` 这类**文件名**，由字面路径优先那一步兜住。）

    顺序与旧的 glob 展开一致：非递归列本层，递归用 os.walk 先排序再下降，
    两边都按自然序，保证 `讲义2` 在 `讲义10` 前。
    """
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort(key=_natural_key)
            for name in sorted(filenames, key=_natural_key):
                if name.lower().endswith(".pdf"):
                    yield os.path.join(dirpath, name)
        return
    try:
        with os.scandir(root) as it:
            entries = sorted(it, key=lambda e: _natural_key(e.name))
    except OSError:
        return
    for entry in entries:
        if not entry.name.lower().endswith(".pdf"):
            continue
        try:
            if entry.is_file():
                yield entry.path
        except OSError:
            continue


def _glob_safe(pattern, recursive=False):
    """`glob.glob`，但先剥掉**磁盘上真实存在**的字面前缀，只对剩下的部分做通配符展开。

    `C:\\课件\\[1]物质及其变化\\*.pdf` 里的 `[1]` 会被 glob 当字符类，整条模式都匹配不上。
    剥掉真实存在的前缀后，交给 glob 的只剩 `*.pdf`，目录名里的方括号就再也伤不到你。

    从前往后逐段判断：`isdir` 为真就当字面量收进前缀，第一个不存在的组件起停手，
    剩下的整段原样交给 glob（保留 `*` / `?` / `[]` / `**` 的通配符语义）。
    所以 `image/*.pdf` 这种正常通配符写法一个字节都不会变。
    """
    drive, rest = os.path.splitdrive(pattern)
    prefix = drive + os.sep if drive else ""
    parts = [p for p in re.split(r"[\\/]+", rest) if p]
    idx = 0
    for i, part in enumerate(parts):
        candidate = os.path.join(prefix, part) if prefix else part
        if os.path.isdir(candidate):
            prefix, idx = candidate, i + 1
        else:
            break
    tail = os.path.join(*parts[idx:]) if parts[idx:] else ""
    if not tail:
        return [prefix] if prefix and os.path.exists(prefix) else []
    # 关键：前缀作为 root_dir 交给 glob，**不拼进模式串** —— 拼回去等于又把 `[1]` 交给
    # 通配符语义，前功尽弃（第一版就是这么写错、被自检抓出来的）。
    try:
        hits = _glob.glob(tail, root_dir=prefix or None, recursive=recursive)
    except TypeError:  # Python < 3.10：没有 root_dir，只能退回旧行为
        return _glob.glob(os.path.join(prefix, tail), recursive=recursive)
    return [os.path.join(prefix, h) if prefix else h for h in hits]


def collect_inputs(inputs, recursive=False, list_file=None, quiet=False,
                   exclude=None, skip_outputs=True):
    """把命令行里的各种"输入写法"展开成 [路径, ...]。

    支持四种：目录（取 *.pdf，-r 则递归）、glob（`image/*.pdf`）、单个文件、
    清单文件（--list-file，一行一个路径，`#` 开头是注释）。重复输入按真实路径去重，
    保持首次出现的顺序 —— 同一个 PDF 在批量里被转两遍纯粹是浪费额度。

    两类**自动排除**（只对目录/glob 这种"批量发现"生效，你亲手点名的文件永远照转）：

    1. `*_handout_glm.pdf` —— 本工具自己的产物。本仓库就是源课件与产物 PDF 同目录放着，
       扫描时把它们再转一遍只会得到 `xxx_handout_glm_handout_glm.md` 这种垃圾。
       要连它们一起转就加 `--include-outputs`。
    2. `--exclude PATTERN`（可重复，glob，对文件名和相对路径都匹配）。
    """
    raw_items = list(inputs)
    if list_file:
        if not os.path.isfile(list_file):
            raise FileNotFoundError(f"找不到清单文件: {list_file}")
        with open(list_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    raw_items.append(line)

    patterns = [p for p in (exclude or []) if p]
    found, seen = [], set()
    skipped_outputs, excluded = [], []

    def _excluded_by(path):
        name = os.path.basename(path)
        rel = os.path.relpath(path).replace("\\", "/")
        for pat in patterns:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
                return pat
        return None

    def _consider(path, auto):
        if auto and skip_outputs and os.path.splitext(os.path.basename(path))[0].endswith(OUT_SUFFIX):
            skipped_outputs.append(path)
            return
        pat = _excluded_by(path)
        if pat:
            excluded.append((path, pat))
            return
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            found.append(path)

    for raw in raw_items:
        path = os.path.expanduser(raw)

        # 顺序很关键：**字面路径优先于通配符展开**。
        # `[2]碳酸钠+碳酸氢钠.pdf` 这种名字里的方括号会被 glob 当成字符类
        # （匹配一个字符 "2"），先当 glob 展开就会"查无此文件"而把这份课件静默漏掉
        # —— 本仓库的示例课件正好叫这个名字，实测踩过。
        if os.path.isdir(path):
            # 目录用 scandir/os.walk 直接列，不拼 glob 模式：目录名里的 `[]`（如
            # `[1]物质及其变化`）一旦进了 glob 模式串就会被当字符类，整批静默漏光。
            hits = sorted(_iter_pdf_paths(path, recursive=recursive), key=_natural_key)
            if not hits and not quiet:
                print(f"  [警告] 目录里没有 PDF：{raw}")
            for hit in hits:
                _consider(hit, auto=True)
        elif os.path.isfile(path):
            if not path.lower().endswith(".pdf") and not quiet:
                print(f"  [警告] 不是 .pdf，仍按 PDF 尝试：{raw}")
            _consider(path, auto=False)
        elif _is_glob(raw):
            hits = sorted(_glob_safe(path, recursive=recursive), key=_natural_key)
            pdfs = [h for h in hits if h.lower().endswith(".pdf")]
            # 只有方括号、没有 * / ? 的模式，"字符类"几乎不可能是本意 —— 更可能是路径
            # 本身带 `[1]` 这类字面量、人又手滑打错了字（`...\pdf后`）。这时不要再甩
            # "通配符没匹配到 PDF"，而是按"这条路径根本不存在"如实报错，省得去猜 glob。
            if not pdfs and not any(ch in raw for ch in "*?"):
                raise FileNotFoundError(
                    f"输入既不是文件也不是目录：{raw}"
                    f"（这条路径里只有方括号、没有 * 或 ?，所以没按通配符展开；请核对拼写。"
                    f"路径里的 [1] 这类方括号是允许的，整条加引号即可）")
            if not pdfs and not quiet:
                print(f"  [警告] 通配符没匹配到 PDF：{raw}")
            for hit in pdfs:
                _consider(hit, auto=True)
        else:
            raise FileNotFoundError(f"输入既不是文件也不是目录：{raw}（glob 写法在 PowerShell 下要加引号）")

    if not quiet:
        if skipped_outputs:
            print(f"  [跳过] {len(skipped_outputs)} 个看起来是本工具自己的产物"
                  f"（*{OUT_SUFFIX}.pdf）："
                  + "、".join(os.path.basename(p) for p in skipped_outputs[:5])
                  + ("…" if len(skipped_outputs) > 5 else "")
                  + "（要一起转就加 --include-outputs）")
        for path, pat in excluded:
            print(f"  [排除] {os.path.basename(path)}（命中 --exclude {pat}）")

    return found


# ---------------------------------------------------------------------------
# 2. 输出计划：命名、目录、同名冲突消歧
# ---------------------------------------------------------------------------
class Job:
    """一个待转任务：源 PDF -> 目标 md（.part 与预览页路径都由它派生）。"""

    __slots__ = ("source", "dest")

    def __init__(self, source, dest):
        self.source = source
        self.dest = dest

    @property
    def part(self):
        return self.dest + PART_SUFFIX

    def preview(self, preview_dir):
        return os.path.join(preview_dir, os.path.splitext(os.path.basename(self.dest))[0] + ".html")


def count_pages(pdf_path):
    """PDF 页数（只为进度/报表好看，读不到就返回 None，绝不让它拖垮任务）。"""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # 老版本只有 fitz 别名
        with pymupdf.open(pdf_path) as doc:
            return len(doc)
    except Exception:  # noqa: BLE001
        return None


def plan_jobs(pdfs, outdir=None, quiet=False):
    """给每个 PDF 排一个输出路径；同名冲突用父目录名区分，仍冲突则加序号。"""
    taken = set()
    jobs = []

    for src in pdfs:
        stem = os.path.splitext(os.path.basename(src))[0]
        dest_dir = outdir or os.path.dirname(os.path.abspath(src)) or "."
        name = stem + OUT_SUFFIX + ".md"
        dest = os.path.join(dest_dir, name)
        renamed_from = None

        key = os.path.normcase(os.path.abspath(dest))
        if key in taken:
            # 同名不同目录：加父目录名。这一步是确定性的，重复跑结果一样。
            parent = os.path.basename(os.path.dirname(os.path.abspath(src))) or "root"
            name = f"{parent}_{stem}{OUT_SUFFIX}.md"
            dest = os.path.join(dest_dir, name)
            renamed_from = stem + OUT_SUFFIX + ".md"
            key = os.path.normcase(os.path.abspath(dest))
            n = 2
            while key in taken:
                name = f"{parent}_{stem}_{n}{OUT_SUFFIX}.md"
                dest = os.path.join(dest_dir, name)
                key = os.path.normcase(os.path.abspath(dest))
                n += 1
            if not quiet:
                print(f"  [提示] 输出名冲突，{os.path.basename(src)} -> {name}"
                      f"（避开 {dest_dir} 里已有的 {renamed_from}）")

        taken.add(key)
        jobs.append(Job(src, dest))

    return jobs


def _looks_complete(md_path):
    """"看起来是成品"：至少有两行非空内容（只有一级标题的是跑空了，不算成品）。"""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    return len([ln for ln in text.splitlines() if ln.strip()]) >= 2


# ---------------------------------------------------------------------------
# 3. 转换：单文件两种模式（整份 / 抽页），与主脚本 main() 走同一条链路
# ---------------------------------------------------------------------------
def convert_one(src, dest, args, api_key, models, base_url):
    """把 src 转成 dest。抽页模式与 `pdf_to_chemistry_handout_glm.py --pages` 等价。"""
    selected = G._parse_page_spec(args.pages)

    if selected is None:
        return G.convert_pdf_to_handout(
            src, dest, api_key=api_key, base_url=base_url, models=models,
            dpi=args.dpi, pages_per_request=args.pages_per_request,
            normalize=not args.no_normalize, temperature=args.temperature,
            max_tokens=args.max_tokens, stream=not args.no_stream,
            thinking=args.thinking, verbose=not args.quiet,
        )

    # 抽页模式：只渲染指定页再走同一条链路（与主脚本 main() 的分支一字不差）
    images, workdir = G.render_pdf_pages(src, dpi=args.dpi)
    keep = [(n, p) for n, p in images if (n + 1) in selected]
    try:
        ns = types.SimpleNamespace(
            input_pdf=src, pages_per_request=args.pages_per_request,
            temperature=args.temperature, no_normalize=args.no_normalize,
            base_url=base_url, max_tokens=args.max_tokens,
            no_stream=args.no_stream, thinking=args.thinking,
        )
        return G.convert_selected_pages(keep, dest, api_key, models, ns)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. 交付物体检：复用 verify_handout 的检查函数（同进程 import，不起子进程）
# ---------------------------------------------------------------------------
def verify_md(md_path, html_path=None):
    """跑一遍 verify_handout 的全部检查，返回 (通过项数, 失败项名列表, 明细日志)。

    verify_handout 是个把结果往模块级 RESULTS 里堆的脚本，所以这里要
    ① 先清空 RESULTS ② 把它的打印收进内存 —— 批量时只报"44/44 通过"和失败项名，
    逐项细节收进报表，需要看细节就单跑一次 verify_handout.py。
    """
    import verify_handout as V

    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    V.RESULTS.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tables, prose = V._split_tables(text)
        V.check_table_structure(tables)
        V.check_dialect(prose)
        V.check_symbols(text)
        V.check_render(text, len(tables), tables, html_path)

    results = list(V.RESULTS)
    bad = [name for name, ok, _ in results if not ok]
    return len(results) - len(bad), bad, buf.getvalue()


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def _fmt_secs(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _print_summary(jobs, results, elapsed, args, out=None):
    """汇总表 + 失败清单 + 一条可直接重跑的命令。"""
    out = out or sys.stdout
    ok = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skip"]
    failed = [r for r in results if r["status"] == "fail"]
    pending = [j for j in jobs if j.dest not in {r["output"] for r in results}]

    print("\n" + "=" * 72, file=out)
    print(f"批量转换汇总：成功 {len(ok)} / 跳过 {len(skipped)} / 失败 {len(failed)}"
          f" / 未处理 {len(pending)}，用时 {_fmt_secs(elapsed)}", file=out)
    print("=" * 72, file=out)

    for r in results:
        if r["status"] == "ok":
            extra = ""
            if r.get("pdf_pages"):
                # --pages 时转的不是整份 PDF，别让"15 页"看起来像转了 15 页
                extra = (f"PDF 共 {r['pdf_pages']} 页" if args.pages
                         else f"{r['pdf_pages']} 页")
            if r.get("verify"):
                extra += (", " if extra else "") + f"体检 {r['verify'][0]}/{r['verify'][0] + len(r['verify'][1])}"
            print(f"  [成功] {os.path.basename(r['source'])} -> {r['output']}"
                  f"  ({_fmt_secs(r['seconds'])}{', ' + extra if extra else ''})", file=out)
        elif r["status"] == "skip":
            why = r.get("error") or f"已存在 {os.path.basename(r['output'])}"
            print(f"  [跳过] {os.path.basename(r['source'])}：{why}", file=out)
        else:
            print(f"  [失败] {os.path.basename(r['source'])}：{r['error']}", file=out)

    if failed:
        print("\n以下是失败项，修好后可直接重跑（--skip-existing 会跳过已成功的）：", file=out)
        cmd = ["python", "batch_convert.py"]
        cmd += ['"%s"' % r["source"] for r in failed]
        if args.outdir:
            cmd += ["-o", '"%s"' % args.outdir]
        if args.model:
            cmd += ["--model", args.model]
        if args.pages:
            cmd += ["--pages", args.pages]
        cmd += ["--skip-existing"]
        print("  " + " ".join(cmd), file=out)

    # 体检不合格是"产物能看但没达到交付契约"，与转换失败分开报：
    # 转换本身是成功的，所以它不进失败清单、也不改退出码（--verify-only 模式例外，
    # 那个模式的唯一目的就是体检，不合格即 status=fail）。
    verify_bad = [r for r in results if r.get("verify") and r["verify"][1]]
    if verify_bad:
        print("\n体检不合格的产物（文件已落盘，转换本身没问题，交付前建议逐项看）：", file=out)
        for r in verify_bad:
            passed, bad = r["verify"]
            print(f"  {os.path.basename(r['output'])}：{len(bad)}/{passed + len(bad)} 项不合格"
                  f" -> {'；'.join(bad)}", file=out)
        print("  逐项细节：python verify_handout.py \"<产物.md>\"", file=out)

    if pending:
        print(f"\n（未处理 {len(pending)} 个：本轮被中断或 --stop-on-error 停下，重跑即可继续）", file=out)
        for j in pending:
            print(f"    {j.source}", file=out)

    return 1 if (failed or pending) else 0


def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="batch_convert.py",
        description="批量转换入口：一批 PDF / 一整个目录 -> 结构化 Markdown 讲义（串行，复用单文件链路）",
        epilog="示例：python batch_convert.py 课件目录 -o out --skip-existing --verify",
    )
    p.add_argument("inputs", nargs="*",
                   help="PDF 文件 / 目录 / glob（如 \"image/*.pdf\"）；可给多个")
    p.add_argument("-o", "--outdir", default=None,
                   help="输出目录（默认与各自 PDF 同目录）；不存在会自动创建")
    p.add_argument("-r", "--recursive", action="store_true", help="目录输入时递归子目录")
    p.add_argument("--list-file", default=None,
                   help="从文本文件读输入路径（一行一个，# 开头是注释）")
    p.add_argument("--exclude", action="append", default=None, metavar="PATTERN",
                   help="排除命中该 glob 的 PDF（对文件名或相对路径匹配），可重复给多次")
    p.add_argument("--include-outputs", action="store_true",
                   help=f"连 *{OUT_SUFFIX}.pdf（本工具自己的产物）也一起转（默认跳过）")
    p.add_argument("--skip-existing", action="store_true",
                   help="跳过已存在且看起来完整的输出（重跑续传用；半成品 .part 不会被当成品）")
    p.add_argument("--dry-run", action="store_true", help="只列出会转哪些、输出到哪，不调用 API")
    p.add_argument("--stop-on-error", action="store_true", help="一有文件失败就停下（默认继续跑完）")
    p.add_argument("--precheck", action="store_true",
                   help="开跑前先做一次 API 连通性自检（大batch前建议开；失败就整批不启动）")
    p.add_argument("--verify", action="store_true",
                   help="每份产物跑一遍 verify_handout 的检查（表格结构/化学记号/符号）")
    p.add_argument("--verify-only", action="store_true",
                   help="不转换，只体检已有产物（不花额度）")
    p.add_argument("--html-preview", default=None, metavar="DIR",
                   help="顺便把 HTML 预览页写到该目录（隐含 --verify）")
    p.add_argument("--report", default=None, metavar="FILE",
                   help="把本次批量结果写成 JSON 报表（含每份的用时/页数/体检/错误）")

    # 以下与单文件入口同名同义，原样透传
    p.add_argument("--model", default=None,
                   help="模型名，可用逗号写降级链（默认 %s）" % ",".join(G.DEFAULT_MODELS))
    p.add_argument("--api-key", default=None, help="API Key（默认读 GLM_API_KEY / ZHIPUAI_API_KEY）")
    p.add_argument("--base-url", default=None, help=f"网关地址（默认 {G.DEFAULT_BASE_URL}）")
    p.add_argument("--dpi", type=int, default=G.RENDER_DPI, help=f"渲染 DPI（默认 {G.RENDER_DPI}）")
    p.add_argument("--pages-per-request", type=int, default=G.PAGES_PER_REQUEST,
                   help=f"每次请求塞几页（默认 {G.PAGES_PER_REQUEST}）")
    p.add_argument("--temperature", type=float, default=0.0, help="采样温度（默认 0）")
    p.add_argument("--max-tokens", type=int, default=G.MAX_TOKENS,
                   help=f"输出 token 上限（默认 {G.MAX_TOKENS}；glm-4v-flash 自动夹到 1024）")
    p.add_argument("--thinking", choices=("auto", "enabled", "disabled"), default="auto",
                   help="思维链开关（默认 auto）")
    p.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    p.add_argument("--pages", default=None,
                   help="只处理每个文件的指定页，如 1-3,7（对所有输入生效，试参数用）")
    p.add_argument("--no-normalize", action="store_true", help="跳过 handout_normalize 后处理")
    p.add_argument("--quiet", action="store_true",
                   help="不逐页回显主脚本的输出（只在失败时把该文件的日志打出来）")
    return p


def _verify_jobs(jobs, args):
    """--verify-only：只体检已有产物；结果用与转换同一套 results 结构记录。"""
    results = []
    for job in jobs:
        start = time.time()
        entry = {"source": job.source, "output": job.dest, "status": "ok",
                 "seconds": 0.0, "pdf_pages": None, "verify": None, "error": None}
        if not os.path.isfile(job.dest):
            entry.update(status="skip", error="产物不存在")
            print(f"  [跳过] 没有产物：{job.dest}")
            results.append(entry)
            continue
        html_path = job.preview(args.html_preview) if args.html_preview else None
        if html_path:
            os.makedirs(os.path.dirname(html_path), exist_ok=True)
        passed, bad, _log = verify_md(job.dest, html_path)
        entry["verify"] = (passed, bad)
        entry["seconds"] = time.time() - start
        if bad:
            entry.update(status="fail", error=f"体检 {len(bad)} 项不合格：" + "；".join(bad))
            print(f"  [不合格] {os.path.basename(job.dest)}：{len(bad)} 项 -> {'；'.join(bad)}")
        else:
            print(f"  [通过] {os.path.basename(job.dest)}：{passed} 项全部通过")
        results.append(entry)
    return results


def main(argv=None):
    args = build_arg_parser().parse_args(argv)

    if not args.inputs and not args.list_file:
        print("没有指定输入。用法示例：python batch_convert.py 课件目录\n"
              "（故意不给默认值：默认扫当前目录会让一次误触就烧掉一批额度）")
        return 2

    # ---- 展开输入 --------------------------------------------------------
    print("=" * 72)
    print("批量转换" + ("（--dry-run 预演）" if args.dry_run else ""))
    print("=" * 72)
    try:
        pdfs = collect_inputs(args.inputs, recursive=args.recursive,
                              list_file=args.list_file, quiet=args.quiet,
                              exclude=args.exclude,
                              skip_outputs=not args.include_outputs)
    except (FileNotFoundError, OSError) as exc:
        print(f"[错误] {exc}")
        return 2

    if not pdfs:
        print("[错误] 没有匹配到任何 PDF。")
        return 2

    jobs = plan_jobs(pdfs, outdir=args.outdir, quiet=args.quiet)
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    print(f"共 {len(jobs)} 个文件：")
    for i, job in enumerate(jobs, 1):
        pages = count_pages(job.source)
        head = f"  {i:>3}. {job.source}" + (f"  ({pages} 页)" if pages else "")
        note = "  [将跳过：产物已存在]" if (args.skip_existing
                                        and _looks_complete(job.dest)) else ""
        print(head)
        print(f"       -> {job.dest}{note}")

    if args.dry_run:
        print("\n预演结束：没有调用 API、没有写任何文件。")
        return 0

    # ---- API 自检（可选，但大 batch 前值得开）----------------------------
    api_key = G.resolve_api_key(args.api_key)
    base_url = G.resolve_base_url(args.base_url)
    models = G.parse_models(args.model)

    if args.precheck:
        print("\n--precheck：先做一次 API 连通性自检 ...")
        if G.check_api(api_key=api_key, base_url=base_url, models=models,
                       stream=not args.no_stream) != 0:
            print("[错误] 连通性自检没通过，整批不启动（确认 Key/网络后再跑，或去掉 --precheck）。")
            return 2

    # ---- 只体检：不转换、不花额度 ---------------------------------------
    if args.verify_only:
        print("\n--verify-only：只体检已有产物，不调用 API。")
        results = _verify_jobs(jobs, args)
        _emit_report(args, jobs, results, 0.0, total_pages=0)
        return _print_summary(jobs, results, 0.0, args)

    # ---- 主循环：串行，一个一个来 ---------------------------------------
    results = []
    started = time.time()
    done_seconds, done_pages = 0.0, 0
    total_pages = sum(filter(None, (count_pages(j.source) for j in jobs)), 0)

    def _finish():
        """收尾：报表 + 汇总。中断、--stop-on-error、正常跑完都走这里，
        所以"跑了一半"也有报表可看（已完成的产物不受影响）。"""
        elapsed = time.time() - started
        _emit_report(args, jobs, results, elapsed, total_pages)
        return _print_summary(jobs, results, elapsed, args)

    try:
        for idx, job in enumerate(jobs, 1):
            pages = count_pages(job.source)
            entry = {"source": job.source, "output": job.dest, "status": "ok",
                     "seconds": 0.0, "pdf_pages": pages, "verify": None, "error": None}

            print(f"\n[{idx}/{len(jobs)}] {job.source}"
                  + (f"（{pages} 页）" if pages else ""))

            if args.skip_existing and _looks_complete(job.dest):
                entry["status"] = "skip"
                print(f"  已存在且完整，跳过：{job.dest}")
                results.append(entry)
                continue

            log = io.StringIO()
            t0 = time.time()
            try:
                os.makedirs(os.path.dirname(os.path.abspath(job.dest)), exist_ok=True)
                # 先写 .part：整份跑完才改名，半成品永远不会冒充成品
                if args.quiet:
                    with contextlib.redirect_stdout(log):
                        convert_one(job.source, job.part, args, api_key, models, base_url)
                else:
                    convert_one(job.source, job.part, args, api_key, models, base_url)
                os.replace(job.part, job.dest)
            except KeyboardInterrupt:
                print("\n[中断] 收到 Ctrl+C，正在收尾 ...")
                if os.path.exists(job.part):
                    print(f"  本次未完成，半成品保留在：{job.part}")
                return _finish()
            except BaseException as exc:  # noqa: BLE001 —— 文件级隔离，绝不让一个坏文件废掉整批
                entry["status"] = "fail"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["seconds"] = time.time() - t0
                results.append(entry)
                print(f"  [失败] {entry['error']}")
                if os.path.exists(job.part):
                    print(f"  内容保留在：{job.part}"
                          f"（若已转完只是改名失败，手动改成 .md 即可；重跑会覆盖它）")
                tail = [ln for ln in log.getvalue().splitlines() if ln.strip()][-12:]
                if tail:
                    print("  该文件的最后几行日志：")
                    for ln in tail:
                        print("    " + ln)
                if args.stop_on_error:
                    print("\n--stop-on-error：停止整批。")
                    return _finish()
                continue

            entry["seconds"] = time.time() - t0
            done_seconds += entry["seconds"]
            done_pages += pages or 0
            print(f"  完成：{job.dest}（{_fmt_secs(entry['seconds'])}）")

            if args.verify or args.html_preview:
                html_path = job.preview(args.html_preview) if args.html_preview else None
                if html_path:
                    os.makedirs(os.path.dirname(html_path), exist_ok=True)
                try:
                    passed, bad, _log = verify_md(job.dest, html_path)
                    entry["verify"] = (passed, bad)
                    if bad:
                        print(f"  体检：{len(bad)} 项不合格 -> {'；'.join(bad)}")
                    else:
                        print(f"  体检：{passed} 项全部通过"
                              + (f"，预览 {html_path}" if html_path else ""))
                except Exception as exc:  # noqa: BLE001 —— 体检挂了不该否定已落盘的产物
                    entry["verify"] = (0, [f"体检脚本异常：{type(exc).__name__}: {exc}"])
                    print(f"  体检异常（产物已落盘）：{exc}")

            results.append(entry)

            # 进度 + 按"每页耗时"估剩余：比按文件数估准得多（页数差异很大）
            if idx < len(jobs):
                elapsed = time.time() - started
                if done_pages and total_pages:
                    remain_pages = max(0, total_pages - done_pages)
                    eta = done_seconds / done_pages * remain_pages
                    print(f"  进度 {idx}/{len(jobs)}｜已用 {_fmt_secs(elapsed)}"
                          f"｜预计剩余约 {_fmt_secs(eta)}（按每页均速估）", flush=True)
                else:
                    print(f"  进度 {idx}/{len(jobs)}｜已用 {_fmt_secs(elapsed)}", flush=True)

        return _finish()

    except KeyboardInterrupt:
        print("\n[中断] 收到 Ctrl+C，已完成的产物不受影响。")
        return _finish()


def _emit_report(args, jobs, results, elapsed, total_pages=0):
    if not args.report:
        return
    payload = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(elapsed, 1),
        "counts": {
            "total": len(jobs),
            "ok": len([r for r in results if r["status"] == "ok"]),
            "skip": len([r for r in results if r["status"] == "skip"]),
            "fail": len([r for r in results if r["status"] == "fail"]),
        },
        "total_pages": total_pages,
        "options": {
            "outdir": args.outdir, "model": args.model, "dpi": args.dpi,
            "pages_per_request": args.pages_per_request, "pages": args.pages,
            "max_tokens": args.max_tokens, "thinking": args.thinking,
            "normalize": not args.no_normalize, "stream": not args.no_stream,
        },
        "results": results,
    }
    try:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\n报表已写出：{args.report}")
    except OSError as exc:
        print(f"\n[警告] 报表写不出去：{exc}")


if __name__ == "__main__":
    sys.exit(main())
