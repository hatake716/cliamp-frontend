"""state.py の試験 (GTK 不要)。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cliamp_music import state as state_mod  # noqa: E402
from cliamp_music.protocol import PlaylistInfo
from cliamp_music.state import GuiState  # noqa: E402


class GuiStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cm-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "sub", "state.json")

    def write(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_saved_playlists_survive_restart_and_deduplicate_youtube_urls(self):
        spotify = PlaylistInfo("spotify", "4dwAa9994411BByyLLxxaa", "週末", 12, owner="作者", image="https://example.com/a")
        youtube = PlaylistInfo("url", "https://music.youtube.com/playlist?list=PLtest_1", "ランニング", 3)
        alias = PlaylistInfo("url", "https://www.youtube.com/watch?v=123&list=PLtest_1", "別表示")
        self.write(json.dumps({"recent_searches": ["以前の検索"], "extra": {"unchanged": 4}}))
        state = GuiState(self.path)
        self.assertEqual(state.saved_playlists(), [])
        for item in (spotify, youtube, alias):
            self.assertTrue(state.set_playlist_saved(item, True))
        self.assertEqual(state.saved_playlists(), [spotify, youtube])
        reloaded = GuiState(self.path)
        self.assertEqual(reloaded.recent_searches, ["以前の検索"])
        self.assertEqual(reloaded.get("unchanged"), 4)
        self.assertEqual(reloaded.saved_playlists(), [spotify, youtube])
        self.assertTrue(reloaded.is_playlist_saved(alias.provider, alias.id))
        self.assertTrue(reloaded.set_playlist_saved(alias, False))
        self.assertEqual(GuiState(self.path).saved_playlists(), [spotify])

    def test_saved_playlist_write_failure_restores_order_and_previous_disk_value(self):
        state = GuiState(self.path)
        first, second = (PlaylistInfo("spotify", key, key) for key in ("first", "second"))
        for item in (first, second):
            self.assertTrue(state.set_playlist_saved(item, True))
        with mock.patch.object(os, "replace", side_effect=OSError("readonly")), mock.patch.object(state_mod, "log"):
            self.assertFalse(state.set_playlist_saved(first, False))
            self.assertFalse(state.set_playlist_saved(PlaylistInfo("spotify", "third", "third"), True))
        self.assertEqual(state.saved_playlists(), [first, second])
        self.assertEqual(GuiState(self.path).saved_playlists(), [first, second])

    def test_saved_playlist_load_ignores_invalid_refs_and_duplicates(self):
        urls = ["file:///tmp/test", "https://youtube.com:bad/playlist?list=PLtest",
                "https://youtube.com.evil.invalid/playlist?list=PLtest", "https://youtube.com/playlist?list="]
        a = {"provider": "url", "id": "https://youtube.com/playlist?list=PLtest", "name": "正本"}
        b = {"provider": "url", "id": "https://music.youtube.com/playlist?list=PLtest", "name": "別名"}
        self.write(json.dumps({"saved_playlists": [a, b, *[dict(a, id=url) for url in urls],
                                                  {"provider": "local", "id": "does-not-belong"}]}))
        self.assertEqual(GuiState(self.path).saved_playlists(), [PlaylistInfo("url", a["id"], "正本")])

    def test_defaults_when_missing(self):
        state = GuiState(self.path)
        self.assertEqual(state.recent_searches, [])
        self.assertEqual(state.search_scope, "youtube")
        self.assertEqual(state.right_panel, "")
        self.assertEqual((state.window_width, state.window_height, state.window_maximized), (1180, 760, False))
        self.assertFalse(os.path.exists(self.path))  # 読むだけでは書かない

    def test_corrupt_file_starts_with_defaults(self):
        for text in ("{not json", "[1, 2]", "", "null"):
            with self.subTest(text=text):
                self.write(text)
                with mock.patch.object(state_mod, "log"):
                    state = GuiState(self.path)
                self.assertEqual(state.recent_searches, [])
                self.assertEqual(state.window_width, 1180)

    def test_wrong_types_are_ignored(self):
        self.write(json.dumps({"recent_searches": ["ok", 3, "", None, "b"], "search_scope": 5,
                               "right_panel": "weird", "window_width": "big", "window_height": 99999,
                               "window_maximized": "yes", "last_page": ["x"], "extra": [1]}))
        state = GuiState(self.path)
        self.assertEqual(state.recent_searches, ["ok", "b"])
        self.assertEqual(state.search_scope, "youtube")
        self.assertEqual(state.right_panel, "")
        self.assertEqual((state.window_width, state.window_height), (1180, 760))
        self.assertFalse(state.window_maximized)
        self.assertEqual(state.last_page, "home")
        self.assertEqual(state.extra, {})

    def test_round_trip_and_atomic_write(self):
        state = GuiState(self.path)
        state.search_scope = "spotify"
        state.right_panel = "lyrics"
        state.last_page = "radio"
        state.window_width, state.window_height, state.window_maximized = 1400, 900, True
        state.set("volume_popover", {"open": False})
        self.assertTrue(state.save())
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.path)) if n != "state.json"], [])
        again = GuiState(self.path)
        self.assertEqual((again.search_scope, again.right_panel, again.last_page), ("spotify", "lyrics", "radio"))
        self.assertEqual((again.window_width, again.window_height, again.window_maximized), (1400, 900, True))
        self.assertEqual(again.get("volume_popover"), {"open": False})
        self.assertIsNone(again.get("nothing"))

    def test_save_failure_is_reported_not_raised(self):
        blocker = os.path.join(self.dir, "file")
        Path(blocker).write_text("x")
        with mock.patch.object(state_mod, "log") as log:
            state = GuiState(os.path.join(blocker, "state.json"))  # 親がファイルなので作れない
            self.assertFalse(state.save())
        log.assert_called()
        # JSON にできない値で失敗しても、前の中身は壊れず一時ファイルも残らない。
        good = GuiState(self.path)
        good.last_page = "radio"
        self.assertTrue(good.save())
        good.set("bad", object())
        good.last_page = "search"
        with mock.patch.object(state_mod, "log"):
            self.assertFalse(good.save())
        self.assertEqual(GuiState(self.path).last_page, "radio")
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["state.json"])

    def test_recent_searches(self):
        state = GuiState(self.path)
        for q in ["a", "b", "  c  ", "", "Ｂ", "A"]:
            state.add_recent_search(q)
        self.assertEqual(state.recent_searches, ["A", "Ｂ", "c"])
        for i in range(20):
            state.add_recent_search(f"q{i}")
        self.assertEqual(len(state.recent_searches), 12)
        self.assertEqual(state.recent_searches[0], "q19")
        self.assertEqual(GuiState(self.path).recent_searches, state.recent_searches)  # その場で保存
        state.clear_recent_searches()
        self.assertEqual(GuiState(self.path).recent_searches, [])

    def test_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.dir}):
            self.assertEqual(state_mod.default_path(), os.path.join(self.dir, "cliamp-music", "state.json"))
            self.assertEqual(GuiState().path, os.path.join(self.dir, "cliamp-music", "state.json"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative/dir", "HOME": "/home/x"}):
            self.assertEqual(state_mod.default_path(), "/home/x/.local/state/cliamp-music/state.json")


if __name__ == "__main__":
    unittest.main()
