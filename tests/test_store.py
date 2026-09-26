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

    def test_devices(self):
        got = []
        self.store.list_devices(got.append)
        self.wait(lambda: got)
        self.assertEqual(got[0][0], ("既定の出力", True))
        self.assertEqual(len(got[0]), 3)
        self.store.set_device(got[0][1][0])
        self.wait(lambda: self.fake.device == 1)

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
