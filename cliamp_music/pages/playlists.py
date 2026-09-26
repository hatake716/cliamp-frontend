"""すべてのプレイリスト (PlaylistsPage) とプレイリストの詳細 (PlaylistDetailPage)。

PlaylistsPage: 大見出し「プレイリスト」。プロバイダーごとの節 (ローカルが先頭、
次に playlists を持つプロバイダー。radio は除く) に、プレイリストのカードの格子。
サインインの要るプロバイダーは節に小さな注意書きを出す。押すと詳細へ。
曲を YouTube で探して鳴らすプロバイダー (ProviderInfo の playback が "youtube"。Web API
だけの Spotify) の節には「曲は YouTube で探して再生します」と書き添える。playback は cliamp が
Spotify のセッションを作った後 (spotify の playlists の初回の後) の providers にしか載らないので、
カタログが取り直した答えを ctx が覚えたとき ("providers-changed") にも書き添えを直す。

PlaylistDetailPage: Apple のアルバムの頁の形。左上に 250px の絵 (先頭 4 曲の 2x2、
無ければ代わりの絵)、題 (26px/700)、提供元 (26px/400、赤)、情報の行、ボタン
(シャッフルの丸 / 「▶ 再生」/ 「…」)。下に番号付きの行。ダブルクリックでその曲から
再生する: 見えている曲の並びをそのまま `replace` で送る (`load_provider` はプロバイダーに
取り直させて添字で選ぶので、その間にリストが変わっていると別の曲が鳴る)。replace の無い
cliamp と、とても長いリストだけ `load_provider`。ローカルのプレイリストは「…」から
削除でき (確認あり)、行の「…」に「プレイリストから削除」が出る。最後の曲を外すと cliamp は
プレイリストごと消すので、そのときはページを閉じる。曲を YouTube で探して鳴らすプレイリスト
(プロバイダーの playback が "youtube" か、曲が YouTube で探す Spotify の曲) は、情報の行の
下に「曲は YouTube で探して再生します」と控えめに書き添える。

Spotify から取り込む: すべてのプレイリストのヘッダーの右にガラスのカプセル「Spotify から取り込む…」
(狭い幅では記号だけ)。ローカルのプレイリストが 1 つも無いときと、Spotify の節が「開発者アプリの持ち主が
Premium でない」で読めないとき (その説明を書く) は、節にも同じボタンを出す。取り込んだローカルの
プレイリストの詳細は、提供元の行を Spotify での作り手 (アルバムならアーティスト)、絵を Spotify の絵にし、
情報の行の下に「Spotify から取り込み · 曲は YouTube で探して再生します」と書き添え、「…」に
「Spotify から更新」(読み直し、確かめてから置き換える)・「Spotify で開く」を足す。カードは副題を
「Spotify · N 曲」、絵を Spotify の絵にする。取り込んだものかは記録 (名前で引く) だけでなく曲でも
確かめる (AppContext.imported_record。TUI で消して同じ名前で作り直したものは別物として記録を忘れる)。
Spotify のプレイリストの詳細が同じ理由で読めないときは、そのプレイリストを取り込むボタンを出す。
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GObject, Gtk  # noqa: E402

from ..protocol import (  # noqa: E402
    PLAYBACK_YOUTUBE,
    RECENTLY_PLAYED,
    SPOTIFY_OWNER_PREMIUM,
    PlaylistInfo,
    Response,
    Source,
    Track,
    is_spotify_not_accessible,
    is_spotify_owner_premium_required,
    is_youtube_bridge,
)
from ..spotify_import import SpotifyRef  # noqa: E402
from ..widgets import (  # noqa: E402
    CapsuleButton,
    GlassButton,
    MediaCard,
    SectionHeader,
    TrackList,
    TrackRow,
    format_count,
)
from .home import (  # noqa: E402
    DETAIL_LIST_SIDE,
    OFFLINE_TEXT,
    OFFLINE_TITLE,
    SIDE,
    UNSUPPORTED_TEXT,
    UNSUPPORTED_TITLE,
    WEB_ONLY_NOTE,
    AdaptiveGrid,
    ChunkedRows,
    ContentStack,
    DetailHeader,
    PageBase,
    _label,
    imported_record,
    info_line,
    is_playing,
    provider_label,
    provider_playback,
    remember_provider_names,
    row_key,
    set_local_playlist_art,
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
IMPORT_LABEL = "Spotify から取り込む…"
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
IMPORTED_NOTE = "Spotify から取り込み · 曲は YouTube で探して再生します"


def imported_note(record) -> str:
    """取り込んだプレイリストの書き添え (公開ページの上限で切れていれば、そのことも)。"""
    if record is not None and record.truncated and record.total > record.count:
        return f"Spotify から取り込み (全 {record.total} 曲のうち最初の {record.count} 曲) · 曲は YouTube で探して再生します"
    return IMPORTED_NOTE


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
        # 「Spotify から取り込む…」(ローカルが空のとき・Spotify のライブラリが読めないとき)
        self.action = CapsuleButton(IMPORT_LABEL, "music-import-symbolic")
        self.action.add_css_class("music-section-action")
        self.action.set_halign(Gtk.Align.START)
        self.action.connect("clicked", weak_call(page.open_import))
        page.inset(self.action)
        self.action.set_visible(False)
        self.append(self.action)
        self.offer_import = False
        self.groups = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        page.inset(self.groups)
        self.append(self.groups)
        # プレイリストの一覧が取れたか (鳴らし方の書き添えは取れたときだけ)、取れないときの注意書き
        self.loaded = False
        self.problem = ""

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
        self.import_button = GlassButton(IMPORT_LABEL, "music-import-symbolic")
        self.import_button.add_css_class("music-header-action")
        self.import_button.connect("clicked", weak_call(self.open_import))
        self.header_bar.pack_end(self.import_button)
        self.state = ContentStack("読み込み中…")
        self.sections: dict[str, _ProviderSection] = {}
        self.body.append(self.state)
        self._serial = 0
        self.watch(ctx.store, "connection-changed", self._on_connection)
        if isinstance(ctx, GObject.Object):
            self.watch(ctx, "local-playlists-changed", self._on_local_changed)
            if GObject.signal_lookup("providers-changed", type(ctx)):
                self.watch(ctx, "providers-changed", self._on_providers_changed)

    # --- 読み込み ---------------------------------------------------------------

    def load(self, force: bool = False) -> None:
        store = self.ctx.store
        self._update_import_button()
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
        self._update_import_button()
        if self.ctx.store.connected:
            self.load(force=False)

    def _update_import_button(self) -> None:
        store = self.ctx.store
        self.import_button.set_sensitive(store.connected and store.supports("playlist_add"))

    def open_import(self) -> None:
        """「Spotify から取り込む」の窓を出す。"""
        opener = getattr(self.ctx, "open_spotify_import", None)
        if callable(opener):
            opener()

    def on_narrow_changed(self, narrow: bool) -> None:
        self.import_button.set_compact(narrow)

    def _on_local_changed(self, *_args) -> None:
        if LOCAL in self.sections:
            self._load_section(self._serial, LOCAL, False)

    def _on_providers_changed(self, *_args) -> None:
        # 鳴らし方が分かった・変わった (Spotify のセッションができた後の providers の答え)
        for section in self.sections.values():
            self._update_note(section)

    def _update_note(self, section: _ProviderSection) -> None:
        """節の注意書き: 取れないときの説明、無ければ曲を YouTube で探して鳴らすことの書き添え。"""
        if not section.loaded:
            return
        section.action.set_visible(section.offer_import and self.ctx.store.supports("playlist_add"))
        if section.problem:
            section.show_note(section.problem)
        elif provider_playback(self.ctx, section.key) == PLAYBACK_YOUTUBE:
            # Web API だけの Spotify (無料プランなど): 曲は YouTube で探して鳴らす
            section.show_note(WEB_ONLY_NOTE)
        else:
            section.show_note("")

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
            section.loaded = True
            if isinstance(result, Response):
                section.keys = None
                self._clear_groups(section)
                # Spotify の開発者アプリの持ち主が Premium でない: 何が起きていて何を使えばよいかを言う
                blocked = is_spotify_owner_premium_required(result.error)
                section.offer_import = blocked
                if result.needs_auth:
                    section.problem = auth_note(self.ctx, key)
                elif blocked:
                    section.problem = SPOTIFY_OWNER_PREMIUM
                else:
                    section.problem = f"読み込めませんでした: {result.message}"
                self._update_note(section)
                return
            infos = [info for info in result if not (key == LOCAL and info.id == RECENTLY_PLAYED)]
            section.offer_import = key == LOCAL and not infos
            if not infos:
                section.problem = ("プレイリストはありません。曲の「…」から「プレイリストに追加」で作るか、"
                                   "Spotify の公開プレイリストを取り込めます。"
                                   if key == LOCAL else "プレイリストはありません。")
            else:
                section.problem = ""
            self._update_note(section)
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

    @staticmethod
    def _local_subtitle(count: int, imported: bool) -> str:
        subtitle = format_count(count) if count else "プレイリスト"
        return f"Spotify · {subtitle}" if imported else subtitle  # 取り込んだもの

    def _card(self, info: PlaylistInfo) -> MediaCard:
        if info.provider == LOCAL:
            # 記録があれば取り込んだものとして出し、曲が届いたら確かめ直す (_fill_art)
            lookup = getattr(self.ctx, "import_record", None)
            subtitle = self._local_subtitle(info.track_count, callable(lookup) and lookup(info.id) is not None)
        else:
            subtitle = format_count(info.track_count) if info.track_count else provider_label(self.ctx, info.provider)
        card = MediaCard(self.ctx.artwork, ("placeholder", f"{info.provider}:{info.id}", "playlist"),
                         info.name, subtitle, CARD, kind="playlist", on_activate=weak_handler(self._on_card))
        card.playlist_info = info
        if info.provider == LOCAL:
            self._fill_art(card, info)
        return card

    def _fill_art(self, card: MediaCard, info: PlaylistInfo) -> None:
        # カードは曲の一覧が届くまで強く持つ (格子のカードは Python から誰も持っていないので、
        # 包みへの弱い参照は部品が生きていても消え、絵が代わりの絵のままになっていた)
        def done(result, target: MediaCard = card) -> None:
            if isinstance(result, Response) or target.get_parent() is None:
                return
            # 記録だけ残って中身が別物 (TUI で作り直した) なら「Spotify ·」を外す (記録も忘れる)
            record = imported_record(self.ctx, info.id, result)
            target.set_subtitle(self._local_subtitle(info.track_count or len(result), record is not None))
            set_local_playlist_art(self.ctx, target.art, info.id, result, f"{info.provider}:{info.id}",
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
        # 取り込んだもの (のまま) なら、その記録 (曲が届いたときに確かめる)
        self._record = None

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
        if (not self.is_local and isinstance(ctx, GObject.Object)
                and GObject.signal_lookup("providers-changed", type(ctx))):
            # カタログが取り直した providers の答え (鳴らし方) を ctx が覚えたとき
            self.watch(ctx, "providers-changed", self._on_providers_changed)

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
        if not self.is_local and store.supports("providers"):
            # 鳴らし方 (Web API だけの Spotify は YouTube で探して鳴らす) を確かめる
            self.ctx.catalog.providers(weak_handler(self._on_providers), force=force)

        def done(result) -> None:
            if serial != self._serial:
                return
            if isinstance(result, Response):
                if result.needs_auth:
                    self._show_problem("music-note-list-symbolic", "サインインが必要です",
                                       auth_note(self.ctx, self.provider))
                elif is_spotify_owner_premium_required(result.error) or is_spotify_not_accessible(result.error):
                    # 開発者アプリの持ち主が Premium でない・Spotify が読ませない。公開プレイリストなら
                    # このまま取り込める (Spotify の Web API を通さずに公開の頁から読む)
                    url = self.spotify_url
                    owner = is_spotify_owner_premium_required(result.error)
                    # 題は断られたこと (このプレイリスト)。理由は説明の文に (題と同じ文を繰り返さない)
                    self._show_problem("music-note-list-symbolic", "このプレイリストは読めません",
                                       SPOTIFY_OWNER_PREMIUM if owner else result.message,
                                       button_label=IMPORT_LABEL if url else None,
                                       on_button=weak_call(self.open_import) if url else None)
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

    def _on_providers(self, result) -> None:
        if isinstance(result, Response):
            return
        remember_provider_names(self.ctx, result)
        self._update_note()

    def _on_providers_changed(self, *_args) -> None:
        self._update_note()

    @property
    def plays_via_youtube(self) -> bool:
        """曲を YouTube で探して鳴らすか (プロバイダーがそう言っているか、曲がそうなっている)。"""
        if self.is_local:
            return False
        return (provider_playback(self.ctx, self.provider) == PLAYBACK_YOUTUBE
                or any(is_youtube_bridge(t) for t in self._tracks))

    @property
    def import_record(self):
        """Spotify から取り込んだローカルのプレイリスト (のまま) なら、その記録 (無ければ None)。
        曲が届いたときに AppContext.imported_record で確かめたもの (記録だけ残って中身が別物なら None)。"""
        return self._record if self.editable else None

    @property
    def imported(self) -> bool:
        """取り込んだプレイリストで、曲の半分以上がまだ取り込んだ曲か。"""
        return self.import_record is not None

    @property
    def spotify_url(self) -> str:
        """Spotify のプレイリストの頁 (Spotify のプレイリストの詳細で、ID が Spotify の形のときだけ)。"""
        if self.provider != "spotify" or not _SPOTIFY_ID.fullmatch(self.playlist_id or ""):
            return ""
        return SpotifyRef("playlist", self.playlist_id).url

    def _update_note(self) -> None:
        if self.is_local:
            record = self.import_record
            self.header.set_note(imported_note(record) if record is not None else "")
            self.header.set_subtitle((record.subtitle or "Spotify") if record is not None
                                     else provider_label(self.ctx, self.provider))
            return
        self.header.set_note(WEB_ONLY_NOTE if self._tracks and self.plays_via_youtube else "")

    def _show_problem(self, icon: str, title: str, text: str, *, button_label: str | None = None,
                      on_button=None) -> None:
        self._tracks = []
        self._keys = None
        self._record = None
        self.rows.build([])
        self.header.set_info("")
        self.header.set_note("")
        self.header.set_actions_sensitive(False)
        # ローカルは曲が読めなくても「…」から削除できる
        self.header.more_button.set_sensitive(self.editable)
        self.state.show_empty(icon, title, text, button_label=button_label, on_button=on_button)

    def open_import(self) -> None:
        """このプレイリスト (Spotify) を「Spotify から取り込む」の窓で開く。"""
        opener = getattr(self.ctx, "open_spotify_import", None)
        if callable(opener):
            opener(self.spotify_url)

    def _show(self, tracks: list[Track]) -> None:
        keys = tuple(row_key(t) for t in tracks)
        self.header.set_info(info_line(len(tracks), total_duration(tracks)))
        self.header.set_actions_sensitive(bool(tracks))
        self.header.more_button.set_sensitive(self.editable or bool(tracks))
        if self.editable:
            # 取り込んだもののままか (記録だけ残って中身が別物なら記録を忘れる)
            self._record = imported_record(self.ctx, self.playlist_id, list(tracks))
        if keys != self._keys:
            self._keys = keys
            self._tracks = list(tracks)
            if self.is_local:
                set_local_playlist_art(self.ctx, self.header.art, self.playlist_id, self._tracks, self._art_key(),
                                       DetailHeader.ART)
            else:
                set_playlist_art(self.ctx, self.header.art, self._tracks, self._art_key(), DetailHeader.ART)
            # 曲を外した・足したときは差分だけ (スクロール位置と開いているメニューを失わない)
            self.rows.update(self._tracks)
        self._update_note()
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
        record = self.import_record
        if record is not None:
            spotify = Gio.Menu()
            spotify.append("Spotify から更新", "page.update-spotify")
            spotify.append("Spotify で開く", "page.open-spotify")
            menu.append_section(None, spotify)
            busy = self.playlist_id in getattr(self.ctx, "spotify_imports_busy", ())
            update = Gio.SimpleAction.new("update-spotify", None)
            update.connect("activate", weak_call(self.update_from_spotify))
            update.set_enabled(not busy and supports("playlist_add") and supports("playlist_delete"))
            group.add_action(update)
            browse = Gio.SimpleAction.new("open-spotify", None)
            browse.connect("activate", weak_call(self.open_in_spotify))
            group.add_action(browse)
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

    # --- Spotify から取り込んだもの ---------------------------------------------------

    def update_from_spotify(self) -> None:
        """取り込んだときのリンクから読み直し、確かめてから曲を置き換える (名前はそのまま)。"""
        updater = getattr(self.ctx, "update_spotify_import", None)
        if callable(updater) and self.import_record is not None:
            updater(self.playlist_id)

    def open_in_spotify(self) -> None:
        record = self.import_record
        if record is not None:
            self.ctx.open_uri(record.url)

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
            forget = getattr(ctx, "forget_import", None)
            if callable(forget):
                forget(name)
            ctx.refresh_local_playlists()
            self._leave()

        ctx.catalog.playlist_delete(self.playlist_id, done)

    def _leave(self) -> None:
        nav = self.get_ancestor(Adw.NavigationView)
        if nav is not None and nav.get_previous_page(self) is not None and nav.get_visible_page() is self:
            nav.pop()
        else:
            self.ctx.navigate("playlists")
