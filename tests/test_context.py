"""context.py の試験。偽の cliamp と偽の窓を使い、画面は開かない。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, isolate_display, run_loop, temp_dir, temp_socket_path  # noqa: E402

isolate_display()

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import GLib

    from cliamp_music import context as context_mod
    from cliamp_music.artwork import ArtworkLoader
    from cliamp_music.client import CliampClient
    from cliamp_music.context import AppContext
    from cliamp_music.protocol import Track, mix_url
    from cliamp_music.radio import RadioBrowser
    from cliamp_music.state import GuiState

    HAVE_GTK = True
except (ImportError, ValueError):  # pragma: no cover
    HAVE_GTK = False

VID = "BoW3OHT6g0s"
YT = Track(path=f"https://www.youtube.com/watch?v={VID}", title="夜明けのバス停", artist="青い灯台")
LOCAL = Track(path="/home/fake/Music/a.flac", title="手元の曲")


class FakeClipboard:
    def __init__(self):
        self.value = None

    def set(self, value):
        self.value = value

    def text(self):
        return self.value


class FakeWindow:
    def __init__(self):
        self.pages = []
        self.toasts = []
        self.clipboard = FakeClipboard()

    def navigate(self, page_id, **params):
        self.pages.append((page_id, params))

    def toast(self, text):
        self.toasts.append(text)

    def get_clipboard(self):
        return self.clipboard


def menu_items(model):
    """Gio.MenuModel を (ラベル, アクション, 対象) の平らな並びに。"""
    out = []
    for i in range(model.get_n_items()):
        label = model.get_item_attribute_value(i, "label", GLib.VariantType.new("s"))
        action = model.get_item_attribute_value(i, "action", GLib.VariantType.new("s"))
        target = model.get_item_attribute_value(i, "target", None)
        if label is not None or action is not None:
            out.append((label.get_string() if label else None, action.get_string() if action else None,
                        target.unpack() if target else None))
        for link in ("section", "submenu"):
            child = model.get_item_link(i, link)
            if child is not None:
                out.extend(menu_items(child))
    return out


@unittest.skipUnless(HAVE_GTK, "GTK がありません")
class ContextTest(unittest.TestCase):
    legacy = False

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path, legacy=self.legacy).start()
        self.client = CliampClient(self.path)
        self.client.set_poll_interval(0.05)
        tmp = temp_dir("cm-ctx-")
        self.window = FakeWindow()
        self.ctx = AppContext(None, window=self.window, client=self.client,
                              artwork=ArtworkLoader(tmp + "/art"), radio=RadioBrowser(),
                              state=GuiState(tmp + "/state.json"))
        self.client.start()
        self.assertTrue(run_loop(lambda: self.ctx.store.status.state != "offline"))

    def tearDown(self):
        self.client.stop()
        self.fake.stop()

    def wait(self, until, timeout=3.0):
        self.assertTrue(run_loop(until, timeout=timeout))

    def activate(self, group, name, value=None):
        group.activate_action(name, value)


class Delegation(ContextTest):
    def test_navigate_and_toast(self):
        self.ctx.navigate("playlist", provider="local", id="Focus", name="Focus")
        self.assertEqual(self.window.pages, [("playlist", {"provider": "local", "id": "Focus", "name": "Focus"})])
        self.ctx.toast("こんにちは")
        self.assertEqual(self.window.toasts, ["こんにちは"])
        self.ctx.window = None
        with mock.patch.object(context_mod, "log") as log:
            self.ctx.toast("窓なし")
            self.ctx.navigate("home")
        log.assert_called_once_with("窓なし")

    def test_parts_are_created(self):
        ctx = AppContext(client=CliampClient(self.path + ".none"), state=GuiState(temp_dir("cm-ctx-") + "/s.json"),
                         artwork=ArtworkLoader(temp_dir("cm-ctx-")))
        self.assertIs(ctx.store.client, ctx.client)
        self.assertIs(ctx.catalog.client, ctx.client)
        self.assertIsInstance(ctx.radio, RadioBrowser)


class Playing(ContextTest):
    def test_play_tracks(self):
        tracks = [YT, LOCAL]
        self.ctx.play_tracks(tracks, 1, {"provider": "youtube", "id": "q", "name": "検索"})
        self.wait(lambda: self.fake.requests_for("replace"))
        request = self.fake.requests_for("replace")[0]
        self.assertEqual(request["index"], 1)
        self.assertEqual(request["source"], {"provider": "youtube", "id": "q", "name": "検索"})
        self.wait(lambda: self.ctx.store.status.index == 1)
        self.ctx.play_tracks([])
        self.assertEqual(len(self.fake.requests_for("replace")), 1)

    def test_play_now_and_station(self):
        self.ctx.play_now(YT)
        self.wait(lambda: self.fake.requests_for("enqueue"))
        self.assertEqual(self.fake.requests_for("enqueue")[0]["mode"], "now")
        self.ctx.start_station(YT)
        self.wait(lambda: self.fake.requests_for("load_provider"))
        request = self.fake.requests_for("load_provider")[0]
        self.assertEqual((request["provider"], request["id"]), ("url", mix_url(VID)))
        self.assertEqual(request["name"], "青い灯台 のステーション")
        self.wait(lambda: self.fake.source.get("provider") == "url")
        self.ctx.start_station(LOCAL)
        self.assertIn("この曲からはステーションを作れません", self.window.toasts)

    def test_failure_is_toasted(self):
        self.ctx.load_provider("radio", "l:99", 0, "無い局")
        self.wait(lambda: any("読み込めませんでした" in t for t in self.window.toasts))


class Menu(ContextTest):
    def test_youtube_track_in_now_playing(self):
        self.ctx.refresh_local_playlists()
        self.wait(lambda: self.ctx.local_playlists == ["ドライブ", "Focus"])
        model, group = self.ctx.track_menu(YT, index=4, context="nowplaying")
        items = menu_items(model)
        labels = [label for label, _, _ in items]
        self.assertEqual(labels, ["次に再生", "最後に再生", "プレイリストに追加", "ドライブ", "Focus",
                                  "新規プレイリスト…", "ステーションを作成", "リンクをコピー", "ブラウザで開く",
                                  "リストから削除"])
        self.assertIn(("ドライブ", "track.add-to-playlist", "ドライブ"), items)
        for name in ("play-next", "play-last", "add-to-playlist", "new-playlist", "start-station",
                     "copy-link", "open-browser", "remove"):
            self.assertTrue(group.has_action(name), name)
            self.assertTrue(group.get_action_enabled(name), name)

    def test_actions(self):
        model, group = self.ctx.track_menu(YT, index=4, context="nowplaying")
        self.activate(group, "play-next")
        self.wait(lambda: self.fake.requests_for("enqueue"))
        self.assertEqual(self.fake.requests_for("enqueue")[0]["mode"], "next")
        self.wait(lambda: any("次に再生します" in t for t in self.window.toasts))
        self.activate(group, "play-last")
        self.wait(lambda: len(self.fake.requests_for("enqueue")) == 2)
        self.assertEqual(self.fake.requests_for("enqueue")[1]["mode"], "end")
        self.activate(group, "add-to-playlist", GLib.Variant.new_string("Focus"))
        self.wait(lambda: self.fake.requests_for("playlist_add"))
        self.assertEqual(self.fake.requests_for("playlist_add")[0]["name"], "Focus")
        self.wait(lambda: "「Focus」に追加しました" in self.window.toasts)
        self.activate(group, "copy-link")
        self.assertEqual(self.window.clipboard.text(), YT.path)
        self.assertIn("リンクをコピーしました", self.window.toasts)
        # 「リストから削除」は見ていた曲の path も送る (違えば cliamp が断る)
        before = len(self.fake.pl)
        self.activate(group, "remove")
        self.wait(lambda: any("削除できませんでした" in t for t in self.window.toasts))
        self.assertEqual(len(self.fake.pl), before)
        self.assertEqual(self.fake.requests_for("remove")[-1]["path"], YT.path)
        shown = Track.from_wire(self.fake.pl.tracks[4])
        _model, group = self.ctx.track_menu(shown, index=4, context="nowplaying")
        self.activate(group, "remove")
        self.wait(lambda: len(self.fake.pl) == before - 1)
        self.assertNotIn(shown.path, [t["path"] for t in self.fake.pl.tracks])

    def test_open_in_browser_uses_uri_launcher(self):
        launched = []

        class FakeLauncher:
            @staticmethod
            def new(uri):
                launcher = mock.Mock()
                launcher.launch.side_effect = lambda parent, cancellable, callback: launched.append((uri, parent))
                return launcher

        _, group = self.ctx.track_menu(YT)
        with mock.patch.object(context_mod.Gtk, "UriLauncher", FakeLauncher):
            self.activate(group, "open-browser")
        self.assertEqual(launched, [(YT.path, None)])

    def test_local_file_has_no_link_or_station(self):
        model, group = self.ctx.track_menu(LOCAL)
        labels = [label for label, _, _ in menu_items(model)]
        self.assertNotIn("リンクをコピー", labels)
        self.assertNotIn("ステーションを作成", labels)
        self.assertNotIn("リストから削除", labels)
        self.assertFalse(group.has_action("copy-link"))

    def focus_track(self, i):
        return Track.from_wire(self.fake.local_playlists["Focus"][i])

    def test_local_playlist_context(self):
        first = self.focus_track(0)
        model, group = self.ctx.track_menu(first, index=0, context="local:Focus")
        self.assertIn("プレイリストから削除", [label for label, _, _ in menu_items(model)])
        before = len(self.fake.local_playlists["Focus"])
        self.activate(group, "remove-from-playlist")
        self.wait(lambda: len(self.fake.local_playlists["Focus"]) == before - 1)
        self.wait(lambda: "「Focus」から削除しました" in self.window.toasts)
        self.assertNotIn(first.path, [t["path"] for t in self.fake.local_playlists["Focus"]])

    def test_remove_uses_the_track_seen_not_a_stale_index(self):
        """見た後で (TUI などで) 並びが変わっていても、見ていた曲を外す (添字の別の曲を消さない)。"""
        second = self.focus_track(1)
        _model, group = self.ctx.track_menu(second, index=1, context="local:Focus")
        with self.fake.lock:
            del self.fake.local_playlists["Focus"][0]  # TUI で先頭を消した (second は 0 番に)
        others = [t["path"] for t in self.fake.local_playlists["Focus"][1:]]
        self.activate(group, "remove-from-playlist")
        self.wait(lambda: second.path not in [t["path"] for t in self.fake.local_playlists["Focus"]])
        self.assertEqual([t["path"] for t in self.fake.local_playlists["Focus"]], others)
        # 見ていた曲がもう無ければ消さずに知らせる
        _model, group = self.ctx.track_menu(second, index=0, context="local:Focus")
        before = [t["path"] for t in self.fake.local_playlists["Focus"]]
        self.activate(group, "remove-from-playlist")
        self.wait(lambda: any("削除しませんでした" in t for t in self.window.toasts))
        self.assertEqual([t["path"] for t in self.fake.local_playlists["Focus"]], before)

    def test_removing_last_track_forgets_the_playlist(self):
        """cliamp は最後の曲を外したプレイリストをファイルごと消す。一覧・メニューからも消える。"""
        self.wait(lambda: "Focus" in self.ctx.local_playlists)
        track = YT
        self.ctx.add_to_playlist("一曲だけ", [track])
        self.wait(lambda: "一曲だけ" in self.ctx.local_playlists)
        changed = []
        self.ctx.connect("local-playlists-changed", lambda *_: changed.append(1))
        stored = Track.from_wire(self.fake.local_playlists["一曲だけ"][0])
        _model, group = self.ctx.track_menu(stored, index=0, context="local:一曲だけ")
        self.activate(group, "remove-from-playlist")
        self.wait(lambda: "一曲だけ" not in self.ctx.local_playlists)
        self.assertNotIn("一曲だけ", self.fake.local_playlists)
        self.assertTrue(changed)
        self.wait(lambda: "「一曲だけ」は空になったので削除しました" in self.window.toasts)
        model, _group = self.ctx.track_menu(YT)
        labels = [label for label, _, _ in menu_items(model)]
        self.assertNotIn("一曲だけ", labels)
        self.assertIn("Focus", labels)

    def test_live_station_cannot_be_added_to_a_playlist(self):
        """ローカルのプレイリストは live と局の絵を持てないので、局は入れさせない。"""
        station = Track(path="https://stream.example.net/jazz.mp3", title="Jazz FM", stream=True, live=True,
                        meta={"art": "https://stream.example.net/favicon.webp"})
        model, group = self.ctx.track_menu(station)
        labels = [label for label, _, _ in menu_items(model)]
        self.assertNotIn("プレイリストに追加", labels)
        self.assertFalse(group.has_action("add-to-playlist"))
        self.assertFalse(group.has_action("new-playlist"))
        self.assertIn("次に再生", labels)

    def test_queue_context(self):
        self.ctx.store.queue_edit("add", index=6)
        self.wait(lambda: self.fake.pl.queue == [6])
        model, group = self.ctx.track_menu(Track.from_wire(self.fake.pl.tracks[6]), index=6, context="queue")
        self.assertIn("待ち行列から外す", [label for label, _, _ in menu_items(model)])
        self.activate(group, "dequeue")
        self.wait(lambda: self.fake.pl.queue == [])

    def test_submenu_updates_when_playlists_change(self):
        """メニューを作った後にプレイリストが増えても、開いているメニューに出る。"""
        model, group = self.ctx.track_menu(YT)
        changed = []
        self.ctx.connect("local-playlists-changed", lambda _c: changed.append(True))
        self.wait(lambda: "Focus" in [label for label, _, _ in menu_items(model)])
        self.activate(group, "add-to-playlist", GLib.Variant.new_string("夜の_ドライブ"))
        self.wait(lambda: "夜の_ドライブ" in self.ctx.local_playlists)
        self.wait(lambda: "夜の__ドライブ" in [label for label, _, _ in menu_items(model)])
        self.assertTrue(changed)


class LegacyContext(ContextTest):
    legacy = True

    def test_unsupported(self):
        self.ctx.play_tracks([YT])
        self.ctx.play_now(YT)
        self.assertEqual(self.window.toasts, ["この cliamp は拡張 IPC に対応していません"] * 2)
        model, group = self.ctx.track_menu(YT, index=0, context="nowplaying")
        for name in ("play-next", "play-last", "add-to-playlist", "new-playlist", "start-station", "remove"):
            self.assertFalse(group.get_action_enabled(name), name)
        self.assertTrue(group.get_action_enabled("copy-link"))


if __name__ == "__main__":
    unittest.main()
