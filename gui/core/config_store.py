# -*- coding: utf-8 -*-
"""配置持久化：JSON 文件 + 原子写入 + 默认值合并 + 轻量迁移。

存储位置：``<项目根>/config/app_config.json``（随项目走，便携）。
写入方式：先写 ``.tmp`` 再 ``os.replace`` —— 中途断电也不会留下半个坏 JSON。

结构（schema_version=1）：

    {
      "schema_version": 1,
      "profiles": [ {name, engine, base_url, api_key, models, params} ],
      "selected_profile": 0,
      "options":    {outdir, skip_existing, verify, ...},
      "ui":         {geometry(b64), splitter, log_visible, auto_scroll}
    }
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from gui.core.models import ApiProfile, ConvertOptions

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = PROJECT_ROOT / "config" / "app_config.json"

DEFAULT_PROFILES = [
    {
        "name": "GLM 智谱（默认）",
        "engine": "glm",
        "base_url": "",
        "api_key": "",
        "models": "glm-4.6v-flash,glm-4v-flash",
        "params": {},
    },
    {
        "name": "Gemini（需代理）",
        "engine": "gemini",
        "base_url": "",
        "api_key": "",
        "models": "gemini-3.8-flash,gemini-2.5-flash",
        "params": {"proxy": "http://127.0.0.1:7897"},
    },
    {
        "name": "DeepSeek / OpenAI 兼容（需填 Key）",
        "engine": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "models": "deepseek-chat",
        "params": {},
    },
]

DEFAULT_OPTIONS = {
    "outdir": "",
    "skip_existing": False,
    "verify": False,
    "html_preview": False,
    "stop_on_error": False,
    "precheck": False,
    "pages": "",
    "report": "",
}

DEFAULT_UI = {
    "geometry": None,          # QWidget.saveGeometry() 的 base64
    "splitter_main": None,     # 左右分栏比例 [w, h]
    "splitter_body": None,     # 上（工作区）下（日志）比例
    "log_visible": True,
    "auto_scroll": True,
}


def _default_config() -> dict:
    return {
        "schema_version": 1,
        "profiles": [dict(p) for p in DEFAULT_PROFILES],
        "selected_profile": 0,
        "options": dict(DEFAULT_OPTIONS),
        "ui": dict(DEFAULT_UI),
    }


class ConfigStore:
    """读写 app_config.json；线程安全（工作线程结束时也会回写报表路径等）。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.Lock()
        self._data: dict | None = None

    # ------------------------------------------------------------------ 读写
    def load(self) -> dict:
        with self._lock:
            if self._data is None:
                self._data = self._load_from_disk()
            return self._data

    def _load_from_disk(self) -> dict:
        cfg = _default_config()
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = self._merge(cfg, raw)
            except (OSError, json.JSONDecodeError) as exc:
                # 配置坏了不致命：用默认值顶上，备份坏文件后继续
                print(f"[配置] 读取 {self.path} 失败（{exc}），使用默认配置")
        # 首次启动或坏文件场景：立刻落一份可用配置（幂等）
        self._write_atomic(cfg)
        return cfg

    @staticmethod
    def _merge(defaults: dict, raw: dict) -> dict:
        """按节浅合并：老配置缺的键补默认值，多出的键原样保留（向前兼容）。"""
        out = dict(defaults)
        out.update({k: v for k, v in raw.items() if k not in defaults})
        for section in ("profiles",):
            if isinstance(raw.get(section), list):
                out[section] = raw[section]
        for section in ("options", "ui"):
            if isinstance(raw.get(section), dict):
                merged = dict(defaults[section])
                merged.update(raw[section])
                out[section] = merged
        return out

    def save(self) -> None:
        """把当前内存状态落盘（原子写）。"""
        with self._lock:
            if self._data is not None:
                self._write_atomic(self._data)

    def _write_atomic(self, cfg: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[配置] 写入 {self.path} 失败（{exc}）")

    # --------------------------------------------------------------- 访问器
    @property
    def profiles(self) -> list[ApiProfile]:
        return [ApiProfile.from_dict(d) for d in self.load()["profiles"]]

    def set_profiles(self, profiles: list[ApiProfile]) -> None:
        cfg = self.load()
        cfg["profiles"] = [p.to_dict() for p in profiles]
        self._clamp_selection(cfg)
        self.save()

    @property
    def selected_index(self) -> int:
        return self._clamp_selection(self.load())

    def set_selected_index(self, index: int) -> None:
        cfg = self.load()
        cfg["selected_profile"] = max(0, int(index))
        self.save()

    @staticmethod
    def _clamp_selection(cfg: dict) -> int:
        n = len(cfg["profiles"])
        idx = cfg.get("selected_profile", 0)
        if not isinstance(idx, int) or idx < 0 or idx >= n:
            idx = 0
        cfg["selected_profile"] = idx
        return idx

    @property
    def options(self) -> ConvertOptions:
        return ConvertOptions.from_dict(self.load().get("options"))

    def set_options(self, options: ConvertOptions) -> None:
        self.load()["options"] = options.to_dict()
        self.save()

    @property
    def ui(self) -> dict:
        return self.load().setdefault("ui", dict(DEFAULT_UI))

    def set_ui(self, **kw) -> None:
        self.load()["ui"].update(kw)
        self.save()

    def patch(self, section: str, key: str, value) -> None:
        """改某个节里的一个键并落盘（避免整块读写）。"""
        cfg = self.load()
        if section not in cfg:
            cfg[section] = {}
        cfg[section][key] = value
        self.save()
