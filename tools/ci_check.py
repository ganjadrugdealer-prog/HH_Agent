# -*- coding: utf-8 -*-
"""Проверка перед сборкой: ставит одобренное ядро и гоняет tools/selfcheck.py.

Запускается в CI до PyInstaller. Если что-то расходится с движком —
сборка падает здесь, а не у пользователя после установки.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def approved_version() -> str | None:
    try:
        data = json.loads((ROOT / "remote_config.json").read_text(encoding="utf-8"))
        return data.get("approved_core_version")
    except Exception:
        return None


def main() -> int:
    version = approved_version()
    spec = f"hh-applicant-tool=={version}" if version else "hh-applicant-tool"
    print(f"[ci_check] устанавливаю ядро: {spec}")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", spec])
    if rc:
        print("[ci_check] не удалось поставить ядро")
        return rc

    subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "pyflakes"])

    print("[ci_check] прогон selfcheck")
    return subprocess.call([sys.executable, str(ROOT / "tools" / "selfcheck.py"),
                            str(ROOT)])


if __name__ == "__main__":
    raise SystemExit(main())
