"""「Spotify から取り込む」(importer.py と、ページ・サイドバー・store・カタログの取り込みまわり) の試験。

Spotify の頁は tests/spotify_fixtures.py の架空のもの (spotify_import.urlopen を差し替える)。cliamp は
偽 (tests/fake_cliamp.py)。偽は本物と同じく、ローカルのプレイリストに足した曲の meta を落とす。

前半 (ImportFlowTest) は窓を出さない。後半 (DialogTest など) は X の画面 (Xvfb の :10 以上) が要り、
無ければ飛ばす:

    nix develop path:. -c xvfb-run -n 95 python3 -m unittest tests.test_importer -v
"""

from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import unittest
import weakref
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import spotify_fixtures as fx  # noqa: E402
from fake_cliamp import FakeCliamp, history_track, run_loop, temp_socket_path  # noqa: E402
from test_pages import HAVE_DISPLAY, SKIP, FakeWindow, PageCase  # noqa: E402

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, Gtk

    import cliamp_music.artwork as artwork_mod
    from cliamp_music import importer
    from cliamp_music import spotify_import as si
    from cliamp_music.artwork import SPOTIFY_OEMBED, ArtworkLoader, art_sources
    from cliamp_music.client import CliampClient
    from cliamp_music.context import AppContext
    from cliamp_music.protocol import (
        SPOTIFY_OWNER_PREMIUM,
        SPOTIFY_SEARCH_BLOCKED_TITLE,
        Track,
        is_youtube_bridge,
        playlist_name_problem,
    )
    from cliamp_music.state import GuiState

    HAVE_GI = True
except (ImportError, ValueError):  # pragma: no cover
    HAVE_GI = False

PID = "37i9dQZF1DXfakePlayls1"
URL = f"https://open.spotify.com/playlist/{PID}?si=abc"
NAME = "夜更かし & <深夜>"


def _no_network(*_args, **_kwargs):
    raise OSError("試験ではネットワークに出ません")


_saved_urlopen = None


def setUpModule():  # noqa: N802
    global _saved_urlopen
    if HAVE_GI:
        _saved_urlopen = artwork_mod.urlopen
        artwork_mod.urlopen = _no_network


def tearDownModule():  # noqa: N802
    if HAVE_GI and _saved_urlopen is not None:
        artwork_mod.urlopen = _saved_urlopen


class WebMixin:
    """spotify_import.urlopen を架空の頁に差し替える。"""

    def install_web(self, count: int = 6, *, total: int | None = None, name: str = NAME) -> "fx.FakeWeb":
        self.web = fx.FakeWeb()
        self.set_page(count, total=total, name=name)
        patcher = mock.patch.object(si, "urlopen", self.web)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.web

    def set_page(self, count: int, *, total: int | None = None, name: str = NAME, seed: str = "p") -> None:
        self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID, name=name,
                                                                       items=fx.tracks(count, seed))
        if total is not None:
            self.web.pages[fx.page_url("playlist", PID)] = fx.count_page(total)

    def fetched(self, count: int = 6, **kwargs) -> "si.ImportedList":
        self.set_page(count, **kwargs)
        return si.fetch_list(URL)


# --------------------------------------------------------------------------
# 窓を出さない流れ


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class ImportFlowTest(WebMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cm-import-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock = temp_socket_path()
        self.fake = FakeCliamp(self.sock).start()
        self.addCleanup(self.fake.stop)
        self.window = FakeWindow()
        self.ctx = AppContext(None, window=self.window, client=CliampClient(self.sock),
                              state=GuiState(os.path.join(self.tmp, "state.json")),
                              artwork=ArtworkLoader(os.path.join(self.tmp, "art")))
        self.ctx.client.start()
        self.addCleanup(self.ctx.client.stop)
        store = self.ctx.store
        self.assertTrue(run_loop(lambda: store.connected and store.status.seq > 0 and self.ctx.local_playlists_loaded,
                                 8), "繋がりません")
        self.install_web()
        self.fake.requests.clear()

    def wait(self, condition, timeout=5.0, message="待ちきれません"):
        self.assertTrue(run_loop(condition, timeout), message)

    def save(self, name: str, result, **kwargs) -> list:
        done = []
        importer.save_import(self.ctx, name, result, callback=done.append, **kwargs)
        self.wait(lambda: done, message="保存が終わりません")
        return done

    def test_index_lives_next_to_the_state_and_hooks_are_set(self):
        self.assertEqual(self.ctx.imports.path, os.path.join(self.tmp, "spotify-imports.json"))
        self.assertEqual(self.ctx.catalog.track_hook, self.ctx.imports.restore_all)
        self.assertEqual(self.ctx.store.track_hook, self.ctx.imports.restore)

    def test_fetch_async_returns_on_the_main_loop(self):
        box = []
        importer.fetch_async(URL, box.append)
        self.wait(lambda: box)
        self.assertIsInstance(box[0], si.ImportedList)
        self.assertEqual(box[0].count, 6)
        self.web.pages[fx.embed_url("playlist", PID)] = fx.missing_page(404)
        box.clear()
        with mock.patch.object(importer, "log"):
            importer.fetch_async(URL, box.append)
            self.wait(lambda: box)
        self.assertIsInstance(box[0], si.SpotifyImportError)
        self.assertEqual(box[0].kind, "not-found")
        # 思わぬ例外も「形が変わった」として返す (worker で落とさない)
        box.clear()
        with mock.patch.object(si, "parse_embed_html", side_effect=KeyError("x")), mock.patch.object(importer, "log"):
            self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID)
            importer.fetch_async(URL, box.append)
            self.wait(lambda: box)
        self.assertEqual(box[0].kind, "shape")

    def test_save_writes_bridged_tracks_and_remembers_their_ids(self):
        result = si.fetch_list(URL)
        self.assertEqual(self.save(NAME, result), [True])
        self.assertIn(f"「{NAME}」を取り込みました (6 曲)", self.window.toasts)
        self.assertEqual(self.window.pages[-1], ("playlist", {"provider": "local", "id": NAME, "name": NAME}))
        self.assertIn(NAME, self.ctx.local_playlists)
        # cliamp へは cliamp の Web API だけの Spotify と同じ形 (meta 付き) で送り、偽は本物と同じく meta を落とす
        sent = self.fake.requests_for("playlist_add")[-1]["tracks"]
        self.assertEqual([t["path"] for t in sent], [t.path for t in result.tracks])
        self.assertTrue(all(t["meta"]["spotify.bridge"] == "youtube" for t in sent))
        stored = self.fake.local_playlists[NAME]
        self.assertTrue(all("meta" not in t for t in stored))
        self.assertEqual(stored[0], {"path": "ytsearch1:青い灯台 夜明けのバス停", "title": "夜明けのバス停",
                                     "artist": "青い灯台", "duration": 214, "stream": True})
        # 表に書いた
        record = self.ctx.imports.get(NAME)
        self.assertEqual((record.url, record.count, record.title), (f"https://open.spotify.com/playlist/{PID}", 6, NAME))
        self.assertTrue(os.path.exists(self.ctx.imports.path))
        # ローカルのプレイリストとして読み戻すと、meta は無いが ID を付け直す
        box = []
        self.ctx.catalog.tracks("local", NAME, box.append, force=True)
        self.wait(lambda: box)
        tracks = box[0]
        self.assertEqual([t.spotify_id for t in tracks], [t.spotify_id for t in result.tracks])
        self.assertTrue(all(is_youtube_bridge(t) for t in tracks))
        self.assertEqual(art_sources(tracks[0])[0],
                         f"{SPOTIFY_OEMBED}https://open.spotify.com/track/{result.tracks[0].spotify_id}")
        self.assertEqual(tracks[0].web_url, f"https://open.spotify.com/track/{result.tracks[0].spotify_id}")

    def test_truncated_import_says_so(self):
        result = self.fetched(100, total=150)
        self.save("長いリスト", result)
        self.assertIn("「長いリスト」を取り込みました (100 曲)", self.window.toasts)
        self.assertIn(f"{si.TRUNCATED_NOTE} (全 150 曲)", self.window.toasts)
        record = self.ctx.imports.get("長いリスト")
        self.assertTrue(record.truncated)
        self.assertEqual((record.count, record.total), (100, 150))

    def test_replace_keeps_the_name(self):
        result = si.fetch_list(URL)
        before = list(self.fake.local_playlists)
        changed = []
        self.ctx.connect("local-playlists-changed", lambda *_a: changed.append(1))
        self.assertEqual(self.save("Focus", result, replace=True), [True])
        self.assertEqual([t["path"] for t in self.fake.local_playlists["Focus"]], [t.path for t in result.tracks])
        self.assertEqual(list(self.fake.local_playlists), before)
        order = [r["cmd"] for r in self.fake.requests if r["cmd"] in ("tracks", "playlist_delete", "playlist_add")]
        self.assertEqual(order[:3], ["tracks", "playlist_delete", "playlist_add"])
        self.assertTrue(changed, "中身が変わったことを知らせません")

    def test_replace_puts_the_old_tracks_back_when_adding_fails(self):
        result = si.fetch_list(URL)
        old = [dict(t) for t in self.fake.local_playlists["Focus"]]
        original = self.fake._cmd_playlist_add

        def refuse_bridged(req, now):
            if any(str(t.get("path", "")).startswith("ytsearch1:") for t in req.get("tracks") or []):
                return {"ok": False, "error": "disk full"}
            return original(req, now)

        self.fake._cmd_playlist_add = refuse_bridged
        with mock.patch.object(importer, "log"):
            self.assertEqual(self.save("Focus", result, replace=True), [False])
        self.wait(lambda: "Focus" in self.fake.local_playlists)
        self.assertEqual([t["path"] for t in self.fake.local_playlists["Focus"]], [t["path"] for t in old])
        self.assertIn("取り込めませんでした: disk full", self.window.toasts)
        self.assertIsNone(self.ctx.imports.get("Focus"))

    def test_update_failures_leave_the_playlist(self):
        self.save(NAME, si.fetch_list(URL))
        self.web.pages[fx.embed_url("playlist", PID)] = fx.missing_page(404)
        done = []
        with mock.patch.object(importer, "log"):
            self.ctx.update_spotify_import(NAME, done.append)
            self.wait(lambda: done)
        self.assertEqual(done, [False])
        self.assertTrue(any(t.startswith("更新できませんでした: 見つかりません") for t in self.window.toasts))
        self.assertEqual(len(self.fake.local_playlists[NAME]), 6)
        self.assertFalse(self.ctx.update_spotify_import("ドライブ"))
        self.assertIn("「ドライブ」は Spotify から取り込んだプレイリストではありません", self.window.toasts)

    def test_replace_reports_only_after_the_old_tracks_are_back(self):
        """足せなかったときは前の曲を戻し、戻し終えてから知らせて一覧を取り直す (戻すのと一覧の取り直しが
        行き違うと、戻したプレイリストが一覧から消えたままになる)。控えは戻せたら消す。"""
        result = si.fetch_list(URL)
        old = [t["path"] for t in self.fake.local_playlists["Focus"]]
        original = self.fake._cmd_playlist_add

        def add(req, now):
            if any(str(t.get("path", "")).startswith("ytsearch1:") for t in req.get("tracks") or []):
                return {"ok": False, "error": "disk full"}
            time.sleep(0.4)  # 戻すのに時間がかかる
            return original(req, now)

        self.fake._cmd_playlist_add = add
        seen = []

        def done(ok):
            with self.fake.lock:
                seen.append((ok, [t["path"] for t in self.fake.local_playlists.get("Focus", [])]))

        with mock.patch.object(importer, "log"):
            importer.save_import(self.ctx, "Focus", result, replace=True, callback=done)
            self.wait(lambda: seen, 8)
        self.assertEqual(seen, [(False, old)], "戻し終える前に知らせました")
        self.assertIn("取り込めませんでした: disk full", self.window.toasts)
        # 知らせた後の一覧の取り直しは、戻した後の一覧を見る
        run_loop(lambda: False, 0.6)
        self.assertIn("Focus", self.ctx.local_playlists)
        self.assertEqual(self.ctx.imports.backups(), [], "戻せたのに控えが残っています")
        self.assertEqual(self.ctx.spotify_imports_busy, set())

    def test_unexpected_errors_release_the_name(self):
        """送る前に落ちても (曲に cliamp へ送れない文字がある、など) 取り込み中の印を外して知らせる。"""
        result = si.fetch_list(URL)
        error = UnicodeEncodeError("utf-8", "\ud83d", 0, 1, "surrogates not allowed")
        done = []
        with mock.patch.object(self.ctx.catalog, "playlist_add", side_effect=error), \
                mock.patch.object(importer, "log"):
            importer.save_import(self.ctx, "Sur", result, callback=done.append)
        self.assertEqual(done, [False])
        self.assertEqual(self.ctx.spotify_imports_busy, set())
        self.assertTrue(any(t.startswith("取り込めませんでした: cliamp に送れません") for t in self.window.toasts),
                        self.window.toasts)
        with mock.patch.object(self.ctx.catalog, "tracks", side_effect=RuntimeError("x")), \
                mock.patch.object(importer, "log"), mock.patch("traceback.print_exc"):
            importer.save_import(self.ctx, "Sur", result, replace=True, callback=done.append)
        self.assertEqual(done, [False, False])
        self.assertEqual(self.ctx.spotify_imports_busy, set())
        # もう一度できる
        self.assertEqual(self.save("Sur", result), [True])

    def test_broken_surrogates_in_the_page_do_not_break_the_import(self):
        """頁の JSON の "\\ud83d" (切れた絵文字) は U+FFFD にして取り込む (名前・曲名・表の保存)。"""
        page = fx.embed_page(id=PID, name="Sur@@", items=[fx.track_item("曲@@", ["人@@"], 200_000, "s1")])
        self.web.pages[fx.embed_url("playlist", PID)] = page.replace("@@", "\\ud83d")
        result = si.fetch_list(URL)
        self.assertEqual(result.name, "Sur\ufffd")
        name = si.playlist_name_for(result)
        self.assertEqual(self.save(name, result), [True])
        self.assertEqual(self.fake.local_playlists[name][0]["path"], "ytsearch1:人\ufffd 曲\ufffd")
        self.assertTrue(self.ctx.imports.save())
        self.assertEqual(si.ImportIndex(self.ctx.imports.path).get(name).title, "Sur\ufffd")

    def test_too_long_names_are_refused_like_real_cliamp(self):
        result = si.fetch_list(URL)
        done = []
        importer.save_import(self.ctx, "夏" * 84, result, callback=done.append)
        self.wait(lambda: done)
        self.assertEqual(done, [False])
        self.assertTrue(any(t.endswith("file name too long") for t in self.window.toasts))
        # 既定の名前は収まるように縮める
        long = si.ImportedList(kind="album", id=PID, name="夏" * 84, tracks=result.tracks)
        self.assertEqual(self.save(si.playlist_name_for(long), long), [True])

    def test_busy_names_are_not_saved_twice(self):
        result = si.fetch_list(URL)
        first, second = [], []
        importer.save_import(self.ctx, NAME, result, callback=first.append)
        importer.save_import(self.ctx, NAME, result, callback=second.append)
        self.assertEqual(second, [False])
        self.assertIn(f"「{NAME}」はいま取り込んでいます", self.window.toasts)
        self.wait(lambda: first)
        self.assertEqual(len(self.fake.local_playlists[NAME]), 6)

    def test_store_restores_tracks_loaded_without_meta(self):
        """TUI や load_provider で読み込んだ取り込み済みのプレイリスト (cliamp の曲に meta が無い) でも、
        再生バー・次に再生・履歴の曲は Spotify の曲として絵とリンクを持つ。"""
        result = si.fetch_list(URL)
        self.save(NAME, result)
        store = self.ctx.store
        self.ctx.load_provider("local", NAME, 1, NAME)
        self.wait(lambda: store.status.track is not None and store.status.track.path == result.tracks[1].path, 8)
        self.assertNotIn("meta", self.fake.pl.current()[0] or {}, "偽の cliamp の曲に meta が残っています")
        track = store.status.track
        self.assertTrue(is_youtube_bridge(track))
        self.assertEqual(track.spotify_id, result.tracks[1].spotify_id)
        self.assertEqual(art_sources(track)[0],
                         f"{SPOTIFY_OEMBED}https://open.spotify.com/track/{result.tracks[1].spotify_id}")
        self.wait(lambda: len(store.playlist.tracks) == 6 and store.playlist.tracks[0].path == result.tracks[0].path)
        self.assertTrue(all(is_youtube_bridge(t) for t in store.playlist.tracks))
        # 履歴 (history.toml にも meta は残らない)
        with self.fake.lock:
            self.fake.history.insert(0, (history_track(result.tracks[2].to_wire()), "2026-09-27T01:00:00Z"))
        store.refresh_history()
        self.wait(lambda: store.history and store.history[0].path == result.tracks[2].path)
        self.assertEqual(store.history[0].spotify_id, result.tracks[2].spotify_id)

    def test_refresh_forgets_records_of_removed_playlists(self):
        result = si.fetch_list(URL)
        self.ctx.imports.record("消えた", result, now=1000.0)
        self.ctx.imports.record("取り込んだばかり", result)
        done = []
        self.ctx.refresh_local_playlists(then=done.append)
        self.wait(lambda: done)
        self.assertIsNone(self.ctx.imports.get("消えた"))
        self.assertIsNotNone(self.ctx.imports.get("取り込んだばかり"))
        self.ctx.forget_import("取り込んだばかり")
        self.assertIsNone(self.ctx.import_record("取り込んだばかり"))


# --------------------------------------------------------------------------
# 窓


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class ImportPageCase(WebMixin, PageCase):
    def setUp(self):
        super().setUp()
        self.install_web()

    def open(self, url: str = "") -> "importer.SpotifyImportDialog":
        dialog = self.ctx.open_spotify_import(url)
        self.addCleanup(close, dialog)
        self.assertTrue(run_loop(dialog.get_mapped, 5), "取り込みの窓が出ません")
        return dialog

    def type_url(self, dialog, text: str) -> None:
        """貼り付けたように入れる (Gtk.Entry.set_text)。"""
        dialog.url_entry.set_text(text)

    def key_in(self, dialog, text: str) -> None:
        """1 文字ずつ打つ (本物のキーと同じく、1 文字ごとに "changed" が 1 回)。"""
        entry = dialog.url_entry
        for ch in text:
            entry.insert_text(ch, len(entry.get_text()))

    @staticmethod
    def page(dialog) -> str:
        return dialog.well.get_visible_child_name()

    def ready(self, dialog) -> None:
        dialog.fetch_now()
        self.wait(lambda: dialog.state in ("ready", "error"), message="読み終わりません")

    def last_dialog(self):
        return self.ctx.spotify_import_dialog


class DialogTest(ImportPageCase):
    def test_link_is_checked_when_pasted(self):
        dialog = self.open()
        self.assertEqual(dialog.state, "empty")
        self.assertEqual(dialog.url_hint.get_text(), importer.URL_HINT)
        self.assertEqual(self.page(dialog), "placeholder")
        self.assertFalse(dialog.get_response_enabled("import"))
        self.assertTrue(dialog.name_box.get_visible())
        self.assertFalse(dialog.name_entry.get_sensitive(), "読む前に名前を打てます")
        for text, reason in ((f"https://open.spotify.com/track/{PID}", "曲のリンク"),
                             ("https://open.spotify.com/collection/tracks", "お気に入りの曲"),
                             ("https://example.com/x", "Spotify のリンクではありません")):
            with self.subTest(text=text):
                self.type_url(dialog, text)  # 貼り付け (1 度に何文字も) はすぐ確かめる
                self.assertEqual(dialog.state, "invalid")
                self.assertEqual(self.page(dialog), "problem")
                self.assertIn(reason, dialog.problem)
                self.assertIn(reason, dialog.problem_label.get_text())
                self.assertTrue(dialog.well.has_css_class("problem"))
                self.assertFalse(dialog.retry_button.get_visible())
                self.assertFalse(dialog.get_response_enabled("import"))
        self.assertEqual(self.web.requests, [], "形の違うリンクで読みに行きました")
        self.type_url(dialog, URL)
        self.assertEqual(dialog.state, "loading")
        self.assertEqual(self.page(dialog), "loading")
        self.assertEqual(dialog.problem, "")
        self.assertEqual(dialog.url_hint.get_text(), importer.URL_HINT)
        # 貼り付けたリンクは待たずに読む
        self.assertTrue(dialog.fetching)
        self.wait(lambda: dialog.state == "ready", message="読みに行きません")
        self.assertEqual(self.page(dialog), "preview")
        self.assertEqual(dialog.preview_title.get_text(), NAME)
        self.assertEqual(dialog.preview_summary.get_text(), "プレイリスト · 架空の作り手 · 6 曲")
        self.assertFalse(dialog.preview_note.get_visible())
        self.assertTrue(dialog.name_entry.get_sensitive())
        self.assertEqual(dialog.name_entry.get_text(), NAME)
        self.assertTrue(dialog.get_response_enabled("import"))
        self.assertEqual(len(self.web.requests), 1)
        # ?si= だけ違う同じリンクは読み直さない
        self.type_url(dialog, f"https://open.spotify.com/playlist/{PID}?si=zzz")
        self.assertEqual(dialog.state, "ready")
        run_loop(lambda: False, 0.5)
        self.assertEqual(len(self.web.requests), 1)

    def test_typing_waits_and_editing_the_same_link_still_reads_it(self):
        """打っているとき: 形の誤りは手を止めるまで言わない。22 文字目の後に同じリストのまま打ち足しても
        (「/」「?si=…」)、待ちが取り消されたまま止まらずに読む (本物のキーは 1 文字ごとに "changed" が 1 回)。"""
        dialog = self.open()
        self.key_in(dialog, "h")
        self.assertEqual(dialog.state, "invalid")
        self.assertEqual(self.page(dialog), "placeholder", "打ち始めから誤りを出しました")
        self.assertEqual(dialog.problem, "")
        base = f"https://open.spotify.com/playlist/{PID}"
        self.key_in(dialog, base[1:-4])
        self.assertEqual(self.page(dialog), "placeholder")
        # 手を止めたら言う
        self.wait(lambda: dialog.problem != "", 3, "手を止めても形の誤りを出しません")
        self.assertIn("22 文字", dialog.problem)
        self.key_in(dialog, base[-4:])
        self.assertEqual(dialog.state, "loading")
        self.assertFalse(dialog.fetching, "打っているのに待たずに読みました")
        self.assertEqual(self.web.requests, [])
        # 待ちの間に同じリストのまま打ち足す
        self.key_in(dialog, "/")
        self.assertEqual(dialog.state, "loading")
        self.key_in(dialog, "?si=ab")
        self.wait(lambda: dialog.state == "ready", 3, "同じリンクを打ち足したら読まなくなりました")
        self.assertEqual(len(self.web.requests), 1)
        # Enter は待たずに読む
        other = "B" * 22
        self.web.pages[fx.embed_url("album", other)] = fx.embed_page(kind="album", id=other, name="別の")
        dialog.url_entry.set_text("")
        self.key_in(dialog, f"spotify:album:{other}")
        self.assertEqual(dialog.state, "loading")
        self.assertFalse(dialog.fetching)
        dialog.url_entry.emit("activate")
        self.assertTrue(dialog.fetching, "Enter で読みません")
        self.wait(lambda: dialog.state == "ready")
        # Enter と欄を離れたときは、打っている途中の誤りもすぐ言う
        dialog.url_entry.set_text("")
        self.key_in(dialog, "spotify:x")
        self.assertEqual(dialog.problem, "")
        dialog.url_entry.emit("activate")
        self.assertIn("プレイリストかアルバム", dialog.problem)
        dialog.url_entry.set_text("")
        self.key_in(dialog, "spotify:y")
        self.assertEqual(dialog.problem, "")
        dialog._on_url_leave()
        self.assertNotEqual(dialog.problem, "")

    def test_network_errors_offer_to_read_again(self):
        self.web.pages[fx.embed_url("playlist", PID)] = OSError("down")
        dialog = self.open()
        with mock.patch.object(importer, "log"):
            self.type_url(dialog, URL)
            self.wait(lambda: dialog.state == "error")
        self.assertIn("ネットワークに繋がりません", dialog.problem)
        self.assertTrue(dialog.retry_button.get_visible())
        self.assertFalse(dialog.get_response_enabled("import"))
        self.set_page(6)
        dialog.retry_button.emit("clicked")
        self.assertEqual(dialog.state, "loading")
        self.wait(lambda: dialog.state == "ready")
        self.assertTrue(dialog.get_response_enabled("import"))

    def test_height_does_not_change_between_states(self):
        """窓は縦の真ん中に置かれるので、中身の高さが変わるとリンクの欄が上下に動く。どの状態でも同じ高さ。"""
        self.set_page(100, total=150)  # 切れる知らせ (いちばん背の高い「読めたもの」)
        dialog = self.open()
        form = dialog.get_extra_child()

        def height() -> int:
            return form.measure(Gtk.Orientation.VERTICAL, importer.FORM_WIDTH)[1]

        heights = {"empty": height()}
        self.type_url(dialog, "https://spotify.link/AbCdEfGhIjKlMnOp")  # 3 行になる長い誤り
        heights["invalid"] = height()
        self.web.pages[fx.embed_url("playlist", "C" * 22)] = OSError("down")
        with mock.patch.object(importer, "log"):
            self.type_url(dialog, f"spotify:playlist:{'C' * 22}")
            heights["loading"] = height()
            self.wait(lambda: dialog.state == "error")
        heights["error"] = height()  # 誤りと「もう一度読む」
        self.type_url(dialog, URL)
        self.wait(lambda: dialog.state == "ready")
        self.assertTrue(dialog.preview_note.get_visible())
        heights["ready"] = height()
        dialog.name_entry.set_text("Focus")  # 名前の下の案内
        heights["collision"] = height()
        self.assertEqual(len(set(heights.values())), 1, heights)

    def test_import_follows_the_cliamp_connection(self):
        dialog = self.open(URL)
        self.ready(dialog)
        self.assertTrue(dialog.get_response_enabled("import"))
        self.assertEqual(dialog.name_hint.get_text(), "")
        self.fake.stop()
        self.wait(lambda: not self.ctx.store.connected, 8, "切れません")
        self.assertFalse(dialog.get_response_enabled("import"), "cliamp が切れても「取り込む」が押せます")
        self.assertIn("cliamp に接続していません", dialog.name_hint.get_text())
        self.assertTrue(dialog.name_hint.has_css_class("problem"))
        # 押されても (試験から応答を出しても) 取り込みを始めない
        dialog.emit("response", "import")
        run_loop(lambda: False, 0.3)
        self.assertFalse(any("取り込めませんでした" in t for t in self.window_stub.toasts))

    def test_import_saves_and_opens_the_playlist(self):
        dialog = self.open(URL)
        self.ready(dialog)
        dialog.emit("response", "import")
        self.wait(lambda: self.window_stub.pages and self.window_stub.pages[-1][0] == "playlist", 8)
        self.assertEqual(self.window_stub.pages[-1], ("playlist", {"provider": "local", "id": NAME, "name": NAME}))
        self.assertIn(f"「{NAME}」を取り込みました (6 曲)", self.window_stub.toasts)
        self.assertEqual(len(self.fake.local_playlists[NAME]), 6)

    def test_name_is_prefilled_but_kept_once_edited(self):
        dialog = self.open(URL)
        self.ready(dialog)
        dialog.name_entry.set_text("a/b")
        self.assertIn("「/」と「\\」は入れられません", dialog.name_hint.get_text())
        self.assertTrue(dialog.name_hint.has_css_class("problem"))
        self.assertFalse(dialog.get_response_enabled("import"))
        dialog.name_entry.set_text("")
        self.assertIn("名前を入れて", dialog.name_hint.get_text())
        for name, reason in (("Recently Played", "cliamp が履歴に使っています"), ("..", "この名前は使えません"),
                             ("夏" * 84, "名前が長すぎます")):
            with self.subTest(name=name[:20]):
                dialog.name_entry.set_text(name)
                self.assertEqual(dialog.name_hint.get_text(), playlist_name_problem(name))
                self.assertIn(reason, dialog.name_hint.get_text())
                self.assertNotIn("「/」", dialog.name_hint.get_text())
                self.assertFalse(dialog.get_response_enabled("import"))
        dialog.name_entry.set_text("夏" * 83)
        self.assertTrue(dialog.get_response_enabled("import"))
        dialog.name_entry.set_text("Focus")
        self.assertIn("同じ名前のプレイリストがあります", dialog.name_hint.get_text())
        self.assertFalse(dialog.name_hint.has_css_class("problem"))
        self.assertTrue(dialog.get_response_enabled("import"))
        dialog.name_entry.set_text("私の名前")
        self.set_page(7, seed="other")
        self.web.pages[fx.embed_url("album", "A" * 22)] = fx.embed_page(kind="album", id="A" * 22, name="別のもの")
        self.type_url(dialog, f"spotify:album:{'A' * 22}")
        self.ready(dialog)
        self.assertEqual(dialog.name_entry.get_text(), "私の名前", "打ち替えた名前を上書きしました")
        self.assertEqual(dialog.preview_summary.get_text(), "アルバム · 架空の作り手 · 6 曲")

    def test_collision_offers_rename_and_replace(self):
        dialog = self.open(URL)
        self.ready(dialog)
        dialog.name_entry.set_text("Focus")
        dialog.emit("response", "import")
        self.wait(lambda: isinstance(self.last_dialog(), Adw.AlertDialog)
                  and not isinstance(self.last_dialog(), importer.SpotifyImportDialog), message="尋ねません")
        ask = self.last_dialog()
        self.addCleanup(close, ask)
        self.assertEqual(ask.get_heading(), "「Focus」はもうあります")
        self.assertTrue(ask.has_response("replace") and ask.has_response("rename"))
        self.assertEqual(ask.get_response_appearance("replace"), Adw.ResponseAppearance.DESTRUCTIVE)
        self.assertFalse(self.fake.requests_for("playlist_add"), "尋ねる前に足しました")
        requests = len(self.web.requests)
        ask.emit("response", "rename")
        again = self.last_dialog()
        self.addCleanup(close, again)
        self.assertIsInstance(again, importer.SpotifyImportDialog)
        self.assertEqual(again.state, "ready")
        self.assertEqual(again.name_entry.get_text(), "Focus 2")
        self.assertEqual(len(self.web.requests), requests, "別の名前にするときに読み直しました")
        again.emit("response", "import")
        self.wait(lambda: "Focus 2" in self.fake.local_playlists, 8)
        self.assertEqual(len(self.fake.local_playlists["Focus"]), 6)
        # 置き換える
        self.fake.requests.clear()
        dialog = self.open(URL)
        self.ready(dialog)
        dialog.name_entry.set_text("ドライブ")
        dialog.emit("response", "import")
        self.wait(lambda: not isinstance(self.last_dialog(), importer.SpotifyImportDialog))
        ask = self.last_dialog()
        self.addCleanup(close, ask)
        ask.emit("response", "replace")
        self.wait(lambda: self.fake.local_playlists.get("ドライブ", [{}])[0].get("path", "").startswith("ytsearch1:"),
                  8, "置き換えません")
        self.assertEqual(len(self.fake.local_playlists["ドライブ"]), 6)
        self.assertIsNotNone(self.ctx.imports.get("ドライブ"))

    def test_problems_are_explained(self):
        cases = (
            (fx.missing_page(404), "非公開"),
            ("<html>形の違う頁</html>", "形が変わった"),
            (OSError("down"), "ネットワークに繋がりません"),
        )
        dialog = self.open()
        for page, text in cases:
            with self.subTest(text=text):
                self.web.pages[fx.embed_url("playlist", PID)] = page
                self.type_url(dialog, "")
                with mock.patch.object(importer, "log"):
                    self.type_url(dialog, URL)
                    self.ready(dialog)
                self.assertEqual(dialog.state, "error")
                self.assertEqual(self.page(dialog), "problem")
                self.assertIn(text, dialog.problem)
                self.assertTrue(dialog.well.has_css_class("problem"))
                # 読み直せば通るかもしれない失敗 (ネットワーク) だけ「もう一度読む」
                self.assertEqual(dialog.retry_button.get_visible(), text == "ネットワークに繋がりません")
                self.assertFalse(dialog.name_entry.get_sensitive())
                self.assertFalse(dialog.get_response_enabled("import"))
        # Enter で読み直す
        self.set_page(6)
        dialog.url_entry.emit("activate")
        self.wait(lambda: dialog.state == "ready")

    def test_truncated_list_is_noted_before_importing(self):
        self.set_page(100, total=150)
        dialog = self.open(URL)
        self.ready(dialog)
        self.assertTrue(dialog.preview_note.get_visible())
        self.assertEqual(dialog.preview_note.get_text(), "公開ページは 100 曲までのため、最初の 100 曲だけ取り込みます")
        self.assertEqual(dialog.preview_summary.get_text(), "プレイリスト · 架空の作り手 · 100 曲 (全 150 曲)")

    def test_closing_while_loading_drops_the_answer(self):
        page = self.web.pages[fx.embed_url("playlist", PID)]
        self.web.pages[fx.embed_url("playlist", PID)] = lambda: (time.sleep(0.5), page)[1]
        dialog = self.open(URL)
        self.assertEqual(dialog.state, "loading")
        handlers = list(dialog._handlers)
        self.assertTrue(handlers, "cliamp との接続を見ていません")
        dialog.force_close()
        self.wait(lambda: dialog.closed)
        self.ctx.emit("local-playlists-changed")
        self.assertEqual(dialog._handlers, [])
        self.assertFalse(any(source.handler_is_connected(h) for source, h in handlers), "見張りを外していません")
        run_loop(lambda: False, 1.0)
        self.assertTrue(self.web.requests, "読みに行っていません")
        self.assertEqual(dialog.state, "loading")
        self.assertIsNone(dialog.result)


def menu_labels(model) -> list[str]:
    labels = []
    for i in range(model.get_n_items()):
        label = model.get_item_attribute_value(i, Gio.MENU_ATTRIBUTE_LABEL, None)
        if label is not None:
            labels.append(label.get_string())
        for link in (Gio.MENU_LINK_SECTION, Gio.MENU_LINK_SUBMENU):
            child = model.get_item_link(i, link)
            if child is not None:
                labels.extend(menu_labels(child))
    return labels


class PlaylistsImportTest(ImportPageCase):

    def test_header_button_opens_the_dialog(self):
        from cliamp_music.pages.playlists import PlaylistsPage

        page = self.show(PlaylistsPage(self.ctx))
        button = page.import_button
        self.assertTrue(button.get_mapped())
        self.assertTrue(button.get_sensitive())
        self.assertEqual(button.get_tooltip_text(), "Spotify から取り込む…")
        self.assertEqual(button.label.get_text(), "Spotify から取り込む…", "窓を開くボタンに「…」がありません")
        button.emit("clicked")
        dialog = self.last_dialog()
        self.addCleanup(close, dialog)
        self.assertIsInstance(dialog, importer.SpotifyImportDialog)
        page.on_narrow_changed(True)
        self.assertFalse(button.label.get_visible())
        self.assertTrue(button.has_css_class("compact"))
        # 取り込みの節の書き添えは空のローカルのときだけ
        self.wait(lambda: page.sections.get("local") is not None and page.sections["local"].loaded)
        self.assertFalse(page.sections["local"].action.get_visible())

    def test_imported_playlist_page(self):
        from cliamp_music.pages.playlists import IMPORTED_NOTE, PlaylistDetailPage, PlaylistsPage

        result = si.fetch_list(URL)
        done = []
        importer.save_import(self.ctx, NAME, result, callback=done.append)
        self.wait(lambda: done)
        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id=NAME, name=NAME))
        self.wait(lambda: len(page.list.rows()) == 6)
        self.assertTrue(page.header.note_label.get_visible())
        self.assertEqual(page.header.note_label.get_text(), IMPORTED_NOTE)
        self.assertEqual(page.header.subtitle_label.get_text(), "架空の作り手")
        self.assertTrue(page.imported)
        self.assertTrue(all(is_youtube_bridge(t) for t in page.tracks))
        model, _group = page._menu()
        labels = menu_labels(model)
        self.assertIn("Spotify から更新", labels)
        self.assertIn("Spotify で開く", labels)
        # 行の「…」のリンクは Spotify の曲の頁
        track_menu, _g = self.ctx.track_menu(page.tracks[0], index=0, context=f"local:{NAME}")
        self.assertIn("Spotify で開く", menu_labels(track_menu))
        # 「Spotify から更新」(確かめてから置き換える)
        self.set_page(3, seed="upd")
        self.fake.requests.clear()
        page.update_from_spotify()
        self.wait(lambda: isinstance(self.last_dialog(), Adw.AlertDialog)
                  and self.last_dialog().has_response("update"), 5, "確かめません")
        ask = self.last_dialog()
        self.addCleanup(close, ask)
        self.assertFalse(self.fake.requests_for("playlist_delete"), "確かめる前に消しました")
        ask.emit("response", "update")
        self.wait(lambda: len(page.list.rows()) == 3, 8, "更新した曲が出ません")
        self.assertTrue(self.fake.requests_for("playlist_delete"))
        self.assertEqual(page.header.note_label.get_text(), IMPORTED_NOTE)
        # すべてのプレイリストのカードには「Spotify ·」
        grid_page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: grid_page.sections.get("local") and grid_page.sections["local"].keys)
        cards = [c for c in _descendants(grid_page.sections["local"].groups) if hasattr(c, "playlist_info")]
        subtitle = {c.playlist_info.id: _texts(c) for c in cards}
        self.assertIn("Spotify · 3 曲", subtitle[NAME])
        # 削除すると記録も忘れる
        detail = self.show(PlaylistDetailPage(self.ctx, provider="local", id=NAME, name=NAME))
        self.wait(lambda: len(detail.list.rows()) == 3)
        detail.delete_playlist()
        self.wait(lambda: NAME not in self.fake.local_playlists)
        self.assertIsNone(self.ctx.imports.get(NAME))

    def test_cards_get_their_art(self):
        """すべてのプレイリストのローカルのカードの絵: 取り込んだものは Spotify の絵、ほかは曲の絵
        (格子のカードは Python から誰も持っていないので、弱い参照で待つと絵が来なかった)。"""
        from gi.repository import GdkPixbuf

        from cliamp_music.pages.playlists import PlaylistsPage

        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 64, 64)
        pixbuf.fill(0x2080ffff)
        cover = os.path.join(self.tmp, f"cover-{self._testMethodName}.png")
        pixbuf.savev(cover, "png", [], [])
        with open(cover, "rb") as handle:
            data = handle.read()

        def serve(request, timeout=None):
            if "i.scdn.co" in request.full_url:
                return fx.FakeResponse(data)
            raise OSError("試験ではネットワークに出ません")

        with mock.patch.object(artwork_mod, "urlopen", serve):
            done = []
            importer.save_import(self.ctx, NAME, si.fetch_list(URL), callback=done.append)
            self.wait(lambda: done)
            page = self.show(PlaylistsPage(self.ctx))
            self.wait(lambda: page.sections.get("local") is not None and page.sections["local"].keys
                      and len(page.sections["local"].keys) == 3)
            cards = {c.playlist_info.id: c for c in _descendants(page.sections["local"].groups)
                     if hasattr(c, "playlist_info")}
            loader = self.ctx.artwork

            def real(card) -> bool:
                texture = card.art.texture
                return texture is not None and texture is not loader.placeholder(
                    f"local:{card.playlist_info.id}", texture.get_width(), "playlist")

            self.wait(lambda: all(real(card) for card in cards.values()), 8, "カードの絵が来ません")
            self.assertEqual(cards[NAME].art.texture.get_width(), 64)  # Spotify の絵そのもの (2x2 ではない)

    def test_ordinary_local_playlist_has_no_import_note(self):
        from cliamp_music.pages.playlists import PlaylistDetailPage

        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id="Focus", name="Focus"))
        self.wait(lambda: len(page.list.rows()) == 6)
        self.assertFalse(page.header.note_label.get_visible())
        self.assertEqual(page.header.subtitle_label.get_text(), "ローカル")
        self.assertNotIn("Spotify から更新", menu_labels(page._menu()[0]))

    def test_pages_are_released(self):
        from cliamp_music.pages.playlists import PlaylistsPage

        box = {"page": PlaylistsPage(self.ctx)}
        refs = [weakref.ref(box["page"])]
        self.show(box["page"])
        self.wait(lambda: box["page"].sections.get("local") is not None)
        box["page"].import_button.emit("clicked")
        dialog = self.last_dialog()
        dialog.force_close()
        self.nav.replace([Adw.NavigationPage(title="空", tag="blank", child=Gtk.Label())])
        box.clear()
        for _ in range(5):
            run_loop(lambda: False, 0.1)
            gc.collect()
        self.assertEqual([r() for r in refs], [None], "外したページが解放されません")


class EmptyLocalTest(ImportPageCase):
    def setUp(self):
        super().setUp()
        with self.fake.lock:
            self.fake.local_playlists.clear()
        self.ctx.refresh_local_playlists()

    def test_empty_local_section_offers_the_import(self):
        from cliamp_music.pages.playlists import PlaylistsPage

        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: page.sections.get("local") is not None and page.sections["local"].loaded)
        section = page.sections["local"]
        self.assertTrue(section.action.get_visible())
        self.assertIn("Spotify の公開プレイリストを取り込めます", section.note.get_text())
        section.action.emit("clicked")
        dialog = self.last_dialog()
        self.addCleanup(close, dialog)
        self.assertIsInstance(dialog, importer.SpotifyImportDialog)


class OwnerPremiumTest(ImportPageCase):
    """Spotify の開発者アプリの持ち主が Premium でない cliamp。"""

    fake_options = {"spotify_owner_premium": True}

    def spotify_requests(self, cmd):
        return [r for r in self.requests(cmd) if r.get("provider") == "spotify"]

    def test_search_scope_explains_and_does_not_keep_asking(self):
        from cliamp_music.pages.search import SearchPage

        page = self.show(SearchPage(self.ctx))
        self.wait(lambda: page.scopes.get_n_toggles() == 3, message="Spotify の範囲が出ません")
        page.set_scope("spotify")
        page.set_query("夜")
        self.wait(lambda: page.results.state == "empty")
        empty = page.results.empty
        # 題は断られたこと (検索)、理由は説明に (題と同じ文を繰り返さない)
        self.assertEqual(empty.title_label.get_text(), SPOTIFY_SEARCH_BLOCKED_TITLE)
        self.assertEqual(empty.description_label.get_text(), SPOTIFY_OWNER_PREMIUM)
        self.assertFalse(SPOTIFY_OWNER_PREMIUM.startswith(empty.title_label.get_text()))
        self.assertNotIn("Active premium", empty.description_label.get_text())
        self.assertEqual(empty.button.label.get_text(), "YouTube で検索")
        # 説明が勧める「Spotify から取り込む」へのボタンも
        self.assertTrue(empty.secondary.get_visible())
        self.assertEqual(empty.secondary.label.get_text(), "Spotify から取り込む…")
        empty.secondary.emit("clicked")
        dialog = self.last_dialog()
        self.addCleanup(close, dialog)
        self.assertIsInstance(dialog, importer.SpotifyImportDialog)
        dialog.force_close()
        page.set_query("朝")
        self.wait(lambda: page.results.state == "empty")
        run_loop(lambda: False, 0.2)
        self.assertEqual(len(self.spotify_requests("search")), 1, "断られた Spotify に頼み続けました")
        empty.button.emit("clicked")
        self.wait(lambda: page.scope == "youtube" and page.results.state == "content")

    def test_playlists_page_and_sidebar_offer_the_import(self):
        from cliamp_music.pages.playlists import PlaylistsPage
        from cliamp_music.sidebar import IMPORT_ACTION, Sidebar

        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: page.sections.get("spotify") is not None and page.sections["spotify"].note.get_visible())
        section = page.sections["spotify"]
        self.assertEqual(section.note.get_text(), SPOTIFY_OWNER_PREMIUM)
        self.assertTrue(section.action.get_visible())
        self.assertFalse(page.sections["local"].action.get_visible())
        # サイドバー: Spotify の行の代わりに取り込みの行を 1 つ
        opened = []
        window = Gtk.Window()
        self.addCleanup(window.destroy)
        sidebar = Sidebar(self.ctx, lambda page_id, params: opened.append(page_id))
        window.set_child(sidebar)
        window.present()
        self.wait(lambda: any(r.page_id == IMPORT_ACTION for r in sidebar.rows()), message="取り込みの行が出ません")
        row = next(r for r in sidebar.rows() if r.page_id == IMPORT_ACTION)
        self.assertFalse(row.get_selectable())
        self.assertEqual(row.get_tooltip_text(), SPOTIFY_OWNER_PREMIUM)
        self.assertEqual(sum(r.page_id == IMPORT_ACTION for r in sidebar.rows()), 1)
        sidebar.select_key("playlists")
        sidebar.list.emit("row-activated", row)
        dialog = self.last_dialog()
        self.addCleanup(close, dialog)
        self.assertIsInstance(dialog, importer.SpotifyImportDialog)
        self.assertEqual(opened, [])
        self.assertEqual(sidebar.selected_key, "playlists")
        # 右クリックのメニュー
        all_row = sidebar.find_row("playlists")
        labels = menu_labels(sidebar.context_menu_model(all_row))
        self.assertEqual(labels, ["Spotify から取り込む…"])
        self.assertIsNone(sidebar.context_menu_model(sidebar.find_row("home")))
        self.assertTrue(sidebar.popup_context_menu(all_row, 10, 10))
        sidebar.context_menu.popdown()

    def test_spotify_playlist_page_offers_to_import_it(self):
        from cliamp_music.pages.playlists import PlaylistDetailPage

        page = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id=PID, name="架空"))
        self.wait(lambda: page.state.state == "empty")
        empty = page.state.empty
        self.assertEqual(empty.title_label.get_text(), "このプレイリストは読めません")
        self.assertIn("Spotify から取り込む", empty.description_label.get_text())
        self.assertEqual(empty.button.label.get_text(), "Spotify から取り込む…")
        empty.button.emit("clicked")
        dialog = self.last_dialog()
        self.addCleanup(close, dialog)
        self.assertEqual(dialog.url_entry.get_text(), f"https://open.spotify.com/playlist/{PID}")
        self.wait(lambda: dialog.state == "ready")
        # 持ち主が Premium でないと分かった後 (playlists の後) は、同じ説明を出す
        box = []
        self.ctx.catalog.playlists("spotify", box.append, force=True)
        self.wait(lambda: box)
        other = self.show(PlaylistDetailPage(self.ctx, provider="spotify", id="37i9dQZF1DXfake0001", name="Chill"))
        self.wait(lambda: other.state.state == "empty")
        self.assertEqual(other.state.empty.title_label.get_text(), "このプレイリストは読めません")
        self.assertEqual(other.state.empty.description_label.get_text(), SPOTIFY_OWNER_PREMIUM)
        button = other.state.empty.button
        self.assertFalse(button is not None and button.get_visible(), "Spotify の形でない ID に取り込みを出しました")


class SidebarMenuTest(ImportPageCase):
    def test_imported_row_offers_update(self):
        from cliamp_music.sidebar import Sidebar

        done = []
        importer.save_import(self.ctx, NAME, si.fetch_list(URL), callback=done.append)
        self.wait(lambda: done)
        window = Gtk.Window()
        self.addCleanup(window.destroy)
        sidebar = Sidebar(self.ctx, lambda *_a: None)
        window.set_child(sidebar)
        window.present()
        self.wait(lambda: sidebar.find_row(f"playlist:local:{NAME}") is not None)
        labels = menu_labels(sidebar.context_menu_model(
            sidebar.find_row(f"playlist:local:{NAME}")))
        self.assertEqual(labels, ["Spotify から取り込む…", "Spotify から更新"])
        labels = menu_labels(sidebar.context_menu_model(
            sidebar.find_row("playlist:local:Focus")))
        self.assertEqual(labels, ["Spotify から取り込む…"])
        self.assertFalse(any(r.is_action for r in sidebar.rows()))
        self.set_page(2, seed="side")
        sidebar.menu_actions.activate_action("update-spotify", gi.repository.GLib.Variant.new_string(NAME))
        self.wait(lambda: isinstance(self.last_dialog(), Adw.AlertDialog)
                  and self.last_dialog().has_response("update"), 5, "確かめません")
        ask = self.last_dialog()
        self.addCleanup(close, ask)
        ask.emit("response", "update")
        self.wait(lambda: len(self.fake.local_playlists.get(NAME, [])) == 2, 8)

    def test_section_header_has_the_import_menu(self):
        """「プレイリスト」の節の見出し (行の外に置かれる) の右クリックでも取り込みのメニュー。"""
        from cliamp_music.sidebar import Sidebar

        window = Gtk.Window()
        window.set_default_size(260, 700)
        self.addCleanup(window.destroy)
        sidebar = Sidebar(self.ctx, lambda *_a: None)
        window.set_child(sidebar)
        window.present()
        # 行が出揃って (Spotify の行は後から足す) 置き終わるまで待つ
        self.wait(lambda: any(r.params.get("provider") == "spotify" for r in sidebar.rows()))
        self.wait(lambda: all(r.get_height() > 0 for r in sidebar.rows()), 5, "行が置かれません")
        all_row = sidebar.find_row("playlists")
        header = all_row.get_header()
        self.assertIsNotNone(header)
        ok, bounds = header.compute_bounds(sidebar.list)
        self.assertTrue(ok)
        y = bounds.get_y() + bounds.get_height() / 2
        self.assertIsNone(sidebar.list.get_row_at_y(int(y)), "見出しの上で行が取れます (試験の前提が違う)")
        row, is_header = sidebar.row_at(y)
        self.assertEqual((row.key, is_header), ("playlists", True))
        self.assertEqual(menu_labels(sidebar.context_menu_model(all_row, header=True)), ["Spotify から取り込む…"])
        self.assertTrue(sidebar.popup_context_menu(all_row, 10, y, header=True))
        sidebar.context_menu.popdown()
        # 「ライブラリ」の見出しには出さない
        recent = sidebar.find_row("recent")
        ok, bounds = recent.get_header().compute_bounds(sidebar.list)
        row, is_header = sidebar.row_at(bounds.get_y() + 1)
        self.assertEqual((row.key, is_header), ("recent", True))
        self.assertIsNone(sidebar.context_menu_model(row, header=True))
        # 見出しの下の行は行のメニュー
        focus = sidebar.find_row("playlist:local:Focus")
        ok, bounds = focus.compute_bounds(sidebar.list)
        row, is_header = sidebar.row_at(bounds.get_y() + bounds.get_height() / 2)
        self.assertEqual((row.key, is_header), (focus.key, False))


class RecordingToasts:
    """本物の Adw.ToastOverlay にトーストを出し、出した時と消えた時を覚える (ctx.window の代わり)。"""

    def __init__(self, stub):
        self.stub = stub
        self.overlay = Adw.ToastOverlay()
        self.overlay.set_child(Gtk.Label())
        self.window = Gtk.Window()
        self.window.set_child(self.overlay)
        self.window.present()
        self.events: list[tuple[str, str]] = []

    def navigate(self, page_id, **params):
        self.stub.navigate(page_id, **params)

    def toast(self, text):
        self.stub.toasts.append(text)
        toast = Adw.Toast.new(text)
        toast.set_timeout(4)
        toast.connect("dismissed", lambda _t: self.events.append(("dismissed", text)))
        self.events.append(("shown", text))
        self.overlay.add_toast(toast)
        return toast


class UpdateTest(ImportPageCase):
    """「Spotify から更新」: 読み直し、今の曲と比べて確かめてから置き換える。"""

    def setUp(self):
        super().setUp()
        self.name = f"更新 {self._testMethodName[-12:]}"
        self.ctx.imports.forget(self.name)
        done = []
        importer.save_import(self.ctx, self.name, si.fetch_list(URL), callback=done.append, navigate=False)
        self.wait(lambda: done == [True])
        self.window_stub.toasts.clear()

    def update(self) -> tuple[list, "Adw.AlertDialog | None"]:
        done = []
        self.assertTrue(self.ctx.update_spotify_import(self.name, done.append))
        self.wait(lambda: done or (isinstance(self.last_dialog(), Adw.AlertDialog)
                                   and self.last_dialog().has_response("update")), 5, "答えません")
        ask = self.last_dialog() if not done else None
        if ask is not None:
            self.addCleanup(close, ask)
        return done, ask

    def test_confirms_counts_then_replaces(self):
        self.set_page(8, seed="new")
        done, ask = self.update()
        self.assertIsNotNone(ask)
        self.assertEqual(ask.get_heading(), "Spotify から更新しますか？")
        self.assertIn(f"「{self.name}」の 6 曲を、Spotify のプレイリスト「{NAME}」の 8 曲に置き換えます", ask.get_body())
        self.assertNotIn("外れます", ask.get_body())
        self.assertEqual(ask.get_default_response(), "update")
        self.assertIn(self.name, self.ctx.spotify_imports_busy)
        self.assertFalse(self.ctx.update_spotify_import(self.name), "確かめている間に 2 度目を始めました")
        ask.emit("response", "update")
        self.wait(lambda: done, 8)
        self.assertEqual(done, [True])
        self.assertIn(f"「{self.name}」を更新しました (8 曲)", self.window_stub.toasts)
        self.assertEqual(len(self.fake.local_playlists[self.name]), 8)
        self.assertEqual(self.ctx.imports.get(self.name).count, 8)
        self.assertEqual(self.ctx.spotify_imports_busy, set())

    def test_cancel_keeps_the_playlist(self):
        self.set_page(8, seed="new")
        before = [dict(t) for t in self.fake.local_playlists[self.name]]
        done, ask = self.update()
        ask.emit("response", "cancel")
        self.assertEqual(done, [False])
        run_loop(lambda: False, 0.3)
        self.assertEqual(self.fake.local_playlists[self.name], before)
        self.assertFalse(self.fake.requests_for("playlist_delete"))
        self.assertEqual(self.ctx.spotify_imports_busy, set())

    def test_own_tracks_are_named_before_they_are_dropped(self):
        own = Track(path="/music/own.flac", title="自分の曲", artist="私")
        added = []
        self.ctx.catalog.playlist_add(self.name, [own], added.append)
        self.wait(lambda: added)
        self.set_page(8, seed="new")
        done, ask = self.update()
        self.assertIn(f"「{self.name}」の 7 曲を", ask.get_body())
        self.assertIn("このうち 1 曲は Spotify から取り込んだ曲ではないため、プレイリストから外れます", ask.get_body())
        self.assertEqual(ask.get_default_response(), "cancel")
        self.assertEqual(ask.get_response_appearance("update"), Adw.ResponseAppearance.DESTRUCTIVE)

    def test_record_of_a_recreated_playlist_is_not_trusted(self):
        """記録は名前だけで引く。TUI で消して同じ名前で作り直したもの (取り込んだ曲が無い) は置き換えない。"""
        with self.fake.lock:
            self.fake.local_playlists[self.name] = [{"path": "/music/a.flac", "title": "A"},
                                                    {"path": "/music/b.flac", "title": "B"}]
        self.ctx.imports.record(self.name, si.fetch_list(URL), now=1000.0)  # 取り込んだのはずっと前
        self.set_page(8, seed="new")
        done, ask = self.update()
        self.assertIsNone(ask, "作り直したプレイリストを置き換えようとしました")
        self.assertEqual(done, [False])
        self.assertIn(f"「{self.name}」はもう Spotify から取り込んだプレイリストではないため、更新しません",
                      self.window_stub.toasts)
        self.assertEqual([t["path"] for t in self.fake.local_playlists[self.name]], ["/music/a.flac", "/music/b.flac"])
        self.assertIsNone(self.ctx.imports.get(self.name), "記録を忘れていません")

    def test_same_tracks_are_not_replaced(self):
        done, ask = self.update()
        self.assertIsNone(ask)
        self.assertEqual(done, [True])
        self.assertIn(f"「{self.name}」はもう最新です (6 曲)", self.window_stub.toasts)
        self.assertFalse(self.fake.requests_for("playlist_delete"))

    def test_loading_toast_is_gone_before_the_result(self):
        """読み込み中のトーストを出したままにすると、Adw.ToastOverlay は後のトーストを 4 秒待たせる。"""
        toasts = RecordingToasts(self.window_stub)
        self.addCleanup(toasts.window.destroy)
        self.ctx.window = toasts
        self.set_page(8, seed="new")
        done, ask = self.update()
        loading = f"「{self.name}」を Spotify から読み込んでいます…"
        self.assertIn(("dismissed", loading), toasts.events, "確かめる前に読み込み中のトーストを消していません")
        ask.emit("response", "update")
        self.wait(lambda: done, 8)
        result = f"「{self.name}」を更新しました (8 曲)"
        self.assertLess(toasts.events.index(("dismissed", loading)), toasts.events.index(("shown", result)))
        # 読めなかったときも
        toasts.events.clear()
        self.web.pages[fx.embed_url("playlist", PID)] = fx.missing_page(404)
        failed = []
        with mock.patch.object(importer, "log"):
            self.ctx.update_spotify_import(self.name, failed.append)
            self.wait(lambda: failed)
        shown = [text for kind, text in toasts.events if kind == "shown"]
        self.assertTrue(shown[-1].startswith("更新できませんでした: 見つかりません"), shown)
        self.assertLess(toasts.events.index(("dismissed", loading)),
                        toasts.events.index(("shown", shown[-1])))


class ReplaceFailureTest(ImportPageCase):
    def test_lost_tracks_are_kept_and_offered_back(self):
        """消した後で足すことも戻すこともできなかった (ディスクが一杯・cliamp が止まった): 曲の控えを残し、
        窓でそのことと「もう一度戻す」を出す (トーストの「取り込めませんでした」だけでは失ったことが分からない)。"""
        result = si.fetch_list(URL)
        old = [t["path"] for t in self.fake.local_playlists["Focus"]]
        before = set(self.ctx.imports.backups())
        original = self.fake._cmd_playlist_add
        self.fake._cmd_playlist_add = lambda req, now: {"ok": False, "error": "write Focus.toml: no space left on device"}
        done = []
        with mock.patch.object(importer, "log"):
            importer.save_import(self.ctx, "Focus", result, replace=True, callback=done.append)
            self.wait(lambda: done, 8)
        self.assertEqual(done, [False])
        self.assertNotIn("Focus", self.fake.local_playlists)
        backups = sorted(set(self.ctx.imports.backups()) - before)
        self.assertEqual(len(backups), 1, "前の曲を控えていません")
        with open(backups[0], encoding="utf-8") as handle:
            kept = __import__("json").load(handle)
        self.assertEqual((kept["name"], [t["path"] for t in kept["tracks"]]), ("Focus", old))
        ask = self.last_dialog()
        self.addCleanup(close, ask)
        self.assertIsInstance(ask, Adw.AlertDialog)
        self.assertEqual(ask.get_heading(), "「Focus」の曲を戻せませんでした")
        self.assertIn(f"前の {len(old)} 曲も戻せませんでした", ask.get_body())
        self.assertIn(os.path.basename(backups[0]), ask.get_body())
        # 直ったら「もう一度戻す」
        self.fake._cmd_playlist_add = original
        ask.emit("response", "restore")
        self.wait(lambda: "Focus" in self.fake.local_playlists, 8)
        self.assertEqual([t["path"] for t in self.fake.local_playlists["Focus"]], old)
        self.wait(lambda: f"「Focus」の前の {len(old)} 曲を戻しました" in self.window_stub.toasts)
        self.assertFalse(os.path.exists(backups[0]), "戻せたのに控えが残っています")
        self.wait(lambda: "Focus" in self.ctx.local_playlists)


class StaleRecordTest(ImportPageCase):
    """記録だけ残って中身が別物 (TUI で消して同じ名前で作り直した) のプレイリストは、取り込んだものとして
    見せない (書き添え・「Spotify ·」・Spotify の絵・「Spotify から更新」) し、記録も忘れる。"""

    def setUp(self):
        super().setUp()
        self.name = f"Chill {self._testMethodName[-8:]}"
        done = []
        importer.save_import(self.ctx, self.name, si.fetch_list(URL), callback=done.append, navigate=False)
        self.wait(lambda: done == [True])
        # TUI で消して、手元の 2 曲で作り直した (GUI は閉じていた)。取り込んだのは前のこと
        with self.fake.lock:
            self.fake.local_playlists[self.name] = [{"path": "/music/a.flac", "title": "A"},
                                                    {"path": "/music/b.flac", "title": "B"}]
        self.ctx.imports.record(self.name, si.fetch_list(URL), now=1000.0)
        self.ctx.catalog.invalidate("local")

    def test_detail_page(self):
        from cliamp_music.pages.playlists import PlaylistDetailPage

        page = self.show(PlaylistDetailPage(self.ctx, provider="local", id=self.name, name=self.name))
        self.wait(lambda: len(page.list.rows()) == 2)
        self.assertFalse(page.imported)
        self.assertFalse(page.header.note_label.get_visible())
        self.assertEqual(page.header.subtitle_label.get_text(), "ローカル")
        labels = menu_labels(page._menu()[0])
        self.assertNotIn("Spotify から更新", labels)
        self.assertNotIn("Spotify で開く", labels)
        self.assertIsNone(self.ctx.imports.get(self.name), "記録を忘れていません")

    def test_card(self):
        from cliamp_music.pages.playlists import PlaylistsPage

        page = self.show(PlaylistsPage(self.ctx))
        self.wait(lambda: page.sections.get("local") is not None and page.sections["local"].keys)
        card = next(c for c in _descendants(page.sections["local"].groups)
                    if getattr(c, "playlist_info", None) is not None and c.playlist_info.id == self.name)
        self.wait(lambda: card.subtitle_label.get_text() == "2 曲", 5,
                  f"「Spotify ·」が残っています ({card.subtitle_label.get_text()!r})")
        self.assertIsNone(self.ctx.imports.get(self.name))

    def test_recent_import_is_not_forgotten_by_a_late_answer(self):
        """取り込んだばかりの記録は、取り込む前に頼んだ曲の一覧が後から届いても忘れない。"""
        self.ctx.imports.record(self.name, si.fetch_list(URL))
        plain = [Track(path="/music/a.flac"), Track(path="/music/b.flac")]
        self.assertIsNone(self.ctx.imported_record(self.name, plain))
        self.assertIsNotNone(self.ctx.imports.get(self.name))


def close(dialog) -> None:
    """試験の後始末: まだ出ている窓だけ閉じる。"""
    if dialog is not None and dialog.get_root() is not None:
        dialog.force_close()


def _descendants(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from _descendants(child)
        child = child.get_next_sibling()


def _texts(widget) -> list[str]:
    return [w.get_text() for w in _descendants(widget) if isinstance(w, Gtk.Label)]


if not HAVE_DISPLAY:  # pragma: no cover
    for _case in (DialogTest, PlaylistsImportTest, EmptyLocalTest, OwnerPremiumTest, SidebarMenuTest, UpdateTest,
                  ReplaceFailureTest, StaleRecordTest):
        _case.__unittest_skip__ = True
        _case.__unittest_skip_why__ = SKIP

if __name__ == "__main__":
    unittest.main()
