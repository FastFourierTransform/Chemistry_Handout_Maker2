# -*- coding: utf-8 -*-
"""桥接层引擎解析的离线断言（不联网）：三种引擎的默认值/必填校验。"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import handout_maker
import handout_maker2

from gui.core import bridge
from gui.core.models import ApiProfile

failures = []


def check(name, cond, detail=""):
    print(("  [通过] " if cond else "  [失败] ") + name + ((" " + detail) if detail else ""))
    if not cond:
        failures.append(name)


print("① GLM 默认解析（留空回落模块内置兜底）")
G, key, url, models, proxy = bridge.resolve_profile(ApiProfile(name="g", engine="glm"))
check("base_url = 模块默认", url == handout_maker.DEFAULT_BASE_URL, url)
check("api_key = 脚本内置兜底", key == handout_maker.DEFAULT_API_KEY)
check("models = 默认降级链", models == list(handout_maker.DEFAULT_MODELS), str(models))
check("proxy 为 None", proxy is None)

print("② Gemini 默认解析")
G2, key2, url2, models2, proxy2 = bridge.resolve_profile(
    ApiProfile(name="g2", engine="gemini", params={"proxy": "http://127.0.0.1:7897"}))
check("base_url = gemini 默认", url2 == handout_maker2.DEFAULT_BASE_URL, url2)
check("models = gemini 默认链", models2 == list(handout_maker2.DEFAULT_MODELS), str(models2))
check("proxy 取配置值", proxy2 == "http://127.0.0.1:7897", str(proxy2))

print("③ OpenAI 兼容引擎（DeepSeek）")
try:
    bridge.resolve_profile(ApiProfile(name="ds", engine="openai", api_key=""))
    check("缺 Key 必须报错", False)
except bridge.ProfileConfigError as exc:
    check("缺 Key 必须报错", "API Key" in str(exc), str(exc))
try:
    bridge.resolve_profile(ApiProfile(name="ds", engine="openai",
                                       api_key="sk-test", base_url=""))
    check("缺 Base URL 必须报错", False)
except bridge.ProfileConfigError as exc:
    check("缺 Base URL 必须报错", "Base URL" in str(exc), str(exc))
os.environ["DEEPSEEK_API_KEY"] = "sk-from-env"
G3, key3, url3, models3, proxy3 = bridge.resolve_profile(
    ApiProfile(name="ds", engine="openai",
               base_url="https://api.deepseek.com/v1", api_key=""))
check("Key 从 DEEPSEEK_API_KEY 环境变量解析", key3 == "sk-from-env", str(key3))
check("base_url 用配置值", url3 == "https://api.deepseek.com/v1", url3)
check("models 用引擎默认模板", models3 == ["deepseek-chat"], str(models3))
del os.environ["DEEPSEEK_API_KEY"]

print("④ 通用参数对 openai 引擎生效（thinking/proxy 不适用）")
from gui.core.bridge import effective_params
kw = effective_params(ApiProfile(name="ds", engine="openai",
                                 params={"temperature": "0.3", "thinking": "enabled"}))
check("temperature 覆盖生效", kw["temperature"] == 0.3, str(kw))
check("thinking 被过滤（仅 GLM）", "thinking" not in kw, str(kw))
check("proxy 被过滤（仅 Gemini）", "proxy" not in kw, str(kw))
check("dpi 等默认值齐备", kw.get("dpi") == 150 and kw.get("max_tokens") == 4096, str(kw))

print("\n" + ("全部通过 ✅" if not failures else f"{len(failures)} 项失败 ❌"))
sys.exit(1 if failures else 0)
