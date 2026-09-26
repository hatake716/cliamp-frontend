"""MusicApp (Adw.Application): CSS とアイコン、アクションとショートカット、--self-check。

    cliamp-music [--socket PATH] [--page ID] [--self-check]

- --socket PATH: cliamp の IPC ソケット (無ければ環境変数 CLIAMP_MUSIC_SOCKET、
  それも無ければ ~/.config/cliamp/cliamp.sock)。
- --page ID: 最初に開くページ (撮影・試験用。ページ ID は pages/__init__.py)。
- --self-check: 窓を出さずに、全モジュールの読み込み・4 つの CSS の解析・
  icons/ のすべての絵の読み込み・gdk-pixbuf の読み込み口 (png / jpeg / svg / webp) を
  確かめ、標準エラーに "self-check: ok" か "self-check: <理由>" を書く (画面が無くても動く)。

環境変数 (撮影・試験用): CLIAMP_MUSIC_APP_ID (アプリ ID を差し替える)、
CLIAMP_MUSIC_NON_UNIQUE=1 (NON_UNIQUE にして既に動いているアプリと繋がない)、
CLIAMP_MUSIC_START_COMMAND (「cliamp を起動」で走らせるコマンド)。

アクション (app.*) とショートカット (DESIGN.md §6、Command → Ctrl):
  Space 再生/一時停止 (窓のキー処理。文字の入力中は除く)
  play-pause, next <Ctrl>→, previous <Ctrl>←, seek-forward <Shift><Ctrl>→,
  seek-backward <Shift><Ctrl>←, volume-up <Ctrl>↑, volume-down <Ctrl>↓, stop <Ctrl>.,
  reveal-current <Ctrl>L, search <Ctrl>F, show-queue <Ctrl><Alt>U,
  show-lyrics <Shift><Ctrl>L, fullscreen-player <Shift><Ctrl>F, miniplayer <Shift><Ctrl>M,
  equalizer <Ctrl><Alt>E, refresh <Ctrl>R, main-window <Ctrl>0, quit <Ctrl>Q,
  window.close <Ctrl>W
Ctrl+矢印の 4 つと Ctrl+. (停止) は、前にある窓の文字の入力欄にフォーカスがある間は
止めて入力欄に譲る (アプリのショートカットは入力欄より先に働くため。Ctrl+. は GtkText の
絵文字の選択)。判断は「いま前にある (active な) 窓」のフォーカスで行う。
Space はどの窓でも再生/一時停止 (widgets.space_toggles。入力中とメニューの中は除く)。
"""

from __future__ import annotations

import argparse
import importlib
import os
import pkgutil
import sys
import traceback

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, HERE, VERSION, log  # noqa: E402

CSS_FILES = ("base", "shell", "pages", "player")
POLL_VISIBLE = 0.4
POLL_HIDDEN = 1.5
SEEK_STEP = 10.0
VOLUME_STEP = 2.0

# (アクション名, ショートカット, 説明)
SHORTCUTS = (
    ("play-pause", [], "再生/一時停止"),
    ("next", ["<Control>Right"], "次へ"),
    ("previous", ["<Control>Left"], "前へ"),
    ("seek-forward", ["<Shift><Control>Right"], "10 秒進む"),
    ("seek-backward", ["<Shift><Control>Left"], "10 秒戻る"),
    ("volume-up", ["<Control>Up"], "音量を上げる"),
    ("volume-down", ["<Control>Down"], "音量を下げる"),
    ("stop", ["<Control>period"], "停止"),
    ("reveal-current", ["<Control>l"], "再生中の曲をリストで表示"),
    ("search", ["<Control>f"], "検索"),
    ("show-queue", ["<Control><Alt>u"], "次に再生"),
    ("show-lyrics", ["<Shift><Control>l"], "歌詞"),
    ("fullscreen-player", ["<Shift><Control>f"], "フルスクリーンプレーヤー"),
    ("miniplayer", ["<Shift><Control>m"], "ミニプレーヤー"),
    ("equalizer", ["<Control><Alt>e"], "イコライザ"),
    ("refresh", ["<Control>r"], "ページを更新"),
    ("main-window", ["<Control>0"], "メインの窓"),
    ("quit", ["<Control>q"], "終了"),
)
# 文字の入力中は止めるもの (入力欄の Ctrl+矢印 = 単語単位の移動、Ctrl+. = 絵文字の選択と重なる)
TEXT_CONFLICTS = ("next", "previous", "seek-forward", "seek-backward", "stop")
# cliamp に繋がっていないと意味の無いもの
NEEDS_CONNECTION = ("play-pause", "next", "previous", "seek-forward", "seek-backward",
                    "volume-up", "volume-down", "stop")
# 窓が隠れている (最小化・覆われている・別のワークスペース) とみなす状態。Wayland では
# xdg-shell に最小化の状態が無く、mutter は見えない窓に SUSPENDED を送る。X11 は MINIMIZED
HIDDEN_STATES = Gdk.ToplevelState.MINIMIZED | Gdk.ToplevelState.SUSPENDED


def state_hidden(state: Gdk.ToplevelState) -> bool:
    """窓の面の状態から、隠れているか。"""
    return bool(state & HIDDEN_STATES)


def text_input_focused(windows) -> bool:
    """前にある (is_active な) 窓のフォーカスが文字の入力欄か。前にある窓が無ければ False
    (アプリのショートカットは前にある窓でしか働かないので、止める理由も無い)。"""
    from .widgets import is_text_input

    active = next((w for w in windows if w.is_active()), None)
    return active is not None and is_text_input(active.get_focus())


# ---------------------------------------------------------------------------
# アプリ


class MusicApp(Adw.Application):
    """ミュージック。`MusicApp(socket_path=None, initial_page=None)`。

    属性: ctx (AppContext。startup で作る)、window (メインの窓。activate で作る)。
    `show_miniplayer()` / `show_equalizer()`: 別の窓を必要になったときに作って出す。
    """

    __gtype_name__ = "CliampMusicApp"

    def __init__(self, socket_path: str | None = None, initial_page: str | None = None):
        app_id = os.environ.get("CLIAMP_MUSIC_APP_ID") or APP_ID
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        if os.environ.get("CLIAMP_MUSIC_NON_UNIQUE") == "1":
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=app_id, flags=flags)
        self.socket_path = socket_path
        self.initial_page = initial_page
        self.ctx = None
        self.window = None
        self.miniplayer = None
        self.equalizer = None
        self.css_errors: list[str] = []
        self._text_input_active = False
        self._main_shown_once = False
        self._quitting = False

    # --- 起動 --------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(HERE, "icons"))
            for provider, _name in load_css(self.css_errors):
                Gtk.StyleContext.add_provider_for_display(
                    display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

        from .client import CliampClient
        from .context import AppContext

        client = CliampClient(self.socket_path)
        self.ctx = AppContext(self, client=client)
        self.ctx.store.connect("connection-changed", MusicApp._on_connection, self)
        self._install_actions()
        self.connect("window-added", MusicApp._on_window_added)
        self.connect("window-removed", MusicApp._on_window_removed)
        client.start()

    def do_activate(self) -> None:
        window = self._ensure_window()
        window.present()
        self._main_shown_once = True
        self._update_poll_interval()

    def do_shutdown(self) -> None:
        if self.window is not None:
            try:
                self.window.save_state()
            except Exception as exc:  # 保存の失敗で終了を止めない
                log(f"窓の状態を保存できません: {exc}")
        if self.ctx is not None:
            self.ctx.state.save()
            self.ctx.client.stop()
        Adw.Application.do_shutdown(self)

    def _ensure_window(self):
        if self.window is None:
            from .window import MusicWindow

            self.window = MusicWindow(self, self.ctx, initial_page=self.initial_page)
            self.window.connect("destroy", MusicApp._on_main_destroyed, self)
            self._update_actions()
        return self.window

    @staticmethod
    def _on_main_destroyed(window, self: "MusicApp") -> None:
        if self.window is window:
            self.window = None
            if self.ctx is not None and self.ctx.window is window:
                self.ctx.window = None

    def present_main(self) -> None:
        window = self._ensure_window()
        window.set_visible(True)
        window.present()

    # --- 窓の見え方 → 状態を取る間隔 --------------------------------------------------

    @staticmethod
    def _on_window_added(self, window) -> None:
        if getattr(window, "_music_watched", False):
            # 隠している間はアプリから外して付け直す窓がある (ミニプレーヤー)。繋ぐのは 1 度だけ
            self._update_poll_interval()
            return
        window._music_watched = True
        window.connect("notify::visible", MusicApp._on_window_visibility, self)
        window.connect("realize", MusicApp._on_window_realized, self)
        # 文字の入力中かは、前にある窓のフォーカスで決める (どの窓が前に来ても見直す)
        window.connect("notify::is-active", MusicApp._on_window_focus_state, self)
        window.connect("notify::focus-widget", MusicApp._on_window_focus_state, self)
        if window.get_realized():
            MusicApp._on_window_realized(window, self)
        self._update_poll_interval()

    @staticmethod
    def _on_window_removed(self, _window) -> None:
        self._update_poll_interval()
        self._quit_if_nothing_visible()

    @staticmethod
    def _on_window_realized(window, self: "MusicApp") -> None:
        surface = window.get_surface()
        if surface is not None and isinstance(surface, Gdk.Toplevel):
            surface.connect("notify::state", MusicApp._on_surface_state, self)
        self._update_poll_interval()

    @staticmethod
    def _on_surface_state(_surface, _pspec, self: "MusicApp") -> None:
        self._update_poll_interval()

    @staticmethod
    def _on_window_focus_state(_window, _pspec, self: "MusicApp") -> None:
        self.refresh_text_input()

    @staticmethod
    def _on_window_visibility(_window, _pspec, self: "MusicApp") -> None:
        self._update_poll_interval()
        self._quit_if_nothing_visible()

    def _window_shown(self, window: Gtk.Window) -> bool:
        if not window.get_visible():
            return False
        surface = window.get_surface()
        if surface is not None and isinstance(surface, Gdk.Toplevel):
            if state_hidden(surface.get_state()):
                return False
        return True

    def _update_poll_interval(self) -> None:
        if self.ctx is None:
            return
        visible = any(self._window_shown(w) for w in self.get_windows())
        self.ctx.client.set_poll_interval(POLL_VISIBLE if visible else POLL_HIDDEN)

    def _quit_if_nothing_visible(self) -> None:
        # ミニプレーヤーを閉じたときに本体が隠れたままなら終わる (見えない窓で居座らない)
        if not self._main_shown_once or self._quitting:
            return
        if any(w.get_visible() for w in self.get_windows()):
            return
        GLib.idle_add(self._quit_idle)

    def _quit_idle(self) -> bool:
        if not any(w.get_visible() for w in self.get_windows()):
            self.quit_app()
        return GLib.SOURCE_REMOVE

    def quit_app(self) -> None:
        self._quitting = True
        if self.window is not None:
            self.window.save_state()
        for window in list(self.get_windows()):
            window.destroy()
        self.quit()

    # --- アクション ------------------------------------------------------------------

    def _install_actions(self) -> None:
        handlers = {
            "play-pause": lambda: self.ctx.store.toggle(),
            "next": lambda: self.ctx.store.next(),
            "previous": lambda: self.ctx.store.prev(),
            "seek-forward": lambda: self._seek(SEEK_STEP),
            "seek-backward": lambda: self._seek(-SEEK_STEP),
            "volume-up": lambda: self.ctx.store.volume_step(VOLUME_STEP),
            "volume-down": lambda: self.ctx.store.volume_step(-VOLUME_STEP),
            "stop": lambda: self.ctx.store.stop(),
            "reveal-current": self.reveal_current,
            "search": self.focus_search,
            "show-queue": lambda: self._toggle_panel("queue"),
            "show-lyrics": lambda: self._toggle_panel("lyrics"),
            "fullscreen-player": self.toggle_fullscreen_player,
            "miniplayer": self.toggle_miniplayer,
            "equalizer": self.show_equalizer,
            "refresh": self.refresh,
            "main-window": self.present_main,
            "quit": self.quit_app,
        }
        for name, accels, _title in SHORTCUTS:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", MusicApp._on_action, handlers[name])
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<Control>w"])
        self._update_actions()

    @staticmethod
    def _on_action(_action, _param, handler) -> None:
        try:
            handler()
        except Exception as exc:  # 1 つの操作の失敗でアプリを止めない
            log(f"操作に失敗: {exc}")
            traceback.print_exc()

    @staticmethod
    def _on_connection(_store, self: "MusicApp") -> None:
        self._update_actions()

    def set_text_input_active(self, active: bool) -> None:
        """文字の入力中か。入力中は Ctrl+矢印と Ctrl+. のアクションを止める。"""
        active = bool(active)
        if active != self._text_input_active:
            self._text_input_active = active
            self._update_actions()

    def refresh_text_input(self) -> None:
        """前にある窓のフォーカスから、文字の入力中かを決め直す。"""
        self.set_text_input_active(text_input_focused(self.get_windows()))

    def _update_actions(self) -> None:
        if self.ctx is None:
            return
        store = self.ctx.store
        connected = store.connected
        for name in NEEDS_CONNECTION:
            action = self.lookup_action(name)
            if action is None:
                continue
            enabled = connected
            if name in TEXT_CONFLICTS and self._text_input_active:
                enabled = False
            if name.startswith("seek") and not store.supports("seek_to"):
                enabled = False
            action.set_enabled(enabled)
        equalizer = self.lookup_action("equalizer")
        if equalizer is not None:
            equalizer.set_enabled(connected and store.supports("eq"))

    def _seek(self, delta: float) -> None:
        if not self.ctx.store.can_seek():
            return
        self.ctx.store.seek_by(delta)

    def _toggle_panel(self, name: str) -> None:
        window = self._ensure_window()
        if not window.get_visible():
            window.set_visible(True)
        if window.fullscreen_shown:
            window.show_fullscreen(False)
        window.present()
        window.toggle_panel(name)

    def reveal_current(self) -> None:
        window = self._ensure_window()
        window.set_visible(True)
        window.present()
        window.reveal_current()

    def focus_search(self) -> None:
        window = self._ensure_window()
        window.set_visible(True)
        window.present()
        window.focus_search()

    def refresh(self) -> None:
        if self.window is not None:
            self.window.refresh()

    def toggle_fullscreen_player(self) -> None:
        window = self._ensure_window()
        window.set_visible(True)
        window.present()
        window.toggle_fullscreen()

    # --- 別の窓 ------------------------------------------------------------------

    def show_miniplayer(self):
        """ミニプレーヤーを出す (最初に呼ばれたときに作る)。作れなければトースト。"""
        if self.miniplayer is None:
            try:
                from .miniplayer import MiniPlayer

                player = MiniPlayer(self.ctx)
            except Exception as exc:
                log(f"ミニプレーヤーを作れません: {exc}")
                traceback.print_exc()
                self._toast("ミニプレーヤーを開けません")
                return None
            if player.get_application() is None:
                player.set_application(self)
            player.connect("destroy", MusicApp._on_miniplayer_destroyed, self)
            self.miniplayer = player
        self.miniplayer.set_visible(True)
        self.miniplayer.present()
        return self.miniplayer

    def toggle_miniplayer(self) -> None:
        player = self.miniplayer
        if player is not None and player.get_visible():
            player.close()
            self.present_main()
            return
        self.show_miniplayer()

    @staticmethod
    def _on_miniplayer_destroyed(player, self: "MusicApp") -> None:
        if self.miniplayer is player:
            self.miniplayer = None

    def show_equalizer(self):
        """イコライザの窓を出す (メインの窓に従属)。作れなければトースト。"""
        if self.equalizer is None:
            try:
                from .equalizer import EqualizerWindow

                window = EqualizerWindow(self.ctx)
            except Exception as exc:
                log(f"イコライザを作れません: {exc}")
                traceback.print_exc()
                self._toast("イコライザを開けません")
                return None
            if window.get_application() is None:
                window.set_application(self)
            window.connect("destroy", MusicApp._on_equalizer_destroyed, self)
            self.equalizer = window
        main = self.window
        if main is not None and main.get_visible() and self.equalizer.get_transient_for() is not main:
            self.equalizer.set_transient_for(main)
            # メインの窓を閉じたらイコライザも閉じる (見えない本体の下に残らない)
            self.equalizer.set_destroy_with_parent(True)
        self.equalizer.set_visible(True)
        self.equalizer.present()
        return self.equalizer

    @staticmethod
    def _on_equalizer_destroyed(window, self: "MusicApp") -> None:
        if self.equalizer is window:
            self.equalizer = None

    def _toast(self, text: str) -> None:
        if self.ctx is not None:
            self.ctx.toast(text)
        else:
            log(text)


# ---------------------------------------------------------------------------
# CSS と自己診断


def css_paths() -> list[tuple[str, str]]:
    """読む順の (名前, パス)。無いファイルも含めて返す。"""
    return [(name, os.path.join(HERE, "style", f"{name}.css")) for name in CSS_FILES]


def load_css(errors: list[str] | None = None) -> list[tuple[Gtk.CssProvider, str]]:
    """style/ の CSS を順に読む (無いものは飛ばしてログに書く)。解析の誤りは errors に足す。"""
    providers = []
    for name, path in css_paths():
        if not os.path.exists(path):
            log(f"{name}.css がありません (飛ばします)")
            continue
        provider = Gtk.CssProvider()

        def on_error(_provider, section, error, name=name):
            where = section.to_string() if section is not None else name
            message = f"{name}.css: {where}: {error.message}"
            log(f"CSS の誤り: {message}")
            if errors is not None:
                errors.append(message)

        provider.connect("parsing-error", on_error)
        provider.load_from_path(path)
        providers.append((provider, name))
    return providers


def _package_modules() -> list[str]:
    names = []
    for info in pkgutil.walk_packages([HERE], prefix="cliamp_music."):
        if info.name.split(".")[-1].startswith("_") and not info.ispkg:
            if info.name != "cliamp_music.__main__":
                continue
        names.append(info.name)
    return sorted(names)


REQUIRED_MODULES = (
    "cliamp_music.protocol", "cliamp_music.client", "cliamp_music.store", "cliamp_music.catalog",
    "cliamp_music.artwork", "cliamp_music.radio", "cliamp_music.state", "cliamp_music.context",
    "cliamp_music.widgets", "cliamp_music.window", "cliamp_music.sidebar", "cliamp_music.playerbar",
    "cliamp_music.panels", "cliamp_music.pages", "cliamp_music.app",
    "cliamp_music.fullscreen", "cliamp_music.miniplayer", "cliamp_music.equalizer",
    "cliamp_music.spotify_import", "cliamp_music.importer",
)


def self_check() -> int:
    """窓を出さずに部品を確かめる。結果は標準エラーに 1 行。"""
    problems: list[str] = []
    # 1. モジュール
    names = sorted(set(REQUIRED_MODULES) | set(_package_modules()))
    for name in names:
        if name == "cliamp_music.__main__":
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:
            problems.append(f"{name} を読めません ({type(exc).__name__}: {exc})")
    # ページのクラス
    try:
        from .pages import PAGE_CLASSES, page_class

        for page_id in PAGE_CLASSES:
            try:
                page_class(page_id)
            except Exception as exc:
                problems.append(f"ページ {page_id} のクラスを読めません ({type(exc).__name__}: {exc})")
    except Exception as exc:
        problems.append(f"pages を読めません ({exc})")
    # 2. CSS
    errors: list[str] = []
    loaded = load_css(errors)
    if not loaded:
        problems.append("CSS が 1 つも読めません")
    elif not any(name == "base" for _p, name in loaded):
        problems.append("base.css がありません")
    problems.extend(f"CSS の誤り: {message}" for message in errors)
    # 3. アイコン
    icons_dir = os.path.join(HERE, "icons")
    count = 0
    for root, _dirs, files in os.walk(icons_dir):
        for file in sorted(files):
            if not file.endswith((".svg", ".png")):
                continue
            path = os.path.join(root, file)
            count += 1
            error = _load_image(path)
            if error:
                problems.append(f"アイコン {file} を読めません ({error})")
    if count == 0:
        problems.append(f"{icons_dir} にアイコンがありません")
    # 4. 絵の読み込み口 (記号は SVG、Radio Browser の局の favicon には WebP のものがある)
    formats = _pixbuf_formats()
    for name in ("png", "jpeg", "svg", "webp"):
        if name not in formats:
            problems.append(f"gdk-pixbuf に {name} の読み込み口がありません (GDK_PIXBUF_MODULE_FILE を確かめる)")
    if problems:
        for problem in problems:
            print(f"self-check: {problem}", file=sys.stderr)
        return 1
    print(f"self-check: ok ({len(names)} モジュール、CSS {len(loaded)} 個、アイコン {count} 個)",
          file=sys.stderr)
    return 0


def _pixbuf_formats() -> set[str]:
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        return {fmt.get_name() for fmt in GdkPixbuf.Pixbuf.get_formats()}
    except Exception:
        return set()


def _load_image(path: str) -> str:
    try:
        texture = Gdk.Texture.new_from_filename(path)
        if texture.get_width() > 0 and texture.get_height() > 0:
            return ""
        error = "大きさが 0"
    except Exception as exc:
        error = str(exc)
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        if pixbuf.get_width() > 0:
            return ""
    except Exception as exc:
        error = f"{error}; {exc}"
    return error


# ---------------------------------------------------------------------------
# 入口


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="cliamp-music", description=f"{APP_NAME} — cliamp のフロントエンド")
    parser.add_argument("--socket", metavar="PATH", help="cliamp の IPC ソケット")
    parser.add_argument("--page", metavar="ID", help="最初に開くページ")
    parser.add_argument("--self-check", action="store_true", help="窓を出さずに部品を確かめる")
    parser.add_argument("--version", action="version", version=f"cliamp-music {VERSION}")
    return parser.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args, rest = parse_args(argv[1:])
    if args.self_check:
        return self_check()
    socket_path = args.socket or os.environ.get("CLIAMP_MUSIC_SOCKET") or None
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    app = MusicApp(socket_path=socket_path, initial_page=args.page)
    return app.run([argv[0] if argv else "cliamp-music"] + rest)


if __name__ == "__main__":
    sys.exit(main())
