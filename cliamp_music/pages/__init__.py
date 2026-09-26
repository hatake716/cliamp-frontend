"""ページの入口と、ページ・骨格の部品が共通に使う小道具。

`create_page(ctx, page_id, **params) -> Adw.NavigationPage` がページを作る。
ページのモジュールは使うときに初めて読む (起動を軽くし、1 つのページの不具合で
アプリ全体が起動できなくならないように)。読めなかったページは「このページを
開けません」の代わりのページになる (理由はログに出す)。

ページ ID (DESIGN.md §3):

| ID | 引数 | クラス |
|---|---|---|
| search | — | search.SearchPage |
| home | — | home.HomePage |
| radio | — | radio.RadioPage |
| recent | — | recent.RecentPage |
| nowplaying | reveal=False | nowplaying.NowPlayingListPage |
| playlists | — | playlists.PlaylistsPage |
| playlist | provider, id, name | playlists.PlaylistDetailPage |

作ったページには `page_id` と `page_params` (dict) の属性を付け、
`Adw.NavigationPage` の tag に `page_key(page_id, **params)` を入れる
(窓がサイドバーの選択と見比べるのに使う)。

`Bindings`: 長生きする相手 (store / ctx / catalog) のシグナルを、部品を
強く掴まずに繋ぐ道具。部品が realize されている間だけ繋ぎ、unrealize で外す。
PyGObject では、self を掴んだ閉包を長生きする相手に繋ぐと部品が解放されない。
"""

from __future__ import annotations

import importlib
import traceback
import weakref
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from .. import log  # noqa: E402

__all__ = [
    "PAGE_CLASSES", "SIDEBAR_PAGES", "BOTTOM_CLEARANCE", "PAGE_TITLES",
    "create_page", "page_class", "page_key", "register", "unregister", "is_sidebar_page",
    "Bindings",
]

# ページ ID → (モジュール名, クラス名)
PAGE_CLASSES: dict[str, tuple[str, str]] = {
    "search": ("search", "SearchPage"),
    "home": ("home", "HomePage"),
    "radio": ("radio", "RadioPage"),
    "recent": ("recent", "RecentPage"),
    "nowplaying": ("nowplaying", "NowPlayingListPage"),
    "playlists": ("playlists", "PlaylistsPage"),
    "playlist": ("playlists", "PlaylistDetailPage"),
}

# サイドバーの固定の項目 (開くとナビゲーションの根を差し替える)
SIDEBAR_PAGES = ("search", "home", "radio", "recent", "nowplaying", "playlists")

# 代わりのページや窓の題に使う名前
PAGE_TITLES = {
    "search": "検索",
    "home": "ホーム",
    "radio": "ラジオ",
    "recent": "最近再生した項目",
    "nowplaying": "再生中のリスト",
    "playlists": "すべてのプレイリスト",
    "playlist": "プレイリスト",
}

# 再生バーの下に潜る分。ページはスクロールの下端にこれだけ余白を取る
# (再生バー 56 + 下の余白 14 + ゆとり)。
BOTTOM_CLEARANCE = 96

_overrides: dict[str, Callable[..., Adw.NavigationPage]] = {}


def register(page_id: str, factory: Callable[..., Adw.NavigationPage]) -> None:
    """ページの作り方を差し替える (撮影や試験で、まだ無いページの代役を入れる)。

    factory(ctx, **params) -> Adw.NavigationPage。"""
    _overrides[page_id] = factory


def unregister(page_id: str) -> None:
    _overrides.pop(page_id, None)


def is_sidebar_page(page_id: str) -> bool:
    return page_id in SIDEBAR_PAGES


def page_key(page_id: str, **params: Any) -> str:
    """ページの同一性の鍵。サイドバーの行と見比べる。

    プレイリストは "playlist:<provider>:<id>"、それ以外はページ ID そのもの
    (nowplaying の reveal のような「開き方」の引数は鍵に含めない)。"""
    if page_id == "playlist":
        return f"playlist:{params.get('provider') or ''}:{params.get('id') or ''}"
    return page_id


def page_class(page_id: str) -> type:
    """ページのクラスを読む。無ければ KeyError、読めなければ ImportError など。"""
    module_name, class_name = PAGE_CLASSES[page_id]
    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, class_name)


def create_page(ctx, page_id: str, **params: Any) -> Adw.NavigationPage:
    """ページを作る。作れなければ理由をログに出し、代わりのページを返す。"""
    factory = _overrides.get(page_id)
    try:
        if factory is None:
            factory = page_class(page_id)
        page = factory(ctx, **params)
    except Exception as exc:  # 1 つのページの不具合でアプリを止めない
        log(f"ページ {page_id} を作れません: {exc}")
        traceback.print_exc()
        page = _UnavailablePage(page_id, str(exc))
    page.page_id = page_id
    page.page_params = dict(params)
    try:
        page.set_tag(page_key(page_id, **params))
    except Exception:  # tag は見比べの補助。付けられなくても動く
        pass
    return page


class _UnavailablePage(Adw.NavigationPage):
    """ページを作れなかったときの代わり。"""

    __gtype_name__ = "CliampMusicUnavailablePage"

    def __init__(self, page_id: str, reason: str):
        title = PAGE_TITLES.get(page_id, page_id)
        super().__init__(title=title)
        from ..widgets import EmptyState

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_title(False)
        view.add_top_bar(header)
        view.set_extend_content_to_top_edge(True)
        empty = EmptyState("music-note-symbolic", "このページを開けません",
                           f"「{title}」の読み込みに失敗しました。{reason}")
        empty.set_margin_bottom(BOTTOM_CLEARANCE)
        view.set_content(empty)
        self.set_child(view)

    def refresh(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 長生きする相手へのシグナルの繋ぎ方


class Bindings:
    """store / ctx などのシグナルを、持ち主の部品を強く掴まずに繋ぐ。

    使い方:
        self._bindings = Bindings(self)
        self._bindings.add(ctx.store, "status-changed", self._on_status)

    - 繋ぐ関数は持ち主の bound method を弱い参照で持つ (持ち主が消えれば黙る)。
    - 持ち主が realize されている間だけ繋ぎ、unrealize で外す。持ち主が
      まだ realize されていなければ realize のときに繋ぐ。
    - on_rebind: realize で繋いだ直後に呼ぶ持ち主の関数。繋いでいなかった間
      (作ってから realize まで、unrealize 中) の変化を取り込むのに使う。
    - `disconnect_all()` でいつでも外せる (以後 realize されても繋がない)。
    """

    def __init__(self, owner: Gtk.Widget, on_rebind: Callable[[], object] | None = None):
        self._owner = weakref.ref(owner)
        self._specs: list[tuple[weakref.ref, str, weakref.WeakMethod]] = []
        self._ids: list[tuple[GObject.Object, int]] = []
        self._rebind = weakref.WeakMethod(on_rebind) if on_rebind is not None else None
        self._closed = False
        # 持ち主 → 閉包 → Bindings の向きだけ (Bindings は持ち主を弱く持つ)
        owner.connect("realize", Bindings._on_realize, self)
        owner.connect("unrealize", Bindings._on_unrealize, self)
        owner.connect("destroy", Bindings._on_destroy, self)

    @staticmethod
    def _on_realize(_owner, self: "Bindings") -> None:
        self._connect_all()
        if self._rebind is not None and not self._closed:
            method = self._rebind()
            if method is not None:
                method()

    @staticmethod
    def _on_unrealize(_owner, self: "Bindings") -> None:
        self._disconnect_ids()

    @staticmethod
    def _on_destroy(_owner, self: "Bindings") -> None:
        self.disconnect_all()

    def add(self, source: GObject.Object, signal: str, method: Callable) -> None:
        """source の signal を持ち主の bound method に繋ぐ (引数はふつうのハンドラと同じ)。"""
        if self._closed:
            return
        spec = (weakref.ref(source), signal, weakref.WeakMethod(method))
        self._specs.append(spec)
        owner = self._owner()
        if owner is not None and owner.get_realized():
            self._connect_one(spec)

    def _connect_one(self, spec) -> None:
        source_ref, signal, method_ref = spec
        source = source_ref()
        if source is None:
            return

        def handler(*args):
            method = method_ref()
            if method is None:
                return None
            return method(*args)

        self._ids.append((source, source.connect(signal, handler)))

    def _connect_all(self) -> None:
        if self._closed:
            return
        self._disconnect_ids()
        for spec in self._specs:
            self._connect_one(spec)

    def _disconnect_ids(self) -> None:
        ids, self._ids = self._ids, []
        for source, handler_id in ids:
            try:
                if source.handler_is_connected(handler_id):
                    source.disconnect(handler_id)
            except Exception:  # 相手が先に消えていても構わない
                pass

    def disconnect_all(self) -> None:
        self._closed = True
        self._disconnect_ids()
        self._specs.clear()

    @property
    def connected(self) -> bool:
        return bool(self._ids)
