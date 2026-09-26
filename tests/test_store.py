"""store.py の試験。偽の cliamp に本物のクライアントで繋ぎ、GLib の main loop を回す。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, run_loop, temp_socket_path  # noqa: E402

try:
    from gi.repository import GObject

    from cliamp_music import store as store_mod
    from cliamp_music.client import CliampClient
    from cliamp_music.protocol import Source, Status, Track
    from cliamp_music.store import PlayerStore

    HAVE_GI = True
except ImportError:  # pragma: no cover
    HAVE_GI = False


class _Recorder:
    def __init__(self, store):
        self.events: list[str] = []
        for name in ("status-changed", "track-changed", "state-changed", "playlist-changed",
                     "history-changed", "connection-changed"):
            store.connect(name, lambda _s, name=name: self.events.append(name))

    def count(self, name: str) -> int:
        return self.events.count(name)


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class StoreWithFake(unittest.TestCase):
    legacy = False

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path, legacy=self.legacy).start()
        self.client = CliampClient(self.path)
        self.client.BACKOFF_MIN = 0.1
        self.client.BACKOFF_MAX = 0.3
        self.client.set_poll_interval(0.05)
        self.store = PlayerStore(self.client)
        self.store.HISTORY_DELAY_MS = 50
        self.rec = _Recorder(self.store)
        self.client.start()
        self.assertTrue(run_loop(lambda: self.store.status.state != "offline"))

    def tearDown(self):
        self.client.stop()
        self.fake.stop()

    def wait(self, until, timeout=3.0):
        self.assertTrue(run_loop(until, timeout=timeout))

    def cmds(self, name):
        return self.fake.requests_for(name)


class Basics(StoreWithFake):
    def test_initial_state(self):
        self.assertTrue(self.store.connected)
        self.assertEqual(self.store.api, 1)
        self.assertEqual(self.store.status.state, "playing")
        self.assertEqual(self.store.current_track().title, "シティライト・ブルース")
        self.wait(lambda: len(self.store.playlist.tracks) == 12)
        self.assertEqual(self.store.playlist.index, 2)
        self.assertEqual(self.store.playlist.source, Source("local", "ドライブ", "ドライブ"))
        self.wait(lambda: self.store.history)
        self.assertTrue(self.store.history[0].played_at)
        self.assertIn("connection-changed", self.rec.events)
        self.assertIn("Rock", self.store.eq_presets)
        # 接続直後のリストの取得は 1 回だけ。
        run_loop(lambda: False, timeout=0.3)
        self.assertEqual(len(self.cmds("playlist")), 1)

    def test_supports(self):
        self.assertTrue(self.store.supports("replace"))
        self.assertTrue(self.store.supports("toggle"))
        self.assertFalse(self.store.supports("no_such_command"))

    def test_position_interpolates_while_playing(self):
        first = self.store.position_now()
        run_loop(lambda: False, timeout=0.3)
        self.assertGreater(self.store.position_now(), first + 0.2)
        self.assertLess(self.store.position_now(), first + 1.5)

    def test_toggle_is_optimistic_and_sticks(self):
        """押した瞬間に表示が変わり、その後に届く古い status で戻らない。"""
        self.store.toggle()
        self.assertEqual(self.store.status.state, "paused")
        self.assertIn("state-changed", self.rec.events)
        seen = []
        self.store.connect("status-changed", lambda _s: seen.append(self.store.status.state))
        self.wait(lambda: len(seen) >= 6)
        self.assertEqual(set(seen), {"paused"})
        self.assertEqual(self.fake.state, "paused")
        self.store.toggle()
        self.assertEqual(self.store.status.state, "playing")
        self.wait(lambda: self.fake.state == "playing")

    def test_play_when_stopped_sends_toggle(self):
        """TUI の play は停止中に何もしないので toggle を送る。"""
        self.store.stop()
        self.wait(lambda: self.fake.state == "stopped")
        self.wait(lambda: self.store.status.state == "stopped" and not self.store._overrides)
        before_play = len(self.cmds("play"))
        self.store.play()
        self.wait(lambda: self.fake.state == "playing")
        self.assertEqual(len(self.cmds("play")), before_play)
        self.assertTrue(self.cmds("toggle"))
        # 一時停止中の play は play を送る。
        self.store.pause()
        self.wait(lambda: self.fake.state == "paused")
        self.wait(lambda: not self.store._overrides)
        self.store.play()
        self.wait(lambda: self.fake.state == "playing")
        self.assertEqual(len(self.cmds("play")), before_play + 1)

    def test_seek_uses_seek_to_only(self):
        self.store.seek_to(100)
        self.assertAlmostEqual(self.store.position_now(), 100, delta=0.2)
        self.wait(lambda: abs(self.fake.position() - 100) < 1.0)
        self.assertEqual(self.cmds("seek_to")[-1]["value"], 100)
        self.store.seek_by(-30)
        self.wait(lambda: abs(self.fake.position() - 70) < 1.5)
        self.assertEqual(self.cmds("seek"), [])
        self.store.seek_to(10_000)  # 長さを超えたら曲の長さで止める
        self.wait(lambda: self.cmds("seek_to")[-1]["value"] == 245)

    def test_volume_is_absolute_db_and_coalesced(self):
        for db in (-10, -12, -14, -16, -18, -20):
            self.store.set_volume_db(db)
        self.assertEqual(self.store.status.volume, -20)
        self.wait(lambda: self.fake.volume == -20)
        self.assertLess(len(self.cmds("volume")), 6)  # 送信中の値は最後だけ送る
        self.store.set_volume_db(40)
        self.assertEqual(self.store.status.volume, 6.0)
        self.wait(lambda: self.fake.volume == 6.0)
        self.store.volume_step(-2)
        self.wait(lambda: self.fake.volume == 4.0)
        self.wait(lambda: not self.store._overrides)
        self.assertEqual(self.store.status.volume, 4.0)

    def test_shuffle_repeat_speed(self):
        self.store.set_shuffle(True)
        self.assertTrue(self.store.status.shuffle)
        self.wait(lambda: self.fake.pl.shuffle)
        self.store.set_repeat("One")
        self.assertEqual(self.store.status.repeat, "one")
        self.wait(lambda: self.fake.pl.repeat == "one")
        self.store.cycle_repeat()
        self.wait(lambda: self.fake.pl.repeat == "off")
        self.store.cycle_repeat()
        self.wait(lambda: self.fake.pl.repeat == "all")
        with self.assertRaises(ValueError):
            self.store.set_repeat("sometimes")
        self.store.set_speed(5)
        self.assertEqual(self.store.status.speed, 2.0)
        self.wait(lambda: self.fake.speed == 2.0)

    def test_eq(self):
        self.store.set_eq_preset("Rock")
        self.wait(lambda: self.store.status.eq[0] == 5 and not self.store._overrides)
        self.store.set_eq_band(0, -3)
        self.store.set_eq_band(9, 20)
        self.assertEqual(self.store.status.eq[0], -3)
        self.assertEqual(self.store.status.eq[9], 12)
        self.assertEqual(self.store.status.eq_preset, "Custom")
        self.wait(lambda: self.fake.eq[0] == -3 and self.fake.eq[9] == 12)
        band0 = [r for r in self.cmds("eq") if r.get("band") == 0 and "name" not in r]
        self.assertTrue(band0)  # 0 番の帯域も band=0 で送る

    def test_next_changes_track_and_refreshes(self):
        self.wait(lambda: self.store.playlist.tracks)
        self.wait(lambda: self.cmds("history"))
        run_loop(lambda: False, timeout=0.2)
        before = len(self.cmds("playlist"))
        history_before = len(self.cmds("history"))
        tracks_changed = self.rec.count("track-changed")
        self.store.next()
        self.wait(lambda: self.store.status.index == 3)
        self.assertEqual(self.rec.count("track-changed"), tracks_changed + 1)
        self.wait(lambda: len(self.cmds("playlist")) > before)  # up_next が変わるので取り直す
        self.wait(lambda: len(self.cmds("history")) > history_before)  # 少し遅らせて履歴も

    def test_replace_triggers_playlist_changed(self):
        tracks = [Track(path=f"https://www.youtube.com/watch?v=abcdefghi{i:02d}", title=f"曲 {i}",
                        meta={"art": "https://x/a.jpg"}) for i in range(5)]
        results = []
        self.store.replace(tracks, 3, Source("youtube", "q", "検索"), callback=results.append)
        self.wait(lambda: results and self.store.playlist.total == 5)
        self.assertTrue(results[0].ok)
        self.assertEqual(self.store.playlist.tracks[3], tracks[3])
        self.assertEqual(self.store.playlist.source.name, "検索")
        self.wait(lambda: self.store.status.index == 3)
        self.assertEqual(self.store.current_track(), tracks[3])

    def test_queue_edit_and_clear_with_omitempty(self):
        self.wait(lambda: self.store.playlist.tracks)
        done = []
        self.store.queue_edit("add", index=7, callback=done.append)
        self.wait(lambda: done)
        self.assertEqual(self.store.playlist.queue, [7])
        self.wait(lambda: self.store.playlist.track_at(7) and self.store.playlist.track_at(7).queued == 1)
        self.assertNotIn(7, self.store.playlist.up_next)
        self.store.queue_edit("clear", callback=done.append)
        self.wait(lambda: len(done) == 2)
        self.assertEqual(self.store.playlist.queue, [])  # 空の queue は省かれて届く

    def test_enqueue_and_remove(self):
        self.wait(lambda: self.store.playlist.tracks)
        done = []
        self.store.enqueue([Track(path="https://www.youtube.com/watch?v=zzzzzzzzzzz", title="足した曲")],
                           "end", callback=done.append)
        self.wait(lambda: done and self.store.playlist.total == 13)
        self.store.remove(0, callback=done.append)
        self.wait(lambda: len(done) == 2 and self.store.playlist.total == 12)
        with mock.patch.object(store_mod, "log"):
            self.store.remove(self.store.status.index, callback=done.append)
            self.wait(lambda: len(done) == 3)
        self.assertFalse(done[2].ok)  # 再生中の曲は消せない

    def test_play_index(self):
        self.store.play_index(5)
        self.wait(lambda: self.store.status.index == 5)
        self.assertEqual(self.store.status.state, "playing")

    def list_devices(self):
        got = []
        self.store.list_devices(lambda devices, error: got.append((devices, error)))
        self.wait(lambda: got)
        return got[0]

    def test_devices(self):
        devices, error = self.list_devices()
        self.assertEqual(error, "")
        self.assertEqual([d.name for d in devices], ["alsa_output.pci-0000_0c_00.4.analog-stereo",
                                                     "alsa_output.pci-0000_03_00.1.hdmi-stereo",
                                                     "bluez_output.AC_80_0A_12_34_56.1"])
        # 説明の無い cliamp: sink 名から見出しを作る。切り替えには sink 名を送る
        self.assertEqual([d.label for d in devices], ["アナログ出力", "HDMI / DisplayPort", "Bluetooth (AC:80:0A:12:34:56)"])
        self.assertEqual([d.active for d in devices], [True, False, False])
        done = []
        self.store.set_device(devices[2].name, callback=done.append)
        self.wait(lambda: done and self.fake.device == 2)
        self.assertEqual(self.cmds("device")[-1]["name"], "bluez_output.AC_80_0A_12_34_56.1")
        # cliamp の "* " は既定の sink のまま (move-sink-input で動かすため)。選んだ出力先に印を付ける
        devices, _ = self.list_devices()
        self.assertEqual([d.active for d in devices], [False, False, True])

    def test_device_descriptions_are_preferred(self):
        with self.fake.lock:
            self.fake.device_descriptions = True
        devices, _ = self.list_devices()
        self.assertEqual(devices[1].label, "Navi 32 HDMI/DP Audio デジタルステレオ (HDMI)")
        self.assertEqual(devices[1].name, "alsa_output.pci-0000_03_00.1.hdmi-stereo")

    def test_device_error_is_explained(self):
        from cliamp_music.protocol import Response

        with mock.patch.object(self.client, "request",
                               lambda cmd, cb, **kw: cb(Response(False, {}, 'list devices: pactl: exec: "pactl": '
                                                                 'executable file not found in $PATH', "error"))):
            got = []
            self.store.list_devices(lambda devices, error: got.append((devices, error)))
        self.assertEqual(got[0][0], [])
        self.assertIn("pactl", got[0][1])

    def test_gen_change_from_elsewhere_refreshes(self):
        """TUI などが別にリストを変えても gen の変化で取り直す。"""
        self.wait(lambda: len(self.store.playlist.tracks) == 12)
        with self.fake.lock:
            self.fake.pl.add({"path": "/x/new.flac", "title": "TUI で足した"})
        self.wait(lambda: len(self.store.playlist.tracks) == 13)
        self.assertIn("playlist-changed", self.rec.events)

    def test_disconnect_goes_offline_and_back(self):
        self.fake.stop()
        self.wait(lambda: self.store.status.state == "offline")
        self.assertFalse(self.store.connected)
        self.assertIsNone(self.store.current_track())
        self.fake.start()
        self.wait(lambda: self.store.status.state == "playing", timeout=5)
        self.assertTrue(self.store.connected)


class Ordering(StoreWithFake):
    def test_back_to_back_queue_edits_keep_order(self):
        self.wait(lambda: self.store.playlist.tracks)
        for trial in range(20):
            self.store.queue_edit("clear")
            self.wait(lambda: not self.fake.pl.queue)
            done = []
            for i in (4, 5, 6, 7):
                self.store.queue_edit("add", index=i, callback=done.append)
            self.wait(lambda: len(done) == 4)
            self.assertEqual(self.fake.pl.queue, [4, 5, 6, 7], f"{trial} 回目")

    def test_preset_then_band_in_one_iteration(self):
        self.store.set_eq_preset("Rock")
        self.store.set_eq_band(0, 3.0)
        self.wait(lambda: self.fake.eq[0] == 3.0 and self.fake._preset_name() == "Custom")
        self.wait(lambda: not self.store._overrides)
        self.assertEqual(self.store.status.eq_preset, "Custom")
        self.assertEqual(self.store.status.eq[0], 3.0)
        self.assertEqual(self.store.status.eq[1], 4.0)  # Rock の 2 番目の帯域は残る

    def test_pause_is_not_stuck_behind_slow_searches(self):
        """遅い検索が 4 本あっても pause はすぐ届き、表示が playing に戻らない。"""
        from cliamp_music.catalog import Catalog

        catalog = Catalog(self.client)
        self.fake.delays["search"] = 4.0
        for query in ("yo", "yoa", "yoaso", "yoasobi"):
            catalog.search("youtube", query, lambda r: None)
        run_loop(lambda: False, timeout=0.1)
        seen = []
        self.store.connect("status-changed", lambda _s: seen.append(self.store.status.state))
        self.store.pause()
        self.wait(lambda: self.cmds("pause"), timeout=1.0)
        self.wait(lambda: self.fake.state == "paused")
        run_loop(lambda: False, timeout=1.0)
        self.assertNotIn("playing", seen)


class Refreshing(StoreWithFake):
    def test_failed_playlist_fetch_is_retried(self):
        """リストの取得が 1 度失敗しても、gen や曲が変わらないまま取り直す。"""
        self.wait(lambda: self.store.playlist.tracks)
        original = self.fake._cmd_playlist
        failures = []

        def flaky(req, now):
            if not failures:
                failures.append(req)
                return {"ok": False, "error": "timeout"}
            return original(req, now)

        with mock.patch.object(store_mod, "log"):
            self.store.RETRY_MIN_MS = 100
            with self.fake.lock:
                self.fake._cmd_playlist = flaky
                self.fake.pl.add({"path": "/x/new.flac", "title": "TUI で足した"})
            self.wait(lambda: failures)
            self.wait(lambda: len(self.store.playlist.tracks) == 13, timeout=4)
        self.assertEqual(self.store.playlist.total, 13)

    def test_failed_history_fetch_is_retried(self):
        self.wait(lambda: self.store.history)
        original = self.fake._cmd_history
        failures = []

        def flaky(req, now):
            if not failures:
                failures.append(req)
                return {"ok": False, "error": "read history.toml: busy"}
            return original(req, now)

        with mock.patch.object(store_mod, "log"):
            self.store.RETRY_MIN_MS = 100
            self.store.history = []
            with self.fake.lock:
                self.fake._cmd_history = flaky
            self.store.refresh_history()
            self.wait(lambda: failures)
            self.wait(lambda: len(self.store.history) == len(self.fake.history), timeout=4)

    def test_history_refresh_while_one_is_running_is_not_lost(self):
        self.wait(lambda: self.store.history)
        self.fake.delays["history"] = 0.3
        before = len(self.cmds("history"))
        self.store.refresh_history()
        self.store.refresh_history()
        self.wait(lambda: len(self.cmds("history")) == before + 2, timeout=3)

    def test_track_change_fetches_only_one_track(self):
        """gen が同じ (曲の中身が変わっていない) なら limit=1 で位置と次の曲だけを取り直し、
        曲の並びは同じものを使い続ける。"""
        self.wait(lambda: len(self.store.playlist.tracks) == 12)
        tracks = self.store.playlist.tracks
        self.fake.requests.clear()
        self.store.play_index(6)
        self.wait(lambda: self.store.playlist.index == 6)
        light = [r for r in self.cmds("playlist") if r.get("limit") == 1]
        self.assertTrue(light)
        self.assertEqual([r for r in self.cmds("playlist") if not r.get("limit")], [])
        self.assertIs(self.store.playlist.tracks, tracks)
        self.assertEqual(self.store.playlist.up_next[:2], [7, 8])
        # 中身が変わった (待ち行列) ときは全部を取り直す
        self.store.queue_edit("add", index=9)
        self.wait(lambda: self.store.playlist.track_at(9) and self.store.playlist.track_at(9).queued == 1)


class Switching(unittest.TestCase):
    """yt-dlp・流れの曲への切り替え: 本物の TUI は読み込みの間も前の曲の位置と長さを返す。"""

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path, buffer_secs=1.2, switch_keeps_old=True).start()
        self.client = CliampClient(self.path)
        self.client.set_poll_interval(0.05)
        self.store = PlayerStore(self.client)
        self.client.start()
        self.assertTrue(run_loop(lambda: self.store.status.state == "playing" and not self.store.status.buffering,
                                 timeout=4))
        self.store.seek_to(100)
        self.assertTrue(run_loop(lambda: abs(self.fake.position() - 100) < 2 and not self.store._overrides))

    def tearDown(self):
        self.client.stop()
        self.fake.stop()

    def test_position_and_duration_during_switch(self):
        next_track = self.fake.pl.tracks[3]
        self.store.next()
        self.assertTrue(run_loop(lambda: self.store.status.buffering and self.store.status.track
                                 and self.store.status.track.path == next_track["path"], timeout=2))
        raw = self.fake.dispatch({"cmd": "status"})
        self.assertEqual(raw["state"], "playing")
        self.assertGreater(raw["position"], 90)  # 本物と同じく前の曲の位置を返している
        self.assertTrue(self.store.switching)
        self.assertEqual(self.store.position_now(), 0.0)
        self.assertEqual(self.store.status.duration, float(next_track["duration"]))
        self.assertFalse(self.store.can_seek())
        before = len(self.fake.requests_for("seek_to"))
        self.store.seek_to(30)
        self.store.seek_by(10)
        self.assertEqual(len(self.fake.requests_for("seek_to")), before)
        self.assertTrue(run_loop(lambda: not self.store.status.buffering, timeout=3))
        self.assertFalse(self.store.switching)
        self.assertTrue(self.store.can_seek())
        self.assertLess(self.store.position_now(), 2.0)

    def test_tui_buffering_on_same_track_is_not_a_switch(self):
        """TUI が URL やフィードを読む間も buffering は立つが、今の曲は正しく鳴っている。"""
        from cliamp_music.protocol import Status

        status = Status(state="playing", track=self.store.status.track, position=50.0, duration=245.0,
                        index=self.store.status.index, buffering=True)
        status.seq = self.client.last_status_seq
        self.store._on_status(self.client, status)
        self.assertFalse(self.store.switching)
        self.assertEqual(self.store.status.position, 50.0)


class SeekSettle(StoreWithFake):
    def test_async_stream_seek_does_not_snap_back(self):
        """HTTP の流れのシークは応答の後で効く。その間の古い位置で表示を戻さない。"""
        self.fake.seek_delay = 0.8
        seen = []
        self.store.connect("status-changed", lambda _s: seen.append(self.store.position_now()))
        self.store.seek_to(150)
        self.wait(lambda: self.cmds("seek_to"))
        self.wait(lambda: abs(self.fake.position() - 150) < 1.5, timeout=3)
        run_loop(lambda: False, timeout=0.3)
        self.assertTrue(seen)
        self.assertTrue(all(pos > 140 for pos in seen), seen)
        self.wait(lambda: "position" not in self.store._overrides)
        self.assertAlmostEqual(self.store.position_now(), self.fake.position(), delta=1.0)

    def test_seek_that_never_lands_gives_up(self):
        self.store.SEEK_SETTLE = 0.4
        with self.fake.lock:
            self.fake._cmd_seek_to = lambda req, now: {"ok": True}  # 受け付けるが動かない
        start = self.fake.position()
        self.store.seek_to(200)
        self.wait(lambda: "position" not in self.store._overrides, timeout=3)
        self.assertLess(abs(self.store.position_now() - self.fake.position()), 1.0)
        self.assertLess(self.store.position_now(), start + 5)


class StaleIndex(StoreWithFake):
    def test_stale_index_refetches_the_list(self):
        """添字の曲が見ていた曲と違えば (TUI でリストが動いた)、cliamp は断り、store は取り直す。"""
        self.wait(lambda: len(self.store.playlist.tracks) == 12)
        full = lambda: [r for r in self.cmds("playlist") if not r.get("limit")]  # noqa: E731
        before = len(full())
        got = []
        with mock.patch.object(store_mod, "log"):
            self.store.play_index(3, callback=got.append, path=self.store.playlist.tracks[2].path)
            self.wait(lambda: got)
        self.assertEqual(got[0].error, "stale")
        self.assertEqual(self.fake.pl.index(), 2)
        self.wait(lambda: len(full()) > before)
        self.assertEqual(self.cmds("play_index")[-1]["path"], self.store.playlist.tracks[2].path)


class QueuedPlay(StoreWithFake):
    def test_play_queued_skips_ahead_and_resumes_the_list(self):
        """待ち行列の曲を選ぶと、その前の待ち行列を外して鳴らす。同じ曲は 2 度鳴らず、
        リストはもとの位置から続く (play_index では曲が 2 度鳴り、間の曲が飛ぶ)。"""
        self.wait(lambda: self.store.playlist.index == 2 and self.store.playlist.tracks)
        self.store.set_repeat("off")
        for index in (5, 7):
            done = []
            self.store.queue_edit("add", index=index, callback=done.append)
            self.wait(lambda: done)
        self.wait(lambda: self.store.playlist.queue == [5, 7])
        done = []
        self.store.play_queued(7, callback=done.append)
        self.wait(lambda: done and self.fake.pl.index() == 7)
        self.assertTrue(done[0].ok)
        self.assertEqual(self.fake.pl.queue, [])
        self.assertEqual(self.cmds("play_index"), [])
        order = []
        for _ in range(2):
            self.fake.dispatch({"cmd": "next"})
            order.append(self.fake.pl.index())
        self.assertEqual(order, [3, 4])

    def test_reshuffle_continuation(self):
        self.wait(lambda: self.store.playlist.tracks)
        self.store.set_shuffle(True)
        self.store.set_repeat("all")
        self.wait(lambda: self.store.status.shuffle and self.store.status.repeat == "all"
                  and not self.store._overrides)
        self.assertTrue(self.store.continues_by_reshuffle())
        self.store.set_repeat("off")
        self.wait(lambda: self.store.status.repeat == "off" and not self.store._overrides)
        self.assertFalse(self.store.continues_by_reshuffle())


class Legacy(StoreWithFake):
    legacy = True

    def test_basic_only(self):
        self.assertEqual(self.store.api, 0)
        self.assertTrue(self.store.supports("toggle"))
        self.assertFalse(self.store.supports("replace"))
        self.assertFalse(self.store.supports("seek_to"))
        self.wait(lambda: self.store.playlist.total == 12)
        self.assertEqual(self.store.playlist.tracks, [])
        self.assertEqual(self.store.playlist.index, 2)
        self.store.seek_to(30)  # 拡張なしでは送らない (相対 seek にも逃げない)
        self.store.toggle()
        self.wait(lambda: self.fake.state == "paused")
        self.assertEqual(self.cmds("seek_to"), [])
        self.assertEqual(self.cmds("seek"), [])
        self.assertEqual(self.cmds("playlist"), [])
        self.assertEqual(self.cmds("history"), [])


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class Interpolation(unittest.TestCase):
    """位置の補間だけを、時計を差し替えて確かめる。"""

    class _Client(GObject.Object):
        __gsignals__ = {
            "connection-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
            "status": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        }
        connected = True
        api = 1
        capabilities: dict = {}
        last_status_seq = 0

        def request(self, *args, **kwargs):
            pass

        def poll_now(self):
            pass

    def setUp(self):
        self.client = self._Client()
        self.store = PlayerStore(self.client)
        self.now = 1000.0
        patcher = mock.patch.object(store_mod.time, "monotonic", lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def feed(self, **fields):
        status = Status(**{"state": "playing", "duration": 200.0, "track": Track(path="x"), **fields})
        status.stamp = self.now
        status.seq = self.client.last_status_seq = self.client.last_status_seq + 1
        self.client.emit("status", status)

    def test_playing_advances_with_speed_and_clamps(self):
        self.feed(position=10.0, speed=1.5)
        self.now += 2.0
        self.assertAlmostEqual(self.store.position_now(), 13.0)
        self.now += 1000
        self.assertEqual(self.store.position_now(), 200.0)

    def test_paused_and_buffering_do_not_advance(self):
        self.feed(position=10.0, state="paused")
        self.now += 5
        self.assertEqual(self.store.position_now(), 10.0)
        self.feed(position=10.0, buffering=True)
        self.now += 5
        self.assertEqual(self.store.position_now(), 10.0)

    def test_live_is_not_clamped(self):
        self.feed(position=10.0, duration=0.0, track=Track(path="x", live=True))
        self.now += 50
        self.assertEqual(self.store.position_now(), 60.0)

    def test_stale_status_does_not_undo_seek(self):
        """シークの応答より前に送られた status では位置を戻さない。後の status で本物に合わせる。"""
        self.feed(position=10.0)
        self.store.seek_to(150)
        self.now += 1
        self.feed(position=11.0)  # シーク前の古い状態
        self.assertAlmostEqual(self.store.position_now(), 151.0)
        # 応答が来た (この時点で送った status の通し番号を覚える)
        self.store._overrides["position"].ack_seq = self.client.last_status_seq
        self.feed(position=149.5)
        self.assertAlmostEqual(self.store.position_now(), 149.5)
        self.assertNotIn("position", self.store._overrides)


if __name__ == "__main__":
    unittest.main()


class FakeBehaviour(unittest.TestCase):
    """偽の cliamp 自体の振る舞い (他の試験や撮影がこれに頼るので確かめておく)。"""

    def setUp(self):
        self.fake = FakeCliamp(temp_socket_path())

    def call(self, cmd, **fields):
        return self.fake.dispatch({"cmd": cmd, **fields})

    def test_track_end_advances_and_records_history(self):
        history = len(self.fake.history)
        self.call("seek_to", value=244.7)
        run_loop(lambda: self.call("status")["index"] == 3, timeout=2)
        status = self.call("status")
        self.assertEqual(status["index"], 3)
        self.assertLess(status["position"], 1.5)
        self.assertEqual(len(self.fake.history), history + 1)
        self.assertEqual(self.fake.history[0][0]["title"], "シティライト・ブルース")

    def test_end_of_list_stops_without_repeat(self):
        self.call("repeat", name="off")
        self.call("play_index", index=11)
        self.call("seek_to", value=10_000)
        status = self.call("status")
        self.assertEqual(status["state"], "stopped")

    def test_queue_and_up_next(self):
        self.call("queue_edit", mode="add", index=9)
        self.call("queue_edit", mode="add", index=5)
        playlist = self.call("playlist")
        self.assertEqual(playlist["queue"], [9, 5])
        self.assertNotIn(9, playlist["up_next"])
        self.assertEqual(playlist["up_next"][:2], [3, 4])
        self.assertEqual(playlist["tracks"][9]["queued"], 1)
        self.assertEqual(self.call("queue_edit", mode="move", index=1, to=0)["queue"], [5, 9])
        self.call("next")
        self.assertEqual(self.call("status")["index"], 5)  # 待ち行列が先

    def test_replace_with_shuffle_starts_at_index(self):
        self.call("shuffle", name="on")
        tracks = [{"path": f"https://www.youtube.com/watch?v=abcdefghi{i:02d}", "title": str(i)} for i in range(10)]
        reply = self.call("replace", tracks=tracks, index=7, source={"provider": "youtube", "id": "q"})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.call("status")["index"], 7)
        self.assertEqual(len(self.call("playlist")["up_next"]), 9)

    def test_legacy_rejects_new_commands(self):
        legacy = FakeCliamp(temp_socket_path(), legacy=True)
        self.assertEqual(legacy.dispatch({"cmd": "seek_to", "value": 3}),
                         {"ok": False, "error": "unknown command: seek_to"})
        status = legacy.dispatch({"cmd": "status"})
        self.assertNotIn("api", status)
        self.assertEqual(set(status["track"]), {"title", "artist", "path"})

    def test_play_does_nothing_when_stopped(self):
        self.call("stop")
        self.call("play")
        self.assertEqual(self.call("status")["state"], "stopped")
        self.call("toggle")
        self.assertEqual(self.call("status")["state"], "playing")
