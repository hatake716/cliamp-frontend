"""端から端までの煙試験: 本物のアプリ (MusicApp) を偽の cliamp に繋いで一通り動かす。

すべてのページを開き、右パネル・フルスクリーン・ミニプレーヤー・イコライザを開け閉めし、
ショートカットのアクション、未接続からの復帰、拡張の無い cliamp (api 0) を通す。
その間に GLib のログ (Gtk-WARNING / Gtk-CRITICAL / Adwaita-WARNING / マークアップの
失敗など) や Python の例外が 1 つでも出たら失敗。GLib のログは
`GLib.log_set_writer_func` で受け取る (1 つのプロセスで 1 度しか置けないので、
置いた後は受け取った警告を標準エラーへそのまま書き戻す)。

X の画面 (Xvfb などの :10 以上の DISPLAY) が無ければ飛ばす。例:

    nix develop path:. -c xvfb-run -n 97 dbus-run-session -- python3 -m unittest tests.test_ui_smoke -v

利用者の cliamp のソケット・状態・キャッシュには触れない (HOME・XDG_*_HOME を一時
ディレクトリにし、ソケットは一時的なもの)。ネットワークには出ない。
"""

from __future__ import annotations

import ctypes
import io
import os
import shutil
import sys
import tempfile
import time
import traceback
import unittest
import urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_cliamp import FakeCliamp, isolate_display, temp_socket_path  # noqa: E402

isolate_display()

HAVE_DISPLAY = False
try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gdk, GLib, Gtk

    if os.environ.get("DISPLAY") and Gtk.init_check():
        HAVE_DISPLAY = True
except (ImportError, ValueError):  # pragma: no cover
    pass

SKIP = "X の画面 (Xvfb の :10 以上の DISPLAY) がありません"
APP_ID = "org.nixos.Music.SmokeTest"

# 失敗とみなす標準エラーの行 (Python の例外と、アプリ自身の失敗の記録)
STDERR_FAILURES = ("Traceback (most recent call last)", "を作れません", "操作に失敗", "CSS の誤り",
                   "Failed to set text", "from markup")

# DESIGN.md §6 のショートカット → アクション
SHORTCUTS = {
    "<Control>Right": "app.next", "<Control>Left": "app.previous",
    "<Shift><Control>Right": "app.seek-forward", "<Shift><Control>Left": "app.seek-backward",
    "<Control>Up": "app.volume-up", "<Control>Down": "app.volume-down",
    "<Control>period": "app.stop", "<Control>l": "app.reveal-current", "<Control>f": "app.search",
    "<Control><Alt>u": "app.show-queue", "<Shift><Control>l": "app.show-lyrics",
    "<Shift><Control>f": "app.fullscreen-player", "<Shift><Control>m": "app.miniplayer",
    "<Control><Alt>e": "app.equalizer", "<Control>r": "app.refresh", "<Control>0": "app.main-window",
    "<Control>w": "window.close", "<Control>q": "app.quit",
}


# --------------------------------------------------------------------------
# GLib のログを受け取る


class _LogCapture:
    """GLib の構造化ログを受け取る。capturing の間は警告以上を records に残す。"""

    installed = False
    capturing = False
    records: list[tuple[str, str, str]] = []  # (レベル, 領域, 文)

    LEVELS = {
        GLib.LogLevelFlags.LEVEL_ERROR: "ERROR", GLib.LogLevelFlags.LEVEL_CRITICAL: "CRITICAL",
        GLib.LogLevelFlags.LEVEL_WARNING: "WARNING", GLib.LogLevelFlags.LEVEL_MESSAGE: "Message",
        GLib.LogLevelFlags.LEVEL_INFO: "INFO", GLib.LogLevelFlags.LEVEL_DEBUG: "DEBUG",
    } if HAVE_DISPLAY else {}

    @classmethod
    def install(cls) -> type:
        """書き手を置く (1 つのプロセスで 1 度だけ。2 度目は GLib が abort する)。

        この試験のモジュールが別の名前でもう一度読まれても (写しを動かすなど) 同じものを
        使うよう、置いたものは sys に覚えておく。受け取り先のクラスを返す。"""
        shared = getattr(sys, "_cliamp_music_log_capture", None)
        if shared is not None:
            return shared
        GLib.log_set_writer_func(cls._writer, None)
        cls.installed = True
        sys._cliamp_music_log_capture = cls
        return cls

    @staticmethod
    def _field(field) -> str:
        value = field.value
        if isinstance(value, int):
            try:
                raw = ctypes.string_at(value) if field.length < 0 else ctypes.string_at(value, field.length)
            except Exception:  # 読めない欄は捨てる
                return ""
            return raw.decode("utf-8", "replace")
        return value if isinstance(value, str) else ""

    @classmethod
    def _writer(cls, level, fields, _n_fields, _user_data=None):
        values = {field.key: cls._field(field) for field in fields}
        name = next((text for flag, text in cls.LEVELS.items() if level & flag), "LOG")
        domain = values.get("GLIB_DOMAIN", "")
        message = values.get("MESSAGE", "")
        serious = name in ("ERROR", "CRITICAL", "WARNING")
        if serious and cls.capturing:
            cls.records.append((name, domain, message))
        if serious or name == "Message" or os.environ.get("G_MESSAGES_DEBUG"):
            # 置く前と同じく標準エラーへ書く (ほかの試験の出力を変えない)
            sys.__stderr__.write(f"({Path(sys.argv[0]).name}:{os.getpid()}): {domain}-{name} **: {message}\n")
        return GLib.LogWriterOutput.HANDLED


class _Tee(io.TextIOBase):
    """標準エラーに書かれた文を覚えつつ、そのまま流す。"""

    def __init__(self, target):
        self.target = target
        self.text = io.StringIO()

    def write(self, text):
        self.text.write(text)
        return self.target.write(text)

    def flush(self):
        self.target.flush()


# --------------------------------------------------------------------------


@unittest.skipUnless(HAVE_DISPLAY, SKIP)
class AppSmokeTest(unittest.TestCase):
    """本物のアプリを一通り動かし、警告が 1 つも出ないことを確かめる。"""

    def setUp(self):
        from cliamp_music import artwork, radio
        from cliamp_music.client import CliampClient
        from cliamp_music.protocol import Track

        self.tmp = Path(tempfile.mkdtemp(prefix="cm-smoke-"))
        self.saved_env = {k: os.environ.get(k) for k in (
            "HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "CLIAMP_MUSIC_APP_ID",
            "CLIAMP_MUSIC_NON_UNIQUE", "CLIAMP_MUSIC_START_COMMAND", "CLIAMP_MUSIC_SOCKET")}
        for sub in ("home", "state", "cache", "config"):
            (self.tmp / sub).mkdir()
        os.environ.update({
            "HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
            "CLIAMP_MUSIC_APP_ID": APP_ID, "CLIAMP_MUSIC_NON_UNIQUE": "1",
            "CLIAMP_MUSIC_START_COMMAND": "false",
        })
        os.environ.pop("CLIAMP_MUSIC_SOCKET", None)
        self.sock = temp_socket_path()
        self.fake = FakeCliamp(self.sock).start()

        # ネットワークに出ない (Radio Browser は手元の局、アートワークの取得は失敗させる)
        self.saved = {
            "artwork.urlopen": artwork.urlopen, "radio.urlopen": radio.urlopen,
            "top": radio.RadioBrowser.top, "by_country": radio.RadioBrowser.by_country,
            "search": radio.RadioBrowser.search,
            "backoff": (CliampClient.BACKOFF_MIN, CliampClient.BACKOFF_MAX),
        }

        def no_network(*_args, **_kwargs):
            raise OSError("試験ではネットワークに出ません")

        def no_artwork(request, *_args, **_kwargs):
            # 絵の無い曲は YouTube のサムネイルを探しに行く。無いもの (404) として答える
            url = getattr(request, "full_url", str(request))
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        artwork.urlopen = no_artwork
        radio.urlopen = no_network
        stations = [Track(path=f"https://radio.example.jp/{i}.mp3", title=name, stream=True, live=True,
                          meta=(("radio.country", "Japan"),))
                    for i, name in enumerate(("夜の & <ジャズ> FM", "Lo-fi 24/7", "港町ラジオ"))]

        def later(callback, value):
            GLib.timeout_add(30, lambda: (callback(value), GLib.SOURCE_REMOVE)[1])

        radio.RadioBrowser.top = lambda _self, cb, limit=40: later(cb, stations[:limit])
        radio.RadioBrowser.by_country = lambda _self, _code, cb, limit=40: later(cb, stations[:limit])
        radio.RadioBrowser.search = lambda _self, _q, cb, limit=60: later(cb, stations[:1])
        # 繋ぎ直しを速く
        CliampClient.BACKOFF_MIN, CliampClient.BACKOFF_MAX = 0.2, 0.4

        self.log = _LogCapture.install()
        self.log.records = []
        self.log.capturing = True
        self.tee = _Tee(sys.stderr)
        self.saved_stderr = sys.stderr
        sys.stderr = self.tee
        self.saved_excepthook = sys.excepthook
        self.exceptions: list[str] = []

        def excepthook(kind, value, tb):
            self.exceptions.append("".join(traceback.format_exception(kind, value, tb)))
            self.saved_excepthook(kind, value, tb)

        sys.excepthook = excepthook

    def tearDown(self):
        from cliamp_music import artwork, radio
        from cliamp_music.client import CliampClient

        self.log.capturing = False
        sys.stderr = self.saved_stderr
        sys.excepthook = self.saved_excepthook
        artwork.urlopen = self.saved["artwork.urlopen"]
        radio.urlopen = self.saved["radio.urlopen"]
        radio.RadioBrowser.top = self.saved["top"]
        radio.RadioBrowser.by_country = self.saved["by_country"]
        radio.RadioBrowser.search = self.saved["search"]
        CliampClient.BACKOFF_MIN, CliampClient.BACKOFF_MAX = self.saved["backoff"]
        try:
            self.fake.stop()
        except Exception:
            pass
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(os.path.dirname(self.sock), ignore_errors=True)

    # --- 道具 ---------------------------------------------------------------

    def run_app(self, script) -> list[str]:
        """アプリを動かし、script (generator) を GLib のタイマーで進める。失敗の一覧を返す。

        script は秒 (float) を yield すると待ち、(述語, 秒, 説明) を yield すると述語が
        真になるまで待つ (待ちきれなければ失敗に数える)。"""
        from cliamp_music.app import MusicApp

        problems: list[str] = []
        app = MusicApp(socket_path=self.sock, initial_page="home")
        steps = script(app, problems)
        started = time.monotonic()

        def advance() -> bool:
            if time.monotonic() - started > 120:
                problems.append("120 秒で終わりませんでした")
                app.quit_app()
                return GLib.SOURCE_REMOVE
            try:
                item = next(steps)
            except StopIteration:
                app.quit_app()
                return GLib.SOURCE_REMOVE
            except Exception:
                problems.append(traceback.format_exc())
                app.quit_app()
                return GLib.SOURCE_REMOVE
            if isinstance(item, tuple):
                predicate, timeout, label = item
                deadline = time.monotonic() + timeout

                def poll() -> bool:
                    try:
                        ok = bool(predicate())
                    except Exception as exc:
                        problems.append(f"{label}: {exc}")
                        ok = True
                    if ok or time.monotonic() > deadline:
                        if not ok:
                            problems.append(f"{label} ({timeout} 秒待っても整わない)")
                        GLib.idle_add(advance)
                        return GLib.SOURCE_REMOVE
                    return GLib.SOURCE_CONTINUE

                GLib.timeout_add(30, poll)
            else:
                GLib.timeout_add(int(float(item) * 1000), advance)
            return GLib.SOURCE_REMOVE

        def begin() -> bool:
            if app.window is None:
                return GLib.SOURCE_CONTINUE
            GLib.idle_add(advance)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(50, begin)
        code = app.run(["cliamp-music"])
        if code != 0:
            problems.append(f"終了コード {code}")
        problems.extend(f"CSS: {message}" for message in app.css_errors)
        return problems

    # --- 試験 ---------------------------------------------------------------

    def test_whole_app(self):
        fake = self.fake
        sock = self.sock

        def script(app, problems):
            def check(condition, message):
                if not condition:
                    problems.append(message)

            def win():
                return app.window

            def store():
                return app.ctx.store

            def page_id():
                page = win().current_page()
                return getattr(page, "page_id", "") if page is not None else ""

            def requests(cmd):
                with fake.lock:
                    return len(fake.requests_for(cmd))

            def descendants(widget):
                child = widget.get_first_child() if widget is not None else None
                while child is not None:
                    yield child
                    yield from descendants(child)
                    child = child.get_next_sibling()

            def texts(widget):
                return "\n".join(w.get_text() for w in descendants(widget)
                                 if isinstance(w, Gtk.Label) and w.get_mapped())

            def tracked_kinds():
                from cliamp_music import widgets as W
                from cliamp_music.pages.home import HomePage
                from cliamp_music.pages.playlists import PlaylistsPage
                from cliamp_music.pages.radio import RadioPage
                from cliamp_music.pages.search import SearchPage

                return (HomePage, SearchPage, RadioPage, PlaylistsPage, W.MediaCard, W.TallCard,
                        W.StationTile, W.CategoryTile, W.Shelf)

            def live_objects() -> list:
                import gc

                for _ in range(3):
                    gc.collect()
                kinds = tracked_kinds()
                return [obj for obj in gc.get_objects() if isinstance(obj, kinds)]

            # 同じプロセスのほかの試験が持っている物は数えない (GObject の番地で見分ける)
            baseline = {hash(obj) for obj in live_objects()}

            def leftovers():
                """最近再生した項目を表示している間に残っていてはいけない物の数。"""
                counts: dict[str, int] = {}
                for obj in live_objects():
                    if hash(obj) not in baseline:
                        counts[type(obj).__name__] = counts.get(type(obj).__name__, 0) + 1
                return counts

            def key_controller(window):
                controllers = window.observe_controllers()
                for i in range(controllers.get_n_items()):
                    controller = controllers.get_item(i)
                    if isinstance(controller, Gtk.EventControllerKey) and \
                            controller.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE:
                        return controller
                return None

            yield (lambda: win() is not None and win().get_mapped(), 10, "窓が出ない")
            yield (lambda: store().connected and bool(store().playlist.tracks) and bool(store().history), 10,
                   "偽の cliamp に繋がらない")
            window = win()
            check(abs(app.ctx.client._poll_interval - 0.4) < 1e-6, "見えている窓で状態を取る間隔が 0.4 秒でない")
            for accel, action in SHORTCUTS.items():
                check(action in app.get_actions_for_accel(accel), f"{accel} が {action} になっていない")

            # すべてのページ (サイドバーの項目) を開く
            from cliamp_music.pages import SIDEBAR_PAGES

            for pid in SIDEBAR_PAGES:
                window.navigate(pid)
                yield (lambda pid=pid: page_id() == pid, 5, f"{pid} が開かない")
                check(type(window.current_page()).__name__ != "CliampMusicUnavailablePage"
                      and "UnavailablePage" not in type(window.current_page()).__name__,
                      f"{pid} のページを作れなかった")
                check(window.sidebar.selected_key == pid, f"{pid} でサイドバーの選択が合わない")
                yield 0.5
            # 2 巡目を回した後、離れたページとその中のカード・行が解放されている
            for pid in SIDEBAR_PAGES:
                window.navigate(pid)
                yield (lambda pid=pid: page_id() == pid, 5, f"{pid} が 2 度目に開かない")
                yield 0.3
            window.navigate("recent")
            yield (lambda: page_id() == "recent", 5, "最近再生した項目に戻らない")
            yield 0.6
            check(leftovers() == {}, f"離れたページの部品が解放されていない: {leftovers()}")
            # プレイリストの詳細と戻る
            window.navigate("playlists")
            yield (lambda: page_id() == "playlists", 5, "すべてのプレイリストが開かない")
            window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
            yield (lambda: page_id() == "playlist", 5, "プレイリストの詳細が開かない")
            yield 0.6
            check(window.sidebar.selected_key == "playlist:local:ドライブ", "プレイリストの行が選ばれない")
            window.current_page().activate_action("navigation.pop", None)
            yield (lambda: page_id() == "playlists", 5, "戻るで戻らない")
            # 戻る動きの途中で同じプレイリストを開き直す (退場中のページと名札が重なる)
            window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
            yield (lambda: page_id() == "playlist", 5, "プレイリストの詳細が 2 度目に開かない")
            yield 0.6
            window.current_page().activate_action("navigation.pop", None)
            window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
            yield (lambda: page_id() == "playlist", 5, "戻ってすぐ同じプレイリストを開き直せない")
            yield 0.6
            # 積んだ詳細と同じ行をサイドバーで押す: そのページが根になる
            window._on_sidebar_select("playlist", {"provider": "local", "id": "ドライブ", "name": "ドライブ"})
            yield 0.4
            stack = window.nav.get_navigation_stack()
            check(page_id() == "playlist" and stack.get_n_items() == 1
                  and not window.current_page().back_button.get_visible(),
                  "サイドバーで開いたプレイリストが根にならない")
            window.navigate("playlists")
            yield (lambda: page_id() == "playlists", 5, "すべてのプレイリストに戻らない")

            # 右パネル (アクションと再生バーのボタンが合うか)
            bar = window.player_bar
            app.activate_action("show-lyrics", None)
            yield (lambda: window.panel == "lyrics", 3, "歌詞のパネルが開かない")
            check(bar.lyrics_button.get_active() and not bar.queue_button.get_active(), "歌詞のボタンが合わない")
            yield 0.6
            app.activate_action("show-queue", None)
            yield (lambda: window.panel == "queue", 3, "次に再生のパネルが開かない")
            check(bar.queue_button.get_active() and not bar.lyrics_button.get_active(), "次に再生のボタンが合わない")
            yield 0.6
            app.activate_action("show-queue", None)
            yield (lambda: window.panel == "", 3, "次に再生のパネルが閉じない")

            # Ctrl+L / Ctrl+F
            app.activate_action("reveal-current", None)
            yield (lambda: page_id() == "nowplaying", 3, "Ctrl+L で再生中のリストが開かない")
            app.activate_action("search", None)
            yield (lambda: page_id() == "search" and isinstance(window.get_focus(), (Gtk.Text, Gtk.Entry)), 3,
                   "Ctrl+F で検索欄に入らない")
            # 文字の入力中は Space を入力欄に渡す (再生/一時停止にしない)
            keys = key_controller(window)
            check(keys is not None, "窓のキー処理が見つからない")
            before = requests("toggle")
            handled = keys.emit("key-pressed", Gdk.KEY_space, 0, Gdk.ModifierType(0))
            check(not handled and requests("toggle") == before, "検索欄の Space が再生/一時停止に使われた")
            check(not app.lookup_action("next").get_enabled(), "入力中に Ctrl+→ (次へ) が入力欄に譲られない")
            window.set_focus(None)
            yield 0.2
            check(app.lookup_action("next").get_enabled(), "入力欄を離れても Ctrl+→ (次へ) が戻らない")
            handled = keys.emit("key-pressed", Gdk.KEY_space, 0, Gdk.ModifierType(0))
            yield (lambda: requests("toggle") > before, 3, "Space で再生/一時停止にならない")
            check(handled, "Space を窓が受け取らない")

            # 再生の操作のアクション
            for action, cmd in (("next", "next"), ("previous", "prev"), ("seek-forward", "seek_to"),
                                ("seek-backward", "seek_to"), ("volume-up", "volume"),
                                ("volume-down", "volume")):
                count = requests(cmd)
                app.activate_action(action, None)
                yield (lambda cmd=cmd, count=count: requests(cmd) > count, 3, f"app.{action} で {cmd} が届かない")
                yield 0.3

            # フルスクリーン (Esc で戻る)
            app.activate_action("fullscreen-player", None)
            yield (lambda: window.fullscreen_shown, 3, "フルスクリーンにならない")
            yield 0.8
            window._fullscreen_player.set_mode("queue")
            yield 0.5
            # 次に再生を何度か作り直し、外した行が (二重の後始末で落ちずに) 解放されるか
            tracks = list(store().playlist.tracks)
            for picks in ((tracks[5], tracks[7]), (tracks[1],)):
                store().enqueue(list(picks), "next")
                yield (lambda: len(store().playlist.queue) >= len(picks), 3, "待ち行列に入らない")
                yield 0.4
                store().queue_edit("clear")
                yield (lambda: not store().playlist.queue, 3, "待ち行列が空にならない")
                yield 0.4
            import gc

            for _ in range(3):
                gc.collect()
            window._fullscreen_player.set_mode("lyrics")
            keys.emit("key-pressed", Gdk.KEY_Escape, 0, Gdk.ModifierType(0))
            yield (lambda: not window.fullscreen_shown, 3, "Esc でフルスクリーンから戻らない")

            # ミニプレーヤーとイコライザ
            app.activate_action("miniplayer", None)
            yield (lambda: app.miniplayer is not None and app.miniplayer.get_mapped(), 5, "ミニプレーヤーが開かない")
            yield 0.6
            app.miniplayer.set_mode("compact")
            yield 0.4
            app.miniplayer.set_mode("square")
            app.activate_action("miniplayer", None)
            yield (lambda: not app.miniplayer.get_visible(), 3, "ミニプレーヤーが閉じない")
            app.activate_action("equalizer", None)
            yield (lambda: app.equalizer is not None and app.equalizer.get_mapped(), 5, "イコライザが開かない")
            check(app.equalizer.get_transient_for() is window, "イコライザがメインの窓に従属していない")
            yield 0.6
            app.equalizer.close()
            app.activate_action("refresh", None)
            yield 0.4

            # 狭い幅 (サイドバーを畳む)
            window.set_default_size(760, 700)
            yield (lambda: window.split.get_collapsed() and window.sidebar_collapsed, 3, "760 でサイドバーが畳まれない")
            yield 0.4
            window.set_default_size(1180, 760)
            yield (lambda: not window.split.get_collapsed(), 3, "広げてもサイドバーが戻らない")

            # 未接続と復帰
            fake.stop()
            yield (lambda: not store().connected and window.offline.get_visible(), 8, "未接続の表示にならない")
            check(not app.lookup_action("next").get_enabled(), "未接続なのに「次へ」が使える")
            yield 0.4
            fake.start()
            yield (lambda: store().connected and not window.offline.get_visible(), 10, "繋ぎ直せない")
            yield (lambda: bool(store().playlist.tracks), 5, "繋ぎ直した後にリストが戻らない")

            # 拡張の無い cliamp (api 0)
            fake.stop()
            yield (lambda: not store().connected, 8, "切れない")
            legacy = FakeCliamp(sock, legacy=True).start()
            try:
                yield (lambda: store().connected and store().api == 0 and window.banner.get_revealed(), 10,
                       "拡張なしの帯が出ない")
                check(not store().playlist.tracks and not store().history, "前の cliamp のリストが残っている")
                for pid in ("home", "search", "radio", "recent", "nowplaying", "playlists"):
                    window.navigate(pid)
                    yield (lambda pid=pid: page_id() == pid, 3, f"拡張なしで {pid} が開かない")
                    yield 0.5
                    check("拡張 IPC" in texts(window.current_page()), f"拡張なしの {pid} に説明が出ない")
                window.set_focus(None)
            finally:
                legacy.stop()
            fake.start()
            yield (lambda: store().connected and store().api == 1 and not window.banner.get_revealed(), 10,
                   "拡張のある cliamp に戻らない")
            window.navigate("home")
            yield (lambda: page_id() == "home" and window.current_page().recent.get_visible(), 8,
                   "拡張のある cliamp に戻ってもホームが戻らない")
            yield 0.4

        problems = self.run_app(script)
        output = self.tee.text.getvalue()
        bad_lines = [line for line in output.splitlines() if any(p in line for p in STDERR_FAILURES)]
        logged = [f"{domain}-{level}: {message}" for level, domain, message in self.log.records]
        self.assertEqual(problems, [], "\n".join(problems))
        self.assertEqual(logged, [], "GLib の警告:\n" + "\n".join(logged))
        self.assertEqual(self.exceptions, [], "\n".join(self.exceptions))
        self.assertEqual(bad_lines, [], "標準エラー:\n" + "\n".join(bad_lines))
        # 窓の大きさなどが保存された
        state = Path(os.environ["XDG_STATE_HOME"]) / "cliamp-music" / "state.json"
        self.assertTrue(state.exists(), "state.json が保存されていない")


if __name__ == "__main__":
    unittest.main()
