"""フルスクリーンプレーヤー・ミニプレーヤー・イコライザの試験。

画面の要らない小道具 (時刻の文字、音量の換算、歌詞を探す曲名、帯域の見出し) は
常に試す。部品を実際に置く試験は Xvfb などの私的な DISPLAY (:10 以上) があるときだけ
動かし、無ければ飛ばす。cliamp は tests/fake_cliamp.py の偽物を一時的なソケットで
立てる (本物のソケットには繋がない)。例:

    nix develop path:. -c xvfb-run -n 96 python3 -m unittest tests.test_players -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, isolate_display, run_loop, temp_socket_path  # noqa: E402

isolate_display()

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, GLib, Gtk

    from cliamp_music.protocol import EQ_BANDS, Lyrics, LyricLine, Status, Track

    HAVE_GI = True
except (ImportError, ValueError):  # pragma: no cover
    HAVE_GI = False

HAVE_DISPLAY = False
if HAVE_GI:
    HAVE_DISPLAY = bool(os.environ.get("DISPLAY")) and bool(Gtk.init_check())
    if HAVE_DISPLAY:
        Adw.init()
        # 試験の中でアニメーションを待たない
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)

if HAVE_GI:
    from cliamp_music import equalizer as eq_mod
    from cliamp_music import fullscreen as fs_mod


def synced_lyrics() -> "Lyrics":
    texts = ["一行目", "二行目 & <記号>", "", "The fourth line is long enough to wrap around", "五行目", "六行目"]
    return Lyrics(lines=[LyricLine(t=6.0 + i * 7.25, text=t) for i, t in enumerate(texts)], synced=True)


# --------------------------------------------------------------------------
# 画面の要らないもの


@unittest.skipUnless(HAVE_GI, "PyGObject がありません")
class HelperTest(unittest.TestCase):
    def test_time_texts(self):
        self.assertEqual(fs_mod.time_texts(60, 206), ("1:00", "−2:26"))
        # 長さを越えた位置は長さで止める
        self.assertEqual(fs_mod.time_texts(300, 206), ("3:26", "−0:00"))
        # 長さが分からなければ残りは空
        self.assertEqual(fs_mod.time_texts(12.9, 0), ("0:12", ""))
        self.assertEqual(fs_mod.time_texts(-3, 10), ("0:00", "−0:10"))

    def test_volume_round_trip(self):
        self.assertEqual(fs_mod.volume_fraction(-30), 0.0)
        self.assertEqual(fs_mod.volume_fraction(6), 1.0)
        self.assertAlmostEqual(fs_mod.volume_fraction(-12), 0.5)
        for db in (-30.0, -18.5, -6.0, 0.0, 6.0):
            self.assertAlmostEqual(fs_mod.fraction_to_volume(fs_mod.volume_fraction(db)), db)
        self.assertEqual(fs_mod.fraction_to_volume(2.0), 6.0)
        self.assertEqual(fs_mod.fraction_to_volume(-1.0), -30.0)

    def test_lyrics_subject(self):
        track = Track(path="/m/a.flac", title="夜明けのバス停", artist="青い灯台")
        self.assertEqual(fs_mod.lyrics_subject(Status(state="playing", track=track)), ("青い灯台", "夜明けのバス停"))
        self.assertIsNone(fs_mod.lyrics_subject(Status(state="playing")))
        station = Track(path="https://example.net/s.mp3", title="局", stream=True, live=True)
        # ICY の「アーティスト - 曲名」を割る
        status = Status(state="playing", track=station, stream_title="Aurora Lane - Glass Harbor")
        self.assertEqual(fs_mod.lyrics_subject(status), ("Aurora Lane", "Glass Harbor"))
        # 割れない ICY の曲名や、曲名の無いラジオは探さない
        self.assertIsNone(fs_mod.lyrics_subject(Status(state="playing", track=station, stream_title="ニュース")))
        self.assertIsNone(fs_mod.lyrics_subject(Status(state="playing", track=station)))
        # 題の無い曲は表示用の題で探す
        untitled = Track(path="/m/Artist/夜の歌.mp3", artist="A")
        self.assertEqual(fs_mod.lyrics_subject(Status(state="playing", track=untitled)), ("A", "夜の歌"))

    def test_band_and_preset_labels(self):
        self.assertEqual([eq_mod.band_label(b) for b in EQ_BANDS[:5]],
                         ["70 Hz", "180 Hz", "320 Hz", "600 Hz", "1 kHz"])
        self.assertEqual(eq_mod.band_label("16K"), "16 kHz")
        self.assertEqual(eq_mod.preset_label("Bass Boost"), "低音を強調")
        self.assertEqual(eq_mod.preset_label("R&B"), "R&B")
        self.assertEqual(eq_mod.preset_label("知らない名前"), "知らない名前")


# --------------------------------------------------------------------------
# 画面を使うもの


class StubHandle:
    def cancel(self):
        pass


class StubLoader:
    """ArtworkLoader の代役 (通信しない)。すぐに小さな絵を返す。"""

    def __init__(self):
        self._placeholders = {}
        self.requests = 0

    def placeholder(self, key, size, kind="track"):
        slot = (key, size, kind)
        if slot not in self._placeholders:
            self._placeholders[slot] = Gdk.MemoryTexture.new(
                2, 2, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(b"\x30\x50\x90\xff" * 4), 8)
        return self._placeholders[slot]

    def request(self, subject, size, callback):
        self.requests += 1
        texture = Gdk.MemoryTexture.new(2, 2, Gdk.MemoryFormat.R8G8B8A8,
                                        GLib.Bytes.new(b"\x90\x40\x30\xff" * 4), 8)
        GLib.idle_add(lambda: callback(texture) and False)
        return StubHandle()


class FakeWindow:
    def __init__(self):
        self.fullscreen_calls = []
        self.toasts = []
        self.panels = []
        self.presented = 0

    def show_fullscreen(self, on):
        self.fullscreen_calls.append(bool(on))

    def toast(self, text):
        self.toasts.append(text)

    def navigate(self, page_id, **params):
        pass

    def show_panel(self, name):
        self.panels.append(name)

    def present(self):
        self.presented += 1


class FakeApp:
    def __init__(self):
        self.mini = 0

    def show_miniplayer(self):
        self.mini += 1


@unittest.skipUnless(HAVE_DISPLAY, "画面 (Xvfb の DISPLAY) がありません")
class PlayerTestBase(unittest.TestCase):
    """偽の cliamp と本物の AppContext (絵だけ代役) を用意する。"""

    fake_kwargs: dict = {}

    def setUp(self):
        from cliamp_music.client import CliampClient
        from cliamp_music.context import AppContext
        from cliamp_music.state import GuiState

        self.tmp = tempfile.TemporaryDirectory()
        self.socket = temp_socket_path()
        self.fake = FakeCliamp(self.socket, **self.fake_kwargs).start()
        self.client = CliampClient(self.socket)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.window = FakeWindow()
        self.app = FakeApp()
        self.ctx = AppContext(self.app, window=self.window, client=self.client, artwork=StubLoader(),
                              state=GuiState(self.state_path))
        self.store = self.ctx.store
        self.client.start()
        self.assertTrue(run_loop(lambda: self.store.connected and self.store.status.seq > 0, 5.0),
                        "偽の cliamp に繋がりません")
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.destroy()
        run_loop(lambda: False, 0.05)
        self.client.stop()
        self.fake.stop()
        self.tmp.cleanup()

    def saved_extra(self) -> dict:
        with open(self.state_path, encoding="utf-8") as handle:
            return json.load(handle)["extra"]

    def host(self, child, width=1180, height=760):
        window = Gtk.Window()
        window.add_css_class("music")
        window.set_default_size(width, height)
        window.set_child(child)
        window.present()
        self.windows.append(window)
        self.assertTrue(run_loop(lambda: child.get_mapped() and child.get_width() > 0, 5.0))
        return window

    def play(self, index, at=None):
        self.store.play_index(index)
        self.assertTrue(run_loop(lambda: self.store.status.index == index
                                 and self.store.status.state == "playing", 5.0))
        if at is not None:
            self.store.seek_to(at)
            self.assertTrue(run_loop(lambda: self.store.status.position >= at - 0.5, 5.0))


class LyricsViewTest(PlayerTestBase):
    def test_current_line_follows_position(self):
        view = fs_mod.LyricsView()
        self.host(view, 600, 700)
        lyrics = synced_lyrics()
        view.set_lyrics(lyrics)
        run_loop(lambda: view.line_top(0) is not None, 2.0)
        for position, expected in ((0.0, -1), (6.0, 0), (13.3, 1), (20.6, 2), (26.0, 2), (44.0, 5), (900.0, 5)):
            view.set_position(position)
            self.assertEqual(view.current_index, expected, f"{position} 秒")
            self.assertEqual(view.current_index, lyrics.index_at(position))

    def test_current_line_sits_at_anchor(self):
        view = fs_mod.LyricsView()
        self.host(view, 600, 700)
        view.set_lyrics(synced_lyrics())
        view.set_position(28.0)  # 4 行目 (添字 3。27.75 秒から)
        run_loop(lambda: False, 0.1)
        self.assertEqual(view.current_index, 3)
        self.assertAlmostEqual(view.line_top(3), max(56.0, view.get_height() * view.ANCHOR), delta=1.0)
        # 前の行は上に、後の行は下に並ぶ
        self.assertLess(view.line_top(2), view.line_top(3))
        self.assertGreater(view.line_top(4), view.line_top(3))

    def test_click_seeks_to_line(self):
        view = fs_mod.LyricsView()
        self.host(view, 600, 700)
        lyrics = synced_lyrics()
        view.set_lyrics(lyrics)
        view.set_position(0.0)
        run_loop(lambda: False, 0.1)
        seeks = []
        view.on_seek = seeks.append
        y = view.line_top(4) + 5
        self.assertEqual(view.line_at(y), 4)
        self.assertEqual(view.seek_at(y), lyrics.lines[4].t)
        self.assertEqual(seeks, [lyrics.lines[4].t])
        self.assertEqual(view.current_index, 4)
        # 行の外 (はるか下) は何もしない
        self.assertIsNone(view.seek_at(10_000))
        self.assertEqual(len(seeks), 1)

    def test_unsynced_lyrics_do_not_seek(self):
        view = fs_mod.LyricsView()
        self.host(view, 600, 700)
        lines = synced_lyrics().lines
        view.set_lyrics(Lyrics(lines=[LyricLine(0.0, line.text) for line in lines], synced=False))
        view.set_position(30.0)
        self.assertEqual(view.current_index, -1)
        view.on_seek = lambda s: self.fail("同期していない歌詞で on_seek が呼ばれました")
        self.assertIsNone(view.seek_at(view.line_top(1) + 3))


class FullscreenTest(PlayerTestBase):
    def test_lyrics_line_and_seek_on_click(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        self.play(0, at=44.0)  # 夜明けのバス停 (偽物では同期した歌詞)
        fs.set_active(True)
        self.assertTrue(run_loop(lambda: fs.lyrics.lyrics is not None, 5.0), "歌詞が届きません")
        self.assertTrue(fs.lyrics.synced)
        self.assertTrue(run_loop(lambda: fs.lyrics.current_index == 5, 3.0),
                        f"今の行が {fs.lyrics.current_index}")
        self.assertTrue(run_loop(lambda: fs.lyrics.line_top(2) is not None, 3.0))
        target = fs.lyrics.line_time(2)
        before = len(self.fake.requests_for("seek_to"))
        fs.lyrics.seek_at(fs.lyrics.line_top(2) + 4)
        self.assertTrue(run_loop(lambda: len(self.fake.requests_for("seek_to")) > before, 3.0))
        sent = self.fake.requests_for("seek_to")[-1]["value"]
        self.assertAlmostEqual(sent, target, delta=0.2)
        self.assertTrue(run_loop(lambda: abs(self.store.status.position - target) < 1.5, 3.0))
        self.assertEqual(fs.lyrics.current_index, 2)

    def test_texts_escape_free_and_live(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        title = "長い曲名 & <特別版> *demo*"
        track = Track(path="https://example.net/a", title=title, artist="A & <B>", album="C", duration=200)
        self.store.replace([track], 0)
        self.assertTrue(run_loop(lambda: fs.title_label.get_text() == title, 5.0))
        self.assertEqual(fs.subtitle_label.get_text(), "A & <B> — C")
        station = Track(path="https://stream.example.net/x.mp3", title="局", stream=True, live=True)
        self.store.replace([station], 0)
        self.assertTrue(run_loop(lambda: self.store.status.is_live and bool(self.store.status.stream_title), 5.0))
        run_loop(lambda: fs.badge.get_text() == "ライブ", 2.0)
        self.assertEqual(fs.badge.get_text(), "ライブ")
        self.assertFalse(fs.scrubber.get_visible())
        self.assertEqual(fs.title_label.get_text(), self.store.status.stream_title)
        self.assertEqual(fs.subtitle_label.get_text(), "局")

    def test_inactive_player_does_not_listen(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        self.assertTrue(fs._handlers)
        fs.set_active(False)
        self.assertEqual(fs._handlers, [])
        old = fs.title_label.get_text()
        self.store.replace([Track(path="https://example.net/b", title="別の曲")], 0)
        run_loop(lambda: self.store.status.track is not None and self.store.status.track.title == "別の曲", 5.0)
        run_loop(lambda: False, 0.2)
        self.assertEqual(fs.title_label.get_text(), old)
        # 戻ると今の曲に合わせる
        fs.set_active(True)
        self.assertEqual(fs.title_label.get_text(), "別の曲")

    def test_buttons_reach_window_and_app(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.close_button.emit("clicked")
        fs.mini_button.emit("clicked")
        self.assertEqual(self.window.fullscreen_calls, [False])
        self.assertEqual(self.app.mini, 1)

    def test_queue_mode_lists_queue_and_up_next(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        fs.set_mode("queue")
        self.assertEqual(self.saved_extra()["fullscreen_mode"], "queue")
        self.assertTrue(run_loop(lambda: bool(self.store.playlist.up_next), 5.0))
        library = list(self.store.playlist.tracks)
        self.store.enqueue([library[5]], "next")
        self.assertTrue(run_loop(lambda: bool(self.store.playlist.queue), 5.0))
        run_loop(lambda: False, 0.3)

        def rows():
            out = []
            content = fs.queue._content
            child = content.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.ListBox):
                    out.append([row.index for row in child.rows()])
                child = child.get_next_sibling()
            return out

        lists = rows()
        self.assertEqual(len(lists), 2, lists)
        self.assertEqual(lists[0], list(self.store.playlist.queue))
        self.assertEqual(lists[1], list(self.store.playlist.up_next[: fs.queue.MAX_ROWS]))


class EqualizerTest(PlayerTestBase):
    def open_eq(self):
        window = eq_mod.EqualizerWindow(self.ctx)
        window.present()
        self.windows.append(window)
        self.assertTrue(run_loop(lambda: window.get_mapped() and bool(window.preset_names), 5.0))
        return window

    def test_presets_listed_with_custom(self):
        window = self.open_eq()
        model = window.preset_dropdown.get_model()
        labels = [model.get_string(i) for i in range(model.get_n_items())]
        self.assertEqual(labels[-1], eq_mod.CUSTOM_LABEL)
        self.assertEqual(labels[0], "フラット")
        self.assertEqual(len(labels), len(self.store.eq_presets) + 1)

    def test_band_edits_are_batched(self):
        window = self.open_eq()
        before = len([r for r in self.fake.requests_for("eq") if "band" in r])
        for i in range(15):
            window.set_band_value(2, -6.0 + i * 0.5)  # 最後は +1.0 dB
        self.assertTrue(run_loop(lambda: abs(self.fake.eq[2] - 1.0) < 0.01, 3.0), self.fake.eq)
        run_loop(lambda: False, 0.3)
        sent = [r for r in self.fake.requests_for("eq") if "band" in r][before:]
        self.assertLessEqual(len(sent), 2, sent)
        self.assertEqual(sent[-1]["band"], 2)
        self.assertEqual(self.store.status.eq_preset, "Custom")

    def test_faders_follow_cliamp_unless_held(self):
        window = self.open_eq()
        window._holding[1] = True
        self.fake.dispatch({"cmd": "eq", "name": "Rock"})
        rock = dict(eq_mod_presets())["Rock"]
        self.assertTrue(run_loop(lambda: self.store.status.eq_preset == "Rock"
                                 and abs(window.faders[0].get_value() - rock[0]) < 0.01, 5.0))
        for i, fader in enumerate(window.faders):
            if i == 1:
                self.assertAlmostEqual(fader.get_value(), 0.0)  # 押している間は合わせない
            else:
                self.assertAlmostEqual(fader.get_value(), rock[i])
        self.assertEqual(window.preset_dropdown.get_selected(), window.preset_names.index("Rock"))
        window._holding[1] = False
        window._touched[1] = 0.0
        self.store.emit("status-changed")
        self.assertAlmostEqual(window.faders[1].get_value(), rock[1])

    def test_preset_selection_sends_eq(self):
        window = self.open_eq()
        index = window.preset_names.index("Jazz")
        window.preset_dropdown.set_selected(index)
        self.assertTrue(run_loop(lambda: any(r.get("name") == "Jazz" for r in self.fake.requests_for("eq")), 3.0))
        self.assertTrue(run_loop(lambda: self.store.status.eq_preset == "Jazz", 3.0))

    def test_speed_is_sent_after_pause(self):
        window = self.open_eq()
        for value in (1.1, 1.2, 1.3):
            window.speed_scale.set_value(value)
        self.assertTrue(run_loop(lambda: abs(self.fake.speed - 1.3) < 0.001, 3.0), self.fake.speed)
        self.assertEqual(len(self.fake.requests_for("speed")), 1)


def eq_mod_presets():
    from fake_cliamp import EQ_PRESETS

    return EQ_PRESETS


class MiniPlayerTest(PlayerTestBase):
    def test_mode_switch_is_remembered(self):
        from cliamp_music.miniplayer import MiniPlayer, SIZES

        mini = MiniPlayer(self.ctx)
        mini.present()
        self.windows.append(mini)
        self.assertTrue(run_loop(lambda: mini.get_mapped(), 5.0))
        self.assertEqual(mini.mode, "square")
        self.assertFalse(mini.get_resizable())
        self.assertEqual(tuple(mini.get_size_request()), SIZES["square"])
        mini.toggle_compact()
        self.assertEqual(mini.mode, "compact")
        self.assertEqual(tuple(mini.get_size_request()), SIZES["compact"])
        self.assertEqual(self.saved_extra()["miniplayer_mode"], "compact")
        self.assertTrue(run_loop(lambda: mini.get_content().get_width() == 400
                                 and mini.get_content().get_height() == 110, 3.0),
                        f"{mini.get_content().get_width()}x{mini.get_content().get_height()}")
        # 次に作るときも横長で出る
        from cliamp_music.state import GuiState

        again = MiniPlayer(type(self.ctx)(self.app, window=self.window, client=self.client,
                                          artwork=StubLoader(), state=GuiState(self.state_path)))
        self.windows.append(again)
        self.assertEqual(again.mode, "compact")
        mini.set_mode("square")
        self.assertEqual(self.saved_extra()["miniplayer_mode"], "square")
        with self.assertRaises(ValueError):
            mini.set_mode("huge")

    def test_hover_band_and_close_hides(self):
        from cliamp_music.miniplayer import MiniPlayer

        mini = MiniPlayer(self.ctx)
        mini.set_mode("square")
        mini.present()
        self.windows.append(mini)
        self.assertTrue(run_loop(lambda: mini.get_mapped(), 5.0))
        self.assertEqual(mini._square_controls.get_opacity(), 0.0)
        mini.set_hover(True, force=True)
        self.assertEqual(mini._square_controls.get_opacity(), 1.0)
        self.assertTrue(mini._square_controls.get_can_target())
        self.assertTrue(mini._handlers)
        mini.close()
        run_loop(lambda: not mini.get_visible(), 2.0)
        self.assertFalse(mini.get_visible())
        self.assertEqual(mini._handlers, [], "隠れた窓が store のシグナルを受け続けています")

    def test_panel_buttons_open_main_window(self):
        from cliamp_music.miniplayer import MiniPlayer, _show_main

        mini = MiniPlayer(self.ctx)
        self.windows.append(mini)
        _show_main(self.ctx, "queue")
        self.assertEqual(self.window.panels, ["queue"])
        self.assertEqual(self.window.presented, 1)


if __name__ == "__main__":
    unittest.main()
