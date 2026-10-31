# -*- coding: utf-8 -*-
"""PDF -> 化学教案 转换器（Google Gemini 视觉模型 API 版）。

这是 `handout_maker.py` 的姊妹脚本：**只有「调用哪个 API」这一处不同**，其余链路
（渲染 -> 看图写 Markdown -> normalize 归一化 -> 增量落盘 -> 连通性自检）与提示词、
后处理、输出契约**一字不差**。两条脚本共用同一份 `normalize.py` 与同一份 `PROMPT`，
所以单文件跑和批量跑、GLM 跑和 Gemini 跑，产物的格式规范完全一致。

    PDF ──PyMuPDF 渲染──> 每页 JPEG ──Gemini 视觉模型──> Markdown 正文
        ──normalize 归一化──> 一份连续的化学讲义 Markdown

输入输出契约（与 handout_maker.py 相同，由后处理与提示词共同保证）：
    * 表格结构：一律原生 HTML（<table>/<tr>/<th>/<td>），不用 Markdown 管道表；
    * 化学记号：**表格内外都只用行内 LaTeX**，同一套写法 --
      表格外 $\\text{H}_2\\text{O}$，表格内也是 <td>$\\text{Na}_2\\text{CO}_3$</td>；
    * 交付物里**没有「第 N 页」这类分页标题**：页码只是流水线的中间量。

为什么用标准库 urllib、而不是照抄 notebook 里的 `google.genai` SDK？
    `verify_gemini_API.ipynb` 给出的有效信息只有四样：API Key、模型名、代理端口、
    请求的形状。但**本机的两个 conda 环境各缺一半**（实测，与 GLM 版同一套环境约束）：

        conda activate Handout-Maker2   # Python 3.12：有 pymupdf/Pillow，缺 google-genai
        conda activate ai-assistant     # Python 3.11：有 google-genai，缺 pymupdf/Pillow

    Gemini 的 REST 网关本身就是标准 HTTP 接口（`:generateContent`），用 urllib 直接 POST
    即可，wire 格式与 notebook 里 `client.models.generate_content(...)` 一一对应。这样本脚本
    在 **Handout-Maker2 环境里开箱即用**（它本来就装着 pymupdf+Pillow），不必再装第 4 个包。

用法：
    python handout_maker2.py                       # 转 test1.pdf
    python handout_maker2.py 输入.pdf 输出.md
    python handout_maker2.py --check               # 只测 Key/网络/代理/看图能力
    python handout_maker2.py 输入.pdf --pages 1-3  # 只转前 3 页（试参数用）

API Key 读取顺序：命令行 --api-key > 环境变量 GEMINI_API_KEY / GOOGLE_API_KEY > 脚本内兜底值
（取自 verify_gemini_API.ipynb）。

⚠️ 本脚本特有的坑（都是 notebook / Gemini REST 的实测或文档语义，不是抄文档）：
    1. **必须走代理**：Gemini 网关国内直连会被节点封锁/风控（notebook 里已经踩过），
       默认代理 `http://127.0.0.1:7897`（notebook 里写死的本地 Clash 端口）。
       代理解析顺序：--proxy > GEMINI_PROXY / HTTPS_PROXY / https_proxy > 内置默认；
       传 `--proxy off` 显式关闭（直连）。
    2. **模型名在 URL 路径里，不在请求体里**：`.../models/{model}:generateContent`。
       notebook 实测 `gemini-2.0-flash` 已下线（404 NOT_FOUND），官方提示改用
       `gemini-3.8-flash`；默认降级链见 DEFAULT_MODELS。
    3. **流式（SSE）是「累计语义」，不是 delta**：每一块都是「到当前为止的完整文本」，
       与 OpenAI/GLM 的 delta 不同 —— 所以流里是**覆盖**而不是**拼接**，否则正文会重复。
    4. **finishReason 用大写枚举**：STOP=正常、MAX_TOKENS=截断（保留已生成部分）、
       SAFETY / RECITATION / BLOCKLIST / PROHIBITED_CONTENT / SPII = 被安全拦截（致命）。
    5. Gemini 的思维链（thinking）不是请求字段，而是独立模型变体（如 `-thinking` 后缀），
       所以本脚本接受 `thinking` 参数但忽略它（仅为与 batch.py 的接口对齐）。
"""

import argparse
import base64
import io
import json
import os
import random
import re
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

from normalize import normalize_handout_markdown

# ---------------------------------------------------------------------------
# 1. 常量与默认配置
# ---------------------------------------------------------------------------
# 取自 verify_gemini_API.ipynb（Key 是 notebook 里能通过鉴权的那把；代理端口也是 notebook 写死的）
DEFAULT_API_KEY = "AIzaSyDI8A9rxuZ5U--R8fZ1UzKB5_dsT2_B5Bc"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_PROXY = "http://127.0.0.1:7897"

# 模型可用性（notebook 实测 / 官方报错提示）：
#   gemini-2.0-flash -> 404「no longer available，请改用 gemini-3.8-flash」：已下线。
#   gemini-3.8-flash -> notebook 报错里官方提示的最新模型名（本账号该走的现行标准名）。
#   gemini-2.5-flash  -> 兜底；若 3.8 不可用/无权限会自动降级到它。
# 所以默认链 = 新模型优先、旧模型兜底；--model 可自行改写整条降级链。
DEFAULT_MODELS = ("gemini-3.8-flash", "gemini-2.5-flash")

# 渲染 DPI。与 GLM 版保持一致：150 DPI 下 16:9 幻灯片约 2000x1125 / 84KB，
# 正文与上下标都清晰，且远低于 Gemini 的单图上限。
RENDER_DPI = int(os.environ.get("HM_DPI", "150"))

# 每次请求塞几页图。默认 1 页：页序最干净、重试粒度最细。
PAGES_PER_REQUEST = int(os.environ.get("HM_PAGES_PER_REQUEST", "1"))

# 图片上限（比 Gemini 官方 20MB/请求 更保守，与 GLM 版同一套自适应收缩逻辑）。
MAX_IMAGE_BYTES = int(os.environ.get("HM_MAX_IMAGE_BYTES", str(4 * 1024 * 1024)))
MAX_IMAGE_EDGE = int(os.environ.get("HM_MAX_IMAGE_EDGE", "6000"))

# 重试与退避。Gemini 的限流同样有并发/配额维度，坚持串行逐页调用 + 指数退避 + 抖动。
MAX_RETRIES = int(os.environ.get("HM_MAX_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.environ.get("HM_RETRY_BASE_DELAY", "5"))

# 单次请求最长等待（秒）。视觉模型 + 代理较慢，给足 300s。
REQUEST_TIMEOUT = float(os.environ.get("HM_REQUEST_TIMEOUT", "300"))

# 输出 token 上限。Gemini 2.x 系列输出上限都 ≥8192，4096 有余量；无需按模型夹紧。
MAX_TOKENS = int(os.environ.get("HM_MAX_TOKENS", "4096"))


# ---------------------------------------------------------------------------
# 2. 提示词
#
# 与 handout_maker.py 的 PROMPT **逐字相同**（钉死三件事：①表格原生 HTML；②化学记号
# 表格内外都是行内 LaTeX；③等号就是等号）。后处理 normalize 会把这套约定再兜一遍，
# 提示词与后处理是「双保险」关系。任何一处改动都要两脚本同步，否则会破坏双引擎产物一致。
# ---------------------------------------------------------------------------
PROMPT = r"""你是一个专业的化学讲义整理助手。请把这张化学课件图片的内容转写成结构化的 Markdown 讲义正文。

【总则】
1. 提取全部文字：标题、正文、列表、表格，保留原始层级与顺序。
2. 只输出 Markdown 正文本身：不要解释、不要寒暄、不要总结、不要代码围栏，
   不要输出「第 N 页」这类页码或分页标题，也不要写 HTML 注释（页序标记除外，见批处理说明）。

【表格：结构用原生 HTML 标签】
3. 表格必须写成 <table> / <thead> / <tr> / <th> / <tbody> / <td>；
   不要用 Markdown 管道表（| a | b |），不要写 border/style/class 等属性。
4. 每行 <td> 的个数必须与表头 <th> 的个数一致，缺失的补空单元格 <td></td>；
   单元格内换行用 <br>，不要在单元格里嵌套表格。

【化学记号：表格内外都是行内 LaTeX，同一套写法】
5. 化学式、化学方程式、公式一律用 $...$ 包裹，元素符号写 \text{}，下标用 _、上标用 ^。
   表格外的正文：$\text{H}_2\text{O}$、$\text{Fe}^{3+}$
   表格单元格里：<td>$\text{Na}_2\text{CO}_3$</td>
   同一条铁律：**凡是有化学含义的式子，一律 $...$ 包裹，表格内也不许只写成
   Na2CO3 / Na<sub>2</sub>CO<sub>3</sub> 这种不带 LaTeX 的裸写法**；
   表格的 HTML 标签（<table>/<tr>/<td>）只负责搭结构，不负责化学记号。
   错误写法：<td>Na<sub>2</sub>CO<sub>3</sub></td>、<td>Na2CO3</td>、<td>$\ce{Na2CO3}$</td>
   正确写法：<td>$\text{Na}_2\text{CO}_3$</td>
6. 单元格里的 <br>、<b> 之类的排版标签可以保留，但化学记号仍然是 LaTeX：
   <td>受热分解<br>$2\text{Na}\text{H}\text{C}\text{O}_3 \overset{\Delta}{=} \text{Na}_2\text{C}\text{O}_3 + \text{H}_2\text{O} + \text{C}\text{O}_2\uparrow$</td>
7. 化学方程式必须使用等号 =（包括双等号），**绝对禁止用 -> 或 → 代替等号**。
8. 可逆反应用 \rightleftharpoons（也可直接写 ⇌），
   如 $\text{C}\text{O}_2 + \text{H}_2\text{O} \rightleftharpoons \text{H}_2\text{C}\text{O}_3$。
9. 反应条件书写规范（等号/箭头的上方与下方）：
   - 无条件或简单方程式：直接用 `=`，如 $4\text{Na} + \text{O}_2 = 2\text{Na}_2\text{O}$
   - 只有上方条件：使用 `\overset{条件}{=}`，如加热 $2\text{Na} + \text{O}_2 \overset{\Delta}{=} \text{Na}_2\text{O}_2$
   - 同时有上下方条件（如催化剂和加热）：
     * 使用双等号：`\overset{上方条件}{\underset{下方条件}{=}}` 或 `\xlongequal[\text{下方条件}]{\text{上方条件}}`
     * 示例：$2\text{SO}_2 + \text{O}_2 \xlongequal[\Delta]{\text{V}_2\text{O}_5} 2\text{SO}_3$
     * 示例：$2\text{K}\text{Cl}\text{O}_3 \overset{\text{Mn}\text{O}_2}{\underset{\Delta}{=}} 2\text{K}\text{Cl} + 3\text{O}_2\uparrow$
   - 箭头形式条件（若图片原文明确绘制了箭头）：`\xrightarrow[\text{下方条件}]{\text{上方条件}}`
10. → 只用于「转化关系 / 合成路线 / 流程图」这类真正的箭头，不得当作方程式等号。

【图示与排版】
11. 遇到图表、装置图、示意图，在对应位置单独一行写 `>[图示: 内容描述]`。
12. 课件里的标题按层级写成 Markdown 标题（#/##/###），正文分段，列表用 - 或 1.。
13. 不要编造图片里没有的内容；看不清的字符用 □ 占位。"""

# 一批多页时追加的页序约定（与 handout_maker.py 逐字相同）。
MULTI_IMAGE_PROMPT = PROMPT + """
14. 本次输入包含多张课件图片，请严格按图片顺序逐页转写，页与页之间用空行隔开；
    在每页内容之前单独输出一行 `<!-- page: N -->`（N 为该图在本批中的序号，从 1 开始），
    除这一行页序标记外不要添加任何其他注释或页码。"""

# 自检用提示词：只要能读出图里的字符就说明 Key/网络/代理/看图四件事都通了。
PROBE_PROMPT = "图中有哪些字符？原样列出，不要解释，不要换行。"

# 系统指令：与 GLM 版的 system 消息同义，落到 Gemini 的 systemInstruction 字段。
SYSTEM_PROMPT = "你是化学课件转写引擎，只输出 Markdown 正文本身，不输出任何解释、寒暄、代码围栏或收尾语。"


# ---------------------------------------------------------------------------
# 3. API Key / 网关 / 代理 / 请求体
# ---------------------------------------------------------------------------
def resolve_api_key(explicit=None):
    """API Key 解析：命令行 > 环境变量 > 脚本内兜底值。"""
    if explicit:
        return explicit
    for env in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY"):
        if os.environ.get(env):
            return os.environ[env]
    return DEFAULT_API_KEY


def resolve_base_url(explicit=None):
    """网关地址。notebook 对应 https://generativelanguage.googleapis.com/v1beta。"""
    return explicit or os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE_URL


def resolve_proxy(explicit=None):
    """代理地址解析：--proxy > 环境变量 > 内置默认；空串/off/none 显式关闭（直连）。

    Gemini 网关国内直连会被节点封锁/风控（见 verify_gemini_API.ipynb），必须走本地代理
    （默认 127.0.0.1:7897，即 notebook 里写死的端口）。
    """
    if explicit is not None:
        v = str(explicit).strip()
        if v.lower() in ("", "off", "none", "no", "false"):
            return None
        return v
    for env in ("GEMINI_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        if os.environ.get(env):
            return os.environ[env]
    return DEFAULT_PROXY


def parse_models(model_arg):
    """把 --model 参数（可逗号分隔）解析成降级链。"""
    if not model_arg:
        return list(DEFAULT_MODELS)
    models = [m.strip() for m in str(model_arg).split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def model_max_tokens(model, requested):
    """Gemini 各模型输出上限都 ≥8192，4096 默认值无需夹紧；保留函数仅为接口对齐。"""
    return int(requested)


def _b64_encode(raw_bytes):
    return base64.b64encode(raw_bytes).decode("ascii")


def _image_b64(image_path):
    """本地 JPEG -> 纯 base64（Gemini inlineData.data 只要裸 base64，不要 data URL 前缀）。"""
    with open(image_path, "rb") as f:
        return _b64_encode(f.read())


def build_payload(model, image_paths, prompt, temperature=0.0, max_tokens=MAX_TOKENS,
                  stream=True, thinking=None):
    """构造 generateContent 请求体（模型名在 URL 路径里，不在请求体里）。

    REST wire 格式用 camelCase（inlineData/mimeType/maxOutputTokens），与 notebook 里
    genai.Client(...).models.generate_content(model=..., contents=[...]) 一一对应。
    `stream` / `thinking` 仅保留签名兼容：Gemini 的流式走独立端点、thinking 是模型变体，
    都不是请求体字段。
    """
    parts = [{"inlineData": {"mimeType": "image/jpeg", "data": _image_b64(p)}}
             for p in image_paths]
    parts.append({"text": prompt})
    return {
        "contents": [{"role": "user", "parts": parts}],
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "generationConfig": {
            "temperature": float(temperature),
            "maxOutputTokens": model_max_tokens(model, max_tokens),
        },
    }


# ---------------------------------------------------------------------------
# 4. 错误分类（Gemini REST 的错误形状：error.code=HTTP 码、error.status=字符串）
# ---------------------------------------------------------------------------
# 可重试：配额/速率限制、服务端过载/抖动 —— 等一会儿再来有救。
_RETRY_STATUS = {"RESOURCE_EXHAUSTED", "INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED",
                 "RATE_LIMIT_EXCEEDED", "CANCELLED"}
# 换模型：这一把 Key 在**这个模型**上不存在/无权限/已下线 —— 换降级链里的下一个模型才有救。
_NEXT_MODEL_STATUS = {"NOT_FOUND", "PERMISSION_DENIED", "FAILED_PRECONDITION"}
# 致命：参数写错、鉴权失败 —— 重试和换模型都救不回来。
_FATAL_STATUS = {"INVALID_ARGUMENT", "UNAUTHENTICATED", "OUT_OF_RANGE", "PAYLOAD_TOO_LARGE"}

_KIND_RETRY = "retry"
_KIND_NEXT_MODEL = "next_model"
_KIND_FATAL = "fatal"


class GeminiApiError(RuntimeError):
    """带分类信息的 API 错误，让调用方能区分「重试 / 换模型 / 直接失败」。"""

    def __init__(self, kind, code, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.status = status

    def __str__(self):
        tag = f"HTTP {self.status} " if self.status else ""
        return f"{tag}[{self.code or '?'}] {super().__str__()}"


def _classify(status, code, message, gstatus):
    """把 (HTTP 状态码, error.code, error.status, error.message) 归到 retry/next_model/fatal。"""
    gs = (gstatus or "").strip().upper()
    if gs in _RETRY_STATUS:
        return _KIND_RETRY
    if gs in _NEXT_MODEL_STATUS:
        return _KIND_NEXT_MODEL
    if gs in _FATAL_STATUS:
        return _KIND_FATAL

    # 没有 error.status（或不在表里）时退化成按 HTTP 状态码 + 文本判断。
    text = (message or "").lower()
    if status in (408, 429, 500, 502, 503, 504):
        return _KIND_RETRY
    if status in (400, 401, 413):
        return _KIND_FATAL
    if status == 404:
        return _KIND_NEXT_MODEL
    if status == 403:
        # 403 多为「该模型无访问权限」，换模型有救；纯鉴权失败一般是 401/400。
        return _KIND_NEXT_MODEL
    if any(k in text for k in ("overloaded", "rate limit", "quota", "exhausted",
                               "too many", "try again", "capacity")):
        return _KIND_RETRY
    if any(k in text for k in ("no longer available", "not found", "does not exist",
                               "deprecated", "unsupported", "not supported")):
        return _KIND_NEXT_MODEL
    return _KIND_FATAL


def _error_from_body(body, status=None):
    """从响应体里抽出 error.status / error.code / message，构造 GeminiApiError。"""
    code = status
    gstatus = None
    message = (body or "").strip()
    try:
        obj = json.loads(body)
        err = obj.get("error")
        if isinstance(err, dict):
            code = err.get("code") or code
            gstatus = err.get("status")
            message = err.get("message") or message
    except (ValueError, TypeError):
        pass
    message = re.sub(r"\s+", " ", str(message))[:300]
    display_code = gstatus or (str(code) if code is not None else None)
    return GeminiApiError(_classify(status, code, message, gstatus), display_code, message, status)


def _error_from_exception(exc):
    """网络层异常（超时/连接被重置/DNS 失败/代理不通）一律按可重试处理。"""
    return GeminiApiError(_KIND_RETRY, None, re.sub(r"\s+", " ", str(exc))[:300])


def _is_retryable(exc):
    """兼容命名习惯：这个异常值得重试吗？"""
    if isinstance(exc, GeminiApiError):
        return exc.kind == _KIND_RETRY
    return True


# ---------------------------------------------------------------------------
# 5. 响应解析
# ---------------------------------------------------------------------------
def _extract_response(obj):
    """从一段 generateContent 响应里抽出 (文本, finishReason, usageMetadata)。"""
    candidates = obj.get("candidates") or []
    text, finish_reason = "", None
    if candidates:
        c = candidates[0] or {}
        parts = (c.get("content") or {}).get("parts") or []
        text = "".join((p or {}).get("text") or "" for p in parts if isinstance(p, dict))
        finish_reason = c.get("finishReason")
    return text, finish_reason, obj.get("usageMetadata")


def _finish_reason_error(reason):
    """把 finishReason 的异常值翻译成异常（大写枚举；MAX_TOKENS=截断不算失败）。"""
    reason = (reason or "").strip().upper()
    if reason in ("", "STOP", "FINISH_REASON_UNSPECIFIED", "MAX_TOKENS"):
        return None
    # SAFETY / RECITATION / BLOCKLIST / PROHIBITED_CONTENT / SPII / LANGUAGE / OTHER
    return GeminiApiError(_KIND_FATAL, reason,
                          f"内容被拦截或异常结束（finishReason={reason}）")


# ---------------------------------------------------------------------------
# 6. 一次（单页或多页）识别：串行 + 重试 + 模型降级
# ---------------------------------------------------------------------------
def _build_opener(proxy):
    """按代理地址构建 opener：有代理走 http/https 代理；None 显式禁用（忽略系统/环境代理）。"""
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open_request(url, api_key, payload, timeout, proxy):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    opener = _build_opener(proxy)
    return opener.open(req, timeout=timeout)


def _call_once_nonstream(url, api_key, payload, timeout, proxy):
    """非流式：一次性拿完整 JSON。"""
    try:
        with _open_request(url, api_key, payload, timeout, proxy) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise _error_from_body(e.read().decode("utf-8", "replace"), e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _error_from_exception(e) from None

    try:
        data = json.loads(raw)
    except ValueError:
        raise GeminiApiError(_KIND_RETRY, None, f"响应不是合法 JSON：{raw[:200]}") from None
    if isinstance(data.get("error"), dict):
        raise _error_from_body(raw)
    if not (data.get("candidates") or []):
        raise GeminiApiError(_KIND_RETRY, None, f"响应缺少 candidates：{raw[:200]}")
    text, finish_reason, usage = _extract_response(data)
    err = _finish_reason_error(finish_reason)
    if err is not None:
        raise err
    return text, finish_reason, usage


def _call_once_stream(url, api_key, payload, timeout, proxy):
    """流式：逐块收候选文本。

    ⚠️ Gemini 的 SSE 是「累计语义」—— 每一块的 text 都是「到当前为止的完整文本」，
    与 OpenAI/GLM 的 delta 不同。所以这里**覆盖**（保留最新一块），**绝不拼接**；
    否则正文会被重复写两遍、三遍。最后一块通常带上 finishReason 与 usageMetadata。
    """
    text, finish_reason, usage = "", None, None
    try:
        with _open_request(url, api_key, payload, timeout, proxy) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):     # 空行 / SSE 心跳注释
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                              # 非 JSON 的行直接跳过
                if isinstance(obj.get("error"), dict):
                    raise _error_from_body(json.dumps(obj, ensure_ascii=False))
                t, fr, u = _extract_response(obj)
                if t:
                    text = t                              # 累计语义：直接覆盖
                if fr:
                    finish_reason = fr
                if u:
                    usage = u
    except urllib.error.HTTPError as e:
        raise _error_from_body(e.read().decode("utf-8", "replace"), e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _error_from_exception(e) from None

    err = _finish_reason_error(finish_reason)
    if err is not None:
        raise err
    return text, finish_reason, usage


def call_gemini(api_key, models, image_paths, base_url=DEFAULT_BASE_URL, temperature=0.0,
                max_tokens=MAX_TOKENS, stream=True, thinking=None, proxy=None, verbose=True):
    """调用 Gemini 完成一次（单页或多页）识别，返回 Markdown 文本。

    * 自动重试：限流/过载/服务端抖动走指数退避 + 抖动；
    * 自动降级：模型不存在/无权限/已下线时切到 models 链里的下一个模型；
    * `proxy` 传的是**已解析好**的代理地址（字符串或 None），本函数不再二次解析。
    """
    multi = len(image_paths) > 1
    prompt = MULTI_IMAGE_PROMPT if multi else PROMPT

    last_err = None
    for model in models:
        payload = build_payload(model, image_paths, prompt, temperature=temperature,
                                max_tokens=max_tokens, stream=stream, thinking=thinking)
        endpoint = "streamGenerateContent?alt=sse" if stream else "generateContent"
        url = base_url.rstrip("/") + f"/models/{model}:{endpoint}"
        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                if verbose:
                    print(f"  -> {model}（{len(image_paths)} 张图，maxOutputTokens="
                          f"{payload['generationConfig']['maxOutputTokens']}，第 {attempt} 次尝试）",
                          flush=True)
                call = _call_once_stream if stream else _call_once_nonstream
                text, finish_reason, usage = call(url, api_key, payload, REQUEST_TIMEOUT, proxy)
                if not text.strip():
                    raise GeminiApiError(_KIND_RETRY, None,
                                         f"响应为空（finishReason={finish_reason}）")
                if finish_reason == "MAX_TOKENS":
                    print(f"  !! 注意：本页输出触到 maxOutputTokens="
                          f"{payload['generationConfig']['maxOutputTokens']} 被截断"
                          f"（finishReason=MAX_TOKENS），已保留已生成的部分", flush=True)
                if verbose and usage:
                    print(f"     ok: {len(text)} 字, usage={usage}", flush=True)
                return text
            except GeminiApiError as e:
                last_err = e
                if e.kind == _KIND_RETRY:
                    if attempt < MAX_RETRIES:
                        delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        delay += delay * random.uniform(0, 0.25)  # 抖动，避免同步重试
                        print(f"  !! 第 {attempt} 次失败（{e}），{delay:.1f}s 后重试", flush=True)
                        time.sleep(delay)
                        continue
                    print(f"  !! {model} 重试 {MAX_RETRIES} 次仍失败，尝试下一个模型", flush=True)
                    break
                if e.kind == _KIND_NEXT_MODEL:
                    print(f"  !! {model} 不可用（{e}），尝试下一个模型", flush=True)
                    break
                raise RuntimeError(f"{model} 调用失败（不可重试）: {e}") from e

    raise RuntimeError(f"所有模型均调用失败，最后一个错误: {last_err}")


# ---------------------------------------------------------------------------
# 7. PDF 页面转 JPEG（带 4MB / 6000px 双上限的自适应收缩）
# ---------------------------------------------------------------------------
def render_pdf_pages(pdf_path, dpi=RENDER_DPI, workdir=None, max_image_bytes=MAX_IMAGE_BYTES,
                     max_edge=MAX_IMAGE_EDGE):
    """把 PDF 每页渲染成 JPEG，返回 ([(page_index, jpg_path), ...], workdir)。

    与 handout_maker.py 完全相同的实现，仅临时目录前缀换成 hm_gemini_pages_。
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"找不到 PDF 文件: {pdf_path}")

    try:
        import pymupdf  # PyMuPDF >= 1.24 的新名字
    except ImportError:  # 老版本只有 fitz 别名
        import fitz as pymupdf

    from PIL import Image

    doc = pymupdf.open(pdf_path)
    workdir = workdir or tempfile.mkdtemp(prefix="hm_gemini_pages_")
    pages = []
    try:
        for page_num in range(len(doc)):
            pix = doc[page_num].get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            if max(img.size) > max_edge:
                ratio = max_edge / float(max(img.size))
                img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                                 Image.LANCZOS)

            scale = 1.0
            quality = 90
            while True:
                buf = io.BytesIO()
                out = img
                if scale < 1.0:
                    out = img.resize(
                        (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                        Image.LANCZOS,
                    )
                out.save(buf, format="JPEG", quality=quality, optimize=True)
                size = buf.tell()
                if size <= max_image_bytes or (quality <= 60 and scale <= 0.5):
                    break
                if quality > 60:
                    quality -= 15
                else:
                    scale *= 0.75

            img_path = os.path.join(workdir, f"page_{page_num}.jpg")
            with open(img_path, "wb") as f:
                f.write(buf.getvalue())
            pages.append((page_num, img_path))
            print(f"  渲染第 {page_num + 1} 页: {pix.width}x{pix.height} -> "
                  f"{size / 1024:.0f}KB (q{quality}, scale={scale:.2f})", flush=True)
    finally:
        doc.close()
    return pages, workdir


# ---------------------------------------------------------------------------
# 8. 按页/按批组织与页序还原
# ---------------------------------------------------------------------------
_PAGE_MARK_RE = re.compile(r"(?m)^[ \t]*(?:<!--\s*page\s*:\s*(\d+)\s*-->|#{1,6}\s*第\s*(\d+)\s*页[^\n]*)$")


def split_by_page_marks(text):
    """把一批多页的输出按 `<!-- page: N -->` 切回单页，返回 [(页码或 None, 正文), ...]。

    模型不总是乖乖打标记：**一个标记都没有**时退化为「整批作为一块返回」，
    绝不猜页序。标记本身只用于切分，剥掉之后不会出现在交付物里。
    """
    marks = list(_PAGE_MARK_RE.finditer(text))
    if len(marks) < 2:
        return [(None, text)]

    blocks = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        num = m.group(1) or m.group(2)
        try:
            page_no = int(num)
        except (TypeError, ValueError):
            page_no = None
        blocks.append((page_no, text[start:end].strip()))
    return [b for b in blocks if b[1]] or [(None, text)]


def _assemble_blocks(text, batch, normalize):
    """把一批的模型输出切成若干块正文，返回 [正文, ...]（每块各自归一化）。

    刻意不产出「## 第 N 页内容」这类分页标题，也不产出 `---` 分隔线。
    ⚠️ 顺序很关键：**先切页、再逐块归一化**（normalize 会把 `->` 还原成等号，
    页序标记里的 `-->` 会被破坏，所以绝不能先归一化再切页）。
    """
    if len(batch) == 1:
        bodies = [text]
    else:
        bodies = [body for _page_no, body in split_by_page_marks(text)]

    return [normalize_handout_markdown(b).strip() if normalize else b.strip()
            for b in bodies if b and b.strip()]


def _write_blocks(f, bodies):
    """把各块正文顺次写入：块间留一个空行，正文连续、无分页标题。"""
    for body in bodies:
        if body.strip():
            f.write(body.strip() + "\n\n")


# ---------------------------------------------------------------------------
# 9. 主流程
# ---------------------------------------------------------------------------
def convert_pdf_to_handout(
    pdf_path,
    output_md_path,
    api_key=None,
    base_url=DEFAULT_BASE_URL,
    models=None,
    dpi=RENDER_DPI,
    pages_per_request=PAGES_PER_REQUEST,
    normalize=True,
    temperature=0.0,
    max_tokens=MAX_TOKENS,
    stream=True,
    thinking=None,
    proxy=None,
    verbose=True,
):
    """PDF -> Markdown 讲义（Gemini 引擎的唯一入口函数，签名与 GLM 版对齐 + proxy）。"""
    api_key = resolve_api_key(api_key)
    models = models or list(DEFAULT_MODELS)
    pages_per_request = max(1, int(pages_per_request))
    proxy = resolve_proxy(proxy)

    images, workdir = render_pdf_pages(pdf_path, dpi=dpi)
    total = len(images)

    try:
        with open(output_md_path, "w", encoding="utf-8") as f:
            title = os.path.splitext(os.path.basename(pdf_path))[0]
            f.write(f"# {title}\n\n")
            f.flush()

            for start in range(0, total, pages_per_request):
                batch = images[start:start + pages_per_request]
                first, last = batch[0][0] + 1, batch[-1][0] + 1
                label = str(first) if first == last else f"{first}-{last}"
                print(f"正在处理第 {label} 页（共 {total} 页）...", flush=True)

                text = call_gemini(
                    api_key, models, [p for _, p in batch], base_url=base_url,
                    temperature=temperature, max_tokens=max_tokens, stream=stream,
                    thinking=thinking, proxy=proxy, verbose=verbose,
                )
                _write_blocks(f, _assemble_blocks(text, batch, normalize))
                f.flush()

        print(f"解析完成！已保存为: {output_md_path}", flush=True)
    finally:
        for _, img_path in images:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

    return output_md_path


def convert_selected_pages(images, output_md, api_key, models, args):
    """--pages 模式：把已渲染的指定页按批送模型并落盘（与主流程同一套切页/归一化逻辑）。"""
    batch_size = max(1, int(args.pages_per_request))
    normalize = not args.no_normalize
    proxy = resolve_proxy(getattr(args, "proxy", None))
    with open(output_md, "w", encoding="utf-8") as f:
        title = os.path.splitext(os.path.basename(args.input_pdf))[0]
        f.write(f"# {title}\n\n")
        f.flush()
        for start in range(0, len(images), batch_size):
            batch = images[start:start + batch_size]
            first, last = batch[0][0] + 1, batch[-1][0] + 1
            label = str(first) if first == last else f"{first}-{last}"
            print(f"正在处理第 {label} 页...", flush=True)
            text = call_gemini(api_key, models, [p for _, p in batch],
                               base_url=resolve_base_url(args.base_url),
                               temperature=args.temperature, max_tokens=args.max_tokens,
                               stream=not args.no_stream, thinking=args.thinking,
                               proxy=proxy)
            _write_blocks(f, _assemble_blocks(text, batch, normalize))
            f.flush()
    print(f"解析完成！已保存为: {output_md}", flush=True)
    return output_md


# ---------------------------------------------------------------------------
# 10. 连通性自检（替代 notebook 里手敲的那段试跑）
# ---------------------------------------------------------------------------
def _png_chunk(tag, payload):
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _geometric_png(width=256, height=64):
    """无 Pillow 时的兜底测试图：标准库现场生成（黑边 + 对角线），不依赖任何三方包。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            border = x < 3 or y < 3 or x >= width - 3 or y >= height - 3
            diag = abs(x * height - y * width) < height * 2
            rows += b"\x00\x00\x00" if (border or diag) else b"\xff\xff\xff"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _png_chunk(b"IEND", b""))


PROBE_TEXT = "H2O  NaCl  Fe3+  Na2CO3"
PROBE_EXPECT = ("h2o", "nacl")


def make_probe_image(text=PROBE_TEXT):
    """生成一张白底黑字的测试图，返回 (PNG 字节, mime)。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return _geometric_png(), "image/png"

    img = Image.new("RGB", (640, 160), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=34)     # Pillow >= 10
    except TypeError:
        font = ImageFont.load_default()
    try:
        draw.text((18, 30), text, fill="black", font=font)
    except Exception:  # noqa: BLE001
        draw.text((18, 60), text, fill="black")
    draw.rectangle([2, 2, 637, 157], outline="black", width=3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _check_one_model(api_key, model, data, mime, base_url, stream, timeout=120, proxy=None):
    """跑一次最小请求，成功返回 (True, 文本)，失败返回 (False, 错误摘要)。"""
    endpoint = "streamGenerateContent?alt=sse" if stream else "generateContent"
    url = base_url.rstrip("/") + f"/models/{model}:{endpoint}"
    payload = {
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": _b64_encode(data)}},
            {"text": PROBE_PROMPT},
        ]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": model_max_tokens(model, 256)},
    }
    try:
        call = _call_once_stream if stream else _call_once_nonstream
        text, _finish, _usage = call(url, api_key, payload, timeout, proxy)
        return True, re.sub(r"\s+", " ", text).strip()
    except GeminiApiError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, re.sub(r"\s+", " ", str(e))[:300]


def check_api(api_key=None, base_url=None, models=None, image_path=None, stream=True, proxy=None):
    """连通性自检：一次验证 Key / 网络 / 代理 / 模型可用性 / 看图能力这五件事。"""
    api_key = resolve_api_key(api_key)
    base_url = resolve_base_url(base_url)
    models = models or list(DEFAULT_MODELS)
    proxy = resolve_proxy(proxy)

    print(f"网关: {base_url}")
    print(f"API Key: ...{api_key[-6:]}（共 {len(api_key)} 字符）")
    print(f"代理: {proxy or '直连（未启用代理）'}")

    if image_path:
        with open(image_path, "rb") as f:
            data = f.read()
        mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
        print(f"测试图: {image_path}（{len(data) / 1024:.1f}KB, {mime}）")
        expect = ()
    else:
        data, mime = make_probe_image()
        print(f"测试图: 内置生成 {mime}（{len(data)} 字节，图内文字：{PROBE_TEXT}）")
        expect = PROBE_EXPECT

    ok = False
    recognized = False
    for model in models:
        print(f"-- 测试 {model} ...", flush=True)
        success, detail = _check_one_model(api_key, model, data, mime, base_url, stream, proxy=proxy)
        print(f"   {'[OK]  ' + detail if success else '[FAIL] ' + detail}", flush=True)
        ok = ok or success
        if success and expect:
            hit = any(e in detail.lower() for e in expect)
            recognized = recognized or hit
            print(f"   {'[识别通过] 读出了测试图里的字符' if hit else '[识别存疑] 没读全期望字符'}",
                  flush=True)

    if ok:
        print("\n自检通过：网关 / API Key / 代理 / 看图能力都正常，可以开始转换。")
        if expect and not recognized:
            print("（但内置测试图的字符没被完整读出，建议再跑一次或检查 --dpi/清晰度。）")
    else:
        print("\n自检失败：按上面的报错依次排查 ——\n"
              "  ① 404 / NOT_FOUND「no longer available」→ 模型已下线，换 --model\n"
              "     （本脚本默认链已按 notebook 的提示改用 gemini-3.8-flash）；\n"
              "  ② 400 / INVALID_ARGUMENT「API key not valid」→ Key 无效（--api-key 或\n"
              "     环境变量 GEMINI_API_KEY / GOOGLE_API_KEY）；\n"
              "  ③ 429 / RESOURCE_EXHAUSTED            → 配额/速率限制，稍后重试；\n"
              "  ④ 代理连接失败 / 超时                  → 检查代理端口（默认 127.0.0.1:7897），\n"
              "     换端口用 --proxy，直连环境用 --proxy off；\n"
              "  ⑤ 403 / PERMISSION_DENIED             → 该模型无访问权限，换 --model。")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 11. 命令行
# ---------------------------------------------------------------------------
def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="handout_maker2.py",
        description="PDF -> 化学教案 Markdown（Google Gemini 视觉模型 API 版）",
    )
    p.add_argument("input_pdf", nargs="?", default="test1.pdf", help="输入 PDF（默认 test1.pdf）")
    p.add_argument("output_md", nargs="?", default=None,
                   help="输出 Markdown（默认 <输入名>.md）")
    p.add_argument("--model", default=None,
                   help="模型名，可用逗号写降级链（默认 %s）" % ",".join(DEFAULT_MODELS))
    p.add_argument("--api-key", default=None,
                   help="API Key（默认读 GEMINI_API_KEY / GOOGLE_API_KEY）")
    p.add_argument("--base-url", default=None,
                   help=f"网关地址（默认 {DEFAULT_BASE_URL}）")
    p.add_argument("--proxy", default=None,
                   help=f"HTTP/HTTPS 代理（默认 {DEFAULT_PROXY}；传 off/none 关闭代理直连）")
    p.add_argument("--dpi", type=int, default=RENDER_DPI, help=f"渲染 DPI（默认 {RENDER_DPI}）")
    p.add_argument("--pages-per-request", type=int, default=PAGES_PER_REQUEST,
                   help=f"每次请求塞几页（默认 {PAGES_PER_REQUEST}）")
    p.add_argument("--temperature", type=float, default=0.0,
                   help="采样温度（默认 0，转写要确定性）")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                   help=f"输出 token 上限（默认 {MAX_TOKENS}）")
    p.add_argument("--no-stream", action="store_true",
                   help="关闭流式输出（默认流式）")
    p.add_argument("--pages", default=None, help="只处理指定页，如 1-3,7（页码从 1 开始）")
    p.add_argument("--no-normalize", action="store_true", help="跳过 normalize 后处理")
    p.add_argument("--check", action="store_true", help="只做 API 连通性自检，不转换 PDF")
    p.add_argument("--check-image", default=None,
                   help="自检时改用这张真实图片（png/jpg）替代内置测试图")
    p.add_argument("--quiet", action="store_true", help="少打印一些过程信息")
    return p


def _parse_page_spec(spec):
    """'1-3,7' -> {1,2,3,7}；返回 None 表示不限。"""
    if not spec:
        return None
    pages = set()
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(chunk))
    return pages or None


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    models = parse_models(args.model)

    if args.check:
        return check_api(api_key=args.api_key, base_url=args.base_url, models=models,
                         image_path=args.check_image, stream=not args.no_stream,
                         proxy=args.proxy)

    output_md = args.output_md or (os.path.splitext(args.input_pdf)[0] + ".md")
    selected = _parse_page_spec(args.pages)
    api_key = resolve_api_key(args.api_key)
    args.base_url = base_url = resolve_base_url(args.base_url)

    # proxy 只透传原始值（None=用默认、字符串=指定、off/none=关闭），
    # 由 convert_pdf_to_handout / convert_selected_pages 各解析一次，避免二次解析把
    # 「显式关闭」的 None 又还原成默认代理。
    if selected is None:
        convert_pdf_to_handout(
            args.input_pdf, output_md, api_key=api_key, base_url=base_url, models=models,
            dpi=args.dpi, pages_per_request=args.pages_per_request,
            normalize=not args.no_normalize, temperature=args.temperature,
            max_tokens=args.max_tokens, stream=not args.no_stream, thinking=None,
            proxy=args.proxy, verbose=not args.quiet,
        )
    else:
        import shutil

        images, workdir = render_pdf_pages(args.input_pdf, dpi=args.dpi)
        keep = [(n, p) for n, p in images if (n + 1) in selected]
        try:
            convert_selected_pages(keep, output_md, api_key, models, args)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
