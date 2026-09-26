"""MusicWindow: 骨格 (サイドバー | ナビゲーション | 右パネル) と再生バーの重ね合わせ。

    Adw.ApplicationWindow.music.music-window   既定 1180x760 (前回の大きさ)、最小 760x520
    └ Adw.ToastOverlay
      └ Gtk.Stack root ("main" / "fullscreen")
        ├ main: Adw.OverlaySplitView (.music-split)           760sp 以下で畳む
        │   ├ sidebar: Sidebar (幅 220)
        │   └ content: Gtk.Overlay (再生バーを右パネルより上に浮かべる)
        │       ├ Adw.OverlaySplitView (.music-panel-split、右パネル、幅 300)
        │       │   ├ content: Gtk.Box (縦)
        │       │   │   ├ Adw.Banner (拡張の無い cliamp のときの細い帯)
        │       │   │   └ Gtk.Overlay (.music-content)
        │       │   │       ├ Adw.NavigationView (ページ)
        │       │   │       ├ 未接続の全面の空状態
        │       │   │       └ 下端のフェード (再生バーの後ろを地の色へ溶かす帯、高さ 110)
        │       │   └ sidebar: Gtk.Stack (LyricsPanel / QueuePanel)
        │       └ PlayerBar (内容の列の中央下、下余白 14、最大幅 780、左右の余白 28)
        └ fullscreen: fullscreen.FullscreenPlayer (最初に開くときに作る)

右パネルは (Revealer ではなく) 終わり側の Adw.OverlaySplitView に
入れる。広い窓では右から滑り込んで内容を押し縮め (Revealer と同じ見た目)、
1080sp 以下では内容の上に重ねる。狭い窓でパネルを開いたときに、内容の列が
再生バーの最小幅より細くなって窓が勝手に広がるのを避けるため。
再生バーは右パネルの割り当て (OverlaySplitView) の外に置く。重ねて出したパネルは
内容全体にクリックを奪う覆い (shield) を掛けるので、内容の中に置くとバーが押せなくなる。
重ねて出している間は、バーはパネルの左に収まればそこへ縮め、収まらなければ内容の列の
幅のままパネルの上に出す (パネルの一覧は下に余白を足して最後の行まで送れる)。

公開する操作 (ctx.window として pages / panels / app から使う):
- navigate(page_id, **params): サイドバーの項目は根を差し替え、それ以外は積む。
- toast(text) -> Adw.Toast | None: 平文 (マークアップにしない)。長い文は真ん中を省く。
- show_panel(name) / toggle_panel(name): "" | "lyrics" | "queue"。GuiState.right_panel に覚える。
- show_fullscreen(on): フルスクリーンプレーヤー。Esc で戻る。
- reveal_current(): 再生中のリストで今の曲を見せる。
- focus_search(): 検索のページを開いて入力欄に移る。
- current_page() -> Adw.NavigationPage | None。
- プロパティ "sidebar-collapsed" (bool): サイドバーが畳まれているか。畳まれている間は
  各ページのヘッダーの先頭にサイドバーを出すボタンを自動で足す (ページ側で要らなければ
  ページに `wants_sidebar_toggle = False` を置く)。
- シグナル "panel-changed" (str)、"page-changed" (str page_key)。
"""

from __future__ import annotations

import os
import shlex
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, log  # noqa: E402
from .pages import Bindings, create_page, is_sidebar_page, page_key  # noqa: E402
from .panels import LyricsPanel, QueuePanel  # noqa: E402
from .playerbar import BAR_HEIGHT, PlayerBar  # noqa: E402
from .sidebar import Sidebar  # noqa: E402
from .widgets import CircleButton, EmptyState, PathLabel, inside_popover, space_toggles  # noqa: E402

MIN_WIDTH, MIN_HEIGHT = 760, 520
SIDEBAR_WIDTH = 220
PANEL_WIDTH = 300
BAR_MAX_WIDTH = 780
BAR_SIDE_MARGIN = 28
BAR_BOTTOM_MARGIN = 14
FADE_HEIGHT = 110
PANELS = ("lyrics", "queue")
START_COMMAND = ("systemctl", "--user", "start", "cliamp.service")
UNSUPPORTED_BANNER = "この cliamp は拡張 IPC に対応していません。再生の操作だけ使えます"
# トーストの題の最大幅 (文字数の目安。日本語ならおよそ 35 文字)
TOAST_MAX_CHARS = 64


class _SidebarGlyph(Gtk.Widget):
    """「サイドバーを表示」の記号 (角丸の枠と、左に塗った帯)。色は CSS の color。

    記号のアイコンはテーマによって形も有無も違う (GTK 4.22 は線の SVG を塗りつぶす)
    ので、ここで描く。"""

    __gtype_name__ = "CliampMusicSidebarGlyph"

    def __init__(self, size: int = 16):
        super().__init__()
        self._size = size
        self.set_can_target(False)

    def do_measure(self, orientation, for_size):
        return self._size, self._size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        color = self.get_color()
        w, h = self._size * 0.94, self._size * 0.78
        x, y = (width - w) / 2, (height - h) / 2
        outer = Gsk.RoundedRect()
        outer.init_from_rect(Graphene.Rect().init(x, y, w, h), 3.0)
        line = 1.6
        snapshot.append_border(outer, [line] * 4, [color] * 4)
        bar = Graphene.Rect().init(x + line + 1.2, y + line + 1.2, w * 0.28, h - 2 * (line + 1.2))
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bar, 1.0)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(color, bar)
        snapshot.pop()


def _descendants(widget: Gtk.Widget, depth: int = 0, limit: int = 8):
    child = widget.get_first_child()
    while child is not None:
        yield child
        if depth < limit:
            yield from _descendants(child, depth + 1, limit)
        child = child.get_next_sibling()


class MusicWindow(Adw.ApplicationWindow):
    """ミュージックのメインの窓。`MusicWindow(app, ctx, initial_page=None, initial_params=None)`。"""

    __gtype_name__ = "CliampMusicWindow"
    __gsignals__ = {
        "panel-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "page-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    sidebar_collapsed = GObject.Property(type=bool, default=False)

    @property
    def sidebar_toggle_needed(self) -> bool:
        """ページがサイドバーを出すボタンを要るか (= サイドバーが畳まれている)。
        変化は "notify::sidebar-collapsed" で分かる。"""
        return bool(self.sidebar_collapsed)

    def __init__(self, app, ctx, *, initial_page: str | None = None, initial_params: dict | None = None):
        super().__init__(application=app)
        self.ctx = ctx
        ctx.window = self
        self._panel = ""
        self._fullscreen_player = None
        self._we_fullscreened = False
        self._starting = None
        self._first_connection_seen = False
        self._toast_last: tuple[str, float] | None = None
        self._navigating = False

        self.add_css_class("music")
        self.add_css_class("music-window")
        self.set_title(APP_NAME)
        self.set_icon_name(APP_ID)
        self.set_size_request(MIN_WIDTH, MIN_HEIGHT)
        state = ctx.state
        self.set_default_size(max(MIN_WIDTH, state.window_width), max(MIN_HEIGHT, state.window_height))
        if state.window_maximized:
            self.maximize()

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)
        self.root_stack = Gtk.Stack()
        self.root_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.root_stack.set_transition_duration(260)
        self._toasts.set_child(self.root_stack)

        # サイドバー | 内容
        self.split = Adw.OverlaySplitView()
        self.split.add_css_class("music-split")
        self.split.set_min_sidebar_width(SIDEBAR_WIDTH)
        self.split.set_max_sidebar_width(SIDEBAR_WIDTH)
        self.split.set_sidebar_width_fraction(0.2)
        self.split.set_enable_show_gesture(True)
        self.root_stack.add_named(self.split, "main")

        self.sidebar = Sidebar(ctx, on_select=self._on_sidebar_select)
        self.split.set_sidebar(self.sidebar)

        # 内容 | 右パネル
        self.panel_split = Adw.OverlaySplitView()
        self.panel_split.add_css_class("music-panel-split")
        self.panel_split.set_sidebar_position(Gtk.PackType.END)
        self.panel_split.set_min_sidebar_width(PANEL_WIDTH)
        self.panel_split.set_max_sidebar_width(PANEL_WIDTH)
        self.panel_split.set_sidebar_width_fraction(0.3)
        self.panel_split.set_show_sidebar(False)
        # 畳む・広げるで開け閉めを勝手に変えない (開いているかは self._panel が決める)
        self.panel_split.set_pin_sidebar(True)
        self.panel_split.set_enable_show_gesture(False)
        self.panel_split.connect("notify::show-sidebar", self._on_panel_split_shown)
        self.panel_split.connect("notify::show-sidebar", MusicWindow._on_panel_layout)
        self.panel_split.connect("notify::collapsed", MusicWindow._on_panel_layout)
        # 再生バーは右パネルの割り当ての外 (上) に浮かべる
        self.bar_layer = Gtk.Overlay()
        self.bar_layer.add_css_class("music-bar-layer")
        self.bar_layer.set_child(self.panel_split)
        self.split.set_content(self.bar_layer)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.add_css_class("music-content-column")
        self.column = column
        self.banner = Adw.Banner()
        self.banner.set_title(UNSUPPORTED_BANNER)
        self.banner.set_use_markup(False)
        self.banner.add_css_class("music-banner")
        self.banner.set_revealed(False)
        column.append(self.banner)

        self.content = Gtk.Overlay()
        self.content.add_css_class("music-content")
        self.content.set_vexpand(True)
        self.content.set_hexpand(True)
        column.append(self.content)
        self.panel_split.set_content(column)

        self.nav = Adw.NavigationView()
        self.nav.add_css_class("music-nav")
        self.nav.connect("notify::visible-page", self._on_visible_page)
        self.content.set_child(self.nav)

        self.offline = self._build_offline()
        self.offline.set_visible(False)
        self.content.add_overlay(self.offline)

        self.fade = Gtk.Box()
        self.fade.add_css_class("music-bar-fade")
        self.fade.set_can_target(False)
        self.fade.set_valign(Gtk.Align.END)
        self.fade.set_size_request(-1, FADE_HEIGHT)
        self.content.add_overlay(self.fade)

        self.content.connect("get-child-position", MusicWindow._place_overlay_child)
        self.player_bar = PlayerBar(ctx)
        self.bar_layer.add_overlay(self.player_bar)
        self.bar_layer.connect("get-child-position", MusicWindow._place_bar)
        self._bar_tick = 0
        self._bar_tick_until = 0

        self.panel_stack = Gtk.Stack()
        self.panel_stack.add_css_class("music-panel-stack")
        self.panel_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.panel_stack.set_transition_duration(160)
        self.lyrics_panel = LyricsPanel(ctx)
        self.queue_panel = QueuePanel(ctx)
        self.panel_stack.add_named(self.lyrics_panel, "lyrics")
        self.panel_stack.add_named(self.queue_panel, "queue")
        self.panel_split.set_sidebar(self.panel_stack)

        self._install_breakpoints()
        self._install_keys()
        show_sidebar = Gio.SimpleAction.new("show-sidebar", None)
        show_sidebar.connect("activate", MusicWindow._on_show_sidebar_action, self)
        self.add_action(show_sidebar)

        self.connect("close-request", MusicWindow._on_close_request)
        self._bindings = Bindings(self, on_rebind=self._on_connection_now)
        self._bindings.add(ctx.store, "connection-changed", self._on_connection)

        # 最初のページ
        page_id, params = self._initial_page(initial_page, initial_params)
        self._navigate_root(page_id, params, animate=False)
        panel = state.right_panel if state.right_panel in PANELS else ""
        if panel:
            self.show_panel(panel, save=False)
        self._on_connection_now()

    # --- 組み立て ------------------------------------------------------------------

    def _build_offline(self) -> Gtk.Widget:
        view = Adw.ToolbarView()
        view.add_css_class("music-offline")
        header = Adw.HeaderBar()
        header.add_css_class("music-offline-header")
        header.set_show_title(False)
        view.add_top_bar(header)
        view.set_extend_content_to_top_edge(True)
        self.offline_state = EmptyState(
            "music-note-symbolic", "cliamp に接続できません",
            "cliamp が動いていないか、ソケットに繋がりません。",
            button_label="cliamp を起動", on_button=self._on_start_cliamp)
        self.offline_state.set_margin_bottom(BAR_HEIGHT + BAR_BOTTOM_MARGIN)
        # ソケットのパス: "/" でだけ折り返す (語の途中で折って "-" を足さない)
        self.offline_socket = PathLabel()
        self.offline_socket.add_css_class("music-offline-socket")
        self.offline_state.insert_child_after(self.offline_socket, self.offline_state.description_label)
        view.set_content(self.offline_state)
        return view

    def _install_breakpoints(self) -> None:
        # 右パネルは 1080sp 以下で内容の上に重ねる。サイドバーは 760sp 以下で畳む
        # (後から足したものが勝つので、狭いほうに両方の設定を入れる)
        overlay_panel = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1080sp"))
        overlay_panel.add_setter(self.panel_split, "collapsed", True)
        self.add_breakpoint(overlay_panel)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 760sp"))
        narrow.add_setter(self.panel_split, "collapsed", True)
        narrow.add_setter(self.split, "collapsed", True)
        narrow.add_setter(self, "sidebar-collapsed", True)
        self.add_breakpoint(narrow)
        self.split.connect("notify::collapsed", self._on_split_collapsed)

    def _install_keys(self) -> None:
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", MusicWindow._on_key_pressed)
        self.add_controller(keys)
        self.connect("notify::focus-widget", MusicWindow._on_focus_changed)

    def _initial_page(self, page_id: str | None, params: dict | None) -> tuple[str, dict]:
        state = self.ctx.state
        if page_id:
            if page_id == "playlist" and not params:
                saved = state.get("last_playlist")
                if isinstance(saved, dict) and saved.get("id"):
                    return "playlist", dict(saved)
                return "playlists", {}
            if is_sidebar_page(page_id) or page_id == "playlist":
                return page_id, dict(params or {})
            log(f"知らないページです: {page_id} (ホームを開きます)")
            return "home", {}
        last = state.last_page or "home"
        if last == "playlist":
            saved = state.get("last_playlist")
            if isinstance(saved, dict) and saved.get("id"):
                return "playlist", {k: saved.get(k, "") for k in ("provider", "id", "name")}
            return "playlists", {}
        return (last if is_sidebar_page(last) else "home"), {}

    # --- 再生バーとフェードの置き場 ----------------------------------------------------

    @staticmethod
    def bar_geometry(x0: float, avail: float, height: int, minimum: int, *, panel_over: bool) -> tuple[int, int, int]:
        """再生バーの (x, y, 幅)。x0・avail は内容の列の左端と幅。

        panel_over: 右パネルを内容の上に重ねて出している。バーがパネルの左に収まる (PlayerBar.TINY
        より広く取れる) ならそこへ縮め、収まらなければ列の幅のままパネルの上に出す。"""
        if panel_over and avail - PANEL_WIDTH - 2 * BAR_SIDE_MARGIN > PlayerBar.TINY:
            avail -= PANEL_WIDTH
        bar_width = min(BAR_MAX_WIDTH, avail - 2 * BAR_SIDE_MARGIN)
        if bar_width < minimum:
            bar_width = min(avail - 16, max(minimum, bar_width))
        bar_width = int(max(1, bar_width))
        x = int(x0 + max(0, (avail - bar_width) // 2))
        return x, max(0, height - BAR_HEIGHT - BAR_BOTTOM_MARGIN), bar_width

    @staticmethod
    def _place_bar(layer: Gtk.Overlay, widget: Gtk.Widget, allocation: Gdk.Rectangle) -> bool:
        if not isinstance(widget, PlayerBar):
            return False
        window = layer.get_root()
        column = getattr(window, "column", None)
        x0, avail = 0.0, float(layer.get_width())
        if column is not None:
            ok, bounds = column.compute_bounds(layer)
            if ok and bounds.get_width() > 0:
                x0, avail = bounds.get_x(), bounds.get_width()
        panel_split = getattr(window, "panel_split", None)
        panel_over = bool(panel_split is not None and panel_split.get_collapsed()
                          and panel_split.get_show_sidebar())
        minimum = widget.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        x, y, width = MusicWindow.bar_geometry(x0, avail, layer.get_height(), minimum, panel_over=panel_over)
        allocation.x, allocation.y, allocation.width, allocation.height = x, y, width, BAR_HEIGHT
        return True

    @staticmethod
    def _on_panel_layout(split, _pspec) -> None:
        """右パネルの開け閉めと畳み方が変わった: バーを置き直す (滑り込む間は毎こま)。"""
        window = split.get_root()
        if not isinstance(window, MusicWindow) or not hasattr(window, "panel_stack"):
            return
        stack = window.panel_stack
        if split.get_collapsed():
            stack.add_css_class("under-bar")
        else:
            stack.remove_css_class("under-bar")
        window.bar_layer.queue_allocate()
        clock = window.bar_layer.get_frame_clock()
        now = clock.get_frame_time() if clock is not None else GLib.get_monotonic_time()
        window._bar_tick_until = now + 600_000  # パネルの滑り込み (Adwaita の動き) の間
        if not window._bar_tick:
            window._bar_tick = window.bar_layer.add_tick_callback(MusicWindow._bar_tick_step)

    @staticmethod
    def _bar_tick_step(layer: Gtk.Overlay, clock) -> bool:
        window = layer.get_root()
        layer.queue_allocate()
        if not isinstance(window, MusicWindow) or clock.get_frame_time() > window._bar_tick_until:
            if isinstance(window, MusicWindow):
                window._bar_tick = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    @staticmethod
    def _place_overlay_child(overlay: Gtk.Overlay, widget: Gtk.Widget, allocation: Gdk.Rectangle) -> bool:
        width, height = overlay.get_width(), overlay.get_height()
        if widget.has_css_class("music-bar-fade"):
            allocation.x = 0
            allocation.width = width
            allocation.height = min(height, FADE_HEIGHT)
            allocation.y = height - allocation.height
            return True
        return False

    # --- ナビゲーション ----------------------------------------------------------------

    def current_page(self) -> Adw.NavigationPage | None:
        return self.nav.get_visible_page()

    def root_page(self) -> Adw.NavigationPage | None:
        stack = self.nav.get_navigation_stack()
        return stack.get_item(0) if stack is not None and stack.get_n_items() else None

    def navigate(self, page_id: str, **params) -> None:
        """サイドバーの項目は根を差し替え、それ以外 (プレイリストなど) は積む。"""
        if self.root_stack.get_visible_child_name() == "fullscreen":
            self.show_fullscreen(False)
        if is_sidebar_page(page_id):
            self._navigate_root(page_id, params)
            return
        key = page_key(page_id, **params)
        visible = self.nav.get_visible_page()
        if visible is not None and visible.get_tag() == key:
            return
        stacked = self._stacked_page(key)
        if stacked is not None:
            # 同じページが下に積まれている: そこまで戻る (同じ名札のページは 2 つ置けない)
            self.nav.pop_to_page(stacked)
            return
        self._release_tag(key)
        page = create_page(self.ctx, page_id, **params)
        self._decorate_page(page)
        self.nav.push(page)

    def _stacked_page(self, key: str) -> Adw.NavigationPage | None:
        stack = self.nav.get_navigation_stack()
        for i in range(stack.get_n_items() if stack is not None else 0):
            page = stack.get_item(i)
            if page.get_tag() == key:
                return page
        return None

    def _release_tag(self, key: str) -> None:
        """退場の動きの途中のページが同じ名札を持っていれば外す。

        Adw.NavigationView は、戻る動きの間まだ子として残っているページと同じ tag の
        ページを積めない (Adwaita-CRITICAL「Duplicate page tag」で積むこと自体が
        失敗する)。戻ってすぐ同じプレイリストを開き直したときに起きる。"""
        leaving = self.nav.find_page(key)
        if leaving is not None and self._stacked_page(key) is None:
            leaving.set_tag(None)

    def _on_sidebar_select(self, page_id: str, params: dict) -> None:
        self._navigate_root(page_id, params)
        if self.split.get_collapsed():
            self.split.set_show_sidebar(False)

    def _navigate_root(self, page_id: str, params: dict, animate: bool = False) -> None:
        key = page_key(page_id, **params)
        root = self.root_page()
        if root is not None and root.get_tag() == key:
            # 同じ根: 積んだページを (動きなしで) 戻し、開き方の指定 (reveal など) だけ伝える
            if self.nav.get_visible_page() is not root:
                previous = self.nav.get_animate_transitions()
                self.nav.set_animate_transitions(animate)
                try:
                    self.nav.pop_to_page(root)
                finally:
                    self.nav.set_animate_transitions(previous)
            if params.get("reveal") and hasattr(root, "reveal_current"):
                root.reveal_current()
                return
            if not params.get("reveal"):
                return
        self._release_tag(key)
        page = self._stacked_page(key)
        if page is None:
            page = create_page(self.ctx, page_id, **params)
            self._decorate_page(page)
        # else: 積んであるページ (プレイリストの詳細を開いたままサイドバーで同じ行を
        # 押したなど) はそのまま根にする。作り直すと同じ名札のページが 2 つになる
        previous = self.nav.get_animate_transitions()
        self.nav.set_animate_transitions(animate)
        try:
            self.nav.replace([page])
        finally:
            self.nav.set_animate_transitions(previous)
        # 積んであったページを根にしたときは表示が変わらない (map されない) ので、
        # 「‹」(前のページがあるときだけ出す) をここで見直させる
        update_back = getattr(page, "_update_back_button", None)
        if callable(update_back):
            update_back()
        state = self.ctx.state
        state.last_page = page_id
        if page_id == "playlist":
            state.set("last_playlist", {k: str(params.get(k, "") or "") for k in ("provider", "id", "name")})
        self._sync_sidebar()

    def _on_visible_page(self, *_args) -> None:
        self._sync_sidebar()
        page = self.nav.get_visible_page()
        self.emit("page-changed", (page.get_tag() or "") if page is not None else "")

    def _sync_sidebar(self) -> None:
        visible = self.nav.get_visible_page()
        key = visible.get_tag() if visible is not None else None
        if key and self.sidebar.find_row(key) is not None:
            self.sidebar.select_key(key)
            return
        root = self.root_page()
        self.sidebar.select_key(root.get_tag() if root is not None else None)

    def _decorate_page(self, page: Gtk.Widget) -> None:
        """畳んだサイドバーを出すボタンをページのヘッダーの先頭に足す。"""
        if getattr(page, "wants_sidebar_toggle", True) is False:
            return
        header = next((w for w in _descendants(page) if isinstance(w, Adw.HeaderBar)), None)
        if header is None:
            return
        button = CircleButton("sidebar-show-symbolic", "サイドバーを表示", size=32, glass=True)
        button.set_child(_SidebarGlyph(16))
        button.add_css_class("music-sidebar-toggle")
        button.set_action_name("win.show-sidebar")
        self.bind_property("sidebar-collapsed", button, "visible", GObject.BindingFlags.SYNC_CREATE)
        header.pack_start(button)

    def _on_split_collapsed(self, split, _pspec) -> None:
        # 畳んだときは隠し、広げたときは出しておく
        split.set_show_sidebar(not split.get_collapsed())

    def show_sidebar(self) -> None:
        self.split.set_show_sidebar(True)

    @staticmethod
    def _on_show_sidebar_action(_action, _param, window_ref) -> None:
        window_ref.show_sidebar()

    def refresh(self) -> None:
        """いまのページを取り直す (Ctrl+R)。"""
        page = self.current_page()
        if page is not None and hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception as exc:  # ページの不具合で窓を止めない
                log(f"ページの更新に失敗: {exc}")
        self.sidebar.refresh()
        self.ctx.refresh_local_playlists()
        self.ctx.store.refresh_playlist()
        self.ctx.store.refresh_history()

    def reveal_current(self) -> None:
        self.navigate("nowplaying", reveal=True)

    def focus_search(self) -> None:
        self.navigate("search")
        page = self.current_page()
        if page is None:
            return
        if hasattr(page, "focus_search"):
            page.focus_search()
            return
        entry = next((w for w in _descendants(page, limit=14)
                      if isinstance(w, (Gtk.Entry, Gtk.SearchEntry))), None)
        if entry is not None:
            entry.grab_focus()

    # --- トースト ------------------------------------------------------------------

    def toast(self, text: str) -> Adw.Toast | None:
        """平文のトースト (曲名に & や < が入るのでマークアップにしない)。出した Adw.Toast を返す。

        長い文 (長い曲名とプレイリスト名を含むもの) で窓の幅いっぱいに広がらないよう、
        題は幅を抑えた自前のラベルにし、真ん中を省略する (「…に追加しました」の結びは残る)。"""
        text = " ".join((text or "").split())
        if not text:
            return None
        now = GLib.get_monotonic_time() / 1e6
        if self._toast_last is not None and self._toast_last[0] == text and now - self._toast_last[1] < 1.5:
            return None
        self._toast_last = (text, now)
        toast = Adw.Toast.new("")
        toast.set_use_markup(False)
        toast.set_title(text)
        label = Gtk.Label()
        label.set_text(text)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(TOAST_MAX_CHARS)
        label.set_width_chars(1)
        label.add_css_class("heading")
        label.add_css_class("music-toast-title")
        if len(text) > TOAST_MAX_CHARS // 2:
            label.set_tooltip_text(text)
        toast.set_custom_title(label)
        toast.set_timeout(4)
        self._toasts.add_toast(toast)
        return toast

    # --- 右パネル ------------------------------------------------------------------

    @property
    def panel(self) -> str:
        return self._panel

    def show_panel(self, name: str, save: bool = True) -> None:
        """右パネルを開く ("lyrics" / "queue") か閉じる ("")。"""
        name = name if name in PANELS else ""
        if name:
            self.panel_stack.set_visible_child_name(name)
        self._panel = name
        self.lyrics_panel.set_active(name == "lyrics")
        self.queue_panel.set_active(name == "queue")
        if self.panel_split.get_show_sidebar() != bool(name):
            self.panel_split.set_show_sidebar(bool(name))
        self.player_bar.set_panel(name)
        if save:
            self.ctx.state.right_panel = name
            self.ctx.state.save()
        self.emit("panel-changed", name)

    def toggle_panel(self, name: str) -> None:
        self.show_panel("" if self._panel == name else name)

    def _on_panel_split_shown(self, split, _pspec) -> None:
        # 重ねて出したパネルの外を押して閉じたとき
        if not split.get_show_sidebar() and self._panel:
            self.show_panel("")

    # --- フルスクリーンプレーヤー --------------------------------------------------------

    @property
    def fullscreen_shown(self) -> bool:
        return self.root_stack.get_visible_child_name() == "fullscreen"

    def show_fullscreen(self, on: bool) -> None:
        on = bool(on)
        if on == self.fullscreen_shown:
            return
        if on:
            player = self._ensure_fullscreen_player()
            if player is None:
                return
            self.root_stack.set_visible_child_name("fullscreen")
            self.add_css_class("fullscreen-player")
            if hasattr(player, "set_active"):
                player.set_active(True)
            if not self.is_fullscreen():
                self._we_fullscreened = True
                self.fullscreen()
            player.grab_focus()
        else:
            player = self._fullscreen_player
            if player is not None and hasattr(player, "set_active"):
                player.set_active(False)
            self.root_stack.set_visible_child_name("main")
            self.remove_css_class("fullscreen-player")
            if self._we_fullscreened:
                self._we_fullscreened = False
                self.unfullscreen()

    def toggle_fullscreen(self) -> None:
        self.show_fullscreen(not self.fullscreen_shown)

    def _ensure_fullscreen_player(self):
        if self._fullscreen_player is not None:
            return self._fullscreen_player
        try:
            from .fullscreen import FullscreenPlayer

            player = FullscreenPlayer(self.ctx)
        except Exception as exc:
            log(f"フルスクリーンプレーヤーを作れません: {exc}")
            self.toast("フルスクリーンプレーヤーを開けません")
            return None
        self._fullscreen_player = player
        self.root_stack.add_named(player, "fullscreen")
        return player

    # --- 接続 --------------------------------------------------------------------

    def _on_connection(self, _store) -> None:
        self._first_connection_seen = True
        self._on_connection_now()

    def _on_connection_now(self) -> None:
        store = self.ctx.store
        connected = store.connected
        # 最初の確認が終わるまでは「未接続」を出さない (起動直後のちらつき)
        show_offline = not connected and (self._first_connection_seen or self._probe_done())
        self.offline.set_visible(show_offline)
        self.banner.set_revealed(connected and store.api < 1)
        if show_offline:
            self.offline_socket.set_path(self.ctx.client.socket_path)

    def _probe_done(self) -> bool:
        return getattr(self.ctx.client, "_announced", None) is not None

    def _on_start_cliamp(self) -> None:
        """cliamp のサービスを起こす。失敗はトーストで知らせる。"""
        if self._starting is not None:
            return
        command = os.environ.get("CLIAMP_MUSIC_START_COMMAND")
        argv = shlex.split(command) if command else list(START_COMMAND)
        button = self.offline_state.button
        if button is not None:
            button.set_sensitive(False)
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as exc:
            self.toast(f"cliamp を起動できません: {exc.message}")
            if button is not None:
                button.set_sensitive(True)
            return
        self._starting = proc
        me = weakref.ref(self)

        def finished(process: Gio.Subprocess, result) -> None:
            this = me()
            try:
                _ok, _stdout, stderr = process.communicate_utf8_finish(result)
            except GLib.Error as exc:
                stderr = exc.message
            if this is None:
                return
            this._starting = None
            if this.offline_state.button is not None:
                this.offline_state.button.set_sensitive(True)
            if process.get_if_exited() and process.get_exit_status() == 0:
                this.toast("cliamp を起動しました")
                this.ctx.client.probe()
                this.ctx.client.poll_now()
            else:
                reason = (stderr or "").strip().splitlines()
                detail = reason[-1] if reason else f"終了コード {process.get_exit_status()}"
                this.toast(f"cliamp を起動できません: {detail}")

        proc.communicate_utf8_async(None, None, finished)

    # --- キー ----------------------------------------------------------------------

    @staticmethod
    def _on_key_pressed(controller, keyval, _keycode, state) -> bool:
        self = controller.get_widget()
        modifiers = state & Gtk.accelerator_get_default_mod_mask()
        if keyval == Gdk.KEY_Escape and not modifiers and self.fullscreen_shown:
            if inside_popover(self.get_focus()):
                return False  # 開いているメニューを先に閉じる
            self.show_fullscreen(False)
            return True
        # Space は再生/一時停止 (ミニプレーヤー・イコライザと同じ判断)
        return space_toggles(self, keyval, state)

    @staticmethod
    def _on_focus_changed(self, _pspec) -> None:
        # 文字の入力中は Ctrl+矢印 (次へ・前へ・10 秒送り) と Ctrl+. を入力欄に譲る。
        # 判断はアプリが「いま前にある窓」のフォーカスで行う (隠れたメインの窓の入力欄が
        # ミニプレーヤーのショートカットを止めないように)
        app = self.get_application()
        if app is not None and hasattr(app, "refresh_text_input"):
            app.refresh_text_input()

    # --- 閉じる --------------------------------------------------------------------

    @staticmethod
    def _on_close_request(self) -> bool:
        self.save_state()
        app = self.get_application()
        others = [w for w in (app.get_windows() if app is not None else [])
                  if w is not self and w.get_visible() and w.get_transient_for() is not self]
        if others:
            # ミニプレーヤーが開いている: 本体は隠すだけ (Ctrl+0 で戻る)
            self.set_visible(False)
            return True
        return False

    def save_state(self) -> None:
        state = self.ctx.state
        width, height = self.get_default_size()
        if width > 0 and height > 0 and not self.is_fullscreen():
            state.window_width = int(width)
            state.window_height = int(height)
        if not self.is_fullscreen():
            state.window_maximized = bool(self.is_maximized())
        state.right_panel = self._panel
        state.save()
