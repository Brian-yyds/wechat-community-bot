# -*- coding: utf-8 -*-
"""在开发 src/ 中发现各方案适配器；找不到则由用例 skip 并说明原因。"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Callable

from adapter import use_mock

REPO_ROOT = Path(__file__).resolve().parents[2]

# 方案 -> 候选模块与类名（开发落地后只要名字能对上即可被自动发现）
SCHEME_SPECS = {
    "local_db_reader": {
        "title": "本地库只读（读消息）",
        "modules": [
            "src.adapters.local_db",
            "src.adapters.local_db_reader",
            "src.adapters.db_reader",
            "src.adapters.wcdb",
            "src.adapters.readonly_db",
            "src.adapters.sqlite_reader",
            "src.bot_api",
        ],
        "class_names": [
            "LocalDbReader",
            "LocalDatabaseReader",
            "ReadonlyDbAdapter",
            "WCDBReader",
            "LocalMsgDbReader",
            "SqliteReader",
        ],
        "required_methods": ("poll",),
        "optional_methods": ("iter_new", "get_cursor", "set_cursor"),
    },
    "uia_sender": {
        "title": "UIA 发送",
        "modules": [
            "src.adapters.uia",
            "src.adapters.uia_sender",
            "src.adapters.ui_send",
            "src.adapters.pyweixin",
            "src.bot_api",
        ],
        "class_names": [
            "UiaSender",
            "UIAutomationSender",
            "PyweixinSender",
            "UiSender",
            "WindowsUiaSender",
        ],
        "required_methods": ("send",),
        "optional_methods": (),
    },
    "notification": {
        "title": "通知辅助",
        "modules": [
            "src.adapters.notification",
            "src.adapters.notify",
            "src.adapters.toast",
            "src.adapters.notifier",
            "src.bot_api",
        ],
        "class_names": [
            "NotificationAssist",
            "NotificationListener",
            "ToastListener",
            "NotifyAssist",
            "NotificationAdapter",
        ],
        "required_methods": ("poll_hints",),
        "optional_methods": ("is_complete_source", "completeness_guaranteed"),
    },
    "ocr_fallback": {
        "title": "OCR 降级",
        "modules": [
            "src.adapters.ocr",
            "src.adapters.ocr_fallback",
            "src.bot_api",
        ],
        "class_names": [
            "OcrFallback",
            "OcrAdapter",
            "OcrRecognizer",
            "OcrFallbackAdapter",
        ],
        "required_methods": ("recognize",),
        "optional_methods": ("should_fallback",),
    },
}


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _class_matches(cls, required) -> bool:
    return all(hasattr(cls, m) for m in required)


def discover_scheme(scheme: str) -> tuple[Any, str]:
    """返回 (cls_or_factory, reason)。找不到时 cls 为 None。"""
    spec = SCHEME_SPECS[scheme]
    tried = []
    # 也扫 src/adapters 目录里尚未列入的模块
    adapters_dir = REPO_ROOT / "src" / "adapters"
    extra_modules = list(spec["modules"])
    if adapters_dir.exists():
        for p in sorted(adapters_dir.glob("*.py")):
            if p.name.startswith("_"):
                continue
            mod = f"src.adapters.{p.stem}"
            if mod not in extra_modules:
                extra_modules.append(mod)

    for mod_name in extra_modules:
        mod = _try_import(mod_name)
        if mod is None:
            tried.append(f"{mod_name}(未安装)")
            continue
        for cls_name in spec["class_names"]:
            cls = getattr(mod, cls_name, None)
            if cls is None:
                continue
            if inspect.isclass(cls) and _class_matches(cls, spec["required_methods"]):
                return cls, f"发现 {mod_name}.{cls_name}"
            if callable(cls) and cls_name:
                # 工厂函数
                return cls, f"发现 {mod_name}.{cls_name}（工厂）"
            tried.append(f"{mod_name}.{cls_name}（缺方法）")
        # 宽松：模块内任意类具备必要方法
        for name, obj in vars(mod).items():
            if not inspect.isclass(obj):
                continue
            if obj.__module__ != mod_name:
                continue
            if _class_matches(obj, spec["required_methods"]) and name not in {
                "Adapter",
                "MockAdapter",
                "Message",
                "Event",
                "Group",
            }:
                # 避免把通用 Adapter 误当成专用方案
                if scheme == "local_db_reader" and not any(
                    k in name.lower() for k in ("db", "sqlite", "wcdb", "readonly", "local")
                ):
                    continue
                if scheme == "uia_sender" and not any(
                    k in name.lower() for k in ("uia", "ui", "send", "pyweixin")
                ):
                    continue
                if scheme == "notification" and not any(
                    k in name.lower() for k in ("notif", "toast", "hint")
                ):
                    continue
                if scheme == "ocr_fallback" and "ocr" not in name.lower():
                    continue
                return obj, f"发现 {mod_name}.{name}"
        tried.append(mod_name)

    reason = (
        f"开发尚未交付「{spec['title']}」适配器。"
        f"请在 src/adapters/ 下实现类 {', '.join(spec['class_names'][:3])}，"
        f"并提供方法 {', '.join(spec['required_methods'])}。"
        f"已尝试: {', '.join(tried[:8])}"
    )
    return None, reason


def load_scheme_factory(scheme: str) -> tuple[Callable, str, str]:
    """返回 (factory, source, reason)。

    source 为 'mock' 或 'real'。mock 模式下始终用 tests/mock；
    real 模式有实现则用开发模块，否则 factory=None。
    """
    if use_mock():
        from mock import adapters as mock_adapters

        mapping = {
            "local_db_reader": mock_adapters.LocalDbReader,
            "uia_sender": mock_adapters.UiaSender,
            "notification": mock_adapters.NotificationAssist,
            "ocr_fallback": mock_adapters.OcrFallback,
        }
        return mapping[scheme], "mock", "脚手架假实现（非产品）"
    cls, reason = discover_scheme(scheme)
    if cls is None:
        return None, "missing", reason
    return cls, "real", reason
