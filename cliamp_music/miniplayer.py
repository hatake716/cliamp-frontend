"""ミニプレーヤー (MiniPlayer、Shift+Ctrl+M)。

別の小さな窓。macOS 27 のミュージックと同じく 2 つの形を持つ:
- 正方形 (320x320): アートワークが全面。マウスを乗せると下からぼかしの帯が
  せり上がり、曲名・副題・「…」・再生位置・操作が出る。右上のガラスのカプセルに
  歌詞・次に再生 (メインの窓の右パネルを開く)・メインの窓。左上に閉じるボタン。
- 横長 (400x110): 小さな絵・曲名・副題・「…」・再生位置・操作。地は今の曲の絵を
  ぼかしたもの。
「…」の「大きなアートワークを隠す / 表示」で切り替え、GuiState の
"miniplayer_mode" ("square" / "compact") に覚える。どちらの形も大きさは固定。

窓は閉じても捨てずに隠し (hide-on-close)、present() で使い回す。隠れている間は
store のシグナルを受けない。Space はメインの窓と同じく再生/一時停止。正方形の操作の帯は
隠れている間フォーカスも受けない (見えない「…」に Space や Tab が届かないように)。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gsk, Gtk  # noqa: E402

from .fullscreen import (  # noqa: E402
    CoverArt,
    ArtBackdrop,
    Scrubber,
    TransportRow,
    _append_cover,
    _label,
    _rect,
    _point,
    _set_text,
    _stops,
    _toggle_class,
    app_while_visible,
    blurred_texture,
    time_texts,
)
from .widgets import CircleButton, GlassCapsule, install_space_toggle  # noqa: E402

__all__ = ["MiniPlayer", "MODES", "SIZES"]

MODES = ("square", "compact")
SIZES = {"square": (320, 320), "compact": (400, 110)}
PANEL_OPENERS = ("show_panel", "show_right_panel", "set_right_panel", "open_panel")


# --------------------------------------------------------------------------
# 正方形の絵 (下にぼかしの帯)


class _MiniArt(CoverArt):
    """全面の絵。band (0〜1) に応じて、下の BAND 分を絵のぼかしと暗さで覆う。"""

    BAND = 150

    def __init__(self, size: int):
        super().__init__(size, radius=0, shadow=False, outline=False)
        self._band = 0.0
        self._blurred: Gdk.Texture | None = None
        self._blur_source: Gdk.Texture | None = None
        self._blur_idle = 0
        self.connect("realize", lambda *_: self._schedule_blur())
        self.connect("unrealize", lambda *_: self._drop_blur())

    def set_band(self, value: float) -> None:
        value = min(1.0, max(0.0, float(value)))
        if abs(value - self._band) > 1e-3:
            self._band = value
            self.queue_draw()

    def _set_texture(self, texture, fade: bool) -> None:
        CoverArt._set_texture(self, texture, fade)
        self._schedule_blur()

    def _drop_blur(self) -> None:
        self._blurred = None
        self._blur_source = None
        if self._blur_idle:
            GLib.source_remove(self._blur_idle)
            self._blur_idle = 0

    def _schedule_blur(self) -> None:
        if self._blur_idle or not self.get_realized() or self._texture is self._blur_source:
            return
        self._blur_idle = GLib.idle_add(self._make_blur)

    def _make_blur(self) -> bool:
        self._blur_idle = 0
        source = self._texture
        self._blur_source = source
        # 局の小さな favicon は色の地に置くので、ぼかしは絵ではなく暗さだけにする
        if source is None or (self._station and self._is_small(source, max(1, self.get_width()))):
            self._blurred = None
        else:
            self._blurred = blurred_texture(self, source, size=160, radius=9.0, saturation=1.2)
        self.queue_draw()
        return GLib.SOURCE_REMOVE

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        CoverArt.do_snapshot(self, snapshot)
        width, height = self.get_width(), self.get_height()
        if self._band <= 0.005 or width <= 0 or height <= 0:
            return
        band = min(float(height), self.BAND)
        top = height - band
        area = _rect(0, top, width, band)
        snapshot.push_opacity(self._band)
        if self._blurred is not None:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            snapshot.append_linear_gradient(area, _point(0, top), _point(0, height), _stops(
                (0.0, "rgba(0,0,0,0)"), (0.42, "rgba(0,0,0,0.9)"), (0.62, "rgba(0,0,0,1)"),
                (1.0, "rgba(0,0,0,1)")))
            snapshot.pop()
            snapshot.push_clip(area)
            _append_cover(snapshot, self._blurred, 0, 0, width, height)
            snapshot.pop()
            snapshot.pop()
        snapshot.append_linear_gradient(area, _point(0, top), _point(0, height), _stops(
            (0.0, "rgba(0,0,0,0)"), (0.5, "rgba(0,0,0,0.26)"), (1.0, "rgba(0,0,0,0.52)")))
        snapshot.pop()

    def do_dispose(self) -> None:
        if getattr(self, "_blur_idle", 0):
            GLib.source_remove(self._blur_idle)
            self._blur_idle = 0
        CoverArt.do_dispose(self)


# --------------------------------------------------------------------------
# 窓の外への操作


def _show_main(ctx, panel: str | None = None) -> None:
    """メインの窓を前に出す。panel ("lyrics" / "queue") があれば右パネルも開く。

    パネルを開くときは、フルスクリーンプレーヤーからは出る (パネルはその下の画面にあるので、
    出ないと何も変わらないように見える。ショートカットの app.show-lyrics と同じ)。"""
    window = ctx.window
    if window is None:
        return
    if panel:
        if getattr(window, "fullscreen_shown", False) and hasattr(window, "show_fullscreen"):
            window.show_fullscreen(False)
        for name in PANEL_OPENERS:
            opener = getattr(window, name, None)
            if callable(opener):
                opener(panel)
                break
        else:
            # 窓が開き方を持っていなければ保存値だけ変える (次に窓を組み立てるときに使われる)
            if ctx.state is not None:
                ctx.state.right_panel = panel
    if hasattr(window, "present"):
        window.present()


def _on_panel(_button, ctx, panel: str) -> None:
    _show_main(ctx, panel)


def _more_button() -> Gtk.MenuButton:
    button = Gtk.MenuButton()
    button.set_icon_name("music-more-symbolic")
    button.set_tooltip_text("その他")
    button.add_css_class("music-mini-more")
    button.set_valign(Gtk.Align.CENTER)
    return button


# --------------------------------------------------------------------------
# ミニプレーヤー


class MiniPlayer(Adw.Window):
    """ミニプレーヤーの窓 (css クラス music, music-mini)。

    `MiniPlayer(ctx)`: application は ctx.app (見せている間だけ。app_while_visible)。
    閉じると隠れる (hide-on-close)。`present()` で再び出す。
    - `set_mode("square" | "compact")` / `toggle_compact()` / `mode`
    - `set_hover(bool, force=False)`: 正方形の操作の帯を出す・隠す。force=True なら
      マウスが離れても出したまま (撮影用)。
    """

    HIDE_DELAY_MS = 450
    REVEAL_MS = 220
    TICK_MS = 250

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.add_css_class("music")
        self.add_css_class("music-mini")
        self.set_title("ミニプレーヤー")
        self.set_resizable(False)
        self.set_hide_on_close(True)
        app_while_visible(self, ctx.app)
        mode = ctx.state.get("miniplayer_mode", "square") if ctx.state is not None else "square"
        self._mode = mode if mode in MODES else "square"
        self._handlers: list[int] = []
        self._tick_id = 0
        self._hover = False
        self._forced = False
        self._reveal = 0.0
        self._reveal_anim = None
        self._hide_timer = 0
        self._menus_open = 0

        self._stack = Gtk.Stack()
        self._stack.set_hhomogeneous(False)
        self._stack.set_vhomogeneous(False)
        self._stack.set_interpolate_size(False)
        self._stack.add_named(self._build_square(), "square")
        self._stack.add_named(self._build_compact(), "compact")
        handle = Gtk.WindowHandle()
        handle.set_child(self._stack)
        self.set_content(handle)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        handle.add_controller(motion)

        self._actions = Gio.SimpleActionGroup()
        for name, handler in (("toggle-compact", self._act_toggle), ("main-window", self._act_main),
                              ("fullscreen", self._act_fullscreen), ("close", self._act_close)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self._actions.add_action(action)
        self.insert_action_group("mini", self._actions)
        self._menu_mode = Gio.Menu()
        menu = Gio.Menu()
        menu.append_section(None, self._menu_mode)
        others = Gio.Menu()
        others.append("フルスクリーンプレーヤー", "mini.fullscreen")
        others.append("メインの窓を表示", "mini.main-window")
        menu.append_section(None, others)
        closing = Gio.Menu()
        closing.append("ミニプレーヤーを閉じる", "mini.close")
        menu.append_section(None, closing)
        for button in (self.square_more, self.compact_more):
            button.set_menu_model(menu)
            button.connect("notify::active", self._on_menu_active)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        install_space_toggle(self)
        self._apply_mode(self._mode)
        self._apply_reveal(0.0)
        self._sync_status()

    # 組み立て -----------------------------------------------------------------

    def _build_square(self) -> Gtk.Widget:
        width, height = SIZES["square"]
        overlay = Gtk.Overlay()
        overlay.add_css_class("music-mini-square")
        overlay.set_size_request(width, height)
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.square_art = _MiniArt(width)
        overlay.set_child(self.square_art)

        controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        controls.add_css_class("music-mini-controls")
        controls.set_valign(Gtk.Align.END)
        controls.set_margin_start(16)
        controls.set_margin_end(16)
        controls.set_margin_bottom(6)
        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        self.square_title = _label("", "music-mini-title")
        self.square_subtitle = _label("", "music-mini-subtitle")
        texts.append(self.square_title)
        texts.append(self.square_subtitle)
        info.append(texts)
        self.square_more = _more_button()
        info.append(self.square_more)
        controls.append(info)
        self.square_scrubber = self._scrubber(height=14)
        self.square_scrubber.set_margin_top(6)
        controls.append(self.square_scrubber)
        self.square_times, self.square_elapsed, self.square_badge, self.square_remaining = self._times()
        controls.append(self.square_times)
        self.square_transport = TransportRow(self.ctx.store, "medium")
        self.square_transport.set_margin_top(2)
        controls.append(self.square_transport)
        overlay.add_overlay(controls)
        self._square_controls = controls

        top = GlassCapsule()
        top.add_css_class("music-mini-glass")
        top.set_halign(Gtk.Align.END)
        top.set_valign(Gtk.Align.START)
        top.set_margin_top(10)
        top.set_margin_end(10)
        for icon, tooltip, panel in (("music-lyrics-symbolic", "歌詞", "lyrics"),
                                     ("music-queue-symbolic", "次に再生", "queue")):
            button = CircleButton(icon, tooltip, size=28, flat=True)
            button.connect("clicked", _on_panel, self.ctx, panel)
            top.append(button)
        self.main_button = CircleButton("music-miniplayer-symbolic", "メインの窓", size=28, flat=True)
        self.main_button.connect("clicked", self._on_main)
        top.append(self.main_button)
        overlay.add_overlay(top)
        self._square_top = top

        controls_left = Gtk.WindowControls(side=Gtk.PackType.START)
        controls_left.set_decoration_layout("close:")
        controls_left.add_css_class("music-mini-window-controls")
        controls_left.set_halign(Gtk.Align.START)
        controls_left.set_valign(Gtk.Align.START)
        controls_left.set_margin_top(12)
        controls_left.set_margin_start(12)
        overlay.add_overlay(controls_left)
        self._square_close = controls_left
        return overlay

    def _build_compact(self) -> Gtk.Widget:
        width, height = SIZES["compact"]
        overlay = Gtk.Overlay()
        overlay.add_css_class("music-mini-compact")
        overlay.set_size_request(width, height)
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.compact_backdrop = ArtBackdrop(dim=((0.0, 0.38), (1.0, 0.56)), radius=10.0,
                                            saturation=1.25, brightness=0.9)
        overlay.set_child(self.compact_backdrop)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.set_margin_top(10)
        body.set_margin_bottom(6)
        body.set_margin_start(12)
        body.set_margin_end(12)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.compact_art = CoverArt(38, radius=5, shadow=False)
        self.compact_art.set_valign(Gtk.Align.CENTER)
        top.append(self.compact_art)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        self.compact_title = _label("", "music-mini-compact-title")
        self.compact_subtitle = _label("", "music-mini-compact-subtitle")
        texts.append(self.compact_title)
        texts.append(self.compact_subtitle)
        top.append(texts)
        self.compact_more = _more_button()
        top.append(self.compact_more)
        body.append(top)
        # 再生位置の線。ライブ配信のときは同じ場所に「ライブ」の印を出す
        self.compact_progress = Gtk.Stack()
        self.compact_progress.set_margin_top(5)
        self.compact_scrubber = self._scrubber(height=12)
        self.compact_progress.add_named(self.compact_scrubber, "seek")
        live = Gtk.CenterBox()
        self.compact_live = _label("ライブ", "music-mini-badge", "live", xalign=0.5, ellipsize=False)
        live.set_center_widget(self.compact_live)
        self.compact_progress.add_named(live, "live")
        body.append(self.compact_progress)
        # 経過と残りは操作の列の両端の上に重ねる (Apple と同じ並び。110px に収めるため)
        row = Gtk.Overlay()
        self.compact_transport = TransportRow(self.ctx.store, "small")
        self.compact_transport.set_margin_top(9)
        row.set_child(self.compact_transport)
        self.compact_times, self.compact_elapsed, self.compact_badge, self.compact_remaining = self._times()
        self.compact_times.set_valign(Gtk.Align.START)
        self.compact_times.set_can_target(False)
        self.compact_times.add_css_class("compact")
        row.add_overlay(self.compact_times)
        body.append(row)
        overlay.add_overlay(body)
        return overlay

    def _scrubber(self, height: int) -> Scrubber:
        scrubber = Scrubber(thickness=4, hover_thickness=6, height=height, label="再生位置")
        scrubber.add_css_class("position")
        scrubber.on_scrub = self._on_scrub
        scrubber.on_seek = self._on_seek
        scrubber.on_step = self._on_step
        return scrubber

    @staticmethod
    def _times() -> tuple[Gtk.CenterBox, Gtk.Label, Gtk.Label, Gtk.Label]:
        times = Gtk.CenterBox()
        times.add_css_class("music-mini-times")
        elapsed = _label("", "music-mini-time", ellipsize=False)
        remaining = _label("", "music-mini-time", xalign=1.0, ellipsize=False)
        badge = _label("", "music-mini-badge", xalign=0.5, ellipsize=False)
        badge.set_visible(False)
        times.set_start_widget(elapsed)
        times.set_center_widget(badge)
        times.set_end_widget(remaining)
        return times, elapsed, badge, remaining

    # 形 -----------------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode は square か compact: {mode!r}")
        if mode == self._mode and self._stack.get_visible_child_name() == mode:
            return
        self._apply_mode(mode)
        state = self.ctx.state
        if state is not None:
            state.set("miniplayer_mode", mode)
            state.save()

    def toggle_compact(self) -> None:
        self.set_mode("square" if self._mode == "compact" else "compact")

    def _apply_mode(self, mode: str) -> None:
        self._mode = mode
        self._stack.set_visible_child_name(mode)
        width, height = SIZES[mode]
        # Adw.Window は大きさの下限 (360x200) を持つので、形ごとの大きさを下限として明示する
        self.set_size_request(width, height)
        self.set_default_size(width, height)
        _toggle_class(self, "compact", mode == "compact")
        self._menu_mode.remove_all()
        self._menu_mode.append("大きなアートワークを表示" if mode == "compact" else "大きなアートワークを隠す",
                               "mini.toggle-compact")
        self._update_ticking()

    # 帯を出す・隠す -----------------------------------------------------------------

    def set_hover(self, hover: bool, force: bool = False) -> None:
        self._forced = bool(force and hover)
        self._set_revealed(bool(hover), animate=not force)

    def _set_revealed(self, revealed: bool, animate: bool = True) -> None:
        if self._hide_timer:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = 0
        self._hover = revealed
        if revealed:
            self._update_times()  # 隠れている間は時計を止めているので、出す前に合わせる
        target = 1.0 if revealed else 0.0
        if not animate or not self.get_mapped():
            if self._reveal_anim is not None:
                self._reveal_anim.pause()
            self._apply_reveal(target)
        else:
            if self._reveal_anim is None:
                anim_target = Adw.CallbackAnimationTarget.new(self._apply_reveal)
                self._reveal_anim = Adw.TimedAnimation.new(self, self._reveal, target, self.REVEAL_MS, anim_target)
                self._reveal_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
            else:
                self._reveal_anim.pause()
                self._reveal_anim.set_value_from(self._reveal)
                self._reveal_anim.set_value_to(target)
            self._reveal_anim.play()
        self._update_ticking()

    def _apply_reveal(self, value: float) -> None:
        self._reveal = value
        self.square_art.set_band(value)
        shown = value > 0.5
        focus = self.get_focus()
        for widget in (self._square_controls, self._square_top, self._square_close):
            widget.set_opacity(value)
            widget.set_can_target(shown)
            # 隠れている帯はフォーカスも受けない (見えない「…」に Space や Tab が届かないように)
            widget.set_can_focus(shown)
            if not shown and focus is not None and (focus is widget or focus.is_ancestor(widget)):
                self.set_focus(None)

    def _on_enter(self, *_args) -> None:
        self._set_revealed(True)

    def _on_leave(self, *_args) -> None:
        if self._forced or self._menus_open:
            return
        if self._hide_timer:
            GLib.source_remove(self._hide_timer)
        self._hide_timer = GLib.timeout_add(self.HIDE_DELAY_MS, self._hide_later)

    def _hide_later(self) -> bool:
        self._hide_timer = 0
        if not self._forced and not self._menus_open:
            self._set_revealed(False)
        return GLib.SOURCE_REMOVE

    def _on_menu_active(self, button: Gtk.MenuButton, _pspec) -> None:
        self._menus_open = max(0, self._menus_open + (1 if button.get_active() else -1))
        if button.get_active() and self._mode == "square" and self._reveal < 0.5:
            # メニューが見えない「…」から出ないよう、開いたら帯を出しておく
            self._set_revealed(True, animate=False)

    # store から -----------------------------------------------------------------

    def _on_map(self, *_args) -> None:
        store = self.ctx.store
        if not self._handlers:
            for name, handler in (("status-changed", self._on_status), ("track-changed", self._on_track),
                                  ("state-changed", self._on_state), ("connection-changed", self._on_track)):
                self._handlers.append(store.connect(name, handler))
        self._on_track(store)
        self._update_ticking()

    def _on_unmap(self, *_args) -> None:
        store = self.ctx.store
        for handler in self._handlers:
            store.disconnect(handler)
        self._handlers = []
        if not self._forced:
            self._set_revealed(False, animate=False)
        self._update_ticking()

    def _on_track(self, store) -> None:
        track = store.current_track()
        loader = self.ctx.artwork
        self.square_art.show(loader, track)
        self.compact_art.show(loader, track)
        if loader is not None:
            if track is None:
                self.compact_backdrop.set_texture(loader.placeholder("cliamp-music:none", 128))
            else:
                self._request_backdrop(track)
        self._sync_status()

    def _request_backdrop(self, track) -> None:
        backdrop = self.compact_backdrop
        path = track.path

        def done(texture) -> None:
            current = self.ctx.store.current_track()
            if current is not None and current.path == path:
                backdrop.set_texture(texture)

        self.ctx.artwork.request(track, 192, done)

    def _on_status(self, _store) -> None:
        self._sync_status()
        self._update_ticking()

    def _on_state(self, _store) -> None:
        self._update_ticking()

    def _sync_status(self) -> None:
        store = self.ctx.store
        status = store.status
        track = status.track
        if status.state == "offline":
            title, subtitle = "cliamp に接続できません", ""
        elif track is None:
            title, subtitle = "再生していません", ""
        else:
            title, subtitle = status.display_title, status.display_subtitle
        for label, text in ((self.square_title, title), (self.square_subtitle, subtitle),
                            (self.compact_title, title), (self.compact_subtitle, subtitle)):
            _set_text(label, text)
        self.square_subtitle.set_visible(bool(subtitle))
        self.compact_subtitle.set_visible(bool(subtitle))
        self.set_title(f"ミニプレーヤー — {title}" if track is not None else "ミニプレーヤー")
        self.square_transport.update(status)
        self.compact_transport.update(status)
        live = status.is_live
        seekable = store.can_seek()
        for scrubber in (self.square_scrubber, self.compact_scrubber):
            scrubber.set_interactive(seekable)
        self.square_scrubber.set_visible(not live)
        self.compact_progress.set_visible_child_name("live" if live else "seek")
        # 読み込み中は副題が「読み込み中…」になるので、印は「ライブ」だけ。
        # 横長では「ライブ」を線の場所に出す (時刻の列の中央は操作の列と重なるため)
        _set_text(self.square_badge, "ライブ" if live else "")
        self.square_badge.set_visible(live)
        _toggle_class(self.square_badge, "live", live)
        self.compact_badge.set_visible(False)
        self._update_times()

    def _update_times(self) -> None:
        store = self.ctx.store
        status = store.status
        pairs = ((self.square_scrubber, self.square_elapsed, self.square_remaining),
                 (self.compact_scrubber, self.compact_elapsed, self.compact_remaining))
        if status.track is None or status.is_live:
            for scrubber, elapsed, remaining in pairs:
                _set_text(elapsed, "")
                _set_text(remaining, "")
                scrubber.set_fraction(0.0)
            return
        position = store.position_now()
        elapsed_text, remaining_text = time_texts(position, status.duration)
        fraction = position / status.duration if status.duration > 0 else 0.0
        for scrubber, elapsed, remaining in pairs:
            if scrubber.dragging:
                continue
            _set_text(elapsed, elapsed_text)
            _set_text(remaining, remaining_text)
            scrubber.set_fraction(fraction)

    # 時計 -----------------------------------------------------------------

    def _update_ticking(self) -> None:
        status = self.ctx.store.status
        visible = self._mode == "compact" or self._reveal > 0 or self._hover
        want = (bool(self._handlers) and self.get_mapped() and visible and status.state == "playing"
                and status.track is not None)
        if want and not self._tick_id:
            self._tick_id = GLib.timeout_add(self.TICK_MS, self._on_tick)
        elif not want and self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self) -> bool:
        self._update_times()
        return GLib.SOURCE_CONTINUE

    # シーク -----------------------------------------------------------------

    def _on_scrub(self, fraction: float) -> None:
        duration = self.ctx.store.status.duration
        elapsed, remaining = time_texts(fraction * duration, duration)
        for label, text in ((self.square_elapsed, elapsed), (self.compact_elapsed, elapsed),
                            (self.square_remaining, remaining), (self.compact_remaining, remaining)):
            _set_text(label, text)

    def _on_seek(self, fraction: float) -> None:
        store = self.ctx.store
        if store.status.duration > 0:
            store.seek_to(fraction * store.status.duration)
        self._update_times()

    def _on_step(self, direction: int) -> None:
        self.ctx.store.seek_by(5.0 * direction)
        self._update_times()

    # 操作 -----------------------------------------------------------------

    def _on_main(self, *_args) -> None:
        _show_main(self.ctx)
        self.close()

    def _act_toggle(self, *_args) -> None:
        self.toggle_compact()

    def _act_main(self, *_args) -> None:
        self._on_main()

    def _act_fullscreen(self, *_args) -> None:
        window = self.ctx.window
        if window is None:
            return
        _show_main(self.ctx)
        if hasattr(window, "show_fullscreen"):
            window.show_fullscreen(True)
        self.close()

    def _act_close(self, *_args) -> None:
        self.close()

    def do_dispose(self) -> None:
        for name in ("_tick_id", "_hide_timer"):
            source = getattr(self, name, 0)
            if source:
                GLib.source_remove(source)
                setattr(self, name, 0)
        anim = getattr(self, "_reveal_anim", None)
        if anim is not None:
            anim.reset()
            self._reveal_anim = None
        Adw.Window.do_dispose(self)
