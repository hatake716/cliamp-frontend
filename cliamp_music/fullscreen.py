"""フルスクリーンプレーヤー (FullscreenPlayer、Shift+Ctrl+F、Esc で戻る)。

窓の根の Gtk.Stack の "fullscreen" に置かれる。窓は切り替えのたびに
`set_active(True/False)` を呼ぶ。動いている間だけ store のシグナルを受け、
再生位置の時計を回し、歌詞を取る (止めている間は何も受けない)。

見た目は macOS 27 のミュージックのフルスクリーン:
- 背景は今の曲の絵を覆うように敷いて大きくぼかし、暗くしたもの。ぼかしは
  曲が変わったときに 1 回だけ小さなテクスチャへ焼き (blurred_texture)、描くときは
  それを引き伸ばすだけにする。
- 左の列に 360px の絵、曲名、「アーティスト — アルバム」、再生位置の線 (経過 / 残り)、
  操作の列 (シャッフル・前へ・再生・次へ・リピート)。
- 右に大きな歌詞 (今の行だけ明るく、離れるほど薄くぼかす) か「次に再生」。
  右下のガラスの切り替えで選ぶ。
- 左上のガラスのカプセルに ✕ とミニプレーヤー、右上に出力先と音量。

ミニプレーヤー (miniplayer.py) と共有する部品もここに置く:
blurred_texture / ArtBackdrop / Scrubber / TransportRow / CoverArt / time_texts。
"""

from __future__ import annotations

import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import log  # noqa: E402
from .artwork import placeholder_colors  # noqa: E402
from .protocol import (  # noqa: E402
    VOLUME_MIN_DB,
    Lyrics,
    Status,
    Track,
    format_time,
    fraction_to_volume,
    playback_error_tooltip,
    track_key,
    volume_fraction,
)
from .widgets import (  # noqa: E402
    Artwork,
    CircleButton,
    EmptyState,
    GlassCapsule,
    SectionHeader,
    TrackList,
    TrackRow,
    color_pair_for,
)

__all__ = [
    "FullscreenPlayer", "LyricsView", "UpNextView",
    "ArtBackdrop", "Scrubber", "TransportRow", "CoverArt", "VolumeControl",
    "blurred_texture", "time_texts", "lyrics_subject", "volume_fraction", "fraction_to_volume",
    "app_while_visible", "station_ground", "backdrop_source",
]

UNSUPPORTED = "この cliamp は拡張 IPC に対応していません"


# --------------------------------------------------------------------------
# 小道具


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    if not color.parse(spec):
        color.parse("#808080")
    return color


def _rect(x: float, y: float, w: float, h: float) -> Graphene.Rect:
    return Graphene.Rect().init(x, y, w, h)


def _point(x: float, y: float) -> Graphene.Point:
    return Graphene.Point().init(x, y)


def _stops(*pairs: tuple[float, str]) -> list[Gsk.ColorStop]:
    out = []
    for offset, spec in pairs:
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = _rgba(spec)
        out.append(stop)
    return out


def _label(text: str = "", *classes: str, xalign: float = 0.0, ellipsize: bool = True) -> Gtk.Label:
    """文字は必ず set_text で入れる (曲名の & や < をマークアップとして読ませない)。"""
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    if ellipsize:
        label.set_ellipsize(Pango.EllipsizeMode.END)
        # 自然幅を小さく申告させ、親の幅で省略させる (長い曲名で列を広げない)
        label.set_max_width_chars(1)
        label.set_width_chars(1)
    for cls in classes:
        label.add_css_class(cls)
    return label


def _set_text(label: Gtk.Label, text: str) -> None:
    text = text or ""
    if label.get_text() != text:
        label.set_text(text)


def _set_tooltip(widget: Gtk.Widget, text: str | None) -> None:
    # 状態は 1 秒に何度も届くので、変わったときだけ触る (出ているツールチップを揺らさない)
    if widget.get_tooltip_text() != (text or None):
        widget.set_tooltip_text(text or None)


def _show_problem(label: Gtk.Label, problem: tuple[str, str] | None, wrap: bool = True) -> None:
    """副題に再生できなかった理由を出しているときの見た目: 琥珀色 (.problem)、2 行まで折り返し
    (手当ての括弧書きまで読めるように)、ツールチップに短文と cliamp の誤りの全文。"""
    on = problem is not None
    _toggle_class(label, "problem", on)
    _set_tooltip(label, playback_error_tooltip(*problem) if on else None)
    wrap = wrap and on
    if label.get_wrap() != wrap:
        label.set_wrap(wrap)
        # WORD_CHAR: 知らない誤りの短文は URL など空白の無い長い語になりうる。WORD だと
        # その語の幅がラベルの最小幅になり、列 (フルスクリーン・ミニプレーヤー) を押し広げる。
        # 語の途中で折るときに "-" を足さない (URL やパスの一部に見える)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR if wrap else Pango.WrapMode.WORD)
        label.set_lines(2 if wrap else -1)
        if wrap:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_insert_hyphens_new(False))
            label.set_attributes(attrs)
        else:
            label.set_attributes(None)


def _animations_enabled(widget: Gtk.Widget) -> bool:
    settings = widget.get_settings() or Gtk.Settings.get_default()
    return bool(settings is None or settings.get_property("gtk-enable-animations"))


def _toggle_class(widget: Gtk.Widget, name: str, on: bool) -> None:
    if on:
        widget.add_css_class(name)
    else:
        widget.remove_css_class(name)


def time_texts(position: float, duration: float) -> tuple[str, str]:
    """(経過, 残り)。残りは Apple と同じく「−2:26」。長さが分からなければ残りは空。"""
    position = max(0.0, float(position or 0.0))
    if duration and duration > 0:
        position = min(position, float(duration))
        return format_time(position), "−" + format_time(max(0.0, float(duration) - position))
    return format_time(position), ""


def lyrics_subject(status: Status) -> tuple[str, str] | None:
    """歌詞を探す (アーティスト, 曲名)。探せないときは None。

    ラジオ (live) は ICY の曲名が「アーティスト - 曲名」の形のときだけ探す。
    """
    track = status.track
    if track is None:
        return None
    icy = (status.stream_title or "").strip()
    if icy:
        for sep in (" - ", " – ", " — ", " / "):
            if sep in icy:
                artist, title = icy.split(sep, 1)
                artist, title = artist.strip(), title.strip()
                if artist and title:
                    return artist, title
        return None
    if track.live:
        return None
    title = track.title.strip() or track.display_title
    if not title:
        return None
    return track.artist.strip(), title


def _attach_app(window: Gtk.Window, app) -> None:
    if window.get_application() is None:
        window.set_application(app)


def _detach_app(window: Gtk.Window) -> None:
    if window.get_application() is not None:
        window.set_application(None)


def app_while_visible(window: Gtk.Window, app) -> None:
    """window を見せている間だけ app の窓にする (隠した窓でアプリが終われなくならないように)。

    ミニプレーヤーとイコライザは閉じても捨てずに隠して使い回す。Gtk.Application は
    隠れた窓も数えるので、そのままだとメインの窓を閉じてもアプリが残る。
    見せるときに付け直すので、app のアクションとショートカットは見えている間ずっと効く。
    """
    if not isinstance(app, Gtk.Application):
        return
    window.set_application(app)
    window.connect("show", _attach_app, app)
    window.connect("hide", _detach_app)


def _color_matrix(saturation: float, brightness: float) -> Graphene.Matrix:
    """彩度と明るさの色の行列。GSK は色を行ベクトルとして v · M で掛ける
    (M[i][j] = 入力の i 番目の色が出力の j 番目に効く量)。"""
    weights = (0.2126, 0.7152, 0.0722)
    values: list[float] = []
    for i in range(3):
        for j in range(3):
            value = (1.0 - saturation) * weights[i] + (saturation if i == j else 0.0)
            values.append(value * brightness)
        values.append(0.0)
    values.extend((0.0, 0.0, 0.0, 1.0))
    matrix = Graphene.Matrix()
    matrix.init_from_float(values)
    return matrix


def blurred_texture(widget: Gtk.Widget, texture: Gdk.Texture | None, *, size: int = 128,
                    radius: float = 15.0, saturation: float = 1.35, brightness: float = 1.0,
                    ground: tuple[Gdk.RGBA, Gdk.RGBA] | None = None) -> Gdk.Texture | None:
    """texture を size 四方に縮めてぼかし、彩度を上げたテクスチャを作る。

    widget の窓の描画器で 1 回だけ描く (実現 (realize) 前は None)。縮めてから
    ぼかすので安く、引き伸ばして使えばさらに大きくぼけて見える。端が透明に
    溶けないよう、絵を枠より大きく描いてから内側を切り出す。
    ground (上の色, 下の色) を渡すと、絵の代わりにその色のグラデーションを使う
    (ラジオ局の小さな favicon や代わりの絵。白い地や記号を引き伸ばすと白い塊になるため)。
    """
    if texture is None and ground is None:
        return None
    native = widget.get_native()
    renderer = native.get_renderer() if native is not None else None
    if renderer is None:
        return None
    pad = radius * 2.5
    box = size + 2 * pad
    snapshot = Gtk.Snapshot()
    snapshot.push_color_matrix(_color_matrix(saturation, brightness), Graphene.Vec4().init(0, 0, 0, 0))
    snapshot.push_blur(radius)
    if ground is not None:
        area = _rect(-pad, -pad, box, box)
        top, bottom = ground
        stops = []
        for offset, color in ((0.0, top), (1.0, bottom)):
            stop = Gsk.ColorStop()
            stop.offset = offset
            stop.color = color
            stops.append(stop)
        snapshot.append_linear_gradient(area, _point(0, -pad), _point(0, size + pad), stops)
    else:
        tw, th = texture.get_width(), texture.get_height()
        if tw <= 0 or th <= 0:
            return None
        scale = max(box / tw, box / th)
        dw, dh = tw * scale, th * scale
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.TRILINEAR,
                                       _rect((size - dw) / 2, (size - dh) / 2, dw, dh))
    snapshot.pop()
    snapshot.pop()
    node = snapshot.to_node()
    if node is None:
        return None
    try:
        return renderer.render_texture(node, _rect(0, 0, size, size))
    except GLib.Error as error:  # 描画器が使えなくても背景が無地になるだけ
        log(f"背景のぼかしを作れません: {error.message}")
        return None


def station_ground(track: Track) -> tuple[Gdk.RGBA, Gdk.RGBA]:
    """ラジオ局の絵の地の色 (名前から決まる。widgets.StationTile と同じ組)。"""
    top, bottom = color_pair_for(track.display_title)
    return _rgba(top), _rgba(bottom)


def _placeholder_ground(key: str) -> tuple[Gdk.RGBA, Gdk.RGBA]:
    out = []
    for red, green, blue in placeholder_colors(key):
        color = Gdk.RGBA()
        color.red, color.green, color.blue, color.alpha = red, green, blue, 1.0
        out.append(color)
    return out[0], out[1]


def backdrop_source(loader, track: Track | None, texture: Gdk.Texture | None, size: int):
    """背景に渡す (絵, 地の色)。

    代わりの絵 (白い記号入り) やラジオ局の小さな favicon をそのまま引き伸ばすと
    白い塊になるので、地の色だけにする。局は CoverArt と同じ局の色、それ以外は
    代わりの絵と同じ色。
    """
    if track is None:
        return None, _placeholder_ground("cliamp-music:none")
    if not track.live:
        try:
            if texture is not None and texture is loader.placeholder(track_key(track), size, "track"):
                return None, _placeholder_ground(track_key(track))
        except Exception:  # 見分けられなければ届いた絵のまま
            pass
        return texture, None
    if texture is not None:
        try:
            if texture is loader.placeholder(track_key(track), size, "station"):
                texture = None
        except Exception:  # 見分けられなければ届いた絵のまま
            pass
    if texture is None or texture.get_width() < size * 0.6:
        return texture, station_ground(track)
    return texture, None


def _append_cover(snapshot: Gtk.Snapshot, texture: Gdk.Texture, x: float, y: float,
                  width: float, height: float, filter_: Gsk.ScalingFilter = Gsk.ScalingFilter.LINEAR) -> None:
    """枠を覆うように中央基準で拡大して描く (はみ出しは呼び手が切る)。"""
    tw, th = texture.get_width(), texture.get_height()
    if tw <= 0 or th <= 0:
        return
    scale = max(width / tw, height / th)
    dw, dh = tw * scale, th * scale
    snapshot.append_scaled_texture(texture, filter_, _rect(x + (width - dw) / 2, y + (height - dh) / 2, dw, dh))


# --------------------------------------------------------------------------
# 背景


class ArtBackdrop(Gtk.Widget):
    """ぼかした絵を全面に覆うように敷き、暗くする背景。

    `ArtBackdrop(dim=((0.0, 0.26), (0.55, 0.36), (1.0, 0.56)))`
    - dim: 上から下への暗さ (位置, 黒の不透明度) の並び。
    - `set_texture(texture)`: 元の絵。ぼかしは実現後に 1 回だけ作り、前の絵から溶かして替える。
    描くのは縮めたテクスチャを引き伸ばすだけなので、上に載る時計の再描画は安い。
    """

    FADE_MS = 650

    def __init__(self, *, dim=((0.0, 0.26), (0.55, 0.36), (1.0, 0.56)), radius: float = 15.0,
                 saturation: float = 1.35, brightness: float = 0.92):
        super().__init__()
        self._dim = tuple(dim)
        self._radius = radius
        self._saturation = saturation
        self._brightness = brightness
        self._source: Gdk.Texture | None = None
        self._ground: tuple[Gdk.RGBA, Gdk.RGBA] | None = None
        self._blurred: Gdk.Texture | None = None
        self._previous: Gdk.Texture | None = None
        self._fade = 1.0
        self._anim = None
        self._idle = 0
        self.set_can_target(False)
        self.add_css_class("music-backdrop")
        self.connect("realize", lambda *_: self._schedule())
        self.connect("unrealize", lambda *_: self._drop())

    @property
    def blurred(self) -> Gdk.Texture | None:
        """いま描いているぼかした絵 (試験と撮影用)。"""
        return self._blurred

    def set_texture(self, texture: Gdk.Texture | None,
                    ground: tuple[Gdk.RGBA, Gdk.RGBA] | None = None) -> None:
        """元の絵。ground (上の色, 下の色) を渡すと絵の代わりにその色の地をぼかす。"""
        if texture is self._source and ground == self._ground:
            return
        self._source = texture
        self._ground = ground
        self._schedule()

    def _drop(self) -> None:
        # 描画器ごとのテクスチャなので、別の窓へ移ったときは作り直す
        self._blurred = None
        self._previous = None
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0

    def _schedule(self) -> None:
        if self._idle or not self.get_realized():
            return
        self._idle = GLib.idle_add(self._rebuild)

    def _rebuild(self) -> bool:
        self._idle = 0
        blurred = blurred_texture(self, self._source, radius=self._radius, saturation=self._saturation,
                                  brightness=self._brightness, ground=self._ground)
        old = self._blurred
        self._blurred = blurred
        if old is not None and blurred is not None and self.get_mapped() and _animations_enabled(self):
            self._previous = old
            self._fade = 0.0
            if self._anim is None:
                target = Adw.CallbackAnimationTarget.new(self._on_fade)
                self._anim = Adw.TimedAnimation.new(self, 0.0, 1.0, self.FADE_MS, target)
                self._anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
                self._anim.connect("done", self._on_fade_done)
            self._anim.play()
        else:
            self._previous = None
            self._fade = 1.0
        self.queue_draw()
        return GLib.SOURCE_REMOVE

    def _on_fade(self, value: float) -> None:
        self._fade = value
        self.queue_draw()

    def _on_fade_done(self, *_args) -> None:
        self._previous = None
        self._fade = 1.0
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        return 0, 0, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        bounds = _rect(0, 0, width, height)
        snapshot.append_color(_rgba("rgb(28,29,33)"), bounds)
        snapshot.push_clip(bounds)
        if self._blurred is not None:
            if self._previous is not None and self._fade < 1.0:
                snapshot.push_cross_fade(self._fade)
                _append_cover(snapshot, self._previous, 0, 0, width, height)
                snapshot.pop()
                _append_cover(snapshot, self._blurred, 0, 0, width, height)
                snapshot.pop()
            else:
                _append_cover(snapshot, self._blurred, 0, 0, width, height)
        snapshot.pop()
        stops = _stops(*((offset, f"rgba(0,0,0,{alpha})") for offset, alpha in self._dim))
        snapshot.append_linear_gradient(bounds, _point(0, 0), _point(0, height), stops)

    def do_dispose(self) -> None:
        if getattr(self, "_idle", 0):
            GLib.source_remove(self._idle)
            self._idle = 0
        anim = getattr(self, "_anim", None)
        if anim is not None:
            anim.reset()
            self._anim = None
        Gtk.Widget.do_dispose(self)


# --------------------------------------------------------------------------
# アートワーク (ラジオ局は favicon を色の地の中央に)


class CoverArt(Artwork):
    """曲の絵。ラジオ局 (live) のときは名前から決まる色の地の中央に favicon を置く
    (favicon は小さく荒いものが多く、全面に引き伸ばすと崩れるため)。

    `CoverArt(size, radius=12, shadow=True)`、`show(loader, track_or_None)`。
    """

    def __init__(self, size: int, radius: float = 12, shadow: bool = True, outline: bool = True):
        super().__init__(size, size, radius=radius, shadow=shadow, outline=outline)
        self._station = False
        self._ground = (_rgba("#4a9ff0"), _rgba("#1c5cb5"))
        self._shown_key: str | None = None

    def show(self, loader, track: Track | None) -> None:
        key = track_key(track) if track is not None else ""
        if key == self._shown_key:
            return
        self._shown_key = key
        if loader is None:
            self.clear()
            return
        if track is None:
            self._station = False
            self.set_subject(loader, ("placeholder", "cliamp-music:none"), kind="track")
            return
        self._station = bool(track.live)
        if self._station:
            self._ground = station_ground(track)
            self.set_subject(loader, track, key_for_placeholder=key, kind="station")
        else:
            self.set_subject(loader, track, key_for_placeholder=key, kind="track")

    def _slot(self, width: float, height: float) -> float:
        return round(min(width, height) * 0.46)

    def _is_small(self, texture: Gdk.Texture | None, width: float) -> bool:
        return texture is not None and texture.get_width() < width * self.get_scale_factor() * 0.5

    def _set_texture(self, texture, fade: bool) -> None:
        # 局の favicon が取れなかったときの代わりの絵 (色の四角) は重ねず、記号を描く
        if self._station and texture is not None and self._loader is not None and self._requested_px:
            try:
                if texture is self._loader.placeholder(self._key, self._requested_px, "station"):
                    texture = None
            except Exception:  # 見分けられなくても届いた絵を出すだけ
                pass
        Artwork._set_texture(self, texture, fade)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if not self._station or width <= 0 or height <= 0:
            Artwork.do_snapshot(self, snapshot)
            return
        texture = self._texture
        if texture is not None and not self._is_small(texture, width):
            Artwork.do_snapshot(self, snapshot)  # 大きな局の絵はそのまま覆う
            return
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(_rect(0, 0, width, height), min(self._radius, width / 2))
        if self._shadow:
            self._append_shadow(snapshot, rounded)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_linear_gradient(_rect(0, 0, width, height), _point(0, 0), _point(0, height),
                                        [self._stop(0.0, self._ground[0]), self._stop(1.0, self._ground[1])])
        slot = self._slot(width, height)
        x, y = (width - slot) / 2, (height - slot) / 2
        if texture is not None:
            inner = Gsk.RoundedRect()
            inner.init_from_rect(_rect(x, y, slot, slot), slot * 0.16)
            snapshot.append_outset_shadow(inner, _rgba("rgba(0,0,0,0.30)"), 0, 3, 0, 8)
            snapshot.push_rounded_clip(inner)
            snapshot.append_color(_rgba("white"), _rect(x, y, slot, slot))
            _append_cover(snapshot, texture, x, y, slot, slot, Gsk.ScalingFilter.TRILINEAR)
            snapshot.pop()
        else:
            self._append_glyph(snapshot, width, height)
        snapshot.pop()
        if self._outline:
            self._append_outline(snapshot, rounded)

    @staticmethod
    def _stop(offset: float, color: Gdk.RGBA) -> Gsk.ColorStop:
        stop = Gsk.ColorStop()
        stop.offset = offset
        stop.color = color
        return stop

    def _append_glyph(self, snapshot: Gtk.Snapshot, width: float, height: float) -> None:
        display = self.get_display()
        if display is None:
            return
        px = round(min(width, height) * 0.34)
        theme = Gtk.IconTheme.get_for_display(display)
        paintable = theme.lookup_icon("music-station-symbolic", None, px, self.get_scale_factor(),
                                      Gtk.TextDirection.NONE, 0)
        snapshot.save()
        snapshot.translate(_point((width - px) / 2, (height - px) / 2))
        paintable.snapshot_symbolic(snapshot, px, px, [_rgba("rgba(255,255,255,0.88)")])
        snapshot.restore()


# --------------------------------------------------------------------------
# 再生位置の線


class Scrubber(Gtk.Widget):
    """細い線のつまみ (再生位置・音量)。

    `Scrubber(thickness=4, hover_thickness=6, height=16, knob=False)`
    - 塗りの色は CSS の color。溝は同じ色を薄くしたもの。
    - 押すかドラッグで位置を選ぶ。ドラッグ中は `on_scrub(fraction)`、離したとき
      `on_seek(fraction)` を呼ぶ (どちらも省略可)。←/→ は `on_step(±1)`。
    - `set_fraction(f)`: 外から位置を入れる (ドラッグ中は無視)。
    - `set_interactive(bool)`: 押せるか (ライブ配信や拡張の無い cliamp では押せない)。
    - knob: 乗せたとき・ドラッグ中に丸いつまみを出す (音量)。
    """

    def __init__(self, *, thickness: float = 4, hover_thickness: float = 6, height: int = 16,
                 knob: bool = False, label: str = ""):
        super().__init__(accessible_role=Gtk.AccessibleRole.SLIDER)
        self._thickness = float(thickness)
        self._hover_thickness = float(hover_thickness)
        self._height = int(height)
        self._knob = bool(knob)
        self._fraction = 0.0
        self._drag_fraction = 0.0
        self._dragging = False
        self._hover = False
        self._interactive = True
        self.on_scrub = None
        self.on_seek = None
        self.on_step = None
        self.add_css_class("music-scrubber")
        self.set_focusable(True)
        if label:
            self.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self.update_property([Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX],
                             [0.0, 100.0])

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", self._on_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_enter)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    # 状態 -----------------------------------------------------------------

    @property
    def fraction(self) -> float:
        return self._drag_fraction if self._dragging else self._fraction

    @property
    def dragging(self) -> bool:
        return self._dragging

    @property
    def interactive(self) -> bool:
        return self._interactive

    def set_fraction(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, float(fraction or 0.0)))
        if self._dragging or abs(fraction - self._fraction) < 1e-5:
            self._fraction = fraction
            return
        old_px = round(self._fraction * max(1, self.get_width()) * 2)
        self._fraction = fraction
        if round(fraction * max(1, self.get_width()) * 2) != old_px:
            self.queue_draw()
            self.update_property([Gtk.AccessibleProperty.VALUE_NOW], [round(fraction * 100, 1)])

    def set_interactive(self, interactive: bool) -> None:
        interactive = bool(interactive)
        if interactive == self._interactive:
            return
        self._interactive = interactive
        self.set_focusable(interactive)
        if not interactive and self._dragging:
            self._dragging = False
        _toggle_class(self, "static", not interactive)
        self.queue_draw()

    # 入力 -----------------------------------------------------------------

    def _fraction_at(self, x: float) -> float:
        width = self.get_width()
        return min(1.0, max(0.0, x / width)) if width > 0 else 0.0

    def _on_begin(self, gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
        if not self._interactive:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        # 窓の移動 (Gtk.WindowHandle) より先に取る
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._dragging = True
        self._drag_fraction = self._fraction_at(x)
        self.queue_draw()
        if self.on_scrub is not None:
            self.on_scrub(self._drag_fraction)

    def _on_update(self, gesture: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        if not self._dragging:
            return
        ok, x, _y = gesture.get_start_point()
        self._drag_fraction = self._fraction_at(x + dx)
        self.queue_draw()
        if self.on_scrub is not None:
            self.on_scrub(self._drag_fraction)

    def _on_end(self, gesture: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        if not self._dragging:
            return
        ok, x, _y = gesture.get_start_point()
        fraction = self._fraction_at(x + dx)
        self._dragging = False
        self._fraction = fraction
        self.queue_draw()
        if self.on_seek is not None:
            self.on_seek(fraction)

    def _on_enter(self, *_args) -> None:
        self._hover = True
        self.queue_draw()

    def _on_leave(self, *_args) -> None:
        self._hover = False
        self.queue_draw()

    def _on_key(self, _controller, keyval: int, _keycode: int, _state) -> bool:
        if not self._interactive or self.on_step is None:
            return False
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Down, Gdk.KEY_KP_Left):
            self.on_step(-1)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_KP_Right):
            self.on_step(1)
            return True
        return False

    # 寸法と描画 -----------------------------------------------------------

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 40, 120, -1, -1
        return self._height, self._height, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        active = self._interactive and (self._hover or self._dragging)
        thickness = self._hover_thickness if active else self._thickness
        y = (height - thickness) / 2
        color = self.get_color()
        trough = Gdk.RGBA()
        trough.red, trough.green, trough.blue = color.red, color.green, color.blue
        trough.alpha = color.alpha * 0.30
        bar = Gsk.RoundedRect()
        bar.init_from_rect(_rect(0, y, width, thickness), thickness / 2)
        snapshot.push_rounded_clip(bar)
        snapshot.append_color(trough, _rect(0, y, width, thickness))
        filled = width * self.fraction
        if filled > 0:
            snapshot.append_color(color, _rect(0, y, filled, thickness))
        snapshot.pop()
        if self._knob and active:
            radius = 7.0
            cx = min(width - radius, max(radius, filled))
            knob = Gsk.RoundedRect()
            knob.init_from_rect(_rect(cx - radius, height / 2 - radius, radius * 2, radius * 2), radius)
            snapshot.append_outset_shadow(knob, _rgba("rgba(0,0,0,0.35)"), 0, 1, 0, 3)
            snapshot.push_rounded_clip(knob)
            snapshot.append_color(_rgba("white"), _rect(cx - radius, height / 2 - radius, radius * 2, radius * 2))
            snapshot.pop()


# --------------------------------------------------------------------------
# 操作の列


def _transport_button(icon: str, tooltip: str, size: int, glyph: int, *classes: str) -> Gtk.Button:
    button = Gtk.Button()
    image = Gtk.Image.new_from_icon_name(icon)
    image.set_pixel_size(glyph)
    button.set_child(image)
    button.add_css_class("music-transport")
    for cls in classes:
        button.add_css_class(cls)
    button.set_size_request(size, size)
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text(tooltip)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return button


def _on_shuffle(_button, store) -> None:
    store.set_shuffle(not store.status.shuffle)


def _on_repeat(_button, store) -> None:
    store.cycle_repeat()


def _on_prev(_button, store) -> None:
    store.prev()


def _on_next(_button, store) -> None:
    store.next()


def _on_play(_button, store) -> None:
    store.toggle()


class TransportRow(Gtk.CenterBox):
    """シャッフル | 前へ 再生/一時停止 次へ | リピート。

    `TransportRow(store, size="large"|"medium"|"small")`
    シャッフルは左端、リピートは右端、残りは中央 (Apple のフルスクリーンと同じ)。
    押したときは store を直接操作する。`update(status)` で見た目を合わせる。
    """

    # (端の丸の直径, 端の記号, 送りの幅, 送りの記号, 再生の幅, 再生の記号, 中央の間隔)
    SIZES = {
        "xlarge": (40, 21, 60, 36, 72, 46, 32),
        "large": (34, 18, 50, 30, 60, 38, 26),
        "medium": (28, 15, 38, 24, 44, 30, 16),
        "small": (24, 14, 32, 19, 34, 23, 10),
    }

    def __init__(self, store, size: str = "large"):
        super().__init__()
        self.add_css_class("music-transport-row")
        self._size = ""
        self._playing = None
        self._repeat = None
        self.shuffle = _transport_button("music-shuffle-symbolic", "シャッフル", 0, 0, "side")
        self.repeat = _transport_button("music-repeat-symbolic", "リピート", 0, 0, "side")
        self.prev = _transport_button("music-previous-symbolic", "前へ", 0, 0, "skip")
        self.play = _transport_button("music-play-symbolic", "再生", 0, 0, "play")
        self.next = _transport_button("music-next-symbolic", "次へ", 0, 0, "skip")
        # 閉包に self を入れない (PyGObject は自分を掴む閉包を持つ部品を解放しない)
        self.shuffle.connect("clicked", _on_shuffle, store)
        self.repeat.connect("clicked", _on_repeat, store)
        self.prev.connect("clicked", _on_prev, store)
        self.play.connect("clicked", _on_play, store)
        self.next.connect("clicked", _on_next, store)
        self._center = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self._center.set_halign(Gtk.Align.CENTER)
        self._center.append(self.prev)
        self._center.append(self.play)
        self._center.append(self.next)
        self.set_start_widget(self.shuffle)
        self.set_center_widget(self._center)
        self.set_end_widget(self.repeat)
        self.set_size(size)

    @property
    def size(self) -> str:
        return self._size

    def set_size(self, size: str) -> None:
        """大きさの段を替える (フルスクリーンの狭い窓では "medium")。"""
        if size not in self.SIZES or size == self._size:
            return
        if self._size:
            self.remove_css_class(self._size)
        self._size = size
        self.add_css_class(size)
        side, side_glyph, skip, skip_glyph, play, play_glyph, spacing = self.SIZES[size]
        for button, box, glyph in ((self.shuffle, side, side_glyph), (self.repeat, side, side_glyph),
                                   (self.prev, skip, skip_glyph), (self.next, skip, skip_glyph),
                                   (self.play, play, play_glyph)):
            button.set_size_request(box, box)
            button.get_child().set_pixel_size(glyph)
        self._center.set_spacing(spacing)

    def update(self, status: Status) -> None:
        online = status.state != "offline"
        has_list = online and (status.total > 0 or status.track is not None)
        playing = status.state == "playing" or (status.state == "stopped" and status.buffering)
        if playing != self._playing:
            self._playing = playing
            image = self.play.get_child()
            image.set_from_icon_name("music-pause-symbolic" if playing else "music-play-symbolic")
            text = "一時停止" if playing else "再生"
            self.play.set_tooltip_text(text)
            self.play.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self.play.set_sensitive(online)
        self.prev.set_sensitive(has_list)
        self.next.set_sensitive(has_list)
        self.shuffle.set_sensitive(online)
        self.repeat.set_sensitive(online)
        _toggle_class(self.shuffle, "on", status.shuffle)
        self.shuffle.set_tooltip_text("シャッフル: オン" if status.shuffle else "シャッフル: オフ")
        if status.repeat != self._repeat:
            self._repeat = status.repeat
            self.repeat.get_child().set_from_icon_name(
                "music-repeat-one-symbolic" if status.repeat == "one" else "music-repeat-symbolic")
            self.repeat.set_tooltip_text({"off": "リピート: オフ", "all": "リピート: すべて",
                                          "one": "リピート: 1 曲"}.get(status.repeat, "リピート"))
        _toggle_class(self.repeat, "on", status.repeat != "off")


# --------------------------------------------------------------------------
# 音量 (右上のガラス)


def _device_popup(button: Gtk.MenuButton, store) -> None:
    """出力先のメニューを開くたびに作る (一覧は非同期で埋める)。"""
    menu = Gio.Menu()
    menu.append("読み込み中…", "vol.none")
    button.set_menu_model(menu)
    group = Gio.SimpleActionGroup()
    select = Gio.SimpleAction.new_stateful("device", GLib.VariantType.new("s"), GLib.Variant.new_string(""))

    def on_change(action, value) -> None:
        action.set_state(value)
        store.set_device(value.get_string())

    select.connect("change-state", on_change)
    group.add_action(select)
    button.insert_action_group("vol", group)

    def done(devices, error: str = "") -> None:
        menu.remove_all()
        if not devices:
            menu.append(error or "出力先を取得できません", "vol.none")
            return
        for device in devices:
            # 見出しは説明 (無ければ sink 名から作ったもの)。切り替えには sink 名を送る
            item = Gio.MenuItem.new(device.label.replace("_", "__"), None)
            item.set_action_and_target_value("vol.device", GLib.Variant.new_string(device.name))
            menu.append_item(item)
            if device.active:
                select.set_state(GLib.Variant.new_string(device.name))

    store.list_devices(done)


class VolumeControl(GlassCapsule):
    """出力先 | 音量の線 | スピーカーの記号 (フルスクリーン右上のガラスのカプセル)。

    `VolumeControl(store)`、`update(status)`。つまみは dB に比例 (-30〜+6 dB)。
    """

    def __init__(self, store):
        super().__init__(spacing=4)
        self.add_css_class("music-volume-capsule")
        self._store = store
        self.output = Gtk.MenuButton()
        self.output.set_icon_name("music-output-symbolic")
        self.output.set_tooltip_text("出力先")
        self.output.add_css_class("music-volume-output")
        self.output.set_valign(Gtk.Align.CENTER)
        self.output.set_create_popup_func(_device_popup, store)
        self.append(self.output)
        self.slider = Scrubber(thickness=4, hover_thickness=5, height=24, knob=True, label="音量")
        self.slider.set_size_request(110, -1)
        self.slider.set_valign(Gtk.Align.CENTER)
        self.slider.add_css_class("volume")
        self.slider.on_scrub = self._set_fraction
        self.slider.on_seek = self._set_fraction
        self.slider.on_step = self._step
        self.append(self.slider)
        self.speaker = Gtk.Image.new_from_icon_name("music-volume-symbolic")
        self.speaker.set_pixel_size(15)
        self.speaker.add_css_class("music-volume-speaker")
        self.speaker.set_margin_start(4)
        self.speaker.set_margin_end(8)
        self.append(self.speaker)

    def _set_fraction(self, fraction: float) -> None:
        self._store.set_volume_db(round(fraction_to_volume(fraction), 1))

    def _step(self, direction: int) -> None:
        self._store.volume_step(2.0 * direction)

    def update(self, status: Status) -> None:
        online = status.state != "offline"
        self.set_sensitive(online)
        self.slider.set_fraction(volume_fraction(status.volume))
        self.speaker.set_from_icon_name("music-volume-mute-symbolic" if status.volume <= VOLUME_MIN_DB + 0.01
                                        else "music-volume-symbolic")


# --------------------------------------------------------------------------
# 歌詞


class _Line:
    __slots__ = ("layout", "top", "height", "alpha", "blur", "scroll", "gap")

    def __init__(self, layout: Pango.Layout, gap: bool):
        self.layout = layout
        self.top = 0.0
        self.height = 0.0
        self.alpha = 0.4
        self.blur = 0.0
        self.scroll = 0.0
        self.gap = gap


class LyricsView(Gtk.Widget):
    """大きな歌詞 (フルスクリーンの右側)。

    - 同期した歌詞: 今の行だけ明るく、離れるほど薄くぼかす。今の行を上から
      ANCHOR (2 割) の位置に保ち、行ごとに少しずつ遅れて滑らかに送る (下の行ほど遅い)。
      行を押すと `on_seek(秒)`。乗せた行は明るくなる。ホイールで自由に送れ、
      しばらく触らなければ今の行へ戻る。
    - 同期していない歌詞: 全行同じ明るさで、ホイールで送るだけ。
    文字は自分で Pango で描く (子の部品を持たない。行ごとのぼかしと送りを
    1 か所で描き、押した位置の判定も自分で行う)。

    `set_lyrics(lyrics_or_None)`, `set_position(seconds)`, `set_font_px(px)`,
    `current_index`, `line_at(y) -> int`, `line_time(i) -> float | None`,
    `line_top(i) -> float | None`, `seek_at(y) -> float | None` (押したのと同じ)。
    """

    ANCHOR = 0.2
    PAD_X = 16
    MANUAL_SECS = 3.0
    TAU_ALPHA = 0.16
    TAU_SCROLL = 0.18
    LAG_PER_LINE = 0.035

    def __init__(self):
        super().__init__(accessible_role=Gtk.AccessibleRole.GROUP)
        self.add_css_class("music-fs-lyrics")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["歌詞"])
        self.on_seek = None
        self._lyrics: Lyrics | None = None
        self._lines: list[_Line] = []
        self._font_px = 34.0
        self._built_width = -1
        self._built_height = -1
        self._current = -1
        self._position = 0.0
        self._target = 0.0
        self._hover = -1
        self._manual_until = 0.0
        self._manual = False
        self._tick_id = 0
        self._last_frame = 0.0
        self._jump = True

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_released)
        self.add_controller(click)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        self.connect("unmap", lambda *_: self._stop_tick())

    # 公開 -----------------------------------------------------------------

    @property
    def lyrics(self) -> Lyrics | None:
        return self._lyrics

    @property
    def synced(self) -> bool:
        return bool(self._lyrics and self._lyrics.synced)

    @property
    def current_index(self) -> int:
        return self._current

    def set_lyrics(self, lyrics: Lyrics | None) -> None:
        self._lyrics = lyrics if lyrics is not None and lyrics.lines else None
        self._lines = []
        self._built_width = -1
        self._current = -1
        self._hover = -1
        self._manual = False
        self._manual_until = 0.0
        self._jump = True
        if self._lyrics is not None and self._lyrics.synced:
            self._current = self._lyrics.index_at(self._position)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [self._accessible_text()])
        self._relayout()

    def _relayout(self) -> None:
        """行を組み直す。幅が分かっていればすぐ、分からなければ次の割り当てで。"""
        width, height = self.get_width(), self.get_height()
        if width > 0:
            self._build(width, height)
            self._jump = True
            self._retarget()
        self.queue_allocate()
        self.queue_draw()

    def set_font_px(self, px: float) -> None:
        if abs(px - self._font_px) < 0.1:
            return
        self._font_px = float(px)
        self._built_width = -1
        self._relayout()

    def set_position(self, seconds: float) -> None:
        """再生位置 (秒)。今の行が変わったら送る。"""
        self._position = max(0.0, float(seconds or 0.0))
        if self._manual and time.monotonic() > self._manual_until:
            self._manual = False
            self._retarget()
        if not self.synced:
            return
        index = self._lyrics.index_at(self._position)
        if index != self._current:
            self._current = index
            self.update_property([Gtk.AccessibleProperty.DESCRIPTION], [self._accessible_text()])
            self._retarget()

    def line_time(self, index: int) -> float | None:
        if self._lyrics is None or not 0 <= index < len(self._lyrics.lines):
            return None
        return self._lyrics.lines[index].t

    def line_at(self, y: float) -> int:
        """ウィジェットの y にある行 (行の間の余白は近い方)。無ければ -1。"""
        if not self._lines:
            return -1
        half_gap = self._gap() / 2
        for i, line in enumerate(self._lines):
            top = line.top - line.scroll
            if top - half_gap <= y < top + line.height + half_gap:
                return i
        return -1

    def line_top(self, index: int) -> float | None:
        """行の上端のいまの y (試験と撮影用)。"""
        if not 0 <= index < len(self._lines):
            return None
        line = self._lines[index]
        return line.top - line.scroll

    def _accessible_text(self) -> str:
        if self._lyrics is None:
            return ""
        if self.synced and 0 <= self._current < len(self._lyrics.lines):
            return self._lyrics.lines[self._current].text
        return "\n".join(line.text for line in self._lyrics.lines[:3])

    # 行の組み立て -----------------------------------------------------------

    def _gap(self) -> float:
        return round(self._font_px * 0.62)

    def _font(self, scale: float = 1.0) -> Pango.FontDescription:
        context = self.get_pango_context()
        base = context.get_font_description() if context is not None else None
        desc = base.copy() if base is not None else Pango.FontDescription.from_string("Sans")
        desc.set_absolute_size(self._font_px * scale * Pango.SCALE)
        desc.set_weight(Pango.Weight.ULTRABOLD)
        return desc

    def _build(self, width: int, height: int) -> None:
        self._built_width = width
        self._built_height = height
        self._lines = []
        if self._lyrics is None:
            return
        text_width = max(60, width - 2 * self.PAD_X)
        font = self._font()
        small = self._font(0.62)
        top = 0.0
        gap = self._gap()
        for entry in self._lyrics.lines:
            text = entry.text.strip()
            layout = self.create_pango_layout(text or "• • •")
            layout.set_font_description(font if text else small)
            layout.set_width(int(text_width * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            _ink, logical = layout.get_pixel_extents()
            line = _Line(layout, gap=not text)
            line.top = top
            line.height = float(max(1, logical.height))
            top += line.height + gap
            self._lines.append(line)

    def _anchor(self) -> float:
        height = max(1, self.get_height())
        return max(56.0, height * self.ANCHOR)

    def _scroll_bounds(self) -> tuple[float, float]:
        if not self._lines:
            return 0.0, 0.0
        height = max(1, self.get_height())
        return self._lines[0].top - height * 0.6, self._lines[-1].top - self._anchor() * 0.5

    def _auto_target(self) -> float:
        if not self._lines:
            return 0.0
        if not self.synced:
            return self._lines[0].top - max(40.0, self.get_height() * 0.14)
        index = min(max(self._current, 0), len(self._lines) - 1)
        return self._lines[index].top - self._anchor()

    def _targets(self, i: int) -> tuple[float, float]:
        """行 i の (不透明度, ぼかし)。"""
        if not self.synced:
            return (0.92 if i == self._hover else 0.8), 0.0
        if self._manual:
            if i == self._current:
                return 1.0, 0.0
            return (0.85 if i == self._hover else 0.5), 0.0
        distance = abs(i - self._current) if self._current >= 0 else i + 1
        if distance == 0:
            return 1.0, 0.0
        if i == self._hover:
            return 0.78, 0.0
        alpha = (0.44, 0.38, 0.33, 0.29)[min(distance, 4) - 1]
        blur = (1.4, 2.4, 3.3, 4.2)[min(distance, 4) - 1]
        if i < self._current:
            # 歌い終えた行は、これから来る行より早く薄れる
            alpha *= 0.72
            blur += 0.8
        return alpha, blur

    def _retarget(self) -> None:
        if not self._lines:
            self.queue_draw()
            return
        if not self._manual:
            self._target = self._auto_target()
        if self._jump or not self.get_mapped() or not _animations_enabled(self):
            self._jump = False
            for i, line in enumerate(self._lines):
                line.alpha, line.blur = self._targets(i)
                line.scroll = self._target
            self._stop_tick()
            self.queue_draw()
            return
        self._start_tick()

    # アニメーション -----------------------------------------------------------

    def _start_tick(self) -> None:
        if not self._tick_id:
            self._last_frame = 0.0
            self._tick_id = self.add_tick_callback(self._on_tick)

    def _stop_tick(self) -> None:
        if getattr(self, "_tick_id", 0):
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_tick(self, _widget, clock: Gdk.FrameClock) -> bool:
        now = clock.get_frame_time() / 1_000_000
        dt = min(0.05, now - self._last_frame) if self._last_frame else 1 / 60
        self._last_frame = now
        moving = False
        base = max(self._current, 0)
        for i, line in enumerate(self._lines):
            alpha, blur = self._targets(i)
            k = 1.0 - math.exp(-dt / self.TAU_ALPHA)
            line.alpha += (alpha - line.alpha) * k
            line.blur += (blur - line.blur) * k
            lag = 0.0 if self._manual else self.LAG_PER_LINE * min(6, max(0, i - base + 1))
            ks = 1.0 - math.exp(-dt / (self.TAU_SCROLL + lag))
            line.scroll += (self._target - line.scroll) * ks
            if abs(alpha - line.alpha) > 0.004 or abs(blur - line.blur) > 0.03:
                moving = True
            if abs(self._target - line.scroll) > 0.4:
                moving = True
        self.queue_draw()
        if not moving:
            for i, line in enumerate(self._lines):
                line.alpha, line.blur = self._targets(i)
                line.scroll = self._target
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # 入力 -----------------------------------------------------------------

    def seek_at(self, y: float) -> float | None:
        """y の行へ飛ぶ (押したときと同じ)。同期した歌詞の行なら on_seek を呼び、その秒を返す。"""
        if not self.synced:
            return None
        index = self.line_at(y)
        seconds = self.line_time(index)
        if seconds is None:
            return None
        self._manual = False
        self._current = index
        self._position = seconds
        self._retarget()
        if self.on_seek is not None:
            self.on_seek(seconds)
        return seconds

    def _on_released(self, gesture: Gtk.GestureClick, _n: int, _x: float, y: float) -> None:
        if self.seek_at(y) is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _on_motion(self, _controller, _x: float, y: float) -> None:
        index = self.line_at(y) if self.synced else -1
        if index != self._hover:
            self._hover = index
            self.set_cursor_from_name("pointer" if index >= 0 else None)
            self._retarget()

    def _on_leave(self, *_args) -> None:
        if self._hover != -1:
            self._hover = -1
            self.set_cursor_from_name(None)
            self._retarget()

    def _on_scroll(self, controller: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if not self._lines:
            return False
        step = dy * 48.0 if controller.get_unit() == Gdk.ScrollUnit.WHEEL else dy
        low, high = self._scroll_bounds()
        self._target = min(high, max(low, self._target + step))
        self._manual = True
        self._manual_until = time.monotonic() + self.MANUAL_SECS
        for line in self._lines:
            line.scroll = self._target
        self._start_tick()  # 明るさ (ぼかしを解く) を溶かす
        self.queue_draw()
        return True

    # 寸法と描画 -----------------------------------------------------------

    def do_measure(self, orientation, for_size):
        return 0, 0, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if width != self._built_width:
            self._build(width, height)
            self._jump = True
        elif height != self._built_height:
            self._built_height = height
            self._jump = True
        if self._jump:
            self._retarget()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0 or not self._lines:
            return
        bounds = _rect(0, 0, width, height)
        fade = min(0.16, 90.0 / height)
        snapshot.push_mask(Gsk.MaskMode.ALPHA)
        snapshot.append_linear_gradient(bounds, _point(0, 0), _point(0, height), _stops(
            (0.0, "rgba(0,0,0,0)"), (fade, "rgba(0,0,0,1)"), (1.0 - fade, "rgba(0,0,0,1)"),
            (1.0, "rgba(0,0,0,0)")))
        snapshot.pop()
        white = _rgba("white")
        hover_fill = _rgba("rgba(255,255,255,0.07)")
        for i, line in enumerate(self._lines):
            y = line.top - line.scroll
            if y + line.height < -60 or y > height + 60:
                continue
            if i == self._hover and self.synced:
                pad = 8.0
                rounded = Gsk.RoundedRect()
                rounded.init_from_rect(_rect(2, y - pad, width - 4, line.height + 2 * pad), 14)
                snapshot.push_rounded_clip(rounded)
                snapshot.append_color(hover_fill, _rect(2, y - pad, width - 4, line.height + 2 * pad))
                snapshot.pop()
            snapshot.save()
            snapshot.translate(_point(self.PAD_X, y))
            blurred = line.blur > 0.3
            if blurred:
                snapshot.push_blur(line.blur)
            faded = line.alpha < 0.995
            if faded:
                snapshot.push_opacity(max(0.0, line.alpha))
            snapshot.append_layout(line.layout, white)
            if faded:
                snapshot.pop()
            if blurred:
                snapshot.pop()
            snapshot.restore()
        snapshot.pop()

    def do_dispose(self) -> None:
        self._stop_tick()
        Gtk.Widget.do_dispose(self)


# --------------------------------------------------------------------------
# 次に再生


def _play_row(row) -> None:
    ctx = getattr(row, "ctx", None)
    if ctx is not None and row.index is not None:
        ctx.store.play_index(row.index, path=row.track.path if row.track is not None else None)


def _play_queued(row) -> None:
    """待ち行列の行: その曲より前の待ち行列を外して next (play_index は待ち行列から外さない)。"""
    ctx = getattr(row, "ctx", None)
    if ctx is not None and row.index is not None:
        ctx.store.play_queued(row.index)


def _clear_queue(_button, store) -> None:
    store.queue_edit("clear")


def _dispose_children(box: Gtk.Box) -> None:
    """作り直す一覧の子を外す。

    widgets.TrackRow は自分を掴む閉包を子に渡さなくなったので、外すだけで解放される。
    run_dispose で木ごと後始末してはいけない: 後で本当に解放されるときにもう一度
    dispose が走り、Gtk.MenuButton が create_popup_func の後始末を 2 度呼んで
    (libffi の二重解放) プロセスごと落ちる。
    """
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


class UpNextView(Gtk.Box):
    """フルスクリーンの「次に再生」。

    上に「シャッフル」「リピート」の横長のカプセル。その下に「次に再生」
    (待ち行列。見出しの右に赤い「消去」)、続いて今のリストから次に来る曲。
    行のダブルクリックでその曲を再生する (待ち行列の曲は store.play_queued)。
    シャッフルとリピート (すべて) で一巡の後も続くときは、そう書き添える (up_next は
    今の一巡の残りだけ)。`refresh()` で store から作り直す (中身が同じなら作り直さない)。
    `update_status(status)` でカプセルを合わせる。
    """

    MAX_ROWS = 120

    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.add_css_class("music-fs-queue")
        self._signature = None

        pills = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=True)
        pills.add_css_class("music-fs-pills")
        self.shuffle_pill = self._pill("シャッフル", "music-shuffle-symbolic")
        self.repeat_pill = self._pill("リピート", "music-repeat-symbolic")
        store = ctx.store
        self.shuffle_pill.connect("clicked", _on_shuffle, store)
        self.repeat_pill.connect("clicked", _on_repeat, store)
        pills.append(self.shuffle_pill)
        pills.append(self.repeat_pill)
        pills.set_margin_start(8)
        pills.set_margin_end(8)
        pills.set_margin_bottom(14)
        self.append(pills)

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.add_css_class("music-fs-queue-scroller")
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # 最後の行を右下の切り替えより上まで送れるように
        self._content.set_margin_bottom(64)
        self._scroller.set_child(self._content)
        self._stack.add_named(self._scroller, "list")
        self._empty = EmptyState("music-queue-symbolic", "次に再生する曲はありません")
        self._stack.add_named(self._empty, "empty")
        self.append(self._stack)

    @staticmethod
    def _pill(text: str, icon: str) -> Gtk.Button:
        button = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        box.set_halign(Gtk.Align.CENTER)
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(15)
        box.append(image)
        box.append(_label(text, "music-fs-pill-label", ellipsize=False))
        button.set_child(box)
        button.add_css_class("music-fs-pill")
        return button

    def update_status(self, status: Status) -> None:
        online = status.state != "offline"
        _toggle_class(self.shuffle_pill, "on", status.shuffle)
        _toggle_class(self.repeat_pill, "on", status.repeat != "off")
        image = self.repeat_pill.get_child().get_first_child()
        image.set_from_icon_name("music-repeat-one-symbolic" if status.repeat == "one" else "music-repeat-symbolic")
        self.shuffle_pill.set_sensitive(online)
        self.repeat_pill.set_sensitive(online)

    def refresh(self, force: bool = False) -> None:
        store = self.ctx.store
        pl = store.playlist
        status = store.status
        if not store.connected:
            signature = ("offline",)
        elif store.api < 1 or not store.supports("playlist"):
            signature = ("unsupported",)
        else:
            # シャッフルとリピートでも空のときの文と添え書きが変わる (up_next が同じでも)
            signature = (tuple((i, pl.track_at(i).path if pl.track_at(i) else "") for i in pl.queue),
                         tuple((i, pl.track_at(i).path if pl.track_at(i) else "")
                               for i in pl.up_next[: self.MAX_ROWS]),
                         pl.source.name, status.index, status.shuffle, status.repeat)
        if signature == self._signature and not force:
            return
        self._signature = signature
        _dispose_children(self._content)
        if signature == ("offline",):
            self._show_empty("cliamp に接続できません", None)
            return
        if signature == ("unsupported",):
            self._show_empty("次に再生を表示できません", UNSUPPORTED)
            return
        queue = [(i, pl.track_at(i)) for i in pl.queue if pl.track_at(i) is not None]
        upcoming = [(i, pl.track_at(i)) for i in pl.up_next[: self.MAX_ROWS] if pl.track_at(i) is not None]
        reshuffle = store.continues_by_reshuffle()
        if not queue and not upcoming:
            if reshuffle:
                self._show_empty("このあとシャッフルし直して続けて再生します",
                                 "いまのシャッフルの一巡が終わると、並びを混ぜ直して続きます。")
            else:
                self._show_empty("次に再生する曲はありません", None)
            return
        self._stack.set_visible_child_name("list")
        if queue:
            header = SectionHeader("次に再生")
            clear = Gtk.Button(label="消去")
            clear.add_css_class("music-fs-clear")
            clear.connect("clicked", _clear_queue, self.ctx.store)
            header.add_end(clear)
            self._content.append(self._section(header))
            self._content.append(self._rows(queue, "queue"))
        if upcoming:
            if queue:
                name = pl.source.name or "再生中のリスト"
                header = SectionHeader(name)
                self._content.append(self._section(header, "このあと続けて再生されます", top=22))
            else:
                header = SectionHeader("次に再生")
                self._content.append(self._section(header, pl.source.name or None))
            self._content.append(self._rows(upcoming, "nowplaying"))
        if reshuffle and len(pl.up_next) < 200:
            note = _label("このあとシャッフルし直して続けて再生します", "music-fs-queue-subtitle")
            note.set_margin_start(8)
            note.set_margin_top(14)
            self._content.append(note)

    def _show_empty(self, title: str, description: str | None) -> None:
        self._empty.set_title(title)
        self._empty.set_description(description)
        self._stack.set_visible_child_name("empty")

    @staticmethod
    def _section(header: SectionHeader, subtitle: str | None = None, top: int = 0) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        box.add_css_class("music-fs-queue-section")
        box.set_margin_top(top)
        box.set_margin_start(8)
        box.set_margin_end(8)
        box.set_margin_bottom(6)
        box.append(header)
        if subtitle:
            box.append(_label(subtitle, "music-fs-queue-subtitle"))
        return box

    def _rows(self, items, context: str) -> TrackList:
        rows = TrackList(selectable=True)
        rows.add_css_class("music-fs-rows")
        activate = _play_queued if context == "queue" else _play_row
        for index, track in items:
            rows.append(TrackRow(self.ctx, track, variant="queue", index=index, menu_context=context,
                                 on_activate=activate))
        return rows


# --------------------------------------------------------------------------
# フルスクリーンプレーヤー


def _exit_fullscreen(_button, ctx) -> None:
    window = ctx.window
    if window is not None and hasattr(window, "show_fullscreen"):
        window.show_fullscreen(False)


def _open_miniplayer(_button, ctx) -> None:
    app = ctx.app
    if app is not None and hasattr(app, "show_miniplayer"):
        app.show_miniplayer()


def _track_popup(button: Gtk.MenuButton, ctx) -> None:
    track = ctx.store.current_track()
    if track is None:
        button.set_menu_model(None)
        return
    model, group = ctx.track_menu(track)
    button.insert_action_group("track", group)
    button.set_menu_model(model)


class FullscreenPlayer(Gtk.Box):
    """フルスクリーンプレーヤー。窓の根の Gtk.Stack の "fullscreen" に置く。

    `FullscreenPlayer(ctx)`
    - `set_active(active)`: 見せている間 True。store のシグナルを受け、時計を回し、
      歌詞を取る。False で全部止める (シグナルも切る)。
    - `set_mode("lyrics" | "queue")`: 右側 (歌詞 / 次に再生)。GuiState に覚える。
    - ✕ は `ctx.window.show_fullscreen(False)`、ミニプレーヤーは `ctx.app.show_miniplayer()`。
      Esc でも戻る。
    """

    TICK_MS = 200
    # (名前, 条件, 絵の大きさ, 歌詞の文字, 操作の列の大きさ)。後ろほど優先。曲名と副題の文字は CSS。
    # large は窓 (1180x760 ほど) の大きさ。本当のフルスクリーン (1920x1080 など) では xlarge
    # (絵は高さの 4 割ほど。Apple のフルスクリーンも窓より大きく描く)、1440p 以上は xxlarge
    LEVELS = (
        ("large", None, 360, 34, "large"),
        ("xlarge", "min-width: 1400px and min-height: 900px", 440, 42, "xlarge"),
        ("xxlarge", "min-width: 2200px and min-height: 1300px", 560, 52, "xlarge"),
        ("medium", "max-width: 1060px or max-height: 720px", 300, 30, "large"),
        ("small", "max-width: 860px or max-height: 600px", 240, 26, "medium"),
    )

    def __init__(self, ctx):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.ctx = ctx
        self.add_css_class("music-fullscreen")
        self.set_focusable(True)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self._active = False
        self._handlers: list[int] = []
        self._tick_id = 0
        self._art_key: str | None = None
        self._backdrop_handle = None
        self._backdrop_token = 0
        self._lyrics_key: tuple | None = None
        self._lyrics_token = 0
        self._level = "large"
        self._mode = ctx.state.get("fullscreen_mode", "lyrics") if ctx.state is not None else "lyrics"
        if self._mode not in ("lyrics", "queue"):
            self._mode = "lyrics"

        self._bin = Adw.BreakpointBin()
        self._bin.set_size_request(360, 320)
        self._bin.set_hexpand(True)
        self._bin.set_vexpand(True)
        self.append(self._bin)
        overlay = Gtk.Overlay()
        self._bin.set_child(overlay)
        self.backdrop = ArtBackdrop()
        overlay.set_child(self.backdrop)

        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        columns.append(self._build_left())
        columns.append(self._build_right())
        overlay.add_overlay(columns)
        overlay.set_measure_overlay(columns, True)
        overlay.add_overlay(self._build_top_left())
        overlay.add_overlay(self._build_top_right())
        overlay.add_overlay(self._build_switch())

        self._breakpoints: dict[Adw.Breakpoint, str] = {}
        for name, condition, *_sizes in self.LEVELS:
            if condition is None:
                continue
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            self._bin.add_breakpoint(breakpoint)
            self._breakpoints[breakpoint] = name
        self._bin.connect("notify::current-breakpoint", self._on_breakpoint)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("map", lambda *_: self._update_ticking())
        self.connect("unmap", lambda *_: self._update_ticking())
        self.connect("unrealize", lambda *_: self.set_active(False))
        self._apply_mode(self._mode)
        self._sync_status(self.ctx.store.status)

    # 組み立て -----------------------------------------------------------------

    def _build_left(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.CENTER)
        outer.add_css_class("music-fs-left")
        self._left = outer
        self.art = CoverArt(360, radius=12, shadow=True)
        self.art.set_halign(Gtk.Align.CENTER)
        outer.append(self.art)

        info = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        info.set_margin_top(18)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        self.title_label = _label("", "music-fs-title")
        self.subtitle_label = _label("", "music-fs-subtitle")
        texts.append(self.title_label)
        texts.append(self.subtitle_label)
        info.append(texts)
        self.more = Gtk.MenuButton()
        self.more.set_icon_name("music-more-symbolic")
        self.more.set_tooltip_text("その他")
        self.more.add_css_class("music-fs-more")
        self.more.set_valign(Gtk.Align.CENTER)
        self.more.set_create_popup_func(_track_popup, self.ctx)
        info.append(self.more)
        outer.append(info)

        self.scrubber = Scrubber(thickness=4, hover_thickness=7, height=18, label="再生位置")
        self.scrubber.set_margin_top(14)
        self.scrubber.add_css_class("position")
        self.scrubber.on_scrub = self._on_scrub
        self.scrubber.on_seek = self._on_seek_fraction
        self.scrubber.on_step = self._on_seek_step
        outer.append(self.scrubber)

        times = Gtk.CenterBox()
        times.add_css_class("music-fs-times")
        self.elapsed_label = _label("", "music-fs-time", ellipsize=False)
        self.remaining_label = _label("", "music-fs-time", xalign=1.0, ellipsize=False)
        self.badge = _label("", "music-fs-badge", xalign=0.5, ellipsize=False)
        self.badge.set_visible(False)
        times.set_start_widget(self.elapsed_label)
        times.set_center_widget(self.badge)
        times.set_end_widget(self.remaining_label)
        outer.append(times)

        self.transport = TransportRow(self.ctx.store, "large")
        self.transport.set_margin_top(12)
        outer.append(self.transport)
        self._apply_level_sizes(360)
        return outer

    def _build_right(self) -> Gtk.Widget:
        self._right = Gtk.Stack()
        self._right.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._right.set_transition_duration(250)
        self._right.set_margin_end(56)
        self._right.set_margin_start(4)
        self._right.add_css_class("music-fs-right")

        self._lyrics_stack = Gtk.Stack()
        self._lyrics_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._lyrics_stack.set_transition_duration(200)
        self.lyrics = LyricsView()
        self.lyrics.on_seek = self._on_lyrics_seek
        self._lyrics_stack.add_named(self.lyrics, "lines")
        self.lyrics_message = _label("", "music-fs-lyrics-message", ellipsize=False)
        self.lyrics_message.set_wrap(True)
        self.lyrics_message.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.lyrics_message.set_valign(Gtk.Align.CENTER)
        self.lyrics_message.set_margin_start(LyricsView.PAD_X)
        self._lyrics_stack.add_named(self.lyrics_message, "message")
        self._right.add_named(self._lyrics_stack, "lyrics")

        self.queue = UpNextView(self.ctx)
        self.queue.set_margin_top(64)
        self._right.add_named(self.queue, "queue")
        return self._right

    def _build_top_left(self) -> Gtk.Widget:
        capsule = GlassCapsule()
        capsule.add_css_class("music-fs-glass")
        capsule.set_halign(Gtk.Align.START)
        capsule.set_valign(Gtk.Align.START)
        capsule.set_margin_start(14)
        capsule.set_margin_top(14)
        close = CircleButton("music-close-symbolic", "フルスクリーンを終了", size=30, flat=True)
        close.connect("clicked", _exit_fullscreen, self.ctx)
        mini = CircleButton("music-miniplayer-symbolic", "ミニプレーヤー", size=30, flat=True)
        mini.connect("clicked", _open_miniplayer, self.ctx)
        capsule.append(close)
        capsule.append(mini)
        self.close_button = close
        self.mini_button = mini
        return capsule

    def _build_top_right(self) -> Gtk.Widget:
        self.volume = VolumeControl(self.ctx.store)
        self.volume.add_css_class("music-fs-glass")
        self.volume.set_halign(Gtk.Align.END)
        self.volume.set_valign(Gtk.Align.START)
        self.volume.set_margin_end(14)
        self.volume.set_margin_top(14)
        return self.volume

    def _build_switch(self) -> Gtk.Widget:
        capsule = GlassCapsule(spacing=2)
        capsule.add_css_class("music-fs-glass")
        capsule.add_css_class("music-fs-switch")
        capsule.set_halign(Gtk.Align.END)
        capsule.set_valign(Gtk.Align.END)
        capsule.set_margin_end(14)
        capsule.set_margin_bottom(14)
        self.lyrics_toggle = self._switch_button("music-lyrics-symbolic", "歌詞")
        self.queue_toggle = self._switch_button("music-queue-symbolic", "次に再生")
        self.queue_toggle.set_group(self.lyrics_toggle)
        self.lyrics_toggle.connect("toggled", self._on_switch, "lyrics")
        self.queue_toggle.connect("toggled", self._on_switch, "queue")
        capsule.append(self.lyrics_toggle)
        capsule.append(self.queue_toggle)
        return capsule

    @staticmethod
    def _switch_button(icon: str, tooltip: str) -> Gtk.ToggleButton:
        button = Gtk.ToggleButton()
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(15)
        button.set_child(image)
        button.add_css_class("music-fs-switch-button")
        button.set_size_request(30, 30)
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        return button

    # 大きさ -----------------------------------------------------------------

    def _on_breakpoint(self, bin_, _pspec) -> None:
        current = bin_.get_current_breakpoint()
        level = self._breakpoints.get(current, "large") if current is not None else "large"
        self.set_level(level)

    def set_level(self, level: str) -> None:
        """大きさの段 ("xxlarge" / "xlarge" / "large" / "medium" / "small")。ふつうは窓の
        大きさで自動で決まる。"""
        sizes = {name: rest for name, _cond, *rest in self.LEVELS}
        if level not in sizes or level == self._level:
            return
        for name in sizes:
            _toggle_class(self, name, name == level)
        self._level = level
        art, lyric, transport = sizes[level]
        self._apply_level_sizes(art)
        self.lyrics.set_font_px(lyric)
        self.transport.set_size(transport)

    def _apply_level_sizes(self, art: int) -> None:
        self.art.set_size(art)
        self._left.set_size_request(art, -1)

    # 動かす・止める -----------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def mode(self) -> str:
        return self._mode

    def set_active(self, active: bool) -> None:
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        store = self.ctx.store
        if active:
            for name, handler in (("status-changed", self._on_status), ("track-changed", self._on_track),
                                  ("state-changed", self._on_state), ("playlist-changed", self._on_playlist),
                                  ("connection-changed", self._on_connection)):
                self._handlers.append(store.connect(name, handler))
            self._art_key = None
            self._lyrics_key = None
            self._on_connection(store)
            if self.get_mapped():
                self.grab_focus()
        else:
            for handler in self._handlers:
                store.disconnect(handler)
            self._handlers = []
            self._cancel_backdrop()
            self._lyrics_token += 1
        self._update_ticking()

    def set_mode(self, mode: str) -> None:
        if mode not in ("lyrics", "queue"):
            raise ValueError(f"mode は lyrics か queue: {mode!r}")
        if mode == self._mode and self._right.get_visible_child_name() == mode:
            return
        self._apply_mode(mode)
        state = self.ctx.state
        if state is not None:
            state.set("fullscreen_mode", mode)
            state.save()

    def _apply_mode(self, mode: str) -> None:
        self._mode = mode
        self._right.set_visible_child_name(mode)
        (self.lyrics_toggle if mode == "lyrics" else self.queue_toggle).set_active(True)
        if self._active:
            if mode == "queue":
                self.queue.refresh()
            else:
                self._refresh_lyrics()

    def _on_switch(self, button: Gtk.ToggleButton, mode: str) -> None:
        if button.get_active() and mode != self._mode:
            self.set_mode(mode)

    # store から -----------------------------------------------------------------

    def _on_connection(self, store) -> None:
        self._on_track(store)
        self._on_status(store)
        self._on_playlist(store)

    def _on_status(self, store) -> None:
        status = store.status
        self._sync_status(status)
        if self._mode == "lyrics":
            self._refresh_lyrics()  # ラジオは ICY の曲名が変わると探し直す
        self._update_ticking()

    def _on_state(self, _store) -> None:
        self._update_ticking()

    def _on_track(self, store) -> None:
        track = store.current_track()
        self.art.show(self.ctx.artwork, track)
        self._update_backdrop(track)
        if self._mode == "lyrics":
            self._refresh_lyrics()
        elif self._mode == "queue":
            self.queue.refresh()

    def _on_playlist(self, _store) -> None:
        if self._mode == "queue":
            self.queue.refresh()

    def _sync_status(self, status: Status) -> None:
        store = self.ctx.store
        track = status.track
        if status.state == "offline":
            title, subtitle = "cliamp に接続できません", ""
        elif track is None:
            title, subtitle = "再生していません", ""
        else:
            title, subtitle = status.display_title, status.display_subtitle
        _set_text(self.title_label, title)
        _set_text(self.subtitle_label, subtitle)
        self.title_label.set_tooltip_text(title or None)
        # 再生できなかった曲: 副題が理由の短文 (琥珀色)、ツールチップに全文
        _show_problem(self.subtitle_label, status.playback_problem if status.state != "offline" else None)
        self.more.set_sensitive(track is not None)
        self.transport.update(status)
        self.volume.update(status)
        self.queue.update_status(status)
        live = status.is_live
        self.scrubber.set_interactive(store.can_seek())
        self.scrubber.set_visible(not live)
        # 読み込み中は副題が「読み込み中…」になるので、印には出さない
        if live:
            self._set_badge("ライブ", "live")
        elif track is not None and abs(status.speed - 1.0) > 0.001:
            self._set_badge(f"{status.speed:g}×", "")
        else:
            self._set_badge("", "")
        self._update_times()

    def _set_badge(self, text: str, kind: str) -> None:
        _set_text(self.badge, text)
        self.badge.set_visible(bool(text))
        _toggle_class(self.badge, "live", kind == "live")

    def _update_times(self) -> None:
        store = self.ctx.store
        status = store.status
        if status.track is None or status.is_live:
            _set_text(self.elapsed_label, "")
            _set_text(self.remaining_label, "")
            self.scrubber.set_fraction(0.0)
            return
        if self.scrubber.dragging:
            return
        position = store.position_now()
        elapsed, remaining = time_texts(position, status.duration)
        _set_text(self.elapsed_label, elapsed)
        _set_text(self.remaining_label, remaining)
        self.scrubber.set_fraction(position / status.duration if status.duration > 0 else 0.0)
        self.lyrics.set_position(position)

    # 時計 -----------------------------------------------------------------

    def _update_ticking(self) -> None:
        status = self.ctx.store.status
        want = (self._active and self.get_mapped() and status.state == "playing"
                and status.track is not None)
        if want and not self._tick_id:
            self._tick_id = GLib.timeout_add(self.TICK_MS, self._on_tick)
        elif not want and self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _on_tick(self) -> bool:
        if not self._active:
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        self._update_times()
        return GLib.SOURCE_CONTINUE

    # シーク -----------------------------------------------------------------

    def _on_scrub(self, fraction: float) -> None:
        duration = self.ctx.store.status.duration
        elapsed, remaining = time_texts(fraction * duration, duration)
        _set_text(self.elapsed_label, elapsed)
        _set_text(self.remaining_label, remaining)

    def _on_seek_fraction(self, fraction: float) -> None:
        store = self.ctx.store
        duration = store.status.duration
        if duration > 0:
            store.seek_to(fraction * duration)
            self.lyrics.set_position(fraction * duration)
        self._update_times()

    def _on_seek_step(self, direction: int) -> None:
        self.ctx.store.seek_by(5.0 * direction)
        self._update_times()

    def _on_lyrics_seek(self, seconds: float) -> None:
        self.ctx.store.seek_to(seconds + 0.05)
        self._update_times()

    # 歌詞 -----------------------------------------------------------------

    def _refresh_lyrics(self) -> None:
        store = self.ctx.store
        status = store.status
        subject = lyrics_subject(status)
        if status.state == "offline":
            key = ("offline",)
        elif status.track is None:
            key = ("none",)
        elif not store.supports("lyrics"):
            key = ("unsupported",)
        elif subject is None:
            key = ("live",) if status.is_live else ("none",)
        else:
            key = ("lyrics",) + subject
        if key == self._lyrics_key:
            return
        self._lyrics_key = key
        self._lyrics_token += 1
        token = self._lyrics_token
        if key[0] != "lyrics":
            self.lyrics.set_lyrics(None)
            self._show_lyrics_message({
                "offline": "cliamp に接続できません",
                "none": "再生していません",
                "unsupported": "この cliamp では歌詞を表示できません",
                "live": "この放送の歌詞はありません",
            }[key[0]])
            return
        self._show_lyrics_message("")

        def done(lyrics: Lyrics | None) -> None:
            if token != self._lyrics_token or not self._active:
                return
            if lyrics is None or not lyrics.lines:
                self.lyrics.set_lyrics(None)
                self._show_lyrics_message("歌詞が見つかりません")
                return
            if self.ctx.store.status.is_live and lyrics.synced:
                # ラジオでは曲の中の位置が分からないので、時刻は使わずに並べるだけにする
                lyrics = Lyrics(lines=list(lyrics.lines), synced=False)
            self.lyrics.set_position(self.ctx.store.position_now())
            self.lyrics.set_lyrics(lyrics)
            self._lyrics_stack.set_visible_child_name("lines")

        self.ctx.catalog.lyrics(subject[0], subject[1], done)

    def _show_lyrics_message(self, text: str) -> None:
        _set_text(self.lyrics_message, text)
        self._lyrics_stack.set_visible_child_name("message")

    # 背景 -----------------------------------------------------------------

    def _cancel_backdrop(self) -> None:
        self._backdrop_token += 1
        handle, self._backdrop_handle = self._backdrop_handle, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # 取り消せなくても token で結果を捨てる
                pass

    def _update_backdrop(self, track: Track | None) -> None:
        key = track_key(track) if track is not None else ""
        if key == self._art_key:
            return
        self._art_key = key
        self._cancel_backdrop()
        loader = self.ctx.artwork
        if loader is None:
            return
        if track is None:
            self.backdrop.set_texture(*backdrop_source(loader, None, None, 0))
            return
        token = self._backdrop_token
        backdrop = self.backdrop
        size = 256

        def done(texture) -> None:
            if token != self._backdrop_token:
                return
            self._backdrop_handle = None
            backdrop.set_texture(*backdrop_source(loader, track, texture, size))

        handle = loader.request(track, size, done)
        if token == self._backdrop_token and self._art_key == key:
            self._backdrop_handle = handle

    # キー -----------------------------------------------------------------

    def _on_key(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if keyval == Gdk.KEY_Escape and not (state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)):
            _exit_fullscreen(None, self.ctx)
            return True
        return False

    def do_dispose(self) -> None:
        if getattr(self, "_tick_id", 0):
            GLib.source_remove(self._tick_id)
            self._tick_id = 0
        Gtk.Box.do_dispose(self)
