"""ホーム (HomePage) と、各ページが共通で使う土台。

ページのファイルの割り当ての都合で、ページの共通部分 (PageBase・DetailHeader・
弱い呼び出し・プレイリストの絵など) もここに置く。ほかのページは
`from .home import PageBase, …` として使う。

ページの形 (DESIGN.md §3):
    Adw.NavigationPage (.music-page)
    └ Adw.BreakpointBin (狭い幅の切り替え)
      └ Adw.ToolbarView (extend-content-to-top-edge)
        ├ 上: Adw.HeaderBar (.music-page-header、透明。スクロールすると地の色がにじむ)
        └ Gtk.ScrolledWindow
          └ body (縦の箱。下端に再生バーの分の余白 96px)

PyGObject では、子の部品のシグナルがページ (self) を強く掴むと、ページを
外してもページごと解放されない (GObject の closure を GC が辿れないため)。
ページから子や長生きする物 (store / ctx / catalog) へ渡す呼び出しは
weak_call / weak_handler で包み、store と ctx のシグナルは表示中 (map) の間だけ繋ぐ。
"""

from __future__ import annotations

import random
import sys
import weakref
from typing import Callable, Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

import cairo  # noqa: E402

from .. import log  # noqa: E402
from ..artwork import art_sources, texture_from_surface  # noqa: E402
from ..protocol import (  # noqa: E402
    RECENTLY_PLAYED,
    PlaylistInfo,
    Response,
    Source,
    Track,
    track_key,
)
from ..widgets import (  # noqa: E402
    Artwork,
    CapsuleButton,
    CircleButton,
    EmptyState,
    LoadingState,
    MediaCard,
    PageTitle,
    Shelf,
    StationTile,
    TallCard,
    format_count,
    format_total_duration,
)

# 左右の余白 (Apple の画面で 37〜40pt)。棚はこの幅を inset にして端まで広げる。
SIDE = 40
# 詳細ページの曲の行 (album) の左の余白。お気に入りの ★ の欄 (14px) と行の内側の
# 余白 (4px) を絵の左端より外に出し、番号を絵の左端の少し内側に揃える (Apple と同じ)。
DETAIL_LIST_SIDE = SIDE - 18
# 下端の余白 (浮かぶ再生バーの分)。
BOTTOM = 96
# 狭い幅に切り替える境目。
NARROW = "max-width: 640sp"

UNSUPPORTED_TITLE = "この cliamp は拡張 IPC に対応していません"
UNSUPPORTED_TEXT = "再生の操作だけ使えます。"
OFFLINE_TITLE = "cliamp に接続できません"
OFFLINE_TEXT = "cliamp が動いていないか、ソケットが見つかりません。"

# プロバイダーの表示名 (cliamp の名前より日本語の画面に馴染むもの)。
PROVIDER_LABELS = {
    "local": "ローカル",
    "spotify": "Spotify",
    "youtube": "YouTube",
    "ytmusic": "YouTube Music",
    "url": "YouTube",
    "radio": "ラジオ",
    "radio-browser": "ラジオ",
    "soundcloud": "SoundCloud",
    "navidrome": "Navidrome",
    "jellyfin": "Jellyfin",
    "plex": "Plex",
    "library": "ライブラリ",
    "history": "ライブラリ",
}

RECENT_SOURCE_NAME = "最近再生した項目"


# --------------------------------------------------------------------------
# 弱い呼び出し


def weak_call(method: Callable, *bound) -> Callable:
    """method (ページのメソッド) を弱く持つ呼び出し。受け取った引数は捨て、bound を渡す。

    子の部品のシグナルや on_activate に渡す。ページが解放されていれば何もしない。
    GLib.timeout_add に渡すと、戻り値 None で 1 回きりになる。"""
    ref = weakref.WeakMethod(method)

    def call(*_args):
        target = ref()
        if target is not None:
            return target(*bound)
        return None

    return call


def weak_handler(method: Callable) -> Callable:
    """method を弱く持つシグナルの受け手。受け取った引数をそのまま渡す。"""
    ref = weakref.WeakMethod(method)

    def handler(*args):
        target = ref()
        if target is not None:
            return target(*args)
        return None

    return handler


# --------------------------------------------------------------------------
# 小道具


def provider_label(ctx, key: str) -> str:
    """プロバイダーの表示名。知らないものは cliamp の名前 (覚えていれば) か鍵そのもの。"""
    if key in PROVIDER_LABELS:
        return PROVIDER_LABELS[key]
    names = getattr(ctx, "_provider_names", None) or {}
    return names.get(key) or key


def remember_provider_names(ctx, providers) -> None:
    """providers の結果から表示名を覚えておく (provider_label が使う)。"""
    try:
        names = {info.key: info.name for info in providers if info.name}
    except TypeError:
        return
    ctx._provider_names = {**(getattr(ctx, "_provider_names", None) or {}), **names}


def keep_together(text: str) -> str:
    """text の文字の間で改行させない (語の結合子 U+2060 を挟む)。

    日本語は文字ごとに改行できるので、2 行に折り返す題が「…のステーショ / ン」の
    ように切れる。後ろの決まり文句をひと塊にして、手前の空白で折り返させる。"""
    return "\u2060".join(text)


def station_title(track: Track) -> str:
    """「<アーティスト> のステーション」(2 行に折り返すときは「のステーション」の前で)。"""
    return f"{track.artist or track.display_title} {keep_together('のステーション')}"


def is_playing(status) -> bool:
    """再生中の印を動かすか。読み込み中 (stopped + buffering) も動かす。"""
    return status.state == "playing" or (status.state == "stopped" and status.buffering)


def unique_tracks(tracks: Iterable[Track], limit: int | None = None) -> list[Track]:
    """同じ曲 (track_key) を 1 つにした並び。先のものを残す。"""
    seen: set[str] = set()
    out: list[Track] = []
    for track in tracks:
        key = track_key(track)
        if key in seen:
            continue
        seen.add(key)
        out.append(track)
        if limit is not None and len(out) >= limit:
            break
    return out


def total_duration(tracks: Iterable[Track]) -> int:
    return sum(t.duration for t in tracks if t.duration > 0 and not t.live)


def info_line(count: int, seconds: float) -> str:
    """「12 曲 · 48 分」。"""
    parts = [format_count(count)]
    duration = format_total_duration(seconds)
    if duration:
        parts.append(duration)
    return " · ".join(parts)


def shuffle_tracks(ctx, tracks: Iterable[Track], source: Source | None = None) -> None:
    """無作為な曲から再生し、cliamp のシャッフルを入れる。

    replace の後でシャッフルを入れると、cliamp は今の曲を先頭にして残りを混ぜる。
    2 つの要求は別の接続で送られるので、replace の応答を待ってから入れる。"""
    tracks = list(tracks)
    if not tracks:
        return
    if not ctx.store.supports("replace"):
        ctx.toast("この cliamp は拡張 IPC に対応していません")
        return
    start = random.randrange(len(tracks))
    store = ctx.store

    def done(response: Response) -> None:
        if not response.ok:
            ctx.toast(f"再生できませんでした: {response.message}")
        elif not store.status.shuffle:
            store.set_shuffle(True)

    store.replace(tracks, start, source, callback=done)


def shuffle_provider(ctx, provider: str, id: str, count: int, name: str = "") -> None:
    """プロバイダーのリストを無作為な曲から読み込み、シャッフルを入れる。"""
    if not ctx.store.supports("load_provider"):
        ctx.toast("この cliamp は拡張 IPC に対応していません")
        return
    start = random.randrange(count) if count > 0 else 0
    store = ctx.store

    def done(response: Response) -> None:
        if not response.ok:
            ctx.toast(f"読み込めませんでした: {response.message}")
        elif not store.status.shuffle:
            store.set_shuffle(True)

    ctx.catalog.load(provider, id, start, name, callback=done)


def _label(text: str = "", css: str | tuple[str, ...] = (), *, xalign: float = 0.0,
           wrap: bool = False, lines: int = 0) -> Gtk.Label:
    """文字は set_text で入れる (曲名やプレイリスト名に & や < が入るため)。"""
    label = Gtk.Label()
    label.set_text(text or "")
    label.set_xalign(xalign)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        if lines:
            label.set_lines(lines)
    for cls in (css,) if isinstance(css, str) else css:
        label.add_css_class(cls)
    return label


def text_button(label: str, on_click: Callable, css: str = "music-text-link") -> Gtk.Button:
    """文字だけのボタン (見出しの右の赤い「消去」など)。"""
    button = Gtk.Button()
    child = Gtk.Label()
    child.set_text(label)
    button.set_child(child)
    button.add_css_class("flat")
    button.add_css_class(css)
    button.set_valign(Gtk.Align.CENTER)
    button.connect("clicked", on_click)
    return button


def make_grid(*, row_spacing: int = 24, column_spacing: int = 18, max_per_line: int = 12) -> Gtk.FlowBox:
    """カードやタイルの格子 (flowbox.music-grid)。"""
    grid = Gtk.FlowBox()
    grid.add_css_class("music-grid")
    grid.set_selection_mode(Gtk.SelectionMode.NONE)
    grid.set_activate_on_single_click(False)
    grid.set_homogeneous(True)
    grid.set_row_spacing(row_spacing)
    grid.set_column_spacing(column_spacing)
    grid.set_min_children_per_line(2)
    grid.set_max_children_per_line(max_per_line)
    grid.set_valign(Gtk.Align.START)
    return grid


class _NaturalLayout(Gtk.LayoutManager):
    """子の寸法をそのまま使い、横の自然幅だけ natural に揃える (最小は子のまま)。"""

    def __init__(self, natural: int):
        super().__init__()
        self.natural = int(natural)

    def do_get_request_mode(self, _widget) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, widget, orientation, for_size):
        child = widget.get_child()
        if child is None or not child.should_layout():
            return 0, 0, -1, -1
        minimum, natural, min_base, nat_base = child.measure(orientation, for_size)
        if orientation == Gtk.Orientation.HORIZONTAL:
            natural = max(minimum, self.natural)
        return minimum, natural, min_base, nat_base

    def do_allocate(self, widget, width: int, height: int, baseline: int) -> None:
        child = widget.get_child()
        if child is not None and child.should_layout():
            child.allocate(width, height, baseline, None)


class NaturalWidth(Adw.Bin):
    """子を 1 つ包み、横の自然幅を natural と申告する (狭いときは子の最小まで縮む)。

    ヘッダーの中央の検索欄 (幅 380、狭い窓では縮む) に使う。後始末は Adw.Bin に任せる。"""

    def __init__(self, child: Gtk.Widget, natural: int):
        super().__init__()
        self.set_layout_manager(_NaturalLayout(natural))
        self.set_child(child)


def search_entry(placeholder: str) -> Gtk.Entry:
    """カプセルの検索欄 (Gtk.SearchEntry はテーマの虫眼鏡が崩れるので使わない)。

    左に music-search-symbolic、文字があるときだけ右に消去の記号 (押すと空にする)。"""
    entry = Gtk.Entry()
    entry.add_css_class("search")
    entry.add_css_class("music-search-entry")
    entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "music-search-symbolic")
    entry.set_icon_activatable(Gtk.EntryIconPosition.PRIMARY, False)
    entry.set_placeholder_text(placeholder)
    entry.set_width_chars(6)
    entry.set_input_hints(Gtk.InputHints.NO_SPELLCHECK)
    entry.connect("changed", _update_clear_icon)
    entry.connect("icon-release", _clear_on_icon)
    return entry


def _update_clear_icon(entry: Gtk.Entry) -> None:
    has_text = bool(entry.get_text())
    current = entry.get_icon_name(Gtk.EntryIconPosition.SECONDARY)
    if has_text and not current:
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "music-clear-symbolic")
        entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "消去")
    elif not has_text and current:
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, None)


def _clear_on_icon(entry: Gtk.Entry, position, *_args) -> None:
    if position == Gtk.EntryIconPosition.SECONDARY:
        entry.set_text("")
        entry.grab_focus()


def clear_box(box: Gtk.Widget) -> None:
    """箱 (Gtk.Box / FlowBox / ListBox) の子をすべて外す。"""
    if hasattr(box, "remove_all"):
        box.remove_all()
        return
    child = box.get_first_child()
    while child is not None:
        following = child.get_next_sibling()
        box.remove(child)
        child = following


# --------------------------------------------------------------------------
# 絵 (プレイリストの 2x2 など)


def _texture_surface(texture: Gdk.Texture) -> cairo.ImageSurface:
    downloader = Gdk.TextureDownloader.new(texture)
    fmt = (Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED if sys.byteorder == "little"
           else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED)
    downloader.set_format(fmt)
    data, stride = downloader.download_bytes()
    buffer = bytearray(data.get_data())
    return cairo.ImageSurface.create_for_data(buffer, cairo.FORMAT_ARGB32,
                                              texture.get_width(), texture.get_height(), stride)


def compose_mosaic(textures: list[Gdk.Texture], size: int) -> Gdk.Texture:
    """4 枚の絵を 2x2 に並べた正方形の絵 (各枚は中央を覆うように切り抜く)。"""
    size = max(2, int(size))
    half = size / 2
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    for i, texture in enumerate(textures[:4]):
        x, y = (i % 2) * half, (i // 2) * half
        source = _texture_surface(texture)
        tw, th = source.get_width(), source.get_height()
        if tw <= 0 or th <= 0:
            continue
        scale = max(half / tw, half / th)
        cr.save()
        cr.rectangle(x, y, half, half)
        cr.clip()
        cr.translate(x + (half - tw * scale) / 2, y + (half - th * scale) / 2)
        cr.scale(scale, scale)
        cr.set_source_surface(source, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
    return texture_from_surface(surface)


def art_tracks(tracks: Iterable[Track], limit: int = 4) -> list[Track]:
    """絵の出どころがある曲を、同じ絵 (同じ曲・同じ URL) を除いて先頭から。"""
    out: list[Track] = []
    seen: set[str] = set()
    for track in tracks:
        sources = art_sources(track)
        if not sources:
            continue
        key = sources[0]
        if key in seen or track_key(track) in seen:
            continue
        seen.add(key)
        seen.add(track_key(track))
        out.append(track)
        if len(out) >= limit:
            break
    return out


def set_playlist_art(ctx, art: Artwork, tracks: list[Track], placeholder_key: str, size: int) -> None:
    """プレイリストの絵: 絵のある曲が 4 曲以上なら 2x2、1〜3 曲なら先頭の曲、無ければ代わりの絵。

    size は絵の一辺 (論理 px)。2x2 は倍率 (scale factor) を掛けた画素数で作る。"""
    loader = ctx.artwork
    picks = art_tracks(tracks)
    if len(picks) >= 4:
        art.set_subject(loader, ("placeholder", placeholder_key, "playlist"), kind="playlist")
        px = int(max(1, size) * max(1, art.get_scale_factor()))
        half = max(1, (px + 1) // 2)
        got: list[Gdk.Texture | None] = [None] * 4
        remaining = {"n": 4}
        art_ref = weakref.ref(art)
        token = object()
        art._mosaic_token = token

        def arrived(i: int, texture: Gdk.Texture) -> None:
            got[i] = texture
            remaining["n"] -= 1
            if remaining["n"] > 0:
                return
            target = art_ref()
            if target is None or getattr(target, "_mosaic_token", None) is not token:
                return
            try:
                target.set_texture(compose_mosaic([t for t in got if t is not None], px))
            except Exception as exc:  # 絵は飾り。失敗しても代わりの絵のまま
                log(f"プレイリストの絵を作れません: {exc}")

        for i, track in enumerate(picks[:4]):
            loader.request(track, half, lambda texture, i=i: arrived(i, texture))
    elif picks:
        art._mosaic_token = None
        art.set_subject(loader, picks[0])
    else:
        art._mosaic_token = None
        art.set_subject(loader, ("placeholder", placeholder_key, "playlist"), kind="playlist")


# --------------------------------------------------------------------------
# ページの土台


class PageBase(Adw.NavigationPage):
    """すべてのページの土台。

    - `self.ctx`、`self.page_id` (= タグ)、`self.header_bar` (窓がボタンを足してよい)、
      `self.body` (縦の箱。ここに節を積む)、`self.scroller`。
    - 初めて表示されたときに `load(force=False)` を呼ぶ。`refresh()` (Ctrl+R) は
      `load(force=True)`。隠れている間に refresh されたら、次に表示されたときに読む。
    - `watch(obj, signal, method)` で繋いだシグナルは、表示中だけ繋がる
      (map で繋ぎ、unmap で外す。繋いだときに `on_resync()` を呼んで追いつく)。
    - 大見出しのページは、見出しがスクロールで隠れるとヘッダーに小さな題を出す。
    """

    PAGE_ID = ""
    TITLE = ""

    def __init__(self, ctx, *, title: str | None = None, tag: str | None = None,
                 large_title: bool = True):
        title = title if title is not None else self.TITLE
        super().__init__(title=title, tag=tag or self.PAGE_ID)
        self.ctx = ctx
        self.page_id = self.PAGE_ID
        self._watch_specs: list[tuple[GObject.Object, str, Callable]] = []
        self._handler_ids: list[tuple[GObject.Object, int]] = []
        self._loaded = False
        self._stale = False
        self._narrow = False
        self._large_title = large_title
        self.add_css_class("music-page")

        self.header_bar = Adw.HeaderBar()
        self.header_bar.add_css_class("music-page-header")
        self.header_title = _label(title, "music-header-title", xalign=0.5)
        self.header_title.set_max_width_chars(40)
        self.header_bar.set_title_widget(self.header_title)
        # 戻るは Apple と同じガラスの丸「‹」。前のページがあるときだけ出す (map で見直す)
        self.header_bar.set_show_back_button(False)
        self.back_button = CircleButton("music-back-symbolic", "戻る", 34, glass=True)
        self.back_button.add_css_class("music-back-button")
        self.back_button.set_action_name("navigation.pop")
        self.back_button.set_visible(False)
        self.header_bar.pack_start(self.back_button)

        self.toolbar = Adw.ToolbarView()
        self.toolbar.set_extend_content_to_top_edge(True)
        self.toolbar.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        self.toolbar.add_top_bar(self.header_bar)

        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.body.add_css_class("music-page-body")
        self.body.set_margin_bottom(BOTTOM)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add_css_class("music-page-scroller")
        self.scroller.set_child(self.body)
        self.toolbar.set_content(self.scroller)

        self._breakpoints = Adw.BreakpointBin()
        self._breakpoints.set_size_request(300, 200)
        self._breakpoints.set_child(self.toolbar)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW))
        breakpoint.connect("apply", weak_call(self._set_narrow, True))
        breakpoint.connect("unapply", weak_call(self._set_narrow, False))
        self._breakpoints.add_breakpoint(breakpoint)
        self.set_child(self._breakpoints)

        self._margin_idle = 0
        # ヘッダーの高さ (CSS の min-height 52) に合わせた初めの値。実際の高さは後で合わせる
        self.body.set_margin_top(54)
        self.scroller.get_vadjustment().connect("value-changed", weak_call(self._on_scrolled))
        self.toolbar.connect("notify::top-bar-height", weak_call(self._update_top_margin))
        self._on_scrolled()

    # --- 部品 ---------------------------------------------------------------

    def add_large_title(self, text: str | None = None) -> PageTitle:
        title = PageTitle(text if text is not None else self.get_title())
        title.add_css_class("music-page-large-title")
        self.inset(title)
        self.body.append(title)
        self.large_title = title
        return title

    @staticmethod
    def inset(widget: Gtk.Widget, side: int = SIDE) -> Gtk.Widget:
        widget.set_margin_start(side)
        widget.set_margin_end(side)
        return widget

    def make_shelf(self, title: str, on_more=None) -> Shelf:
        shelf = Shelf(title, on_more, inset=SIDE)
        shelf.add_css_class("music-page-shelf")
        return shelf

    # --- 狭い幅 ---------------------------------------------------------------

    @property
    def narrow(self) -> bool:
        return self._narrow

    def _set_narrow(self, narrow: bool) -> None:
        if narrow == self._narrow:
            return
        self._narrow = narrow
        if narrow:
            self.add_css_class("narrow")
        else:
            self.remove_css_class("narrow")
        self.on_narrow_changed(narrow)

    def on_narrow_changed(self, narrow: bool) -> None:
        """狭い幅に切り替わった (派生で上書き)。"""

    # --- スクロールとヘッダー ---------------------------------------------------------

    def _update_top_margin(self) -> None:
        # top-bar-height は ToolbarView の割り当ての最中に変わる。その場で余白を変えると
        # 測り直しの前に割り当てることになる (Gtk-WARNING) ので、一呼吸おいてから変える
        if not self._margin_idle:
            self._margin_idle = GLib.idle_add(weak_call(self._apply_top_margin))

    def _apply_top_margin(self) -> bool:
        self._margin_idle = 0
        height = self.toolbar.get_top_bar_height()
        margin = max(height, 38) + 2
        if self.body.get_margin_top() != margin:
            self.body.set_margin_top(margin)
        return GLib.SOURCE_REMOVE

    def _on_scrolled(self) -> None:
        value = self.scroller.get_vadjustment().get_value()
        if value > 1:
            self.header_bar.add_css_class("scrolled")
        else:
            self.header_bar.remove_css_class("scrolled")
        if self._large_title:
            shown = value > 44
            if shown:
                self.header_title.add_css_class("shown")
            else:
                self.header_title.remove_css_class("shown")

    def scroll_to_top(self) -> None:
        self.scroller.get_vadjustment().set_value(0)

    # --- シグナル (表示中だけ繋ぐ) ----------------------------------------------------

    def watch(self, obj: GObject.Object, signal: str, method: Callable) -> None:
        """obj の signal を、表示中だけ method に繋ぐ (ページは弱く持つ)。"""
        spec = (obj, signal, method)
        self._watch_specs.append(spec)
        if self.get_mapped():
            self._connect_spec(spec)

    def _connect_spec(self, spec) -> None:
        obj, signal, method = spec
        handler_id = obj.connect(signal, weak_handler(method))
        self._handler_ids.append((obj, handler_id))

    def _disconnect_all(self) -> None:
        ids, self._handler_ids = self._handler_ids, []
        for obj, handler_id in ids:
            try:
                if GObject.signal_handler_is_connected(obj, handler_id):
                    obj.disconnect(handler_id)
            except (TypeError, ValueError):
                pass

    def _update_back_button(self) -> None:
        nav = self.get_ancestor(Adw.NavigationView)
        self.back_button.set_visible(nav is not None and nav.get_previous_page(self) is not None)

    def do_map(self) -> None:
        Adw.NavigationPage.do_map(self)
        self._update_back_button()
        if not self._handler_ids:
            for spec in self._watch_specs:
                self._connect_spec(spec)
        if not self._loaded:
            self._loaded = True
            self._stale = False
            self.load(force=False)
        elif self._stale:
            self._stale = False
            self.load(force=True)
        else:
            self.on_resync()

    def do_unmap(self) -> None:
        self._disconnect_all()
        self.on_hidden()
        Adw.NavigationPage.do_unmap(self)

    def do_unrealize(self) -> None:
        self._disconnect_all()
        Adw.NavigationPage.do_unrealize(self)

    # --- 読み込み (派生で上書き) ------------------------------------------------------

    def load(self, force: bool = False) -> None:
        """中身を読み込む。force は覚えている結果を使わない (Ctrl+R)。"""

    def on_resync(self) -> None:
        """隠れていた後で再び表示された。隠れている間の変化に追いつく。"""

    def on_hidden(self) -> None:
        """隠れた (タイマーを止めるなど)。"""

    def refresh(self) -> None:
        """ページを更新する (Ctrl+R)。表示していなければ次に表示したときに読む。"""
        if self.get_mapped():
            self._loaded = True
            self._stale = False
            self.load(force=True)
        elif self._loaded:
            self._stale = True


# --------------------------------------------------------------------------
# 詳細ページの頭 (プレイリスト・再生中のリスト)


class DetailHeader(Gtk.Box):
    """左に 250px の絵、右に題・提供元 (赤)・情報の行・ボタン (Apple のアルバムの頁)。

    ボタン: シャッフルの丸 (34) / 「▶ 再生」カプセル / 「…」の丸。
    on_shuffle / on_play はページの weak_call を渡す。menu_factory は「…」を開くたびに
    呼ばれ、(Gio.MenuModel, Gio.ActionGroup | None) を返す (group は "page" で差し込む)。
    """

    ART = 250
    ART_NARROW = 168

    def __init__(self, ctx, *, on_shuffle=None, on_play=None, menu_factory=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=30)
        self.add_css_class("music-detail-header")
        # 中の文字の塊は縦に伸びる (vexpand) が、頭そのものは伸ばさない。伸ばすと
        # 曲の少ないプレイリストで余った高さを頭が吸い、ボタンが絵より下へずれる
        self.set_vexpand(False)
        self.ctx = ctx
        self.art = Artwork(self.ART, self.ART, radius=10, shadow=True)
        self.art.set_valign(Gtk.Align.START)
        self.append(self.art)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        column.set_hexpand(True)
        column.set_valign(Gtk.Align.FILL)
        self.append(column)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        texts.set_vexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        texts.add_css_class("music-detail-texts")
        self.title_label = _label("", "music-detail-title", wrap=True, lines=2)
        self.subtitle_label = _label("", "music-detail-subtitle")
        self.info_label = _label("", ("music-info", "music-detail-info"))
        self.info_label.set_margin_top(6)
        for label in (self.title_label, self.subtitle_label, self.info_label):
            texts.append(label)
        column.append(texts)

        self.buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        self.buttons.set_valign(Gtk.Align.END)
        self.buttons.add_css_class("music-detail-buttons")
        self.shuffle_button = CircleButton("music-shuffle-symbolic", "シャッフル", 34, accent=True)
        self.play_button = CapsuleButton("再生", "music-play-symbolic")
        self.play_button.add_css_class("music-detail-play")
        self.more_button = Gtk.MenuButton()
        self.more_button.set_icon_name("music-more-symbolic")
        self.more_button.add_css_class("music-circle-menu")
        self.more_button.set_tooltip_text("その他")
        self.more_button.set_valign(Gtk.Align.CENTER)
        if on_shuffle is not None:
            self.shuffle_button.connect("clicked", on_shuffle)
        if on_play is not None:
            self.play_button.connect("clicked", on_play)
        self._menu_factory = menu_factory
        if menu_factory is not None:
            self.more_button.set_create_popup_func(DetailHeader._create_popup)
        else:
            self.more_button.set_visible(False)
        for widget in (self.shuffle_button, self.play_button, self.more_button):
            self.buttons.append(widget)
        column.append(self.buttons)

    @staticmethod
    def _create_popup(button: Gtk.MenuButton) -> None:
        header = button.get_ancestor(DetailHeader)
        if header is None or header._menu_factory is None:
            return
        made = header._menu_factory()
        if not made:
            button.set_menu_model(None)
            return
        model, group = made
        if group is not None:
            button.insert_action_group("page", group)
        button.set_menu_model(model)

    def set_title(self, text: str) -> None:
        self.title_label.set_text(text or "")
        self.title_label.set_tooltip_text(text or None)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_label.set_text(text or "")
        self.subtitle_label.set_visible(bool(text))

    def set_info(self, text: str) -> None:
        self.info_label.set_text(text or "")
        self.info_label.set_visible(bool(text))

    def set_actions_sensitive(self, sensitive: bool) -> None:
        self.shuffle_button.set_sensitive(sensitive)
        self.play_button.set_sensitive(sensitive)

    def set_narrow(self, narrow: bool) -> None:
        size = self.ART_NARROW if narrow else self.ART
        self.art.set_size(size, size)
        self.set_spacing(20 if narrow else 30)


class ChunkedRows:
    """TrackList に行を少しずつ足す (数千曲のリストでも画面を止めない)。

    `build(tracks)` で並びを入れ替える。最初の chunk 行はその場で、残りは idle で足す。
    make_row(index, track) は行を返す (ページの weak_handler を渡す)。on_progress(built)
    は足すたびに呼ぶ (再生中の行の印を付け直すため)。"""

    def __init__(self, track_list: Gtk.ListBox, make_row: Callable, *, chunk: int = 120,
                 on_progress: Callable | None = None):
        self.list = track_list
        self.make_row = make_row
        self.chunk = max(1, int(chunk))
        self.on_progress = on_progress
        self.tracks: list[Track] = []
        self.built = 0
        self._serial = 0
        self._idle = 0

    @property
    def done(self) -> bool:
        return self.built >= len(self.tracks)

    def cancel(self) -> None:
        self._serial += 1
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0

    def build(self, tracks: list[Track]) -> None:
        self.cancel()
        self.tracks = list(tracks)
        self.built = 0
        clear_box(self.list)
        self._step(self._serial)

    def _step(self, serial: int) -> bool:
        self._idle = 0
        if serial != self._serial:
            return GLib.SOURCE_REMOVE
        end = min(len(self.tracks), self.built + self.chunk)
        for i in range(self.built, end):
            row = self.make_row(i, self.tracks[i])
            if row is not None:
                self.list.append(row)
        self.built = end
        if self.on_progress is not None:
            self.on_progress(self.built)
        if self.built < len(self.tracks):
            self._idle = GLib.idle_add(self._step, serial, priority=GLib.PRIORITY_LOW)
        return GLib.SOURCE_REMOVE


class AdaptiveGrid(Gtk.FlowBox):
    """カードの大きさを幅に合わせて変える格子 (右端までぴったり並ぶ)。

    列の数は min_size 以上になる最大の数。カード (MediaCard / StationTile) の
    `art.set_size()` と中の箱の幅を揃えて変える。"""

    def __init__(self, *, min_size: int = 150, max_size: int = 210, spacing: int = 18,
                 row_spacing: int = 24):
        super().__init__()
        self.add_css_class("music-grid")
        self.set_selection_mode(Gtk.SelectionMode.NONE)
        self.set_activate_on_single_click(False)
        self.set_homogeneous(True)
        self.set_row_spacing(row_spacing)
        self.set_column_spacing(spacing)
        self.set_min_children_per_line(1)
        self.set_max_children_per_line(30)
        self.set_valign(Gtk.Align.START)
        self.min_size = int(min_size)
        self.max_size = int(max_size)
        self.spacing = int(spacing)
        self.card_size = 0
        self._idle = 0

    def size_for(self, width: int) -> int:
        columns = max(1, (width + self.spacing) // (self.min_size + self.spacing))
        size = (width - self.spacing * (columns - 1)) // columns
        return int(max(1, min(size, self.max_size)))

    def append_card(self, card: Gtk.Widget) -> None:
        if self.card_size:
            self._resize(card, self.card_size)
        self.append(card)

    @staticmethod
    def _resize(card: Gtk.Widget, size: int) -> None:
        art = getattr(card, "art", None)
        if art is not None:
            art.set_size(size, size)
        child = card.get_child() if hasattr(card, "get_child") else None
        if child is not None:
            child.set_size_request(size, -1)
        if hasattr(card, "art_height"):
            card.art_height = size

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        Gtk.FlowBox.do_size_allocate(self, width, height, baseline)
        size = self.size_for(width)
        if size != self.card_size and not self._idle:
            self._idle = GLib.idle_add(self._apply, size)

    def _apply(self, size: int) -> bool:
        self._idle = 0
        size = self.size_for(self.get_width()) if self.get_width() > 0 else size
        if size == self.card_size:
            return GLib.SOURCE_REMOVE
        self.card_size = size
        child = self.get_first_child()
        while child is not None:
            card = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if card is not None:
                self._resize(card, size)
            child = child.get_next_sibling()
        return GLib.SOURCE_REMOVE

    def do_unrealize(self) -> None:
        if self._idle:
            GLib.source_remove(self._idle)
            self._idle = 0
        Gtk.FlowBox.do_unrealize(self)


class ContentStack(Gtk.Stack):
    """読み込み中 / 空・失敗 / 中身 を切り替える箱。

    `show_loading(text=None)` / `show_empty(icon, title, description=None)` /
    `show_content()`。中身は `content` (縦の箱) に積む。"""

    def __init__(self, loading_text: str | None = None):
        super().__init__()
        self.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.set_transition_duration(120)
        self.set_vhomogeneous(False)
        self.set_hhomogeneous(False)
        self.set_interpolate_size(False)
        self.loading = LoadingState(loading_text)
        self.loading.add_css_class("music-page-state")
        self.empty = EmptyState("music-note-symbolic", "")
        self.empty.add_css_class("music-page-state")
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_named(self.loading, "loading")
        self.add_named(self.empty, "empty")
        self.add_named(self.content, "content")

    def show_loading(self, text: str | None = None) -> None:
        if text is not None:
            self.loading.set_label(text)
        self.set_visible_child_name("loading")

    def show_empty(self, icon: str, title: str, description: str | None = None) -> None:
        self.empty.set_icon_name(icon)
        self.empty.set_title(title)
        self.empty.set_description(description)
        self.set_visible_child_name("empty")

    def show_content(self) -> None:
        self.set_visible_child_name("content")

    @property
    def state(self) -> str:
        return self.get_visible_child_name() or ""


# --------------------------------------------------------------------------
# ホーム


class HomePage(PageBase):
    """ホーム: 大見出し「ホーム」と 4 つの棚。

    1. おすすめ: 縦長のカード。最近再生した YouTube の曲から「<アーティスト> の
       ステーション」(start_station)、「再生中のリスト」(→ nowplaying)、「cliamp ラジオ」
       (radio の l:0 を読み込む)。
    2. 最近再生した項目 › (→ recent): 押すと履歴でリストを差し替えてその曲から再生。
    3. プレイリスト › (→ playlists): ローカルと各プロバイダーのプレイリスト。
    4. ラジオ局 › (→ radio): Radio Browser の日本の人気局。
    """

    PAGE_ID = "home"
    TITLE = "ホーム"
    MAX_STATIONS = 4
    MAX_RECENT = 20
    MAX_PLAYLISTS = 16
    MAX_RADIO = 20

    def __init__(self, ctx, **_params):
        super().__init__(ctx)
        self.add_large_title()

        self.picks = self.make_shelf("おすすめ")
        self.recent = self.make_shelf("最近再生した項目", weak_call(self._navigate, "recent"))
        self.playlists = self.make_shelf("プレイリスト", weak_call(self._navigate, "playlists"))
        self.stations = self.make_shelf("ラジオ局", weak_call(self._navigate, "radio"))
        for shelf in (self.picks, self.recent, self.playlists, self.stations):
            shelf.set_visible(False)
            self.body.append(shelf)

        self.state = ContentStack()
        self.state.set_visible(False)
        self.body.append(self.state)

        self._pick_keys: tuple = ()
        self._recent_keys: tuple = ()
        self._recent_tracks: list[Track] = []
        self._playlist_keys: tuple = ()
        self._station_keys: tuple = ()
        self._radio_stations: list[Track] = []
        self._has_radio_provider = False
        self._pending = 0
        self._serial = 0

        store = ctx.store
        self.watch(store, "history-changed", self._on_history)
        self.watch(store, "playlist-changed", self._on_playlist)
        self.watch(store, "track-changed", self._on_playlist)
        self.watch(store, "connection-changed", self._on_connection)
        if isinstance(ctx, GObject.Object):
            self.watch(ctx, "local-playlists-changed", self._on_local_playlists)

    # --- 読み込み ---------------------------------------------------------------

    def _unsupported(self) -> bool:
        """拡張の無い cliamp に繋がっている (ホームの棚はどれも使えない)。"""
        store = self.ctx.store
        return store.connected and store.api < 1

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        if self._unsupported():
            self._show_unsupported()
            return
        if store.connected and store.supports("history") and (force or not store.history):
            store.refresh_history()
        self._build_picks()
        self._build_recent()
        self._load_providers(force)
        self._load_radio(force)
        self._update_state()

    def on_resync(self) -> None:
        self._build_picks()
        self._build_recent()
        self._update_state()

    def _on_history(self, *_args) -> None:
        self._build_picks()
        self._build_recent()
        self._update_state()

    def _on_playlist(self, *_args) -> None:
        self._build_picks()

    def _on_connection(self, *_args) -> None:
        if self.ctx.store.connected:
            # 繋ぎ直した cliamp は別物かもしれない (拡張の有無・プロバイダー)。作り直す
            self._pick_keys = ()
            self._recent_keys = ()
            self._playlist_keys = ()
            self._has_radio_provider = False
            self.load(force=False)
        else:
            self._update_state()

    def _show_unsupported(self) -> None:
        """拡張なし: 棚を隠して説明だけを出す (履歴・プレイリスト・局の再生はどれも拡張が要る)。"""
        self._serial += 1
        self._pick_keys = self._recent_keys = self._playlist_keys = self._station_keys = ()
        self._has_radio_provider = False
        for shelf in (self.picks, self.recent, self.playlists, self.stations):
            shelf.remove_all()
            shelf.set_visible(False)
        self._update_state()

    def _on_local_playlists(self, *_args) -> None:
        self._load_providers(force=False)

    # --- おすすめ -----------------------------------------------------------------

    def _station_seeds(self) -> list[Track]:
        """最近再生した YouTube の曲から、アーティストごとに 1 曲ずつ。"""
        seeds: list[Track] = []
        artists: set[str] = set()
        for track in self.ctx.store.history:
            if not track.youtube_id:
                continue
            artist = (track.artist or track.display_title).casefold()
            if artist in artists:
                continue
            artists.add(artist)
            seeds.append(track)
            if len(seeds) >= self.MAX_STATIONS:
                break
        return seeds

    def _build_picks(self) -> None:
        if self._unsupported():
            return
        store = self.ctx.store
        items: list[tuple] = []
        playlist = store.playlist
        if playlist.tracks:
            current = store.current_track() or playlist.tracks[0]
            source = store.status.source if store.status.source else playlist.source
            name = source.name if source and source.name else "再生中のリスト"
            items.append(("nowplaying", current, "再生中のリスト", name))
        seeds = self._station_seeds()
        if seeds:
            first = seeds[0]
            items.insert(0, ("station", first, "ステーション", station_title(first)))
        if self._has_radio_provider:
            items.append(("radio", ("placeholder", "cliamp-radio", "station"), "ラジオ", "cliamp ラジオ"))
        for track in seeds[1:]:
            items.append(("station", track, "ステーション", station_title(track)))
        # 「再生中のリスト」の絵は曲が変わるたびに変わるので、並びの比較には含めず、
        # 並びが同じなら絵だけ差し替える (曲ごとにカードを作り直さない)
        keys = tuple((kind, None if kind == "nowplaying" else
                      (track_key(subject) if isinstance(subject, Track) else subject), over, title)
                     for kind, subject, over, title in items)
        if keys == self._pick_keys:
            for card in self.picks.items():
                if getattr(card, "pick_kind", "") != "nowplaying":
                    continue
                subject = next(item[1] for item in items if item[0] == "nowplaying")
                if track_key(subject) != track_key(card.pick_subject):
                    card.pick_subject = subject
                    card.art.set_subject(self.ctx.artwork, subject)
            return
        self._pick_keys = keys
        self.picks.remove_all()
        for kind, subject, overline, title in items:
            card = TallCard(self.ctx.artwork, subject, overline, title, weak_handler(self._on_pick),
                            kind="station" if kind == "radio" else "track")
            card.pick_kind = kind
            card.pick_subject = subject
            self.picks.append(card)
        self.picks.set_visible(bool(items))

    def _on_pick(self, card) -> None:
        kind = getattr(card, "pick_kind", "")
        if kind == "station":
            self.ctx.start_station(card.pick_subject)
        elif kind == "nowplaying":
            self.ctx.navigate("nowplaying", reveal=True)
        elif kind == "radio":
            self.ctx.load_provider("radio", "l:0", 0, "cliamp ラジオ")

    # --- 最近再生した項目 -----------------------------------------------------------

    def _build_recent(self) -> None:
        if self._unsupported():
            return
        tracks = unique_tracks(self.ctx.store.history, self.MAX_RECENT)
        keys = tuple(track_key(t) for t in tracks)
        if keys == self._recent_keys:
            return
        self._recent_keys = keys
        self._recent_tracks = tracks
        self.recent.remove_all()
        for i, track in enumerate(tracks):
            card = MediaCard(self.ctx.artwork, track, track.display_title, track.artist or track.album,
                             170, on_activate=weak_handler(self._on_recent))
            card.track_index = i
            self.recent.append(card)
        self.recent.set_visible(bool(tracks))

    def _on_recent(self, card) -> None:
        index = getattr(card, "track_index", 0)
        self.ctx.play_tracks(self._recent_tracks, index,
                             Source(provider="local", id=RECENTLY_PLAYED, name=RECENT_SOURCE_NAME))

    # --- プレイリスト -----------------------------------------------------------------

    def _load_providers(self, force: bool) -> None:
        store = self.ctx.store
        if not store.connected or not store.supports("providers"):
            return
        self._serial += 1
        serial = self._serial
        self._pending += 1

        def done(result) -> None:
            self._pending -= 1
            if serial != self._serial:
                return
            if isinstance(result, Response):
                self._update_state()
                return
            remember_provider_names(self.ctx, result)
            had_radio = self._has_radio_provider
            self._has_radio_provider = any(p.key == "radio" and p.playlists for p in result)
            if had_radio != self._has_radio_provider:
                self._build_picks()
            keys = ["local"] + [p.key for p in result
                                if p.playlists and p.key not in ("local", "radio") and not p.virtual]
            self._collect_playlists(serial, keys, force)
            self._update_state()

        self.ctx.catalog.providers(done, force=force)

    def _collect_playlists(self, serial: int, keys: list[str], force: bool) -> None:
        found: dict[str, list[PlaylistInfo]] = {}
        remaining = {"n": len(keys)}
        self._pending += 1

        def finish() -> None:
            self._pending -= 1
            if serial != self._serial:
                return
            infos: list[PlaylistInfo] = []
            for key in keys:
                infos.extend(found.get(key, []))
            self._build_playlists(infos[: self.MAX_PLAYLISTS])
            self._update_state()

        def one(key: str, result) -> None:
            if not isinstance(result, Response):
                found[key] = [info for info in result if not (key == "local" and info.id == RECENTLY_PLAYED)]
            remaining["n"] -= 1
            if remaining["n"] == 0:
                finish()

        for key in keys:
            self.ctx.catalog.playlists(key, lambda result, key=key: one(key, result), force=force)

    def _build_playlists(self, infos: list[PlaylistInfo]) -> None:
        if self._unsupported():
            return
        keys = tuple((i.provider, i.id, i.name, i.track_count) for i in infos)
        if keys == self._playlist_keys:
            return
        self._playlist_keys = keys
        self.playlists.remove_all()
        for info in infos:
            subtitle = (format_count(info.track_count) if info.provider == "local" and info.track_count
                        else provider_label(self.ctx, info.provider))
            card = MediaCard(self.ctx.artwork, ("placeholder", f"{info.provider}:{info.id}", "playlist"),
                             info.name, subtitle, 170, kind="playlist",
                             on_activate=weak_handler(self._on_playlist_card))
            card.playlist_info = info
            self.playlists.append(card)
            if info.provider == "local":
                self._fill_playlist_art(card, info)
        self.playlists.set_visible(bool(infos))

    def _fill_playlist_art(self, card: MediaCard, info: PlaylistInfo) -> None:
        card_ref = weakref.ref(card)

        def done(result) -> None:
            target = card_ref()
            if target is None or isinstance(result, Response):
                return
            set_playlist_art(self.ctx, target.art, result, f"{info.provider}:{info.id}", target.art_height)

        self.ctx.catalog.tracks(info.provider, info.id, done)

    def _on_playlist_card(self, card) -> None:
        info = getattr(card, "playlist_info", None)
        if info is not None:
            self.ctx.navigate("playlist", provider=info.provider, id=info.id, name=info.name)

    # --- ラジオ局 -------------------------------------------------------------------

    def _load_radio(self, force: bool) -> None:
        if self._radio_stations and not force:
            self._build_stations()
            return
        self._pending += 1

        def done(result) -> None:
            self._pending -= 1
            if isinstance(result, list):
                self._radio_stations = result[: self.MAX_RADIO]
                self._build_stations()
            self._update_state()

        self.ctx.radio.by_country("JP", done, limit=self.MAX_RADIO)

    def _build_stations(self) -> None:
        if self._unsupported():
            return
        keys = tuple(t.path for t in self._radio_stations)
        if keys == self._station_keys:
            return
        self._station_keys = keys
        self.stations.remove_all()
        for station in self._radio_stations:
            self.stations.append(StationTile(self.ctx.artwork, station, weak_handler(self._on_station), size=170))
        self.stations.set_visible(bool(self._radio_stations))

    def _on_station(self, tile) -> None:
        station = tile.track
        self.ctx.play_tracks([station], 0, Source(provider="radio-browser", id=station.path,
                                                  name=station.display_title))

    # --- 共通 ---------------------------------------------------------------------

    def _navigate(self, page_id: str) -> None:
        self.ctx.navigate(page_id)

    def _update_state(self) -> None:
        shelves = (self.picks, self.recent, self.playlists, self.stations)
        if any(shelf.get_visible() for shelf in shelves):
            self.state.set_visible(False)
            return
        self.state.set_visible(True)
        store = self.ctx.store
        if self._unsupported():
            self.state.show_empty("music-home-symbolic", UNSUPPORTED_TITLE,
                                  "再生の操作だけ使えます。おすすめ・最近再生した項目・プレイリスト・"
                                  "ラジオ局を使うには、GUI 用の拡張 (api 1) を当てた cliamp が必要です。")
        elif self._pending > 0:
            self.state.show_loading()
        elif not store.connected:
            self.state.show_empty("music-radio-symbolic", OFFLINE_TITLE, OFFLINE_TEXT)
        else:
            self.state.show_empty("music-home-symbolic", "まだ何もありません",
                                  "cliamp で曲を再生すると、ここにおすすめや最近の曲が並びます。")

    def refresh(self) -> None:
        self._pick_keys = ()
        self._radio_stations = []
        super().refresh()
