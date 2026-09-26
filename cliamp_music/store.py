"""再生状態の唯一の置き場 (PlayerStore)。

画面の部品はここを読み、ここのメソッドで操作する。cliamp との通信は
CliampClient に任せ、結果は main loop で受け取る。

操作は「楽観的」に行う: 送った瞬間に手元の status を書き換えてシグナルを出し、
画面をすぐ変える。cliamp の応答 (受け付けた) が来た後に送った status が
届いた時点で、本物の値に合わせる。応答より前に送られていた status は操作前の
状態を映しているので、それでは戻さない (つまみが一瞬戻る「ちらつき」を防ぐ)。
シークだけは応答の後も、届いた位置がシーク先に着くまで (最大 SEEK_SETTLE 秒) 保つ
(HTTP の流れは応答の後で繋ぎ直すので、その間の status は古い位置を返す)。

曲の切り替え中 (yt-dlp・流れの曲を読み込んでいる間) の cliamp は、新しい曲と
buffering を返しつつ、位置と長さはまだ鳴っている前の曲のものを返す。その間は
switching を立て、位置 0・長さは新しい曲の長さとして見せ、シークさせない (can_seek)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace as dc_replace
from typing import Callable, Iterable

from gi.repository import GLib, GObject

from . import log
from .client import CliampClient
from .protocol import (
    ALL_COMMANDS,
    EQ_MAX_DB,
    EQ_MIN_DB,
    LEGACY_COMMANDS,
    REPEAT_MODES,
    STALE,
    Device,
    PlaylistState,
    Response,
    Source,
    Status,
    Track,
    clamp_volume,
    parse_device_list,
    parse_playlist,
    parse_tracks,
)

SPEED_MIN = 0.25
SPEED_MAX = 2.0

ResponseCallback = Callable[[Response], object] | None


@dataclass
class _Override:
    """楽観的に書き換えた値。ack_seq は操作の応答が来た時点の status の通し番号。

    seek は位置の書き換えがシークか (応答の後も、届く位置が着くまで保つ)。sig はそのときの
    曲 (path, index)、acked_at は応答が来た時刻。"""

    value: object
    since: float
    expires: float
    ack_seq: int | None = None
    seek: bool = False
    sig: tuple[str, int] | None = None
    acked_at: float = 0.0


class PlayerStore(GObject.Object):
    """再生状態 (status / playlist / history) と操作。

    シグナル (引数なし):
      "status-changed"     状態が変わった (位置の更新を含む。0.4 秒ごと程度)
      "track-changed"      再生中の曲 (path と index) が変わった
      "state-changed"      playing/paused/stopped/offline が変わった
      "playlist-changed"   リストの写し (self.playlist) を取り直した
      "history-changed"    self.history を取り直した
      "connection-changed" 接続や api が変わった
    """

    __gtype_name__ = "CliampMusicPlayerStore"
    __gsignals__ = {
        name: (GObject.SignalFlags.RUN_FIRST, None, ())
        for name in ("status-changed", "track-changed", "state-changed", "playlist-changed",
                     "history-changed", "connection-changed")
    }

    # 曲が変わってから履歴を取り直すまで (ms)。cliamp は半分聞いた時点で記録するので急がない。
    HISTORY_DELAY_MS = 2500
    # gen の変化などでリストを取り直すときにまとめる時間 (ms)。
    PLAYLIST_DEBOUNCE_MS = 120
    # 応答が来ないまま楽観的な値を保つ上限 (秒)。
    OVERRIDE_TTL = 6.0
    # シークの応答の後、届いた位置がシーク先に着くのを待つ上限 (秒)。
    SEEK_SETTLE = 3.0
    HISTORY_LIMIT = 50
    # リスト・履歴の取得に失敗したときの取り直しの間隔 (ms、倍々で最大 30 秒)
    RETRY_MIN_MS = 1000
    RETRY_MAX_MS = 30000

    def __init__(self, client: CliampClient):
        super().__init__()
        self.client = client
        self.status = Status()
        self.playlist = PlaylistState()
        self.history: list[Track] = []
        self._base_time = time.monotonic()
        self._overrides: dict[str, _Override] = {}
        self._track_sig: tuple[str, int] | None = None
        self._pl_inflight = False
        self._pl_dirty = False
        self._pl_timer = 0
        self._pl_gen_seen: int | None = None
        self._pl_fail = 0
        self._pl_retry = 0
        self._hist_inflight = False
        self._hist_dirty = False
        self._hist_timer = 0
        self._hist_fail = 0
        self._coalesce: dict[tuple, list] = {}
        # 曲の切り替え (読み込み中) を見分ける: 最後に読み込みの終わった曲と、応答済みの切り替え
        self._settled_sig: tuple[str, int] | None = None
        self._switch_pending = False
        self.switching = False
        # GUI で選んだ出力先 (cliamp の "* " は既定の sink で、切り替えても動かないため)
        self._chosen_device: str | None = None
        client.connect("status", self._on_status)
        client.connect("connection-changed", self._on_connection)
        if client.connected:
            # 繋がった後で作られたときは、接続の知らせを待たずに履歴を取る。
            self.refresh_history()

    # --- 読み取り -----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self.client.connected

    @property
    def api(self) -> int:
        return self.client.api

    @property
    def eq_presets(self) -> list[str]:
        """組み込みの EQ プリセット名 (capabilities から。拡張なしなら空)。"""
        return list(self.client.capabilities.get("eq_presets") or [])

    def supports(self, cmd: str) -> bool:
        """cliamp がそのコマンドを知っているか (接続の有無は見ない)。"""
        if self.client.api >= 1:
            commands = self.client.capabilities.get("commands")
            if commands:
                return cmd in commands
            return cmd in ALL_COMMANDS
        return cmd in LEGACY_COMMANDS

    def current_track(self) -> Track | None:
        return self.status.track

    def can_seek(self) -> bool:
        """いまシークできるか (再生バー・ミニプレーヤー・フルスクリーン・キーの共通の判断)。

        曲の切り替え中は cliamp の位置と長さが前の曲のもので、シークは前の曲に効いて
        失われるので止める。"""
        st = self.status
        return (self.client.connected and st.track is not None and not st.is_live and st.duration > 0
                and self.supports("seek_to") and not self.switching)

    def continues_by_reshuffle(self) -> bool:
        """シャッフルとリピート (すべて) で、次に再生の並びが尽きても混ぜ直して続けるか。

        cliamp の up_next はシャッフル中の回り込みを含めない (次の混ぜ方が決まっていない)。"""
        st = self.status
        return bool(st.shuffle and st.repeat == "all" and max(st.total, self.playlist.total) > 1)

    def position_now(self) -> float:
        """最後の status から経過時間で補間した再生位置 (秒)。再生中のみ進む。"""
        st = self.status
        if st.state != "playing" or st.buffering:
            return max(0.0, st.position)
        speed = st.speed if st.speed > 0 else 1.0
        pos = st.position + max(0.0, time.monotonic() - self._base_time) * speed
        if st.duration > 0 and not st.is_live:
            pos = min(pos, st.duration)
        return max(0.0, pos)

    # --- 受信 -----------------------------------------------------------------------

    def _on_connection(self, _client, connected: bool) -> None:
        old = self.status
        self._cancel_retries()
        self._settled_sig = None
        self._switch_pending = False
        self.switching = False
        self._chosen_device = None  # 繋ぎ直した cliamp の流れは既定の sink から始まる
        if connected:
            # リストは次の status (最初の 1 回は必ず曲が「変わった」扱い) で取り直す。
            self._pl_gen_seen = None
            stale_playlist = stale_history = False
            if self.client.api < 1:
                # 拡張の無い cliamp (別の cliamp に繋ぎ直したときなど) からはリストの中身も
                # 履歴も取れない。前の接続の写しを残すと、古い曲が今のものに見える
                stale_playlist = bool(self.playlist.tracks) or self.playlist.index >= 0
                stale_history = bool(self.history)
                self.playlist = PlaylistState()
                self.history = []
            self.emit("connection-changed")
            if stale_playlist:
                self.emit("playlist-changed")
            if stale_history:
                self.emit("history-changed")
            self.refresh_history()
            return
        self._overrides.clear()
        self.status = Status(state="offline", volume=old.volume, shuffle=old.shuffle,
                             repeat=old.repeat, speed=old.speed)
        self._base_time = time.monotonic()
        track_changed = self._track_sig is not None and self._track_sig != ("", 0)
        self._track_sig = None
        self.emit("connection-changed")
        if track_changed:
            self.emit("track-changed")
        if old.state != "offline":
            self.emit("state-changed")
        self.emit("status-changed")

    def _on_status(self, _client, status: Status) -> None:
        now = time.monotonic()
        base = status.stamp or now
        base = self._apply_overrides(status, now, base)
        sig = (status.track.path if status.track else "", status.index)
        self._apply_switching(status, sig)
        old = self.status
        self.status = status
        self._base_time = base
        first = self._track_sig is None
        track_changed = sig != self._track_sig
        self._track_sig = sig
        state_changed = status.state != old.state

        if self.client.api >= 1 and self.supports("playlist"):
            # 次に来る曲 (up_next) は今の曲・シャッフル・リピートでも変わる。
            if (status.gen != self._pl_gen_seen or track_changed or status.shuffle != old.shuffle
                    or status.repeat != old.repeat):
                self._pl_gen_seen = status.gen
                self._schedule_playlist_refresh()
            elif (not self._pl_inflight and not self._pl_retry and not self._pl_timer
                  and self.playlist.gen and status.total != self.playlist.total):
                # 取り直しに失敗したまま写しが古い (件数が合わない)。安く確かめ直す
                self._schedule_playlist_refresh()
        elif status.index != self.playlist.index or status.total != self.playlist.total:
            # 拡張の無い cliamp ではリストの中身は取れない。位置と件数だけ写す。
            self.playlist = PlaylistState(index=status.index, total=status.total)
            self.emit("playlist-changed")
        if track_changed and not first:
            self._schedule_history_refresh()

        if track_changed:
            self.emit("track-changed")
        if state_changed:
            self.emit("state-changed")
        self.emit("status-changed")

    def _apply_switching(self, status: Status, sig: tuple[str, int]) -> None:
        """曲の切り替え中 (読み込み中) なら、位置 0・長さは新しい曲のものにする。

        cliamp は読み込みの間も前の曲を鳴らし続け、位置と長さはその曲のものを返す。
        buffering だけでは決めない (TUI が URL やフィードを読む間も立つが、そのときの曲は
        正しく鳴っている)。最後に読み込みの終わった曲から変わったか、こちらの next / prev /
        play_index が受け付けられた後だけを切り替え中とみなす。"""
        if not status.buffering or status.track is None:
            self._settled_sig = sig
            self._switch_pending = False
            self.switching = False
            return
        if sig != self._settled_sig or self._switch_pending:
            self.switching = True
            status.position = 0.0
            status.duration = float(status.track.duration) if status.track.duration > 0 else 0.0
        else:
            self.switching = False

    def _apply_overrides(self, status: Status, now: float, base: float) -> float:
        """まだ本物に反映されていない楽観的な値を、届いた status に重ねる。"""
        if not self._overrides:
            return base
        for name in ("state",) + tuple(k for k in self._overrides if k != "state"):
            ov = self._overrides.get(name)
            if ov is None:
                continue
            if now > ov.expires:
                del self._overrides[name]
                continue
            if ov.ack_seq is not None and status.seq > ov.ack_seq:
                if not (ov.seek and self._seek_pending(ov, status, now)):
                    del self._overrides[name]
                    continue
            if name == "position":
                pos = float(ov.value)
                if status.state == "playing" and not status.buffering:
                    pos += (now - ov.since) * (status.speed or 1.0)
                if status.duration > 0:
                    pos = min(pos, status.duration)
                status.position = max(0.0, pos)
                base = now
            else:
                setattr(status, name, ov.value)
        return base

    def _seek_pending(self, ov: _Override, status: Status, now: float) -> bool:
        """応答の後の status がまだシーク先に着いていないか (まだ楽観的な位置を見せるか)。

        HTTP の流れ (ポッドキャストなど) は cliamp が応答した後で繋ぎ直すので、その間の
        status は止まった古い位置を返す。着いた (シーク先の近く)・曲が変わった・止まった・
        応答から SEEK_SETTLE 秒たった、のどれかで本物に戻す。"""
        if not ov.acked_at:
            ov.acked_at = now
        if now - ov.acked_at > self.SEEK_SETTLE or status.state == "stopped":
            return False
        sig = (status.track.path if status.track else "", status.index)
        if ov.sig is not None and sig != ov.sig:
            return False
        target = float(ov.value)
        if status.duration > 0:
            target = min(target, max(0.0, status.duration - 1.0))
        expected = target + max(0.0, now - ov.since) * (status.speed or 1.0)
        return not (target - 1.0 <= status.position <= expected + 1.5)

    # --- 楽観的な変更 -----------------------------------------------------------------

    def _optimistic(self, seek: bool = False, **values) -> dict[str, _Override]:
        now = time.monotonic()
        made = {}
        st = self.status
        old_state = st.state
        if "position" in values or "state" in values:
            # 補間の起点を今に移してから変える (再生中の位置を失わない)。
            st.position = self.position_now()
            self._base_time = now
        sig = (st.track.path if st.track else "", st.index)
        for name, value in values.items():
            ov = _Override(value=value, since=now, expires=now + self.OVERRIDE_TTL,
                           seek=seek and name == "position", sig=sig)
            self._overrides[name] = ov
            made[name] = ov
            setattr(st, name, value)
        if "state" in values and st.state != old_state:
            self.emit("state-changed")
        self.emit("status-changed")
        return made

    def _command(self, cmd: str, *, optimistic: dict | None = None, callback: ResponseCallback = None,
                 seek: bool = False, switch: bool = False, **fields) -> None:
        """seek: 楽観的な位置はシーク (着くまで保つ)。switch: 曲を替える操作 (next / prev /
        play_index)。受け付けられたら、その後の読み込み中を切り替え中とみなす。"""
        made = self._optimistic(seek=seek, **optimistic) if optimistic and self.client.connected else {}

        def done(response: Response) -> None:
            now = time.monotonic()
            for name, ov in made.items():
                if self._overrides.get(name) is not ov:
                    continue  # もっと新しい操作が上書きした
                if response.ok:
                    ov.ack_seq = self.client.last_status_seq
                    ov.acked_at = now
                else:
                    del self._overrides[name]
            if switch and response.ok:
                self._switch_pending = True
            if not response.ok and response.kind not in ("offline",):
                log(f"{cmd} に失敗: {response.error}")
            self.client.poll_now()
            if callback is not None:
                callback(response)

        self.client.request(cmd, done, **fields)

    def _coalesced(self, key: tuple, send: Callable[[object, Callable[[Response], None]], None], value,
                   ack: tuple[str, ...] = ()) -> None:
        """つまみを動かし続けたときに要求を溜めない。送信中は最後の値だけ覚えておく。

        最後の値が受け付けられたら、ack に挙げた楽観的な値を「応答済み」にする
        (それより後の status で本物に合わせる)。
        """
        slot = self._coalesce.get(key)
        if slot is not None:
            slot[0] = value
            return
        slot = [value]
        self._coalesce[key] = slot

        def pump() -> None:
            current = slot[0]

            def finished(response: Response) -> None:
                if slot[0] != current:
                    pump()
                    return
                self._coalesce.pop(key, None)
                if any(other[0] == key[0] for other in self._coalesce):
                    return  # 同じ種類 (EQ の別の帯域など) がまだ送信中
                for name in ack:
                    ov = self._overrides.get(name)
                    if ov is None:
                        continue
                    if response.ok:
                        ov.ack_seq = self.client.last_status_seq
                    else:
                        del self._overrides[name]

            send(current, finished)

        pump()

    # --- 再生の操作 -------------------------------------------------------------------

    def toggle(self) -> None:
        st = self.status
        if st.state == "playing":
            self._command("toggle", optimistic={"state": "paused"})
        elif st.state == "paused" or (st.state == "stopped" and st.total > 0):
            self._command("toggle", optimistic={"state": "playing"})
        else:
            self._command("toggle")

    def play(self) -> None:
        """再生。TUI の play は停止中に何もしないので、停止中は toggle を送る。"""
        st = self.status
        if st.state == "paused":
            self._command("play", optimistic={"state": "playing"})
        elif st.state == "stopped":
            self._command("toggle", optimistic={"state": "playing"} if st.total > 0 else None)
        elif st.state == "offline":
            self._command("toggle")

    def pause(self) -> None:
        if self.status.state == "playing":
            self._command("pause", optimistic={"state": "paused"})

    def stop(self) -> None:
        self._command("stop", optimistic={"state": "stopped", "position": 0.0})

    def next(self) -> None:
        self._command("next", optimistic={"position": 0.0}, switch=True)

    def prev(self) -> None:
        self._command("prev", optimistic={"position": 0.0}, switch=True)

    def seek_to(self, seconds: float) -> None:
        """絶対位置へ。相対の seek は yt-dlp の曲で TUI を止めるので使わない。

        曲の切り替え中 (switching) は送らない (cliamp の位置はまだ前の曲のもので、
        シークはその曲に効いて失われる)。"""
        if not self.supports("seek_to") or self.switching:
            return
        target = max(0.0, float(seconds))
        if self.status.duration > 0:
            target = min(target, self.status.duration)
        self._command("seek_to", optimistic={"position": target}, seek=True, value=round(target, 3))

    def seek_by(self, delta: float) -> None:
        if self.switching:
            return
        self.seek_to(self.position_now() + float(delta))

    def set_volume_db(self, db: float) -> None:
        """音量 (dB、-30〜+6。絶対値)。"""
        db = round(clamp_volume(db), 2)
        self._optimistic(volume=db)

        def send(value, finished):
            self._command("volume", value=value, callback=finished)

        self._coalesced(("volume",), send, db, ack=("volume",))

    def volume_step(self, delta_db: float) -> None:
        self.set_volume_db(self.status.volume + float(delta_db))

    def set_shuffle(self, on: bool) -> None:
        self._command("shuffle", optimistic={"shuffle": bool(on)}, name="on" if on else "off")

    def set_repeat(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in REPEAT_MODES:
            raise ValueError(f"repeat は off / all / one のどれか: {mode!r}")
        self._command("repeat", optimistic={"repeat": mode}, name=mode)

    def cycle_repeat(self) -> None:
        """オフ → すべて → 1 曲 → オフ (cliamp の CycleRepeat と同じ順)。"""
        order = {"off": "all", "all": "one", "one": "off"}
        self.set_repeat(order.get(self.status.repeat, "off"))

    def set_speed(self, x: float) -> None:
        speed = round(min(SPEED_MAX, max(SPEED_MIN, float(x))), 3)
        self._command("speed", optimistic={"speed": speed}, value=speed)

    def set_eq_preset(self, name: str) -> None:
        self._command("eq", optimistic={"eq_preset": name}, name=name)

    def set_eq_band(self, index: int, db: float) -> None:
        index = int(index)
        if not 0 <= index < len(self.status.eq):
            raise ValueError(f"帯域の番号が範囲外: {index}")
        db = round(min(EQ_MAX_DB, max(EQ_MIN_DB, float(db))), 2)
        bands = list(self.status.eq)
        bands[index] = db
        self._optimistic(eq=bands, eq_preset="Custom")

        def send(value, finished):
            # band=0 も送る (cliamp は band が 0 で name が空なら 0 番の帯域とみなす)。
            self._command("eq", band=index, value=value, callback=finished)

        self._coalesced(("eq", index), send, db, ack=("eq", "eq_preset"))

    def list_devices(self, callback: Callable[[list[Device], str], object]) -> None:
        """出力先の一覧。callback(devices, error) — error は取れなかった理由 (取れたら "")。

        active はこのアプリで選んだ出力先 (いま cliamp が選べば) を優先し、無ければ cliamp の
        "* " (既定の sink) を使う。cliamp は流れを move-sink-input で動かすので、"* " は
        切り替えても既定の sink に残るため。"""

        def done(response: Response) -> None:
            if not response.ok:
                callback([], device_error_text(response))
                return
            devices = parse_device_list(response.data)
            chosen = self._chosen_device
            if chosen and any(d.name == chosen for d in devices):
                devices = [Device(d.name, d.label, d.name == chosen) for d in devices]
            callback(devices, "")

        self.client.request("device", done, name="list")

    def set_device(self, name: str, callback: ResponseCallback = None) -> None:
        def done(response: Response) -> None:
            if response.ok:
                self._chosen_device = name
            if callback is not None:
                callback(response)

        self._command("device", name=name, callback=done)

    # --- リストの操作 -------------------------------------------------------------------

    def _guarded(self, callback: ResponseCallback) -> Callable[[Response], None]:
        """添字で曲を指す要求の答え。cliamp が "stale" (その添字の曲が送った path と違う =
        手元の写しが古い) と断ったら、すぐにリストを取り直す (古い添字で送り直さない)。"""

        def done(response: Response) -> None:
            if not response.ok and response.error == STALE:
                self.refresh_playlist(full=True)
            if callback is not None:
                callback(response)

        return done

    def play_index(self, i: int, callback: ResponseCallback = None, path: str | None = None) -> None:
        """リストの i 番目を再生 (TUI の「行で Enter」)。

        path: その添字で見えていた曲の path。パッチ済みの cliamp は違えば "stale" で断る
        (TUI などでリストが動いた後に別の曲を鳴らさない)。古い cliamp は見ない。"""
        self._command("play_index", optimistic={"position": 0.0}, switch=True, index=int(i),
                      path=path or None, callback=self._guarded(callback))

    def play_queued(self, index: int, callback: ResponseCallback = None) -> None:
        """待ち行列に入っている曲 (リスト上の添字 index) を今すぐ再生する。

        play_index ではいけない: cliamp の SetIndex は待ち行列から外さず、リストの位置を
        その曲へ移すので、その曲がもう 1 度鳴り、間のリストの曲が飛ばされる。代わりに
        待ち行列でその曲より前の曲を外し (Apple と同じく飛ばした扱い)、next で待ち行列の
        先頭 (選んだ曲) を鳴らす。リストの位置は動かないので、その後は元の続きから。"""
        queue = list(self.playlist.queue)
        if index not in queue:
            track = self.playlist.track_at(index)
            self.play_index(index, callback, path=track.path if track is not None else None)
            return
        ahead = queue[: queue.index(index)]

        def path_of(i: int) -> str | None:
            track = self.playlist.track_at(i)
            return track.path if track is not None else None

        def step(remaining: list[int]):
            def done(response: Response) -> None:
                if not response.ok:
                    if callback is not None:
                        callback(response)
                    return
                if remaining:
                    self.queue_edit("remove", index=remaining[0], path=path_of(remaining[0]),
                                    callback=step(remaining[1:]))
                else:
                    self._command("next", optimistic={"position": 0.0}, switch=True, callback=callback)

            return done

        if ahead:
            self.queue_edit("remove", index=ahead[0], path=path_of(ahead[0]), callback=step(ahead[1:]))
        else:
            step([])(Response(True))

    def replace(self, tracks: Iterable[Track], index: int = 0, source: Source | dict | None = None,
                callback: ResponseCallback = None) -> None:
        """リストを差し替えて index の曲から再生。"""
        tracks = list(tracks)
        if isinstance(source, dict):
            source = Source.from_wire(source)
        self._command("replace", optimistic={"position": 0.0}, tracks=tracks, index=int(index),
                      source=source if source else None, callback=callback)

    def enqueue(self, tracks: Iterable[Track], mode: str = "next", callback: ResponseCallback = None) -> None:
        """mode: "next" (次に再生) / "end" (最後に再生) / "now" (すぐ再生)。"""
        self._command("enqueue", tracks=list(tracks), mode=mode, callback=callback)

    def queue_edit(self, mode: str, index: int | None = None, to: int | None = None,
                   callback: ResponseCallback = None, path: str | None = None) -> None:
        """待ち行列の編集。add/remove の index はリスト上の添字、move は待ち行列の位置。

        path: その位置で見えていた曲の path (違えば cliamp が "stale" で断る)。"""
        guarded = self._guarded(callback)

        def done(response: Response) -> None:
            if response.ok:
                # queue が省かれていれば空 (omitempty)。
                self.playlist.queue = [int(i) for i in response.data.get("queue") or []]
                self.emit("playlist-changed")
                self._schedule_playlist_refresh()
            guarded(response)

        self._command("queue_edit", mode=mode, index=index, to=to, path=path or None, callback=done)

    def remove(self, index: int, callback: ResponseCallback = None, path: str | None = None) -> None:
        """リストから 1 曲消す。path はその添字で見えていた曲 (違えば "stale" で断られる)。"""
        self._command("remove", index=int(index), path=path or None, callback=self._guarded(callback))

    # --- 取り直し -------------------------------------------------------------------

    def _schedule_playlist_refresh(self) -> None:
        if self._pl_timer:
            return

        def fire() -> bool:
            self._pl_timer = 0
            self.refresh_playlist()
            return GLib.SOURCE_REMOVE

        self._pl_timer = GLib.timeout_add(self.PLAYLIST_DEBOUNCE_MS, fire)

    def _retry_delay(self, failures: int) -> int:
        return min(self.RETRY_MAX_MS, self.RETRY_MIN_MS * 2 ** max(0, failures - 1))

    def _cancel_retries(self) -> None:
        for name in ("_pl_retry", "_hist_timer"):
            source = getattr(self, name, 0)
            if source:
                GLib.source_remove(source)
                setattr(self, name, 0)
        self._pl_fail = 0
        self._hist_fail = 0

    def refresh_playlist(self, full: bool = False) -> None:
        """いまのリストを取り直す (拡張の無い cliamp では何もしない)。

        世代 (gen) が手元の写しと同じなら曲の中身は変わっていない (cliamp は曲の追加・削除・
        並べ替え・待ち行列・ブックマークのたびに gen を増やす) ので、1 曲だけ (limit=1) 取って
        index / queue / up_next / source だけを入れ替える (曲が進むたびに数千曲を読み直さない)。
        全部を取るときは worker のスレッドで解析する。失敗したら間を空けて取り直す。"""
        if not self.client.connected or self.client.api < 1 or not self.supports("playlist"):
            return
        if self._pl_inflight:
            self._pl_dirty = True
            return
        if self._pl_retry:
            GLib.source_remove(self._pl_retry)
            self._pl_retry = 0
        self._pl_inflight = True
        self._pl_dirty = False
        light = (not full and bool(self.playlist.tracks) and self.playlist.gen > 0
                 and self.status.gen == self.playlist.gen)

        def failed(response: Response) -> None:
            if response.kind == "offline":
                return  # 繋ぎ直したときに取り直す (_on_connection)
            self._pl_fail += 1

            def retry() -> bool:
                self._pl_retry = 0
                self.refresh_playlist()
                return GLib.SOURCE_REMOVE

            self._pl_retry = GLib.timeout_add(self._retry_delay(self._pl_fail), retry)

        def done_full(response: Response, parsed: PlaylistState | None) -> None:
            self._pl_inflight = False
            if response.ok and parsed is not None:
                self._pl_fail = 0
                self.playlist = parsed
                self.emit("playlist-changed")
            elif not self._pl_dirty:
                failed(response)
            if self._pl_dirty:
                self.refresh_playlist()

        def done_light(response: Response, parsed: PlaylistState | None) -> None:
            self._pl_inflight = False
            current = self.playlist
            if (response.ok and parsed is not None and parsed.gen == current.gen
                    and parsed.total == len(current.tracks)):
                self._pl_fail = 0
                self.playlist = dc_replace(current, index=parsed.index, total=parsed.total, queue=parsed.queue,
                                           up_next=parsed.up_next, source=parsed.source)
                self.emit("playlist-changed")
                if self._pl_dirty:
                    self.refresh_playlist()
                return
            if not response.ok and not self._pl_dirty:
                failed(response)
                return
            self.refresh_playlist(full=True)  # 中身が変わっていた (その間に gen が進んだ)

        if light:
            self.client.request("playlist", done_light, parse=_parse_playlist_response, limit=1)
        else:
            self.client.request("playlist", done_full, parse=_parse_playlist_response)

    def _schedule_history_refresh(self, delay_ms: int | None = None) -> None:
        if self._hist_timer:
            GLib.source_remove(self._hist_timer)

        def fire() -> bool:
            self._hist_timer = 0
            self.refresh_history()
            return GLib.SOURCE_REMOVE

        self._hist_timer = GLib.timeout_add(self.HISTORY_DELAY_MS if delay_ms is None else delay_ms, fire)

    def refresh_history(self) -> None:
        """最近再生した曲を取り直す (新しい順、played_at 付き)。

        取っている間に頼まれたら、終わった後にもう 1 度取る。失敗したら間を空けて取り直す。"""
        if not self.client.connected or not self.supports("history"):
            return
        if self._hist_inflight:
            self._hist_dirty = True
            return
        self._hist_inflight = True
        self._hist_dirty = False

        def done(response: Response) -> None:
            self._hist_inflight = False
            if response.ok:
                self._hist_fail = 0
                self.history = parse_tracks(response.data)
                self.emit("history-changed")
            elif response.kind != "offline" and not self._hist_dirty:
                self._hist_fail += 1
                self._schedule_history_refresh(self._retry_delay(self._hist_fail))
            if self._hist_dirty:
                self.refresh_history()

        self.client.request("history", done, limit=self.HISTORY_LIMIT)

    def snapshot(self) -> Status:
        """いまの status の写し (位置は補間済み)。"""
        return dc_replace(self.status, position=self.position_now(), eq=list(self.status.eq))


def _parse_playlist_response(response: Response) -> PlaylistState | None:
    """playlist の応答を worker のスレッドで読む (数千曲でも main loop を止めない)。"""
    return parse_playlist(response.data) if response.ok else None


def device_error_text(response: Response) -> str:
    """出力先の一覧が取れなかった理由 (メニューに出す短い文)。"""
    error = response.error or ""
    if response.kind == "offline":
        return "cliamp に接続していません"
    if "pactl" in error:
        return "pactl が見つかりません (出力先の切り替えには PulseAudio の pactl が要ります)"
    if response.kind == "unsupported":
        return "この cliamp は出力先を切り替えられません"
    return "出力先を取得できません"

