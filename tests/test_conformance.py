"""IPC の適合試験: 同じ断言を偽の cliamp と (あれば) 本物のパッチ済み cliamp に当てる。

- 偽 (tests/fake_cliamp.py) にはいつも当てる。
- 環境変数 CLIAMP_MUSIC_REAL_SOCKET に本物の cliamp のソケットがあれば、そちらにも当てる。
  本物はリストを差し替え、再生し、ローカルのプレイリストを作って消す。利用者の
  ~/.config/cliamp/cliamp.sock を指していたら走らない (聞いている音楽を壊すため)。
  本物の側の用意: 一時的な HOME に「ドライブ」(5 曲以上、手元のファイル) と
  「Focus」のプレイリスト、履歴 (history.toml)、[spotify] の節 (資格情報なし =
  needs_auth) を置いた TUI モードの cliamp。音は ALSA の null などで鳴らさないこと。
- CLIAMP_MUSIC_REAL_NETWORK=1 のときだけ、本物で yt-dlp の検索 (1 回) と LRCLIB の
  歌詞を引く。偽はネットワークを使わないので常に走る。
- 本物の側で使う値は環境変数で変えられる:
  CLIAMP_MUSIC_REAL_LYRICS="アーティスト|曲名" (見つかる曲)、
  CLIAMP_MUSIC_REAL_LYRICS_MISSING="アーティスト|曲名" (見つからない曲)、
  CLIAMP_MUSIC_REAL_LOCAL_QUERY (ローカル検索で当たる語)。

断言は PROTOCOL.md と、本物のパッチ (patches/cliamp-1.50.0-gui-ipc.patch) の実際の
振る舞いに合わせてある。偽がこれに落ちるなら偽が本物とずれている。
要求はアプリと同じ protocol.encode_request で作り、1 要求 1 接続で送る。
"""

from __future__ import annotations

import functools
import json
import os
import pwd
import socket
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, temp_socket_path  # noqa: E402

from cliamp_music.protocol import (  # noqa: E402
    decode_response,
    encode_request,
    parse_lyrics,
    parse_playlist,
    parse_providers,
    parse_status,
    parse_tracks,
)

REAL_SOCKET = os.environ.get("CLIAMP_MUSIC_REAL_SOCKET", "")
REAL_NETWORK = os.environ.get("CLIAMP_MUSIC_REAL_NETWORK") == "1"

EQ_PRESETS = ["Flat", "Rock", "Pop", "Jazz", "Classical", "Bass Boost", "Treble Boost", "Vocal",
              "Electronic", "Acoustic", "Hip-Hop", "R&B", "Loudness", "Late Night", "Podcast",
              "Small Speakers"]
STATE_COMMANDS = ["capabilities", "seek_to", "playlist", "play_index", "replace", "enqueue",
                  "queue_edit", "remove"]
CATALOG_COMMANDS = ["providers", "playlists", "tracks", "search", "load_provider", "lyrics",
                    "history", "playlist_add", "playlist_delete", "playlist_remove_track"]
BASE_COMMANDS = ["play", "pause", "toggle", "stop", "next", "prev", "volume", "seek", "load",
                 "queue", "theme", "vis", "shuffle", "repeat", "mono", "speed", "eq", "device",
                 "status", "bands"]
# 再生はされない (繋がらない) 手元の URL。外のネットワークには出ない。
DUMMY_STREAM = "http://127.0.0.1:9/e2e-conformance-{}.mp3"


def _user_socket() -> str:
    home = pwd.getpwuid(os.getuid()).pw_dir
    return os.path.realpath(os.path.join(home, ".config", "cliamp", "cliamp.sock"))


def _call_raw(path: str, line: bytes, timeout: float) -> str:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(line)
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8")
    finally:
        sock.close()


class _Conformance:
    """偽と本物に共通の断言。サブクラスが sock と、相手ごとの値を決める。"""

    sock = ""
    real = False
    network = True
    lyrics_found = ("", "")
    lyrics_missing = ("", "")
    local_query = ""

    # --- 送受信 ---------------------------------------------------------------

    def raw(self, cmd: str, timeout: float = 130.0, **fields) -> str:
        return _call_raw(self.sock, encode_request(cmd, **fields), timeout)

    def call(self, cmd: str, **fields) -> dict:
        line = self.raw(cmd, **fields)
        data = json.loads(line)
        self.assertIsInstance(data, dict, line)
        self.assertIn("ok", data, line)
        return data

    def ok(self, cmd: str, **fields) -> dict:
        data = self.call(cmd, **fields)
        self.assertIs(data["ok"], True, f"{cmd} {fields}: {data}")
        return data

    def err(self, cmd: str, **fields) -> str:
        data = self.call(cmd, **fields)
        self.assertIs(data["ok"], False, f"{cmd} {fields} が成功しました: {data}")
        self.assertIsInstance(data.get("error"), str)
        return data["error"]

    def status(self) -> dict:
        return self.ok("status")

    def wait(self, predicate, timeout: float = 6.0, what: str = "") -> dict:
        deadline = time.monotonic() + timeout
        st = self.status()
        while not predicate(st):
            if time.monotonic() > deadline:
                self.fail(f"{what or '待つ条件'} にならない: {st}")
            time.sleep(0.1)
            st = self.status()
        return st

    # --- 用意 ---------------------------------------------------------------

    def library(self, n: int = 5) -> list[dict]:
        tracks = self.ok("tracks", provider="local", id="ドライブ")["tracks"]
        self.assertGreaterEqual(len(tracks), n, "「ドライブ」の曲が足りません")
        return [dict(t) for t in tracks[:n]]

    def fresh(self, n: int = 5, index: int = 0, shuffle: bool = False, repeat: str = "off",
              source: dict | None = None) -> list[dict]:
        """シャッフル・リピートを決め、library の n 曲に差し替えて index から再生する。"""
        self.ok("shuffle", name="on" if shuffle else "off")
        self.ok("repeat", name=repeat)
        tracks = self.library(n)
        self.ok("replace", tracks=tracks, index=index,
                source=source or {"provider": "local", "id": "ドライブ", "name": "ドライブ"})
        self.wait(lambda st: st.get("track", {}).get("path") == tracks[index]["path"]
                  and st["state"] == "playing", what="差し替えた曲の再生")
        return tracks

    def queue(self) -> list[int]:
        return list(self.ok("playlist").get("queue") or [])

    # ======================================================================
    # 全体

    def test_capabilities(self):
        line = self.raw("capabilities")
        data = json.loads(line)
        self.assertIs(data["ok"], True)
        self.assertEqual(data["api"], 1)
        self.assertIsInstance(data["api"], int)
        for cmd in BASE_COMMANDS + STATE_COMMANDS + CATALOG_COMMANDS:
            self.assertIn(cmd, data["commands"])
        self.assertEqual(data["eq_presets"], EQ_PRESETS)
        # Go の json.Marshal は & < > を \u0026 などに逃がす
        self.assertIn("R\\u0026B", line)

    def test_unknown_command_and_bad_json(self):
        self.assertEqual(self.err("frobnicate"), "unknown command: frobnicate")
        reply = json.loads(_call_raw(self.sock, b"this is not json\n", 5))
        self.assertIs(reply["ok"], False)
        self.assertTrue(reply["error"].startswith("invalid JSON"), reply)

    def test_decode_response_marks_unknown_as_unsupported(self):
        response = decode_response(self.raw("frobnicate"))
        self.assertEqual(response.kind, "unsupported")

    # ======================================================================
    # status

    def test_status_shape(self):
        tracks = self.fresh(index=1)
        line = self.raw("status")
        st = json.loads(line)
        self.assertIn(st["state"], ("playing", "paused", "stopped"))
        self.assertEqual(st["api"], 1)
        for key in ("shuffle", "mono", "buffering"):
            self.assertIsInstance(st.get(key), bool, f"{key} が無いか真偽でない: {st}")
        self.assertIn(st["repeat"], ("Off", "All", "One"))
        self.assertIsInstance(st["eq"], list)
        self.assertEqual(len(st["eq"]), 10)
        self.assertIsInstance(st["gen"], int)
        self.assertGreater(st["gen"], 0)
        self.assertEqual(st["index"], 1)
        self.assertEqual(st["total"], 5)
        self.assertEqual(st["track"]["path"], tracks[1]["path"])
        self.assertEqual(st["track"]["title"], tracks[1]["title"])
        self.assertEqual(st["source"], {"provider": "local", "id": "ドライブ", "name": "ドライブ"})
        self.assertIsInstance(st["theme"], dict)
        self.assertGreater(st["speed"], 0)
        parsed = parse_status(st)
        self.assertEqual(parsed.api, 1)
        self.assertEqual(parsed.repeat, "off")

    def test_status_omits_zero_index(self):
        self.fresh(index=0)
        line = self.raw("status")
        self.assertNotIn('"index"', line, "index 0 は omitempty で省かれる")
        self.assertEqual(parse_status(json.loads(line)).index, 0)

    def test_status_gen_matches_playlist(self):
        self.fresh()
        self.assertEqual(self.status()["gen"], self.ok("playlist")["gen"])

    # ======================================================================
    # 再生の失敗 (status の playback_error)

    MISSING = "/nonexistent/cliamp-music-conformance/__fail__.flac"

    def test_playback_error_for_a_missing_file(self):
        """手元に無いファイルは鳴らせず、その曲がいまの曲の間だけ playback_error に理由が出る。
        止めても残り、次の開始で消える (ネットワークを使わずに必ず失敗する曲)。"""
        self.ok("shuffle", name="off")
        self.ok("repeat", name="off")
        tracks = self.library(2)
        missing = {"path": self.MISSING, "title": "無いファイル"}
        self.ok("replace", tracks=[missing] + tracks, index=0)
        st = self.wait(lambda st: st.get("playback_error"), 6, "playback_error")
        want = f"open source: open {self.MISSING}: no such file or directory"
        self.assertEqual(st["playback_error"], want)
        self.assertEqual(st["state"], "stopped")
        self.assertEqual(st["track"]["path"], self.MISSING)
        self.assertEqual(parse_status(st).playback_problem, ("ファイルが見つかりません", want))
        # 止めても消えない (いまの曲はまだ失敗した曲)
        self.ok("stop")
        self.assertEqual(self.status().get("playback_error"), want)
        # 別の曲を始めれば消え、omitempty で省かれる
        self.ok("play_index", index=1)
        self.wait(lambda st: st["state"] == "playing" and st.get("index") == 1, 6, "次の曲の再生")
        self.assertNotIn('"playback_error"', self.raw("status"))
        # 止めてから失敗した曲をやり直せば、また失敗する (鳴っている曲から移ると、本物の TUI は
        # 前の曲を止めずに始めるので、失敗の後も前の曲が鳴り続け、終わると次の曲へ進む)
        self.ok("stop")
        self.wait(lambda st: st["state"] == "stopped", 3, "stop")
        self.ok("play_index", index=0)
        st = self.wait(lambda st: st.get("playback_error") == want and st["state"] == "stopped", 6, "やり直しの失敗")
        # 止まっている間にいまの曲が別の曲になれば出さない
        self.ok("remove", index=0)
        st = self.status()
        self.assertEqual(st["track"]["path"], tracks[0]["path"])
        self.assertNotIn("playback_error", st)

    # ======================================================================
    # seek_to

    def test_seek_to_negative_is_error(self):
        self.assertEqual(self.err("seek_to", value=-1), "seek_to requires a non-negative position")

    def test_seek_to_moves_position(self):
        self.fresh(index=0)
        self.ok("seek_to", value=30)
        st = self.wait(lambda st: 29.0 <= st.get("position", 0) <= 36.0, 3, "seek_to 30")
        self.assertEqual(st["state"], "playing")

    # ======================================================================
    # playlist

    def test_playlist_snapshot(self):
        tracks = self.fresh(index=0, repeat="off")
        line = self.raw("playlist")
        data = json.loads(line)
        self.assertEqual([t["path"] for t in data["tracks"]], [t["path"] for t in tracks])
        self.assertNotIn('"index"', line)
        self.assertEqual(data["total"], 5)
        self.assertNotIn("queue", data, "空の待ち行列は省かれる")
        self.assertEqual(data["up_next"], [1, 2, 3, 4])
        self.assertEqual(data["source"], {"provider": "local", "id": "ドライブ", "name": "ドライブ"})
        state = parse_playlist(data)
        self.assertEqual(state.index, 0)

    def test_playlist_limit(self):
        self.fresh(index=0)
        data = self.ok("playlist", limit=2)
        self.assertEqual(len(data["tracks"]), 2)
        self.assertEqual(data["total"], 5)

    def test_up_next_repeat_all_wraps(self):
        self.fresh(index=2, repeat="all")
        self.assertEqual(self.ok("playlist")["up_next"], [3, 4, 0, 1])

    def test_up_next_repeat_off_stops_at_end(self):
        self.fresh(index=2, repeat="off")
        self.assertEqual(self.ok("playlist")["up_next"], [3, 4])

    def test_queued_field_and_up_next(self):
        self.fresh(index=0, repeat="off")
        self.ok("queue_edit", mode="add", index=3)
        self.ok("queue_edit", mode="add", index=1)
        data = self.ok("playlist")
        self.assertEqual(data["queue"], [3, 1])
        self.assertEqual(data["tracks"][3].get("queued"), 1)
        self.assertEqual(data["tracks"][1].get("queued"), 2)
        self.assertNotIn("queued", data["tracks"][2])
        self.assertEqual(data["up_next"], [2, 4])

    # ======================================================================
    # play_index

    def test_play_index(self):
        tracks = self.fresh(index=0)
        self.ok("play_index", index=3)
        st = self.wait(lambda st: st.get("index") == 3 and st["state"] == "playing", what="play_index 3")
        self.assertEqual(st["track"]["path"], tracks[3]["path"])
        self.ok("play_index", index=0)
        self.wait(lambda st: st.get("index", 0) == 0 and st.get("track", {}).get("path") == tracks[0]["path"],
                  what="play_index 0 (0 も有効な値)")

    def test_play_index_errors(self):
        self.fresh()
        self.assertEqual(self.err("play_index"), "play_index requires an index")
        self.assertEqual(self.err("play_index", index=99), "index out of range")
        self.assertEqual(self.err("play_index", index=-1), "index out of range")

    # ======================================================================
    # replace

    def test_replace_response_and_gen(self):
        self.fresh()
        before = self.status()["gen"]
        tracks = self.library(4)
        data = self.ok("replace", tracks=tracks, index=2, source={"provider": "local", "id": "x", "name": "X"})
        self.assertEqual(data["total"], 4)
        self.assertGreater(data["gen"], before)
        st = self.wait(lambda st: st.get("index") == 2, what="replace の index")
        self.assertEqual(st["track"]["path"], tracks[2]["path"])
        self.assertEqual(st["source"], {"provider": "local", "id": "x", "name": "X"})

    def test_replace_errors(self):
        self.assertEqual(self.err("replace", tracks=[]), "replace requires tracks")
        self.assertEqual(self.err("replace"), "replace requires tracks")
        tracks = self.library(2)
        self.assertEqual(self.err("replace", tracks=tracks, index=2), "index out of range")

    def test_replace_rejects_track_without_path(self):
        tracks = self.library(2)
        bad = [tracks[0], {"title": "no path"}]
        self.assertEqual(self.err("replace", tracks=bad), "replace: track 1 has no path")

    def test_replace_shuffle_starts_at_chosen(self):
        tracks = self.fresh(index=3, shuffle=True, repeat="off")
        st = self.status()
        self.assertEqual(st["index"], 3)
        self.assertEqual(st["track"]["path"], tracks[3]["path"])
        up_next = self.ok("playlist")["up_next"]
        self.assertEqual(sorted(up_next), [0, 1, 2, 4], "シャッフルでも選んだ曲の後に残りが全部来る")
        self.ok("shuffle", name="off")

    def test_replace_round_trip(self):
        """受け取った TrackInfo をそのまま送り返せば元に戻る (meta・bookmark を含む)。"""
        self.fresh()
        tracks = self.library(3)
        tracks[1] = dict(tracks[1], meta={"art": "file:///nonexistent/art.png", "x.id": "42"},
                         bookmark=True, genre="Test & <Genre>")
        self.ok("replace", tracks=tracks, index=0)
        got = self.ok("playlist")["tracks"]
        for sent, back in zip(tracks, got):
            back = {k: v for k, v in back.items() if k != "queued"}
            self.assertEqual(back, {k: v for k, v in sent.items() if v not in (None, "", 0, False, {})})

    # ======================================================================
    # enqueue

    def _dummy(self, n: int, **extra) -> dict:
        return dict({"path": DUMMY_STREAM.format(n), "title": f"Dummy {n}", "stream": True}, **extra)

    def test_enqueue_next(self):
        """「次に再生」は末尾に足し、待ち行列ではなく再生順でいまの曲のすぐ後ろに並べる
        (待ち行列に入れると、待ち行列とリストの順で 2 度鳴る)。"""
        self.fresh(index=0, repeat="off")
        data = self.ok("enqueue", tracks=[self._dummy(1), self._dummy(11)], mode="next")
        self.assertEqual(data["total"], 7)
        pl = self.ok("playlist")
        self.assertNotIn("queue", pl)
        self.assertEqual(pl["up_next"], [5, 6, 1, 2, 3, 4])
        self.assertEqual(pl["tracks"][5]["path"], DUMMY_STREAM.format(1))
        self.assertEqual(self.status().get("index", 0), 0, "次に再生で今の曲は変わらない")

    def test_enqueue_default_mode_is_next(self):
        self.fresh(index=0, repeat="off")
        self.ok("enqueue", tracks=[self._dummy(2)])
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.ok("playlist")["up_next"][0], 5)

    def test_enqueue_next_with_repeat_one_uses_the_queue(self):
        """1 曲リピートでは順送りで進まないので、「次に再生」は従来どおり待ち行列へ。"""
        self.fresh(index=0, repeat="one")
        self.ok("enqueue", tracks=[self._dummy(12)], mode="next")
        self.assertEqual(self.queue(), [5])
        self.ok("repeat", name="off")

    def test_enqueue_end(self):
        self.fresh(index=0, repeat="off")
        data = self.ok("enqueue", tracks=[self._dummy(3), self._dummy(4)], mode="end")
        self.assertEqual(data["total"], 7)
        pl = self.ok("playlist")
        self.assertNotIn("queue", pl)
        self.assertEqual(pl["up_next"], [1, 2, 3, 4, 5, 6])

    def test_enqueue_now_single(self):
        self.fresh(index=0, repeat="off")
        tracks = self.library(5)
        self.ok("remove", index=4)
        data = self.ok("enqueue", tracks=[tracks[4]], mode="now")
        self.assertEqual(data["total"], 5)
        st = self.wait(lambda st: st.get("index") == 4 and st["state"] == "playing", what="enqueue now")
        self.assertEqual(st["track"]["path"], tracks[4]["path"])

    def test_enqueue_now_multiple_without_shuffle_does_not_queue_rest(self):
        """シャッフルでなければ 2 曲目以降は再生順でそのまま続くので、待ち行列には入れない
        (入れると 2 度鳴る)。"""
        self.fresh(index=0, repeat="off")
        tracks = self.library(5)
        self.ok("remove", index=4)
        self.ok("remove", index=3)
        self.ok("enqueue", tracks=[tracks[3], tracks[4]], mode="now")
        self.wait(lambda st: st.get("index") == 3, what="enqueue now の 1 曲目")
        pl = self.ok("playlist")
        self.assertNotIn("queue", pl, pl.get("queue"))
        self.assertEqual(pl["up_next"], [4])

    def test_enqueue_errors(self):
        self.assertEqual(self.err("enqueue", tracks=[self._dummy(5)], mode="later"),
                         'enqueue: unknown mode "later"')
        self.assertEqual(self.err("enqueue", tracks=[]), "enqueue requires tracks")
        self.assertEqual(self.err("enqueue", tracks=[{"title": "x"}]), "enqueue: track 0 has no path")

    def test_enqueue_round_trip_live_and_meta(self):
        self.fresh(index=0, repeat="off")
        station = {"path": DUMMY_STREAM.format(6), "title": "Station & <FM>", "stream": True, "live": True,
                   "meta": {"art": "file:///nonexistent/favicon.png", "radio.country": "Japan"}}
        self.ok("enqueue", tracks=[station], mode="end")
        back = self.ok("playlist")["tracks"][5]
        self.assertEqual(back, station)

    def test_enqueue_http_url_gets_stream(self):
        """http(s) の URL (yt-dlp でないもの) は stream が無くても立てる (TUI を止めないため)。"""
        self.fresh(index=0, repeat="off")
        self.ok("enqueue", tracks=[{"path": DUMMY_STREAM.format(7), "title": "no stream flag"}], mode="end")
        self.assertIs(self.ok("playlist")["tracks"][5].get("stream"), True)

    # ======================================================================
    # queue_edit

    def test_queue_edit_add_remove_move_clear(self):
        self.fresh(index=0, repeat="off")
        self.assertEqual(self.ok("queue_edit", mode="add", index=2)["queue"], [2])
        self.assertEqual(self.ok("queue_edit", mode="add", index=4)["queue"], [2, 4])
        self.assertEqual(self.ok("queue_edit", mode="add", index=1)["queue"], [2, 4, 1])
        self.assertEqual(self.ok("queue_edit", mode="add", index=4)["queue"], [2, 4, 1], "同じ曲は二重に入らない")
        self.assertEqual(self.ok("queue_edit", mode="move", index=0, to=2)["queue"], [4, 1, 2])
        self.assertEqual(self.ok("queue_edit", mode="remove", index=1)["queue"], [4, 2])
        cleared = self.ok("queue_edit", mode="clear")
        self.assertNotIn("queue", cleared, "空の待ち行列は省かれる")
        self.assertEqual(self.queue(), [])

    def test_queue_edit_errors(self):
        self.fresh(index=0)
        self.assertEqual(self.err("queue_edit", mode="add"), "queue_edit add requires an index")
        self.assertEqual(self.err("queue_edit", mode="remove"), "queue_edit remove requires an index")
        self.assertEqual(self.err("queue_edit", mode="move", index=0), "queue_edit move requires index and to")
        self.assertEqual(self.err("queue_edit", mode="move", index=0, to=1), "queue position out of range")
        self.assertEqual(self.err("queue_edit", mode="add", index=99), "index out of range")
        self.assertEqual(self.err("queue_edit", mode="shuffle"), 'queue_edit: unknown mode "shuffle"')

    def test_queue_edit_move_to_same_position(self):
        """MoveQueueTo は同じ位置への移動を成功として何もしない。"""
        self.fresh(index=0, repeat="off")
        self.ok("queue_edit", mode="add", index=2)
        self.ok("queue_edit", mode="add", index=3)
        gen = self.status()["gen"]
        self.assertEqual(self.ok("queue_edit", mode="move", index=1, to=1)["queue"], [2, 3])
        self.assertEqual(self.status()["gen"], gen)

    def test_enqueue_and_queue_edit_return_gen(self):
        self.fresh(index=0, repeat="off")
        before = self.status()["gen"]
        added = self.ok("enqueue", tracks=[self._dummy(9)], mode="end")
        self.assertGreater(added["gen"], before)
        edited = self.ok("queue_edit", mode="add", index=2)
        self.assertGreater(edited["gen"], added["gen"])

    def test_up_next_includes_playing_queued_track(self):
        """待ち行列から鳴っている曲は待ち行列から外れるので、リスト上の後ろの位置に出る。"""
        self.fresh(index=0, repeat="off")
        self.ok("queue_edit", mode="add", index=3)
        self.ok("next")
        self.wait(lambda st: st.get("index") == 3, what="待ち行列の曲へ")
        data = self.ok("playlist")
        self.assertNotIn("queue", data)
        self.assertEqual(data["up_next"], [1, 2, 3, 4])

    def test_stale_path_is_refused(self):
        """添字で指す要求に path を付けると、その添字の曲が違えば何も変えずに "stale"。"""
        tracks = self.fresh(index=0, repeat="off")
        wrong = tracks[2]["path"]
        gen = self.status()["gen"]
        self.assertEqual(self.err("play_index", index=3, path=wrong), "stale")
        self.assertEqual(self.err("remove", index=3, path=wrong), "stale")
        self.assertEqual(self.err("queue_edit", mode="add", index=3, path=wrong), "stale")
        self.assertEqual(self.status()["gen"], gen)
        self.assertEqual(self.status().get("index", 0), 0)
        self.ok("queue_edit", mode="add", index=3, path=tracks[3]["path"])
        self.ok("queue_edit", mode="add", index=4, path=tracks[4]["path"])
        self.assertEqual(self.err("queue_edit", mode="move", index=0, to=1, path=wrong), "stale")
        self.assertEqual(self.ok("queue_edit", mode="move", index=0, to=1, path=tracks[3]["path"])["queue"],
                         [4, 3])
        self.ok("play_index", index=2, path=tracks[2]["path"])
        self.wait(lambda st: st.get("index") == 2, what="path の合う play_index")

    def test_stale_path_in_local_playlist_removal(self):
        name = self._scratch()
        tracks = self.library(2)
        self.ok("playlist_add", name=name, tracks=tracks)
        self.assertEqual(self.err("playlist_remove_track", name=name, index=0, path=tracks[1]["path"]), "stale")
        self.ok("playlist_remove_track", name=name, index=1, path=tracks[1]["path"])
        back = self.ok("tracks", provider="local", id=name)["tracks"]
        self.ok("playlist_delete", name=name)
        self.assertEqual([t["path"] for t in back], [tracks[0]["path"]])

    def test_path_raw_round_trips(self):
        """UTF-8 でない path は path_raw (base64) で運ばれ、送り返せば元に戻る。"""
        import base64

        self.fresh(index=0, repeat="off")
        raw = base64.b64encode(b"/nonexistent/e2e/\x83e\x83X\x83g.flac").decode()
        odd = {"path": "/nonexistent/e2e/\ufffde\ufffdX\ufffdg.flac", "path_raw": raw, "title": "Shift_JIS の名前"}
        tracks = self.library(2) + [odd]
        self.ok("replace", tracks=tracks, index=0)
        back = self.ok("playlist")["tracks"][2]
        self.assertEqual(back.get("path_raw"), raw)
        self.assertEqual(self.err("replace", tracks=[dict(odd, path_raw="!!not base64!!")]),
                         "replace: track 0 has a bad path_raw")

    def test_queue_edit_changes_gen(self):
        self.fresh(index=0)
        before = self.status()["gen"]
        self.ok("queue_edit", mode="add", index=2)
        self.assertGreater(self.status()["gen"], before)

    # ======================================================================
    # remove

    def test_remove(self):
        tracks = self.fresh(index=0, repeat="off")
        self.ok("queue_edit", mode="add", index=4)
        before = self.status()["gen"]
        data = self.ok("remove", index=2)
        self.assertEqual(data["total"], 4)
        self.assertGreater(data["gen"], before)
        pl = self.ok("playlist")
        self.assertEqual([t["path"] for t in pl["tracks"]],
                         [tracks[i]["path"] for i in (0, 1, 3, 4)])
        self.assertEqual(pl["queue"], [3], "待ち行列の添字も詰まる")

    def test_remove_current_when_stopped(self):
        """止まっている間は今の曲 (index) も消せる (消せないのは再生・一時停止・読み込み中だけ)。"""
        self.fresh(index=1)
        self.ok("stop")
        self.wait(lambda st: st["state"] == "stopped", 3, "stop")
        self.assertEqual(self.ok("remove", index=1)["total"], 4)

    def test_remove_errors(self):
        self.fresh(index=1)
        self.assertEqual(self.err("remove", index=1), "cannot remove the current track")
        self.assertEqual(self.err("remove", index=9), "index out of range")
        self.assertEqual(self.err("remove"), "remove requires an index")

    # ======================================================================
    # providers / playlists / tracks

    def test_providers(self):
        line = self.raw("providers")
        data = json.loads(line)
        by_key = {p["key"]: p for p in data["providers"]}
        for entry in data["providers"]:
            for key in ("key", "name", "search", "playlists", "virtual"):
                self.assertIn(key, entry, f"ProviderInfo の {key} は false でも省かない: {entry}")
        self.assertEqual(by_key["youtube"], {"key": "youtube", "name": "YouTube", "search": True,
                                             "playlists": False, "virtual": True})
        self.assertIs(by_key["local"]["search"], True)
        self.assertEqual(by_key["local"]["name"], "Local")
        self.assertEqual(parse_providers(data)[0].key, data["providers"][0]["key"])

    def test_providers_radio_is_not_searchable(self):
        """radio は Searcher でない (search は YouTube へ退避するだけ) ので search は false。"""
        by_key = {p["key"]: p for p in self.ok("providers")["providers"]}
        self.assertIs(by_key["radio"]["search"], False)
        self.assertIs(by_key["radio"]["playlists"], True)

    def test_playlists_local(self):
        lists = self.ok("playlists", provider="local")["playlists"]
        names = [p["name"] for p in lists]
        self.assertEqual(names[0], "Recently Played", "履歴があれば仮想の Recently Played が先頭")
        drive = next(p for p in lists if p["id"] == "ドライブ")
        self.assertEqual(drive["name"], "ドライブ")
        self.assertGreaterEqual(drive["track_count"], 5)
        self.assertGreater(drive["duration"], 0)
        self.assertIn("Focus", names)

    def test_playlists_errors(self):
        self.assertEqual(self.err("playlists"), "playlists requires a provider")
        self.assertEqual(self.err("playlists", provider="nope"), "unknown provider: nope")

    def test_playlists_youtube_is_error(self):
        self.assertEqual(self.err("playlists", provider="youtube"), "provider youtube has no playlists")

    def test_playlists_spotify_needs_auth(self):
        data = self.call("playlists", provider="spotify")
        self.assertIs(data["ok"], False)
        self.assertIs(data.get("needs_auth"), True)
        self.assertEqual(data["error"], "sign-in required")
        self.assertTrue(decode_response(json.dumps(data)).needs_auth)

    def test_tracks_local(self):
        tracks = self.ok("tracks", provider="local", id="ドライブ")["tracks"]
        self.assertGreaterEqual(len(tracks), 5)
        for t in tracks:
            self.assertTrue(t["path"])
            self.assertTrue(t.get("title"))
            self.assertGreater(t.get("duration", 0), 0)
            self.assertNotIn("queued", t)
            self.assertNotIn("played_at", t)
        self.assertEqual(len(parse_tracks(tracks)), len(tracks))

    def test_tracks_errors(self):
        self.assertEqual(self.err("tracks", provider="local"), "tracks requires a provider and an id")
        self.assertEqual(self.err("tracks", provider="youtube", id="x"), "provider youtube has no playlists")
        self.assertEqual(self.err("tracks", provider="nope", id="x"), "unknown provider: nope")
        self.assertEqual(self.err("tracks", provider="url", id="/etc/passwd"),
                         'url: not an http(s) URL: "/etc/passwd"')

    def test_tracks_local_missing_playlist(self):
        error = self.err("tracks", provider="local", id="no-such-e2e-list")
        self.assertIn("no-such-e2e-list.toml", error)
        self.assertIn("no such file or directory", error)

    # ======================================================================
    # search

    def test_search_errors(self):
        self.assertEqual(self.err("search", provider="youtube", query="  "), "search requires a query")
        self.assertEqual(self.err("search", provider="url", query="x"), "provider url does not support search")
        self.assertEqual(self.err("search", provider="nope", query="x"), "unknown provider: nope")

    def test_search_local(self):
        found = self.ok("search", provider="local", query=self.local_query, limit=10).get("tracks") or []
        self.assertTrue(found, f"ローカル検索 {self.local_query!r} で何も当たらない")
        for t in found:
            hay = " ".join(str(t.get(k, "")) for k in ("title", "artist", "album")).casefold()
            self.assertIn(self.local_query.casefold(), hay)
        limited = self.ok("search", provider="local", query=self.local_query, limit=1).get("tracks") or []
        self.assertLessEqual(len(limited), 1)

    def test_search_youtube(self):
        if not self.network:
            self.skipTest("CLIAMP_MUSIC_REAL_NETWORK=1 のときだけ本物で yt-dlp を使う")
        found = self.ok("search", provider="youtube", query="lofi hip hop radio", limit=3)["tracks"]
        self.assertTrue(1 <= len(found) <= 3, found)
        for t in found:
            self.assertTrue(t["path"].startswith("https://www.youtube.com/watch?v="), t)
            self.assertTrue(t.get("title"))
            self.assertIs(t.get("stream"), True, t)

    # ======================================================================
    # load_provider

    def test_load_provider(self):
        self.ok("shuffle", name="off")
        tracks = self.library(5)
        data = self.ok("load_provider", provider="local", id="ドライブ", index=1, name="ドライブ (読み込み)")
        self.assertGreaterEqual(data["total"], 5)
        st = self.wait(lambda st: st.get("index") == 1 and st["state"] == "playing", what="load_provider")
        self.assertEqual(st["track"]["path"], tracks[1]["path"])
        self.assertEqual(st["source"], {"provider": "local", "id": "ドライブ", "name": "ドライブ (読み込み)"})

    def test_load_provider_errors(self):
        self.assertEqual(self.err("load_provider", provider="local"), "load_provider requires a provider and an id")
        self.assertEqual(self.err("load_provider", provider="local", id="ドライブ", index=99), "index out of range")

    # ======================================================================
    # lyrics

    def test_lyrics_found(self):
        if not self.network:
            self.skipTest("CLIAMP_MUSIC_REAL_NETWORK=1 のときだけ本物で LRCLIB を引く")
        artist, title = self.lyrics_found
        data = self.ok("lyrics", artist=artist, title=title)
        self.assertIsInstance(data.get("synced"), bool, "synced は false でも省かない")
        self.assertTrue(data["lyrics"])
        for line in data["lyrics"]:
            self.assertIn("t", line, "t は 0 でも省かない")
            self.assertIn("text", line)
        self.assertEqual(len(parse_lyrics(data).lines), len(data["lyrics"]))

    def test_lyrics_not_found(self):
        if not self.network:
            self.skipTest("CLIAMP_MUSIC_REAL_NETWORK=1 のときだけ本物で LRCLIB を引く")
        artist, title = self.lyrics_missing
        self.assertEqual(self.err("lyrics", artist=artist, title=title), "not found")

    def test_lyrics_requires_artist_or_title(self):
        self.assertEqual(self.err("lyrics"), "lyrics requires an artist or a title")

    # ======================================================================
    # history

    def test_history(self):
        tracks = self.ok("history", limit=3)["tracks"]
        self.assertTrue(1 <= len(tracks) <= 3)
        moments = []
        for t in tracks:
            self.assertTrue(t["path"])
            stamp = t["played_at"]
            self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$")
            moments.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        self.assertEqual(moments, sorted(moments, reverse=True), "新しい順")

    def test_history_records_track_played_past_half(self):
        tracks = self.fresh(index=0, repeat="off")
        duration = tracks[0]["duration"]
        self.ok("seek_to", value=duration * 0.5 + 2)
        self.wait(lambda st: st.get("position", 0) > duration * 0.5 + 1, 4, "半分を過ぎる")
        self.ok("next")
        self.wait(lambda st: st.get("index") == 1, what="次の曲")
        deadline = time.monotonic() + 5
        while True:
            top = self.ok("history", limit=1)["tracks"][0]
            if top["path"] == tracks[0]["path"] or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertEqual(top["path"], tracks[0]["path"])

    def test_history_keeps_only_toml_fields(self):
        """履歴 (history.toml) は path・title・artist・album・genre・year・track_number・duration
        だけを持つ。meta・stream・bookmark は残らない。"""
        self.fresh(index=0, repeat="off")
        tracks = self.library(2)
        tracks[0] = dict(tracks[0], meta={"art": "file:///nonexistent/art.png"}, bookmark=True)
        self.ok("replace", tracks=tracks, index=0)
        self.wait(lambda st: st["state"] == "playing" and st.get("track", {}).get("path") == tracks[0]["path"],
                  what="差し替えた曲の再生")
        duration = tracks[0]["duration"]
        self.ok("seek_to", value=duration * 0.5 + 2)
        self.wait(lambda st: st.get("position", 0) > duration * 0.5 + 1, 4, "半分を過ぎる")
        self.ok("next")
        self.wait(lambda st: st.get("index") == 1, what="次の曲")
        deadline = time.monotonic() + 5
        while True:
            top = self.ok("history", limit=1)["tracks"][0]
            if top["path"] == tracks[0]["path"] or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertEqual(top["path"], tracks[0]["path"])
        allowed = {"path", "title", "artist", "album", "genre", "year", "track_number", "duration", "played_at",
                   "stream"}
        self.assertLessEqual(set(top), allowed, top)
        # stream は持ち越さず、読み戻すときに path が URL かで決め直す (playlist.IsURL)
        self.assertEqual(top.get("stream", False), top["path"].startswith(("http://", "https://")), top)

    # ======================================================================
    # ローカルのプレイリストの編集

    def _scratch(self) -> str:
        name = f"e2e 適合 & <試験> {os.getpid()}"
        self.call("playlist_delete", name=name)
        return name

    def test_playlist_add_and_remove_track(self):
        name = self._scratch()
        tracks = self.library(3)
        self.ok("playlist_add", name=name, tracks=tracks[:2])
        self.ok("playlist_add", name=name, tracks=tracks[2:3])
        lists = {p["id"]: p for p in self.ok("playlists", provider="local")["playlists"]}
        self.assertEqual(lists[name]["track_count"], 3)
        back = self.ok("tracks", provider="local", id=name)["tracks"]
        self.assertEqual([t["path"] for t in back], [t["path"] for t in tracks])
        for sent, got in zip(tracks, back):
            for key in ("title", "artist", "album", "duration"):
                self.assertEqual(got.get(key), sent.get(key), key)
        self.ok("playlist_remove_track", name=name, index=0)
        back = self.ok("tracks", provider="local", id=name)["tracks"]
        self.assertEqual([t["path"] for t in back], [t["path"] for t in tracks[1:]])
        self.ok("playlist_delete", name=name)
        ids = [p["id"] for p in self.ok("playlists", provider="local")["playlists"]]
        self.assertNotIn(name, ids)

    def test_playlist_remove_last_track_deletes_playlist(self):
        name = self._scratch()
        self.ok("playlist_add", name=name, tracks=self.library(1))
        self.ok("playlist_remove_track", name=name, index=0)
        ids = [p["id"] for p in self.ok("playlists", provider="local")["playlists"]]
        self.assertNotIn(name, ids, "最後の曲を外すとプレイリストのファイルごと消える")

    def test_playlist_add_drops_live_and_meta(self):
        """ローカルのプレイリスト (TOML) は live と meta を持てない。URL の曲は stream に戻る。"""
        name = self._scratch()
        station = {"path": DUMMY_STREAM.format(8), "title": "Station", "stream": True, "live": True,
                   "meta": {"art": "file:///nonexistent/favicon.png"}}
        self.ok("playlist_add", name=name, tracks=[station])
        back = self.ok("tracks", provider="local", id=name)["tracks"]
        self.ok("playlist_delete", name=name)
        self.assertEqual(back, [{"path": station["path"], "title": "Station", "stream": True}])

    def test_playlist_edit_errors(self):
        tracks = self.library(1)
        self.assertEqual(self.err("playlist_add", tracks=tracks), "playlist_add requires a name")
        self.assertEqual(self.err("playlist_add", name="x"), "playlist_add requires tracks")
        self.assertEqual(self.err("playlist_add", name="a/b", tracks=tracks), 'invalid playlist name "a/b"')
        self.assertEqual(self.err("playlist_add", name="Recently Played", tracks=tracks),
                         '"Recently Played" is a virtual history playlist and cannot be modified')
        self.assertEqual(self.err("playlist_delete"), "playlist_delete requires a name")
        self.assertIn("no such file or directory", self.err("playlist_delete", name="no-such-e2e-list"))
        self.assertEqual(self.err("playlist_remove_track", name="ドライブ"),
                         "playlist_remove_track requires a name and an index")
        self.assertEqual(self.err("playlist_remove_track", name="ドライブ", index=99),
                         "track index 99 out of range")

    def test_large_request_over_64k(self):
        """要求 1 行 64 KiB 超 (8 MiB まで) を受け付ける。プレイリストへの追加で確かめる。"""
        name = self._scratch()
        many = [{"path": DUMMY_STREAM.format(1000 + i), "title": f"Bulk {i} " + "x" * 40} for i in range(1500)]
        self.assertGreater(len(encode_request("playlist_add", name=name, tracks=many)), 64 * 1024)
        self.ok("playlist_add", name=name, tracks=many)
        lists = {p["id"]: p for p in self.ok("playlists", provider="local")["playlists"]}
        self.ok("playlist_delete", name=name)
        self.assertEqual(lists[name]["track_count"], 1500)

    # ======================================================================
    # 既存のコマンド (GUI が使うもの)

    def test_volume_is_absolute(self):
        self.fresh()
        self.ok("volume", value=-12)
        self.wait(lambda st: st.get("volume") == -12, 3, "volume -12")
        self.ok("volume", value=-12)
        self.wait(lambda st: st.get("volume") == -12, 3, "2 度送っても -12 のまま (絶対値)")
        self.ok("volume", value=-20)

    def test_speed(self):
        self.fresh()
        self.assertEqual(self.ok("speed", value=1.25).get("speed"), 1.25)
        self.wait(lambda st: st.get("speed") == 1.25, 3, "speed 1.25")
        self.assertEqual(self.err("speed", value=0), "speed must be positive")
        self.ok("speed", value=1)

    def test_shuffle_and_repeat_replies(self):
        self.fresh()
        self.assertIs(self.ok("shuffle", name="on")["shuffle"], True)
        self.assertIs(self.ok("shuffle", name="off")["shuffle"], False, "false でも省かない (ポインタ)")
        self.assertEqual(self.ok("repeat", name="one")["repeat"], "One")
        self.assertEqual(self.ok("repeat", name="off")["repeat"], "Off")
        st = self.status()
        self.assertIs(st["shuffle"], False)
        self.assertEqual(st["repeat"], "Off")

    def test_shuffle_and_repeat_change_gen(self):
        self.fresh(repeat="off")
        gen = self.status()["gen"]
        self.ok("shuffle", name="on")
        gen2 = self.status()["gen"]
        self.assertGreater(gen2, gen)
        self.ok("repeat", name="all")
        self.assertGreater(self.status()["gen"], gen2)
        self.ok("shuffle", name="off")

    def test_eq_preset_and_band(self):
        self.fresh()
        self.assertEqual(self.ok("eq", name="Rock")["eq_preset"], "Rock")
        st = self.wait(lambda st: st.get("eq_preset") == "Rock", 3, "eq Rock")
        self.assertEqual(st["eq"], [5, 4, 2, -1, -2, 2, 4, 5, 5, 5])
        # band 0 (omitempty で band が落ちても 0 番) と band 3
        self.assertEqual(self.ok("eq", band=0, value=3.5)["eq_preset"], "Custom")
        self.ok("eq", band=3, value=-4)
        st = self.wait(lambda st: st["eq"][0] == 3.5 and st["eq"][3] == -4, 3, "eq の帯域")
        self.assertEqual(st["eq_preset"], "Custom")
        self.ok("eq", name="Flat")
        self.wait(lambda st: st["eq"] == [0] * 10, 3, "eq Flat")

    def test_device_list_format(self):
        data = self.ok("device", name="list")
        lines = data["device"].split("\n")
        self.assertTrue(lines)
        self.assertEqual(sum(1 for line in lines if line.startswith("* ")), 1, lines)
        self.assertTrue(all(line.startswith(("* ", "  ")) for line in lines), lines)

    def test_play_does_nothing_when_stopped(self):
        """TUI の play は一時停止の解除だけ。停止中の再生は toggle。"""
        self.fresh()
        self.ok("stop")
        self.wait(lambda st: st["state"] == "stopped", 3, "stop")
        self.ok("play")
        time.sleep(0.6)
        self.assertEqual(self.status()["state"], "stopped")
        self.ok("toggle")
        self.wait(lambda st: st["state"] == "playing", 5, "停止中の toggle で再生")

    def test_pause_and_play(self):
        self.fresh()
        self.ok("pause")
        self.wait(lambda st: st["state"] == "paused", 3, "pause")
        self.ok("play")
        self.wait(lambda st: st["state"] == "playing", 3, "play で再開")

    def test_next_and_prev(self):
        self.fresh(index=1, repeat="off")
        self.ok("next")
        self.wait(lambda st: st.get("index") == 2, 5, "next")
        self.ok("prev")
        self.wait(lambda st: st.get("index") == 1, 5, "prev")


def _real_socket_problem() -> str:
    if not REAL_SOCKET:
        return "CLIAMP_MUSIC_REAL_SOCKET がありません"
    if os.path.realpath(REAL_SOCKET) == _user_socket():
        return "利用者の cliamp のソケットには当てません (リストを差し替えて音楽を壊すため)"
    if not os.path.exists(REAL_SOCKET):
        return f"{REAL_SOCKET} がありません"
    return ""


class FakeConformance(_Conformance, unittest.TestCase):
    """偽の cliamp に同じ断言を当てる (いつも走る)。"""

    lyrics_found = ("青い灯台", "夜明けのバス停")
    lyrics_missing = ("真夜中ポスト", "シティライト・ブルース")
    local_query = "Aurora"

    @classmethod
    def setUpClass(cls):
        cls.sock = temp_socket_path()
        # 本物の側と同じく Spotify は資格情報なし (needs_auth)
        cls.fake = FakeCliamp(cls.sock, spotify_needs_auth=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.stop()


# 偽が本物 (2026-09-26 に本物のパッチ済み cliamp 1.50.0 で確かめた) とずれている断言。
# 試験の全体を緑に保つため偽の側だけ expectedFailure にする。偽を直すと
# 「unexpected success」で落ちるので、そのときここから外すこと。
# 2026-09-26: 偽を本物に合わせ直し (文言・引数の確かめ・enqueue / remove / queue_edit /
# load_provider・TOML に残る欄・providers・radio の形)、いまは空。
KNOWN_FAKE_GAPS: tuple[str, ...] = ()


def _expect_gap(name: str):
    # _Conformance の関数そのものに印を付けると RealConformance にも効いてしまうので包む
    base = getattr(_Conformance, name)

    @functools.wraps(base)
    def gap(self):
        return base(self)

    return unittest.expectedFailure(gap)


for _gap_name in KNOWN_FAKE_GAPS:
    setattr(FakeConformance, _gap_name, _expect_gap(_gap_name))


@unittest.skipIf(_real_socket_problem(), _real_socket_problem())
class RealConformance(_Conformance, unittest.TestCase):
    """本物のパッチ済み cliamp (TUI モード) に同じ断言を当てる。"""

    real = True
    network = REAL_NETWORK
    sock = REAL_SOCKET

    @classmethod
    def setUpClass(cls):
        def pair(name: str, default: tuple[str, str]) -> tuple[str, str]:
            value = os.environ.get(name, "")
            return tuple(value.split("|", 1)) if "|" in value else default

        cls.lyrics_found = pair("CLIAMP_MUSIC_REAL_LYRICS", ("YOASOBI", "夜に駆ける"))
        cls.lyrics_missing = pair("CLIAMP_MUSIC_REAL_LYRICS_MISSING",
                                  ("Nobody Artist zzqx", "Nonexistent Lyrics Song zzqx"))
        cls.local_query = os.environ.get("CLIAMP_MUSIC_REAL_LOCAL_QUERY", "Silent Track")


if __name__ == "__main__":
    unittest.main()
