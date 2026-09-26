"""検索 (SearchPage)。

ヘッダーの中央にカプセルの検索欄 (幅 380、赤いフォーカスの輪)、右に範囲の切り替え
(Adw.ToggleGroup: YouTube / Spotify (使えるときだけ) / ライブラリ)。Enter か
0.6 秒の入力停止で検索する。

- 入力前: 「最近の検索」(丸いチップ、右に赤い「消去」) と「カテゴリーを探す」
  (16:9 のタイル。色の組はカテゴリーごとに固定)。タイルを押すとその語で検索。
- 結果: 左に「トップの結果」、右に「曲」の最初の 4 行 (狭い幅では縦に積む)。
  その下に「すべての曲」。行のダブルクリック/Enter で結果全体を replace して
  その曲から再生 (出どころは Source(provider=範囲, name=「語」))。
- 検索中はスピナー、失敗は理由を、該当なしは「結果がありません」を空状態で出す。
- 範囲は GuiState.search_scope に保存する。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..protocol import Response, Source, Track  # noqa: E402
from ..widgets import (  # noqa: E402
    Artwork,
    CategoryTile,
    Chip,
    FillWidth,
    SectionHeader,
    TrackList,
    TrackRow,
)
from .home import (  # noqa: E402
    SIDE,
    UNSUPPORTED_TITLE,
    ContentStack,
    NaturalWidth,
    PageBase,
    _label,
    clear_box,
    make_grid,
    provider_label,
    remember_provider_names,
    search_entry,
    text_button,
    weak_call,
    weak_handler,
)

DEBOUNCE_MS = 600
SEARCH_LIMIT = 25
TOP_SONGS = 4

# (範囲の名前, 表示)。spotify は providers に検索できる spotify があるときだけ出す。
SCOPES = (("youtube", "YouTube"), ("spotify", "Spotify"), ("library", "ライブラリ"))
SCOPE_LABELS = dict(SCOPES)

# カテゴリーと色の組 (上の明るい色, 下の深い色)。並びも色も固定。
CATEGORIES = (
    ("J-POP", ("#ff6a88", "#c2185b")),
    ("アニメ", ("#a08ff5", "#5a3cc8")),
    ("シティポップ", ("#ffab4c", "#e2443f")),
    ("ロック", ("#6d6d73", "#1c1c20")),
    ("ヒップホップ", ("#5b93f0", "#1d3f96")),
    ("ジャズ", ("#c79c86", "#5a3629")),
    ("クラシック", ("#c9ba98", "#6a593a")),
    ("エレクトロニック", ("#2dd4f0", "#4d45e0")),
    ("Lo-fi", ("#9aa8ba", "#35455a")),
    ("作業用BGM", ("#72b33a", "#1f6b30")),
    ("K-POP", ("#f57ac0", "#9533e6")),
    ("90年代", ("#f9c23c", "#c9480f")),
)


class _TopResult(Gtk.Button):
    """「トップの結果」のカード (大きめの絵・曲名・「曲 · アーティスト」・赤い再生の丸)。"""

    ART = 112

    def __init__(self, ctx, track: Track, on_activate):
        super().__init__()
        self.add_css_class("music-top-result")
        self.track = track
        overlay = Gtk.Overlay()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.art = Artwork(self.ART, self.ART, radius=7, shadow=True)
        self.art.set_halign(Gtk.Align.START)
        self.art.set_subject(ctx.artwork, track)
        box.append(self.art)
        self.title_label = _label(track.display_title, "music-top-result-title", wrap=True, lines=2)
        # 自然幅を抑える (長い曲名でカードが横に伸びないよう、列の幅で折り返す)
        self.title_label.set_max_width_chars(16)
        self.title_label.set_margin_top(14)
        box.append(self.title_label)
        kind = "ライブ" if track.live else "曲"
        subtitle = " · ".join(part for part in (kind, track.artist) if part)
        self.subtitle_label = _label(subtitle, "music-top-result-subtitle")
        self.subtitle_label.set_margin_top(3)
        box.append(self.subtitle_label)
        overlay.set_child(box)
        play = Gtk.Image.new_from_icon_name("music-play-symbolic")
        play.set_pixel_size(16)
        play.add_css_class("music-top-result-play")
        play.set_halign(Gtk.Align.END)
        play.set_valign(Gtk.Align.END)
        play.set_can_target(False)
        overlay.add_overlay(play)
        self.set_child(overlay)
        self.set_tooltip_text(track.display_title)
        self.connect("clicked", on_activate)


class SearchPage(PageBase):
    """検索のページ。`focus_entry()` で検索欄にフォーカスを移す (Ctrl+F)。"""

    PAGE_ID = "search"
    TITLE = "検索"

    def __init__(self, ctx, **_params):
        super().__init__(ctx, large_title=False)
        self.add_css_class("music-search-page")
        self._query = ""
        self._results: list[Track] = []
        self._results_query = ""
        self._results_scope = ""
        self._serial = 0
        self._timer = 0
        self._suppress_changed = False
        self._suppress_scope = False
        self._spotify = False
        self._scope = ctx.state.search_scope if ctx.state.search_scope in SCOPE_LABELS else "youtube"
        # 表示し終えたら検索欄にフォーカスを移す (Apple も検索を開くと欄に入る)。
        # 自分自身のシグナルなのでクラスの関数を渡す (ページを掴む closure を作らない)
        self.connect("shown", SearchPage._on_shown)

        # ヘッダー: 中央の検索欄と右の範囲
        self.entry = search_entry("")
        self.entry.connect("changed", weak_call(self._on_changed))
        self.entry.connect("activate", weak_call(self._on_activate))
        field = NaturalWidth(self.entry, 380)
        field.set_valign(Gtk.Align.CENTER)
        field.add_css_class("music-search-field")
        self.scopes = Adw.ToggleGroup()
        self.scopes.add_css_class("music-scope")
        self.scopes.set_valign(Gtk.Align.CENTER)
        self.scopes.connect("notify::active-name", weak_call(self._on_scope_changed))
        # 検索欄と範囲は 1 つの CenterBox に入れてヘッダーの題の場所いっぱいに置く
        # (pack_end の箱は縮まずに検索欄に重なる)。狭いときは検索欄から縮む
        bar = Gtk.CenterBox()
        bar.add_css_class("music-search-bar")
        bar.set_shrink_center_last(False)
        bar.set_center_widget(field)
        bar.set_end_widget(self.scopes)
        self.header_bar.set_title_widget(FillWidth(bar))
        self._fill_scopes()

        # 入力前
        self.browse = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.browse.add_css_class("music-search-browse")
        self.recent_header = SectionHeader("最近の検索")
        self.recent_header.add_end(text_button("消去", weak_call(self._clear_recent)))
        self.inset(self.recent_header)
        self.recent_chips = Gtk.FlowBox()
        self.recent_chips.add_css_class("music-chips")
        self.recent_chips.set_selection_mode(Gtk.SelectionMode.NONE)
        self.recent_chips.set_homogeneous(False)
        self.recent_chips.set_row_spacing(8)
        self.recent_chips.set_column_spacing(8)
        self.recent_chips.set_max_children_per_line(30)
        self.recent_chips.set_valign(Gtk.Align.START)
        self.inset(self.recent_chips)
        self.recent_chips.set_margin_top(10)
        self.recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.recent_box.add_css_class("music-search-recent")
        self.recent_box.append(self.recent_header)
        self.recent_box.append(self.recent_chips)
        self.browse.append(self.recent_box)

        categories_header = SectionHeader("カテゴリーを探す")
        self.inset(categories_header)
        categories_header.add_css_class("music-search-categories-header")
        self.browse.append(categories_header)
        self.categories = make_grid(row_spacing=18, column_spacing=18, max_per_line=6)
        self.categories.set_margin_top(12)
        self.inset(self.categories)
        for title, colors in CATEGORIES:
            tile = CategoryTile(title, colors, weak_handler(self._on_category))
            self.categories.append(tile)
        self.browse.append(self.categories)
        self.body.append(self.browse)

        # 結果
        self.results = ContentStack("検索しています…")
        self.results.set_visible(False)
        content = self.results.content
        self.top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=32)
        self.top_row.add_css_class("music-search-top")
        self.inset(self.top_row)
        top_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        # 見出しの中の伸びる余白が列ごと横に伸ばすので、明示的に止める
        top_column.set_hexpand(False)
        top_column.append(SectionHeader("トップの結果"))
        self.top_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top_column.append(self.top_slot)
        self.top_column = top_column
        songs_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        songs_column.set_hexpand(True)
        songs_column.append(SectionHeader("曲"))
        self.top_songs = TrackList()
        self.top_songs.add_css_class("music-search-top-songs")
        songs_column.append(self.top_songs)
        self.songs_column = songs_column
        self.top_row.append(top_column)
        self.top_row.append(songs_column)
        content.append(self.top_row)

        self.all_header = SectionHeader("すべての曲")
        self.all_header.add_css_class("music-search-all-header")
        self.inset(self.all_header)
        content.append(self.all_header)
        self.all_songs = TrackList()
        self.all_songs.add_css_class("music-search-all-songs")
        self.inset(self.all_songs, SIDE - 4)
        content.append(self.all_songs)
        self.body.append(self.results)

        self._update_placeholder()
        self._fill_recent()
        self.on_narrow_changed(False)
        self.watch(ctx.store, "connection-changed", self._on_connection)

    # --- 表示 -------------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        self._load_providers(force)
        self._fill_recent()
        if self._unsupported():
            self._show_unsupported()
        elif force and self._query:
            self.search(self._query, force=True)

    def _on_shown(self) -> None:
        self.focus_entry()

    def on_resync(self) -> None:
        self._fill_recent()
        self._check_support()

    def _on_connection(self, *_args) -> None:
        # 繋ぎ直したら (cliamp を再起動したなど) Spotify の有無を確かめ直す
        if self.ctx.store.connected:
            self._load_providers(False)
            self._check_support()

    # --- 拡張の無い cliamp ----------------------------------------------------------

    def _unsupported(self) -> bool:
        """拡張の無い cliamp に繋がっている (検索はできない)。"""
        store = self.ctx.store
        return store.connected and store.api < 1

    def _show_unsupported(self) -> None:
        self._serial += 1
        self._cancel_timer()
        self.scopes.set_visible(False)
        self.browse.set_visible(False)
        self.results.set_visible(True)
        self.results.show_empty("music-search-symbolic", UNSUPPORTED_TITLE,
                                "再生の操作だけ使えます。検索には GUI 用の拡張 (api 1) を当てた "
                                "cliamp が必要です。")
        self._shown_unsupported = True

    def _check_support(self) -> None:
        """繋ぎ直しで拡張の有無が変わったら、表示を合わせる。"""
        if self._unsupported():
            self._show_unsupported()
        elif getattr(self, "_shown_unsupported", False):
            self._shown_unsupported = False
            self.scopes.set_visible(True)
            if self._query:
                self.search(self._query)
            else:
                self._show_browse()

    def on_hidden(self) -> None:
        self._cancel_timer()

    def focus_entry(self) -> None:
        """検索欄にフォーカスを移す (Ctrl+F)。"""
        self.entry.grab_focus()

    def focus_search(self) -> None:
        """窓の Ctrl+F から呼ばれる (focus_entry と同じ)。"""
        self.focus_entry()

    def on_narrow_changed(self, narrow: bool) -> None:
        if narrow:
            self.top_row.set_orientation(Gtk.Orientation.VERTICAL)
            self.top_row.set_spacing(22)
            self.top_column.set_size_request(-1, -1)
            self.top_column.set_hexpand(True)
        else:
            self.top_row.set_orientation(Gtk.Orientation.HORIZONTAL)
            self.top_row.set_spacing(32)
            self.top_column.set_size_request(340, -1)
            self.top_column.set_hexpand(False)

    # --- 範囲 -------------------------------------------------------------------

    def _available_scopes(self) -> list[tuple[str, str]]:
        return [(name, label) for name, label in SCOPES if name != "spotify" or self._spotify]

    def _fill_scopes(self) -> None:
        self._suppress_scope = True
        try:
            self.scopes.remove_all()
            names = []
            for name, label in self._available_scopes():
                self.scopes.add(Adw.Toggle(name=name, label=label))
                names.append(name)
            active = self._scope if self._scope in names else "youtube"
            self.scopes.set_active_name(active)
        finally:
            self._suppress_scope = False
        self._update_placeholder()

    @property
    def scope(self) -> str:
        """いま検索に使う範囲 (保存した範囲が使えないときは youtube)。"""
        active = self.scopes.get_active_name()
        return active if active in SCOPE_LABELS else "youtube"

    def set_scope(self, name: str) -> None:
        """範囲を切り替える (保存し、語があれば検索し直す)。"""
        if name not in [n for n, _ in self._available_scopes()]:
            return
        self.scopes.set_active_name(name)

    def _on_scope_changed(self) -> None:
        if self._suppress_scope:
            return
        name = self.scope
        self._scope = name
        state = self.ctx.state
        if state.search_scope != name:
            state.search_scope = name
            state.save()
        self._update_placeholder()
        if self._query:
            self.search(self._query)

    def _update_placeholder(self) -> None:
        label = SCOPE_LABELS.get(self.scope, "YouTube")
        self.entry.set_placeholder_text(f"{label} を検索")

    def _load_providers(self, force: bool) -> None:
        store = self.ctx.store
        if not store.connected or not store.supports("providers"):
            return

        def done(result) -> None:
            if isinstance(result, Response):
                return
            remember_provider_names(self.ctx, result)
            spotify = any(p.key == "spotify" and p.search for p in result)
            if spotify != self._spotify:
                self._spotify = spotify
                self._fill_scopes()

        self.ctx.catalog.providers(done, force=force)

    # --- 入力 -------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_changed(self) -> None:
        if self._suppress_changed:
            return
        self._cancel_timer()
        text = self.entry.get_text()
        if not text.strip():
            self._show_browse()
            return
        self._timer = GLib.timeout_add(DEBOUNCE_MS, weak_call(self._on_debounced))

    def _on_debounced(self) -> bool:
        self._timer = 0
        self.search(self.entry.get_text())
        return GLib.SOURCE_REMOVE

    def _on_activate(self) -> None:
        self._cancel_timer()
        text = self.entry.get_text()
        if text.strip():
            self.search(text, remember=True)

    def set_query(self, text: str, *, search: bool = True) -> None:
        """検索欄に語を入れる (待たずに検索し、最近の検索にも足す)。"""
        self._suppress_changed = True
        try:
            self.entry.set_text(text)
            self.entry.set_position(-1)
        finally:
            self._suppress_changed = False
        self._cancel_timer()
        if search:
            self.search(text, remember=True)

    def _on_category(self, tile) -> None:
        self.set_query(getattr(tile, "title", ""))

    def _on_chip(self, chip) -> None:
        self.set_query(getattr(chip, "text", ""))

    def _clear_recent(self) -> None:
        self.ctx.state.clear_recent_searches()
        self._fill_recent()

    def _fill_recent(self) -> None:
        searches = list(self.ctx.state.recent_searches)
        keys = tuple(searches)
        if keys == getattr(self, "_recent_keys", None):
            return
        self._recent_keys = keys
        clear_box(self.recent_chips)
        for query in searches:
            chip = Chip(query, weak_handler(self._on_chip), icon_name="music-recent-symbolic")
            self.recent_chips.append(chip)
        self.recent_box.set_visible(bool(searches))

    # --- 検索 -------------------------------------------------------------------

    def _show_browse(self) -> None:
        if self._unsupported():
            self._query = ""
            self._show_unsupported()
            return
        self._serial += 1
        self._query = ""
        self.results.set_visible(False)
        self.browse.set_visible(True)
        self._fill_recent()

    def search(self, query: str, *, remember: bool = False, force: bool = False) -> None:
        """query を今の範囲で探す。remember なら最近の検索に足す。"""
        query = " ".join((query or "").split())
        if not query:
            self._show_browse()
            return
        if self._unsupported():
            self._query = query
            self._show_unsupported()
            return
        if remember:
            self.ctx.state.add_recent_search(query)
        self._query = query
        self._serial += 1
        serial = self._serial
        scope = self.scope
        self.browse.set_visible(False)
        self.results.set_visible(True)
        self.results.show_loading()
        self.scroll_to_top()

        def done(result) -> None:
            if serial != self._serial:
                return
            self._show_results(query, scope, result)

        catalog = self.ctx.catalog
        if scope == "library":
            catalog.search_library(query, done)
        else:
            catalog.search(scope, query, done, SEARCH_LIMIT, force=force)

    def _show_results(self, query: str, scope: str, result) -> None:
        if isinstance(result, Response):
            if result.needs_auth:
                label = provider_label(self.ctx, scope)
                self.results.show_empty("music-search-symbolic", "サインインが必要です",
                                        f"{label} はサインインが必要です (cliamp の端末で設定)")
            else:
                self.results.show_empty("music-search-symbolic", "検索できませんでした", result.message)
            return
        tracks = list(result)
        if not tracks:
            self.results.show_empty("music-search-symbolic", "結果がありません",
                                    f"「{query}」に一致する曲は見つかりませんでした。"
                                    "綴りを確かめるか、別の語や範囲で探してください。")
            return
        self._results = tracks
        self._results_query = query
        self._results_scope = scope
        clear_box(self.top_slot)
        self.top_slot.append(_TopResult(self.ctx, tracks[0], weak_call(self._play_index, 0)))
        clear_box(self.top_songs)
        for i, track in enumerate(tracks[:TOP_SONGS]):
            self.top_songs.append(TrackRow(self.ctx, track, variant="list", index=i, extra_text="",
                                           on_activate=weak_handler(self._on_row)))
        clear_box(self.all_songs)
        for i, track in enumerate(tracks):
            self.all_songs.append(TrackRow(self.ctx, track, variant="list", index=i, extra_text="",
                                           on_activate=weak_handler(self._on_row)))
        self.all_header.set_title(f"すべての曲 ({len(tracks)})")
        self.results.show_content()

    @property
    def results_tracks(self) -> list[Track]:
        return list(self._results)

    def _on_row(self, row) -> None:
        index = getattr(row, "index", None)
        if index is not None:
            self._play_index(index)

    def _play_index(self, index: int) -> None:
        if not self._results:
            return
        query = self._results_query
        self.ctx.state.add_recent_search(query)
        self.ctx.play_tracks(self._results, index, Source(provider=self._results_scope, name=f"「{query}」"))

    def refresh(self) -> None:
        self._recent_keys = None
        super().refresh()
