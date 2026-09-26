"""最近再生した項目 (RecentPage)。

大見出しの右に「▶ 再生」カプセルとシャッフルの丸。下に行 (絵 40px、曲名、
アーティスト、「3 時間前」、時間、「…」)。同じ曲は新しい方の 1 行にまとめる。
行のダブルクリック/Enter で、並び全体でリストを差し替えてその曲から再生する。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

from ..protocol import RECENTLY_PLAYED, Response, Source, Track, relative_time, track_key  # noqa: E402
from ..widgets import CapsuleButton, CircleButton, PageTitle, TrackList, TrackRow  # noqa: E402
from .home import (  # noqa: E402
    OFFLINE_TEXT,
    OFFLINE_TITLE,
    RECENT_SOURCE_NAME,
    SIDE,
    UNSUPPORTED_TEXT,
    UNSUPPORTED_TITLE,
    ContentStack,
    PageBase,
    clear_box,
    shuffle_tracks,
    unique_tracks,
    weak_call,
    weak_handler,
)

HISTORY_LIMIT = 100


class RecentPage(PageBase):
    """最近再生した項目のページ。"""

    PAGE_ID = "recent"
    TITLE = "最近再生した項目"

    def __init__(self, ctx, **_params):
        super().__init__(ctx)
        self.add_css_class("music-recent-page")
        self._tracks: list[Track] = []
        self._keys: tuple = ()
        self._serial = 0

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        row.add_css_class("music-title-row")
        self.large_title = PageTitle(self.TITLE)
        self.large_title.add_css_class("music-page-large-title")
        self.large_title.set_hexpand(True)
        row.append(self.large_title)
        self.play_button = CapsuleButton("再生", "music-play-symbolic")
        self.play_button.add_css_class("music-detail-play")
        self.play_button.connect("clicked", weak_call(self.play_all))
        self.shuffle_button = CircleButton("music-shuffle-symbolic", "シャッフル", 34, accent=True)
        self.shuffle_button.connect("clicked", weak_call(self.shuffle_all))
        row.append(self.play_button)
        row.append(self.shuffle_button)
        self.inset(row)
        self.body.append(row)

        self.state = ContentStack("読み込み中…")
        self.state.set_margin_top(14)
        self.list = TrackList()
        self.list.add_css_class("music-recent-list")
        self.inset(self.list, SIDE - 4)
        self.state.content.append(self.list)
        self.body.append(self.state)
        self._set_actions(False)

        store = ctx.store
        self.watch(store, "history-changed", self._on_history)
        self.watch(store, "connection-changed", self._on_connection)

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        if not store.connected:
            self._show_problem(OFFLINE_TITLE, OFFLINE_TEXT, "music-radio-symbolic")
            return
        if not store.supports("history"):
            self._show_problem(UNSUPPORTED_TITLE, UNSUPPORTED_TEXT, "music-recent-symbolic")
            return
        if not self._tracks:
            self.state.show_loading()
        self._serial += 1
        serial = self._serial

        def done(result) -> None:
            if serial != self._serial:
                return
            if isinstance(result, Response):
                if not self._tracks:
                    self._show_problem("履歴を読めませんでした", result.message, "music-recent-symbolic")
                return
            self._show(result)

        self.ctx.catalog.history(done, limit=HISTORY_LIMIT, force=force)

    def on_resync(self) -> None:
        # 「3 時間前」を今に合わせ、隠れている間に増えた分も取る
        self.load(force=False)

    def _on_history(self, *_args) -> None:
        self.load(force=False)

    def _on_connection(self, *_args) -> None:
        self.load(force=False)

    def _show_problem(self, title: str, text: str, icon: str) -> None:
        self._tracks = []
        self._keys = ()
        clear_box(self.list)
        self._set_actions(False)
        self.state.show_empty(icon, title, text)

    def _show(self, tracks: list[Track]) -> None:
        tracks = unique_tracks(tracks)
        keys = tuple((track_key(t), t.played_at) for t in tracks)
        if keys != self._keys:
            self._keys = keys
            self._tracks = tracks
            clear_box(self.list)
            for i, track in enumerate(tracks):
                self.list.append(TrackRow(self.ctx, track, variant="list", index=i,
                                          on_activate=weak_handler(self._on_row)))
        else:
            # 並びは同じ。「3 時間前」だけ今に合わせる
            for row in self.list.rows():
                row.set_extra_text(relative_time(row.track.played_at))
        self._set_actions(bool(tracks))
        if tracks:
            self.state.show_content()
        else:
            self.state.show_empty("music-recent-symbolic", "最近再生した曲はありません",
                                  "cliamp で再生した曲がここに並びます。")

    def _set_actions(self, sensitive: bool) -> None:
        self.play_button.set_sensitive(sensitive)
        self.shuffle_button.set_sensitive(sensitive)

    # --- 再生 -------------------------------------------------------------------

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def _source(self) -> Source:
        return Source(provider="local", id=RECENTLY_PLAYED, name=RECENT_SOURCE_NAME)

    def _on_row(self, row) -> None:
        index = getattr(row, "index", None)
        if index is not None:
            self.ctx.play_tracks(self._tracks, index, self._source())

    def play_all(self) -> None:
        """先頭の曲から再生する。"""
        self.ctx.play_tracks(self._tracks, 0, self._source())

    def shuffle_all(self) -> None:
        """シャッフルを入れて無作為な曲から再生する。"""
        shuffle_tracks(self.ctx, self._tracks, self._source())
