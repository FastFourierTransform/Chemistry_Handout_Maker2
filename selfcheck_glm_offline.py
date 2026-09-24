# -*- coding: utf-8 -*-
"""本地离线自检：不联网，用假传输层跑通「渲染 -> 分批 -> 调用 -> 重试/降级 -> 归一化 -> 落盘」全链路。

在 Handout-Maker2 环境里运行（有 pymupdf + PIL + handout_normalize）：

    python selfcheck_glm_offline.py [输入.pdf]
"""
import contextlib
import glob as _glob
import hashlib
import io
import os
import re
import shutil
import struct
import sys
import tempfile
import types as pytypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdf_to_chemistry_handout_glm as G  # noqa: E402
import batch_convert as B  # noqa: E402

PDF = sys.argv[1] if len(sys.argv) > 1 else "test1.pdf"
OUT = "_tmp_glm_offline_out.md"
FAILS = []


def check(name, cond, extra=""):
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


# 记录开跑前已有的渲染临时目录：结束时按“差集”清理，确保本自检不留任何残留
# （Windows 上偶发句柄未释放会让 rmtree 静默失败，所以最终用差集断言而不是只删自己那个变量）。
_PRE_TMP = set(_glob.glob(os.path.join(tempfile.gettempdir(), "hm_glm_pages_*")))


# ---------------------------------------------------------------------------
print("== 1. 纯函数 ==")
check("parse_models 默认链", G.parse_models(None) == list(G.DEFAULT_MODELS), str(G.DEFAULT_MODELS))
check("parse_models 逗号解析", G.parse_models("a, b ,c") == ["a", "b", "c"])
check("_parse_page_spec 1-3,7", G._parse_page_spec("1-3,7") == {1, 2, 3, 7})
check("_parse_page_spec 空", G._parse_page_spec(None) is None)
check("resolve_api_key 环境变量优先",
      G.resolve_api_key() == (os.environ.get("GLM_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
                              or os.environ.get("BIGMODEL_API_KEY") or os.environ.get("ZHIPU_API_KEY")
                              or G.DEFAULT_API_KEY))
check("resolve_api_key 显式覆盖", G.resolve_api_key("XYZ") == "XYZ")
check("resolve_base_url 默认=notebook 里的网关",
      G.resolve_base_url() == "https://open.bigmodel.cn/api/paas/v4/", G.resolve_base_url())

print("\n== 2. max_tokens 按模型夹紧（glm-4v-flash 上限 1024，实测 1210 报错）==")
check("glm-4v-flash 4096 -> 1024", G.model_max_tokens("glm-4v-flash", 4096) == 1024)
check("glm-4v-flash 512 不动", G.model_max_tokens("glm-4v-flash", 512) == 512)
check("glm-4.6v-flash 4096 不夹", G.model_max_tokens("glm-4.6v-flash", 4096) == 4096)

print("\n== 3. 响应形状归一（content 三种形状 / 思维链剥离）==")
check("content 字符串", G._content_to_text("abc") == "abc")
check("content 多模态数组（GLM-4V 系列）",
      G._content_to_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab")
check("content 为 null（tool_calls 场景）", G._content_to_text(None) == "")
check("剥离 <think> 思维链",
      G._strip_model_scaffolding("<think>想了半天</think>答案") == "答案")
check("剥离 <|begin_of_box|> 边界标记",
      G._strip_model_scaffolding("<|begin_of_box|>正文<|end_of_box|>") == "正文")

print("\n== 4. 错误分类（业务码取自官方错误码文档）==")
check("1302 速率限制 -> 重试", G._classify(429, "1302", "速率限制") == G._KIND_RETRY)
check("1305 平台过载 -> 重试", G._classify(429, "1305", "访问量过大") == G._KIND_RETRY)
check("1113 欠费/付费模型 -> 换模型", G._classify(429, "1113", "余额不足") == G._KIND_NEXT_MODEL)
check("1211 模型不存在 -> 换模型", G._classify(400, "1211", "模型不存在") == G._KIND_NEXT_MODEL)
check("1000 鉴权失败 -> 致命", G._classify(401, "1000", "身份验证失败") == G._KIND_FATAL)
check("1210 参数非法 -> 致命", G._classify(400, "1210", "参数非法") == G._KIND_FATAL)
check("1301 敏感内容 -> 致命", G._classify(400, "1301", "敏感") == G._KIND_FATAL)
check("未知码 + 503 -> 重试", G._classify(503, "9999", "busy") == G._KIND_RETRY)
check("未知码 + 401 -> 致命", G._classify(401, "9999", "unauthorized") == G._KIND_FATAL)
e = G._error_from_body('{"error":{"code":"1113","message":"余额不足或无可用资源包,请充值。"}}', 429)
check("_error_from_body 解析业务码", e.code == "1113" and e.kind == G._KIND_NEXT_MODEL, str(e))
check("_error_from_body 非 JSON 兜底", G._error_from_body("<html>502</html>", 502).code is None)
check("网络异常 -> 可重试", G._error_from_exception(TimeoutError("timed out")).kind == G._KIND_RETRY)
check("_is_retryable 兼容命名", G._is_retryable(G.GlmApiError(G._KIND_RETRY, None, "x"))
      and not G._is_retryable(G.GlmApiError(G._KIND_FATAL, None, "x")))

print("\n== 5. finish_reason 异常值（流式下错误不走业务码，见模块 docstring 坑 3）==")
check("stop 无错", G._finish_reason_error("stop") is None)
check("空 无错", G._finish_reason_error("") is None)
check("length 不算失败（截断保留）", G._finish_reason_error("length") is None)
check("sensitive -> 致命", G._finish_reason_error("sensitive").kind == G._KIND_FATAL)
check("network_error -> 重试", G._finish_reason_error("network_error").kind == G._KIND_RETRY)
check("context_window_exceeded -> 致命",
      G._finish_reason_error("model_context_window_exceeded").kind == G._KIND_FATAL)

print("\n== 6. 请求体形状（对齐 notebook 的 create(...) 参数）==")
from PIL import Image as _Image  # noqa: E402
_Image.new("RGB", (32, 32), "white").save("_tmp_glm_dummy.jpg", format="JPEG")
DUMMY = "_tmp_glm_dummy.jpg"
pl = G.build_payload("glm-4.6v-flash", [DUMMY], G.PROMPT)
check("model 字段", pl["model"] == "glm-4.6v-flash")
check("temperature 默认 0（确定性）", pl["temperature"] == 0.0)
check("stream 默认 True", pl["stream"] is True)
check("system 角色压助手人格", pl["messages"][0]["role"] == "system")
check("user 内容是 image_url + text",
      [c["type"] for c in pl["messages"][1]["content"]] == ["image_url", "text"])
check("图片是 base64 data URL",
      pl["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
check("thinking=auto 时不发该字段", "thinking" not in pl)
check("thinking=disabled 时显式发送",
      G.build_payload("glm-4.6v-flash", [DUMMY], G.PROMPT,
                      thinking="disabled")["thinking"] == {"type": "disabled"})
check("多图时自动用页序标记提示词",
      "page: N" in G.build_payload("m", [DUMMY, DUMMY], G.MULTI_IMAGE_PROMPT)["messages"][1]["content"][-1]["text"])

print("\n== 7. PDF 渲染 -> JPEG（含 5MB / 6000px 上限）==")
pages, workdir = G.render_pdf_pages(PDF, dpi=100)
check("页数与 PDF 一致", len(pages) > 0, f"{len(pages)} 页, 临时目录 {workdir}")
sizes = [os.path.getsize(p) for _, p in pages]
check("JPEG 魔数正确", all(open(p, "rb").read(2) == b"\xff\xd8" for _, p in pages))
check("单页体积可控", all(s <= G.MAX_IMAGE_BYTES for s in sizes),
      f"min={min(sizes)/1024:.0f}KB max={max(sizes)/1024:.0f}KB")
try:
    with _Image.open(pages[0][1]) as im:
        fmt, mode, size = im.format, im.mode, im.size
        im.load()
    check("PIL 可解码", fmt == "JPEG" and mode == "RGB", f"{size} {fmt} {mode}")
except Exception as ex:  # noqa: BLE001
    check("PIL 可解码", False, str(ex))

tiny, wd_tiny = G.render_pdf_pages(PDF, dpi=200, max_edge=600)
with _Image.open(tiny[0][1]) as im:
    tiny_size = im.size
check("max_edge 生效（像素不超过上限）", max(tiny_size) <= 600, str(tiny_size))
shutil.rmtree(wd_tiny, ignore_errors=True)

print("\n== 8. 页序切分 ==")
t = "<!-- page: 1 -->\n# 甲\n\nA\n<!-- page: 2 -->\n# 乙\n\nB"
blocks = G.split_by_page_marks(t)
check("两页标记 -> 两块", [b[0] for b in blocks] == [1, 2], str([b[0] for b in blocks]))
check("块内容正确", blocks[0][1].endswith("A") and blocks[1][1].endswith("B"))
check("无标记 -> 单块兜底", len(G.split_by_page_marks("只有正文")) == 1)
check("识别 '第 N 页' 标题",
      [b[0] for b in G.split_by_page_marks("## 第 1 页\nA\n\n## 第 2 页\nB")] == [1, 2])

print("\n== 8b. 回归：归一化不得破坏页序标记（--> 被还原成 = 的坑）==")
RAW = "<!-- page: 1 -->\nH₂O\n<!-- page: 2 -->\n2Na+2H2O===2NaOH"
check("切页在归一化之前完成", [b[0] for b in G.split_by_page_marks(RAW)] == [1, 2])
rb = G._assemble_blocks(RAW, [(0, "a.jpg"), (1, "b.jpg")], normalize=True)
check("两块正文", len(rb) == 2, str(len(rb)))
check("正文里没有页序标记", all("page:" not in b and "<!--" not in b for b in rb))
check("块内归一化仍生效", r"\text{H}_2\text{O}" in rb[0] and "===" not in rb[1])
deg = G._assemble_blocks("无标记正文", [(0, "a"), (1, "b")], True)
check("缺标记时退化为整块", deg == ["无标记正文"], str(deg))
check("单页批次也返回纯正文", G._assemble_blocks("x", [(4, "e")], False) == ["x"])
check("分页标题不进交付物（模型自己加的也要删）",
      G._assemble_blocks("## 第 1 页内容\nH₂O\n", [(0, "a")], True) == [r"$\text{H}_2\text{O}$"],
      str(G._assemble_blocks("## 第 1 页内容\nH₂O\n", [(0, "a")], True)))

print("\n== 8c. 归一化稳健性（GLM 实测暴露的三个坑，全部钉在测试里）==")
N = G.normalize_handout_markdown

# 坑 1：GLM-4.6V-Flash 会写 \(...\) 这种 LaTeX 圆括号行内数学（实测页 1 的原子结构示意图）
check("\\(...\\) → $...$", N(r"钠原子 \(Na\)").strip() == "钠原子 $Na$", repr(N(r"钠原子 \(Na\)").strip()))
check("\\(\\ce{...}\\) 不产生嵌套 $", N(r"\(\ce{H2O}\)").strip() == r"$\text{H}_2\text{O}$")
check("既有 $...$ 不被改动", N(r"$2\text{Na}$") == "$2\\text{Na}$\n")

# 坑 2：模型偶发漏写一个 $（表格单元格里夹 <br> 时实测出现）会让配对整体错位，
# 修复前会把 \text{Na} 再包一层 $，一行烂出十几处嵌套 $。
_BROKEN = ("化学方程式：$2Na + 2H2O = 2NaOH + H2\\uparrow<br>离子方程式："
           r"$2\text{Na} + 2\text{H}_2\text{O} = 2\text{Na}^+ + 2\text{OH}^-"r"$")
_out = N(_BROKEN)
check("$ 不配平时不产生嵌套 $", not re.search(r"\$[^$\n]*\\text\{\$", _out), repr(_out[:120]))
check("$ 不配平时 LaTeX 命令仍完整",
      r"\text{Na}" in _out and r"\text{OH}" in _out and r"\text{$" not in _out)

# 坑 3：裸写（不带 $）的 LaTeX 片段里的 _2 会被误当成 Unicode 下标再包一层 $
check("裸 \\text{H}_2\\text{O} 原样保留", N(r"\text{H}_2\text{O}").strip() == r"\text{H}_2\text{O}")
check("裸 \\ce{H2O} 转 \\text{} 后不被再包 $", N(r"\ce{H2O}").strip() == r"\text{H}_2\text{O}")
check("带分组的箭头整段保留",
      N(r"\xrightarrow[\Delta]{\text{MnO}_2}").strip() == r"\xrightarrow[\Delta]{\text{MnO}_2}")
check("命令 + 多组上下标保留", N(r"\text{SO}_4^{2-}").strip() == r"\text{SO}_4^{2-}")
# 保护逻辑不能把“该转换的普通文本”也一起放过
check("普通 Unicode 下标仍被包装", N("H₂O").strip() == r"$\text{H}_2\text{O}$")

print("\n== 8d. 表格内一律行内 LaTeX（表格结构仍是 HTML）==")
_TBL = ("<table><thead><tr><th>物质</th><th>化学式</th><th>方程式</th></tr></thead><tbody>"
        r"<tr><td>纯碱</td><td>$\text{Na}_2\text{CO}_3$</td>"
        r"<td>$2\text{Na} + 2\text{H}_2\text{O} = 2\text{NaOH} + \text{H}_2\uparrow$</td></tr>"
        "<tr><td>铁离子</td><td>Fe3+</td><td>Fe3+ + 3OH- = Fe(OH)3↓</td></tr>"
        "</tbody></table>")
_t = N(_TBL)
check("表格保留为 HTML 表格", _t.count("<table>") == 1 and _t.count("</table>") == 1)
check("表格里的 LaTeX 原样保留（不再被降级）",
      r"$\text{Na}_2\text{CO}_3$" in _t and r"\uparrow" in _t, repr(_t[:160]))
check("裸化学式也被包成 $...$", r"$\text{Fe}^{3+}$" in _t, repr(_t[:160]))
check("表格内不残留 HTML 上下标", "<sub>" not in _t and "<sup>" not in _t)
check("表格内不残留 Unicode 上下标", not re.search(r"[₀-₉₊₋⁰-⁹⁺⁻ⁿ]", _t), repr(_t[:120]))
check("方程式等号正确、加号不被当电荷",
      r"$2\text{Na} + 2\text{H}_2\text{O} = 2\text{NaOH} + \text{H}_2\uparrow$" in _t,
      repr(_t[:200]))
check("电荷消歧：OH- → OH^-（走 LaTeX 上标）", "\\text{O}\\text{H}^-" in _t, repr(_t[:220]))
check("沉淀符号进数学片段：↓ → \\downarrow", r"\text{Fe}(\text{O}\text{H})_3\downarrow" in _t,
      repr(_t[:220]))
check("表格内部没有空行（空行会终结 HTML 块）", "\n\n" not in _t.split("</table>")[0])
check("表格块独立成段（前后留空行）", _t.startswith("<table>") and _t.endswith("</table>\n"))
check("表格归一化幂等", N(_t) == _t, repr(N(_t)[:200]))

print("\n== 8d2. 表格内 <sub>/<sup> 折叠回 LaTeX（模型沿用旧写法时的兜底）==")
_t2 = N("<table><tr><td>Na<sub>2</sub>CO<sub>3</sub></td>"
        "<td>Fe<sup>3+</sup></td><td>H<sub>2</sub>O</td></tr></table>")
check("<sub> 下标 → $...$ + _", r"$\text{Na}_2\text{C}\text{O}_3$" in _t2, _t2)
check("<sup> 上标 → $...$ + ^", r"$\text{Fe}^{3+}$" in _t2)
check("折叠后不留 <sub>/<sup>", "<sub>" not in _t2 and "<sup>" not in _t2)

print("\n== 8e. 表格结构修复：补齐行 / 管道表升级 / 补 <tr> ==")
_rag = N("<table><tr><th>a</th><th>b</th><th>c</th></tr><tr><td>1</td><td>2</td></tr></table>")
check("行内单元格不齐时补空 <td>", "<td>1</td><td>2</td><td></td>" in _rag, _rag)
_blk = N("<table>\n\n<tr><td>Ca(OH)2</td></tr>\n\n</table>")
check("表格内部空行被清除", "\n\n" not in _blk, repr(_blk))
check("括号系数 → 下标", r"$\text{Ca}(\text{O}\text{H})_2$" in _blk, _blk)
_pipe = N("| 名称 | 化学式 |\n| --- | --- |\n| 纯碱 | Na2CO3 |\n| 小苏打 | NaHCO3 |\n")
check("管道表升级为 HTML 表", "<table>" in _pipe and "<th>名称</th>" in _pipe, _pipe)
check("管道表单元格也走 LaTeX 方言",
      r"$\text{Na}_2\text{C}\text{O}_3$" in _pipe and r"$\text{Na}\text{H}\text{C}\text{O}_3$" in _pipe, _pipe)
check("管道表行数正确", _pipe.count("<tr>") == 3, str(_pipe.count("<tr>")))
check("无 <tr> 的表格补一层 <tr>",
      "<tr><td>x</td><td>y</td></tr>" in N("<table><td>x</td><td>y</td></table>"))

print("\n== 8f. 方程式符号：等号 / 可逆 / 箭头 ==")
_eq = N("2Na+2H2O===2NaOH+H2↑").strip()
check("多重等号收敛成一个 =", "===" not in _eq and _eq.count("=") == 1, _eq)
check("无空格方程式照样拆对加号",
      r"$2\text{Na}+2\text{H}_2\text{O}=2\text{Na}\text{O}\text{H}+\text{H}_2\uparrow$" == _eq, _eq)
_arrow = N("化学方程式：2H2 + O2 → 2H2O").strip()
check("方程式里的 Unicode → 还原成等号", "→" not in _arrow and "=" in _arrow, _arrow)
_chain = N("转化关系：Na → NaOH → Na2CO3").strip()
check("转化关系的 → 保留（不是等号）",
      _chain.count("→") + _chain.count(r"\to") == 2 and "=" not in _chain, _chain)
check("ASCII 箭头 -> 当等号", "=" in N("CaCO3 -> CaO + CO2↑") and "->" not in N("CaCO3 -> CaO + CO2↑"))
check("可逆符号 <=> → ⇌", "⇌" in N("CaCO3 <=> CaO + CO2↑"))
check("LaTeX 可逆符号保留 \\rightleftharpoons",
      r"\rightleftharpoons" in N(r"$\text{CO}_2 + \text{H}_2\text{O} \rightleftharpoons \text{H}_2\text{CO}_3$"))
check("表格内可逆符号写成 ⇌ / \\rightleftharpoons",
      "⇌" in N("<table><tr><td>CaCO3 <=> CaO + CO2↑</td></tr></table>")
      or r"\rightleftharpoons" in N("<table><tr><td>CaCO3 <=> CaO + CO2↑</td></tr></table>"))
check("表格内加热符号写 \\Delta",
      r"\Delta" in N("<table><tr><td>2KClO3 (MnO2, \\Delta) = 2KCl + 3O2↑</td></tr></table>"))
check("表格内 \\xrightarrow 原样保留（KaTeX 支持，不再降级）",
      r"\xrightarrow[\Delta]{\text{MnO}_2}" in N(
          r"<table><tr><td>$2\text{KClO}_3 \xrightarrow[\Delta]{\text{MnO}_2} 2\text{KCl} + 3\text{O}_2\uparrow$"
          r"</td></tr></table>"),
      N(r"<table><tr><td>$2\text{KClO}_3 \xrightarrow[\Delta]{\text{MnO}_2} 2\text{KCl}$"
        r"</td></tr></table>").strip())
check("非化学语境的箭头统一成 →", "3 → 4" in N("步骤 3 -> 4"))
check("分页标题被清除", "第 1 页" not in N("## 第 1 页内容\n\n正文"))

print("\n== 8g. HTML 实体归一（实测真实输出里出现过 &Delt; 与 &#x200b;）==")
check("&Delt;（模型漏字母）→ 加热符号进数学片段",
      r"\Delta" in N("<table><tr><td>2NaHCO3 &Delt; Na2CO3</td></tr></table>"))
check("&Delta; 也归一成 \\Delta",
      r"\Delta" in N("<table><tr><td>(MnO2, &Delta;) = 2KCl</td></tr></table>"))
check("&uarr; 在正文里归一成 $\\uparrow$", r"\uparrow" in N("反应产生气体&uarr;。"))
check("&darr; 在表格里归一成 \\downarrow",
      r"\downarrow" in N("<table><tr><td>CaCO3&darr;</td></tr></table>"))
check("&#8593; 数字实体 → ↑ 并包进 $...$",
      r"\uparrow" in N("<table><tr><td>H2&#8593;</td></tr></table>"))
check("&rarr; → Unicode →（非化学语境保留箭头）", "→" in N("转化：Na &rarr; NaOH"))
check("零宽空格被丢掉", "\u200b" not in N("a &#x200b; b") and "\u200b" not in N("a \u200b b"))
check("&nbsp; 不被误删（表格里也要活着）",
      "&nbsp;" in N("a&nbsp;b") and "&nbsp;" in N("<table><tr><td>a&nbsp;b</td></tr></table>"))
check("&lt; 不被误删", "&lt;" in N("a &lt; b"))

print("\n== 8h. KaTeX 兼容：\\xlongequal / 数学片段内的 Unicode 符号 ==")
check("\\xlongequal{\\Delta} → \\overset{\\Delta}{=}",
      r"$2NaHCO_3 \overset{\Delta}{=} Na_2CO_3$" in N(r"$2NaHCO_3 \xlongequal{\Delta} Na_2CO_3$"),
      N(r"$2NaHCO_3 \xlongequal{\Delta} Na_2CO_3$").strip())
check("\\xlongequal[下]{上} → \\overset{上}{\\underset{下}{=}}",
      r"\overset{\text{V}_2\text{O}_5}{\underset{\Delta}{=}}" in N(r"$A \xlongequal[\Delta]{\text{V}_2\text{O}_5} B$"),
      N(r"$A \xlongequal[\Delta]{\text{V}_2\text{O}_5} B$").strip())
check("裸 \\xlongequal → =", r"$A = B$" in N(r"$A \xlongequal B$"))
check("\\overset{...}{=} 原样保留（KaTeX 支持）",
      r"$A \overset{\Delta}{=} B$" in N(r"$A \overset{\Delta}{=} B$"))
check("\\stackrel{\\text{..}}{=} 原样保留（KaTeX 支持）",
      r"$C \stackrel{\text{MnO}_2}{=} D$" in N(r"$C \stackrel{\text{MnO}_2}{=} D$"))
check("旧写法 (\\Delta) = → \\overset{\\Delta}{=}",
      r"$A \overset{\Delta}{=} B$" in N(r"$A (\Delta) = B$"),
      N(r"$A (\Delta) = B$").strip())
check("表格内 \\xlongequal → \\overset{\\Delta}{=} 且缝成一个式子",
      r"$2\text{Na}\text{H}\text{C}\text{O}_3 \overset{\Delta}{=} \text{Na}_2\text{C}\text{O}_3$" in
      N(r"<table><tr><td>2NaHCO3 \xlongequal{\Delta} Na2CO3</td></tr></table>"),
      N(r"<table><tr><td>2NaHCO3 \xlongequal{\Delta} Na2CO3</td></tr></table>").strip())
check("中文注释被吞进下标 → 提出来并包 \\text{}",
      r"(\text{淡黄色})" in N(r"$\text{Na}_2\text{O}_{2(\text{淡黄色})}$")
      and r"_{2(\text{淡黄色})}" not in N(r"$\text{Na}_2\text{O}_{2(\text{淡黄色})}$"))
check("中文注释被吞进 \\text{} → 提出来",
      r"\text{O}(\text{白色})" in N(r"$2\text{Na}_2\text{O(白色)}$"))
check("数学片段内 ⇌ → \\rightleftharpoons",
      r"\rightleftharpoons" in N(r"$\text{A} ⇌ \text{B}$"))
check("数学片段内 → 在方程式里还原成 =",
      "=" in N(r"$\text{A} + \text{B} → \text{C}$") and "→" not in N(r"$\text{A} + \text{B} → \text{C}$"))
check("\\text{} 里的 ↑ 不会被塞进 \\uparrow",
      N(r"$\text{H}_2\text{↑}$").strip() == r"$\text{H}_2\text{↑}$",
      N(r"$\text{H}_2\text{↑}$").strip())
check("转化关系片段内 → 变 \\to", r"\to" in N(r"转化：$\text{Na} → \text{NaOH}$"))
check("\\xrightleftharpoons（KaTeX 不支持）→ (条件) \\rightleftharpoons",
      r"\rightleftharpoons" in N(r"$\text{A} \xrightleftharpoons[\text{下}]{\text{上}} \text{B}$")
      and r"(\text{上}, \text{下})" in N(r"$\text{A} \xrightleftharpoons[\text{下}]{\text{上}} \text{B}$"),
      N(r"$\text{A} \xrightleftharpoons[\text{下}]{\text{上}} \text{B}$").strip())
check("替换出的 LaTeX 命令后面必须有空格（否则命令名会和后面的式子连成一体）",
      "\\rightleftharpoonsNa" not in N(
          r"$\text{Na}_2\text{C}\text{O}_3 \xrightleftharpoons[固]{\Delta}\text{NaHCO}_3$")
      and "\\rightleftharpoons " in N(
          r"$\text{Na}_2\text{C}\text{O}_3 \xrightleftharpoons[固]{\Delta}\text{NaHCO}_3$"),
      N(r"$\text{Na}_2\text{C}\text{O}_3 \xrightleftharpoons[固]{\Delta}\text{NaHCO}_3$").strip())
check("表格内 \\xrightleftharpoons → (条件) \\rightleftharpoons",
      r"(上, 下) \rightleftharpoons" in N(r"<table><tr><td>A \xrightleftharpoons[下]{上} B</td></tr></table>"),
      N(r"<table><tr><td>A \xrightleftharpoons[下]{上} B</td></tr></table>").strip())
check("表格里 KaTeX 支持的 \\xleftarrow 原样保留（与正文同款）",
      r"$\text{A} \xleftarrow{\Delta} \text{B}$" in
      N(r"<table><tr><td>$\text{A} \xleftarrow{\Delta} \text{B}$</td></tr></table>"),
      N(r"<table><tr><td>$\text{A} \xleftarrow{\Delta} \text{B}$</td></tr></table>").strip())
check("正文里 \\xrightarrow 保留原样（KaTeX 支持）",
      r"\xrightarrow[\Delta]{\text{MnO}_2}" in N(r"$A \xrightarrow[\Delta]{\text{MnO}_2} B$"))
check("命令紧贴字母时补空格（\\rightleftharpoonsNa → \\rightleftharpoons Na）",
      r"\rightleftharpoons NaCl" in N(r"$\rightleftharpoonsNaCl$"))
check("本来就有空格的不会变成两个空格",
      N(r"$\uparrow H_2$").strip() == r"$\uparrow H_2$", N(r"$\uparrow H_2$").strip())

print("\n== 9. call_glm 的重试 / 降级 / 夹紧（假传输层）==")
G.RETRY_BASE_DELAY = 0.01
_real_stream = G._call_once_stream
_calls = {"n": 0, "payloads": []}


def _reset():
    _calls["n"] = 0
    _calls["payloads"] = []


def _fake_stream_factory(script):
    """script 里是异常实例（抛出）或字符串（当返回值）。"""
    seq = list(script)

    def _fake(url, api_key, payload, timeout):
        _calls["n"] += 1
        # 存副本：payload 是同一个可变 dict（降档时会原地改 max_tokens），
        # 直接存引用会让所有历史记录都变成最后一次的值。
        _calls["payloads"].append(dict(payload))
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, "stop", {"completion_tokens": 1}

    return _fake


_reset()
G._call_once_stream = _fake_stream_factory([G.GlmApiError(G._KIND_RETRY, "1302", "速率限制"), "OK"])
out = G.call_glm("k", ["m1"], [DUMMY], verbose=False)
check("1302 重试后成功", out == "OK" and _calls["n"] == 2, f"调用次数={_calls['n']}")

_reset()
G._call_once_stream = _fake_stream_factory([G.GlmApiError(G._KIND_NEXT_MODEL, "1113", "余额不足"),
                                            "OK-from-m2"])
out = G.call_glm("k", ["m1", "m2"], [DUMMY], verbose=False)
check("1113 时降级到下一个模型", out == "OK-from-m2" and _calls["n"] == 2)
check("降级时换了 model 字段",
      [p["model"] for p in _calls["payloads"]] == ["m1", "m2"], str([p["model"] for p in _calls["payloads"]]))

_reset()
G._call_once_stream = _fake_stream_factory([G.GlmApiError(G._KIND_FATAL, "1000", "身份验证失败")])
try:
    G.call_glm("k", ["m1", "m2"], [DUMMY], verbose=False)
    check("鉴权错误立即抛出", False)
except RuntimeError as e:
    check("鉴权错误立即抛出（不重试不降级）", _calls["n"] == 1, str(e)[:80])

# 第一层防护：build_payload 就按模型把 max_tokens 夹到 1024，根本不该发出超限请求
_reset()
G._call_once_stream = _fake_stream_factory(["OK-preclamped"])
out = G.call_glm("k", ["glm-4v-flash"], [DUMMY], verbose=False, max_tokens=4096)
check("glm-4v-flash 请求发出前就夹到 1024",
      out == "OK-preclamped" and [p["max_tokens"] for p in _calls["payloads"]] == [1024],
      str([p["max_tokens"] for p in _calls["payloads"]]))

# 第二层防护：若某个未登记上限的模型仍被拒（1210 max_tokens），应自适应降档重试
_reset()
G._call_once_stream = _fake_stream_factory([
    G.GlmApiError(G._KIND_FATAL, "1210", "max_tokens参数非法：限制数值范围[1,1024]"),
    "OK-clamped",
])
out = G.call_glm("k", ["glm-unknown-v"], [DUMMY], verbose=False, max_tokens=4096)
check("1210 max_tokens 超上限时自动降档重试", out == "OK-clamped" and _calls["n"] == 2,
      f"调用次数={_calls['n']}")
check("降档后 max_tokens=1024",
      [p["max_tokens"] for p in _calls["payloads"]] == [4096, 1024],
      str([p["max_tokens"] for p in _calls["payloads"]]))

# 非 max_tokens 的 1210 不该被误当成可降档，必须直接失败
_reset()
G._call_once_stream = _fake_stream_factory(
    [G.GlmApiError(G._KIND_FATAL, "1210", "temperature参数非法")])
try:
    G.call_glm("k", ["m1"], [DUMMY], verbose=False, max_tokens=4096)
    check("其它 1210 参数错误应直接失败", False)
except RuntimeError as e:
    check("其它 1210 参数错误直接失败（不误降档）", _calls["n"] == 1, str(e)[:80])

# 全链失败要抛出，不能静默返回空
_reset()
G._call_once_stream = _fake_stream_factory(
    [G.GlmApiError(G._KIND_RETRY, "1305", "过载")] * G.MAX_RETRIES)
try:
    G.call_glm("k", ["m1"], [DUMMY], verbose=False)
    check("重试耗尽应抛出", False)
except RuntimeError as e:
    check("重试耗尽后抛出（不静默）", "1305" in str(e), str(e)[:80])

# 空响应必须当成故障
_reset()
G._call_once_stream = _fake_stream_factory([""] * G.MAX_RETRIES)
try:
    G.call_glm("k", ["m1"], [DUMMY], verbose=False)
    check("空响应应视为失败", False)
except RuntimeError as e:
    check("空响应视为失败并抛出", "为空" in str(e), str(e)[:80])

print("\n== 10. 端到端（假传输层，不联网）==")


class EchoTransport:
    """把每张图的 sha1 前 8 位当成"识别结果"，并按新约定回一段真实的模型输出形状：

    正文（表格外）用行内 LaTeX；表格用原生 HTML，表格内只用 HTML + Unicode。
    """

    calls = []
    existed = []

    @staticmethod
    def _fake(url, api_key, payload, timeout):
        content = payload["messages"][1]["content"]
        imgs = [c for c in content if c["type"] == "image_url"]
        EchoTransport.calls.append(len(imgs))
        chunks = []
        for i, c in enumerate(imgs):
            b64 = c["image_url"]["url"].split(",", 1)[1]
            EchoTransport.existed.append(bool(b64))
            digest = hashlib.sha1(b64.encode()).hexdigest()[:8]
            chunks.append(
                f"<!-- page: {i + 1} -->\n"
                f"第 {i + 1} 图指纹 `{digest}`，含 H₂O 与 2Na+2H2O===2NaOH+H2↑ 及 \\(Cu^{{2+}}\\)\n\n"
                "<table><thead><tr><th>物质</th><th>化学式</th><th>方程式</th></tr></thead>"
                "<tbody><tr><td>纯碱</td><td>Na2CO3</td>"
                "<td>Na2CO3 + 2HCl = 2NaCl + H2O + CO2↑</td></tr></tbody></table>")
        return "\n".join(chunks), "stop", None


G._call_once_stream = EchoTransport._fake
conv = G.convert_pdf_to_handout(PDF, OUT, api_key="k", models=["m1"], dpi=100,
                                pages_per_request=3, verbose=False)
body = open(conv, encoding="utf-8").read()
check("输出文件已生成", os.path.exists(conv))
check("标题行正确", body.startswith("# "), body.splitlines()[0])
check("交付物里没有「第 N 页」分页标题", "## 第 " not in body and "页内容" not in body)
check("页序标记/HTML 注释未写进正文", "page:" not in body and "<!--" not in body)
check("每页正文都落盘（按指纹计数）", body.count("图指纹") == len(pages),
      f"指纹={body.count('图指纹')} 页数={len(pages)}")
check("后处理：Unicode 下标 → LaTeX", "H₂O" not in body and r"\text{H}_2\text{O}" in body)
check("后处理：=== → =", "===" not in body and "=" in body)
check("后处理：↑ → \\uparrow", "↑" not in body.split("<table>")[0] and r"\uparrow" in body)
check("后处理：\\(...\\) → $...$（不再出现 LaTeX 圆括号）",
      "\\(" not in body and "$Cu^{2+}$" in body)
check("表格以 HTML 表格形式落盘", "<table>" in body and "</table>" in body)
check("表格单元格用行内 LaTeX 化学记号",
      r"$\text{Na}_2\text{C}\text{O}_3$" in body and r"\text{C}\text{O}_2\uparrow" in body, body[-400:])
check("表格里没有 HTML 上下标 / Unicode 上下标",
      "<sub>" not in body and "<sup>" not in body
      and not re.search(r"[₀-₉₊₋⁰-⁹⁺⁻ⁿ]", body.split("<table>")[1].split("</table>")[0]))
check("表格里的 $ 定界符成对",
      body.split("<table>")[1].split("</table>")[0].count("$") % 2 == 0)
check("分批调用（3 页/批）", EchoTransport.calls[0] == 3, f"每批图数={EchoTransport.calls}")
check("每批调用时临时图片确实存在（清理发生在最后）", all(EchoTransport.existed))

print("\n== 10b. 异常路径：已完成的批次要落盘，临时目录要删净 ==")
_before = set(_glob.glob(os.path.join(tempfile.gettempdir(), "hm_glm_pages_*")))


class BoomTransport:
    n = 0

    @staticmethod
    def _fake(url, api_key, payload, timeout):
        BoomTransport.n += 1
        if BoomTransport.n == 2:
            raise G.GlmApiError(G._KIND_FATAL, "1000", "boom 身份验证失败")
        return "ok", "stop", None


G._call_once_stream = BoomTransport._fake
try:
    G.convert_pdf_to_handout(PDF, OUT + ".boom", api_key="k", models=["m1"], dpi=60,
                             pages_per_request=5, verbose=False)
    check("异常应向上抛出", False)
except RuntimeError as e:
    check("异常向上抛出（不静默吞掉）", "boom" in str(e), str(e)[:60])
_left = set(_glob.glob(os.path.join(tempfile.gettempdir(), "hm_glm_pages_*"))) - _before
check("异常路径也不留临时目录（finally 兜底）", not _left, str(sorted(_left)))
check("异常前已完成的批次仍落盘（增量写入）",
      os.path.exists(OUT + ".boom") and "ok" in open(OUT + ".boom", encoding="utf-8").read())

print("\n== 10c. --no-normalize 与 --pages 抽页模式 ==")
sel_args = pytypes.SimpleNamespace(
    input_pdf=PDF, pages_per_request=2, temperature=0.0, no_normalize=True,
    base_url=G.DEFAULT_BASE_URL, max_tokens=G.MAX_TOKENS, no_stream=False, thinking="auto",
)
img2, wd3 = G.render_pdf_pages(PDF, dpi=80)
keep = [(n, p) for n, p in img2 if (n + 1) in {1, 2, 5}]
G._call_once_stream = EchoTransport._fake
out2 = G.convert_selected_pages(keep, "_tmp_glm_offline_pages.md", "k", ["m1"], sel_args)
body2 = open(out2, encoding="utf-8").read()
check("只输出选中页", body2.count("图指纹") == 3, f"指纹={body2.count('图指纹')}")
check("页码标注不进正文", "## 第 " not in body2 and "页内容" not in body2)
check("--no-normalize 生效（保留 Unicode 下标与表格原样）", "H₂O" in body2 and "<table>" in body2)
shutil.rmtree(wd3, ignore_errors=True)

print("\n== 10d. 回归：CLI --pages 分支（曾经因 base_url 未解析而崩在 None.rstrip）==")
G._call_once_stream = EchoTransport._fake
_rc = G.main(["test1.pdf", "_tmp_glm_cli_pages.md", "--pages", "1-2", "--dpi", "60",
              "--quiet"])
_cli_body = open("_tmp_glm_cli_pages.md", encoding="utf-8").read()
check("main(--pages) 不抛异常（base_url 已解析）", _rc == 0, f"return={_rc}")
check("--pages 产出 2 页正文", _cli_body.count("图指纹") == 2, f"指纹={_cli_body.count('图指纹')}")
check("--pages 输出也没有分页标题", "## 第 " not in _cli_body)
check("--pages 默认走流式传输", G._call_once_stream is EchoTransport._fake)

print("\n== 11. 内置自检图 ==")
png, mime = G.make_probe_image()
check("PNG 签名", png[:8] == b"\x89PNG\r\n\x1a\n")
check("mime 正确", mime == "image/png")
try:
    import io as _io
    with _Image.open(_io.BytesIO(png)) as im:
        check("PNG 可被 PIL 解码且尺寸合理", im.size == (640, 160), str(im.size))
except Exception as ex:  # noqa: BLE001
    check("PNG 可被 PIL 解码", False, str(ex))
geo = G._geometric_png()
check("兜底几何 PNG 合法", geo[:8] == b"\x89PNG\r\n\x1a\n"
      and struct.unpack(">II", geo[16:24]) == (256, 64))

print("\n== 12. CLI 参数解析 ==")
ap = G.build_arg_parser()
a = ap.parse_args(["in.pdf", "out.md", "--dpi", "220", "--pages", "2-4", "--model", "m1,m2",
                   "--no-normalize", "--thinking", "disabled", "--no-stream"])
check("位置参数", a.input_pdf == "in.pdf" and a.output_md == "out.md")
check("--dpi", a.dpi == 220)
check("--pages", a.pages == "2-4")
check("--model", a.model == "m1,m2")
check("--no-normalize", a.no_normalize is True)
check("--thinking", a.thinking == "disabled")
check("--no-stream", a.no_stream is True)
check("--check 与 --check-image 存在", hasattr(ap.parse_args(["--check"]), "check_image"))
check("默认 thinking=auto", ap.parse_args([]).thinking == "auto")

print("\n== 13. 批量入口 batch_convert（假传输层，不联网、不算额度）==")
_BATCH = "_tmp_batch_selfcheck"
_BATCH_OUT = os.path.join(_BATCH, "out")
shutil.rmtree(_BATCH, ignore_errors=True)
for _sub in ("a", "b"):
    os.makedirs(os.path.join(_BATCH, _sub), exist_ok=True)
# 三个正常 PDF（其中两个同名、散在不同子目录，用来验输出名冲突消歧）
_pdf_a = os.path.join(_BATCH, "a", "课件1.pdf")
_pdf_b = os.path.join(_BATCH, "b", "课件1.pdf")
_pdf_c = os.path.join(_BATCH, "课件2.pdf")
for _p in (_pdf_a, _pdf_b, _pdf_c):
    shutil.copyfile(PDF, _p)
# 一个假的"PDF"：内容不是 PDF，渲染阶段就会炸 —— 用来验"一个文件失败不拖垮整批"
_pdf_broken = os.path.join(_BATCH, "课件10.pdf")
with open(_pdf_broken, "w", encoding="utf-8") as _f:
    _f.write("这不是一个 PDF 文件")

print("\n== 13a. 输入展开：目录 / glob / 清单 / 去重 / 自然序 ==")
_ins = B.collect_inputs([_BATCH], recursive=True, quiet=True)
_names = [os.path.basename(p) for p in _ins]
check("目录递归展开 4 个 PDF", len(_ins) == 4, str(_names))
check("自然序：课件2 排在 课件10 前面",
      _names == ["课件1.pdf", "课件1.pdf", "课件2.pdf", "课件10.pdf"], str(_names))
check("同一文件重复输入只算一次（目录给两遍 + 再点名一次）",
      len(B.collect_inputs([_BATCH, _BATCH, _pdf_a], recursive=True, quiet=True)) == 4)
_lst = os.path.join(_BATCH, "list.txt")
with open(_lst, "w", encoding="utf-8") as _f:
    _f.write(f"# 清单里的注释行\n{_pdf_c}\n\n{_pdf_b}\n")
check("--list-file 读清单（忽略注释与空行）",
      len(B.collect_inputs([], list_file=_lst, quiet=True)) == 2)

# 回归：本仓库的示例课件就叫 `[2]碳酸钠+碳酸氢钠.pdf`，名字里的 [2] 会被 glob 当字符类，
# 若"先当通配符展开、再看字面路径"，这份课件会被静默漏掉（实测踩过，已修）。
_pdf_bracket = os.path.join(_BATCH, "[2]课件3.pdf")
shutil.copyfile(PDF, _pdf_bracket)
try:
    check("名字里带 [] 的文件按字面路径处理（不被 glob 吃掉）",
          B.collect_inputs([_pdf_bracket], quiet=True) == [_pdf_bracket])
    check("（复现前提）它确实不是有效的 glob 模式", _glob.glob(_pdf_bracket) == [])
finally:
    os.remove(_pdf_bracket)
try:
    B.collect_inputs([os.path.join(_BATCH, "没有这个目录")], quiet=True)
    check("不存在的输入应报错", False)
except FileNotFoundError:
    check("不存在的输入直接报错（不静默跳过）", True)

print("\n== 13b. 输出计划：命名与同名冲突消歧 ==")
_jobs = B.plan_jobs(_ins, outdir=_BATCH_OUT, quiet=True)
check("每个输入一个任务", len(_jobs) == 4)
check("输出名冲突被消歧，4 个目标互不相同",
      len({os.path.normcase(os.path.abspath(j.dest)) for j in _jobs}) == 4,
      str([os.path.basename(j.dest) for j in _jobs]))
check("冲突的那个改用父目录名区分",
      any(os.path.basename(j.dest).startswith("b_") for j in _jobs),
      str([os.path.basename(j.dest) for j in _jobs]))
check("输出名沿用单文件契约 <名>_handout_glm.md",
      all(os.path.basename(j.dest).endswith("_handout_glm.md") for j in _jobs))

print("\n== 13c. 成品判定（半成品不许冒充成品）==")
_only_title = os.path.join(_BATCH, "只有标题.md")
with open(_only_title, "w", encoding="utf-8") as _f:
    _f.write("# 只有标题\n\n")
check("只有一级标题的文件不算成品", not B._looks_complete(_only_title))
check("有正文的文件算成品", B._looks_complete("test1_handout_glm.md"))
check("文件不存在不算成品", not B._looks_complete(os.path.join(_BATCH, "没有.md")))

print("\n== 13d. 端到端批量：.part 改名 / 失败隔离 / 报表 ==")
G._call_once_stream = EchoTransport._fake
EchoTransport.calls = []
EchoTransport.existed = []
_report = os.path.join(_BATCH, "report.json")
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    _rc = B.main([_BATCH, "-r", "-o", _BATCH_OUT, "--report", _report,
                  "--quiet", "--dpi", "80"])
_out_files = sorted(os.listdir(_BATCH_OUT))
check("坏 PDF 只让退出码=1，整批照跑完",
      _rc == 1 and _out_files.count("课件2_handout_glm.md") == 1, f"rc={_rc} 产物={_out_files}")
check("3 个正常 PDF 全部产出", len(_out_files) == 3, str(_out_files))
check("成功后不留 .part 残留（.part -> .md 改名完成）",
      not [f for f in _out_files if f.endswith(".part")], str(_out_files))
check("坏 PDF 没有产出半成品", "课件10_handout_glm.md" not in _out_files)
_body = open(os.path.join(_BATCH_OUT, "课件2_handout_glm.md"), encoding="utf-8").read()
check("批量产物与单文件同构（标题 + 正文 + HTML 表格）",
      _body.startswith("# ") and "图指纹" in _body and "<table>" in _body)
check("批量产物同样没有分页标题/页序标记",
      "## 第 " not in _body and "page:" not in _body and "<!--" not in _body)
check("批量产物同样归一化（Unicode 下标 -> LaTeX）",
      "H₂O" not in _body and r"\text{H}_2\text{O}" in _body)
import json as _json  # noqa: E402
_report_data = _json.load(open(_report, encoding="utf-8"))
check("--report 报表写出且计数正确",
      _report_data["counts"] == {"total": 4, "ok": 3, "skip": 0, "fail": 1},
      str(_report_data["counts"]))
check("报表里记了失败原因", any(r["status"] == "fail" and r["error"] for r in _report_data["results"]))

print("\n== 13e. --skip-existing 续传：已完成的文件不再调用 API ==")
_calls_done = len(EchoTransport.calls)
_buf2 = io.StringIO()
with contextlib.redirect_stdout(_buf2):
    _rc2 = B.main([_BATCH, "-r", "-o", _BATCH_OUT, "--skip-existing", "--quiet"])
check("重跑只重试失败的那个（3 个跳过、1 个仍失败）", _rc2 == 1, f"rc={_rc2}")
check("已完成的文件确实没有再发请求（跳过=不花钱）",
      len(EchoTransport.calls) == _calls_done, f"新增调用={len(EchoTransport.calls) - _calls_done}")
check("跳过路径也没把 .part 当成品", not [f for f in os.listdir(_BATCH_OUT) if f.endswith(".part")])

print("\n== 13f. --verify-only 与体检函数（复用 verify_handout，不起子进程）==")
_calls_done2 = len(EchoTransport.calls)
_buf3 = io.StringIO()
with contextlib.redirect_stdout(_buf3):
    _rc3 = B.main([_BATCH, "-r", "-o", _BATCH_OUT, "--verify-only", "--quiet"])
_v_out = _buf3.getvalue()
# 只留每份产物的结论行当诊断信息（整段 stdout 拿来当 extra 会把自检刷得看不清）
_v_lines = [ln.strip() for ln in _v_out.splitlines()
            if "[通过]" in ln or "[不合格]" in ln or "[跳过]" in ln]
_v_diag = " | ".join(_v_lines[:4]) or _v_out.strip()[:160]
check("--verify-only 一个 API 请求都不发", len(EchoTransport.calls) == _calls_done2)
check("--verify-only 逐份给出体检结论",
      _v_out.count("通过") + _v_out.count("不合格") >= 3, _v_diag)
check("没有产物的输入按「跳过」处理，不算不合格",
      "没有产物" in _v_out and "[不合格] 课件10" not in _v_out, _v_diag)
check("--verify-only 退出码反映体检结果（0=全过 / 1=有不合格）", _rc3 in (0, 1), f"rc={_rc3}")
if os.path.exists("[2]碳酸钠+碳酸氢钠_handout_glm.md"):
    _prev = os.path.join(_BATCH, "preview.html")
    _n_ok, _n_bad, _ = B.verify_md("[2]碳酸钠+碳酸氢钠_handout_glm.md", _prev)
    check("verify_md 对真实产物全部通过（静默，不刷屏）", not _n_bad and _n_ok >= 60,
          f"{_n_ok} 项通过 / 不合格={_n_bad}")
    check("--html-preview 预览页写出且自带 KaTeX",
          os.path.exists(_prev) and "katex" in open(_prev, encoding="utf-8").read().lower())
else:
    print("  （跳过：仓库里没有真实产物 [2]碳酸钠+碳酸氢钠_handout_glm.md）")

print("\n== 13g. --dry-run 不碰 API、不写文件 ==")
_before_files = set(os.listdir(_BATCH_OUT))
_calls_done3 = len(EchoTransport.calls)
_buf4 = io.StringIO()
with contextlib.redirect_stdout(_buf4):
    _rc4 = B.main([_BATCH, "-r", "-o", _BATCH_OUT, "--dry-run", "--quiet"])
check("--dry-run 退出码=0", _rc4 == 0, f"rc={_rc4}")
check("--dry-run 不发任何请求", len(EchoTransport.calls) == _calls_done3)
check("--dry-run 不写任何文件", set(os.listdir(_BATCH_OUT)) == _before_files)
check("--dry-run 预演里列出了待转文件", "课件2.pdf" in _buf4.getvalue())

print("\n== 13h. --stop-on-error：出错即停，但报表照样写出（收尾走同一条路）==")
_stop_report = os.path.join(_BATCH, "stop.json")
_buf5 = io.StringIO()
with contextlib.redirect_stdout(_buf5):
    _rc5 = B.main([_pdf_broken, "-o", _BATCH_OUT, "--stop-on-error",
                   "--report", _stop_report, "--quiet"])
check("--stop-on-error 退出码=1", _rc5 == 1, f"rc={_rc5}")
check("停批也写出了报表（中断/停批/跑完共用同一套收尾）", os.path.exists(_stop_report))
check("报表计数正确（1 失败 0 成功）",
      _json.load(open(_stop_report, encoding="utf-8"))["counts"]
      == {"total": 1, "ok": 0, "skip": 0, "fail": 1})

# 收尾：恢复被替换的传输层，清理本测试自渲染的目录与全部临时产物
G._call_once_stream = _real_stream
import gc as _gc  # noqa: E402
_gc.collect()
shutil.rmtree(workdir, ignore_errors=True)
check("测试自渲染目录已收尾", not os.path.isdir(workdir))
shutil.rmtree(_BATCH, ignore_errors=True)
check("批量自检目录已收尾（不留 _tmp_batch_selfcheck）", not os.path.isdir(_BATCH))

for _d in set(_glob.glob(os.path.join(tempfile.gettempdir(), "hm_glm_pages_*"))) - _PRE_TMP:
    shutil.rmtree(_d, ignore_errors=True)
_still = set(_glob.glob(os.path.join(tempfile.gettempdir(), "hm_glm_pages_*"))) - _PRE_TMP
check("自检不留渲染临时目录（差集为空）", not _still, str(sorted(_still)))

for _leftover in ("_tmp_glm_offline_out.md", "_tmp_glm_offline_out.md.boom",
                  "_tmp_glm_offline_pages.md", "_tmp_glm_cli_pages.md", "_tmp_glm_dummy.jpg"):
    try:
        if os.path.exists(_leftover):
            os.remove(_leftover)
    except OSError:
        pass
_leaked = [f for f in ("_tmp_glm_offline_out.md", "_tmp_glm_offline_out.md.boom",
                       "_tmp_glm_offline_pages.md", "_tmp_glm_cli_pages.md", "_tmp_glm_dummy.jpg")
           if os.path.exists(f)]
check("自检不留临时产物", not _leaked, str(_leaked))
check("未误删待转换的 PDF", os.path.exists(PDF))

print("\n" + "=" * 60)
print(("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
sys.exit(1 if FAILS else 0)
