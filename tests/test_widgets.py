"""widgets.py (デザインの部品) の試験。

画面の要らない小道具は常に試す。部品を実際に置く試験は DISPLAY (Xvfb など) が
あるときだけ動かし、無ければ飛ばす。例:

    nix develop path:. -c xvfb-run -n 93 python3 -m unittest tests.test_widgets -v
"""

from __future__ import annotations

import functools
import os
import sys
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 部品を置く試験は Xvfb などの X の画面だけで動かす。GDK は WAYLAND_DISPLAY が
# 無くても wayland-0 へ繋ぎに行くので、放っておくと使っている机の上に窓が出る。
os.environ.setdefault("GDK_BACKEND", "x11")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    HAVE_GI = True
except (ImportError, ValueError):
    HAVE_GI = False

HAVE_DISPLAY = False
if HAVE_GI:
    HAVE_DISPLAY = bool(os.environ.get("DISPLAY")) and bool(Gtk.init_check())
    if HAVE_DISPLAY:
        Adw.init()
        # 試験の中で時間を待たないよう、送りのアニメーションを切る
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

if HAVE_GI:
    from cliamp_music import widgets as W
    from cliamp_music.protocol import Track

ICON_DIR = ROOT / "cliamp_music" / "icons" / "hicolor" / "scalable"
REQUIRED_ICONS = (
    "search", "home", "radio", "recent", "note", "note-list", "grid", "queue", "lyrics",
    "shuffle", "repeat", "repeat-one", "play", "pause", "next", "previous", "stop",
    "volume", "volume-mute", "output", "more", "star", "back", "forward", "chevron-right",
    "close", "miniplayer", "fullscreen", "plus", "equalizer", "station", "clear", "warning",
)
SVG = "{http://www.w3.org/2000/svg}"


def run_until(condition, timeout: float = 3.0) -> bool:
    """main loop を回しながら condition() が真になるのを待つ。"""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return condition()


class FakeHandle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoader:
    """ArtworkLoader の代役。依頼を覚え、callback は試験から呼ぶ。"""

    def __init__(self):
        self.requests = []   # (subject, size, callback, handle)
        self.placeholders = []

    def placeholder(self, key, size, kind="track"):
        self.placeholders.append((key, size, kind))
        return Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(b"\x10\x20\x30\xff"), 4)

    def request(self, subject, size, callback):
        handle = FakeHandle()
        self.requests.append((subject, size, callback, handle))
        return handle


def texture(color: bytes = b"\xff\x00\x00\xff") -> "Gdk.Texture":
    return Gdk.MemoryTexture.new(1, 1, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(color), 4)


# --------------------------------------------------------------------------
# 画面の要らないもの


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class FormatTest(unittest.TestCase):
    def test_count(self):
        self.assertEqual(W.format_count(12), "12 曲")
        self.assertEqual(W.format_count(0), "0 曲")

    def test_total_duration(self):
        self.assertEqual(W.format_total_duration(48 * 60), "48 分")
        self.assertEqual(W.format_total_duration(72 * 60), "1 時間 12 分")
        self.assertEqual(W.format_total_duration(3600), "1 時間")
        # 秒は出さず、0 秒より長ければ最低 1 分
        self.assertEqual(W.format_total_duration(20), "1 分")
        # 不明 (0) は空。情報の行に「0 分」と出さない
        self.assertEqual(W.format_total_duration(0), "")


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class ColorPairTest(unittest.TestCase):
    def test_stable_and_hex(self):
        """同じ名前は起動をまたいでも同じ色 (hash() でなく md5 で選ぶため)。"""
        a = W.color_pair_for("Jazz Sakura")
        self.assertEqual(a, W.color_pair_for("Jazz Sakura"))
        for color in a:
            self.assertRegex(color, r"^#[0-9a-f]{6}$")
        self.assertEqual(len({W.color_pair_for(f"局 {i}") for i in range(60)}) > 3, True)


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class InvokeTest(unittest.TestCase):
    """on_activate は引数なしでも、部品を 1 つ受け取っても書ける。"""

    def test_zero_and_one_argument(self):
        calls = []
        W.invoke(lambda: calls.append("zero"), "widget")
        W.invoke(lambda widget: calls.append(widget), "widget")
        W.invoke(functools.partial(lambda tag, widget: calls.append((tag, widget)), "p"), "widget")
        W.invoke(calls.append, "builtin")
        W.invoke(None, "ignored")
        self.assertEqual(calls, ["zero", "widget", ("p", "widget"), "builtin"])

    def test_bound_method(self):
        class Page:
            def __init__(self):
                self.got = None

            def on_card(self, card):
                self.got = card

        page = Page()
        W.invoke(page.on_card, "card")
        self.assertEqual(page.got, "card")


class IconFilesTest(unittest.TestCase):
    """記号アイコンは塗りだけで描く (GTK 4.22 は symbolic の線を塗りに変えるため)。"""

    def test_symbolic_icons(self):
        for name in REQUIRED_ICONS:
            path = ICON_DIR / "actions" / f"music-{name}-symbolic.svg"
            with self.subTest(icon=name):
                self.assertTrue(path.is_file(), path)
                root = ET.parse(path).getroot()
                self.assertEqual(root.get("viewBox"), "0 0 16 16")
                shapes = [el for el in root.iter() if el.tag != f"{SVG}svg"]
                self.assertTrue(shapes)
                for el in shapes:
                    self.assertEqual(el.tag, f"{SVG}path")
                    self.assertIsNone(el.get("stroke"))
                    self.assertNotEqual(el.get("fill"), "none")
                    self.assertTrue(el.get("d"))

    def test_app_icon(self):
        root = ET.parse(ICON_DIR / "apps" / "org.nixos.Music.svg").getroot()
        self.assertEqual(root.get("viewBox"), "0 0 128 128")
        self.assertEqual(root.find(f"{SVG}title").text, "ミュージック")
        self.assertTrue(root.find(f"{SVG}desc").text)


def _lines(layout) -> list[str]:
    data = layout.get_text().encode("utf-8")
    out = []
    for i in range(layout.get_line_count()):
        line = layout.get_line_readonly(i)
        out.append(data[line.start_index:line.start_index + line.length].decode("utf-8"))
    return out


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class PathWrapTest(unittest.TestCase):
    """ファイルのパスは "/" の直後でだけ折り返し、語の途中で "-" を足さない。"""

    PATHS = (
        "/tmp/nix-shell.pDVMAx/cm-shoot-5d8dkglc/cliamp.sock",
        "/home/user/.config/cliamp/cliamp.sock",
        "/run/user/1000/some-long-directory-name/cliamp.sock",
        "/tmp/曲の置き場/cliamp.sock",
    )

    def layout(self, path: str, width_px: int, attrs=None):
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo

        layout = Pango.Layout.new(PangoCairo.FontMap.get_default().create_context())
        layout.set_text(path, -1)
        layout.set_wrap(Pango.WrapMode.WORD)
        layout.set_width(width_px * Pango.SCALE)
        layout.set_attributes(W.path_wrap_attributes(path) if attrs is None else attrs)
        return layout

    def test_breaks_only_after_slashes(self):
        for path in self.PATHS:
            for width in (40, 90, 160, 260):
                with self.subTest(path=path, width=width):
                    lines = _lines(self.layout(path, width))
                    self.assertEqual("".join(lines), path)
                    for line in lines[:-1]:
                        self.assertTrue(line.endswith("/"), lines)

    def test_check_catches_mid_word_breaks(self):
        """この試験の判定が本当に働くか (属性なしなら "-" や語の途中で折れること)。"""
        from gi.repository import Pango

        lines = _lines(self.layout(self.PATHS[2], 90, attrs=Pango.AttrList()))
        self.assertFalse(all(line.endswith("/") for line in lines[:-1]), lines)

    def test_no_inserted_hyphens(self):
        from gi.repository import Pango

        attrs = W.path_wrap_attributes(self.PATHS[0])
        kinds = [a.klass.type for a in attrs.get_attributes()]
        self.assertIn(Pango.AttrType.INSERT_HYPHENS, kinds)
        hyphens = [a for a in attrs.get_attributes() if a.klass.type == Pango.AttrType.INSERT_HYPHENS]
        self.assertEqual(hyphens[0].as_int().value, 0)
        self.assertEqual(W.path_wrap_attributes("").get_attributes()[0].klass.type, Pango.AttrType.INSERT_HYPHENS)


# --------------------------------------------------------------------------
# 部品を置くもの


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class CssTest(unittest.TestCase):
    def load(self, provider_text: str | None = None) -> list[str]:
        errors = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda _p, section, error: errors.append(error.message))
        if provider_text is None:
            provider.load_from_path(str(ROOT / "cliamp_music" / "style" / "base.css"))
        else:
            provider.load_from_string(provider_text)
        return errors

    def test_base_css_parses_cleanly(self):
        self.assertEqual(self.load(), [])

    def test_check_catches_errors(self):
        """この試験の判定が本当に働くか (壊した CSS で失敗が拾えること)。"""
        self.assertTrue(self.load("window.music { colour: red; }"))

    def test_tokens_present(self):
        text = (ROOT / "cliamp_music" / "style" / "base.css").read_text()
        for token in ("--m-canvas", "--m-sidebar", "--m-key", "--m-key-text", "--m-label",
                      "--m-secondary", "--m-tertiary", "--m-fill", "--m-separator",
                      "--m-selected", "--m-hover", "--m-glass", "--accent-bg-color", "--accent-color"):
            self.assertIn(f"{token}:", text)


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.window = Gtk.Window()
        self.window.add_css_class("music")
        self.window.set_default_size(600, 400)

    def tearDown(self):
        self.window.destroy()
        run_until(lambda: False, 0.02)

    def show(self, child: Gtk.Widget) -> None:
        self.window.set_child(child)
        self.window.present()
        self.assertTrue(run_until(child.get_mapped), "表示されませんでした")


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class PathLabelTest(WindowCase):
    def test_narrow_label_wraps_at_slashes_and_keeps_the_whole_path(self):
        path = "/tmp/nix-shell.pDVMAx/cm-shoot-5d8dkglc/cliamp.sock"
        label = W.PathLabel(path)
        box = Gtk.Box()
        box.set_size_request(170, -1)
        label.set_hexpand(True)
        box.append(label)
        self.window.set_default_size(170, 200)
        self.show(box)
        self.assertTrue(run_until(lambda: label.get_layout().get_line_count() > 1))
        lines = _lines(label.get_layout())
        for line in lines[:-1]:
            self.assertTrue(line.endswith("/"), lines)
        self.assertEqual(label.get_text(), path)
        self.assertEqual(label.get_tooltip_text(), path)
        self.assertTrue(label.get_selectable())
        label.set_path("/run/user/1000/cliamp.sock")
        self.assertEqual(label.get_tooltip_text(), "/run/user/1000/cliamp.sock")


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class ArtworkTest(WindowCase):
    def test_placeholder_then_request_on_map(self):
        loader = FakeLoader()
        art = W.Artwork(40, radius=4)
        art.set_subject(loader, "https://example.com/a.jpg", kind="track")
        # 代わりの絵はすぐ、本物の依頼は表示されてから
        self.assertIsNotNone(art.texture)
        self.assertEqual(loader.placeholders[0][0], "https://example.com/a.jpg")
        self.assertEqual(loader.requests, [])
        self.show(art)
        self.assertTrue(run_until(lambda: loader.requests))
        subject, size, _callback, _handle = loader.requests[0]
        self.assertEqual(subject, "https://example.com/a.jpg")
        self.assertEqual(size, 40 * art.get_scale_factor())

    def test_stale_result_is_ignored(self):
        loader = FakeLoader()
        art = W.Artwork(40, 60)
        self.show(art)
        art.set_subject(loader, "first")
        art.set_subject(loader, "second")
        first, second = loader.requests
        self.assertTrue(first[3].cancelled)
        placeholder = art.texture
        first[2](texture())           # 取り消した依頼の結果は捨てる
        self.assertIs(art.texture, placeholder)
        fresh = texture(b"\x00\xff\x00\xff")
        second[2](fresh)
        self.assertIs(art.texture, fresh)
        # 縦長の枠では長い辺の画素数で頼む
        self.assertEqual(second[1], 60 * art.get_scale_factor())

    def test_long_list_requests_only_near_view(self):
        """スクロールの中では見える範囲 (前後 1 画面) の絵だけを頼み、送ると続きを頼む。"""
        loader = FakeLoader()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        arts = []
        for i in range(120):
            art = W.Artwork(40)
            art.set_subject(loader, f"item-{i}")
            arts.append(art)
            box.append(art)
        scroller = Gtk.ScrolledWindow()
        scroller.set_size_request(200, 200)
        scroller.set_child(box)
        self.show(scroller)
        self.assertTrue(run_until(lambda: len(loader.requests) > 0))
        run_until(lambda: False, 0.2)
        first = len(loader.requests)
        self.assertLess(first, 40)          # 1 画面 = 5 枚。前後 1 画面ぶんで 20 枚前後
        self.assertIn("item-0", [r[0] for r in loader.requests])
        adj = scroller.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        self.assertTrue(run_until(lambda: "item-119" in [r[0] for r in loader.requests]))
        self.assertLess(len(loader.requests), 120)

    def test_set_texture_cancels(self):
        loader = FakeLoader()
        art = W.Artwork(40)
        self.show(art)
        art.set_subject(loader, "x")
        tex = texture()
        art.set_texture(tex)
        self.assertTrue(loader.requests[0][3].cancelled)
        self.assertIs(art.texture, tex)


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class PlayingIndicatorTest(WindowCase):
    def test_ticks_only_while_playing_and_mapped(self):
        # この試験ではアニメーションを切っているので、一時的に入れる
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-enable-animations", True)
        try:
            ind = W.PlayingIndicator(14)
            ind.set_playing(True)
            self.assertEqual(ind._tick_id, 0)   # まだ表示されていない
            self.show(ind)
            self.assertNotEqual(ind._tick_id, 0)
            ind.set_playing(False)
            self.assertEqual(ind._tick_id, 0)
            ind.set_playing(True)
            self.window.set_child(None)
            self.assertEqual(ind._tick_id, 0)
        finally:
            settings.set_property("gtk-enable-animations", False)


class FakeContext:
    def __init__(self, loader=None):
        self.artwork = loader
        self.calls = []
        self.activated = []

    def track_menu(self, track, *, index=None, context=""):
        self.calls.append((track, index, context))
        group = Gio.SimpleActionGroup()
        action = Gio.SimpleAction.new("play-next", None)
        action.connect("activate", lambda *_: self.activated.append(track.title))
        group.add_action(action)
        menu = Gio.Menu()
        menu.append("次に再生", "track.play-next")
        return menu, group


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class TrackRowTest(WindowCase):
    def tracks(self):
        return [Track(path=f"/music/{i}.flac", title=f"曲 {i}", artist="A", duration=180 + i)
                for i in range(4)]

    def test_current_index(self):
        ctx = FakeContext()
        lst = W.TrackList()
        for i, track in enumerate(self.tracks()):
            lst.append(W.TrackRow(ctx, track, variant="album", index=i))
        self.show(lst)
        lst.set_current_index(2, True)
        flags = [row.has_css_class("current") for row in lst.rows()]
        self.assertEqual(flags, [False, False, True, False])
        self.assertIs(lst.current_row(), lst.rows()[2])
        lst.set_current_index(None, False)
        self.assertEqual([row.is_current for row in lst.rows()], [False] * 4)

    def test_activation_calls_on_activate(self):
        got = []
        lst = W.TrackList()
        row = W.TrackRow(FakeContext(), self.tracks()[1], variant="list", index=1,
                         on_activate=lambda r: got.append(r.index))
        lst.append(row)
        self.show(lst)
        lst.emit("row-activated", row)
        self.assertEqual(got, [1])

    def test_menu_is_built_on_demand(self):
        ctx = FakeContext()
        track = self.tracks()[0]
        row = W.TrackRow(ctx, track, variant="queue", index=5, menu_context="queue")
        lst = W.TrackList()
        lst.append(row)
        self.show(lst)
        self.assertEqual(ctx.calls, [])      # 行を作っただけではメニューを作らない
        row._create_popup(row.more)
        self.assertEqual(ctx.calls, [(track, 5, "queue")])
        self.assertIsNotNone(row.more.get_menu_model())
        self.assertTrue(row.activate_action("track.play-next", None))
        self.assertEqual(ctx.activated, [track.title])

    def test_context_menu_popup(self):
        ctx = FakeContext()
        row = W.TrackRow(ctx, self.tracks()[0], variant="album", index=0)
        lst = W.TrackList()
        lst.append(row)
        self.show(lst)
        self.assertTrue(row.popup_context_menu(10, 10))
        popover = row._context_popover
        self.assertTrue(run_until(popover.get_mapped))
        popover.popdown()
        self.assertTrue(run_until(lambda: popover.get_parent() is None))

    def test_texts(self):
        live = Track(path="https://radio.example/x", title="局", live=True, stream=True)
        row = W.TrackRow(FakeContext(), live, variant="album", index=0)
        self.assertEqual(row.duration_label.get_text(), "ライブ")
        broken = Track(path="/x.mp3", title="壊れた曲", unplayable=True)
        self.assertTrue(W.TrackRow(FakeContext(), broken, variant="album").has_css_class("unplayable"))
        played = Track(path="/y.mp3", title="履歴", played_at="2020-01-01T00:00:00Z")
        self.assertTrue(W.TrackRow(FakeContext(), played, variant="list").extra_label.get_text())
        self.assertEqual(W.TrackRow(FakeContext(), played, variant="list", extra_text="").extra_label.get_text(), "")
        # 記号入りの曲名もそのまま (マークアップにしない)
        odd = Track(path="/z.mp3", title="Rock & Roll <Live>", artist="*A*")
        self.assertEqual(W.TrackRow(FakeContext(), odd, variant="list").title_label.get_text(), "Rock & Roll <Live>")
        with self.assertRaises(ValueError):
            W.TrackRow(FakeContext(), odd, variant="grid")


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class ShelfTest(WindowCase):
    def test_page_scroll_snaps_to_cards(self):
        shelf = W.Shelf("棚", inset=20)
        for i in range(12):
            shelf.append(W.MediaCard(None, ("placeholder", f"k{i}"), f"カード {i}", "副題", size=120))
        self.show(shelf)
        adj = shelf._scroller.get_hadjustment()
        self.assertTrue(run_until(lambda: adj.get_upper() > adj.get_page_size() > 0))
        self.assertEqual(adj.get_value(), 0)
        shelf.scroll_page(1)
        value = adj.get_value()
        self.assertGreater(value, 0)
        starts = [round(x) for x, _w in shelf._child_spans()]
        self.assertIn(round(value), starts)   # カードの境目に揃う
        shelf.scroll_page(-1)
        self.assertEqual(adj.get_value(), 0)
        self.assertEqual(len(shelf.items()), 12)
        shelf.remove_all()
        self.assertEqual(shelf.items(), [])

    def test_pagers_follow_hover(self):
        shelf = W.Shelf("棚")
        for i in range(12):
            shelf.append(W.MediaCard(None, ("placeholder", f"k{i}"), f"カード {i}", "", size=120))
        self.show(shelf)
        adj = shelf._scroller.get_hadjustment()
        self.assertTrue(run_until(lambda: adj.get_upper() > adj.get_page_size() > 0))
        shelf._set_hover(True)
        self.assertFalse(shelf._prev.has_css_class("shown"))   # 先頭では「‹」を出さない
        self.assertTrue(shelf._next.has_css_class("shown"))
        shelf._set_hover(False)
        self.assertFalse(shelf._next.has_css_class("shown"))
        self.assertFalse(shelf._next.get_can_target())


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class SizingTest(WindowCase):
    def test_fill_width_reports_huge_natural(self):
        label = Gtk.Label(label="題")
        fill = W.FillWidth(label)
        minimum, natural, _b, _n = fill.measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertGreaterEqual(natural, W.FillWidth.NATURAL)
        self.assertLess(minimum, 100)
        self.assertIs(fill.child, label)

    def test_category_tile_keeps_16_9(self):
        tile = W.CategoryTile("J-POP", ("#ff5f6d", "#c2185b"))
        _min, natural, _b, _n = tile.surface.measure(Gtk.Orientation.VERTICAL, 320)
        self.assertEqual(natural, 180)

    def test_buttons(self):
        circle = W.CircleButton("music-shuffle-symbolic", "シャッフル", accent=True)
        self.assertEqual(circle.get_size_request(), (34, 34))
        self.assertTrue(circle.has_css_class("accent"))
        capsule = W.CapsuleButton("再生", "music-play-symbolic")
        capsule.set_label("停止")
        self.assertEqual(capsule.label.get_text(), "停止")
        toggle = W.ToggleCircle("music-repeat-symbolic", "リピート")
        toggle.set_icon_name("music-repeat-one-symbolic")
        self.assertEqual(toggle.image.get_icon_name(), "music-repeat-one-symbolic")
        clicked = []
        chip = W.Chip("YOASOBI", lambda c: clicked.append(c.text))
        chip.emit("clicked")
        self.assertEqual(clicked, ["YOASOBI"])
        pressed = []
        empty = W.EmptyState("music-search-symbolic", "結果がありません", None, "もう一度", lambda: pressed.append(1))
        empty.button.emit("clicked")
        self.assertEqual(pressed, [1])
        self.assertFalse(empty.description_label.get_visible())


@unittest.skipUnless(HAVE_DISPLAY, "画面 (DISPLAY) がありません")
class ReleaseTest(WindowCase):
    """外した部品が解放されるか (子やアニメーションに渡した閉包が部品を掴んでいないか)。

    PyGObject の GC は C 側に渡した閉包の中を辿れないので、部品 → 子 → 閉包 → 部品
    の輪があると、一覧を作り直すたびに行やカードが溜まっていく。"""

    def released(self, make) -> list[str]:
        import gc
        import weakref

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.show(box)
        widget, parts = make()
        box.append(widget)
        run_until(lambda: False, 0.2)
        refs = [(type(p).__name__, weakref.ref(p)) for p in parts]
        box.remove(widget)
        del widget, parts
        # 代役の読み手は依頼 (と絵の部品の callback) を覚えたままにするので捨てる
        # (本物の ArtworkLoader は届けた後に手放す)
        self.loader.requests.clear()
        run_until(lambda: False, 0.05)
        for _ in range(3):
            gc.collect()
        return [name for name, ref in refs if ref() is not None]

    def test_widgets_are_released(self):
        loader = self.loader = FakeLoader()
        track = Track(path="/music/a.flac", title="曲 & <A>", artist="A", duration=100)

        def rows():
            lst = W.TrackList()
            items = [W.TrackRow(FakeContext(loader), track, variant=v, index=i, on_activate=lambda r: None)
                     for i, v in enumerate(("album", "list", "queue"))]
            for row in items:
                lst.append(row)
            items[0].set_current(True, True)
            return lst, items

        def shelf():
            s = W.Shelf("棚", on_more=lambda: None)
            cards = [W.MediaCard(loader, track, "題", "副題"), W.TallCard(loader, track, "上", "題"),
                     W.StationTile(loader, track), W.CategoryTile("J-POP", ("#ff5f6d", "#c2185b"))]
            for card in cards:
                s.append(card)
            return s, [s, s.header] + cards

        def art():
            a = W.Artwork(40)
            a.set_subject(loader, track)
            return a, [a]

        def empty():
            e = W.EmptyState("music-note-symbolic", "題", "説明", "押す", lambda: None)
            return e, [e]

        for make in (rows, shelf, art, empty):
            with self.subTest(make.__name__):
                self.assertEqual(self.released(make), [])
        # 届いた絵で溶け込み (アニメーション) を動かした後も
        a = W.Artwork(40)
        a.set_subject(loader, track)
        self.show(a)
        run_until(lambda: loader.requests, 1.0)
        for request in list(loader.requests):
            request[2](texture())
        del request  # 絵の部品の callback を試験の中で掴んだままにしない
        loader.requests.clear()
        self.window.set_child(None)
        import gc
        import weakref

        ref = weakref.ref(a)
        del a
        for _ in range(3):
            gc.collect()
        self.assertIsNone(ref(), "溶け込みを動かした Artwork が解放されない")


if __name__ == "__main__":
    unittest.main()
