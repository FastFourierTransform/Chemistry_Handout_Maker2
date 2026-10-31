# -*- coding: utf-8 -*-
"""底层对接桥（decoupling bridge）—— 整个 GUI 里**唯一** import 底层脚本的模块。

UI 层（widgets / main_window）与工作线程（worker）都只跟这里的纯函数打交道，
不 import batch.py / handout_maker.py / handout_maker2.py。这样：

* 底层脚本升级、改名、换参数 → 只改这一个文件；
* 底层脚本将来想换成 CLI 子进程 / 远程服务 → 换掉本文件的实现即可，
  UI 与 worker 一行都不用动（桥的两头都是纯数据 + 回调）；
* 引擎注册表（ENGINE_SPECS）与参数注册表（models.PARAM_SPECS）共同构成
  「新增一种模型 API」的扩展点：加一个引擎 = 加一条 EngineSpec。

为什么选「进程内模块化调用」而不是 CLI 子进程：
    1. 底层进度（“正在处理第 3 页…”）是 print 出来的，子进程模式只能靠轮询
       stdout 猜测进度；进程内用自定义 stdout 捕获器可以**实时**把每行日志和
       逐页进度变成 Qt 信号（见 worker._LogCapture）。
    2. 进程内可直接复用 batch 的 Job 规划 / .part 语义 / 体检 / JSON 报表，
       不需要发明第二套序列化协议。
    3. 底层本来就是纯函数 + print，无全局状态依赖（verify 的 RESULTS 除外，
       batch.verify_md 已经处理）。
    代价是底层脚本的 bug 会跟 GUI 同进程 —— 用「文件级异常隔离 + .part 语义」
    兜住，跟 batch.py 自己的批量循环是同一套防线。
"""

from __future__ import annotations

import os
import sys
import types

# batch.py 自己也这么干：确保本工程根目录在 sys.path 上（跨目录运行时也找得到）
_ROOT = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_ROOT))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import batch as _batch  # noqa: E402  —— 批量外壳（输入规划/串行循环/.part/体检/报表）
import handout_maker as _GLM  # noqa: E402  —— GLM 引擎（OpenAI 兼容视觉接口）
import handout_maker2 as _GEMINI  # noqa: E402  —— Gemini 引擎

from gui.core.models import ApiProfile, ConvertOptions, PARAM_SPECS, coerce_param  # noqa: E402


# ---------------------------------------------------------------------------
# 引擎注册表 —— 新增一种模型 API 只改这里（UI 的引擎下拉框由它生成）
# ---------------------------------------------------------------------------
class EngineSpec:
    """一个转换引擎：管线模块 + 显示名 + 默认网关/模型/Key 环境变量。

    base_url  / models   —— 引擎默认值（空 = 必须由用户在配置里填写）；
    use_module_defaults  —— True 时留空字段回落到模块内置默认（glm/gemini 的
                            脚本内兜底链）；False 时留空直接报错（OpenAI 兼容
                            引擎没有放之四海皆准的默认网关）；
    require_key          —— True 时 Key 必须来自配置或 api_key_envs 环境变量，
                            **绝不**回落 GLM 脚本里的内置 Key（把智谱的 Key 发给
                            别人的网关等于泄 Key）。
    """

    def __init__(self, engine_id, module, label, note,
                 base_url="", models=(), api_key_envs=(),
                 require_key=False, use_module_defaults=True):
        self.id = engine_id
        self.module = module
        self.label = label
        self.note = note
        self.base_url = base_url
        self.models = tuple(models)
        self.api_key_envs = tuple(api_key_envs)
        self.require_key = require_key
        self.use_module_defaults = use_module_defaults

    @property
    def models_label(self) -> str:
        if self.models:
            return ", ".join(self.models)
        return ", ".join(self.module.DEFAULT_MODELS)

    @property
    def key_envs_label(self) -> str:
        return " / ".join(self.api_key_envs)


ENGINE_SPECS: dict[str, EngineSpec] = {
    "glm": EngineSpec(
        "glm", _GLM, "智谱 GLM（OpenAI 兼容网关）",
        "官方网关；视觉模型 glm-4.6v-flash 免费可用，脚本内置兜底 Key。",
        api_key_envs=("GLM_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY", "ZHIPU_API_KEY"),
    ),
    "gemini": EngineSpec(
        "gemini", _GEMINI, "Google Gemini（需代理）",
        "generativelanguage 网关；国内需本地代理（默认 127.0.0.1:7897）。",
        api_key_envs=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY"),
    ),
    "openai": EngineSpec(
        "openai", _GLM, "OpenAI 兼容（DeepSeek / GPT / vLLM …）",
        "任何 OpenAI 兼容的 /chat/completions 网关：DeepSeek、GPT-4o、Qwen-VL、"
        "中转站、本地 vLLM/Ollama。⚠ 模型必须支持图片输入（视觉模型）——"
        "纯文本模型（如 deepseek-chat）会被接口拒绝，换 gpt-4o / qwen-vl-max 等即可。",
        base_url="", models=("deepseek-chat",),
        api_key_envs=("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        require_key=True, use_module_defaults=False,
    ),
}


class ProfileConfigError(RuntimeError):
    """配置缺失/不合法（消息可直接展示给用户，不需要堆栈）。"""

# 底层逐页进度行（两个引擎同一格式，实测稳定）：
#   “正在处理第 3 页（共 15 页）...” / “正在处理第 1-2 页（共 15 页）...”
#   “正在处理第 3 页...”（--pages 抽页模式没有“共”）
PAGE_PROGRESS_RE = r"正在处理第\s*(\d+)(?:\s*-\s*(\d+))?\s*页(?:（共\s*(\d+)\s*页）)?"


def engine_spec(engine_id: str) -> EngineSpec:
    return ENGINE_SPECS.get(engine_id) or ENGINE_SPECS["glm"]


def resolve_profile(profile: ApiProfile):
    """把配置档案解析成引擎函数要的四件套：G、api_key、base_url、models、proxy。

    * api_key：配置 > 引擎注册的环境变量 >（glm/gemini）脚本内兜底；
      require_key 的引擎（openai 类）找不到 Key 直接抛 ProfileConfigError，
      **不会**错用 GLM 的内置 Key 发给别人的网关。
    * base_url **必须**解析成字符串 —— 引擎入口函数只在 CLI main() 里调
      resolve_base_url，直接调 convert_pdf_to_handout 时 None 会一路传到
      ``base_url.rstrip("/")`` 炸掉（冒烟测试实测抓到的坑）。
    """
    spec = engine_spec(profile.engine)
    G = spec.module

    api_key = profile.api_key.strip() or None
    if api_key is None:
        for env in spec.api_key_envs:
            value = os.environ.get(env)
            if value:
                api_key = value
                break
    if api_key is None and spec.require_key:
        raise ProfileConfigError(
            f"引擎「{spec.label}」需要 API Key：请在配置里填写，"
            f"或设置环境变量 {spec.key_envs_label}")
    if api_key is None:
        api_key = G.resolve_api_key(None)   # glm/gemini 的脚本内兜底链

    base_url = profile.base_url.strip() or spec.base_url or None
    if base_url is None and spec.use_module_defaults:
        base_url = G.resolve_base_url(None)
    if not base_url:
        raise ProfileConfigError(
            f"引擎「{spec.label}」需要填写 API Base URL"
            f"（如 https://api.deepseek.com/v1）")

    if profile.models.strip():
        models = G.parse_models(profile.models)
    elif spec.models:
        models = list(spec.models)
    else:
        models = G.parse_models(None)

    proxy = None
    if profile.engine == "gemini":
        raw = profile.effective_param("proxy")
        proxy = str(raw).strip() if raw not in (None, "") else None
    return G, api_key, base_url, models, proxy


def effective_params(profile: ApiProfile) -> dict:
    """把 params 里所有「本引擎适用」的参数按注册表强转、映射成 kwargs 字典。

    参数键 → kwargs 键的映射完全由 models.PARAM_SPECS 决定（含 invert 语义），
    所以新增参数不需要动这里。
    """
    kw = {}
    for key, spec in PARAM_SPECS.items():
        if profile.engine not in spec.engines:
            continue
        value = profile.effective_param(key)
        if spec.invert:
            value = not value
        kw[spec.kwarg] = value
    return kw


def build_args(profile: ApiProfile, options: ConvertOptions) -> types.SimpleNamespace:
    """按 batch.convert_one 的契约拼出 args 命名空间（与 batch CLI 同构）。"""
    kw = effective_params(profile)
    _, _, _, models, proxy = resolve_profile(profile)
    args = types.SimpleNamespace(
        engine=profile.engine,
        pages=options.pages.strip() or None,
        dpi=kw["dpi"],
        pages_per_request=kw["pages_per_request"],
        no_normalize=kw["no_normalize"],
        temperature=kw["temperature"],
        max_tokens=kw["max_tokens"],
        no_stream=not kw["stream"],
        # thinking 只注册给 glm 引擎；gemini 下取不到就传 None（引擎自会忽略）
        thinking=kw.get("thinking"),
        quiet=False,                      # 我们要日志（worker 会捕获它们）
        # 以下给 batch._emit_report / 汇总用，convert_one 不读
        model=profile.models,
        outdir=options.outdir or None,
        proxy=proxy,
        report=options.report or None,
        verify=options.verify,
        html_preview=options.html_preview,
        skip_existing=options.skip_existing,
        stop_on_error=options.stop_on_error,
    )
    return args


# ---------------------------------------------------------------------------
# 复用 batch.py 的规划 / 体检 / 报表（进程内直接调，不起子进程）
# ---------------------------------------------------------------------------
natural_key = _batch._natural_key
count_pages = _batch.count_pages
looks_complete = _batch._looks_complete


def plan_jobs(pdf_paths, outdir: str = ""):
    """给一批 PDF 排输出路径（同名冲突自动消歧），返回 [batch.Job, ...]。"""
    return _batch.plan_jobs(list(pdf_paths), outdir=outdir or None, quiet=True)


def convert_one(job_source_pdf, dest_path, args, api_key, models, base_url, G, proxy=None):
    """转单个 PDF → dest_path（.part 语义由调用方负责，与 batch 主循环一致）。"""
    return _batch.convert_one(job_source_pdf, dest_path, args, api_key, models,
                              base_url, G, proxy=proxy)


def verify_output(md_path, html_path=None):
    """跑 verify 全套检查：返回 (通过项数, 失败项名列表, 明细日志)。"""
    return _batch.verify_md(md_path, html_path)


def emit_report(args, jobs, results, elapsed, total_pages=0):
    """复用 batch 的 JSON 报表（shape 与 CLI 完全一致）。"""
    _batch._emit_report(args, jobs, results, elapsed, total_pages)


def precheck(profile: ApiProfile):
    """API 连通性自检：真发一张现场生成的小图（几秒出结果）。

    返回 (ok: bool, message: str)。
    """
    try:
        G, api_key, base_url, models, proxy = resolve_profile(profile)
    except ProfileConfigError as exc:
        return False, str(exc)
    try:
        kw = dict(api_key=api_key, base_url=base_url, models=models, stream=True)
        if profile.engine == "gemini":
            kw["proxy"] = proxy
        code = G.check_api(**kw)
        if code == 0:
            return True, f"连通性自检通过（引擎 {profile.engine}，模型 {models[0]}）"
        return False, f"自检未通过（退出码 {code}）"
    except BaseException as exc:  # noqa: BLE001 —— 自检只是探针，什么错都别带崩 GUI
        return False, f"自检异常：{type(exc).__name__}: {exc}"


def check_runtime_deps():
    """启动时体检第三方依赖，返回缺失项描述列表（不阻止启动，只在日志里提示）。"""
    missing = []
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            missing.append("pymupdf（PDF 渲染必需：pip install pymupdf）")
    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("Pillow（图像处理必需：pip install Pillow）")
    try:
        import markdown_it  # noqa: F401
    except ImportError:
        missing.append("markdown-it-py（可选，缺了 verify 的渲染校验会自动跳过）")
    return missing
