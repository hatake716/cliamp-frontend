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

    def test_volume_mapping_is_shared_with_the_bar(self):
        from cliamp_music import protocol

        self.assertIs(fs_mod.volume_fraction, protocol.volume_fraction)
        self.assertIs(fs_mod.fraction_to_volume, protocol.fraction_to_volume)

    def test_text_input_follows_the_active_window(self):
        """入力中かは前にある窓のフォーカスで決める (隠れたメインの窓の入力欄では止めない)。"""
        from cliamp_music.app import text_input_focused

        class Win:
            def __init__(self, active, focus):
                self._active, self._focus = active, focus

            def is_active(self):
                return self._active

            def get_focus(self):
                return self._focus

        entry = Gtk.Text() if HAVE_DISPLAY else None
        if entry is None:
            self.skipTest("画面がありません")
        self.assertFalse(text_input_focused([Win(False, entry), Win(True, None)]))
        self.assertTrue(text_input_focused([Win(True, entry), Win(False, None)]))
        self.assertFalse(text_input_focused([Win(False, entry)]))

    def test_hidden_states(self):
        from cliamp_music.app import state_hidden

        self.assertTrue(state_hidden(Gdk.ToplevelState.SUSPENDED))  # Wayland (mutter) の最小化・覆われた窓
        self.assertTrue(state_hidden(Gdk.ToplevelState.MINIMIZED))  # X11
        self.assertFalse(state_hidden(Gdk.ToplevelState.FOCUSED | Gdk.ToplevelState.MAXIMIZED))

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
        self.store.queue_edit("add", index=5)
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


    def test_levels_grow_on_big_screens(self):
        """本当のフルスクリーン (1920x1080) では絵と歌詞を大きくする (窓の大きさの 360px のままにしない)。"""
        fs = fs_mod.FullscreenPlayer(self.ctx)
        window = self.host(fs, 1920, 1080)
        self.assertTrue(run_loop(lambda: fs._level == "xlarge", 3.0), fs._level)
        self.assertEqual(fs.art.get_width(), 440)
        self.assertGreater(fs.lyrics._font_px, 34)
        self.assertTrue(fs.has_css_class("xlarge"))
        window.set_default_size(1180, 760)
        self.assertTrue(run_loop(lambda: fs._level == "large", 3.0), fs._level)

    def test_queue_says_it_continues_after_reshuffle(self):
        """シャッフル + リピート (すべて) で一巡の最後の曲: up_next は空でも「続く」と書く。"""
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        fs.set_mode("queue")
        self.store.set_shuffle(True)
        self.store.set_repeat("all")
        self.assertTrue(run_loop(lambda: self.fake.pl.shuffle and self.fake.pl.repeat == "all", 3.0))
        self.store.play_index(self.fake.pl.order[-1])
        self.assertTrue(run_loop(lambda: self.store.status.shuffle and not self.store.playlist.up_next
                                 and self.store.playlist.index == self.fake.pl.order[-1]
                                 and not self.store._overrides, 5.0))
        fs.queue.refresh()
        self.assertTrue(run_loop(lambda: fs.queue._stack.get_visible_child_name() == "empty", 3.0))
        self.assertIn("シャッフルし直して", fs.queue._empty.title_label.get_text())
        # リピートを切ると (up_next は同じ空でも) ふつうの「ありません」に戻る
        self.store.set_repeat("off")
        self.assertTrue(run_loop(lambda: "ありません" in fs.queue._empty.title_label.get_text(), 3.0))

    def test_queued_row_plays_without_replaying(self):
        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        fs.set_mode("queue")
        self.assertTrue(run_loop(lambda: len(self.store.playlist.tracks) == 12, 5.0))
        for index in (5, 7):
            self.store.queue_edit("add", index=index)
            self.assertTrue(run_loop(lambda index=index: index in self.store.playlist.queue, 3.0))
        self.assertTrue(run_loop(lambda: self.store.playlist.queue == [5, 7]
                                 and self.store.playlist.track_at(7).queued == 2, 3.0))
        fs.queue.refresh(force=True)
        queue_rows = next(child for child in _children(fs.queue._content) if isinstance(child, Gtk.ListBox))
        row = queue_rows.rows()[1]
        self.assertEqual(row.index, 7)
        queue_rows.emit("row-activated", row)
        self.assertTrue(run_loop(lambda: self.fake.pl.index() == 7 and not self.fake.pl.queue, 5.0))
        self.assertEqual(self.fake.requests_for("play_index"), [])


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


class PlayerBarTest(PlayerTestBase):
    def bar(self):
        from cliamp_music.playerbar import PlayerBar

        bar = PlayerBar(self.ctx)
        self.host(bar, 900, 80)
        return bar

    def test_volume_matches_fullscreen_slider(self):
        bar = self.bar()
        self.store.set_volume_db(-6.0)
        self.assertTrue(run_loop(lambda: self.fake.volume == -6.0 and not self.store._overrides, 3.0))
        bar._volume_user_at = 0.0
        self.store.emit("status-changed")
        fs = fs_mod.VolumeControl(self.store)
        fs.update(self.store.status)
        self.assertAlmostEqual(bar.volume_scale.get_value(), fs.slider.fraction, places=2)
        self.assertAlmostEqual(bar.volume_scale.get_value(), 2 / 3, places=2)
        self.assertEqual(bar.volume.get_tooltip_text(), "音量 67%")
        bar.volume_scale.set_value(0.5)
        self.assertTrue(run_loop(lambda: abs(self.fake.volume - (-12.0)) < 0.2, 3.0), self.fake.volume)

    def test_speed_not_in_menu_checks_nothing(self):
        bar = self.bar()
        self.store.set_speed(1.1)
        self.assertTrue(run_loop(lambda: abs(self.store.status.speed - 1.1) < 1e-6, 3.0))
        self.store.emit("status-changed")
        self.assertEqual(bar.actions.lookup_action("speed").get_state().get_string(), "")
        self.store.set_speed(1.5)
        self.assertTrue(run_loop(lambda: bar.actions.lookup_action("speed").get_state().get_string() == "1.5", 3.0))

    def test_output_menu_shows_labels_and_sends_sink_names(self):
        bar = self.bar()
        with self.fake.lock:
            self.fake.device_descriptions = True
        bar.output.set_visible(True)
        bar.output.popup()
        items = bar._device_items
        self.assertTrue(run_loop(lambda: items.get_n_items() == 3, 3.0))
        label = items.get_item_attribute_value(2, "label", GLib.VariantType.new("s")).get_string()
        target = items.get_item_attribute_value(2, "target", None).unpack()
        self.assertEqual(label, "WH-1000XM5")
        self.assertEqual(target, "bluez_output.AC_80_0A_12_34_56.1")
        first = items.get_item_attribute_value(0, "label", GLib.VariantType.new("s")).get_string()
        self.assertEqual(first, "内蔵オーディオ アナログステレオ")
        bar.output.popdown()

    def test_output_menu_explains_missing_pactl(self):
        bar = self.bar()
        with self.fake.lock:
            self.fake._cmd_device = lambda req, now: {
                "ok": False, "error": 'list devices: pactl: exec: "pactl": executable file not found in $PATH'}
        bar.output.set_visible(True)
        bar.output.popup()
        items = bar._device_items
        self.assertTrue(run_loop(lambda: items.get_n_items() == 1 and "pactl" in items.get_item_attribute_value(
            0, "label", GLib.VariantType.new("s")).get_string(), 3.0))
        bar.output.popdown()


FAIL_TRACK = Track(path="https://www.youtube.com/watch?v=__fail__age", title="年齢確認の曲", artist="誰か",
                   album="盤", duration=200, stream=True) if HAVE_GI else None
OK_TRACK = Track(path="https://www.youtube.com/watch?v=okokokokok1", title="ふつうの曲", artist="誰か",
                 album="盤", duration=200, stream=True) if HAVE_GI else None
SIGN_IN = "YouTube のサインインが必要な曲です (cliamp の設定で Cookie を使う)"


class PlaybackProblemTest(PlayerTestBase):
    """再生できなかった曲: 再生バー・フルスクリーン・ミニプレーヤーの副題とトースト。"""

    def fail(self):
        self.store.replace([FAIL_TRACK, OK_TRACK], 0)
        self.assertTrue(run_loop(lambda: self.store.status.playback_problem is not None, 5.0))

    def recover(self):
        self.store.next()
        self.assertTrue(run_loop(lambda: self.store.status.state == "playing"
                                 and self.store.status.track.title == "ふつうの曲", 5.0))

    def with_css(self, scheme=None):
        """アプリの CSS を足す (外観は scheme に固定。既定はダーク)。足した provider を返す。"""
        from cliamp_music.app import load_css

        errors: list[str] = []
        display = Gdk.Display.get_default()
        providers = []
        for provider, _name in load_css(errors, scheme=scheme or Gtk.InterfaceColorScheme.DARK):
            Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
            self.addCleanup(Gtk.StyleContext.remove_provider_for_display, display, provider)
            providers.append(provider)
        self.assertEqual(errors, [])
        return providers

    def test_bar_shows_the_reason_in_a_warning_tone(self):
        from cliamp_music.playerbar import PlayerBar

        providers = self.with_css()
        bar = PlayerBar(self.ctx)
        self.host(bar, 900, 80)
        self.fail()
        self.assertTrue(run_loop(lambda: bar.subtitle_label.get_text() == SIGN_IN, 3.0),
                        bar.subtitle_label.get_text())
        self.assertTrue(bar.subtitle_label.has_css_class("problem"))
        self.assertFalse(bar.subtitle_label.has_css_class("music-key-text"))  # 赤 (操作の色) にしない
        self.assertTrue(bar.problem_icon.get_visible())
        # ツールチップは短文 (手当てまで) と cliamp の誤りの全文
        tip = bar.subtitle_row.get_tooltip_text()
        self.assertTrue(tip.startswith(SIGN_IN + "\n\nyt-dlp: ERROR: [youtube]"), tip)
        self.assertIn("Sign in to confirm your age", tip)
        self.assertEqual(bar.title_label.get_text(), "年齢確認の曲")
        # 色は赤 (操作の色 #fa586a) ではなく琥珀色 (#ffb340)
        run_loop(lambda: False, 0.1)
        color = bar.subtitle_label.get_color()
        self.assertGreater(color.red, 0.9)
        self.assertTrue(0.6 < color.green < 0.8, color.green)
        self.assertLess(color.blue, 0.35)
        # ライトでは白の上で読める暗い琥珀 (#b35c00。赤 #e0223b でもない)
        for provider in providers:
            provider.set_property("prefers-color-scheme", Gtk.InterfaceColorScheme.LIGHT)
        run_loop(lambda: False, 0.1)
        color = bar.subtitle_label.get_color()
        self.assertTrue(0.6 < color.red < 0.8, color.red)
        self.assertTrue(0.3 < color.green < 0.45, color.green)
        self.assertLess(color.blue, 0.1)
        self.recover()
        self.assertTrue(run_loop(lambda: bar.subtitle_label.get_text() == "誰か — 盤", 3.0))
        self.assertFalse(bar.subtitle_label.has_css_class("problem"))
        self.assertFalse(bar.problem_icon.get_visible())
        self.assertEqual(bar.subtitle_row.get_tooltip_text(), "誰か — 盤")

    def test_toast_once_per_new_failure(self):
        self.fail()
        self.assertTrue(run_loop(lambda: len(self.window.toasts) == 1, 3.0), self.window.toasts)
        # トーストは幅が限られるので、手当ての括弧書きを除いた見出し
        self.assertEqual(self.window.toasts[0], "「年齢確認の曲」を再生できません — YouTube のサインインが必要な曲です")
        run_loop(lambda: False, 1.0)  # 何度問い合わせても重ねない
        self.assertEqual(len(self.window.toasts), 1)
        self.store.play()  # やり直してまた失敗すれば、もう 1 度
        self.assertTrue(run_loop(lambda: len(self.window.toasts) == 2, 3.0), self.window.toasts)

    def test_fullscreen_and_miniplayer_show_it_under_the_title(self):
        from cliamp_music.miniplayer import MiniPlayer

        fs = fs_mod.FullscreenPlayer(self.ctx)
        self.host(fs)
        fs.set_active(True)
        mini = MiniPlayer(self.ctx)
        mini.present()
        self.windows.append(mini)
        self.fail()
        self.assertTrue(run_loop(lambda: fs.subtitle_label.get_text() == SIGN_IN
                                 and mini.square_subtitle.get_text() == SIGN_IN, 3.0))
        self.assertEqual(fs.title_label.get_text(), "年齢確認の曲")
        for label in (fs.subtitle_label, mini.square_subtitle, mini.compact_subtitle):
            self.assertTrue(label.has_css_class("problem"))
            self.assertTrue(label.get_tooltip_text().startswith(SIGN_IN))
            self.assertIn("Sign in to confirm your age", label.get_tooltip_text())
        self.assertEqual(mini.compact_subtitle.get_text(), SIGN_IN)
        # 幅の狭い列でも手当てまで読めるよう 2 行まで折り返す (横長のミニは 1 行)
        self.assertTrue(fs.subtitle_label.get_wrap() and mini.square_subtitle.get_wrap())
        self.assertFalse(mini.compact_subtitle.get_wrap())
        self.recover()
        self.assertTrue(run_loop(lambda: fs.subtitle_label.get_text() == "誰か — 盤", 3.0))
        for label in (fs.subtitle_label, mini.square_subtitle, mini.compact_subtitle):
            self.assertFalse(label.has_css_class("problem"))
            self.assertIsNone(label.get_tooltip_text())
            self.assertFalse(label.get_wrap())


class ProblemLabelTest(unittest.TestCase):
    """_show_problem: 折り返す副題が、空白の無い長い理由 (URL など) で列を押し広げない。"""

    @unittest.skipUnless(HAVE_DISPLAY, "画面が無い")
    def test_unbreakable_reason_keeps_the_minimum_width_small(self):
        from gi.repository import Pango

        short = "x" * 79 + "…"  # 空白の無い語 (知らない誤りの短文は 80 字まで)
        label = fs_mod._label("", "music-fs-subtitle")
        label.set_text(short)
        fs_mod._show_problem(label, (short, short + "\n2 行目"))
        self.assertTrue(label.get_wrap())
        minimum = label.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        self.assertLess(minimum, 120, "空白の無い理由の幅がラベルの最小幅になっている")
        # 語の途中で折っても "-" を足さない (URL の一部に見える)
        attrs = label.get_attributes()
        self.assertIsNotNone(attrs)
        self.assertTrue(any(a.klass.type == Pango.AttrType.INSERT_HYPHENS for a in attrs.get_attributes()))
        # 理由が消えれば元に戻す
        fs_mod._show_problem(label, None)
        self.assertFalse(label.get_wrap())
        self.assertIsNone(label.get_attributes())


class PanelsTest(PlayerTestBase):
    def queue_panel(self):
        from cliamp_music.panels import QueuePanel

        panel = QueuePanel(self.ctx)
        self.host(panel, 300, 700)
        panel.set_active(True)
        self.assertTrue(run_loop(lambda: panel.row_count > 0, 5.0))
        return panel

    def test_queue_and_continuation_are_separate_sections(self):
        panel = self.queue_panel()
        self.assertFalse(panel.queue_header.get_visible())
        self.assertEqual(panel.next_header.title_label.get_text(), "次に再生")
        self.store.queue_edit("add", index=9)
        self.assertTrue(run_loop(lambda: panel.queue_header.get_visible() and len(panel.queue_list.rows()) == 1, 3.0))
        self.assertEqual(panel.queue_list.rows()[0].index, 9)
        self.assertEqual(panel.next_header.title_label.get_text(), "ドライブ")
        self.assertEqual(panel.next_subtitle.get_text(), "このあと続けて再生されます")
        self.assertTrue(run_loop(lambda: 9 not in self.store.playlist.up_next, 3.0))
        self.assertTrue(run_loop(lambda: [r.index for r in panel.next_list.rows()] == self.store.playlist.up_next, 3.0))
        self.assertNotIn(9, [r.index for r in panel.next_list.rows()])
        self.store.queue_edit("clear")
        self.assertTrue(run_loop(lambda: not panel.queue_header.get_visible(), 3.0))
        self.assertFalse(panel.clear_button.get_visible())
        self.assertEqual(panel.next_header.title_label.get_text(), "次に再生")

    def test_appends_keep_rows_and_selection(self):
        panel = self.queue_panel()
        self.store.set_repeat("off")
        self.assertTrue(run_loop(lambda: self.store.playlist.up_next == list(range(3, 12)), 3.0))
        self.assertTrue(run_loop(lambda: [r.index for r in panel.next_list.rows()] == list(range(3, 12)), 3.0))
        rows = panel.next_list.rows()
        panel.next_list.select_row(rows[3])
        selected = rows[3]
        extra = [Track(path=f"https://www.youtube.com/watch?v=appended{i:03d}", title=f"足した {i}") for i in range(5)]
        self.store.enqueue(extra, "end")
        self.assertTrue(run_loop(lambda: len(panel.next_list.rows()) == len(rows) + 5, 3.0))
        self.assertEqual([id(r) for r in panel.next_list.rows()[: len(rows)]], [id(r) for r in rows])
        self.assertIs(panel.next_list.get_selected_row(), selected)

    def test_reshuffle_note(self):
        panel = self.queue_panel()
        self.store.set_shuffle(True)
        self.store.set_repeat("all")
        self.assertTrue(run_loop(lambda: self.fake.pl.shuffle and self.fake.pl.repeat == "all", 3.0))
        self.store.play_index(self.fake.pl.order[-1])
        self.assertTrue(run_loop(lambda: not self.store.playlist.up_next and not self.store.playlist.queue
                                 and self.store.playlist.index == self.fake.pl.order[-1], 5.0))
        self.assertTrue(run_loop(lambda: panel.next_empty.get_visible(), 3.0))
        self.assertEqual(panel.next_empty.get_text(), "このあとシャッフルし直して続けて再生します")
        self.store.set_repeat("off")
        self.assertTrue(run_loop(lambda: panel.next_empty.get_text() == "次に再生する曲はありません", 3.0))

    def test_queued_row_uses_play_queued(self):
        panel = self.queue_panel()
        for index in (5, 7):
            self.store.queue_edit("add", index=index)
            self.assertTrue(run_loop(lambda index=index: index in self.store.playlist.queue, 3.0))
        self.assertTrue(run_loop(lambda: len(panel.queue_list.rows()) == 2, 3.0))
        panel.queue_list.emit("row-activated", panel.queue_list.rows()[1])
        self.assertTrue(run_loop(lambda: self.fake.pl.index() == 7 and not self.fake.pl.queue, 5.0))
        self.assertEqual(self.fake.requests_for("play_index"), [])
        self.assertTrue(run_loop(lambda: not panel.queue_header.get_visible(), 3.0))

    def test_lyrics_scroller_shows_no_scrollbar(self):
        from cliamp_music.panels import LyricsPanel

        panel = LyricsPanel(self.ctx)
        self.host(panel, 300, 600)
        self.assertEqual(panel.scroller.get_policy()[1], Gtk.PolicyType.EXTERNAL)
        panel.set_active(True)
        self.play(0, at=44.0)
        self.assertTrue(run_loop(lambda: panel._current >= 1, 5.0))
        run_loop(lambda: False, 0.6)
        self.assertFalse(panel.scroller.get_vscrollbar().get_mapped())


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

    def test_space_toggles_playback(self):
        window = self.open_eq()
        keys = _capture_keys(window)
        self.assertIsNotNone(keys, "イコライザに Space の処理がありません")
        before = len(self.fake.requests_for("toggle"))
        self.assertTrue(keys.emit("key-pressed", Gdk.KEY_space, 0, Gdk.ModifierType(0)))
        self.assertTrue(run_loop(lambda: len(self.fake.requests_for("toggle")) == before + 1, 3.0))

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
        self.assertEqual(self.window.fullscreen_calls, [])

    def test_panel_button_leaves_fullscreen_player(self):
        """フルスクリーンプレーヤーの下のパネルを開いても見えないので、先に出る。"""
        from cliamp_music.miniplayer import _show_main

        self.window.fullscreen_shown = True
        _show_main(self.ctx, "lyrics")
        self.assertEqual(self.window.fullscreen_calls, [False])
        self.assertEqual(self.window.panels, ["lyrics"])
        self.assertEqual(self.window.presented, 1)
        _show_main(self.ctx)  # パネルなし (メインの窓を出すだけ) では出ない
        self.assertEqual(self.window.fullscreen_calls, [False])

    def test_space_toggles_and_hidden_band_takes_no_focus(self):
        from cliamp_music.miniplayer import MiniPlayer

        mini = MiniPlayer(self.ctx)
        mini.set_mode("square")
        mini.present()
        self.windows.append(mini)
        self.assertTrue(run_loop(lambda: mini.get_mapped(), 5.0))
        run_loop(lambda: False, 0.2)
        focus = mini.get_focus()
        for band in (mini._square_controls, mini._square_top, mini._square_close):
            self.assertFalse(band.get_can_focus())
            self.assertFalse(focus is not None and (focus is band or focus.is_ancestor(band)),
                             "見えない操作の帯にフォーカスがあります")
        keys = _capture_keys(mini)
        self.assertIsNotNone(keys, "ミニプレーヤーに Space の処理がありません")
        before = len(self.fake.requests_for("toggle"))
        mode = mini.mode
        for _ in range(2):
            self.assertTrue(keys.emit("key-pressed", Gdk.KEY_space, 0, Gdk.ModifierType(0)))
        self.assertTrue(run_loop(lambda: len(self.fake.requests_for("toggle")) == before + 2, 3.0))
        self.assertFalse(mini.square_more.get_active())
        self.assertEqual(mini.mode, mode)
        mini.set_hover(True, force=True)
        self.assertTrue(mini._square_controls.get_can_focus())
        mini.set_hover(False)
        self.assertFalse(mini._square_controls.get_can_focus())


def _capture_keys(window):
    controllers = window.observe_controllers()
    for i in range(controllers.get_n_items()):
        controller = controllers.get_item(i)
        if isinstance(controller, Gtk.EventControllerKey) and \
                controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE:
            return controller
    return None


if __name__ == "__main__":
    unittest.main()
