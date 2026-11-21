"""アルバムと曲の ID を区別し、全曲を取得できた場合だけ追加へ進む。"""
import unittest
from unittest.mock import MagicMock, patch

from cliamp_music import album
from cliamp_music.protocol import Track
from cliamp_music.spotify_import import ImportedList, SpotifyRef


class AlbumTest(unittest.TestCase):
    song_id = "s" * 22
    album_id = "a" * 22
    video_id = "first_video"
    second_id = "other_video"

    def spotify_track(self, **meta):
        return Track(path="ytsearch1:Artist Song", meta={"spotify.id": self.song_id, **meta})

    def test_native_album_uses_album_id_and_keeps_order(self):
        ctx = MagicMock()
        songs = [self.spotify_track(), Track("spotify:track:" + "t" * 22)]
        ctx.catalog.tracks.side_effect = lambda provider, key, callback: callback(songs)
        with patch.object(album.spotify_import, "fetch_text") as public_lookup:
            album.choose_album(ctx, self.spotify_track(**{"spotify.album_id": self.album_id}))
            ctx.catalog.tracks.assert_called_once()
            self.assertEqual(ctx.catalog.tracks.call_args.args[:2], ("spotify", "spotify:album:" + self.album_id))
            self.assertEqual(tuple(ctx.choose_playlist.call_args.args[0]), tuple(songs))
            public_lookup.assert_not_called()

    def test_public_track_meta_resolves_the_exact_album(self):
        ref = SpotifyRef("album", self.album_id)
        songs = (self.spotify_track(), Track("spotify:track:" + "t" * 22))
        html = f'<meta name="music:album:track" content="3"><meta content="{ref.url}" property="music:album">'
        response = ImportedList("album", self.album_id, "Album", tracks=songs)
        with patch.object(album.spotify_import, "fetch_text", return_value=html) as get_page, \
             patch.object(album.spotify_import, "fetch_list", return_value=response) as get_album:
            self.assertEqual(album.resolve_album(self.spotify_track()), songs)
            self.assertEqual(get_page.call_args.args[0], "https://open.spotify.com/track/" + self.song_id)
            get_album.assert_called_once_with(ref)

    def test_incomplete_or_unrelated_public_album_cannot_be_saved(self):
        track = self.spotify_track(**{"spotify.album_id": self.album_id})
        for override in ({"truncated": True}, {"skipped": 1}, {"id": "z" * 22}, {"kind": "playlist"},
                         {"tracks": (Track("spotify:track:" + "z" * 22),)}):
            fields = dict(kind="album", id=self.album_id, name="Album", tracks=(track,))
            fields.update(override)
            with self.subTest(override), patch.object(album.spotify_import, "fetch_list", return_value=ImportedList(**fields)), \
                 patch.object(album.spotify_import, "fetch_text", side_effect=AssertionError("Album ID already known")):
                with self.assertRaises(ValueError):
                    album.resolve_album(track)

    def test_youtube_resolves_album_from_selected_video_and_preserves_order(self):
        target = Track("https://music.youtube.com/watch?v=" + self.video_id)
        with patch("ytmusicapi.YTMusic") as client:
            yt = client.return_value
            yt.get_watch_playlist.return_value = {"tracks": [
                {"videoId": self.second_id, "album": {"id": "unrelated"}},
                {"videoId": self.video_id, "album": {"id": "MPREb_actual"}},
            ]}
            yt.get_album.return_value = {"title": "Album", "trackCount": 2, "tracks": [
                {"videoId": vid, "title": title, "artists": [{"name": "A"}], "duration_seconds": 135, "isAvailable": True}
                for vid, title in ((self.video_id, "First"), (self.second_id, "Second"))]}
            songs = album.resolve_album(target)
            self.assertEqual([t.youtube_id for t in songs], [self.video_id, self.second_id])
            self.assertEqual([(t.title, t.album, t.duration) for t in songs],
                             [("First", "Album", 135), ("Second", "Album", 135)])
            yt.get_album.assert_called_once_with("MPREb_actual")
            yt.get_album.return_value["trackCount"] = 5
            with self.assertRaises(ValueError):
                album.resolve_album(target)
            yt.get_watch_playlist.return_value["tracks"] = [{"videoId": self.second_id, "album": {"id": "unrelated"}}]
            yt.get_album.reset_mock()
            with self.assertRaises(ValueError):
                album.resolve_album(target)
            yt.get_album.assert_not_called()
