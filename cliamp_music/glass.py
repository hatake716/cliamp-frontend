"""面の濃さを Liquid Glass の濃さ (設定 > 外観 のスライダー) に合わせる。

macOS 27 の Liquid Glass のスライダー (透明 ↔ 色付き) は、macos-desktop 拡張の
GSettings org.gnome.shell.extensions.macos-desktop の glass-tint (0.0〜1.0) が
持つ。Shell のメニューや通知はその拡張が塗り直すが、自作の GTK アプリ
(ミュージック・Spotlight・AirDrop・このコンピュータについて) の面は style.css の
固定の濃さのままで、スライダーに従っていなかった (2026-10-04)。

ここは、アプリの style.css の上に 2 つ目の CSS の provider を 1 段上の優先度で
重ね、面の background-color だけを glass-tint から作り直す。値が変わるたびに
読み直すので、常駐しているミュージックも止めずに追従する。外観 (ライト /
ダーク) は style.css と同じく @media (prefers-color-scheme: dark) で書き、
provider を GtkSettings の gtk-interface-color-scheme に束ねる。

■ 濃さの式 (desktop/APPEARANCE.md 5 章)

  ライト  0.62 + 0.35t  Shell の浮いた面と同じ (lib/glass.js の glassPalette の
                        surface)。透明の端でも、本文 (黒 0.85) が黒い窓の上で
                        6.3:1、補助 (黒 0.50) は Shell と同じ約 3:1。
  ダーク  0.75 + 0.22t  Shell より下限が高い。白い窓の上では、0.62 の暗い地に
                        純白の字を置いても比が 4.5〜4.8 しか出ず、艶 (上辺の
                        白の重ね) と字の alpha を入れた本文は 3.3〜4.4 だった。
                        0.75 で本文が 4.5:1 を越え、下の窓の字の透け (地の明暗
                        の差) もライトの透明の端より弱くなる。色付きの端は
                        Shell と同じ 0.97。

style.css の固定の濃さは、スキーマが読めないとき (単体で動かしたときなど) の
見た目として残る。

■ スキーマ

拡張のスキーマはシステムの既定の場所に無い (拡張のパッケージの中にだけある。
gsettings でも見えない)。ラッパー (package.nix) が MACOS_DESKTOP_SCHEMA_DIR に
拡張の schemas を渡し、ここで Gio.SettingsSchemaSource.new_from_directory で
読む。XDG_DATA_DIRS や GSETTINGS_SCHEMA_DIR で渡すと、アプリから起動するほかの
アプリにも混ざるので使わない。

■ このアプリの面

ミュージックの窓には 3 種類の地がある (base.css / shell.css)。それぞれ地の色が
違うので、glass.install() を面のグループごとに呼ぶ。呼び出し側 (app.py) が
セレクタと地の色の組を渡す。

GTK の provider は画面が無くても作れるので、install() 以外はビルドの中で
試せる (tests/test_glass.py)。
"""

from __future__ import annotations

import math
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

SCHEMA = "org.gnome.shell.extensions.macos-desktop"
KEY = "glass-tint"
# スキーマの既定 (glass.js の DEFAULT_TINT)。値が壊れているときに使う。
DEFAULT_TINT = 0.6
# ラッパーが拡張の schemas のディレクトリを渡す環境変数。
SCHEMA_DIR_ENV = "MACOS_DESKTOP_SCHEMA_DIR"

# 外観ごとの (透明の端, 色付きの端までの幅)。上の「濃さの式」を参照。
ALPHA = {
    "light": (0.62, 0.35),
    "dark": (0.75, 0.22),
}


def alpha(tint: float, variant: str) -> float:
    """濃さ tint (0〜1) の、外観 variant の面の不透明度。

    範囲の外は端へ寄せ、数でないものは既定の濃さにする (glass.js の
    readTint と同じ)。glass.js と同じく小数 3 桁に丸める。
    """
    if not isinstance(tint, (int, float)) or not math.isfinite(tint):
        tint = DEFAULT_TINT
    t = min(1.0, max(0.0, float(tint)))
    clear, span = ALPHA[variant]
    return round(clear + span * t, 3)


def css(selectors: list[str], rgb: dict[str, tuple[int, int, int]], tint: float) -> str:
    """面の background-color だけを書いた CSS。

    rgb は外観ごとの地の色で、アプリの style.css の同じ面の色と同じにする
    (濃さだけを差し替える)。
    """

    def rule(variant: str, indent: str) -> str:
        red, green, blue = rgb[variant]
        value = f"rgba({red}, {green}, {blue}, {alpha(tint, variant):.3f})"
        head = f",\n{indent}".join(selectors)
        return f"{indent}{head} {{\n{indent}  background-color: {value};\n{indent}}}\n"

    return (
        "/* Liquid Glass の濃さ (glass.py が glass-tint から作る) */\n"
        + rule("light", "")
        + "\n@media (prefers-color-scheme: dark) {\n"
        + rule("dark", "  ")
        + "}\n"
    )


def find_settings(program: str) -> Gio.Settings | None:
    """glass-tint を読む Gio.Settings。見つからなければ None (固定の濃さのまま)。

    環境変数があるのに読めないときは stderr に 1 行出す。ラッパーのパスの
    書き損じやスキーマの改名を、黙って固定の濃さへ落ちて見逃さないため。
    環境変数そのものが無い (名前の書き損じを含む) ときは何も出さない。
    ラッパーが名前ごと渡していることは、パッケージのビルドが確かめる。
    """
    default = Gio.SettingsSchemaSource.get_default()
    source = default
    directory = os.environ.get(SCHEMA_DIR_ENV)
    reported = False
    if directory:
        try:
            source = Gio.SettingsSchemaSource.new_from_directory(directory, default, False)
        except GLib.Error as error:
            # 既定の場所にスキーマがあればそちらを使う。無ければ固定のまま
            # (下で 2 行目は出さない)。
            print(f"{program}: {SCHEMA_DIR_ENV} ({directory}) を読めない: {error.message}",
                  file=sys.stderr)
            reported = True
    schema = source.lookup(SCHEMA, True) if source is not None else None
    if schema is None or not schema.has_key(KEY):
        if directory and not reported:
            print(f"{program}: {SCHEMA} の {KEY} が見つからない。面の濃さは固定のまま",
                  file=sys.stderr)
        return None
    return Gio.Settings.new_full(schema, None, None)


class GlassStyle:
    """glass-tint に合わせて面の CSS を作り直す provider。

    画面が無くても動く (試験はここを使う)。settings を持ち続けるので、
    このオブジェクトを生かしておけば変更が届く。
    """

    def __init__(self, settings: Gio.Settings, selectors: list[str],
                 rgb: dict[str, tuple[int, int, int]]) -> None:
        self.settings = settings
        self.selectors = list(selectors)
        self.rgb = dict(rgb)
        self.provider = Gtk.CssProvider()
        # 読み直した回数 (試験用)。
        self.loads = 0
        self._css: str | None = None
        # changed:: は、つないだ後にそのキーを一度読んだときに届くと GLib の
        # 文書にある (gio/gsettings.c の g_settings_get_value の注)。つないで
        # から読む。
        settings.connect(f"changed::{KEY}", self._on_changed)
        self.reload()

    def _on_changed(self, _settings: Gio.Settings, _key: str) -> None:
        self.reload()

    def reload(self) -> None:
        """今の値で CSS を作り直す。前と同じ文字列なら読み直さない。

        読み直すと、表示している部品の外観がすべて計算し直される (行の多い
        窓で 1 回 10 ms ほど)。スライダーを動かすと値が細かく何度も書かれる
        ので、変わらないときは何もしない。
        """
        text = css(self.selectors, self.rgb, self.settings.get_double(KEY))
        if text == self._css:
            return
        self._css = text
        self.provider.load_from_string(text)
        self.loads += 1


def install(display, settings: Gio.Settings | None, selectors: list[str],
            rgb: dict[str, tuple[int, int, int]], priority: int) -> GlassStyle | None:
    """GlassStyle を作り、外観に束ねて display へ足す。settings が None なら何もしない。

    priority は style.css の provider より 1 段上にする。同じ優先度の
    provider は、詳細度に関係なく後から足したほうが勝つので、順番に頼らない。
    """
    if display is None or settings is None:
        return None
    glass = GlassStyle(settings, selectors, rgb)
    Gtk.Settings.get_for_display(display).bind_property(
        "gtk-interface-color-scheme",
        glass.provider,
        "prefers-color-scheme",
        GObject.BindingFlags.SYNC_CREATE,
    )
    Gtk.StyleContext.add_provider_for_display(display, glass.provider, priority)
    return glass
