"""catalog.py の試験。偽の cliamp のカタログ系コマンドを使う。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, run_loop, temp_socket_path  # noqa: E402

try:
    from cliamp_music.catalog import Catalog
    from cliamp_music.client import CliampClient
    from cliamp_music.protocol import RECENTLY_PLAYED, Lyrics, Response, Track, mix_url

    HAVE_GI = True
except ImportError:  # pragma: no cover
    HAVE_GI = False


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class CatalogTest(unittest.TestCase):
    legacy = False
    spotify_needs_auth = False

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path, legacy=self.legacy,
                               spotify_needs_auth=self.spotify_needs_auth).start()
        self.catalog = Catalog(CliampClient(self.path))

    def tearDown(self):
        self.fake.stop()

    def get(self, method, *args, timeout=5.0, **kwargs):
        box = []
        method(*args, box.append, **kwargs)
        self.assertTrue(run_loop(lambda: box, timeout=timeout), f"{method.__name__} が返りません")
        return box[0]

    def count(self, cmd):
        return len(self.fake.requests_for(cmd))


class Reading(CatalogTest):
    def test_providers(self):
        providers = self.get(self.catalog.providers)
        keys = [p.key for p in providers]
        self.assertEqual(keys, ["radio", "local", "spotify", "youtube"])
        youtube = providers[-1]
        self.assertTrue(youtube.virtual and youtube.search and not youtube.playlists)

    def test_playlists_are_cached(self):
        lists = self.get(self.catalog.playlists, "radio")
        self.assertEqual([p.id for p in lists], ["l:0", "l:1", "l:2", "l:3", "f:0"])
        self.assertEqual(lists[0].provider, "radio")
        self.get(self.catalog.playlists, "radio")
        self.assertEqual(self.count("playlists"), 1)
        self.get(self.catalog.playlists, "radio", force=True)
        self.assertEqual(self.count("playlists"), 2)

    def test_local_playlists_include_recently_played(self):
        lists = self.get(self.catalog.playlists, "local")
        self.assertEqual([p.id for p in lists], [RECENTLY_PLAYED, "ドライブ", "Focus"])
        writable = self.get(self.catalog.local_playlists)
        self.assertEqual([p.id for p in writable], ["ドライブ", "Focus"])

    def test_tracks(self):
        station = self.get(self.catalog.tracks, "radio", "l:0")
        self.assertEqual(len(station), 1)
        self.assertTrue(station[0].live and station[0].stream)
        local = self.get(self.catalog.tracks, "local", "ドライブ")
        self.assertEqual(len(local), 7)
        mix = self.get(self.catalog.tracks, "url", mix_url("abcdefghijk"))
        self.assertEqual(mix[0].youtube_id, "abcdefghijk")
        self.assertGreater(len(mix), 10)

    def test_search_cache_and_dedupe(self):
        """同じ語の検索は 1 回だけ送る (重なった要求も、後からの要求も)。全角半角は同じ語。"""
        self.fake.delays["search"] = 0.2
        first, second = [], []
        self.catalog.search("youtube", "Lo-fi", first.append)
        self.catalog.search("youtube", "Lo-fi", second.append)
        self.assertTrue(run_loop(lambda: first and second))
        self.assertEqual(first[0], second[0])
        self.assertEqual(len(first[0]), 25)
        again = self.get(self.catalog.search, "youtube", "ＬＯ－ＦＩ")
        self.assertEqual(again, first[0])
        self.assertEqual(self.count("search"), 1)
        # 渡した並びを書き換えてもキャッシュは壊れない
        first[0].clear()
        self.assertEqual(len(self.get(self.catalog.search, "youtube", "Lo-fi")), 25)
        self.assertEqual(len(self.get(self.catalog.search, "youtube", "Lo-fi", limit=5)), 5)

    def test_search_empty_and_failure(self):
        self.assertEqual(self.get(self.catalog.search, "youtube", "  "), [])
        self.assertEqual(self.get(self.catalog.search, "youtube", "noresults"), [])
        failed = self.get(self.catalog.search, "youtube", "__fail__")
        self.assertIsInstance(failed, Response)
        self.assertEqual(failed.kind, "error")
        self.get(self.catalog.search, "youtube", "__fail__")
        self.assertEqual(self.count("search"), 3)  # 失敗は覚えない (noresults と __fail__ ×2)

    def test_lyrics(self):
        synced = self.get(self.catalog.lyrics, "青い灯台", "夜明けのバス停")
        self.assertIsInstance(synced, Lyrics)
        self.assertTrue(synced.synced)
        self.assertGreater(synced.lines[1].t, synced.lines[0].t)
        plain = self.get(self.catalog.lyrics, "Aurora Lane", "Paper Moon Drive")
        self.assertFalse(plain.synced)
        self.assertTrue(all(line.t == 0 for line in plain.lines))
        self.assertIsNone(self.get(self.catalog.lyrics, "真夜中ポスト", "シティライト・ブルース"))
        self.assertIsNone(self.get(self.catalog.lyrics, "真夜中ポスト", "シティライト・ブルース"))
        self.assertEqual(self.count("lyrics"), 3)  # 「見つからない」も覚える

    def test_history(self):
        history = self.get(self.catalog.history)
        self.assertEqual(len(history), 8)
        self.assertTrue(all(t.played_at for t in history))
        self.get(self.catalog.history)
        self.assertEqual(self.count("history"), 2)  # 履歴は覚えない


class Writing(CatalogTest):
    def test_playlist_add_invalidates(self):
        self.get(self.catalog.local_playlists)
        track = Track(path="https://www.youtube.com/watch?v=abcdefghijk", title="足す曲", meta={"art": "x"})
        result = self.get(self.catalog.playlist_add, "新しいリスト", [track])
        self.assertTrue(result.ok)
        names = [p.name for p in self.get(self.catalog.local_playlists)]
        self.assertIn("新しいリスト", names)
        added = self.get(self.catalog.tracks, "local", "新しいリスト")
        self.assertEqual(added, [track])
        removed = self.get(self.catalog.playlist_remove_track, "新しいリスト", 0)
        self.assertTrue(removed.ok)
        self.assertEqual(self.get(self.catalog.tracks, "local", "新しいリスト"), [])
        self.assertTrue(self.get(self.catalog.playlist_delete, "新しいリスト").ok)
        self.assertNotIn("新しいリスト", [p.name for p in self.get(self.catalog.local_playlists)])

    def test_invalid_name(self):
        result = self.get(self.catalog.playlist_add, "a/b", [Track(path="x")])
        self.assertFalse(result.ok)

    def test_load(self):
        result = self.get(self.catalog.load, "radio", "l:1", 0, "Harbor Jazz FM")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.fake.source, {"provider": "radio", "id": "l:1", "name": "Harbor Jazz FM"})
        self.assertEqual(self.fake.pl.tracks[0]["path"], "https://stream.example.net/harbor-jazz.mp3")


class LibrarySearch(CatalogTest):
    def test_matches_nfkc_and_case(self):
        results = self.get(self.catalog.search_library, "ｼﾃｨﾗｲﾄ")
        self.assertEqual([t.title for t in results], ["シティライト・ブルース"])
        results = self.get(self.catalog.search_library, "PAPER")
        self.assertEqual(results[0].title, "Paper Moon Drive")

    def test_ranking_and_dedupe(self):
        """履歴とプレイリストの両方にある曲は 1 つ。曲名の先頭一致が先。"""
        results = self.get(self.catalog.search_library, "tape garden")
        keys = [t.path for t in results]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all("Tape Garden" == t.artist for t in results))
        by_title = self.get(self.catalog.search_library, "Lo-fi")
        self.assertEqual(by_title[0].title, "Lo-fi Study Session")
        self.assertTrue(by_title[0].played_at)  # 履歴の曲 (played_at 付き) が先に来る

    def test_multiple_terms_and_empty(self):
        self.assertEqual(self.get(self.catalog.search_library, "   "), [])
        results = self.get(self.catalog.search_library, "velvet spin")
        self.assertTrue(results)
        self.assertTrue(all(t.artist == "The Velvet Hours" for t in results))
        self.assertEqual(self.get(self.catalog.search_library, "存在しない曲名"), [])


class NeedsAuth(CatalogTest):
    spotify_needs_auth = True

    def test_spotify_needs_auth(self):
        result = self.get(self.catalog.playlists, "spotify")
        self.assertIsInstance(result, Response)
        self.assertTrue(result.needs_auth)
        self.assertIn("サインイン", result.message)


class LegacyCatalog(CatalogTest):
    legacy = True

    def test_unsupported_passthrough(self):
        result = self.get(self.catalog.providers)
        self.assertIsInstance(result, Response)
        self.assertEqual(result.kind, "unsupported")
        library = self.get(self.catalog.search_library, "x")
        self.assertIsInstance(library, Response)
        self.assertEqual(library.kind, "unsupported")
        self.assertIsNone(self.get(self.catalog.lyrics, "a", "b"))


class Offline(unittest.TestCase):
    @unittest.skipUnless(HAVE_GI, "PyGObject がありません")
    def test_offline(self):
        catalog = Catalog(CliampClient(temp_socket_path()))
        box = []
        catalog.playlists("local", box.append)
        self.assertTrue(run_loop(lambda: box))
        self.assertEqual(box[0].kind, "offline")


if __name__ == "__main__":
    unittest.main()
