"""保存先の表示と書き込みの境界を、実際の GTK ダイアログで確かめる。"""
import unittest
from unittest.mock import Mock, patch

from cliamp_music.protocol import Response, Track

try:
    import gi
    gi.require_version("Adw", "1")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Adw, Gtk
    HAS_DISPLAY = Gtk.init_check() and bool(Gtk.Settings.get_default())
except (ImportError, ValueError):
    HAS_DISPLAY = False

SONG_ID = "0123456789ABCDEFGHIJKL"
SONG = Track("ytsearch1:Artist Song", title="一曲目", meta={"spotify.id": SONG_ID})
VIDEO = Track("https://music.youtube.com/watch?v=abcdef12345", title="二曲目")


@unittest.skipUnless(HAS_DISPLAY, "GTK の画面が必要")
class DestinationTest(unittest.TestCase):
    def setUp(self):
        from cliamp_music import playlist_picker
        self.picker = playlist_picker
        self.window = Adw.Window()
        self.window.present()
        self.ctx = Mock(window=self.window, playlist_picker=None)
        self.ctx.store.supports.return_value = True
        self.ctx.imports.auto_sync_names.return_value = ["自動同期"]
        self.ctx.refresh_local_playlists.side_effect = lambda *, then: then(["手元", "自動同期"])

    def tearDown(self):
        self.window.destroy()

    def open(self, *tracks):
        self.dialog = self.picker.present_picker(self.ctx, tracks)
        content = self.dialog.get_extra_child()
        self.service = content.get_first_child()
        self.target = self.service.get_next_sibling()
        self.name = self.target.get_next_sibling()

    def test_local_order_and_managed_lists(self):
        self.open(SONG, VIDEO)
        self.assertEqual(self.target.get_model().get_string(0), "手元")
        self.assertEqual(self.target.get_model().get_n_items(), 2)
        self.target.set_selected(1)
        self.name.set_text("自動同期")
        self.assertFalse(self.dialog.get_response_enabled("add"))
        self.name.set_text("新しいリスト")
        self.assertTrue(self.dialog.get_response_enabled("add"))
        self.dialog.emit("response", "add")
        self.ctx.add_to_playlist.assert_called_once_with("新しいリスト", (SONG, VIDEO))

    def test_spotify_saves_exact_id_and_reports_partial_write(self):
        self.open(SONG)
        self.service.set_selected(1)
        self.assertFalse(self.dialog.get_response_enabled("add"))
        cmd, callback = self.ctx.client.request.call_args.args
        self.assertEqual(cmd, "spotify_destinations")
        callback(Response(True, {"playlists": [{"id": "dest", "name": "名前"}]}))
        self.assertTrue(self.dialog.get_response_enabled("add"))
        self.dialog.emit("response", "add")
        args, fields = self.ctx.client.request.call_args
        self.assertEqual(args[0], "spotify_save")
        self.assertEqual(fields["id"], "dest")
        self.assertNotIn("name", fields)
        self.assertEqual([t.path for t in fields["tracks"]], ["spotify:track:" + SONG_ID])
        args[1](Response(False, {"id":"dest", "added": 1}, "quota"))
        self.assertIn("1", self.ctx.toast.call_args.args[0])
        self.ctx.emit.assert_called_with("account-playlists-changed", "spotify")

    def test_switching_service_ignores_stale_destinations(self):
        self.open(SONG)
        self.service.set_selected(1)
        callback = self.ctx.client.request.call_args.args[1]
        self.service.set_selected(2)
        callback(Response(True, {"playlists": [{"id":"stale", "name":"古い一覧"}]}))
        self.assertFalse(self.dialog.get_response_enabled("add"))
        self.assertFalse(self.target.get_visible())

    def test_youtube_existing_target_and_private_creation(self):
        for new in (False, True):
            with self.subTest(new=new), patch.object(self.picker, "account") as api, patch.object(
                    self.picker, "run_async", side_effect=lambda fn, done: done(fn(), None)):
                api.is_connected.return_value = True
                api.list_playlists.return_value = [("PLexisting","公開リスト")]
                api.create_playlist.return_value = "PLcreated"
                api.add_tracks.return_value = 1
                self.open(VIDEO)
                self.service.set_selected(2)
                if new:
                    self.target.set_selected(1)
                    self.name.set_text("非公開テスト")
                self.assertTrue(self.dialog.get_response_enabled("add"))
                self.dialog.emit("response", "add")
                api.add_tracks.assert_called_once_with("PLcreated" if new else "PLexisting", ["abcdef12345"])
                if new:
                    api.create_playlist.assert_called_once_with("非公開テスト")
                else:
                    api.create_playlist.assert_not_called()
                self.ctx.emit.assert_called_with("account-playlists-changed", "youtube-account")
                self.dialog.force_close()
