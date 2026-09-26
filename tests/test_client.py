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
