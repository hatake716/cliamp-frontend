"""Spotify が Web API で読ませない公開プレイリストの表示 (ネットワークなし)。

公開ページの取得は fetch_async の返答を手で進め、API の失敗・取得の行き違い・
表示した曲からの再生を、本物のページと偽の cliamp で確かめる。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import spotify_fixtures as fx  # noqa: E402
import test_pages as pages_tests  # noqa: E402
from cliamp_music.protocol import is_spotify_not_accessible  # noqa: E402
from cliamp_music.spotify_import import (  # noqa: E402
    SpotifyImportError,
    SpotifyRef,
    parse_embed_html,
    with_total,
)

if pages_tests.HAVE_DISPLAY:
    from cliamp_music.pages.playlists import PlaylistDetailPage


PID = "37i9dQZF1DXfakePlayls1"
LEGACY_FORBIDDEN = ("spotify: playlist not accessible: only playlists you own or collaborate on "
                    "can be loaded")
NATIVE_FORBIDDEN = ('spotify: list tracks: http status 403 Forbidden: '
                    '{"error":{"status":403,"message":"Forbidden"}}')
OWNER_PREMIUM = ('spotify: list tracks: http status 403 Forbidden: '
                 '{"error":{"status":403,"message":"Active premium subscription required for the owner of the app"}}')
PUBLIC_NOTE = "Spotify の公開ページから表示 · 曲は YouTube で探して再生します"


def public_list(*, count=3, total=0, name="公開プレイリスト", seed="fallback"):
    result = parse_embed_html(
        fx.embed_page(id=PID, name=name, items=fx.tracks(count, seed), cover=None),
        SpotifyRef("playlist", PID),
    )
    return with_total(result, total) if total else result


def setUpModule():  # noqa: N802
    # PageCase と同じく、アートワークも実ネットワークへ出さない。
    pages_tests.setUpModule()


def tearDownModule():  # noqa: N802
    pages_tests.tearDownModule()


class SpotifyPlaylistAccessErrorTest(unittest.TestCase):
    def test_legacy_and_native_playlist_forbidden_are_recognized(self):
        for error in (LEGACY_FORBIDDEN, NATIVE_FORBIDDEN):
            with self.subTest(error=error):
                self.assertTrue(is_spotify_not_accessible(error))

    def test_unrelated_forbidden_is_not_a_playlist_access_error(self):
        for error in (
            'spotify: search: http status 403 Forbidden: {"error":{"status":403,"message":"Forbidden"}}',
            'navidrome: list tracks: http status 403 Forbidden',
            'spotify: list tracks: http status 401 Unauthorized',
        ):
            with self.subTest(error=error):
                self.assertFalse(is_spotify_not_accessible(error))


class SpotifyPublicPlaylistFallbackTest(pages_tests.PageCase):
    def setUp(self):
        super().setUp()
        self.fetches = []
        fetcher = patch("cliamp_music.importer.fetch_async", side_effect=self._fetch)
        fetcher.start()
        self.addCleanup(fetcher.stop)

    def _fetch(self, target, callback):
        self.fetches.append((target, callback))

    def open_forbidden(self, error=NATIVE_FORBIDDEN, *, id=PID):
        self.fake.spotify_error = error
        return self.show(PlaylistDetailPage(self.ctx, provider="spotify", id=id, name="共有したリスト"))

    def answer(self, result, index=-1):
        self.fetches[index][1](result)

    def assert_public_rows(self, page, result):
        self.wait(lambda: len(page.list.rows()) == result.count)
        self.assertEqual([row.track.path for row in page.list.rows()], [t.path for t in result.tracks])
        self.assertEqual(page.state.state, "content")
        self.assertIs(page._public_list, result)
        self.assertIn(PUBLIC_NOTE, page.header.note_label.get_text())
        self.assertTrue(page.header.play_button.get_sensitive())

    def test_native_forbidden_displays_public_tracks(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        self.assertEqual(self.fetches[0][0], SpotifyRef("playlist", PID))
        result = public_list()
        self.answer(result)
        self.assert_public_rows(page, result)
        self.assertEqual(page.header.info_label.get_text().split(" · ")[0], "3 曲")

    def test_legacy_forbidden_also_displays_public_tracks(self):
        page = self.open_forbidden(LEGACY_FORBIDDEN)
        self.wait(lambda: len(self.fetches) == 1)
        result = public_list()
        self.answer(result)
        self.assert_public_rows(page, result)

    def test_truncated_public_page_explains_displayed_and_total_counts(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        result = public_list(count=100, total=102)
        self.answer(result)
        self.assert_public_rows(page, result)
        note = page.header.note_label.get_text()
        self.assertIn("100", note)
        self.assertIn("102", note)

    def test_play_and_shuffle_use_displayed_tracks(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        result = public_list(count=6)
        self.answer(result)
        self.assert_public_rows(page, result)

        page.play_from(2)
        self.wait(lambda: len(self.requests("replace")) == 1)
        request = self.requests("replace")[-1]
        self.assertEqual(request["index"], 2)
        self.assertEqual([t["path"] for t in request["tracks"]], [t.path for t in result.tracks])

        page.shuffle()
        self.wait(lambda: len(self.requests("replace")) == 2)
        shuffled = self.requests("replace")[-1]
        self.assertCountEqual([t["path"] for t in shuffled["tracks"]], [t.path for t in result.tracks])
        self.assertEqual(self.requests("load_provider"), [], "読めなかった Web API を再生時に取り直しました")

    def test_public_failure_explains_reason_and_can_retry(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        reason = "Spotify の応答がありません (時間切れ)"
        self.answer(SpotifyImportError(reason, "network"))
        self.assertEqual(page.state.state, "empty")
        self.assertIn(reason, page.state.empty.description_label.get_text())
        retry = page.state.empty.button
        self.assertIsNotNone(retry)
        self.assertTrue(retry.get_visible())
        retry.emit("clicked")
        self.wait(lambda: len(self.fetches) == 2)
        result = public_list()
        self.answer(result)
        self.assert_public_rows(page, result)

    def test_old_public_reply_cannot_replace_a_newer_refresh(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        page.load(force=True)
        self.wait(lambda: len(self.fetches) == 2)
        newer = public_list(count=4, seed="newer")
        self.answer(newer, index=1)
        self.assert_public_rows(page, newer)
        self.answer(SpotifyImportError("古い取得の失敗", "network"), index=0)
        self.assert_public_rows(page, newer)

    def test_hidden_page_reloads_after_public_reply_arrives_offline(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        blank = pages_tests.Adw.NavigationPage(title="別のページ", child=pages_tests.Gtk.Label())
        self.nav.push(blank)
        self.wait(lambda: not page.get_mapped())

        # 隠れたページは接続シグナルを監視していない。ポーリングを止めずに、
        # 公開ページの返答時だけ接続断を再現する。
        with patch.object(type(self.ctx.store), "connected", new_callable=PropertyMock, return_value=False):
            self.answer(public_list())
        self.assertTrue(page._stale, "未接続中の返答を捨てたページが再読み込み対象になりません")
        self.assertEqual(page.list.rows(), [])

        self.nav.pop()
        self.wait(lambda: page.get_mapped() and len(self.fetches) == 2)
        result = public_list(count=4, seed="reconnected")
        self.answer(result)
        self.assert_public_rows(page, result)
        self.assertFalse(page._stale)

    def test_successful_api_refresh_clears_public_page_state_and_note(self):
        page = self.open_forbidden()
        self.wait(lambda: len(self.fetches) == 1)
        result = public_list()
        self.answer(result)
        self.assert_public_rows(page, result)

        # 所有者・共同編集者になった等で、次の Web API 取得は通る。
        api_tracks = list(self.fake.spotify_lists["YOUR MUSIC"][2])
        self.fake.spotify_lists[PID] = ("共有したリスト", "Your playlists", api_tracks)
        self.fake.spotify_error = ""
        page.load(force=True)
        self.wait(lambda: page._public_list is None and len(page.list.rows()) == len(api_tracks))
        self.assertEqual([row.track.path for row in page.list.rows()], [track["path"] for track in api_tracks])
        self.assertEqual(page.state.state, "content")
        self.assertNotIn("公開ページから表示", page.header.note_label.get_text())
        self.assertFalse(page.header.note_label.get_visible())
        self.assertEqual(len(self.fetches), 1, "Web API の成功後も公開ページを取得しました")

    def test_non_playlist_id_does_not_request_public_page(self):
        page = self.open_forbidden(id="YOUR MUSIC")
        self.wait(lambda: page.state.state == "empty")
        self.assertEqual(self.fetches, [])

    def test_owner_premium_requirement_keeps_existing_explanation(self):
        page = self.open_forbidden(OWNER_PREMIUM)
        self.wait(lambda: page.state.state == "empty")
        self.assertEqual(self.fetches, [])
        self.assertIn("Premium", page.state.empty.description_label.get_text())
