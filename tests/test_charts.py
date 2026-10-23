"""charts.py の単体テスト (日本トップ50、合成データのみ、ネットワークなし)。"""

import base64
import json
import threading
import unittest
from unittest.mock import patch

try:
    from gi.repository import GLib
    _GI_AVAILABLE = True
except ImportError:
    _GI_AVAILABLE = False

if _GI_AVAILABLE:
    from cliamp_music.charts import (
        CHART_DISPLAY_LIMIT,
        CHART_URL,
        ChartEntry,
        ChartSnapshot,
        TopCharts,
        parse_chart_html,
        parse_chart_json,
    )
    from cliamp_music.protocol import Track
    from cliamp_music.spotify_import import SpotifyImportError


def _make_playlist_dict(count=2, rank_order=None):
    """Minimal valid playlist dict for parse_chart_json tests."""
    if rank_order is None:
        rank_order = list(range(1, count + 1))
    items = []
    for i in range(count):
        tid = rank_order[i] if i < len(rank_order) else i + 1
        items.append({
            "itemV2": {
                "__typename": "TrackResponseWrapper",
                "data": {
                    "__typename": "Track",
                    "uri": f"spotify:track:{tid:022d}",
                    "name": f"Song {tid}",
                    "artists": {"items": [{"profile": {"name": f"Artist {tid}"}, "uri": f"spotify:artist:{tid:022d}"}]},
                    "albumOfTrack": {
                        "name": f"Album {tid}",
                        "coverArt": {"sources": [{"width": 640, "height": 640, "url": f"https://i.scdn.co/image/img{tid}"}]},
                    },
                    "duration": {"totalMilliseconds": 180000},
                },
            }
        })
    return {
        "entities": {"items": {"spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq": {
            "__typename": "Playlist",
            "id": "37i9dQZEVXbKXQ4mDTEBXq",
            "uri": "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq",
            "name": "Top 50 - Japan",
            "ownerV2": {"data": {"__typename": "User", "uri": "spotify:user:spotify", "username": "spotify", "name": "Spotify"}},
            "content": {"__typename": "PlaylistItemsPage", "pagingInfo": {"nextOffset": count}, "totalCount": 50, "items": items},
        }}}
    }


def _make_html(data_dict):
    b64 = base64.b64encode(json.dumps(data_dict).encode("utf-8")).decode("ascii")
    return f'<script id="initialState" type="text/plain">{b64}</script>'


@unittest.skipUnless(_GI_AVAILABLE, "GI が使えないため charts テストをスキップ")
class TestParseChartJson(unittest.TestCase):
    def test_basic_parse(self):
        data = _make_playlist_dict(count=2)
        snap = parse_chart_json(data)
        self.assertIsInstance(snap, ChartSnapshot)
        self.assertEqual(len(snap.entries), 2)
        self.assertEqual(snap.entries[0].rank, 1)
        self.assertEqual(snap.entries[0].track.title, "Song 1")
        self.assertEqual(snap.entries[0].track.artist, "Artist 1")
        self.assertEqual(snap.entries[0].track.path, "spotify:track:0000000000000000000001")

    def test_source_order_preserved_not_sorted(self):
        """Source order is preserved; titles do not affect ordering."""
        # rank_order=[5,1,4,2,3] means track identities appear in that order
        # but source positions (ranks) are 1,2,3,4,5
        data = _make_playlist_dict(count=5, rank_order=[5, 1, 4, 2, 3])
        snap = parse_chart_json(data)
        self.assertEqual([e.rank for e in snap.entries], [1, 2, 3, 4, 5])
        # Track at rank 1 is the one with identity 5 (first in source)
        self.assertIn("Song 5", snap.entries[0].track.title)
        self.assertIn("Song 1", snap.entries[1].track.title)

    def test_top12_limit(self):
        data = _make_playlist_dict(count=20)
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), CHART_DISPLAY_LIMIT)
        self.assertEqual(snap.entries[0].rank, 1)
        self.assertEqual(snap.entries[-1].rank, CHART_DISPLAY_LIMIT)

    def test_skipped_malformed_maintains_real_positions(self):
        """A malformed row at position 2 is skipped; position 3 keeps rank 3."""
        data = _make_playlist_dict(count=3)
        # Break item at index 1 (position 2)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"][1]["itemV2"]["data"]["name"] = ""
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 2)
        self.assertEqual(snap.entries[0].rank, 1)
        self.assertEqual(snap.entries[1].rank, 3)

    def test_duplicate_retains_earliest_rank(self):
        """Duplicate URI keeps first occurrence (earliest rank)."""
        data = _make_playlist_dict(count=3)
        # Make item at index 1 have same URI as index 0
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[1]["itemV2"]["data"]["uri"] = items[0]["itemV2"]["data"]["uri"]
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 2)
        self.assertEqual(snap.entries[0].rank, 1)
        self.assertEqual(snap.entries[1].rank, 3)

    def test_invalid_uri_skipped(self):
        data = _make_playlist_dict(count=2)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["uri"] = "spotify:track:short"
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 1)
        self.assertEqual(snap.entries[0].rank, 2)

    def test_uri_trailing_newline_rejected(self):
        data = _make_playlist_dict(count=2)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["uri"] = "spotify:track:0000000000000000000001\n"
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 1)

    def test_largest_direct_album_art(self):
        data = _make_playlist_dict(count=1)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["albumOfTrack"]["coverArt"]["sources"] = [
            {"width": 64, "height": 64, "url": "https://i.scdn.co/image/small"},
            {"width": 640, "height": 640, "url": "https://i.scdn.co/image/large"},
            {"width": 300, "height": 300, "url": "https://i.scdn.co/image/mid"},
        ]
        snap = parse_chart_json(data)
        art = dict(snap.entries[0].track.meta).get("art", "")
        self.assertEqual(art, "https://i.scdn.co/image/large")

    def test_missing_image_fallback(self):
        """No coverArt sources → empty meta (oEmbed fallback)."""
        data = _make_playlist_dict(count=1)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        del items[0]["itemV2"]["data"]["albumOfTrack"]["coverArt"]
        snap = parse_chart_json(data)
        self.assertEqual(snap.entries[0].track.meta, ())

    def test_nonempty_sources_no_safe_image_skips_row(self):
        data = _make_playlist_dict(count=2)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["albumOfTrack"]["coverArt"]["sources"] = [
            {"width": 640, "height": 640, "url": "http://evil.com/bad"},
        ]
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 1)
        self.assertEqual(snap.entries[0].rank, 2)

    def test_invalid_image_newline_rejected(self):
        data = _make_playlist_dict(count=2)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["albumOfTrack"]["coverArt"]["sources"] = [
            {"width": 640, "height": 640, "url": "https://i.scdn.co/image/ok\n"},
        ]
        snap = parse_chart_json(data)
        self.assertEqual(len(snap.entries), 1)

    def test_wrong_typename_raises(self):
        data = _make_playlist_dict(count=1)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["__typename"] = "Album"
        with self.assertRaises(SpotifyImportError):
            parse_chart_json(data)

    def test_wrong_id_raises(self):
        data = _make_playlist_dict(count=1)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["id"] = "wrong"
        with self.assertRaises(SpotifyImportError):
            parse_chart_json(data)

    def test_wrong_owner_raises(self):
        data = _make_playlist_dict(count=1)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["ownerV2"]["data"]["uri"] = "spotify:user:other"
        with self.assertRaises(SpotifyImportError):
            parse_chart_json(data)

    def test_noninitial_page_rejected(self):
        """nextOffset != len(items) means not initial page."""
        data = _make_playlist_dict(count=2)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["pagingInfo"]["nextOffset"] = 99
        with self.assertRaises(SpotifyImportError):
            parse_chart_json(data)

    def test_explicit_offset_rejected(self):
        """content.offset or pagingInfo.offset must be int 0 or absent."""
        for bad in (30, True, "0"):
            for loc in ("content", "pagingInfo"):
                data = _make_playlist_dict(count=2)
                target = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]
                if loc == "content":
                    target["offset"] = bad
                else:
                    target["pagingInfo"]["offset"] = bad
                with self.assertRaises(SpotifyImportError, msg=f"{loc} offset={bad!r}"):
                    parse_chart_json(data)
        # zero accepted
        data = _make_playlist_dict(count=2)
        target = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]
        target["offset"] = 0
        target["pagingInfo"]["offset"] = 0
        parse_chart_json(data)

    def test_empty_items_raises(self):
        data = _make_playlist_dict(count=0)
        data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"] = []
        with self.assertRaises(SpotifyImportError):
            parse_chart_json(data)

    def test_non_dict_raises(self):
        with self.assertRaises(SpotifyImportError):
            parse_chart_json("not a dict")

    def test_surrogate_replacement(self):
        data = _make_playlist_dict(count=1)
        items = data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]["content"]["items"]
        items[0]["itemV2"]["data"]["name"] = "Hi\ud800there"
        items[0]["itemV2"]["data"]["artists"]["items"][0]["profile"]["name"] = "Art\ud801ist"
        snap = parse_chart_json(data)
        self.assertEqual(snap.entries[0].track.title, "Hi\ufffdthere")
        self.assertEqual(snap.entries[0].track.artist, "Art\ufffdist")


@unittest.skipUnless(_GI_AVAILABLE, "GI が使えないため charts テストをスキップ")
class TestParseChartHtml(unittest.TestCase):
    def test_valid_html(self):
        data = _make_playlist_dict(count=2)
        html = _make_html(data)
        snap = parse_chart_html(html)
        self.assertEqual(len(snap.entries), 2)

    def test_invalid_script_raises(self):
        with self.assertRaises(SpotifyImportError):
            parse_chart_html("<html><body>no script</body></html>")

    def test_invalid_base64_raises(self):
        html = '<script id="initialState" type="text/plain">!!!not-base64!!!</script>'
        with self.assertRaises(SpotifyImportError):
            parse_chart_html(html)

    def test_invalid_json_raises(self):
        b64 = base64.b64encode(b"not json").decode()
        html = f'<script id="initialState" type="text/plain">{b64}</script>'
        with self.assertRaises(SpotifyImportError):
            parse_chart_html(html)

    def test_wrong_source_id_raises(self):
        data = _make_playlist_dict(count=1)
        # Change the key to wrong playlist
        data["entities"]["items"] = {"spotify:playlist:WRONG": data["entities"]["items"]["spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"]}
        html = _make_html(data)
        with self.assertRaises(SpotifyImportError):
            parse_chart_html(html)

    def test_single_quote_attributes(self):
        data = _make_playlist_dict(count=1)
        b64 = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        html = f"<script id='initialState' type='text/plain'>{b64}</script>"
        snap = parse_chart_html(html)
        self.assertEqual(len(snap.entries), 1)


@unittest.skipUnless(_GI_AVAILABLE, "GObject introspection not available")
class TestTopCharts(unittest.TestCase):
    """Deterministic tests using fake Thread or controlled Event."""

    def _make_charts(self, fetcher=None):
        return TopCharts(fetcher=fetcher)

    def _fake_thread_patch(self):
        captured = []

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None, **kw):
                self._target = target
                self._started = False
                captured.append(self)

            def start(self):
                self._started = True

            def run(self):
                if self._target:
                    self._target()

        return captured, patch("cliamp_music.charts.threading.Thread", FakeThread)

    def _pump_idle(self, mock_idle):
        for call in mock_idle.call_args_list:
            args = call[0]
            if args:
                args[0](*args[1:])

    def _valid_fetcher(self, count=2):
        data = _make_playlist_dict(count=count)
        html = _make_html(data)
        def fetcher(url, **kw):
            return html
        return fetcher

    def test_fetch_uses_chart_url_and_page_ua(self):
        """Actual fetch must target CHART_URL with PAGE_USER_AGENT."""
        from cliamp_music.spotify_import import PAGE_USER_AGENT
        captured_calls = []
        def spy_fetch(url, **kw):
            captured_calls.append((url, kw))
            return _make_html(_make_playlist_dict(count=1))
        charts = self._make_charts(fetcher=spy_fetch)
        captured, tp = self._fake_thread_patch()
        with tp, patch("cliamp_music.charts.GLib.idle_add", return_value=0) as mock_idle:
            charts.start_fetch()
        captured[0].run()
        self._pump_idle(mock_idle)
        self.assertEqual(len(captured_calls), 1)
        url, kw = captured_calls[0]
        self.assertEqual(url, CHART_URL)
        self.assertEqual(kw.get("user_agent"), PAGE_USER_AGENT)
        charts.stop()

    def test_start_fetch_once(self):
        call_count = 0
        def fake_fetch(url, **kw):
            nonlocal call_count
            call_count += 1
            return _make_html(_make_playlist_dict(count=2))
        charts = self._make_charts(fetcher=fake_fetch)
        captured, tp = self._fake_thread_patch()
        with tp, patch("cliamp_music.charts.GLib.idle_add", return_value=0) as mock_idle:
            charts.start_fetch()
            charts.start_fetch()
            charts.start_fetch()
        self.assertEqual(len(captured), 1)
        self.assertEqual(call_count, 0)
        captured[0].run()
        self._pump_idle(mock_idle)
        self.assertEqual(call_count, 1)
        charts.stop()

    def test_start_fetch_no_more_after_failure(self):
        call_count = 0
        def failing_fetch(url, **kw):
            nonlocal call_count
            call_count += 1
            raise SpotifyImportError("fail")
        charts = self._make_charts(fetcher=failing_fetch)
        captured, tp = self._fake_thread_patch()
        with tp, patch("cliamp_music.charts.GLib.idle_add", return_value=0) as mock_idle:
            charts.start_fetch()
            charts.start_fetch()
        self.assertEqual(len(captured), 1)
        captured[0].run()
        self._pump_idle(mock_idle)
        self.assertEqual(call_count, 1)
        charts.stop()

    def test_stop_ignores_late_completion(self):
        charts = self._make_charts(fetcher=self._valid_fetcher())
        charts.start_fetch()
        charts.stop()
        result = charts._on_complete(1, ChartSnapshot(entries=()), None)
        self.assertFalse(charts.has_data)

    def test_stop_prevents_future_fetch(self):
        charts = self._make_charts(fetcher=self._valid_fetcher())
        charts.stop()
        charts.start_fetch()
        self.assertFalse(charts.fetching)

    def test_exception_containment(self):
        charts = self._make_charts(fetcher=lambda url, **kw: "{}")
        with patch("cliamp_music.charts.threading.Thread") as mock_thread:
            mock_thread.return_value.start.side_effect = RuntimeError("no threads")
            charts.start_fetch()
        self.assertFalse(charts.fetching)
        self.assertIn("取得", charts.error)

    def test_properties_initial_state(self):
        charts = self._make_charts()
        self.assertEqual(charts.entries, ())
        self.assertEqual(charts.tracks, ())
        self.assertEqual(charts.fetched_at, "")
        self.assertFalse(charts.has_data)
        self.assertFalse(charts.fetching)
        self.assertEqual(charts.error, "")

    def test_completion_sets_data_and_fetched_at(self):
        charts = self._make_charts()
        snap = ChartSnapshot(
            entries=(ChartEntry(rank=1, track=Track(path="spotify:track:0000000000000000000001", title="T", artist="A", meta=())),),
        )
        charts._generation = 1
        charts._on_complete(1, snap, None)
        self.assertTrue(charts.has_data)
        self.assertEqual(len(charts.entries), 1)
        # fetched_at is set on success
        self.assertNotEqual(charts.fetched_at, "")
        self.assertRegex(charts.fetched_at, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_failure_keeps_previous_data(self):
        charts = self._make_charts()
        snap = ChartSnapshot(
            entries=(ChartEntry(rank=1, track=Track(path="spotify:track:0000000000000000000001", title="T", artist="A", meta=())),),
        )
        charts._generation = 1
        charts._on_complete(1, snap, None)
        charts._generation = 2
        charts._on_complete(2, None, "network error")
        self.assertTrue(charts.has_data)
        self.assertEqual(charts.error, "network error")

    def test_no_date_attribute(self):
        """ChartSnapshot has no date field."""
        snap = ChartSnapshot(entries=())
        self.assertFalse(hasattr(snap, "date"))


@unittest.skipUnless(_GI_AVAILABLE, "GObject introspection not available")
class TestTopChartsRealWorker(unittest.TestCase):
    """One controlled real-worker integration test with threading.Event."""

    def test_real_worker_fetch_and_completion(self):
        fetch_started = threading.Event()
        fetch_release = threading.Event()
        fetch_thread_id = [None]
        main_thread_id = threading.current_thread().ident
        changed_count = [0]
        completion_thread_ids = []

        html = _make_html(_make_playlist_dict(count=2))

        def blocking_fetcher(url, **kw):
            fetch_thread_id[0] = threading.current_thread().ident
            fetch_started.set()
            fetch_release.wait(timeout=5)
            return html

        charts = TopCharts(fetcher=blocking_fetcher)

        def on_changed(*_a):
            changed_count[0] += 1
            completion_thread_ids.append(threading.current_thread().ident)

        charts.connect("changed", on_changed)

        charts.start_fetch()
        charts.start_fetch()
        charts.start_fetch()

        self.assertTrue(fetch_started.wait(timeout=5), "fetcher never started")
        self.assertEqual(changed_count[0], 1)

        fetch_release.set()

        ctx = GLib.MainContext.default()
        import time
        t0 = time.monotonic()
        while charts.fetching and (time.monotonic() - t0) < 5.0:
            while ctx.pending():
                ctx.iteration(may_block=False)
            GLib.usleep(1000)
        while ctx.pending():
            ctx.iteration(may_block=False)

        self.assertFalse(charts.fetching, "still fetching after pump")
        self.assertTrue(charts.has_data)
        self.assertNotEqual(charts.fetched_at, "")
        self.assertNotEqual(fetch_thread_id[0], main_thread_id)
        self.assertGreaterEqual(changed_count[0], 2)
        for tid in completion_thread_ids:
            self.assertEqual(tid, main_thread_id, "callback not on main thread")
        self.assertEqual(changed_count[0], 2)

        charts.stop()

    def test_real_worker_failure_and_stop_drops_completion(self):
        fetch_started = threading.Event()
        fetch_release = threading.Event()

        def blocking_fail(url, **kw):
            fetch_started.set()
            fetch_release.wait(timeout=5)
            raise SpotifyImportError("boom")

        charts = TopCharts(fetcher=blocking_fail)
        charts.start_fetch()
        self.assertTrue(fetch_started.wait(timeout=5))

        charts.stop()
        fetch_release.set()

        ctx = GLib.MainContext.default()
        import time
        t0 = time.monotonic()
        while (time.monotonic() - t0) < 1.0:
            while ctx.pending():
                ctx.iteration(may_block=False)
            GLib.usleep(1000)
        while ctx.pending():
            ctx.iteration(may_block=False)

        self.assertFalse(charts.has_data)
        self.assertFalse(charts.fetching)


if __name__ == "__main__":
    unittest.main()
