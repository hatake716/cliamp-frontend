"""pages/*.py (各ページ) の試験。偽の cliamp に本物の AppContext で繋ぎ、ページを窓に置いて試す。

ページは表示 (map) されてから読み込むので、X の画面 (Xvfb など、:10 以上) が
要る。無ければ全部飛ばす。例:

    nix develop path:. -c xvfb-run -n 95 python3 -m unittest tests.test_pages -v

ネットワークには出ない (アートワークの urlopen は失敗させ、Radio Browser は
手元の局に差し替える)。利用者の cliamp のソケット・状態・キャッシュには触れない。
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import unittest
import weakref
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import (  # noqa: E402
    SPOTIFY_SEARCH_BLOCKED,
    SPOTIFY_SEARCH_REFUSED_PAGE,
    FakeCliamp,
    isolate_display,
    run_loop,
    temp_socket_path,
)

isolate_display()

HAVE_DISPLAY = False
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Adw, GdkPixbuf, GLib, Gtk

    if os.environ.get("DISPLAY") and Gtk.init_check():
        Adw.init()
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        HAVE_DISPLAY = True
except (ImportError, ValueError):  # pragma: no cover
    pass

if HAVE_DISPLAY:
    import cliamp_music.artwork as artwork_mod
    from cliamp_music.artwork import ArtworkLoader
    from cliamp_music.client import CliampClient, send_once
    from cliamp_music.context import AppContext
    from cliamp_music.pages.home import WEB_ONLY_NOTE, HomePage, provider_playback, weak_call
    from cliamp_music.pages.nowplaying import NowPlayingListPage
    from cliamp_music.pages.playlists import PlaylistDetailPage, PlaylistsPage
    from cliamp_music.pages.radio import RadioPage
    from cliamp_music.pages.recent import RecentPage
    from cliamp_music.pages.search import SearchPage
    from cliamp_music.protocol import SPOTIFY_SEARCH_BLOCKED_TITLE, Response, Track, is_youtube_bridge, parse_tracks
    from cliamp_music.state import GuiState

SKIP = "X の画面 (Xvfb の :10 以上の DISPLAY) がありません"


def _no_network(*_args, **_kwargs):
    raise OSError("試験ではネットワークに出ません")


_saved_urlopen = None


def setUpModule():  # noqa: N802
    global _saved_urlopen
    if HAVE_DISPLAY:
        _saved_urlopen = artwork_mod.urlopen
        artwork_mod.urlopen = _no_network


def tearDownModule():  # noqa: N802
    if HAVE_DISPLAY and _saved_urlopen is not None:
        artwork_mod.urlopen = _saved_urlopen


class FakeWindow:
    """ctx.window の代わり (navigate と toast を覚えるだけ)。"""

    def __init__(self):
        self.pages = []
        self.toasts = []

    def navigate(self, page_id, **params):
        self.pages.append((page_id, params))

    def toast(self, text):
        self.toasts.append(text)


def stations():
    return [
        Track(path="https://stream.example.net/a.mp3", title="港町 FM & <ジャズ>", stream=True, live=True,
              meta=(("radio.country", "日本"),)),
        Track(path="https://stream.example.net/b.mp3", title="Tokyo Lo-fi Radio", stream=True, live=True,
              meta=(("radio.country", "日本"),)),
    ]


class PageCase(unittest.TestCase):
    """偽の cliamp と AppContext を用意し、ページを窓の NavigationView に置く。"""

    fake_options: dict = {}

    @classmethod
    def setUpClass(cls):
        if not HAVE_DISPLAY:
            raise unittest.SkipTest(SKIP)
        cls.tmp = tempfile.mkdtemp(prefix="cm-pages-")
        # 曲に手元の絵 (file://) を付け、アートワークがネットワークへ取りに行かないようにする
        cls.art_dir = os.path.join(cls.tmp, "covers")
        os.makedirs(cls.art_dir)
        for n, color in enumerate((0xe0406080, 0x40a0e0ff, 0x60c070ff)):
            pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 32, 32)
            pixbuf.fill(color)
            pixbuf.savev(os.path.join(cls.art_dir, f"cover-{n}.png"), "png", [], [])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # 偽の cliamp は試験ごとに作り直す (前の試験の削除や差し替えを持ち越さない)
        self.sock = temp_socket_path()
        self.fake = FakeCliamp(self.sock, art_dir=self.art_dir, **self.fake_options).start()
        self.addCleanup(shutil.rmtree, os.path.dirname(self.sock), True)
        self.addCleanup(self.fake.stop)
        self.window_stub = FakeWindow()
        self.state_path = os.path.join(self.tmp, f"state-{self._testMethodName}.json")
        self.ctx = AppContext(None, window=self.window_stub, client=CliampClient(self.sock),
                              state=GuiState(self.state_path),
                              artwork=ArtworkLoader(os.path.join(self.tmp, "art")))
        radio = self.ctx.radio
        radio.by_country = lambda code, cb, limit=40: GLib.idle_add(lambda: cb(stations()) and False)
        radio.top = lambda cb, limit=40: GLib.idle_add(lambda: cb(stations()[::-1]) and False)
        radio.search = lambda q, cb, limit=60: GLib.idle_add(
            lambda: cb([s for s in stations() if q.casefold() in s.title.casefold()]) and False)
        self.ctx.client.start()
        store = self.ctx.store
        self.assertTrue(run_loop(lambda: store.connected and store.status.seq > 0, 8), "繋がりません")
        if not self.fake_options.get("empty"):
            self.assertTrue(run_loop(lambda: bool(store.playlist.tracks) and bool(store.history), 8),
                            "リストか履歴を取れません")
        self.window = Gtk.Window()
        self.window.set_default_size(1100, 700)
        self.nav = Adw.NavigationView()
        self.window.set_child(self.nav)
        self.window.present()
        self.fake.requests.clear()

    def tearDown(self):
        self.window.destroy()
        self.ctx.client.stop()
        run_loop(lambda: False, 0.05)

    def show(self, page):
        self.nav.replace([page])
        self.assertTrue(run_loop(page.get_mapped, 5), "ページが表示されません")
        return page

    def wait(self, condition, timeout=5.0, message="待ちきれません"):
        self.assertTrue(run_loop(condition, timeout), message)

    def requests(self, cmd):
        return self.fake.requests_for(cmd)


class HomePageTest(PageCase):
    def test_recent_card_replaces_with_history(self):
        page = self.show(HomePage(self.ctx))
        self.wait(lambda: len(page.recent.items()) > 0)
        cards = page.recent.items()
        cards[2].emit("clicked")
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 2)
        self.assertEqual(request["source"]["name"], "最近再生した項目")
        self.assertEqual(request["tracks"][2]["path"], cards[2].subject.path)

    def test_picks_start_station_and_open_nowplaying(self):
        page = self.show(HomePage(self.ctx))
        self.wait(lambda: any(getattr(c, "pick_kind", "") == "station" for c in page.picks.items()))
        kinds = [c.pick_kind for c in page.picks.items()]
        self.assertIn("nowplaying", kinds)
        station = next(c for c in page.picks.items() if c.pick_kind == "station")
        station.emit("clicked")
        self.wait(lambda: self.requests("load_provider"))
        request = self.requests("load_provider")[-1]
        self.assertEqual(request["provider"], "url")
        self.assertIn("list=RD", request["id"])
        nowplaying = next(c for c in page.picks.items() if c.pick_kind == "nowplaying")
        nowplaying.emit("clicked")
        self.assertEqual(self.window_stub.pages[-1], ("nowplaying", {"reveal": True}))

    def test_picks_are_kept_when_track_changes(self):
        store = self.ctx.store
        page = self.show(HomePage(self.ctx))
        # プロバイダー (「cliamp ラジオ」のカード) の答えが届くまで待ってから数える
        # (届く順は決まっていない。届いて並びが変わるのは作り直して正しい)
        self.wait(lambda: page._pending == 0
                  and any(getattr(c, "pick_kind", "") == "nowplaying" for c in page.picks.items()))
        # 同じカードかは印で見る (PyGObject は Python の包みを作り直すことがあり、id() は変わる。
        # 包みの属性は GObject の側に残るので、作り直していないカードには印が残る)
        before = len(page.picks.items())
        for c in page.picks.items():
            c.kept_marker = True
        card = next(c for c in page.picks.items() if c.pick_kind == "nowplaying")
        old_path = card.pick_subject.path
        store.next()
        self.wait(lambda: card.pick_subject.path != old_path, message="再生中のリストの絵が次の曲に替わりません")
        items = page.picks.items()
        self.assertEqual(len(items), before)
        self.assertTrue(all(getattr(c, "kept_marker", False) for c in items), "曲が変わっただけでカードを作り直しました")

    def test_recent_shelf_keeps_scroll_and_cards(self):
        """履歴の先頭が入れ替わっても (曲が変わるたび) 棚を先頭へ戻さず、カードを使い回す。"""
        store = self.ctx.store
        page = self.show(HomePage(self.ctx))
        self.window.set_default_size(700, 700)  # 棚を横に送れる幅にする
        self.wait(lambda: len(page.recent.items()) == len({t.path for t in store.history}))
        for card in page.recent.items():
            card.kept_marker = True
        adj = page.recent._scroller.get_hadjustment()
        self.wait(lambda: adj.get_upper() > adj.get_page_size() + 200)
        adj.set_value(200)
        # 新しい曲を履歴の先頭に足した (cliamp が記録した) ことにして知らせる
        newest = Track(path="https://www.youtube.com/watch?v=newhistory1", title="いま聞いた曲", artist="誰か",
                       played_at="2026-09-26T10:00:00Z")
        store.history = [newest] + list(store.history)
        store.emit("history-changed")
        run_loop(lambda: False, 0.3)
        items = page.recent.items()
        self.assertEqual(items[0].subject.path, newest.path)
        self.assertTrue(all(getattr(c, "kept_marker", False) for c in items[1:]), "カードを作り直しました")
        self.assertGreater(adj.get_value(), 0.0, "棚が先頭へ戻りました")
        # 押したときの添字は並びに合う
        items[1].emit("clicked")
        self.wait(lambda: self.requests("replace"))
        self.assertEqual(self.requests("replace")[-1]["index"], 1)

    def test_radio_shelf_and_playlists_shelf(self):
        page = self.show(HomePage(self.ctx))
        self.wait(lambda: len(page.stations.items()) == 2 and len(page.playlists.items()) >= 2)
        page.stations.items()[0].emit("clicked")
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["source"], {"provider": "radio-browser", "id": "https://stream.example.net/a.mp3",
                                             "name": "港町 FM & <ジャズ>"})
        names = [c.playlist_info.name for c in page.playlists.items()]
        self.assertNotIn("Recently Played", names)
        page.playlists.items()[0].emit("clicked")
        self.assertEqual(self.window_stub.pages[-1][0], "playlist")

    def test_page_is_released_after_removal(self):
        box = {"page": HomePage(self.ctx)}
        self.show(box["page"])
        self.wait(lambda: len(box["page"].recent.items()) > 0 and box["page"]._pending == 0)
        ref = weakref.ref(box["page"])
        self.assertGreater(len(box["page"]._handler_ids), 0)
        self.nav.replace([Adw.NavigationPage(title="空", tag="blank", child=Gtk.Label())])
        box.clear()
        for _ in range(5):
            run_loop(lambda: False, 0.1)
            gc.collect()
        self.assertIsNone(ref(), "外したページが解放されません (store か ctx が掴んでいる)")


class SearchPageTest(PageCase):
    def search_requests(self):
        return [r for r in self.requests("search")]

    def test_debounce_and_enter(self):
        page = self.show(SearchPage(self.ctx))
        page.entry.set_text("夜")
        run_loop(lambda: False, 0.3)
        page.entry.set_text("夜の街")
        run_loop(lambda: False, 0.4)
        self.assertEqual(self.search_requests(), [], "0.6 秒待たずに検索しました")
        self.wait(lambda: self.search_requests(), 2)
        run_loop(lambda: False, 0.3)
        self.assertEqual([r["query"] for r in self.search_requests()], ["夜の街"])
        self.wait(lambda: page.results.state == "content")
        self.assertEqual(self.ctx.state.recent_searches, [], "入力の停止だけで最近の検索に足しました")
        # Enter は待たずに検索し、最近の検索にも足す
        page.entry.set_text("雨 & <傘>")
        page.entry.emit("activate")
        run_loop(lambda: False, 0.1)
        self.assertEqual(self.search_requests()[-1]["query"], "雨 & <傘>")
        self.assertEqual(self.ctx.state.recent_searches[0], "雨 & <傘>")

    def test_spotify_scope_waits_longer_and_asks_for_20(self):
        """Spotify の範囲は入力停止を 0.9 秒待ち (開発者の利用枠を分け合うので打ちながらの検索を減らす)、
        1 回に 20 件を頼む (Web API は 1 回 10 件まで。cliamp が 2 回に分ける)。Enter は待たない。"""
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        self.assertEqual(page.debounce_ms, 900)
        calls = []
        search = self.ctx.catalog.search

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return search(*args, **kwargs)

        self.ctx.catalog.search = spy
        page.entry.set_text("夜")
        run_loop(lambda: False, 0.3)
        page.entry.set_text("夜の街")
        run_loop(lambda: False, 0.7)
        self.assertEqual(self.search_requests(), [], "Spotify の範囲で 0.9 秒待たずに検索しました")
        self.wait(lambda: self.search_requests(), 2)
        run_loop(lambda: False, 0.3)
        self.assertEqual([(r["provider"], r["query"], r["limit"]) for r in self.search_requests()],
                         [("spotify", "夜の街", 20)])
        self.assertEqual(len(calls), 1, "打ち直しの前の語も頼みました")
        self.assertEqual(calls[0][1].get("lane"), "search-page", "打ちながらの検索に lane が付いていません")
        self.wait(lambda: page.results.state == "content")
        self.assertEqual(len(page.results_tracks), 20)
        # Enter は待たない
        page.entry.set_text("雨")
        page.entry.emit("activate")
        run_loop(lambda: False, 0.1)
        self.assertEqual(self.search_requests()[-1]["query"], "雨")
        self.assertEqual(self.search_requests()[-1]["limit"], 20)
        # YouTube の範囲は今までどおり (0.6 秒・25 件)
        page.set_scope("youtube")
        self.assertEqual(page.debounce_ms, 600)
        self.wait(lambda: self.search_requests()[-1]["provider"] == "youtube")
        self.assertEqual(self.search_requests()[-1]["limit"], 25)

    def test_spotify_quota_is_explained(self):
        self.fake.spotify_error = ("spotify: search: spotify: Spotify quota exceeded for this developer account; "
                                   "retry after 1h0m0s")
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        page.set_query("雨")
        self.wait(lambda: page.results.state == "empty")
        empty = page.results.empty
        self.assertEqual(empty.title_label.get_text(), "検索できませんでした")
        self.assertEqual(empty.description_label.get_text(),
                         "Spotify の開発者向けの利用枠を使い切りました。1 時間ほどしてから試してください")
        # 覚えた断りで、打ちながらの検索は Spotify に頼み続けない (枠を減らさない)
        page.set_query("雪")
        self.wait(lambda: page.results.state == "empty" and page._query == "雪")
        run_loop(lambda: False, 0.2)
        self.assertEqual(len(self.search_requests()), 1)
        self.assertEqual(empty.description_label.get_text(),
                         "Spotify の開発者向けの利用枠を使い切りました。1 時間ほどしてから試してください")

    def test_result_row_plays_all_results(self):
        page = self.show(SearchPage(self.ctx))
        page.set_query("星")
        self.wait(lambda: page.results.state == "content")
        rows = page.all_songs.rows()
        self.assertEqual(len(rows), len(page.results_tracks))
        page.all_songs.emit("row-activated", rows[3])
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 3)
        self.assertEqual(request["source"], {"provider": "youtube", "name": "「星」"})
        self.assertEqual(len(request["tracks"]), len(rows))

    def test_scope_is_saved_and_reused(self):
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        self.assertEqual(self.ctx.state.search_scope, "spotify")
        self.assertEqual(GuiState(self.state_path).search_scope, "spotify")
        page.set_query("海")
        self.wait(lambda: self.search_requests())
        self.assertEqual(self.search_requests()[-1]["provider"], "spotify")
        # 語があるときに範囲を変えると、その範囲で探し直す (ライブラリは手元で探す)
        page.set_scope("library")
        self.wait(lambda: page.results.state in ("content", "empty"))
        self.assertEqual(GuiState(self.state_path).search_scope, "library")
        again = SearchPage(self.ctx)
        self.assertEqual(again.scope, "library")

    def test_no_results_and_failure(self):
        page = self.show(SearchPage(self.ctx))
        page.set_query("該当なし")
        self.wait(lambda: page.results.state == "empty")
        self.assertEqual(page.results.empty.title_label.get_text(), "結果がありません")
        page.set_query("__fail__")
        self.wait(lambda: page.results.empty.title_label.get_text() == "検索できませんでした")
        self.assertIn("yt-dlp", page.results.empty.description_label.get_text())

    def test_category_and_recent_chip(self):
        self.ctx.state.add_recent_search("前の検索")
        page = self.show(SearchPage(self.ctx))
        self.assertTrue(page.recent_box.get_visible())
        tile = page.categories.get_child_at_index(1).get_child()
        tile.emit("clicked")
        self.wait(lambda: self.search_requests())
        self.assertEqual(self.search_requests()[-1]["query"], "アニメ")
        page.set_query("")
        page._clear_recent()
        self.assertEqual(self.ctx.state.recent_searches, [])
        self.assertFalse(page.recent_box.get_visible())


class NowPlayingTest(PageCase):
    def test_rows_current_and_activation(self):
        store = self.ctx.store
        page = self.show(NowPlayingListPage(self.ctx))
        self.wait(lambda: len(page.list.rows()) == len(store.playlist.tracks))
        self.assertEqual(page.header.title_label.get_text(), "ドライブ")
        self.wait(lambda: page.list.current_row() is not None)
        self.assertEqual(page.list.current_row().index, store.status.index)
        page.list.emit("row-activated", page.list.rows()[5])
        self.wait(lambda: self.requests("play_index"))
        self.assertEqual(self.requests("play_index")[-1]["index"], 5)
        self.wait(lambda: page.list.current_row() is not None and page.list.current_row().index == 5)

    def test_rebuild_only_when_list_changes(self):
        store = self.ctx.store
        page = self.show(NowPlayingListPage(self.ctx))
        self.wait(lambda: len(page.list.rows()) == len(store.playlist.tracks))
        first_rows = page.list.rows()
        self.wait(lambda: page.list.current_row() is not None)
        before = page.list.current_row().index
        store.next()
        self.wait(lambda: page.list.current_row() is not None and page.list.current_row().index != before,
                  message="次の曲へ進んでも印が動きません")
        # playlist-changed (up_next の取り直し) が届くのを待ってから比べる
        run_loop(lambda: False, 0.5)
        self.assertEqual([id(r) for r in page.list.rows()], [id(r) for r in first_rows],
                         "曲が変わっただけで行を作り直しました")
        tracks = parse_tracks(send_once(self.sock, "tracks", provider="local", id="Focus").data)
        send_once(self.sock, "replace", tracks=tracks[:3], index=1, source={"provider": "local", "id": "Focus",
                                                                             "name": "Focus"})
        self.wait(lambda: len(page.list.rows()) == 3, message="リストが変わっても作り直しません")
        self.wait(lambda: page.header.title_label.get_text() == "Focus")
        self.assertEqual(page.header.info_label.get_text().split(" · ")[0], "3 曲")


class NowPlayingEditTest(PageCase):
    """リストを変えても行を作り直さない (スクロール位置・選択・フォーカスを失わない)。"""

    def setUp(self):
        super().setUp()
        many = [Track(path=f"https://www.youtube.com/watch?v=many{i:07d}", title=f"曲 {i}", artist="A",
                      duration=200) for i in range(200)]
        send_once(self.sock, "replace", tracks=many, index=150, source={"provider": "youtube", "id": "q",
                                                                        "name": "たくさん"})
        store = self.ctx.store
        self.assertTrue(run_loop(lambda: len(store.playlist.tracks) == 200, 5))

    def rows_page(self):
        page = self.show(NowPlayingListPage(self.ctx))
        self.window.set_default_size(1100, 600)
        self.wait(lambda: len(page.list.rows()) == 200 and page.rows.done)
        adj = page.scroller.get_vadjustment()
        self.wait(lambda: adj.get_upper() > 5000)
        row = page.list.rows()[120]
        ok, bounds = row.compute_bounds(page.body)
        adj.set_value(bounds.get_y())
        run_loop(lambda: False, 0.1)
        return page, adj

    def test_remove_and_append_keep_scroll_and_rows(self):
        store = self.ctx.store
        page, adj = self.rows_page()
        before = adj.get_value()
        rows = page.list.rows()
        store.remove(190)
        self.wait(lambda: len(page.list.rows()) == 199)
        self.assertAlmostEqual(adj.get_value(), before, delta=2)
        self.assertEqual([id(r) for r in page.list.rows()[:190]], [id(r) for r in rows[:190]])
        extra = [Track(path=f"https://www.youtube.com/watch?v=added{i:07d}", title=f"足した {i}") for i in range(3)]
        store.enqueue(extra, "end")
        self.wait(lambda: len(page.list.rows()) == 202)
        self.assertAlmostEqual(adj.get_value(), before, delta=2)
        self.assertEqual([r.index for r in page.list.rows()], list(range(202)))
        self.assertEqual(page.list.rows()[201].number, 202)

    def test_queue_changes_do_not_touch_rows(self):
        store = self.ctx.store
        page, adj = self.rows_page()
        before = adj.get_value()
        for row in page.list.rows():
            row.kept_marker = True
        store.queue_edit("add", index=170)
        self.wait(lambda: store.playlist.track_at(170) is not None and store.playlist.track_at(170).queued == 1)
        run_loop(lambda: False, 0.2)
        self.assertTrue(all(getattr(r, "kept_marker", False) for r in page.list.rows()))
        self.assertAlmostEqual(adj.get_value(), before, delta=2)
        self.assertEqual(page.list.rows()[170].track.queued, 1)  # 行の曲は新しいものに

    def test_removing_from_the_row_menu_keeps_focus_nearby(self):
        store = self.ctx.store
        page, adj = self.rows_page()
        before = adj.get_value()
        target = page.list.rows()[125]
        target.more.grab_focus()
        store.remove(125)
        self.wait(lambda: len(page.list.rows()) == 199)
        focus = self.window.get_focus()
        self.assertIsNotNone(focus)
        self.assertIs(focus.get_ancestor(Gtk.ListBoxRow), page.list.rows()[124])
        self.assertAlmostEqual(adj.get_value(), before, delta=60)


class PlaylistsTest(PageCase):
    def test_grid_sections_and_navigation(self):
        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: "local" in page.sections and page.sections["local"].keys
                  and "spotify" in page.sections and page.sections["spotify"].keys)
        self.assertNotIn("radio", page.sections)
        local_names = [name for _id, name, _n, _s in page.sections["local"].keys]
        self.assertEqual(local_names, ["ドライブ", "Focus"])
        grid = page.sections["local"].groups.get_first_child()
        grid.get_child_at_index(1).get_child().emit("clicked")
        self.assertEqual(self.window_stub.pages[-1],
                         ("playlist", {"provider": "local", "id": "Focus", "name": "Focus"}))

    def test_detail_activation_and_delete(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="Focus", name="Focus"))
        self.assertEqual(page.get_tag(), "playlist")
        self.wait(lambda: len(page.list.rows()) == 6)
        self.assertEqual(page.header.info_label.get_text().split(" · ")[0], "6 曲")
        self.assertEqual(page.list.rows()[0].menu_context, "local:Focus")
        page.list.emit("row-activated", page.list.rows()[3])
        # 見えている並びをそのまま送る (load_provider は取り直して添字で選ぶので、その間に
        # 並びが変わると別の曲が鳴る)
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 3)
        self.assertEqual(request["source"], {"provider": "local", "id": "Focus", "name": "Focus"})
        self.assertEqual([t["path"] for t in request["tracks"]], [t.path for t in page.tracks])
        self.assertEqual(self.requests("load_provider"), [])
        page.delete_playlist()
        self.wait(lambda: self.requests("playlist_delete"))
        self.assertEqual(self.requests("playlist_delete")[-1]["name"], "Focus")
        self.wait(lambda: self.window_stub.pages and self.window_stub.pages[-1][0] == "playlists")
        self.assertNotIn("Focus", self.fake.local_playlists)

    def test_history_playlist_is_not_editable(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="Recently Played",
                                            name="Recently Played"))
        self.wait(lambda: len(page.list.rows()) > 0)
        self.assertFalse(page.editable)
        self.assertEqual(page.list.rows()[0].menu_context, "")
        model, _group = page._menu()
        labels = [model.get_item_link(i, "section").get_item_attribute_value(0, "label", None).unpack()
                  for i in range(model.get_n_items())]
        self.assertNotIn("プレイリストを削除…", labels)
        page.delete_playlist()
        run_loop(lambda: False, 0.2)
        self.assertEqual(self.requests("playlist_delete"), [])

    def test_detail_refresh_fetches_again(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001",
                                            name="Chill Mix"))
        self.wait(lambda: len(page.list.rows()) == 4)
        self.assertEqual(len(self.requests("tracks")), 1)
        page.refresh()
        self.wait(lambda: len(self.requests("tracks")) == 2, message="Ctrl+R で取り直しません")
        self.assertEqual(page.list.rows()[0].menu_context, "")
        self.assertTrue(page.header.more_button.get_sensitive())
        # 自分で鳴らせる Spotify (Premium) には「YouTube で探して再生」の書き添えを出さない
        self.wait(lambda: self.requests("providers"))
        run_loop(lambda: False, 0.2)
        self.assertFalse(page.plays_via_youtube)
        self.assertFalse(page.header.note_label.get_visible())

    def test_detail_reloads_after_track_removed(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="ドライブ", name="ドライブ"))
        self.wait(lambda: len(page.list.rows()) == 7)
        self.ctx._remove_from_playlist("ドライブ", 0)
        self.wait(lambda: len(page.list.rows()) == 6, message="曲を消しても作り直しません")

    def test_detail_leaves_when_last_track_removed(self):
        """最後の曲を外すと cliamp はプレイリストごと消す。ページを閉じ、一覧からも消す。"""
        store = self.ctx.store
        track = store.playlist.tracks[0]
        done = []
        self.ctx.catalog.playlist_add("一曲", [track], done.append)
        self.wait(lambda: done)
        self.ctx.refresh_local_playlists()
        self.wait(lambda: "一曲" in self.ctx.local_playlists)
        home = Adw.NavigationPage(title="すべてのプレイリスト", tag="playlists", child=Gtk.Label())
        page = PlaylistDetailPage(self.ctx, provider="local", id="一曲", name="一曲")
        self.nav.replace([home])
        self.nav.push(page)
        self.wait(lambda: page.get_mapped() and len(page.list.rows()) == 1)
        _model, group = self.ctx.track_menu(page.tracks[0], index=0, context="local:一曲")
        group.activate_action("remove-from-playlist", None)
        self.wait(lambda: self.nav.get_visible_page() is home, message="消えたプレイリストのページが残っています")
        self.assertNotIn("一曲", self.ctx.local_playlists)
        self.assertNotIn("一曲", self.fake.local_playlists)
        self.assertIn("「一曲」は空になったので削除しました", self.window_stub.toasts)
        self.assertFalse(any("no such file" in t for t in self.window_stub.toasts))

    def test_missing_local_playlist_is_explained_without_paths(self):
        self.wait(lambda: self.ctx.local_playlists_loaded)
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="無いリスト", name="無いリスト"))
        # 一覧が取れていれば、消えたプレイリストのページは閉じる (根なので「すべてのプレイリスト」へ)
        self.wait(lambda: self.window_stub.pages and self.window_stub.pages[-1][0] == "playlists")
        # 一覧がまだ取れていないときは、ファイルの場所ではなく分かる文を出す
        self.ctx._local_playlists_loaded = False
        page.load(force=True)
        self.wait(lambda: page.state.empty.title_label.get_text() == "プレイリストが見つかりません")
        self.assertNotIn(".toml", page.state.empty.description_label.get_text())

    def test_detail_reloads_after_reconnect(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="ドライブ", name="ドライブ"))
        self.wait(lambda: len(page.list.rows()) == 7)
        with self.fake.lock:
            del self.fake.local_playlists["ドライブ"][0]
        self.fake.restart()
        self.wait(lambda: len(page.list.rows()) == 6, timeout=8, message="繋ぎ直しても読み直しません")


class PlaylistsAuthTest(PageCase):
    fake_options = {"spotify_needs_auth": True}

    def test_needs_auth_note_and_detail(self):
        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: "spotify" in page.sections and page.sections["spotify"].note.get_visible())
        self.assertEqual(page.sections["spotify"].note.get_text(),
                         "Spotify はサインインが必要です (cliamp の端末で設定)")
        detail = PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001", name="Chill Mix")
        self.show(detail)
        self.wait(lambda: detail.state.state == "empty")
        self.assertEqual(detail.state.empty.title_label.get_text(), "サインインが必要です")
        self.assertFalse(detail.header.play_button.get_sensitive())


class WebOnlySpotifyTest(PageCase):
    """Spotify の接続が Web API だけの cliamp (プレイリスト・保存した曲・検索の曲は YouTube で探して鳴らす)。
    Spotify が検索の件数を断ったときの説明は、偽の spotify_search_refused で確かめる。"""

    fake_options = {"spotify_web_only": True}

    def test_playlist_detail_says_songs_play_via_youtube(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001",
                                            name="Chill Mix"))
        self.wait(lambda: len(page.list.rows()) == 4)
        self.wait(lambda: page.header.note_label.get_visible(), message="書き添えが出ません")
        self.assertEqual(page.header.note_label.get_text(), WEB_ONLY_NOTE)
        self.assertTrue(page.header.note_label.has_css_class("music-info"))
        self.assertEqual(page.header.info_label.get_text().split(" · ")[0], "4 曲")
        self.assertTrue(page.plays_via_youtube)
        self.assertTrue(all(is_youtube_bridge(t) for t in page.tracks))
        # 行から鳴らすと、YouTube で探す曲の形 (meta 付き) のまま送る
        page.list.emit("row-activated", page.list.rows()[1])
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 1)
        self.assertTrue(all(t["path"].startswith("ytsearch1:") for t in request["tracks"]))
        self.assertEqual(request["tracks"][1]["meta"]["spotify.bridge"], "youtube")
        # 「…」のリンクは Spotify の曲の頁
        model, _group = self.ctx.track_menu(page.tracks[0], index=0)
        self.assertEqual(page.tracks[0].web_url,
                         f"https://open.spotify.com/track/{page.tracks[0].meta_get('spotify.id')}")
        self.assertIsNotNone(model)

    def test_note_follows_the_tracks_even_before_providers_arrive(self):
        """プロバイダーの答えが古くても (覚えた一覧に playback が無い)、曲が YouTube で探す形なら書き添える。"""
        self.ctx._provider_playback = {}
        page = PlaylistDetailPage(self.ctx, provider="spotify", id="YOUR MUSIC", name="Your Music")
        page.ctx.catalog.providers = lambda *a, **k: None  # 答えない
        self.show(page)
        self.wait(lambda: len(page.list.rows()) == 5)
        self.assertEqual(page.header.note_label.get_text(), WEB_ONLY_NOTE)
        self.assertTrue(page.header.note_label.get_visible())

    def test_local_playlist_has_no_note(self):
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="Focus", name="Focus"))
        self.wait(lambda: len(page.list.rows()) == 6)
        run_loop(lambda: False, 0.2)
        self.assertFalse(page.header.note_label.get_visible())

    def test_playlists_page_notes_the_spotify_section(self):
        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: "spotify" in page.sections and page.sections["spotify"].keys)
        self.wait(lambda: page.sections["spotify"].note.get_visible(), message="書き添えが出ません")
        self.assertEqual(page.sections["spotify"].note.get_text(), WEB_ONLY_NOTE)
        self.assertFalse(page.sections["local"].note.get_visible())
        self.assertEqual(len(page.sections["spotify"].keys), 3)

    def test_playlists_page_note_on_a_fresh_cliamp(self):
        """起動したばかりの cliamp: Spotify のセッションは playlists の初回にでき、それまでの
        providers には playback が付かない (本物と同じく偽も)。ページは providers → spotify の
        playlists の順に頼むので、初めの答えだけでは書き添えを決められない。カタログが初めての
        成功の後で providers を 1 度だけ取り直し、最初の表示のうちに書き添える。"""
        self.assertFalse(self.fake._spotify_session)
        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: "spotify" in page.sections and page.sections["spotify"].keys)
        self.wait(lambda: page.sections["spotify"].note.get_visible(),
                  message="起動したばかりの cliamp で Spotify の節に書き添えが出ません")
        self.assertEqual(page.sections["spotify"].note.get_text(), WEB_ONLY_NOTE)
        self.assertEqual(len(self.requests("providers")), 2, "初めての成功の後に 1 度だけ取り直す")
        # 覚えた答えは取り直したもの: 開き直し (force なし) でも書き添えは消えず、もう取り直さない
        page.load(force=False)
        run_loop(lambda: False, 0.3)
        self.assertEqual(page.sections["spotify"].note.get_text(), WEB_ONLY_NOTE)
        self.assertTrue(page.sections["spotify"].note.get_visible())
        self.assertEqual(len(self.requests("providers")), 2)
        # 失敗の注意書きは鳴らし方で上書きしない
        self.assertFalse(page.sections["local"].note.get_visible())

    def test_detail_page_on_a_fresh_cliamp_learns_the_playback(self):
        """詳細ページは providers と tracks を同時に頼む。先の providers の答えが古くても、
        tracks の成功の後で取り直した答えを覚える (曲の印と両方で書き添える)。"""
        page = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001",
                                            name="Chill Mix"))
        self.wait(lambda: len(page.list.rows()) == 4)
        self.wait(lambda: provider_playback(self.ctx, "spotify") == "youtube",
                  message="取り直した providers の playback を覚えていません")
        self.assertEqual(page.header.note_label.get_text(), WEB_ONLY_NOTE)
        self.assertTrue(page.header.note_label.get_visible())

    def test_spotify_search_shows_bridged_results(self):
        """自前の client_id で Web API だけに繋がった Spotify でも、Spotify の範囲で検索でき (1 回に 20 件)、
        結果の曲は YouTube で探して鳴らす形のまま送る。"""
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        page.set_query("夜明けのバス停")
        self.wait(lambda: page.results.state == "content", message="Spotify の検索の結果が出ません")
        request = self.requests("search")[-1]
        self.assertEqual((request["provider"], request["query"], request["limit"]), ("spotify", "夜明けのバス停", 20))
        tracks = page.results_tracks
        self.assertEqual(len(tracks), 20)
        self.assertTrue(all(is_youtube_bridge(t) for t in tracks))
        rows = page.all_songs.rows()
        self.assertEqual(len(rows), 20)
        page.all_songs.emit("row-activated", rows[2])
        self.wait(lambda: self.requests("replace"))
        replace = self.requests("replace")[-1]
        self.assertEqual(replace["index"], 2)
        self.assertEqual(replace["source"], {"provider": "spotify", "name": "「夜明けのバス停」"})
        self.assertTrue(all(t["path"].startswith("ytsearch1:") for t in replace["tracks"]))
        self.assertEqual(replace["tracks"][2]["meta"]["spotify.bridge"], "youtube")
        self.assertEqual(replace["tracks"][2]["meta"]["spotify.id"], tracks[2].spotify_id)

    def test_spotify_search_restriction_is_explained(self):
        """Spotify が検索の件数を断ったとき (パッチの "Spotify refused a page of 10 results")。"""
        self.fake.spotify_search_refused = SPOTIFY_SEARCH_REFUSED_PAGE
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        page.set_query("海 & <波>")
        self.wait(lambda: page.results.state == "empty")
        empty = page.results.empty
        self.assertEqual(empty.title_label.get_text(), SPOTIFY_SEARCH_BLOCKED_TITLE)
        description = empty.description_label.get_text()
        self.assertIn("YouTube", description)
        self.assertNotIn("client_id is too new", description)
        self.assertIsNotNone(empty.button)
        self.assertTrue(empty.button.get_visible())
        self.assertEqual(empty.button.label.get_text(), "YouTube で検索")
        # ボタンで YouTube の範囲に替えて探し直す
        empty.button.emit("clicked")
        self.assertEqual(page.scope, "youtube")
        self.assertEqual(self.ctx.state.search_scope, "youtube")
        self.wait(lambda: page.results.state == "content")
        self.assertEqual(self.requests("search")[-1]["provider"], "youtube")
        self.assertEqual(self.requests("search")[-1]["query"], "海 & <波>")
        # ほかの空状態ではボタンを出さない
        page.set_query("該当なし")
        self.wait(lambda: page.results.state == "empty")
        self.assertEqual(empty.title_label.get_text(), "結果がありません")
        self.assertFalse(empty.button.get_visible())

    def test_youtube_failure_that_echoes_the_words_is_not_a_spotify_restriction(self):
        """YouTube の検索の誤りは語を繰り返す ("resolving yt-dlp ytsearch20:<語>: …")。語に
        "search blocked" や "Invalid limit" が入っていても、Spotify の断りの説明とボタンは出さない。"""
        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3)
        page.set_scope("youtube")
        for query in ("search blocked __fail__", "spotify: search blocked __fail__", "Invalid limit __fail__"):
            with self.subTest(query=query):
                page.set_query(query)
                empty = page.results.empty
                self.wait(lambda: page.results.state == "empty"
                          and f":{query}:" in empty.description_label.get_text(),
                          message="YouTube の検索の誤りが出ません")
                self.assertEqual(empty.title_label.get_text(), "検索できませんでした")
                self.assertFalse(empty.button is not None and empty.button.get_visible(),
                                 "YouTube の範囲で「YouTube で検索」を出しました")
                self.assertEqual(self.requests("search")[-1]["provider"], "youtube")
        # Spotify の範囲でない検索の結果には、文言が Spotify の断りそのものでも出さない
        # (ボタンはいまの範囲へ移るだけになる)
        blocked = Response(False, {}, SPOTIFY_SEARCH_BLOCKED, "error")
        page._show_results("x", "youtube", blocked)
        self.assertEqual(page.results.empty.title_label.get_text(), "検索できませんでした")
        self.assertFalse(page.results.empty.button is not None and page.results.empty.button.get_visible())
        page._show_results("x", "spotify", blocked)
        self.assertEqual(page.results.empty.title_label.get_text(), SPOTIFY_SEARCH_BLOCKED_TITLE)

    def test_rate_limit_is_explained(self):
        self.fake.spotify_error = "spotify: rate limited by Spotify; retry after 24h0m0s"
        want = "Spotify から回数の制限を受けています。24 時間ほど待ってから、もう一度試してください"
        search = self.show(SearchPage(self.ctx))
        self.wait(lambda: search.scopes.get_n_toggles() == 3)
        search.set_scope("spotify")
        search.set_query("雨")
        self.wait(lambda: search.results.state == "empty")
        self.assertEqual(search.results.empty.title_label.get_text(), "検索できませんでした")
        self.assertEqual(search.results.empty.description_label.get_text(), want)
        self.assertFalse(search.results.empty.button is not None and search.results.empty.button.get_visible())
        detail = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001",
                                              name="Chill Mix"))
        self.wait(lambda: detail.state.state == "empty")
        self.assertEqual(detail.state.empty.title_label.get_text(), "プレイリストを読めませんでした")
        self.assertEqual(detail.state.empty.description_label.get_text(), want)
        self.assertFalse(detail.header.note_label.get_visible())
        playlists = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: "spotify" in playlists.sections and playlists.sections["spotify"].note.get_visible())
        self.assertEqual(playlists.sections["spotify"].note.get_text(), f"読み込めませんでした: {want}")

    def test_pages_are_released(self):
        box = {"page": SearchPage(self.ctx)}
        self.show(box["page"])
        self.wait(lambda: box["page"].scopes.get_n_toggles() == 3)
        box["page"].set_scope("spotify")
        box["page"].set_query("星")
        self.wait(lambda: box["page"].results.state == "content")
        detail = {"page": PlaylistDetailPage(self.ctx, provider="spotify", id="YOUR MUSIC", name="Your Music")}
        refs = [weakref.ref(box["page"]), weakref.ref(detail["page"])]
        self.show(detail["page"])
        self.wait(lambda: detail["page"].header.note_label.get_visible())
        self.nav.replace([Adw.NavigationPage(title="空", tag="blank", child=Gtk.Label())])
        box.clear()
        detail.clear()
        for _ in range(5):
            run_loop(lambda: False, 0.1)
            gc.collect()
        self.assertEqual([r() for r in refs], [None, None], "外したページが解放されません")


class RadioAndRecentTest(PageCase):
    # 利用者の radios.toml の局とお気に入りもある cliamp (l:0〜l:3、f:0)
    fake_options = {"radios_toml": True}

    def test_radio_tiles(self):
        page = self.show(RadioPage(self.ctx))
        self.wait(lambda: len(page.cliamp_shelf.items()) == 5 and len(page.japan_shelf.items()) == 2)
        first = page.cliamp_shelf.items()[0]
        self.assertEqual(first.track.display_title, "cliamp ラジオ")
        first.emit("clicked")
        self.wait(lambda: self.requests("load_provider"))
        request = self.requests("load_provider")[-1]
        self.assertEqual((request["provider"], request["id"]), ("radio", "l:0"))
        page.japan_shelf.items()[1].emit("clicked")
        self.wait(lambda: self.requests("replace"))
        self.assertEqual(self.requests("replace")[-1]["source"]["provider"], "radio-browser")
        page.search("lo-fi")
        self.wait(lambda: page.results.state == "content")
        self.assertEqual(page.grid.get_child_at_index(0).get_child().track.title, "Tokyo Lo-fi Radio")

    def test_recent_rows_and_play(self):
        page = self.show(RecentPage(self.ctx))
        self.wait(lambda: page.state.state == "content")
        rows = page.list.rows()
        self.assertEqual(len(rows), len(page.tracks))
        self.assertTrue(rows[0].extra_label.get_text().endswith("前"))
        page.play_button.emit("clicked")
        self.wait(lambda: self.requests("replace"))
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 0)
        self.assertEqual(request["source"]["id"], "Recently Played")
        page.list.emit("row-activated", rows[2])
        self.wait(lambda: len(self.requests("replace")) == 2)
        self.assertEqual(self.requests("replace")[-1]["index"], 2)


class ReleaseTest(PageCase):
    """外したページが解放される (store / ctx / 子の部品のシグナルがページを掴んでいない)。"""

    def check_released(self, make, ready):
        box = {"page": make()}
        self.show(box["page"])
        self.wait(lambda: ready(box["page"]), message=f"{type(box['page']).__name__} が読み込み終わりません")
        run_loop(lambda: False, 0.3)
        ref = weakref.ref(box["page"])
        self.assertGreater(len(box["page"]._handler_ids), 0)
        self.nav.replace([Adw.NavigationPage(title="空", tag="blank", child=Gtk.Label())])
        box.clear()
        for _ in range(5):
            run_loop(lambda: False, 0.1)
            gc.collect()
        self.assertIsNone(ref(), "外したページが解放されません")

    def test_all_pages_are_released(self):
        cases = [
            (lambda: SearchPage(self.ctx), lambda p: p.scopes.get_n_toggles() == 3),
            (lambda: RadioPage(self.ctx), lambda p: len(p.cliamp_shelf.items()) > 0),
            (lambda: RecentPage(self.ctx), lambda p: p.state.state == "content"),
            (lambda: NowPlayingListPage(self.ctx, reveal=True), lambda p: len(p.list.rows()) > 0),
            (lambda: PlaylistsPage(self.ctx), lambda p: bool(p.sections.get("local") and p.sections["local"].keys)),
            (lambda: PlaylistDetailPage(self.ctx, provider="local", id="ドライブ", name="ドライブ"),
             lambda p: len(p.list.rows()) == 7),
        ]
        for make, ready in cases:
            with self.subTest(page=make.__code__.co_names):
                self.check_released(make, ready)

    def test_search_page_with_results_is_released(self):
        box = {"page": SearchPage(self.ctx)}
        self.show(box["page"])
        box["page"].set_query("星")
        self.wait(lambda: box["page"].results.state == "content")
        ref = weakref.ref(box["page"])
        self.nav.replace([Adw.NavigationPage(title="空", tag="blank", child=Gtk.Label())])
        box.clear()
        for _ in range(5):
            run_loop(lambda: False, 0.1)
            gc.collect()
        self.assertIsNone(ref(), "検索の結果を出したページが解放されません")


class WeakCallTest(unittest.TestCase):
    def test_weak_call_drops_target(self):
        if not HAVE_DISPLAY:
            self.skipTest(SKIP)

        class Target:
            def __init__(self):
                self.calls = []

            def hit(self, *args):
                self.calls.append(args)
                return "ok"

        target = Target()
        call = weak_call(target.hit, 1, 2)
        self.assertEqual(call("捨てる引数"), "ok")
        self.assertEqual(target.calls, [(1, 2)])
        del target
        gc.collect()
        self.assertIsNone(call())


if __name__ == "__main__":
    unittest.main()
