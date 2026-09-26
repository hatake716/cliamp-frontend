"""ミュージックのデザインの部品 (見た目は style/base.css)。

ページや骨格はここの部品を組み合わせて画面を作る。色・大きさは CSS の変数
(`--m-…`) が持ち、ここでは形と振る舞いだけを決める。

約束:
- 曲名・アーティスト名は必ず `set_text` で入れる (マークアップは使わない。
  曲名には `&` や `<` が普通に入り、壊れたマークアップは黙って空になるため)。
- 押したときの呼び出し (`on_activate` / `on_more` / `on_button`) は、部品自身を
  1 つ受け取る関数でも、引数を取らない関数でもよい。
- 絵は `ArtworkLoader.request(subject, size, callback) -> handle` で取り、
  取れるまでは `placeholder(key, size, kind)` をすぐに出す。
- 子の部品・コントローラ・アニメーションなど「別の GObject」に渡す呼び出しで部品
  自身 (self) を強く掴まない。PyGObject の GC は C 側に渡した閉包の中を辿れず、
  部品 → 子 → 閉包 → 部品 の輪が残って、外した行やカードが解放されない。
  クラスの関数 (受け取った相手から get_ancestor で部品を引く) か `_weak()` を使う。
"""

from __future__ import annotations

import hashlib
import inspect
import math
import sys
import weakref

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from .protocol import format_time, format_total, relative_time, track_key  # noqa: E402

__all__ = [
    "FillWidth", "Artwork", "PlayingIndicator",
    "CircleButton", "CapsuleButton", "GlassCapsule", "ToggleCircle",
    "PageTitle", "SectionHeader", "Shelf",
    "MediaCard", "TallCard", "CategoryTile", "StationTile",
    "TrackRow", "TrackList", "EmptyState", "LoadingState", "Chip",
    "format_count", "format_total_duration", "color_pair_for", "invoke",
    "is_text_input", "inside_popover", "space_toggles", "install_space_toggle",
]


# --------------------------------------------------------------------------
# 小道具


def format_count(n: int) -> str:
    """曲数の表示。`format_count(12)` → "12 曲"。"""
    return f"{int(n)} 曲"


def format_total_duration(secs: float) -> str:
    """合計時間の表示。"48 分" / "1 時間" / "1 時間 12 分"。0 以下 (不明) は空。

    分へ丸める (Apple と同じく秒は出さない)。実体は protocol.format_total
    (情報の行とほかの画面で書き方が揃うよう、1 か所で決める)。"""
    return format_total(secs)


# 絵の無いもの (局など) の地の色。どれも白い文字と白い記号が読める濃さにし、
# 色相は偏らないよう一周に散らす。上が明るく下が深い組。
_PALETTE = (
    ("#f2536b", "#b3183f"),  # 赤
    ("#f5874f", "#c24a14"),  # 橙
    ("#e8b53a", "#a86f06"),  # 山吹
    ("#4fc27a", "#1c7d45"),  # 緑
    ("#2fb5ac", "#11706d"),  # 青緑
    ("#4a9ff0", "#1c5cb5"),  # 青
    ("#7b7ce0", "#4135a8"),  # 藍
    ("#b067c9", "#6a2c91"),  # 紫
    ("#ea5f9d", "#a3246b"),  # 桃
    ("#8193a6", "#46566a"),  # 灰青
)


def color_pair_for(key: str) -> tuple[str, str]:
    """名前から決まる色の組 (上の明るい色, 下の深い色) を "#rrggbb" で返す。

    局のタイルなど、絵の無いものの地に使う。同じ名前は毎回同じ色になるよう
    Python の hash (起動ごとに変わる) ではなく md5 で選ぶ。"""
    digest = hashlib.md5((key or "").encode("utf-8")).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def invoke(callback, widget) -> None:
    """押されたときの呼び出し。引数を 1 つ取れる関数には部品を渡し、
    引数を取らない関数はそのまま呼ぶ (ページ側が lambda: … でも書けるように)。"""
    if callback is None:
        return
    try:
        params = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        callback(widget)
        return
    takes_arg = any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        for p in params
    )
    if takes_arg:
        callback(widget)
    else:
        callback()


def is_text_input(widget: Gtk.Widget | None) -> bool:
    """文字の入力欄 (か、その中) か。"""
    while widget is not None:
        if isinstance(widget, (Gtk.Editable, Gtk.Text, Gtk.TextView)):
            return True
        widget = widget.get_parent()
    return False


def inside_popover(widget: Gtk.Widget | None) -> bool:
    """開いているメニュー (ポップオーバー) の中か。"""
    while widget is not None:
        if isinstance(widget, Gtk.Popover):
            return True
        widget = widget.get_parent()
    return False


def space_toggles(window: Gtk.Window, keyval: int, state) -> bool:
    """Space を再生/一時停止にする (DESIGN.md §6、どの窓でも)。受け取ったら True。

    文字の入力中と、開いているメニューの中では渡す (メニューの項目は Space で選ぶ)。
    アプリのショートカットにはしない (入力欄より先に働き、検索欄の空白を奪うため)。"""
    if keyval != Gdk.KEY_space or (state & Gtk.accelerator_get_default_mod_mask()):
        return False
    focus = window.get_focus()
    if is_text_input(focus) or inside_popover(focus):
        return False
    ctx = getattr(window, "ctx", None)
    store = getattr(ctx, "store", None)
    if store is None:
        return False
    store.toggle()
    return True


def _on_space_key(controller, keyval, _keycode, state) -> bool:
    window = controller.get_widget()
    return isinstance(window, Gtk.Window) and space_toggles(window, keyval, state)


def install_space_toggle(window: Gtk.Window) -> Gtk.EventControllerKey:
    """窓に Space (再生/一時停止) を付ける (捕まえる段で。フォーカスのある部品より先)。

    ミニプレーヤーとイコライザで使う (メインの窓は自分のキー処理から space_toggles を呼ぶ)。
    窓 (self) を掴む閉包を渡さず、コントローラから窓を引く。"""
    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", _on_space_key)
    window.add_controller(keys)
    return keys


def _weak(method):
    """bound method を弱く持つ呼び出し。子やアニメーションに渡しても部品を掴まない。"""
    ref = weakref.WeakMethod(method)

    def call(*args):
        target = ref()
        return target(*args) if target is not None else None

    return call


def _rgba(spec: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    if not color.parse(spec):
        color.parse("#808080")
    return color


def _rounded(width: float, height: float, radius: float) -> Gsk.RoundedRect:
    rect = Graphene.Rect().init(0, 0, width, height)
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(rect, max(0.0, min(radius, width / 2, height / 2)))
    return rounded


def _glyph_px(size: int) -> int:
    """丸いボタンの直径に対する記号の大きさ (34 → 16)。"""
    return max(12, round(size * 0.47))


def _label(text: str = "", css: str | tuple[str, ...] = (), *, xalign: float = 0.0,
           ellipsize: bool = True, lines: int = 0) -> Gtk.Label:
    """文字は必ず set_text で入れる (マークアップにしない)。"""
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    if ellipsize:
        label.set_ellipsize(Pango.EllipsizeMode.END)
        # 自然幅を小さく申告させ、親の幅で省略させる (長い曲名で親を広げない)
        label.set_max_width_chars(1)
        label.set_width_chars(1)
    if lines:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(lines)
    for cls in (css,) if isinstance(css, str) else css:
        label.add_css_class(cls)
    return label


def _subject_key(subject) -> str:
    """代わりの絵の色を決める鍵。"""
    if isinstance(subject, tuple) and len(subject) == 2 and subject[0] == "placeholder":
        return str(subject[1])
    if isinstance(subject, str):
        return subject
    if hasattr(subject, "path"):
        return track_key(subject)
    return str(subject)


def _animations_enabled(widget: Gtk.Widget) -> bool:
    settings = widget.get_settings() or Gtk.Settings.get_default()
    return bool(settings is None or settings.get_property("gtk-enable-animations"))


# --------------------------------------------------------------------------
# 包み


class _FillLayout(Gtk.LayoutManager):
    """FillWidth の寸法。子の寸法をそのまま使い、横の自然幅だけ大きく言う。"""

    NATURAL = 10000

    def do_get_request_mode(self, _widget) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, widget, orientation, for_size):
        child = widget.get_child()
        if child is None or not child.should_layout():
            return 0, 0, -1, -1
        minimum, natural, min_base, nat_base = child.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            natural = max(natural, self.NATURAL)
        return minimum, natural, min_base, nat_base

    def do_allocate(self, widget, width: int, height: int, baseline: int) -> None:
        child = widget.get_child()
        if child is not None and child.should_layout():
            child.allocate(width, height, baseline, None)


class FillWidth(Adw.Bin):
    """子を 1 つ包み、横の自然幅を大きく申告する。

    `FillWidth(child)`。Adw.HeaderBar の title_widget は自然幅のまま中央に
    置かれ、pack_start / pack_end の箱は縮まずに重なる。自然幅を窓より大きく
    言えば、両端の箱の間いっぱいに広がり、狭いときは子が省略して縮む。

    子の後始末 (unparent) を C 側の Adw.Bin に任せるため、Gtk.Widget の直接の
    派生にしない。PyGObject の do_dispose は終了時などに呼ばれないことがあり、
    「Finalizing …, but it still has children left」の警告になる。"""

    NATURAL = _FillLayout.NATURAL

    def __init__(self, child: Gtk.Widget):
        super().__init__()
        self.set_layout_manager(_FillLayout())
        self.set_child(child)

    @property
    def child(self) -> Gtk.Widget | None:
        return self.get_child()


# --------------------------------------------------------------------------
# アートワーク


class Artwork(Gtk.Widget):
    """角を丸めたアートワーク。

    `Artwork(width=40, height=None, radius=4, shadow=False, outline=True)`
    - 大きさは固定で申告する (height を省くと正方形)。親が広く割り当てたとき
      (hexpand など) はその大きさで描き、足りなくなった解像度を取り直す。
    - 絵は中央を基準に覆うように拡大して切り抜く (正方形でない枠でも歪めない)。
    - shadow: 下に柔らかい影を落とす。ぼかしは角丸の半径より小さくする。
    - outline: 暗い絵が地に沈まないよう、ふちに薄い光の線を引く。

    `set_subject(loader, subject, key_for_placeholder=None, kind="track")`
      代わりの絵をすぐに出し、倍率 (scale factor) を掛けた画素数で本物を頼む。
      前の依頼は取り消す。表示されていない間は頼まない。スクロールの中では
      見える範囲 (の前後 1 画面) に入ってから頼む (長い一覧で数百枚を一度に
      頼まないため。GTK は画面外の部品も描画を呼ぶので、描画では判断できない)。
    `set_texture(texture)` 絵を直接置く (None で空に)。
    `clear()` 絵も依頼も捨てる。
    """

    FADE_MS = 180

    def __init__(self, width: int = 40, height: int | None = None, radius: float = 4,
                 shadow: bool = False, outline: bool = True):
        super().__init__()
        self._width = int(width)
        self._height = int(height if height is not None else width)
        self._radius = float(radius)
        self._shadow = bool(shadow)
        self._outline = bool(outline)
        self._texture: Gdk.Texture | None = None
        self._previous: Gdk.Texture | None = None
        self._fade = 1.0
        self._fade_anim = None
        self._loader = None
        self._subject = None
        self._key = ""
        self._kind = "track"
        self._handle = None
        self._token = 0
        self._requested_px = 0
        self._pending = False
        self._watches: list[tuple[Gtk.Adjustment, int]] = []
        self._idle_id = 0
        self._placeholder_enabled = True
        self.add_css_class("music-artwork")
        self.connect("notify::scale-factor", Artwork._on_scale_factor)
        self.connect("map", Artwork._on_map_signal)

    @staticmethod
    def _on_scale_factor(widget, _pspec) -> None:
        widget._maybe_rerequest()

    @staticmethod
    def _on_map_signal(widget) -> None:
        widget._on_map()

    # 大きさ ---------------------------------------------------------------

    def set_size(self, width: int, height: int | None = None) -> None:
        self._width = int(width)
        self._height = int(height if height is not None else width)
        self.queue_resize()
        self._maybe_rerequest()

    def set_radius(self, radius: float) -> None:
        self._radius = float(radius)
        self.queue_draw()

    @property
    def texture(self) -> Gdk.Texture | None:
        return self._texture

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        size = self._width if orientation == Gtk.Orientation.HORIZONTAL else self._height
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._idle_id:
            return
        if self._pending:
            # 一覧の並びが変わって見える範囲に入ったかもしれない
            self._idle_id = GLib.idle_add(self._after_allocate, False)
        elif (self._loader is not None and self._requested_px
              and self._wanted_px(width, height) > self._requested_px * 1.15):
            # 広げて使われた (ミニプレーヤーなど)。解像度が足りないので取り直す
            self._idle_id = GLib.idle_add(self._after_allocate, True)

    def _after_allocate(self, grew: bool) -> bool:
        self._idle_id = 0
        if grew:
            self._maybe_rerequest()
        else:
            self._try_request()
        return GLib.SOURCE_REMOVE

    # 取得 -----------------------------------------------------------------

    def _wanted_px(self, width: int | None = None, height: int | None = None) -> int:
        w = width if width else max(self.get_width(), self._width)
        h = height if height else max(self.get_height(), self._height)
        return int(math.ceil(max(w, h, 1) * max(1, self.get_scale_factor())))

    def set_subject(self, loader, subject, key_for_placeholder: str | None = None,
                    kind: str = "track") -> None:
        self._cancel()
        self._loader = loader
        self._subject = subject
        self._kind = kind
        self._key = key_for_placeholder if key_for_placeholder is not None else _subject_key(subject)
        if loader is None or subject is None:
            self._set_texture(None, fade=False)
            return
        if self._placeholder_enabled:
            try:
                self._set_texture(loader.placeholder(self._key, self._wanted_px(), kind), fade=False)
            except Exception as error:  # 代わりの絵は飾り。失敗しても本物の取得は続ける
                print(f"cliamp-music: 代わりの絵を作れません: {error}", file=sys.stderr)
        else:
            self._set_texture(None, fade=False)
        self._requested_px = 0
        self._pending = True
        self._try_request()

    def clear(self) -> None:
        self._cancel()
        self._loader = None
        self._subject = None
        self._set_texture(None, fade=False)

    def set_texture(self, texture: Gdk.Texture | None) -> None:
        """絵を直接置く。取得中の依頼は取り消す。"""
        self._cancel()
        self._loader = None
        self._subject = None
        self._set_texture(texture, fade=False)

    def _cancel(self) -> None:
        """依頼を取り消す (取り消せなくても token で結果を捨てる)。"""
        self._token += 1
        self._pending = False
        self._unwatch()
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # 相手の不具合で取り消しに失敗しても、結果は token で捨てる
                pass

    def _try_request(self) -> None:
        """待っている依頼を、表示されていて見える範囲にあれば出す。無理なら見張る。"""
        if not self._pending or self._loader is None or self._subject is None:
            return
        if not self.get_mapped():
            return  # map で呼び直す
        if self._in_view():
            self._unwatch()
            self._request()
        else:
            self._watch()

    def _request(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.cancel()
            except Exception:  # 取り消せなくても token で古い結果は捨てる
                pass
        self._pending = False
        self._token += 1
        token = self._token
        px = self._wanted_px()
        self._requested_px = px
        # 手元のキャッシュにあれば request の中で done が同期で呼ばれる。そのときは
        # 絵を溶かさずに置き、終わった依頼の handle も覚えない
        state = {"sync": True, "done": False}

        def done(texture):
            if token != self._token:
                return
            state["done"] = True
            self._handle = None
            self._set_texture(texture, fade=not state["sync"])

        handle = self._loader.request(self._subject, px, done)
        state["sync"] = False
        if token == self._token and not state["done"]:
            self._handle = handle

    # スクロールの中で見える範囲に入るまで待つ ------------------------------

    def _scrollers(self) -> list[Gtk.ScrolledWindow]:
        out = []
        widget = self.get_parent()
        while widget is not None:
            if isinstance(widget, Gtk.ScrolledWindow):
                out.append(widget)
            widget = widget.get_parent()
        return out

    def _in_view(self) -> bool:
        """すべての祖先のスクロールで、見える範囲の前後 1 画面以内にあるか。

        位置はスクロールの中身から測り、adjustment の値と比べる。スクロールした
        直後は中身の並べ直しがまだなので、画面に対する位置では判断できない。"""
        for scroller in self._scrollers():
            if scroller.get_width() <= 0 or scroller.get_height() <= 0:
                return False  # まだ並べられていない。size_allocate で見直す
            content = scroller.get_child()
            if isinstance(content, Gtk.Viewport):
                content = content.get_child() or content
            if content is None:
                continue
            ok, bounds = self.compute_bounds(content)
            if not ok:
                return False
            for adjustment, start, length in (
                (scroller.get_hadjustment(), bounds.get_x(), bounds.get_width()),
                (scroller.get_vadjustment(), bounds.get_y(), bounds.get_height()),
            ):
                page = adjustment.get_page_size()
                if page <= 0:
                    continue  # その向きにはスクロールしない
                value = adjustment.get_value()
                if start + length < value - page or start > value + 2 * page:
                    return False
        return True

    def _watch(self) -> None:
        if self._watches:
            return
        for scroller in self._scrollers():
            for adjustment in (scroller.get_hadjustment(), scroller.get_vadjustment()):
                handler = adjustment.connect("value-changed", _weak(self._on_scrolled))
                self._watches.append((adjustment, handler))

    def _unwatch(self) -> None:
        watches, self._watches = getattr(self, "_watches", []), []
        for adjustment, handler in watches:
            adjustment.disconnect(handler)

    def _on_scrolled(self, *_args) -> None:
        self._try_request()

    def _on_map(self) -> None:
        self._try_request()

    def _maybe_rerequest(self) -> None:
        if (self._loader is not None and self._subject is not None and self._requested_px
                and self._wanted_px() > self._requested_px):
            self._pending = True
            self._try_request()

    def _set_texture(self, texture, fade: bool) -> None:
        if texture is self._texture:
            return
        if fade and self._texture is not None and self.get_mapped() and _animations_enabled(self):
            self._previous = self._texture
            self._fade = 0.0
            if self._fade_anim is None:
                # アニメーションは部品が持つので、逆向き (アニメーション → 部品) は弱く
                target = Adw.CallbackAnimationTarget.new(_weak(self._on_fade))
                self._fade_anim = Adw.TimedAnimation.new(self, 0.0, 1.0, self.FADE_MS, target)
                self._fade_anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
                self._fade_anim.connect("done", _weak(self._on_fade_done))
            self._texture = texture
            self._fade_anim.play()
        else:
            self._previous = None
            self._fade = 1.0
            self._texture = texture
        self.queue_draw()

    def _on_fade(self, value: float) -> None:
        self._fade = value
        self.queue_draw()

    def _on_fade_done(self, *_args) -> None:
        self._previous = None
        self._fade = 1.0
        self.queue_draw()

    def do_unmap(self) -> None:
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.skip()
        # 隠れている間はスクロールを見張らない (次の map で見直す)
        if hasattr(self, "_watches"):
            self._unwatch()
        Gtk.Widget.do_unmap(self)

    def do_dispose(self) -> None:
        # 終了時などは __init__ を通っていない包みで呼ばれることがあるので getattr で見る
        if hasattr(self, "_token"):
            self._cancel()
        if getattr(self, "_idle_id", 0):
            GLib.source_remove(self._idle_id)
            self._idle_id = 0
        anim = getattr(self, "_fade_anim", None)
        if anim is not None:
            anim.reset()
            self._fade_anim = None
        Gtk.Widget.do_dispose(self)

    # 描画 -----------------------------------------------------------------

    @staticmethod
    def _append_cover(snapshot: Gtk.Snapshot, texture: Gdk.Texture, x: float, y: float,
                      width: float, height: float) -> None:
        tw, th = texture.get_width(), texture.get_height()
        if tw <= 0 or th <= 0:
            return
        scale = max(width / tw, height / th)
        dw, dh = tw * scale, th * scale
        rect = Graphene.Rect().init(x + (width - dw) / 2, y + (height - dh) / 2, dw, dh)
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.TRILINEAR, rect)

    def _append_shadow(self, snapshot: Gtk.Snapshot, rounded: Gsk.RoundedRect) -> None:
        # ぼかしは角丸より小さく (大きいと角の影が四角く見える)
        blur = max(2.0, min(self._radius - 1, 14.0))
        snapshot.append_outset_shadow(rounded, _rgba("rgba(0,0,0,0.34)"), 0, blur * 0.55, 0, blur)

    def _append_outline(self, snapshot: Gtk.Snapshot, rounded: Gsk.RoundedRect) -> None:
        color = _rgba("rgba(255,255,255,0.07)")
        snapshot.append_border(rounded, [1, 1, 1, 1], [color, color, color, color])

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rounded = _rounded(width, height, self._radius)
        if self._shadow:
            self._append_shadow(snapshot, rounded)
        snapshot.push_rounded_clip(rounded)
        if self._texture is None:
            snapshot.append_color(_rgba("rgba(118,118,128,0.24)"), Graphene.Rect().init(0, 0, width, height))
        elif self._previous is not None and self._fade < 1.0:
            snapshot.push_cross_fade(self._fade)
            self._append_cover(snapshot, self._previous, 0, 0, width, height)
            snapshot.pop()
            self._append_cover(snapshot, self._texture, 0, 0, width, height)
            snapshot.pop()
        else:
            self._append_cover(snapshot, self._texture, 0, 0, width, height)
        snapshot.pop()
        if self._outline:
            self._append_outline(snapshot, rounded)


# --------------------------------------------------------------------------
# 再生中の印


class PlayingIndicator(Gtk.Widget):
    """再生中の行に出す、上下する赤い棒 (イコライザの印)。

    `PlayingIndicator(size=14, bars=4)`、`set_playing(playing: bool)`。
    表示中かつ再生中のときだけ frame clock で動かし、一時停止中は低い棒で止める。
    色は CSS の color (`.music-playing-indicator`、Music の赤)。"""

    # 棒ごとの動き (周期の違う sin を 2 つ重ねて、規則的に見えないようにする)
    _WAVES = ((5.3, 0.0, 8.9, 1.3), (6.7, 2.1, 4.3, 0.4), (4.1, 4.0, 7.7, 2.6), (7.9, 1.2, 5.1, 3.7))
    _PAUSED = (0.42, 0.78, 0.56, 0.30)

    def __init__(self, size: int = 14, bars: int = 4):
        super().__init__()
        self._size = int(size)
        self._bars = max(3, min(4, int(bars)))
        self._playing = False
        self._tick_id = 0
        self._time = 0.0
        self.add_css_class("music-playing-indicator")
        self.set_can_target(False)
        self.connect("map", PlayingIndicator._on_mapped)
        self.connect("unmap", PlayingIndicator._on_mapped)

    @staticmethod
    def _on_mapped(widget) -> None:
        widget._update_ticking()

    @property
    def playing(self) -> bool:
        return self._playing

    def set_playing(self, playing: bool) -> None:
        playing = bool(playing)
        if playing == self._playing:
            return
        self._playing = playing
        self._update_ticking()
        self.queue_draw()

    def _update_ticking(self) -> None:
        want = self._playing and self.get_mapped() and _animations_enabled(self)
        if want and not self._tick_id:
            self._tick_id = self.add_tick_callback(self._on_tick)
        elif not want and self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0

    def _on_tick(self, _widget, clock: Gdk.FrameClock) -> bool:
        self._time = clock.get_frame_time() / 1_000_000
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation, for_size):
        return self._size, self._size, -1, -1

    def _levels(self) -> list[float]:
        if not self._playing or not self._tick_id:
            return list(self._PAUSED[: self._bars])
        t = self._time
        levels = []
        for f1, p1, f2, p2 in self._WAVES[: self._bars]:
            v = 0.5 + 0.32 * math.sin(t * f1 + p1) + 0.18 * math.sin(t * f2 + p2)
            levels.append(max(0.18, min(1.0, v)))
        return levels

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        size = min(width, height)
        x0 = (width - size) / 2
        y0 = (height - size) / 2
        gap = max(1.0, size / 9)
        bar = (size - gap * (self._bars - 1)) / self._bars
        color = self.get_color()
        for i, level in enumerate(self._levels()):
            h = max(bar, size * level)
            rect = Graphene.Rect().init(x0 + i * (bar + gap), y0 + size - h, bar, h)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, min(bar / 2, 1.0))
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(color, rect)
            snapshot.pop()

    def do_dispose(self) -> None:
        if getattr(self, "_tick_id", 0):
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        Gtk.Widget.do_dispose(self)


# --------------------------------------------------------------------------
# ボタン


class CircleButton(Gtk.Button):
    """丸いボタン (シャッフル・「…」・戻るなど)。

    `CircleButton(icon_name, tooltip="", size=34, accent=False, *, flat=False, glass=False)`
    - accent: 記号を Music の赤にする (詳細ページのシャッフル・「…」)。
    - flat: 地を塗らない (再生バーの前へ・次へなど)。乗せたときだけ薄く塗る。
    - glass: ガラスの丸 (ページ左上の「‹」など、内容の上に浮かべるもの)。
    `set_icon_name(name)` で記号を差し替える。"""

    def __init__(self, icon_name: str, tooltip: str = "", size: int = 34, accent: bool = False,
                 *, flat: bool = False, glass: bool = False):
        super().__init__()
        self.image = Gtk.Image.new_from_icon_name(icon_name)
        self.image.set_pixel_size(_glyph_px(size))
        self.set_child(self.image)
        self.add_css_class("music-circle")
        # テーマの「button:not(.circular) に余白」の規則を外すため
        self.add_css_class("circular")
        if accent:
            self.add_css_class("accent")
        if flat:
            self.add_css_class("flat")
        if glass:
            self.add_css_class("music-glass")
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        if tooltip:
            self.set_tooltip_text(tooltip)
            self.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])

    def set_icon_name(self, icon_name: str) -> None:
        self.image.set_from_icon_name(icon_name)


class ToggleCircle(Gtk.ToggleButton):
    """入り切りする丸いボタン (シャッフル・リピート・歌詞など)。

    `ToggleCircle(icon_name, tooltip="", size=30)`。入 = 薄い赤の丸に赤い記号、
    切 = 地なしの記号。`set_icon_name(name)` で記号を差し替える (リピート 1 曲など)。"""

    def __init__(self, icon_name: str, tooltip: str = "", size: int = 30):
        super().__init__()
        self.image = Gtk.Image.new_from_icon_name(icon_name)
        self.image.set_pixel_size(_glyph_px(size))
        self.set_child(self.image)
        self.add_css_class("music-toggle-circle")
        self.add_css_class("circular")
        self.set_size_request(size, size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        if tooltip:
            self.set_tooltip_text(tooltip)
            self.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])

    def set_icon_name(self, icon_name: str) -> None:
        self.image.set_from_icon_name(icon_name)


class CapsuleButton(Gtk.Button):
    """横長のカプセル (「▶ 再生」)。

    `CapsuleButton(label, icon_name=None, accent_text=True, *, filled=False)`
    - accent_text: 文字と記号を赤にする (灰色の地に赤の「再生」)。
    - filled: 赤で塗り、白い文字にする (空状態の「cliamp を起動」など、画面で 1 つだけ)。
    高さ 34、最小幅 120。`set_label(text)` / `set_icon_name(name)`。"""

    def __init__(self, label: str, icon_name: str | None = None, accent_text: bool = True,
                 *, filled: bool = False):
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        self.image = Gtk.Image()
        self.image.set_pixel_size(14)
        if icon_name:
            self.image.set_from_icon_name(icon_name)
        self.image.set_visible(bool(icon_name))
        box.append(self.image)
        self.label = _label(label, "music-capsule-label", ellipsize=False)
        box.append(self.label)
        self.set_child(box)
        self.add_css_class("music-capsule")
        self.add_css_class("pill")
        if accent_text:
            self.add_css_class("accent")
        if filled:
            self.add_css_class("filled")
        self.set_valign(Gtk.Align.CENTER)

    def set_label(self, text: str) -> None:  # Gtk.Button.set_label を置き換える (子を壊さない)
        self.label.set_text(text or "")

    def set_icon_name(self, icon_name: str | None) -> None:
        if icon_name:
            self.image.set_from_icon_name(icon_name)
        self.image.set_visible(bool(icon_name))


class GlassCapsule(Gtk.Box):
    """ガラスのカプセル。中に CircleButton を並べる (全画面の ✕ / ミニプレーヤー など)。

    `GlassCapsule(spacing=0, orientation=Gtk.Orientation.HORIZONTAL)`。
    中の CircleButton は地を塗らず、乗せたときだけ薄く塗る。"""

    def __init__(self, spacing: int = 0, orientation: Gtk.Orientation = Gtk.Orientation.HORIZONTAL):
        super().__init__(orientation=orientation, spacing=spacing)
        self.add_css_class("music-glass")
        self.add_css_class("music-glass-capsule")
        self.set_valign(Gtk.Align.CENTER)


class Chip(Gtk.Button):
    """丸いチップ (最近の検索)。`Chip(label, on_activate=None, *, icon_name=None)`。"""

    def __init__(self, label: str, on_activate=None, *, icon_name: str | None = None):
        super().__init__()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        if icon_name:
            image = Gtk.Image.new_from_icon_name(icon_name)
            image.set_pixel_size(12)
            box.append(image)
        self.label = _label(label, "music-chip-label", ellipsize=True)
        self.label.set_max_width_chars(24)
        box.append(self.label)
        self.set_child(box)
        self.add_css_class("music-chip")
        self.add_css_class("pill")
        self.text = label
        self.on_activate = on_activate
        self.connect("clicked", Chip._on_clicked)

    @staticmethod
    def _on_clicked(chip) -> None:
        invoke(chip.on_activate, chip)


# --------------------------------------------------------------------------
# 見出し


class PageTitle(Gtk.Label):
    """ページの大見出し (34px/700)。`PageTitle(text)`。"""

    def __init__(self, text: str):
        super().__init__(accessible_role=Gtk.AccessibleRole.HEADING)
        self.set_text(text or "")
        self.set_xalign(0)
        self.set_ellipsize(Pango.EllipsizeMode.END)
        self.add_css_class("music-page-title")


class SectionHeader(Gtk.Box):
    """棚や節の見出し (17px/700)。

    `SectionHeader(title, on_more=None)`。on_more があれば見出し全体が押せる
    リンクになり、右に「›」が付く (「最近再生した項目 ›」)。
    `add_end(widget)` で右端に物を置く (「消去」など)。`set_title(text)`。"""

    def __init__(self, title: str, on_more=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("music-section-header")
        self.on_more = on_more
        # 自然幅は題の全長のまま (狭いときだけ省略)。_label の「自然幅 1 文字」は
        # 横に並ぶ箱の中では題が「…」だけになるので使わない
        self.title_label = _label(title, "music-section-title", ellipsize=False)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.link = None
        if on_more is not None:
            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            inner.append(self.title_label)
            chevron = Gtk.Image.new_from_icon_name("music-chevron-right-symbolic")
            chevron.set_pixel_size(13)
            chevron.add_css_class("music-section-chevron")
            chevron.set_valign(Gtk.Align.CENTER)
            inner.append(chevron)
            self.link = Gtk.Button()
            self.link.set_child(inner)
            self.link.add_css_class("music-section-link")
            self.link.add_css_class("flat")
            self.link.connect("clicked", SectionHeader._on_link_clicked)
            self.link.set_halign(Gtk.Align.START)
            self.append(self.link)
        else:
            self.append(self.title_label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        self.append(spacer)
        self._end = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._end.set_valign(Gtk.Align.CENTER)
        self.append(self._end)

    @staticmethod
    def _on_link_clicked(link) -> None:
        header = link.get_ancestor(SectionHeader)
        if header is not None:
            invoke(header.on_more, header)

    def set_title(self, text: str) -> None:
        self.title_label.set_text(text or "")

    def add_end(self, widget: Gtk.Widget) -> None:
        self._end.append(widget)


# --------------------------------------------------------------------------
# 棚


class Shelf(Gtk.Box):
    """見出し付きの横スクロールの棚。

    `Shelf(title, on_more=None, *, inset=0, spacing=18)`
    - `append(widget)` / `remove_all()` / `items()`。
    - スクロールバーは出さない (タッチパッドの横送りと Shift+ホイールは効く。
      縦のホイールはページのスクロールへ渡す)。
    - マウスを乗せると左右に丸い「‹ ›」が出て、押すと 1 画面ぶん滑らかに送る。
      送り先はカードの境目に揃える。
    - inset: 中身の左右の余白。棚を画面の端まで広げたまま、静止時の先頭を
      ページの見出しに揃えるのに使う。
    - 部品が `art_height` を持っていれば、「‹ ›」をその絵の縦の中央に置く。"""

    PAGE_MS = 450

    def __init__(self, title: str, on_more=None, *, inset: int = 0, spacing: int = 18):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("music-shelf")
        self._inset = int(inset)
        self.header = SectionHeader(title, on_more)
        self.header.set_margin_start(self._inset)
        self.header.set_margin_end(self._inset)
        if title:
            Gtk.Box.append(self, self.header)

        self._box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
        self._box.set_margin_start(self._inset)
        self._box.set_margin_end(self._inset)
        # 影 (カードの下) が切れないよう、下に少し余白を持たせる
        self._box.set_margin_bottom(6)
        self._box.set_valign(Gtk.Align.START)

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        self._scroller.set_propagate_natural_height(True)
        self._scroller.set_hexpand(True)
        self._scroller.set_child(self._box)
        self._scroller.add_css_class("music-shelf-scroller")

        self._overlay = Gtk.Overlay()
        self._overlay.set_child(self._scroller)
        self._prev = self._make_pager("music-back-symbolic", "前へ", Gtk.Align.START, -1)
        self._next = self._make_pager("music-forward-symbolic", "次へ", Gtk.Align.END, 1)
        Gtk.Box.append(self, self._overlay)

        self._hover = False
        self._anim = None
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", Shelf._on_enter)
        motion.connect("leave", Shelf._on_leave)
        self._overlay.add_controller(motion)
        adj = self._scroller.get_hadjustment()
        update = _weak(self._update_pagers)
        adj.connect("value-changed", lambda *_: update())
        adj.connect("changed", lambda *_: update())

    @staticmethod
    def _on_enter(controller, *_args) -> None:
        shelf = controller.get_widget().get_ancestor(Shelf)
        if shelf is not None:
            shelf._set_hover(True)

    @staticmethod
    def _on_leave(controller, *_args) -> None:
        shelf = controller.get_widget().get_ancestor(Shelf)
        if shelf is not None:
            shelf._set_hover(False)

    @staticmethod
    def _on_pager_clicked(button, direction: int) -> None:
        shelf = button.get_ancestor(Shelf)
        if shelf is not None:
            shelf.scroll_page(direction)

    def _make_pager(self, icon: str, tooltip: str, halign: Gtk.Align, direction: int) -> Gtk.Button:
        button = Gtk.Button()
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(14)
        button.set_child(image)
        button.add_css_class("music-pager")
        button.set_tooltip_text(tooltip)
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.set_halign(halign)
        button.set_valign(Gtk.Align.CENTER)
        button.set_margin_start(6)
        button.set_margin_end(6)
        button.set_can_target(False)
        button.set_can_focus(False)
        button.connect("clicked", Shelf._on_pager_clicked, direction)
        self._overlay.add_overlay(button)
        return button

    # 中身 -----------------------------------------------------------------

    @property
    def title(self) -> str:
        return self.header.title_label.get_text()

    def set_title(self, text: str) -> None:
        self.header.set_title(text)
        if text and self.header.get_parent() is None:
            self.prepend(self.header)

    def append(self, widget: Gtk.Widget) -> None:
        """棚に部品を足す (Gtk.Box.append を棚の中身へ向ける)。"""
        self._box.append(widget)
        art = getattr(widget, "art_height", None)
        if art and self._box.get_first_child() == widget:
            for pager in (self._prev, self._next):
                pager.set_valign(Gtk.Align.START)
                pager.set_margin_top(max(0, int(art / 2 - 26)))
        self._update_pagers()

    def remove_all(self, reset_scroll: bool = True) -> None:
        """中身をすべて外す。reset_scroll=False なら横の送り位置を残す (データの取り直しで
        作り直すとき。先頭へ戻すのは中身がまるごと替わったときだけ)。"""
        child = self._box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._box.remove(child)
            child = following
        if reset_scroll:
            self._scroller.get_hadjustment().set_value(0)
        self._update_pagers()

    def set_items(self, widgets) -> None:
        """棚の中身を widgets の並びにする。今ある部品はそのまま使い回して並べ替え、
        要らないものだけ外す (送り位置・フォーカス・読み込んだ絵を失わない)。"""
        wanted = list(widgets)
        keep = {id(w) for w in wanted}
        for child in self.items():
            if id(child) not in keep:
                self._box.remove(child)
        previous = None
        for widget in wanted:
            parent = widget.get_parent()
            if parent is None:
                self._box.insert_child_after(widget, previous)
            elif parent is self._box:
                self._box.reorder_child_after(widget, previous)
            previous = widget
        first = wanted[0] if wanted else None
        art = getattr(first, "art_height", None) if first is not None else None
        if art:
            for pager in (self._prev, self._next):
                pager.set_valign(Gtk.Align.START)
                pager.set_margin_top(max(0, int(art / 2 - 26)))
        self._update_pagers()

    def items(self) -> list[Gtk.Widget]:
        out = []
        child = self._box.get_first_child()
        while child is not None:
            out.append(child)
            child = child.get_next_sibling()
        return out

    # 送り -----------------------------------------------------------------

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._update_pagers()

    def _update_pagers(self) -> None:
        adj = self._scroller.get_hadjustment()
        value, lower = adj.get_value(), adj.get_lower()
        upper, page = adj.get_upper(), adj.get_page_size()
        can_prev = value > lower + 1
        can_next = value + page < upper - 1
        for button, able in ((self._prev, can_prev), (self._next, can_next)):
            shown = self._hover and able
            if shown:
                button.add_css_class("shown")
            else:
                button.remove_css_class("shown")
            button.set_can_target(shown)

    def _child_spans(self) -> list[tuple[float, float]]:
        spans = []
        for child in self.items():
            if not child.get_visible():
                continue
            ok, bounds = child.compute_bounds(self._box)
            if ok:
                spans.append((bounds.get_x(), bounds.get_width()))
        return spans

    def scroll_page(self, direction: int) -> None:
        """1 画面ぶん送る (direction: 1 = 右へ、-1 = 左へ)。"""
        adj = self._scroller.get_hadjustment()
        value, page = adj.get_value(), adj.get_page_size()
        lower, upper = adj.get_lower(), adj.get_upper() - page
        spans = self._child_spans()
        target = value + direction * page
        if direction > 0:
            # 右端で途切れているカードを、次の画面の先頭にする
            for x, w in spans:
                if self._inset + x + w > value + page + 0.5:
                    if x > value + 1:
                        target = x
                    break
        else:
            back = value - page
            for x, _w in spans:
                if x >= back - 0.5:
                    target = x
                    break
        target = max(lower, min(upper, target))
        if self._anim is not None:
            self._anim.pause()
        if not _animations_enabled(self):
            adj.set_value(target)
            return
        anim_target = Adw.CallbackAnimationTarget.new(lambda v: adj.set_value(v))
        self._anim = Adw.TimedAnimation.new(self._scroller, value, target, self.PAGE_MS, anim_target)
        self._anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self._anim.play()


# --------------------------------------------------------------------------
# カードとタイル


class _Card(Gtk.Button):
    """押せるカードの共通部分 (ボタンの枠は CSS で消す)。"""

    def __init__(self, on_activate, css: str):
        super().__init__()
        self.add_css_class("music-card")
        self.add_css_class(css)
        self.on_activate = on_activate
        self.connect("clicked", _Card._on_clicked)
        self.set_valign(Gtk.Align.START)
        self.set_halign(Gtk.Align.START)

    @staticmethod
    def _on_clicked(card) -> None:
        invoke(card.on_activate, card)


class MediaCard(_Card):
    """正方形の絵に題と副題を添えたカード (最近再生した項目・プレイリスト)。

    `MediaCard(loader, subject, title, subtitle, size=170, kind="track", on_activate=None)`
    角 7。乗せると絵が少し暗くなる。属性: subject, art (Artwork), art_height。"""

    def __init__(self, loader, subject, title: str, subtitle: str, size: int = 170,
                 kind: str = "track", on_activate=None):
        super().__init__(on_activate, "music-media-card")
        self.subject = subject
        self.art_height = size
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overlay = Gtk.Overlay()
        self.art = Artwork(size, size, radius=7)
        self.art.set_subject(loader, subject, kind=kind)
        overlay.set_child(self.art)
        scrim = Gtk.Box()
        scrim.add_css_class("music-card-scrim")
        scrim.set_can_target(False)
        overlay.add_overlay(scrim)
        box.append(overlay)
        self.title_label = _label(title, "music-card-title")
        self.subtitle_label = _label(subtitle, "music-card-subtitle")
        self.subtitle_label.set_visible(bool(subtitle))
        box.append(self.title_label)
        box.append(self.subtitle_label)
        box.set_size_request(size, -1)
        self.set_child(box)
        tip = title if not subtitle else f"{title}\n{subtitle}"
        self.set_tooltip_text(tip)


class TallCard(_Card):
    """縦長のカード (ホームの「おすすめ」、220x290、角 12)。

    `TallCard(loader, subject, overline, title, on_activate=None, *, kind="track")`
    絵の下半分に暗いグラデーションを敷き、白い小見出しと太字の題を載せる。"""

    WIDTH, HEIGHT = 220, 290

    def __init__(self, loader, subject, overline: str, title: str, on_activate=None,
                 *, kind: str = "track"):
        super().__init__(on_activate, "music-tall-card")
        self.subject = subject
        self.art_height = self.HEIGHT
        overlay = Gtk.Overlay()
        self.art = Artwork(self.WIDTH, self.HEIGHT, radius=12, shadow=True)
        self.art.set_subject(loader, subject, kind=kind)
        overlay.set_child(self.art)
        shade = Gtk.Box()
        shade.add_css_class("music-tall-card-shade")
        shade.set_can_target(False)
        overlay.add_overlay(shade)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text.set_valign(Gtk.Align.END)
        text.set_margin_start(16)
        text.set_margin_end(16)
        text.set_margin_bottom(16)
        text.set_can_target(False)
        self._text = text
        # 文字の塊は自分で置く。Overlay の既定は塊の自然幅 (題が 1 行に収まる幅) で
        # 高さを測ってからカードの幅に縮めるので、2 行に折り返したときに高さが
        # 足りなくなる (Allocation height too small)。カードの幅で高さを測り直す。
        overlay.connect("get-child-position", TallCard._place_text)
        self.overline_label = _label(overline, "music-tall-card-overline")
        self.overline_label.set_visible(bool(overline))
        # 2 行まで折り返して省略する。_label の「自然幅 1 文字」を使うと、折り返す
        # 文字の高さの見積もりが Overlay の割り当てと食い違う (Allocation height too small)
        self.title_label = _label(title, "music-tall-card-title", ellipsize=False, lines=2)
        self.title_label.set_ellipsize(Pango.EllipsizeMode.END)
        text.append(self.overline_label)
        text.append(self.title_label)
        overlay.add_overlay(text)
        scrim = Gtk.Box()
        scrim.add_css_class("music-card-scrim")
        scrim.add_css_class("tall")
        scrim.set_can_target(False)
        overlay.add_overlay(scrim)
        self.set_child(overlay)
        self.set_tooltip_text(title)

    @staticmethod
    def _place_text(overlay: Gtk.Overlay, widget: Gtk.Widget, allocation: Gdk.Rectangle) -> bool:
        card = overlay.get_ancestor(TallCard)
        if card is None or widget != card._text:
            return False
        width, height = overlay.get_width(), overlay.get_height()
        _min, natural, _b, _n = widget.measure(Gtk.Orientation.VERTICAL, width)
        allocation.x, allocation.width = 0, width
        allocation.height = min(height, natural)
        allocation.y = height - allocation.height
        return True


class _GradientTile(Gtk.Widget):
    """16:9 を保つ色の面。幅に合わせて高さが決まる (格子で幅が伸びても崩れない)。
    文字は持たない (子を持つ自作の部品は後始末を Python に頼ることになるため、
    文字は Gtk.Overlay で上に重ねる)。"""

    MIN_WIDTH, NATURAL_WIDTH = 120, 180

    def __init__(self, colors: tuple[str, str], radius: float = 8):
        super().__init__()
        self._colors = (_rgba(colors[0]), _rgba(colors[1]))
        self._radius = radius

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return self.MIN_WIDTH, self.NATURAL_WIDTH, -1, -1
        # 最小は大きく緩める。同じ幅に揃える FlowBox は、行の高さを測った幅より
        # 広く子を割り当てる (行の余りを配る) ことがあり、最小を 16:9 に近づけると
        # 「Allocation height too small」になる (幅 216 で測り 236 で割り当てるなど)。
        # 高さは自然な値 (16:9) が渡るので、見た目は変わらない。
        # 幅を決めない測り (for_size = -1) の最小は最小の幅での値にする (GTK は
        # 「どの幅でも最小はこれ以上」を求め、崩れると Gtk-WARNING を出す)
        if for_size > 0:
            natural = int(round(for_size * 9 / 16))
            return max(1, natural * 3 // 4), natural, -1, -1
        smallest = int(round(self.MIN_WIDTH * 9 / 16))
        return max(1, smallest * 3 // 4), int(round(self.NATURAL_WIDTH * 9 / 16)), -1, -1

    @staticmethod
    def _stops(pairs) -> list:
        stops = []
        for offset, color in pairs:
            stop = Gsk.ColorStop()
            stop.offset = offset
            stop.color = color if isinstance(color, Gdk.RGBA) else _rgba(color)
            stops.append(stop)
        return stops

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rounded = _rounded(width, height, self._radius)
        bounds = Graphene.Rect().init(0, 0, width, height)
        snapshot.push_rounded_clip(rounded)
        snapshot.append_linear_gradient(bounds, Graphene.Point().init(0, 0),
                                        Graphene.Point().init(width, height),
                                        self._stops(((0.0, self._colors[0]), (1.0, self._colors[1]))))
        # 左上から差す光と、文字の下の影 (白い文字を読ませるため)
        snapshot.append_radial_gradient(bounds, Graphene.Point().init(width * 0.18, height * 0.05),
                                        width * 0.75, height * 0.95, 0.0, 1.0,
                                        self._stops(((0.0, "rgba(255,255,255,0.22)"),
                                                     (1.0, "rgba(255,255,255,0)"))))
        snapshot.append_linear_gradient(bounds, Graphene.Point().init(0, height * 0.45),
                                        Graphene.Point().init(0, height),
                                        self._stops(((0.0, "rgba(0,0,0,0)"), (1.0, "rgba(0,0,0,0.22)"))))
        snapshot.pop()


class CategoryTile(_Card):
    """カテゴリーのタイル (16:9、角 8、2 色のグラデーション、左下に白い太字)。

    `CategoryTile(title, colors: tuple[str, str], on_activate=None)`
    colors は CSS の色の文字列 ("#ff5f6d" など)。格子に入れると幅に合わせて伸びる。"""

    def __init__(self, title: str, colors: tuple[str, str], on_activate=None):
        super().__init__(on_activate, "music-category-tile")
        self.title = title
        overlay = Gtk.Overlay()
        self.surface = _GradientTile(colors)
        overlay.set_child(self.surface)
        # 自然幅は題の全長 (タイルの幅に収まらないときだけ省略する)
        self.label = _label(title, "music-category-label", ellipsize=False)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_halign(Gtk.Align.START)
        self.label.set_valign(Gtk.Align.END)
        self.label.set_margin_start(11)
        self.label.set_margin_end(11)
        self.label.set_margin_bottom(9)
        self.label.set_can_target(False)
        overlay.add_overlay(self.label)
        self.set_child(overlay)
        self.set_halign(Gtk.Align.FILL)
        self.set_valign(Gtk.Align.START)


class _StationArt(Artwork):
    """局の絵。名前から決まる色の地の中央に favicon を小さく置く
    (favicon は小さく荒いものが多く、全面に引き伸ばすと崩れるため)。"""

    def __init__(self, size: int, name: str):
        super().__init__(size, size, radius=7, shadow=False)
        self._placeholder_enabled = False
        top, bottom = color_pair_for(name)
        self._ground = (_rgba(top), _rgba(bottom))

    def _slot(self, width: float, height: float) -> float:
        return round(min(width, height) * 0.46)

    def _set_texture(self, texture, fade: bool) -> None:
        # favicon が取れなかったとき、ArtworkLoader は代わりの絵 (局の記号入りの
        # 色の四角) を返す。色の地の上にもう 1 枚色の四角を重ねると入れ子に見えるので、
        # それと分かったら絵を置かずに自前の記号を描く。ArtworkLoader.placeholder は
        # 同じ (key, size, kind) に同じ物を返すので、同一性で見分けられる。
        loader = self._loader
        if texture is not None and loader is not None and self._requested_px:
            try:
                if texture is loader.placeholder(self._key, self._requested_px, "station"):
                    texture = None
            except Exception:  # 見分けられなくても、届いた絵をそのまま出すだけ
                pass
        Artwork._set_texture(self, texture, fade)

    def _wanted_px(self, width=None, height=None) -> int:
        w = width if width else max(self.get_width(), self._width)
        h = height if height else max(self.get_height(), self._height)
        return int(math.ceil(self._slot(w, h) * max(1, self.get_scale_factor())))

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rounded = _rounded(width, height, self._radius)
        bounds = Graphene.Rect().init(0, 0, width, height)
        snapshot.push_rounded_clip(rounded)
        stops = []
        for offset, color in ((0.0, self._ground[0]), (1.0, self._ground[1])):
            stop = Gsk.ColorStop()
            stop.offset = offset
            stop.color = color
            stops.append(stop)
        snapshot.append_linear_gradient(bounds, Graphene.Point().init(0, 0),
                                        Graphene.Point().init(0, height), stops)
        slot = self._slot(width, height)
        x, y = (width - slot) / 2, (height - slot) / 2
        if self._texture is not None:
            inner = Gsk.RoundedRect()
            inner.init_from_rect(Graphene.Rect().init(x, y, slot, slot), slot * 0.16)
            snapshot.append_outset_shadow(inner, _rgba("rgba(0,0,0,0.30)"), 0, 2, 0, 5)
            snapshot.push_rounded_clip(inner)
            snapshot.append_color(_rgba("white"), Graphene.Rect().init(x, y, slot, slot))
            self._append_cover(snapshot, self._texture, x, y, slot, slot)
            snapshot.pop()
        else:
            self._append_glyph(snapshot, width, height)
        snapshot.pop()
        self._append_outline(snapshot, rounded)

    def _append_glyph(self, snapshot: Gtk.Snapshot, width: float, height: float) -> None:
        display = self.get_display()
        if display is None:
            return
        px = round(min(width, height) * 0.34)
        theme = Gtk.IconTheme.get_for_display(display)
        paintable = theme.lookup_icon("music-station-symbolic", None, px, self.get_scale_factor(),
                                      Gtk.TextDirection.NONE, 0)
        snapshot.save()
        snapshot.translate(Graphene.Point().init((width - px) / 2, (height - px) / 2))
        paintable.snapshot_symbolic(snapshot, px, px, [_rgba("rgba(255,255,255,0.88)")])
        snapshot.restore()


class StationTile(_Card):
    """ラジオ局のタイル。色の地に favicon、下に局名と国。

    `StationTile(loader, track, on_activate=None, *, size=150)`
    track は RadioBrowser が返す Track (meta に "art" = favicon、"radio.country")。
    favicon が無ければ局の記号を出す。属性: track, art_height。"""

    def __init__(self, loader, track, on_activate=None, *, size: int = 150):
        super().__init__(on_activate, "music-station-tile")
        self.track = track
        self.art_height = size
        name = getattr(track, "display_title", "") or getattr(track, "title", "")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overlay = Gtk.Overlay()
        self.art = _StationArt(size, name)
        favicon = track.meta_get("art") if hasattr(track, "meta_get") else ""
        if favicon and loader is not None:
            # 曲として頼む (ArtworkLoader は meta の art = favicon を取り、失敗すれば
            # 局 (live) 用の代わりの絵を返す。鍵は track_key で揃える)
            self.art.set_subject(loader, track, key_for_placeholder=track_key(track), kind="station")
        overlay.set_child(self.art)
        scrim = Gtk.Box()
        scrim.add_css_class("music-card-scrim")
        scrim.set_can_target(False)
        overlay.add_overlay(scrim)
        box.append(overlay)
        country = track.meta_get("radio.country") if hasattr(track, "meta_get") else ""
        self.title_label = _label(name, "music-card-title")
        self.subtitle_label = _label(country, "music-card-subtitle")
        self.subtitle_label.set_visible(bool(country))
        box.append(self.title_label)
        box.append(self.subtitle_label)
        box.set_size_request(size, -1)
        self.set_child(box)
        self.set_tooltip_text(name)


# --------------------------------------------------------------------------
# 曲の行


class TrackRow(Gtk.ListBoxRow):
    """曲の行。

    `TrackRow(ctx, track, *, variant="album", number=None, index=None,
              menu_context="", on_activate=None, show_album=True, extra_text=None)`
    - variant:
      - "album": 43px。番号 | 曲名 | アーティスト (— アルバム) | 時間 | 「…」。
        区切り線は曲名の列から。お気に入り (bookmark) は左の余白に赤い ★。
      - "list": 52px。絵 40px | 曲名と副題 | extra_text (「3 時間前」) | 時間 | 「…」。
      - "queue": 48px。絵 38px | 曲名 (13px) と副題 (11px) | 「…」。
    - number: 番号の表示 (省くと index + 1)。index: リスト上の添字 (メニューにも渡す)。
    - menu_context: ctx.track_menu に渡す context ("nowplaying" / "queue" / "local:<名前>")。
    - show_album: 副題を「アーティスト — アルバム」にするか、アーティストだけにするか。
    - extra_text: 時間の左に添える短い文。"list" で省く (None) と、曲に played_at が
      あれば「3 時間前」を自分で出す。出したくなければ "" を渡す。
    - on_activate(row): ダブルクリックか Enter で呼ばれる (TrackList が呼ぶ)。
    `set_current(is_current, playing)`: 再生中の曲なら番号を赤い動く棒に替え、曲名を赤くする。
    「…」は乗せたときに出る。右クリックでも同じメニューを出す。
    メニューは開くたびに ctx.track_menu(track, index=, context=) で作り、
    返った action group を行へ "track" として差し込む。"""

    HEIGHTS = {"album": 43, "list": 52, "queue": 48}

    def __init__(self, ctx, track, *, variant: str = "album", number: int | None = None,
                 index: int | None = None, menu_context: str = "", on_activate=None,
                 show_album: bool = True, extra_text: str | None = None):
        if variant not in self.HEIGHTS:
            raise ValueError(f"TrackRow: 知らない variant です: {variant}")
        super().__init__()
        self.ctx = ctx
        self.track = track
        self.variant = variant
        self.index = index
        self.number = number if number is not None else (index + 1 if index is not None else None)
        self.menu_context = menu_context
        self.on_activate = on_activate
        self._current = False
        self._playing = False
        self._indicator: PlayingIndicator | None = None
        self._context_popover = None

        self.add_css_class("music-track-row")
        self.add_css_class(variant)
        if getattr(track, "unplayable", False):
            self.add_css_class("unplayable")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.set_child(outer)

        title = getattr(track, "display_title", "") or getattr(track, "title", "")
        if show_album:
            subtitle = getattr(track, "subtitle", "") or getattr(track, "artist", "")
        else:
            subtitle = getattr(track, "artist", "")

        if variant == "album":
            gutter = Gtk.Image.new_from_icon_name("music-star-symbolic")
            gutter.set_pixel_size(9)
            gutter.add_css_class("music-track-favorite")
            gutter.set_size_request(14, -1)
            gutter.set_opacity(1.0 if getattr(track, "bookmark", False) else 0.0)
            outer.append(gutter)
            self._lead = Gtk.Stack()
            self._lead.set_size_request(28, -1)
            self._lead.set_transition_type(Gtk.StackTransitionType.NONE)
            self._number_label = _label("" if self.number is None else str(self.number),
                                        "music-track-number", xalign=1.0, ellipsize=False)
            self._lead.add_named(self._number_label, "number")
            outer.append(self._lead)
        else:
            art_size = 40 if variant == "list" else 38
            self._lead = Gtk.Overlay()
            self._lead.set_valign(Gtk.Align.CENTER)
            self.art = Artwork(art_size, art_size, radius=4)
            loader = getattr(ctx, "artwork", None)
            if loader is not None:
                self.art.set_subject(loader, track)
            self._lead.set_child(self.art)
            self._scrim = Gtk.Box()
            self._scrim.add_css_class("music-track-art-scrim")
            self._scrim.set_visible(False)
            self._lead.add_overlay(self._scrim)
            outer.append(self._lead)

        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        main.add_css_class("music-track-main")
        main.set_hexpand(True)
        main.set_margin_start(12)
        outer.append(main)

        if variant == "album":
            columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, homogeneous=bool(subtitle))
            columns.set_hexpand(True)
            self.title_label = _label(title, "music-track-title")
            self.title_label.set_hexpand(True)
            columns.append(self.title_label)
            self.subtitle_label = _label(subtitle, "music-track-subtitle")
            self.subtitle_label.set_hexpand(True)
            self.subtitle_label.set_visible(bool(subtitle))
            columns.append(self.subtitle_label)
            main.append(columns)
        else:
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            texts.set_valign(Gtk.Align.CENTER)
            texts.set_hexpand(True)
            self.title_label = _label(title, "music-track-title")
            self.subtitle_label = _label(subtitle, "music-track-subtitle")
            self.subtitle_label.set_visible(bool(subtitle))
            texts.append(self.title_label)
            texts.append(self.subtitle_label)
            main.append(texts)

        if extra_text is None and variant == "list" and getattr(track, "played_at", ""):
            # 履歴の曲 (played_at 付き) は「3 時間前」を自分で添える
            extra_text = relative_time(track.played_at)
        self.extra_label = _label(extra_text or "", "music-track-extra", xalign=1.0, ellipsize=False)
        self.extra_label.set_visible(bool(extra_text))
        main.append(self.extra_label)

        self.duration_label = _label(self._duration_text(track), "music-track-duration",
                                     xalign=1.0, ellipsize=False)
        self.duration_label.set_width_chars(5)
        self.duration_label.set_visible(variant != "queue" and bool(self.duration_label.get_text()))
        main.append(self.duration_label)

        self.more = Gtk.MenuButton()
        self.more.set_icon_name("music-more-symbolic")
        self.more.set_has_frame(False)
        self.more.add_css_class("music-row-more")
        self.more.set_valign(Gtk.Align.CENTER)
        self.more.set_tooltip_text("その他")
        self.more.set_create_popup_func(TrackRow._create_popup)
        main.append(self.more)

        self.set_size_request(-1, self.HEIGHTS[variant])
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", TrackRow._on_secondary)
        self.add_controller(click)

    @staticmethod
    def _duration_text(track) -> str:
        if getattr(track, "live", False):
            return "ライブ"
        duration = getattr(track, "duration", 0) or 0
        return format_time(duration) if duration > 0 else ""

    # 状態 -----------------------------------------------------------------

    @property
    def is_current(self) -> bool:
        return self._current

    def set_current(self, is_current: bool, playing: bool) -> None:
        is_current, playing = bool(is_current), bool(playing)
        if is_current == self._current and playing == self._playing:
            return
        self._current, self._playing = is_current, playing
        if is_current:
            self.add_css_class("current")
        else:
            self.remove_css_class("current")
        if is_current and self._indicator is None:
            self._indicator = PlayingIndicator(13 if self.variant == "album" else 16)
            self._indicator.set_halign(Gtk.Align.END if self.variant == "album" else Gtk.Align.CENTER)
            self._indicator.set_valign(Gtk.Align.CENTER)
            if self.variant == "album":
                self._indicator.set_margin_end(3)
                self._lead.add_named(self._indicator, "indicator")
            else:
                self._lead.add_overlay(self._indicator)
        if self._indicator is not None:
            self._indicator.set_playing(is_current and playing)
            if self.variant == "album":
                self._lead.set_visible_child_name("indicator" if is_current else "number")
            else:
                self._indicator.set_visible(is_current)
                self._scrim.set_visible(is_current)

    def set_number(self, number: int | None) -> None:
        self.number = number
        if self.variant == "album":
            self._number_label.set_text("" if number is None else str(number))

    def set_extra_text(self, text: str | None) -> None:
        self.extra_label.set_text(text or "")
        self.extra_label.set_visible(bool(text))

    # メニュー -------------------------------------------------------------

    def _menu_model(self):
        track_menu = getattr(self.ctx, "track_menu", None)
        if track_menu is None:
            return None
        model, group = track_menu(self.track, index=self.index, context=self.menu_context)
        self.insert_action_group("track", group)
        return model

    # 子の MenuButton と GestureClick には行 (self) を掴む関数を渡さない (行が解放されなくなる)。
    # クラスの関数で受け、相手から行を引く

    @staticmethod
    def _create_popup(button, *_args) -> None:
        row = button.get_ancestor(TrackRow)
        button.set_menu_model(row._menu_model() if row is not None else None)

    @staticmethod
    def _on_secondary(gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        row = gesture.get_widget()
        if isinstance(row, TrackRow) and row.popup_context_menu(x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def popup_context_menu(self, x: float, y: float) -> bool:
        """行の (x, y) を指して「…」と同じメニューを出す (右クリック)。出せたら True。"""
        model = self._menu_model()
        if model is None:
            return False
        if self._context_popover is not None:
            self._context_popover.unparent()
        popover = Gtk.PopoverMenu.new_from_model(model)
        popover.set_parent(self)
        popover.set_has_arrow(False)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.connect("closed", _weak(self._on_context_closed))
        self._context_popover = popover
        popover.popup()
        return True

    def _on_context_closed(self, popover) -> None:
        # 項目の action は閉じた後に走ることがあるので、外すのは一呼吸おいてから
        def drop():
            if self._context_popover is popover:
                self._context_popover = None
            if popover.get_parent() is not None:
                popover.unparent()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(drop)

    def do_dispose(self) -> None:
        popover = getattr(self, "_context_popover", None)
        if popover is not None:
            popover.unparent()
            self._context_popover = None
        Gtk.ListBoxRow.do_dispose(self)


class TrackList(Gtk.ListBox):
    """曲の行を並べる一覧。

    `TrackList(selectable=True)`
    - 1 回のクリックでは選ぶだけ、ダブルクリックか Enter で行の on_activate(row) を呼ぶ。
    - 区切り線は CSS で曲名の列から引く (最後の行には引かない)。
    - `set_current_index(i, playing)`: 行の index (無ければ並び順) が i の行を再生中にする。
      i が None か負なら、どの行も再生中にしない。"""

    def __init__(self, selectable: bool = True):
        super().__init__()
        self.set_activate_on_single_click(False)
        self.set_selection_mode(Gtk.SelectionMode.SINGLE if selectable else Gtk.SelectionMode.NONE)
        self.add_css_class("music-track-list")
        self.connect("row-activated", self._on_row_activated)

    @staticmethod
    def _on_row_activated(_list, row) -> None:
        invoke(getattr(row, "on_activate", None), row)

    def rows(self) -> list[Gtk.ListBoxRow]:
        out = []
        child = self.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.ListBoxRow):
                out.append(child)
            child = child.get_next_sibling()
        return out

    def set_current_index(self, index: int | None, playing: bool) -> None:
        target = -1 if index is None else int(index)
        for position, row in enumerate(self.rows()):
            if not hasattr(row, "set_current"):
                continue
            row_index = row.index if getattr(row, "index", None) is not None else position
            row.set_current(target >= 0 and row_index == target, playing)

    def current_row(self):
        for row in self.rows():
            if getattr(row, "is_current", False):
                return row
        return None


# --------------------------------------------------------------------------
# 空・読み込み中


class EmptyState(Gtk.Box):
    """何も無いときや失敗したときの表示 (中央に記号・題・説明・ボタン)。

    `EmptyState(icon_name, title, description=None, button_label=None, on_button=None)`
    ボタンは赤で塗ったカプセル (画面で 1 つだけの色付きの操作)。
    `set_title(text)` / `set_description(text)` / `set_icon_name(name)`。属性 button。"""

    def __init__(self, icon_name: str, title: str, description: str | None = None,
                 button_label: str | None = None, on_button=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("music-empty")
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_vexpand(True)
        self.image = Gtk.Image.new_from_icon_name(icon_name)
        self.image.set_pixel_size(48)
        self.image.add_css_class("music-empty-icon")
        self.image.set_margin_bottom(8)
        self.append(self.image)
        self.title_label = _label(title, "music-empty-title", xalign=0.5, ellipsize=False)
        self.title_label.set_wrap(True)
        self.title_label.set_justify(Gtk.Justification.CENTER)
        self.title_label.set_max_width_chars(30)
        self.append(self.title_label)
        self.description_label = _label(description or "", "music-empty-description",
                                        xalign=0.5, ellipsize=False)
        self.description_label.set_wrap(True)
        self.description_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.description_label.set_justify(Gtk.Justification.CENTER)
        self.description_label.set_max_width_chars(42)
        self.description_label.set_visible(bool(description))
        self.append(self.description_label)
        self.on_button = on_button
        self.button = None
        if button_label:
            self.button = CapsuleButton(button_label, accent_text=False, filled=True)
            self.button.set_halign(Gtk.Align.CENTER)
            self.button.set_margin_top(14)
            self.button.connect("clicked", EmptyState._on_button_clicked)
            self.append(self.button)

    @staticmethod
    def _on_button_clicked(button) -> None:
        state = button.get_ancestor(EmptyState)
        if state is not None:
            invoke(state.on_button, state)

    def set_title(self, text: str) -> None:
        self.title_label.set_text(text or "")

    def set_description(self, text: str | None) -> None:
        self.description_label.set_text(text or "")
        self.description_label.set_visible(bool(text))

    def set_icon_name(self, icon_name: str) -> None:
        self.image.set_from_icon_name(icon_name)


class LoadingState(Gtk.Box):
    """読み込み中の表示 (中央にスピナーと短い文)。`LoadingState(label=None)`。"""

    def __init__(self, label: str | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add_css_class("music-loading")
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_vexpand(True)
        self.spinner = Adw.Spinner()
        self.spinner.set_size_request(28, 28)
        self.spinner.set_halign(Gtk.Align.CENTER)
        self.append(self.spinner)
        self.label = _label(label or "", "music-loading-label", xalign=0.5, ellipsize=False)
        self.label.set_visible(bool(label))
        self.append(self.label)

    def set_label(self, text: str | None) -> None:
        self.label.set_text(text or "")
        self.label.set_visible(bool(text))
