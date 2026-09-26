"""ミュージック — cliamp を macOS 27 のミュージック風に操作する GTK フロントエンド。

ここには GTK に依存しない定数と小道具だけを置く (tests/ から画面なしで読めるように)。
"""

from __future__ import annotations

import os
import sys

APP_ID = "org.nixos.Music"
APP_NAME = "ミュージック"
VERSION = "0.1.0"

# パッケージのディレクトリ。style/ や icons/ をここから探す。
HERE = os.path.dirname(os.path.abspath(__file__))


def log(*parts: object) -> None:
    """標準エラーへ `cliamp-music: …` の形で 1 行書く。"""
    text = " ".join(str(part) for part in parts)
    print(f"cliamp-music: {text}", file=sys.stderr, flush=True)
