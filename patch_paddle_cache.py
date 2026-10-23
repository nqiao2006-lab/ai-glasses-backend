"""Apply a narrow, repeatable Paddle 3.3.1 dataset-cache compatibility patch.

The installed Paddle package is located without importing it, because its import
creates the default dataset directory before the application can configure it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import sysconfig


ORIGINAL = "DATA_HOME = os.path.join(HOME, '.cache', 'paddle', 'dataset')"
PATCHED = (
    "DATA_HOME = os.environ.get('PADDLE_DATA_HOME') or "
    "os.path.join(HOME, '.cache', 'paddle', 'dataset')"
)


def locate_common() -> Path:
    """Find the active virtual environment's Paddle source without importing it."""
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("请使用项目 .venv 中的 Python 运行此脚本，以免修改全局环境。")
    for key in ("purelib", "platlib"):
        directory = sysconfig.get_path(key)
        if directory:
            candidate = Path(directory) / "paddle" / "dataset" / "common.py"
            if candidate.is_file():
                return candidate
    specification = importlib.util.find_spec("paddle")
    if specification and specification.submodule_search_locations:
        prefix = Path(sys.prefix).resolve()
        for location in specification.submodule_search_locations:
            candidate = (Path(location) / "dataset" / "common.py").resolve()
            if candidate.is_relative_to(prefix) and candidate.is_file():
                return candidate
    raise RuntimeError("未找到项目虚拟环境中的 Paddle，请先安装 requirements.txt。")


def patch_common(path: Path) -> bool:
    """Return True after patching, False if already patched; reject unknown code."""
    original_bytes = path.read_bytes()
    content = original_bytes.decode("utf-8")
    if PATCHED in content.splitlines():
        return False
    if content.splitlines().count(ORIGINAL) != 1:
        raise RuntimeError(f"Paddle 缓存代码格式与预期不同，未作修改：{path}")
    updated = content.replace(ORIGINAL, PATCHED, 1)
    path.write_bytes(updated.encode("utf-8"))
    return True


def main() -> int:
    try:
        target = locate_common()
        changed = patch_common(target)
    except (OSError, UnicodeError, RuntimeError) as exc:
        print(f"缓存兼容配置失败：{exc}", file=sys.stderr)
        return 1
    print(f"{'已应用' if changed else '已存在'} Paddle 项目缓存兼容配置：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
