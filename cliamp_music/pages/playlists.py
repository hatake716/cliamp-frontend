"""すべてのプレイリスト (PlaylistsPage) とプレイリストの詳細 (PlaylistDetailPage)。

PlaylistsPage: 大見出し「プレイリスト」。プロバイダーごとの節 (ローカルが先頭、
次に playlists を持つプロバイダー。radio は除く) に、プレイリストのカードの格子。
サインインの要るプロバイダーは節に小さな注意書きを出す。押すと詳細へ。

PlaylistDetailPage: Apple のアルバムの頁の形。左上に 250px の絵 (先頭 4 曲の 2x2、
無ければ代わりの絵)、題 (26px/700)、提供元 (26px/400、赤)、情報の行、ボタン
(シャッフルの丸 / 「▶ 再生」/ 「…」)。下に番号付きの行。ダブルクリックでその曲から
再生する: 見えている曲の並びをそのまま `replace` で送る (`load_provider` はプロバイダーに
取り直させて添字で選ぶので、その間にリストが変わっていると別の曲が鳴る)。replace の無い
cliamp と、とても長いリストだけ `load_provider`。ローカルのプレイリストは「…」から
削除でき (確認あり)、行の「…」に「プレイリストから削除」が出る。最後の曲を外すと cliamp は
プレイリストごと消すので、そのときはページを閉じる。
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from ..protocol import RECENTLY_PLAYED, PlaylistInfo, Response, Source, Track  # noqa: E402
from ..widgets import MediaCard, SectionHeader, TrackList, TrackRow, format_count  # noqa: E402
from .home import (  # noqa: E402
    DETAIL_LIST_SIDE,
    OFFLINE_TEXT,
    OFFLINE_TITLE,
    SIDE,
    UNSUPPORTED_TEXT,
    UNSUPPORTED_TITLE,
    AdaptiveGrid,
    ChunkedRows,
    ContentStack,
    DetailHeader,
    PageBase,
    _label,
    info_line,
    is_playing,
    provider_label,
    remember_provider_names,
    row_key,
    set_playlist_art,
    shuffle_provider,
    total_duration,
    weak_call,
    weak_handler,
)

CARD = 170
LOCAL = "local"
# これより長いリストは replace で送らず load_provider に任せる (要求 1 行は 8 MiB まで)
REPLACE_MAX_TRACKS = 10000


def auth_note(ctx, provider: str) -> str:
    return f"{provider_label(ctx, provider)} はサインインが必要です (cliamp の端末で設定)"


# --------------------------------------------------------------------------
# すべてのプレイリスト


class _ProviderSection(Gtk.Box):
    """1 つのプロバイダーの節 (見出し・注意書き・カードの格子)。"""

    def __init__(self, page: "PlaylistsPage", key: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("music-playlists-section")
        self.key = key
        self.keys: tuple | None = None
        self.header = SectionHeader(provider_label(page.ctx, key))
        page.inset(self.header)
        self.append(self.header)
        self.note = _label("", "music-page-note")
        self.note.set_wrap(True)
        page.inset(self.note)
        self.note.set_visible(False)
        self.append(self.note)
        self.groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.inset(self.groups)
        self.append(self.groups)

    def show_note(self, text: str) -> None:
        self.note.set_text(text)
        self.note.set_visible(bool(text))


class PlaylistsPage(PageBase):
    """すべてのプレイリストのページ。"""

    PAGE_ID = "playlists"
    TITLE = "プレイリスト"

    def __init__(self, ctx, **_params):
        super().__init__(ctx)
        self.add_css_class("music-playlists-page")
        self.add_large_title()
        self.state = ContentStack("読み込み中…")
        self.sections: dict[str, _ProviderSection] = {}
        self.body.append(self.state)
        self._serial = 0
        self.watch(ctx.store, "connection-changed", self._on_connection)
        if isinstance(ctx, GObject.Object):
            self.watch(ctx, "local-playlists-changed", self._on_local_changed)

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        if not store.connected:
            self.state.show_empty("music-radio-symbolic", OFFLINE_TITLE, OFFLINE_TEXT)
            return
        if not store.supports("providers") or not store.supports("playlists"):
            self.state.show_empty("music-grid-symbolic", UNSUPPORTED_TITLE, UNSUPPORTED_TEXT)
            return
        if not self.sections:
            self.state.show_loading()
        self._serial += 1
        serial = self._serial

        def done(result) -> None:
            if serial != self._serial:
                return
            if isinstance(result, Response):
                self.state.show_empty("music-grid-symbolic", "プレイリストを読めませんでした", result.message)
                return
            remember_provider_names(self.ctx, result)
            keys = [LOCAL] + [p.key for p in result
                              if p.playlists and not p.virtual and p.key not in (LOCAL, "radio")]
            self._arrange(keys)
            for key in keys:
                self._load_section(serial, key, force)
            self.state.show_content()

        self.ctx.catalog.providers(done, force=force)

    def _on_connection(self, *_args) -> None:
        if self.ctx.store.connected:
            self.load(force=False)

    def _on_local_changed(self, *_args) -> None:
        if LOCAL in self.sections:
            self._load_section(self._serial, LOCAL, False)

    def _arrange(self, keys: list[str]) -> None:
        """節をプロバイダーの並びに揃える (無くなったものは外す)。"""
        content = self.state.content
        for key in list(self.sections):
            if key not in keys:
                content.remove(self.sections.pop(key))
        previous = None
        for key in keys:
            section = self.sections.get(key)
            if section is None:
                section = _ProviderSection(self, key)
                self.sections[key] = section
                content.insert_child_after(section, previous)
            else:
                content.reorder_child_after(section, previous)
            section.header.set_title(provider_label(self.ctx, key))
            previous = section

    def _load_section(self, serial: int, key: str, force: bool) -> None:
        def done(result) -> None:
            if serial != self._serial:
                return
            section = self.sections.get(key)
            if section is None:
                return
            if isinstance(result, Response):
                section.keys = None
                self._clear_groups(section)
                section.show_note(auth_note(self.ctx, key) if result.needs_auth
                                  else f"読み込めませんでした: {result.message}")
                return
            infos = [info for info in result if not (key == LOCAL and info.id == RECENTLY_PLAYED)]
            if not infos:
                section.show_note("プレイリストはありません。曲の「…」から「プレイリストに追加」で作れます。"
                                  if key == LOCAL else "プレイリストはありません。")
            else:
                section.show_note("")
            self._fill(section, infos)

        self.ctx.catalog.playlists(key, done, force=force)

    @staticmethod
    def _clear_groups(section: _ProviderSection) -> None:
        child = section.groups.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            section.groups.remove(child)
            child = following

    def _fill(self, section: _ProviderSection, infos: list[PlaylistInfo]) -> None:
        keys = tuple((i.id, i.name, i.track_count, i.section) for i in infos)
        if keys == section.keys:
            return
        section.keys = keys
        self._clear_groups(section)
        if not infos:
            return
        grid = AdaptiveGrid(min_size=150, max_size=200)
        grid.add_css_class("music-playlists-grid")
        for info in infos:
            grid.append_card(self._card(info))
        section.groups.append(grid)

    def _card(self, info: PlaylistInfo) -> MediaCard:
        if info.provider == LOCAL:
            subtitle = format_count(info.track_count) if info.track_count else "プレイリスト"
        else:
            subtitle = format_count(info.track_count) if info.track_count else provider_label(self.ctx, info.provider)
        card = MediaCard(self.ctx.artwork, ("placeholder", f"{info.provider}:{info.id}", "playlist"),
                         info.name, subtitle, CARD, kind="playlist", on_activate=weak_handler(self._on_card))
        card.playlist_info = info
        if info.provider == LOCAL:
            self._fill_art(card, info)
        return card

    def _fill_art(self, card: MediaCard, info: PlaylistInfo) -> None:
        import weakref

        card_ref = weakref.ref(card)

        def done(result) -> None:
            target = card_ref()
            if target is None or isinstance(result, Response):
                return
            set_playlist_art(self.ctx, target.art, result, f"{info.provider}:{info.id}",
                             max(CARD, getattr(target, "art_height", CARD)))

        self.ctx.catalog.tracks(info.provider, info.id, done)

    def _on_card(self, card) -> None:
        info = getattr(card, "playlist_info", None)
        if info is not None:
            self.ctx.navigate("playlist", provider=info.provider, id=info.id, name=info.name)

    def refresh(self) -> None:
        self.ctx.catalog.invalidate()
        for section in self.sections.values():
            section.keys = None
        super().refresh()


# --------------------------------------------------------------------------
# プレイリストの詳細


class PlaylistDetailPage(PageBase):
    """プレイリストの詳細。属性 provider / playlist_id / playlist_name (窓がサイドバーの選択に使う)。"""

    PAGE_ID = "playlist"
    TITLE = "プレイリスト"

    def __init__(self, ctx, provider: str = "", id: str = "", name: str = "", **_params):
        playlist_id = id or name or ""
        playlist_name = name or id or ""
        super().__init__(ctx, title=playlist_name or self.TITLE)
        self.provider = provider or ""
        self.playlist_id = playlist_id
        self.playlist_name = playlist_name
        self.add_css_class("music-detail-page")
        self._tracks: list[Track] = []
        self._keys: tuple | None = None
        self._serial = 0
        self._dialog = None

        self.header = DetailHeader(ctx, on_shuffle=weak_call(self.shuffle), on_play=weak_call(self.play_first),
                                   menu_factory=weak_call(self._menu))
        self.header.set_title(self.playlist_name or self.TITLE)
        self.header.set_subtitle(provider_label(ctx, self.provider))
        self.header.set_info("")
        self.header.set_actions_sensitive(False)
        self.header.art.set_subject(ctx.artwork, ("placeholder", self._art_key(), "playlist"), kind="playlist")
        self.inset(self.header)
        self.body.append(self.header)

        self.state = ContentStack("読み込み中…")
        self.state.set_margin_top(26)
        self.list = TrackList()
        self.list.add_css_class("music-detail-list")
        self.list.set_margin_start(DETAIL_LIST_SIDE)
        self.list.set_margin_end(SIDE - 4)
        self.state.content.append(self.list)
        self.body.append(self.state)
        self.rows = ChunkedRows(self.list, weak_handler(self._make_row), on_progress=weak_handler(self._on_built))

        store = ctx.store
        self.watch(store, "track-changed", self._on_track)
        self.watch(store, "state-changed", self._on_track)
        self.watch(store, "connection-changed", self._on_connection)
        if self.editable and isinstance(ctx, GObject.Object):
            self.watch(ctx, "local-playlists-changed", self._on_local_changed)

    @property
    def is_local(self) -> bool:
        return self.provider == LOCAL

    @property
    def editable(self) -> bool:
        """書き換えられるローカルのプレイリストか (履歴の仮想プレイリストは除く)。"""
        return self.is_local and self.playlist_id != RECENTLY_PLAYED

    def _art_key(self) -> str:
        return f"{self.provider}:{self.playlist_id}"

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        if not store.connected:
            self._show_problem("music-radio-symbolic", OFFLINE_TITLE, OFFLINE_TEXT)
            return
        if not store.supports("tracks"):
            self._show_problem("music-note-list-symbolic", UNSUPPORTED_TITLE, UNSUPPORTED_TEXT)
            return
        if not self._tracks:
            self.state.show_loading()
        self._serial += 1
        serial = self._serial

        def done(result) -> None:
            if serial != self._serial:
                return
            if isinstance(result, Response):
                if result.needs_auth:
                    self._show_problem("music-note-list-symbolic", "サインインが必要です",
                                       auth_note(self.ctx, self.provider))
                elif self.is_local and "no such file or directory" in result.error:
                    # 空になって消えた・TUI で消した。ファイルの場所 (open /…/X.toml) は見せない
                    if not self._leave_if_gone():
                        self._show_problem("music-note-list-symbolic", "プレイリストが見つかりません",
                                           f"「{self.playlist_name}」は削除されたか、名前が変わりました。")
                else:
                    self._show_problem("music-note-list-symbolic", "プレイリストを読めませんでした", result.message)
                return
            self._show(result)

        self.ctx.catalog.tracks(self.provider, self.playlist_id, done, force=force)

    def on_resync(self) -> None:
        if self.is_local:
            # 他の画面や TUI で曲を足した・消した後。ローカルのファイルは安いので読み直す
            self.load(force=True)
        else:
            self._update_current()

    def _on_local_changed(self, *_args) -> None:
        if self._leave_if_gone():
            return
        self.load(force=False)

    def _leave_if_gone(self) -> bool:
        """このローカルのプレイリストが一覧から消えていれば (最後の曲を外した・削除した)
        ページを閉じる。一覧が実際に取れているときだけ判断する (最初の空の一覧や未接続では
        閉じない)。"""
        ctx = self.ctx
        store = ctx.store
        if not self.editable or not store.connected or not store.supports("playlists"):
            return False
        if not getattr(ctx, "local_playlists_loaded", False):
            return False
        if self.playlist_id in getattr(ctx, "local_playlists", ()):
            return False
        self._leave()
        return True

    def _on_connection(self, *_args) -> None:
        # 繋ぎ直した cliamp は別物かもしれない (カタログのキャッシュは ctx が捨てている)
        if self.ctx.store.connected:
            self.load(force=False)

    def _show_problem(self, icon: str, title: str, text: str) -> None:
        self._tracks = []
        self._keys = None
        self.rows.build([])
        self.header.set_info("")
        self.header.set_actions_sensitive(False)
        # ローカルは曲が読めなくても「…」から削除できる
        self.header.more_button.set_sensitive(self.editable)
        self.state.show_empty(icon, title, text)

    def _show(self, tracks: list[Track]) -> None:
        keys = tuple(row_key(t) for t in tracks)
        self.header.set_info(info_line(len(tracks), total_duration(tracks)))
        self.header.set_actions_sensitive(bool(tracks))
        self.header.more_button.set_sensitive(self.editable or bool(tracks))
        if keys != self._keys:
            self._keys = keys
            self._tracks = list(tracks)
            set_playlist_art(self.ctx, self.header.art, self._tracks, self._art_key(), DetailHeader.ART)
            # 曲を外した・足したときは差分だけ (スクロール位置と開いているメニューを失わない)
            self.rows.update(self._tracks)
        if not tracks:
            self.state.show_empty("music-note-list-symbolic", "曲がありません",
                                  "このプレイリストにはまだ曲がありません。")
        else:
            self.state.show_content()

    def on_narrow_changed(self, narrow: bool) -> None:
        self.header.set_narrow(narrow)

    # --- 行 ---------------------------------------------------------------------

    def _make_row(self, index: int, track: Track) -> TrackRow:
        context = f"local:{self.playlist_id}" if self.editable else ""
        return TrackRow(self.ctx, track, variant="album", index=index, number=index + 1,
                        menu_context=context, on_activate=weak_handler(self._on_row))

    def _on_built(self, _built: int) -> None:
        self._update_current()

    def _on_track(self, *_args) -> None:
        self._update_current()

    def _loaded_here(self) -> bool:
        """cliamp がいまこのプレイリストを読み込んでいるか。"""
        store = self.ctx.store
        source = store.status.source if store.status.source else store.playlist.source
        return bool(source) and source.provider == self.provider and source.id == self.playlist_id

    def _update_current(self) -> None:
        store = self.ctx.store
        index = None
        status = store.status
        if self._loaded_here() and status.track is not None:
            i = status.index
            if 0 <= i < len(self._tracks) and self._tracks[i].path == status.track.path:
                index = i
        self.list.set_current_index(index, is_playing(status))

    def _on_row(self, row) -> None:
        index = getattr(row, "index", None)
        if index is not None:
            self.play_from(index)

    def play_from(self, index: int) -> None:
        """index の曲から再生する。見えている並びをそのまま送る (replace)。

        load_provider はプロバイダーに取り直させて添字で選ぶので、見た後でリストが変わって
        いると (TUI での並べ替え・削除、いいねの追加など) 黙って別の曲が鳴る。"""
        tracks = self._tracks
        store = self.ctx.store
        if (tracks and 0 <= index < len(tracks) and store.supports("replace")
                and len(tracks) <= REPLACE_MAX_TRACKS):
            source = Source(provider=self.provider, id=self.playlist_id, name=self.playlist_name)
            self.ctx.play_tracks(tracks, index, source)
            return
        self.ctx.load_provider(self.provider, self.playlist_id, index, self.playlist_name)

    # --- ボタン -------------------------------------------------------------------

    @property
    def tracks(self) -> list[Track]:
        return list(self._tracks)

    def play_first(self) -> None:
        """先頭の曲から再生する。"""
        self.play_from(0)

    def shuffle(self) -> None:
        """シャッフルを入れて無作為な曲から再生する。"""
        shuffle_provider(self.ctx, self.provider, self.playlist_id, len(self._tracks), self.playlist_name)

    def _menu(self):
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()
        supports = self.ctx.store.supports
        playable = [t for t in self._tracks if not t.unplayable]
        queue = Gio.Menu()
        queue.append("次に再生", "page.play-next")
        queue.append("最後に再生", "page.play-last")
        menu.append_section(None, queue)
        for name, mode in (("play-next", "next"), ("play-last", "end")):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", weak_call(self.enqueue_all, mode))
            action.set_enabled(bool(playable) and supports("enqueue"))
            group.add_action(action)
        if self.editable:
            danger = Gio.Menu()
            danger.append("プレイリストを削除…", "page.delete")
            menu.append_section(None, danger)
            action = Gio.SimpleAction.new("delete", None)
            action.connect("activate", weak_call(self.confirm_delete))
            action.set_enabled(supports("playlist_delete"))
            group.add_action(action)
        return menu, group

    def enqueue_all(self, mode: str) -> None:
        tracks = [t for t in self._tracks if not t.unplayable]
        if not tracks:
            return
        name = self.playlist_name

        def done(response: Response) -> None:
            if response.ok:
                self.ctx.toast(f"「{name}」を{'次に' if mode == 'next' else '最後に'}再生します")
            else:
                self.ctx.toast(f"追加できませんでした: {response.message}")

        self.ctx.store.enqueue(tracks, mode, callback=done)

    # --- 削除 -------------------------------------------------------------------

    def confirm_delete(self) -> None:
        """確認してからローカルのプレイリストを消す。"""
        if not self.editable:
            return
        dialog = Adw.AlertDialog(heading="プレイリストを削除しますか？",
                                 body=f"「{self.playlist_name}」を削除します。この操作は取り消せません。")
        dialog.add_response("cancel", "キャンセル")
        dialog.add_response("delete", "削除")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", weak_handler(self._on_delete_response))
        self._dialog = dialog
        dialog.present(self)

    def _on_delete_response(self, _dialog, response: str) -> None:
        self._dialog = None
        if response == "delete":
            self.delete_playlist()

    def delete_playlist(self) -> None:
        """確認なしで消す (確認は confirm_delete)。消せたら前のページへ戻る。"""
        if not self.editable:
            return
        name = self.playlist_name
        ctx = self.ctx

        def done(response: Response) -> None:
            if not response.ok:
                ctx.toast(f"削除できませんでした: {response.message}")
                return
            ctx.toast(f"「{name}」を削除しました")
            ctx.refresh_local_playlists()
            self._leave()

        ctx.catalog.playlist_delete(self.playlist_id, done)

    def _leave(self) -> None:
        nav = self.get_ancestor(Adw.NavigationView)
        if nav is not None and nav.get_previous_page(self) is not None and nav.get_visible_page() is self:
            nav.pop()
        else:
            self.ctx.navigate("playlists")
