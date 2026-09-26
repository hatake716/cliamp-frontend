"""サイドバー (macOS 27 の形: 窓の端まで続く帯、赤い記号、選択は灰色の面と太字)。

Gtk.ListBox (.navigation-sidebar) で作る。Adw.Sidebar (libadwaita 1.9) も
試せるが、項目の絵が「決まった大きさの記号」に限られ、プレイリストの角丸の
小さな絵 (Artwork の遅延読み込み・溶け込み) を置けない。中の CSS の節も新しく、
利用者の gtk.css (USER 優先度) が当てる .navigation-sidebar の規則を上書きする
足場として ListBox のほうが確か。

    [信号]                    ← 透明なヘッダー (窓の操作ボタンだけ)
     検索 / ホーム / ラジオ
    ライブラリ
     最近再生した項目 / 再生中のリスト
    プレイリスト
     すべてのプレイリスト
     <ローカルのプレイリスト…> <Spotify などのプレイリスト…>
    ● cliamp · 再生中            ← 下端の状態。押すと接続の詳細

`Sidebar(ctx, on_select)`:
- on_select(page_id, params: dict) は行を押したときに呼ばれる (窓が根を差し替える)。
- `select_key(key)` で選択だけを合わせる (on_select は呼ばない)。key は
  pages.page_key() の形。合う行が無ければ選択を外す。
- `refresh()` でプロバイダーのプレイリストを取り直す。

Spotify から取り込む: 「プレイリスト」の節の見出しと行を右クリックすると「Spotify から取り込む…」(取り込んだ
ローカルのプレイリストの行なら「Spotify から更新」も) のメニュー。Spotify のプレイリストが「開発者アプリの
持ち主が Premium でない」で読めないときは、Spotify の行の代わりに「Spotify から取り込む…」の行を 1 つ
出す (選べない行。押すと取り込みの窓。ツールチップに理由)。ほかの失敗は今までどおり黙って出さない。
"""

from __future__ import annotations

import weakref
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .pages import Bindings, page_key  # noqa: E402
from .protocol import SPOTIFY_OWNER_PREMIUM, Response, is_spotify_owner_premium_required  # noqa: E402
from .widgets import Artwork  # noqa: E402

# (ページ ID, 表示名, 記号, 節)
FIXED_ITEMS = (
    ("search", "検索", "music-search-symbolic", ""),
    ("home", "ホーム", "music-home-symbolic", ""),
    ("radio", "ラジオ", "music-radio-symbolic", ""),
    ("recent", "最近再生した項目", "music-recent-symbolic", "library"),
    ("nowplaying", "再生中のリスト", "music-note-list-symbolic", "library"),
    ("playlists", "すべてのプレイリスト", "music-grid-symbolic", "playlists"),
)

SECTION_TITLES = {"library": "ライブラリ", "playlists": "プレイリスト"}

# プレイリストの行を出さないプロバイダー (local は ctx.local_playlists から、
# radio はラジオのページで扱う)
SKIP_PROVIDERS = ("local", "radio")

# 押すと窓を出す行 (ページではない)
IMPORT_ACTION = "action:spotify-import"
IMPORT_LABEL = "Spotify から取り込む…"

STATE_TEXT = {
    "playing": "再生中",
    "paused": "一時停止",
    "stopped": "停止",
    "loading": "読み込み中",
    "offline": "未接続",
}


def _label(text: str, css: str | tuple[str, ...] = (), xalign: float = 0.0) -> Gtk.Label:
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    for cls in (css,) if isinstance(css, str) else css:
        label.add_css_class(cls)
    return label


class SidebarRow(Gtk.ListBoxRow):
    """サイドバーの 1 行。記号 (赤) か小さな絵と、名前。"""

    __gtype_name__ = "CliampMusicSidebarRow"

    def __init__(self, page_id: str, title: str, *, icon_name: str | None = None, section: str = "",
                 params: dict | None = None, art: tuple | None = None, loader=None, tooltip: str | None = None):
        super().__init__()
        self.page_id = page_id
        self.params = dict(params or {})
        self.section = section
        self.key = page_key(page_id, **self.params)
        self.add_css_class("music-sidebar-row")
        if self.is_action:
            # 窓を出すだけの行。選ばない (いまのページの行の選択を動かさない)
            self.add_css_class("music-sidebar-action")
            self.set_selectable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        if art is not None and loader is not None:
            self.add_css_class("with-art")
            thumb = Artwork(20, 20, radius=4, outline=True)
            thumb.set_subject(loader, art, kind="playlist")
            thumb.set_valign(Gtk.Align.CENTER)
            box.append(thumb)
        else:
            image = Gtk.Image.new_from_icon_name(icon_name or "music-note-symbolic")
            image.set_pixel_size(16)
            image.add_css_class("music-sidebar-icon")
            box.append(image)
        self.title_label = _label(title, "music-sidebar-label")
        self.title_label.set_hexpand(True)
        box.append(self.title_label)
        self.set_child(box)
        self.set_tooltip_text(tooltip or (title if len(title) > 18 else None))
        self.update_property([Gtk.AccessibleProperty.LABEL], [title])

    @property
    def is_action(self) -> bool:
        return self.page_id.startswith("action:")


class Sidebar(Adw.Bin):
    """サイドバー全体。"""

    __gtype_name__ = "CliampMusicSidebar"

    def __init__(self, ctx, on_select: Callable[[str, dict], object]):
        super().__init__()
        self.ctx = ctx
        self._on_select = on_select
        self._syncing = False
        self._selected_key: str | None = None
        self._provider_lists: dict[str, list] = {}  # key → [PlaylistInfo]
        self._provider_order: list[str] = []
        self._provider_names: list[str] = []
        self._fetch_token = 0
        self._just_selected = None
        # Spotify のプレイリストが「持ち主が Premium でない」で読めない (取り込みの行を出す)
        self._spotify_blocked = False
        self.add_css_class("music-sidebar")

        view = Adw.ToolbarView()
        view.add_css_class("music-sidebar-view")
        header = Adw.HeaderBar()
        header.add_css_class("music-sidebar-header")
        header.set_show_title(False)
        header.set_title_widget(Gtk.Box())
        view.add_top_bar(header)

        self.list = Gtk.ListBox()
        self.list.add_css_class("navigation-sidebar")
        self.list.add_css_class("music-sidebar-list")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.set_activate_on_single_click(True)
        self.list.set_header_func(self._header_func)
        self.list.connect("row-activated", self._on_row_activated)
        self.list.connect("row-selected", self._on_row_selected)
        self._key_time = 0
        self._press_time = 0
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", Sidebar._on_list_key)
        self.list.add_controller(keys)
        press = Gtk.GestureClick()
        press.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        press.connect("pressed", Sidebar._on_list_press)
        self.list.add_controller(press)
        # 「プレイリスト」の節の右クリック (長押し) のメニュー
        secondary = Gtk.GestureClick()
        secondary.set_button(Gdk.BUTTON_SECONDARY)
        secondary.connect("pressed", Sidebar._on_list_secondary)
        self.list.add_controller(secondary)
        long_press = Gtk.GestureLongPress()
        long_press.set_touch_only(True)
        long_press.connect("pressed", Sidebar._on_list_long_press)
        self.list.add_controller(long_press)
        self.menu_actions = Gio.SimpleActionGroup()
        importer = Gio.SimpleAction.new("import-spotify", None)
        importer.connect("activate", Sidebar._on_import_action, weakref.ref(self))
        self.menu_actions.add_action(importer)
        updater = Gio.SimpleAction.new("update-spotify", GLib.VariantType.new("s"))
        updater.connect("activate", Sidebar._on_update_action, weakref.ref(self))
        self.menu_actions.add_action(updater)
        self.insert_action_group("sidebar", self.menu_actions)
        self.context_menu: Gtk.PopoverMenu | None = None
        self.list.update_property([Gtk.AccessibleProperty.LABEL], ["サイドバー"])

        for page_id, title, icon, section in FIXED_ITEMS:
            self.list.append(SidebarRow(page_id, title, icon_name=icon, section=section))
        self._playlist_rows: list[SidebarRow] = []

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.add_css_class("music-sidebar-scroller")
        scroller.set_child(self.list)
        view.set_content(scroller)

        self.status = _StatusButton(ctx)
        view.add_bottom_bar(self.status)
        self.set_child(view)

        self._bindings = Bindings(self, on_rebind=self._rebuild_all)
        self._bindings.add(ctx, "local-playlists-changed", self._on_local_playlists)
        self._bindings.add(ctx.store, "connection-changed", self._on_connection)
        self._rebuild_playlists()

    # --- 見出し -----------------------------------------------------------------

    @staticmethod
    def _header_func(row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None) -> None:
        section = getattr(row, "section", "")
        previous = getattr(before, "section", None) if before is not None else None
        if section and section != previous:
            header = row.get_header()
            if header is None or getattr(header, "section", None) != section:
                header = Gtk.Label(accessible_role=Gtk.AccessibleRole.HEADING)
                header.set_text(SECTION_TITLES.get(section, ""))
                header.set_xalign(0)
                header.set_ellipsize(Pango.EllipsizeMode.END)
                header.add_css_class("music-sidebar-section")
                header.section = section
                row.set_header(header)
        else:
            row.set_header(None)

    # --- 選択 -----------------------------------------------------------------

    def rows(self) -> list[SidebarRow]:
        out = []
        child = self.list.get_first_child()
        while child is not None:
            if isinstance(child, SidebarRow):
                out.append(child)
            child = child.get_next_sibling()
        return out

    def find_row(self, key: str | None) -> SidebarRow | None:
        if not key:
            return None
        for row in self.rows():
            if row.key == key:
                return row
        return None

    def select_key(self, key: str | None) -> None:
        """選択を key の行に合わせる (on_select は呼ばない)。合う行が無ければ外す。"""
        self._selected_key = key
        row = self.find_row(key)
        self._syncing = True
        try:
            if row is None:
                self.list.unselect_all()
            elif self.list.get_selected_row() is not row:
                self.list.select_row(row)
        finally:
            self._syncing = False

    @property
    def selected_key(self) -> str | None:
        return self._selected_key

    def _on_row_selected(self, _list, row) -> None:
        # 矢印キーで選び移ったときは開く (macOS のサイドバーと同じ)。
        # それ以外の選択の変化 (フォーカスが一覧に移ったときに GTK がカーソルの行を
        # 選ぶなど) では開かず、いまのページの行に選び直す。
        # 押したときは row-selected の直後に row-activated も来るので、同じ行の
        # 2 度目は見送る (選び直さずに同じ行を押したときだけ「根へ戻る」にする)
        if self._syncing or row is None:
            return
        by_key = GLib.get_monotonic_time() - self._key_time < 400_000
        by_click = GLib.get_monotonic_time() - self._press_time < 400_000
        if not by_key and not by_click:
            GLib.idle_add(self._resync)
            return
        self._just_selected = row
        GLib.idle_add(self._forget_just_selected)
        self._open(row)

    def _resync(self) -> bool:
        self.select_key(self._selected_key)
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _on_list_key(controller, *_args) -> bool:
        sidebar = controller.get_widget().get_ancestor(Sidebar)
        if sidebar is not None:
            sidebar._key_time = GLib.get_monotonic_time()
        return False

    @staticmethod
    def _on_list_press(gesture, *_args) -> None:
        sidebar = gesture.get_widget().get_ancestor(Sidebar)
        if sidebar is not None:
            sidebar._press_time = GLib.get_monotonic_time()

    def _forget_just_selected(self) -> bool:
        self._just_selected = None
        return GLib.SOURCE_REMOVE

    def _on_row_activated(self, _list, row) -> None:
        if self._syncing:
            return
        if row is self._just_selected:
            self._just_selected = None
            return
        self._open(row, again=True)

    def _open(self, row: SidebarRow, again: bool = False) -> None:
        if row.is_action:
            # 窓を出すだけ (選択はいまのページの行のまま)
            self.select_key(self._selected_key)
            if row.page_id == IMPORT_ACTION:
                self.open_import()
            return
        if not again and row.key == self._selected_key:
            return
        self._selected_key = row.key
        self._on_select(row.page_id, dict(row.params))

    # --- 右クリックのメニュー -----------------------------------------------------------

    def open_import(self) -> None:
        opener = getattr(self.ctx, "open_spotify_import", None)
        if callable(opener):
            opener()

    @staticmethod
    def _on_import_action(_action, _param, ref) -> None:
        sidebar = ref()
        if sidebar is not None:
            sidebar.open_import()

    @staticmethod
    def _on_update_action(_action, param, ref) -> None:
        sidebar = ref()
        updater = getattr(sidebar.ctx, "update_spotify_import", None) if sidebar is not None else None
        if callable(updater):
            updater(param.get_string())

    def context_menu_model(self, row: SidebarRow | None, *, header: bool = False) -> Gio.Menu | None:
        """row (「プレイリスト」の節の行) の右クリックのメニュー。節の外なら None。header なら row の上の
        節の見出し (「プレイリスト」) のメニュー (取り込みだけ)。"""
        if row is None or row.section != "playlists":
            return None
        store = self.ctx.store
        can_save = store.connected and store.supports("playlist_add")
        menu = Gio.Menu()
        main = Gio.Menu()
        main.append(IMPORT_LABEL, "sidebar.import-spotify")
        menu.append_section(None, main)
        self.menu_actions.lookup_action("import-spotify").set_enabled(can_save)
        name = row.params.get("id") if row.params.get("provider") == "local" and not header else None
        lookup = getattr(self.ctx, "import_record", None)
        if name and callable(lookup) and lookup(name) is not None:
            update = Gio.Menu()
            item = Gio.MenuItem.new("Spotify から更新", None)
            item.set_action_and_target_value("sidebar.update-spotify", GLib.Variant.new_string(name))
            update.append_item(item)
            menu.append_section(None, update)
            busy = name in getattr(self.ctx, "spotify_imports_busy", ())
            self.menu_actions.lookup_action("update-spotify").set_enabled(
                can_save and store.supports("playlist_delete") and not busy)
        return menu

    def popup_context_menu(self, row: SidebarRow | None, x: float, y: float, *, header: bool = False) -> bool:
        """一覧の (x, y) を指して row (header なら row の上の節の見出し) の右クリックのメニューを出す。
        出せたら True。"""
        model = self.context_menu_model(row, header=header)
        if model is None:
            return False
        if self.context_menu is not None:
            self.context_menu.unparent()
        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.add_css_class("music-sidebar-menu")
        popover.set_parent(self.list)
        popover.set_has_arrow(False)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", Sidebar._on_menu_closed)
        self.context_menu = popover
        popover.popup()
        return True

    @staticmethod
    def _on_menu_closed(popover: Gtk.PopoverMenu) -> None:
        # 項目の action は閉じた後に走ることがあるので、外すのは一呼吸おいてから
        def drop() -> bool:
            sidebar = popover.get_ancestor(Sidebar)
            if sidebar is not None and sidebar.context_menu is popover:
                sidebar.context_menu = None
            if popover.get_parent() is not None:
                popover.unparent()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(drop)

    def row_at(self, y: float) -> tuple[SidebarRow | None, bool]:
        """一覧の y にある行と、そこがその行の上の節の見出しか。見出しは行の外に置かれるので
        (Gtk.ListBox.get_row_at_y は見出しの上では None)、見出しの矩形でも探す。"""
        row = self.list.get_row_at_y(int(y))
        if row is not None:
            return row, False
        for candidate in self.rows():
            header = candidate.get_header()
            if header is None or not header.get_visible():
                continue
            ok, bounds = header.compute_bounds(self.list)
            if ok and bounds.get_y() <= y < bounds.get_y() + bounds.get_height():
                return candidate, True
        return None, False

    @staticmethod
    def _on_list_secondary(gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        listbox = gesture.get_widget()
        sidebar = listbox.get_ancestor(Sidebar)
        if sidebar is None:
            return
        row, header = sidebar.row_at(y)
        if sidebar.popup_context_menu(row, x, y, header=header):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    @staticmethod
    def _on_list_long_press(gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
        listbox = gesture.get_widget()
        sidebar = listbox.get_ancestor(Sidebar)
        if sidebar is not None:
            row, header = sidebar.row_at(y)
            sidebar.popup_context_menu(row, x, y, header=header)

    def do_dispose(self) -> None:
        menu = getattr(self, "context_menu", None)
        if menu is not None and menu.get_parent() is not None:
            menu.unparent()
        self.context_menu = None
        Adw.Bin.do_dispose(self)

    # --- プレイリストの行 -----------------------------------------------------------

    def _on_local_playlists(self, _ctx) -> None:
        self._rebuild_playlists()

    def _on_connection(self, _store) -> None:
        store = self.ctx.store
        if store.connected and store.supports("providers"):
            self.refresh()
        else:
            self._provider_lists.clear()
            self._provider_order = []
            self._spotify_blocked = False
            self._rebuild_playlists()

    def _rebuild_all(self) -> None:
        self._rebuild_playlists()
        if self.ctx.store.connected and not self._provider_order:
            self.refresh()

    def refresh(self) -> None:
        """プロバイダーの一覧とプレイリストを取り直す (失敗は黙って出さない)。"""
        store = self.ctx.store
        if not store.connected or not store.supports("providers"):
            self._provider_lists.clear()
            self._provider_order = []
            self._spotify_blocked = False
            self._rebuild_playlists()
            return
        self._fetch_token += 1
        token = self._fetch_token
        # 自分を掴まずに結果を受ける (取得中に窓が閉じても部品を残さない)
        me = weakref.ref(self)

        def on_providers(result) -> None:
            this = me()
            if this is None or token != this._fetch_token:
                return
            if isinstance(result, Response):
                this._provider_names = []
                return
            this._provider_names = [p.name or p.key for p in result if not p.virtual]
            wanted = [p for p in result if p.playlists and p.key not in SKIP_PROVIDERS]
            this._provider_order = [p.key for p in wanted]
            if "spotify" not in this._provider_order:
                this._spotify_blocked = False
            for key in list(this._provider_lists):
                if key not in this._provider_order:
                    del this._provider_lists[key]
            this._rebuild_playlists()
            this.status.refresh_details()
            for info in wanted:
                this.ctx.catalog.playlists(info.key, lambda res, k=info.key: on_lists(k, res))

        def on_lists(key: str, result) -> None:
            this = me()
            if this is None or token != this._fetch_token:
                return
            if isinstance(result, Response):
                # サインインが要る・失敗した: 黙って出さない。ただし Spotify の開発者アプリの持ち主が
                # Premium でないときは、代わりに取り込みの行を出す (公開プレイリストはそれで使える)
                this._provider_lists.pop(key, None)
                if key == "spotify":
                    this._spotify_blocked = is_spotify_owner_premium_required(result.error)
            else:
                this._provider_lists[key] = list(result)
                if key == "spotify":
                    this._spotify_blocked = False
            this._rebuild_playlists()

        self.ctx.catalog.providers(on_providers)

    def _wanted_playlist_rows(self) -> list[tuple[str, dict, str]]:
        out = []
        store = self.ctx.store
        if store.connected and store.supports("playlists"):
            for name in self.ctx.local_playlists:
                out.append((name, {"provider": "local", "id": name, "name": name}, f"local:{name}"))
            for key in self._provider_order:
                for info in self._provider_lists.get(key, []):
                    out.append((info.name or info.id,
                                {"provider": info.provider or key, "id": info.id, "name": info.name or info.id},
                                f"{info.provider or key}:{info.id}"))
        return out

    def _rebuild_playlists(self) -> None:
        wanted = self._wanted_playlist_rows()
        store = self.ctx.store
        action = (self._spotify_blocked and store.connected and store.supports("playlists")
                  and store.supports("playlist_add"))
        current = [(row.title_label.get_text(), row.params) for row in self._playlist_rows]
        current_action = any(row.is_action for row in self._playlist_rows)
        if current == [(title, params) for title, params, _art in wanted] + ([(IMPORT_LABEL, {})] if action else []) \
                and current_action == action:
            return
        selected_key = self._selected_key
        for row in self._playlist_rows:
            self.list.remove(row)
        self._playlist_rows = []
        for title, params, art_key in wanted:
            row = SidebarRow("playlist", title, section="playlists", params=params,
                             art=("placeholder", art_key, "playlist"), loader=self.ctx.artwork)
            self.list.append(row)
            self._playlist_rows.append(row)
        if action:
            row = SidebarRow(IMPORT_ACTION, IMPORT_LABEL, icon_name="music-import-symbolic", section="playlists",
                             tooltip=SPOTIFY_OWNER_PREMIUM)
            self.list.append(row)
            self._playlist_rows.append(row)
        self.list.invalidate_headers()
        self.select_key(selected_key)

    @property
    def provider_names(self) -> list[str]:
        return list(self._provider_names)


class _StatusButton(Gtk.MenuButton):
    """下端の状態表示「● cliamp · 再生中」。押すと接続の詳細。"""

    __gtype_name__ = "CliampMusicSidebarStatus"

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.add_css_class("music-sidebar-status")
        self.set_has_frame(False)
        self.set_direction(Gtk.ArrowType.UP)
        self.set_tooltip_text("cliamp との接続")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.dot = Gtk.Box()
        self.dot.add_css_class("music-status-dot")
        self.dot.set_valign(Gtk.Align.CENTER)
        self.dot.set_size_request(8, 8)
        box.append(self.dot)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        texts.set_hexpand(True)
        self.state_label = _label("cliamp", "music-status-title")
        self.detail_label = _label("", "music-status-detail")
        texts.append(self.state_label)
        texts.append(self.detail_label)
        box.append(texts)
        self.set_child(box)

        self.popover = Gtk.Popover()
        self.popover.add_css_class("music-status-popover")
        self.popover.set_has_arrow(False)
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)
        grid.set_margin_start(8)
        grid.set_margin_end(8)
        heading = _label("cliamp との接続", "music-status-heading")
        grid.attach(heading, 0, 0, 2, 1)
        self._values: dict[str, Gtk.Label] = {}
        for row, (key, title) in enumerate((("state", "状態"), ("socket", "ソケット"),
                                             ("api", "IPC"), ("providers", "プロバイダー")), start=1):
            name = _label(title, "music-status-key")
            name.set_valign(Gtk.Align.START)
            value = Gtk.Label()
            value.set_xalign(0)
            value.set_wrap(True)
            value.set_wrap_mode(Pango.WrapMode.CHAR)
            value.set_max_width_chars(34)
            value.set_selectable(key == "socket")
            value.add_css_class("music-status-value")
            grid.attach(name, 0, row, 1, 1)
            grid.attach(value, 1, row, 1, 1)
            self._values[key] = value
        reconnect = Gtk.Button(label="接続を確かめる")
        reconnect.add_css_class("music-status-reconnect")
        reconnect.set_halign(Gtk.Align.START)
        reconnect.set_margin_top(4)
        reconnect.connect("clicked", self._on_reconnect)
        grid.attach(reconnect, 1, 5, 1, 1)
        self.popover.set_child(grid)
        # 開いたときに選べる文字 (ソケット) に移ると全体が選択されて見えるので、ボタンに置く
        self._reconnect = reconnect
        self.popover.connect("show", _StatusButton._on_popover_show)
        self.set_popover(self.popover)
        self.connect("notify::active", self._on_active)

        self._state = ""
        self._bindings = Bindings(self, on_rebind=self.update)
        self._bindings.add(ctx.store, "state-changed", self._on_store)
        self._bindings.add(ctx.store, "connection-changed", self._on_store)
        self._bindings.add(ctx.store, "status-changed", self._on_status)
        self.update()

    def _on_store(self, _store) -> None:
        self.update()

    def _on_status(self, _store) -> None:
        # 読み込み中 (stopped + buffering) の出入りは state-changed では来ない
        if self._state_name() != self._state:
            self.update()

    def _state_name(self) -> str:
        store = self.ctx.store
        if not store.connected:
            return "offline"
        st = store.status
        if st.state == "stopped" and st.buffering:
            return "loading"
        return st.state if st.state in STATE_TEXT else "stopped"

    def update(self) -> None:
        store = self.ctx.store
        state = self._state_name()
        self._state = state
        self.state_label.set_text(f"cliamp · {STATE_TEXT[state]}")
        for cls in ("playing", "paused", "stopped", "loading", "offline"):
            if cls == state:
                self.dot.add_css_class(cls)
            else:
                self.dot.remove_css_class(cls)
        if not store.connected:
            detail = "接続を待っています…"
        elif store.api >= 1:
            detail = f"拡張 IPC · api {store.api}"
        else:
            detail = "基本操作のみ · api 0"
        self.detail_label.set_text(detail)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"cliamp {STATE_TEXT[state]}、{detail}"])
        self._fill_details()

    def _fill_details(self) -> None:
        store = self.ctx.store
        self._values["state"].set_text(STATE_TEXT[self._state_name()])
        self._values["socket"].set_text(self.ctx.client.socket_path)
        if not store.connected:
            api = "—"
        elif store.api >= 1:
            api = f"api {store.api} (拡張 IPC に対応)"
        else:
            api = "api 0 (拡張なし。再生の操作だけ使えます)"
        self._values["api"].set_text(api)
        sidebar = self.get_ancestor(Sidebar)
        names = sidebar.provider_names if sidebar is not None else []
        if not store.connected or store.api < 1:
            providers = "—"
        else:
            providers = "、".join(names) if names else "取得中…"
        self._values["providers"].set_text(providers)

    def refresh_details(self) -> None:
        self._fill_details()

    def _on_active(self, *_args) -> None:
        if self.get_active():
            self._fill_details()

    @staticmethod
    def _on_popover_show(popover: Gtk.Popover) -> None:
        button = popover.get_parent()
        if isinstance(button, _StatusButton):
            GLib.idle_add(button._focus_reconnect)

    def _focus_reconnect(self) -> bool:
        self._reconnect.grab_focus()
        self._values["socket"].select_region(0, 0)
        return GLib.SOURCE_REMOVE

    def _on_reconnect(self, _button) -> None:
        self.ctx.client.probe()
        self.ctx.client.poll_now()
        self.popover.popdown()
