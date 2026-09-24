# -*- coding: utf-8 -*-
"""PDF -> 化学教案 转换器（智谱 GLM 免费视觉模型 API 版）。

本工程只保留这一条链路：**云端 GLM 视觉模型 + 确定性后处理**，不需要 GPU、
不需要本地权重、不需要代理（国内直连）。

    PDF ──PyMuPDF 渲染──> 每页 JPEG ──GLM 视觉模型──> Markdown 正文
        ──handout_normalize 归一化──> 一份连续的化学讲义 Markdown

输入输出契约（后处理与提示词共同保证）：
    * 表格结构：一律原生 HTML（<table>/<tr>/<th>/<td>），不用 Markdown 管道表；
    * 化学记号：**表格内外都只用行内 LaTeX**，同一套写法 --
      表格外 $\\text{H}_2\\text{O}$，表格内也是 <td>$\\text{Na}_2\\text{CO}_3$</td>；
    * 交付物里**没有「第 N 页」这类分页标题**：页码只是流水线的中间量。

为什么用标准库发 HTTP、而不是照抄 notebook 里的 openai SDK？
    `verify_GLM-Flash_API.ipynb` 给出的关键信息只有三样：网关地址、Bearer Key、
    `messages` 的形状 —— 这三样本脚本原样保留（见 DEFAULT_BASE_URL / resolve_api_key /
    build_payload）。但**本机的两个 conda 环境各缺一半**（实测）：

        conda activate Handout-Maker2   # Python 3.12：有 pymupdf/Pillow，缺 openai
        conda activate ai-assistant     # Python 3.11：有 openai，缺 pymupdf/Pillow

    智谱这个网关本身就是 OpenAI 兼容的 REST 接口，请求体与 notebook 里 `client.chat.completions.create(...)`
    的参数一一对应，用 urllib 直接 POST 即可。这样本脚本在 **Handout-Maker2 环境里开箱即用**
    （它本来就装着 pymupdf+Pillow），不必再装第 4 个包。若你更习惯 SDK，`pip install openai` 后
    把 base_url 设成同一个地址即可，wire 格式完全一致。

用法：
    python pdf_to_chemistry_handout_glm.py                       # 转 test1.pdf
    python pdf_to_chemistry_handout_glm.py 输入.pdf 输出.md
    python pdf_to_chemistry_handout_glm.py --check               # 只测 Key/网络/看图能力
    python pdf_to_chemistry_handout_glm.py 输入.pdf --pages 1-3  # 只转前 3 页（试参数用）

API Key 读取顺序：命令行 --api-key > 环境变量 GLM_API_KEY / ZHIPUAI_API_KEY / BIGMODEL_API_KEY
> 脚本内兜底值（取自 verify_GLM-Flash_API.ipynb）。

⚠️ 本脚本特有的三个坑（都是**实测**出来的，不是抄文档）：
    1. `glm-4v-flash` 的 `max_tokens` 上限是 **1024**，传 2048 会直接回
       `400 / 1210 max_tokens参数非法：限制数值范围[1,1024]`。本脚本按模型自动夹紧
       （见 _MODEL_MAX_TOKENS_CAP），并在真被拒时自适应降档重试。
    2. `glm-4.6v-flash` 默认**开着思维链**：实测同一页 completion_tokens=1479，其中
       reasoning_tokens=930 —— 思考会吃掉输出预算。所以默认 max_tokens 给 4096 留足余量，
       并且只取 `delta.content`、丢弃 `reasoning_content`，思维链不会混进讲义。
    3. 流式（SSE）模式下，如果推理中途异常终止，**不会**返回上面的业务错误码，
       而是把原因放在 `finish_reason` 里（sensitive / network_error / length /
       model_context_window_exceeded）。因此本脚本在流里必须自己检查 finish_reason，
       否则会把半截内容当成功写进讲义。
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

from handout_normalize import normalize_handout_markdown

# ---------------------------------------------------------------------------
# 1. 常量与默认配置
# ---------------------------------------------------------------------------
# 取自 verify_GLM-Flash_API.ipynb（网关与 Key 都是 notebook 里那一套）
DEFAULT_API_KEY = "1e23670feab74a90af54bbc0c3b45573.MIIybtm98HfwZcu5"
DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"

# 实测（本机、同一把 Key）的模型可用性：
#   glm-4.6v-flash  -> 免费、可用。视觉+思维链，输出可直接用 \text{} 行内 LaTeX。
#   glm-4v-flash    -> 免费、可用。智谱首个完全免费的图像理解模型，速度更快。
#   glm-4.5v / glm-4.6v / glm-4v-plus -> 429 / 1113「余额不足或无可用资源包」：付费模型，本账号无余额。
# 所以默认链 = 新的免费模型优先、旧的免费模型兜底；两个都不花配额。
DEFAULT_MODELS = ("glm-4.6v-flash", "glm-4v-flash")

# 各模型的 max_tokens 硬上限（未列出的视为不设上限）。实测来源见模块 docstring 的坑 1。
_MODEL_MAX_TOKENS_CAP = {"glm-4v-flash": 1024}

# 渲染 DPI。走网络传输，不像本地推理那样受显存约束；实测一张 16:9 幻灯片在 150 DPI 下
# 是 2000x1125 / JPEG 84KB，正文与上下标都清晰，且远低于平台的 5MB 单图上限。
RENDER_DPI = int(os.environ.get("HM_DPI", "150"))

# 每次请求塞几页图。默认 1 页：官方文档写 `GLM-4V-Flash` 限制 1 张图（实测 2 张也能跑通，
# 但既然默认主力是 4.6v-flash，就按“一页一请求”换取最干净的页序与最稳的重试粒度）。
PAGES_PER_REQUEST = int(os.environ.get("HM_PAGES_PER_REQUEST", "1"))

# 平台限制：单图 5MB 以下、像素不超过 6000x6000。这里留出余量并按需自适应收缩。
MAX_IMAGE_BYTES = int(os.environ.get("HM_MAX_IMAGE_BYTES", str(4 * 1024 * 1024)))
MAX_IMAGE_EDGE = int(os.environ.get("HM_MAX_IMAGE_EDGE", "6000"))

# 重试与退避。智谱的限流是**并发维度**（同一时刻处理中的请求数），
# 所以本脚本坚持串行逐页调用；1302（速率限制）/1305（平台过载）走指数退避 + 抖动。
MAX_RETRIES = int(os.environ.get("HM_MAX_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.environ.get("HM_RETRY_BASE_DELAY", "5"))

# 单次请求最长等待（秒）。免费模型 + 思维链较慢，实测单页 13~25s，给足 300s。
REQUEST_TIMEOUT = float(os.environ.get("HM_REQUEST_TIMEOUT", "300"))

# 输出 token 上限。4.6v-flash 会先思考（实测 930 reasoning tokens / 页），4096 才有余量。
MAX_TOKENS = int(os.environ.get("HM_MAX_TOKENS", "4096"))

# 输出文档的标题后缀：`<PDF 文件名>` + 这个后缀作为一级标题。
_DEFAULT_TITLE_SUFFIX = " 课程讲义"


# ---------------------------------------------------------------------------
# 2. 提示词
#
# 这份提示词要同时钉死三件事，缺一件交付物就会坏：
#   ① 表格结构一律原生 HTML —— <table> 是 Markdown 里唯一能保证"排成表"的写法，
#      管道表会被各家渲染器各自解释；
#   ② 化学记号表格内外都用行内 LaTeX —— 一套写法管到底，不出现"同一种物质两种长相"；
#      注意这要求交付物的渲染环境挂 KaTeX（本工程的预览页与体检预览都自带 CDN）；
#   ③ 等号就是等号：方程式一律 `=`，可逆用 `\rightleftharpoons`，`→` 只留给转化关系。
# 后处理（handout_normalize）会把这套约定再兜一遍，提示词与后处理是"双保险"关系。
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

# 一批多页时追加的页序约定：便于按批请求时仍能还原页边界（切页后会把标记剥掉，
# 交付物里不会出现任何注释或页码）。
MULTI_IMAGE_PROMPT = PROMPT + """
14. 本次输入包含多张课件图片，请严格按图片顺序逐页转写，页与页之间用空行隔开；
    在每页内容之前单独输出一行 `<!-- page: N -->`（N 为该图在本批中的序号，从 1 开始），
    除这一行页序标记外不要添加任何其他注释或页码。"""

# 自检用提示词：只要能读出图里的字符就说明 Key/网络/看图三件事都通了。
PROBE_PROMPT = "图中有哪些字符？原样列出，不要解释，不要换行。"


# ---------------------------------------------------------------------------
# 3. API Key 与请求体
# ---------------------------------------------------------------------------
def resolve_api_key(explicit=None):
    """API Key 解析：命令行 > 环境变量 > 脚本内兜底值。"""
    if explicit:
        return explicit
    for env in ("GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY", "ZHIPU_API_KEY"):
        if os.environ.get(env):
            return os.environ[env]
    return DEFAULT_API_KEY


def resolve_base_url(explicit=None):
    """网关地址。notebook 里是 https://open.bigmodel.cn/api/paas/v4/。"""
    return explicit or os.environ.get("GLM_BASE_URL") or DEFAULT_BASE_URL


def parse_models(model_arg):
    """把 --model 参数（可逗号分隔）解析成降级链。"""
    if not model_arg:
        return list(DEFAULT_MODELS)
    models = [m.strip() for m in str(model_arg).split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def model_max_tokens(model, requested):
    """按模型夹紧 max_tokens（glm-4v-flash 的硬上限是 1024，见模块 docstring 坑 1）。"""
    cap = _MODEL_MAX_TOKENS_CAP.get(model)
    return min(int(requested), cap) if cap else int(requested)


def _data_url(image_path):
    """把本地 JPEG 编成 data URL —— 对应 notebook 里图片输入的形状。"""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return "data:image/jpeg;base64," + b64


def build_payload(model, image_paths, prompt, temperature=0.0, max_tokens=MAX_TOKENS,
                  stream=True, thinking=None):
    """构造 /chat/completions 的请求体（与 notebook 里 create(...) 的参数一一对应）。"""
    content = [{"type": "image_url", "image_url": {"url": _data_url(p)}} for p in image_paths]
    content.append({"type": "text", "text": prompt})

    payload = {
        "model": model,
        # 转写要确定性，不要创造性：温度压到 0。
        "temperature": float(temperature),
        "max_tokens": model_max_tokens(model, max_tokens),
        "stream": bool(stream),
        "messages": [
            # 系统消息用来对抗“助手人格”：模型有强烈的“包一层代码块 / 补一句总结”倾向，
            # 提示词里已经写了一遍，这里再以更高的优先级压一次（后处理还会兜底）。
            {"role": "system", "content":
                "你是化学课件转写引擎，只输出 Markdown 正文本身，"
                "不输出任何解释、寒暄、代码围栏或收尾语。"},
            {"role": "user", "content": content},
        ],
    }
    # 思维链默认不显式配置（4.6v-flash 默认 enabled）；只有用户显式指定时才发这个字段。
    if thinking and thinking != "auto":
        payload["thinking"] = {"type": thinking}
    return payload


# ---------------------------------------------------------------------------
# 4. 错误分类（业务错误码取自官方《错误码》文档，均已实测核对）
# ---------------------------------------------------------------------------
# 可重试：并发限流 / 平台过载 / 服务端抖动 —— 等一会儿再来有救。
_RETRY_CODES = {"1200", "1230", "1234", "1302", "1305"}
# 换模型：这一把 Key 在**这个模型**上没额度/没权限 —— 换降级链里的下一个模型才有救。
_NEXT_MODEL_CODES = {
    "1113",  # 账户欠费/无可用资源包（实测：付费视觉模型 glm-4.5v/4.6v/4v-plus 都是这个）
    "1211",  # 模型不存在
    "1220",  # 无权访问该 API
    "1221", "1222",  # API 已下线/不存在
    "1308", "1310",  # 已达使用上限（限额到点才重置）
    "1316", "1317", "1318", "1319", "1320", "1321",  # 各类 5 小时/7 天/月消费上限
}
# 致命：参数写错、鉴权失败、内容被安全审核拦截 —— 重试和换模型都救不回来，直接报错。
_FATAL_CODES = {
    "1000", "1001", "1003", "1005",  # 鉴权类
    "1210",  # 参数非法（含 max_tokens 超上限，另有自适应降档逻辑先行处理）
    "1212", "1213", "1214", "1215",  # 调用方式/缺参/参数非法/互斥参数
    "1261",  # Prompt 超长
    "1301",  # 输入或生成内容被判定为敏感
    "1309", "1311", "1313", "1314", "1315",  # 套餐/权益类
}

_KIND_RETRY = "retry"
_KIND_NEXT_MODEL = "next_model"
_KIND_FATAL = "fatal"


class GlmApiError(RuntimeError):
    """带分类信息的 API 错误，让调用方能区分「重试 / 换模型 / 直接失败」。"""

    def __init__(self, kind, code, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.code = code
        self.status = status

    def __str__(self):
        tag = f"HTTP {self.status} " if self.status else ""
        return f"{tag}[{self.code or '?'}] {super().__str__()}"


def _classify(status, code, message):
    """把 (HTTP 状态码, 业务错误码, 错误文本) 归到 retry / next_model / fatal 三类。"""
    code = str(code or "").strip()
    if code in _RETRY_CODES:
        return _KIND_RETRY
    if code in _NEXT_MODEL_CODES:
        return _KIND_NEXT_MODEL
    if code in _FATAL_CODES:
        return _KIND_FATAL

    # 没有业务码（或码不在表里）时退化成按 HTTP 状态码 + 文本判断。
    text = (message or "").lower()
    if status in (408, 429, 500, 502, 503, 504):
        # 429 但码不认识：仍按可重试处理（要么限流要么过载），换模型是无用功。
        return _KIND_RETRY
    if status in (401, 403):
        return _KIND_FATAL
    if status == 404:
        return _KIND_NEXT_MODEL
    if status == 400:
        return _KIND_FATAL
    if any(k in text for k in ("capacity", "overload", "rate limit", "too many")):
        return _KIND_RETRY
    return _KIND_FATAL


def _error_from_body(body, status=None):
    """从响应体里抽出业务错误码与信息，构造 GlmApiError。"""
    code, message = None, (body or "").strip()
    try:
        obj = json.loads(body)
        err = obj.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or message
        elif isinstance(err, str):
            message = err
    except (ValueError, TypeError):
        pass
    message = re.sub(r"\s+", " ", str(message))[:300]
    return GlmApiError(_classify(status, code, message), code, message, status)


def _error_from_exception(exc):
    """网络层异常（超时/连接被重置/DNS 失败）一律按可重试处理。"""
    return GlmApiError(_KIND_RETRY, None, re.sub(r"\s+", " ", str(exc))[:300])


def _is_retryable(exc):
    """兼容自检脚本的命名习惯：这个异常值得重试吗？"""
    if isinstance(exc, GlmApiError):
        return exc.kind == _KIND_RETRY
    return True


# ---------------------------------------------------------------------------
# 5. 响应解析
# ---------------------------------------------------------------------------
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_BOX_RE = re.compile(r"<\|(?:begin|end)_of_box\|>")


def _strip_model_scaffolding(text):
    """剥掉 GLM-4.5V/4.6V 系列可能带的思维链与边界标记。

    官方文档明确：`GLM-4.5V` 系列的返回内容可能包含 `<think> </think>` 与
    `<|begin_of_box|> <|end_of_box|>`。思维链绝不能混进讲义正文。
    """
    text = _THINK_RE.sub("", text)
    return _BOX_RE.sub("", text)


def _content_to_text(content):
    """把 message.content 归一成纯文本。

    文档里 `content` 有三种形状：字符串、多模态数组（GLM-4V 系列会把文本包成
    `[{"type":"text","text":...}]`）、以及 tool_calls 场景下的 null。三种都要能接住。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                chunks.append(item.get("text") or "")
            elif isinstance(item, str):
                chunks.append(item)
        return "".join(chunks)
    return str(content)


def _finish_reason_error(reason):
    """把流式返回里的 finish_reason 异常值翻译成异常（见模块 docstring 坑 3）。"""
    reason = (reason or "").strip()
    if reason in ("", "stop", "tool_calls"):
        return None
    if reason == "sensitive":
        return GlmApiError(_KIND_FATAL, "1301", "内容被安全审核拦截（finish_reason=sensitive）")
    if reason == "network_error":
        return GlmApiError(_KIND_RETRY, None, "模型推理异常（finish_reason=network_error）")
    if reason == "model_context_window_exceeded":
        return GlmApiError(_KIND_FATAL, None,
                           "超出模型上下文窗口（finish_reason=model_context_window_exceeded），"
                           "请调小 --dpi 或减少 --pages-per-request")
    if reason == "length":
        # 截断不是“失败”：内容确实生成了一部分，报出去让人知道比静默丢掉好。
        return None
    return None


# ---------------------------------------------------------------------------
# 6. 一次（单页或多页）识别：串行 + 重试 + 模型降级
# ---------------------------------------------------------------------------
def _open_request(url, api_key, payload, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if payload.get("stream") else "application/json",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _call_once_nonstream(url, api_key, payload, timeout):
    """非流式：一次性拿完整 JSON。"""
    try:
        with _open_request(url, api_key, payload, timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise _error_from_body(e.read().decode("utf-8", "replace"), e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _error_from_exception(e) from None

    try:
        data = json.loads(raw)
    except ValueError:
        raise GlmApiError(_KIND_RETRY, None, f"响应不是合法 JSON：{raw[:200]}") from None
    if isinstance(data.get("error"), (dict, str)):
        raise _error_from_body(raw)

    choices = data.get("choices") or []
    if not choices:
        raise GlmApiError(_KIND_RETRY, None, f"响应缺少 choices：{raw[:200]}")
    choice = choices[0]
    text = _strip_model_scaffolding(_content_to_text((choice.get("message") or {}).get("content")))
    err = _finish_reason_error(choice.get("finish_reason"))
    if err is not None:
        raise err
    return text, choice.get("finish_reason"), data.get("usage")


def _call_once_stream(url, api_key, payload, timeout):
    """流式：逐块收 delta.content，最后一块给出 finish_reason。

    只取 content，**丢弃 reasoning_content** —— 4.6v-flash 默认开思维链，
    那些推理过程不属于讲义正文。
    """
    chunks = []
    finish_reason = None
    usage = None
    try:
        with _open_request(url, api_key, payload, timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):     # 空行 / SSE 心跳注释
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                              # 非 JSON 的行直接跳过
                if isinstance(obj.get("error"), (dict, str)):
                    # 流已经 200 建立，之后才出错时，网关也可能在流里塞 error 对象。
                    raise _error_from_body(json.dumps(obj, ensure_ascii=False))
                if obj.get("usage"):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                piece = _content_to_text(delta.get("content"))
                if piece:
                    chunks.append(piece)
    except urllib.error.HTTPError as e:
        raise _error_from_body(e.read().decode("utf-8", "replace"), e.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise _error_from_exception(e) from None

    text = _strip_model_scaffolding("".join(chunks))
    err = _finish_reason_error(finish_reason)
    if err is not None:
        raise err
    return text, finish_reason, usage


def call_glm(api_key, models, image_paths, base_url=DEFAULT_BASE_URL, temperature=0.0,
             max_tokens=MAX_TOKENS, stream=True, thinking=None, verbose=True):
    """调用 GLM 完成一次（单页或多页）识别，返回 Markdown 文本。

    * 自动重试：限流/过载/服务端抖动走指数退避 + 抖动；
    * 自动降级：额度不足、模型不存在时切到 models 链里的下一个模型；
    * 自动夹紧：`glm-4v-flash` 的 max_tokens 上限 1024，被拒时自适应降档再来一次。
    """
    multi = len(image_paths) > 1
    prompt = MULTI_IMAGE_PROMPT if multi else PROMPT
    url = base_url.rstrip("/") + "/chat/completions"

    last_err = None
    for model in models:
        payload = build_payload(model, image_paths, prompt, temperature=temperature,
                                max_tokens=max_tokens, stream=stream, thinking=thinking)
        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                if verbose:
                    print(f"  -> {model}（{len(image_paths)} 张图，max_tokens="
                          f"{payload['max_tokens']}，第 {attempt} 次尝试）", flush=True)
                call = _call_once_stream if stream else _call_once_nonstream
                text, finish_reason, usage = call(url, api_key, payload, REQUEST_TIMEOUT)
                if not text.strip():
                    raise GlmApiError(_KIND_RETRY, None,
                                      f"响应为空（finish_reason={finish_reason}）")
                if finish_reason == "length":
                    print(f"  !! 注意：本页输出触到 max_tokens={payload['max_tokens']} 被截断"
                          f"（finish_reason=length），已保留已生成的部分", flush=True)
                if verbose and usage:
                    print(f"     ok: {len(text)} 字, usage={usage}", flush=True)
                return text
            except GlmApiError as e:
                last_err = e
                # max_tokens 超上限：按上限夹紧后立刻重来（不浪费退避时间）。
                if e.code == "1210" and "max_token" in str(e).lower():
                    clamped = model_max_tokens(model, 1024)
                    if clamped < payload["max_tokens"]:
                        print(f"  !! {model} 的 max_tokens 上限是 {clamped}，自动降档重试", flush=True)
                        payload["max_tokens"] = clamped
                        continue
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
# 7. PDF 页面转 JPEG（带 5MB / 6000px 双上限的自适应收缩）
# ---------------------------------------------------------------------------
def render_pdf_pages(pdf_path, dpi=RENDER_DPI, workdir=None, max_image_bytes=MAX_IMAGE_BYTES,
                     max_edge=MAX_IMAGE_EDGE):
    """把 PDF 每页渲染成 JPEG，返回 ([(page_index, jpg_path), ...], workdir)。

    平台限制：每张图 5MB 以下、像素不超过 6000x6000。幻灯片在 150 DPI 下只有约
    2000x1125 / 84KB，正常完全够用；这里的收缩逻辑是给“A4 扫描件 + 高 DPI”之类
    的极端输入兜底的。
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"找不到 PDF 文件: {pdf_path}")

    try:
        import pymupdf  # PyMuPDF >= 1.24 的新名字
    except ImportError:  # 老版本只有 fitz 别名
        import fitz as pymupdf

    from PIL import Image

    doc = pymupdf.open(pdf_path)
    workdir = workdir or tempfile.mkdtemp(prefix="hm_glm_pages_")
    pages = []
    try:
        for page_num in range(len(doc)):
            pix = doc[page_num].get_pixmap(dpi=dpi)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            # 先按像素上限整体缩一次，再按字节上限降质量/降分辨率。
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

    模型不总是乖乖打标记：**一个标记都没有**时退化为“整批作为一块返回”，
    绝不猜页序 —— 宁可少几个切分点，也不能把 A 页的内容挂到 B 页上去。
    标记本身只用于切分，剥掉之后不会出现在交付物里。
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

    刻意不产出「## 第 N 页内容」这类分页标题，也不产出 `---` 分隔线：
    页码是流水线的中间量（批大小、页序对齐用），交付物要的是一份连续讲义。
    页序切分本身仍然要做 —— 右侧模型偶尔会把两页的内容顺序写反，
    逐页归一化能保证表格/方程式的处理边界与图片边界对齐。

    ⚠️ 顺序很关键，这里踩过一次真实的坑：
    `handout_normalize` 会把 `->` / `→` 还原成等号，而页序标记 `<!-- page: 2 -->`
    里的 `-->` 正好命中这条规则，于是**先归一化再切页**会把标记破坏成
    `<!-- page: 2 =`，切页直接失效。所以必须**先切页、再逐块归一化**
    （另外 handout_normalize 现在会先删 HTML 注释，双保险）。
    """
    if len(batch) == 1:
        bodies = [text]
    else:
        # 模型没按约定打 page 标记时，split_by_page_marks 会整批作为一块返回 ——
        # 宁可少几个切分点，也不能把 A 页的内容挂到 B 页上去。
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
    verbose=True,
):
    """PDF -> Markdown 讲义（本工程唯一的入口函数）。"""
    api_key = resolve_api_key(api_key)
    models = models or list(DEFAULT_MODELS)
    pages_per_request = max(1, int(pages_per_request))

    images, workdir = render_pdf_pages(pdf_path, dpi=dpi)
    total = len(images)

    # 增量写入：每批解析完立即落盘 + flush，中途崩溃也留得下可用的部分讲义。
    try:
        with open(output_md_path, "w", encoding="utf-8") as f:
            title = os.path.splitext(os.path.basename(pdf_path))[0] + _DEFAULT_TITLE_SUFFIX
            f.write(f"# {title}\n\n")
            f.flush()

            for start in range(0, total, pages_per_request):
                batch = images[start:start + pages_per_request]
                first, last = batch[0][0] + 1, batch[-1][0] + 1
                label = str(first) if first == last else f"{first}-{last}"
                print(f"正在处理第 {label} 页（共 {total} 页）...", flush=True)

                text = call_glm(
                    api_key, models, [p for _, p in batch], base_url=base_url,
                    temperature=temperature, max_tokens=max_tokens, stream=stream,
                    thinking=thinking, verbose=verbose,
                )
                # 注意：切页必须在归一化之前（见 _assemble_blocks 的说明），
                # 所以这里的 normalize 开关是透传进去逐块生效的。
                _write_blocks(f, _assemble_blocks(text, batch, normalize))
                f.flush()

        print(f"解析完成！已保存为: {output_md_path}", flush=True)
    finally:
        # 清理临时图片：清理失败绝不该让整个任务失败（Windows 上句柄未释放很常见）。
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
    with open(output_md, "w", encoding="utf-8") as f:
        title = os.path.splitext(os.path.basename(args.input_pdf))[0] + _DEFAULT_TITLE_SUFFIX
        f.write(f"# {title}\n\n")
        f.flush()
        for start in range(0, len(images), batch_size):
            batch = images[start:start + batch_size]
            first, last = batch[0][0] + 1, batch[-1][0] + 1
            label = str(first) if first == last else f"{first}-{last}"
            print(f"正在处理第 {label} 页...", flush=True)
            text = call_glm(api_key, models, [p for _, p in batch],
                            base_url=resolve_base_url(args.base_url),
                            temperature=args.temperature, max_tokens=args.max_tokens,
                            stream=not args.no_stream, thinking=args.thinking)
            _write_blocks(f, _assemble_blocks(text, batch, normalize))
            f.flush()
    print(f"解析完成！已保存为: {output_md}", flush=True)
    return output_md


# ---------------------------------------------------------------------------
# 10. 连通性自检（替代 notebook 里手敲的那两段试跑）
# ---------------------------------------------------------------------------
def _png_chunk(tag, payload):
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _geometric_png(width=256, height=64):
    """无 Pillow 时的兜底测试图：标准库现场生成（黑边 + 对角线），不依赖任何三方包。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # 每行的 filter type = 0 (None)
        for x in range(width):
            border = x < 3 or y < 3 or x >= width - 3 or y >= height - 3
            diag = abs(x * height - y * width) < height * 2
            rows += b"\x00\x00\x00" if (border or diag) else b"\xff\xff\xff"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit RGB
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _png_chunk(b"IEND", b""))


# 自检图里的文字与判定关键词：只要模型读出了这些，就说明“Key / 网络 / 看图”三件事都通了。
PROBE_TEXT = "H2O  NaCl  Fe3+  Na2CO3"
PROBE_EXPECT = ("h2o", "nacl")


def make_probe_image(text=PROBE_TEXT):
    """生成一张白底黑字的测试图，返回 (PNG 字节, mime)。

    刻意**真的画上化学式**而不是画几何图形：这样自检验证的是“能不能识别文字”，
    而不只是“网络通不通”。没有 Pillow 时退回几何图，至少还能验证连通性。
    """
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
    except Exception:  # noqa: BLE001 —— 字体异常不该让自检挂掉
        draw.text((18, 60), text, fill="black")
    draw.rectangle([2, 2, 637, 157], outline="black", width=3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"


def _check_one_model(api_key, model, data, mime, base_url, stream, timeout=120):
    """跑一次最小请求，成功返回 (True, 文本)，失败返回 (False, 错误摘要)。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": model_max_tokens(model, 256),
        "stream": bool(stream),
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))}},
            {"type": "text", "text": PROBE_PROMPT},
        ]}],
    }
    try:
        call = _call_once_stream if stream else _call_once_nonstream
        text, _finish, _usage = call(url, api_key, payload, timeout)
        return True, re.sub(r"\s+", " ", text).strip()
    except GlmApiError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, re.sub(r"\s+", " ", str(e))[:300]


def check_api(api_key=None, base_url=None, models=None, image_path=None, stream=True):
    """连通性自检：一次验证 Key / 网络 / 模型可用性 / 看图能力这四件事。

    默认用 `make_probe_image()` 现场画的小图（上面写着化学式）；也可以用
    `--check-image 真实截图.png` 换成任意一张真实课件截图。
    """
    api_key = resolve_api_key(api_key)
    base_url = resolve_base_url(base_url)
    models = models or list(DEFAULT_MODELS)

    print(f"网关: {base_url}")
    print(f"API Key: ...{api_key[-6:]}（共 {len(api_key)} 字符）")

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
        success, detail = _check_one_model(api_key, model, data, mime, base_url, stream)
        print(f"   {'[OK]  ' + detail if success else '[FAIL] ' + detail}", flush=True)
        ok = ok or success
        if success and expect:
            hit = any(e in detail.lower() for e in expect)
            recognized = recognized or hit
            print(f"   {'[识别通过] 读出了测试图里的字符' if hit else '[识别存疑] 没读全期望字符'}",
                  flush=True)

    if ok:
        print("\n自检通过：网关 / API Key / 看图能力都正常，可以开始转换。")
        if expect and not recognized:
            print("（但内置测试图的字符没被完整读出，建议再跑一次或检查 --dpi/清晰度。）")
    else:
        print("\n自检失败：按上面的报错依次排查 ——\n"
              "  ① 1113 / 「余额不足或无可用资源包」 → 该模型是**付费模型**，把 --model 换成\n"
              "     免額度的 glm-4.6v-flash 或 glm-4v-flash（本脚本默认链就是这两个）；\n"
              "  ② 1000-1005 /「身份验证失败」        → Key 无效或过期（--api-key 或环境变量 GLM_API_KEY）；\n"
              "  ③ 1302 /「已达到速率限制」            → 并发过高，串行逐页跑（本脚本默认就是串行）；\n"
              "  ④ 1305 /「模型当前访问量过大」        → 平台过载，稍后重试；\n"
              "  ⑤ 超时 / 连接被重置                   → 国内网关无需代理；若开了全局代理反而可能绕出国，\n"
              "     可临时 unset http_proxy/https_proxy 再试。")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 11. 命令行
# ---------------------------------------------------------------------------
def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="pdf_to_chemistry_handout_glm.py",
        description="PDF -> 化学教案 Markdown（智谱 GLM 视觉模型 API 版）",
    )
    p.add_argument("input_pdf", nargs="?", default="test1.pdf", help="输入 PDF（默认 test1.pdf）")
    p.add_argument("output_md", nargs="?", default=None,
                   help="输出 Markdown（默认 <输入名>_handout_glm.md）")
    p.add_argument("--model", default=None,
                   help="模型名，可用逗号写降级链（默认 %s）" % ",".join(DEFAULT_MODELS))
    p.add_argument("--api-key", default=None,
                   help="API Key（默认读 GLM_API_KEY / ZHIPUAI_API_KEY）")
    p.add_argument("--base-url", default=None,
                   help=f"网关地址（默认 {DEFAULT_BASE_URL}）")
    p.add_argument("--dpi", type=int, default=RENDER_DPI, help=f"渲染 DPI（默认 {RENDER_DPI}）")
    p.add_argument("--pages-per-request", type=int, default=PAGES_PER_REQUEST,
                   help=f"每次请求塞几页（默认 {PAGES_PER_REQUEST}）")
    p.add_argument("--temperature", type=float, default=0.0,
                   help="采样温度（默认 0，转写要确定性）")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                   help=f"输出 token 上限（默认 {MAX_TOKENS}；glm-4v-flash 会被自动夹到 1024）")
    p.add_argument("--thinking", choices=("auto", "enabled", "disabled"), default="auto",
                   help="思维链开关（默认 auto=不显式发送，交给模型默认值）")
    p.add_argument("--no-stream", action="store_true",
                   help="关闭流式输出（默认流式：更快看到首字，也便于发现半途异常）")
    p.add_argument("--pages", default=None, help="只处理指定页，如 1-3,7（页码从 1 开始）")
    p.add_argument("--no-normalize", action="store_true", help="跳过 handout_normalize 后处理")
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
                         image_path=args.check_image, stream=not args.no_stream)

    output_md = args.output_md or (os.path.splitext(args.input_pdf)[0] + "_handout_glm.md")
    selected = _parse_page_spec(args.pages)
    api_key = resolve_api_key(args.api_key)
    # 解析一次就写回 args：--pages 分支的 convert_selected_pages 直接读 args.base_url，
    # 若这里只放局部变量，那边会拿到原始 None。
    args.base_url = base_url = resolve_base_url(args.base_url)

    if selected is None:
        convert_pdf_to_handout(
            args.input_pdf, output_md, api_key=api_key, base_url=base_url, models=models,
            dpi=args.dpi, pages_per_request=args.pages_per_request,
            normalize=not args.no_normalize, temperature=args.temperature,
            max_tokens=args.max_tokens, stream=not args.no_stream, thinking=args.thinking,
            verbose=not args.quiet,
        )
    else:
        # 抽页模式：只渲染指定页再走同一条链路（用于快速试参数，不必等整份 PDF）。
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
