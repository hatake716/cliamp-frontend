"""catalog.py の試験。偽の cliamp のカタログ系コマンドを使う。"""

from __future__ import annotations

import sys
import unittest
import unittest.mock
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
        # 素の cliamp: 組み込みの l:0 と Radio Browser のカタログ (c:N)
        self.assertEqual([p.id for p in lists], ["l:0", "c:0", "c:1", "c:2"])
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
        # l:0 (cliamp radio) は M3U の中の 15 本の配信に展開される
        station = self.get(self.catalog.tracks, "radio", "l:0")
        self.assertEqual(len(station), 15)
        self.assertTrue(all(t.live and t.stream for t in station))
        self.assertEqual(station[0].title, "Lofi")
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
        # TOML には meta が残らず、stream は path が URL かで決め直される
        self.assertEqual(added, [Track(path=track.path, title="足す曲", stream=True)])
        removed = self.get(self.catalog.playlist_remove_track, "新しいリスト", 0)
        self.assertTrue(removed.ok)
        # 最後の曲を外すとプレイリストのファイルごと消える (external/local の RemoveTrack)
        gone = self.get(self.catalog.tracks, "local", "新しいリスト")
        self.assertIsInstance(gone, Response)
        self.assertIn("no such file or directory", gone.message)
        self.assertNotIn("新しいリスト", [p.name for p in self.get(self.catalog.local_playlists)])

    def test_invalid_name(self):
        result = self.get(self.catalog.playlist_add, "a/b", [Track(path="x")])
        self.assertFalse(result.ok)

    def test_load(self):
        result = self.get(self.catalog.load, "radio", "l:0", 2, "cliamp ラジオ")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.get("total"), 15)
        self.assertEqual(self.fake.source, {"provider": "radio", "id": "l:0", "name": "cliamp ラジオ"})
        self.assertEqual(self.fake.pl.current()[0]["title"], "Synthwave")
        bad = self.get(self.catalog.load, "radio", "l:0", 99, "cliamp ラジオ")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error, "index out of range")


class CacheLimits(CatalogTest):
    """覚えている結果は期限切れを捨て、検索は新しい 200 件までにする。"""

    def test_expired_entries_are_swept_on_insert(self):
        from cliamp_music import catalog as catalog_mod

        for i in range(300):
            self.catalog._cache[("lyrics", "a", f"t{i}")] = (0.0, None)  # 期限切れ
        self.get(self.catalog.providers)
        self.assertLess(len(self.catalog._cache), 10)
        self.assertEqual(catalog_mod.CACHE_CAPS["search"], 200)

    def test_search_entries_are_capped(self):
        from cliamp_music import catalog as catalog_mod

        with unittest.mock.patch.dict(catalog_mod.CACHE_CAPS, {"search": 5}):
            for i in range(8):
                self.get(self.catalog.search, "youtube", f"query {i}")
        searches = [k for k in self.catalog._cache if k[0] == "search"]
        self.assertEqual(len(searches), 5)
        self.assertIn(("search", "youtube", "query 7", 25), searches)
        self.assertNotIn(("search", "youtube", "query 0", 25), searches)

    def test_superseded_search_is_resent_when_asked_again(self):
        """「abc」→「abcd」→「abc」: 捨てられた古い語にまた頼まれたら送り直す。"""
        self.fake.delays["lyrics"] = 0.4
        for i in range(4):
            self.catalog.lyrics("x", f"t{i}", lambda r: None)  # worker をふさぐ
        got = []
        self.catalog.search("youtube", "abc", got.append, lane="page")
        self.catalog.search("youtube", "abcd", lambda r: None, lane="page")
        self.catalog.search("youtube", "abc", got.append, lane="page")
        self.assertTrue(run_loop(lambda: len(got) == 2, timeout=5))
        self.assertTrue(all(isinstance(r, list) and r for r in got), got)


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
