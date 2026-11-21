"""アカウントのプレイリストを既存の詳細・再生操作から使う。"""
from unittest.mock import patch

from cliamp_music.protocol import Track
from tests import test_pages


class AccountLibraryTest(test_pages.PageCase):
    def test_authenticated_cards_open_the_private_detail_and_play_in_order(self):
        from cliamp_music.pages import playlists as pages
        songs = [Track(f"https://music.youtube.com/watch?v={i:011d}", title=f"曲{i}") for i in (2, 1)]
        with patch.object(pages.youtube_account, "is_connected", return_value=True), patch.object(
                pages.youtube_account, "list_playlists", return_value=[("PLprivate", "非公開")]), patch.object(
                pages.youtube_account, "list_tracks", return_value=songs) as lookup:
            page = self.show(pages.PlaylistsPage(self.ctx))
            self.wait(lambda: bool(page.sections.get("youtube-account") and page.sections["youtube-account"].keys))
            grid = page.sections["youtube-account"].groups.get_first_child()
            info = grid.get_child_at_index(0).get_child().playlist_info
            self.assertEqual((info.provider, info.id, info.name), ("youtube-account", "PLprivate", "非公開"))
            detail = self.show(pages.PlaylistDetailPage(self.ctx, provider=info.provider, id=info.id, name=info.name))
            self.wait(lambda: detail.tracks == songs)
            lookup.assert_called_with("PLprivate")
            detail.play_from(1)
            self.wait(lambda: bool(self.requests("replace")))
            request = self.requests("replace")[-1]
            self.assertEqual([t["path"] for t in request["tracks"]], [t.path for t in songs])
            self.assertEqual(request["index"], 1)
            self.assertFalse(any(r.get("provider") == "youtube-account" for r in self.requests("load_provider")))
