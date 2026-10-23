"""Spotify 公式日本トップ50プレイリストの取得とキャッシュ。

公式公開ページ https://open.spotify.com/playlist/37i9dQZEVXbKXQ4mDTEBXq を
一度だけ取得し、initialState スクリプトからスナップショットをメモリに保持する。
検索ページの「カテゴリーを探す」欄にサムネイルグリッドとして表示する。

- 取得は worker スレッド (fetch_async と同じパターン)。結果は GLib.idle_add で main loop に戻す。
- 失敗してもアプリを止めない (ログのみ)。キャッシュが無ければ検索ページは従来通りカテゴリーを出す。
- start_fetch はインスタンス生存中に一度だけ。stop 後は二度と取得しない。
- 取得時刻 (retrieval time) のみ保持し、チャート日付は存在しない。
"""

from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from gi.repository import GLib, GObject  # noqa: E402

from . import log  # noqa: E402
from .protocol import Track  # noqa: E402
from .spotify_import import (  # noqa: E402
    PAGE_USER_AGENT,
    FETCH_TIMEOUT,
    SpotifyImportError,
    clean_text as _clean_text_base,
    fetch_text,
)

CHART_URL = "https://open.spotify.com/playlist/37i9dQZEVXbKXQ4mDTEBXq"
CHART_PAGE_URL = "https://open.spotify.com/playlist/37i9dQZEVXbKXQ4mDTEBXq"
CHART_DISPLAY_LIMIT = 12

_PLAYLIST_KEY = "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"
_PLAYLIST_ID = "37i9dQZEVXbKXQ4mDTEBXq"
_PLAYLIST_URI = "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"
_OWNER_URI = "spotify:user:spotify"

_TRACK_URI_RE = re.compile(r"spotify:track:[A-Za-z0-9]{22}")
_IMAGE_RE = re.compile(
    r"https://i\.scdn\.co/image/[A-Za-z0-9._~%\-]+"
)

# Script tag with id="initialState" and type="text/plain" in any attribute order
_SCRIPT_RE = re.compile(
    r'<script(?=[^>]*\bid\s*=\s*["\']initialState["\'])'
    r'(?=[^>]*\btype\s*=\s*["\']text/plain["\'])'
    r'[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _clean_text(value: Any) -> str:
    """Surrogate を U+FFFD に置換し、ASCII control を除去し、前後空白をトリムする。"""
    if not isinstance(value, str):
        return ""
    cleaned = _clean_text_base(value)
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 0x20)
    return cleaned.strip()


def _valid_image(url: Any) -> str:
    """安全な i.scdn.co /image/ URL を検証し、合格なら URL を返す。"""
    if not isinstance(url, str) or not url:
        return ""
    if not _IMAGE_RE.fullmatch(url):
        return ""
    return url


@dataclass(frozen=True)
class ChartEntry:
    rank: int
    track: Track


@dataclass(frozen=True)
class ChartSnapshot:
    entries: tuple[ChartEntry, ...]


def parse_chart_html(page: str) -> ChartSnapshot:
    """HTML ページ文字列から initialState スクリプトを抽出し解析する。

    不正なスクリプト/base64/JSON/形状なら SpotifyImportError (日本語) を送出。
    ページ内容やデコード済み dict をログに出さない。
    """
    if not isinstance(page, str):
        raise SpotifyImportError("チャートページの形式が不正です")

    m = _SCRIPT_RE.search(page)
    if m is None:
        raise SpotifyImportError("チャートデータのスクリプトが見つかりません")

    b64_raw = m.group(1).strip()
    if not b64_raw:
        raise SpotifyImportError("チャートデータが空です")

    try:
        decoded_bytes = base64.b64decode(b64_raw, validate=True)
    except Exception:
        raise SpotifyImportError("チャートデータのエンコードが不正です") from None

    try:
        decoded_dict = json.loads(decoded_bytes)
    except Exception:
        raise SpotifyImportError("チャートデータの形式が不正です") from None

    return parse_chart_json(decoded_dict)


def parse_chart_json(decoded_dict: Any) -> ChartSnapshot:
    """デコード済み dict から公式日本トップ50プレイリストを解析する。

    不正・空・メタデータ不一致なら SpotifyImportError (日本語) を送出。
    """
    if not isinstance(decoded_dict, dict):
        raise SpotifyImportError("チャートデータの形式が不正です")

    entities = decoded_dict.get("entities")
    if not isinstance(entities, dict):
        raise SpotifyImportError("チャートデータの構造が不正です")

    items = entities.get("items")
    if not isinstance(items, dict):
        raise SpotifyImportError("チャートデータの構造が不正です")

    playlist = items.get(_PLAYLIST_KEY)
    if not isinstance(playlist, dict):
        raise SpotifyImportError("該当するプレイリストが見つかりません")

    # Validate playlist metadata
    if playlist.get("__typename") != "Playlist":
        raise SpotifyImportError("プレイリストの型が不正です")
    if playlist.get("id") != _PLAYLIST_ID:
        raise SpotifyImportError("プレイリストのIDが一致しません")
    if playlist.get("uri") != _PLAYLIST_URI:
        raise SpotifyImportError("プレイリストのURIが一致しません")

    # Validate owner
    owner_v2 = playlist.get("ownerV2")
    if not isinstance(owner_v2, dict):
        raise SpotifyImportError("プレイリストの所有者情報が不正です")
    owner_data = owner_v2.get("data")
    if not isinstance(owner_data, dict):
        raise SpotifyImportError("プレイリストの所有者情報が不正です")
    if owner_data.get("__typename") != "User":
        raise SpotifyImportError("プレイリストの所有者型が不正です")
    if owner_data.get("uri") != _OWNER_URI:
        raise SpotifyImportError("プレイリストの所有者が公式ではありません")

    # Validate content
    content = playlist.get("content")
    if not isinstance(content, dict):
        raise SpotifyImportError("プレイリストの内容が不正です")
    if content.get("__typename") != "PlaylistItemsPage":
        raise SpotifyImportError("プレイリストの内容型が不正です")

    raw_items = content.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise SpotifyImportError("プレイリストに曲がありません")

    # Validate pagingInfo: must be initial page
    for _loc in (content, content.get("pagingInfo") if isinstance(content.get("pagingInfo"), dict) else {}):
        _off = _loc.get("offset")
        if _off is not None and (type(_off) is not int or _off != 0):
            raise SpotifyImportError("最初のページではありません")
    paging_info = content.get("pagingInfo")
    if isinstance(paging_info, dict):
        next_offset = paging_info.get("nextOffset")
        if next_offset is not None:
            if not isinstance(next_offset, int) or isinstance(next_offset, bool):
                raise SpotifyImportError("ページ情報が不正です")
            if next_offset != len(raw_items):
                raise SpotifyImportError("最初のページではありません")

    # Enumerate ORIGINAL items with start=1 BEFORE filtering/dedup
    # rank = source position (1-based)
    candidates: list[tuple[int, str, ChartEntry]] = []

    for pos, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            continue
        item_v2 = raw.get("itemV2")
        if not isinstance(item_v2, dict):
            continue
        if item_v2.get("__typename") != "TrackResponseWrapper":
            continue
        data = item_v2.get("data")
        if not isinstance(data, dict):
            continue
        if data.get("__typename") != "Track":
            continue

        uri = data.get("uri")
        if not isinstance(uri, str) or not _TRACK_URI_RE.fullmatch(uri):
            continue

        title = _clean_text(data.get("name"))
        if not title:
            continue

        artists_raw = data.get("artists")
        if not isinstance(artists_raw, dict):
            continue
        artist_items = artists_raw.get("items")
        if not isinstance(artist_items, list) or not artist_items:
            continue
        names: list[str] = []
        for a in artist_items:
            if not isinstance(a, dict):
                continue
            profile = a.get("profile")
            if not isinstance(profile, dict):
                continue
            n = _clean_text(profile.get("name"))
            if n:
                names.append(n)
        if not names:
            continue

        # Duration
        duration = 0
        dur_obj = data.get("duration")
        if isinstance(dur_obj, dict):
            ms = dur_obj.get("totalMilliseconds")
            if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0:
                duration = ms // 1000

        # Album (optional string)
        album = ""
        album_obj = data.get("albumOfTrack")
        if isinstance(album_obj, dict):
            album = _clean_text(album_obj.get("name"))

        # Cover art: choose largest width safe URL
        img = ""
        has_sources = False
        if isinstance(album_obj, dict):
            cover_art = album_obj.get("coverArt")
            if isinstance(cover_art, dict):
                sources = cover_art.get("sources")
                if isinstance(sources, list) and sources:
                    has_sources = True
                    best_width = -1
                    for src in sources:
                        if not isinstance(src, dict):
                            continue
                        w = src.get("width")
                        if not isinstance(w, int) or isinstance(w, bool):
                            continue
                        url = src.get("url")
                        safe = _valid_image(url)
                        if safe and w > best_width:
                            best_width = w
                            img = safe
                    # If sources provided but none safe → skip row
                    if has_sources and not img:
                        continue

        meta_tuple: tuple[tuple[str, str], ...] = (("art", img),) if img else ()
        track = Track(
            path=uri,
            title=title,
            artist=", ".join(names),
            album=album,
            duration=duration,
            meta=meta_tuple,
        )
        candidates.append((pos, uri, ChartEntry(rank=pos, track=track)))

    if not candidates:
        raise SpotifyImportError("有効なチャート曲がありません")

    # Preserve source order (already in position order). Dedup by URI keeping first.
    seen_uris: set[str] = set()
    deduped: list[ChartEntry] = []
    for _pos, uri, entry in candidates:
        if uri in seen_uris:
            continue
        seen_uris.add(uri)
        deduped.append(entry)

    # Limit to CHART_DISPLAY_LIMIT
    limited = deduped[:CHART_DISPLAY_LIMIT]
    entries = tuple(limited)

    return ChartSnapshot(entries=entries)


class TopCharts(GObject.Object):
    """Spotify 公式日本トップ50プレイリストの非同期取得とキャッシュ。

    "changed" シグナルは、取得開始・完了時に main loop で出る。
    start_fetch はインスタンス生存中に一度だけ有効。stop 後は二度と取得しない。
    取得時刻 (retrieval time) のみ保持し、チャート日付は存在しない。
    """

    __gtype_name__ = "CliampMusicTopCharts"
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, *, fetcher: Callable[..., str] | None = None) -> None:
        super().__init__()
        self._fetcher = fetcher
        self._snapshot: ChartSnapshot | None = None
        self._error: str = ""
        self._fetching: bool = False
        self._started: bool = False
        self._stopped: bool = False
        self._generation: int = 0
        self._fetched_at: str = ""

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def entries(self) -> tuple[ChartEntry, ...]:
        if self._snapshot is None:
            return ()
        return self._snapshot.entries

    @property
    def tracks(self) -> tuple[Track, ...]:
        """後方互換: Track のタプルを返す。"""
        return tuple(e.track for e in self.entries)

    @property
    def fetched_at(self) -> str:
        """取得完了時刻 (retrieval time)。チャート日付ではない。"""
        return self._fetched_at

    @property
    def has_data(self) -> bool:
        return self._snapshot is not None and len(self._snapshot.entries) > 0

    @property
    def fetching(self) -> bool:
        return self._fetching

    @property
    def error(self) -> str:
        return self._error

    # ── Public API ──────────────────────────────────────────────────────

    def start_fetch(self) -> None:
        """バックグラウンドでチャートを取り始める。一度だけ。"""
        if self._started or self._stopped:
            return
        self._started = True
        self._fetching = True
        self._error = ""
        self._generation += 1
        gen = self._generation
        self.emit("changed")

        def run() -> None:
            try:
                fetch_fn = self._fetcher if self._fetcher is not None else fetch_text
                raw = fetch_fn(
                    CHART_URL,
                    user_agent=PAGE_USER_AGENT,
                    timeout=FETCH_TIMEOUT,
                )
                snapshot = parse_chart_html(raw)
                GLib.idle_add(self._on_complete, gen, snapshot, None)
            except SpotifyImportError as exc:
                log(f"トップチャートの取得に失敗 ({exc.kind}): {exc.detail or exc.message}")
                GLib.idle_add(self._on_complete, gen, None, str(exc))
            except Exception as exc:
                log(f"トップチャートの取得で例外: {exc!r}")
                GLib.idle_add(self._on_complete, gen, None, str(exc))

        try:
            t = threading.Thread(target=run, name="cliamp-music-charts", daemon=True)
            t.start()
        except Exception as exc:
            log(f"トップチャートスレッド起動失敗: {exc!r}")
            self._fetching = False
            self._error = "取得を開始できませんでした"
            self.emit("changed")

    def stop(self) -> None:
        """取得を停止し、保留中の完了を無効化する。二度と取得しない。"""
        self._stopped = True
        self._generation += 1
        self._fetching = False

    # ── Internal ────────────────────────────────────────────────────────

    def _on_complete(self, gen: int, snapshot: ChartSnapshot | None, err: str | None) -> bool:
        if gen != self._generation or self._stopped:
            return GLib.SOURCE_REMOVE
        self._fetching = False
        if err is not None:
            self._error = err
            # Keep previous data on failure
        else:
            self._snapshot = snapshot
            self._error = ""
            self._fetched_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        self.emit("changed")
        return GLib.SOURCE_REMOVE
