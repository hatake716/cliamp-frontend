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


# YouTube で探して鳴らす Spotify の曲 (Web API だけの接続の cliamp が返す形)。
BRIDGE_SID = "4uLU6hMCjMI75M1A2tKUQC"
BRIDGED = Track(path="ytsearch1:Aurora Lane Kite Theory 夜明けのバス停 & <demo>", title="夜明けのバス停 & <demo>",
                artist="Aurora Lane, Kite Theory", album="港町", year=2021, track_number=4, duration=201,
                meta={"spotify.id": BRIDGE_SID, "spotify.bridge": "youtube"})


class YouTubeBridge(unittest.TestCase):
    def test_bridged_track_is_a_spotify_track_played_by_search(self):
        self.assertTrue(p.is_youtube_bridge(BRIDGED))
        self.assertIsNone(BRIDGED.youtube_id, "検索式には動画 ID が無い")
        self.assertEqual(BRIDGED.spotify_id, BRIDGE_SID)
        self.assertFalse(BRIDGED.is_local_file)
        self.assertEqual(BRIDGED.display_title, "夜明けのバス停 & <demo>")
        # リンクは探した動画ではなく Spotify の曲の頁
        self.assertEqual(BRIDGED.web_url, f"https://open.spotify.com/track/{BRIDGE_SID}")
        self.assertEqual(p.track_key(BRIDGED), f"spotify:{BRIDGE_SID}")
        # 往復で meta が失われない (replace で送り返しても Spotify の曲のまま)
        self.assertEqual(Track.from_wire(json.loads(json.dumps(BRIDGED.to_wire()))), BRIDGED)

    def test_search_paths_have_no_youtube_id(self):
        for path in ("ytsearch1:米津玄師 Lemon", "ytsearch:lofi", "ytsearch10:a b", "scsearch1:x"):
            with self.subTest(path=path):
                self.assertIsNone(Track(path=path).youtube_id)
                self.assertIsNone(p.youtube_id_of(path))

    def test_bridge_prefers_spotify_even_when_path_is_a_video(self):
        """cliamp が探した動画の URL を path に入れても、Spotify の曲として扱う。"""
        track = Track(path="https://www.youtube.com/watch?v=BoW3OHT6g0s",
                      meta={"spotify.id": BRIDGE_SID, "spotify.bridge": "youtube"})
        self.assertEqual(track.web_url, f"https://open.spotify.com/track/{BRIDGE_SID}")
        self.assertEqual(p.track_key(track), f"spotify:{BRIDGE_SID}")
        # 印の無い YouTube の曲は今までどおり動画の URL と動画 ID
        plain = Track(path="https://www.youtube.com/watch?v=BoW3OHT6g0s")
        self.assertEqual(plain.web_url, plain.path)
        self.assertEqual(p.track_key(plain), "youtube:BoW3OHT6g0s")

    def test_not_a_bridge(self):
        self.assertFalse(p.is_youtube_bridge(None))
        self.assertFalse(p.is_youtube_bridge(Track(path=f"spotify:track:{BRIDGE_SID}")))
        self.assertFalse(p.is_youtube_bridge(Track(path="ytsearch1:x", meta={"spotify.bridge": "soundcloud"})))
        self.assertTrue(p.is_youtube_bridge(Track(path="ytsearch1:x", meta={"spotify.bridge": " YouTube "})))

    def test_malformed_meta_id_is_not_used_in_urls(self):
        for bad in ("x", "", "../../evil?a=1&b=2aaaaaaaaaa", BRIDGE_SID + "/x"):
            with self.subTest(bad=bad):
                track = Track(path="ytsearch1:foo", meta={"spotify.id": bad, "spotify.bridge": "youtube"})
                self.assertIsNone(track.spotify_id)
                self.assertIsNone(track.web_url)
                self.assertEqual(p.track_key(track), "path:ytsearch1:foo")


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
        self.assertEqual(provs[0].playback, "", "playback は省かれていれば空 (自分で鳴らす)")
        self.assertFalse(provs[0].plays_via_youtube)
        web_only = p.parse_providers({"providers": [{"key": "spotify", "name": "Spotify", "search": True,
                                                     "playlists": True, "virtual": False, "playback": "youtube"}]})
        self.assertEqual(web_only[0].playback, "youtube")
        self.assertTrue(web_only[0].plays_via_youtube)
        self.assertEqual(p.parse_providers([{"key": "spotify", "playback": " YouTube "}])[0].playback, "youtube")
        self.assertEqual(p.parse_providers([{"key": "spotify", "playback": 3}])[0].playback, "3")
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

    def test_path_raw_round_trip_and_local_path(self):
        """UTF-8 でない名前のファイル: path_raw をそのまま送り返し、ファイルはそのバイト列で開く。"""
        import base64
        import os

        raw = base64.b64encode(b"/music/\x83e\x83X.flac").decode()
        track = p.Track.from_wire({"path": "/music/\ufffde\ufffdX.flac", "path_raw": raw, "title": "t"})
        self.assertEqual(track.to_wire()["path_raw"], raw)
        self.assertEqual(os.fsencode(track.local_path), b"/music/\x83e\x83X.flac")
        self.assertNotIn("path_raw", p.Track(path="/music/a.flac").to_wire())
        self.assertEqual(p.Track(path="/m/a.flac", path_raw="!!").local_path, "/m/a.flac")  # 壊れていれば path

    def test_stale_message(self):
        response = p.Response(False, {"ok": False, "error": "stale"}, "stale", "error")
        self.assertIn("もう一度", response.message)

    def test_device_list_prefers_descriptions(self):
        data = {"device": "* alsa_output.pci-0000_0c_00.4.analog-stereo\n  bluez_output.AC_80_0A_12_34_56.1",
                "devices": [{"name": "alsa_output.pci-0000_0c_00.4.analog-stereo",
                             "description": "Starship/Matisse HD Audio Controller Analog Stereo", "active": True},
                            {"name": "bluez_output.AC_80_0A_12_34_56.1", "description": "WH-1000XM5"}]}
        devices = p.parse_device_list(data)
        self.assertEqual([d.label for d in devices], ["Starship/Matisse HD Audio Controller Analog Stereo",
                                                      "WH-1000XM5"])
        self.assertEqual(devices[1].name, "bluez_output.AC_80_0A_12_34_56.1")
        self.assertEqual([d.active for d in devices], [True, False])

    def test_device_list_without_descriptions(self):
        devices = p.parse_device_list({"device": "* alsa_output.pci-0000_0c_00.4.analog-stereo\n"
                                                 "  alsa_output.pci-0000_03_00.1.hdmi-stereo-extra1\n"
                                                 "  alsa_output.usb-Focusrite_Scarlett_2i2-00.analog-stereo\n"
                                                 "  my_custom_sink"})
        self.assertEqual([d.label for d in devices],
                         ["アナログ出力", "HDMI / DisplayPort 2", "USB オーディオ (アナログ出力)", "my_custom_sink"])
        self.assertEqual(devices[0].name, "alsa_output.pci-0000_0c_00.4.analog-stereo")
        self.assertTrue(devices[0].active)
        # 見出しが重なれば sink 名を添えて見分ける
        twins = p.parse_device_list({"device": "* alsa_output.a.analog-stereo\n  alsa_output.b.analog-stereo"})
        self.assertEqual(twins[0].label, "アナログ出力 — alsa_output.a.analog-stereo")

    def test_devices(self):
        devices = p.parse_devices("* 既定の出力\n  HDMI 2\n\n  USB DAC ")
        self.assertEqual(devices, [("既定の出力", True), ("HDMI 2", False), ("USB DAC", False)])
        self.assertEqual(p.parse_devices(""), [])

    def test_tracks_accepts_list(self):
        self.assertEqual(p.parse_tracks([{"path": "a"}]), [Track(path="a")])
        self.assertEqual(p.parse_tracks({"ok": True}), [])


class Helpers(unittest.TestCase):
    def test_volume_slider_mapping_is_linear_in_db(self):
        """再生バーとフルスクリーンの音量のつまみは同じ換算 (-6 dB は 2/3 の位置)。"""
        self.assertAlmostEqual(p.volume_fraction(-6.0), 24 / 36)
        self.assertAlmostEqual(p.volume_fraction(-12.0), 0.5)
        self.assertEqual(p.volume_fraction(-40), 0.0)
        self.assertEqual(p.fraction_to_volume(0.5), -12.0)
        for db in (-30, -18, -6, 0, 6):
            self.assertAlmostEqual(p.fraction_to_volume(p.volume_fraction(db)), db)

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


# 本物の yt-dlp が年齢確認の要る動画で出した stderr (cliamp が "yt-dlp: " を付けて包む)。
AGE_GATE = ("yt-dlp: ERROR: [youtube] x8VYWazR5mE: Sign in to confirm your age. This video may be "
            "inappropriate for some users. Use --cookies-from-browser or --cookies for the authentication. "
            "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to "
            "manually pass cookies. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-"
            "youtube-cookies  for tips on effectively exporting YouTube cookies")


SEARCH_BLOCKED = (
    "spotify: search blocked \u2014 your client_id is too new. Spotify's Nov 27 2024 change blocks /v1/search "
    "for apps in Development Mode (the rest of cliamp still works on your app). Remove client_id from "
    "[spotify] in config.toml to use the built-in fallback for search, or apply for Extended Quota Mode")


class SpotifyRefusals(unittest.TestCase):
    """Spotify の Web API の断り (回数の制限・検索の封鎖・鳴らせない曲) の言い直し。"""

    def test_parse_go_duration(self):
        for text, want in (("24h0m0s", 86400.0), ("1h30m0s", 5400.0), ("45m0s", 2700.0), ("1m30.5s", 90.5),
                           ("30s", 30.0), ("250ms", 0.25), ("0s", 0.0), ("2h", 7200.0), ("1.5h", 5400.0)):
            with self.subTest(text=text):
                self.assertAlmostEqual(p.parse_go_duration(text), want)
        for text in ("", "abc", "24h0m0sx", "5", "h", "-1s", None, "1d"):
            with self.subTest(text=text):
                self.assertIsNone(p.parse_go_duration(text))

    def test_format_wait(self):
        for seconds, want in ((86400, "24 時間"), (5400, "1 時間 30 分"), (3599, "1 時間"), (3601, "1 時間 1 分"),
                              (2700, "45 分"), (90, "2 分"), (60, "1 分"), (30, "30 秒"), (0.2, "1 秒"),
                              (0, ""), (-5, ""), (float("inf"), "")):
            with self.subTest(seconds=seconds):
                self.assertEqual(p.format_wait(seconds), want)

    def test_rate_limit_wait(self):
        self.assertEqual(p.spotify_rate_limit_wait("spotify: rate limited by Spotify; retry after 24h0m0s"), 86400)
        self.assertEqual(p.spotify_rate_limit_wait(
            "spotify: search: spotify: rate limited by Spotify; retry after 1h0m0s"), 3600)
        self.assertEqual(p.spotify_rate_limit_wait(
            "spotify: playlists: spotify: rate limited by Spotify; retry after 45m0s."), 2700)
        # 待ちの分からない制限 (cliamp 1.50.0 の素の文言・長さの無いもの) は 0
        self.assertEqual(p.spotify_rate_limit_wait(
            "spotify: web api rate-limited on /v1/me/playlists after 8 retries (try re-authenticating)"), 0.0)
        self.assertEqual(p.spotify_rate_limit_wait("spotify: rate limited by Spotify"), 0.0)
        self.assertEqual(p.spotify_rate_limit_wait("spotify: rate limited by Spotify; retry after soon"), 0.0)
        for text in ("yt-dlp: ERROR: HTTP Error 429: Too Many Requests", "", None, "rate limited by YouTube",
                     # cliamp の文言の位置に無いもの (曲名・フォルダ名・検索の語)
                     "open /music/Rate Limited By Spotify/01.flac: no such file or directory",
                     "yt-dlp: timed out resolving ytsearch1:Band Rate Limited By Spotify (30s)"):
            with self.subTest(text=text):
                self.assertIsNone(p.spotify_rate_limit_wait(text))

    def test_rate_limit_message_in_catalog_errors(self):
        r = decode_response('{"ok":false,"error":"spotify: rate limited by Spotify; retry after 24h0m0s"}')
        self.assertEqual(r.message, "Spotify から回数の制限を受けています。24 時間ほど待ってから、もう一度試してください")
        r = Response(False, {}, "spotify: web api rate-limited on /v1/search after 8 retries (try re-authenticating)",
                     "error")
        self.assertEqual(r.message, "Spotify から回数の制限を受けています。しばらく待ってから、もう一度試してください")

    def test_search_blocked(self):
        self.assertTrue(p.is_spotify_search_blocked(SEARCH_BLOCKED))
        # friendlySearchError を通らない元の 400 "Invalid limit" も同じ
        raw = 'spotify: search: http status 400 Bad Request: {"error":{"status":400,"message":"Invalid limit"}}'
        self.assertTrue(p.is_spotify_search_blocked(raw))
        for text in ("yt-dlp: exit status 1", "", None, "spotify: search: context deadline exceeded"):
            with self.subTest(text=text):
                self.assertFalse(p.is_spotify_search_blocked(text))
        message = Response(False, {}, SEARCH_BLOCKED, "error").message
        self.assertTrue(message.startswith(p.SPOTIFY_SEARCH_BLOCKED_TITLE), message)
        self.assertIn("YouTube", message)
        self.assertNotIn("client_id is too new", message)
        # 回数の制限を受けた検索 (friendlySearchError は "spotify: search: " を前に付ける) は
        # 「封鎖」とは言わない
        limited = "spotify: search: spotify: rate limited by Spotify; retry after 1h0m0s"
        self.assertFalse(p.is_spotify_search_blocked(limited))
        self.assertIn("1 時間ほど", Response(False, {}, limited, "error").message)

    def test_youtube_errors_that_echo_the_query_are_not_spotify(self):
        """YouTube の検索の誤りは利用者の語をそのまま繰り返す (cliamp の resolve.go:
        "resolving yt-dlp ytsearch20:<語>: …"、"yt-dlp: timed out resolving ytsearch20:<語> (30s)")。
        語に Spotify の断りの言葉が入っていても、Spotify の断りとは読まない。"""
        for query in ("search blocked", "Invalid Limit", "rate limited by Spotify; retry after 24h0m0s",
                      "spotify: search blocked", "spotify: rate limited by Spotify; retry after 1h0m0s",
                      "spotify: streaming unavailable", "spotify: web api rate-limited"):
            for text in (f"resolving yt-dlp ytsearch20:{query}: yt-dlp: timed out resolving ytsearch20:{query} (30s)",
                         f"yt-dlp: timed out resolving ytsearch20:{query} (30s)",
                         f"resolving yt-dlp scsearch20:x: {query}: exit status 1"):
                with self.subTest(text=text):
                    self.assertFalse(p.is_spotify_search_blocked(text))
                    self.assertEqual(p.describe_catalog_error(text), "")
                    self.assertEqual(Response(False, {}, text, "error").message, text)
        # 語が Spotify の文言に似ていなければ、Spotify の誤りの中でも見ない
        self.assertFalse(p.is_spotify_search_blocked("spotify: playlist \"search blocked\" not found"))
        self.assertFalse(p.is_spotify_search_blocked("spotify: list tracks: Invalid limit"))

    def test_other_errors_are_unchanged(self):
        self.assertEqual(Response(False, {}, "unknown provider: x", "error").message, "unknown provider: x")
        self.assertEqual(p.describe_catalog_error("unknown provider: x"), "")
        self.assertEqual(p.describe_catalog_error(""), "")
        self.assertEqual(p.describe_catalog_error("spotify: streaming unavailable"), p.SPOTIFY_PREMIUM_REQUIRED)
        self.assertEqual(p.describe_catalog_error(
            "spotify: your music: spotify: rate limited by Spotify; retry after 24h0m0s"),
            "Spotify から回数の制限を受けています。24 時間ほど待ってから、もう一度試してください")


class PlaybackError(unittest.TestCase):
    """describe_playback_error: cliamp・yt-dlp の誤りの文言 → (短い日本語, 全文)。"""

    SIGN_IN = "YouTube のサインインが必要な曲です (cliamp の設定で Cookie を使う)"
    GONE = "この動画は再生できません (非公開か削除)"

    def short(self, text: str) -> str:
        return p.describe_playback_error(text)[0]

    def test_age_and_bot_checks_need_sign_in(self):
        for text in (
            AGE_GATE,
            "yt-dlp: ERROR: [youtube] abcdefghijk: Sign in to confirm you\u2019re not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication.",
            "yt-dlp: ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. This helps protect our community.",
            "yt-dlp: ERROR: [youtube] abcdefghijk: This video is age-restricted and only available on YouTube.",
            "yt-dlp: ERROR: [youtube] abcdefghijk: Join this channel to get access to members-only content like this video",
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), self.SIGN_IN)

    def test_unavailable_private_or_removed(self):
        for text in (
            "yt-dlp: ERROR: [youtube] abcdefghijk: Video unavailable",
            "yt-dlp: ERROR: [youtube] abcdefghijk: Video unavailable. This video has been removed by the uploader",
            # 「Sign in if you've been granted access」はサインインの案内ではなく非公開の印
            "yt-dlp: ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access to this video",
            "yt-dlp: ERROR: [youtube] abcdefghijk: Video unavailable. This video is no longer available because "
            "the YouTube account associated with this video has been terminated.",
            "yt-dlp: ERROR: [youtube] abcdefghijk: HTTP Error 404: Not Found",
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), self.GONE)

    def test_warning_lines_before_the_error(self):
        text = ("yt-dlp: WARNING: [youtube] abcdefghijk: nsig extraction failed: Some formats may be missing\n"
                "         Install PhantomJS to workaround the issue\n"
                "ERROR: [youtube] abcdefghijk: Video unavailable")
        short, detail = p.describe_playback_error(text)
        self.assertEqual(short, self.GONE)
        self.assertIn("Install PhantomJS", detail)

    def test_geo_block(self):
        self.assertEqual(self.short("yt-dlp: ERROR: [youtube] abcdefghijk: The uploader has not made this video "
                                    "available in your country"), "この地域では再生できない動画です")

    def test_denied_403_429(self):
        self.assertEqual(self.short("yt-dlp: ERROR: unable to download video data: HTTP Error 403: Forbidden"),
                         "YouTube に一時的に拒否されました")
        self.assertEqual(self.short("yt-dlp: ERROR: [youtube] abcdefghijk: HTTP Error 429: Too Many Requests"),
                         "YouTube に一時的に拒否されました")
        self.assertEqual(self.short("open source: http status 403 Forbidden"), "配信元に一時的に拒否されました")
        self.assertEqual(self.short("open source: http status 429 Too Many Requests"), "配信元に一時的に拒否されました")

    def test_link_gone(self):
        self.assertEqual(self.short("open source: http status 404 Not Found"), "見つかりません (リンクが切れています)")

    def test_network_and_dns(self):
        for text in (
            'open source: http get: Get "https://ice.example/stream": dial tcp: lookup ice.example: no such host',
            "yt-dlp: ERROR: [youtube] abcdefghijk: Unable to download API page: <urlopen error [Errno -3] "
            "Temporary failure in name resolution> (caused by URLError(gaierror(-3, 'Temporary failure in name resolution')))",
            'open source: http get: Get "https://x.example/a.mp3": dial tcp 192.0.2.1:443: connect: connection refused',
            'open source: http get: Get "https://x.example/a.mp3": dial tcp 192.0.2.1:443: connect: network is unreachable',
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), "ネットワークに繋がりません")

    def test_timeout(self):
        self.assertEqual(self.short("timed out waiting for audio data (30s)"), "読み込みが時間切れになりました")

    def test_undecodable_files(self):
        for text in (
            "decode: ffmpeg decode: exit status 1",
            "decode: flac.NewSeek: flac.parseStreamInfo: invalid FLAC signature; expected \"fLaC\", got \"RIFF\"",
            "decode: mp3: no audio frames found (unsupported format?)",
            "open source: /music/a.xyz: unknown format",
            "yt-dlp: ffmpeg start: exit status 1",
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), "このファイルは再生できません")

    def test_local_file_problems_and_tools(self):
        self.assertEqual(self.short("open source: open /music/gone.flac: no such file or directory"),
                         "ファイルが見つかりません")
        self.assertEqual(self.short("open source: open /music/a.flac: permission denied"),
                         "ファイルを読めません (アクセス権がありません)")
        self.assertEqual(self.short("ffmpeg is required to play .m4a files — install it with your package manager"),
                         "再生に ffmpeg が要ります")
        self.assertEqual(self.short('yt-dlp start: exec: "yt-dlp": executable file not found in $PATH'),
                         "再生に yt-dlp が要ります")
        self.assertEqual(self.short("no episodes found in feed"), "このフィードにはエピソードがありません")

    def test_spotify_and_generic_sign_in(self):
        for text in (
            "custom streamer: spotify: stream auth error after silent reconnect: sign-in required",
            "custom streamer: spotify: stream auth error, silent reconnect failed: sign-in required",
            "spotify: web api token unavailable, run 'cliamp spotify reset' and sign in again: sign-in required",
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), "Spotify へのサインインが必要です (cliamp の端末で)")
        self.assertEqual(self.short("sign-in required"), "サインインが必要です (cliamp の端末で)")

    def test_spotify_without_a_streaming_session_needs_premium(self):
        """Web API だけの接続で spotify:track: の曲を始めた。login5 や credentials の語が続いても
        サインインの誤りではない (サインインし直しても鳴らない)。"""
        for text in (
            "spotify: streaming unavailable",
            "custom streamer: spotify: streaming unavailable: this Spotify connection is Web API only",
            "spotify: streaming unavailable (no librespot session: failed authenticating with login5: "
            "INVALID_CREDENTIALS)",
        ):
            with self.subTest(text=text[:60]):
                short, detail = p.describe_playback_error(text)
                self.assertEqual(short, "Spotify の曲の再生には Premium が必要です")
                self.assertEqual(detail, text)
        self.assertEqual(p.playback_error_headline("Spotify の曲の再生には Premium が必要です"),
                         "Spotify の曲の再生には Premium が必要です")

    def test_spotify_rate_limit(self):
        short = self.short("spotify: rate limited by Spotify; retry after 24h0m0s")
        self.assertEqual(short, "Spotify から回数の制限を受けています (24 時間ほど待つ)")
        self.assertEqual(p.playback_error_headline(short), "Spotify から回数の制限を受けています")
        self.assertEqual(self.short("custom streamer: spotify: rate limited by Spotify"),
                         "Spotify から回数の制限を受けています (しばらく待つ)")
        # YouTube の 429 は今までどおり
        self.assertEqual(self.short("yt-dlp: ERROR: [youtube] abcdefghijk: HTTP Error 429: Too Many Requests"),
                         "YouTube に一時的に拒否されました")

    def test_unknown_error_is_a_cleaned_first_line(self):
        self.assertEqual(self.short("yt-dlp: ERROR: [generic] Unsupported URL: https://example.com/x"),
                         "この URL は再生できません")
        short, detail = p.describe_playback_error(
            "yt-dlp: ERROR: [soundcloud] 12345: The frobnicator declined to cooperate with the request because "
            "of a reason that is much longer than eighty characters\nsecond line")
        self.assertTrue(short.startswith("The frobnicator declined"), short)
        self.assertLessEqual(len(short), p.PLAYBACK_ERROR_SHORT_MAX)
        self.assertTrue(short.endswith("…"))
        self.assertIn("second line", detail)
        self.assertEqual(self.short("  something odd happened  "), "something odd happened")

    def test_words_in_paths_and_urls_do_not_decide(self):
        """曲のパスやフォルダ名・URL の中の語 (Timeout、Spotify、Private Video、403) で理由を
        取り違えない。見分けるのは cliamp・yt-dlp の文言の部分だけ。"""
        for text, want in (
            ("open source: open /music/Timeout/x.flac: no such file or directory", "ファイルが見つかりません"),
            ("open source: open /music/Spotify Singles/Authority - x.flac: no such file or directory",
             "ファイルが見つかりません"),
            ("open source: open /music/Private Video/x.flac: permission denied",
             "ファイルを読めません (アクセス権がありません)"),
            ("decode: ffmpeg decode: /music/Timed Out/x.m4a: Invalid data found when processing input",
             "このファイルは再生できません"),
            ('open source: http get: Get "https://ice.example/private-video/403-timeout.mp3": dial tcp: '
             "lookup ice.example: no such host", "ネットワークに繋がりません"),
            ("yt-dlp: ERROR: [generic] Unsupported URL: https://example.com/video-unavailable/403",
             "この URL は再生できません"),
            # Spotify の断り (回数の制限・鳴らせない曲) の言葉も、cliamp の文言の位置に無ければ見ない
            ("open /home/u/Music/Rate Limited By Spotify/01 Intro.flac: no such file or directory",
             "ファイルが見つかりません"),
            ("open /music/Spotify Streaming Unavailable/x.flac: no such file or directory", "ファイルが見つかりません"),
            ('yt-dlp: ERROR: [youtube:search] "ytsearch1:x spotify: rate limited by Spotify; retry after 1h0m0s": '
             "Unable to download webpage", "ネットワークに繋がりません"),
        ):
            with self.subTest(text=text[:60]):
                self.assertEqual(self.short(text), want)

    def test_service_follows_the_extractor(self):
        """cliamp の yt-dlp の誤りはどれも "yt-dlp: " で始まる。出どころは抽出器の印で決める。"""
        self.assertEqual(self.short("yt-dlp: ERROR: [soundcloud] 123: HTTP Error 403: Forbidden"),
                         "SoundCloud に一時的に拒否されました")
        self.assertEqual(self.short("yt-dlp: ERROR: [soundcloud] 123: HTTP Error 404: Not Found"),
                         "見つかりません (リンクが切れています)")
        self.assertEqual(self.short("yt-dlp: ERROR: [vimeo] 123: This video is only available for registered "
                                    "users. Use --cookies-from-browser or --cookies for the authentication."),
                         "Vimeo のサインインが必要な曲です (cliamp の設定で Cookie を使う)")
        self.assertEqual(self.short("yt-dlp: ERROR: [generic] x: HTTP Error 429: Too Many Requests"),
                         "配信元に一時的に拒否されました")
        self.assertEqual(self.short("yt-dlp: ERROR: [youtube:tab] PLx: HTTP Error 403: Forbidden"),
                         "YouTube に一時的に拒否されました")

    def test_missing_tool_is_the_one_named(self):
        self.assertEqual(self.short('yt-dlp seek: ffmpeg start: exec: "ffmpeg": executable file not found in $PATH'),
                         "再生に ffmpeg が要ります")
        self.assertEqual(self.short("open source: http get: context deadline exceeded"), "読み込みが時間切れになりました")

    def test_error_line_after_a_long_preamble(self):
        text = "yt-dlp: " + "\n".join(f"note {i}: " + "x" * 90 for i in range(20)) + \
            "\nERROR: [youtube] abcdefghijk: Sign in to confirm your age."
        short, detail = p.describe_playback_error(text)
        self.assertEqual(short, self.SIGN_IN)
        self.assertLessEqual(len(detail), p.PLAYBACK_ERROR_DETAIL_MAX)

    def test_detail_is_the_whole_text_and_empty_is_empty(self):
        short, detail = p.describe_playback_error(AGE_GATE)
        self.assertEqual(detail, " ".join(AGE_GATE.split()))
        self.assertEqual(p.describe_playback_error(""), ("", ""))
        self.assertEqual(p.describe_playback_error("  \n "), ("", ""))
        self.assertEqual(p.describe_playback_error(None), ("", ""))
        long = "x" * 5000
        self.assertLessEqual(len(p.describe_playback_error(long)[1]), p.PLAYBACK_ERROR_DETAIL_MAX)

    def test_headline_and_tooltip(self):
        self.assertEqual(p.playback_error_headline(self.SIGN_IN), "YouTube のサインインが必要な曲です")
        self.assertEqual(p.playback_error_headline("Spotify へのサインインが必要です (cliamp の端末で)"),
                         "Spotify へのサインインが必要です")
        self.assertEqual(p.playback_error_headline("ネットワークに繋がりません"), "ネットワークに繋がりません")
        self.assertEqual(p.playback_error_headline("(only brackets)"), "(only brackets)")
        short, detail = p.describe_playback_error(AGE_GATE)
        self.assertEqual(p.playback_error_tooltip(short, detail), f"{self.SIGN_IN}\n\n{detail}")
        # 知らない誤りで短文が全文と同じなら重ねない
        self.assertEqual(p.playback_error_tooltip(*p.describe_playback_error("odd")), "odd")
        self.assertEqual(p.playback_error_tooltip("", ""), "")

    def test_status_carries_the_problem(self):
        st = parse_status({"ok": True, "state": "stopped", "playback_error": AGE_GATE,
                           "track": {"path": "https://www.youtube.com/watch?v=x8VYWazR5mE", "title": "t", "artist": "a"}})
        self.assertEqual(st.playback_error, AGE_GATE)
        self.assertEqual(st.playback_problem, (self.SIGN_IN, " ".join(AGE_GATE.split())))
        self.assertEqual(st.display_subtitle, self.SIGN_IN)
        self.assertEqual(st.display_title, "t")
        st.buffering = True  # やり直している間は出さない
        self.assertIsNone(st.playback_problem)
        self.assertEqual(st.display_subtitle, "読み込み中…")
        # 省かれていれば (omitempty) 無し
        st = parse_status({"ok": True, "state": "playing", "track": {"path": "x", "title": "t", "artist": "a"}})
        self.assertEqual(st.playback_error, "")
        self.assertIsNone(st.playback_problem)
        self.assertEqual(st.display_subtitle, "a")


if __name__ == "__main__":
    unittest.main()
