# -*- coding: utf-8 -*-
"""交付物体检：拿一份生成好的讲义 Markdown，逐条核对"能不能正确显示"。

这不是自检脚本的替代品 —— `selfcheck_glm_offline.py` 测的是**程序**（假传输层，
不联网），本脚本测的是**产物**（真实跑出来的 .md 文件）。

核对三件事：

    1. 表格结构与渲染 —— 标签配平、每行单元格数一致、表格内部没有空行
       （CommonMark 里空行会终结 HTML 块，表格会当场断成两半）、
       用 markdown-it 真渲染一遍，确认表格没有被降级成一段纯文本；
    2. 化学记号 —— 表格内外**都是行内 LaTeX**：$ 定界符成对、LaTeX 命令都在 $...$ 里、
       没有残留的 Unicode 上下标与裸 ↑↓、表格的 HTML 标签只搭结构、不用 <sub>/<sup> 冒充化学记号；
    3. 符号正确性 —— 没有 `===`、没有 `->` / `-->` 这类"被写坏的等号"，
       可逆符号是 ⇌ / \\rightleftharpoons，交付物里没有「第 N 页」分页标题。

注意：记号全是 LaTeX，所以交付物的渲染环境必须挂 KaTeX/MathJax（预览页自带 CDN）。

用法：
    python verify_handout.py test1.md
    python verify_handout.py test1.md --html preview.html   # 顺便出预览页
"""

import argparse
import os
import re
import sys

_TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)\b[^<>]*?(/?)>")
_PAIRED = ("table", "thead", "tbody", "tfoot", "tr", "td", "th", "sub", "sup",
           "b", "i", "u", "em", "strong", "span", "div", "p")
_VOID = ("br", "hr", "col", "img")

_PAGE_HEADING_RE = re.compile(r"(?m)^[ \t]*#{0,6}[ \t]*(?:第[ \t]*\d+[ \t]*页|[Pp]age[ \t]*\d+)[^\n]*$")
# 「化学记号」的判据：带化学含义的上下标/系数，出现在 $...$ 之外就是不文明的写法。
# 注意不能只找元素符号 —— 表格里常常写 <sub>2</sub> 这种"HTML 冒充化学记号"，漏检。
_CHEM_UNICODE_SCRIPT_RE = re.compile(r"[₀-₉₊₋⁰-⁹⁺⁻₌₍₎ⁿ]")
_HTML_SCRIPT_TAG_RE = re.compile(r"</?(?:sub|sup)\b", re.IGNORECASE)
# 旧方言（HTML 化学记号）在表格里会留下的旁证：&Delta; / 裸 ↑↓ 在单元格里
_HTML_CHEM_ENTITY_RE = re.compile(r"&(?:Delta|Delt|darr|uarr|harr|rarr|larr);")
_UNICODE_SCRIPT_RE = re.compile(r"[₀-₉₊₋⁰-⁹⁺⁻ⁿ]")
_BAD_EQUALS_RE = re.compile(r"={2,}|(?<![\w\\])-{1,2}>(?!\w)")

RESULTS = []


def check(name, ok, extra=""):
    RESULTS.append((name, bool(ok), extra))
    print(("  [OK]   " if ok else "  [BAD]  ") + name + (f"   {extra}" if extra else ""))
    return ok


def _split_tables(text):
    """返回 (表格块列表, 去掉表格后的正文)。"""
    tables = [m.group(0) for m in _TABLE_RE.finditer(text)]
    return tables, _TABLE_RE.sub("\n", text)


# ---------------------------------------------------------------------------
# 1. 表格结构
# ---------------------------------------------------------------------------
def _row_columns(row_html):
    """一行的**有效列数**：单元格个数 + colspan 的增量。

    合并单元格（colspan="2"）的行天生比别的行少一个 <td>，直接数标签会误判成
    "表格错位"。所以按有效列数算，这才对应渲染出来的列。
    """
    total = 0
    for m in re.finditer(r"<t[dh]\b([^>]*)>", row_html, re.IGNORECASE):
        cs = re.search(r"colspan\s*=\s*[\"']?(\d+)", m.group(1), re.IGNORECASE)
        total += int(cs.group(1)) if cs else 1
    return total


def check_table_structure(tables):
    print("\n== 1. 表格结构 ==")
    if not tables:
        print("  （本文档没有表格，跳过）")
        return
    for i, html in enumerate(tables, 1):
        tag = f"表{i}"

        # 标签配平
        stack, ok = [], True
        for m in _TAG_RE.finditer(html):
            closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
            if name in _VOID or selfclose:
                continue
            if name not in _PAIRED:
                continue
            if closing:
                if not stack or stack.pop() != name:
                    ok = False
                    break
            else:
                stack.append(name)
        check(f"{tag} 标签配平", ok and not stack, f"未闭合={stack}" if stack else "")

        # 行/列结构
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr\s*>", html, re.DOTALL | re.IGNORECASE)
        counts = [_row_columns(r) for r in rows]
        check(f"{tag} 有 {len(rows)} 行", len(rows) >= 1)
        if rows and re.search(r"rowspan\s*=", html, re.IGNORECASE):
            # rowspan 会让"一行占了几列"随行而变，这种表不该按列数一致来判
            print(f"  （{tag} 用了 rowspan：各行占位数本来就可以不同，跳过列数一致性检查）")
        elif rows:
            note = "（含 colspan，按有效列数算）" if re.search(r"colspan\s*=", html, re.I) else ""
            check(f"{tag} 每行有效列数一致（{counts[0]} 列）{note}",
                  len(set(counts)) == 1, f"各行有效列数={counts}")

        # 表格内部空行会终结 HTML 块
        check(f"{tag} 内部没有空行", not re.search(r"\n[ \t]*\n", html))

        # 单元格文本（剥掉 HTML 标签后剩下的才是"化学记号"该呆的地方）
        cell_text = re.sub(r"<[^>]*>", "", html)

        # 单元格里的化学记号必须是行内 LaTeX
        bad = _find_latex_outside_math(cell_text)
        check(f"{tag} 单元格里的 LaTeX 命令都写在 $...$ 里", bad is None,
              f"裸写：{bad}" if bad else "")
        check(f"{tag} 单元格里的 $ 定界符成对", cell_text.count("$") % 2 == 0,
              f"$ 出现 {cell_text.count('$')} 次")
        check(f"{tag} 单元格里没有残留的 Unicode 上下标（应写进 $...$）",
              not _CHEM_UNICODE_SCRIPT_RE.search(cell_text),
              _CHEM_UNICODE_SCRIPT_RE.search(cell_text).group(0)
              if _CHEM_UNICODE_SCRIPT_RE.search(cell_text) else "")
        check(f"{tag} 单元格里没有裸 ↑ / ↓（应写进 $...$）",
              not re.search(r"[↑↓]", cell_text),
              re.search(r"[↑↓]", cell_text).group(0) if re.search(r"[↑↓]", cell_text) else "")
        check(f"{tag} 没有用 <sub>/<sup> 冒充化学记号（结构标签归 HTML，记号归 LaTeX）",
              not _HTML_SCRIPT_TAG_RE.search(html),
              _HTML_SCRIPT_TAG_RE.search(html).group(0) if _HTML_SCRIPT_TAG_RE.search(html) else "")
        ent = _HTML_CHEM_ENTITY_RE.search(cell_text)
        check(f"{tag} 单元格里没有 HTML 化学实体（&Delta; 之流，应写 \\Delta）", ent is None,
              f"命中 {ent.group(0)!r}" if ent else "")


# ---------------------------------------------------------------------------
# 2. 化学记号（表格内外统一的行内 LaTeX）
# ---------------------------------------------------------------------------
def _find_latex_outside_math(text):
    """找出"不在 $...$ 里"的 LaTeX 命令 —— 那种位置渲染器只会原样显示源码。

    只按 `$` 的奇偶切换数学态，不做完整 LaTeX 解析：讲义里够用，且不会误报。
    表格与正文共用这一个判据，所以"表格里漏了 $ 定界符"也会被同一把尺子量出来。
    """
    inside, i = False, 0
    pattern = re.compile(r"\\(?:text|ce|mathrm|uparrow|downarrow|rightleftharpoons"
                         r"|xrightarrow|xleftarrow|overset|underset|stackrel"
                         r"|cdot|Delta|to|leftarrow)\b")
    while i < len(text):
        if text[i] == "$":
            inside = not inside
            i += 1
            continue
        if not inside:
            m = pattern.match(text, i)
            if m:
                return m.group(0)
        i += 1
    return None


def check_dialect(prose):
    print("\n== 2. 化学记号（表格外，行内 LaTeX）==")
    check("正文里没有残留的 Unicode 上下标（应转成 $...$）",
          not _UNICODE_SCRIPT_RE.search(prose),
          _UNICODE_SCRIPT_RE.search(prose).group(0) if _UNICODE_SCRIPT_RE.search(prose) else "")
    outside = _find_latex_outside_math(prose)
    check("LaTeX 命令都写在 $...$ 里", outside is None, f"裸写：{outside}" if outside else "")
    check("正文里没有裸 ↑ / ↓（应写在 $...$ 里）",
          not re.search(r"[↑↓]", prose),
          re.search(r"[↑↓]", prose).group(0) if re.search(r"[↑↓]", prose) else "")
    for i, line in enumerate(prose.split("\n"), 1):
        n = line.count("$")
        if n % 2:
            check(f"第 {i} 行 $ 成对", False, line.strip()[:70])
            break
    else:
        check("所有行的 $ 定界符成对", True)


# ---------------------------------------------------------------------------
# 3. 符号与分页
# ---------------------------------------------------------------------------
def check_symbols(text):
    print("\n== 3. 符号与分页 ==")
    bad_eq = _BAD_EQUALS_RE.search(text)
    check("没有 === / -> / --> 这类坏等号", bad_eq is None,
          f"命中 {bad_eq.group(0)!r}" if bad_eq else "")
    check("没有「第 N 页」分页标题", not _PAGE_HEADING_RE.search(text),
          _PAGE_HEADING_RE.search(text).group(0).strip() if _PAGE_HEADING_RE.search(text) else "")
    check("没有 HTML 注释/页序标记", "<!--" not in text)
    check("没有代码围栏残留", "```" not in text)
    has_rev = "⇌" in text or r"\rightleftharpoons" in text
    print(f"  （可逆符号出现：{'是' if has_rev else '本文档没有可逆反应'}）")


# ---------------------------------------------------------------------------
# 4. 真渲染一遍（markdown-it）
# ---------------------------------------------------------------------------
def render_html(text):
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        return None
    md = MarkdownIt("commonmark", {"html": True, "linkify": False})
    md.enable("table")
    return md.render(text)


def check_render(text, n_tables, tables=(), out_html=None):
    print("\n== 4. 渲染检查（markdown-it）==")
    html = render_html(text)
    if html is None:
        print("  （未安装 markdown-it-py，跳过渲染检查：pip install markdown-it-py）")
        return
    rendered_tables = len(re.findall(r"<table\b", html, re.IGNORECASE))
    check("渲染出的表格数 = 源码里的表格数", rendered_tables == n_tables,
          f"渲染 {rendered_tables} / 源码 {n_tables}")
    check("源码里的表格没有被转义成纯文本", "&lt;table" not in html)
    # 只有"表格里确实写了化学式"时才要求出现 $...$：
    # 一张纯文字的对照表不该因为没有公式而被判不合格。
    chem_in_tables = any(
        re.search(r"[A-Z][a-z]?\d|[₀-₉₊₋⁰-⁹⁺⁻ⁿ]|[↑↓]|\\text\{",
                  re.sub(r"<[^>]*>", "", t))
        for t in tables
    )
    check("渲染结果里保留了 $...$ 数学片段（表格里有化学式就该有）",
          (not chem_in_tables) or "$" in html)
    if out_html:
        page = _build_preview(html)
        with open(out_html, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"  HTML 预览已写出：{out_html}（含 KaTeX CDN，联网打开即可看到公式）")


_PREVIEW = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>讲义预览</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"
        onload="renderMathInElement(document.body,{delimiters:[{left:'$$',right:'$$',display:true},{left:'$',right:'$',display:false}]});"></script>
<style>
 body{max-width:920px;margin:32px auto;padding:0 18px;line-height:1.75;
      font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:#1f2328}
 table{border-collapse:collapse;margin:16px 0;width:100%}
 th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left;vertical-align:top}
 th{background:#f6f8fa}
 sub,sup{font-size:.72em}
 hr{border:none;border-top:1px solid #e5e7eb;margin:24px 0}
 code{background:#f6f8fa;padding:1px 4px;border-radius:4px}
</style></head><body>
__BODY__
</body></html>
"""


def _build_preview(html):
    return _PREVIEW.replace("__BODY__", html)


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="verify_handout.py",
        description="对生成好的讲义 Markdown 做交付前体检（表格渲染 / 化学式方言 / 符号）",
    )
    ap.add_argument("markdown", help="待体检的讲义 .md")
    ap.add_argument("--html", default=None, help="顺便写出一份带表格样式的 HTML 预览")
    args = ap.parse_args(argv)

    with open(args.markdown, "r", encoding="utf-8") as f:
        text = f.read()

    tables, prose = _split_tables(text)
    print(f"文件：{args.markdown}")
    print(f"体量：{len(text)} 字符，{text.count(chr(10)) + 1} 行，表格 {len(tables)} 张")

    check_table_structure(tables)
    check_dialect(prose)
    check_symbols(text)
    check_render(text, len(tables), tables, args.html)

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    if bad:
        print(f"体检不通过 ❌  共 {len(bad)} 项：")
        for n in bad:
            print("  - " + n)
        return 1
    print(f"体检通过 ✅  共 {len(RESULTS)} 项检查全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
