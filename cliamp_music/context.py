"""AppContext: 部品の束と、ページから使う共通の操作。

ページや部品は ctx だけを受け取り、ここから store / catalog / artwork などを使う。
navigate と toast は窓 (self.window) に任せる。窓はダックタイピングで、
window.navigate(page_id, **params) と window.toast(text) があればよい
(試験では偽の窓を差す)。
"""

from __future__ import annotations

import weakref
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
    STALE,
    Response,
    Source,
    Track,
    mix_url,
    playback_error_headline,
    valid_playlist_name,
)
from .radio import RadioBrowser  # noqa: E402
from .spotify_import import ImportIndex, default_index_path  # noqa: E402
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
    シグナル "local-playlists-changed": ローカルのプレイリストの一覧か中身が変わった
    (新規プレイリストを作った・曲を外したときなど。サイドバーと詳細ページの取り直しに使う)。
    cliamp は最後の曲を外したプレイリストをファイルごと消すので、外した後は一覧を
    取り直してから知らせる (消えたプレイリストのページは自分で閉じる)。
    シグナル "providers-changed": 覚えているプロバイダーの鳴らし方 (ProviderInfo の playback) が
    変わった。Spotify の Web API だけの接続は、cliamp がセッションを作った後の providers の答えに
    しか playback が載らないので、カタログがカタログ系の初めての成功の後で取り直した答えで
    変わる (すべてのプレイリストの Spotify の節の書き添えなどに使う)。

    imports (spotify_import.ImportIndex) は Spotify から取り込んだローカルのプレイリストの記録と、橋渡しの
    path → Spotify の曲 ID の表 (state.json と同じディレクトリの spotify-imports.json)。カタログと store の
    track_hook に差し、ローカルの TOML や履歴で meta を失った取り込んだ曲に Spotify の曲 ID を付け直す
    (アートワーク・リンク・track_key が cliamp の Web API だけの Spotify の曲と同じに働く)。
    """

    __gtype_name__ = "CliampMusicAppContext"
    __gsignals__ = {
        "local-playlists-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "providers-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, app: Any = None, *, window: Any = None, client: CliampClient | None = None,
                 store: PlayerStore | None = None, catalog: Catalog | None = None,
                 artwork: ArtworkLoader | None = None, radio: RadioBrowser | None = None,
                 state: GuiState | None = None, imports: ImportIndex | None = None):
        super().__init__()
        self.app = app
        self.window = window
        self.client = client or (store.client if store is not None else CliampClient())
        self.store = store or PlayerStore(self.client)
        self.catalog = catalog or Catalog(self.client)
        self.artwork = artwork or ArtworkLoader()
        self.radio = radio or RadioBrowser()
        self.state = state or GuiState()
        # 取り込んだ Spotify のプレイリストの記録 (ファイルは初めて使うときに読む)
        self.imports = imports or ImportIndex(default_index_path(getattr(self.state, "path", None)))
        if hasattr(self.catalog, "track_hook"):
            self.catalog.track_hook = self.imports.restore_all
        if hasattr(self.store, "track_hook"):
            self.store.track_hook = self.imports.restore
        # 取り込み・更新の最中のローカルのプレイリスト名と、最後に出した取り込みの窓 (試験・撮影が見る)
        self.spotify_imports_busy: set[str] = set()
        self.spotify_import_dialog = None
        self._local_playlists: list[str] = []
        self._local_playlists_loaded = False
        self._playlist_menus: deque[Gio.Menu] = deque(maxlen=32)
        # providers の答えから覚えた表示名と鳴らし方 (pages.home の provider_label・provider_playback)
        self._provider_names: dict[str, str] = {}
        self._provider_playback: dict[str, str] = {}
        add_listener = getattr(self.catalog, "add_providers_listener", None)
        if add_listener is not None:
            # カタログは ctx が持つので、聞き手が ctx を強く持つと循環になる (弱く持つ)
            ref = weakref.WeakMethod(self.remember_providers)

            def on_providers(result, ref=ref) -> None:
                remember = ref()
                if remember is not None:
                    remember(result)

            add_listener(on_providers)
        self.store.connect("connection-changed", self._on_connection)
        self.store.connect("playback-failed", self._on_playback_failed)
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

    def _on_playback_failed(self, store: PlayerStore) -> None:
        """いまの曲が再生できなかった (新しい失敗ごとに 1 度)。理由の短文をトーストに出す
        (全文は再生バーの副題のツールチップ)。"""
        status = store.status
        problem = status.playback_problem
        if problem is None or status.track is None:
            return
        title = status.track.display_title
        if len(title) > 18:
            title = title[:17] + "…"
        self.toast(f"「{title}」を再生できません — {playback_error_headline(problem[0])}")

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

    # --- プロバイダー --------------------------------------------------------------------

    def remember_providers(self, providers) -> None:
        """providers の答えから表示名と鳴らし方 (playback) を覚える。鳴らし方が変われば
        "providers-changed"。表示名は足していき、鳴らし方は届いた答えで置き換える (サインインの
        具合で変わるので前の値を持ち越さない)。"""
        try:
            infos = list(providers)
            names = {info.key: info.name for info in infos if info.name}
            playback = {info.key: getattr(info, "playback", "") or "" for info in infos}
        except (TypeError, AttributeError):
            return
        self._provider_names = {**self._provider_names, **names}
        before = {key: value for key, value in (self._provider_playback or {}).items() if value}
        self._provider_playback = playback
        if {key: value for key, value in playback.items() if value} != before:
            self.emit("providers-changed")

    # --- ローカルのプレイリスト --------------------------------------------------------

    @property
    def local_playlists(self) -> list[str]:
        """覚えているローカルのプレイリスト名 (書き込めるものだけ)。"""
        return list(self._local_playlists)

    @property
    def local_playlists_loaded(self) -> bool:
        """local_playlists が今の cliamp から実際に取れた一覧か (最初の空や未接続ではない)。"""
        return self._local_playlists_loaded and self.store.connected

    def refresh_local_playlists(self, force: bool = True, then=None) -> None:
        """ローカルのプレイリストの一覧を取り直す。一覧が変われば "local-playlists-changed"。

        then(names) は取り終えたときに呼ぶ (names は取れた一覧、取れなければ None)。"""
        if not self.store.supports("playlists"):
            # 拡張の無い cliamp に繋ぎ直したときは、前の cliamp の一覧を残さない
            self._local_playlists_loaded = False
            if self._local_playlists:
                self._local_playlists = []
                for menu in list(self._playlist_menus):
                    self._fill_playlist_menu(menu)
                self.emit("local-playlists-changed")
            if then is not None:
                then(None)
            return

        def done(result) -> None:
            if isinstance(result, Response):
                if then is not None:
                    then(None)
                return
            names = [info.name for info in result if info.id != RECENTLY_PLAYED]
            self._local_playlists_loaded = True
            try:
                # TUI で消した・名前を変えた取り込みの記録を忘れる (取り込んだばかりのものは残す)
                self.imports.prune(names)
            except Exception as exc:  # 記録は飾り。失敗しても一覧は使う
                log(f"取り込みの記録を整理できません: {exc}")
            if names != self._local_playlists:
                self._local_playlists = names
                for menu in list(self._playlist_menus):
                    self._fill_playlist_menu(menu)
                self.emit("local-playlists-changed")
            if then is not None:
                then(names)

        self.catalog.playlists("local", done, force=force)

    def _on_connection(self, _store) -> None:
        self._local_playlists_loaded = False
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

    # --- Spotify から取り込む -----------------------------------------------------------

    def open_spotify_import(self, url: str = ""):
        """「Spotify から取り込む」の窓を出す (url を渡すとすぐ読む)。出した窓を返す。"""
        from .importer import open_dialog

        return open_dialog(self, url)

    def update_spotify_import(self, name: str, callback=None) -> bool:
        """取り込んだプレイリスト name を、取り込んだときのリンクから読み直して置き換える
        (「Spotify から更新」)。始めたら True。callback(できたか)。"""
        from .importer import update_import

        return update_import(self, name, callback)

    def import_record(self, name: str):
        """ローカルのプレイリスト name を Spotify から取り込んだ記録 (無ければ None)。"""
        try:
            return self.imports.get(name)
        except Exception as exc:  # 記録は飾り
            log(f"取り込みの記録を読めません: {exc}")
            return None

    def imported_record(self, name: str, tracks):
        """いまの曲 tracks から見て、ローカルのプレイリスト name が Spotify から取り込んだもののままなら、
        その記録 (無ければ None)。記録は名前だけで引くので、中身が別物になっていれば (TUI で消して同じ名前で
        作り直したなど) 記録を忘れる (ImportIndex.confirm)。取り込んだものとしての見せ方 (書き添え・
        「Spotify ·」・Spotify の絵・「Spotify から更新」) はこれで決める。"""
        try:
            return self.imports.confirm(name, tracks)
        except Exception as exc:  # 記録は飾り
            log(f"取り込みの記録を確かめられません: {exc}")
            return None

    def forget_import(self, name: str) -> None:
        """ローカルのプレイリスト name を消したので、取り込みの記録も忘れる。"""
        try:
            self.imports.forget(name)
        except Exception as exc:
            log(f"取り込みの記録を消せません: {exc}")

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

        # ラジオなど終わりの無い流れ (live) はプレイリストに入れない: ローカルのプレイリスト
        # (TOML) には live も局の絵も残らず、ふつうの曲として読み戻されるため
        # (「プレイリストとして保存」も live を除く)
        if not track.live:
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
            # Spotify の曲 (YouTube で探して鳴らす曲・取り込んだ曲も) は Spotify の頁を開く
            links.append("Spotify で開く" if url.startswith("https://open.spotify.com/") else "ブラウザで開く",
                         "track.open-browser")
            menu.append_section(None, links)
            action("copy-link", lambda _v: self.copy_text(url))
            action("open-browser", lambda _v: self.open_uri(url))

        removal = None
        if index is not None:
            if context == "nowplaying":
                removal = ("リストから削除", "remove", lambda _v: self.store.remove(
                    index, callback=self._toast_failure("削除できませんでした"), path=track.path),
                    supports("remove"))
            elif context.startswith("local:") and len(context) > len("local:"):
                playlist_name = context[len("local:"):]
                removal = ("プレイリストから削除", "remove-from-playlist",
                           lambda _v: self._remove_from_playlist(playlist_name, index, track),
                           supports("playlist_remove_track"))
            elif context == "queue":
                removal = ("待ち行列から外す", "dequeue", lambda _v: self.store.queue_edit(
                    "remove", index=index, callback=self._toast_failure("外せませんでした"), path=track.path),
                    supports("queue_edit"))
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

    def _remove_from_playlist(self, name: str, index: int, track: Track | None = None) -> None:
        """ローカルのプレイリストから index の曲を外す。

        cliamp は添字で外すので、先に今のファイルの中身を読み直し、その位置の曲が見ていた曲
        (track) と違えば (TUI で並べ替えた・消したなど) 同じ曲のいちばん近い位置を外す
        (見ていない曲を消さない)。最後の曲を外すと cliamp はプレイリストごと消すので、
        一覧を取り直してから知らせる。"""
        if track is None:
            self._remove_at(name, index)
            return

        def checked(result) -> None:
            if isinstance(result, Response):
                self.toast(f"削除できませんでした: {result.message}")
                return
            paths = [t.path for t in result]
            if 0 <= index < len(paths) and paths[index] == track.path:
                self._remove_at(name, index, track)
                return
            candidates = [i for i, path in enumerate(paths) if path == track.path]
            if not candidates:
                self.toast(f"「{name}」の中身が変わっていたので、削除しませんでした")
                self.emit("local-playlists-changed")
                return
            self._remove_at(name, min(candidates, key=lambda i: abs(i - index)), track)

        self.catalog.tracks("local", name, checked, force=True)

    def _remove_at(self, name: str, index: int, track: Track | None = None, retried: bool = False) -> None:
        def done(response: Response) -> None:
            if not response.ok and response.error == STALE and track is not None and not retried:
                # 読み直した後でまた動いた (TUI で編集中など)。もう 1 度だけ読み直して選び直す
                def again(result) -> None:
                    paths = [] if isinstance(result, Response) else [t.path for t in result]
                    candidates = [i for i, path in enumerate(paths) if path == track.path]
                    if not candidates:
                        self.toast(f"「{name}」の中身が変わっていたので、削除しませんでした")
                        self.emit("local-playlists-changed")
                        return
                    self._remove_at(name, min(candidates, key=lambda i: abs(i - index)), track, True)

                self.catalog.tracks("local", name, again, force=True)
                return
            if not response.ok:
                self.toast(f"削除できませんでした: {response.message}")
                return

            def listed(names) -> None:
                if names is not None and name not in names:
                    # 空になったのでファイルごと消えた (一覧が変わったので知らせは出ている)
                    self.toast(f"「{name}」は空になったので削除しました")
                    return
                self.toast(f"「{name}」から削除しました")
                self.emit("local-playlists-changed")  # 中身が変わった (一覧の名前は同じ)

            self.refresh_local_playlists(force=True, then=listed)

        self.catalog.playlist_remove_track(name, index, done, path=track.path if track is not None else None)
