"""再生バー: 画面下に浮かぶガラスのカプセル (高さ 56、角は完全な丸)。

左から:
1. シャッフル・前へ・再生/一時停止・次へ・リピート (1 曲のときは「1」の印の記号)
2. アートワーク 36px (角 5)。押すとメニュー「ミニプレーヤー」「フルスクリーンプレーヤー」
3. 曲名 (13px/600) と「アーティスト — アルバム」(12px)。ラジオは ICY の曲名と局名、
   読み込み中は副題が「読み込み中…」(Status.display_title / display_subtitle)
4. 2 と 3 の下に細い再生位置の線 (3px、ホバーで 5px とつまみ、経過と残りの時間)。
   ドラッグ・クリックで seek_to。ライブ配信では線を出さず「ライブ」の印
5. 「…」: 再生速度 ▸ / イコライザ… / 再生中のリストを表示 / リンクをコピー / ブラウザで開く
6. 歌詞・次に再生 (右パネルの開け閉め)、出力先 (device list)、音量 (横のスライダー)

狭い幅では 6 の出力先と 5 を先に隠し、次に 3 の副題を隠す (自分の幅で判断する
Adw.BreakpointBin)。cliamp に繋がっていないときは操作を止めて薄く見せる。

`PlayerBar(ctx)`:
- `set_panel(name)`: 窓の右パネルの状態 ("" / "lyrics" / "queue") を歌詞と次に再生の
  ボタンに映す (窓が呼ぶ)。ボタンを押すと ctx.window.toggle_panel(name) を呼ぶ。
- 再生位置は store.position_now() を frame clock で読み、線が 1 画素動くときだけ描き直す。
  表示されていて再生中のときだけ回す。
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .pages import Bindings  # noqa: E402
from .protocol import db_to_linear, format_time, linear_to_db  # noqa: E402
from .widgets import Artwork, CircleButton, ToggleCircle  # noqa: E402

SPEEDS = ("0.5", "0.75", "1.0", "1.25", "1.5", "2.0")
BAR_HEIGHT = 56
ART_SIZE = 36
# 自分の操作の後、届いた状態で値を戻さない時間 (秒)
USER_HOLD = 1.2


def _label(text: str = "", css: str | tuple[str, ...] = (), xalign: float = 0.0,
           ellipsize: bool = True) -> Gtk.Label:
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    if ellipsize:
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_width_chars(1)
        label.set_max_width_chars(1)
    for cls in (css,) if isinstance(css, str) else css:
        label.add_css_class(cls)
    return label


def _speed_key(speed: float) -> str:
    """いまの速さに最も近いメニューの項目。"""
    return min(SPEEDS, key=lambda s: abs(float(s) - (speed or 1.0)))


def _speed_label(key: str) -> str:
    return f"{float(key):g}×"


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(spec)
    return color


# ---------------------------------------------------------------------------
# 再生位置の線


class ProgressLine(Gtk.Widget):
    """細い再生位置の線。ホバーで太くなり、つまみが出る。ドラッグ・クリックで位置を選ぶ。

    シグナル:
      "seek" (double 秒): 放したとき (または押しただけのとき) に選んだ位置
      "preview" (double 秒): ドラッグ中の位置 (-1 で終わり)
      "hover" (bool): 乗せた / 離した
    線の色は CSS の color (塗った部分)。地の線はその色を薄くしたもの。
    """

    __gtype_name__ = "CliampMusicProgressLine"
    __gsignals__ = {
        "seek": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "preview": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        "hover": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    HIT_HEIGHT = 10
    THIN = 3.0
    THICK = 5.0
    KNOB = 11.0

    def __init__(self):
        super().__init__()
        self.add_css_class("music-progress")
        self._position = 0.0
        self._duration = 0.0
        self._seekable = False
        self._hover = False
        self._dragging = False
        self._drag_fraction = 0.0
        self._drag_start_x = 0.0
        self._drawn_px = -1
        self._thickness = self.THIN
        self._anim: Adw.TimedAnimation | None = None
        self.set_focusable(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["再生位置"])

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", ProgressLine._on_enter)
        motion.connect("leave", ProgressLine._on_leave)
        self.add_controller(motion)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", ProgressLine._on_drag_begin)
        drag.connect("drag-update", ProgressLine._on_drag_update)
        drag.connect("drag-end", ProgressLine._on_drag_end)
        self.add_controller(drag)

    # 値 --------------------------------------------------------------------

    @property
    def dragging(self) -> bool:
        return self._dragging

    @property
    def hovering(self) -> bool:
        return self._hover

    @property
    def seekable(self) -> bool:
        return self._seekable

    def set_seekable(self, seekable: bool) -> None:
        self._seekable = bool(seekable)
        self.set_cursor_from_name("pointer" if seekable else None)

    def set_position(self, position: float, duration: float) -> None:
        """位置と長さ。線が 1 画素以上動くときだけ描き直す。ドラッグ中は無視する。"""
        self._position = max(0.0, float(position))
        self._duration = max(0.0, float(duration))
        if self._dragging:
            return
        px = self._fill_px()
        if px != self._drawn_px:
            self.queue_draw()

    def _fraction(self) -> float:
        if self._dragging:
            return self._drag_fraction
        if self._duration <= 0:
            return 0.0
        return min(1.0, self._position / self._duration)

    def _fill_px(self) -> int:
        scale = max(1, self.get_scale_factor())
        return int(round(self._fraction() * max(0, self.get_width()) * scale))

    # 形 --------------------------------------------------------------------

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 24, 24, -1, -1
        return self.HIT_HEIGHT, self.HIT_HEIGHT, -1, -1

    def _set_emphasis(self, on: bool) -> None:
        target = self.THICK if on else self.THIN
        if abs(self._thickness - target) < 0.01:
            return
        settings = self.get_settings()
        if not self.get_mapped() or (settings is not None and not settings.get_property("gtk-enable-animations")):
            self._thickness = target
            self.queue_draw()
            return
        if self._anim is None:
            anim_target = Adw.CallbackAnimationTarget.new(self._set_thickness)
            self._anim = Adw.TimedAnimation.new(self, self.THIN, self.THICK, 120, anim_target)
            self._anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._anim.set_value_from(self._thickness)
        self._anim.set_value_to(target)
        self._anim.play()

    def _set_thickness(self, value: float) -> None:
        self._thickness = value
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        color = self.get_color()
        track = Gdk.RGBA()
        track.red, track.green, track.blue = color.red, color.green, color.blue
        track.alpha = color.alpha * 0.26
        thickness = self._thickness
        y = (height - thickness) / 2
        radius = thickness / 2
        whole = Graphene.Rect().init(0, y, width, thickness)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(whole, radius)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_color(track, whole)
        fill_w = self._fraction() * width
        if fill_w > 0:
            snapshot.append_color(color, Graphene.Rect().init(0, y, fill_w, thickness))
        snapshot.pop()
        self._drawn_px = self._fill_px()
        if (self._hover or self._dragging) and self._seekable:
            knob = self.KNOB
            cx = min(max(fill_w, knob / 2), width - knob / 2)
            rect = Graphene.Rect().init(cx - knob / 2, height / 2 - knob / 2, knob, knob)
            circle = Gsk.RoundedRect()
            circle.init_from_rect(rect, knob / 2)
            snapshot.append_outset_shadow(circle, _rgba("rgba(0,0,0,0.35)"), 0, 1, 0, 2)
            snapshot.push_rounded_clip(circle)
            snapshot.append_color(_rgba("#ffffff"), rect)
            snapshot.pop()

    # 操作 ------------------------------------------------------------------

    @staticmethod
    def _on_enter(controller, _x, _y) -> None:
        self = controller.get_widget()
        self._hover = True
        self._set_emphasis(self._seekable)
        self.emit("hover", True)

    @staticmethod
    def _on_leave(controller) -> None:
        self = controller.get_widget()
        self._hover = False
        if not self._dragging:
            self._set_emphasis(False)
        self.emit("hover", False)

    def _fraction_at(self, x: float) -> float:
        width = self.get_width()
        return min(1.0, max(0.0, x / width)) if width > 0 else 0.0

    @staticmethod
    def _on_drag_begin(gesture, x, _y) -> None:
        self = gesture.get_widget()
        if not self._seekable or self._duration <= 0:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._dragging = True
        self._drag_start_x = x
        self._drag_fraction = self._fraction_at(x)
        self._set_emphasis(True)
        self.queue_draw()
        self.emit("preview", self._drag_fraction * self._duration)

    @staticmethod
    def _on_drag_update(gesture, dx, _dy) -> None:
        self = gesture.get_widget()
        if not self._dragging:
            return
        self._drag_fraction = self._fraction_at(self._drag_start_x + dx)
        self.queue_draw()
        self.emit("preview", self._drag_fraction * self._duration)

    @staticmethod
    def _on_drag_end(gesture, dx, _dy) -> None:
        self = gesture.get_widget()
        if not self._dragging:
            return
        self._drag_fraction = self._fraction_at(self._drag_start_x + dx)
        target = self._drag_fraction * self._duration
        self._dragging = False
        self._position = target
        if not self._hover:
            self._set_emphasis(False)
        self.queue_draw()
        self.emit("preview", -1.0)
        self.emit("seek", target)

    def do_unmap(self) -> None:
        if getattr(self, "_anim", None) is not None:
            self._anim.skip()
        Gtk.Widget.do_unmap(self)


# ---------------------------------------------------------------------------
# 再生バー


class PlayerBar(Adw.BreakpointBin):
    """再生バー。`PlayerBar(ctx)`。"""

    __gtype_name__ = "CliampMusicPlayerBar"

    # 自分の幅で隠す物を決める (狭い順に後から足すほうが勝つ)
    WIDE_ENOUGH = 620    # これより狭いと 出力先と「…」を隠す
    NARROW = 480         # これより狭いと 副題も隠す
    TINY = 380           # これより狭いと 歌詞・次に再生・シャッフル・リピートも隠す

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.add_css_class("music-player-bar-bin")
        self.set_size_request(300, BAR_HEIGHT)
        self.set_valign(Gtk.Align.END)
        self._panel = ""
        self._syncing = False
        self._tick_id = 0
        self._shown_second = -1
        self._preview = -1.0
        self._volume_user_at = 0.0
        self._volume_syncing = False
        self._track_sig = None
        self._hover_times = False
        self._play_key = None
        self._repeat_shown = None

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        bar.add_css_class("music-player-bar")
        bar.add_css_class("music-glass")
        bar.set_size_request(-1, BAR_HEIGHT)
        self.bar = bar

        # 1. 再生の操作 ------------------------------------------------------
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
        transport.add_css_class("music-bar-transport")
        transport.set_valign(Gtk.Align.CENTER)
        self.shuffle = ToggleCircle("music-shuffle-symbolic", "シャッフル", size=26)
        self.shuffle.image.set_pixel_size(13)
        self.prev = CircleButton("music-previous-symbolic", "前へ", size=32, flat=True)
        self.prev.image.set_pixel_size(18)
        self.play = CircleButton("music-play-symbolic", "再生", size=36, flat=True)
        self.play.image.set_pixel_size(22)
        self.play.add_css_class("music-bar-play")
        self.next = CircleButton("music-next-symbolic", "次へ", size=32, flat=True)
        self.next.image.set_pixel_size(18)
        self.repeat = ToggleCircle("music-repeat-symbolic", "リピート", size=26)
        self.repeat.image.set_pixel_size(13)
        for button in (self.shuffle, self.prev, self.play, self.next, self.repeat):
            button.add_css_class("music-bar-button")
            transport.append(button)
        self._shuffle_id = self.shuffle.connect("toggled", PlayerBar._on_shuffle_toggled)
        self._repeat_id = self.repeat.connect("clicked", PlayerBar._on_repeat_clicked)
        self.prev.connect("clicked", PlayerBar._on_prev)
        self.play.connect("clicked", PlayerBar._on_play)
        self.next.connect("clicked", PlayerBar._on_next)
        bar.append(transport)

        # 2〜5. 再生中の曲 -----------------------------------------------------
        self.now_stack = Gtk.Stack()
        self.now_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.now_stack.set_transition_duration(160)
        self.now_stack.set_hexpand(True)
        self.now_stack.add_css_class("music-bar-now")
        bar.append(self.now_stack)

        now = Gtk.Overlay()
        now.set_hexpand(True)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("music-bar-now-row")
        row.set_valign(Gtk.Align.CENTER)
        row.set_margin_start(12)
        row.set_margin_end(4)

        self.art_button = Gtk.MenuButton()
        self.art_button.add_css_class("music-bar-art")
        self.art_button.set_direction(Gtk.ArrowType.UP)
        self.art_button.set_tooltip_text("ミニプレーヤー / フルスクリーンプレーヤー")
        art_overlay = Gtk.Overlay()
        self.art = Artwork(ART_SIZE, ART_SIZE, radius=5)
        art_overlay.set_child(self.art)
        expand = Gtk.Image.new_from_icon_name("music-fullscreen-symbolic")
        expand.set_pixel_size(14)
        expand.add_css_class("music-bar-art-expand")
        expand.set_can_target(False)
        art_overlay.add_overlay(expand)
        self.art_button.set_child(art_overlay)
        art_menu = Gio.Menu()
        art_menu.append("ミニプレーヤー", "app.miniplayer")
        art_menu.append("フルスクリーンプレーヤー", "app.fullscreen-player")
        self.art_button.set_menu_model(art_menu)
        self.art_button.update_property([Gtk.AccessibleProperty.LABEL], ["アートワーク"])
        row.append(self.art_button)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        texts.set_valign(Gtk.Align.CENTER)
        texts.set_hexpand(True)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        # 自然幅は題の全長 (★ や「ライブ」を題のすぐ後ろに置き、狭いときだけ省略する)
        self.title_label = _label("", "music-bar-title")
        self.title_label.set_width_chars(-1)
        self.title_label.set_max_width_chars(-1)
        self.title_label.set_hexpand(False)
        title_row.append(self.title_label)
        self.star = Gtk.Image.new_from_icon_name("music-star-symbolic")
        self.star.set_pixel_size(10)
        self.star.add_css_class("music-bar-star")
        self.star.set_valign(Gtk.Align.CENTER)
        self.star.set_visible(False)
        title_row.append(self.star)
        self.live_badge = _label("ライブ", "music-bar-live", ellipsize=False)
        self.live_badge.set_valign(Gtk.Align.CENTER)
        self.live_badge.set_visible(False)
        title_row.append(self.live_badge)
        texts.append(title_row)

        self.sub_stack = Gtk.Stack()
        self.sub_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.sub_stack.set_transition_duration(140)
        self.sub_stack.set_hhomogeneous(False)
        self.sub_stack.set_vhomogeneous(True)
        self.subtitle_label = _label("", "music-bar-subtitle")
        self.sub_stack.add_named(self.subtitle_label, "subtitle")
        times = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.elapsed_label = _label("0:00", ("music-bar-time", "music-numeric"), ellipsize=False)
        self.elapsed_label.set_hexpand(True)
        self.remaining_label = _label("-0:00", ("music-bar-time", "music-numeric"), xalign=1.0, ellipsize=False)
        times.append(self.elapsed_label)
        times.append(self.remaining_label)
        self.sub_stack.add_named(times, "times")
        texts.append(self.sub_stack)
        row.append(texts)

        self.more = Gtk.MenuButton()
        self.more.set_icon_name("music-more-symbolic")
        self.more.add_css_class("music-bar-icon")
        self.more.add_css_class("music-bar-more")
        self.more.set_direction(Gtk.ArrowType.UP)
        self.more.set_tooltip_text("その他")
        self.more.set_valign(Gtk.Align.CENTER)
        self.more.set_menu_model(self._build_more_menu())
        row.append(self.more)
        now.set_child(row)

        self.progress = ProgressLine()
        self.progress.set_valign(Gtk.Align.END)
        self.progress.set_margin_start(12)
        self.progress.set_margin_end(10)
        self.progress.set_margin_bottom(1)
        self.progress.connect("seek", PlayerBar._on_seek)
        self.progress.connect("preview", PlayerBar._on_preview)
        self.progress.connect("hover", PlayerBar._on_progress_hover)
        now.add_overlay(self.progress)
        self.now_stack.add_named(now, "track")

        idle = Gtk.Image.new_from_icon_name("music-note-symbolic")
        idle.set_pixel_size(20)
        idle.add_css_class("music-bar-idle")
        idle.set_halign(Gtk.Align.CENTER)
        idle.set_valign(Gtk.Align.CENTER)
        self.now_stack.add_named(idle, "idle")
        self.now_stack.set_visible_child_name("idle")

        # 6. パネル・出力先・音量 ------------------------------------------------
        side = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        side.add_css_class("music-bar-side")
        side.set_valign(Gtk.Align.CENTER)
        self.lyrics_button = ToggleCircle("music-lyrics-symbolic", "歌詞", size=32)
        self.lyrics_button.image.set_pixel_size(17)
        self.queue_button = ToggleCircle("music-queue-symbolic", "次に再生", size=32)
        self.queue_button.image.set_pixel_size(17)
        for button in (self.lyrics_button, self.queue_button):
            button.add_css_class("music-bar-button")
            button.add_css_class("music-bar-panel-toggle")
            side.append(button)
        self.lyrics_button.connect("toggled", PlayerBar._on_panel_toggled, "lyrics")
        self.queue_button.connect("toggled", PlayerBar._on_panel_toggled, "queue")

        self.output = Gtk.MenuButton()
        self.output.set_icon_name("music-output-symbolic")
        self.output.add_css_class("music-bar-icon")
        self.output.set_direction(Gtk.ArrowType.UP)
        self.output.set_tooltip_text("出力先")
        self._device_menu = Gio.Menu()
        self._device_items = Gio.Menu()
        self._device_menu.append_section("出力先", self._device_items)
        self.output.set_menu_model(self._device_menu)
        self.output.connect("notify::active", PlayerBar._on_output_active)
        side.append(self.output)

        self.volume = Gtk.MenuButton()
        self.volume.set_icon_name("music-volume-symbolic")
        self.volume.add_css_class("music-bar-icon")
        self.volume.set_direction(Gtk.ArrowType.UP)
        self.volume.set_tooltip_text("音量")
        self.volume.set_popover(self._build_volume_popover())
        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL
                                           | Gtk.EventControllerScrollFlags.DISCRETE)
        scroll.connect("scroll", PlayerBar._on_volume_scroll)
        self.volume.add_controller(scroll)
        side.append(self.volume)
        bar.append(side)
        self.side = side

        self.set_child(bar)
        self._install_actions()
        self._install_breakpoints()

        self._bindings = Bindings(self, on_rebind=self.update_all)
        store = ctx.store
        self._bindings.add(store, "status-changed", self._on_status)
        self._bindings.add(store, "track-changed", self._on_track)
        self._bindings.add(store, "state-changed", self._on_state)
        self._bindings.add(store, "connection-changed", self._on_connection)
        self.connect("map", PlayerBar._on_map_changed)
        self.connect("unmap", PlayerBar._on_map_changed)
        self.update_all()

    # --- 組み立て ------------------------------------------------------------------

    def _build_more_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        speeds = Gio.Menu()
        for key in SPEEDS:
            item = Gio.MenuItem.new(_speed_label(key), None)
            item.set_action_and_target_value("bar.speed", GLib.Variant.new_string(key))
            speeds.append_item(item)
        first = Gio.Menu()
        first.append_submenu("再生速度", speeds)
        first.append("イコライザ…", "app.equalizer")
        menu.append_section(None, first)
        second = Gio.Menu()
        second.append("再生中のリストを表示", "app.reveal-current")
        menu.append_section(None, second)
        links = Gio.Menu()
        links.append("リンクをコピー", "bar.copy-link")
        links.append("ブラウザで開く", "bar.open-browser")
        menu.append_section(None, links)
        return menu

    def _build_volume_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        popover.add_css_class("music-volume-popover")
        popover.set_has_arrow(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        low = Gtk.Image.new_from_icon_name("music-volume-mute-symbolic")
        low.set_pixel_size(14)
        low.add_css_class("music-volume-glyph")
        high = Gtk.Image.new_from_icon_name("music-volume-symbolic")
        high.set_pixel_size(16)
        high.add_css_class("music-volume-glyph")
        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.01)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.set_size_request(170, -1)
        self.volume_scale.add_css_class("music-volume-scale")
        self.volume_scale.update_property([Gtk.AccessibleProperty.LABEL], ["音量"])
        self.volume_scale.connect("value-changed", PlayerBar._on_volume_changed)
        box.append(low)
        box.append(self.volume_scale)
        box.append(high)
        popover.set_child(box)
        return popover

    def _install_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        speed = Gio.SimpleAction.new_stateful("speed", GLib.VariantType.new("s"), GLib.Variant.new_string("1.0"))
        speed.connect("change-state", PlayerBar._on_speed_action, self.ctx.store)
        group.add_action(speed)
        device = Gio.SimpleAction.new_stateful("device", GLib.VariantType.new("s"), GLib.Variant.new_string(""))
        device.connect("change-state", PlayerBar._on_device_action, self.ctx)
        group.add_action(device)
        loading = Gio.SimpleAction.new("device-loading", None)
        loading.set_enabled(False)
        group.add_action(loading)
        copy = Gio.SimpleAction.new("copy-link", None)
        copy.connect("activate", PlayerBar._on_copy_link, self.ctx)
        group.add_action(copy)
        browser = Gio.SimpleAction.new("open-browser", None)
        browser.connect("activate", PlayerBar._on_open_browser, self.ctx)
        group.add_action(browser)
        self.actions = group
        self.insert_action_group("bar", group)

    def _install_breakpoints(self) -> None:
        wide = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {self.WIDE_ENOUGH}px"))
        for widget in (self.output, self.more):
            wide.add_setter(widget, "visible", False)
        self.add_breakpoint(wide)
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {self.NARROW}px"))
        for widget in (self.output, self.more, self.sub_stack):
            narrow.add_setter(widget, "visible", False)
        self.add_breakpoint(narrow)
        tiny = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {self.TINY}px"))
        for widget in (self.output, self.more, self.sub_stack, self.lyrics_button, self.queue_button,
                       self.shuffle, self.repeat):
            tiny.add_setter(widget, "visible", False)
        self.add_breakpoint(tiny)

    # --- 窓から ------------------------------------------------------------------

    def set_panel(self, name: str) -> None:
        """右パネルの状態をボタンに映す (窓が呼ぶ)。"""
        self._panel = name or ""
        self._syncing = True
        try:
            self.lyrics_button.set_active(self._panel == "lyrics")
            self.queue_button.set_active(self._panel == "queue")
        finally:
            self._syncing = False

    @staticmethod
    def _on_panel_toggled(button: ToggleCircle, name: str) -> None:
        self = button.get_ancestor(PlayerBar)
        if self is None or self._syncing:
            return
        window = getattr(self.ctx, "window", None)
        if window is None or not hasattr(window, "show_panel"):
            return
        if button.get_active():
            window.show_panel(name)
        elif self._panel == name:
            window.show_panel("")

    # --- 操作 --------------------------------------------------------------------

    @staticmethod
    def _bar_of(widget):
        return widget.get_ancestor(PlayerBar)

    @staticmethod
    def _on_prev(button) -> None:
        self = PlayerBar._bar_of(button)
        if self is not None:
            self.ctx.store.prev()

    @staticmethod
    def _on_next(button) -> None:
        self = PlayerBar._bar_of(button)
        if self is not None:
            self.ctx.store.next()

    @staticmethod
    def _on_play(button) -> None:
        self = PlayerBar._bar_of(button)
        if self is not None:
            self.ctx.store.toggle()

    @staticmethod
    def _on_shuffle_toggled(button) -> None:
        self = PlayerBar._bar_of(button)
        if self is None or self._syncing:
            return
        self.ctx.store.set_shuffle(button.get_active())

    @staticmethod
    def _on_repeat_clicked(button) -> None:
        self = PlayerBar._bar_of(button)
        if self is None or self._syncing:
            return
        self.ctx.store.cycle_repeat()
        self._update_modes()

    @staticmethod
    def _on_seek(line: ProgressLine, seconds: float) -> None:
        self = PlayerBar._bar_of(line)
        if self is not None:
            self.ctx.store.seek_to(seconds)
            self._update_times(force=True)

    @staticmethod
    def _on_preview(line: ProgressLine, seconds: float) -> None:
        self = PlayerBar._bar_of(line)
        if self is None:
            return
        self._preview = seconds
        self._update_time_visibility()
        self._update_times(force=True)

    @staticmethod
    def _on_progress_hover(line: ProgressLine, hover: bool) -> None:
        self = PlayerBar._bar_of(line)
        if self is None:
            return
        self._hover_times = hover
        self._update_time_visibility()

    @staticmethod
    def _on_speed_action(action: Gio.SimpleAction, value: GLib.Variant, store) -> None:
        key = value.get_string()
        if key not in SPEEDS:
            return
        action.set_state(value)
        store.set_speed(float(key))

    @staticmethod
    def _on_device_action(action: Gio.SimpleAction, value: GLib.Variant, ctx) -> None:
        name = value.get_string()
        if not name:
            return
        previous = action.get_state()
        action.set_state(value)

        def done(response) -> None:
            if not response.ok:
                action.set_state(previous)
                ctx.toast(f"出力先を切り替えられませんでした: {response.message}")

        ctx.store.set_device(name, callback=done)

    @staticmethod
    def _on_copy_link(_action, _value, ctx) -> None:
        track = ctx.store.current_track()
        url = track.web_url if track is not None else None
        if url:
            ctx.copy_text(url)

    @staticmethod
    def _on_open_browser(_action, _value, ctx) -> None:
        track = ctx.store.current_track()
        url = track.web_url if track is not None else None
        if url:
            ctx.open_uri(url)

    @staticmethod
    def _on_output_active(button: Gtk.MenuButton, _pspec) -> None:
        if not button.get_active():
            return
        self = PlayerBar._bar_of(button)
        if self is None:
            return
        items = self._device_items
        items.remove_all()
        items.append("読み込み中…", "bar.device-loading")
        action = self.actions.lookup_action("device")

        def done(devices) -> None:
            items.remove_all()
            if not devices:
                items.append("出力先が見つかりません", "bar.device-loading")
                return
            active = ""
            for name, used in devices:
                item = Gio.MenuItem.new(name.replace("_", "__"), None)
                item.set_action_and_target_value("bar.device", GLib.Variant.new_string(name))
                items.append_item(item)
                if used:
                    active = name
            if action is not None:
                action.set_state(GLib.Variant.new_string(active))

        self.ctx.store.list_devices(done)

    @staticmethod
    def _on_volume_changed(scale: Gtk.Scale) -> None:
        self = scale.get_ancestor(PlayerBar)
        if self is None:
            # ポップオーバーは再生バーの子ではない。持ち主の MenuButton から辿る
            popover = scale.get_ancestor(Gtk.Popover)
            parent = popover.get_parent() if popover is not None else None
            self = parent.get_ancestor(PlayerBar) if parent is not None else None
        if self is None or self._volume_syncing:
            return
        self._volume_user_at = time.monotonic()
        self.ctx.store.set_volume_db(linear_to_db(scale.get_value()))
        self._update_volume_icon(linear_to_db(scale.get_value()))

    @staticmethod
    def _on_volume_scroll(controller, _dx, dy) -> bool:
        self = PlayerBar._bar_of(controller.get_widget())
        if self is None or not self.ctx.store.connected:
            return False
        self._volume_user_at = time.monotonic()
        self.ctx.store.volume_step(-2.0 if dy > 0 else 2.0)
        self._update_volume()
        return True

    # --- 状態を映す ------------------------------------------------------------------

    def _on_status(self, _store) -> None:
        self._update_texts()
        self._update_modes()
        self._update_progress()
        self._update_volume()

    def _on_track(self, _store) -> None:
        self._update_track()

    def _on_state(self, _store) -> None:
        self._update_play()
        self._update_ticking()

    def _on_connection(self, _store) -> None:
        self.update_all()

    def update_all(self) -> None:
        self._play_key = None
        self._repeat_shown = None
        self._update_sensitivity()
        self._update_track()
        self._update_play()
        self._update_modes()
        self._update_volume()
        self._update_ticking()

    def _update_sensitivity(self) -> None:
        store = self.ctx.store
        connected = store.connected
        if connected:
            self.bar.remove_css_class("disconnected")
        else:
            self.bar.add_css_class("disconnected")
        for widget in (self.shuffle, self.repeat, self.prev, self.play, self.next, self.output, self.volume):
            widget.set_sensitive(connected)
        self.lyrics_button.set_sensitive(True)
        self.queue_button.set_sensitive(True)
        self.actions.lookup_action("speed").set_enabled(connected and store.supports("speed"))

    def _update_track(self) -> None:
        store = self.ctx.store
        track = store.current_track()
        if track is None or not store.connected:
            self.now_stack.set_visible_child_name("idle")
            self.art.clear()
            self._track_sig = None
        else:
            self.now_stack.set_visible_child_name("track")
            sig = (track.path, track.meta_get("art"))
            if sig != self._track_sig:
                self._track_sig = sig
                self.art.set_subject(self.ctx.artwork, track, kind="station" if track.live else "track")
        url = track.web_url if track is not None else None
        self.actions.lookup_action("copy-link").set_enabled(bool(url))
        self.actions.lookup_action("open-browser").set_enabled(bool(url))
        self._update_texts()
        self._update_progress()
        self._update_ticking()

    def _update_texts(self) -> None:
        st = self.ctx.store.status
        track = st.track
        title = st.display_title if track is not None else ""
        subtitle = st.display_subtitle if track is not None else ""
        if self.title_label.get_text() != title:
            self.title_label.set_text(title)
            self.title_label.set_tooltip_text(title or None)
        if self.subtitle_label.get_text() != subtitle:
            self.subtitle_label.set_text(subtitle)
            self.subtitle_label.set_tooltip_text(subtitle or None)
        if st.buffering != self.subtitle_label.has_css_class("loading"):
            if st.buffering:
                self.subtitle_label.add_css_class("loading")
            else:
                self.subtitle_label.remove_css_class("loading")
        star = bool(track and track.bookmark)
        if self.star.get_visible() != star:
            self.star.set_visible(star)
        live = bool(track and track.live)
        if self.live_badge.get_visible() != live:
            self.live_badge.set_visible(live)

    def _update_play(self) -> None:
        # 状態は 1 秒に何度も届くので、変わったときだけ部品に触る
        st = self.ctx.store.status
        playing = st.state == "playing" or (st.state == "stopped" and st.buffering)
        has_list = st.total > 0 or st.track is not None
        key = (playing, self.ctx.store.connected and has_list)
        if key == self._play_key:
            return
        self._play_key = key
        self.play.set_icon_name("music-pause-symbolic" if playing else "music-play-symbolic")
        label = "一時停止" if playing else "再生"
        self.play.set_tooltip_text(label)
        self.play.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.prev.set_sensitive(key[1])
        self.next.set_sensitive(key[1])

    def _update_modes(self) -> None:
        st = self.ctx.store.status
        self._syncing = True
        try:
            self.shuffle.set_active(bool(st.shuffle))
            self.repeat.set_active(st.repeat in ("all", "one"))
        finally:
            self._syncing = False
        if st.repeat != self._repeat_shown:
            self._repeat_shown = st.repeat
            one = st.repeat == "one"
            self.repeat.set_icon_name("music-repeat-one-symbolic" if one else "music-repeat-symbolic")
            tip = {"off": "リピート: オフ", "all": "リピート: すべて", "one": "リピート: 1 曲"}.get(st.repeat, "リピート")
            self.repeat.set_tooltip_text(tip)
        speed = self.actions.lookup_action("speed")
        key = _speed_key(st.speed)
        if speed.get_state().get_string() != key:
            speed.set_state(GLib.Variant.new_string(key))
        self._update_play()

    def _seekable(self) -> bool:
        st = self.ctx.store.status
        return (self.ctx.store.connected and st.track is not None and not st.is_live
                and st.duration > 0 and self.ctx.store.supports("seek_to"))

    def _update_progress(self) -> None:
        st = self.ctx.store.status
        live = st.is_live
        show_line = st.track is not None and not live and st.duration > 0
        if self.progress.get_visible() != show_line:
            self.progress.set_visible(show_line)
        seekable = self._seekable()
        if seekable != self.progress.seekable:
            self.progress.set_seekable(seekable)
        if show_line:
            self.progress.set_position(self.ctx.store.position_now(), st.duration)
        self._update_time_visibility()
        self._update_times()

    def _update_time_visibility(self) -> None:
        st = self.ctx.store.status
        show = (self._hover_times or self._preview >= 0) and st.track is not None and not st.is_live \
            and st.duration > 0
        name = "times" if show else "subtitle"
        if self.sub_stack.get_visible_child_name() != name:
            self.sub_stack.set_visible_child_name(name)
            self._update_times(force=True)

    def _update_times(self, force: bool = False) -> None:
        if self.sub_stack.get_visible_child_name() != "times" and not force:
            return
        st = self.ctx.store.status
        position = self._preview if self._preview >= 0 else self.ctx.store.position_now()
        second = int(position)
        if second == self._shown_second and not force:
            return
        self._shown_second = second
        self.elapsed_label.set_text(format_time(position))
        remaining = max(0.0, st.duration - position)
        self.remaining_label.set_text("-" + format_time(remaining))

    def _update_volume(self) -> None:
        st = self.ctx.store.status
        if time.monotonic() - self._volume_user_at > USER_HOLD:
            value = db_to_linear(st.volume)
            if abs(self.volume_scale.get_value() - value) > 0.004:
                self._volume_syncing = True
                try:
                    self.volume_scale.set_value(value)
                finally:
                    self._volume_syncing = False
        self._update_volume_icon(st.volume)

    def _update_volume_icon(self, db: float) -> None:
        # 変わったときだけ差し替える (MenuButton の記号を替えると開いているポップオーバーが閉じる)
        muted = db_to_linear(db) <= 0.0
        icon = "music-volume-mute-symbolic" if muted else "music-volume-symbolic"
        if self.volume.get_icon_name() != icon:
            self.volume.set_icon_name(icon)
        tip = f"音量 {int(round(db_to_linear(db) * 100))}%"
        if self.volume.get_tooltip_text() != tip:
            self.volume.set_tooltip_text(tip)

    # --- 位置の刻み ------------------------------------------------------------------

    @staticmethod
    def _on_map_changed(self) -> None:
        self._update_ticking()

    def _update_ticking(self) -> None:
        st = self.ctx.store.status
        want = (self.get_mapped() and st.state == "playing" and not st.buffering
                and st.track is not None and not st.is_live and st.duration > 0)
        if want and not self._tick_id:
            self._tick_id = self.add_tick_callback(PlayerBar._on_tick)
        elif not want and self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    @staticmethod
    def _on_tick(self, _clock) -> bool:
        st = self.ctx.store.status
        self.progress.set_position(self.ctx.store.position_now(), st.duration)
        self._update_times()
        return GLib.SOURCE_CONTINUE

    def do_unrealize(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        Adw.BreakpointBin.do_unrealize(self)
