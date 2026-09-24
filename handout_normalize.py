# -*- coding: utf-8 -*-
r"""输出后处理：把模型生成的化学讲义 Markdown 归一化成单一、稳定、能正确渲染的格式。

核心约定只有一条：**表格用 HTML 搭结构，表格内外的化学记号一律 LaTeX**。

    表格外（正文、题目、方程式）→ 行内 LaTeX：$\\text{H}_2\\text{O}$、$\\text{Fe}^{3+}$
    表格内（HTML 表格的单元格）  → 同一套行内 LaTeX，只是被 <td>/<th> 包着：
                                    <td>$\\text{Na}_2\\text{CO}_3$</td>

为什么表格结构仍归 HTML、记号却归 LaTeX：`<table>` 是 Markdown 里唯一能保证
表格被正确排版的写法（管道表会被各种渲染器各自解释）；而化学记号只有一套写法
才好维护 —— 上下标、可逆箭头、加热条件、电荷都交给 KaTeX/MathJax 排，正文与
表格不会出现"同一种物质两种长相"。代价是渲染环境必须挂 KaTeX（本工程的预览页
与体检预览都自带 CDN），所以交付物的渲染前提写在 README 里。

本模块做的事（全部确定性、纯标准库，可离线复跑）：

1. 剥掉包裹正文的 ```markdown / ```latex 代码围栏，删掉 HTML 注释与页序标记；
   HTML 实体归一：`&Delt;` → `&Delta;`、`&uarr;` → `↑`、丢掉 `&#x200b;` 这类隐形字符；
2. 删掉「第 N 页」这类分页标题，删除助手口吻的收尾寒暄 —— 交付物是一份连续讲义，
   页码只是流水线的中间量；
3. 把 \\[ ... \\]、\\begin{align*}、\\( ... \\) 统一成行内 $...$；
   把 `\\xlongequal{\\Delta}`（KaTeX 不支持的"等号上写条件"）改写成 `\\overset{\\Delta}{=}`，
   `\\overset{..}{=}` / `\\stackrel{..}{=}`（KaTeX 支持）原样保留；
   把旧写法 `(\\Delta) =`（加热条件写在等号左侧括号里）统一成 `\\overset{\\Delta}{=}`；
4. 表格感知的化学记号归一（见上）：
   - HTML 表格：单元格里的化学记号走与正文完全相同的 LaTeX 流水线
     （\ce{} → \text{}、Unicode 上下标 → `_`/`^`、裸化学式包进 `$...$`、条件箭头保形），
     另加三件表格专属的事：把模型写顺手了的 <sub>/<sup> 折叠回 Unicode 上下标、
     转义裸 `<`、补齐长短不一的行（缺的单元格补空 <td>）；
     表格内部的空行一律删掉（CommonMark 里空行会终结 HTML 块，表格会当场断掉）；
   - Markdown 管道表（| a | b |）就地升级成 HTML 表格，保证交付物里没有"半成品表"；
5. 化学记号本身的写法归一：
   - 等号写成 =；ASCII 箭头 -> / --> 还原成 =（它本来就是被写坏的等号）；
   - 可逆反应统一成 ⇌（LaTeX 侧是 \\rightleftharpoons）；
   - Unicode 箭头 → 只在"真正的箭头"（转化关系、合成路线，两侧没有加号式子）里保留；
   - 上下标与电荷消歧：Fe3+ → Fe^{3+}、SO42- → SO_4^{2-}、MnO4- → MnO_4^-、
     NH4+ → NH_4^+，而 2Na+2H2O 里的 + 仍然是加号；
   - 被空格切开的相邻数学片段缝合成一个 `$...$`；
   - `$...$` 内的 Unicode 化学符号换成 LaTeX 命令（\\rightleftharpoons / \\uparrow / \\to）；
6. 收尾：空行压缩、行尾空白、连续分隔线合并。

本模块可独立运行（便于对已有输出文件做离线清洗）：

    python handout_normalize.py 输入.md [输出.md]      # 缺省输出时原地覆盖
"""

import re
import sys

# ---------------------------------------------------------------------------
# 0. 基础字符表：Unicode 上下标、元素符号
# ---------------------------------------------------------------------------
_UNICODE_SUB = {
    "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
    "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
    "₊": "+", "₋": "-",
}

_UNICODE_SUP = {
    "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
    "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
    "⁺": "+", "⁻": "-", "ⁿ": "n",
}

# 真实元素符号表（1-118）。有它才能把 "LaTeX"、"A1"、"Delta" 这类词排除在化学式之外，
# 也能让 O2、Cl- 这种单元素式子被正确识别。
_ELEMENTS = frozenset("""
H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn
Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La
Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po
At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg
Cn Nh Fl Mc Lv Ts Og
""".split())

_ELEMENT_RE = re.compile(r"[A-Z][a-z]?")

_BLACKLIST = frozenset({
    "LaTeX", "TeX", "KaTeX", "Markdown", "MathJax",
    "PDF", "PNG", "GPU", "CPU", "ID", "OK", "HTML", "CSS", "API",
})

# 需要触发化学记号转换的字符（Unicode 上下标 / 气体沉淀符号 / 显式下标）
_NEED_CONV = set(_UNICODE_SUB) | set(_UNICODE_SUP) | {"↑", "↓", "_"}


# ---------------------------------------------------------------------------
# 1. 代码围栏、HTML 注释
# ---------------------------------------------------------------------------
_FENCE_LINE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _strip_code_fences(text):
    """删除所有 ``` 围栏行（markdown/latex/空语言），保留围栏内部内容。"""
    return _FENCE_LINE_RE.sub("", text).strip()


def _strip_html_comments(text):
    """删掉 HTML 注释。

    必须在"箭头 → 等号"之前做：页序标记 `<!-- page: 2 -->` 里的 `-->` 会被
    箭头规则命中（`-+>` 匹配），留下 `<!-- page: 2 =` 这种半截注释。
    """
    return _COMMENT_RE.sub("", text)


# --- 1b. HTML 实体归一 ------------------------------------------------------
# 实测 GLM 会写 `&Delt;`（漏了字母 a，浏览器原样显示 "&Delt;"）、`&uarr;`、
# `&#x200b;`（零宽空格，它拿来做缩进对齐的隐形字符）等等。这里把化学相关的实体
# 统一成项目约定的写法（Unicode 箭头 / &Delta;），并把不可见字符丢掉。
_ENTITY_MAP = {
    "uarr": "↑", "darr": "↓", "rarr": "→", "larr": "←", "harr": "⇌", "hArr": "⇌",
    "uparrow": "↑", "downarrow": "↓", "rightarrow": "→", "leftarrow": "←",
    "rightleftharpoons": "⇌", "leftrightharpoons": "⇌",
    "Delta": "&Delta;", "delta": "&Delta;", "Delt": "&Delta;",   # Delt：模型漏字母
    "times": "×", "divide": "÷", "deg": "°", "plusmn": "±", "middot": "·",
}
_ENTITY_NUM_MAP = {
    "8593": "↑", "8595": "↓", "8594": "→", "8592": "←", "8652": "⇌", "8660": "⇌",
    "916": "&Delta;", "9651": "&Delta;", "215": "×", "247": "÷", "176": "°",
    "177": "±", "183": "·", "8722": "-",
}
_ZW_ENTITY_CODES = {"x200b", "8203", "x200c", "8204", "x200d", "8205", "xfeff", "65279"}
_ENTITY_RE = re.compile(r"&(#[Xx][0-9A-Fa-f]+|#\d+|[A-Za-z][A-Za-z0-9]*);")
_INVISIBLE_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff")


def _normalize_entities(text):
    """HTML 实体 → 统一的 Unicode / &Delta; 写法；丢掉零宽空格之类的隐形字符。"""
    def repl(m):
        body = m.group(1)
        if not body.startswith("#"):
            return _ENTITY_MAP.get(body, m.group(0))
        code = body[2:] if body[1] in "xX" else body[1:]
        if (code.lower() if body[1] in "xX" else code) in _ZW_ENTITY_CODES:
            return ""
        try:
            value = int(code, 16) if body[1] in "xX" else int(code)
        except ValueError:
            return m.group(0)
        return _ENTITY_NUM_MAP.get(str(value), m.group(0))

    text = _ENTITY_RE.sub(repl, text)
    for ch in _INVISIBLE_CHARS:
        text = text.replace(ch, "")
    return text


# ---------------------------------------------------------------------------
# 2. 助手口吻收尾语
# ---------------------------------------------------------------------------
_ASSISTANT_RE = re.compile(
    r"(?m)^[ \t]*[>]*[ \t]*(?:"
    r"请注意[，,][^\n]*Markdown[^\n]*"
    r"|请注意[，,][^\n]*LaTeX[^\n]*"
    r"|以上(?:就是|是)[^\n]*(?:详细说明|说明|总结|介绍)[^\n]*"
    r"|希望这[^\n]*(?:对|帮助)[^\n]*"
    r"|如需(?:进一步|更多)[^\n]*"
    r"|如果(?:需要|您需要|你需要)[^\n]*(?:请随时|告诉)[^\n]*"
    r"|请随时(?:告诉)?[^\n]*"
    r"|通过(?:这些|上述|以上)[^\n]*(?:我们|可以)[^\n]*"
    r")[。！!]?\s*$"
)


def _strip_assistant_remarks(text):
    """删除成段的助手口吻收尾语。"""
    return _ASSISTANT_RE.sub("", text).strip()


# --- 2b. 分页标题：交付物是一份连续讲义，不是"按页拼起来的流水账" ----------------
# 页码是流水线的中间量（批大小、页序对齐用）。提示词里已经禁止模型输出，
# 但模型偶尔照抄课件页脚/自己加标题，这里做最后一道清除：
# 只删"标题行"与"独占一行的页码"，不碰正文里正常提到的页码。
_PAGE_HEADING_RE = re.compile(
    r"(?m)^[ \t]*(?:"
    r"#{1,6}[ \t]*第[ \t]*\d+[ \t]*页[^\n]*"                     # ## 第 3 页 / ## 第 3 页内容
    r"|#{1,6}[ \t]*[Pp]age[ \t]*\d+[^\n]*"                       # ## Page 3
    r"|第[ \t]*\d+[ \t]*页(?:内容|讲义|正文)?[ \t]*"                # 独占一行的 第 3 页
    r"|[Pp]age[ \t]*\d+[ \t]*"
    r")$"
)
_PAGE_MARK_LINE_RE = re.compile(r"(?m)^[ \t]*<!--\s*page\s*:\s*\d+\s*-->[ \t]*$")


def _strip_page_headings(text):
    """删掉「第 N 页」这类分页标题与页序标记行。"""
    text = _PAGE_HEADING_RE.sub("", text)
    return _PAGE_MARK_LINE_RE.sub("", text)


# ---------------------------------------------------------------------------
# 3. display math / align / 圆括号数学 → 行内 $...$
# ---------------------------------------------------------------------------
def _collapse_align(text):
    def repl(match):
        body = match.group(1)
        parts = [p.strip() for p in re.split(r"\\\\", body)]
        parts = [re.sub(r"^\s*&", "", p).strip() for p in parts if p.strip()]
        return " ".join("$" + p + "$" for p in parts)

    return re.sub(
        r"\\begin\{align\*?\}(.*?)\\end\{align\*?\}",
        repl,
        text,
        flags=re.DOTALL,
    )


def _display_to_inline(text):
    def repl(match):
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        return "$" + body + "$"

    return re.sub(r"\\\[(.*?)\\\]", repl, text, flags=re.DOTALL)


# --- 3b. \( ... \)（LaTeX 行内数学的另一种写法）→ $...$ ---------------------
# 云端模型（实测 GLM-4.6V-Flash）会写出 \(\ce{+11 2 8 1}\) 这种"LaTeX 圆括号"行内数学，
# 它与本项目"化学式只能是 $...$"的约定不符，且**必须在这一步就换掉**：
# 后面的 _unicode_to_latex 只把 $...$ 当数学片段保护，\(...\) 不在保护范围内，
# 里面的 \text{Na} 会被逐字符扫到、再包一层 $，产出 $..\text{$...$}..$ 这种坏输出。
_PAREN_MATH_RE = re.compile(r"\\\((.*?)\\\)", re.DOTALL)


def _paren_math_to_inline(text):
    def repl(match):
        body = re.sub(r"\s+", " ", match.group(1)).strip()
        return "$" + body + "$" if body else ""

    return _PAREN_MATH_RE.sub(repl, text)


# --- 3c. "等号/箭头上方写条件"的几种写法 → 项目统一写法 --------------------------
# 项目约定：反应条件压在等号上（KaTeX 支持 \overset / \underset / \stackrel），
# 加热写 `\overset{\Delta}{=}`，催化剂 + 加热写 `\overset{催化剂}{\underset{\Delta}{=}}`。
# 实测 GLM 会写 `\xlongequal{\Delta}`（来自 extpfeil 宏包），KaTeX **不支持**它，
# 渲染出来就是一个红色报错，所以把它改写成等价的 \overset{...}{=}。
# `\overset` / `\stackrel` 本身 KaTeX 支持，原样保留，绝不降级成 `(\Delta) =`。
_XLONGEQUAL_RE = re.compile(
    r"\\(?:xlongequal|longequal)\s*(?:\[([^\]]*)\])?\s*(\{(?:[^{}]|\{[^{}]*\})*\})")
_XLONGEQUAL_BARE_RE = re.compile(r"\\(?:xlongequal|longequal)")

_GROUP_CMD_RE = re.compile(r"\\(?:text|mathrm|textrm|mbox|ce|mathbf|mathsf|operatorname)\b")

# 带条件的箭头/等号命令。xrightarrow / xleftarrow 是 KaTeX 支持的，一律保留原样
# （条件压在箭头上更贴近课本排版，表格内外同款）；其余几个 KaTeX 不支持，必须改写。
_COND_ARROW_RE = re.compile(
    r"\\(xrightarrow|xleftarrow|xlongequal|longequal|xrightleftharpoons"
    r"|xleftrightharpoons|xrightleftarrows|xleftrightarrows)\b")
# 正文与表格共用的写法：KaTeX 不支持的那几个改写成 LaTeX 命令
_LATEX_COND_ARROW = {
    "xrightleftharpoons": r"\rightleftharpoons",
    "xleftrightharpoons": r"\rightleftharpoons",
    "xrightleftarrows": r"\rightleftharpoons",
    "xleftrightarrows": r"\rightleftharpoons",
}


def _replace_cond_arrows(text, arrow_map, transform=None):
    """`\\xrightarrow[下]{上}` / `\\xrightleftharpoons[下]{上}` → `(上, 下) 箭头`。

    arrow_map 里没有的命令原样保留；transform 用来递归处理条件文本本身
    （早年 HTML 方言要把条件里的 LaTeX 也降级掉，现在两种语境都是 LaTeX，缺省恒等）。
    """
    transform = transform or (lambda s: s)
    out, i = [], 0
    while True:
        m = _COND_ARROW_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        arrow = arrow_map.get(m.group(1))
        out.append(text[i:m.start()])
        if arrow is None:
            out.append(m.group(0))
            i = m.end()
            continue

        j = m.end()
        while j < len(text) and text[j] == " ":
            j += 1
        above = below = None
        while j < len(text) and text[j] in "[{":
            open_ch = text[j]
            end = _balanced_group_end(text, j)
            if end is None:
                break
            if open_ch == "[":
                if below is None:
                    below = text[j + 1:end - 1]
            else:
                above = text[j + 1:end - 1]
            j = end
            while j < len(text) and text[j] == " ":
                j += 1
        conds = [transform(p) for p in (above, below) if p and p.strip()]
        # 尾部一定要留空格：LaTeX 的命令名吃字母，`\rightleftharpoonsNaHCO_3`
        # 会被当成一个叫 rightleftharpoonsNaHCO 的命令（未定义 → 渲染报错）。
        out.append("(" + ", ".join(conds) + ") " + arrow + " " if conds else arrow + " ")
        i = j
    return "".join(out)


def _normalize_condition_equals(text):
    """把 KaTeX 不支持的条件等号命令改写成 KaTeX 支持的写法。

    * `\\xlongequal{上}` → `\\overset{上}{=}`
    * `\\xlongequal[下]{上}` → `\\overset{上}{\\underset{下}{=}}`
    * 裸 `\\xlongequal` → `=`
    * `\\overset{..}{=}` / `\\stackrel{..}{=}` 原样保留（KaTeX 支持，不再降级）
    * `\\xrightleftharpoons[下]{上}` → `(上, 下) \\rightleftharpoons`（KaTeX 不支持前者）

    分两步正则是有意的：带条件的写法要吃掉 `{...}`，裸写法不能顺手把后面的空格也吃掉
    （否则 `$A \\xlongequal B$` 会变成 `$A =B$`）。
    """
    def longequal(m):
        above = (m.group(2) or "")[1:-1].strip()
        below = (m.group(1) or "").strip()
        if above and below:
            return r"\overset{%s}{\underset{%s}{=}}" % (above, below)
        if above:
            return r"\overset{%s}{=}" % above
        if below:
            return r"\underset{%s}{=}" % below
        return "="

    text = _XLONGEQUAL_RE.sub(longequal, text)
    text = _XLONGEQUAL_BARE_RE.sub("=", text)
    return _replace_cond_arrows(text, _LATEX_COND_ARROW)


# ===========================================================================
# 4. 化学记号内核：一次解析，两种方言（latex / html）
# ===========================================================================
_OPEN_CLOSE = {"{": "}", "[": "]", "(": ")"}


def _balanced_group_end(text, i):
    """text[i] 是 { 或 [，返回配对的收尾字符之后的下标；不闭合则返回 None。"""
    open_ch = text[i]
    close_ch = _OPEN_CLOSE.get(open_ch)
    if close_ch is None:
        return None
    depth = 0
    k = i
    while k < len(text):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return None


def _read_script(text, i):
    """读取 `_` / `^` 后面的上下标内容，返回 (内容, 新下标)。

    只接受数字、`{...}` 与单个正负号：`H_2O` 是下标，但 `file_name` 里的
    `_` 不是上下标（后面跟字母），必须原样留着，否则会把普通文本改成数学式。
    """
    if i >= len(text):
        return None, i
    if text[i] == "{":
        end = _balanced_group_end(text, i)
        if end is None:
            return None, i
        return _clean_script(text[i + 1:end - 1]), end
    m = re.match(r"[0-9]+[+-]?|[+-][0-9]*", text[i:])
    if m:
        return m.group(0), i + len(m.group(0))
    return None, i


def _clean_script(inner):
    """剥掉上下标内容里残留的 LaTeX 外壳：\\text{2} → 2。"""
    inner = re.sub(r"\\(?:text|mathrm|rm|mathbf|mathsf|ce)\{([^{}]*)\}", r"\1", inner)
    return inner.replace("{", "").replace("}", "").replace("\\", "").strip()


# 电荷符号后面出现这些字符（或直接到结尾）时，它才是"电荷"而不是"加号分隔符"。
_CHARGE_TAIL = set(" \t\u3000\u00a0,;，；、)]}）】>|=&*$↑↓→⇌")


def _at_charge_tail(text, i):
    """text[i] 是 + / -，判断它是否处在"电荷位置"。"""
    if i + 1 >= len(text):
        return True
    nxt = text[i + 1]
    if nxt in _CHARGE_TAIL:
        return True
    return nxt in "+-"          # Na+ + Cl- 里的第一个 +：后面紧跟另一个符号


def _parse_chem_token(token):
    """把一串化学记号解析成 [(kind, value), ...]；kind ∈ {elem, sub, sup, sep, plain}。

    "数字紧跟符号"到底是下标还是电荷，是这份后处理里唯一需要动脑的地方：

        H2O     → H_2 O          （数字是下标）
        Fe3+    → Fe^{3+}        （单元素 + 数字 + 符号 = 电荷）
        Ca2+    → Ca^{2+}
        SO42-   → SO_4^{2-}      （多元素式子：末位数字是电荷量，其余是下标）
        MnO4-   → MnO_4^-
        NH4+    → NH_4^+
        2Na+2H2O → 2Na + 2H_2O   （+ 后面紧跟数字 → 它是加号，不是电荷）

    判据：数字后紧跟 +/-，且该符号处在"电荷位置"（后面是结尾/空白/右括号/另一个符号），
    才可能是电荷；否则那个符号是分隔两个式子的加号。
    """
    parts = []
    i, n = 0, len(token)
    seen_element = False

    def push(kind, value):
        if value:
            parts.append((kind, value))

    while i < n:
        c = token[i]

        # --- 显式上下标 ---------------------------------------------------
        if c in "_^":
            kind = "sub" if c == "_" else "sup"
            value, j = _read_script(token, i + 1)
            if value is None:
                push("plain", c)              # 不是上下标（如 file_name）→ 原样保留
                i += 1
            else:
                push(kind, value)
                i = j
            continue

        # --- Unicode 上下标 ----------------------------------------------
        if c in _UNICODE_SUB or c in _UNICODE_SUP:
            table = _UNICODE_SUB if c in _UNICODE_SUB else _UNICODE_SUP
            kind = "sub" if table is _UNICODE_SUB else "sup"
            j, buf = i, []
            while j < n and token[j] in table:
                buf.append(table[token[j]])
                j += 1
            push(kind, "".join(buf))
            i = j
            continue

        # --- 元素符号 -----------------------------------------------------
        m = _ELEMENT_RE.match(token, i)
        if m and m.group(0) in _ELEMENTS:
            sym = m.group(0)
            first_element = not seen_element
            seen_element = True
            i = m.end()

            m2 = re.match(r"\d+", token[i:])
            digits = m2.group(0) if m2 else ""
            after = i + len(digits)

            # 数字 + 正负号 处在电荷位置
            if digits and after < n and token[after] in "+-" and _at_charge_tail(token, after):
                if first_element:
                    push("elem", sym)
                    push("sup", digits + token[after])
                elif len(digits) > 1:
                    push("elem", sym)
                    push("sub", digits[:-1])
                    push("sup", digits[-1] + token[after])
                else:
                    push("elem", sym)
                    push("sub", digits)
                    push("sup", token[after])
                i = after + 1
                continue

            # 普通下标（ASCII 数字 + Unicode 下标）与 Unicode 上标
            sub = digits
            i = after
            while i < n and token[i] in _UNICODE_SUB:
                sub += _UNICODE_SUB[token[i]]
                i += 1
            push("elem", sym)
            push("sub", sub)
            sup = ""
            while i < n and token[i] in _UNICODE_SUP:
                sup += _UNICODE_SUP[token[i]]
                i += 1
            push("sup", sup)
            # 裸电荷：Na+ / Cl- / O2-（前面没有数字，或者数字已当下标吃掉）
            if i < n and token[i] in "+-" and _at_charge_tail(token, i):
                if not sub and not sup:
                    push("sup", token[i])
                    i += 1
            continue

        # --- 右括号后的系数：Cu(OH)2 → Cu(OH)_2 ---------------------------
        if c == ")":
            push("plain", c)
            i += 1
            m2 = re.match(r"\d+", token[i:])
            if m2:
                push("sub", m2.group(0))
                i += len(m2.group(0))
            continue

        # --- 光秃秃的 ^ / _（mhchem 的"气体/沉淀上标"写法，如 `... + H2 ^`）---
        if c in "^_" and not re.match(r"[0-9]+[+-]?|[+-][0-9]*", token[i + 1:i + 2]):
            rest = token[i + 1:].strip()
            if not rest:
                # 裸露的 ^ 留在 LaTeX 里会让 KaTeX 直接报错，按化学惯例补成上标 +
                push("sup", "+")
                i += 1
            else:
                push("plain", c)
                i += 1
            continue

        # --- 其它字符 -----------------------------------------------------
        if c == "+":
            push("sep", c)                    # 分隔两个式子的加号（渲染时补空格）
        else:
            push("plain", c)
        i += 1

    return parts


_LATEX_PLAIN = {"↑": r"\uparrow", "↓": r"\downarrow", "·": r"\cdot", "⇌": r"\rightleftharpoons"}


def _render_script(value, kind, dialect):
    if not value:
        return ""
    if dialect == "html":
        return f"<{kind}>{value}</{kind}>"
    if kind == "sub":
        return "_" + (value if len(value) == 1 else "{" + value + "}")
    return "^" + (value if len(value) == 1 else "{" + value + "}")


def _render_chem(parts, dialect):
    out = []
    for kind, value in parts:
        if kind == "elem":
            out.append(value if dialect == "html" else "\\text{" + value + "}")
        elif kind in ("sub", "sup"):
            out.append(_render_script(value, kind, dialect))
        elif kind == "sep":
            out.append(" + " if dialect == "html" else "+")
        elif dialect == "latex":
            out.append(_LATEX_PLAIN.get(value, value))
        else:
            out.append(value)
    return "".join(out)


def _formula_to_latex(s):
    """化学记号串 → 行内 LaTeX 片段。

    Na₂O₂    → \\text{Na}_2\\text{O}_2
    SO₄²⁻     → \\text{S}\\text{O}_4^{2-}
    Cu(OH)_2↓ → \\text{Cu}(\\text{O}\\text{H})_2\\downarrow
    2Na+2H2O  → 2\\text{Na} + 2\\text{H}_2\\text{O}
    """
    return _render_chem(_parse_chem_token(s), "latex")


def _formula_to_html(s):
    """化学记号串 → 原生 HTML 化学标记。

    历史遗留：表格单元格曾经用 HTML 方言（<sub>/<sup>），现在表格内外统一走
    LaTeX，这个函数只剩"渲染成 HTML 标记"的能力，供离线工具或自定义渲染使用。
    """
    return _render_chem(_parse_chem_token(s), "html")


# --- 4b. 化学式识别 ---------------------------------------------------------
def _is_formula_token(token):
    """token 是不是一个"化学式"：真实元素符号 + 数字 + 括号 + 可选末尾电荷。

    H2O / Na2CO3 / 2NaOH / Ca(OH)2 / Fe3+ / SO42- / Cl- / MnO4- / O2 → True
    LaTeX / Markdown / A1 / pH / 2 / OK / Delta                        → False
    """
    if not token or token in _BLACKLIST:
        return False
    body = token[:-1] if token[-1] in "+-" else token
    if len(body) < 2:
        return False

    elements, i, n = 0, 0, len(body)
    while i < n:
        c = body[i]
        if c.isdigit():
            i += 1
            continue
        if c == "(":
            end = _balanced_group_end(body, i)
            if end is None:
                return False
            if not _is_formula_token(body[i + 1:end - 1]):
                return False
            elements += 1
            i = end
            continue
        m = _ELEMENT_RE.match(body, i)
        if m and m.group(0) in _ELEMENTS:
            elements += 1
            i = m.end()
            continue
        return False

    if elements < 1:
        return False
    if elements >= 2:
        return True
    # 单元素式子：只有带数字或带电荷才算化学式（H2 → 是；K → 不是，那可能只是选项 A/B/C）
    return token[-1] in "+-" or any(ch.isdigit() for ch in body)


def _needs_chem(token):
    """这段 token 需要做化学记号转换吗？两种方言共用同一判据。"""
    if not token:
        return False
    if any(ch in _NEED_CONV for ch in token) or "^" in token:
        return True
    if _is_formula_token(token):
        return True
    # 多个式子用 + / = 连成一串（2Na+2H2O=2NaOH+H2↑ 这种没空格的写法）：逐段判断
    if re.search(r"[+\-=]", token):
        segs = [s for s in re.split(r"[+\-=]", token) if s]
        if len(segs) >= 2 and all(_is_formula_token(s) for s in segs):
            return True
    return False


# 化学记号里允许出现的字符（决定"一个 token 到哪儿结束"）。
# 必须包含 -：否则 "SO42-" 会在 "-" 处断开，电荷丢在数学片段外面变成 $SO_42$-。
_CHEM_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789()·↑↓_+-=^")
_CHEM_CHARS |= _NEED_CONV


def _looks_chemical(text):
    """这一段文本是不是"化学语境"（决定 -> / → 该当等号还是当箭头）。"""
    if any(k in text for k in (r"\text{", r"\ce{", r"\xrightarrow", r"\mathrm{",
                               r"\rightleftharpoons", r"\to ")):
        return True
    if any(ch in _NEED_CONV for ch in text if ch != "_"):
        return True
    if re.search(r"[A-Z][a-z]?\d", text):
        return True
    for tok in re.findall(r"[A-Za-z()0-9+]+", text):
        if _is_formula_token(tok):
            return True
    return False


def _looks_like_equation(text):
    """带 → 的一段是否其实是个方程式（→ 是被写坏的等号）。

    判据：箭头两侧是"式子 + 加号"的结构（2Na + 2H2O → 2NaOH + H2↑）。
    纯转化关系 / 合成路线（Na → NaOH → Na2CO3）里没有加号，箭头原样保留。
    """
    if "+" not in text:
        return False
    if not any(a in text for a in ("→", "->", "-->", "=", r"\xrightarrow")):
        return False
    return _looks_chemical(text)


# ===========================================================================
# 5. 表格：HTML 搭结构，单元格内的化学记号走与正文相同的 LaTeX 流水线
# ===========================================================================
_TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.DOTALL | re.IGNORECASE)

_HTML_TAGS = frozenset({
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "br", "sub", "sup", "b", "i", "u", "s", "em", "strong", "span", "div", "p",
    "small", "big", "code", "mark", "ul", "ol", "li", "font", "hr",
})

_KNOWN_TAG_RE = re.compile(
    r"</?(?:" + "|".join(sorted(_HTML_TAGS)) + r")\b[^<>]*/?>", re.IGNORECASE)

_REVERSIBLE_RE = re.compile(r"<\s*(?:-|=){1,2}\s*>")

# 单元格里的 <sub>/<sup>：模型照抄课件、或沿用了旧提示词时会写出来。
# 表格结构归 HTML，化学记号归 LaTeX —— 所以先把它们折叠成 Unicode 上下标，
# 再交给正文那条 LaTeX 流水线（H<sub>2</sub>O → H₂O → $\text{H}_2\text{O}$）。
_SUBSUP_PAIR_RE = re.compile(r"<(sub|sup)\b[^>]*>(.*?)</\1\s*>", re.DOTALL | re.IGNORECASE)
_SUBSUP_OPEN_RE = re.compile(r"</?(?:sub|sup)\b[^>]*>", re.IGNORECASE)
_SUBSUP_MAP = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "−": "₋", "=": "₌", "(": "₍", ")": "₎",
    "n": "ₙ", "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ",
}
_SUBSUP_SUP_MAP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "−": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "n": "ⁿ",
}


def _flatten_subsup(text):
    """`H<sub>2</sub>O` → `H₂O`；`Fe<sup>3+</sup>` → `Fe³⁺`；无法折叠的原样保留。"""
    def repl(m):
        table = _SUBSUP_MAP if m.group(1).lower() == "sub" else _SUBSUP_SUP_MAP
        body = re.sub(r"<[^>]*>", "", m.group(2)).strip()
        if not body or any(ch not in table for ch in body):
            return m.group(0)
        return "".join(table[ch] for ch in body)

    text = _SUBSUP_PAIR_RE.sub(repl, text)
    # 落单的 <sub>/<sup>（没闭合）直接摘掉标签，别让它们混进数学片段
    return _SUBSUP_OPEN_RE.sub("", text)


# 单元格里的可逆符号：`<=>` / `<->` / `&lt;=>`（后者是"裸 < 转义"跑过一次的残留）。
# 必须在 _escape_stray_lt **之前**还原成 ⇌，否则 `<=>` 会先变成 `&lt;=>`，
# 可逆符号就再也认不出来了（实测踩过）。
_TABLE_REVERSIBLE_RE = re.compile(
    r"(?:&lt;|<)\s*(?:-|=){1,2}\s*(?:&gt;|>)")


def _table_reversible_to_arrow(text):
    """单元格里的 ASCII 可逆符号 → ⇌（`<=>`、`<->`、`&lt;=>` 都算）。"""
    return _TABLE_REVERSIBLE_RE.sub("⇌", text)


def _outside_math(text, fn):
    r"""只对"不在 `$...$` 里"的片段应用 fn（数学片段原样保留）。

    表格里所有"补数学片段"的收尾处理都走这一个入口：把"跳过已有 $...$"这件事
    收敛成一处，避免每个 pass 各写一遍、漏掉一处就产生 `$$\Delta$$` 这种嵌套。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])       # 数学片段原样保留
            i = j + 1
            continue
        j = text.find("$", i)
        if j == -1:
            j = n
        out.append(fn(text[i:j]))
        i = j
    return "".join(out)


# 单元格里孤立的加热符号（不在 LaTeX 命令里、也不在 $...$ 里）
_BARE_DELTA_RE = re.compile(r"(?<!\\)(△|Δ)")


def _wrap_delta_symbols(text):
    """孤立的 Δ / △ → `$\\Delta$`（只处理数学片段之外的）。

    Δ 不属于 _CHEM_CHARS，_unicode_to_latex 的 token 扫描不会认领它，
    于是它会以裸字符留在单元格里（旧 HTML 方言里这是对的，LaTeX 方言里不是）。
    """
    return _outside_math(text, lambda gap: _BARE_DELTA_RE.sub(r"$\\Delta$", gap))


# 交付物里不该出现的定界符毛病：连续 $（`($$\Delta$$)` 这种叠起来的定界符）
_DOUBLE_DOLLAR_RE = re.compile(r"\${2,}")


def _tidy_math_spans(text):
    """单元格收尾：把叠起来的 `$` 收敛成一层。

    逐段包装偶尔会产出 `($$\\Delta$$)` 之类的叠层。它不会让渲染崩掉，
    但读起来像坏掉的源码，也会让体检的"$ 成对"判据失真。
    """
    return _DOUBLE_DOLLAR_RE.sub("$", text)


def _unicode_arrows_outside_math(text):
    """数学片段**之外**的裸 `→` 归一：方程式里的 → 还原成 =，转化关系里保留。

    这一遍必须在 _unicode_to_latex 之前跑：等号还原靠 `→` 的 Unicode 形态判断，
    而 `→` 一旦被包进 `$...$`，_latex_math_symbols 就会把它换成 \\to，
    再想"把它当等号"就晚了 —— 那样表格里会出现 `$A$ \\to $B$` 这种把
    转化关系写成箭头的方程式（实测产出过 CaCO3 → CaO + CO2↑ 的表格）。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])       # 数学片段原样保留
            i = j + 1
            continue
        j = text.find("$", i)
        if j == -1:
            j = n
        gap = text[i:j]
        if "→" in gap and _looks_like_equation(gap):
            gap = re.sub(r"(?<![\w\\⇌])→(?![\w])", "=", gap)
        out.append(gap)
        i = j
    return "".join(out)


def _escape_stray_lt(text):
    """把不属于任何已知标签的裸 `<` 转义成 &lt;（模型偶尔会写 "a < b"）。

    放在可逆符号处理之后、化学记号包装之前。
    """
    out, i = [], 0
    for m in _KNOWN_TAG_RE.finditer(text):
        out.append(text[i:m.start()].replace("<", "&lt;"))
        out.append(m.group(0))
        i = m.end()
    out.append(text[i:].replace("<", "&lt;"))
    return "".join(out)


def _collapse_spaces_outside_tags(text):
    """压缩标签外的连续空格（标签属性里的空格不能被碰到）。"""
    out, i = [], 0
    for m in _KNOWN_TAG_RE.finditer(text):
        out.append(re.sub(r"[ \t]{2,}", " ", text[i:m.start()]))
        out.append(m.group(0))
        i = m.end()
    out.append(re.sub(r"[ \t]{2,}", " ", text[i:]))
    return "".join(out)


def _wrap_bare_latex_commands(text):
    """把"不在 `$...$` 里"的裸 LaTeX 命令段包进数学片段。

    只认"包含反斜杠命令、且不含汉字"的连续段 —— 中文小标题（如「受热分解」）
    永远不会被拖进数学模式；整段已经"很化学"（含 +、= 或系数）时整段一起包，
    免得一条式子被切成 `$A$ + $B$` 那样零碎。
    """
    def gap_fn(gap):
        if "\\" not in gap or _HAN_RE.search(gap) or "$" in gap:
            return gap
        if _needs_chem(gap):
            return _wrap_pure_command_run(gap)
        return _CHEM_RUN_RE.sub(_wrap_command_run, gap)

    return _outside_math(text, gap_fn)


def _wrap_command_run(match):
    """把一个裸命令段（含它后面的化学尾巴）包成 `$...$`。

    尾巴之所以要一起收：`\\ce{...}` 换算出来的
    `2\\text{Na} + 2\\text{H}_2\\text{O} = ... + \\text{H}_2 ^+` 是一个整体，
    只包住前几个命令会让式子被切成几段、裸 `^` 还会被单独包成 `$^+$`（KaTeX 报错）。
    延长只在"后面确实是化学记号、且不含汉字"时发生；遇到 `$` 立刻停手，
    留在数学片段外面的部分原样返回（否则会把 `$...$` 咬进新片段里，产生 `$$`）。
    """
    raw = match.group(0)
    end = match.end()
    gap = match.string
    while True:
        k = end
        while k < len(gap) and gap[k] == " ":
            k += 1
        if k >= len(gap) or gap[k] in "$^_" or gap[k] not in _CHEM_CHARS:
            break
        chunk = re.match(r"\S+", gap[k:]).group(0)
        if not _needs_chem(chunk):
            break
        end = k + len(chunk)
        raw = gap[match.start():end]
    # 片段里混进了 $（说明这条 run 跨过了已有数学片段）：只处理 $ 之前的部分，
    # 其余原样返回 —— 否则会把已有片段咬进新片段，产出 `$$` 这种坏标记。
    cut = raw.find("$")
    if cut != -1:
        return _wrap_pure_command_run(raw[:cut]) + raw[cut:]
    return _wrap_pure_command_run(raw)


def _wrap_pure_command_run(raw):
    """确定不含 `$` 的裸命令段 → `$...$`（去掉 \\text{} 外壳与非法裸 ^ / _）。"""
    if "$" in raw:
        # 段里已经带着完整的数学片段（`($\Delta$)`）：再包一层就是 `($$\Delta$$)`，
        # 定界符会叠起来。这种段原样放过，交给 _tidy_math_spans 收尾。
        return raw
    body = _TEXT_GROUP_RE.sub(r"\1", raw.strip())
    body = _STRAY_CARET_RE.sub("", body).strip()
    if not body:
        return ""
    lead = raw[:len(raw) - len(raw.lstrip())]
    tail = raw[len(raw.rstrip()):]
    return lead + "$" + body + "$" + tail


def _cell_latex_chem(text):
    """单个单元格内容 → 行内 LaTeX 化学记号（表格内的唯一入口）。

    走的是与正文一模一样的流水线（_ce_to_latex → _unicode_to_latex →
    _normalize_equals → _latex_math_symbols → _space_out_commands），
    外加两步表格专属的收纳：裸 LaTeX 命令段整段包 `$...$`、`<sub>/<sup>` 折叠。
    这样"正文里对、表格里错"的方言漂移从结构上就不存在了。

    顺序不能换：实体必须在化学符号归一之前落成字符（`&Delta;` → Δ 才会被
    包进 `$...$` 变成 \\Delta），可逆符号必须在裸 `<` 转义之前还原成 ⇌。
    """
    text = _flatten_subsup(text)                     # 必须先做：标签切分前把 <sub> 折叠掉
    text = _table_reversible_to_arrow(text)          # <=> / <-> → ⇌（必须在转义前）
    text = (text.replace("&Delta;", "Δ").replace("&Delt;", "Δ")
                .replace("&delta;", "Δ").replace("&darr;", "↓")
                .replace("&uarr;", "↑").replace("&harr;", "⇌")
                .replace("&rarr;", "→").replace("&larr;", "←")
                .replace("&nbsp;", "\u00a0"))
    text = _escape_stray_lt(text)                    # 剩下的裸 `<` 才当文本转义
    text = _ce_to_latex(text)
    text = _unicode_arrows_outside_math(text)        # 必须在包 $...$ 之前（见函数说明）
    text = _unicode_to_latex(text)                   # 裸化学式 → $\text{Na}_2...$
    # \ce / \xlongequal 换算出的"半个式子"（裸 LaTeX 命令）收进数学片段；
    # 必须在 _unicode_to_latex 之后：它只处理已经是 $...$ 之外、含反斜杠的段，
    # 不会去动刚包好的公式，也不会把 `2\text{Na} + 2\text{H}_2\text{O}` 拆散。
    text = _wrap_bare_latex_commands(text)
    text = _wrap_delta_symbols(text)
    text = _merge_math_spans(text)
    text = _normalize_equals(text)
    text = _latex_math_symbols(text)
    text = _condition_delta_to_overset(text)
    text = _repair_math_annotations(text)
    text = _space_out_commands(text)
    text = _cell_wrap_math(text)
    text = _collapse_spaces_outside_tags(text)
    text = _tidy_math_spans(text)                    # 叠起来的 $ 收敛成一层
    # 里面落下来的不换行空格再变回实体（HTML 块里 U+00A0 会被当普通空格，实体才稳）
    return text.replace("\u00a0", "&nbsp;")


# 单元格"整段包一层 $...$"用得到的两条规则（见 _cell_wrap_math）
_TEXT_GROUP_RE = re.compile(r"\\text\{((?:[^{}]|\{[^{}]*\})*)\}")
_CELL_LATEX_HINT_RE = re.compile(r"\\[A-Za-z]+|[↑↓⇌→←·]|\$")
# 一段文本里"像化学记号/LaTeX 的一段"（不碰汉字与全角标点）
_CHEM_RUN_RE = re.compile(r"[A-Za-z0-9()\[\]{}\\^_=+\-*.,:; ↑↓]+")
# 进数学片段前必须清掉的非法裸记号（KaTeX: "Expected group after '^'"）
_STRAY_CARET_RE = re.compile(r"(?<![\\\w])\^(?!\s*[0-9{+\-])|(?<![\\\w])_(?!\s*[0-9{+\-])")
_HAN_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


def _cell_wrap_math(text):
    """兜底：单元格里还剩"裸 LaTeX"（从来就没被包进 `$...$`）时整段补一层。

    正常路径上 `_wrap_bare_latex_commands` 已经处理过了，这里只兜住少数漏网形状。
    """
    if not text.strip() or "$" in text or not _CELL_LATEX_HINT_RE.search(text):
        return text
    return "$" + _TEXT_GROUP_RE.sub(r"\1", text).strip() + "$"


def _lowercase_tags(html):
    """把已知 HTML 标签名统一成小写（<TD> → <td>），属性原样保留。"""
    def repl(m):
        name = m.group(2).lower()
        if name not in _HTML_TAGS:
            return m.group(0)
        return "<" + m.group(1) + name + (m.group(3) or "") + ">"

    return re.sub(r"<(/?)([A-Za-z][A-Za-z0-9]*)((?:\s[^<>]*?)?)/?>", repl, html)


def _balance_rows(html):
    """补齐行内单元格数不齐的表格：缺的补空 <td>，避免渲染时整列错位。

    带 colspan/rowspan 的表格结构本来就复杂，保守跳过不碰。
    """
    if re.search(r"\b(?:col|row)span\s*=", html, re.IGNORECASE):
        return html
    rows = list(re.finditer(r"<tr\b[^>]*>(.*?)</tr\s*>", html, re.DOTALL | re.IGNORECASE))
    if len(rows) < 2:
        return html
    counts = [len(re.findall(r"<t[dh]\b", m.group(1), re.IGNORECASE)) for m in rows]
    target = max(counts)
    if target == 0 or min(counts) == target:
        return html

    out, last = [], 0
    for m, cnt in zip(rows, counts):
        out.append(html[last:m.end(1)])
        out.append("<td></td>" * (target - cnt))
        out.append(html[m.end(1):m.end()])
        last = m.end()
    out.append(html[last:])
    return "".join(out)


def _wrap_loose_cells(html):
    """只有 <td> 没有 <tr> 的表格：补一层 <tr>，否则浏览器会把它排成散装单元格。"""
    if re.search(r"<tr\b", html, re.IGNORECASE) or not re.search(r"<t[dh]\b", html, re.IGNORECASE):
        return html
    m = re.match(r"(?is)\s*(<table\b[^>]*>)(.*)(</table\s*>)\s*$", html)
    if not m:
        return html
    return m.group(1) + "<tr>" + m.group(2) + "</tr>" + m.group(3)


def _strip_blanks_inside(html):
    """去掉表格内部的空行。

    CommonMark 里 HTML 块遇到空行就结束 —— 表格中间留空行，后半张表会掉出去
    被当成普通段落重新解析。这是"表格渲染崩掉"最常见的原因之一。
    """
    html = re.sub(r"\n[ \t]*\n+", "\n", html)
    html = re.sub(r"[ \t]+\n", "\n", html)
    return html.strip()


def _html_table(block):
    """HTML 表格：标签小写化 → <sub>/<sup> 折叠 → 单元格化学记号 LaTeX 化 → 补行 → 去空行。"""
    html = _lowercase_tags(block.strip())
    # <sub>/<sup> 必须在"按标签切分"之前折叠掉：切分后 <sub>2</sub> 会被拆成
    # 三个片段，配对信息就丢了，单元格里会留下 HTML 上下标与 Unicode 混杂的半成品。
    html = _flatten_subsup(html)
    # 逐段处理"标签之间"的文本：标签本身（<tr>/<td>/<br>…）原样保留，
    # 只有单元格内容进化学记号流水线。切分必须只认"标签样"的尖括号
    # （`<` + 可选 `/` + 字母开头），否则 `<->` / `<=>` 会被当成标签整段跳过。
    pieces = re.split(r"(</?[A-Za-z][^<>]*>)", html)
    for k in range(0, len(pieces), 2):        # 偶数下标是标签之间的文本
        pieces[k] = _cell_latex_chem(pieces[k])
    html = "".join(pieces)
    html = _wrap_loose_cells(html)
    html = _balance_rows(html)
    return _strip_blanks_inside(html)


# --- 5b. Markdown 管道表 → HTML 表格 ---------------------------------------
def _split_pipe_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return re.split(r"(?<!\\)\|", s)


def _is_pipe_separator(line):
    """`| --- | :--: |` 这种对齐行 —— 它是"这确实是张表"的唯一可靠证据。"""
    if "|" not in line:
        return False
    cells = [c.strip() for c in _split_pipe_row(line)]
    filled = [c for c in cells if c]
    if not filled:
        return False
    return all(re.fullmatch(r":?-{1,}:?", c) for c in filled)


def _pipe_table_to_html(header, rows):
    def cells(values, tag):
        return "".join(f"<{tag}>{_cell_latex_chem(v.strip())}</{tag}>" for v in values)

    head = "<tr>" + cells(_split_pipe_row(header), "th") + "</tr>"
    body = "".join("<tr>" + cells(_split_pipe_row(r), "td") + "</tr>" for r in rows)
    html = f"<table><thead>{head}</thead><tbody>{body}</tbody></table>"
    return _balance_rows(html)


# --- 5c. 表格抽取与占位保护 ------------------------------------------------
# 占位符只用 \x00 + 数字 + \x00：不含字母，不会被"裸化学式"识别成 Na、CO2，
# 也不会被等号/箭头规则碰到，可以安全穿过后面所有工具函数。
def _placeholder(idx):
    return "\x00" + str(idx) + "\x00"


_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")


def _extract_tables(text):
    """把表格抽出来单独处理，返回 (带占位符的正文, [表格 HTML, ...])。"""
    tables = []

    def repl(m):
        tables.append(_html_table(m.group(0)))
        return "\n\n" + _placeholder(len(tables) - 1) + "\n\n"

    text = _TABLE_RE.sub(repl, text)

    # 管道表：模型没听"表格用 HTML"时的兜底，就地升级成真正的 HTML 表格
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if ("|" in lines[i] and i + 1 < len(lines) and _is_pipe_separator(lines[i + 1])):
            header, rows, j = lines[i], [], i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(lines[j])
                j += 1
            tables.append(_pipe_table_to_html(header, rows))
            out.append("")
            out.append(_placeholder(len(tables) - 1))
            out.append("")
            i = j
            continue
        out.append(lines[i])
        i += 1
    text = "\n".join(out)

    return text, tables


def _restore_tables(text, tables):
    """把处理好的表格放回去（表格块前后留空行，Markdown 才会当成 HTML 块）。"""
    def repl(m):
        idx = int(m.group(1))
        return "\n\n" + tables[idx] + "\n\n" if 0 <= idx < len(tables) else ""

    text = _PLACEHOLDER_RE.sub(repl, text)
    return text.replace("\x00", "")          # 万一有漏网的占位符，绝不让它进交付物


# ---------------------------------------------------------------------------
# 6. 等号 / 可逆符号 / 箭头
# ---------------------------------------------------------------------------
def _normalize_equals(text):
    """化学方程式的符号归一（表格外的正文部分）。

    * `===` → `=`；ASCII 箭头 `->` / `-->` 在化学语境里还原成 `=`（它本来就是等号），
      在非化学语境里统一成 Unicode `→`；
    * 可逆反应 `<=>` / `<->` / `⇋` → `⇌`；
    * Unicode `→` 只在"真正的箭头"里保留：两侧是"式子 + 加号"的方程式结构时，
      它是被写坏的等号（2Na + 2H2O → 2NaOH + H2↑）；纯转化关系（Na → NaOH）保留箭头。
    """
    text = _REVERSIBLE_RE.sub("⇌", text)
    text = text.replace("⇋", "⇌")
    text = re.sub(r"={2,}", "=", text)

    lines = []
    for line in text.split("\n"):
        chem = _looks_chemical(line)
        if chem:
            line = re.sub(r"(?<!\\)-{1,2}>", "=", line)
            if "→" in line and _looks_like_equation(line):
                line = re.sub(r"(?<![\w\\⇌])→(?![\w])", "=", line)
        else:
            line = re.sub(r"(?<!\\)-{1,2}>", "→", line)
        lines.append(line)
    return "\n".join(lines)


# --- 6b. $...$ 里的 Unicode 符号 → LaTeX 命令 -------------------------------
# KaTeX 对 Unicode 数学符号的支持时好时坏（还取决于版本与字体），而
# \uparrow / \rightleftharpoons 是"到处都能渲染"的写法。所以数学片段内部统一换成命令；
# 片段外的 Unicode 符号保持不动（那是正文排版，交给 HTML 引擎）。
_MATH_SYMBOLS = {
    "⇌": r"\rightleftharpoons", "↑": r"\uparrow", "↓": r"\downarrow",
    "Δ": r"\Delta", "△": r"\Delta", "·": r"\cdot", "×": r"\times", "÷": r"\div",
    "→": r"\to", "←": r"\leftarrow", "−": "-",
}

# 数学片段里的 ASCII 箭头：`->` / `-->` 是被写坏的等号，`<-` / `<--` 是向左的真箭头。
# 捕获组最后一位就是方向符；前面用 (?<!\\) 挡住 `\rightarrow` 这类命令名里的 `-`。
_ASCII_ARROW_RE = re.compile(r"(?<!\\)(?<!-)(?:-+>|<+-)")

# 自己会往正文里"造"的无参 LaTeX 命令。造完必须保证命令名后面不是字母/数字：
# LaTeX 的命令名吃字母，`\rightleftharpoonsNaHCO_3` 会被读成一个叫
# rightleftharpoonsNaHCO 的命令 → 未定义命令 → 渲染报错（实测产物里出现过）。
_SPACED_COMMANDS = (
    "rightleftharpoons", "leftrightharpoons", "uparrow", "downarrow",
    "rightarrow", "leftarrow", "longrightarrow", "longleftarrow",
    "Delta", "cdot", "times", "div", "to", "pm", "mp", "approx", "neq",
    "leq", "geq", "circ", "ldots", "dots", "quad", "qquad",
)
_SPACED_COMMANDS_RE = re.compile(
    r"\\(?:" + "|".join(_SPACED_COMMANDS) + r")(?=[0-9A-Za-z])")


def _space_out_commands(text):
    """给紧贴着字母/数字的命令补一个空格（`\\rightleftharpoonsNa` → `\\rightleftharpoons Na`）。"""
    return _SPACED_COMMANDS_RE.sub(lambda m: m.group(0) + " ", text)


def _math_body_symbols(body):
    """数学片段内部的符号替换：\\text{...} 之类的文本组整段跳过。

    不能无脑替换：`\\text{H}_2\\text{↑}` 里的 ↑ 在文本模式里直接把 `\\uparrow`
    塞进去会变成 `\\text{\\uparrow}` —— LaTeX 数学模式命令进了文本模式，KaTeX 直接报错。
    """
    # ASCII 箭头先处理掉：`->` 本来就是被写坏的等号，`<-` 才是真箭头。
    # 必须排在下面的逐字符循环之前，否则 `-` 会被原样留在数学片段里。
    body = _ASCII_ARROW_RE.sub(lambda m: "=" if m.group(1) == ">" else "←", body)
    out, i, n = [], 0, len(body)
    while i < n:
        if body[i] == "\\":
            m = _GROUP_CMD_RE.match(body, i)
            if m:
                k = m.end()
                while k < n and body[k] == " ":
                    k += 1
                if k < n and body[k] == "{":
                    end = _balanced_group_end(body, k)
                    if end is not None:
                        out.append(body[i:end])          # 文本组原样保留
                        i = end
                        continue
                out.append(body[i:m.end()])
                i = m.end()
                continue
            j = _latex_span_end(body, i)                 # 其它命令整段保留
            out.append(body[i:j])
            i = max(j, i + 1)
            continue
        ch = body[i]
        cmd = _MATH_SYMBOLS.get(ch)
        out.append(cmd + " " if cmd else ch)
        i += 1
    return "".join(out)


def _latex_math_symbols(text):
    """把 `$...$` 数学片段里的 Unicode 化学符号换成 LaTeX 命令。

    必须排在 _normalize_equals 之后：等号还原靠 `→` 的 Unicode 形态来判断，
    提前换成 \\to 会让"方程式里的 → 应当作等号"这条规则失效。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "$":
            out.append(text[i])
            i += 1
            continue
        j = text.find("$", i + 1)
        if j == -1:
            out.append(text[i:])
            break
        body = _math_body_symbols(text[i + 1:j])
        out.append("$" + re.sub(r"[ \t]{2,}", " ", body) + "$")
        i = j + 1
    return "".join(out)


# --- 6c. 旧写法 `(Δ) =` → 项目统一的 `\overset{Δ}{=}`；中文注释从式子里提出来 ------
# 只认"括号里只有一个加热符号"的写法：`(Δ)` / `(\Delta)` / `(\triangle)` 后面紧跟 `=`。
# 不碰 `(\text{MnO}_2, \Delta) =` 这类多条件，也不碰 `(s)/(l)/(g)/(aq)` 这类物质状态
# 符号（它们紧贴物质、不会出现在 = 前面）。
_COND_DELTA_EQUAL_RE = re.compile(r"\((?:\\(?:Delta|triangle)|Δ|△)\s*\)[ \t]*=")


def _condition_delta_to_overset(text):
    """数学片段内 `(Δ) =` / `(\\Delta) =` → `\\overset{\\Delta}{=}`。

    加热是"等号上方的条件"，课本排版就是 `\\overset{\\Delta}{=}`；旧产物里写在等号左侧
    括号里（`(\\Delta) =`）的写法在这里统一升级成等号上方写法。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "$":
            out.append(text[i])
            i += 1
            continue
        j = text.find("$", i + 1)
        if j == -1:
            out.append(text[i:])
            break
        body = _COND_DELTA_EQUAL_RE.sub(lambda m: r"\overset{\Delta}{=}", text[i + 1:j])
        out.append("$" + body + "$")
        i = j + 1
    return "".join(out)


# 中文注释（白色 / 淡黄色 / 无色……）的正文字符范围。KaTeX 数学模式不吃裸中文，
# 所以注释被提到式子外面后必须重新包一层 \text{}。
# 只含 CJK 统一表意文字与 CJK 标点；不含全角括号（\uff08/\uff09），免得注释内容
# 把右括号吞进去、配对判断跑偏。
_CJK_RE = r"\u3400-\u9fff\u3000-\u303f"
_ANNOT_ATOM_RE = r"(?:\\text\{[^{}]*\}|[" + _CJK_RE + r"]+?)"
# `\text{O(白色)}`：注释被吞进了 \text{} 里 → `\text{O}(\text{白色})`
_TEXT_ANNOT_RE = re.compile(
    r"\\text\{([^{}]*?)[（(](" + _ANNOT_ATOM_RE + r")[)）]\}")
# `\text{O}_{2(\text{淡黄色})}`：注释被吞进了下标里 → `\text{O}_{2}(\text{淡黄色})`
_SCRIPT_ANNOT_RE = re.compile(
    r"([_^])\{([0-9+\-]*)[（(](" + _ANNOT_ATOM_RE + r")[)）]\}")
# 数学片段里裸的中文注释 `(淡黄色)` / `（淡黄色）` → `(\text{淡黄色})`
_BARE_ANNOT_RE = re.compile(r"[（(]([" + _CJK_RE + r"]+)[)）]")


def _ann_wrap(ann):
    """注释内容 → 能在数学模式里渲染的 \\text{...}（已经是 \\text{} 就不重复包）。"""
    ann = (ann or "").strip()
    if not ann:
        return ""
    return ann if ann.startswith("\\text") else "\\text{" + ann + "}"


def _repair_math_annotations(text):
    """把错塞进 \\text{} / 上下标里的中文注释提出来，挂到式子后面。

    模型常把 "Na₂O₂（淡黄色）" 写成 `\\text{Na}_2\\text{O}_{2(\\text{淡黄色})}`（注释
    被吞进下标），或把 "2Na₂O（白色）" 写成 `2\\text{Na}_2\\text{O(白色)}`（注释被吞进
    \\text{}）。这里统一修成 `\\text{...}(\\text{中文})`，中文必须进 \\text{}。
    """
    def text_repl(m):
        prefix = m.group(1)
        ann = _ann_wrap(m.group(2))
        return "(" + ann + ")" if not prefix else "\\text{" + prefix + "}(" + ann + ")"

    def script_repl(m):
        return m.group(1) + "{" + m.group(2) + "}(" + _ann_wrap(m.group(3)) + ")"

    def bare_repl(m):
        return "(\\text{" + m.group(1) + "})"

    def sub_bare(body):
        """对 body 里"不在 \\text{...} 组内"的裸片段应用 _BARE_ANNOT_RE。

        \\text{XXX（中文）YYY} 里的中文本来就能被 KaTeX 的文本模式渲染，绝不能去动它；
        只有数学模式里真正裸露的中文注释才需要包 \\text{}。
        """
        out, i, n = [], 0, len(body)
        while i < n:
            if body[i] == "\\":
                m = _GROUP_CMD_RE.match(body, i)
                if m:
                    k = m.end()
                    while k < n and body[k] == " ":
                        k += 1
                    if k < n and body[k] == "{":
                        end = _balanced_group_end(body, k)
                        if end is not None:
                            out.append(body[i:end])       # \text{...} 整段原样保留
                            i = end
                            continue
                j = _latex_span_end(body, i)             # 其它 LaTeX 命令整段保留
                out.append(body[i:j])
                i = max(j, i + 1)
                continue
            j = body.find("\\", i)
            if j == -1:
                j = n
            out.append(_BARE_ANNOT_RE.sub(bare_repl, body[i:j]))
            i = j
        return "".join(out)

    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "$":
            out.append(text[i])
            i += 1
            continue
        j = text.find("$", i + 1)
        if j == -1:
            out.append(text[i:])
            break
        body = text[i + 1:j]
        prev = None
        while prev != body:                     # 规则反复跑，直到稳定（幂等）
            prev = body
            body = _SCRIPT_ANNOT_RE.sub(script_repl, body)
            body = _TEXT_ANNOT_RE.sub(text_repl, body)
            body = sub_bare(body)
        out.append("$" + body + "$")
        i = j + 1
    return "".join(out)


# ---------------------------------------------------------------------------
# 7. 收尾：空行压缩、去除行尾空白、合并连续分隔线
# ---------------------------------------------------------------------------
def _tidy(text):
    text = re.sub(r"[ \t]+\n", "\n", text)              # 行尾空白
    text = re.sub(r"(?m)^---\s*\n(?:---\s*\n)+", "---\n", text)  # 连续水平线 → 单条
    text = re.sub(r"\n{3,}", "\n\n", text)              # 3+ 空行 → 1 空行
    return text.strip() + "\n"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def normalize_handout_markdown(text):
    """对单页（或整份）Markdown 讲义执行全部归一化。"""
    text = _strip_code_fences(text)
    text = _strip_html_comments(text)
    text = _normalize_entities(text)
    text = _strip_page_headings(text)
    text = _strip_assistant_remarks(text)
    text = _collapse_align(text)
    text = _display_to_inline(text)
    text = _paren_math_to_inline(text)
    text = _normalize_condition_equals(text)

    # 表格先抽出来（占位保护），单元格内容走 _cell_latex_chem —— 与下面正文的
    # 五步 LaTeX 流水线是同一套函数，所以表格不会成为"另一种方言"的孤岛。
    text, tables = _extract_tables(text)
    text = _ce_to_latex(text)
    text = _unicode_to_latex(text)
    text = _merge_math_spans(text)
    text = _normalize_equals(text)
    text = _latex_math_symbols(text)
    text = _condition_delta_to_overset(text)
    text = _repair_math_annotations(text)
    text = _space_out_commands(text)
    text = _restore_tables(text, tables)

    return _tidy(text)


# ---------------------------------------------------------------------------
# 8. 表格外的 LaTeX 方言
# ---------------------------------------------------------------------------
def _ce_word_to_latex(word):
    """把 \\ce{...} 内部的单个"词"转成普通 LaTeX。

    关键点：mhchem 里的 -> 在化学方程式中应还原为等号 =。
    """
    if word in ("->", "→", "-->", "=", "==>", "=>"):
        return "="
    if word in ("<=>", "⇌", "<->"):
        return r"\rightleftharpoons"
    return _formula_to_latex(word)


# mhchem 里的上/下箭头简写：`^` 是气体（↑）、`v` 是沉淀（↓）。
# 实测 GLM 会写 `\ce{2Na + 2H2O -> 2NaOH + H2 ^}` —— 那个裸露的 ^ 到了 LaTeX 里
# 会让 KaTeX 直接报"Expected group after '^'"，所以必须在换算前补齐它的语义。
_CE_CARET_RE = re.compile(r"(?<![\^\\])\^(?!\s*(\{[^{}]*\}|[0-9+\-]))")
_CE_VEE_RE = re.compile(r"(?<![\w\\])v(?![\w])")


def _normalize_ce_body(content):
    """\\ce{} 内部先做 mhchem 简写归一（^ → ↑、v → ↓），再逐词换算。"""
    content = _CE_CARET_RE.sub("^+ ", content)
    content = _CE_VEE_RE.sub("↓", content)
    return content


def _ce_to_latex(text):
    def repl(match):
        content = _normalize_ce_body(match.group(1))
        words = content.split(" ")
        return " ".join(_ce_word_to_latex(w) for w in words if w != "")

    return re.sub(r"\\ce\{([^{}]*)\}", repl, text)


def _latex_span_end(text, i):
    """从 text[i] == '\\\\' 开始，返回整个 LaTeX 片段结束后的下标。

    例：\\text{Fe}^{3+} 算一个片段；\\xrightarrow[\\Delta]{\\text{MnO}_2} 也算一个。
    片段内部原样保留，绝不再做化学式包装。
    """
    m = re.match(r"\\(?:[A-Za-z]+|.)", text[i:], re.DOTALL)
    if not m:
        return i + 1
    j = i + m.end()
    while j < len(text):
        if text[j] in "{[":
            end = _balanced_group_end(text, j)
            if end is None:
                return j            # 括号不闭合：保守收手，不吞掉后面的内容
            j = end
            continue
        if text[j] in "_^":
            k = j + 1
            if k < len(text) and text[k] == "{":
                end = _balanced_group_end(text, k)
                if end is None:
                    return j
                j = end
            elif k < len(text):
                j = k + 1           # 单字符上下标，如 _2
            else:
                return j
            continue
        break
    return j


def _unicode_to_latex(text):
    """把含 Unicode 上下标的化学式与带系数的裸化学式包装成行内 $...$。

    已有的 $...$ 数学片段会被原样保留，绝不在其内部再做替换，
    避免把 LaTeX 的 _ 下标误当成 Unicode 上下标而嵌套 $；
    同理，`\\cmd{...}` 形式的 LaTeX 片段也整段保护（见 _latex_span_end）。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "$":
            j = text.find("$", i + 1)
            if j == -1:
                out.append(text[i:])
                break
            out.append(text[i:j + 1])   # 数学片段原样保留
            i = j + 1
            continue

        c = text[i]
        if c == "\\":
            j = _latex_span_end(text, i)
            out.append(text[i:j])       # LaTeX 片段原样保留
            i = j
            continue
        if c in _CHEM_CHARS:
            j = i
            while j < n and text[j] in _CHEM_CHARS and text[j] != "$":
                j += 1
            token = text[i:j]
            out.append("$" + _formula_to_latex(token) + "$" if _needs_chem(token) else token)
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _merge_math_spans(text):
    """把被空格/等号/条件等号切开的相邻数学片段缝合回一个 $...$。

    逐 token 包装时，`CaCO3 = CaO + CO2↑` 会变成三个独立片段
    `$...$ = $...$ + $...$`；渲染出来虽然对，但源码读起来像断裂的式子，
    再被别的工具二次处理也容易出错。这里只缝合"中间只隔一个 = / + / ⇌ / → / \\to"
    的情况（分隔符两侧都已经是数学片段，所以不会把中文说明缝进去）。

    条件等号（`\\overset{..}{=}` 之类）被单独包成 `$\\overset{..}{=}$` 时也要缝回：
    `$A$ $\\overset{\\Delta}{=}$ $B$` → `$A \\overset{\\Delta}{=} B$`。
    """
    pattern = re.compile(
        r"\$([^$\n]+)\$(\s*(?:\([^()\n]{0,40}\)\s*)?"
        r"(?:[=+]|⇌|→|\\to|\\rightleftharpoons|\\xrightarrow)\s*)\$([^$\n]+)\$")
    cond_eq = re.compile(
        r"\$([^$\n]+)\$[ \t]*\$(\\(?:overset|underset|stackrel)[^$\n]*)\$[ \t]*\$([^$\n]+)\$")
    prev = None
    while prev != text:
        prev = text
        text = pattern.sub(r"$\1\2\3$", text)
        text = cond_eq.sub(r"$\1 \2 \3$", text)
    return text


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    src = argv[0]
    dst = argv[1] if len(argv) > 1 else src
    with open(src, "r", encoding="utf-8") as f:
        raw = f.read()
    cleaned = normalize_handout_markdown(raw)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(cleaned)
    print(f"已归一化：{src} -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
