"""radio.py の試験。ネットワークには出ない (urlopen を差し替える)。"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import run_loop  # noqa: E402

try:
    from cliamp_music import radio
    from cliamp_music.protocol import Track

    HAVE_GI = True
except ImportError:  # pragma: no cover
    HAVE_GI = False

# Radio Browser の応答の形を写した試験用の局 (架空)。
FIXTURE = [
    {"stationuuid": "1", "name": "  Harbor  Jazz FM ", "url": "http://h.example/m3u",
     "url_resolved": "https://stream.example.net/harbor-jazz.mp3", "favicon": "https://h.example/icon.png",
     "country": "Japan", "countrycode": "jp", "tags": "jazz, smooth jazz,,", "codec": "MP3",
     "bitrate": 128, "homepage": "https://h.example/", "votes": 900},
    {"stationuuid": "2", "name": "Harbor Jazz FM (mirror)", "url_resolved": "https://stream.example.net/harbor-jazz.mp3",
     "favicon": "", "country": "Japan", "bitrate": 64},
    {"stationuuid": "3", "name": "harbor jazz fm", "url_resolved": "https://other.example/stream",
     "favicon": "data:image/png;base64,xx", "bitrate": 0},
    {"stationuuid": "4", "name": "No URL", "url_resolved": "", "url": "ftp://x"},
    {"stationuuid": "5", "name": "Tokyo Lo-fi Radio", "url_resolved": "http://lofi.example/aac",
     "favicon": "http://lofi.example/f.ico", "country": "Japan", "tags": "lofi", "codec": "AAC+",
     "bitrate": 48},
    "not a dict",
]


class FakeHTTP:
    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)

    def read(self, *args):
        return self._body.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class ParseStations(unittest.TestCase):
    def test_parse(self):
        stations = radio.parse_stations(json.dumps(FIXTURE).encode())
        self.assertEqual([s.title for s in stations], ["Harbor Jazz FM", "Tokyo Lo-fi Radio"])
        harbor = stations[0]
        self.assertEqual(harbor.path, "https://stream.example.net/harbor-jazz.mp3")
        self.assertTrue(harbor.stream and harbor.live)
        self.assertEqual(harbor.meta_get("art"), "https://h.example/icon.png")
        self.assertEqual(harbor.meta_get("radio.country"), "Japan")
        self.assertEqual(harbor.meta_get("radio.countrycode"), "JP")
        self.assertEqual(harbor.meta_get("radio.tags"), "jazz,smooth jazz")
        self.assertEqual(harbor.meta_get("radio.codec"), "MP3")
        self.assertEqual(harbor.meta_get("radio.bitrate"), "128")
        self.assertEqual(radio.station_subtitle(harbor), "Japan · jazz · 128 kbps")

    def test_station_round_trips_for_replace(self):
        """局はそのまま replace に渡せる (往復で失われない)。"""
        station = radio.parse_stations(FIXTURE)[1]
        self.assertEqual(Track.from_wire(station.to_wire()), station)
        self.assertNotIn("", dict(station.meta).values())  # 空の meta は持たない

    def test_bad_input(self):
        self.assertEqual(radio.parse_stations({"x": 1}), [])
        self.assertEqual(radio.parse_stations([]), [])
        with self.assertRaises(ValueError):
            radio.parse_stations(b"not json")

    def test_errors_are_japanese(self):
        http = urllib.error.HTTPError("u", 503, "busy", {}, None)
        self.assertIn("HTTP 503", radio.describe_error(http))
        self.assertIn("接続できません", radio.describe_error(urllib.error.URLError("no route")))
        self.assertIn("時間内", radio.describe_error(urllib.error.URLError(TimeoutError("timed out"))))
        self.assertIn("読めません", radio.describe_error(ValueError("x")))


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class Browser(unittest.TestCase):
    def setUp(self):
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self.body = json.dumps(FIXTURE).encode()
        self.fail: Exception | None = None

        def fake_urlopen(request, timeout):
            self.urls.append(request.full_url)
            self.headers.append(dict(request.header_items()))
            self.assertEqual(timeout, radio.TIMEOUT)
            if self.fail is not None:
                raise self.fail
            return FakeHTTP(self.body)

        patcher = mock.patch.object(radio, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.browser = radio.RadioBrowser()

    def call(self, method, *args, **kwargs):
        box = []
        method(*args, box.append, **kwargs)
        self.assertTrue(run_loop(lambda: box))
        return box[0]

    def test_top(self):
        stations = self.call(self.browser.top)
        self.assertEqual(len(stations), 2)
        self.assertEqual(self.urls[0], "https://de1.api.radio-browser.info/json/stations/topvote/40?hidebroken=true")
        self.assertTrue(self.headers[0]["User-agent"].startswith("cliamp-music/"))

    def test_by_country_and_cache(self):
        self.call(self.browser.by_country, "jp", limit=20)
        self.assertIn("/stations/bycountrycodeexact/JP?", self.urls[0])
        self.assertIn("order=votes", self.urls[0])
        self.assertIn("hidebroken=true", self.urls[0])
        self.assertIn("limit=20", self.urls[0])
        self.call(self.browser.by_country, "JP", limit=20)
        self.assertEqual(len(self.urls), 1)

    def test_search_encodes_query(self):
        self.call(self.browser.search, "ジャズ/FM & more")
        path = self.urls[0].split("?")[0]
        self.assertTrue(path.startswith("https://de1.api.radio-browser.info/json/stations/byname/"))
        self.assertEqual(unquote(path.rsplit("/", 1)[1]), "ジャズ/FM & more")
        self.assertNotIn(" ", self.urls[0])
        self.assertEqual(self.call(self.browser.search, "   "), [])
        self.assertEqual(len(self.urls), 1)

    def test_failure_is_a_message(self):
        self.fail = urllib.error.URLError("offline")
        with mock.patch.object(radio, "log"):
            result = self.call(self.browser.top)
        self.assertIsInstance(result, str)
        self.assertIn("Radio Browser", result)
        # 失敗は覚えない
        self.fail = None
        self.assertEqual(len(self.call(self.browser.top)), 2)

    def test_concurrent_requests_share_one_fetch(self):
        boxes = [[], []]
        self.browser.top(boxes[0].append)
        self.browser.top(boxes[1].append)
        self.assertTrue(run_loop(lambda: boxes[0] and boxes[1]))
        self.assertEqual(len(self.urls), 1)
        self.assertEqual(boxes[0][0], boxes[1][0])


if __name__ == "__main__":
    unittest.main()
