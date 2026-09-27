"""cliamp のカタログ系コマンド (providers / playlists / tracks / search / lyrics /
history / ローカルのプレイリスト編集) の窓口。

結果は短い間だけ覚えておく (検索は同じ語で 10 分、プレイリスト一覧は 5 分)。
期限の切れたものは足すたびに (まとめて) 捨て、検索は新しい 200 件だけを覚える。
同じ要求が重なったときは 1 回だけ送り、待っている全員に同じ結果を渡す。
失敗は Response (kind 付き) をそのまま callback に渡す。検索の打ち直しで送らずに
捨てられた古い検索 (kind "cancelled") も同じく渡す。

callback は常に main loop で、呼び出しから戻った後に呼ばれる (キャッシュに
あっても同期では呼ばない)。

providers の答えの playback (曲を YouTube で探して鳴らすか) は、cliamp の Spotify のセッションが
できてからしか付かない。セッションは spotify の playlists / tracks / search の初回に作られるので、
それより前の答えには載らない。そこで、spotify のカタログ系が (この cliamp で) 初めて成功した後に
providers を 1 度だけ取り直し、覚えている答えを置き換える (PROTOCOL.md の ProviderInfo)。
providers の答えを受け取るたびに add_providers_listener の聞き手へ渡す (取り直しの答えも)。

Spotify の Web API が「開発者アプリの持ち主が Premium でない」と断ったとき (403 "Active premium
subscription required for the owner of the app") と、開発者の利用枠を使い切ったとき (429 の reason
"QUOTA_EXCEEDED"。1 人の開発者のアプリはすべて 1 つの枠を分け合う) は、どの呼び出しも同じ答えになる。
サイドバー・ホーム・すべてのプレイリスト・打ちながらの検索がそれぞれ Spotify に頼み直さない (利用枠を
減らさない) よう、その答えを STICKY_FAILURE_TTL (2 分。利用枠で言われた待ちがそれより短ければその間) だけ覚え、
そのプロバイダーの playlists / tracks / search には送らずに同じ失敗を返す (force (Ctrl+R) と繋ぎ直しでは
忘れて頼み直す)。短めなのは、Premium にした後で Spotify が断りを解いた (反映に数時間かかる) ことに
早く気づくため。

track_hook (AppContext が差す) は、届いた曲の並び (tracks / search / history) を渡す前に通す関数。
取り込んだ Spotify のプレイリストの曲 (ローカルの TOML で meta を失ったもの) に Spotify の曲 ID を
付け直すのに使う (spotify_import.ImportIndex.restore_all)。
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
    is_spotify_error,
    is_spotify_owner_premium_required,
    parse_lyrics,
    parse_playlists,
    parse_providers,
    parse_tracks,
    spotify_quota_wait,
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
# 覚えておく数の上限 (種類ごと)。期限より先にこれを超えたら古いものから捨てる
CACHE_CAPS = {"search": 200, "lyrics": 300, "tracks": 100}
# 期限切れをまとめて捨てる目安 (件数と間隔)
SWEEP_ENTRIES = 256
SWEEP_SECONDS = 60.0
# カタログ系の初回にセッションを作り、その後で providers の playback が変わりうるプロバイダー
LAZY_SESSION_PROVIDERS = frozenset({"spotify"})
_SESSION_KINDS = frozenset({"playlists", "tracks", "search"})
_PROVIDERS_KEY = ("providers",)
# 「持ち主が Premium でない」「利用枠を使い切った」のように、そのプロバイダーのどの呼び出しも同じに
# 断られる失敗を覚える時間。Premium にした後で Spotify が断りを解いたら早く気づけるよう短め
# (Ctrl+R はいつでもすぐ頼み直す)
STICKY_FAILURE_TTL = 120.0
# その失敗を覚えるプロバイダー (持ち主の Premium と利用枠は Spotify の開発者アプリの断り)
STICKY_PROVIDERS = frozenset({"spotify"})


def sticky_failure_ttl(error: str) -> float:
    """どの呼び出しも同じに断られる失敗なら、それを覚えておく秒。違えば 0。

    cliamp の Spotify の誤り ("spotify: " で始まる) だけが対象: 持ち主が Premium でない (403) は
    STICKY_FAILURE_TTL。利用枠を使い切った (429 QUOTA_EXCEEDED) は STICKY_FAILURE_TTL か、言われた待ちが
    それより短ければその待ち。ふつうの回数の制限は覚えない。YouTube の検索の誤りは利用者の語を繰り返す
    ("resolving yt-dlp ytsearch25:<語>: …") ので、語に同じ言葉があっても覚えない (覚えると、その後の
    YouTube の検索がすべて送られずに同じ誤りになる)。"""
    if not is_spotify_error(error):
        return 0.0
    if is_spotify_owner_premium_required(error):
        return STICKY_FAILURE_TTL
    wait = spotify_quota_wait(error)
    if wait is not None:
        return min(wait, STICKY_FAILURE_TTL) if wait > 0 else STICKY_FAILURE_TTL
    return 0.0


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
        self._lane_latest: dict[str, tuple] = {}
        self._swept = 0.0
        # カタログ系が成功した (= cliamp がセッションを作った) プロバイダー。繋ぎ直しで空にする
        self._sessions: set[str] = set()
        # 送った providers の答えがセッションより前のものかもしれない (届いたら取り直す)
        self._providers_again = False
        self._providers_listeners: list[Callable[[list[ProviderInfo]], object]] = []
        # プロバイダー → (期限, 失敗の Response)。どの呼び出しも同じに断られる失敗 (持ち主が Premium でない・
        # 利用枠を使い切った)
        self._provider_failures: dict[str, tuple[float, Response]] = {}
        # 曲の並びを渡す前に通す関数 (AppContext が取り込んだ曲の ID を付け直すのに使う)
        self.track_hook: Callable[[list[Track]], list[Track]] | None = None

    # --- 共通 -------------------------------------------------------------------

    def _later(self, callback: Callable | None, value: Any) -> None:
        if callback is not None:
            GLib.idle_add(_call, callback, _copy(value))

    def _store(self, key: tuple, ttl: float, value: Any) -> None:
        """結果を覚える。期限切れをまとめて捨て、種類ごとの上限を超えたら古いものから捨てる。"""
        now = time.monotonic()
        self._cache.pop(key, None)  # 入れ直して並びを新しい順の末尾にする
        self._cache[key] = (now + ttl, value)
        if len(self._cache) > SWEEP_ENTRIES or now - self._swept > SWEEP_SECONDS:
            self._swept = now
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        cap = CACHE_CAPS.get(key[0])
        if cap is not None:
            same = [k for k in self._cache if k[0] == key[0]]
            for old in same[: max(0, len(same) - cap)]:
                del self._cache[old]

    def _fetch(self, key: tuple, ttl: float, cmd: str, parse: Callable[[dict], Any],
               callback: Callable, *, force: bool = False, miss: Callable[[Response], tuple[Any, float] | None] | None = None,
               lane: str | None = None, **fields) -> None:
        now = time.monotonic()
        provider = key[1] if len(key) > 1 and key[0] in _SESSION_KINDS else None
        if provider is not None:
            failure = self._provider_failures.get(provider)
            if failure is not None and (force or failure[0] <= now):
                del self._provider_failures[provider]  # 頼み直す (Ctrl+R・期限切れ)
            elif failure is not None:
                self._later(callback, failure[1])
                return
        if not force:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                self._later(callback, hit[1])
                return
        if lane:
            self._lane_latest[lane] = key
        waiters = self._waiting.get(key)
        if waiters is not None:
            waiters.append(callback)
            return
        self._waiting[key] = [callback]

        def done(response: Response) -> None:
            if response.kind == "cancelled" and lane and self._lane_latest.get(lane) == key:
                # 打ち直しで捨てられた検索に、また同じ語で頼まれていた (「abc」→「abcd」→「abc」)。
                # 待っている人がいるので送り直す
                self.client.request(cmd, done, lane=lane, **fields)
                return
            callbacks = self._waiting.pop(key, [])
            # セッションより前に送った providers の答え (覚えない。届いたら取り直す)
            stale = key == _PROVIDERS_KEY and self._providers_again
            if response.ok:
                value = parse(response.data)
                if ttl > 0 and not stale:
                    self._store(key, ttl, value)
                if provider is not None:
                    self._provider_failures.pop(provider, None)
            else:
                value = response
                # 覚えるのは Spotify のアカウント (開発者アプリ) ごとの断りだけ
                sticky = sticky_failure_ttl(response.error) if provider in STICKY_PROVIDERS else 0.0
                if sticky > 0:
                    self._provider_failures[provider] = (time.monotonic() + sticky, response)
                substitute = miss(response) if miss is not None else None
                if substitute is not None:
                    value, miss_ttl = substitute
                    if miss_ttl > 0:
                        self._store(key, miss_ttl, value)
            for cb in callbacks:
                _call(cb, _copy(value))
            self._after(key, response.ok, value, stale)

        self.client.request(cmd, done, lane=lane, **fields)

    def _after(self, key: tuple, ok: bool, value: Any, stale: bool) -> None:
        """答えが届いた後の始末: providers の聞き手へ渡す・セッションができたら providers を取り直す。"""
        if key == _PROVIDERS_KEY:
            if stale:
                self._providers_again = False
                self._refresh_providers()
            elif ok:
                for listener in list(self._providers_listeners):
                    _call(listener, _copy(value))
        elif ok and key[0] in _SESSION_KINDS and len(key) > 1 and key[1] in LAZY_SESSION_PROVIDERS:
            self._session_started(key[1])

    def _session_started(self, provider: str) -> None:
        """provider のカタログ系が初めて成功した (cliamp がセッションを作った)。覚えている providers の
        答えがそれより前のもので、provider の鳴らし方が載っていなければ、1 度だけ取り直す。"""
        if provider in self._sessions:
            return
        self._sessions.add(provider)
        hit = self._cache.get(_PROVIDERS_KEY)
        if (_PROVIDERS_KEY not in self._waiting and hit is not None
                and any(info.key == provider and info.playback for info in hit[1])):
            return  # もう載っている (セッションはこの答えより前からあった)
        self._cache.pop(_PROVIDERS_KEY, None)  # 古い答えを配らない
        self._refresh_providers()

    def _refresh_providers(self) -> None:
        if _PROVIDERS_KEY in self._waiting:
            # 送ってある providers はセッションより前に答えたかもしれない。届いてから取り直す
            self._providers_again = True
            return
        self.providers(lambda _result: None, force=True)

    def add_providers_listener(self, listener: Callable[[list[ProviderInfo]], object]) -> None:
        """providers の答え (成功したもの) が届くたびに listener(答え) を呼ぶ。カタログが自分で
        取り直した答え (セッションができた後) もここに来る。キャッシュからの答えでは呼ばない。"""
        self._providers_listeners.append(listener)

    def invalidate(self, provider: str | None = None) -> None:
        """覚えている結果を捨てる (Ctrl+R・繋ぎ直しなど)。provider を指定すればそのプロバイダーの分だけ。

        全部を捨てるときは、どのプロバイダーのセッションができたかも忘れる (繋ぎ直した cliamp は
        再起動したものかもしれない。次のカタログ系の成功の後でまた providers を取り直す)。"""
        if provider is None:
            self._cache.clear()
            self._sessions.clear()
            self._provider_failures.clear()
            return
        self._provider_failures.pop(provider, None)
        for key in [k for k in self._cache if len(k) > 1 and k[1] == provider]:
            del self._cache[key]

    def provider_failure(self, provider: str) -> Response | None:
        """provider のどの呼び出しも同じに断られている失敗 (持ち主が Premium でない・利用枠を使い切った)。
        無ければ None。"""
        failure = self._provider_failures.get(provider)
        if failure is None or failure[0] <= time.monotonic():
            return None
        return failure[1]

    def _tracks(self, data: dict) -> list[Track]:
        tracks = parse_tracks(data)
        hook = self.track_hook
        if hook is not None and tracks:
            try:
                tracks = list(hook(tracks))
            except Exception as exc:  # 飾り (絵とリンク) のための付け直し。失敗しても曲は渡す
                log(f"曲の付け直しに失敗: {exc}")
        return tracks

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
        self._fetch(("tracks", provider, id), ttl, "tracks", self._tracks, callback, force=force,
                    provider=provider, id=id)

    def search(self, provider: str, query: str, callback: Callable[[list[Track] | Response], object],
               limit: int = 25, *, force: bool = False, lane: str | None = None) -> None:
        """lane を渡すと、同じ lane の後の検索が来た時点でまだ送っていないこの検索は送らずに
        捨てる (Response kind "cancelled"。打ちながらの検索で古い語が worker を塞がないように)。"""
        query = (query or "").strip()
        if not query:
            self._later(callback, [])
            return
        limit = max(1, min(50, int(limit)))
        key = ("search", provider, fold_text(query), limit)
        self._fetch(key, SEARCH_TTL, "search", self._tracks, callback, force=force, lane=lane,
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
        self._fetch(("history", "", int(limit)), 0.0, "history", self._tracks, callback, force=force,
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
                              callback: Callable[[Response], object] | None = None,
                              path: str | None = None) -> None:
        """index の曲を外す。path (その位置で見ていた曲) を付けると、パッチ済みの cliamp は
        違う曲なら "stale" で断る。最後の曲を外すとプレイリストのファイルごと消える。"""
        self._edit("playlist_remove_track", name, callback, index=int(index), path=path or None)

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
