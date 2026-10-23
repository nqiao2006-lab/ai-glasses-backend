"""Configure the medicine OCR cache without changing the user's home directory."""

from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def configure_runtime() -> Path:
    """Prepare configured caches before importing Paddle; return the cache root."""
    config_path = ROOT / "runtime_config.json"
    config = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法读取运行配置 {config_path}：{exc}") from exc
        if not isinstance(config, dict):
            raise RuntimeError("runtime_config.json 应是包含 cache_dir 的 JSON 对象。")

    cache_setting = config.get("cache_dir", "models")
    if not isinstance(cache_setting, str) or not cache_setting.strip():
        raise RuntimeError("runtime_config.json 的 cache_dir 必须是非空目录路径。")
    cache_root = Path(cache_setting).expanduser()
    if not cache_root.is_absolute():
        cache_root = ROOT / cache_root
    cache_root = cache_root.resolve()
    cache_paths = {
        "PADDLE_HOME": cache_root / "paddle",
        "PADDLE_DATA_HOME": cache_root / "paddle" / "dataset",
        "PADDLE_PDX_CACHE_HOME": cache_root / "paddlex",
    }
    for variable, path in cache_paths.items():
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    return cache_root
