"""アートワークの取得・切り抜き・キャッシュ・代わりの絵。

cliamp はアートワークを一切持たないので、曲の path などから GUI が自分で求める
(art_sources)。取れた絵は正方形に切り抜き、最大 600px の PNG として
~/.cache/cliamp-music/artwork/ に置く。取れなければ key の hash で色を決めた
グラデーションに白い記号を描いた「代わりの絵」を返す。

取得はスレッドで行い、callback は main loop で必ず 1 回呼ぶ (cancel したときを除く)。
切り抜きの計算と求め方は純粋な関数にして、GTK なしで試験する。
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib  # noqa: E402

import cairo  # noqa: E402

from . import VERSION, log  # noqa: E402
from .protocol import Track, is_url, track_key  # noqa: E402

MAX_CACHE_PX = 600
TIMEOUT = 10.0
MAX_BYTES = 16 << 20
MEMORY_ITEMS = 400
WORKERS = 4
FAIL_TTL = 600.0  # 絵が取れなかった曲は 10 分は取りに行かない
MISSING_TTL = 3600.0  # 404 の URL は 1 時間は取りに行かない
DISK_MAX_FILES = 4000

SPOTIFY_OEMBED = "https://open.spotify.com/oembed?url="
FOLDER_COVERS = ("cover.jpg", "cover.png", "folder.jpg", "folder.png", "front.jpg", "Cover.jpg",
                 "Folder.jpg", "AlbumArt.jpg")
KINDS = ("track", "playlist", "station", "artist")

_YT_THUMB = re.compile(
    r"^https?://i\d?\.ytimg\.com/vi(?:_webp)?/[A-Za-z0-9_-]{11}/(?:hq|sd)default\.(?:jpg|webp)(?:\?.*)?$")


# ---------------------------------------------------------------------------
# 純粋な関数


def art_sources(track: Track, size: int = 0) -> list[str]:
    """曲の絵を探す順に並べた出どころ。

    1. meta["art"] (http(s):// か file://、または絶対パス)
    2. YouTube: 大きな表示 (>= 300px) では sddefault.jpg、次に hqdefault.jpg
    3. Spotify: oEmbed の URL (応答の JSON の thumbnail_url を取る)
    4. 手元のファイル: "embedded:<パス>" (埋め込みの絵) → 同じフォルダの cover/folder.(jpg|png)
    """
    out: list[str] = []

    def add(source: str) -> None:
        if source and source not in out:
            out.append(source)

    art = track.meta_get("art").strip()
    if art.startswith(("http://", "https://", "file://")):
        add(art)
    elif art.startswith("/"):
        add(Path(art).as_uri())
    video = track.youtube_id
    if video:
        if size >= 300:
            add(f"https://i.ytimg.com/vi/{video}/sddefault.jpg")
        add(f"https://i.ytimg.com/vi/{video}/hqdefault.jpg")
    spotify = track.spotify_id
    if spotify:
        add(f"{SPOTIFY_OEMBED}https://open.spotify.com/track/{spotify}")
    path = track.local_path
    if path and os.path.isabs(path):
        add("embedded:" + path)
        folder = os.path.dirname(path)
        for name in FOLDER_COVERS:
            add(Path(folder, name).as_uri())
    return out


def is_youtube_thumb(url: str) -> bool:
    """上下に黒帯の付く YouTube のサムネイル (hqdefault / sddefault、480x360 / 640x480) か。"""
    return bool(_YT_THUMB.match(url or ""))


def square_crop_box(width: int, height: int, letterboxed: bool = False) -> tuple[int, int, int, int]:
    """正方形に切り抜く範囲 (x, y, 幅, 高さ)。

    letterboxed: 4:3 の画像の中央に 16:9 の映像があり上下が黒帯のとき。16:9 の範囲の
    中央の正方形を取る (480x360 なら (105, 45, 270, 270))。そうでなければ中央の正方形。
    """
    if width <= 0 or height <= 0:
        return (0, 0, 0, 0)
    if letterboxed:
        band = min(height, int(round(width * 9 / 16)))
        top = (height - band) // 2
        side = min(width, band)
        return ((width - side) // 2, top + (band - side) // 2, side, side)
    side = min(width, height)
    return ((width - side) // 2, (height - side) // 2, side, side)


def detect_letterbox(pixels: bytes, width: int, height: int, rowstride: int, n_channels: int,
                     threshold: int = 24) -> bool:
    """16:9 の外側 (上下の帯) がほぼ真っ黒なら True。

    4:3 の古い動画のサムネイルには黒帯が無いので、決め打ちで切ると顔や文字が欠ける。
    帯の中の 3 行ずつを間引いて見て、97% 以上が暗ければ黒帯とみなす。
    """
    if width <= 0 or height <= 0 or n_channels < 3 or height < width * 0.6:
        return False
    band = (height - int(round(width * 9 / 16))) // 2
    if band < 4:
        return False
    rows = [max(0, int(band * f)) for f in (0.2, 0.5, 0.8)]
    rows += [height - 1 - r for r in rows]
    step = max(1, width // 64)
    dark = total = 0
    for y in rows:
        base = y * rowstride
        for x in range(0, width, step):
            i = base + x * n_channels
            if i + 2 >= len(pixels):
                continue
            total += 1
            if max(pixels[i], pixels[i + 1], pixels[i + 2]) <= threshold:
                dark += 1
    return total > 0 and dark / total >= 0.97


def placeholder_colors(key: str) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """代わりの絵のグラデーションの 2 色 (上と下、0〜1 の RGB)。key が同じなら同じ色。"""
    digest = hashlib.sha1((key or "").encode("utf-8", "replace")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65536.0
    top = colorsys.hsv_to_rgb(hue, 0.42, 0.66)
    bottom = colorsys.hsv_to_rgb((hue + 0.07) % 1.0, 0.58, 0.36)
    return top, bottom


def cache_file_name(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8", "replace")).hexdigest() + ".png"


def default_cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cliamp-music", "artwork")


# ---------------------------------------------------------------------------
# 代わりの絵 (cairo)


def _glyph(cr: cairo.Context, kind: str) -> None:
    """1x1 の枠に白い記号を描く。GTK の symbolic ではないので線も使える。"""
    if kind == "station":
        cr.arc(0.5, 0.5, 0.1, 0, 2 * math.pi)
        cr.fill()
        cr.set_line_width(0.075)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for radius in (0.25, 0.42):
            for start in (-0.7, math.pi - 0.7):
                cr.new_sub_path()
                cr.arc(0.5, 0.5, radius, start, start + 1.4)
                cr.stroke()
        return
    if kind == "artist":
        cr.arc(0.5, 0.33, 0.19, 0, 2 * math.pi)
        cr.fill()
        cr.move_to(0.12, 0.95)
        cr.curve_to(0.12, 0.66, 0.33, 0.57, 0.5, 0.57)
        cr.curve_to(0.67, 0.57, 0.88, 0.66, 0.88, 0.95)
        cr.close_path()
        cr.fill()
        return

    def head(cx: float, cy: float) -> None:
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(-0.38)
        cr.scale(0.17, 0.12)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.fill()

    if kind == "playlist":
        head(0.27, 0.8)
        head(0.74, 0.72)
        cr.rectangle(0.385, 0.2, 0.065, 0.6)
        cr.rectangle(0.855, 0.12, 0.065, 0.6)
        cr.fill()
        cr.move_to(0.385, 0.2)
        cr.line_to(0.92, 0.1)
        cr.line_to(0.92, 0.24)
        cr.line_to(0.385, 0.34)
        cr.close_path()
        cr.fill()
        return
    # track: 旗の付いた 8 分音符
    head(0.38, 0.78)
    cr.rectangle(0.5, 0.12, 0.07, 0.66)
    cr.fill()
    cr.move_to(0.5, 0.1)
    cr.curve_to(0.58, 0.22, 0.86, 0.3, 0.8, 0.58)
    cr.curve_to(0.76, 0.44, 0.68, 0.38, 0.57, 0.36)
    cr.line_to(0.5, 0.36)
    cr.close_path()
    cr.fill()


def draw_placeholder(key: str, size: int, kind: str = "track") -> cairo.ImageSurface:
    """代わりの絵を描いた ARGB32 の面。"""
    size = max(1, int(size))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    top, bottom = placeholder_colors(key)
    gradient = cairo.LinearGradient(0, 0, size, size)
    gradient.add_color_stop_rgb(0, *top)
    gradient.add_color_stop_rgb(1, *bottom)
    cr.set_source(gradient)
    cr.paint()
    glyph = size * (0.40 if kind != "station" else 0.46)
    cr.translate((size - glyph) / 2, (size - glyph) / 2)
    cr.scale(glyph, glyph)
    cr.set_source_rgba(1, 1, 1, 0.88)
    _glyph(cr, kind if kind in KINDS else "track")
    surface.flush()
    return surface


def texture_from_surface(surface: cairo.ImageSurface) -> Gdk.Texture:
    surface.flush()
    fmt = (Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED if sys.byteorder == "little"
           else Gdk.MemoryFormat.A8R8G8B8_PREMULTIPLIED)
    data = GLib.Bytes.new(bytes(surface.get_data()))
    return Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(), fmt, data, surface.get_stride())


def texture_from_pixbuf(pixbuf: GdkPixbuf.Pixbuf) -> Gdk.Texture:
    return _texture_from_pixels(_pixels_of(pixbuf))


def _pixels_of(pixbuf: GdkPixbuf.Pixbuf) -> tuple[int, int, int, bool, bytes]:
    return (pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride(), pixbuf.get_has_alpha(),
            bytes(pixbuf.get_pixels()))


def _texture_from_pixels(pixels: tuple[int, int, int, bool, bytes]) -> Gdk.Texture:
    width, height, stride, alpha, data = pixels
    fmt = Gdk.MemoryFormat.R8G8B8A8 if alpha else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(width, height, fmt, GLib.Bytes.new(data), stride)


# ---------------------------------------------------------------------------
# 取得 (スレッドで呼ぶ)


def urlopen(request, timeout):
    """urllib.request.urlopen の薄い包み (試験で差し替える)。"""
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_bytes(url: str, timeout: float = TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"cliamp-music/{VERSION}"})
    with urlopen(request, timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"画像が大きすぎます: {url}")
    return data


def embedded_art(path: str) -> bytes | None:
    """音声ファイルに埋め込まれた絵 (表紙を優先)。mutagen が無ければ None。"""
    try:
        import mutagen
        from mutagen.id3 import ID3, ID3NoHeaderError
    except ImportError:
        return None
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    candidates: list[tuple[int, bytes]] = []
    if audio is not None:
        for picture in getattr(audio, "pictures", None) or []:  # FLAC
            candidates.append((getattr(picture, "type", 0), bytes(picture.data)))
        tags = getattr(audio, "tags", None)
        if tags is not None:
            if hasattr(tags, "getall"):  # ID3 (MP3 など)
                for frame in tags.getall("APIC"):
                    candidates.append((int(getattr(frame, "type", 0)), bytes(frame.data)))
            try:
                covers = tags.get("covr") if hasattr(tags, "get") else None  # MP4
            except Exception:
                covers = None
            for cover in covers or []:
                candidates.append((3, bytes(cover)))
            try:
                blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Ogg
            except Exception:
                blocks = None
            if blocks:
                import base64

                from mutagen.flac import Picture

                for block in blocks:
                    try:
                        picture = Picture(base64.b64decode(block))
                    except Exception:
                        continue
                    candidates.append((picture.type, bytes(picture.data)))
    else:
        # 音声の本体が無く ID3 だけのファイル (試験など) も読めるようにする。
        try:
            tags = ID3(path)
        except (ID3NoHeaderError, Exception):
            return None
        for frame in tags.getall("APIC"):
            candidates.append((int(getattr(frame, "type", 0)), bytes(frame.data)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: 0 if item[0] == 3 else 1)
    return candidates[0][1]


def decode_image(data: bytes) -> GdkPixbuf.Pixbuf:
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error as exc:
        try:
            loader.close()
        except GLib.Error:
            pass
        raise ValueError(f"画像を読めません: {exc.message}") from None
    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        raise ValueError("画像を読めません")
    return pixbuf.apply_embedded_orientation() or pixbuf


def is_four_by_three(width: int, height: int) -> bool:
    """4:3 の横長 (480x360 / 640x480 など、黒帯付きの動画のサムネイルの形) か。"""
    return width > 0 and height > 0 and abs(width * 3 - height * 4) <= max(2, width // 100)


def square_pixbuf(pixbuf: GdkPixbuf.Pixbuf, letterbox_candidate: bool = False,
                  max_px: int = MAX_CACHE_PX) -> GdkPixbuf.Pixbuf:
    """正方形に切り抜く (最大 max_px)。

    黒帯の検出は YouTube のサムネイル (letterbox_candidate) と、出どころに関わらず
    4:3 の横長の絵に行う (yt-dlp が meta の art に書いた file:// や別の URL の
    サムネイルにも同じ黒帯がある)。検出は上下の帯がほぼ真っ黒なときだけ真になるので、
    ふつうの 4:3 の絵は中央の正方形のまま。"""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    letterboxed = False
    if letterbox_candidate or is_four_by_three(width, height):
        letterboxed = detect_letterbox(pixbuf.get_pixels(), width, height, pixbuf.get_rowstride(),
                                       pixbuf.get_n_channels())
    x, y, side, _ = square_crop_box(width, height, letterboxed)
    sub = pixbuf.new_subpixbuf(x, y, side, side)
    target = min(side, max_px)
    if target != side:
        return sub.scale_simple(target, target, GdkPixbuf.InterpType.HYPER)
    return sub.copy()


# ---------------------------------------------------------------------------
# 読み込み器


class _Job:
    """1 枚の絵の取得。waiters は (handle, callback) の並び。"""

    __slots__ = ("key", "size", "kind", "sources", "waiters")

    def __init__(self, key: str, size: int, kind: str, sources: list[str], waiters: list):
        self.key = key
        self.size = size
        self.kind = kind
        self.sources = sources
        self.waiters = waiters


class Handle:
    """request の戻り値。cancel() すると callback は呼ばれない。"""

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def _deliver(callback: Callable[[Gdk.Texture], object], texture: Gdk.Texture) -> None:
    try:
        callback(texture)
    except Exception as exc:
        import traceback

        log(f"アートワークの受け取りで例外: {exc}")
        traceback.print_exc()


class ArtworkLoader:
    """アートワークの読み込み器。main loop のスレッドから使う。"""

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = cache_dir or default_cache_dir()
        self._memory: OrderedDict[tuple, Gdk.Texture] = OrderedDict()
        self._pending: dict[tuple, _Job] = {}
        self._failed: dict[str, float] = {}
        self._missing: dict[str, float] = {}
        self._missing_lock = threading.Lock()
        self._jobs: queue.LifoQueue = queue.LifoQueue()  # 最後に頼まれた (いま見えている) ものから
        self._threads: list[threading.Thread] = []
        self._pruned = False

    # --- 公開 -------------------------------------------------------------------

    def request(self, subject: Any, size: int, callback: Callable[[Gdk.Texture], object]) -> Handle:
        """絵を頼む。subject は Track / URL の文字列 / ("placeholder", key[, kind])。

        size は画素数 (HiDPI では倍にして渡す)。callback(Gdk.Texture) は main loop で
        必ず 1 回呼ばれる (取れなければ代わりの絵)。handle.cancel() で取り消せる。
        """
        size = max(1, int(size))
        handle = Handle()
        key, sources, kind = self._describe(subject, size)
        if isinstance(subject, tuple):
            self._soon(handle, callback, self.placeholder(key, size, kind))
            return handle
        cached = self._memory_get((key, size))
        if cached is not None:
            self._soon(handle, callback, cached)
            return handle
        failed_until = self._failed.get(key)
        if not sources or (failed_until is not None and failed_until > time.monotonic()):
            self._soon(handle, callback, self.placeholder(key, size, kind))
            return handle
        job = self._pending.get((key, size))
        if job is not None:
            # 同じ絵を待っている依頼があれば相乗りする (取りに行くのは 1 回)。
            job.waiters.append((handle, callback))
            return handle
        job = _Job(key, size, kind, sources, [(handle, callback)])
        self._pending[(key, size)] = job
        self._enqueue(job)
        return handle

    def placeholder(self, key: str, size: int, kind: str = "track") -> Gdk.Texture:
        """代わりの絵をすぐ返す。色は key の hash で決まる。kind: track / playlist / station / artist。"""
        size = max(1, int(size))
        memory_key = ("placeholder", key or "", size, kind)
        cached = self._memory_get(memory_key)
        if cached is not None:
            return cached
        texture = texture_from_surface(draw_placeholder(key or "", size, kind))
        self._memory_put(memory_key, texture)
        return texture

    def clear_memory(self) -> None:
        self._memory.clear()
        self._failed.clear()

    # --- 内部 -------------------------------------------------------------------

    @staticmethod
    def _describe(subject: Any, size: int) -> tuple[str, list[str], str]:
        if isinstance(subject, tuple):
            key = str(subject[1]) if len(subject) > 1 else ""
            kind = str(subject[2]) if len(subject) > 2 else "track"
            return key, [], kind
        if isinstance(subject, Track):
            return track_key(subject), art_sources(subject, size), "station" if subject.live else "track"
        if isinstance(subject, str) and subject:
            source = subject
            if subject.startswith("/"):
                source = Path(subject).as_uri()
            return subject, [source], "track"
        return "", [], "track"

    def _soon(self, handle: Handle, callback: Callable, texture: Gdk.Texture) -> None:
        # 同期では呼ばない (呼び出し側がまだ handle を受け取っていないため)。
        # 描画 (HIGH_IDLE + 20) より前に走るので、手元にある絵ならちらつかない。
        def run() -> bool:
            if not handle.cancelled:
                _deliver(callback, texture)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run, priority=GLib.PRIORITY_HIGH_IDLE)

    def _memory_get(self, key: tuple) -> Gdk.Texture | None:
        texture = self._memory.get(key)
        if texture is not None:
            self._memory.move_to_end(key)
        return texture

    def _memory_put(self, key: tuple, texture: Gdk.Texture) -> None:
        self._memory[key] = texture
        self._memory.move_to_end(key)
        while len(self._memory) > MEMORY_ITEMS:
            self._memory.popitem(last=False)

    def _enqueue(self, job: "_Job") -> None:
        def run() -> None:
            if all(handle.cancelled for handle, _ in list(job.waiters)):
                GLib.idle_add(self._finish, job, None, True)
                return
            pixels = None
            try:
                pixels = self._load(job.sources, job.size)
            except Exception as exc:  # 1 枚の失敗で読み込み器を止めない
                log(f"アートワークの読み込みで例外: {exc}")
            GLib.idle_add(self._finish, job, pixels, False)

        self._jobs.put(run)
        if not self._pruned:
            self._pruned = True
            self._jobs.put(self._prune_disk)
        alive = [t for t in self._threads if t.is_alive()]
        self._threads = alive
        if len(alive) < WORKERS:
            thread = threading.Thread(target=self._work, name="cliamp-artwork", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _work(self) -> None:
        while True:
            try:
                run = self._jobs.get(timeout=30)
            except queue.Empty:
                return
            try:
                run()
            except Exception as exc:
                log(f"アートワークの処理で例外: {exc}")

    def _finish(self, job: "_Job", pixels, skipped: bool) -> bool:
        live = [(h, cb) for h, cb in job.waiters if not h.cancelled]
        if skipped and live:
            # 取り消しで飛ばした後に同じ絵の依頼が来ていた。やり直す。
            job.waiters = live
            self._enqueue(job)
            return GLib.SOURCE_REMOVE
        if self._pending.get((job.key, job.size)) is job:
            del self._pending[(job.key, job.size)]
        if skipped:
            return GLib.SOURCE_REMOVE
        if pixels is None:
            self._failed[job.key] = time.monotonic() + FAIL_TTL
            texture = self.placeholder(job.key, job.size, job.kind)
        else:
            texture = _texture_from_pixels(pixels)
            self._memory_put((job.key, job.size), texture)
        for handle, callback in live:
            _deliver(callback, texture)
        return GLib.SOURCE_REMOVE

    def _load(self, sources: list[str], size: int):
        for source in sources:
            pixbuf = self._load_source(source)
            if pixbuf is None:
                continue
            if pixbuf.get_width() > size:
                pixbuf = pixbuf.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
            return _pixels_of(pixbuf)
        return None

    def _is_missing(self, source: str) -> bool:
        with self._missing_lock:
            until = self._missing.get(source)
            return until is not None and until > time.monotonic()

    def _mark_missing(self, source: str, ttl: float) -> None:
        with self._missing_lock:
            self._missing[source] = time.monotonic() + ttl

    def _load_source(self, source: str) -> GdkPixbuf.Pixbuf | None:
        """1 つの出どころから正方形の絵 (最大 600px) を作る。取れなければ None。"""
        try:
            if source.startswith("embedded:"):
                data = embedded_art(source[len("embedded:"):])
                return square_pixbuf(decode_image(data)) if data else None
            if source.startswith("file://"):
                path = unquote(urlsplit(source).path)
                if not os.path.isfile(path):
                    return None
                with open(path, "rb") as handle:
                    return square_pixbuf(decode_image(handle.read()))
        except (OSError, ValueError, GLib.Error) as exc:
            log(f"{source}: {exc}")
            return None
        if not is_url(source):
            return None
        cache_path = os.path.join(self.cache_dir, cache_file_name(source))
        if os.path.exists(cache_path):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(cache_path)
                try:
                    os.utime(cache_path)  # 古いものから消すので、使ったら新しくする
                except OSError:
                    pass
                return pixbuf
            except GLib.Error:
                try:
                    os.unlink(cache_path)
                except OSError:
                    pass
        if self._is_missing(source):
            return None
        try:
            if source.startswith(SPOTIFY_OEMBED):
                info = json.loads(fetch_bytes(source).decode("utf-8"))
                thumbnail = info.get("thumbnail_url") if isinstance(info, dict) else None
                if not isinstance(thumbnail, str) or not is_url(thumbnail):
                    self._mark_missing(source, MISSING_TTL)
                    return None
                data = fetch_bytes(thumbnail)
            else:
                data = fetch_bytes(source)
            pixbuf = square_pixbuf(decode_image(data), letterbox_candidate=is_youtube_thumb(source))
        except urllib.error.HTTPError as exc:
            self._mark_missing(source, MISSING_TTL if exc.code in (403, 404, 410) else FAIL_TTL)
            return None
        except Exception as exc:
            log(f"{source}: {exc}")
            self._mark_missing(source, FAIL_TTL)
            return None
        self._save(pixbuf, cache_path)
        return pixbuf

    def _save(self, pixbuf: GdkPixbuf.Pixbuf, path: str) -> None:
        tmp = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".art-", suffix=".png", dir=os.path.dirname(path))
            os.close(fd)
            pixbuf.savev(tmp, "png", [], [])
            os.replace(tmp, path)
            tmp = None
        except (OSError, GLib.Error) as exc:
            log(f"アートワークを保存できません: {exc}")
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _prune_disk(self) -> None:
        """ディスクのキャッシュが増えすぎたら古いものから消す。"""
        try:
            entries = [e for e in os.scandir(self.cache_dir) if e.name.endswith(".png") and e.is_file()]
        except OSError:
            return
        if len(entries) <= DISK_MAX_FILES:
            return
        entries.sort(key=lambda e: e.stat().st_mtime)
        for entry in entries[: len(entries) - DISK_MAX_FILES * 3 // 4]:
            try:
                os.unlink(entry.path)
            except OSError:
                pass
