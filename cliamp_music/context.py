"""AppContext: 部品の束と、ページから使う共通の操作。

ページや部品は ctx だけを受け取り、ここから store / catalog / artwork などを使う。
navigate と toast は窓 (self.window) に任せる。窓はダックタイピングで、
window.navigate(page_id, **params) と window.toast(text) があればよい
(試験では偽の窓を差す)。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from . import log  # noqa: E402
from .artwork import ArtworkLoader  # noqa: E402
from .catalog import Catalog  # noqa: E402
from .client import CliampClient  # noqa: E402
from .protocol import (  # noqa: E402
    RECENTLY_PLAYED,
    Response,
    Source,
    Track,
    mix_url,
    valid_playlist_name,
)
from .radio import RadioBrowser  # noqa: E402
from .state import GuiState  # noqa: E402
from .store import PlayerStore  # noqa: E402

UNSUPPORTED_TEXT = "この cliamp は拡張 IPC に対応していません"


def _title_of(track: Track) -> str:
    title = track.display_title
    return title if len(title) <= 40 else title[:39] + "…"


def _menu_label(text: str) -> str:
    # メニューの項目は "_" を下線付きの近道として読むので、名前の "_" は重ねる。
    return text.replace("_", "__")


class AppContext(GObject.Object):
    """アプリの部品 (app, window, client, store, catalog, artwork, radio, state) の束。

    省いた部品は既定の設定で作る。window は窓を作った後で代入する。
    シグナル "local-playlists-changed": ローカルのプレイリストの一覧が変わった
    (新規プレイリストを作ったときなど。サイドバーの取り直しに使う)。
    """

    __gtype_name__ = "CliampMusicAppContext"
    __gsignals__ = {
        "local-playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: Any = None, *, window: Any = None, client: CliampClient | None = None,
                 store: PlayerStore | None = None, catalog: Catalog | None = None,
                 artwork: ArtworkLoader | None = None, radio: RadioBrowser | None = None,
                 state: GuiState | None = None):
        super().__init__()
        self.app = app
        self.window = window
        self.client = client or (store.client if store is not None else CliampClient())
        self.store = store or PlayerStore(self.client)
        self.catalog = catalog or Catalog(self.client)
        self.artwork = artwork or ArtworkLoader()
        self.radio = radio or RadioBrowser()
        self.state = state or GuiState()
        self._local_playlists: list[str] = []
        self._playlist_menus: deque[Gio.Menu] = deque(maxlen=32)
        self.store.connect("connection-changed", self._on_connection)
        if self.store.connected:
            self.refresh_local_playlists()

    # --- 窓に任せるもの ----------------------------------------------------------------

    def navigate(self, page_id: str, **params) -> None:
        """サイドバーの項目なら根を差し替え、それ以外は積む (窓が決める)。"""
        window = self.window
        if window is not None and hasattr(window, "navigate"):
            window.navigate(page_id, **params)

    def toast(self, text: str):
        """窓に平文のトーストを出す (窓が無ければログへ)。窓が返したトーストを返す。"""
        window = self.window
        if window is not None and hasattr(window, "toast"):
            return window.toast(text)
        log(text)
        return None

    def _toast_failure(self, prefix: str):
        def done(response: Response) -> None:
            if not response.ok:
                self.toast(f"{prefix}: {response.message}")

        return done

    # --- 再生 ---------------------------------------------------------------------

    def play_tracks(self, tracks: Iterable[Track], index: int = 0, source: Source | dict | None = None) -> None:
        """曲の並びでリストを差し替え、index の曲から再生する。"""
        tracks = list(tracks)
        if not tracks:
            return
        if not self.store.supports("replace"):
            self.toast(UNSUPPORTED_TEXT)
            return
        index = min(max(0, int(index)), len(tracks) - 1)
        self.store.replace(tracks, index, source, callback=self._toast_failure("再生できませんでした"))

    def play_now(self, track: Track) -> None:
        """いまのリストの末尾に足してすぐ再生する。"""
        if not self.store.supports("enqueue"):
            self.toast(UNSUPPORTED_TEXT)
            return
        self.store.enqueue([track], mode="now", callback=self._toast_failure("再生できませんでした"))

    def load_provider(self, provider: str, id: str, index: int = 0, name: str = "") -> None:
        """プロバイダーのリストを cliamp に読み込ませて再生する。"""
        if not self.store.supports("load_provider"):
            self.toast(UNSUPPORTED_TEXT)
            return
        self.catalog.load(provider, id, index, name, callback=self._toast_failure("読み込めませんでした"))

    def start_station(self, track: Track) -> None:
        """YouTube の曲からミックス (ステーション) を読み込んで再生する。"""
        video = track.youtube_id
        if not video:
            self.toast("この曲からはステーションを作れません")
            return
        name = f"{track.artist or track.display_title} のステーション"
        self.toast(f"「{name}」を読み込んでいます…")
        self.load_provider("url", mix_url(video), 0, name)

    # --- ローカルのプレイリスト --------------------------------------------------------

    @property
    def local_playlists(self) -> list[str]:
        """覚えているローカルのプレイリスト名 (書き込めるものだけ)。"""
        return list(self._local_playlists)

    def refresh_local_playlists(self, force: bool = True) -> None:
        if not self.store.supports("playlists"):
            # 拡張の無い cliamp に繋ぎ直したときは、前の cliamp の一覧を残さない
            if self._local_playlists:
                self._local_playlists = []
                for menu in list(self._playlist_menus):
                    self._fill_playlist_menu(menu)
                self.emit("local-playlists-changed")
            return

        def done(result) -> None:
            if isinstance(result, Response):
                return
            names = [info.name for info in result if info.id != RECENTLY_PLAYED]
            if names != self._local_playlists:
                self._local_playlists = names
                for menu in list(self._playlist_menus):
                    self._fill_playlist_menu(menu)
                self.emit("local-playlists-changed")

        self.catalog.playlists("local", done, force=force)

    def _on_connection(self, _store) -> None:
        if self.store.connected:
            # 繋ぎ直した cliamp は別物かもしれない (再起動・拡張の有無・サインイン)。
            # 覚えているカタログの結果を捨ててから取り直す
            self.catalog.invalidate()
            self.refresh_local_playlists()

    def add_to_playlist(self, name: str, tracks: Iterable[Track]) -> None:
        tracks = list(tracks)
        if not tracks:
            return

        def done(response: Response) -> None:
            if response.ok:
                self.toast(f"「{name}」に追加しました")
                self.refresh_local_playlists()
            else:
                self.toast(f"プレイリストに追加できませんでした: {response.message}")

        self.catalog.playlist_add(name, tracks, done)

    def ask_new_playlist(self, tracks: Iterable[Track]) -> None:
        """名前を尋ねて新しいプレイリストを作り、曲を入れる。"""
        tracks = list(tracks)
        dialog = Adw.AlertDialog(heading="新規プレイリスト",
                                 body="プレイリストの名前を入力してください。")
        entry = Gtk.Entry()
        entry.set_placeholder_text("名前")
        entry.set_activates_default(True)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "キャンセル")
        dialog.add_response("create", "作成")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")
        dialog.set_response_enabled("create", False)
        entry.connect("changed", lambda e: dialog.set_response_enabled(
            "create", valid_playlist_name(e.get_text())))

        def on_response(_dialog, response: str) -> None:
            name = entry.get_text().strip()
            if response == "create" and valid_playlist_name(name):
                self.add_to_playlist(name, tracks)

        dialog.connect("response", on_response)
        parent = self.window if isinstance(self.window, Gtk.Widget) else None
        dialog.present(parent)

    # --- 窓の外とのやりとり ----------------------------------------------------------------

    def _clipboard(self):
        window = self.window
        if window is not None and hasattr(window, "get_clipboard"):
            return window.get_clipboard()
        display = Gdk.Display.get_default()
        return display.get_clipboard() if display is not None else None

    def copy_text(self, text: str, done_text: str | None = "リンクをコピーしました") -> None:
        clipboard = self._clipboard()
        if clipboard is None:
            self.toast("クリップボードを使えません")
            return
        clipboard.set(text)  # gdk_clipboard_set_value (文字列は GValue に包まれる)
        if done_text:
            self.toast(done_text)

    def open_uri(self, uri: str) -> None:
        """既定のブラウザなどで開く。"""
        launcher = Gtk.UriLauncher.new(uri)
        parent = self.window if isinstance(self.window, Gtk.Window) else None

        def finished(source, result) -> None:
            try:
                source.launch_finish(result)
            except GLib.Error as exc:
                if not exc.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.toast(f"開けませんでした: {exc.message}")

        launcher.launch(parent, None, finished)

    # --- 「…」メニュー -----------------------------------------------------------------

    def _fill_playlist_menu(self, menu: Gio.Menu) -> None:
        menu.remove_all()
        names = Gio.Menu()
        for name in self._local_playlists:
            item = Gio.MenuItem.new(_menu_label(name), None)
            item.set_action_and_target_value("track.add-to-playlist", GLib.Variant.new_string(name))
            names.append_item(item)
        if names.get_n_items():
            menu.append_section(None, names)
        extra = Gio.Menu()
        extra.append("新規プレイリスト…", "track.new-playlist")
        menu.append_section(None, extra)

    def track_menu(self, track: Track, *, index: int | None = None,
                   context: str = "") -> tuple[Gio.MenuModel, Gio.ActionGroup]:
        """曲の「…」メニュー。返した action group を行に "track" の名前で差し込んで使う。

        context: "nowplaying" (index = リスト上の添字) なら「リストから削除」、
        "local:<名前>" (index = プレイリスト内の添字) なら「プレイリストから削除」、
        "queue" (index = リスト上の添字) なら「待ち行列から外す」を足す。
        """
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()
        supports = self.store.supports

        def action(name: str, handler, enabled: bool = True, param: GLib.VariantType | None = None) -> None:
            act = Gio.SimpleAction.new(name, param)
            act.connect("activate", lambda _a, value: handler(value))
            act.set_enabled(enabled)
            group.add_action(act)

        title = _title_of(track)

        play = Gio.Menu()
        play.append("次に再生", "track.play-next")
        play.append("最後に再生", "track.play-last")
        menu.append_section(None, play)
        action("play-next", lambda _v: self.store.enqueue(
            [track], "next", callback=self._toast_done(f"「{title}」を次に再生します", "追加できませんでした")),
            supports("enqueue"))
        action("play-last", lambda _v: self.store.enqueue(
            [track], "end", callback=self._toast_done(f"「{title}」を最後に再生します", "追加できませんでした")),
            supports("enqueue"))

        playlists = Gio.Menu()
        self._fill_playlist_menu(playlists)
        self._playlist_menus.append(playlists)
        add = Gio.Menu()
        add.append_submenu("プレイリストに追加", playlists)
        menu.append_section(None, add)
        can_add = supports("playlist_add")
        action("add-to-playlist", lambda v: self.add_to_playlist(v.get_string(), [track]), can_add,
               GLib.VariantType.new("s"))
        action("new-playlist", lambda _v: self.ask_new_playlist([track]), can_add)
        if can_add:
            self.refresh_local_playlists(force=False)

        if track.youtube_id:
            station = Gio.Menu()
            station.append("ステーションを作成", "track.start-station")
            menu.append_section(None, station)
            action("start-station", lambda _v: self.start_station(track), supports("load_provider"))

        url = track.web_url
        if url:
            links = Gio.Menu()
            links.append("リンクをコピー", "track.copy-link")
            links.append("ブラウザで開く", "track.open-browser")
            menu.append_section(None, links)
            action("copy-link", lambda _v: self.copy_text(url))
            action("open-browser", lambda _v: self.open_uri(url))

        removal = None
        if index is not None:
            if context == "nowplaying":
                removal = ("リストから削除", "remove", lambda _v: self.store.remove(
                    index, callback=self._toast_failure("削除できませんでした")), supports("remove"))
            elif context.startswith("local:") and len(context) > len("local:"):
                playlist_name = context[len("local:"):]
                removal = ("プレイリストから削除", "remove-from-playlist",
                           lambda _v: self._remove_from_playlist(playlist_name, index),
                           supports("playlist_remove_track"))
            elif context == "queue":
                removal = ("待ち行列から外す", "dequeue", lambda _v: self.store.queue_edit(
                    "remove", index=index, callback=self._toast_failure("外せませんでした")), supports("queue_edit"))
        if removal is not None:
            label, action_name, handler, enabled = removal
            section = Gio.Menu()
            section.append(label, f"track.{action_name}")
            menu.append_section(None, section)
            action(action_name, handler, enabled)
        return menu, group

    def _toast_done(self, ok_text: str, fail_prefix: str):
        def done(response: Response) -> None:
            self.toast(ok_text if response.ok else f"{fail_prefix}: {response.message}")

        return done

    def _remove_from_playlist(self, name: str, index: int) -> None:
        def done(response: Response) -> None:
            if response.ok:
                self.toast(f"「{name}」から削除しました")
                self.emit("local-playlists-changed")
            else:
                self.toast(f"削除できませんでした: {response.message}")

        self.catalog.playlist_remove_track(name, index, done)
