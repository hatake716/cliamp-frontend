#!/usr/bin/env python3
"""デザインの部品の見本帳。部品を色々な状態で並べ、Xvfb の上で PNG に撮る。

使い方 (開発用の Xvfb の画面番号は他と重ならないものを選ぶ):

    nix develop path:. -c xvfb-run -n 93 -s "-screen 0 2400x2400x24" \\
        python3 scripts/gallery.py --out /tmp/gallery

- 親のプロセスが子 (--child) を dbus-run-session の下で起こし、子の標準エラーを
  見張る。「Theme parser error」「Gtk-CRITICAL」「Gtk-WARNING」「markup」
  「Traceback」(シグナルの中の Python の例外は落ちずに印字だけされる) などの
  行が 1 つでも出たら失敗 (終了コード 1) にする。
- CSS (style/base.css) は USER + 1 で読み、parsing-error が 1 つでも出たら失敗。
- 自作の記号アイコンがアイコンテーマから引けなければ失敗。
- アプリ本体 (MusicApp) には触れない。アプリ ID は別 (…Gallery)、NON_UNIQUE。
- 他の部品 (protocol / artwork) は読めれば本物を使い、無ければここの小さな
  代役で動かす。アートワークは通信せず、ここで描いた絵を少し遅らせて返す
  (読み込み中の代わりの絵 → 本物へ、の流れを通すため)。
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURE_PATTERNS = ("Theme parser error", "Gtk-CRITICAL", "Gtk-WARNING",
                    "Failed to set text", "from markup", "Traceback (most recent call last)",
                    "-CRITICAL **", "-WARNING **")
SHEETS = ("controls", "browse", "lists", "menu")


# --------------------------------------------------------------------------
# 親: 子を起こして標準エラーを見張る


def run_parent(args: argparse.Namespace) -> int:
    if not os.environ.get("DISPLAY"):
        print("gallery: DISPLAY がありません。xvfb-run -n <番号> … の下で動かしてください",
              file=sys.stderr)
        return 2
    env = dict(os.environ)
    env.update({
        "GDK_BACKEND": "x11",
        "GSK_RENDERER": "cairo",
        "GDK_DEBUG": "no-portals",
        "ADW_DISABLE_PORTAL": "1",
        "GTK_A11Y": "none",
        "NO_AT_BRIDGE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # 専用のバスで gvfsd が起きて、終わりぎわに雑音を出すのを避ける
        "GIO_USE_VFS": "local",
    })
    cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--out", args.out]
    for sheet in args.sheet or []:
        cmd += ["--sheet", sheet]
    if shutil.which("dbus-run-session"):
        cmd = ["dbus-run-session", "--"] + cmd
    try:
        proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr or ""
        sys.stderr.write(stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr)
        print(f"gallery: {args.timeout} 秒で終わりませんでした", file=sys.stderr)
        return 1
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    bad = [line for line in proc.stderr.splitlines()
           if any(pattern in line for pattern in FAILURE_PATTERNS)]
    if bad:
        print(f"gallery: 標準エラーに警告が {len(bad)} 行あります", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"gallery: 子が終了コード {proc.returncode} で終わりました", file=sys.stderr)
        return proc.returncode or 1
    print("gallery: ok")
    return 0


# --------------------------------------------------------------------------
# 子: 代役


def install_protocol_stub() -> None:
    """protocol.py が読めないときだけ、見本に要る分の代役を入れる。"""
    try:
        importlib.import_module("cliamp_music.protocol")
        return
    except ImportError:
        pass
    module = types.ModuleType("cliamp_music.protocol")

    @dataclasses.dataclass(frozen=True)
    class Track:
        path: str
        title: str = ""
        artist: str = ""
        album: str = ""
        genre: str = ""
        year: int = 0
        track_number: int = 0
        duration: int = 0
        stream: bool = False
        live: bool = False
        feed: bool = False
        unplayable: bool = False
        bookmark: bool = False
        meta: tuple = ()
        queued: int = 0
        played_at: str = ""

        @property
        def display_title(self):
            return self.title or self.path.rstrip("/").rsplit("/", 1)[-1]

        @property
        def subtitle(self):
            return " — ".join(p for p in (self.artist, self.album) if p)

        def meta_get(self, key, default=""):
            return dict(self.meta).get(key, default)

    def format_time(secs):
        if secs is None or secs < 0:
            return "--:--"
        total = int(secs)
        h, rest = divmod(total, 3600)
        m, s = divmod(rest, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def format_total(secs):
        if not secs or secs <= 0:
            return ""
        minutes = max(1, int(round(secs / 60)))
        h, m = divmod(minutes, 60)
        return f"{h} 時間 {m} 分" if h and m else (f"{h} 時間" if h else f"{m} 分")

    module.Track = Track
    module.format_time = format_time
    module.format_total = format_total
    module.relative_time = lambda iso, now=None: "3 時間前" if iso else ""
    module.track_key = lambda t: t if isinstance(t, str) else f"path:{t.path}"
    sys.modules["cliamp_music.protocol"] = module
    print("gallery: protocol.py が無いので代役を使います", file=sys.stderr)


def child_main(args: argparse.Namespace) -> int:
    install_protocol_stub()

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango
    import cairo

    from cliamp_music import widgets as W
    from cliamp_music.protocol import Track, track_key

    try:
        from cliamp_music.artwork import ArtworkLoader as RealLoader
    except ImportError:
        RealLoader = None

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = args.sheet or list(SHEETS)
    problems: list[str] = []

    # --- 絵の代役 ----------------------------------------------------------

    def texture_from_surface(surface) -> Gdk.Texture:
        surface.flush()
        width, height, stride = surface.get_width(), surface.get_height(), surface.get_stride()
        data = GLib.Bytes.new(bytes(surface.get_data()))
        # cairo の ARGB32 はリトルエンディアンで B, G, R, A (乗算済み) の並び
        return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, stride)

    def hex_rgb(spec: str) -> tuple[float, float, float]:
        spec = spec.lstrip("#")
        return tuple(int(spec[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def draw_cover(key: str, size: int) -> Gdk.Texture:
        """アルバムの絵らしい抽象画 (key ごとに色と形が決まる)。"""
        top, bottom = W.color_pair_for(key + "/cover")
        accent, _ = W.color_pair_for(key + "/accent")
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        grad = cairo.LinearGradient(0, 0, size, size)
        grad.add_color_stop_rgb(0, *hex_rgb(top))
        grad.add_color_stop_rgb(1, *hex_rgb(bottom))
        cr.set_source(grad)
        cr.paint()
        seed = sum(ord(c) for c in key)
        style = seed % 3
        if style == 0:  # 大きな円と光
            cr.arc(size * 0.62, size * 0.42, size * 0.30, 0, 2 * math.pi)
            cr.set_source_rgba(*hex_rgb(accent), 0.85)
            cr.fill()
            cr.arc(size * 0.30, size * 0.72, size * 0.16, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.22)
            cr.fill()
        elif style == 1:  # 斜めの帯
            for i in range(5):
                cr.save()
                cr.translate(size / 2, size / 2)
                cr.rotate(-0.6)
                cr.rectangle(-size, -size * 0.5 + i * size * 0.22, size * 2, size * 0.09)
                cr.restore()
                cr.set_source_rgba(1, 1, 1, 0.10 + 0.05 * i)
                cr.fill()
            cr.arc(size * 0.5, size * 0.5, size * 0.18, 0, 2 * math.pi)
            cr.set_source_rgba(*hex_rgb(accent), 0.9)
            cr.fill()
        else:  # 夕日と水平線
            cr.arc(size * 0.5, size * 0.62, size * 0.26, math.pi, 2 * math.pi)
            cr.set_source_rgba(1.0, 0.78, 0.35, 0.92)
            cr.fill()
            cr.rectangle(0, size * 0.62, size, size * 0.38)
            cr.set_source_rgba(0, 0, 0, 0.30)
            cr.fill()
            for i in range(4):
                cr.rectangle(size * 0.2, size * (0.68 + i * 0.07), size * 0.6, size * 0.012)
                cr.set_source_rgba(1, 1, 1, 0.25)
                cr.fill()
        return texture_from_surface(surface)

    def draw_favicon(key: str, size: int) -> Gdk.Texture:
        """小さなロゴ (白い地に色の丸と波)。"""
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        top, bottom = W.color_pair_for(key + "/logo")
        cr.arc(size / 2, size / 2, size * 0.36, 0, 2 * math.pi)
        cr.set_source_rgb(*hex_rgb(bottom))
        cr.fill()
        cr.set_line_width(size * 0.07)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(size * 0.25, size * 0.52)
        for i in range(1, 9):
            x = size * (0.25 + i * 0.0625)
            y = size * (0.52 + 0.10 * math.sin(i * 1.3))
            cr.line_to(x, y)
        cr.stroke()
        return texture_from_surface(surface)

    def draw_placeholder(key: str, size: int) -> Gdk.Texture:
        """artwork.py が無いときの代わりの絵 (灰色の地に音符の気配)。"""
        top, bottom = W.color_pair_for(key)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        grad = cairo.LinearGradient(0, 0, 0, size)
        grad.add_color_stop_rgb(0, *hex_rgb(top))
        grad.add_color_stop_rgb(1, *hex_rgb(bottom))
        cr.set_source(grad)
        cr.paint()
        cr.set_source_rgba(1, 1, 1, 0.55)
        cr.arc(size * 0.44, size * 0.62, size * 0.09, 0, 2 * math.pi)
        cr.fill()
        cr.rectangle(size * 0.51, size * 0.30, size * 0.035, size * 0.32)
        cr.fill()
        return texture_from_surface(surface)

    class Handle:
        def __init__(self):
            self.source = 0

        def cancel(self):
            if self.source:
                GLib.source_remove(self.source)
                self.source = 0

    class GalleryLoader:
        """ArtworkLoader の代役。通信はせず、描いた絵を少し遅らせて返す。"""

        def __init__(self):
            self.real = None
            if RealLoader is not None:
                try:
                    self.real = RealLoader(cache_dir=tempfile.mkdtemp(prefix="music-gallery-"))
                except Exception as error:  # 見本帳なので、本物が作れなくても代役で続ける
                    print(f"gallery: ArtworkLoader を作れません ({error})。代役の絵を使います",
                          file=sys.stderr)
            self.requests = 0

        def placeholder(self, key, size, kind="track"):
            if self.real is not None:
                return self.real.placeholder(key, size, kind)
            return draw_placeholder(key, size)

        def request(self, subject, size, callback):
            """本物と同じく、取れなければ placeholder(key, size, kind) を返す。
            鍵と kind も本物に合わせる (曲は track_key、ラジオ (live) は station)。"""
            self.requests += 1
            handle = Handle()
            if isinstance(subject, tuple):
                key, kind, source = str(subject[1]), (subject[2] if len(subject) > 2 else "track"), ""
            elif isinstance(subject, str):
                key, kind, source = subject, "track", subject
            else:
                key = track_key(subject)
                kind = "station" if subject.live else "track"
                source = subject.meta_get("art") or subject.path
            make = draw_favicon if source.startswith("fav:") else draw_cover
            if not source or "missing" in source:
                make = None

            def deliver():
                handle.source = 0
                if make is None:
                    callback(self.placeholder(key, size, kind))
                else:
                    callback(make(source, max(16, min(size, 600))))
                return GLib.SOURCE_REMOVE

            handle.source = GLib.timeout_add(40 + (hash(key) % 5) * 30, deliver)
            return handle

    loader = GalleryLoader()

    class GalleryContext:
        """AppContext の代役。track_menu だけを持つ。"""

        artwork = loader

        def track_menu(self, track, *, index=None, context=""):
            group = Gio.SimpleActionGroup()
            for name in ("play-next", "play-last", "add-to", "new-playlist", "station",
                         "copy-link", "open-browser", "remove"):
                action = Gio.SimpleAction.new(name, GLib.VariantType.new("s") if name == "add-to" else None)
                group.add_action(action)
            menu = Gio.Menu()
            first = Gio.Menu()
            first.append("次に再生", "track.play-next")
            first.append("最後に再生", "track.play-last")
            menu.append_section(None, first)
            add = Gio.Menu()
            for name in ("作業用", "ドライブ"):
                item = Gio.MenuItem.new(name, None)
                item.set_action_and_target_value("track.add-to", GLib.Variant.new_string(name))
                add.append_item(item)
            add.append("新規プレイリスト…", "track.new-playlist")
            second = Gio.Menu()
            second.append_submenu("プレイリストに追加", add)
            second.append("ステーションを作成", "track.station")
            menu.append_section(None, second)
            third = Gio.Menu()
            third.append("リンクをコピー", "track.copy-link")
            third.append("ブラウザで開く", "track.open-browser")
            menu.append_section(None, third)
            if context:
                last = Gio.Menu()
                last.append("リストから削除", "track.remove")
                menu.append_section(None, last)
            return menu, group

    ctx = GalleryContext()

    # --- 見本のデータ --------------------------------------------------------

    def yt(n: int, title: str, artist: str, album: str = "", duration: int = 200, **kw) -> Track:
        return Track(path=f"https://www.youtube.com/watch?v=demo{n:07d}", title=title,
                     artist=artist, album=album, duration=duration, stream=True, **kw)

    songs = [
        yt(1, "夜に駆ける", "YOASOBI", "THE BOOK", 261),
        yt(2, "Plastic Love", "竹内まりや", "VARIETY", 474, bookmark=True),
        yt(3, "真夜中のドア〜stay with me〜 (2021 Remaster) & <Special Edit>", "松原みき",
           "POCKET PARK", 311),
        yt(4, "Cruel World", "Holly Humberstone", "Cruel World", 207),
        yt(5, "とても長い曲名がここに入って行の幅を越えたときにきちんと省略されるかの確認用の題",
           "とても長いアーティスト名の例 feat. もう一人のアーティスト", "長いアルバム名", 3725),
        yt(6, "ライブ配信 24/7 lofi hip hop radio", "Lofi Girl", "", 0, live=True),
        yt(7, "再生できない曲", "Unknown", "", 180, unplayable=True),
        yt(8, "Ride on Time", "山下達郎", "RIDE ON TIME", 358),
    ]
    missing = Track(path="https://example.com/missing-art.mp3", title="絵の無い曲", artist="手元のファイル",
                    duration=245)
    stations = [
        Track(path="https://radio.example/jazz", title="Jazz Sakura", stream=True, live=True,
              meta=(("art", "fav:jazz"), ("radio.country", "日本"))),
        Track(path="https://radio.example/lofi", title="Lo-fi 作業用ラジオ", stream=True, live=True,
              meta=(("art", "fav:lofi"), ("radio.country", "Japan"))),
        Track(path="https://radio.example/nhk", title="とても長い局の名前がここに入って省略されるかの確認",
              stream=True, live=True, meta=(("radio.country", "日本"),)),
        Track(path="https://radio.example/fip", title="FIP", stream=True, live=True,
              meta=(("art", "fav:fip"), ("radio.country", "France"))),
        Track(path="https://radio.example/nts", title="NTS 1", stream=True, live=True,
              meta=(("radio.country", "United Kingdom"),)),
        Track(path="https://radio.example/kexp", title="KEXP 90.3", stream=True, live=True,
              meta=(("art", "fav:kexp"), ("radio.country", "United States"))),
        Track(path="https://radio.example/broken", title="favicon が壊れた局", stream=True, live=True,
              meta=(("art", "fav:missing-broken"), ("radio.country", "日本"))),
    ]
    categories = [
        ("J-POP", ("#ff5f6d", "#c2185b")), ("アニメ", ("#7f7fd5", "#5b3cc4")),
        ("シティポップ", ("#f7971e", "#e5484d")), ("ロック", ("#4b4b4b", "#1f1f1f")),
        ("ヒップホップ", ("#3a7bd5", "#1c3f94")), ("ジャズ", ("#c79081", "#6d3b2f")),
        ("クラシック", ("#b8a47e", "#6b5b3e")), ("エレクトロニック", ("#00c6ff", "#6a3de8")),
        ("Lo-fi", ("#8e9eab", "#4a5a6a")), ("作業用BGM", ("#56ab2f", "#2d6a1b")),
        ("K-POP", ("#ff6fd8", "#9b2fae")), ("90年代", ("#f8b500", "#c55a11")),
    ]

    # --- 部品を並べる --------------------------------------------------------

    def section(title: str, child: Gtk.Widget) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        caption = Gtk.Label(label=title, xalign=0)
        caption.add_css_class("music-overline")
        box.append(caption)
        box.append(child)
        return box

    def hbox(*children, spacing=12) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
        for child in children:
            box.append(child)
        return box

    def vbox(*children, spacing=24) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        for child in children:
            box.append(child)
        return box

    INSET = 40

    class Page(Gtk.Box):
        """ページの中身。棚は端まで広げ (inset で先頭を揃える)、それ以外は左右に余白。"""

        def __init__(self):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=28)
            self.set_margin_top(28)
            self.set_margin_bottom(36)

        def append(self, child):
            if not isinstance(child, W.Shelf):
                child.set_margin_start(INSET)
                child.set_margin_end(INSET)
            Gtk.Box.append(self, child)

    def page_box() -> Gtk.Box:
        return Page()

    icon_dir = ROOT / "cliamp_music" / "icons" / "hicolor" / "scalable" / "actions"
    icon_names = sorted(p.name[: -len(".svg")] for p in icon_dir.glob("music-*-symbolic.svg"))

    def sheet_controls() -> Gtk.Widget:
        page = page_box()
        page.append(W.PageTitle("ホーム"))

        headers = vbox(
            W.SectionHeader("最近再生した項目", on_more=lambda: None),
            W.SectionHeader("おすすめ"),
            spacing=12,
        )
        queue_header = W.SectionHeader("次に再生")
        clear = Gtk.Button(label="消去")
        clear.add_css_class("flat")
        clear.add_css_class("music-key-text")
        queue_header.add_end(clear)
        headers.append(queue_header)
        page.append(section("見出し (SectionHeader)", headers))

        detail = hbox(
            W.CircleButton("music-shuffle-symbolic", "シャッフル", accent=True),
            W.CapsuleButton("再生", "music-play-symbolic"),
            W.CircleButton("music-more-symbolic", "その他", accent=True),
            spacing=11,
        )
        toggles = hbox(spacing=8)
        for icon, on in (("music-shuffle-symbolic", True), ("music-repeat-one-symbolic", True),
                         ("music-repeat-symbolic", False), ("music-lyrics-symbolic", True),
                         ("music-queue-symbolic", False)):
            toggle = W.ToggleCircle(icon, "切り替え")
            toggle.set_active(on)
            toggles.append(toggle)
        transport = hbox(
            W.CircleButton("music-previous-symbolic", "前へ", 32, flat=True),
            W.CircleButton("music-pause-symbolic", "一時停止", 40, flat=True),
            W.CircleButton("music-next-symbolic", "次へ", 32, flat=True),
            spacing=2,
        )
        capsule = W.GlassCapsule()
        capsule.append(W.CircleButton("music-close-symbolic", "閉じる", 30))
        capsule.append(W.CircleButton("music-miniplayer-symbolic", "ミニプレーヤー", 30))
        disabled = W.CircleButton("music-plus-symbolic", "追加", accent=True)
        disabled.set_sensitive(False)
        buttons = hbox(detail, toggles, transport, capsule,
                       W.CircleButton("music-back-symbolic", "戻る", glass=True), disabled, spacing=28)
        page.append(section("ボタン (Circle / Capsule / ToggleCircle / GlassCapsule)", buttons))

        search = Gtk.SearchEntry()
        search.set_placeholder_text("YouTube を検索")
        search.set_size_request(380, -1)
        idle_search = Gtk.SearchEntry()
        idle_search.set_placeholder_text("局を検索")
        idle_search.set_size_request(220, -1)
        chips = hbox(*(W.Chip(text, lambda chip: None) for text in ("YOASOBI", "シティポップ", "lofi hip hop")),
                     spacing=8)
        filled = W.CapsuleButton("cliamp を起動", accent_text=False, filled=True)
        scope = Adw.ToggleGroup()
        for name, label in (("youtube", "YouTube"), ("spotify", "Spotify"), ("library", "ライブラリ")):
            scope.add(Adw.Toggle(name=name, label=label))
        scope.set_active_name("youtube")
        scope.set_valign(Gtk.Align.CENTER)
        page.append(section("入力欄・範囲の切り替え・チップ",
                            hbox(search, scope, chips, filled, spacing=20)))
        del idle_search
        page._focus_target = search  # 撮る前にフォーカスを当てて赤い輪を見せる

        # ヘッダーバーの罠 (pack_end の箱は縮まない) を FillWidth で避けられるか。幅 900 で長い題
        bar = Adw.HeaderBar()
        bar.set_show_start_title_buttons(False)
        bar.set_show_end_title_buttons(False)
        heading = vbox(spacing=0)
        heading.set_valign(Gtk.Align.CENTER)
        long_title = Gtk.Label(xalign=0)
        long_title.set_text("とても長いプレイリストの名前 & <記号> がヘッダーバーに入って右のカプセルの下へ潜り込まずに省略されるかの確認")
        long_title.set_ellipsize(Pango.EllipsizeMode.END)
        long_title.add_css_class("music-section-title")
        heading.append(long_title)
        bar.set_title_widget(W.FillWidth(heading))
        tools = W.GlassCapsule()
        tools.append(W.CircleButton("music-plus-symbolic", "追加", 30))
        tools.append(W.CircleButton("music-more-symbolic", "その他", 30))
        bar.pack_end(tools)
        bar.pack_start(W.CircleButton("music-back-symbolic", "戻る", glass=True))
        # FillWidth は自然幅を大きく言うので、幅は Clamp で 900 に抑える
        bar_box = Adw.Clamp(maximum_size=900, tightening_threshold=900)
        bar_box.set_halign(Gtk.Align.START)
        bar_box.set_child(bar)
        page.append(section("ヘッダーバーの題 (FillWidth、幅 900)", bar_box))

        grid = Gtk.FlowBox()
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_max_children_per_line(16)
        grid.set_min_children_per_line(16)
        grid.set_row_spacing(8)
        grid.set_column_spacing(8)
        for name in icon_names:
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = hbox(spacing=8)
            plain = Gtk.Image.new_from_icon_name(name)
            plain.set_pixel_size(16)
            red = Gtk.Image.new_from_icon_name(name)
            red.set_pixel_size(16)
            red.add_css_class("music-key-text")
            row.append(plain)
            row.append(red)
            row.set_halign(Gtk.Align.CENTER)
            cell.append(row)
            caption = Gtk.Label(label=name[len("music-"): -len("-symbolic")])
            caption.add_css_class("music-overline")
            cell.append(caption)
            cell.set_size_request(62, -1)
            grid.append(cell)
        page.append(section(f"記号アイコン ({len(icon_names)} 個、16px)", grid))

        indicators = hbox(spacing=18)
        for playing, size in ((True, 14), (False, 14), (True, 28), (False, 28)):
            ind = W.PlayingIndicator(size)
            ind.set_playing(playing)
            indicators.append(ind)
        page.append(section("再生中の印 (再生中 / 一時停止)", indicators))

        tall = W.Shelf("おすすめ", inset=INSET)
        for key, over, title in (("mix:yoasobi", "ステーション", "YOASOBI のステーション"),
                                 ("list:now", "再生中のリスト", "ドライブ用のシティポップ 2026 年版とても長い題"),
                                 ("radio:cliamp", "ラジオ", "cliamp ラジオ"),
                                 ("mix:tatsuro", "ステーション", "山下達郎 のステーション"),
                                 ("mix:lofi", "ステーション", "Lofi Girl のステーション")):
            tall.append(W.TallCard(loader, key, over, title, lambda card: None))
        page.append(tall)

        recent = W.Shelf("最近再生した項目", on_more=lambda: None, inset=INSET)
        for track in songs[:5] + [missing] + songs[5:]:
            recent.append(W.MediaCard(loader, track, track.display_title, track.artist,
                                      on_activate=lambda card: None))
        page.append(recent)
        page._hover_shelf = recent
        return page

    def sheet_browse() -> Gtk.Widget:
        page = page_box()
        page.append(W.PageTitle("検索"))
        page.append(W.SectionHeader("カテゴリーを探す"))
        grid = Gtk.FlowBox()
        grid.add_css_class("music-grid")
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_homogeneous(True)
        grid.set_min_children_per_line(5)
        grid.set_max_children_per_line(5)
        grid.set_row_spacing(18)
        grid.set_column_spacing(18)
        for title, colors in categories:
            grid.append(W.CategoryTile(title, colors, lambda tile: None))
        page.append(grid)

        radio = W.Shelf("ラジオ局", on_more=lambda: None, inset=INSET)
        for station in stations:
            radio.append(W.StationTile(loader, station, lambda tile: None))
        page.append(radio)

        states = hbox(spacing=24)
        for child in (
            W.EmptyState("music-search-symbolic", "結果がありません",
                         "「とても長い検索語 & <記号>」に一致する曲は見つかりませんでした。綴りを確かめるか、別の語で探してください。"),
            W.EmptyState("music-station-symbolic", "cliamp に接続できません",
                         "cliamp が動いていないか、ソケットが見つかりません。", "cliamp を起動", lambda: None),
            W.LoadingState("読み込み中…"),
        ):
            frame = Gtk.Box()
            frame.set_size_request(340, 290)
            frame.add_css_class("gallery-frame")
            child.set_hexpand(True)
            frame.append(child)
            states.append(frame)
        page.append(section("空・読み込み中 (EmptyState / LoadingState)", states))
        return page

    def sheet_lists() -> Gtk.Widget:
        outer = hbox(spacing=0)
        page = page_box()
        page.set_hexpand(True)
        outer.append(page)

        head = hbox(spacing=28)
        art = W.Artwork(250, 250, radius=10, shadow=True)
        art.set_subject(loader, songs[1])
        head.append(art)
        info = vbox(spacing=0)
        info.set_valign(Gtk.Align.END)
        title = Gtk.Label(xalign=0)
        title.set_text("ドライブ用のシティポップ & <夜の街> — とても長いプレイリストの名前")
        title.add_css_class("music-detail-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(1)
        title.set_hexpand(True)
        provider = Gtk.Label(label="YouTube", xalign=0)
        provider.add_css_class("music-detail-subtitle")
        total = sum(t.duration for t in songs)
        meta = Gtk.Label(label=f"{W.format_count(len(songs))} · {W.format_total_duration(total)}", xalign=0)
        meta.add_css_class("music-info")
        meta.set_margin_top(6)
        buttons = hbox(
            W.CircleButton("music-shuffle-symbolic", "シャッフル", accent=True),
            W.CapsuleButton("再生", "music-play-symbolic"),
            W.CircleButton("music-more-symbolic", "その他", accent=True),
            spacing=11,
        )
        buttons.set_margin_top(22)
        for child in (title, provider, meta, buttons):
            info.append(child)
        head.append(info)
        page.append(head)

        album = W.TrackList()
        for i, track in enumerate(songs):
            album.append(W.TrackRow(ctx, track, variant="album", index=i, menu_context="nowplaying",
                                    on_activate=lambda row: None))
        album.set_current_index(3, True)
        album.select_row(album.get_row_at_index(1))
        page.append(section("曲の行 (album、4 行目が再生中、2 行目を選択)", album))

        recent = W.TrackList()
        played = ("2026-09-26T05:10:00Z", "2026-09-25T20:00:00Z", "2026-09-19T12:00:00Z", "")
        for i, (track, when) in enumerate(zip([songs[0], songs[2], missing, songs[4]], played)):
            track = dataclasses.replace(track, played_at=when)
            recent.append(W.TrackRow(ctx, track, variant="list", index=i,
                                     extra_text=None if when else "たった今",
                                     on_activate=lambda row: None))
        recent.set_current_index(1, False)
        page.append(section("曲の行 (list、2 行目が再生中で一時停止)", recent))

        panel = vbox(spacing=10)
        panel.add_css_class("gallery-panel")
        panel.set_size_request(300, -1)
        panel.set_hexpand(False)  # 中の hexpand が上へ伝わって広がらないように
        toggles = hbox(spacing=10)
        for label, icon, on in (("シャッフル", "music-shuffle-symbolic", False),
                                ("リピート", "music-repeat-symbolic", True)):
            button = Gtk.ToggleButton()
            button.set_child(hbox(Gtk.Image.new_from_icon_name(icon), Gtk.Label(label=label), spacing=6))
            button.get_child().set_halign(Gtk.Align.CENTER)
            button.set_active(on)
            button.set_hexpand(True)
            button.add_css_class("gallery-queue-toggle")
            toggles.append(button)
        panel.append(toggles)
        history_header = W.SectionHeader("履歴")
        panel.append(history_header)
        history = W.TrackList()
        for i, track in enumerate(songs[6:8]):
            history.append(W.TrackRow(ctx, track, variant="queue", index=i))
        panel.append(history)
        next_header = W.SectionHeader("次に再生")
        clear = Gtk.Button(label="消去")
        clear.add_css_class("flat")
        clear.add_css_class("music-key-text")
        next_header.add_end(clear)
        panel.append(next_header)
        queue = W.TrackList()
        for i, track in enumerate([songs[0], songs[4], missing, songs[2]]):
            queue.append(W.TrackRow(ctx, track, variant="queue", index=i, menu_context="queue"))
        panel.append(queue)
        outer.append(panel)
        return outer

    def sheet_menu() -> Gtk.Widget:
        page = page_box()
        page.set_size_request(520, -1)
        rows = W.TrackList()
        for i, track in enumerate(songs[:3]):
            rows.append(W.TrackRow(ctx, track, variant="album", index=i, menu_context="nowplaying"))
        page.append(rows)
        page._menu_row = rows.get_row_at_index(1)
        return page

    builders = {"controls": sheet_controls, "browse": sheet_browse, "lists": sheet_lists,
                "menu": sheet_menu}

    # --- アプリ -------------------------------------------------------------

    app = Adw.Application(application_id="org.nixos.Music.Gallery",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    css_errors: list[str] = []
    state = {"queue": list(sheets), "code": 0}

    def load_css(display: Gdk.Display) -> None:
        provider = Gtk.CssProvider()

        def on_error(_provider, section, error):
            css_errors.append(f"{section.to_string()}: {error.message}")

        provider.connect("parsing-error", on_error)
        provider.load_from_path(str(ROOT / "cliamp_music" / "style" / "base.css"))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        # 見本帳だけの枠 (アプリの CSS ではない)
        extra = Gtk.CssProvider()
        extra.connect("parsing-error", on_error)
        extra.load_from_string(
            "window.music .gallery-frame { border: 1px dashed rgba(255,255,255,0.12); border-radius: 12px; }"
            "window.music .gallery-panel { background-color: var(--m-sidebar); padding: 18px 12px 24px 12px; }"
            "window.music button.gallery-queue-toggle { min-height: 34px; border-radius: 9999px; border: none;"
            " background-image: none; box-shadow: none; background-color: var(--m-fill); color: var(--m-label); }"
            "window.music button.gallery-queue-toggle:checked { background-color: var(--m-key); color: white; }"
        )
        Gtk.StyleContext.add_provider_for_display(display, extra, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)

    def capture(widget: Gtk.Widget, path: Path, clip: bool = True) -> None:
        paintable = Gtk.WidgetPaintable.new(widget)
        width, height = widget.get_width(), widget.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            problems.append(f"{path.name}: 描く物がありません")
            return
        renderer = widget.get_native().get_renderer()
        viewport = Graphene.Rect().init(0, 0, width, height) if clip else None
        texture = renderer.render_texture(node, viewport)
        texture.save_to_png(str(path))
        print(f"gallery: {path} ({texture.get_width()}x{texture.get_height()})")

    def next_sheet() -> bool:
        if not state["queue"]:
            if css_errors:
                for message in css_errors:
                    print(f"gallery: CSS の解析に失敗: {message}", file=sys.stderr)
                state["code"] = 1
            if problems:
                for message in problems:
                    print(f"gallery: {message}", file=sys.stderr)
                state["code"] = 1
            app.quit()
            return GLib.SOURCE_REMOVE
        name = state["queue"].pop(0)
        content = builders[name]()
        window = Adw.ApplicationWindow(application=app)
        window.add_css_class("music")
        window.set_title(f"見本帳 — {name}")
        # 実際のページと同じく縦のスクロールに入れる (FlowBox に高さ固定で幅を
        # 問い合わせると GTK が寸法の食い違いを警告するため。アプリでも同じ形になる)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_child(content)
        overlay = Adw.ToastOverlay()
        overlay.set_child(scroller)
        window.set_content(overlay)
        window.set_default_size(1280 if name != "menu" else 520, -1)
        window.present()
        if name == "browse":
            toast = Adw.Toast.new("プレイリスト「作業用」に追加しました")
            toast.set_button_label("取り消す")
            toast.set_timeout(0)
            overlay.add_toast(toast)

        def after_layout() -> bool:
            focus = getattr(content, "_focus_target", None)
            if focus is not None:
                focus.grab_focus()
            shelf = getattr(content, "_hover_shelf", None)
            if shelf is not None:
                adj = shelf._scroller.get_hadjustment()
                adj.set_value(min(120, adj.get_upper() - adj.get_page_size()))
                shelf._set_hover(True)
            menu_row = getattr(content, "_menu_row", None)
            if menu_row is not None:
                menu_row.more.popup()
            GLib.timeout_add(700, shoot)
            return GLib.SOURCE_REMOVE

        def shoot() -> bool:
            if name == "menu":
                capture(window, out_dir / "gallery-menu-rows.png")
                popover = content._menu_row.more.get_popover()
                if popover is None or not popover.get_mapped():
                    problems.append("メニューが開きませんでした")
                else:
                    capture(popover, out_dir / "gallery-menu.png", clip=False)
                    popover.popdown()
            else:
                capture(window, out_dir / f"gallery-{name}.png")
            window.destroy()
            GLib.timeout_add(100, next_sheet)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(500, after_layout)
        return GLib.SOURCE_REMOVE

    def on_startup(application) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        load_css(display)
        theme = Gtk.IconTheme.get_for_display(display)
        theme.add_search_path(str(ROOT / "cliamp_music" / "icons"))
        for name in icon_names + ["org.nixos.Music"]:
            if not theme.has_icon(name):
                problems.append(f"アイコン {name} が引けません")
        if len(icon_names) < 32:
            problems.append(f"記号アイコンが {len(icon_names)} 個しかありません")

    def on_activate(application) -> None:
        application.hold()
        GLib.idle_add(next_sheet)

    app.connect("startup", on_startup)
    app.connect("activate", on_activate)
    app.run([sys.argv[0]])
    print(f"gallery: 絵の依頼 {loader.requests} 件", file=sys.stderr if state["code"] else sys.stdout)
    return state["code"]


def main() -> int:
    parser = argparse.ArgumentParser(description="ミュージックの部品の見本帳を PNG に撮る")
    parser.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "cliamp-music-gallery"),
                        help="PNG を置くディレクトリ")
    parser.add_argument("--sheet", action="append", choices=SHEETS, help="撮る面 (複数可。省くと全部)")
    parser.add_argument("--timeout", type=int, default=120, help="子を待つ秒数")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child_main(args)
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
