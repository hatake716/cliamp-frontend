"""protocol.py (純粋な部品) の試験。GTK も cliamp も要らない。"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cliamp_music import protocol as p  # noqa: E402
from cliamp_music.protocol import (  # noqa: E402
    Response,
    Source,
    Track,
    decode_response,
    encode_request,
    parse_status,
)

# 本物の cliamp 1.50.0 (TUI) が返した status (調査のときに 1 回だけ読んだもの)。
LIVE_STATUS = (
    '{"ok":true,"state":"playing","track":{"title":"t","artist":"a",'
    '"path":"https://www.youtube.com/watch?v=BoW3OHT6g0s"},"position":255.65,"duration":2915,'
    '"volume":-6.04,"index":2,"total":3,"visualizer":"Bars","shuffle":true,"repeat":"All",'
    '"mono":false,"speed":1,"eq_preset":"Custom","theme":{"name":"default"}}'
)


class TrackWire(unittest.TestCase):
    def test_round_trip_keeps_everything(self):
        """GUI が受け取った曲をそのまま送り返しても何も失われない (meta・live を含む)。"""
        track = Track(path="https://example.net/a.mp3", title="題", artist="歌手", album="盤",
                      genre="J-Pop", year=2024, track_number=3, duration=187, stream=True, live=True,
                      feed=True, unplayable=True, bookmark=True,
                      meta=(("spotify.id", "x"), ("art", "https://img/1.jpg")), queued=2,
                      played_at="2026-09-26T07:00:00Z")
        wire = track.to_wire()
        self.assertEqual(Track.from_wire(json.loads(json.dumps(wire))), track)
        self.assertEqual(wire["meta"], {"art": "https://img/1.jpg", "spotify.id": "x"})

    def test_to_wire_omits_empty(self):
        self.assertEqual(Track(path="/a.flac").to_wire(), {"path": "/a.flac"})
        self.assertEqual(Track(path="").to_wire(), {"path": ""})

    def test_from_wire_defaults_missing_fields(self):
        """omitempty で省かれた数値は 0、真偽は False。"""
        track = Track.from_wire({"path": "x"})
        self.assertEqual((track.duration, track.year, track.stream, track.live, track.meta), (0, 0, False, False, ()))

    def test_from_wire_tolerates_wrong_types(self):
        track = Track.from_wire({"path": "x", "duration": "12", "year": 2020.0, "stream": "true",
                                 "meta": {"k": 5}, "title": None})
        self.assertEqual((track.duration, track.year, track.stream, track.title), (12, 2020, True, ""))
        self.assertEqual(track.meta_get("k"), "5")

    def test_meta_order_does_not_matter(self):
        a = Track(path="x", meta=(("b", "2"), ("a", "1")))
        b = Track(path="x", meta={"a": "1", "b": "2"})
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual(a.meta_get("b"), "2")
        self.assertEqual(a.meta_get("zz", "d"), "d")


class TrackDerived(unittest.TestCase):
    def test_youtube_ids(self):
        vid = "BoW3OHT6g0s"
        for url in (
            f"https://www.youtube.com/watch?v={vid}",
            f"https://youtube.com/watch?v={vid}&list=RD{vid}",
            f"https://m.youtube.com/watch?feature=share&v={vid}",
            f"https://music.youtube.com/watch?v={vid}",
            f"https://youtu.be/{vid}?si=abc",
            f"https://www.youtube.com/shorts/{vid}",
            f"https://www.youtube.com/embed/{vid}",
            f"http://www.youtube.com/live/{vid}",
        ):
            with self.subTest(url=url):
                self.assertEqual(Track(path=url).youtube_id, vid)
        for url in ("https://www.youtube.com/watch?v=short", "https://example.com/watch?v=BoW3OHT6g0s",
                    "ytsearch1:foo", "/music/a.flac", "https://www.youtube.com/playlist?list=PL123",
                    "spotify:track:4uLU6hMCjMI75M1A2tKUQC"):
            with self.subTest(url=url):
                self.assertIsNone(Track(path=url).youtube_id)

    def test_spotify_ids(self):
        sid = "4uLU6hMCjMI75M1A2tKUQC"
        self.assertEqual(Track(path=f"spotify:track:{sid}").spotify_id, sid)
        self.assertEqual(Track(path=f"https://open.spotify.com/intl-ja/track/{sid}?si=1").spotify_id, sid)
        self.assertEqual(Track(path="https://x/a.mp3", meta={"spotify.id": sid}).spotify_id, sid)
        self.assertIsNone(Track(path="spotify:album:" + sid).spotify_id)

    def test_local_file(self):
        self.assertTrue(Track(path="/home/u/Music/a.flac").is_local_file)
        self.assertTrue(Track(path="file:///home/u/a%20b.mp3").is_local_file)
        self.assertEqual(Track(path="file:///home/u/a%20b.mp3").local_path, "/home/u/a b.mp3")
        for path in ("https://x/a.mp3", "spotify:track:x", "ytsearch1:foo", ""):
            self.assertFalse(Track(path=path).is_local_file, path)

    def test_display_title_and_subtitle(self):
        self.assertEqual(Track(path="/m/01 夜明け.flac").display_title, "01 夜明け")
        self.assertEqual(Track(path="https://s.example/live/stream.mp3").display_title, "stream.mp3")
        self.assertEqual(Track(path="x", title="T").display_title, "T")
        self.assertEqual(Track(path="https://www.youtube.com/watch?v=BoW3OHT6g0s").display_title, "YouTube の動画")
        self.assertEqual(Track(path="x", artist="A", album="B").subtitle, "A — B")
        self.assertEqual(Track(path="x", artist="A").subtitle, "A")
        self.assertEqual(Track(path="x").subtitle, "")

    def test_web_url(self):
        self.assertEqual(Track(path="https://youtu.be/BoW3OHT6g0s").web_url, "https://youtu.be/BoW3OHT6g0s")
        self.assertEqual(Track(path="spotify:track:4uLU6hMCjMI75M1A2tKUQC").web_url,
                         "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
        self.assertIsNone(Track(path="/m/a.flac").web_url)
        self.assertIsNone(Track(path="ytsearch1:foo").web_url)


class RequestResponse(unittest.TestCase):
    def test_encode_drops_none_keeps_zero(self):
        """index=0 は有効値 (cliamp 側はポインタ) なので省かない。None だけ省く。"""
        raw = encode_request("replace", index=0, source=None, tracks=[Track(path="a", title="ア")])
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        data = json.loads(raw)
        self.assertEqual(data, {"cmd": "replace", "index": 0, "tracks": [{"path": "a", "title": "ア"}]})
        self.assertIn("ア".encode(), raw)  # ensure_ascii しない

    def test_encode_source(self):
        data = json.loads(encode_request("replace", source=Source("spotify", "x", "")))
        self.assertEqual(data["source"], {"provider": "spotify", "id": "x"})

    def test_decode_kinds(self):
        self.assertEqual(decode_response(b'{"ok":true}').kind, "ok")
        r = decode_response('{"ok":false,"error":"unknown command: playlist"}')
        self.assertEqual((r.ok, r.kind), (False, "unsupported"))
        r = decode_response('{"ok":false,"error":"sign-in required","needs_auth":true}')
        self.assertEqual(r.kind, "error")
        self.assertTrue(r.needs_auth)
        self.assertIn("サインイン", r.message)
        r = decode_response(b"not json")
        self.assertEqual((r.ok, r.kind), (False, "error"))
        self.assertEqual(decode_response(b"[1]").kind, "error")
        self.assertEqual(Response.offline().kind, "offline")
        self.assertEqual(Response.timeout().message, "cliamp の応答がありません")

    def test_decode_go_html_escapes(self):
        r = decode_response(b'{"ok":true,"track":{"title":"Rock \\u0026 Roll \\u003cdemo\\u003e","path":"x"}}')
        self.assertEqual(r.data["track"]["title"], "Rock & Roll <demo>")


class ParseStatus(unittest.TestCase):
    def test_live_sample(self):
        st = parse_status(json.loads(LIVE_STATUS))
        self.assertEqual(st.state, "playing")
        self.assertEqual(st.repeat, "all")
        self.assertTrue(st.shuffle)
        self.assertAlmostEqual(st.volume, -6.04)
        self.assertEqual((st.index, st.total, st.api, st.gen), (2, 3, 0, 0))
        self.assertEqual(st.track.youtube_id, "BoW3OHT6g0s")
        self.assertEqual(st.eq, [0.0] * 10)
        self.assertEqual(st.speed, 1.0)

    def test_omitempty_defaults(self):
        """index 0、position 0、volume 0 dB は省かれて届く。"""
        st = parse_status({"ok": True, "state": "paused", "shuffle": False, "mono": False, "repeat": "One"})
        self.assertEqual((st.index, st.position, st.volume, st.total), (0, 0.0, 0.0, 0))
        self.assertEqual(st.repeat, "one")
        self.assertEqual(st.speed, 1.0)  # 0 は「省かれた」なので 1 倍とみなす
        self.assertIsNone(st.track)
        self.assertFalse(st.buffering)

    def test_extended_fields(self):
        st = parse_status({"ok": True, "state": "Playing", "api": 1, "gen": 9, "eq": [1, 2, 3],
                           "source": {"provider": "local", "id": "x", "name": "X"},
                           "stream_title": "A - B", "buffering": True,
                           "track": {"path": "https://r/x", "title": "局", "live": True, "duration": 0}})
        self.assertEqual(st.state, "playing")
        self.assertEqual(st.eq, [1.0, 2.0, 3.0] + [0.0] * 7)
        self.assertEqual(st.source, Source("local", "x", "X"))
        self.assertTrue(st.is_live)
        self.assertEqual(st.display_title, "A - B")
        self.assertEqual(st.display_subtitle, "読み込み中…")
        st.buffering = False
        self.assertEqual(st.display_subtitle, "局")

    def test_duration_falls_back_to_track(self):
        st = parse_status({"ok": True, "state": "playing", "track": {"path": "x", "duration": 200}})
        self.assertEqual(st.duration, 200.0)

    def test_unknown_values(self):
        st = parse_status({"state": "weird", "repeat": "sometimes", "eq": "x"})
        self.assertEqual((st.state, st.repeat), ("stopped", "off"))
        self.assertEqual(len(st.eq), 10)


class ParseOthers(unittest.TestCase):
    def test_playlist(self):
        pl = p.parse_playlist({"ok": True, "tracks": [{"path": "a", "queued": 1}, {"path": "b"}, {"title": "no path"}],
                               "total": 2, "queue": [0], "up_next": [1], "gen": 4})
        self.assertEqual([t.path for t in pl.tracks], ["a", "b"])
        self.assertEqual(pl.tracks[0].queued, 1)
        self.assertEqual((pl.index, pl.total, pl.queue, pl.up_next, pl.gen), (0, 2, [0], [1], 4))
        empty = p.parse_playlist({"ok": True, "index": -1})
        self.assertEqual((empty.index, empty.total, empty.queue), (-1, 0, []))

    def test_providers_and_playlists(self):
        provs = p.parse_providers({"providers": [{"key": "youtube", "name": "YouTube", "search": True,
                                                  "virtual": True}, {"name": "no key"}]})
        self.assertEqual(provs, [p.ProviderInfo("youtube", "YouTube", True, False, True)])
        lists = p.parse_playlists({"playlists": [{"id": "l:0", "name": "cliamp radio"},
                                                 {"id": "x", "name": "X", "track_count": 3, "section": "Library"}]},
                                  "radio")
        self.assertEqual(lists[0], p.PlaylistInfo("radio", "l:0", "cliamp radio"))
        self.assertEqual((lists[1].track_count, lists[1].section), (3, "Library"))

    def test_lyrics(self):
        ly = p.parse_lyrics({"lyrics": [{"t": 12.5, "text": "b"}, {"t": 1.0, "text": "a"}, {"text": "c"}],
                             "synced": True})
        self.assertEqual([line.text for line in ly.lines], ["c", "a", "b"])  # 時刻の順 (欠けた t は 0)
        self.assertEqual(ly.index_at(0.5), 0)
        self.assertEqual(ly.index_at(5.0), 1)
        self.assertEqual(ly.index_at(100), 2)
        plain = p.parse_lyrics({"lyrics": [{"text": "x"}, {"text": "y"}]})
        self.assertFalse(plain.synced)
        self.assertEqual(plain.index_at(30), -1)

    def test_devices(self):
        devices = p.parse_devices("* 既定の出力\n  HDMI 2\n\n  USB DAC ")
        self.assertEqual(devices, [("既定の出力", True), ("HDMI 2", False), ("USB DAC", False)])
        self.assertEqual(p.parse_devices(""), [])

    def test_tracks_accepts_list(self):
        self.assertEqual(p.parse_tracks([{"path": "a"}]), [Track(path="a")])
        self.assertEqual(p.parse_tracks({"ok": True}), [])


class Helpers(unittest.TestCase):
    def test_volume_matches_cliamp_mpris(self):
        """cliamp の mediactl/volume.go と同じ換算 (lin = 10^((dB-6)/20)、-30 dB 以下は 0)。"""
        self.assertEqual(p.db_to_linear(-30), 0.0)
        self.assertEqual(p.db_to_linear(-45), 0.0)
        self.assertEqual(p.db_to_linear(6), 1.0)
        self.assertEqual(p.db_to_linear(9), 1.0)
        self.assertAlmostEqual(p.db_to_linear(0), 10 ** (-6 / 20))
        self.assertAlmostEqual(p.linear_to_db(0.5), 20 * __import__("math").log10(0.5) + 6)
        self.assertAlmostEqual(p.linear_to_db(0.25), -6.04, places=2)
        self.assertEqual(p.linear_to_db(0), -30.0)
        self.assertEqual(p.linear_to_db(1), 6.0)
        self.assertEqual(p.linear_to_db(0.001), -30.0)
        for db in (-29.5, -20, -6.04, 0, 5.5):
            self.assertAlmostEqual(p.linear_to_db(p.db_to_linear(db)), db, places=6)

    def test_format_time(self):
        self.assertEqual(p.format_time(0), "0:00")
        self.assertEqual(p.format_time(187.9), "3:07")
        self.assertEqual(p.format_time(3723), "1:02:03")
        for bad in (None, -1, float("nan"), float("inf"), "x"):
            self.assertEqual(p.format_time(bad), "--:--")

    def test_format_total(self):
        self.assertEqual(p.format_total(2880), "48 分")
        self.assertEqual(p.format_total(4320), "1 時間 12 分")
        self.assertEqual(p.format_total(3600), "1 時間")
        self.assertEqual(p.format_total(20), "1 分")
        self.assertEqual(p.format_total(0), "")

    def test_mix_url_and_keys(self):
        vid = "BoW3OHT6g0s"
        self.assertEqual(p.mix_url(vid), f"https://www.youtube.com/watch?v={vid}&list=RD{vid}")
        self.assertEqual(Track(path=p.mix_url(vid)).youtube_id, vid)
        self.assertEqual(p.track_key(Track(path=f"https://music.youtube.com/watch?v={vid}")),
                         p.track_key(f"https://www.youtube.com/watch?v={vid}"))
        self.assertEqual(p.track_key(Track(path="spotify:track:4uLU6hMCjMI75M1A2tKUQC")),
                         "spotify:4uLU6hMCjMI75M1A2tKUQC")
        self.assertEqual(p.track_key(Track(path="/a.flac")), "path:/a.flac")
        self.assertEqual(p.track_key(None), "")

    def test_commands(self):
        """状態系とカタログ系は重ならず、PROTOCOL の新コマンドをすべて含む。"""
        self.assertFalse(p.STATE_COMMANDS & p.CATALOG_COMMANDS)
        new = {"capabilities", "seek_to", "playlist", "play_index", "replace", "enqueue", "queue_edit",
               "remove", "providers", "playlists", "tracks", "search", "load_provider", "lyrics", "history",
               "playlist_add", "playlist_delete", "playlist_remove_track"}
        self.assertTrue(new <= p.ALL_COMMANDS)
        self.assertTrue(p.LEGACY_COMMANDS <= p.ALL_COMMANDS)
        self.assertIn("status", p.STATE_COMMANDS)
        self.assertIn("search", p.CATALOG_COMMANDS)
        self.assertEqual(len(p.EQ_BANDS), 10)

    def test_fold_text(self):
        self.assertEqual(p.fold_text("ｼﾃｨﾗｲﾄ"), p.fold_text("シティライト"))
        self.assertEqual(p.fold_text("ＰＡＰＥＲ Moon"), "paper moon")

    def test_valid_playlist_name(self):
        self.assertTrue(p.valid_playlist_name("ドライブ 2"))
        for bad in ("", "  ", ".", "..", "a/b", "a\\b", "Recently Played", None):
            self.assertFalse(p.valid_playlist_name(bad), bad)

    def test_relative_time(self):
        now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
        cases = {
            "2026-09-26T11:59:30Z": "たった今",
            "2026-09-26T11:50:00Z": "10 分前",
            "2026-09-26T09:00:00Z": "3 時間前",
            "2026-09-25T10:00:00Z": "昨日",
            "2026-09-22T12:00:00Z": "4 日前",
            "2026-09-12T12:00:00Z": "2 週間前",
            "2026-06-20T12:00:00Z": "3 か月前",
            "2024-09-01T12:00:00Z": "2 年前",
            "2026-09-26T18:00:00+09:00": "3 時間前",
            "2026-09-26T08:59:59.123456789Z": "3 時間前",
            "2026-09-27T12:00:00Z": "たった今",
        }
        for iso, expected in cases.items():
            with self.subTest(iso=iso):
                self.assertEqual(p.relative_time(iso, now), expected)
        self.assertEqual(p.relative_time("garbage", now), "")
        self.assertEqual(p.relative_time("", now), "")
        self.assertEqual(p.relative_time("2026-09-26T11:00:00Z", now.timestamp()), "1 時間前")


if __name__ == "__main__":
    unittest.main()
