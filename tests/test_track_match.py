"""サービス間の追加で、ユーザーが見て選ぶ候補の契約。"""

import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from gi.repository import GLib

from cliamp_music.protocol import Response, Track
from cliamp_music.track_match import search_candidates


class CandidateSearchTest(unittest.TestCase):
    def resolve(self, ctx, track, provider):
        replies = []
        main_thread = threading.get_ident()

        def report(candidates, error):
            replies.append((candidates, error, threading.get_ident()))

        search_candidates(ctx, track, provider, report)
        context = GLib.MainContext.default()
        end = time.monotonic() + 3
        while not replies and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.003)
        while context.pending():
            context.iteration(False)
        self.assertEqual(len(replies), 1)
        candidates, error, thread_id = replies[0]
        self.assertEqual(thread_id, main_thread)
        return candidates, error

    def test_spotify_keeps_versions_and_discards_duplicates_before_limiting(self):
        sid = lambda n: f"{n:022}"
        def track(n, title, **extra):
            return Track("spotify:track:" + sid(n), title, "Artist", "Album", **extra)
        bridge = Track("ytsearch1:Artist Song", "Song", "Artist", "Alternative", duration=251,
                       meta={"spotify.id": sid(2), "spotify.bridge": "youtube"})
        available = [track(1, "Song"), bridge, *[track(n, "Song") for n in range(3, 8)]]
        rows = [track(20, "Radio", live=True), track(21, "Blocked", unplayable=True),
                Track("/file", "No ID"), available[0], available[0], *available[1:]]
        catalog = mock.Mock()
        catalog.search.side_effect = lambda provider, query, cb, limit: cb(rows)
        result, error = self.resolve(SimpleNamespace(catalog=catalog), Track("/in", "Song", "Artist"), "spotify")
        self.assertEqual((result, error), (available[:5], ""))
        self.assertEqual(catalog.search.call_args.args[:2], ("spotify", "Artist Song"))
        self.assertEqual(result[1].duration, 251)

    def test_youtube_album_artist_duration_and_exact_recording_ids(self):
        ytmusicapi = mock.MagicMock()
        requests = mock.MagicMock()
        first = "abcdefghij1"
        second = "abcdefghij2"
        def row(identifier, **extra):
            return {"videoId": identifier, "title": "歌", "artists": [{"name": "A"}, {"name": "B"}],
                    "album": {"name": "盤"}, "duration": "3:07", **extra}
        mocked_search = ytmusicapi.YTMusic.return_value.search
        search_threads = []
        def search(*_args, **_kwargs):
            search_threads.append(threading.get_ident())
            return [row("!badvideo!!"), row("abcdefghij0", isAvailable=False), row(first), row(first),
                    row(second, title="歌 (ライブ)", duration_seconds=218),
                    *[row(f"ABCDEFGHI{i:02}", artists=[]) for i in range(10)]]
        mocked_search.side_effect = search
        with mock.patch.dict(sys.modules, {"ytmusicapi": ytmusicapi, "requests": requests}):
            rows, error = self.resolve(SimpleNamespace(), Track("/file", "歌", "A"), "youtube")
        self.assertEqual(error, "")
        self.assertEqual([t.youtube_id for t in rows], [first, second, "ABCDEFGHI00", "ABCDEFGHI01", "ABCDEFGHI02"])
        self.assertEqual((rows[0].title, rows[0].artist, rows[0].album, rows[0].duration), ("歌", "A, B", "盤", 187))
        self.assertEqual((rows[1].title, rows[1].duration), ("歌 (ライブ)", 218))
        self.assertTrue(rows[0].path.startswith("https://music.youtube.com/"))
        mocked_search.assert_called_once_with("A 歌", filter="songs", limit=10)
        self.assertNotEqual(search_threads, [threading.get_ident()])
        requests.Session.return_value.__exit__.assert_called_once()

    def test_youtube_source_cleans_search_terms_and_preserves_recording_version(self):
        catalog = mock.Mock()
        catalog.search.side_effect = lambda _, query, cb, limit: cb([])
        ctx = SimpleNamespace(catalog=catalog)
        title = "A - Blue (Night Remix) (Official Music Video)"
        for path, expected in [
            ("https://music.youtube.com/watch?v=abcdefghij1", "A Blue (Night Remix)"),
            ("/local/recording", "A - Topic " + title),
        ]:
            with self.subTest(path=path):
                source = Track(path, title, "A - Topic")
                self.resolve(ctx, source, "spotify")
                self.assertEqual(catalog.search.call_args.args[1], expected)
                self.assertEqual((source.artist, source.title), ("A - Topic", title))

    def test_no_title_no_lookup_and_no_results(self):
        catalog = mock.Mock()
        ctx = SimpleNamespace(catalog=catalog)
        rows, error = self.resolve(ctx, Track("/no-title"), "spotify")
        self.assertFalse(rows)
        self.assertTrue(error)
        catalog.search.assert_not_called()
        catalog.search.side_effect = lambda _, query, cb, limit: cb([])
        self.assertEqual(self.resolve(ctx, Track("/track", "song"), "spotify"), ([], ""))

    def test_search_failure_is_not_an_empty_success(self):
        catalog = mock.Mock()
        catalog.search.side_effect = lambda _, query, cb, limit: cb(Response(False, {}, "失敗", "error"))
        self.assertEqual(self.resolve(SimpleNamespace(catalog=catalog), Track("/track", "song"), "spotify"),
                         ([], "失敗"))
        broken = mock.MagicMock()
        broken.YTMusic.side_effect = TimeoutError("sensitive URL")
        with mock.patch.dict(sys.modules, {"ytmusicapi": broken, "requests": mock.MagicMock()}):
            rows, error = self.resolve(SimpleNamespace(), Track("/track", "song"), "youtube")
        self.assertFalse(rows)
        self.assertIn("再試行", error)
        self.assertNotIn("sensitive", error)
