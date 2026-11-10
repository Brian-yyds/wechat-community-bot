"""配置加载：支持 TOML / JSON / YAML。真实配置走 config/local.* 或环境变量，不入库。"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any

from src.logging_util import mask_config, safe_config_text, setup_logging
from src.safety import ForbiddenContentError

# 加载配置时这些键不会被打印。
_CONFIG_SEARCH_NAMES = (
    "local.yaml",
    "local.yml",
    "local.toml",
    "local.json",
    "example.yaml",
    "example.yml",
    "example.toml",
    "example.json",
)


class ConfigError(ValueError):
    """配置无效。"""


def load_file(path: Path) -> dict[str, Any]:
    """按后缀读取配置文件。YAML 需要可选依赖 pyyaml。"""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".toml":
        data = tomllib.loads(text)
    elif suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ConfigError("读取 YAML 需要安装 pyyaml：pip install pyyaml") from exc
        data = yaml.safe_load(text)
    else:
        raise ConfigError(f"不支持的配置格式: {path}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        # JSON/YAML 允许顶层为列表（问答库），包装一下便于统一处理。
        return {"_root_list": data}
    return data


def find_config_path(
    explicit: str | Path | None = None,
    *,
    project_root: Path | None = None,
    env: dict[str, str] | None = None,
) -> Path | None:
    """解析配置路径：显式参数 > 环境变量 WECHAT_BOT_CONFIG > config/local.* > config/example.*。"""
    environ = env if env is not None else os.environ
    root = project_root or Path.cwd()
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise ConfigError(f"指定的配置文件不存在: {path}")
        return path
    env_path = environ.get("WECHAT_BOT_CONFIG")
    if env_path:
        path = Path(env_path)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise ConfigError(f"环境变量 WECHAT_BOT_CONFIG 指向的文件不存在: {path}")
        return path
    config_dir = root / "config"
    for name in _CONFIG_SEARCH_NAMES:
        candidate = config_dir / name
        if candidate.exists():
            return candidate
    return None


def load_config(
    path: str | Path | None = None,
    *,
    project_root: Path | None = None,
    env: dict[str, str] | None = None,
    logger=None,
) -> dict[str, Any]:
    """加载并做浅层环境变量覆盖。加载结果若打印，必须先走脱敏。"""
    root = project_root or Path.cwd()
    environ = env if env is not None else os.environ
    config_path = find_config_path(path, project_root=root, env=environ)
    if config_path is None:
        data: dict[str, Any] = {}
    else:
        data = load_file(config_path)
    data = _apply_env_overrides(data, environ)
    if logger is not None:
        # 只打印脱敏后的配置，绝不输出密钥明文。
        logger.info("已加载配置 %s:\n%s", str(config_path) if config_path else "(默认空配置)", safe_config_text(data))
    # 触发一次 mask，确保调用方若误打印 data 也有工具可用；此处不把明文写进日志。
    _ = mask_config(data)
    return data


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str] | os._Environ) -> dict[str, Any]:
    """允许用环境变量覆盖常用路径，不把密钥写入仓库。"""
    bot = dict(data.get("bot") or {})
    if environ.get("WECHAT_BOT_DATA_DIR"):
        bot["data_dir"] = environ["WECHAT_BOT_DATA_DIR"]
    if bot:
        data = dict(data)
        data["bot"] = bot
    return data


def project_root_from_here() -> Path:
    """本仓库根目录（src 的上一级）。"""
    return Path(__file__).resolve().parent.parent
