"""Spotify のプレイリストの自動同期 (裏で読み直して置き換える)。

取り込んだプレイリストを「Spotify と自動同期」にすると、この部品が定期的に公開の頁を
読み直し、今の曲と違っていれば静かに置き換える (確認の窓は出さない。手動の「Spotify から
更新」と同じ replace_local を使う)。曲が変わらなければ記録の synced_at だけ直す。

- 読み取りは worker のスレッド (importer.fetch_async)、結果は main loop に戻す。
- cliamp に繋がっていないとき・同じプレイリストをいま取り込んでいるときは飛ばす。
- 置き換えたときは ctx の "local-playlists-changed" と自分の "synced" を出し、トーストで
  知らせる。失敗はログと静かなトーストだけ (自動同期の失敗でアプリを止めない)。
- 間隔は既定 SYNC_INTERVAL_MS。環境変数 CLIAMP_MUSIC_SYNC_MS で差し替えられる (試験用)。
  一度にすべてのプレイリストへ同時に HTTP を投げないため、1 tick で最大 SYNC_BATCH 件。
"""

from __future__ import annotations

import os
import weakref
from typing import Callable

from gi.repository import GLib, GObject  # noqa: E402

from . import log  # noqa: E402
from .protocol import Response  # noqa: E402
from .spotify_import import SpotifyImportError  # noqa: E402

SYNC_INTERVAL_MS = 300_000  # 既定 5 分
SYNC_BATCH = 3  # 1 回に読み直す最大件数 (まとめて HTTP を投げない)
MISSING_TEXT = "no such file or directory"


def _interval_ms() -> int:
    raw = os.environ.get("CLIAMP_MUSIC_SYNC_MS")
    if raw and raw.isdigit():
        return max(200, int(raw))
    return SYNC_INTERVAL_MS


class SpotifySync(GObject.Object):
    """自動同期を有効なプレイリストの定期ポーリング。ctx を弱く持つ。

    窓の表示や接続の状態によらず動く (裏で同期するのが目的)。ctx が無くなれば止まる。
    """

    __gtype_name__ = "CliampMusicSpotifySync"
    __gsignals__ = {
        # (name, 置き換えた曲数, 前の曲数)。UI の取り直し・記録に使う。
        "synced": (GObject.SignalFlags.RUN_FIRST, None, (str, int, int)),
    }

    def __init__(self, ctx):
        super().__init__()
        self._ctx = weakref.ref(ctx)
        self._timer: int | None = None
        self._running = False

    @property
    def ctx(self):
        return self._ctx()

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """定期の読み直しを始める (既に動いていれば何もしない)。"""
        if self._running or self.ctx is None:
            return
        self._running = True
        self._schedule()

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _schedule(self) -> None:
        if not self._running:
            return
        # 次回を予約。戻り値 False でこの 1 回だけ走り、_tick の最後でまた予約する
        # (途中で ctx が無くなった・stop されたら予約しない)。
        self._timer = GLib.timeout_add(_interval_ms(), self._due)

    def _due(self) -> bool:
        self._timer = None
        if not self._running or self.ctx is None:
            return False
        self._tick()
        self._schedule()
        return False  # _schedule が次回を予約したので、この source は 1 回限り

    def _tick(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        store = getattr(ctx, "store", None)
        if store is None or not store.connected:
            return
        if not store.supports("playlist_add") or not store.supports("playlist_delete"):
            return
        try:
            names = ctx.imports.auto_sync_names()
        except Exception as exc:  # 記録は飾り
            log(f"自動同期の記録を読めません: {exc}")
            return
        busy = getattr(ctx, "spotify_imports_busy", set())
        due = [n for n in names if n not in busy][:SYNC_BATCH]
        for name in due:
            self.sync_one(name)

    def sync_one(self, name: str, callback: Callable[[bool], object] | None = None) -> None:
        """プレイリスト name を読み直して、違っていれば静かに置き換える。

        callback(置き換えたか)。busy 中・記録が無ければ False。曲が変わらなければ
        synced_at だけ直して False。置き換えたら "synced" と local-playlists-changed を出す。
        """
        ctx = self.ctx
        if ctx is None:
            return
        busy: set[str] = ctx.spotify_imports_busy
        if name in busy:
            if callback is not None:
                callback(False)
            return
        record = ctx.imports.get(name)
        if record is None:
            if callback is not None:
                callback(False)
            return
        busy.add(name)
        was_auto = bool(record.auto_sync)
        done: dict[str, object] = {}

        def finish(ok: bool, text: str = "") -> None:
            busy.discard(name)
            if text:
                ctx.toast(text)
            if callback is not None:
                callback(ok)

        def both() -> None:
            if "fetched" not in done or "current" not in done:
                return
            value, current = done["fetched"], done["current"]
            if isinstance(value, SpotifyImportError):
                finish(False, f"「{name}」の自動同期を読み直せませんでした")
                return
            if isinstance(current, Response):
                if MISSING_TEXT in current.error:
                    # 消えた・名前が変わった。記録を忘れ、自動同期も外す
                    ctx.forget_import(name)
                    ctx.refresh_local_playlists(force=True)
                finish(False)
                return
            current = list(current)
            if current and ctx.imported_record(name, current) is None:
                # 中身が別物になった (TUI で作り直したなど)。置き換えずに記録を忘れる
                finish(False)
                ctx.emit("local-playlists-changed")
                return
            if [t.path for t in current] == [t.path for t in value.tracks]:
                ctx.imports.mark_synced(name)
                finish(False)  # 変わっていない = 置き換えていない
                return
            # 手動更新と違い、確かめずに置き換える (auto_sync を明示しているため)。busy の印は
            # 置き換えのあいだ保ったまま (finish が外す) で、同じ曲を二重に扱わない。
            from .importer import replace_local

            def saved(response: Response, lost=None, backup=None) -> None:
                if not response.ok:
                    ctx.refresh_local_playlists(force=True)
                    finish(False, f"「{name}」の自動同期で置き換えられませんでした: {response.message}")
                    return
                try:
                    # record() は新しい記録を作る (auto_sync の既定は off)。自動同期を ON のまま
                    # 置き換えたなら、その印を保つ (off のものは off のまま)。
                    ctx.imports.record(name, value)
                    if was_auto:
                        ctx.imports.set_auto_sync(name, True, save=False)
                    ctx.imports.mark_synced(name)
                except Exception as exc:  # 記録は飾り。曲は置き換わっている
                    log(f"自動同期の記録を書けません: {exc}")
                self.emit("synced", name, value.count, len(current))
                ctx.refresh_local_playlists(force=True)
                finish(True, f"「{name}」を Spotify と同期しました ({value.count} 曲)")

            replace_local(ctx, name, value.tracks, saved)

        def fetched(value) -> None:
            done["fetched"] = value
            both()

        def read(value) -> None:
            done["current"] = value
            both()

        from .importer import fetch_async

        try:
            fetch_async(record.url, fetched)
            ctx.catalog.tracks("local", name, read, force=True)
        except Exception as exc:
            import traceback

            log(f"「{name}」の自動同期を始められません: {exc!r}")
            traceback.print_exc()
            finish(False)
