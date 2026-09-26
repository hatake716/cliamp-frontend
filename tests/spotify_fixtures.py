"""Spotify の公開頁の代わり (試験と撮影で spotify_import の HTTP を差し替える)。

本物の頁の中身は使わない。形だけを 2026-09 の実測に合わせた小さな頁を作る:

- 埋め込みの頁 (https://open.spotify.com/embed/<種類>/<id>): <script id="__NEXT_DATA__"> の
  props.pageProps.state.data.entity に name・title・subtitle・coverArt (プレイリスト) か
  visualIdentity.image (アルバム)・trackList ({uri "spotify:track:<id>", title, subtitle (アーティストを
  ",\\u00a0" で繋いだもの), duration (ミリ秒), isPlayable, isExplicit…})。上限は 100 曲。
  無い ID は HTTP 200 のまま pageProps.status 404 (「Page not found」)。
- 通常の頁 (https://open.spotify.com/<種類>/<id>、ブラウザでない User-Agent への答え):
  <meta name="music:song_count"> と og:description ("Playlist · <作り手> · 150 items · 3 saves")。

名前はすべて架空のもの。
"""

from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message

from fake_cliamp import fake_spotify_id

NBSP_COMMA = ",\u00a0"

# 架空の曲 (題, アーティストの並び, 長さのミリ秒)
SONGS = (
    ("夜明けのバス停", ["青い灯台"], 214_530),
    ("Tape Garden", ["Hyperlocal", "Kite & <Theory>"], 166_999),
    ("September Rain", ["Earth, Wind & Water"], 251_004),
    ("港町ラジオ", ["真夜中ポスト"], 198_000),
    ("Neon  Drive\n(Night Mix)", ["City Lights", "", "Ms. Tone"], 305_880),
    ("星の&<かけら>", ["Luna*Park"], 187_250),
)


def track_item(title: str, artists: list[str], duration_ms: int, seed: str, *, uri: str | None = None) -> dict:
    return {
        "uri": uri or f"spotify:track:{fake_spotify_id(seed)}",
        "uid": fake_spotify_id("uid" + seed)[:20],
        "title": title,
        "subtitle": NBSP_COMMA.join(artists),
        "isExplicit": False,
        "isNineteenPlus": False,
        "duration": duration_ms,
        "isPlayable": True,
        "playabilityReason": "PLAYABLE",
        "entityType": "track",
    }


def tracks(count: int, seed: str = "list") -> list[dict]:
    """架空の曲を count 曲 (SONGS を繰り返し、2 巡目からは題に番号を付ける)。"""
    out = []
    for i in range(count):
        title, artists, ms = SONGS[i % len(SONGS)]
        if i >= len(SONGS):
            title = f"{title} #{i + 1}"
        out.append(track_item(title, list(artists), ms + i * 1000, f"{seed}|{i}"))
    return out


def next_data_page(page_props: dict, page: str = "/playlist/[id]") -> str:
    data = {"props": {"pageProps": page_props}, "page": page, "query": {}, "buildId": "fake-build"}
    return ("<!DOCTYPE html><html><head><title>Spotify</title></head><body><div id=\"__next\"></div>"
            f"<script id=\"__NEXT_DATA__\" type=\"application/json\">{json.dumps(data, ensure_ascii=False)}</script>"
            "</body></html>")


def embed_page(*, kind: str = "playlist", id: str | None = None, name: str = "夜更かしのための曲",
               subtitle: str = "架空の作り手", items: list[dict] | None = None, cover: str | None = "default") -> str:
    """埋め込みの頁。items は trackList (既定は 6 曲)。cover は絵の URL ("default" で架空の i.scdn.co)。"""
    sid = id or fake_spotify_id(f"{kind}|{name}")
    items = tracks(6, name) if items is None else items
    if cover == "default":
        cover = f"https://i.scdn.co/image/ab67fake{sid.lower()[:16]}"
    entity = {
        "type": kind,
        "name": name,
        "uri": f"spotify:{kind}:{sid}",
        "id": sid,
        "title": name,
        "subtitle": subtitle,
        "releaseDate": None,
        "duration": 0,
        "isPlayable": True,
        "playabilityReason": "PLAYABLE",
        "isExplicit": False,
        "hasVideo": False,
        "relatedEntityUri": f"spotify:{kind}:{sid}",
        "trackList": items,
        "visualIdentity": {"backgroundBase": {"alpha": 255, "blue": 50, "green": 24, "red": 155}},
    }
    if cover and kind == "playlist":
        entity["coverArt"] = {"sources": [{"height": None, "width": None, "url": cover}]}
        entity["authors"] = None
    elif cover:
        entity["visualIdentity"]["image"] = [
            {"url": cover.replace("ab67fake", "ab67small"), "maxHeight": 64, "maxWidth": 64},
            {"url": cover, "maxHeight": 640, "maxWidth": 640},
            {"url": cover.replace("ab67fake", "ab67mid"), "maxHeight": 300, "maxWidth": 300},
        ]
    props = {"state": {"data": {"entity": entity, "embeded_entity_uri": entity["uri"],
                                "defaultAudioFileObject": {"passthrough": "NONE"}},
                       "settings": {}, "machineState": {}},
             "config": {}, "_sentryTraceData": "x", "_sentryBaggage": "y"}
    return next_data_page(props, f"/{kind}/[id]")


def missing_page(status: int = 404) -> str:
    """無い ID の埋め込みの頁 (HTTP は 200 のまま、pageProps.status が 404 / 500)。"""
    title = "Page not found" if status == 404 else "Page not available"
    return next_data_page({"status": status, "title": title, "description": "…", "links": [], "rtl": False})


def count_page(total: int, *, kind: str = "playlist", owner: str = "架空の作り手") -> str:
    """通常の頁 (ブラウザでない User-Agent への答え) の頭だけ。"""
    word = "items" if kind == "playlist" else "songs"
    return ("<!DOCTYPE html><html><head>"
            f"<meta property=\"og:title\" content=\"x\"/>"
            f"<meta property=\"og:description\" content=\"{kind.title()} · {owner} · {total} {word} · 3 saves\"/>"
            f"<meta name=\"music:song_count\" content=\"{total}\"/>"
            "</head><body></body></html>")


class FakeResponse(io.BytesIO):
    """urllib の応答の代わり (read と with と headers)。"""

    def __init__(self, body: bytes, charset: str = "utf-8"):
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = f"text/html; charset={charset}"


class FakeWeb:
    """spotify_import.urlopen の代わり。pages は URL → 文字列 (頁) か例外。頼まれた (URL, User-Agent) を
    requests に残す。知らない URL は 404。"""

    def __init__(self, pages: dict | None = None):
        self.pages: dict = dict(pages or {})
        self.requests: list[tuple[str, str]] = []
        self.languages: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, request.get_header("User-agent") or ""))
        self.languages.append(request.get_header("Accept-language") or "")
        value = self.pages.get(url)
        if value is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value()
        return FakeResponse(value.encode("utf-8") if isinstance(value, str) else value)

    def urls(self) -> list[str]:
        return [url for url, _ua in self.requests]


def embed_url(kind: str, sid: str) -> str:
    return f"https://open.spotify.com/embed/{kind}/{sid}"


def page_url(kind: str, sid: str) -> str:
    return f"https://open.spotify.com/{kind}/{sid}"
