"""cliamp の IPC ソケットとのやりとり。

- 要求は 1 つにつき 1 本の接続を張る。cliamp は 1 本の接続の要求を順番に処理するので、
  カタログ系の長い要求 (検索は数秒〜数十秒) の後ろに他の要求を並べないためである。
- 送る列は 3 つに分ける (どれもスレッドで送り、main loop は待たない):
  - 順番の列 (ORDERED_COMMANDS: toggle / next / seek_to / queue_edit / replace など、状態を
    変えるもの): 1 本のスレッドが、前の要求の応答を待ってから次を送る。cliamp は接続ごとの
    goroutine で処理するので、別々の接続で続けて送ると届く順が入れ替わるため。1 つが
    offline / timeout で終われば、列に残っているものはすぐ同じ失敗で返す (固まった cliamp に
    溜めて、後でまとめて届かないように)。
  - 状態の読み取りの列 (playlist / capabilities / device list など): 小さなプール (2 本)。
  - カタログの列 (CATALOG_COMMANDS): 最大 4 本のプール。利用者の操作 (load_provider と
    ローカルのプレイリストの編集) を裏の読み込みより先に送る。lane を付けた要求は、同じ
    lane の新しい要求が来たら (まだ送っていなければ) 送らずに "cancelled" で返す
    (検索の打ち直し)。
  状態系の 2 つの列は待ち時間を「頼んだ時点」から数え、待つうちに時間切れになったものは
  送らずに timeout で返す (楽観的な表示が戻った後で、古い操作が遅れて届かないように)。
- 状態 (status) は専用のスレッドが専用の接続で定期的に取る。切れたら
  1→2→4→5 秒の間隔で繋ぎ直し、繋がるたびに capabilities で拡張の有無を調べる。
- 結果とシグナルは必ず GLib.idle_add で main loop に戻す (GTK のスレッドで待たない)。

GLib/GObject だけに依存し、GTK は使わない。
"""

from __future__ import annotations

import os
import queue
import socket
import threading
import time
from typing import Callable

from gi.repository import GLib, GObject

from . import log
from .protocol import (
    CATALOG_COMMANDS,
    ORDERED_COMMANDS,
    Response,
    Status,
    decode_response,
    encode_request,
    parse_status,
)

STATE_TIMEOUT = 5.0
CATALOG_TIMEOUT = 130.0
CONNECT_TIMEOUT = 3.0
MAX_WORKERS = 4  # カタログの列
READ_WORKERS = 2  # 状態の読み取りの列
# 先に送るカタログ系 (利用者が押した操作。裏の読み込みの後ろに並ばせない)
URGENT_CATALOG = frozenset({"load_provider", "playlist_add", "playlist_delete", "playlist_remove_track"})


def default_socket_path() -> str:
    """cliamp と同じ既定の場所。cliamp は os.UserHomeDir()/.config/cliamp を使い、
    XDG_CONFIG_HOME を見ない。環境変数 CLIAMP_MUSIC_SOCKET があればそれを優先する。"""
    override = os.environ.get("CLIAMP_MUSIC_SOCKET")
    if override:
        return override
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "cliamp", "cliamp.sock")


def default_timeout(cmd: str) -> float:
    """コマンドごとの待ち時間 (状態系 5 秒、カタログ系 130 秒)。"""
    return CATALOG_TIMEOUT if cmd in CATALOG_COMMANDS else STATE_TIMEOUT


class LineConnection:
    """改行区切りの JSON を 1 行ずつやりとりする接続。応答が何 MB あっても最後まで読む。"""

    def __init__(self, path: str, connect_timeout: float = CONNECT_TIMEOUT):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buf = bytearray()
        try:
            self._sock.settimeout(connect_timeout)
            self._sock.connect(path)
        except BaseException:
            self._sock.close()
            raise

    def call(self, payload: bytes, timeout: float) -> bytes:
        """1 行送って 1 行受け取る。時間切れは TimeoutError、切断は ConnectionError。"""
        deadline = time.monotonic() + timeout
        view = memoryview(payload)
        while view:
            self._sock.settimeout(self._remaining(deadline))
            sent = self._sock.send(view)
            view = view[sent:]
        start = 0
        while True:
            newline = self._buf.find(b"\n", start)
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return line
            start = len(self._buf)
            self._sock.settimeout(self._remaining(deadline))
            chunk = self._sock.recv(1 << 20)
            if not chunk:
                raise ConnectionResetError("cliamp が応答の途中で接続を閉じました")
            self._buf += chunk

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("時間切れ")
        return remaining

    def shutdown(self) -> None:
        """別のスレッドで待っている recv を起こす。"""
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def exchange(path: str, payload: bytes, timeout: float) -> Response:
    """1 本の接続で 1 要求を送り、応答を Response で返す (同期。スレッドから呼ぶ)。"""
    deadline = time.monotonic() + timeout
    try:
        conn = LineConnection(path, min(CONNECT_TIMEOUT, timeout))
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
        return Response.offline(f"cliamp に接続できません ({exc.strerror or exc})")
    except TimeoutError:
        return Response.offline("cliamp に接続できません (時間切れ)")
    except OSError as exc:
        return Response.offline(f"cliamp に接続できません ({exc})")
    try:
        line = conn.call(payload, max(0.001, deadline - time.monotonic()))
    except TimeoutError:
        return Response.timeout("cliamp の応答が時間内にありませんでした")
    except OSError as exc:
        return Response.offline(f"cliamp との接続が切れました ({exc})")
    finally:
        conn.close()
    return decode_response(line)


def send_once(path: str, cmd: str, *, timeout: float | None = None, **fields) -> Response:
    """同期で 1 要求を送る小道具 (試験や --self-check 向け。GTK のスレッドでは使わない)。"""
    return exchange(path, encode_request(cmd, **fields), default_timeout(cmd) if timeout is None else timeout)


def _deliver(callback: Callable[..., object], response: Response, *extra) -> bool:
    try:
        callback(response, *extra)
    except Exception as exc:  # 呼び出し側の誤りで main loop を止めない
        import traceback

        log(f"応答の処理で例外: {exc}")
        traceback.print_exc()
    return GLib.SOURCE_REMOVE


def is_ordered(cmd: str, fields: dict) -> bool:
    """状態を変える (順番の列で送る) 要求か。device は "list" 以外 (切り替え) だけ。"""
    if cmd not in ORDERED_COMMANDS:
        return False
    if cmd == "device":
        return str(fields.get("name") or "").lower() != "list"
    return True


class _Job:
    """1 つの要求。worker のスレッドが run() する。"""

    __slots__ = ("cmd", "payload", "timeout", "submitted", "from_submit", "callback", "parse", "lane",
                 "started", "superseded")

    def __init__(self, cmd: str, payload: bytes, timeout: float, callback, parse, lane: str | None,
                 from_submit: bool):
        self.cmd = cmd
        self.payload = payload
        self.timeout = timeout
        self.submitted = time.monotonic()
        self.from_submit = from_submit
        self.callback = callback
        self.parse = parse
        self.lane = lane
        self.started = False
        self.superseded = False


class _Pool:
    """要求を送る worker の群れ。必要になったときに max_workers まで増やす。

    priority: 小さい数を先に送る (同じなら頼んだ順)。fail_fast: 1 つが offline / timeout で
    終わったら、待っているものをすぐ同じ失敗で返す (順番の列)。"""

    def __init__(self, name: str, max_workers: int, run: Callable[[_Job], Response],
                 finish: Callable[[_Job, Response], None], *, fail_fast: bool = False):
        self.name = name
        self.max_workers = max_workers
        self._run = run
        self._finish = finish
        self._fail_fast = fail_fast
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._workers: list[threading.Thread] = []
        self._idle = 0
        self._queued = 0
        self._seq = 0
        self._lock = threading.Lock()

    def submit(self, job: _Job, priority: int = 0) -> None:
        with self._lock:
            self._seq += 1
            self._jobs.put((priority, self._seq, job))
            self._queued += 1
            # 手の空いた worker より待ちの仕事が多ければ増やす (遅い検索の後ろに並ばせない)。
            if self._queued > self._idle and len(self._workers) < self.max_workers:
                worker = threading.Thread(target=self._work, name=self.name, daemon=True)
                self._workers.append(worker)
                self._idle += 1
                worker.start()

    def _take(self, block: bool = True) -> _Job | None:
        try:
            _priority, _seq, job = self._jobs.get(block=block)
        except queue.Empty:
            return None
        with self._lock:
            self._queued -= 1
        return job

    def _work(self) -> None:
        while True:
            job = self._take()
            with self._lock:
                self._idle -= 1
            try:
                response = self._run(job)
                self._finish(job, response)
                if self._fail_fast and response.kind in ("offline", "timeout"):
                    while (rest := self._take(block=False)) is not None:
                        failed = Response.offline() if response.kind == "offline" else Response.timeout(
                            "前の操作に cliamp が応えなかったので送りませんでした")
                        self._finish(rest, failed)
            except Exception as exc:
                log(f"要求の処理で例外: {exc}")
            finally:
                with self._lock:
                    self._idle += 1


class CliampClient(GObject.Object):
    """cliamp のソケットの窓口。

    シグナル:
      "connection-changed" (bool connected) — 接続か api / capabilities が変わった。
          起動後の最初の確認の結果は、未接続でも必ず 1 回出す。
      "status" (object Status) — 状態を取るたび (表示中は 0.4 秒ごと程度)。
    属性 (main loop でだけ書き換わる): socket_path, connected, api (0 = 拡張なし),
    capabilities (capabilities の応答から "commands" と "eq_presets")。
    """

    __gtype_name__ = "CliampMusicClient"
    __gsignals__ = {
        "connection-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "status": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    # 繋ぎ直しの間隔 (秒)。試験で縮められるようクラス属性にしておく。
    BACKOFF_MIN = 1.0
    BACKOFF_MAX = 5.0

    def __init__(self, socket_path: str | None = None):
        super().__init__()
        self.socket_path: str = socket_path or default_socket_path()
        self.connected: bool = False
        self.api: int = 0
        self.capabilities: dict = {}
        # 最後に送った status の通し番号 (状態取得のスレッドが書く)。
        # 操作の応答が来た時点のこの値より新しい status は、操作の後の状態を映している。
        self.last_status_seq: int = 0

        self._ordered = _Pool("cliamp-ordered", 1, self._run_job, self._finish_job, fail_fast=True)
        self._reads = _Pool("cliamp-read", READ_WORKERS, self._run_job, self._finish_job)
        self._catalog = _Pool("cliamp-request", MAX_WORKERS, self._run_job, self._finish_job)
        self._lanes: dict[str, _Job] = {}
        self._lanes_lock = threading.Lock()

        self._poll_interval = 0.4
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._probe_requested = False
        self._thread: threading.Thread | None = None
        self._conn: LineConnection | None = None
        self._announced: bool | None = None

    # --- 1 要求 1 接続 -----------------------------------------------------

    def request(self, cmd: str, callback: Callable[..., object] | None = None, *,
                timeout: float | None = None, lane: str | None = None,
                parse: Callable[[Response], object] | None = None, **fields) -> None:
        """要求を 1 つ送る。callback(Response) は main loop で必ず 1 回呼ばれる。

        timeout の既定: 状態系 5 秒 (頼んだ時点から数える)、カタログ系 130 秒。
        値が None の欄は送らない。
        lane: カタログ系で、同じ lane の後の要求が来たら (まだ送っていなければ) 送らずに
        Response.cancelled で返す (検索の打ち直しなど)。
        parse: worker のスレッドで応答を読む関数。渡すと callback(Response, 結果) で呼ぶ
        (長いリストの解析で main loop を止めない。例外は None として渡す)。
        """
        if timeout is None:
            timeout = default_timeout(cmd)
        payload = encode_request(cmd, **fields)
        catalog = cmd in CATALOG_COMMANDS
        job = _Job(cmd, payload, timeout, callback, parse, lane if catalog else None, not catalog)
        if catalog:
            if lane:
                with self._lanes_lock:
                    previous = self._lanes.get(lane)
                    if previous is not None and not previous.started:
                        previous.superseded = True
                    self._lanes[lane] = job
            self._catalog.submit(job, 0 if cmd in URGENT_CATALOG else 1)
        elif is_ordered(cmd, fields):
            self._ordered.submit(job)
        else:
            self._reads.submit(job)

    def _run_job(self, job: _Job) -> Response:
        """worker のスレッドで 1 要求を送り、応答を返す。"""
        with self._lanes_lock:
            job.started = True
            if job.lane and self._lanes.get(job.lane) is job:
                del self._lanes[job.lane]
            superseded = job.superseded
        if superseded:
            return Response.cancelled()
        timeout = job.timeout
        if job.from_submit:
            timeout -= time.monotonic() - job.submitted
            if timeout <= 0:
                # 待つうちに時間切れ。遅れて届くと楽観的な表示が戻った後で効いてしまうので送らない
                return Response.timeout("cliamp への要求が待ちきれませんでした")
        response = exchange(self.socket_path, job.payload, timeout)
        if response.kind == "offline":
            # 状態取得のスレッドにすぐ確かめさせる (未接続の表示を早く出す)。
            self._wake.set()
        return response

    @staticmethod
    def _finish_job(job: _Job, response: Response) -> None:
        if job.callback is None:
            return
        if job.parse is None:
            GLib.idle_add(_deliver, job.callback, response)
            return
        try:
            parsed = job.parse(response)
        except Exception as exc:
            log(f"応答の解析で例外: {exc}")
            parsed = None
        GLib.idle_add(_deliver, job.callback, response, parsed)

    # --- 状態の定期取得 ------------------------------------------------------

    def start(self) -> None:
        """状態の定期取得を始める (専用スレッド・専用接続)。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="cliamp-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """定期取得を止める。1 要求の request はその後も使える。"""
        self._stopping.set()
        self._wake.set()
        conn = self._conn
        if conn is not None:
            conn.shutdown()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def set_poll_interval(self, seconds: float) -> None:
        """状態を取る間隔。表示中 0.4 秒、隠れているとき 1.5 秒の想定。

        変わったときだけ状態取得のスレッドを起こす (窓のフォーカスが動くたびに余計に取らない)。"""
        interval = min(10.0, max(0.05, float(seconds)))
        if interval != self._poll_interval:
            self._poll_interval = interval
            self._wake.set()

    def poll_now(self) -> None:
        """次の状態取得を待たずに今すぐ取らせる (操作の直後に使う)。"""
        self._wake.set()

    def probe(self) -> None:
        """capabilities を取り直す。繋ぎ直したときは自動で行う。"""
        self._probe_requested = True
        self._wake.set()

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _probe_on(self, conn: LineConnection) -> tuple[int, dict]:
        response = decode_response(conn.call(encode_request("capabilities"), STATE_TIMEOUT))
        if not response.ok:
            # "unknown command" (拡張なし) も、その他の失敗も基本操作だけとみなす。
            return 0, {}
        caps = {
            "commands": [str(c) for c in response.data.get("commands") or [] if isinstance(c, str)],
            "eq_presets": [str(p) for p in response.data.get("eq_presets") or [] if isinstance(p, str)],
        }
        api = response.data.get("api")
        return (int(api) if isinstance(api, (int, float)) and not isinstance(api, bool) and api > 0 else 1), caps

    def _announce(self, connected: bool, api: int | None, caps: dict | None) -> None:
        first = self._announced is None
        self._announced = connected
        GLib.idle_add(self._apply_connection, connected, api, caps, first)

    def _apply_connection(self, connected: bool, api: int | None, caps: dict | None, force: bool) -> bool:
        changed = force or connected != self.connected
        self.connected = connected
        if api is not None:
            if api != self.api or caps != self.capabilities:
                changed = True
            self.api = api
            self.capabilities = caps or {}
        if changed:
            self.emit("connection-changed", connected)
        return GLib.SOURCE_REMOVE

    def _emit_status(self, status: Status) -> bool:
        self.emit("status", status)
        return GLib.SOURCE_REMOVE

    def _poll_loop(self) -> None:
        backoff = self.BACKOFF_MIN
        conn: LineConnection | None = None
        seq = 0
        status_payload = encode_request("status")
        while not self._stopping.is_set():
            fresh = False
            if conn is None:
                try:
                    conn = LineConnection(self.socket_path)
                    self._conn = conn
                    api, caps = self._probe_on(conn)
                    self._probe_requested = False
                except OSError:
                    if conn is not None:
                        conn.close()
                    conn = self._conn = None
                    if self._stopping.is_set():
                        break
                    if self._announced is not False:
                        self._announce(False, None, None)
                    self._sleep(backoff)
                    backoff = min(self.BACKOFF_MAX, backoff * 2)
                    continue
                # capabilities に答えたなら cliamp は生きている (status は TUI 次第で遅れることがある)。
                fresh = True
                self._announce(True, api, caps)
            try:
                if self._probe_requested:
                    self._probe_requested = False
                    self._announce(True, *self._probe_on(conn))
                seq += 1
                self.last_status_seq = seq
                line = conn.call(status_payload, STATE_TIMEOUT)
                received = time.monotonic()
            except TimeoutError:
                # TUI の Update が止まっている (yt-dlp の読み込みなど) だけのことがある。
                # 接続は張り直すが、繋ぎ直しで capabilities が返れば未接続とは言わない。
                conn.close()
                conn = self._conn = None
                continue
            except OSError:
                conn.close()
                conn = self._conn = None
                if self._stopping.is_set():
                    break
                if self._announced is not False:
                    self._announce(False, None, None)
                if fresh:
                    # 繋いだ直後に切られた。すぐ繋ぎ直すと空回りするので間隔を空ける。
                    self._sleep(backoff)
                    backoff = min(self.BACKOFF_MAX, backoff * 2)
                # それ以外 (しばらく繋がっていた接続が切れた) は 1 度だけすぐ繋ぎ直す。
                continue
            backoff = self.BACKOFF_MIN
            response = decode_response(line)
            if response.ok:
                status = parse_status(response.data)
                status.seq = seq
                status.stamp = received
                GLib.idle_add(self._emit_status, status)
            self._sleep(self._poll_interval)
        if conn is not None:
            conn.close()
        self._conn = None
