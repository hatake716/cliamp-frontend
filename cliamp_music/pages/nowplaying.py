"""再生中のリスト (NowPlayingListPage)。

詳細ページの形: 左上に今の曲 (無ければ先頭の曲) の絵、題は出どころの名前
(source.name) か「再生中のリスト」、赤い副題は出どころのプロバイダー、情報の行は
「12 曲 · 48 分」。ボタンはシャッフルの丸 / 「▶ 再生」/ 「…」(プレイリストとして保存)。

行は番号付き (album の行)。再生中の行は番号の代わりに赤い動く棒。ダブルクリックで
play_index。リストの中身が変わったときだけ作り直し、曲や状態の変化では印だけ動かす。
reveal=True なら (作り終えたあとで) 再生中の行までスクロールする。
"""

from __future__ import annotations

import random
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib  # noqa: E402

from ..protocol import Response, Track, track_key  # noqa: E402
from ..widgets import TrackList, TrackRow  # noqa: E402
from .home import (  # noqa: E402
    DETAIL_LIST_SIDE,
    OFFLINE_TEXT,
    OFFLINE_TITLE,
    SIDE,
    UNSUPPORTED_TEXT,
    UNSUPPORTED_TITLE,
    ChunkedRows,
    ContentStack,
    DetailHeader,
    PageBase,
    info_line,
    is_playing,
    provider_label,
    total_duration,
    weak_call,
    weak_handler,
)


class NowPlayingListPage(PageBase):
    """再生中のリストのページ。`reveal_current()` で再生中の行までスクロールする (Ctrl+L)。"""

    PAGE_ID = "nowplaying"
    TITLE = "再生中のリスト"
    # reveal を頼んでから、リストの取り直しを待って行を探し続ける時間 (秒)
    REVEAL_WAIT = 6.0

    def __init__(self, ctx, reveal: bool = False, **_params):
        super().__init__(ctx)
        self.add_css_class("music-detail-page")
        self._reveal_requested = bool(reveal)
        self._reveal_deadline: float | None = None
        self._reveal_tries = 0
        self._reveal_timer = 0
        self._tracks: list[Track] | None = None
        self._art_key: object = ()

        self.header = DetailHeader(ctx, on_shuffle=weak_call(self.shuffle), on_play=weak_call(self.play_first),
                                   menu_factory=weak_call(self._menu))
        self.header.set_title(self.TITLE)
        self.inset(self.header)
        self.body.append(self.header)

        self.state = ContentStack("読み込み中…")
        self.state.set_margin_top(26)
        self.list = TrackList()
        self.list.add_css_class("music-detail-list")
        self.list.set_margin_start(DETAIL_LIST_SIDE)
        self.list.set_margin_end(SIDE - 4)
        self.state.content.append(self.list)
        self.body.append(self.state)
        self.rows = ChunkedRows(self.list, weak_handler(self._make_row), on_progress=weak_handler(self._on_built))

        store = ctx.store
        self.watch(store, "playlist-changed", self._on_playlist)
        self.watch(store, "track-changed", self._on_track)
        self.watch(store, "state-changed", self._on_state)
        self.watch(store, "connection-changed", self._on_connection)

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        if force or (store.connected and not store.playlist.tracks and store.status.total > 0):
            store.refresh_playlist()
        if self._reveal_requested:
            self._reveal_requested = False
            self._reveal_deadline = time.monotonic() + self.REVEAL_WAIT
        self._sync()

    def on_resync(self) -> None:
        self._sync()

    def on_hidden(self) -> None:
        self._cancel_reveal()

    def on_narrow_changed(self, narrow: bool) -> None:
        self.header.set_narrow(narrow)

    def _on_playlist(self, *_args) -> None:
        self._sync()

    def _on_track(self, *_args) -> None:
        self._update_header()
        self._update_current()
        self._try_reveal()

    def _on_state(self, *_args) -> None:
        self._update_current()

    def _on_connection(self, *_args) -> None:
        self._sync()

    def _sync(self) -> None:
        """store の今のリストに合わせる (中身が変わったときだけ行を作り直す)。"""
        store = self.ctx.store
        if not store.connected:
            self._show_problem("music-radio-symbolic", OFFLINE_TITLE, OFFLINE_TEXT)
            return
        if store.api < 1 or not store.supports("playlist"):
            self._show_problem("music-note-list-symbolic", UNSUPPORTED_TITLE, UNSUPPORTED_TEXT)
            return
        tracks = store.playlist.tracks
        self._update_header()
        if not tracks:
            self._tracks = []
            self.rows.build([])
            if store.status.total > 0:
                self.header.set_visible(True)
                self.state.show_loading()
            else:
                self.header.set_visible(False)
                self.state.show_empty("music-note-list-symbolic", "再生中のリストは空です",
                                      "曲を選んで再生すると、ここに並びます。")
            return
        self.header.set_visible(True)
        if tracks != self._tracks:
            self._tracks = list(tracks)
            self.rows.build(self._tracks)
        self.state.show_content()
        self._update_current()
        self._try_reveal()

    def _show_problem(self, icon: str, title: str, text: str) -> None:
        self._tracks = None
        self.rows.build([])
        self.header.set_visible(False)
        self.header.set_actions_sensitive(False)
        self._art_key = ()
        self.state.show_empty(icon, title, text)

    # --- 頭 ---------------------------------------------------------------------

    def _source(self):
        store = self.ctx.store
        source = store.playlist.source
        if not source:
            source = store.status.source
        return source

    def _update_header(self) -> None:
        store = self.ctx.store
        tracks = store.playlist.tracks
        source = self._source()
        title = source.name if source and source.name else self.TITLE
        self.header.set_title(title)
        self.set_title(title)
        self.header_title.set_text(title)
        self.header.set_subtitle(provider_label(self.ctx, source.provider) if source and source.provider else "")
        self.header.set_info(info_line(len(tracks), total_duration(tracks)) if tracks else "")
        self.header.set_actions_sensitive(bool(tracks))
        current = store.current_track()
        index = self._current_index()
        if index is not None and 0 <= index < len(tracks):
            current = tracks[index]
        subject = current or (tracks[0] if tracks else None)
        key = track_key(subject) if subject is not None else None
        if key != self._art_key:
            self._art_key = key
            if subject is not None:
                self.header.art.set_subject(self.ctx.artwork, subject)
            else:
                self.header.art.set_subject(self.ctx.artwork, ("placeholder", "nowplaying", "playlist"),
                                            kind="playlist")

    # --- 行 ---------------------------------------------------------------------

    def _make_row(self, index: int, track: Track) -> TrackRow:
        return TrackRow(self.ctx, track, variant="album", index=index, number=index + 1,
                        menu_context="nowplaying", on_activate=weak_handler(self._on_row))

    def _on_built(self, _built: int) -> None:
        self._update_current()
        self._try_reveal()

    def _current_index(self) -> int | None:
        """リスト上の再生中の添字。status の曲とリストが食い違えば None。"""
        store = self.ctx.store
        status = store.status
        tracks = store.playlist.tracks
        index = status.index
        if status.track is not None and 0 <= index < len(tracks):
            if tracks[index].path == status.track.path:
                return index
            # 別の位置に同じ曲があればそれ (リストの取り直し待ちの間など)
            for i, track in enumerate(tracks):
                if track.path == status.track.path:
                    return i
            return None
        if status.track is None and status.state in ("stopped", "offline"):
            return None
        pl_index = store.playlist.index
        return pl_index if 0 <= pl_index < len(tracks) else None

    def _update_current(self) -> None:
        index = self._current_index()
        self.list.set_current_index(index, is_playing(self.ctx.store.status))

    def _on_row(self, row) -> None:
        index = getattr(row, "index", None)
        if index is not None:
            self.ctx.store.play_index(index, callback=self._toast_failure)

    def _toast_failure(self, response: Response) -> None:
        if not response.ok:
            self.ctx.toast(f"再生できませんでした: {response.message}")

    # --- 再生中の行までスクロール ---------------------------------------------------

    def reveal_current(self) -> None:
        """再生中の行までスクロールする。

        行をまだ作っていない・リストの取り直しを待っているときは、数秒のうちに
        再生中の行が現れたらそこへ送る。"""
        self._reveal_deadline = time.monotonic() + self.REVEAL_WAIT
        self._reveal_tries = 0
        self._try_reveal()

    def _cancel_reveal(self) -> None:
        if self._reveal_timer:
            GLib.source_remove(self._reveal_timer)
            self._reveal_timer = 0

    def _try_reveal(self) -> None:
        if self._reveal_deadline is None or self._reveal_timer:
            return
        if time.monotonic() > self._reveal_deadline:
            self._reveal_deadline = None
            return
        index = self._current_index()
        if index is None or index >= self.rows.built:
            return  # リストの取り直しか行の作成を待つ (_on_built / _sync / _on_track で呼び直す)
        self._reveal_tries = 0
        self._reveal_timer = GLib.timeout_add(30, weak_call(self._reveal_step, index))

    def _reveal_step(self, index: int) -> bool:
        self._reveal_timer = 0
        rows = self.list.rows()
        if index >= len(rows) or not self.get_mapped():
            return GLib.SOURCE_REMOVE
        row = rows[index]
        ok, bounds = row.compute_bounds(self.body)
        adj = self.scroller.get_vadjustment()
        if not ok or bounds.get_height() <= 0 or adj.get_upper() < bounds.get_y() + bounds.get_height():
            self._reveal_tries += 1
            if self._reveal_tries < 60:
                self._reveal_timer = GLib.timeout_add(30, weak_call(self._reveal_step, index))
            return GLib.SOURCE_REMOVE
        page = adj.get_page_size()
        target = bounds.get_y() - (page - bounds.get_height()) / 2
        adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - page)))
        self.list.select_row(row)
        self._reveal_deadline = None
        return GLib.SOURCE_REMOVE

    # --- ボタン -------------------------------------------------------------------

    @property
    def tracks(self) -> list[Track]:
        return list(self.ctx.store.playlist.tracks)

    def play_first(self) -> None:
        """リストの先頭から再生する。"""
        if self.ctx.store.playlist.tracks:
            self.ctx.store.play_index(0, callback=self._toast_failure)

    def shuffle(self) -> None:
        """シャッフルを入れて無作為な曲から再生する。"""
        store = self.ctx.store
        count = len(store.playlist.tracks)
        if not count:
            return

        def done(response: Response) -> None:
            if not response.ok:
                self.ctx.toast(f"再生できませんでした: {response.message}")
            elif not store.status.shuffle:
                store.set_shuffle(True)

        store.play_index(random.randrange(count), callback=done)

    def _menu(self):
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("再生中の曲を表示", "page.reveal")
        section.append("プレイリストとして保存…", "page.save")
        menu.append_section(None, section)
        group = Gio.SimpleActionGroup()
        reveal = Gio.SimpleAction.new("reveal", None)
        reveal.connect("activate", weak_call(self.reveal_current))
        reveal.set_enabled(self._current_index() is not None)
        group.add_action(reveal)
        save = Gio.SimpleAction.new("save", None)
        save.connect("activate", weak_call(self.save_as_playlist))
        save.set_enabled(bool(self.ctx.store.playlist.tracks) and self.ctx.store.supports("playlist_add"))
        group.add_action(save)
        return menu, group

    def save_as_playlist(self) -> None:
        """いまのリストの曲を新しいプレイリストに入れる (名前を尋ねる)。"""
        tracks = [t for t in self.ctx.store.playlist.tracks if not t.live]
        if tracks:
            self.ctx.ask_new_playlist(tracks)
