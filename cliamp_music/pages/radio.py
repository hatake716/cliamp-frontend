"""ラジオ (RadioPage)。

大見出し「ラジオ」、ヘッダーの右に局の検索欄 (Radio Browser)。棚:
- 「cliamp ラジオ」: cliamp の radio プロバイダーの局 (`playlists radio` の
  `l:` (登録した局) と `f:` (お気に入り))。押すと `load_provider radio <id>`。
- 「日本の人気局」「世界の人気局」: Radio Browser (ctx.radio)。押すと局 1 つの
  リストに差し替えて再生 (Source(provider="radio-browser", id=URL, name=局名))。
検索の結果は格子で出す。局のタイルは favicon を色の地の中央に置く (StationTile)。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

from ..protocol import PlaylistInfo, Response, Source, Track  # noqa: E402
from ..widgets import SectionHeader, StationTile  # noqa: E402
from .home import (  # noqa: E402
    UNSUPPORTED_TITLE,
    ContentStack,
    NaturalWidth,
    PageBase,
    _label,
    clear_box,
    make_grid,
    search_entry,
    weak_call,
    weak_handler,
)

DEBOUNCE_MS = 600
SHELF_LIMIT = 30
TILE = 150


def cliamp_station(info: PlaylistInfo) -> Track:
    """radio プロバイダーの局 (PlaylistInfo) をタイル用の曲にする (表示だけに使う)。

    お気に入り (f:) の名前は「★ 局名 [128k] · Japan」の形なので、局名と国に分ける。"""
    name = info.name.strip()
    note = "お気に入り" if info.id.startswith("f:") else "cliamp"
    if name.startswith("★"):
        name = name.lstrip("★").strip()
    country = ""
    if " · " in name:
        name, country = name.rsplit(" · ", 1)
    if name.endswith("]") and " [" in name:
        name = name[: name.rindex(" [")]
    if info.id == "l:0" and name.casefold() == "cliamp radio":
        name = "cliamp ラジオ"
    subtitle = f"{note} · {country}" if country else note
    return Track(path=f"cliamp-radio:{info.id}", title=name or info.id, stream=True, live=True,
                 meta=(("radio.country", subtitle),))


class RadioPage(PageBase):
    """ラジオのページ。"""

    PAGE_ID = "radio"
    TITLE = "ラジオ"

    def __init__(self, ctx, **_params):
        super().__init__(ctx)
        self.add_css_class("music-radio-page")
        self._timer = 0
        self._serial = 0
        self._query = ""
        self._cliamp_keys: tuple = ()
        self._loaded_sections: dict[str, tuple] = {}

        self.entry = search_entry("局を検索")
        self.entry.connect("changed", weak_call(self._on_changed))
        self.entry.connect("activate", weak_call(self._on_activate))
        field = NaturalWidth(self.entry, 220)
        field.set_valign(Gtk.Align.CENTER)
        field.add_css_class("music-radio-search")
        self.header_bar.pack_end(field)
        self.search_field = field
        self._shown_unsupported = False

        self.add_large_title()

        # 棚
        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.cliamp_shelf = self.make_shelf("cliamp ラジオ")
        self.japan_shelf = self.make_shelf("日本の人気局")
        self.world_shelf = self.make_shelf("世界の人気局")
        self.japan_note = self._note()
        self.world_note = self._note()
        self.cliamp_shelf.set_visible(False)
        self.sections.append(self.cliamp_shelf)
        self.sections.append(self.japan_shelf)
        self.sections.append(self.japan_note)
        self.sections.append(self.world_shelf)
        self.sections.append(self.world_note)
        self.body.append(self.sections)

        # 検索の結果
        self.results = ContentStack("局を探しています…")
        self.results.set_visible(False)
        self.results_header = SectionHeader("")
        self.inset(self.results_header)
        self.results.content.append(self.results_header)
        self.grid = make_grid(row_spacing=22, column_spacing=18, max_per_line=12)
        self.grid.set_margin_top(12)
        self.inset(self.grid)
        self.results.content.append(self.grid)
        self.body.append(self.results)

        self.watch(ctx.store, "connection-changed", self._on_connection)
        self.connect("shown", RadioPage._on_shown)

    def _on_shown(self) -> None:
        # Adw.NavigationView は表示したページの最初の部品 (ヘッダーの検索欄) に
        # フォーカスを移す。空の欄に入ったままだと Space で再生/一時停止できないので外す
        root = self.get_root()
        focus = root.get_focus() if root is not None and hasattr(root, "get_focus") else None
        if focus is not None and (focus is self.entry or focus.is_ancestor(self.entry)) \
                and not self.entry.get_text():
            root.set_focus(None)

    def _note(self) -> Gtk.Label:
        note = _label("", ("music-page-note",))
        note.set_wrap(True)
        self.inset(note)
        note.set_visible(False)
        return note

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        if self._unsupported():
            self._show_unsupported()
            return
        self._load_cliamp(force)
        self._load_section("japan", self.japan_shelf, self.japan_note,
                           lambda cb: self.ctx.radio.by_country("JP", cb, limit=SHELF_LIMIT))
        self._load_section("world", self.world_shelf, self.world_note,
                           lambda cb: self.ctx.radio.top(cb, limit=SHELF_LIMIT))
        if force and self._query:
            self.search(self._query)

    def on_hidden(self) -> None:
        self._cancel_timer()

    def _on_connection(self, *_args) -> None:
        if not self.ctx.store.connected:
            return
        if self._unsupported():
            self._show_unsupported()
        elif self._shown_unsupported:
            # 拡張のある cliamp に繋ぎ直した: 棚に戻して読み直す
            self._shown_unsupported = False
            self.search_field.set_visible(True)
            self.entry.set_text("")
            self._show_sections()
            self.load(force=False)
        else:
            self._load_cliamp(False)

    def on_resync(self) -> None:
        if self._unsupported() and not self._shown_unsupported:
            self._show_unsupported()

    # --- 拡張の無い cliamp ----------------------------------------------------------

    def _unsupported(self) -> bool:
        """拡張の無い cliamp に繋がっている (局の読み込みも再生もできない)。"""
        store = self.ctx.store
        return store.connected and store.api < 1

    def _show_unsupported(self) -> None:
        self._serial += 1
        self._cancel_timer()
        self._shown_unsupported = True
        self.search_field.set_visible(False)
        self.sections.set_visible(False)
        self.results.set_visible(True)
        self.results.show_empty("music-radio-symbolic", UNSUPPORTED_TITLE,
                                "再生の操作だけ使えます。ラジオ局を再生するには、GUI 用の拡張 (api 1) を"
                                "当てた cliamp が必要です。")

    def _load_cliamp(self, force: bool) -> None:
        store = self.ctx.store
        if not store.connected or not store.supports("playlists"):
            self.cliamp_shelf.set_visible(False)
            return

        def done(result) -> None:
            if isinstance(result, Response):
                self.cliamp_shelf.set_visible(False)
                return
            infos = [info for info in result if info.id.startswith(("l:", "f:"))]
            keys = tuple((i.id, i.name) for i in infos)
            if keys != self._cliamp_keys:
                self._cliamp_keys = keys
                self.cliamp_shelf.remove_all()
                for info in infos:
                    tile = StationTile(self.ctx.artwork, cliamp_station(info), weak_handler(self._on_cliamp),
                                       size=TILE)
                    tile.playlist_info = info
                    self.cliamp_shelf.append(tile)
            self.cliamp_shelf.set_visible(bool(infos))

        self.ctx.catalog.playlists("radio", done, force=force)

    def _load_section(self, key: str, shelf, note: Gtk.Label, fetch) -> None:
        def done(result) -> None:
            if isinstance(result, str):
                # 見出しは残し、その下に理由を出す (前に取れた局があればそのまま)
                note.set_text(result)
                note.set_visible(True)
                shelf.set_visible(True)
                return
            note.set_visible(not result)
            if not result:
                note.set_text("局が見つかりませんでした。")
            keys = tuple(t.path for t in result)
            if self._loaded_sections.get(key) == keys:
                return
            self._loaded_sections[key] = keys
            shelf.remove_all()
            for station in result:
                shelf.append(StationTile(self.ctx.artwork, station, weak_handler(self._on_station), size=TILE))
            shelf.set_visible(True)

        fetch(done)

    # --- 再生 -------------------------------------------------------------------

    def _on_cliamp(self, tile) -> None:
        info = getattr(tile, "playlist_info", None)
        if info is not None:
            self.ctx.load_provider("radio", info.id, 0, tile.track.display_title)

    def _on_station(self, tile) -> None:
        station = tile.track
        self.ctx.play_tracks([station], 0, Source(provider="radio-browser", id=station.path,
                                                  name=station.display_title))

    # --- 検索 -------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _on_changed(self) -> None:
        self._cancel_timer()
        if not self.entry.get_text().strip():
            self._show_sections()
            return
        self._timer = GLib.timeout_add(DEBOUNCE_MS, weak_call(self._on_debounced))

    def _on_debounced(self) -> bool:
        self._timer = 0
        self.search(self.entry.get_text())
        return GLib.SOURCE_REMOVE

    def _on_activate(self) -> None:
        self._cancel_timer()
        self.search(self.entry.get_text())

    def _show_sections(self) -> None:
        self._serial += 1
        self._query = ""
        self.results.set_visible(False)
        self.sections.set_visible(True)

    def search(self, query: str) -> None:
        """局名で探して格子に出す (空の語なら棚に戻る)。"""
        query = " ".join((query or "").split())
        if self._unsupported():
            self._show_unsupported()
            return
        if not query:
            self._show_sections()
            return
        self._query = query
        self._serial += 1
        serial = self._serial
        self.sections.set_visible(False)
        self.results.set_visible(True)
        self.results.show_loading()
        self.results_header.set_title(f"「{query}」の局")

        def done(result) -> None:
            if serial != self._serial:
                return
            if isinstance(result, str):
                self.results.show_empty("music-radio-symbolic", "局を探せませんでした", result)
                return
            if not result:
                self.results.show_empty("music-radio-symbolic", "局が見つかりません",
                                        f"「{query}」という名前の局は見つかりませんでした。")
                return
            clear_box(self.grid)
            for station in result:
                self.grid.append(StationTile(self.ctx.artwork, station, weak_handler(self._on_station), size=TILE))
            self.results.show_content()

        self.ctx.radio.search(query, done)

    def refresh(self) -> None:
        self._loaded_sections.clear()
        self._cliamp_keys = ()
        super().refresh()
