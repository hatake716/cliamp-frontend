#!/usr/bin/env python3
"""ミュージックの全画面を、本物のアプリを動かして PNG に撮る (端から端までの撮影ハーネス)。

使い方 (Xvfb の画面番号は他と重ならない私用のものを選ぶ。:10 以上でないと動かない):

    nix develop path:. -c xvfb-run -n 97 -s "-screen 0 1600x1100x24" \\
        python3 scripts/shoot.py [--out DIR] [--only NAME ...] [--keys] [--no-user-theme]

何をするか:
- 親: 一時ディレクトリに曲の絵を描く (色の違うグラデーションの正方形と、YouTube の
  hqdefault のような 480x360 の上下に黒帯の付いた 16:9 の絵) と、局の小さな favicon。
  子を dbus-run-session の下で、HOME・XDG_*_HOME を一時ディレクトリにして起こす。
  子の標準エラーを見張り、「Theme parser error」「Gtk-CRITICAL」「Gtk-WARNING」
  「Adwaita-WARNING」「Failed to set text … from markup」「Traceback」などが 1 行でも
  出れば失敗 (終了コード 1)。最後に主な画面を角丸 20px + 影で中立の壁紙に重ねた
  合成 (framed/*.png) を作る (Xvfb には合成器が無く、窓の角と影が出ないため)。
- 子: 偽の cliamp (tests/fake_cliamp.py) を別プロセスで --art-dir 付きで一時的な
  ソケットに起こし、本物のアプリ (cliamp_music.app.main --socket …) を
  CLIAMP_MUSIC_APP_ID=org.nixos.Music.Shoot、CLIAMP_MUSIC_NON_UNIQUE=1 で動かす。
  Radio Browser は手元の架空の局に差し替え、インターネットへの接続はすべて止める
  (試みたら失敗として記録する)。操作は公開の入口 (window.navigate、app のアクション、
  store の操作) だけを GLib のタイマーで順に呼ぶ。
- 本物の cliamp のソケット (~/.config/cliamp/cliamp.sock)、利用者の保存値・キャッシュには
  触れない。利用者の ~/.config/gtk-4.0/ の gtk.css・colors.css・settings.ini は読むだけで、
  一時的な XDG_CONFIG_HOME に写して使う (実際のデスクトップと同じ上書きの中で見るため。
  --no-user-theme で使わない)。
- --keys: xdotool があれば本物のキーを送ってショートカット (DESIGN.md §6) を確かめる。

撮る画面 (NAME): home, search, search-results, radio, recent, playlists, playlist,
nowplaying, lyrics, queue, fullscreen-lyrics, fullscreen-queue, mini-square, mini-compact,
equalizer, stress, playback-error, playback-error-fullscreen, playback-error-mini, narrow, collapsed,
collapsed-sidebar,
panel-over, disconnected, reconnected, legacy, legacy-playlists, legacy-search,
playlist-web-only, playlists-web-only, search-spotify-blocked (Spotify の接続が Web API だけの
cliamp。偽を --spotify-web-only で起こし直す)。
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_ID = "org.nixos.Music.Shoot"
DEFAULT_OUT = ROOT / "shots"
FAILURE_PATTERNS = (
    "Theme parser error", "Gtk-CRITICAL", "Gtk-WARNING", "Adwaita-WARNING", "Adwaita-CRITICAL",
    "Adw-WARNING", "Adw-CRITICAL", "GLib-CRITICAL", "GLib-GObject-CRITICAL", "GLib-GObject-WARNING",
    "Gdk-CRITICAL", "Gdk-WARNING", "Gsk-CRITICAL", "Gsk-WARNING", "Pango-CRITICAL", "Pango-WARNING",
    "-CRITICAL **", "-WARNING **", "Failed to set text", "from markup",
    "Traceback (most recent call last)", "DeprecationWarning", "RuntimeWarning",
    # アプリ自身の失敗の記録
    "を作れません", "操作に失敗", "CSS の誤り", "shoot: 失敗",
)
# 角丸の合成を作る画面 (窓全体を撮ったもの)
FRAMED = ("home", "search", "search-results", "radio", "recent", "playlists", "playlist",
          "nowplaying", "lyrics", "queue", "fullscreen-lyrics", "fullscreen-queue", "stress",
          "narrow", "collapsed-sidebar", "panel-over", "disconnected", "legacy", "mini-square", "mini-compact",
          "equalizer", "playback-error", "playback-error-fullscreen", "playback-error-mini",
          "playlist-web-only", "search-spotify-blocked")

LONG_TITLE = "夜明け前のプラットホームで君を待つ & <特別版> — とても長い日本語の曲名が再生バーに入りきらないとき"
LONG_ARTIST = "青い灯台 & <Friends> feat. 真夜中ポスト"
LONG_ALBUM = "港町ラジオ <Deluxe Edition> *2026* & ボーナストラック"
LONG_PLAYLIST = "夜更かし & <深夜> のためのとても長い名前のプレイリスト"


# --------------------------------------------------------------------------
# 絵


def _rgb(spec: str) -> tuple[float, float, float]:
    spec = spec.lstrip("#")
    return tuple(int(spec[i:i + 2], 16) / 255 for i in (0, 2, 4))


PALETTE = [("#f2536b", "#5e0f2c"), ("#f5874f", "#6e2408"), ("#e8b53a", "#5e3c04"),
           ("#4fc27a", "#0d4328"), ("#2fb5ac", "#093a3a"), ("#4a9ff0", "#0e2a66"),
           ("#7b7ce0", "#211a63"), ("#b067c9", "#3a1350"), ("#ea5f9d", "#561133"),
           ("#8193a6", "#232c38"), ("#d9c7a3", "#5c4a2c"), ("#3d3d45", "#0c0c10")]


def _cover(cr, size: float, n: int) -> None:
    """アルバムの絵らしい抽象画を (0, 0)-(size, size) に描く。n で色と形を変える。"""
    import cairo

    top, bottom = PALETTE[n % len(PALETTE)]
    accent = PALETTE[(n * 5 + 3) % len(PALETTE)][0]
    grad = cairo.LinearGradient(0, 0, size * 0.35, size)
    grad.add_color_stop_rgb(0, *_rgb(top))
    grad.add_color_stop_rgb(1, *_rgb(bottom))
    cr.set_source(grad)
    cr.rectangle(0, 0, size, size)
    cr.fill()
    style = n % 6
    if style == 0:  # 大きな円と小さな円
        cr.arc(size * 0.62, size * 0.40, size * 0.27, 0, 2 * math.pi)
        cr.set_source_rgba(*_rgb(accent), 0.92)
        cr.fill()
        cr.arc(size * 0.28, size * 0.72, size * 0.14, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.fill()
    elif style == 1:  # 斜めの帯
        for i in range(7):
            cr.save()
            cr.translate(size / 2, size / 2)
            cr.rotate(-0.6)
            cr.rectangle(-size, -size * 0.7 + i * size * 0.2, size * 2, size * 0.07)
            cr.restore()
            cr.set_source_rgba(1, 1, 1, 0.06 + 0.035 * i)
            cr.fill()
    elif style == 2:  # 夕日と水平線
        cr.arc(size * 0.5, size * 0.62, size * 0.24, math.pi, 2 * math.pi)
        cr.set_source_rgba(1.0, 0.80, 0.40, 0.95)
        cr.fill()
        cr.rectangle(0, size * 0.62, size, size * 0.38)
        cr.set_source_rgba(0, 0, 0, 0.30)
        cr.fill()
    elif style == 3:  # 同心の輪 (レコード盤)
        cr.arc(size * 0.5, size * 0.5, size * 0.36, 0, 2 * math.pi)
        cr.set_source_rgba(0.06, 0.06, 0.08, 0.92)
        cr.fill()
        cr.set_line_width(size * 0.006)
        for i in range(9):
            cr.arc(size * 0.5, size * 0.5, size * (0.14 + 0.024 * i), 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.10)
            cr.stroke()
        cr.arc(size * 0.5, size * 0.5, size * 0.11, 0, 2 * math.pi)
        cr.set_source_rgba(*_rgb(accent), 1.0)
        cr.fill()
        cr.arc(size * 0.5, size * 0.5, size * 0.014, 0, 2 * math.pi)
        cr.set_source_rgb(0.05, 0.05, 0.06)
        cr.fill()
    elif style == 4:  # 格子
        step = size / 6
        for i in range(6):
            for j in range(6):
                if (i + j + n) % 3 == 0:
                    cr.rectangle(i * step + step * 0.12, j * step + step * 0.12, step * 0.76, step * 0.76)
                    cr.set_source_rgba(1, 1, 1, 0.10 + 0.02 * ((i * j) % 5))
                    cr.fill()
    else:  # 波
        cr.set_line_width(size * 0.035)
        for i in range(5):
            y = size * (0.30 + 0.11 * i)
            cr.move_to(0, y)
            for k in range(1, 9):
                x = size * k / 8
                cr.line_to(x, y + math.sin(k * 1.3 + i) * size * 0.04)
            cr.set_source_rgba(*_rgb(accent), 0.35 + 0.1 * i)
            cr.stroke()
    # 文字の代わりの小さな札 (ジャケットらしさ)
    cr.rectangle(size * 0.08, size * 0.08, size * 0.26, size * 0.035)
    cr.set_source_rgba(1, 1, 1, 0.55)
    cr.fill()


def draw_art(art_dir: Path, favicon_dir: Path) -> None:
    """曲の絵 (偽の cliamp の --art-dir 用) と局の favicon を描く。

    3 枚に 1 枚は YouTube の hqdefault と同じ 480x360 で、中央の 16:9 に絵があり
    上下が真っ黒の帯 (アプリが帯を検出して 16:9 の中央を切り抜くかを見る)。"""
    import cairo

    art_dir.mkdir(parents=True, exist_ok=True)
    favicon_dir.mkdir(parents=True, exist_ok=True)
    for n in range(18):
        if n % 3 == 1:
            width, height = 480, 360
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
            cr = cairo.Context(surface)
            cr.set_source_rgb(0, 0, 0)
            cr.paint()
            band = 270
            top = (height - band) // 2
            cr.save()
            cr.rectangle(0, top, width, band)
            cr.clip()
            cr.translate(0, top - (width - band) / 2)
            _cover(cr, width, n)
            cr.restore()
        else:
            size = 600
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24, size, size)
            cr = cairo.Context(surface)
            _cover(cr, size, n)
        surface.write_to_png(str(art_dir / f"art-{n:02d}.png"))
    # favicon: 小さく、白い地や透明な地のものが多い (Radio Browser の実物に似せる)
    for n, (ground, mark) in enumerate((("#ffffff", "#e5483a"), (None, "#2f7df6"), ("#111111", "#f5c542"))):
        size = 48 if n != 1 else 64
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        if ground:
            cr.set_source_rgb(*_rgb(ground))
            cr.paint()
        cr.set_source_rgb(*_rgb(mark))
        cr.arc(size / 2, size / 2, size * 0.34, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(size * 0.06)
        for r in (0.10, 0.19):
            cr.arc(size / 2, size / 2, size * r, -2.4, -0.7)
            cr.stroke()
        surface.write_to_png(str(favicon_dir / f"favicon-{n}.png"))


# --------------------------------------------------------------------------
# 角丸の合成 (Xvfb には合成器が無いので、窓の角と影をここで付ける)


def _rounded_path(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def frame_png(source: Path, target: Path, radius: float = 20.0, margin: int = 64) -> None:
    """窓の PNG を角丸 radius で切り、柔らかい影を付けて中立の壁紙に重ねる。"""
    import cairo

    image = cairo.ImageSurface.create_from_png(str(source))
    # 撮った窓の外周 1px はテーマの縁の線なので落とす (代わりに下で細い線を引く)
    edge = 1
    w, h = image.get_width() - 2 * edge, image.get_height() - 2 * edge
    W, H = w + 2 * margin, h + 2 * margin
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(out)
    # 中立の壁紙 (灰みの青の縦のグラデーションと、左上からの弱い光)
    grad = cairo.LinearGradient(0, 0, 0, H)
    grad.add_color_stop_rgb(0, 0.36, 0.40, 0.47)
    grad.add_color_stop_rgb(1, 0.17, 0.19, 0.24)
    cr.set_source(grad)
    cr.paint()
    glow = cairo.RadialGradient(W * 0.2, H * 0.1, 0, W * 0.2, H * 0.1, max(W, H) * 0.8)
    glow.add_color_stop_rgba(0, 1, 1, 1, 0.10)
    glow.add_color_stop_rgba(1, 1, 1, 1, 0)
    cr.set_source(glow)
    cr.paint()
    # 影: 少しずつ広げた角丸を薄く重ねる (ぼかしの代わり)
    steps = 26
    for i in range(steps, 0, -1):
        spread = i * 1.2
        alpha = 0.022 * (1 - i / (steps + 1)) ** 1.4 * 2.2
        _rounded_path(cr, margin - spread, margin - spread + 10, w + 2 * spread, h + 2 * spread,
                      radius + spread)
        cr.set_source_rgba(0, 0, 0, alpha)
        cr.fill()
    # 窓の中身。撮った窓の端 (テーマの細い角丸と縁) は角丸の外に切り落とす
    cr.save()
    _rounded_path(cr, margin, margin, w, h, radius)
    cr.clip()
    # 撮影で透明な角が残るので、窓の地の色で先に埋める
    cr.set_source_rgb(28 / 255, 29 / 255, 33 / 255)
    cr.paint()
    cr.set_source_surface(image, margin - edge, margin - edge)
    cr.paint()
    cr.restore()
    # 窓の縁 (macOS の暗い窓の細い明るい線)
    _rounded_path(cr, margin + 0.5, margin + 0.5, w - 1, h - 1, radius - 0.5)
    cr.set_source_rgba(1, 1, 1, 0.14)
    cr.set_line_width(1)
    cr.stroke()
    out.write_to_png(str(target))


# --------------------------------------------------------------------------
# 親


def _private_display() -> bool:
    display = os.environ.get("DISPLAY", "")
    number = display[1:].split(".")[0] if display.startswith(":") else ""
    return number.isdigit() and int(number) >= 10


def _copy_user_theme(config: Path) -> None:
    source = Path.home() / ".config" / "gtk-4.0"
    target = config / "gtk-4.0"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("gtk.css", "colors.css", "settings.ini"):
        path = source / name
        if path.is_file():
            shutil.copyfile(path, target / name)


def run_parent(args: argparse.Namespace) -> int:
    if not _private_display():
        print("shoot: 私用の Xvfb (DISPLAY=:10 以上) の下で動かしてください (xvfb-run -n <番号> …)",
              file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    # ソケットのパスは 108 バイトまで。短い場所に作る
    work = Path(tempfile.mkdtemp(prefix="cm-shoot-"))
    if len(str(work / "cliamp.sock").encode()) > 90:
        shutil.rmtree(work, ignore_errors=True)
        work = Path(tempfile.mkdtemp(prefix="cm-shoot-", dir="/tmp"))
    code = 1
    try:
        for sub in ("home", "config", "state", "cache", "data", "run"):
            (work / sub).mkdir()
        os.chmod(work / "run", 0o700)
        draw_art(work / "art", work / "favicons")
        if not args.no_user_theme:
            _copy_user_theme(work / "config")
        env = dict(os.environ)
        for name in ("WAYLAND_DISPLAY", "CLIAMP_MUSIC_SOCKET", "DBUS_SESSION_BUS_ADDRESS"):
            env.pop(name, None)
        env.update({
            "HOME": str(work / "home"),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_STATE_HOME": str(work / "state"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_DATA_HOME": str(work / "data"),
            "XDG_RUNTIME_DIR": str(work / "run"),
            "GDK_BACKEND": "x11",
            "GSK_RENDERER": "cairo",
            "GDK_DEBUG": "no-portals",
            "GDK_DISABLE": "gl",
            "ADW_DISABLE_PORTAL": "1",
            "GTK_A11Y": "none",
            "NO_AT_BRIDGE": "1",
            "GIO_USE_VFS": "local",
            "PYTHONDONTWRITEBYTECODE": "1",
            # PyGObject 自身の読み込みで出る非推奨の警告だけは除く (アプリのものは見る)
            "PYTHONWARNINGS": "default,ignore:GLib.unix_signal_add_full is deprecated",
            "CLIAMP_MUSIC_APP_ID": APP_ID,
            "CLIAMP_MUSIC_NON_UNIQUE": "1",
            # 「cliamp を起動」で本物のサービスに触れない
            "CLIAMP_MUSIC_START_COMMAND": "false",
        })
        cmd = [sys.executable, str(Path(__file__).resolve()), "--child", "--out", str(out),
               "--work", str(work)]
        if args.keys:
            cmd.append("--keys")
        for only in args.only or []:
            cmd += ["--only", only]
        if shutil.which("dbus-run-session"):
            cmd = ["dbus-run-session", "--"] + cmd
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            sys.stdout.write(stdout or "")
            sys.stderr.write(stderr or "")
            print(f"shoot: 失敗: {args.timeout} 秒で終わりませんでした", file=sys.stderr)
            return 1
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        bad = [line for line in stderr.splitlines() if any(p in line for p in FAILURE_PATTERNS)]
        if bad:
            print(f"shoot: 標準エラーに警告・失敗が {len(bad)} 行あります", file=sys.stderr)
            for line in bad[:20]:
                print(f"  {line}", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            print(f"shoot: 子が終了コード {proc.returncode} で終わりました", file=sys.stderr)
            return proc.returncode or 1
        framed = out / "framed"
        framed.mkdir(exist_ok=True)
        for name in FRAMED:
            source = out / f"{name}.png"
            if source.exists():
                frame_png(source, framed / f"{name}.png")
                print(f"shoot: {framed / (name + '.png')}")
        code = 0
        print("shoot: ok")
        return code
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 子: 偽の cliamp


def _die_with_parent() -> None:
    """子プロセス (偽の cliamp) が、撮影の子が死んだら一緒に終わるように。"""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


class FakeProcess:
    """tests/fake_cliamp.py を別プロセスで動かす。止めて、別の設定で起こし直せる。"""

    def __init__(self, socket_path: str, art_dir: Path):
        self.socket_path = socket_path
        self.art_dir = art_dir
        self.proc: subprocess.Popen | None = None

    def start(self, *extra: str) -> None:
        self.stop()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        # 利用者の radios.toml の局とお気に入りもある cliamp として撮る (ラジオの棚が並ぶ)
        cmd = [sys.executable, str(ROOT / "tests" / "fake_cliamp.py"), "--socket", self.socket_path,
               "--art-dir", str(self.art_dir), "--radios-toml", *extra]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     preexec_fn=_die_with_parent)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(self.socket_path):
                return
            if self.proc.poll() is not None:
                raise RuntimeError(f"偽の cliamp が終了コード {self.proc.returncode} で終わりました")
            time.sleep(0.03)
        raise RuntimeError("偽の cliamp のソケットができません")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# --------------------------------------------------------------------------
# 子: ネットワークを止める・Radio Browser を差し替える


def install_network_guard(problems: list[str], art_dir: Path) -> None:
    """インターネットへの接続を止め、試みたら失敗として記録する (Unix ソケットは通す)。

    YouTube のサムネイル (i.ytimg.com の hqdefault) だけは、手元の黒帯付きの絵で
    答える (meta に絵の無い曲。拡張の無い cliamp の status の曲など)。sddefault は
    無いものとして 404 を返す (実際にも無い動画が多い)。"""
    import hashlib
    import io
    import re
    import socket
    import urllib.error

    from cliamp_music import artwork, radio

    thumbs = sorted(p for p in art_dir.glob("*.png") if int(p.stem.split("-")[-1]) % 3 == 1)
    youtube = re.compile(r"^https://i\.ytimg\.com/vi/([A-Za-z0-9_-]{11})/(hq|sd)default\.jpg$")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def refuse(what: str):
        problems.append(f"ネットワークに出ようとしました: {what}")
        return OSError(f"shoot: ネットワークは使えません ({what})")

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise refuse(str(address))
        return original_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse(str(address))
            return 111
        return original_connect_ex(self, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex

    def no_urlopen(request, timeout=None):
        url = getattr(request, "full_url", request)
        raise urllib.error.URLError(refuse(str(url)))

    def artwork_urlopen(request, timeout=None):
        url = str(getattr(request, "full_url", request))
        match = youtube.match(url)
        if match is None or not thumbs:
            return no_urlopen(request, timeout)
        if match.group(2) == "sd":
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        index = int(hashlib.sha1(match.group(1).encode()).hexdigest(), 16) % len(thumbs)
        return io.BytesIO(thumbs[index].read_bytes())

    artwork.urlopen = artwork_urlopen
    radio.urlopen = no_urlopen


def install_radio_fixtures(favicon_dir: Path) -> None:
    """Radio Browser の代わりに手元の架空の局を返す (応答は少し遅らせる)。"""
    from gi.repository import GLib

    from cliamp_music import radio
    from cliamp_music.protocol import Track

    favicons = sorted(favicon_dir.glob("*.png"))
    rows = [
        ("J-WAVE & <Tokyo> Hits", "Japan", "jpop,pop"),
        ("夜のジャズ FM", "Japan", "jazz"),
        ("Lo-fi Beats 24/7", "Japan", "lofi,chill"),
        ("NHK ラジオ第 1 (架空)", "Japan", "news,talk"),
        ("Anime <Songs> Channel", "Japan", "anime,jpop"),
        ("City Pop Radio", "Japan", "citypop,80s"),
        ("クラシック & 朗読の時間", "Japan", "classical"),
        ("港町コミュニティ FM", "Japan", "community"),
        ("Ambient Drift", "Germany", "ambient"),
        ("KEXP-like Indie", "United States", "indie,rock"),
        ("Radio Paradise (架空)", "United States", "eclectic"),
        ("BBC-ish World", "United Kingdom", "news"),
    ]

    def stations(offset: int, limit: int, country: str | None = None) -> list:
        out = []
        for i, (name, where, tags) in enumerate(rows):
            if country == "JP" and where != "Japan":
                continue
            meta = [("radio.country", where), ("radio.codec", "MP3" if i % 2 else "AAC"),
                    ("radio.bitrate", "128" if i % 3 else "320"), ("radio.tags", tags),
                    ("radio.countrycode", "JP" if where == "Japan" else "XX")]
            if (i + offset) % 4 != 3 and favicons:
                meta.append(("art", favicons[(i + offset) % len(favicons)].as_uri()))
            out.append(Track(path=f"https://radio.example.jp/{offset}-{i}.mp3", title=name,
                             stream=True, live=True, meta=tuple(meta)))
        return out[:limit]

    def later(callback, value) -> None:
        GLib.timeout_add(90, lambda: (callback(value), GLib.SOURCE_REMOVE)[1])

    radio.RadioBrowser.top = lambda self, callback, limit=40: later(callback, stations(1, limit))
    radio.RadioBrowser.by_country = (
        lambda self, code, callback, limit=40: later(callback, stations(0, limit, code.upper())))
    radio.RadioBrowser.search = (
        lambda self, query, callback, limit=60: later(callback, [
            t for t in stations(2, 60) if query.casefold() in t.title.casefold()][:limit]))


# --------------------------------------------------------------------------
# 子: 撮影の手順


class Until:
    """手順から yield すると、predicate() が真になるまで (最大 timeout 秒) 待つ。"""

    def __init__(self, predicate, timeout: float = 10.0, label: str = ""):
        self.predicate = predicate
        self.timeout = timeout
        self.label = label


def child_main(args: argparse.Namespace) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    from gi.repository import Gio, GLib, Graphene, Gtk

    from cliamp_music import app as app_module
    from cliamp_music.pages.home import WEB_ONLY_NOTE, provider_playback
    from cliamp_music.protocol import SPOTIFY_SEARCH_BLOCKED_TITLE, Track, is_youtube_bridge

    out = Path(args.out)
    work = Path(args.work)
    art_dir = work / "art"
    sock = str(work / "cliamp.sock")
    problems: list[str] = []
    install_network_guard(problems, art_dir)
    install_radio_fixtures(work / "favicons")
    fake = FakeProcess(sock, art_dir)
    fake.start()
    shots: list[str] = []
    result: dict = {}

    def app():
        return Gio.Application.get_default()

    def win():
        return app().window

    def ctx():
        return app().ctx

    def store():
        return app().ctx.store

    def fail(message: str) -> None:
        problems.append(message)
        print(f"shoot: 失敗: {message}", file=sys.stderr, flush=True)

    def check(condition: bool, message: str) -> None:
        if not condition:
            fail(message)

    def wanted(name: str) -> bool:
        return not args.only or name in args.only

    def capture(widget: Gtk.Widget, name: str) -> None:
        if not wanted(name):
            return
        width, height = widget.get_width(), widget.get_height()
        if width <= 0 or height <= 0:
            fail(f"{name}: 大きさが 0 です")
            return
        paintable = Gtk.WidgetPaintable.new(widget)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            fail(f"{name}: 描く物がありません")
            return
        renderer = widget.get_native().get_renderer()
        texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
        path = out / f"{name}.png"
        texture.save_to_png(str(path))
        shots.append(name)
        print(f"shoot: {path} ({texture.get_width()}x{texture.get_height()})", flush=True)

    def resize(width: int, height: int = 760) -> None:
        window = win()
        window.unmaximize()
        window.set_default_size(width, height)

    def page():
        return win().current_page()

    def page_id() -> str:
        current = page()
        return getattr(current, "page_id", "") if current is not None else ""

    def selected() -> str | None:
        return win().sidebar.selected_key

    def descendants(widget):
        child = widget.get_first_child() if widget is not None else None
        while child is not None:
            yield child
            yield from descendants(child)
            child = child.get_next_sibling()

    def texts(widget) -> str:
        return "\n".join(w.get_text() for w in descendants(widget)
                         if isinstance(w, Gtk.Label) and w.get_mapped())

    def library() -> list[Track]:
        return list(store().playlist.tracks)

    long_art = sorted(art_dir.glob("*.png"))

    def long_tracks() -> list[Track]:
        base = library()
        first = Track(path="https://www.youtube.com/watch?v=AbCdEfGhIj0", title=LONG_TITLE,
                      artist=LONG_ARTIST, album=LONG_ALBUM, duration=333, stream=True, bookmark=True,
                      meta=(("art", long_art[4].as_uri()),))
        second = Track(path="https://www.youtube.com/watch?v=ZyXwVuTsRq9",
                       title="<Untitled> & *demo* — 仮の題のまま公開された曲", artist="Kite & <Theory>",
                       album="B-Sides & <Rarities>", duration=133, stream=True,
                       meta=(("art", long_art[7].as_uri()),))
        return [first, second] + base[:6]

    # --- 場面 ------------------------------------------------------------------

    def scene_start():
        yield Until(lambda: app() is not None and win() is not None and win().get_mapped(), 15, "窓が出ない")
        yield Until(lambda: store().connected and bool(store().playlist.tracks) and bool(store().history),
                    15, "最初の接続")
        check(store().api == 1, f"api が 1 ではありません ({store().api})")
        check(abs(ctx().client._poll_interval - 0.4) < 1e-6,
              f"見えているのに状態を取る間隔が {ctx().client._poll_interval} 秒です")
        # ショートカットの表 (DESIGN.md §6)
        table = {
            "<Control>Right": "app.next", "<Control>Left": "app.previous",
            "<Shift><Control>Right": "app.seek-forward", "<Shift><Control>Left": "app.seek-backward",
            "<Control>Up": "app.volume-up", "<Control>Down": "app.volume-down",
            "<Control>period": "app.stop", "<Control>l": "app.reveal-current", "<Control>f": "app.search",
            "<Control><Alt>u": "app.show-queue", "<Shift><Control>l": "app.show-lyrics",
            "<Shift><Control>f": "app.fullscreen-player", "<Shift><Control>m": "app.miniplayer",
            "<Control><Alt>e": "app.equalizer", "<Control>r": "app.refresh", "<Control>0": "app.main-window",
            "<Control>w": "window.close", "<Control>q": "app.quit",
        }
        for accel, action in table.items():
            actions = app().get_actions_for_accel(accel)
            check(action in actions, f"ショートカット {accel} が {action} になっていません ({list(actions)})")

    def scene_home():
        window = win()
        window.navigate("home")
        yield Until(lambda: page_id() == "home", 5, "ホームが開かない")
        home = page()
        yield Until(lambda: all(s.get_visible() for s in (home.picks, home.recent, home.playlists, home.stations)),
                    10, "ホームの棚が揃わない")
        yield 1.6
        check(selected() == "home", f"サイドバーがホームを選んでいません ({selected()})")
        capture(window, "home")

    def scene_search():
        window = win()
        app().activate_action("search", None)
        yield Until(lambda: page_id() == "search", 5, "Ctrl+F で検索が開かない")
        yield 0.9
        focus = window.get_focus()
        check(isinstance(focus, (Gtk.Text, Gtk.Entry)), f"検索欄にフォーカスがありません ({type(focus).__name__})")
        check(selected() == "search", f"サイドバーが検索を選んでいません ({selected()})")
        # 最近の検索のチップを見せる
        ctx().state.add_recent_search("シティポップ")
        ctx().state.add_recent_search("夜 & <朝>")
        page().refresh()
        yield 0.6
        capture(window, "search")
        window.set_focus(None)

    def scene_search_results():
        window = win()
        search = page()
        search.set_query("夜のドライブ")
        yield Until(lambda: search.results.state == "content", 10, "検索の結果が出ない")
        yield 1.6
        capture(window, "search-results")

    def scene_radio():
        window = win()
        window.navigate("radio")
        yield Until(lambda: page_id() == "radio", 5, "ラジオが開かない")
        yield 1.8
        check(selected() == "radio", f"サイドバーがラジオを選んでいません ({selected()})")
        capture(window, "radio")

    def scene_recent():
        window = win()
        window.navigate("recent")
        yield Until(lambda: page_id() == "recent", 5, "最近再生した項目が開かない")
        yield 1.4
        check(selected() == "recent", f"サイドバーが最近再生した項目を選んでいません ({selected()})")
        capture(window, "recent")

    def scene_playlists():
        window = win()
        window.navigate("playlists")
        yield Until(lambda: page_id() == "playlists", 5, "すべてのプレイリストが開かない")
        yield 1.8
        check(selected() == "playlists", f"サイドバーがすべてのプレイリストを選んでいません ({selected()})")
        capture(window, "playlists")

    def scene_playlist():
        window = win()
        window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
        yield Until(lambda: page_id() == "playlist", 5, "プレイリストが開かない")
        yield 1.8
        detail = page()
        check(selected() == "playlist:local:ドライブ",
              f"サイドバーがプレイリストの行を選んでいません ({selected()})")
        back = getattr(detail, "back_button", None)
        check(back is not None and back.get_visible(), "プレイリストの詳細に戻るボタンがありません")
        capture(window, "playlist")
        # 戻る (ヘッダーの ‹ と同じ navigation.pop)
        detail.activate_action("navigation.pop", None)
        yield Until(lambda: page_id() == "playlists", 5, "戻るですべてのプレイリストに戻らない")
        yield 0.5
        check(selected() == "playlists", f"戻った後のサイドバーが合いません ({selected()})")

    def scene_nowplaying():
        window = win()
        # 少し後ろの曲にして、Ctrl+L で行までスクロールするのを見る
        store().play_index(9)
        yield Until(lambda: store().status.index == 9, 5, "9 曲目にならない")
        app().activate_action("reveal-current", None)
        yield Until(lambda: page_id() == "nowplaying", 5, "Ctrl+L で再生中のリストが開かない")
        yield 1.8
        check(selected() == "nowplaying", f"サイドバーが再生中のリストを選んでいません ({selected()})")
        capture(window, "nowplaying")

    def scene_lyrics():
        window = win()
        window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
        # 偽の cliamp の歌詞: ライブラリの添字 % 3 == 0 が同期した歌詞
        store().play_index(3)
        yield Until(lambda: store().status.index == 3, 5, "3 曲目にならない")
        store().seek_to(40.0)
        app().activate_action("show-lyrics", None)
        yield Until(lambda: window.panel == "lyrics", 3, "Shift+Ctrl+L で歌詞が開かない")
        bar = window.player_bar
        check(bar.lyrics_button.get_active() and not bar.queue_button.get_active(),
              "再生バーの歌詞のボタンがパネルと合いません")
        yield Until(lambda: window.lyrics_panel._lines and window.lyrics_panel._current >= 0, 6,
                    "同期した歌詞の今の行が出ない")
        yield 1.4
        capture(window, "lyrics")

    def scene_queue():
        window = win()
        # 待ち行列 (「次に再生」の節) に 2 曲。enqueue next は待ち行列を使わず続きの先頭に並べる
        store().queue_edit("add", index=7)
        store().queue_edit("add", index=10)
        yield Until(lambda: len(store().playlist.queue) >= 2, 5, "待ち行列に入らない")
        app().activate_action("show-queue", None)
        yield Until(lambda: window.panel == "queue", 3, "Ctrl+Alt+U で次に再生が開かない")
        bar = window.player_bar
        check(bar.queue_button.get_active() and not bar.lyrics_button.get_active(),
              "再生バーの次に再生のボタンがパネルと合いません")
        yield 1.4
        capture(window, "queue")
        # もう一度で閉じる
        app().activate_action("show-queue", None)
        yield 0.5
        check(window.panel == "" and not bar.queue_button.get_active(), "次に再生が 2 度目で閉じません")
        # 再生バーのボタンでも開け閉めできる
        bar.lyrics_button.set_active(True)
        yield 0.3
        check(window.panel == "lyrics", "再生バーの歌詞のボタンでパネルが開きません")
        bar.lyrics_button.set_active(False)
        yield 0.3
        check(window.panel == "", "再生バーの歌詞のボタンでパネルが閉じません")

    def scene_fullscreen():
        window = win()
        store().play_index(6)
        yield Until(lambda: store().status.index == 6, 5, "6 曲目にならない")
        store().seek_to(33.0)
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: window.fullscreen_shown, 3, "Shift+Ctrl+F でフルスクリーンにならない")
        player = window._fullscreen_player
        player.set_mode("lyrics")
        yield Until(lambda: player.lyrics.lyrics is not None and player.lyrics.current_index >= 0, 6,
                    "フルスクリーンの歌詞が出ない")
        yield 1.6
        capture(window, "fullscreen-lyrics")
        player.set_mode("queue")
        store().queue_edit("add", index=5)
        store().queue_edit("add", index=8)
        yield Until(lambda: len(store().playlist.queue) >= 2, 5, "待ち行列に入らない")
        yield 1.2
        capture(window, "fullscreen-queue")
        # 次に再生を作り直した後、外した行が落ちずに解放されるか (二重の後始末で abort した不具合)
        store().queue_edit("clear")
        yield Until(lambda: not store().playlist.queue, 5, "待ち行列が空にならない")
        yield 0.5
        import gc

        for _ in range(3):
            gc.collect()
        player.set_mode("lyrics")
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: not window.fullscreen_shown, 3, "Shift+Ctrl+F でフルスクリーンから戻らない")
        yield 0.4

    def scene_miniplayer():
        a = app()
        a.activate_action("miniplayer", None)
        yield Until(lambda: a.miniplayer is not None and a.miniplayer.get_mapped(), 5, "ミニプレーヤーが開かない")
        mini = a.miniplayer
        mini.set_mode("square")
        mini.set_hover(True, force=True)
        yield 1.4
        capture(mini, "mini-square")
        mini.set_mode("compact")
        yield Until(lambda: mini.get_content().get_width() == 400, 3, "ミニプレーヤーが横長にならない")
        yield 1.0
        capture(mini, "mini-compact")
        mini.set_mode("square")
        a.activate_action("miniplayer", None)
        yield 0.5
        check(not mini.get_visible(), "ミニプレーヤーが 2 度目で閉じません")
        check(a.window.get_visible(), "ミニプレーヤーを閉じた後にメインの窓が見えません")

    def scene_equalizer():
        a = app()
        a.activate_action("equalizer", None)
        yield Until(lambda: a.equalizer is not None and a.equalizer.get_mapped(), 5, "イコライザが開かない")
        eq = a.equalizer
        check(eq.get_transient_for() is a.window, "イコライザがメインの窓に従属していません")
        store().set_eq_preset("Rock")
        yield 1.2
        capture(eq, "equalizer")
        eq.close()
        yield 0.4

    def scene_stress():
        window = win()
        c = ctx()
        tracks = long_tracks()
        c.catalog.playlist_add(LONG_PLAYLIST, tracks[:4], lambda *_: c.refresh_local_playlists(force=True))
        c.play_tracks(tracks, 0, {"provider": "local", "id": LONG_PLAYLIST, "name": LONG_PLAYLIST})
        yield Until(lambda: store().status.track is not None and store().status.track.title == LONG_TITLE,
                    6, "長い曲名の曲にならない")
        window.navigate("nowplaying")
        window.show_panel("queue")
        yield Until(lambda: window.sidebar.find_row(f"playlist:local:{LONG_PLAYLIST}") is not None, 6,
                    "長い名前のプレイリストがサイドバーに出ない")
        yield 1.2
        toast = c.toast(f"「{LONG_TITLE}」を「{LONG_PLAYLIST}」に追加しました")
        check(toast is not None, "トーストが出ません")
        yield 0.9
        bar = window.player_bar
        check(bar.title_label.get_text() == LONG_TITLE, f"再生バーの曲名が違います ({bar.title_label.get_text()!r})")
        check(LONG_PLAYLIST in texts(page()), "再生中のリストの題に長い名前が出ません")
        capture(window, "stress")
        window.show_panel("")
        if toast is not None:
            toast.dismiss()  # 窓が前に出ていないと時間切れで消えないので、次の場面に残さない

    def scene_playback_error():
        """再生できなかった曲 (偽の cliamp は path に __fail__ を含む曲を年齢確認の誤りで止める)。"""
        from cliamp_music.protocol import describe_playback_error, playback_error_tooltip

        window = win()
        c = ctx()
        base = library()
        failing = Track(path="https://www.youtube.com/watch?v=__fail__AgeGate1", title="放課後サイダー (Live)",
                        artist="小春日和", album="四季録", duration=212, stream=True,
                        meta=(("art", long_art[2].as_uri()),))
        c.play_tracks([failing] + base[:5], 0, {"provider": "youtube", "id": "放課後", "name": "放課後"})
        window.navigate("nowplaying")
        yield Until(lambda: store().status.playback_problem is not None, 6, "再生できない曲の理由が出ない")
        bar = window.player_bar
        short, detail = store().status.playback_problem
        check(short == describe_playback_error(store().status.playback_error)[0], "短文が合いません")
        check(short.startswith("YouTube のサインインが必要"), f"年齢確認の誤りの短文が違います ({short!r})")
        yield Until(lambda: bar.subtitle_label.get_text() == short, 3, "再生バーの副題が理由になりません")
        check(bar.subtitle_label.has_css_class("problem") and bar.problem_icon.get_visible(),
              "再生バーの副題が警告の見た目になりません")
        check(bar.subtitle_row.get_tooltip_text() == playback_error_tooltip(short, detail),
              "再生バーの副題のツールチップが短文と全文ではありません")
        check(store().status.state == "stopped", f"再生できない曲で止まりません ({store().status.state})")
        yield 1.4
        capture(window, "playback-error")
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: window.fullscreen_shown, 3, "フルスクリーンにならない")
        player = window._fullscreen_player
        yield Until(lambda: player.subtitle_label.get_text() == short, 3, "フルスクリーンの副題が理由になりません")
        yield 1.2
        capture(window, "playback-error-fullscreen")
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: not window.fullscreen_shown, 3, "フルスクリーンから戻らない")
        a = app()
        a.activate_action("miniplayer", None)
        yield Until(lambda: a.miniplayer is not None and a.miniplayer.get_mapped(), 5, "ミニプレーヤーが開かない")
        mini = a.miniplayer
        mini.set_mode("square")
        mini.set_hover(True, force=True)
        yield Until(lambda: mini.square_subtitle.get_text() == short, 3, "ミニプレーヤーの副題が理由になりません")
        yield 1.2
        capture(mini, "playback-error-mini")
        a.activate_action("miniplayer", None)
        yield 0.5
        # 次の曲へ進めば消える
        store().next()
        yield Until(lambda: store().status.state == "playing" and store().status.playback_problem is None, 6,
                    "次の曲で理由が消えない")
        yield Until(lambda: not bar.subtitle_label.has_css_class("problem"), 3, "再生バーの警告が消えない")
        overlay = getattr(window, "_toasts", None)
        if overlay is not None and hasattr(overlay, "dismiss_all"):
            overlay.dismiss_all()  # 窓が前に出ていないと時間切れで消えないので、次の場面に残さない
        yield 0.4

    def scene_narrow():
        window = win()
        window.navigate("home")
        store().play_index(2)
        resize(780)
        yield Until(lambda: window.get_width() <= 790, 3, "幅 780 にならない")
        yield 1.4
        check(not window.split.get_collapsed(), "幅 780 でサイドバーが畳まれました (760 以下で畳むはず)")
        capture(window, "narrow")
        resize(760)
        yield Until(lambda: window.split.get_collapsed(), 3, "幅 760 でサイドバーが畳まれない")
        yield 1.0
        check(window.sidebar_collapsed, "sidebar-collapsed が真になりません")
        capture(window, "collapsed")
        window.show_sidebar()
        yield 0.9
        capture(window, "collapsed-sidebar")
        # 畳んだサイドバーから選ぶと閉じる
        window.sidebar.select_key("radio")
        window._on_sidebar_select("radio", {})
        yield 0.6
        check(page_id() == "radio" and not window.split.get_show_sidebar(),
              "畳んだサイドバーで選んでもページが変わらないか、サイドバーが閉じません")
        # 右パネルを内容の上に重ねる幅 (1080sp 以下): 再生バーはパネルの左に縮めて押せるまま
        resize(1000)
        yield Until(lambda: not window.split.get_collapsed() and window.panel_split.get_collapsed(), 3,
                    "幅 1000 で右パネルが重ねにならない")
        window.show_panel("queue")
        yield 1.2
        bar = window.player_bar
        ok, bounds = bar.compute_bounds(window)
        ok2, panel = window.panel_stack.compute_bounds(window)
        check(ok and ok2 and bounds.get_x() + bounds.get_width() <= panel.get_x() + 1,
              "重ねた右パネルの下に再生バーが潜っています")
        capture(window, "panel-over")
        window.show_panel("")
        yield 0.4
        resize(1180)
        yield Until(lambda: not window.split.get_collapsed(), 3, "幅を戻してもサイドバーが戻らない")
        window.navigate("home")
        yield 0.8

    def scene_disconnected():
        window = win()
        fake.stop()
        yield Until(lambda: not store().connected, 8, "偽の cliamp を止めても接続のまま")
        yield Until(lambda: window.offline.get_visible(), 3, "未接続の表示が出ない")
        yield 1.0
        check(not app().lookup_action("next").get_enabled(), "未接続なのに「次へ」が使えます")
        capture(window, "disconnected")
        # 起こし直すと戻る
        fake.start()
        yield Until(lambda: store().connected, 15, "偽の cliamp を起こし直しても繋がらない")
        yield Until(lambda: not window.offline.get_visible(), 3, "繋ぎ直しても未接続の表示が消えない")
        yield Until(lambda: bool(store().playlist.tracks), 5, "繋ぎ直した後にリストが戻らない")
        yield 1.4
        check(app().lookup_action("next").get_enabled(), "繋ぎ直しても「次へ」が使えません")
        capture(window, "reconnected")

    def scene_legacy():
        window = win()
        fake.start("--legacy")
        yield Until(lambda: store().connected and store().api == 0, 15, "拡張の無い cliamp に繋がらない")
        yield Until(lambda: window.banner.get_revealed(), 3, "拡張なしの帯が出ない")
        window.navigate("home")
        yield 1.6
        capture(window, "legacy")
        window.navigate("playlists")
        yield 1.2
        body = texts(page())
        check("拡張 IPC" in body, "拡張なしのプレイリストのページに説明がありません")
        capture(window, "legacy-playlists")
        window.navigate("search")
        yield 0.6
        search = page()
        search.set_query("テスト")
        yield 1.2
        body = texts(page())
        check("拡張 IPC" in body, "拡張なしの検索に説明がありません")
        capture(window, "legacy-search")
        window.set_focus(None)
        # 基本操作 (Space の代わりのアクション) は使える
        check(app().lookup_action("play-pause").get_enabled(), "拡張なしで再生/一時停止が使えません")
        check(not store().playlist.tracks and not store().history,
              "拡張なしに繋ぎ直しても前の cliamp のリストや履歴が残っています")
        # 拡張のある cliamp に戻すと、帯が消えて各ページが元に戻る
        fake.start()
        yield Until(lambda: store().connected and store().api == 1, 15, "拡張のある cliamp に戻らない")
        yield Until(lambda: not window.banner.get_revealed(), 3, "拡張のある cliamp に戻っても帯が消えない")
        yield Until(lambda: search.results.state == "content" and search.scopes.get_visible(), 10,
                    "拡張のある cliamp に戻っても検索が使えるようにならない")
        window.navigate("home")
        yield Until(lambda: page_id() == "home" and page().recent.get_visible() and page().stations.get_visible(),
                    10, "拡張のある cliamp に戻ってもホームの棚が戻らない")
        yield Until(lambda: bool(store().playlist.tracks), 5, "拡張のある cliamp に戻ってもリストが戻らない")
        yield 1.0

    def restart_fake(*extra: str):
        """偽の cliamp を別の設定で起こし直し、アプリが切れて繋ぎ直す (カタログを捨てる) まで待つ。"""
        seen = {"offline": False}

        def on_connection(_client, connected):
            if not connected:
                seen["offline"] = True

        client = ctx().client
        handler = client.connect("connection-changed", on_connection)
        try:
            fake.start(*extra)
            yield Until(lambda: seen["offline"] and store().connected and store().api == 1, 15,
                        f"偽の cliamp ({' '.join(extra) or '既定'}) に繋ぎ直さない")
            yield Until(lambda: bool(store().playlist.tracks), 5, "繋ぎ直した後にリストが戻らない")
        finally:
            client.disconnect(handler)

    def scene_web_only():
        """Spotify の接続が Web API だけの cliamp: 曲は YouTube で探して鳴らし、検索は断られる。"""
        window = win()
        yield from restart_fake("--spotify-web-only")
        resize(1180)
        window.navigate("playlist", provider="spotify", id="YOUR MUSIC", name="Your Music")
        yield Until(lambda: page_id() == "playlist" and len(page().tracks) == 5, 10,
                    "Web API だけの Spotify のプレイリストが開かない")
        detail = page()
        yield Until(lambda: detail.header.note_label.get_visible(), 5, "「YouTube で探して再生」の書き添えが出ない")
        check(detail.header.note_label.get_text() == WEB_ONLY_NOTE,
              f"書き添えが違います ({detail.header.note_label.get_text()!r})")
        check(provider_playback(ctx(), "spotify") == "youtube", "providers の playback を覚えていません")
        check(all(is_youtube_bridge(t) for t in detail.tracks), "曲が YouTube で探す形ではありません")
        check(selected() == "playlist:spotify:YOUR MUSIC", f"サイドバーが Spotify の行を選んでいません ({selected()})")
        # 2 曲目から鳴らす (探す曲の形のまま送り、再生バーには Spotify の曲名と絵)
        detail.play_from(1)
        yield Until(lambda: store().status.track is not None and is_youtube_bridge(store().status.track)
                    and store().status.index == 1 and store().status.state == "playing", 8,
                    "YouTube で探す曲が鳴らない")
        yield 1.8
        capture(window, "playlist-web-only")
        # すべてのプレイリスト: Spotify の節にも書き添える
        window.navigate("playlists")
        yield Until(lambda: page_id() == "playlists" and "spotify" in page().sections
                    and page().sections["spotify"].note.get_visible(), 10,
                    "すべてのプレイリストの Spotify の節に書き添えが出ない")
        yield 1.2
        check(page().sections["spotify"].note.get_text() == WEB_ONLY_NOTE, "Spotify の節の書き添えが違います")
        capture(window, "playlists-web-only")
        # 検索: Spotify の範囲は開発モードのアプリでは断られる。英語の文ではなく日本語の説明とボタン
        window.navigate("search")
        yield Until(lambda: page_id() == "search", 5, "検索が開かない")
        search = page()
        yield Until(lambda: search.scopes.get_n_toggles() == 3, 10, "Spotify の範囲が出ない")
        search.set_scope("spotify")
        search.set_query("夜のドライブ")
        yield Until(lambda: search.results.state == "empty", 10, "Spotify の検索の断りが出ない")
        window.set_focus(None)
        yield 1.0
        empty = search.results.empty
        check(empty.title_label.get_text() == SPOTIFY_SEARCH_BLOCKED_TITLE,
              f"Spotify の検索の断りの題が違います ({empty.title_label.get_text()!r})")
        check("client_id is too new" not in texts(search), "cliamp の英語の文がそのまま出ています")
        check(empty.button is not None and empty.button.get_visible(), "「YouTube で検索」のボタンがありません")
        capture(window, "search-spotify-blocked")
        # ボタンで YouTube の範囲に替えて探し直す
        empty.button.emit("clicked")
        yield Until(lambda: search.scope == "youtube" and search.results.state == "content", 10,
                    "「YouTube で検索」で YouTube の結果が出ない")
        yield from restart_fake()

    scenes = [scene_start, scene_home, scene_search, scene_search_results, scene_radio, scene_recent,
              scene_playlists, scene_playlist, scene_nowplaying, scene_lyrics, scene_queue,
              scene_fullscreen, scene_miniplayer, scene_equalizer, scene_stress, scene_playback_error,
              scene_narrow, scene_disconnected, scene_legacy, scene_web_only]

    def all_steps():
        for scene in scenes:
            name = scene.__name__.replace("scene_", "").replace("_", "-")
            print(f"shoot: 場面 {name}", flush=True)
            gen = scene()
            while True:
                try:
                    item = next(gen)
                except StopIteration:
                    break
                except Exception as exc:
                    traceback.print_exc()
                    fail(f"{name}: {exc}")
                    break
                yield item
        if args.keys:
            yield from key_checks(app, win, store, check, fail)

    steps = all_steps()

    def advance() -> bool:
        try:
            item = next(steps)
        except StopIteration:
            finish()
            return GLib.SOURCE_REMOVE
        if isinstance(item, Until):
            deadline = time.monotonic() + item.timeout

            def poll() -> bool:
                try:
                    ok = bool(item.predicate())
                except Exception as exc:
                    fail(f"{item.label}: 待つ条件で例外: {exc}")
                    ok = True
                if ok or time.monotonic() > deadline:
                    if not ok:
                        fail(f"{item.label} ({item.timeout} 秒)")
                    GLib.idle_add(advance)
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            GLib.timeout_add(50, poll)
        else:
            GLib.timeout_add(int(float(item or 0) * 1000), advance)
        return GLib.SOURCE_REMOVE

    def finish() -> None:
        a = app()
        if a is not None:
            a.quit_app()

    def start() -> bool:
        a = app()
        if a is None or a.window is None:
            return GLib.SOURCE_CONTINUE
        result["app"] = a
        GLib.idle_add(advance)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(100, start)
    try:
        code = app_module.main(["cliamp-music", "--socket", sock, "--page", "home"])
    finally:
        fake.stop()
    for message in getattr(result.get("app"), "css_errors", []):
        fail(f"CSS: {message}")
    # 窓の大きさなどが保存されたか
    import json

    state_path = Path(os.environ["XDG_STATE_HOME"]) / "cliamp-music" / "state.json"
    try:
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if not saved.get("window_width") or not saved.get("last_page"):
            fail(f"窓の保存値が足りません ({saved})")
    except (OSError, ValueError) as exc:
        fail(f"保存値を読めません: {exc}")
    for message in problems:
        print(f"shoot: 失敗: {message}", file=sys.stderr)
    print(f"shoot: {len(shots)} 枚を撮りました ({out})", flush=True)
    return 1 if problems else code


# --------------------------------------------------------------------------
# 子: 本物のキー (xdotool)


def key_checks(app, win, store, check, fail):
    """xdotool で本物のキーを送る。最後に Ctrl+Q で終わる。"""
    import warnings

    import gi

    gi.require_version("GdkX11", "4.0")
    from gi.repository import GdkX11, Gtk

    xdotool = shutil.which("xdotool")
    if not xdotool:
        fail("xdotool がありません (--keys)")
        return
    window = win()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        xid = GdkX11.X11Surface.get_xid(window.get_surface())

    def key(*keys: str) -> None:
        subprocess.run([xdotool, "key", "--clearmodifiers", *keys], check=False)

    print("shoot: キー操作を確かめます", flush=True)
    window.navigate("home")
    window.present()
    subprocess.run([xdotool, "windowfocus", "--sync", str(xid)], check=False)
    window.set_focus(None)
    yield 0.6

    before = store().status.state
    key("space")
    yield Until(lambda: store().status.state != before, 3, "Space で再生/一時停止にならない")
    if store().status.state != "playing":
        key("space")
        yield Until(lambda: store().status.state == "playing", 3, "Space で再生に戻らない")

    index = store().status.index
    key("ctrl+Right")
    yield Until(lambda: store().status.index != index, 3, "Ctrl+→ で次へ進まない")
    index = store().status.index
    key("ctrl+Left")
    yield Until(lambda: store().status.index != index, 3, "Ctrl+← で前へ戻らない")

    yield 1.0
    position = store().status.position
    key("shift+ctrl+Right")
    yield Until(lambda: store().status.position >= position + 8, 3, "Shift+Ctrl+→ で 10 秒進まない")

    volume = store().status.volume
    key("ctrl+Down")
    yield Until(lambda: store().status.volume <= volume - 1.5, 3, "Ctrl+↓ で音量が下がらない")
    volume = store().status.volume
    key("ctrl+Up")
    yield Until(lambda: store().status.volume >= volume + 1.5, 3, "Ctrl+↑ で音量が上がらない")

    key("ctrl+l")
    yield Until(lambda: getattr(window.current_page(), "page_id", "") == "nowplaying", 3,
                "Ctrl+L で再生中のリストが開かない")
    key("shift+ctrl+l")
    yield Until(lambda: window.panel == "lyrics", 3, "Shift+Ctrl+L で歌詞が開かない")
    key("ctrl+alt+u")
    yield Until(lambda: window.panel == "queue", 3, "Ctrl+Alt+U で次に再生が開かない")
    key("ctrl+alt+u")
    yield Until(lambda: window.panel == "", 3, "Ctrl+Alt+U で次に再生が閉じない")

    key("ctrl+f")
    yield Until(lambda: isinstance(window.get_focus(), (Gtk.Text, Gtk.Entry)), 3, "Ctrl+F で検索欄に入らない")
    entry = window.get_focus()
    entry = entry.get_ancestor(Gtk.Entry) if isinstance(entry, Gtk.Text) else entry
    state = store().status.state
    index = store().status.index
    key("a", "space", "b")
    yield 0.8
    check(entry.get_text() == "a b", f"検索欄に Space が入りません ({entry.get_text()!r})")
    check(store().status.state == state, "検索欄の Space で再生/一時停止になりました")
    key("ctrl+Left")
    yield 0.8
    check(store().status.index == index, "検索欄の Ctrl+← で曲が変わりました")
    window.set_focus(None)
    yield 0.3

    key("shift+ctrl+f")
    yield Until(lambda: window.fullscreen_shown, 3, "Shift+Ctrl+F でフルスクリーンにならない")
    yield 0.6
    key("Escape")
    yield Until(lambda: not window.fullscreen_shown, 3, "Esc でフルスクリーンから戻らない")

    a = app()
    key("shift+ctrl+m")
    yield Until(lambda: a.miniplayer is not None and a.miniplayer.get_visible(), 3,
                "Shift+Ctrl+M でミニプレーヤーが開かない")
    yield 0.6
    subprocess.run([xdotool, "windowfocus", "--sync", str(xid)], check=False)
    key("shift+ctrl+m")
    yield Until(lambda: a.miniplayer is None or not a.miniplayer.get_visible(), 3,
                "Shift+Ctrl+M でミニプレーヤーが閉じない")
    subprocess.run([xdotool, "windowfocus", "--sync", str(xid)], check=False)
    key("ctrl+alt+e")
    yield Until(lambda: a.equalizer is not None and a.equalizer.get_visible(), 3, "Ctrl+Alt+E でイコライザが開かない")
    a.equalizer.close()
    yield 0.4
    subprocess.run([xdotool, "windowfocus", "--sync", str(xid)], check=False)
    key("ctrl+r")
    yield 0.6
    key("ctrl+period")
    yield Until(lambda: store().status.state == "stopped", 3, "Ctrl+. で止まらない")
    key("ctrl+0")
    yield 0.4
    check(window.get_visible(), "Ctrl+0 でメインの窓が見えません")
    key("ctrl+q")
    yield Until(lambda: not window.get_visible() or a.window is None, 5, "Ctrl+Q で終わらない")


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="ミュージックの全画面を PNG に撮る")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="PNG を置くディレクトリ (既定: shots/)")
    parser.add_argument("--timeout", type=int, default=300, help="子を待つ秒数")
    parser.add_argument("--no-user-theme", action="store_true", help="利用者の gtk.css を写さない")
    parser.add_argument("--keys", action="store_true", help="xdotool で本物のキーを確かめる")
    parser.add_argument("--only", action="append", help="この画面だけ保存する (複数可。手順はすべて通す)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child_main(args)
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
