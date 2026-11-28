"""アプリ自身の保存値 (~/.local/state/cliamp-music/state.json)。

再生の状態は cliamp が持つので、ここには検索から保存したプレイリストの参照と画面設定
(最近の検索、窓の大きさ、開いていた右パネルなど) を置く。壊れていても読めなくても既定値で起動する。
書き込みは一時ファイルに書いてから置き換える (途中で落ちても壊れない)。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from typing import Any

from . import log
from .protocol import PlaylistInfo, fold_text, parse_playlist_hits, public_playlist_key

MAX_RECENT_SEARCHES = 12
RIGHT_PANELS = ("", "lyrics", "queue")


def default_path() -> str:
    base = os.environ.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "cliamp-music", "state.json")


class GuiState:
    """画面の保存値。属性を書き換えて save() で保存する。

    add_recent_search() と clear_recent_searches() はその場で保存する。
    ここに無い値は get(key) / set(key, value) で extra に置ける (JSON にできる値だけ)。
    """

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        self.recent_searches: list[str] = []
        self.search_scope: str = "spotify"
        self.right_panel: str = ""
        self.last_page: str = "home"
        self.window_width: int = 1180
        self.window_height: int = 760
        self.window_maximized: bool = False
        self.extra: dict[str, Any] = {}
        self._saved_playlists: dict[tuple[str, str], PlaylistInfo] = {}
        self.load()

    # --- 読み書き -----------------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log(f"{self.path} を読めないので既定値で始めます: {exc}")
            return
        if not isinstance(data, dict):
            log(f"{self.path} の形が違うので既定値で始めます")
            return
        self._load_saved_playlists(data.get("saved_playlists", []))
        searches = data.get("recent_searches")
        if isinstance(searches, list):
            self.recent_searches = [s for s in searches if isinstance(s, str) and s.strip()][:MAX_RECENT_SEARCHES]
        scope = data.get("search_scope")
        if isinstance(scope, str) and scope:
            self.search_scope = scope
        panel = data.get("right_panel")
        if panel in RIGHT_PANELS:
            self.right_panel = panel
        page = data.get("last_page")
        if isinstance(page, str) and page:
            self.last_page = page
        for name, low in (("window_width", 360), ("window_height", 300)):
            value = data.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= 16384:
                setattr(self, name, value)
        if isinstance(data.get("window_maximized"), bool):
            self.window_maximized = data["window_maximized"]
        if isinstance(data.get("extra"), dict):
            self.extra = dict(data["extra"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "recent_searches": list(self.recent_searches),
            "search_scope": self.search_scope,
            "right_panel": self.right_panel,
            "last_page": self.last_page,
            "window_width": int(self.window_width),
            "window_height": int(self.window_height),
            "window_maximized": bool(self.window_maximized),
            "extra": self.extra,
            "saved_playlists": [asdict(hit) for hit in self._saved_playlists.values()],
        }

    def save(self) -> bool:
        """保存する。失敗してもアプリは続ける (False を返してログに書く)。"""
        directory = os.path.dirname(self.path)
        tmp = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            tmp = None
            return True
        except (OSError, TypeError, ValueError) as exc:
            log(f"{self.path} に保存できません: {exc}")
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _load_saved_playlists(self, raw: object) -> None:
        """
        保存済みプレイリストを canonical key でインデックス化して復元する。
        不正な型や重複は無視し、最初の有効な要素のみ保持する。
        """
        self._saved_playlists = {}
        if not isinstance(raw, list):
            return

        for hit in parse_playlist_hits({"playlists": raw}):
            key = public_playlist_key(hit.provider, hit.id)
            if key is None:
                continue
            if key not in self._saved_playlists:
                self._saved_playlists[key] = hit

    def saved_playlists(self) -> list[PlaylistInfo]:
        """保存済みプレイリストの新しいリストを挿入順で返す。"""
        return list(self._saved_playlists.values())

    def is_playlist_saved(self, provider: str, identifier: str) -> bool:
        """指定 provider/id が保存済みかどうかを canonical key で判定する。"""
        key = public_playlist_key(provider, identifier)
        if key is None:
            return False
        return key in self._saved_playlists

    def set_playlist_saved(self, hit: PlaylistInfo, saved: bool) -> bool:
        """
        プレイリストの保存状態を変更し、永続化に成功すれば True を返す。
        不正な入力や IO 失敗時は False を返す。
        """
        if not isinstance(hit, PlaylistInfo):
            return False

        key = public_playlist_key(hit.provider, hit.id)
        if key is None:
            return False

        current = self._saved_playlists.get(key)
        if saved:
            if current is not None:
                return True
        else:
            if current is None:
                return True

        previous = self._saved_playlists
        new_dict = dict(previous)
        if saved:
            new_dict[key] = hit
        else:
            new_dict.pop(key, None)

        self._saved_playlists = new_dict
        if not self.save():
            self._saved_playlists = previous
            return False

        return True

    # --- 最近の検索 -----------------------------------------------------------------

    def add_recent_search(self, q: str) -> None:
        """最近の検索の先頭に足す (同じ語は全角半角・大小を問わず 1 つに。最大 12)。保存もする。"""
        q = " ".join((q or "").split())
        if not q:
            return
        key = fold_text(q)
        self.recent_searches = [q] + [s for s in self.recent_searches if fold_text(s) != key]
        del self.recent_searches[MAX_RECENT_SEARCHES:]
        self.save()

    def clear_recent_searches(self) -> None:
        self.recent_searches = []
        self.save()

    # --- その他の値 -----------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.extra[key] = value
