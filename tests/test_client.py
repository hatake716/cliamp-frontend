"""client.py の試験。偽の cliamp (fake_cliamp.py) に本物のソケットで繋ぐ。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, run_loop, temp_socket_path  # noqa: E402

try:
    from cliamp_music import client as client_mod
    from cliamp_music.client import CliampClient, default_socket_path, send_once
    from cliamp_music.protocol import Track

    HAVE_GI = True
except ImportError:  # pragma: no cover
    HAVE_GI = False


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class SocketPath(unittest.TestCase):
    def test_default_is_home_config_not_xdg(self):
        """cliamp は os.UserHomeDir()/.config/cliamp を使い XDG_CONFIG_HOME を見ない。"""
        env = {"HOME": "/home/someone", "XDG_CONFIG_HOME": "/elsewhere"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("CLIAMP_MUSIC_SOCKET", None)
            self.assertEqual(default_socket_path(), "/home/someone/.config/cliamp/cliamp.sock")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"CLIAMP_MUSIC_SOCKET": "/tmp/x.sock"}):
            self.assertEqual(CliampClient().socket_path, "/tmp/x.sock")
            self.assertEqual(CliampClient("/tmp/explicit.sock").socket_path, "/tmp/explicit.sock")


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class OneShot(unittest.TestCase):
    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path).start()

    def tearDown(self):
        self.fake.stop()

    def test_request_delivers_on_main_loop(self):
        client = CliampClient(self.path)
        got = []
        client.request("status", got.append)
        self.assertTrue(run_loop(lambda: got))
        self.assertTrue(got[0].ok)
        self.assertEqual(got[0].data["state"], "playing")

    def test_large_response_is_read_fully(self):
        """数 MB の応答 (長いリスト) も途中で切らずに読む。要求も 64 KiB を超える。"""
        tracks = [Track(path=f"https://www.youtube.com/watch?v=v{i:010d}", title="長い曲名 " * 20 + str(i),
                        artist="アーティスト " * 5, album="アルバム", duration=200) for i in range(6000)]
        replaced = send_once(self.path, "replace", tracks=tracks, index=0)
        self.assertTrue(replaced.ok, replaced.error)
        client = CliampClient(self.path)
        got = []
        client.request("playlist", got.append)
        self.assertTrue(run_loop(lambda: got, timeout=20))
        self.assertTrue(got[0].ok, got[0].error)
        self.assertEqual(len(got[0].data["tracks"]), 6000)
        self.assertEqual(got[0].data["tracks"][-1]["title"], "長い曲名 " * 20 + "5999")

    def test_offline(self):
        response = send_once(self.path + ".missing", "status")
        self.assertEqual((response.ok, response.kind), (False, "offline"))

    def test_timeout(self):
        self.fake.delays["search"] = 1.0
        response = send_once(self.path, "search", timeout=0.3, provider="youtube", query="x")
        self.assertEqual(response.kind, "timeout")

    def test_default_timeouts(self):
        self.assertEqual(client_mod.default_timeout("status"), 5.0)
        self.assertEqual(client_mod.default_timeout("search"), 130.0)

    def test_parallel_requests_do_not_queue_behind_catalog(self):
        """カタログ系の遅い要求があっても、状態系の要求はすぐ返る (1 要求 1 接続)。"""
        self.fake.delays["search"] = 1.5
        client = CliampClient(self.path)
        slow, fast = [], []
        client.request("search", slow.append, provider="youtube", query="遅い")
        client.request("status", fast.append)
        self.assertTrue(run_loop(lambda: fast, timeout=1.0))
        self.assertFalse(slow)
        self.assertTrue(run_loop(lambda: slow, timeout=3.0))

    def test_legacy_unknown_command(self):
        self.fake.stop()
        legacy = FakeCliamp(self.path, legacy=True).start()
        try:
            response = send_once(self.path, "playlist")
            self.assertEqual(response.kind, "unsupported")
            self.assertTrue(send_once(self.path, "status").ok)
        finally:
            legacy.stop()


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class Lanes(unittest.TestCase):
    """要求を送る 3 つの列 (順番・状態の読み取り・カタログ)。"""

    def setUp(self):
        self.path = temp_socket_path()
        self.fake = FakeCliamp(self.path).start()
        self.client = CliampClient(self.path)

    def tearDown(self):
        self.fake.stop()

    def test_state_commands_reach_cliamp_in_call_order(self):
        """cliamp は接続ごとの goroutine で処理するので、別々の接続で続けて送ると順が入れ替わる。
        状態を変える要求は 1 本の列で、前の応答を待ってから次を送る。"""
        for trial in range(20):
            send_once(self.path, "queue_edit", mode="clear")
            self.fake.requests.clear()
            got = []
            for i in (1, 2, 3, 4):  # main loop を回さずに続けて頼む
                self.client.request("queue_edit", got.append, mode="add", index=i)
            self.assertTrue(run_loop(lambda: len(got) == 4))
            self.assertEqual(send_once(self.path, "playlist").data["queue"], [1, 2, 3, 4], f"{trial} 回目")
            self.assertEqual([r["index"] for r in self.fake.requests_for("queue_edit")], [1, 2, 3, 4])

    def test_slow_state_command_holds_the_next_one_but_not_reads(self):
        self.fake.delays["pause"] = 0.8
        order = []
        self.client.request("pause", lambda r: order.append("pause"))
        self.client.request("play", lambda r: order.append("play"))
        self.client.request("playlist", lambda r: order.append("playlist"))
        self.assertTrue(run_loop(lambda: len(order) == 3, timeout=4))
        self.assertEqual(order, ["playlist", "pause", "play"])

    def test_state_commands_do_not_wait_behind_catalog(self):
        """遅いカタログ系 (検索) が 4 本の worker を埋めても、pause はすぐ届く。"""
        self.fake.delays["search"] = 3.0
        slow = []
        for query in ("yo", "yoa", "yoaso", "yoasobi", "yoasobi 夜"):
            self.client.request("search", slow.append, provider="youtube", query=query)
        run_loop(lambda: False, timeout=0.1)
        got = []
        self.client.request("pause", got.append)
        self.assertTrue(run_loop(lambda: got, timeout=1.0), "pause が検索の後ろに並んだ")
        self.assertTrue(got[0].ok)
        self.assertEqual(self.fake.state, "paused")

    def test_queued_state_command_times_out_from_submit(self):
        """順番の列で待つうちに時間切れになったものは送らない (遅れて効かせない)。"""
        self.fake.delays["pause"] = 0.6
        got = []
        self.client.request("pause", got.append, timeout=5.0)
        self.client.request("toggle", got.append, timeout=0.2)
        self.assertTrue(run_loop(lambda: len(got) == 2, timeout=3))
        self.assertEqual(got[1].kind, "timeout")
        self.assertEqual(self.fake.requests_for("toggle"), [])

    def test_superseded_search_is_not_sent(self):
        """同じ lane の新しい検索が来たら、まだ送っていない古い検索は送らずに cancelled で返す。"""
        got = {}
        # カタログの worker を全部ふさいでから lane 付きの検索を 2 つ頼む
        self.fake.delays["lyrics"] = 0.5
        for i in range(client_mod.MAX_WORKERS):
            self.client.request("lyrics", lambda r: None, artist="x", title=f"t{i}")
        self.client.request("search", lambda r: got.setdefault("old", r), lane="page", provider="youtube",
                            query="古い語")
        self.client.request("search", lambda r: got.setdefault("new", r), lane="page", provider="youtube",
                            query="新しい語")
        self.assertTrue(run_loop(lambda: len(got) == 2, timeout=5))
        self.assertEqual(got["old"].kind, "cancelled")
        self.assertTrue(got["new"].ok)
        self.assertEqual([r["query"] for r in self.fake.requests_for("search")], ["新しい語"])

    def test_parse_runs_on_worker(self):
        import threading

        seen = []

        def parse(response):
            seen.append(threading.current_thread().name)
            return len(response.data.get("tracks") or [])

        got = []
        self.client.request("playlist", lambda r, n: got.append(n), parse=parse)
        self.assertTrue(run_loop(lambda: got))
        self.assertEqual(got, [12])
        self.assertNotEqual(seen[0], threading.main_thread().name)


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class Polling(unittest.TestCase):
    def setUp(self):
        self.path = temp_socket_path()
        self.fake = None
        self.client = CliampClient(self.path)
        self.client.BACKOFF_MIN = 0.1
        self.client.BACKOFF_MAX = 0.3
        self.client.set_poll_interval(0.05)
        self.events: list[bool] = []
        self.statuses = []
        self.client.connect("connection-changed", lambda _c, connected: self.events.append(connected))
        self.client.connect("status", lambda _c, st: self.statuses.append(st))

    def tearDown(self):
        self.client.stop()
        if self.fake is not None:
            self.fake.stop()

    def test_connects_probes_and_polls(self):
        self.fake = FakeCliamp(self.path).start()
        self.client.start()
        self.assertTrue(run_loop(lambda: len(self.statuses) >= 3))
        self.assertEqual(self.events[0], True)
        self.assertTrue(self.client.connected)
        self.assertEqual(self.client.api, 1)
        self.assertIn("replace", self.client.capabilities["commands"])
        self.assertIn("Bass Boost", self.client.capabilities["eq_presets"])
        seqs = [st.seq for st in self.statuses]
        self.assertEqual(seqs, sorted(seqs))
        self.assertGreater(self.statuses[-1].stamp, 0)
        # 状態は専用の 1 本の接続で取る (capabilities は繋いだとき 1 回だけ)。
        self.assertEqual(len(self.fake.requests_for("capabilities")), 1)

    def test_first_failure_is_announced(self):
        """起動時に cliamp がいなければ、未接続を 1 回知らせる (画面が「接続中」のままにならない)。"""
        self.client.start()
        self.assertTrue(run_loop(lambda: self.events))
        self.assertEqual(self.events, [False])
        self.assertFalse(self.client.connected)

    def test_legacy_sets_api_zero(self):
        self.fake = FakeCliamp(self.path, legacy=True).start()
        self.client.start()
        self.assertTrue(run_loop(lambda: self.statuses))
        self.assertEqual(self.client.api, 0)
        self.assertEqual(self.client.capabilities, {})
        self.assertEqual(self.statuses[0].api, 0)

    def test_reconnects_after_restart(self):
        self.fake = FakeCliamp(self.path).start()
        self.client.start()
        self.assertTrue(run_loop(lambda: self.client.connected and self.statuses))
        self.fake.stop()
        self.assertTrue(run_loop(lambda: not self.client.connected, timeout=3))
        self.assertEqual(self.events[-1], False)
        self.statuses.clear()
        self.fake.start()
        self.assertTrue(run_loop(lambda: self.client.connected and self.statuses, timeout=5))
        self.assertEqual(self.events[-1], True)
        self.assertEqual(self.events.count(False), 1)
        self.assertEqual(len(self.fake.requests_for("capabilities")), 2)

    def test_busy_tui_is_not_offline(self):
        """status だけが遅い (TUI の Update が止まっている) ときは未接続と言わない。"""
        self.fake = FakeCliamp(self.path).start()
        self.fake.delays["status"] = 0.6
        with mock.patch.object(client_mod, "STATE_TIMEOUT", 0.3):
            self.client.start()
            self.assertTrue(run_loop(lambda: len(self.fake.requests_for("capabilities")) >= 3, timeout=5))
            self.assertEqual(self.events, [True])
            self.assertTrue(self.client.connected)
            self.fake.delays.clear()
            self.assertTrue(run_loop(lambda: self.statuses, timeout=3))
        self.assertEqual(self.events, [True])

    def test_upgrade_to_api1_is_noticed(self):
        """拡張の無い cliamp から拡張ありに入れ替わったら api が 1 になる。"""
        self.fake = FakeCliamp(self.path, legacy=True).start()
        self.client.start()
        self.assertTrue(run_loop(lambda: self.statuses))
        self.assertEqual(self.client.api, 0)
        self.fake.stop()
        self.fake = FakeCliamp(self.path).start()
        self.assertTrue(run_loop(lambda: self.client.api == 1, timeout=5))
        self.assertTrue(self.client.connected)

    def test_stop_ends_polling(self):
        self.fake = FakeCliamp(self.path).start()
        self.client.start()
        self.assertTrue(run_loop(lambda: self.statuses))
        self.client.stop()
        run_loop(lambda: False, timeout=0.2)
        count = len(self.fake.requests_for("status"))
        run_loop(lambda: False, timeout=0.3)
        self.assertEqual(len(self.fake.requests_for("status")), count)


if __name__ == "__main__":
    unittest.main()
