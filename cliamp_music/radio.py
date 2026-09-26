"""Radio Browser (radio-browser.info) から局の一覧を取る。

cliamp の radio プロバイダーと同じ API (de1.api.radio-browser.info) を使う。
cliamp の radio プロバイダーで検索すると TUI の表示まで入れ替わるので、GUI は
自分で API を呼び、局を「そのまま replace できる Track」として返す。

取得はスレッドで行い、callback は main loop で 1 回呼ぶ。失敗は日本語の理由 (str)。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote

from gi.repository import GLib

from . import VERSION, log
from .protocol import Track

BASE_URL = "https://de1.api.radio-browser.info/json"
TIMEOUT = 10.0
CACHE_TTL = 1800.0
SEARCH_TTL = 600.0
CACHE_SWEEP = 64  # 覚えている URL がこれを超えたら期限切れを捨てる
SEARCH_CACHE_MAX = 100  # 局名の検索を覚えておく数

StationsCallback = Callable[[list[Track] | str], object]


def urlopen(request, timeout):
    """urllib.request.urlopen の薄い包み (試験で差し替える)。"""
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_json(url: str, timeout: float = TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers={
        "User-Agent": f"cliamp-music/{VERSION}",
        "Accept": "application/json",
    })
    with urlopen(request, timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_stations(data: Any) -> list[Track]:
    """Radio Browser の局の配列を Track に。同じ URL・同じ名前の重複は先のものを残す。"""
    if isinstance(data, (bytes, bytearray)):
        data = data.decode("utf-8", "replace")
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, list):
        return []
    out: list[Track] = []
    seen_urls: set[str] = set()
    seen_names: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url_resolved")) or _text(item.get("url"))
        name = " ".join(_text(item.get("name")).split())
        if not url.startswith(("http://", "https://")) or not name:
            continue
        name_key = name.casefold()
        if url in seen_urls or name_key in seen_names:
            continue
        seen_urls.add(url)
        seen_names.add(name_key)
        favicon = _text(item.get("favicon"))
        if not favicon.startswith(("http://", "https://")):
            favicon = ""
        bitrate = item.get("bitrate")
        bitrate = int(bitrate) if isinstance(bitrate, (int, float)) and not isinstance(bitrate, bool) and bitrate > 0 else 0
        tags = ",".join(t.strip() for t in _text(item.get("tags")).split(",") if t.strip())
        meta = {
            "art": favicon,
            "radio.country": _text(item.get("country")),
            "radio.countrycode": _text(item.get("countrycode")).upper(),
            "radio.tags": tags,
            "radio.codec": _text(item.get("codec")),
            "radio.bitrate": str(bitrate) if bitrate else "",
            "radio.homepage": _text(item.get("homepage")),
        }
        out.append(Track(
            path=url,
            title=name,
            stream=True,
            live=True,
            meta=tuple((k, v) for k, v in meta.items() if v),
        ))
    return out


def station_subtitle(track: Track) -> str:
    """局の副題「日本 · jpop · 128 kbps」。"""
    parts = []
    country = track.meta_get("radio.country")
    if country:
        parts.append(country)
    tags = track.meta_get("radio.tags")
    if tags:
        parts.append(tags.split(",")[0])
    bitrate = track.meta_get("radio.bitrate")
    if bitrate:
        parts.append(f"{bitrate} kbps")
    return " · ".join(parts)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"Radio Browser が応答しませんでした (HTTP {exc.code})"
    if isinstance(exc, (TimeoutError, )) or "timed out" in str(exc):
        return "Radio Browser の応答が時間内にありませんでした"
    if isinstance(exc, urllib.error.URLError):
        return "Radio Browser に接続できません"
    if isinstance(exc, (ValueError, UnicodeDecodeError)):
        return "Radio Browser の応答を読めません"
    return f"局の一覧を取得できませんでした ({exc})"


def _call(callback: StationsCallback, value) -> bool:
    try:
        callback(value)
    except Exception as exc:
        import traceback

        log(f"ラジオの結果の処理で例外: {exc}")
        traceback.print_exc()
    return GLib.SOURCE_REMOVE


class RadioBrowser:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._cache: dict[str, tuple[float, list[Track]]] = {}
        self._waiting: dict[str, list[StationsCallback]] = {}

    def top(self, callback: StationsCallback, limit: int = 40) -> None:
        """投票の多い局 (stations/topvote)。"""
        url = f"{self.base_url}/stations/topvote/{int(limit)}?hidebroken=true"
        self._get(url, callback, CACHE_TTL)

    def by_country(self, code: str, callback: StationsCallback, limit: int = 40) -> None:
        """国コード (JP など) の局を投票の多い順に。"""
        code = quote((code or "").strip().upper(), safe="")
        url = (f"{self.base_url}/stations/bycountrycodeexact/{code}"
               f"?order=votes&reverse=true&limit={int(limit)}&hidebroken=true")
        self._get(url, callback, CACHE_TTL)

    def search(self, query: str, callback: StationsCallback, limit: int = 60) -> None:
        """局名で探す (投票の多い順)。空の語は空の一覧。"""
        query = (query or "").strip()
        if not query:
            GLib.idle_add(_call, callback, [])
            return
        url = (f"{self.base_url}/stations/byname/{quote(query, safe='')}"
               f"?limit={int(limit)}&order=votes&reverse=true&hidebroken=true")
        self._get(url, callback, SEARCH_TTL)

    def _get(self, url: str, callback: StationsCallback, ttl: float) -> None:
        hit = self._cache.get(url)
        if hit is not None and hit[0] > time.monotonic():
            GLib.idle_add(_call, callback, list(hit[1]))
            return
        waiters = self._waiting.get(url)
        if waiters is not None:
            waiters.append(callback)
            return
        self._waiting[url] = [callback]

        def work() -> None:
            try:
                result: list[Track] | str = parse_stations(fetch_json(url))
            except Exception as exc:
                result = describe_error(exc)
                log(f"Radio Browser: {url}: {exc}")
            GLib.idle_add(self._finish, url, result, ttl)

        threading.Thread(target=work, name="radio-browser", daemon=True).start()

    def _finish(self, url: str, result: list[Track] | str, ttl: float) -> bool:
        if isinstance(result, list):
            now = time.monotonic()
            self._cache.pop(url, None)
            self._cache[url] = (now + ttl, result)
            if len(self._cache) > CACHE_SWEEP:
                # 期限の切れたものを捨て、局名の検索は新しいものだけを残す (打ちながら探すと増える)
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                searches = [k for k in self._cache if "/stations/byname/" in k]
                for old in searches[: max(0, len(searches) - SEARCH_CACHE_MAX)]:
                    del self._cache[old]
        for callback in self._waiting.pop(url, []):
            _call(callback, list(result) if isinstance(result, list) else result)
        return GLib.SOURCE_REMOVE
