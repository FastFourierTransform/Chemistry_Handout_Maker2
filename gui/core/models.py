# -*- coding: utf-8 -*-
"""数据模型 + 「高级参数注册表」（GUI 可扩展性的核心）。

设计要点
--------
1. ``ApiProfile`` 只存**用户显式改过的**参数（params 字典，值一律字符串），
   未改过的参数由 ``PARAM_SPECS`` 里的默认值兜底 —— 未来新版本改默认值，
   老配置文件无需迁移。
2. ``PARAM_SPECS`` 是**参数元数据的唯一事实来源**：配置弹窗的高级参数表由它
   自动生成，bridge 按它做类型强转与 kwarg 映射。**新增一个可调参数 =
   在这里加一条 ParamSpec**，UI 与桥接层都会自动跟上，不需要改任何界面代码。
3. 任务状态机（FileTask.status）驱动文件列表的显示与「开始转换」按钮的可用性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """任务状态（值是直接显示给用户的中文文案）。"""

    PREPARING = "准备中"    # PPT/PPTX 正在转 PDF 缓存（还没进入转换队列）
    PENDING = "等待中"      # 已就绪，等待转换
    RUNNING = "转换中"
    SUCCESS = "已完成"
    FAILED = "失败"
    CANCELLED = "已取消"
    SKIPPED = "已跳过"      # 勾选「跳过已存在输出」时命中已有产物

    @property
    def can_start(self) -> bool:
        """该状态的任务可以被「开始转换」再次纳入队列。"""
        return self in (TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @property
    def finished(self) -> bool:
        """是否已是终态（不再自动变化）。"""
        return self in (TaskStatus.SUCCESS, TaskStatus.FAILED,
                        TaskStatus.CANCELLED, TaskStatus.SKIPPED)


@dataclass(frozen=True)
class ParamSpec:
    """一条可调参数的完整元数据：UI 渲染 + 类型强转 + kwarg 映射都由它驱动。

    key        —— 配置 JSON 里 params 字典的键名
    label      —— 弹窗里显示的名字
    kind       —— "int" / "float" / "bool" / "str" / "choice"
    default    —— 未设置时的兜底值
    kwarg      —— 传给引擎入口函数的形参名（invert=True 时取反后传入）
    engines    —— 适用于哪些引擎（见 bridge.ENGINE_SPECS 的键）
    choices    —— kind="choice" 时的候选项
    invert     —— 语义取反（如 normalize=True → no_normalize=False）
    tip        —— 弹窗「说明」列 / 悬浮提示
    """

    key: str
    label: str
    kind: str
    default: Any
    kwarg: str
    engines: tuple = ("glm", "gemini")
    choices: tuple = ()
    invert: bool = False
    tip: str = ""


# ---------------------------------------------------------------------------
# 高级参数注册表 —— 新增可调参数只改这里
# ---------------------------------------------------------------------------
PARAM_SPECS: dict[str, ParamSpec] = {
    "temperature": ParamSpec(
        "temperature", "采样温度", "float", 0.0, "temperature",
        engines=("glm", "gemini", "openai"),
        tip="转写要确定性，保持 0；调高会开始自由发挥。",
    ),
    "max_tokens": ParamSpec(
        "max_tokens", "输出上限 max_tokens", "int", 4096, "max_tokens",
        engines=("glm", "gemini", "openai"),
        tip="单页输出预算；glm-4v-flash 会自动夹到 1024。",
    ),
    "thinking": ParamSpec(
        "thinking", "思维链 thinking", "choice", "auto", "thinking",
        engines=("glm",), choices=("auto", "enabled", "disabled"),
        tip="仅 GLM 后端生效；思维链会吃掉输出预算，默认 auto。",
    ),
    "stream": ParamSpec(
        "stream", "流式输出 stream", "bool", True, "stream",
        engines=("glm", "gemini", "openai"),
        tip="关闭后逐页结果一次性返回，但无法中途看到生成进度。",
    ),
    "dpi": ParamSpec(
        "dpi", "渲染 DPI", "int", 150, "dpi",
        engines=("glm", "gemini", "openai"),
        tip="PDF 渲染清晰度；越高图越清楚、也越占流量（上限 6000px 边长）。",
    ),
    "pages_per_request": ParamSpec(
        "pages_per_request", "每请求页数", "int", 1, "pages_per_request",
        engines=("glm", "gemini", "openai"),
        tip="默认 1 页一请求：页序最干净、重试粒度最细、失败成本最低。",
    ),
    "normalize": ParamSpec(
        "normalize", "归一化后处理", "bool", True, "no_normalize", invert=True,
        engines=("glm", "gemini", "openai"),
        tip="关闭后跳过 normalize（调试用；交付物别这么产）。",
    ),
    "proxy": ParamSpec(
        "proxy", "HTTP 代理", "str", "", "proxy",
        engines=("gemini",),
        tip="仅 Gemini 后端生效；留空用引擎默认（127.0.0.1:7897），"
             "填 off/none 直连。",
    ),
}


def coerce_param(spec: ParamSpec, raw: Any) -> Any:
    """把配置里存的字符串值按 spec.kind 强转成引擎要的类型。"""
    if raw is None or raw == "":
        return spec.default
    try:
        if spec.kind == "int":
            return int(float(str(raw)))
        if spec.kind == "float":
            return float(str(raw))
        if spec.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "y")
        if spec.kind == "choice":
            v = str(raw)
            return v if v in spec.choices else spec.default
        return str(raw)
    except (TypeError, ValueError):
        return spec.default


@dataclass
class ApiProfile:
    """一个模型 / API 配置档案（对应弹窗里的一条配置）。

    base_url / api_key 留空表示「交给引擎脚本的默认解析链」
    （环境变量 / 脚本内兜底值），models 留空表示用引擎默认降级链。
    """

    name: str = "新配置"
    engine: str = "glm"
    base_url: str = ""
    api_key: str = ""
    models: str = ""                      # 逗号分隔的降级链
    params: dict = field(default_factory=dict)   # key -> 字符串（只存覆盖值）

    def effective_param(self, key: str) -> Any:
        """取参数生效值：用户覆盖值（强转）优先，否则注册表默认值。"""
        spec = PARAM_SPECS.get(key)
        if spec is None:
            return None
        if key in self.params:
            return coerce_param(spec, self.params[key])
        return spec.default

    def key_status(self) -> str:
        if not self.api_key.strip():
            return "未设置（用环境变量 / 脚本兜底）"
        return "已设置（●●●●●●●）"

    @classmethod
    def from_dict(cls, d: dict) -> "ApiProfile":
        return cls(
            name=str(d.get("name") or "未命名"),
            engine=str(d.get("engine") or "glm"),
            base_url=str(d.get("base_url") or ""),
            api_key=str(d.get("api_key") or ""),
            models=str(d.get("models") or ""),
            params={str(k): str(v) for k, v in (d.get("params") or {}).items()},
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "engine": self.engine,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "models": self.models,
            "params": dict(self.params),
        }


@dataclass
class FileTask:
    """文件列表里的一行：源文件（PDF/PPT）+ 派生 PDF + 状态。"""

    uid: int
    source: str            # 用户给的文件（.pdf/.pptx/.ppt）
    pdf: str = ""          # 真正交给转换引擎的 PDF（PDF 输入时=source，PPT 时=缓存）
    size: int = 0
    pages: int = 0         # 预读的页数（进度估算用；读不到为 0）
    status: TaskStatus = TaskStatus.PENDING
    message: str = ""
    output: str = ""       # 成功后的 .md 路径
    seconds: float = 0.0

    @property
    def needs_prep(self) -> bool:
        return self.source.lower().endswith((".pptx", ".ppt"))


@dataclass
class ConvertOptions:
    """转换批次级的选项（与具体模型无关，对应 batch.py 的批量参数）。"""

    outdir: str = ""               # 空 = 与各 PDF 同目录
    skip_existing: bool = False
    verify: bool = False
    html_preview: bool = False     # 体检时顺带生成 KaTeX 预览页（preview/ 子目录）
    stop_on_error: bool = False
    precheck: bool = False         # 开跑前先做一次 API 连通性自检
    pages: str = ""                # 只处理指定页，如 1-3,7（试参数用）
    report: str = ""               # 空 = 不写 JSON 报表

    def to_dict(self) -> dict:
        return {
            "outdir": self.outdir, "skip_existing": self.skip_existing,
            "verify": self.verify, "html_preview": self.html_preview,
            "stop_on_error": self.stop_on_error, "precheck": self.precheck,
            "pages": self.pages, "report": self.report,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConvertOptions":
        d = d or {}
        return cls(
            outdir=str(d.get("outdir") or ""),
            skip_existing=bool(d.get("skip_existing")),
            verify=bool(d.get("verify")),
            html_preview=bool(d.get("html_preview")),
            stop_on_error=bool(d.get("stop_on_error")),
            precheck=bool(d.get("precheck")),
            pages=str(d.get("pages") or ""),
            report=str(d.get("report") or ""),
        )
