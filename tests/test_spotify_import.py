"""spotify_import.py (Spotify の公開頁からの取り込み) の試験。画面もネットワークも使わない。

頁は tests/spotify_fixtures.py で作った架空のもの (本物の頁の中身は使わない)。HTTP は
spotify_import.urlopen を FakeWeb に差し替える。
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import spotify_fixtures as fx  # noqa: E402
from fake_cliamp import fake_spotify_id  # noqa: E402

from cliamp_music import spotify_import as si  # noqa: E402
from cliamp_music.protocol import (  # noqa: E402
    PLAYLIST_NAME_MAX_BYTES,
    SPOTIFY_NOT_ACCESSIBLE,
    SPOTIFY_OWNER_PREMIUM,
    Response,
    Track,
    describe_catalog_error,
    is_spotify_not_accessible,
    is_spotify_owner_premium_required,
    is_youtube_bridge,
    track_key,
    valid_playlist_name,
)

PID = "37i9dQZF1DXfakePlayls1"  # 22 文字 (架空)
AID = "4aawyAB9vmqNfakeAlbum1"
NBSP = "\u00a0"


class ParseUrlTest(unittest.TestCase):
    def test_accepted_forms(self):
        cases = [
            (f"https://open.spotify.com/playlist/{PID}", ("playlist", PID)),
            (f"http://open.spotify.com/playlist/{PID}", ("playlist", PID)),
            (f"open.spotify.com/playlist/{PID}", ("playlist", PID)),
            (f"https://open.spotify.com/playlist/{PID}?si=abcdef0123456789", ("playlist", PID)),
            (f"https://open.spotify.com/playlist/{PID}?si=x&pt=y#top", ("playlist", PID)),
            (f"https://open.spotify.com/intl-ja/playlist/{PID}", ("playlist", PID)),
            (f"https://open.spotify.com/intl-pt/album/{AID}?si=1", ("album", AID)),
            (f"https://open.spotify.com/embed/playlist/{PID}?utm_source=generator", ("playlist", PID)),
            (f"https://open.spotify.com/album/{AID}", ("album", AID)),
            (f"https://open.spotify.com/user/someone/playlist/{PID}", ("playlist", PID)),
            (f"https://play.spotify.com/playlist/{PID}", ("playlist", PID)),
            (f"spotify:playlist:{PID}", ("playlist", PID)),
            (f"spotify:album:{AID}", ("album", AID)),
            (f"spotify:user:someone:playlist:{PID}", ("playlist", PID)),
            (f"  https://open.spotify.com/playlist/{PID}\n", ("playlist", PID)),
            (f"聴いて https://open.spotify.com/playlist/{PID}?si=1 どう?", ("playlist", PID)),
            (f"聴いて→https://open.spotify.com/playlist/{PID}。", ("playlist", PID)),
            (f"HTTPS://OPEN.SPOTIFY.COM/playlist/{PID}", ("playlist", PID)),
            # 文の終わりに置いたリンク (?si= の無いもの) の後の句読点・括弧はリンクではない
            (f"https://open.spotify.com/playlist/{PID}.", ("playlist", PID)),
            (f"https://open.spotify.com/playlist/{PID}。", ("playlist", PID)),
            (f"https://open.spotify.com/playlist/{PID}」", ("playlist", PID)),
            (f"https://open.spotify.com/album/{AID})", ("album", AID)),
            (f"https://open.spotify.com/playlist/{PID}!", ("playlist", PID)),
            (f"https://open.spotify.com/playlist/{PID}?si=abc.", ("playlist", PID)),
            (f"spotify:playlist:{PID}、", ("playlist", PID)),
            (f"「https://open.spotify.com/playlist/{PID}」", ("playlist", PID)),
            (f"(https://open.spotify.com/playlist/{PID})", ("playlist", PID)),
            (f"\"https://open.spotify.com/playlist/{PID}\"", ("playlist", PID)),
        ]
        for text, (kind, sid) in cases:
            with self.subTest(text=text):
                ref = si.parse_spotify_url(text)
                self.assertEqual((ref.kind, ref.id), (kind, sid))
                self.assertEqual(ref.url, f"https://open.spotify.com/{kind}/{sid}")
                self.assertEqual(ref.embed_url, f"https://open.spotify.com/embed/{kind}/{sid}")
                self.assertEqual(ref.uri, f"spotify:{kind}:{sid}")

    def test_rejected_forms_say_why_in_japanese(self):
        cases = [
            ("", "リンクを貼り付けて"),
            ("   ", "リンクを貼り付けて"),
            (f"https://open.spotify.com/track/{PID}", "曲のリンク"),
            (f"spotify:track:{PID}", "曲のリンク"),
            (f"https://open.spotify.com/artist/{PID}", "アーティストのリンク"),
            (f"https://open.spotify.com/show/{PID}", "ポッドキャスト"),
            (f"https://open.spotify.com/episode/{PID}", "ポッドキャスト"),
            ("https://open.spotify.com/collection/tracks", "お気に入りの曲"),
            ("spotify:user:someone:collection", "お気に入りの曲"),
            ("https://spotify.link/AbCdEf", "短縮リンク"),
            ("https://www.youtube.com/playlist?list=PL123", "Spotify のリンクではありません"),
            ("https://example.com/playlist/37i9dQZF1DXfakePlayls1", "Spotify のリンクではありません"),
            ("https://open.spotify.com/playlist/short", "22 文字"),
            (f"https://open.spotify.com/playlist/{PID}x", "22 文字"),
            # "$" は末尾の改行の前にも当たる。ID に改行を入れたまま通さない
            (f"https://open.spotify.com/playlist/{PID}%0A", "22 文字"),
            (f"spotify:playlist:{PID}%0A", "22 文字"),
            (f"https://open.spotify.com/playlist/{PID}%0D%0A", "22 文字"),
            ("https://open.spotify.com/playlist/", "ID がありません"),
            ("https://open.spotify.com/", "プレイリストかアルバムのリンクではありません"),
            ("ftp://open.spotify.com/playlist/37i9dQZF1DXfakePlayls1", "リンクの形ではありません"),
            ("ただの文", "Spotify のリンクではありません"),
        ]
        for text, reason in cases:
            with self.subTest(text=text):
                with self.assertRaises(si.SpotifyImportError) as caught:
                    si.parse_spotify_url(text)
                self.assertEqual(caught.exception.kind, "url")
                self.assertIn(reason, caught.exception.message)
                self.assertEqual(str(caught.exception), caught.exception.message)

    def test_id_shape(self):
        self.assertTrue(si.is_spotify_id(PID))
        for bad in (PID + "\n", PID[:-1], PID + "x", " " + PID[1:], PID[:-1] + "-", None, 22):
            self.assertFalse(si.is_spotify_id(bad), repr(bad))

    def test_check_is_quiet_for_empty_text(self):
        self.assertEqual(si.check_spotify_url(""), (None, ""))
        ref, reason = si.check_spotify_url(f"spotify:playlist:{PID}")
        self.assertEqual((ref.kind, ref.id, reason), ("playlist", PID, ""))
        ref, reason = si.check_spotify_url("https://open.spotify.com/track/x")
        self.assertIsNone(ref)
        self.assertIn("曲のリンク", reason)


class BridgeTest(unittest.TestCase):
    """cliamp の external/spotify/bridge.go (TestBridgedTrack) と同じ結果になるか。"""

    GO_CASES = [
        ("single artist", ["米津玄師"], "Lemon", "ytsearch1:米津玄師 Lemon", "米津玄師"),
        ("multiple artists", ["Queen", "David Bowie"], "Under Pressure",
         "ytsearch1:Queen David Bowie Under Pressure", "Queen, David Bowie"),
        ("commas stay whole", ["Earth, Wind & Fire"], "September",
         "ytsearch1:Earth, Wind & Fire September", "Earth, Wind & Fire"),
        ("no artists", [], "Untitled", "ytsearch1:Untitled", ""),
        ("empty artist skipped", ["", "B"], "Song", "ytsearch1:B Song", ", B"),
        ("special characters kept", ["AC/DC", "Beyoncé"], 'Don\'t Stop Me Now? #1 / "Live" & 100% :)',
         'ytsearch1:AC/DC Beyoncé Don\'t Stop Me Now? #1 / "Live" & 100% :)', "AC/DC, Beyoncé"),
        ("whitespace collapses", [" Spaced  Out "], "  Two\nLines\tTab  ",
         "ytsearch1:Spaced Out Two Lines Tab", " Spaced  Out "),
    ]

    def test_go_examples(self):
        for name, artists, title, path, artist in self.GO_CASES:
            with self.subTest(name):
                track = si.bridge_track("id" + name.replace(" ", "")[:20], title, artists, duration=254)
                self.assertEqual(track.path, path)
                self.assertEqual(track.artist, artist)
                self.assertEqual(track.title, title)
                self.assertEqual(track.duration, 254)
                self.assertFalse(track.stream or track.unplayable or track.live)
                self.assertEqual(dict(track.meta), {"spotify.id": track.meta_get("spotify.id"),
                                                    "spotify.bridge": "youtube"})

    def test_protocol_example_on_the_wire(self):
        """PROTOCOL.md の「Spotify の Web API だけの接続」の例と同じ TrackInfo になる。"""
        track = si.bridge_track("2aoo2jlRnM3A0NyLQqMN2f", "Under Pressure", ["Queen", "David Bowie"],
                                duration=248, album="Hot Space", year=1982, track_number=11)
        self.assertEqual(track.to_wire(), {
            "path": "ytsearch1:Queen David Bowie Under Pressure", "title": "Under Pressure",
            "artist": "Queen, David Bowie", "album": "Hot Space", "year": 1982, "track_number": 11,
            "duration": 248, "meta": {"spotify.id": "2aoo2jlRnM3A0NyLQqMN2f", "spotify.bridge": "youtube"}})
        self.assertTrue(is_youtube_bridge(track))
        self.assertEqual(track.spotify_id, "2aoo2jlRnM3A0NyLQqMN2f")
        self.assertEqual(track.web_url, "https://open.spotify.com/track/2aoo2jlRnM3A0NyLQqMN2f")
        self.assertIsNone(track.youtube_id)

    def test_go_whitespace_rules(self):
        """Go の strings.Fields の空白 (unicode.IsSpace) だけで区切る。U+001C〜U+001F は Python の
        str.split() では空白だが Go では空白でない。改行しない空白と全角の空白は空白。"""
        self.assertEqual(si.bridge_query(["A\x1cB"], "T"), "ytsearch1:A\x1cB T")
        self.assertEqual(si.bridge_query([f"青い{NBSP}灯台"], "夜\u3000明け"), "ytsearch1:青い 灯台 夜 明け")
        self.assertEqual(si.bridge_query(["A\u2028B"], "C\u0085D\u200aE"), "ytsearch1:A B C D E")
        self.assertEqual(si.bridge_query(["A\u200bB"], "T"), "ytsearch1:A\u200bB T")  # 幅の無い空白は字
        self.assertEqual(si.go_fields(" \t\n"), [])

    def test_split_artists_uses_the_nbsp_comma(self):
        self.assertEqual(si.split_artists(f"Pitbull,{NBSP}Sensato"), ["Pitbull", "Sensato"])
        self.assertEqual(si.split_artists("Earth, Wind & Fire"), ["Earth, Wind & Fire"])
        self.assertEqual(si.split_artists(f"Earth, Wind & Fire,{NBSP}Chaka"), ["Earth, Wind & Fire", "Chaka"])
        self.assertEqual(si.split_artists(""), [])


class EmbedTest(unittest.TestCase):
    def test_playlist(self):
        page = fx.embed_page(id=PID, name="夜更かし & <深夜>", subtitle="架空の作り手")
        result = si.parse_embed_html(page, si.SpotifyRef("playlist", PID))
        self.assertEqual((result.kind, result.id, result.name, result.subtitle), ("playlist", PID, "夜更かし & <深夜>",
                                                                                  "架空の作り手"))
        self.assertTrue(result.cover_url.startswith("https://i.scdn.co/image/"))
        self.assertEqual(result.count, 6)
        self.assertFalse(result.truncated)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.url, f"https://open.spotify.com/playlist/{PID}")
        first, second, third, _fourth, fifth, sixth = result.tracks
        self.assertEqual(first.path, "ytsearch1:青い灯台 夜明けのバス停")
        self.assertEqual(first.duration, 214)  # ミリ秒を切り捨て (Go の DurationMs / 1000)
        self.assertEqual(second.artist, "Hyperlocal, Kite & <Theory>")
        self.assertEqual(second.path, "ytsearch1:Hyperlocal Kite & <Theory> Tape Garden")
        self.assertEqual(second.duration, 167)
        self.assertEqual(third.artist, "Earth, Wind & Water")
        self.assertEqual(third.path, "ytsearch1:Earth, Wind & Water September Rain")
        self.assertEqual(fifth.path, "ytsearch1:City Lights Ms. Tone Neon Drive (Night Mix)")
        self.assertEqual(fifth.artist, "City Lights, , Ms. Tone")
        self.assertEqual(fifth.title, "Neon  Drive\n(Night Mix)")
        self.assertEqual(sixth.title, "星の&<かけら>")
        for track in result.tracks:
            self.assertTrue(is_youtube_bridge(track))
            self.assertEqual(len(track.spotify_id or ""), 22)
            self.assertEqual(track.album, "")
        self.assertEqual(si.summary_line(result), "プレイリスト · 架空の作り手 · 6 曲")

    def test_album_takes_cover_from_visual_identity_and_names_the_album(self):
        page = fx.embed_page(kind="album", id=AID, name="Global Fake", subtitle=f"Pit,{NBSP}Sen")
        result = si.parse_embed_html(page)
        self.assertEqual((result.kind, result.id), ("album", AID))
        self.assertEqual(result.subtitle, "Pit, Sen")
        self.assertIn("ab67fake", result.cover_url)  # いちばん大きな (640) もの
        self.assertTrue(all(t.album == "Global Fake" for t in result.tracks))
        self.assertEqual(si.summary_line(result), "アルバム · Pit, Sen · 6 曲")

    def test_non_tracks_are_skipped(self):
        items = fx.tracks(3, "mix")
        items.insert(1, fx.track_item("Episode 1", ["A Show"], 1_800_000, "ep", uri="spotify:episode:" + "e" * 22))
        items.append(fx.track_item("local.mp3", ["Me"], 1000, "loc", uri="spotify:local:Me::local:1"))
        items.append({"uri": "spotify:track:" + fake_spotify_id("x"), "title": "", "subtitle": ""})
        items.append("壊れた項目")
        result = si.parse_embed_html(fx.embed_page(id=PID, items=items))
        self.assertEqual(result.count, 3)
        self.assertEqual(result.skipped, 4)
        self.assertEqual(result.listed, 7)

    def test_missing_duration_and_bad_values(self):
        items = fx.tracks(2, "odd")
        items[0]["duration"] = None
        items[1]["duration"] = "12"
        result = si.parse_embed_html(fx.embed_page(id=PID, items=items))
        self.assertEqual([t.duration for t in result.tracks], [0, 0])

    def test_truncated_at_the_cap(self):
        result = si.parse_embed_html(fx.embed_page(id=PID, items=fx.tracks(100, "big")))
        self.assertTrue(result.truncated)
        self.assertEqual(result.count, 100)
        self.assertEqual(si.with_total(result, 150).total, 150)
        self.assertTrue(si.with_total(result, 150).truncated)
        self.assertFalse(si.with_total(result, 100).truncated)
        self.assertTrue(si.with_total(result, 0).truncated)  # 分からなければ切れているものとする
        small = si.parse_embed_html(fx.embed_page(id=PID, items=fx.tracks(99, "small")))
        self.assertFalse(small.truncated)
        self.assertFalse(si.with_total(small, 250).truncated)

    def test_not_found_and_unavailable_pages(self):
        with self.assertRaises(si.SpotifyImportError) as caught:
            si.parse_embed_html(fx.missing_page(404))
        self.assertEqual(caught.exception.kind, "not-found")
        self.assertIn("非公開", caught.exception.message)
        with self.assertRaises(si.SpotifyImportError) as caught:
            si.parse_embed_html(fx.missing_page(500))
        self.assertEqual(caught.exception.kind, "unavailable")
        self.assertIn("500", caught.exception.message)

    def test_changed_page_shapes(self):
        good = json.loads(fx.embed_page(id=PID).split('type="application/json">', 1)[1].split("</script>")[0])

        def page(data) -> str:
            return fx.next_data_page(data["props"]["pageProps"]) if "props" in data else ""

        broken = json.loads(json.dumps(good))
        del broken["props"]["pageProps"]["state"]["data"]["entity"]
        no_list = json.loads(json.dumps(good))
        no_list["props"]["pageProps"]["state"]["data"]["entity"]["trackList"] = {"items": []}
        cases = {
            "no script": "<html><body>Spotify</body></html>",
            "bad json": '<script id="__NEXT_DATA__" type="application/json">{"props": </script>',
            "no entity": page(broken),
            "track list is not a list": page(no_list),
            "props missing": '<script id="__NEXT_DATA__" type="application/json">{"page": "/"}</script>',
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(si.SpotifyImportError) as caught:
                    si.parse_embed_html(text)
                self.assertEqual(caught.exception.kind, "shape")
                self.assertIn("形が変わった", caught.exception.message)
                self.assertTrue(caught.exception.detail)

    def test_broken_surrogates_become_replacement_characters(self):
        """JSON の "\\ud83d" (切れた絵文字) は Python では UTF-8 にできない文字になる。頁から取った文字列は
        どれも U+FFFD にする (cliamp へ送る・GTK に渡す・表に保存するところで落ちない)。"""
        page = fx.embed_page(id=PID, name="夜@@", subtitle="作り手@@",
                             items=[fx.track_item("曲@@", ["人@@", "B"], 200_000, "s1")],
                             cover="https://i.scdn.co/image/x@@")
        result = si.parse_embed_html(page.replace("@@", "\\ud83d"))
        self.assertEqual((result.name, result.subtitle), ("夜\ufffd", "作り手\ufffd"))
        self.assertEqual(result.cover_url, "https://i.scdn.co/image/x\ufffd")
        track = result.tracks[0]
        self.assertEqual((track.title, track.artist, track.path), ("曲\ufffd", "人\ufffd, B", "ytsearch1:人\ufffd B 曲\ufffd"))
        for text in (result.name, result.subtitle, result.cover_url, track.title, track.artist, track.path,
                     si.playlist_name_for(result)):
            text.encode("utf-8")
        # 対になったもの (正しい絵文字) はそのまま
        whole = si.parse_embed_html(fx.embed_page(id=PID, name="@@").replace("@@", "\\ud83c\\udfb5"))
        self.assertEqual(whole.name, "\U0001f3b5")
        self.assertEqual(si.clean_text("a\ud83db"), "a\ufffdb")
        self.assertEqual(si.clean_text("\ud83c\udfb5"), "\U0001f3b5")

    def test_empty_playlist(self):
        with self.assertRaises(si.SpotifyImportError) as caught:
            si.parse_embed_html(fx.embed_page(id=PID, items=[]))
        self.assertEqual(caught.exception.kind, "empty")
        self.assertIn("曲がありません", caught.exception.message)

    def test_total_count(self):
        self.assertEqual(si.parse_total_count(fx.count_page(150)), 150)
        self.assertEqual(si.parse_total_count(fx.count_page(1234, kind="album")), 1234)
        only_description = '<meta property="og:description" content="Playlist · X · 1,204 items · 12M saves"/>'
        self.assertEqual(si.parse_total_count(only_description), 1204)
        self.assertEqual(si.parse_total_count('<meta name="description" content="Album · A · 2012 · 18 songs"/>'),
                         18)
        self.assertEqual(si.parse_total_count("<html></html>"), 0)
        self.assertEqual(si.parse_total_count(None), 0)


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.web = fx.FakeWeb()
        patcher = mock.patch.object(si, "urlopen", self.web)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_small_playlist_needs_one_request_with_a_browser_user_agent(self):
        self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID, items=fx.tracks(50, "s"))
        result = si.fetch_list(f"https://open.spotify.com/playlist/{PID}?si=1")
        self.assertEqual(result.count, 50)
        self.assertFalse(result.truncated)
        self.assertEqual(len(self.web.requests), 1)
        url, agent = self.web.requests[0]
        self.assertEqual(url, fx.embed_url("playlist", PID))
        self.assertIn("Mozilla/5.0", agent)
        # 日本語を頼むとアーティスト名が訳される (cliamp の Web API の名前と違ってしまう)
        self.assertEqual(self.web.languages[0], "en")

    def test_truncated_playlist_reads_the_total_with_a_plain_user_agent(self):
        self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID, items=fx.tracks(100, "t"))
        self.web.pages[fx.page_url("playlist", PID)] = fx.count_page(150)
        result = si.fetch_list(si.SpotifyRef("playlist", PID))
        self.assertTrue(result.truncated)
        self.assertEqual((result.count, result.total), (100, 150))
        self.assertEqual(self.web.urls(), [fx.embed_url("playlist", PID), fx.page_url("playlist", PID)])
        self.assertNotIn("Mozilla", self.web.requests[1][1])
        self.assertEqual(si.truncated_note(result), f"{si.TRUNCATED_NOTE} (全 150 曲)")
        self.assertEqual(si.summary_line(result), "プレイリスト · 架空の作り手 · 100 曲 (全 150 曲)")

    def test_exactly_one_hundred_is_not_truncated(self):
        self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID, items=fx.tracks(100, "h"))
        self.web.pages[fx.page_url("playlist", PID)] = fx.count_page(100)
        result = si.fetch_list(si.SpotifyRef("playlist", PID))
        self.assertFalse(result.truncated)
        self.assertEqual(si.truncated_note(result), "")

    def test_unknown_total_still_imports(self):
        self.web.pages[fx.embed_url("playlist", PID)] = fx.embed_page(id=PID, items=fx.tracks(100, "u"))
        self.web.pages[fx.page_url("playlist", PID)] = urllib.error.URLError(OSError("no route"))
        with mock.patch.object(si, "log"):
            result = si.fetch_list(si.SpotifyRef("playlist", PID))
        self.assertTrue(result.truncated)
        self.assertEqual(result.total, 0)
        self.assertEqual(si.truncated_note(result), si.TRUNCATED_NOTE)

    def test_http_and_network_failures(self):
        url = fx.embed_url("playlist", PID)
        cases = [
            (None, "not-found", "見つかりません"),
            (urllib.error.HTTPError(url, 429, "Too Many", None, None), "rate-limited", "回数の制限"),
            (urllib.error.HTTPError(url, 503, "Unavailable", None, None), "unavailable", "503"),
            (urllib.error.URLError(OSError(-2, "Name or service not known")), "network", "ネットワークに繋がりません"),
            (urllib.error.URLError(socket.timeout("timed out")), "network", "時間切れ"),
            (TimeoutError("timed out"), "network", "時間切れ"),
            (ConnectionResetError("reset"), "network", "ネットワークに繋がりません"),
            (fx.missing_page(404), "not-found", "非公開"),
            ("x" * (si.MAX_PAGE_BYTES + 10), "shape", "大きすぎ"),
        ]
        for value, kind, text in cases:
            with self.subTest(kind=kind, value=repr(value)[:40]):
                if value is None:
                    self.web.pages.pop(url, None)
                else:
                    self.web.pages[url] = value
                with self.assertRaises(si.SpotifyImportError) as caught:
                    si.fetch_list(si.SpotifyRef("playlist", PID))
                self.assertEqual(caught.exception.kind, kind)
                self.assertIn(text, caught.exception.message)

    def test_bad_url_raises_before_any_request(self):
        with self.assertRaises(si.SpotifyImportError):
            si.fetch_list("https://open.spotify.com/track/" + PID)
        self.assertEqual(self.web.requests, [])


class NamingTest(unittest.TestCase):
    def result(self, name: str, kind: str = "playlist") -> si.ImportedList:
        return si.ImportedList(kind=kind, id=PID, name=name, tracks=(si.bridge_track(PID, "T", ["A"]),))

    def test_playlist_name(self):
        self.assertEqual(si.playlist_name_for(self.result("AC/DC  ベスト\\2")), "AC／DC ベスト＼2")
        self.assertEqual(si.playlist_name_for(self.result("Recently Played")), "Recently Played (Spotify)")
        self.assertEqual(si.playlist_name_for(self.result("..")), "Spotify のプレイリスト")
        self.assertEqual(si.playlist_name_for(self.result("   ", "album")), "Spotify のアルバム")

    def test_suggest_name(self):
        self.assertEqual(si.suggest_name("Focus", ["ドライブ"]), "Focus")
        self.assertEqual(si.suggest_name("Focus", ["Focus", "Focus 2"]), "Focus 3")

    def test_long_names_fit_the_file_name_limit(self):
        """cliamp は <名前>.toml を開くので、名前 + ".toml" が 255 バイト (NAME_MAX) を超えると断られる。
        既定の名前と「名前 2」は文字の切れ目で縮めて収める (縮めたら「…」)。"""
        limit = PLAYLIST_NAME_MAX_BYTES
        self.assertEqual(limit, 250)
        for name in ("夏" * 84, "a" * 300, "🎵" * 70, "夏" * 83 + "ab"):
            with self.subTest(name=name[:3]):
                fitted = si.playlist_name_for(self.result(name))
                self.assertLessEqual(len(fitted.encode("utf-8")), limit)
                self.assertTrue(fitted.endswith("…"))
                self.assertTrue(name.startswith(fitted[:-1]))
                self.assertTrue(valid_playlist_name(fitted))
                again = si.suggest_name(fitted, [fitted])
                self.assertLessEqual(len(again.encode("utf-8")), limit)
                self.assertTrue(again.endswith(" 2"))
                self.assertTrue(valid_playlist_name(again))
        self.assertEqual(si.playlist_name_for(self.result("夏" * 83)), "夏" * 83)  # 249 バイトは収まる


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cm-imports-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "spotify-imports.json")
        self.result = si.parse_embed_html(fx.embed_page(id=PID, name="夜更かし", items=fx.tracks(6, "idx")))

    def test_default_path_is_next_to_the_state_file(self):
        self.assertEqual(si.default_index_path("/x/state/cliamp-music/state.json"),
                         "/x/state/cliamp-music/spotify-imports.json")
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/y/state"}):
            self.assertEqual(si.default_index_path(), "/y/state/cliamp-music/spotify-imports.json")

    def test_nothing_is_read_or_written_until_used(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{壊れた")
        index = si.ImportIndex(self.path)
        self.assertIsNone(index._data)
        with mock.patch.object(si, "log") as log:
            self.assertIsNone(index.lookup("ytsearch1:x"))
        log.assert_called_once()  # 壊れていたので空で始めた
        other = si.ImportIndex(os.path.join(self.dir, "none", "x.json"))
        other.restore(Track(path="https://example.com/a.mp3"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "none")))

    def test_record_and_restore_after_reload(self):
        index = si.ImportIndex(self.path)
        record = index.record("夜更かし", self.result, now=1_790_000_000)
        self.assertEqual(record.url, f"https://open.spotify.com/playlist/{PID}")
        self.assertEqual(record.imported_at, "2026-09-21T14:13:20Z")
        self.assertEqual([p for p in os.listdir(self.dir)], ["spotify-imports.json"])  # 一時ファイルは残らない
        reloaded = si.ImportIndex(self.path)
        got = reloaded.get("夜更かし")
        self.assertEqual((got.kind, got.id, got.title, got.count, got.cover_url),
                         ("playlist", PID, "夜更かし", 6, self.result.cover_url))
        # ローカルの TOML から読み戻した曲 (meta が無い) に ID を付け直す
        original = self.result.tracks[1]
        stored = Track(path=original.path, title=original.title, artist=original.artist, duration=original.duration,
                       stream=True)
        self.assertFalse(is_youtube_bridge(stored))
        self.assertIsNone(stored.spotify_id)
        restored = reloaded.restore(stored)
        self.assertTrue(is_youtube_bridge(restored))
        self.assertEqual(restored.spotify_id, original.spotify_id)
        self.assertEqual(restored.web_url, f"https://open.spotify.com/track/{original.spotify_id}")
        self.assertEqual(track_key(restored), f"spotify:{original.spotify_id}")
        self.assertEqual((restored.path, restored.title, restored.stream), (stored.path, stored.title, True))
        try:
            from cliamp_music.artwork import SPOTIFY_OEMBED, art_sources
        except (ImportError, ValueError):  # pragma: no cover
            return
        self.assertEqual(art_sources(restored)[0],
                         f"{SPOTIFY_OEMBED}https://open.spotify.com/track/{original.spotify_id}")
        self.assertEqual(art_sources(stored), [])

    def test_restore_leaves_other_tracks_alone(self):
        index = si.ImportIndex(self.path)
        index.record("夜更かし", self.result)
        other = Track(path="ytsearch1:誰も知らない曲", title="x")
        youtube = Track(path="https://www.youtube.com/watch?v=BoW3OHT6g0s")
        already = self.result.tracks[0]
        own = Track(path=already.path, meta={"spotify.id": "0" * 22})
        self.assertIs(index.restore(other), other)
        self.assertIs(index.restore(youtube), youtube)
        self.assertIs(index.restore(already), already)
        self.assertIs(index.restore(own), own)
        self.assertIsNone(index.restore(None))
        plain = [youtube, Track(path="/music/a.flac")]
        self.assertEqual(index.restore_all(plain), plain)

    def test_prune_forget_and_bounds(self):
        index = si.ImportIndex(self.path)
        index.record("古い", self.result, now=1000.0)
        index.record("新しい", self.result, now=5000.0)
        self.assertEqual(index.prune(["新しい"], now=5050.0), ["古い"])
        self.assertEqual(index.prune([], now=5050.0), [])  # 取り込んだばかりは残す
        self.assertEqual(index.names(), ["新しい"])
        self.assertTrue(index.forget("新しい"))
        self.assertFalse(index.forget("新しい"))
        # 曲の ID は残す (別のプレイリストに入れた曲の絵のため)
        self.assertIsNotNone(index.lookup(self.result.tracks[0].path))
        with mock.patch.object(si, "MAX_TRACKS", 4), mock.patch.object(si, "MAX_PLAYLISTS", 2):
            for n in range(3):
                index.record(f"list{n}", self.result)
            self.assertEqual(len(index._loaded().tracks), 4)
            self.assertEqual(index.names(), ["list1", "list2"])
            self.assertIsNone(index.lookup(self.result.tracks[0].path))  # 古いものから忘れる
            self.assertIsNotNone(index.lookup(self.result.tracks[-1].path))

    def test_bad_entries_in_the_file_are_dropped(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1,
                       "tracks": {"ytsearch1:A T": "0" * 22, "https://x": "0" * 22, "ytsearch1:B": "short"},
                       "playlists": {"ok": {"url": f"spotify:playlist:{PID}", "count": 3},
                                     "bad": {"url": "https://example.com"}, "": {"url": "x"}}}, handle)
        index = si.ImportIndex(self.path)
        self.assertEqual(index.lookup("ytsearch1:A T"), "0" * 22)
        self.assertIsNone(index.lookup("ytsearch1:B"))
        self.assertEqual(index.names(), ["ok"])
        self.assertEqual(index.get("ok").url, f"https://open.spotify.com/playlist/{PID}")

    def test_confirm_checks_the_tracks_and_forgets_stale_records(self):
        index = si.ImportIndex(self.path)
        index.record("夜更かし", self.result, now=1000.0)
        tracks = list(self.result.tracks)
        own = [Track(path=f"/music/{n}.flac") for n in range(4)]
        self.assertEqual(index.known_count(tracks + own), 6)
        self.assertIs(index.confirm("夜更かし", tracks, now=5000.0), index.get("夜更かし"))
        self.assertIsNotNone(index.confirm("夜更かし", tracks + own, now=5000.0))  # 自分の曲を少し足した
        self.assertIsNone(index.confirm("夜更かし", [], now=5000.0))  # 空では決めない
        self.assertIsNotNone(index.get("夜更かし"))
        self.assertIsNone(index.confirm("ほか", tracks, now=5000.0))
        # 取り込んだばかりなら、合わなくても忘れない (取り込む前に頼んだ一覧が後から届く)
        with mock.patch.object(si, "log"):
            self.assertIsNone(index.confirm("夜更かし", own, now=1000.0 + si.PRUNE_GRACE - 1))
        self.assertIsNotNone(index.get("夜更かし"))
        # 取り込んだ曲が半分に満たない (作り直した・自分の曲の方が多い) なら別物として忘れる
        with mock.patch.object(si, "log") as log:
            self.assertIsNone(index.confirm("夜更かし", tracks[:2] + own[:3], now=5000.0))
        log.assert_called_once()
        self.assertIsNone(index.get("夜更かし"))
        self.assertIsNone(si.ImportIndex(self.path).get("夜更かし"), "忘れたことを保存していません")

    def test_backups_are_written_trimmed_and_dropped(self):
        index = si.ImportIndex(self.path)
        tracks = list(self.result.tracks)
        path = index.backup("夜/更かし", tracks, now=1_790_000_000)
        self.assertTrue(path.startswith(os.path.join(self.dir, si.BACKUP_DIR) + os.sep))
        with open(path, encoding="utf-8") as handle:
            kept = json.load(handle)
        self.assertEqual(kept["name"], "夜/更かし")
        self.assertEqual(kept["saved_at"], "2026-09-21T14:13:20Z")
        self.assertEqual(kept["tracks"], [t.to_wire() for t in tracks])
        self.assertEqual([Track.from_wire(t) for t in kept["tracks"]], tracks)
        self.assertEqual(index.backups(), [path])
        with mock.patch.object(si, "MAX_BACKUPS", 3):
            for n in range(5):
                index.backup(f"n{n}", tracks[:1], now=1_790_000_100 + n)
            self.assertEqual(len(index.backups()), 3)
            self.assertNotIn(path, index.backups())  # 古いものから消す
        latest = index.backups()[-1]
        index.drop_backup(latest)
        index.drop_backup(latest)  # もう無くてもよい
        index.drop_backup(None)
        self.assertNotIn(latest, index.backups())
        self.assertFalse([n for n in os.listdir(os.path.join(self.dir, si.BACKUP_DIR)) if n.startswith(".")])
        blocker = os.path.join(self.dir, "file")
        with open(blocker, "w") as handle:
            handle.write("x")
        blocked = si.ImportIndex(os.path.join(blocker, "x.json"))  # ファイルの下にはディレクトリを作れない
        with mock.patch.object(si, "log") as log:
            self.assertIsNone(blocked.backup("x", tracks))
        self.assertTrue(log.called)

    def test_strings_that_utf8_cannot_hold_do_not_stop_saving(self):
        """壊れたサロゲートの紛れた名前・path があっても表は保存でき (ASCII で書く)、読み直すと U+FFFD。"""
        index = si.ImportIndex(self.path)
        index.record("ok", self.result)
        data = index._loaded()
        data.tracks["ytsearch1:壊れた\ud83d"] = "0" * 22
        data.playlists["壊れた\ud83d"] = dataclasses.replace(index.get("ok"), name="壊れた\ud83d", title="x\ud83d")
        with mock.patch.object(si, "log") as log:
            self.assertTrue(index.save())
        log.assert_not_called()
        reloaded = si.ImportIndex(self.path)
        self.assertEqual(reloaded.lookup("ytsearch1:壊れた\ufffd"), "0" * 22)
        self.assertEqual(reloaded.get("壊れた\ufffd").title, "x\ufffd")
        self.assertIsNotNone(reloaded.get("ok"))
        # ふだんは読める UTF-8 のまま
        index = si.ImportIndex(os.path.join(self.dir, "plain.json"))
        index.record("夜更かし", self.result)
        with open(index.path, encoding="utf-8") as handle:
            self.assertIn("夜更かし", handle.read())

    def test_save_failure_is_logged(self):
        blocker = os.path.join(self.dir, "file")
        with open(blocker, "w") as handle:
            handle.write("x")
        index = si.ImportIndex(os.path.join(blocker, "sub", "x.json"))
        with mock.patch.object(si, "log") as log:
            index.record("x", self.result)
        self.assertTrue(log.called)


class OwnerPremiumErrorTest(unittest.TestCase):
    BODY = ('{\n  "error" : {\n    "status" : 403,\n    "message" : "Active premium subscription required for '
            'the owner of the app"\n  }\n}')

    def test_recognized_in_cliamp_spotify_errors(self):
        for text in (f"spotify: your music: http status 403 Forbidden: {self.BODY}",
                     f"spotify: search: http status 403 Forbidden: {self.BODY}",
                     f"spotify: list playlists: http status 403 Forbidden: {self.BODY}"):
            with self.subTest(text=text[:30]):
                self.assertTrue(is_spotify_owner_premium_required(text))
                self.assertEqual(describe_catalog_error(text), SPOTIFY_OWNER_PREMIUM)
                self.assertEqual(Response(False, {}, text, "error").message, SPOTIFY_OWNER_PREMIUM)

    def test_youtube_errors_that_echo_the_words_are_not_it(self):
        text = ("resolving yt-dlp ytsearch25:Active premium subscription required for the owner of the app: "
                "yt-dlp: exit status 1")
        self.assertFalse(is_spotify_owner_premium_required(text))
        self.assertEqual(describe_catalog_error(text), "")

    def test_not_accessible_is_explained(self):
        text = "spotify: playlist not accessible: only playlists you own or collaborate on can be loaded"
        self.assertTrue(is_spotify_not_accessible(text))
        self.assertEqual(describe_catalog_error(text), SPOTIFY_NOT_ACCESSIBLE)
        self.assertFalse(is_spotify_not_accessible("resolving yt-dlp ytsearch1:spotify: playlist not accessible"))


if __name__ == "__main__":
    unittest.main()
