"""artwork.py の試験。ネットワークには出ない (urlopen を差し替える)。画面も要らない。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import isolate_display, run_loop  # noqa: E402

isolate_display()

try:
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf

    from cliamp_music import artwork
    from cliamp_music.artwork import ArtworkLoader, art_sources, detect_letterbox, square_crop_box
    from cliamp_music.protocol import Track

    HAVE_GTK = True
except (ImportError, ValueError):  # pragma: no cover
    HAVE_GTK = False

VID = "BoW3OHT6g0s"
YT = Track(path=f"https://www.youtube.com/watch?v={VID}", title="曲")
HQ = f"https://i.ytimg.com/vi/{VID}/hqdefault.jpg"
SD = f"https://i.ytimg.com/vi/{VID}/sddefault.jpg"
RED = 0xE0202000
BLUE = 0x2040E000
BLACK = 0x000000FF


def solid(width, height, color):
    pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    pixbuf.fill(color)
    return pixbuf


def letterboxed(width=480, height=360):
    """YouTube の hqdefault の形: 上下が黒帯、16:9 の中は両脇が青で中央の正方形が赤。"""
    pixbuf = solid(width, height, BLACK)
    band = round(width * 9 / 16)
    top = (height - band) // 2
    pixbuf.new_subpixbuf(0, top, width, band).fill(BLUE)
    pixbuf.new_subpixbuf((width - band) // 2, top, band, band).fill(RED)
    return pixbuf


def png(pixbuf) -> bytes:
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    assert ok
    return bytes(data)


def pixel(texture, x, y):
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
    data, stride = downloader.download_bytes()
    raw = data.get_data()
    i = y * stride + x * 4
    return tuple(raw[i:i + 3])


class FakeHTTP(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PureFunctions(unittest.TestCase):
    @unittest.skipUnless(HAVE_GTK, "GTK がありません")
    def test_art_sources(self):
        self.assertEqual(art_sources(YT, 128), [HQ])
        self.assertEqual(art_sources(YT, 400), [SD, HQ])
        music = Track(path=f"https://music.youtube.com/watch?v={VID}", meta={"art": "https://img.example/a.jpg"})
        self.assertEqual(art_sources(music, 64), ["https://img.example/a.jpg", HQ])
        spotify = Track(path="spotify:track:4uLU6hMCjMI75M1A2tKUQC")
        self.assertEqual(art_sources(spotify), [
            "https://open.spotify.com/oembed?url=https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"])
        local = art_sources(Track(path="/music/盤 1/01 曲.flac"))
        self.assertEqual(local[0], "embedded:/music/盤 1/01 曲.flac")
        self.assertIn("file:///music/%E7%9B%A4%201/cover.jpg", local)
        self.assertIn("file:///music/%E7%9B%A4%201/folder.png", local)
        self.assertEqual(art_sources(Track(path="https://r.example/stream", live=True,
                                           meta={"art": "https://r.example/favicon.png"})),
                         ["https://r.example/favicon.png"])
        self.assertEqual(art_sources(Track(path="x", meta={"art": "/tmp/a.png"})), ["file:///tmp/a.png"])
        self.assertEqual(art_sources(Track(path="https://r.example/stream")), [])

    @unittest.skipUnless(HAVE_GTK, "GTK がありません")
    def test_crop_boxes(self):
        self.assertEqual(square_crop_box(480, 360, True), (105, 45, 270, 270))
        self.assertEqual(square_crop_box(640, 480, True), (140, 60, 360, 360))
        self.assertEqual(square_crop_box(480, 360, False), (60, 0, 360, 360))
        self.assertEqual(square_crop_box(300, 200), (50, 0, 200, 200))
        self.assertEqual(square_crop_box(200, 300), (0, 50, 200, 200))
        self.assertEqual(square_crop_box(0, 10), (0, 0, 0, 0))

    @unittest.skipUnless(HAVE_GTK, "GTK がありません")
    def test_detect_letterbox(self):
        def check(pixbuf):
            return detect_letterbox(pixbuf.get_pixels(), pixbuf.get_width(), pixbuf.get_height(),
                                    pixbuf.get_rowstride(), pixbuf.get_n_channels())

        self.assertTrue(check(letterboxed()))
        self.assertTrue(check(letterboxed(640, 480)))
        self.assertFalse(check(solid(480, 360, BLUE)))  # 4:3 の古い動画 (黒帯なし)
        self.assertFalse(check(solid(640, 360, BLACK)))  # 16:9 にはそもそも帯が無い
        dark_top = solid(480, 360, BLUE)
        dark_top.new_subpixbuf(0, 0, 480, 45).fill(BLACK)  # 上だけ黒い
        self.assertFalse(check(dark_top))

    @unittest.skipUnless(HAVE_GTK, "GTK がありません")
    def test_square_pixbuf_crops_letterbox(self):
        square = artwork.square_pixbuf(letterboxed(), letterbox_candidate=True)
        self.assertEqual((square.get_width(), square.get_height()), (270, 270))
        texture = artwork.texture_from_pixbuf(square)
        for x, y in ((2, 2), (135, 135), (267, 267)):
            self.assertEqual(pixel(texture, x, y), (0xE0, 0x20, 0x20))
        big = artwork.square_pixbuf(solid(1200, 900, RED))
        self.assertEqual(big.get_width(), artwork.MAX_CACHE_PX)
        # 出どころが YouTube でなくても (file:// など)、4:3 で黒帯があれば切り抜く
        local = artwork.square_pixbuf(letterboxed())
        self.assertEqual((local.get_width(), local.get_height()), (270, 270))
        self.assertEqual(pixel(artwork.texture_from_pixbuf(local), 2, 2), (0xE0, 0x20, 0x20))
        # 黒帯の無い 4:3 は中央の正方形のまま
        plain = artwork.square_pixbuf(solid(480, 360, BLUE))
        self.assertEqual((plain.get_width(), plain.get_height()), (360, 360))
        self.assertTrue(artwork.is_four_by_three(640, 480))
        self.assertFalse(artwork.is_four_by_three(640, 360))

    @unittest.skipUnless(HAVE_GTK, "GTK がありません")
    def test_placeholder_colors_are_stable(self):
        self.assertEqual(artwork.placeholder_colors("a"), artwork.placeholder_colors("a"))
        self.assertNotEqual(artwork.placeholder_colors("a"), artwork.placeholder_colors("b"))
        for rgb in artwork.placeholder_colors("key"):
            self.assertTrue(all(0 <= c <= 1 for c in rgb))


@unittest.skipUnless(HAVE_GTK, "GTK がありません")
class Loader(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="cm-art-")
        self.served: dict[str, bytes] = {HQ: png(letterboxed())}
        self.fetched: list[str] = []
        self.agents: list[str] = []

        def fake_urlopen(request, timeout):
            url = request.full_url
            self.fetched.append(url)
            self.agents.append(request.get_header("User-agent"))
            if url not in self.served:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return FakeHTTP(self.served[url])

        patcher = mock.patch.object(artwork, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        log = mock.patch.object(artwork, "log")
        log.start()
        self.addCleanup(log.stop)
        self.loader = ArtworkLoader(self.cache)

    def get(self, subject, size, timeout=5.0):
        got = []
        self.loader.request(subject, size, got.append)
        self.assertTrue(run_loop(lambda: got, timeout=timeout))
        run_loop(lambda: False, timeout=0.05)
        self.assertEqual(len(got), 1)  # 必ず 1 回
        return got[0]

    def cache_files(self):
        return sorted(n for n in os.listdir(self.cache) if n.endswith(".png")) if os.path.isdir(self.cache) else []

    def test_youtube_is_cropped_cached_and_reused(self):
        texture = self.get(YT, 128)
        self.assertEqual((texture.get_width(), texture.get_height()), (128, 128))
        self.assertEqual(pixel(texture, 5, 5), (0xE0, 0x20, 0x20))  # 黒帯と両脇の青は落ちている
        self.assertEqual(self.fetched, [HQ])
        self.assertTrue(self.agents[0].startswith("cliamp-music/"))
        self.assertEqual(self.cache_files(), [artwork.cache_file_name(HQ)])
        cached = GdkPixbuf.Pixbuf.new_from_file(os.path.join(self.cache, artwork.cache_file_name(HQ)))
        self.assertEqual(cached.get_width(), 270)
        again = self.get(YT, 128)
        self.assertIs(again, texture)  # 手元 (メモリ) から
        other = self.get(YT, 64)
        self.assertEqual(other.get_width(), 64)
        fresh = ArtworkLoader(self.cache)  # ディスクのキャッシュから (取りに行かない)
        got = []
        fresh.request(YT, 200, got.append)
        self.assertTrue(run_loop(lambda: got))
        self.assertEqual(self.fetched, [HQ])

    def test_concurrent_requests_fetch_once(self):
        got = []
        self.loader.request(YT, 96, got.append)
        self.loader.request(YT, 96, got.append)
        self.assertTrue(run_loop(lambda: len(got) == 2))
        self.assertIs(got[0], got[1])
        self.assertEqual(self.fetched, [HQ])
        by_url = self.get(HQ, 96)  # URL で頼んでも同じ出どころなのでディスクから
        self.assertEqual(by_url.get_width(), 96)
        self.assertEqual(self.fetched, [HQ])

    def test_sd_falls_back_to_hq_and_404_is_remembered(self):
        texture = self.get(YT, 400)
        self.assertEqual(self.fetched, [SD, HQ])
        self.assertEqual(texture.get_width(), 270)  # 元より大きくは引き伸ばさない
        self.loader.clear_memory()
        self.get(YT, 400)
        self.assertEqual(self.fetched, [SD, HQ])  # 404 の sddefault は取りに行かず、hq はディスクから

    def test_failure_gives_placeholder_once(self):
        track = Track(path="https://www.youtube.com/watch?v=zzzzzzzzzzz", title="無い")
        texture = self.get(track, 80)
        self.assertEqual(texture.get_width(), 80)
        self.assertEqual(texture.get_height(), 80)
        count = len(self.fetched)
        self.get(track, 80)
        self.assertEqual(len(self.fetched), count)  # 失敗はしばらく覚える

    def test_cancel_suppresses_callback(self):
        got = []
        handle = self.loader.request(YT, 128, got.append)
        handle.cancel()
        run_loop(lambda: False, timeout=0.3)
        self.assertEqual(got, [])
        # 取り消した後の同じ依頼にはちゃんと応える
        self.assertEqual(self.get(YT, 128).get_width(), 128)

    def test_placeholder_subjects(self):
        texture = self.get(("placeholder", "プレイリスト", "playlist"), 50)
        self.assertEqual(texture.get_width(), 50)
        for kind in artwork.KINDS:
            t = self.loader.placeholder("k", 40, kind)
            self.assertEqual((t.get_width(), t.get_height()), (40, 40))
        self.assertIs(self.loader.placeholder("k", 40, "track"), self.loader.placeholder("k", 40, "track"))
        self.assertNotEqual(pixel(self.loader.placeholder("a", 20), 1, 1),
                            pixel(self.loader.placeholder("b", 20), 1, 1))
        station = self.get(Track(path="https://r.example/s", live=True), 32)  # 絵の無い局は局の絵
        self.assertEqual(station.get_width(), 32)
        self.assertEqual(self.fetched, [])

    def test_spotify_oembed(self):
        sid = "4uLU6hMCjMI75M1A2tKUQC"
        oembed = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{sid}"
        thumb = "https://image-cdn.example/ab67616d00001e02"
        self.served[oembed] = json.dumps({"thumbnail_url": thumb, "title": "x"}).encode()
        self.served[thumb] = png(solid(300, 300, BLUE))
        texture = self.get(Track(path=f"spotify:track:{sid}"), 100)
        self.assertEqual(pixel(texture, 50, 50), (0x20, 0x40, 0xE0))
        self.assertEqual(self.fetched, [oembed, thumb])

    def test_file_url_and_folder_cover(self):
        folder = tempfile.mkdtemp(prefix="cm-album-")
        Path(folder, "cover.png").write_bytes(png(solid(320, 240, BLUE)))
        track = Track(path=os.path.join(folder, "01 曲.flac"))
        texture = self.get(track, 64)
        self.assertEqual(pixel(texture, 32, 32), (0x20, 0x40, 0xE0))
        direct = self.get(Path(folder, "cover.png").as_uri(), 32)
        self.assertEqual(direct.get_width(), 32)
        self.assertEqual(self.fetched, [])

    def test_embedded_art(self):
        try:
            from mutagen.id3 import APIC, ID3
        except ImportError:
            self.skipTest("mutagen がありません")
        folder = tempfile.mkdtemp(prefix="cm-tag-")
        path = os.path.join(folder, "tagged.mp3")
        Path(path).write_bytes(b"")
        tags = ID3()
        tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=png(solid(200, 200, RED))))
        tags.save(path)
        self.assertIsNotNone(artwork.embedded_art(path))
        texture = self.get(Track(path=path), 48)
        self.assertEqual(pixel(texture, 24, 24), (0xE0, 0x20, 0x20))

    def test_broken_image_is_not_cached(self):
        self.served[HQ] = b"not an image"
        texture = self.get(YT, 64)
        self.assertEqual(texture.get_width(), 64)  # 代わりの絵
        self.assertEqual(self.cache_files(), [])


if __name__ == "__main__":
    unittest.main()
