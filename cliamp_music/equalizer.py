"""イコライザ (EqualizerWindow、Ctrl+Alt+E)。

macOS のミュージックのイコライザを写したもの (外観のライト / ダークに従う)。上にプリセットの選択
(cliamp の eq_presets + 「カスタム」) と「フラットに戻す」、中央に 10 本の縦の
つまみ (−12〜+12 dB、赤い丸)。後ろに目盛りの線と dB の見出しを描く。
下に再生速度 (0.5〜2.0 倍)。

つまみを動かすと 80ms まとめて store.set_eq_band を呼ぶ (store の側でも送信中は
最後の値だけ覚えて溜めない)。cliamp の値 (status.eq) には、利用者が触っていない
ときだけ合わせる (押している間と、最後に動かしてから少しの間は合わせない)。
cliamp に帯域の値を読む拡張が無いとき (api 0) は、つまみは送るだけになる。
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .fullscreen import _label, _rect, _rgba, _set_text, app_while_visible  # noqa: E402
from .protocol import EQ_BANDS, EQ_MAX_DB, EQ_MIN_DB  # noqa: E402
from .widgets import install_space_toggle  # noqa: E402

__all__ = ["EqualizerWindow", "band_label", "preset_label", "FALLBACK_PRESETS", "CUSTOM_LABEL"]

# 自分で描く線と文字の色 (ダーク, ライト): 帯域の溝、目盛りの線 (0 dB とそれ以外)、dB の見出し。
# ダークは暗色固定だったときの値のまま、ライトは style/base.css の --m-track・--m-secondary と
# 同じ考えの黒。CSS の変数は描く側から読めないので、ここで外観を見て選ぶ (イコライザの窓は
# フルスクリーンと違い外観に従うので、アプリ全体の外観 = Adw.StyleManager の dark でよい)
EQ_COLORS = {
    "track": ("rgba(255,255,255,0.14)", "rgba(0,0,0,0.10)"),
    "line-zero": ("rgba(255,255,255,0.20)", "rgba(0,0,0,0.16)"),
    "line": ("rgba(255,255,255,0.075)", "rgba(0,0,0,0.06)"),
    "label": ("rgba(235,235,245,0.55)", "rgba(0,0,0,0.45)"),
}


def eq_color(name: str) -> Gdk.RGBA:
    """EQ_COLORS の今の外観の色。"""
    dark = Adw.StyleManager.get_default().get_dark()
    return _rgba(EQ_COLORS[name][0 if dark else 1])

CUSTOM_LABEL = "カスタム"
# 拡張の無い cliamp (capabilities が無い) のときに出すプリセット (cliamp 1.50.0 の eq_presets.go の並び)。
FALLBACK_PRESETS = ("Flat", "Rock", "Pop", "Jazz", "Classical", "Bass Boost", "Treble Boost", "Vocal",
                    "Electronic", "Acoustic", "Hip-Hop", "R&B", "Loudness", "Late Night", "Podcast",
                    "Small Speakers")
_PRESET_NAMES = {
    "flat": "フラット", "rock": "ロック", "pop": "ポップ", "jazz": "ジャズ", "classical": "クラシック",
    "bass boost": "低音を強調", "treble boost": "高音を強調", "vocal": "ボーカル",
    "electronic": "エレクトロニック", "acoustic": "アコースティック", "hip-hop": "ヒップホップ",
    "r&b": "R&B", "loudness": "ラウドネス", "late night": "深夜", "podcast": "ポッドキャスト",
    "small speakers": "小型スピーカー", "custom": CUSTOM_LABEL,
}
SPEED_MIN = 0.5
SPEED_MAX = 2.0


def band_label(band: str) -> str:
    """"70" → "70 Hz"、"1K" → "1 kHz"。"""
    text = str(band).strip()
    if text[-1:] in ("K", "k"):
        return f"{text[:-1]} kHz"
    return f"{text} Hz"


def preset_label(name: str) -> str:
    """プリセット名の表示 (知らない名前はそのまま)。"""
    return _PRESET_NAMES.get((name or "").strip().lower(), name)


def _format_db(db: float) -> str:
    if abs(db) < 0.05:
        return "0 dB"
    return f"{'+' if db > 0 else '−'}{abs(db):.1f} dB"


# --------------------------------------------------------------------------
# つまみ


class _Fader(Gtk.Scale):
    """1 本の帯域のつまみ。溝と 0 dB からの赤い塗りは自分で描き、丸は CSS。

    CSS で溝の余白と丸の余白を 0 にしてあるので、値の位置は溝の中で線形に決まる
    (value_y)。0 dB に目印 (mark) を置き、近くでは吸い付く。
    """

    def __init__(self, index: int, band: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.index = index
        self.band = band
        self.set_range(EQ_MIN_DB, EQ_MAX_DB)
        self.set_increments(0.5, 3.0)
        self.set_round_digits(1)
        self.set_inverted(True)
        self.set_draw_value(False)
        self.set_value(0.0)
        self.add_mark(0.0, Gtk.PositionType.RIGHT, None)
        self.add_css_class("music-eq-fader")
        self.set_vexpand(True)
        self.set_halign(Gtk.Align.CENTER)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{band_label(band)} の帯域"])

    def value_y(self, db: float) -> float:
        """値 db のつまみの中心の y (このウィジェットの座標)。"""
        rect = self.get_range_rect()
        start, end = self.get_slider_range()
        knob = max(0, end - start)
        travel = max(1.0, rect.height - knob)
        fraction = (EQ_MAX_DB - db) / (EQ_MAX_DB - EQ_MIN_DB)
        return rect.y + knob / 2 + fraction * travel

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        rect = self.get_range_rect()
        if rect.width > 0 and rect.height > 0:
            start, end = self.get_slider_range()
            knob = max(0, end - start)
            cx = rect.x + rect.width / 2
            track_w = 4.0
            top = rect.y + knob / 2
            bottom = rect.y + rect.height - knob / 2
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(_rect(cx - track_w / 2, top - 2, track_w, bottom - top + 4), track_w / 2)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(eq_color("track"), _rect(cx - track_w / 2, top - 2, track_w,
                                                            bottom - top + 4))
            snapshot.pop()
            zero = self.value_y(0.0)
            here = self.value_y(self.get_value())
            if abs(here - zero) > 0.5:
                y0, y1 = sorted((zero, here))
                fill = Gsk.RoundedRect()
                fill.init_from_rect(_rect(cx - track_w / 2, y0, track_w, y1 - y0), 1.0)
                snapshot.push_rounded_clip(fill)
                color = _rgba("#fa2d48") if self.get_sensitive() else _rgba("rgba(250,45,72,0.35)")
                snapshot.append_color(color, _rect(cx - track_w / 2, y0, track_w, y1 - y0))
                snapshot.pop()
        Gtk.Scale.do_snapshot(self, snapshot)


class _EqBoard(Gtk.Box):
    """帯域のつまみを並べ、後ろに目盛りの線 (±12, ±6, 0 dB) と dB の見出しを描く。"""

    LABEL_W = 56
    COLUMN_W = 44
    LINES = (12.0, 6.0, 0.0, -6.0, -12.0)
    LABELED = {12.0: "+12 dB", 0.0: "0 dB", -12.0: "−12 dB"}

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("music-eq-board")
        spacer = Gtk.Box()
        spacer.set_size_request(self.LABEL_W, -1)
        self.append(spacer)
        self.faders: list[_Fader] = []
        self.band_labels: list[Gtk.Label] = []
        for index, band in enumerate(EQ_BANDS):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            column.set_size_request(self.COLUMN_W, -1)
            column.set_hexpand(True)
            fader = _Fader(index, band)
            column.append(fader)
            label = _label(band_label(band), "music-eq-band", xalign=0.5, ellipsize=False)
            column.append(label)
            self.append(column)
            self.faders.append(fader)
            self.band_labels.append(label)
        self._layouts: dict[float, Pango.Layout] = {}

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        if self.faders and width > 0:
            first, last = self.faders[0], self.faders[-1]
            ok_a, left = first.get_parent().compute_point(self, Graphene.Point().init(0, 0))
            ok_b, right = last.get_parent().compute_point(
                self, Graphene.Point().init(last.get_parent().get_width(), 0))
            x0 = left.x if ok_a else self.LABEL_W
            x1 = right.x if ok_b else width
            for db in self.LINES:
                ok, point = first.compute_point(self, Graphene.Point().init(0, first.value_y(db)))
                if not ok:
                    continue
                y = round(point.y) + 0.5
                strong = abs(db) < 0.01
                color = eq_color("line-zero" if strong else "line")
                snapshot.append_color(color, _rect(x0 + 4, y - 0.5, x1 - x0 - 8, 1))
                text = self.LABELED.get(db)
                if text:
                    layout = self._layouts.get(db)
                    if layout is None:
                        layout = self.create_pango_layout(text)
                        self._layouts[db] = layout
                    _ink, logical = layout.get_pixel_extents()
                    snapshot.save()
                    snapshot.translate(Graphene.Point().init(x0 - 8 - logical.width, y - logical.height / 2))
                    snapshot.append_layout(layout, eq_color("label"))
                    snapshot.restore()
        Gtk.Box.do_snapshot(self, snapshot)


# --------------------------------------------------------------------------
# 窓


class EqualizerWindow(Adw.Window):
    """イコライザの窓 (css クラス music, music-eq)。ctx.window の上に出し、閉じると隠れる。

    `EqualizerWindow(ctx)`、`present()` で出す。
    - `faders` (10 本の Gtk.Scale)、`preset_dropdown`、`flat_button`、`speed_scale`。
    - `set_band_value(index, db)`: 利用者が動かしたのと同じ扱いで値を入れる (試験用)。
    """

    SEND_MS = 80
    HOLD_SECS = 0.8
    SPEED_SEND_MS = 150

    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        self.add_css_class("music")
        self.add_css_class("music-eq")
        self.set_title("イコライザ")
        self.set_resizable(False)
        self.set_hide_on_close(True)
        parent = ctx.window if isinstance(ctx.window, Gtk.Window) else None
        if parent is not None:
            self.set_transient_for(parent)
            self.set_destroy_with_parent(True)
        app_while_visible(self, ctx.app)
        # Space はメインの窓と同じく再生/一時停止 (プリセットの選択を開かない。Enter で開ける)
        install_space_toggle(self)

        self._handlers: list[int] = []
        self._scheme_handler = 0
        self._syncing = False
        self._pending: dict[int, float] = {}
        self._send_timer = 0
        self._touched = [0.0] * len(EQ_BANDS)
        self._holding = [False] * len(EQ_BANDS)
        self._preset_names: list[str] = []
        self._speed_timer = 0
        self._speed_touched = 0.0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        header.set_title_widget(Adw.WindowTitle(title="イコライザ"))
        toolbar.add_top_bar(header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body.add_css_class("music-eq-body")
        body.set_margin_start(18)
        body.set_margin_end(18)
        body.set_margin_bottom(16)
        toolbar.set_content(body)
        self.set_content(toolbar)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        top.add_css_class("music-eq-top")
        top.set_margin_start(_EqBoard.LABEL_W - 4)
        caption = _label("プリセット", "music-eq-caption", ellipsize=False)
        top.append(caption)
        self.preset_dropdown = Gtk.DropDown.new_from_strings([CUSTOM_LABEL])
        self.preset_dropdown.add_css_class("music-eq-preset")
        self.preset_dropdown.set_size_request(190, -1)
        self.preset_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["プリセット"])
        self._preset_handler = self.preset_dropdown.connect("notify::selected", self._on_preset)
        top.append(self.preset_dropdown)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        self.flat_button = Gtk.Button(label="フラットに戻す")
        self.flat_button.add_css_class("music-eq-flat")
        self.flat_button.connect("clicked", self._on_flat)
        top.append(self.flat_button)
        body.append(top)

        self.board = _EqBoard()
        self.board.set_margin_top(18)
        self.board.set_size_request(-1, 250)
        self.faders = self.board.faders
        self._fader_handlers = []
        for fader in self.faders:
            self._fader_handlers.append(fader.connect("value-changed", self._on_fader))
            legacy = Gtk.EventControllerLegacy()
            legacy.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            legacy.connect("event", self._on_fader_event, fader.index)
            fader.add_controller(legacy)
            fader.set_tooltip_text(_format_db(0.0))
        body.append(self.board)

        self.note = _label("", "music-eq-note", xalign=0.5, ellipsize=False)
        self.note.set_wrap(True)
        self.note.set_margin_top(10)
        self.note.set_visible(False)
        body.append(self.note)

        separator = Gtk.Separator()
        separator.add_css_class("music-eq-separator")
        separator.set_margin_top(16)
        separator.set_margin_bottom(12)
        body.append(separator)

        speed = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        speed.add_css_class("music-eq-speed")
        speed_caption = _label("再生速度", "music-eq-caption", ellipsize=False)
        speed_caption.set_valign(Gtk.Align.START)
        speed_caption.set_margin_top(4)
        speed.append(speed_caption)
        self.speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, SPEED_MIN, SPEED_MAX, 0.05)
        self.speed_scale.add_css_class("music-eq-speed-scale")
        self.speed_scale.set_draw_value(False)
        self.speed_scale.set_round_digits(2)
        self.speed_scale.set_hexpand(True)
        self.speed_scale.set_value(1.0)
        for value, text in ((0.5, "0.5×"), (1.0, "1×"), (1.5, "1.5×"), (2.0, "2×")):
            self.speed_scale.add_mark(value, Gtk.PositionType.BOTTOM, text)
        self.speed_scale.update_property([Gtk.AccessibleProperty.LABEL], ["再生速度"])
        self._speed_handler = self.speed_scale.connect("value-changed", self._on_speed)
        speed.append(self.speed_scale)
        self.speed_label = _label("1.00×", "music-eq-speed-value", xalign=1.0, ellipsize=False)
        self.speed_label.set_width_chars(5)
        self.speed_label.set_valign(Gtk.Align.START)
        self.speed_label.set_margin_top(4)
        speed.append(self.speed_label)
        body.append(speed)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self._sync()

    # 試験・撮影用 -----------------------------------------------------------------

    def set_band_value(self, index: int, db: float) -> None:
        """利用者がつまみを動かしたのと同じ扱いで値を入れる。"""
        self.faders[index].set_value(db)

    @property
    def preset_names(self) -> list[str]:
        return list(self._preset_names)

    # store から -----------------------------------------------------------------

    def _on_map(self, *_args) -> None:
        store = self.ctx.store
        if not self._handlers:
            for name in ("status-changed", "connection-changed"):
                self._handlers.append(store.connect(name, self._on_store))
        if not self._scheme_handler:
            # 自分で描く線 (EQ_COLORS) は CSS の読み直しでは変わらないので、外観が変われば描き直す
            self._scheme_handler = Adw.StyleManager.get_default().connect("notify::dark", self._on_scheme)
        self._sync()

    def _on_unmap(self, *_args) -> None:
        store = self.ctx.store
        for handler in self._handlers:
            store.disconnect(handler)
        self._handlers = []
        if self._scheme_handler:
            Adw.StyleManager.get_default().disconnect(self._scheme_handler)
            self._scheme_handler = 0
        self._flush()

    def _on_scheme(self, *_args) -> None:
        self.board.queue_draw()
        for fader in self.faders:
            fader.queue_draw()

    def _on_store(self, _store) -> None:
        self._sync()

    def _presets(self) -> list[str]:
        store = self.ctx.store
        names = store.eq_presets
        if not names and store.connected and store.supports("eq"):
            names = list(FALLBACK_PRESETS)
        return names

    def _sync(self) -> None:
        store = self.ctx.store
        status = store.status
        online = store.connected and status.state != "offline" and store.supports("eq")
        readable = online and store.api >= 1
        names = self._presets()
        if names != self._preset_names:
            self._preset_names = list(names)
            model = Gtk.StringList.new([preset_label(n) for n in names] + [CUSTOM_LABEL])
            self._syncing = True
            try:
                with self.preset_dropdown.handler_block(self._preset_handler):
                    self.preset_dropdown.set_model(model)
            finally:
                self._syncing = False
        current = (status.eq_preset or "").strip().lower()
        index = next((i for i, n in enumerate(self._preset_names) if n.lower() == current), len(self._preset_names))
        if self.preset_dropdown.get_selected() != index:
            with self.preset_dropdown.handler_block(self._preset_handler):
                self.preset_dropdown.set_selected(index)
        self.preset_dropdown.set_sensitive(online and bool(self._preset_names))
        self.flat_button.set_sensitive(online)
        self.board.set_sensitive(online)
        now = time.monotonic()
        if readable:
            for fader, value in zip(self.faders, status.eq):
                i = fader.index
                if self._holding[i] or i in self._pending or now - self._touched[i] < self.HOLD_SECS:
                    continue
                if abs(fader.get_value() - value) > 0.01:
                    with fader.handler_block(self._fader_handlers[i]):
                        fader.set_value(value)
                    fader.set_tooltip_text(_format_db(value))
                    fader.queue_draw()
        if not store.connected:
            text = "cliamp に接続できません"
        elif not store.supports("eq"):
            text = "この cliamp ではイコライザを使えません"
        elif not readable:
            text = "この cliamp からは今の値を読めません。動かした値だけが送られます"
        else:
            text = ""
        _set_text(self.note, text)
        self.note.set_visible(bool(text))
        # 再生速度
        self.speed_scale.set_sensitive(online and store.supports("speed"))
        if now - self._speed_touched > self.HOLD_SECS and not self._speed_timer:
            speed = min(SPEED_MAX, max(SPEED_MIN, status.speed or 1.0))
            if abs(self.speed_scale.get_value() - speed) > 0.001:
                with self.speed_scale.handler_block(self._speed_handler):
                    self.speed_scale.set_value(speed)
            _set_text(self.speed_label, f"{status.speed or 1.0:.2f}×")

    # 利用者の操作 -----------------------------------------------------------------

    def _on_fader_event(self, _controller, event: Gdk.Event, index: int) -> bool:
        kind = event.get_event_type()
        if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._holding[index] = True
        elif kind in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            self._holding[index] = False
            self._touched[index] = time.monotonic()
        return False

    def _on_fader(self, fader: _Fader) -> None:
        value = round(fader.get_value() * 2) / 2
        self._touched[fader.index] = time.monotonic()
        self._pending[fader.index] = value
        fader.set_tooltip_text(_format_db(value))
        if not self._send_timer:
            self._send_timer = GLib.timeout_add(self.SEND_MS, self._on_send_timer)

    def _on_send_timer(self) -> bool:
        self._send_timer = 0
        self._flush()
        return GLib.SOURCE_REMOVE

    def _flush(self) -> None:
        if self._send_timer:
            GLib.source_remove(self._send_timer)
            self._send_timer = 0
        pending, self._pending = self._pending, {}
        store = self.ctx.store
        if not store.connected:
            return
        now = time.monotonic()
        for index, value in sorted(pending.items()):
            self._touched[index] = now
            store.set_eq_band(index, value)

    def _on_preset(self, dropdown: Gtk.DropDown, _pspec) -> None:
        if self._syncing:
            return
        index = dropdown.get_selected()
        if 0 <= index < len(self._preset_names):
            self._pending.clear()
            self._touched = [0.0] * len(EQ_BANDS)
            self.ctx.store.set_eq_preset(self._preset_names[index])

    def _on_flat(self, _button) -> None:
        store = self.ctx.store
        self._pending.clear()
        flat = next((n for n in self._preset_names if n.lower() == "flat"), None)
        if flat is not None:
            self._touched = [0.0] * len(EQ_BANDS)
            store.set_eq_preset(flat)
            return
        for fader in self.faders:
            fader.set_value(0.0)

    def _on_speed(self, scale: Gtk.Scale) -> None:
        value = round(scale.get_value() * 20) / 20
        self._speed_touched = time.monotonic()
        _set_text(self.speed_label, f"{value:.2f}×")
        if self._speed_timer:
            GLib.source_remove(self._speed_timer)
        self._speed_timer = GLib.timeout_add(self.SPEED_SEND_MS, self._send_speed)

    def _send_speed(self) -> bool:
        self._speed_timer = 0
        self._speed_touched = time.monotonic()
        value = round(self.speed_scale.get_value() * 20) / 20
        self.ctx.store.set_speed(value)
        return GLib.SOURCE_REMOVE

    def do_dispose(self) -> None:
        for name in ("_send_timer", "_speed_timer"):
            source = getattr(self, name, 0)
            if source:
                GLib.source_remove(source)
                setattr(self, name, 0)
        Adw.Window.do_dispose(self)
