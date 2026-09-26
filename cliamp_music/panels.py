"""右パネル: 歌詞 (LyricsPanel) と 次に再生 (QueuePanel)。幅 300、サイドバーと同じ地。

どちらも窓が `set_active(True/False)` で開け閉めを知らせる。開いていない間は
取得も描き直しもしない (歌詞は開いているときだけ取りに行く)。

歌詞:
- 22px/700 の行。今の行は明るく、他は 3 次色。今の行を上から 1/3 に保って滑らかに送る。
  行を押すとその時刻へ seek_to。同期していない歌詞は全行同じ色で送らない。
- 状態: 読み込み中 / 見つからない / 同期なし / 拡張なし (api 0) / ラジオ (ICY の曲名で探す)。
- 曲ごとに結果を覚える (最大 64 曲)。

次に再生 (フルスクリーンの UpNextView と Apple の「次に再生」と同じ組み立て):
- 上に横長のカプセル「シャッフル」「リピート」(オンで赤の塗り)。
- 「履歴」(最近再生した 10 曲、古い順に下へ)。
- 待ち行列があれば「次に再生」(右に赤い「消去」) と待ち行列の曲。続いて見出し
  (出どころの名前か「再生中のリスト」、副題「このあと続けて再生されます」) と up_next の曲。
  待ち行列が無ければ見出しは「次に再生」(副題は出どころの名前) と up_next の曲。
- 待ち行列の曲のダブルクリックは store.play_queued (その曲より前の待ち行列を外して
  next。play_index は待ち行列から外さないので同じ曲が 2 度鳴る)。続きの曲は play_index。
- シャッフルとリピート (すべて) では、cliamp の up_next は今の一巡の残りだけ (混ぜ直した
  後の並びはまだ決まっていない)。尽きても「次に再生する曲はありません」とは言わず、
  「このあとシャッフルし直して続けて再生します」と書く。
- 開いたときは最初の「次に再生」の見出しまでスクロールしておく (上へ戻すと履歴)。
- リストが変わったとき: 行を鍵 (種類・添字・曲) で使い回し、変わった行だけ外して足す
  (選んだ行とフォーカスが残る)。まとめて入れ替わる (シャッフルなど) ときは、見える分を
  先に作り、残りは少しずつ足す。
"""

from __future__ import annotations

import time
import weakref
from collections import OrderedDict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402

from .pages import Bindings  # noqa: E402
from .protocol import Lyrics, Track, track_key  # noqa: E402
from .widgets import EmptyState, LoadingState, SectionHeader, TrackList, TrackRow  # noqa: E402

LYRICS_CACHE_SIZE = 64
HISTORY_ROWS = 10
# 次に再生の行を一度に作る数 (残りは idle で少しずつ)
QUEUE_SYNC_ROWS = 30
QUEUE_CHUNK_ROWS = 20
EMPTY_NEXT = "次に再生する曲はありません"
RESHUFFLE_NOTE = "このあとシャッフルし直して続けて再生します"
# 手でスクロールしたあと、自動の送りを止めておく秒数
USER_SCROLL_HOLD = 4.0
# 今の行を置く高さ (上から)
CURRENT_LINE_FRACTION = 1 / 3


def _animations_enabled(widget: Gtk.Widget) -> bool:
    settings = widget.get_settings() or Gtk.Settings.get_default()
    return bool(settings is None or settings.get_property("gtk-enable-animations"))


def split_icy_title(text: str) -> tuple[str, str]:
    """ICY の曲名 "Artist - Title" を (アーティスト, 曲名) に。区切りが無ければ ("", 全体)。"""
    text = (text or "").strip()
    for sep in (" - ", " – ", " — ", " / "):
        if sep in text:
            artist, title = text.split(sep, 1)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip()
    return "", text


def lyrics_query(track: Track | None, stream_title: str = "") -> tuple[str, str] | None:
    """歌詞を探す (アーティスト, 曲名)。探せなければ None。

    ラジオ (live) は ICY の曲名から。YouTube などで artist が無く曲名が
    "Artist - Title" の形ならそれを分ける。"""
    if track is None:
        return None
    if track.live or (track.stream and stream_title.strip()):
        if not stream_title.strip():
            return None
        artist, title = split_icy_title(stream_title)
        return (artist, title) if title else None
    title = (track.title or "").strip()
    artist = (track.artist or "").strip()
    if not title:
        return None
    if not artist:
        guessed_artist, guessed_title = split_icy_title(title)
        if guessed_artist:
            return guessed_artist, guessed_title
    return artist, title


def _label(text: str, css: str | tuple[str, ...] = (), *, wrap: bool = False, xalign: float = 0.0) -> Gtk.Label:
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    else:
        label.set_ellipsize(Pango.EllipsizeMode.END)
    for cls in (css,) if isinstance(css, str) else css:
        label.add_css_class(cls)
    return label


class _PanelTop(Gtk.WindowHandle):
    """パネルの上端 (ページのツールバーと同じ高さ)。窓を掴んで動かせる。"""

    def __init__(self, child: Gtk.Widget | None = None):
        super().__init__()
        self.add_css_class("music-panel-top")
        if child is not None:
            self.set_child(child)
        else:
            spacer = Gtk.Box()
            spacer.set_size_request(-1, 38)
            self.set_child(spacer)


# ---------------------------------------------------------------------------
# 歌詞


class _LyricLine(Gtk.Button):
    """歌詞の 1 行。押すとその時刻へ。"""

    __gtype_name__ = "CliampMusicLyricLine"

    def __init__(self, index: int, text: str, t: float, synced: bool):
        super().__init__()
        self.index = index
        self.t = t
        self.add_css_class("music-lyric-line")
        self.add_css_class("flat")
        if not synced:
            self.add_css_class("unsynced")
        self.label = _label(text or "♪", "music-lyric-text", wrap=True)
        self.set_child(self.label)
        self.set_can_focus(synced)
        self.set_focus_on_click(False)
        if not synced:
            self.set_can_target(False)


class LyricsPanel(Gtk.Box):
    """歌詞のパネル。`LyricsPanel(ctx)`、`set_active(on)`。"""

    __gtype_name__ = "CliampMusicLyricsPanel"

    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.add_css_class("music-panel")
        self.add_css_class("music-lyrics-panel")
        self._active = False
        self._cache: OrderedDict[tuple[str, str], Lyrics | None] = OrderedDict()
        self._query: tuple[str, str] | None = None
        self._shown_query: tuple[str, str] | None = None
        self._lyrics: Lyrics | None = None
        self._lines: list[_LyricLine] = []
        self._current = -2
        self._token = 0
        self._tick_id = 0
        self._scroll_anim: Adw.TimedAnimation | None = None
        self._user_scrolled_at = 0.0
        self._pending_scroll = False
        self._resize_idle = 0
        self._relayout_idle = 0

        self.append(_PanelTop())

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(180)
        self.stack.set_vexpand(True)
        self.append(self.stack)

        self.scroller = Gtk.ScrolledWindow()
        # 縦は EXTERNAL: ホイールやタッチパッドでは動くが、スクロールバーは出さない (行を送る
        # たびに重ねのスクロールバーが浮かび、パネルの端に明るい線が出続けるため。Apple も出さない)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self.scroller.add_css_class("music-lyrics-scroller")
        self.lines_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.lines_box.add_css_class("music-lyrics")
        self._top_space = Gtk.Box()
        self._bottom_space = Gtk.Box()
        self.lines_box.append(self._top_space)
        self._lines_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.lines_box.append(self._lines_holder)
        self.lines_box.append(self._bottom_space)
        self.scroller.set_child(self.lines_box)
        self.stack.add_named(self.scroller, "lines")

        self.loading = LoadingState("歌詞を探しています…")
        self.stack.add_named(self.loading, "loading")
        self.empty = EmptyState("music-lyrics-symbolic", "歌詞が見つかりません")
        self.empty.add_css_class("music-panel-empty")
        self.stack.add_named(self.empty, "empty")
        self.stack.set_visible_child_name("empty")

        self._setting_scroll = False
        vadj = self.scroller.get_vadjustment()
        vadj.connect("notify::page-size", self._on_page_size)
        # 手で動かしたか: 自分で動かしている最中でない値の変化 (ホイール・スクロールバー・
        # キーボード) を見て、しばらく自動の送りを止める
        vadj.connect("value-changed", self._on_value_changed)
        vadj.connect("changed", self._on_adjustment_changed)
        self._layout_changed_at = 0.0
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect("scroll", self._on_user_scroll)
        self.scroller.add_controller(scroll)

        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._update_ticking())

        self._bindings = Bindings(self, on_rebind=self._on_store_track)
        self._bindings.add(ctx.store, "track-changed", self._on_track)
        self._bindings.add(ctx.store, "status-changed", self._on_status)
        self._bindings.add(ctx.store, "state-changed", self._on_state)
        self._bindings.add(ctx.store, "connection-changed", self._on_connection)

    # --- 開け閉め ----------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if active:
            self._refresh(force_scroll=True)
        self._update_ticking()

    def _on_map(self) -> None:
        if self._active:
            self._refresh(force_scroll=True)
        self._update_ticking()

    def _visible_now(self) -> bool:
        return self._active and self.get_mapped()

    # --- 取得 --------------------------------------------------------------------

    def _on_track(self, _store) -> None:
        self._on_store_track()

    def _on_store_track(self) -> None:
        if self._visible_now():
            self._refresh()

    def _on_connection(self, _store) -> None:
        if self._visible_now():
            self._refresh()

    def _on_status(self, _store) -> None:
        if not self._visible_now():
            return
        st = self.ctx.store.status
        # ラジオは曲名 (ICY) が変われば探し直す
        if st.track is not None and (st.track.live or st.track.stream):
            query = lyrics_query(st.track, st.stream_title)
            if query != self._query:
                self._refresh()
                return
        if self._lyrics is not None and self._lyrics.synced:
            self._update_current()

    def _on_state(self, _store) -> None:
        self._update_ticking()

    def _refresh(self, force_scroll: bool = False) -> None:
        store = self.ctx.store
        if not store.connected:
            self._show_empty("music-lyrics-symbolic", "cliamp に接続していません", None)
            return
        if not store.supports("lyrics"):
            self._show_empty("music-lyrics-symbolic", "歌詞を表示できません",
                             "拡張 IPC の無い cliamp からは歌詞を取れません。")
            return
        st = store.status
        track = st.track
        if track is None:
            self._show_empty("music-lyrics-symbolic", "再生していません", None)
            return
        query = lyrics_query(track, st.stream_title)
        self._query = query
        if query is None:
            if track.live:
                self._show_empty("music-radio-symbolic", "ラジオの歌詞",
                                 "この局は曲名を送っていないため、歌詞を探せません。")
            else:
                self._show_empty("music-lyrics-symbolic", "歌詞が見つかりません", None)
            return
        if query == self._shown_query and self._lyrics is not None:
            if force_scroll:
                self._current = -2
                self._update_current(animate=False)
            return
        if query in self._cache:
            self._cache.move_to_end(query)
            self._show_lyrics(query, self._cache[query])
            return
        self._token += 1
        token = self._token
        self._shown_query = None
        self._lyrics = None
        self.stack.set_visible_child_name("loading")
        me = weakref.ref(self)

        def done(lyrics: Lyrics | None) -> None:
            this = me()
            if this is None:
                return
            this._cache[query] = lyrics
            while len(this._cache) > LYRICS_CACHE_SIZE:
                this._cache.popitem(last=False)
            if token != this._token or this._query != query:
                return
            this._show_lyrics(query, lyrics)

        self.ctx.catalog.lyrics(query[0], query[1], done)

    def _show_empty(self, icon: str, title: str, description: str | None) -> None:
        self._token += 1
        self._shown_query = None
        self._lyrics = None
        self._clear_lines()
        self.empty.set_icon_name(icon)
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.stack.set_visible_child_name("empty")
        self._update_ticking()

    def _show_lyrics(self, query: tuple[str, str], lyrics: Lyrics | None) -> None:
        if lyrics is None or not [line for line in lyrics.lines if line.text.strip()]:
            track = self.ctx.store.status.track
            if track is not None and track.live:
                self._show_empty("music-radio-symbolic", "歌詞が見つかりません",
                                 f"「{query[1]}」の歌詞は見つかりませんでした。")
            else:
                self._show_empty("music-lyrics-symbolic", "歌詞が見つかりません", None)
            return
        self._shown_query = query
        self._lyrics = lyrics
        self._clear_lines()
        for i, line in enumerate(lyrics.lines):
            row = _LyricLine(i, line.text, line.t, lyrics.synced)
            if lyrics.synced:
                row.connect("clicked", LyricsPanel._on_line_clicked, self.ctx.store)
            self._lines_holder.append(row)
            self._lines.append(row)
        if lyrics.synced:
            self.lines_box.remove_css_class("unsynced")
        else:
            self.lines_box.add_css_class("unsynced")
        self.stack.set_visible_child_name("lines")
        self._current = -2
        self._user_scrolled_at = 0.0
        self._set_scroll(0)
        self._update_spacers()
        self._update_current(animate=False)
        self._update_ticking()

    def _clear_lines(self) -> None:
        if self._scroll_anim is not None:
            self._scroll_anim.pause()
        for row in self._lines:
            self._lines_holder.remove(row)
        self._lines = []
        self._current = -2

    @staticmethod
    def _on_line_clicked(row: _LyricLine, store) -> None:
        store.seek_to(row.t)
        if store.status.state == "paused":
            store.play()

    # --- 今の行 -------------------------------------------------------------------

    def _update_ticking(self) -> None:
        lyrics = self._lyrics
        want = (self._visible_now() and lyrics is not None and lyrics.synced
                and self.ctx.store.status.state == "playing")
        if want and not self._tick_id:
            self._tick_id = self.add_tick_callback(LyricsPanel._on_tick)
        elif not want and self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    @staticmethod
    def _on_tick(self, _clock) -> bool:
        self._update_current()
        return GLib.SOURCE_CONTINUE

    def _update_current(self, animate: bool = True) -> None:
        lyrics = self._lyrics
        if lyrics is None or not self._lines:
            return
        if not lyrics.synced:
            if self._current != -1:
                self._current = -1
                for row in self._lines:
                    row.remove_css_class("current")
                    row.remove_css_class("past")
            return
        index = lyrics.index_at(self.ctx.store.position_now())
        if index == self._current:
            if self._pending_scroll:
                self._scroll_to_current(animate)
            return
        self._current = index
        for row in self._lines:
            if row.index == index:
                row.add_css_class("current")
                row.remove_css_class("past")
            elif row.index < index:
                row.remove_css_class("current")
                row.add_css_class("past")
            else:
                row.remove_css_class("current")
                row.remove_css_class("past")
        self._scroll_to_current(animate)

    def _scroll_to_current(self, animate: bool = True) -> None:
        if time.monotonic() - self._user_scrolled_at < USER_SCROLL_HOLD:
            return
        adj = self.scroller.get_vadjustment()
        page = adj.get_page_size()
        if page <= 0 or not self._lines:
            self._pending_scroll = True
            return
        index = max(0, self._current)
        row = self._lines[min(index, len(self._lines) - 1)]
        ok, bounds = row.compute_bounds(self.lines_box)
        if not ok or (bounds.get_height() <= 0 and len(self._lines) > 1):
            self._pending_scroll = True
            return
        self._pending_scroll = False
        if self._current < 0:
            target = 0.0
        else:
            target = bounds.get_y() + bounds.get_height() / 2 - page * CURRENT_LINE_FRACTION
        target = max(adj.get_lower(), min(adj.get_upper() - page, target))
        if abs(adj.get_value() - target) < 1:
            return
        if self._scroll_anim is not None:
            self._scroll_anim.pause()
        if not animate or not self.get_mapped() or not _animations_enabled(self):
            self._set_scroll(target)
            return
        if self._scroll_anim is None:
            anim_target = Adw.CallbackAnimationTarget.new(self._set_scroll)
            self._scroll_anim = Adw.TimedAnimation.new(self.scroller, 0.0, 1.0, 520, anim_target)
            self._scroll_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._scroll_anim.set_value_from(adj.get_value())
        self._scroll_anim.set_value_to(target)
        self._scroll_anim.play()

    def _set_scroll(self, value: float) -> None:
        self._setting_scroll = True
        try:
            self.scroller.get_vadjustment().set_value(value)
        finally:
            self._setting_scroll = False

    def _on_adjustment_changed(self, _adj) -> None:
        # 行の並べ直しや大きさの変化で値が詰められたのは、手で動かしたのではない
        self._layout_changed_at = time.monotonic()
        # 並べ直しで行の位置が変わった (歌詞を入れた直後は、上の余白を広げた次の
        # 割り当てで行が下がる)。今の行を 1/3 に置き直す。割り当ての最中なので後で
        if self._lyrics is not None and self._lyrics.synced and self._lines and not self._relayout_idle:
            self._relayout_idle = GLib.idle_add(self._after_relayout)

    def _after_relayout(self) -> bool:
        self._relayout_idle = 0
        animating = self._scroll_anim is not None and self._scroll_anim.get_state() == Adw.AnimationState.PLAYING
        if self._lyrics is not None and self._lyrics.synced and not animating:
            self._pending_scroll = True
            self._scroll_to_current(animate=False)
        return GLib.SOURCE_REMOVE

    def _on_value_changed(self, _adj) -> None:
        now = time.monotonic()
        if not self._setting_scroll and self._lines and now - self._layout_changed_at > 0.3:
            self._user_scrolled_at = now

    def _on_page_size(self, adj, _pspec) -> None:
        # 大きさの割り当ての最中に呼ばれる。ここで部品の大きさを変えると割り当てと
        # 食い違う (Allocation height too small) ので、終わってから直す
        if not self._resize_idle:
            self._resize_idle = GLib.idle_add(self._after_resize)

    def _after_resize(self) -> bool:
        self._resize_idle = 0
        self._update_spacers()
        if self._lyrics is not None and self._lyrics.synced:
            self._pending_scroll = True
            self._scroll_to_current(animate=False)
        return GLib.SOURCE_REMOVE

    def _update_spacers(self) -> None:
        page = int(self.scroller.get_vadjustment().get_page_size())
        synced = self._lyrics is not None and self._lyrics.synced
        top = int(page * CURRENT_LINE_FRACTION) - 20 if synced and page > 0 else 12
        bottom = int(page * (1 - CURRENT_LINE_FRACTION)) if synced and page > 0 else 40
        self._top_space.set_size_request(-1, max(12, top))
        self._bottom_space.set_size_request(-1, max(40, bottom))

    def _on_user_scroll(self, *_args) -> bool:
        self._user_scrolled_at = time.monotonic()
        if self._scroll_anim is not None:
            self._scroll_anim.pause()
        return False

    def do_unrealize(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        if self._resize_idle:
            GLib.source_remove(self._resize_idle)
            self._resize_idle = 0
        if self._relayout_idle:
            GLib.source_remove(self._relayout_idle)
            self._relayout_idle = 0
        if self._scroll_anim is not None:
            self._scroll_anim.reset()
            self._scroll_anim = None
        Gtk.Box.do_unrealize(self)


# ---------------------------------------------------------------------------
# 次に再生


class _PanelToggle(Gtk.ToggleButton):
    """パネル上端の横長のカプセル (シャッフル / リピート)。オンで赤の塗り。"""

    __gtype_name__ = "CliampMusicPanelToggle"

    def __init__(self, icon_name: str, label: str):
        super().__init__()
        self.add_css_class("music-panel-toggle")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        self.image = Gtk.Image.new_from_icon_name(icon_name)
        self.image.set_pixel_size(15)
        box.append(self.image)
        self.label = _label(label, "music-panel-toggle-label")
        box.append(self.label)
        self.set_child(box)
        self.set_hexpand(True)
        self.update_property([Gtk.AccessibleProperty.LABEL], [label])


class _KeyedRows:
    """TrackList の行を鍵で使い回す (次に再生の一覧)。

    sync(items) で [(鍵, 作り方)] に合わせる。鍵の同じ行は外さずに動かすだけにするので、
    選んだ行・フォーカス・開いているメニューが残る。新しく作る行が多いときは最初の
    QUEUE_SYNC_ROWS 行だけその場で作り、残りは idle で少しずつ (後の sync で打ち切る)。"""

    def __init__(self, listbox: Gtk.ListBox):
        self.list = listbox
        self.keys: list = []
        self._idle = 0
        self._serial = 0

    def cancel(self) -> None:
        self._serial += 1
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0

    def sync(self, items: list) -> None:
        keys = [key for key, _make in items]
        if keys == self.keys and not self._idle:
            return
        self.cancel()
        rows = self.list.rows()
        by_key = {}
        if len(rows) == len(self.keys):
            for key, row in zip(self.keys, rows):
                by_key.setdefault(key, row)
        made = 0
        pending = None
        for pos, (key, make) in enumerate(items):
            current = self.list.get_row_at_index(pos)
            reuse = by_key.pop(key, None)
            if reuse is not None and current is reuse:
                continue
            if reuse is not None:
                self.list.remove(reuse)  # 動かす (解放しない)
                self.list.insert(reuse, pos)
                continue
            if made >= QUEUE_SYNC_ROWS:
                pending = pos
                break
            self.list.insert(make(), pos)
            made += 1
        # 使わなかった行 (と、作り切れなかった分の後ろにある行) を外す
        stop = len(items) if pending is None else pending
        while (extra := self.list.get_row_at_index(stop)) is not None:
            self.list.remove(extra)
            _release_row(extra)
        self.keys = keys if pending is None else keys[:pending]
        if pending is not None:
            serial = self._serial
            rest = items[pending:]

            def more() -> bool:
                if serial != self._serial:
                    return GLib.SOURCE_REMOVE
                chunk, remaining = rest[:QUEUE_CHUNK_ROWS], rest[QUEUE_CHUNK_ROWS:]
                for key, make in chunk:
                    self.list.append(make())
                    self.keys.append(key)
                rest[:] = remaining
                if rest:
                    return GLib.SOURCE_CONTINUE
                self._idle = 0
                return GLib.SOURCE_REMOVE

            self._idle = GLib.idle_add(more, priority=GLib.PRIORITY_LOW)

    def clear(self) -> None:
        self.cancel()
        _clear(self.list)
        self.keys = []


class QueuePanel(Gtk.Box):
    """次に再生のパネル。`QueuePanel(ctx)`、`set_active(on)`。"""

    __gtype_name__ = "CliampMusicQueuePanel"

    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.add_css_class("music-panel")
        self.add_css_class("music-queue-panel")
        self._active = False
        self._syncing = False
        self._history_keys: list[str] = []
        self._want_scroll = True
        self._repeat_shown = None
        self._modes_shown = None

        toggles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)
        toggles.add_css_class("music-panel-toggles")
        self.shuffle = _PanelToggle("music-shuffle-symbolic", "シャッフル")
        self.shuffle.set_tooltip_text("シャッフル")
        self.repeat = _PanelToggle("music-repeat-symbolic", "リピート")
        self.repeat.set_tooltip_text("リピート (オフ → すべて → 1 曲)")
        toggles.append(self.shuffle)
        toggles.append(self.repeat)
        self._shuffle_id = self.shuffle.connect("toggled", QueuePanel._on_shuffle_toggled, ctx.store)
        self._repeat_id = self.repeat.connect("clicked", QueuePanel._on_repeat_clicked, ctx.store)
        self.append(_PanelTop(toggles))

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        self.stack.set_vexpand(True)
        self.append(self.stack)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add_css_class("music-queue-scroller")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.add_css_class("music-queue-content")

        self.history_header = SectionHeader("履歴")
        self.history_header.add_css_class("music-queue-header")
        content.append(self.history_header)
        self.history_list = TrackList(selectable=True)
        self.history_list.add_css_class("music-queue-list")
        content.append(self.history_list)
        self.history_empty = _label("まだ履歴がありません", "music-queue-note")
        content.append(self.history_empty)

        # 待ち行列 (「次に再生」と赤い「消去」)。待ち行列があるときだけ出す
        self.queue_header = SectionHeader("次に再生")
        self.queue_header.add_css_class("music-queue-header")
        self.queue_header.add_css_class("next")
        self.clear_button = Gtk.Button(label="消去")
        self.clear_button.add_css_class("music-link-button")
        self.clear_button.add_css_class("flat")
        self.clear_button.set_tooltip_text("待ち行列を空にする")
        self.clear_button.connect("clicked", QueuePanel._on_clear, ctx)
        self.queue_header.add_end(self.clear_button)
        content.append(self.queue_header)
        self.queue_list = TrackList(selectable=True)
        self.queue_list.add_css_class("music-queue-list")
        self.queue_list.add_css_class("queued")
        content.append(self.queue_list)

        # 続きの曲 (up_next)
        self.next_header = SectionHeader("次に再生")
        self.next_header.add_css_class("music-queue-header")
        self.next_header.add_css_class("next")
        content.append(self.next_header)
        self.next_subtitle = _label("", "music-queue-subtitle")
        content.append(self.next_subtitle)
        self.next_list = TrackList(selectable=True)
        self.next_list.add_css_class("music-queue-list")
        content.append(self.next_list)
        self.next_empty = _label(EMPTY_NEXT, "music-queue-note")
        content.append(self.next_empty)
        # シャッフルで一巡の後も続くときの添え書き (並びの最後に)
        self.continue_note = _label(RESHUFFLE_NOTE, "music-queue-note")
        self.continue_note.add_css_class("music-queue-continue")
        content.append(self.continue_note)
        self.content = content
        self.scroller.set_child(content)
        self.stack.add_named(self.scroller, "list")

        self._queue_rows = _KeyedRows(self.queue_list)
        self._next_rows = _KeyedRows(self.next_list)

        self.empty = EmptyState("music-queue-symbolic", "次に再生")
        self.empty.add_css_class("music-panel-empty")
        self.stack.add_named(self.empty, "empty")

        self.connect("map", lambda *_: self._on_map())
        self._bindings = Bindings(self, on_rebind=self._refresh_all)
        self._bindings.add(ctx.store, "playlist-changed", self._on_playlist)
        self._bindings.add(ctx.store, "history-changed", self._on_history)
        self._bindings.add(ctx.store, "status-changed", self._on_status)
        self._bindings.add(ctx.store, "connection-changed", self._on_connection)

    # --- 開け閉め ----------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if active:
            self._want_scroll = True
            self._refresh_all()

    def _on_map(self) -> None:
        if self._active:
            self._refresh_all()

    def _visible_now(self) -> bool:
        return self._active and self.get_mapped()

    # --- 上のカプセル -----------------------------------------------------------------

    @staticmethod
    def _on_shuffle_toggled(button: _PanelToggle, store) -> None:
        panel = button.get_ancestor(QueuePanel)
        if panel is not None and panel._syncing:
            return
        store.set_shuffle(button.get_active())

    @staticmethod
    def _on_repeat_clicked(button: _PanelToggle, store) -> None:
        panel = button.get_ancestor(QueuePanel)
        if panel is not None and panel._syncing:
            return
        store.cycle_repeat()
        if panel is not None:
            panel._sync_toggles()

    @staticmethod
    def _on_clear(_button, ctx) -> None:
        ctx.store.queue_edit("clear", callback=lambda r: None if r.ok else ctx.toast(
            f"待ち行列を空にできませんでした: {r.message}"))

    def _sync_toggles(self) -> None:
        st = self.ctx.store.status
        connected = self.ctx.store.connected
        self._syncing = True
        try:
            self.shuffle.set_active(bool(st.shuffle))
            self.repeat.set_active(st.repeat in ("all", "one"))
        finally:
            self._syncing = False
        # 記号と文字は変わったときだけ差し替える (状態は 1 秒に何度も届く)
        if st.repeat != self._repeat_shown:
            self._repeat_shown = st.repeat
            self.repeat.image.set_from_icon_name(
                "music-repeat-one-symbolic" if st.repeat == "one" else "music-repeat-symbolic")
            self.repeat.label.set_text("1 曲をリピート" if st.repeat == "one" else "リピート")
        if self.shuffle.get_sensitive() != connected:
            self.shuffle.set_sensitive(connected)
            self.repeat.set_sensitive(connected)

    # --- 更新 --------------------------------------------------------------------

    def _on_status(self, _store) -> None:
        if self._visible_now():
            self._sync_toggles()
            modes = (self.ctx.store.status.shuffle, self.ctx.store.status.repeat)
            if modes != self._modes_shown:
                self._update_notes()

    def _on_connection(self, _store) -> None:
        if self._visible_now():
            self._refresh_all()

    def _on_playlist(self, _store) -> None:
        if self._visible_now():
            self._update_queue()

    def _on_history(self, _store) -> None:
        if self._visible_now():
            self._update_history()

    def _refresh_all(self) -> None:
        if not self._visible_now():
            return
        self._sync_toggles()
        store = self.ctx.store
        if not store.connected:
            self._show_empty("cliamp に接続していません", None)
            return
        if not store.supports("playlist"):
            self._show_empty("次に再生を表示できません",
                             "拡張 IPC の無い cliamp からはリストの中身を取れません。")
            return
        self.stack.set_visible_child_name("list")
        self._update_history()
        self._update_queue()
        if self._want_scroll:
            self._scroll_to_next_header()

    def _show_empty(self, title: str, description: str | None) -> None:
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.stack.set_visible_child_name("empty")

    def _update_history(self) -> None:
        store = self.ctx.store
        tracks = list(reversed(store.history[:HISTORY_ROWS]))
        keys = [track_key(t) + "|" + t.played_at for t in tracks]
        if keys == self._history_keys:
            return
        self._history_keys = keys
        _clear(self.history_list)
        for track in tracks:
            row = TrackRow(self.ctx, track, variant="queue", on_activate=_play_history_row(self.ctx))
            row.add_css_class("history")
            self.history_list.append(row)
        self.history_empty.set_visible(not tracks)

    def _wanted(self) -> tuple[list[tuple[int, Track]], list[tuple[int, Track]]]:
        """(待ち行列の曲, 続きの曲)。どちらも (リスト上の添字, 曲)。"""
        pl = self.ctx.store.playlist
        queue: list[tuple[int, Track]] = []
        seen = set()
        for index in pl.queue:
            track = pl.track_at(index)
            if track is not None:
                queue.append((index, track))
                seen.add(index)
        upcoming = [(index, pl.track_at(index)) for index in pl.up_next
                    if index not in seen and pl.track_at(index) is not None]
        return queue, upcoming

    def _update_queue(self) -> None:
        queue, upcoming = self._wanted()
        pl = self.ctx.store.playlist
        has_queue = bool(queue)
        self.queue_header.set_visible(has_queue)
        self.queue_list.set_visible(has_queue)
        self.clear_button.set_visible(has_queue)
        if has_queue:
            # 待ち行列の後の見出しは出どころ (Apple と同じ)。区切りの線は待ち行列の見出しに
            self.next_header.set_title(pl.source.name or "再生中のリスト")
            self.next_header.remove_css_class("next")
            self.next_header.add_css_class("continued")
            subtitle = "このあと続けて再生されます"
        else:
            self.next_header.set_title("次に再生")
            self.next_header.add_css_class("next")
            self.next_header.remove_css_class("continued")
            subtitle = pl.source.name
        self.next_subtitle.set_text(subtitle or "")
        show_next = bool(upcoming) or not has_queue
        self.next_header.set_visible(show_next)
        self.next_subtitle.set_visible(show_next and bool(subtitle))
        self.next_list.set_visible(bool(upcoming))
        self._queue_rows.sync([(("queue", index, track_key(track)),
                                (lambda index=index, track=track: self._make_row("queue", index, track)))
                               for index, track in queue])
        self._next_rows.sync([(("next", index, track_key(track)),
                               (lambda index=index, track=track: self._make_row("next", index, track)))
                              for index, track in upcoming])
        self._update_notes()

    def _update_notes(self) -> None:
        """空のとき・一巡の終わりの添え書き (シャッフルとリピートで変わる)。"""
        store = self.ctx.store
        st = store.status
        self._modes_shown = (st.shuffle, st.repeat)
        queue, upcoming = self._wanted()
        reshuffle = store.continues_by_reshuffle()
        empty = not queue and not upcoming
        self.next_empty.set_text(RESHUFFLE_NOTE if reshuffle else EMPTY_NEXT)
        self.next_empty.set_visible(empty)
        # 並びが尽きた後も続くことを最後に添える (200 曲で切った一覧のときは出さない)
        self.continue_note.set_visible(reshuffle and not empty and len(store.playlist.up_next) < 200)

    def _make_row(self, kind: str, index: int, track: Track) -> TrackRow:
        activate = _play_queued_row(self.ctx.store) if kind == "queue" else _play_index_row(self.ctx.store)
        row = TrackRow(self.ctx, track, variant="queue", index=index,
                       menu_context="queue" if kind == "queue" else "nowplaying",
                       on_activate=activate)
        if kind == "queue":
            row.add_css_class("queued")
        return row

    def _scroll_to_next_header(self) -> None:
        """最初の「次に再生」の見出しを上端に (並べ終わってから)。"""
        self._want_scroll = False
        tries = [0]

        def apply(widget, _clock) -> bool:
            if not isinstance(widget, QueuePanel):
                return GLib.SOURCE_REMOVE
            header = widget.queue_header if widget.queue_header.get_visible() else widget.next_header
            ok, bounds = header.compute_bounds(widget.content)
            adj = widget.scroller.get_vadjustment()
            if not ok or adj.get_page_size() <= 0 or adj.get_upper() <= adj.get_page_size() and tries[0] < 3:
                tries[0] += 1
                # 並べ終わるまで数フレーム待つ (いつまでも回らないよう上限を置く)
                return GLib.SOURCE_CONTINUE if tries[0] < 120 else GLib.SOURCE_REMOVE
            target = max(0.0, bounds.get_y() - 6)
            adj.set_value(min(target, max(0.0, adj.get_upper() - adj.get_page_size())))
            return GLib.SOURCE_REMOVE

        self.add_tick_callback(apply)

    @property
    def row_count(self) -> int:
        return len(self._queue_rows.keys) + len(self._next_rows.keys)

    @property
    def _row_keys(self) -> list[tuple[str, int, str]]:
        """並んでいる行の鍵 (種類 "queue" / "next", 添字, 曲の鍵)。待ち行列の行が先。"""
        return list(self._queue_rows.keys) + list(self._next_rows.keys)

    def do_unrealize(self) -> None:
        self._queue_rows.cancel()
        self._next_rows.cancel()
        Gtk.Box.do_unrealize(self)


def _clear(listbox: Gtk.ListBox) -> None:
    child = listbox.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        listbox.remove(child)
        _release_row(child)
        child = following


def _release_row(row: Gtk.Widget) -> None:
    """外した曲の行の循環参照を切る。

    widgets.TrackRow は self を掴んだ関数を子の MenuButton (create_popup_func) と
    GestureClick に渡しており、PyGObject の GC からは見えない C 側の参照になるので、
    一覧から外しただけでは行 (と絵) が解放されない (試すと 20 行中 20 行が残った)。
    次に再生は曲が進むたびに行を入れ替えるので、外した行はここで切っておく。"""
    if not isinstance(row, TrackRow):
        return
    more = getattr(row, "more", None)
    if isinstance(more, Gtk.MenuButton):
        more.set_create_popup_func(None)
    controllers = row.observe_controllers()
    for i in reversed(range(controllers.get_n_items())):
        controller = controllers.get_item(i)
        if controller is not None:
            row.remove_controller(controller)
    row.on_activate = None


def _play_index_row(store):
    # 行は store (長生き) だけを掴む。パネル (self) は掴まない
    def activate(row) -> None:
        if row.index is not None:
            track = getattr(row, "track", None)
            store.play_index(row.index, path=track.path if track is not None else None)

    return activate


def _play_queued_row(store):
    """待ち行列の行: その曲より前の待ち行列を外し、next で鳴らす (play_index は待ち行列から
    外さないので、同じ曲がもう 1 度鳴り、リストの位置も飛ぶ)。"""

    def activate(row) -> None:
        if row.index is not None:
            store.play_queued(row.index)

    return activate


def _play_history_row(ctx):
    def activate(row) -> None:
        ctx.play_now(row.track)

    return activate
