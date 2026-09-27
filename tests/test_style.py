"""外観 (ライト / ダーク) の試験: style/*.css の変数と、それを外観に従わせる仕組み。

2026-09-27 に FORCE_DARK をやめ、CSS はライトを既定に、ダークを
@media (prefers-color-scheme: dark) に持つようにした (style/base.css の先頭)。ここで確かめること:

- 変数の 3 つの塊 (ライト・ダーク・外観によらず暗いフルスクリーンとミニプレーヤー) の形。
  ダークの 2 つが同じ中身か、ダークの変数にライトの値があるか、使う変数がどれも定義されているか。
- 規則に色を直に書いていないか (外観で変わらない所を除く)。暗色の値の書き残しを止める。
- 変数の値が、それを使う性質の値として GTK に読めるか (変数の中身は使われる時まで検査されず、
  誤りは黙ってその性質を無効にする)。
- app.load_css の CssProvider が GtkSettings の外観に従うか (束ねないとダークが決して当たらない)、
  フルスクリーンの中は外観によらず暗いか、絵のふちの線とイコライザの線が外観に従うか。

画面の要らない試験は常に動く。部品を置く試験は Xvfb などの X の画面 (:10 以上) があるときだけ。
"""

from __future__ import annotations

import gc
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from fake_cliamp import isolate_display, run_loop  # noqa: E402

isolate_display()

STYLE = ROOT / "cliamp_music" / "style"
CSS_FILES = ("base", "shell", "pages", "player")

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gtk

    HAVE_GI = True
except (ImportError, ValueError):  # pragma: no cover
    HAVE_GI = False

HAVE_DISPLAY = False
if HAVE_GI:
    HAVE_DISPLAY = bool(os.environ.get("DISPLAY")) and bool(Gtk.init_check())
    if HAVE_DISPLAY:
        Adw.init()


# --------------------------------------------------------------------------
# CSS を読む小道具 (GTK を使わない)

COLOR = re.compile(r"rgba?\([^)]*\)|#[0-9a-fA-F]{3,8}\b|\b(?:white|black)\b")
VAR = re.compile(r"var\((--[a-z0-9-]+)")
DARK_MEDIA = "@media (prefers-color-scheme: dark)"
FIXED_DARK_SELECTOR = "window.music .music-fullscreen, window.music.music-mini"

# 色を直に書いてよい所 (選択子の一部)。外観で変わらないもの
FIXED_SCOPES = (
    # 外観によらず暗いところ (ぼかした絵の上に白い文字と記号。Apple のフルスクリーンと同じ)。
    # .music-scrubber・.music-transport・.music-volume-output はフルスクリーンとミニプレーヤーでだけ
    # 使う部品 (fullscreen.py の Scrubber・TransportRow・VolumeControl)
    ".music-fullscreen", ".music-mini", "music-fs-", "music-mini-", ".music-scrubber", ".music-transport",
    ".music-volume-output",
    # 絵の上 (暗くする覆い、絵の上の白い文字、絵の影)
    "music-card-scrim", "music-tall-card", "music-category-label", "music-track-art-scrim", "music-bar-art",
    # 赤で塗った面の上の白い文字・記号 (メニューの項目は乗せると赤で塗る)
    ".filled", "modelbutton:hover", "modelbutton:selected", "music-panel-toggle:checked",
    "music-top-result-play",
    # つまみは Apple と同じくどちらの外観でも白い丸 (影と縁で地から浮かせる)
    "> slider",
)
# Music の赤 (塗り・赤いフォーカスの輪・赤い重ね)。白の上でも暗い地の上でも同じ
RED = re.compile(r"rgba\(250, (?:45, 72|88, 106), [0-9.]+\)|#fa2d48|#e0223b")


def css_rules(text: str) -> list[tuple[str | None, str, list[tuple[str, str]]]]:
    """(@media の条件か None, 選択子, [(性質, 値)]) を順に返す。@media は 1 段だけ (このアプリの CSS)。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules = []
    media: list[str] = []
    head = ""
    i = 0
    while i < len(text):
        c = text[i]
        if c == "{":
            selector = " ".join(head.split())
            head = ""
            if selector.startswith("@media"):
                media.append(selector)
                i += 1
                continue
            end = text.index("}", i)
            decls = []
            for part in text[i + 1:end].split(";"):
                if ":" in part:
                    prop, value = part.split(":", 1)
                    decls.append((prop.strip(), " ".join(value.split())))
            rules.append((media[-1] if media else None, selector, decls))
            i = end + 1
            continue
        if c == "}":
            media.pop()
            head = ""
        else:
            head += c
        i += 1
    return rules


def all_rules():
    """(ファイル名, @media, 選択子, 宣言) を 4 つの CSS から。"""
    for name in CSS_FILES:
        for media, selector, decls in css_rules((STYLE / f"{name}.css").read_text()):
            yield name, media, selector, decls


def palettes() -> dict[str, dict[str, str]]:
    """base.css の変数の塊: light (既定)、dark (@media の中)、fixed (外観によらず暗いところ)。"""
    found: dict[str, dict[str, str]] = {}
    for media, selector, decls in css_rules((STYLE / "base.css").read_text()):
        tokens = {prop: value for prop, value in decls if prop.startswith("--")}
        if not tokens:
            continue
        if media is None and selector == "window.music":
            key = "light"
        elif media == DARK_MEDIA and selector == "window.music":
            key = "dark"
        elif media is None and selector == FIXED_DARK_SELECTOR:
            key = "fixed"
        else:
            raise AssertionError(f"base.css に知らない変数の塊があります: {media} {selector}")
        if key in found:
            raise AssertionError(f"base.css に {key} の塊が 2 つあります")
        found[key] = tokens
    return found


def usages() -> dict[str, list[tuple[str, str]]]:
    """変数 → それを使う (性質, 値) の一覧。"""
    used: dict[str, list[tuple[str, str]]] = {}
    for _name, _media, _selector, decls in all_rules():
        for prop, value in decls:
            for token in VAR.findall(value):
                used.setdefault(token, []).append((prop, value))
    return used


def over_tokens() -> set[str]:
    """player.css が絵の上の部品のために置く --m-over-… (外観で変わらない)。"""
    names = set()
    for media, selector, decls in css_rules((STYLE / "player.css").read_text()):
        names.update(prop for prop, _value in decls if prop.startswith("--m-over-"))
    return names


# --------------------------------------------------------------------------
# 画面の要らないもの


class PaletteTest(unittest.TestCase):
    def setUp(self):
        self.p = palettes()

    def test_three_blocks(self):
        self.assertEqual(sorted(self.p), ["dark", "fixed", "light"])
        self.assertIn("--m-canvas", self.p["light"])

    def test_fixed_dark_is_the_same_as_dark(self):
        """フルスクリーンとミニプレーヤーの塊は @media のダークと同じ中身 (片方だけ直すと食い違う)。"""
        self.assertEqual(self.p["fixed"], self.p["dark"])

    def test_dark_tokens_have_light_values(self):
        """ダークで置き直す変数は、どれもライト (既定) にもある (無ければライトでダークの値が残る)。"""
        self.assertEqual(sorted(set(self.p["dark"]) - set(self.p["light"])), [])

    def test_scheme_tokens_differ(self):
        """ダークで置き直す変数は、ライトと値が違う (同じなら外観で変わらない所へ置く)。"""
        same = [t for t, v in self.p["dark"].items() if self.p["light"].get(t) == v]
        self.assertEqual(same, [])

    def test_every_color_token_has_a_dark_value(self):
        """外観で変わらない変数として置いてよいのは、Music の赤と libadwaita の強調色の地と文字だけ。"""
        fixed = sorted(set(self.p["light"]) - set(self.p["dark"]))
        self.assertEqual(fixed, ["--accent-bg-color", "--accent-fg-color", "--m-key", "--m-key-hover"])

    def test_used_tokens_are_defined(self):
        defined = set(self.p["light"]) | over_tokens()
        missing = sorted(t for t in usages() if t not in defined)
        self.assertEqual(missing, [])

    def test_defined_tokens_are_used(self):
        """置いたのに誰も読まない変数を残さない (libadwaita が読む --accent-… と、
        widgets.Artwork が color として読む --m-art-edge は CSS から読む)。"""
        used = set(usages())
        unused = sorted(t for t in self.p["light"] if t not in used and not t.startswith("--accent-"))
        self.assertEqual(unused, [])

    def test_no_fixed_colors_in_rules(self):
        """規則に色を直に書くのは外観で変わらない所だけ (FIXED_SCOPES と Music の赤)。"""
        bad = []
        for name, _media, selector, decls in all_rules():
            fixed_scope = all(any(part in s for part in FIXED_SCOPES) for s in selector.split(","))
            for prop, value in decls:
                if prop.startswith("--") or fixed_scope:
                    continue
                for match in COLOR.finditer(value):
                    if not RED.fullmatch(match.group(0)):
                        bad.append(f"{name}.css: {selector} {{ {prop}: {value} }}")
                        break
        self.assertEqual(bad, [])

    def test_check_catches_a_dark_literal(self):
        """上の判定が本当に働くか (暗色の値を直に書いた規則を拾えること)。"""
        rules = css_rules("window.music .x { background-color: rgba(255, 255, 255, 0.08); }")
        (_media, selector, decls), = rules
        self.assertFalse(any(part in selector for part in FIXED_SCOPES))
        self.assertIsNotNone(COLOR.search(decls[0][1]))
        self.assertIsNone(RED.fullmatch(COLOR.search(decls[0][1]).group(0)))


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class TokenValueTest(unittest.TestCase):
    """変数の値を、それを使う性質に入れて GTK に読ませる (外観ごとの値のすべて)。"""

    def parse(self, text: str) -> list[str]:
        errors = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda _p, section, error: errors.append(
            f"{section.to_string()}: {error.message}"))
        provider.load_from_string(text)
        return errors

    def test_values_parse_where_they_are_used(self):
        used = usages()
        blocks = palettes()
        problems = []
        for block, tokens in blocks.items():
            def lookup(match, tokens=tokens):
                name = match.group(1)
                return tokens.get(name) or blocks["light"].get(name) or "transparent"

            for token, value in tokens.items():
                places = used.get(token) or [("color", f"var({token})")]  # --accent-… は色
                for prop, template in places:
                    text = template.replace(f"var({token})", value)
                    # 同じ値に入っている別の変数 (例: 2 つの影の色) も同じ塊の値にする
                    text = re.sub(r"var\((--[a-z0-9-]+)\)", lookup, text)
                    errors = self.parse(f"window.probe {{ {prop}: {text}; }}")
                    if errors:
                        problems.append(f"{block} {token} in {prop}: {text} → {errors}")
        self.assertEqual(problems, [])

    def test_check_catches_a_bad_value(self):
        self.assertTrue(self.parse("window.probe { box-shadow: 0 1px rgbx(0, 0, 0, 0.1); }"))

    def test_css_parses_in_both_schemes(self):
        from cliamp_music.app import load_css

        for scheme in (Gtk.InterfaceColorScheme.LIGHT, Gtk.InterfaceColorScheme.DARK):
            with self.subTest(scheme=scheme):
                errors: list[str] = []
                loaded = load_css(errors, scheme=scheme)
                self.assertEqual([name for _p, name in loaded], list(CSS_FILES))
                self.assertEqual(errors, [])
                for provider, _name in loaded:
                    self.assertEqual(provider.props.prefers_color_scheme, scheme)

    def test_gtk_supports_scheme_media(self):
        from cliamp_music.app import color_scheme_problems

        self.assertEqual(color_scheme_problems(), [])

    def test_check_catches_old_gtk(self):
        """自己診断の判定が本当に働くか (性質の無い GTK を真似て失敗が拾えること)。"""
        from cliamp_music.app import color_scheme_problems

        class Old:
            class props:  # noqa: N801 (GObject の props の代役)
                pass

        self.assertEqual(len(color_scheme_problems(Old, Old)), 2)


# --------------------------------------------------------------------------
# 部品を置くもの


@unittest.skipUnless(HAVE_DISPLAY, "X の画面 (Xvfb の :10 以上の DISPLAY) がありません")
class SchemeSwitchTest(unittest.TestCase):
    """本物の app.load_css を画面に足し、Adw.StyleManager で外観を切り替える。"""

    LIGHT_SECONDARY = (0.0, 0.0, 0.0, 0.50)
    DARK_SECONDARY = (235 / 255, 235 / 255, 245 / 255, 0.60)

    def setUp(self):
        from cliamp_music.app import load_css

        self.display = Gdk.Display.get_default()
        self.manager = Adw.StyleManager.get_default()
        self.addCleanup(self.manager.set_color_scheme, Adw.ColorScheme.DEFAULT)
        errors: list[str] = []
        for provider, _name in load_css(errors, settings=Gtk.Settings.get_for_display(self.display)):
            Gtk.StyleContext.add_provider_for_display(self.display, provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
            self.addCleanup(Gtk.StyleContext.remove_provider_for_display, self.display, provider)
        self.assertEqual(errors, [])
        # 束ねは provider と settings が持つ。Python の側の参照を捨てても外れないこと
        gc.collect()

        self.window = Gtk.Window()
        self.window.add_css_class("music")
        self.addCleanup(self.window.destroy)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.label = Gtk.Label(label="副次")
        self.label.add_css_class("music-secondary")
        box.append(self.label)
        self.fullscreen = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.fullscreen.add_css_class("music-fullscreen")
        self.fs_label = Gtk.Label(label="副次 (フルスクリーン)")
        self.fs_label.add_css_class("music-secondary")
        self.fullscreen.append(self.fs_label)
        box.append(self.fullscreen)
        self.window.set_child(box)
        self.window.present()
        self.assertTrue(run_loop(lambda: self.label.get_mapped(), 5.0))

    def scheme(self, scheme) -> None:
        self.manager.set_color_scheme(scheme)
        dark = scheme == Adw.ColorScheme.FORCE_DARK
        self.assertTrue(run_loop(lambda: self.manager.get_dark() == dark, 3.0))
        run_loop(lambda: False, 0.1)

    def assertColor(self, color, expected, places=2):  # noqa: N802
        got = (color.red, color.green, color.blue, color.alpha)
        for g, e in zip(got, expected):
            self.assertAlmostEqual(g, e, places=places, msg=f"{got} != {expected}")

    def test_label_follows_the_scheme_and_fullscreen_stays_dark(self):
        self.scheme(Adw.ColorScheme.FORCE_LIGHT)
        self.assertColor(self.label.get_color(), self.LIGHT_SECONDARY)
        self.assertColor(self.fs_label.get_color(), self.DARK_SECONDARY)
        self.scheme(Adw.ColorScheme.FORCE_DARK)
        self.assertColor(self.label.get_color(), self.DARK_SECONDARY)
        self.assertColor(self.fs_label.get_color(), self.DARK_SECONDARY)
        # 戻しても (切り替えの度に読み直されている)
        self.scheme(Adw.ColorScheme.FORCE_LIGHT)
        self.assertColor(self.label.get_color(), self.LIGHT_SECONDARY)

    def test_window_ground(self):
        self.scheme(Adw.ColorScheme.FORCE_LIGHT)
        self.assertColor(self.window.get_color(), (0.0, 0.0, 0.0, 0.85))
        self.scheme(Adw.ColorScheme.FORCE_DARK)
        self.assertColor(self.window.get_color(), (1.0, 1.0, 1.0, 0.92))

    def test_artwork_outline_follows_the_css(self):
        from cliamp_music.widgets import Artwork

        art = Artwork(40)
        self.label.get_parent().append(art)
        fs_art = Artwork(40)
        self.fullscreen.append(fs_art)
        self.assertTrue(run_loop(lambda: art.get_mapped() and fs_art.get_mapped(), 3.0))
        self.scheme(Adw.ColorScheme.FORCE_LIGHT)
        self.assertColor(art.get_color(), (0.0, 0.0, 0.0, 0.08))
        self.assertColor(fs_art.get_color(), (1.0, 1.0, 1.0, 0.07))
        self.scheme(Adw.ColorScheme.FORCE_DARK)
        self.assertColor(art.get_color(), (1.0, 1.0, 1.0, 0.07))

    def test_equalizer_lines_follow_the_scheme(self):
        from cliamp_music import equalizer

        self.scheme(Adw.ColorScheme.FORCE_LIGHT)
        self.assertColor(equalizer.eq_color("track"), (0.0, 0.0, 0.0, 0.10))
        self.scheme(Adw.ColorScheme.FORCE_DARK)
        # ダークは暗色固定だったときの値のまま
        self.assertColor(equalizer.eq_color("track"), (1.0, 1.0, 1.0, 0.14))
        self.assertColor(equalizer.eq_color("label"), (235 / 255, 235 / 255, 245 / 255, 0.55))


if __name__ == "__main__":
    unittest.main()
