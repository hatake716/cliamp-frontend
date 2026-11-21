"""プレイリストの登録と再試行。通信と認証は試験用の相手だけを使う。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from cliamp_music.protocol import PlaylistInfo, Response

try:
    from cliamp_music import account_bookmarks as bookmarks
    from cliamp_music.context import AppContext
    from cliamp_music.window import MusicWindow
except (ImportError, ValueError):
    bookmarks = None


SPOTIFY = PlaylistInfo("spotify", "37i9dQZF1DXcBWIGoYBM5M", "雨の音")
LIST_ID = "PL12345678901234567890123456789012"
YOUTUBE = PlaylistInfo("url", f"https://www.youtube.com/watch?v=12345678901&list={LIST_ID}", "雨の音")


@unittest.skipIf(bookmarks is None, "GTK がありません")
class AccountBookmarksTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Mock()
        self.ctx.window = None
        self.ctx._pending_account_saves = set()
        self.ctx.set_playlist_saved.return_value = True
        self.ctx.store.supports.return_value = True

    def test_spotify_pending_and_retry_after_failure(self):
        ctx = self.ctx
        bookmarks.save_playlist_to_account(ctx, SPOTIFY)
        args, kwargs = ctx.client.request.call_args
        self.assertEqual(args[0], "save_playlist_to_library")
        self.assertEqual(kwargs, {"provider": "spotify", "id": SPOTIFY.id})
        self.assertLess(ctx.mock_calls.index(call.set_playlist_saved(SPOTIFY, True)),
                        ctx.mock_calls.index(call.client.request(*args, **kwargs)))
        ctx.app.begin_account_save.assert_called_once_with()
        bookmarks.save_playlist_to_account(ctx, SPOTIFY)
        ctx.client.request.assert_called_once()
        ctx.app.end_account_save.assert_not_called()
        ctx.catalog.invalidate.assert_not_called()

        args[1](Response(False, error="保存できません"))
        ctx.app.end_account_save.assert_called_once_with()
        ctx.emit.assert_not_called()
        ctx.set_playlist_saved.assert_called_once_with(SPOTIFY, True)
        message, options = ctx.toast.call_args
        self.assertIn(SPOTIFY.name, message[0])
        uri = "https://open.spotify.com/playlist/" + SPOTIFY.id
        self.assertEqual(options["action_uri"], uri)
        ctx.open_uri.assert_not_called()
        ctx.toast.return_value.connect.call_args.args[1](None)
        ctx.open_uri.assert_called_once_with(uri)

        bookmarks.save_playlist_to_account(ctx, SPOTIFY)
        self.assertEqual(ctx.client.request.call_count, 2)
        ctx.client.request.call_args.args[1](Response(True))
        self.assertEqual(ctx.app.begin_account_save.call_count, 2)
        self.assertEqual(ctx.app.end_account_save.call_count, 2)
        ctx.catalog.invalidate.assert_called_once_with("spotify")
        ctx.emit.assert_called_once_with("account-playlists-changed", "spotify")
        self.assertFalse(ctx._pending_account_saves)

    def test_local_failure_and_invalid_ids_send_nothing(self):
        ctx = self.ctx
        ctx.set_playlist_saved.return_value = False
        bookmarks.save_playlist_to_account(ctx, SPOTIFY)
        bookmarks.save_playlist_to_account(ctx, PlaylistInfo("spotify", "abc", "短いID"))
        bookmarks.save_playlist_to_account(ctx, PlaylistInfo("url", "https://example.org/playlist?list=PL123", "外部"))
        ctx.set_playlist_saved.assert_called_once_with(SPOTIFY, True)
        ctx.app.begin_account_save.assert_not_called()
        ctx.client.request.assert_not_called()
        self.assertFalse(ctx._pending_account_saves)

    def test_connected_youtube_updates_only_after_acknowledgement(self):
        ctx = self.ctx
        with (patch.object(bookmarks.youtube_account, "is_connected", return_value=True),
              patch.object(bookmarks.youtube_account, "save_playlist_to_library") as remote,
              patch.object(bookmarks, "run_async") as worker):
            bookmarks.save_playlist_to_account(ctx, YOUTUBE)
            ctx.set_playlist_saved.assert_called_once_with(YOUTUBE, True)
            work, done = worker.call_args.args
            self.assertEqual(worker.call_args.kwargs, {"keep_running": True})
            work()
            remote.assert_called_once_with(LIST_ID)
            ctx.app.begin_account_save.assert_called_once_with()
            ctx.catalog.invalidate.assert_not_called()
            ctx.emit.assert_not_called()
            ctx.open_uri.assert_not_called()
            done(None, None)
            ctx.app.end_account_save.assert_called_once_with()
            ctx.catalog.invalidate.assert_called_once_with("ytmusic")
            ctx.emit.assert_called_once_with("account-playlists-changed", "youtube-account")
            self.assertFalse(ctx._pending_account_saves)

    def test_disconnected_youtube_explicit_web_and_cancel(self):
        ctx = self.ctx
        with (patch.object(bookmarks.youtube_account, "is_connected", return_value=False),
              patch.object(bookmarks.Adw, "AlertDialog") as alert,
              patch.object(bookmarks, "connect_youtube") as connect,
              patch.object(bookmarks, "run_async") as worker):
            for answer in ("close", "connect", "web"):
                bookmarks.save_playlist_to_account(ctx, YOUTUBE)
                self.assertIn("このPC", alert.call_args.kwargs["body"])
                dialog = alert.return_value
                dialog.add_response.assert_any_call("web", "YouTube Music で開く")
                ctx.open_uri.assert_not_called()
                self.assertTrue(ctx._pending_account_saves)
                dialog.connect.call_args.args[1](dialog, answer)
                if answer == "connect":
                    connect.call_args.args[1]("キャンセルしました")
                self.assertFalse(ctx._pending_account_saves)
            ctx.open_uri.assert_called_once_with(f"https://music.youtube.com/playlist?list={LIST_ID}")
            worker.assert_not_called()
            ctx.emit.assert_not_called()
            ctx.app.begin_account_save.assert_not_called()

    def test_same_title_toast_distinguishes_destinations(self):
        view = SimpleNamespace(_toast_last=None, _toasts=Mock())
        first, second = ("https://open.spotify.com/playlist/" + c * 22 for c in "AB")
        with (patch("cliamp_music.window.Adw.Toast") as toast_type,
              patch("cliamp_music.window.Gtk.Label"),
              patch("cliamp_music.window.GLib.get_monotonic_time", return_value=12345)):
            toast_type.new.side_effect = lambda title: Mock()
            show = lambda url: MusicWindow.toast_for_uri(view, "雨の音を保存できませんでした", url)
            self.assertIsNotNone(show(first))
            self.assertIsNone(show(first))
            self.assertIsNotNone(show(second))
            self.assertEqual(view._toasts.add_toast.call_count, 2)

        legacy = SimpleNamespace(toast=Mock())
        AppContext.toast(SimpleNamespace(window=legacy), "保存できませんでした", action_uri=first)
        legacy.toast.assert_called_once_with("保存できませんでした")
