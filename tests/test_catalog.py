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
    from cliamp_music.protocol import RECENTLY_PLAYED, Lyrics, Response, Track, is_youtube_bridge, mix_url

    HAVE_GI = True
except ImportError:  # pragma: no cover
    HAVE_GI = False


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class CatalogTest(unittest.TestCase):
    legacy = False
    spotify_needs_auth = False
    spotify_web_only = False

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path, legacy=self.legacy, spotify_needs_auth=self.spotify_needs_auth,
                               spotify_web_only=self.spotify_web_only).start()
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


class WebOnlySpotify(CatalogTest):
    """Spotify の接続が Web API だけの cliamp (曲は YouTube で探して鳴らす)。"""

    spotify_web_only = True

    def test_providers_report_playback_after_the_session(self):
        """playback は cliamp が Spotify のセッションを作った後の答えにしか載らない (偽も本物と同じ)。
        カタログは spotify のカタログ系が初めて成功した後で providers を 1 度だけ取り直し、
        覚えている答えを置き換えて聞き手に渡す。"""
        heard = []
        self.catalog.add_providers_listener(heard.append)
        first = {p.key: p for p in self.get(self.catalog.providers)}
        self.assertEqual(first["spotify"].playback, "", "セッションの前の答えには載らない")
        self.assertEqual(len(heard), 1)
        self.get(self.catalog.playlists, "spotify")
        self.assertTrue(run_loop(lambda: len(heard) == 2, timeout=5), "取り直しません")
        fresh = {p.key: p for p in heard[-1]}
        self.assertEqual(fresh["spotify"].playback, "youtube")
        self.assertTrue(fresh["spotify"].plays_via_youtube)
        self.assertEqual(fresh["local"].playback, "")
        # 覚えている答えは取り直したもの (もう送らない)
        cached = {p.key: p for p in self.get(self.catalog.providers)}
        self.assertEqual(cached["spotify"].playback, "youtube")
        self.assertEqual(self.count("providers"), 2)
        # 2 度目からの成功では取り直さない
        self.get(self.catalog.tracks, "spotify", "YOUR MUSIC")
        run_loop(lambda: False, 0.2)
        self.assertEqual(self.count("providers"), 2)
        # 繋ぎ直した (再起動した) cliamp: 捨てた後はまた初めての成功の後に 1 度
        self.fake._spotify_session = False
        self.catalog.invalidate()
        self.assertEqual(self.get(self.catalog.providers)[2].playback, "")
        self.get(self.catalog.tracks, "spotify", "YOUR MUSIC")
        self.assertTrue(run_loop(lambda: self.count("providers") == 4, timeout=5))
        self.assertTrue(run_loop(lambda: len(heard) == 4, timeout=5))
        self.assertEqual({p.key: p.playback for p in heard[-1]}["spotify"], "youtube")

    def test_tracks_are_bridged(self):
        tracks = self.get(self.catalog.tracks, "spotify", "YOUR MUSIC")
        self.assertTrue(tracks)
        for track in tracks:
            self.assertTrue(is_youtube_bridge(track))
            self.assertTrue(track.path.startswith("ytsearch1:"))
            self.assertTrue(track.path.endswith(" " + track.title))
            self.assertIsNotNone(track.spotify_id)
            self.assertIsNone(track.youtube_id)
            self.assertFalse(track.unplayable)

    def test_search_is_explained_in_japanese(self):
        result = self.get(self.catalog.search, "spotify", "夜")
        self.assertIsInstance(result, Response)
        self.assertIn("search blocked", result.error)
        self.assertTrue(result.message.startswith("Spotify では検索できません"), result.message)
        # 失敗は覚えない (次もまた頼む)
        self.get(self.catalog.search, "spotify", "夜")
        self.assertEqual(self.count("search"), 2)
        # YouTube の検索はふつうに使える
        self.assertTrue(self.get(self.catalog.search, "youtube", "夜"))

    def test_rate_limit_is_explained_in_japanese(self):
        self.fake.spotify_error = "spotify: rate limited by Spotify; retry after 24h0m0s"
        want = "Spotify から回数の制限を受けています。24 時間ほど待ってから、もう一度試してください"
        for method, args in ((self.catalog.playlists, ("spotify",)), (self.catalog.tracks, ("spotify", "YOUR MUSIC")),
                             (self.catalog.search, ("spotify", "朝"))):
            with self.subTest(method=method.__name__):
                result = self.get(method, *args)
                self.assertIsInstance(result, Response)
                self.assertEqual(result.message, want)


class _ManualClient:
    """答えを試験が手で返す client (カタログの要求の重なりを決まった順で起こす)。"""

    def __init__(self):
        self.sent: list[tuple[str, dict, object]] = []

    def request(self, cmd, callback=None, *, lane=None, **fields):
        self.sent.append((cmd, fields, callback))

    def answer(self, index: int, **data) -> None:
        self.sent[index][2](Response(True, data, "", "ok"))

    def count(self, cmd: str) -> int:
        return sum(1 for sent in self.sent if sent[0] == cmd)


def _providers(playback: str = "") -> list[dict]:
    spotify = {"key": "spotify", "name": "Spotify", "search": True, "playlists": True, "virtual": False}
    if playback:
        spotify["playback"] = playback
    return [{"key": "local", "name": "Local", "search": True, "playlists": True, "virtual": False}, spotify]


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class ProvidersAfterSession(unittest.TestCase):
    """spotify のカタログ系の初めての成功の後の providers の取り直し (要求の重なり)。"""

    def setUp(self):
        self.client = _ManualClient()
        self.catalog = Catalog(self.client)
        self.heard: list = []
        self.catalog.add_providers_listener(self.heard.append)

    def test_answer_sent_before_the_session_is_not_kept(self):
        """詳細ページは providers と tracks を同時に頼む。tracks が先に成功したとき、まだ届いて
        いない providers の答えはセッションの前のものかもしれないので覚えず、届いてから取り直す。"""
        got = []
        self.catalog.providers(got.append)
        self.catalog.tracks("spotify", "YOUR MUSIC", lambda _r: None)
        self.client.answer(1, tracks=[])
        self.assertEqual(self.client.count("providers"), 1, "送ってある答えを待つ")
        self.client.answer(0, providers=_providers())
        self.assertEqual(len(got), 1, "頼んだ人には届いた答えを渡す")
        self.assertEqual(self.heard, [], "古いかもしれない答えは聞き手に渡さない")
        self.assertEqual(self.client.count("providers"), 2, "届いてから取り直す")
        self.client.answer(2, providers=_providers("youtube"))
        self.assertEqual(len(self.heard), 1)
        self.assertEqual(self.heard[0][1].playback, "youtube")
        cached = []
        self.catalog.providers(cached.append)
        self.assertTrue(run_loop(lambda: cached, timeout=2))
        self.assertEqual(cached[0][1].playback, "youtube", "取り直した答えを覚える")
        self.assertEqual(self.client.count("providers"), 2)

    def test_premium_answer_is_checked_once(self):
        """Premium (playback の無い答え) でも、初めての成功の後に 1 度だけ確かめる。"""
        self.catalog.providers(lambda _r: None)
        self.client.answer(0, providers=_providers())
        self.catalog.playlists("spotify", lambda _r: None)
        self.client.answer(1, playlists=[])
        self.assertEqual(self.client.count("providers"), 2)
        self.client.answer(2, providers=_providers())
        self.catalog.search("spotify", "夜", lambda _r: None)
        self.client.answer(3, tracks=[])
        self.catalog.tracks("spotify", "x", lambda _r: None)
        self.client.answer(4, tracks=[])
        self.assertEqual(self.client.count("providers"), 2, "2 度目からの成功では取り直さない")

    def test_known_playback_and_failures_do_not_refetch(self):
        # もう playback の載った答えを覚えていれば取り直さない (GUI より前にセッションがあった)
        self.catalog.providers(lambda _r: None)
        self.client.answer(0, providers=_providers("youtube"))
        self.catalog.playlists("spotify", lambda _r: None)
        self.client.answer(1, playlists=[])
        self.assertEqual(self.client.count("providers"), 1)
        # 失敗 (サインインが要る) はセッションができた印ではない。ほかのプロバイダーも見ない
        catalog = Catalog(_ManualClient())
        catalog.playlists("spotify", lambda _r: None)
        catalog.client.sent[0][2](Response(False, {"needs_auth": True}, "sign-in required", "error"))
        catalog.playlists("navidrome", lambda _r: None)
        catalog.client.answer(1, playlists=[])
        catalog.playlists("local", lambda _r: None)
        catalog.client.answer(2, playlists=[])
        self.assertEqual(catalog.client.count("providers"), 0)
        catalog.playlists("spotify", lambda _r: None, force=True)
        catalog.client.answer(3, playlists=[])
        self.assertEqual(catalog.client.count("providers"), 1, "サインインの後の初めての成功で取り直す")


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
