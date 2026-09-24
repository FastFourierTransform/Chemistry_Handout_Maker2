# Handout-Maker2

把**化学课件 PDF（幻灯片）**转换成**结构化 Markdown 讲义**的小工具。

核心思路：不做 OCR + 版面分析的传统流水线，而是**把每一页 PDF 渲染成图片，直接交给视觉大模型"看图写讲义"**，再用一层确定性的后处理把化学式、方程式、表格的格式钉死。全程不需要 GPU、不需要本地权重、不需要代理。

```
PDF ──PyMuPDF 渲染──▶ 每页 JPEG ──智谱 GLM 视觉模型──▶ Markdown 正文
                    ──handout_normalize 归一化──▶ 一份连续的化学讲义 Markdown
```

工程一共 5 个 Python 文件：

| 文件 | 职责 |
| --- | --- |
| `pdf_to_chemistry_handout_glm.py` | 主脚本：渲染页面、调用 GLM、按批组织、增量落盘、连通性自检 |
| `batch_convert.py` | **批量入口**：一批 PDF / 一整个目录依次转换（串行、断点续传、逐份体检、JSON 报表） |
| `handout_normalize.py` | 确定性后处理：表格 HTML 化 + 单元格 LaTeX 化、化学记号归一、等号/可逆/箭头、分页标题清除 |
| `selfcheck_glm_offline.py` | 离线自检：假传输层跑通全链路（不联网、不花额度） |
| `verify_handout.py` | 交付物体检：拿真实产出的 .md 核对表格渲染与化学记号写法 |

---

## 目录

- [1. 快速开始](#1-快速开始)
- [2. 输出契约（本工程的核心约定）](#2-输出契约本工程的核心约定)
- [3. 系统工作流](#3-系统工作流)
- [4. 实现细节](#4-实现细节)
  - [4.1 为什么用标准库 urllib 而不是 openai SDK](#41-为什么用标准库-urllib-而不是-openai-sdk)
  - [4.2 提示词：把格式约定写死在 14 条规则里](#42-提示词把格式约定写死在-14-条规则里)
  - [4.3 handout_normalize：表格内外同一条流水线](#43-handout_normalize表格内外同一条流水线)
  - [4.4 电荷消歧：Fe3+ 是电荷、H2O 是下标](#44-电荷消歧fe3-是电荷h2o-是下标)
  - [4.5 表格结构为什么这么难：三个必踩的坑](#45-表格结构为什么这么难三个必踩的坑)
  - [4.6 GLM 实测的三个坑](#46-glm-实测的三个坑)
  - [4.7 错误分类：重试 / 换模型 / 直接失败](#47-错误分类重试--换模型--直接失败)
  - [4.8 增量落盘与资源清理](#48-增量落盘与资源清理)
- [5. 命令行参数](#5-命令行参数)
  - [5.1 单文件入口](#51-单文件入口)
  - [5.2 批量入口 batch_convert.py](#52-批量入口-batch_convertpy)
- [6. 输出格式说明](#6-输出格式说明)
- [7. 测试与验证](#7-测试与验证)
- [8. 已知限制](#8-已知限制)
- [9. 目录结构](#9-目录结构)
- [10. 故障排查](#10-故障排查)
- [附录 A：常量与环境变量速查](#附录-a常量与环境变量速查)

---

## 1. 快速开始

```bash
conda activate Handout-Maker2          # Python 3.12，装着 pymupdf + Pillow

# ① 先确认 Key / 网络 / 看图能力都通（发一张现场画的小图，几秒出结果）
python pdf_to_chemistry_handout_glm.py --check

# ② 转整份 PDF（输出默认 <输入名>_handout_glm.md）
python pdf_to_chemistry_handout_glm.py test1.pdf

# ③ 只转前 3 页试参数
python pdf_to_chemistry_handout_glm.py 输入.pdf --pages 1-3

# ④ 批量：一整个目录 / 一批 PDF，可断点续传并逐份体检
python batch_convert.py 课件目录 -r -o 讲义 --skip-existing --verify
python batch_convert.py 课件目录 --dry-run       # 先看看会转哪些、输出到哪（不花额度）

# ⑤ 交付前体检：表格渲染 / 化学式方言 / 符号，顺便出一份 HTML 预览
python verify_handout.py test1_handout_glm.md --html preview.html

# ⑥ 不联网的回归自检（假传输层，改代码后必跑）
python selfcheck_glm_offline.py test1.pdf
```

API Key 读取顺序：`--api-key` > 环境变量 `GLM_API_KEY` / `ZHIPUAI_API_KEY` /
`BIGMODEL_API_KEY` / `ZHIPU_API_KEY` > 脚本内兜底值。

依赖只有三个：`pymupdf`（PDF 渲染）、`Pillow`（图像处理）、`markdown-it-py`（仅
`verify_handout.py` 的渲染校验用，缺了会自动跳过那一步）。

---

## 2. 输出契约（本工程的核心约定）

**表格用 HTML 搭结构，表格内外的化学记号都用行内 LaTeX。**

| 位置 | 写法 | 例子 |
| --- | --- | --- |
| 表格结构 | 原生 HTML 标签 | `<table><tr><th>…</th><td>…</td></tr></table>` |
| 表格外（正文、题目、方程式） | 行内 LaTeX，`$...$` 包裹 | `$2\text{Na} + 2\text{H}_2\text{O} = 2\text{NaOH} + \text{H}_2\uparrow$` |
| 表格内（HTML 表格单元格） | **同一套行内 LaTeX** | `<td>$\text{Na}_2\text{CO}_3$</td>` |

表格的硬规则：

- 表格**结构**只用 HTML 标签；**化学记号**一律 `$...$`，不许出现裸的
  `Na2CO3`、也不许用 `<sub>/<sup>` 冒充上下标（模型写出来也会被折叠回 LaTeX）
- 单元格内换行用 `<br>`；每行 `<td>` 个数与表头 `<th>` 一致
- 单元格末尾的 `&nbsp;` 仍然保留（缩进排版用）

符号规则（表格内外都适用）：

- 化学方程式一律用等号 `=`，**绝不用 `->` / `→` 代替等号**
- 可逆反应用 `\rightleftharpoons`（也可直接写 `⇌`）
- `→` 只留给**真正的箭头**：转化关系、合成路线、流程图
- 反应条件压在等号/箭头的上方与下方（KaTeX 原生支持 `\overset` / `\underset`）：
  - 只有上方条件（如加热）：`\overset{\Delta}{=}`
  - 上下都有条件（如催化剂 + 加热）：`\overset{\text{MnO}_2}{\underset{\Delta}{=}}`
  - 箭头形式的条件：`\xrightarrow[\Delta]{\text{MnO}_2}`

交付形态：**一份连续的讲义**，没有「第 N 页」这类分页标题，也没有 `---` 分隔线。
页码只是流水线的中间量（批大小、页序对齐用），不是给读者看的东西。

**为什么表格结构仍归 HTML、记号却归 LaTeX？** `<table>` 是 Markdown 里唯一能保证
"排成表"的写法（管道表各渲染器解释不一，模型也常常把列数写歪）；而化学记号只有
一套写法才好维护 —— 上下标、可逆箭头、加热条件、电荷全交给 KaTeX/MathJax 排，
正文与表格不会出现"同一种物质两种长相"。

**代价与前提：交付物现在要求渲染环境挂 KaTeX/MathJax**（以前表格里的化学式靠浏览器
原生 `<sub>` 就能看）。本工程的预览页自带 CDN（`verify_handout.py --html`），
Obsidian / VS Code 预览 / 教学平台一般也都带公式渲染；纯 HTML 导出时记得带上。

---

## 3. 系统工作流

```
① 渲染       PyMuPDF 按 DPI 把每页 PDF 画成 JPEG
             （150 DPI 下实测 1334×750、46~140KB，远低于平台 5MB 上限）
                    │
② 分批       默认一页一请求（页序最干净、重试粒度最细）
                    │
③ 调用       urllib POST /chat/completions（流式 SSE）
             失败按 重试 → 换模型 → 报错 三级处理
                    │
④ 切页       多页批里按 `<!-- page: N -->` 切回单页
             ⚠ 必须在下一条之前做（见 4.3 的 `-->` 陷阱）
                    │
⑤ 归一化     handout_normalize：
             表格（HTML 结构）与正文走**同一条** LaTeX 化学记号流水线
                    │
⑥ 落盘       每批解析完立刻写入 + flush（中途崩溃也留得下可用的一半）
```

第 ④ 与 ⑤ 的顺序是硬约束：归一化会把 `->` 还原成等号，而页序标记
`<!-- page: 2 -->` 里的 `-->` 正好中招 —— 先归一化再切页，标记会变成
`<!-- page: 2 =`，切页当场失效。（现在归一化会先删 HTML 注释，算双保险，
但顺序依然不能反。）

---

## 4. 实现细节

### 4.1 为什么用标准库 urllib 而不是 openai SDK

`verify_GLM-Flash_API.ipynb` 提供的有效信息只有三样：网关地址、Bearer Key、
`messages` 的形状 —— 这三样在主脚本里原样保留（`DEFAULT_BASE_URL` /
`resolve_api_key` / `build_payload`）。

但本机的两个 conda 环境各缺一半（实测）：

```
conda activate Handout-Maker2   # Python 3.12：有 pymupdf/Pillow，缺 openai
conda activate ai-assistant     # Python 3.11：有 openai，缺 pymupdf/Pillow
```

智谱网关本身是 OpenAI 兼容的 REST 接口，请求体与 `client.chat.completions.create(...)`
一一对应，用 `urllib` 直接 POST 即可 —— 主脚本因此在 Handout-Maker2 环境里开箱即用。
想用 SDK 就把 base_url 指向同一地址，wire 格式完全一致。

### 4.2 提示词：把格式约定写死在 14 条规则里

提示词（`PROMPT`）与后处理是"双保险"关系：提示词负责让模型**一次就写对**，
后处理负责**把没写对的兜回来**。14 条规则分四组：

| 组 | 规则 | 要点 |
| --- | --- | --- |
| 总则 | 1–2 | 提取全部文字；只输出正文，不要解释/围栏/页码 |
| 表格结构 | 3–4 | 必须原生 HTML；`<td>` 数与 `<th>` 一致；缺的补空单元格；不要在单元格里嵌套表格 |
| 化学记号 | 5–10 | **表格内外都用 `$...$` + `\text{}`**（表格里也不许写 `Na2CO3` 或 `<sub>`）；等号就是 `=`；可逆用 `\rightleftharpoons`；条件写括号；`→` 只给转化关系 |
| 图示排版 | 11–13 | 图表用 `>[图示: ...]`；标题分级；不编造内容，看不清用 □ |

批量模式（`--pages-per-request > 1`）会追加第 14 条：按图片顺序逐页转写，
每页前输出一行 `<!-- page: N -->`。这些标记只用于切页，切完就丢掉。

### 4.3 handout_normalize：表格内外同一条流水线

`handout_normalize.py` 的顺序是经过实测打磨的，不能随便调换：

```python
1  _strip_code_fences       剥 ```markdown / ```latex 围栏
2  _strip_html_comments     删 HTML 注释（关键：`-->` 会被箭头规则命中）
2b _normalize_entities      HTML 实体归一：&Delt; → &Delta;、&uarr; → ↑、丢掉零宽空格
3  _strip_page_headings     删「第 N 页」分页标题与页序标记行
4  _strip_assistant_remarks 删"希望这份讲义对您有帮助"这类收尾语
5  _collapse_align / _display_to_inline / _paren_math_to_inline
                            \[...\]、\begin{align*}、\(...\) → 行内 $...$
5b _normalize_condition_equals
                            \xlongequal{\Delta} → \overset{\Delta}{=}（KaTeX 不认
                            \xlongequal，认 \overset/\underset/\stackrel）
                            \overset{\Delta}{=} / \stackrel{..}{=} 原样保留
6  _extract_tables          表格抽成占位符（\x00N\x00），单元格走 _cell_latex_chem
7  _ce_to_latex             \ce{...} → \text{...}（mhchem 的 ^ / v 也一并归一）
8  _unicode_to_latex        H₂O → $\text{H}_2\text{O}$（$...$ 与 \cmd{} 片段整段保护）
8b _wrap_bare_latex_commands \ce / \xlongequal 换算出的裸命令段整段收进 $...$
8c _wrap_delta_symbols      孤立的 Δ → $\Delta$
9  _merge_math_spans        把被空格切开的相邻 $...$ 缝合：$a$ = $b$ → $a = b$
10 _normalize_equals        === → =，-> → =，可逆 → ⇌，真箭头保留
10b _latex_math_symbols     $...$ 里的 Unicode 符号 → \rightleftharpoons / \uparrow / \to
10c _condition_delta_to_overset  旧写法 (\Delta) = → \overset{\Delta}{=}（只认单个加热符号）
10d _repair_math_annotations     被吞进 \text{}/下标的（白色/淡黄色）注释提到式子后并包 \text{}
10e _space_out_commands     给紧贴字母的命令补空格：\rightleftharpoonsNa → \rightleftharpoons Na
11 _restore_tables          表格放回（前后补空行，Markdown 才当 HTML 块）
12 _tidy                    空行压缩、行尾空白、连续分隔线合并
```

**单元格的唯一入口是 `_cell_latex_chem()`**，它跑的就是上面这条正文流水线，
只在两头加表格专属的两步：

1. 进流水线之前：`H<sub>2</sub>O` → `H₂O`（`_flatten_subsup()`）、
   `<=>` → `⇌`（`_table_reversible_to_arrow()`，必须在裸 `<` 转义之前）、
   `&Delta;` → `Δ`、裸 `<` 转义；
2. 出流水线之后：把裸 LaTeX 命令段整段收进 `$...$`、叠起来的 `$` 收敛成一层。

化学记号的处理仍然是"**一次解析、多种渲染**"：`_parse_chem_token()` 把
`H2O` / `Fe3+` / `\text{Na}_2` / `SO₄²⁻` 统一解析成
`[(elem, H), (sub, 2), (elem, O)]` 这样的部件序列，再交给

- `_render_chem(parts, "latex")` → `\text{H}_2\text{O}`（表格内外都用这条）
- `_render_chem(parts, "html")` → `H<sub>2</sub>O`（历史遗留，`_formula_to_html()` 仍在，
  但已不参与交付物生成）

同一个解析器 + 同一条流水线，保证"正文里对、表格里错"的漂移从结构上不存在。
旧方言的 HTML 化学记号（模型沿用旧提示词，或对老文件重新归一化）会被
`_flatten_subsup()` 折叠回 Unicode 上下标，再自动变成 LaTeX —— 老产物不用手改。

### 4.4 电荷消歧：Fe3+ 是电荷、H2O 是下标

"元素后面跟数字"到底是下标还是电荷，是这份后处理里唯一需要动脑的地方：

| 输入 | 输出 | 判据 |
| --- | --- | --- |
| `H2O` | `$\text{H}_2\text{O}$` | 数字后面不是符号 → 下标 |
| `Fe3+` | `$\text{Fe}^{3+}$` | 单元素 + 数字 + 末位符号 → 电荷 |
| `Ca2+` | `$\text{Ca}^{2+}$` | 同上 |
| `SO42-` | `$\text{S}\text{O}_4^{2-}$` | 多元素式子：末位数字是电荷量，其余是下标 |
| `MnO4-` | `$\text{Mn}\text{O}_4^-$` | 多元素、数字只有一位 → 下标 + 电荷 |
| `NH4+` | `$\text{N}\text{H}_4^+$` | 同上 |
| `2Na+2H2O` | `$2\text{Na}+2\text{H}_2\text{O}$` | `+` 后面紧跟数字 → 它是**加号**，不是电荷 |
| `Ca(OH)2` | `$\text{Ca}(\text{O}\text{H})_2$` | 右括号后的数字是系数 |
| `LaTeX` / `A1` / `pH` | 原样不动 | 元素表校验：`T`、`A`、`p` 不是元素符号 |

核心判据是"**电荷位置**"：`+`/`-` 后面是结尾、空白、右括号或另一个符号时，它才可能
是电荷；否则它只是分隔两个式子的加号。元素表（1–118）用来把
`LaTeX`、`Markdown`、`A1` 这类词挡在化学式之外 —— 早期版本靠黑名单，漏一个就误伤一次。

`file_name` 里的 `_` 也不会被当成下标：只有 `_` 后面跟数字或 `{` 才当上下标。

### 4.5 表格结构为什么这么难：三个必踩的坑

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| 表格内部有空行 | CommonMark 里 HTML 块遇到空行就结束，后半张表掉出去被当普通段落 | `_strip_blanks_inside()` 删掉表格内部空行 |
| 行内单元格数不齐 | 渲染时整列错位、表头对不上数据 | `_balance_rows()` 补空 `<td>`（有 colspan/rowspan 时保守跳过） |
| 单元格里半个式子裸露 | `\ce{...}` 换算完是裸 LaTeX，逐 token 包装只裹住一小截，渲染出一半公式一半源码 | `_wrap_bare_latex_commands()` 把命令段整段收进 `$...$`（中文说明不会被拖进数学模式） |

另外两个兜底：

- **只有 `<td>` 没有 `<tr>`** → `_wrap_loose_cells()` 补一层 `<tr>`；
- **模型写了 Markdown 管道表**（`| a | b |`）→ `_is_pipe_separator()` 认出对齐行，
  `_pipe_table_to_html()` 就地升级成真正的 HTML 表格，单元格同样走 LaTeX 流水线。

还有一个小而致命的细节：标签切分必须用"**像标签的尖括号**"
（`</?[A-Za-z][^<>]*>`），不能简单用 `<[^>]*>` —— 否则可逆符号 `<=>` 会被当成一个
标签整段跳过，单元格里的可逆反应永远归一化不到（实测踩过，已修）。

### 4.6 GLM 实测的三个坑

1. **`glm-4v-flash` 的 `max_tokens` 上限是 1024**。传 2048 直接回
   `400 / 1210 max_tokens参数非法：限制数值范围[1,1024]`。
   `_MODEL_MAX_TOKENS_CAP` 在发请求前就夹紧（第一层），真被拒时再自适应降档重试（第二层）。
2. **`glm-4.6v-flash` 默认开着思维链**：实测同一页 `completion_tokens=1479`，其中
   `reasoning_tokens=930` —— 思考会吃掉输出预算。默认 `max_tokens=4096` 留足余量，
   并且**只取 `delta.content`、丢弃 `reasoning_content`**，思维链不会混进讲义。
3. **流式（SSE）下错误不走业务码**：推理中途异常终止时，原因放在 `finish_reason`
   里（`sensitive` / `network_error` / `length` / `model_context_window_exceeded`）。
   主脚本必须自己检查 `finish_reason`，否则会把半截内容当成功写进讲义。
   `length` 例外：内容确实生成了一部分，报个提示但保留。

此外，`glm-4.6v-flash` 在高峰期会返回 `1305 该模型当前访问量过大`（实测：一轮
13 页的转换里，它有 8 页连续失败 4 次后退到 `glm-4v-flash`，全程无需人工干预），
属于可重试类，重试耗尽后自动降级到 `glm-4v-flash`。

**模型的几个写法怪癖**（都是真实输出里抓到的，后处理专门兜了）：

| 模型写的 | 问题 | 归一化后 |
| --- | --- | --- |
| `&Delt; Na2CO3` | 漏了字母 a，浏览器原样显示 `&Delt;` | `&Delta;`，进数学片段后是 `$\Delta$` |
| `&#x200b;`（零宽空格） | 隐形字符，用来对齐的，会污染文本 | 删除 |
| `&uarr;` / `&rarr;` | 实体写法不统一 | `↑` / `→`（进数学片段后是 `\uparrow` / `\to`） |
| `\xlongequal{\Delta}` | KaTeX **不支持**（extpfeil 宏包），渲染必报错 | `\overset{\Delta}{=}` |
| `(\Delta) =` | 加热条件写在等号左侧括号里的旧写法 | `\overset{\Delta}{=}` |
| `\text{Na}_2\text{O}_{2(\text{淡黄色})}` | 中文注释（淡黄色）被吞进下标 | `\text{Na}_2\text{O}_2(\text{淡黄色})` |
| `\xrightleftharpoons[下]{上}` | 同上（extpfeil 宏包） | `(上, 下) \rightleftharpoons` |
| `\rightleftharpoonsNaHCO_3` | LaTeX 命令名吃字母，会被读成一个不存在的命令 | `\rightleftharpoons NaHCO_3` |
| `$\text{Na}_2\text{C}\text{O}_3$ = $2\text{Na}\text{H}\text{C}\text{O}_3$` | 式子被空格切成两段，源码读起来是断的 | 缝合成一个 `$...$` |
| `$\text{A} ⇌ \text{B}$` | 数学片段里的 Unicode 符号，KaTeX 支持时好时坏 | `\rightleftharpoons` |
| `$\ce{2H2 ^}$` | mhchem 的气体上标 `^` 裸留在 LaTeX 里 → KaTeX 报 "Expected group after '^'" | 上标 `^+`（或整段丢弃裸 `^`） |
| 单元格里的 `<sub>/<sup>` | 旧方言的 HTML 化学记号，与新约定冲突 | 折叠回 Unicode 上下标，再变成 `$...$` 里的 LaTeX |

模型可用性（同一把 Key 实测）：

| 模型 | 状态 |
| --- | --- |
| `glm-4.6v-flash` | 免费可用，视觉 + 思维链，输出质量最好；高峰期偶发 1305 |
| `glm-4v-flash` | 免费可用，速度更快，输出上限 1024 tokens |
| `glm-4.5v` / `glm-4.6v` / `glm-4v-plus` | `429 / 1113 余额不足或无可用资源包`（付费模型） |

默认降级链 `glm-4.6v-flash → glm-4v-flash`，两个都不花额度。

### 4.7 错误分类：重试 / 换模型 / 直接失败

错误码按官方《错误码》文档分类（均已实测核对）：

| 类别 | 错误码 | 处理 |
| --- | --- | --- |
| 可重试 | 1200 / 1230 / 1234 / 1302（速率限制）/ 1305（平台过载） | 指数退避 + 抖动，最多 4 次 |
| 换模型 | 1113（欠费）/ 1211（模型不存在）/ 1220 / 1221 / 1222 / 1308 / 1310 / 1316-1321 | 切到降级链的下一个模型 |
| 致命 | 1000-1005（鉴权）/ 1210（参数非法）/ 1261（Prompt 超长）/ 1301（敏感内容）等 | 直接抛错，不浪费额度 |

没有业务码时退化成按 HTTP 状态码判断（408/429/5xx → 重试，401/403/400 → 致命，
404 → 换模型）。网络层异常一律按可重试处理。

智谱的限流是**并发维度**（同一时刻处理中的请求数），所以主脚本坚持**串行逐页**调用。

### 4.8 增量落盘与资源清理

- 输出文件在开头就创建并写好一级标题，**每批解析完立即 `write + flush`**：
  中途崩了，已完成的页仍然是完整的、能用的讲义（自检里专门测了这条）。
- 渲染出的临时 JPEG 全部放在 `tempfile.mkdtemp(prefix="hm_glm_pages_")`，
  在 `finally` 里逐个删除；清理失败（Windows 句柄未释放很常见）**绝不让整个任务失败**。

---

## 5. 命令行参数

### 5.1 单文件入口

```
python pdf_to_chemistry_handout_glm.py [输入.pdf] [输出.md] [选项]
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `input_pdf` | `test1.pdf` | 输入 PDF |
| `output_md` | `<输入名>_handout_glm.md` | 输出 Markdown |
| `--model` | `glm-4.6v-flash,glm-4v-flash` | 模型名，逗号分隔即降级链 |
| `--api-key` | 环境变量/内置 | API Key |
| `--base-url` | `https://open.bigmodel.cn/api/paas/v4/` | 网关地址 |
| `--dpi` | 150 | 页面渲染 DPI |
| `--pages-per-request` | 1 | 每次请求塞几页图 |
| `--temperature` | 0.0 | 采样温度（转写要确定性，别调高） |
| `--max-tokens` | 4096 | 输出上限（`glm-4v-flash` 自动夹到 1024） |
| `--thinking` | auto | 思维链开关（auto / enabled / disabled） |
| `--no-stream` | 关 | 关闭流式输出 |
| `--pages` | 全部 | 只处理指定页，如 `1-3,7` |
| `--no-normalize` | 关 | 跳过 handout_normalize（调试用，交付物别这么产） |
| `--check` | — | 只做 API 连通性自检 |
| `--check-image` | 内置测试图 | 自检改用真实截图 |
| `--quiet` | 关 | 少打印过程信息 |

### 5.2 批量入口 batch_convert.py

一次转一批 PDF / 一整个目录。它只是单文件入口的**批量外壳**：转换逻辑直接调用
`convert_pdf_to_handout()` / `convert_selected_pages()`，一行都没有重写，所以单跑与
批量跑的产物完全一致（同一套提示词、同一套后处理、同一套落盘节奏）。

```bash
conda activate Handout-Maker2

python batch_convert.py 课件目录                    # 目录下所有 PDF（不递归）
python batch_convert.py 课件目录 -r                 # 递归子目录
python batch_convert.py 课件目录 -o 讲义            # 统一输出到 讲义/（自动建目录）
python batch_convert.py "image/*.pdf"               # glob（PowerShell 下记得加引号）
python batch_convert.py --list-file list.txt        # 清单文件，一行一个路径
python batch_convert.py a.pdf b.pdf -o 讲义 --skip-existing --verify --report r.json
python batch_convert.py 课件目录 --dry-run          # 只看会转哪些、输出到哪（不花额度）
python batch_convert.py 课件目录 --verify-only      # 只体检已有产物，不花额度
```

批量专有参数（其余 `--model / --dpi / --pages / --max-tokens / --no-normalize / …`
与单文件入口同名同义，原样透传）：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `inputs` | 必填 | 文件 / 目录 / glob，可混写多个；**不给就报错退出**（刻意不默认扫当前目录，免得一次误触烧掉一批额度） |
| `-o, --outdir` | 与各 PDF 同目录 | 输出目录，不存在自动创建 |
| `-r, --recursive` | 关 | 目录输入时递归子目录 |
| `--list-file` | — | 从文本文件读输入路径（`#` 开头是注释） |
| `--exclude` | — | 排除命中该 glob 的 PDF（对文件名或相对路径匹配），可重复给 |
| `--include-outputs` | 关 | 连 `*_handout_glm.pdf`（本工具自己的产物）也一起转 |
| `--skip-existing` | 关 | 跳过已存在且完整的输出（重跑续传用） |
| `--dry-run` | 关 | 只列计划，不调 API、不写文件 |
| `--stop-on-error` | 关 | 一失败就停下（默认继续跑完整批） |
| `--precheck` | 关 | 开跑前先做一次 API 连通性自检，不通就整批不启动 |
| `--verify` | 关 | 每份产物跑一遍 `verify_handout` 的检查 |
| `--verify-only` | 关 | 不转换，只体检已有产物（不花额度） |
| `--html-preview DIR` | — | 顺便把 KaTeX 预览页写到该目录（隐含 `--verify`） |
| `--report FILE` | — | 把本次结果写成 JSON 报表（每份的用时 / 页数 / 体检 / 错误） |
| `--quiet` | 关 | 不逐页回显主脚本输出（失败时仍会打出该文件的最后几行日志） |

退出码：`0` 全部成功（含跳过） / `1` 有失败 / `2` 用法或输入有问题。

七个刻意的设计（都是"批量"与"单文件"的真正差别）：

1. **串行，永远串行**。智谱的限流是并发维度（同一时刻处理中的请求数），并行跑多个 PDF
   只会一起撞 1302 然后集体退避 —— 所以这里**没有** `--jobs`。
2. **先写 `<输出>.md.part`，整份跑完才改名**。单文件时"中途崩了也留下半份可用讲义"是
   优点，但在批量里会让 `--skip-existing` 把半成品当成已完成；所以批量一律先写 `.part`，
   成功才 `os.replace` 成正式名。失败时 `.part` 原地保留供排查，但绝不被视为成品。
3. **一个文件失败不拖垮整批**。文件级异常全部隔离，末尾给失败清单 + 一条可直接复制
   粘贴的重跑命令（配 `--skip-existing` 只补失败的那几个）。
4. **体检内置**。`--verify` 复用 `verify_handout.py` 的检查函数（同进程 import，不起
   子进程），逐份核对表格结构 / 化学记号 / 符号，汇总里单列"体检不合格"清单 ——
   体检不合格**不改退出码**、也**不算转换失败**（文件本身已落盘）；只有 `--verify-only`
   模式里体检结果才决定退出码，因为那个模式的唯一目的就是体检。
5. **输出名冲突自动消歧**。同名 PDF 散在不同子目录、又都输出到同一个 `-o` 时，用父目录名
   区分，不会静默互相覆盖；输入按自然序排（`讲义2` 排在 `讲义10` 前面）。
6. **输入展开替你把坑踩平**：自动去重（同一个 PDF 不会转两遍），**字面路径优先于通配符
   展开** —— `[2]碳酸钠+碳酸氢钠.pdf` 这种名字里的 `[2]` 会被 glob 当成字符类，
   先展开就会"查无此文件"而把整份课件静默漏掉（实测踩过，已修）。
7. **默认跳过 `*_handout_glm.pdf`**。产物 PDF 常常和源课件放在同一个目录（本仓库就是），
   扫描时把它们再转一遍只会得到 `xxx_handout_glm_handout_glm.md`。要一起转加
   `--include-outputs`，要手工排除加 `--exclude PATTERN`。

`--report` 的 JSON 形状：`counts`（total/ok/skip/fail）、`total_pages`、`options`
（本次生效的参数）、`results[]`（每份的 `source / output / status / seconds /
pdf_pages / verify / error`）。

---

## 6. 输出格式说明

```markdown
# test1 课程讲义                      ← 一级标题，取 PDF 文件名

## 钠的化合物                        ← 课件里的标题按层级还原，正文连续

碳酸钠的化学式为 $\text{Na}_2\text{CO}_3$，与水反应：
$2\text{Na} + 2\text{H}_2\text{O} = 2\text{NaOH} + \text{H}_2\uparrow$

<table><thead><tr><th>物质</th><th>化学式</th><th>与盐酸反应</th></tr></thead><tbody>
<tr><td>纯碱</td><td>$\text{Na}_2\text{CO}_3$</td><td>$\text{Na}_2\text{CO}_3 + 2\text{H}\text{Cl} = 2\text{Na}\text{Cl} + \text{H}_2\text{O} + \text{C}\text{O}_2\uparrow$</td></tr>
<tr><td>小苏打</td><td>$\text{Na}\text{H}\text{C}\text{O}_3$</td><td>$\text{Na}\text{H}\text{C}\text{O}_3 + \text{H}\text{Cl} = \text{Na}\text{Cl} + \text{H}_2\text{O} + \text{C}\text{O}_2\uparrow$</td></tr>
</tbody></table>

>[图示: 碳酸钠与碳酸氢钠的转化关系]
```

要点：

- 一级标题只有一个，正文连续，**没有「## 第 N 页内容」**；
- 表格是独立成段的 HTML 块（前后有空行，内部无空行），能直接交给任何 Markdown 渲染器；
- 化学记号表格内外都是 `$...$`（需要 KaTeX/MathJax），表格结构与化学记号各归各的；
- 图表位置留 `>[图示: ...]` 占位。

---

## 7. 测试与验证

### 7.1 离线自检（不联网、不花额度）

```bash
python selfcheck_glm_offline.py test1.pdf
```

用**假传输层**替换网络调用，跑通「渲染 → 分批 → 调用 → 重试/降级/夹紧 → 切页 →
归一化 → 落盘 → 清理」全链路，13 组 223 项断言，覆盖：

| 组 | 内容 |
| --- | --- |
| 1–2 | 纯函数、`max_tokens` 按模型夹紧 |
| 3–5 | 响应形状归一（三种 `content`）、思维链剥离、错误分类、`finish_reason` |
| 6 | 请求体形状（对齐 notebook 的 `create(...)`） |
| 7 | PDF 渲染 → JPEG（含 5MB / 6000px 上限） |
| 8, 8b | 页序切分、归一化不得破坏页序标记、**分页标题不进交付物** |
| 8c | GLM 实测暴露的三个归一化坑（`\(...\)`、漏一个 `$`、裸 `\text{}`） |
| 8d | **表格内行内 LaTeX**：原样保留 `$...$`、裸化学式被包成 `$...$`、无 `<sub>/<sup>`、等号、电荷、`↓`、无空行、幂等 |
| 8d2 | 表格里旧方言的 `<sub>/<sup>` 被折叠回 LaTeX |
| 8e | 表格结构修复：补空 `<td>`、管道表升级、补 `<tr>` |
| 8f | **方程式符号**：`===`→`=`、`->`→`=`、`→` 方程式还原/转化关系保留、`⇌`、`\xrightarrow` 保留 |
| 8g | HTML 实体归一：`&Delt;`、`&uarr;`、`&#x200b;`、`&nbsp;` 不被误删 |
| 8h | KaTeX 兼容：`\xlongequal` → `\overset{...}{=}`、`\overset`/`\stackrel` 原样保留、旧写法 `(\Delta) =` → `\overset{\Delta}{=}`、数学片段内 Unicode 符号 → LaTeX 命令、命令后补空格、中文注释从下标里提出 |
| 9 | `call_glm` 的重试 / 降级 / 夹紧（假传输层注入异常） |
| 10–10d | 端到端落盘、异常路径、`--no-normalize`、`--pages`、CLI 参数 |
| 13a–13h | **批量入口**：输入展开（目录/glob/清单/去重/自然序）、`[2]xxx.pdf` 不被 glob 的字符类吃掉、输出名冲突消歧、半成品不算成品、`.part` 改名、坏文件不拖垮整批、`--skip-existing` 不重复发请求、`--report` 计数、`--verify-only` 不发请求、`--dry-run` 不写文件、`--stop-on-error` 停批也写报表 |

### 7.2 交付物体检（对真实产物）

```bash
python verify_handout.py test1_handout_glm.md --html preview.html
```

`selfcheck` 测的是**程序**，`verify_handout` 测的是**产物**：

1. 表格结构 —— 标签配平、每行单元格数一致、表格内部无空行；
2. 化学记号 —— **表格内外都必须是行内 LaTeX**：`$` 定界符成对、LaTeX 命令都在
   `$...$` 里、没有残留的 Unicode 上下标与裸 `↑↓`、没有用 `<sub>/<sup>` 冒充化学记号；
3. 符号 —— 没有 `===` / `->` / `-->`，没有「第 N 页」分页标题，没有 HTML 注释与围栏；
4. **真渲染** —— 用 markdown-it 渲染一遍，确认渲染出的 `<table>` 数量与源码一致、
   表格没有被转义成纯文本、表格里的化学式确实带 `$...$`。

`--html` 会额外写出一份带表格样式的预览页（含 KaTeX CDN），浏览器打开即可肉眼确认
公式与表格 —— 记号全是 LaTeX 之后，这份预览页也是验收公式渲染的**唯一可靠手段**。

### 7.3 真实 API 联调

```bash
python pdf_to_chemistry_handout_glm.py --check            # Key / 网络 / 看图
python pdf_to_chemistry_handout_glm.py "[2]碳酸钠+碳酸氢钠.pdf"
python verify_handout.py "[2]碳酸钠+碳酸氢钠_handout_glm.md" --html preview.html

# 批量链路（同一套转换逻辑，多一层批量外壳）
python batch_convert.py 课件目录 -r -o 讲义 --skip-existing --verify --report r.json
```

`--check` 用现场生成的一张写着 `H2O NaCl Fe3+ Na2CO3` 的小图做探针：
能读出这些字符，就说明鉴权、网关、看图三件事都通了（比只 ping 接口更能说明问题）。

实测（13 页课件，150 DPI）：

- 每页渲染成 2000×1125 / 108~264KB，远低于平台 5MB 单图上限；
- `glm-4.6v-flash` 高峰期频繁 1305，脚本自动退避重试并降级到 `glm-4v-flash`，13 页全部落盘；
- 每页 `reasoning_tokens`（思维链）330~1229，占掉相当一部分输出预算 —— 这也是默认
  `max_tokens=4096`、且只取 `content` 的原因；
- 产物里 `verify_handout.py` 的检查项与 `selfcheck_glm_offline.py` 的离线断言全部通过。

批量链路实测（`test1.pdf` + `[2]碳酸钠+碳酸氢钠.pdf`，各只转第 1 页）：

- 两份都按 `<名>_handout_glm.md` 落盘，`.part` → `.md` 改名完成，报告 `counts={"ok":2}`；
- 高峰期两份都先撞了 4 次 1305 再降级到 `glm-4v-flash`，整批无需人工干预；
- 逐份体检 11/11、21/21 全通过，`--report` 的 JSON 与屏幕汇总一致。

---

## 8. 已知限制

- **幻觉**：模型可能补出图里没有的数据。提示词要求"看不清用 □ 占位"，但仍需人工复核，
  尤其是数值、实验条件。
- **图表丢失**：结构式、装置图不会重建，只留 `>[图示: ...]` 占位。
- **长表格截断**：`glm-4v-flash` 输出上限 1024 tokens，超长页面会被截断
  （`finish_reason=length` 会打印提示）。要长输出就用 `glm-4.6v-flash`（4096）。
- **页序依赖标记**：多页批模式下若模型一个 `<!-- page: N -->` 都没打，
  整批会作为一块处理（宁可少切分，也不把 A 页内容挂到 B 页上）。默认一页一请求可规避。
- **化学歧义**：`SO42-` 这类"写法本身有歧义"的输入按化学惯例消歧（→ `SO₄²⁻`）；
  罕见离子仍可能被解释成下标。提示词要求写成 `$...$` 里的 LaTeX，从源头避免歧义。
- **渲染前提**：化学记号与表格内公式都要求渲染环境挂 KaTeX/MathJax（见第 2 节）。
  纯 HTML 导出、不挂公式渲染器的场景下，表格里的公式会显示成 `$...$` 源码。
- **速度**：串行逐页，免费模型 + 思维链，单页约十几秒到二十几秒。

---

## 9. 目录结构

```
Handout-Maker2/
├── pdf_to_chemistry_handout_glm.py           # 主脚本（渲染 + API + 落盘 + 自检）
├── batch_convert.py                          # 批量入口（串行 + 续传 + 体检 + JSON 报表）
├── handout_normalize.py                      # 确定性后处理（可独立 CLI 清洗已有 md）
├── selfcheck_glm_offline.py                  # 离线自检（不联网）
├── verify_handout.py                         # 交付物体检（渲染校验 + HTML 预览）
├── verify_GLM-Flash_API.ipynb                # 最初的 API 验证 notebook（网关/Key 来源）
├── README.md
├── test1.pdf                                 # 示例课件（15 页）
├── [2]碳酸钠+碳酸氢钠.pdf                     # 示例课件（13 页，含多张对照表）
├── [2]碳酸钠+碳酸氢钠_handout_glm.md          # 示例产物（真实 API 跑出来的）
├── preview_碳酸钠.html                        # 上面那份产物的 HTML 渲染预览
└── legacy_non_glm_backup.zip                 # 历史版本备份（本地 Qwen / Gemini 两个脚本等）
```

> 本工程只保留 GLM 这一条链路。历史版本（本地 Qwen2-VL 4bit 版、Gemini 版）的脚本、
> 自检与 notebook 已打包进 `legacy_non_glm_backup.zip` 并从工作目录移除。

`handout_normalize.py` 可以单独用（对已有输出做离线清洗，缺省原地覆盖）：

```bash
python handout_normalize.py 输入.md [输出.md]
```

---

## 10. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| `1113 余额不足或无可用资源包` | 该模型是付费模型，换 `--model glm-4.6v-flash` 或 `glm-4v-flash` |
| `1000-1005 身份验证失败` | Key 无效/过期：`--api-key` 或环境变量 `GLM_API_KEY` |
| `1302 已达到速率限制` | 并发过高。本脚本默认串行逐页；别同时跑多个实例 |
| `1305 模型当前访问量过大` | 平台过载，自动重试 + 降级；也可手动换成 `glm-4v-flash` |
| 超时 / 连接被重置 | 国内网关无需代理；若开了全局代理反而可能绕出国，临时 unset `http_proxy` / `https_proxy` |
| `ModuleNotFoundError: pymupdf` | 用错环境了：`conda activate Handout-Maker2` |
| 输出里表格显示成源码 | 跑一下 `verify_handout.py`，它会指出是哪张表、哪一项不合格 |
| 表格里出现 `$...$` 源码 | 渲染环境没挂 KaTeX：`verify_handout.py --html preview.html` 生成的预览页自带 |
| 老产物（表格里是 `<sub>`）想换成新约定 | `python handout_normalize.py 老产物.md`：HTML 上下标会被折叠成 LaTeX，幂等可重复跑 |
| `finish_reason=length` 提示 | 该页输出被截断：换 `glm-4.6v-flash` 或调大 `--max-tokens` |
| 批量中断了，不想从头再来 | 重跑时加 `--skip-existing`：已完整落盘的产物直接跳过，不重复发请求 |
| 输出目录里出现 `xxx.md.part` | 那是中途失败（或 Ctrl+C）留下的半成品，不是成品：看一眼没问题就手动改名成 `.md`，否则直接删掉重跑 |
| 某个课件没被批量转到 | ① 名字里带 `[]` 的（如 `[2]碳酸钠+碳酸氢钠.pdf`）已按字面路径处理，不会再被 glob 吃掉；② 名字以 `_handout_glm` 结尾的 PDF 被当成自家产物默认跳过了，要转就加 `--include-outputs`；③ 目录没加 `-r` 时不含子目录 |
| 想先确认会转哪些、转到哪 | `python batch_convert.py 输入 --dry-run`：只列计划，不调 API、不写文件 |
| 批量跑之前想先确认 Key 还能用 | `python batch_convert.py 输入 --precheck`：自检不过就整批不启动，不会白跑一轮 |

---

## 附录 A：常量与环境变量速查

| 常量 | 默认值 | 环境变量 |
| --- | --- | --- |
| 渲染 DPI | 150 | `HM_DPI` |
| 每请求页数 | 1 | `HM_PAGES_PER_REQUEST` |
| 单图字节上限 | 4 MB | `HM_MAX_IMAGE_BYTES` |
| 单图像素上限 | 6000 px | `HM_MAX_IMAGE_EDGE` |
| 最大重试次数 | 4 | `HM_MAX_RETRIES` |
| 重试基础退避 | 5 s（指数 + 抖动） | `HM_RETRY_BASE_DELAY` |
| 单请求超时 | 300 s | `HM_REQUEST_TIMEOUT` |
| 输出 token 上限 | 4096 | `HM_MAX_TOKENS` |
| 降级链 | `glm-4.6v-flash,glm-4v-flash` | `--model` |
| 网关 | `https://open.bigmodel.cn/api/paas/v4/` | `GLM_BASE_URL` |
| API Key | 内置兜底值 | `GLM_API_KEY` / `ZHIPUAI_API_KEY` / `BIGMODEL_API_KEY` / `ZHIPUAI_API_KEY` |

标题后缀 `_DEFAULT_TITLE_SUFFIX = " 课程讲义"`：输出文件的一级标题是
`<PDF 文件名>` + 这个后缀。
