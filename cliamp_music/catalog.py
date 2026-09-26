"""cliamp のカタログ系コマンド (providers / playlists / tracks / search / lyrics /
history / ローカルのプレイリスト編集) の窓口。

結果は短い間だけ覚えておく (検索は同じ語で 10 分、プレイリスト一覧は 5 分)。
同じ要求が重なったときは 1 回だけ送り、待っている全員に同じ結果を渡す。
失敗は Response (kind 付き) をそのまま callback に渡す。

callback は常に main loop で、呼び出しから戻った後に呼ばれる (キャッシュに
あっても同期では呼ばない)。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable

from gi.repository import GLib

from . import log
from .client import CliampClient
from .protocol import (
    RECENTLY_PLAYED,
    Lyrics,
    PlaylistInfo,
    ProviderInfo,
    Response,
    Track,
    fold_text,
    parse_lyrics,
    parse_playlists,
    parse_providers,
    parse_tracks,
    track_key,
)

LOCAL = "local"

PROVIDERS_TTL = 300.0
PLAYLISTS_TTL = 300.0
TRACKS_TTL = 300.0
LOCAL_TRACKS_TTL = 60.0  # TUI 側でも書き換えられるので短め
SEARCH_TTL = 600.0
LYRICS_TTL = 1800.0
LYRICS_MISS_TTL = 600.0


def _call(callback: Callable[[Any], object], value: Any) -> bool:
    try:
        callback(value)
    except Exception as exc:
        import traceback

        log(f"カタログの結果の処理で例外: {exc}")
        traceback.print_exc()
    return GLib.SOURCE_REMOVE


def _copy(value: Any) -> Any:
    # 呼び出し側が並びを書き換えてもキャッシュが壊れないよう、渡すたびに写す。
    return list(value) if isinstance(value, list) else value


class Catalog:
    def __init__(self, client: CliampClient):
        self.client = client
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._waiting: dict[tuple, list[Callable]] = {}

    # --- 共通 -------------------------------------------------------------------

    def _later(self, callback: Callable | None, value: Any) -> None:
        if callback is not None:
            GLib.idle_add(_call, callback, _copy(value))

    def _fetch(self, key: tuple, ttl: float, cmd: str, parse: Callable[[dict], Any],
               callback: Callable, *, force: bool = False, miss: Callable[[Response], tuple[Any, float] | None] | None = None,
               **fields) -> None:
        now = time.monotonic()
        if not force:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                self._later(callback, hit[1])
                return
        waiters = self._waiting.get(key)
        if waiters is not None:
            waiters.append(callback)
            return
        self._waiting[key] = [callback]

        def done(response: Response) -> None:
            callbacks = self._waiting.pop(key, [])
            if response.ok:
                value = parse(response.data)
                if ttl > 0:
                    self._cache[key] = (time.monotonic() + ttl, value)
            else:
                value = response
                substitute = miss(response) if miss is not None else None
                if substitute is not None:
                    value, miss_ttl = substitute
                    if miss_ttl > 0:
                        self._cache[key] = (time.monotonic() + miss_ttl, value)
            for cb in callbacks:
                _call(cb, _copy(value))

        self.client.request(cmd, done, **fields)

    def invalidate(self, provider: str | None = None) -> None:
        """覚えている結果を捨てる (Ctrl+R など)。provider を指定すればそのプロバイダーの分だけ。"""
        if provider is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if len(k) > 1 and k[1] == provider]:
            del self._cache[key]

    # --- 読み取り -----------------------------------------------------------------

    def providers(self, callback: Callable[[list[ProviderInfo] | Response], object], *, force: bool = False) -> None:
        self._fetch(("providers",), PROVIDERS_TTL, "providers", parse_providers, callback, force=force)

    def playlists(self, provider: str, callback: Callable[[list[PlaylistInfo] | Response], object], *,
                  force: bool = False) -> None:
        """プロバイダーのプレイリスト一覧。local には履歴の仮想プレイリスト "Recently Played" も入る。"""
        self._fetch(("playlists", provider), PLAYLISTS_TTL, "playlists",
                    lambda d: parse_playlists(d, provider), callback, force=force, provider=provider)

    def local_playlists(self, callback: Callable[[list[PlaylistInfo] | Response], object], *,
                        force: bool = False) -> None:
        """書き込めるローカルのプレイリストだけ ("Recently Played" を除く)。"""

        def done(result):
            if isinstance(result, Response):
                callback(result)
            else:
                callback([info for info in result if info.id != RECENTLY_PLAYED])

        self.playlists(LOCAL, done, force=force)

    def tracks(self, provider: str, id: str, callback: Callable[[list[Track] | Response], object], *,
               force: bool = False) -> None:
        ttl = LOCAL_TRACKS_TTL if provider == LOCAL else TRACKS_TTL
        self._fetch(("tracks", provider, id), ttl, "tracks", parse_tracks, callback, force=force,
                    provider=provider, id=id)

    def search(self, provider: str, query: str, callback: Callable[[list[Track] | Response], object],
               limit: int = 25, *, force: bool = False) -> None:
        query = (query or "").strip()
        if not query:
            self._later(callback, [])
            return
        limit = max(1, min(50, int(limit)))
        key = ("search", provider, fold_text(query), limit)
        self._fetch(key, SEARCH_TTL, "search", parse_tracks, callback, force=force,
                    provider=provider, query=query, limit=limit)

    def lyrics(self, artist: str, title: str, callback: Callable[[Lyrics | None], object]) -> None:
        """歌詞。見つからない・失敗は None (見つからない結果も 10 分覚える)。"""

        def miss(response: Response):
            if response.error == "not found":
                return None, LYRICS_MISS_TTL
            return None, 0.0

        def done(result):
            callback(result if isinstance(result, Lyrics) else None)

        self._fetch(("lyrics", artist or "", title or ""), LYRICS_TTL, "lyrics", parse_lyrics, done,
                    miss=miss, artist=artist or None, title=title or None)

    def history(self, callback: Callable[[list[Track] | Response], object], limit: int = 50, *,
                force: bool = False) -> None:
        """最近再生した曲 (新しい順、played_at 付き)。覚えない (いつも取り直す)。"""
        self._fetch(("history", "", int(limit)), 0.0, "history", parse_tracks, callback, force=force,
                    limit=int(limit))

    # --- 再生と編集 -----------------------------------------------------------------

    def load(self, provider: str, id: str, index: int = 0, name: str = "",
             callback: Callable[[Response], object] | None = None) -> None:
        """プロバイダーのリストを読み込んで index の曲から再生 (load_provider)。"""
        self.client.request("load_provider", callback, provider=provider, id=id, index=int(index),
                            name=name or None)

    def _edit(self, cmd: str, name: str, callback, **fields) -> None:
        def done(response: Response) -> None:
            if response.ok:
                self.invalidate(LOCAL)
                self._cache.pop(("tracks", LOCAL, name), None)
            if callback is not None:
                callback(response)

        self.client.request(cmd, done, name=name, **fields)

    def playlist_add(self, name: str, tracks: Iterable[Track],
                     callback: Callable[[Response], object] | None = None) -> None:
        """ローカルのプレイリストに曲を足す (無ければ作る)。"""
        self._edit("playlist_add", name, callback, tracks=list(tracks))

    def playlist_delete(self, name: str, callback: Callable[[Response], object] | None = None) -> None:
        self._edit("playlist_delete", name, callback)

    def playlist_remove_track(self, name: str, index: int,
                              callback: Callable[[Response], object] | None = None) -> None:
        self._edit("playlist_remove_track", name, callback, index=int(index))

    # --- ライブラリの検索 ---------------------------------------------------------------

    def search_library(self, query: str, callback: Callable[[list[Track] | Response], object],
                       limit: int = 200) -> None:
        """「ライブラリ」の範囲の検索。ローカルのプレイリストの曲と履歴から手元で探す。

        大文字小文字と全角・半角 (NFKC) を区別しない。語を空白で区切るとすべてを含む曲。
        並びは 曲名の先頭一致 → 曲名 → アーティスト → アルバム、同じ順位なら履歴 → プレイリスト。
        両方とも取れなかったときだけ Response を返す。
        """
        terms = fold_text(query).split()
        if not terms:
            self._later(callback, [])
            return

        pools: dict[str, list[Track]] = {}
        list_order: list[str] = []
        failures: list[Response] = []
        pending = {"n": 2}

        def finish_one() -> None:
            pending["n"] -= 1
            if pending["n"] > 0:
                return
            if not pools and failures:
                callback(failures[0])
                return
            ordered = list(pools.get("history", []))
            for list_id in list_order:
                ordered.extend(pools.get(f"list:{list_id}", []))
            callback(_rank(ordered, terms, limit))

        def on_history(result) -> None:
            if isinstance(result, Response):
                failures.append(result)
            else:
                pools["history"] = result
            finish_one()

        def on_playlists(result) -> None:
            if isinstance(result, Response):
                failures.append(result)
                finish_one()
                return
            lists = [info for info in result if info.id != RECENTLY_PLAYED]
            list_order.extend(info.id for info in lists)
            pending["n"] += len(lists)
            for info in lists:
                self.tracks(LOCAL, info.id, lambda r, info=info: on_tracks(info, r))
            finish_one()

        def on_tracks(info: PlaylistInfo, result) -> None:
            if not isinstance(result, Response):
                pools[f"list:{info.id}"] = result
            finish_one()

        self.history(on_history, limit=200)
        self.playlists(LOCAL, on_playlists)


def _rank(tracks: list[Track], terms: list[str], limit: int) -> list[Track]:
    seen: set[str] = set()
    scored: list[tuple[int, int, Track]] = []
    for order, track in enumerate(tracks):
        key = track_key(track)
        if key in seen:
            continue
        title = fold_text(track.display_title)
        artist = fold_text(track.artist)
        album = fold_text(track.album)
        haystack = " ".join((title, artist, album))
        if not all(term in haystack for term in terms):
            continue
        seen.add(key)
        first = terms[0]
        if title.startswith(first):
            score = 0
        elif first in title:
            score = 1
        elif first in artist:
            score = 2
        else:
            score = 3
        scored.append((score, order, track))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [track for _, _, track in scored[:limit]]
