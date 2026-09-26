"""Spotify の公開プレイリスト・アルバムを取り込む (純粋な部品。GTK に依存しない)。

Spotify の Web API は、開発者アプリの持ち主が Premium でないと 403 ("Active premium subscription
required for the owner of the app") を返すので、無料プランでは cliamp の Spotify からライブラリを
読めない。そこで、ログインの要らない埋め込み用の頁 (https://open.spotify.com/embed/playlist/<id>、
/embed/album/<id>) の `<script id="__NEXT_DATA__">` にある曲の一覧を読み、cliamp の Web API だけの
Spotify と同じ「YouTube で探して鳴らす形」(パッチの external/spotify/bridge.go) にして、ローカルの
プレイリストに保存する。

- 読めるのは公開のプレイリストとアルバムだけ (非公開・「お気に入りの曲」は読めない)。
- 埋め込みの頁は 100 曲までしか載せない。100 曲あれば通常の頁 (open.spotify.com/<種類>/<id>。
  ブラウザでない User-Agent には meta の music:song_count などが載る) から全体の曲数を読み、
  切れていれば truncated と total で知らせる。
- 頁の形は Spotify の都合で変わりうる。形が違えば「頁の形が変わった」ことを日本語で言う。

ローカルのプレイリスト (TOML) には meta が残らない (PROTOCOL.md) ので、橋渡しの path から
Spotify の曲 ID を引く表 (ImportIndex) をアプリの側に持つ。AppContext がカタログと store の
曲をこの表で「元に戻し」(meta の spotify.id / spotify.bridge を付け直し)、アートワーク (Spotify の
oEmbed)・「リンクをコピー」・track_key が Web API だけの Spotify の曲と同じに働く。

HTTP (fetch_list) は GTK のスレッドで呼ばない (worker のスレッドから呼び、結果は GLib.idle_add で戻す)。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import http.client
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from . import VERSION, log
from .protocol import PLAYBACK_YOUTUBE, Track, fit_playlist_name

# 取り込める種類と表示名
KINDS = {"playlist": "プレイリスト", "album": "アルバム"}
# 埋め込みの頁が載せる曲の上限 (2026-09 の実測: 150 曲のプレイリストで 100 曲)
EMBED_TRACK_CAP = 100
EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
PAGE_URL = "https://open.spotify.com/{kind}/{id}"
FETCH_TIMEOUT = 15.0  # 1 回の操作 (接続・読み取り) の待ち
FETCH_DEADLINE = 30.0  # 1 つの頁の取得の全体の上限
MAX_PAGE_BYTES = 4 << 20  # 埋め込みの頁は 100 KB ほど、通常の頁は 500 KB ほど
# 埋め込みの頁はブラウザからの表示を前提にしているので、ブラウザらしい User-Agent で頼む
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0 Safari/537.36")
# 通常の頁は、ブラウザには中身の無いアプリの殻を、それ以外には meta 付きの頁を返す
PAGE_USER_AGENT = f"cliamp-music/{VERSION}"

# cliamp の橋渡し (bridge.go) と同じ meta の鍵
META_ID = "spotify.id"
META_BRIDGE = "spotify.bridge"
BRIDGE_PREFIX = "ytsearch1:"

# fullmatch で使う ("$" は末尾の改行の前にも当たるので、^…$ と match では "<ID>\n" が通ってしまう)
_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
# Go の strings.Fields が区切りに使う空白 (unicode.IsSpace)。Python の str.split() は
# U+001C〜U+001F も空白に数えるので、cliamp と同じ path にするために自前で持つ
_GO_SPACE = re.compile("[\t\n\v\f\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")
# 埋め込みの頁の曲の subtitle はアーティストを ",\u00a0" (カンマと改行しない空白) で繋いだもの。
# ふつうの ", " はアーティスト名の中身 ("Earth, Wind & Fire") なので区切りにしない
ARTIST_SEPARATOR = ",\u00a0"
_NEXT_DATA = re.compile(r"<script\b[^>]*\bid=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.S | re.I)
_SONG_COUNT = re.compile(r"<meta\b[^>]*\b(?:name|property)=[\"']music:song_count[\"'][^>]*>", re.I)
_META_CONTENT = re.compile(r"\bcontent=[\"']([^\"']*)[\"']", re.I)
_DESCRIPTION = re.compile(
    r"<meta\b[^>]*\b(?:name|property)=[\"'](?:og:description|description|twitter:description)[\"'][^>]*>", re.I)
_COUNT_WORDS = re.compile(r"(\d[\d,.\u00a0 ]*)\s*(?:items?|songs?|tracks?|曲)\b", re.I)
_URL_TOKEN = re.compile(r"(?<![\w:/.])(?:https?://|open\.spotify\.com/|spotify:)\S+", re.I)
# 文の終わりにリンクを置いたときに続く句読点・括弧・引用符 (リンクの一部ではない)
_TRAILING_PUNCT = ".,、。，．)）」』>］]!！?？\"'”’"
_SHORT_HOSTS = frozenset({"spotify.link", "spoti.fi", "spotify.app.link"})
_OTHER_KINDS = {
    "track": "曲のリンクです。プレイリストかアルバムのリンクを貼ってください",
    "artist": "アーティストのリンクです。プレイリストかアルバムのリンクを貼ってください",
    "show": "ポッドキャストのリンクです。プレイリストかアルバムのリンクを貼ってください",
    "episode": "ポッドキャストのリンクです。プレイリストかアルバムのリンクを貼ってください",
}
LIKED_SONGS_REASON = ("「お気に入りの曲」は Spotify の外からは読めません。公開のプレイリストに入れてから"
                      "取り込んでください")


class SpotifyImportError(Exception):
    """取り込めなかった理由。str() は利用者に見せる日本語の短い文。

    kind: "url" (リンクの形が違う) | "not-found" (無い・非公開) | "unavailable" (Spotify が頁を返さない) |
    "shape" (頁の形が変わった) | "network" (繋がらない・時間切れ) | "rate-limited" | "empty" (曲が無い)。
    detail は記録用の元の誤り (英語のこともある)。"""

    def __init__(self, message: str, kind: str = "error", detail: str = ""):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# リンク


@dataclass(frozen=True)
class SpotifyRef:
    """取り込むもの (種類 "playlist" / "album" と 22 文字の ID)。"""

    kind: str
    id: str

    @property
    def url(self) -> str:
        """利用者に見せる・保存する URL (open.spotify.com/<種類>/<id>)。"""
        return PAGE_URL.format(kind=self.kind, id=self.id)

    @property
    def embed_url(self) -> str:
        return EMBED_URL.format(kind=self.kind, id=self.id)

    @property
    def uri(self) -> str:
        return f"spotify:{self.kind}:{self.id}"

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)


def _pick_token(text: str) -> str:
    """貼り付けた文から Spotify のリンクらしい部分を選ぶ (「聴いて→https://open.spotify.com/…」など)。"""
    text = (text or "").strip()
    if not text:
        return ""
    first = text.split()[0]
    if first.lower().startswith(("http://", "https://", "spotify:", "open.spotify.com", "play.spotify.com")):
        return first.rstrip(_TRAILING_PUNCT)
    for match in _URL_TOKEN.finditer(text):
        token = match.group(0).rstrip(_TRAILING_PUNCT)
        if "spotify" in token.lower():
            return token
    return text


def _ref(kind: str, rest: list[str]) -> SpotifyRef:
    kind = kind.lower()
    if kind in KINDS:
        if not rest or not rest[0]:
            raise SpotifyImportError("リンクに ID がありません", "url")
        sid = rest[0]
        if not is_spotify_id(sid):
            raise SpotifyImportError("リンクの ID が Spotify の形 (英数字 22 文字) ではありません", "url")
        return SpotifyRef(kind, sid)
    if kind == "collection":
        raise SpotifyImportError(LIKED_SONGS_REASON, "url")
    reason = _OTHER_KINDS.get(kind)
    raise SpotifyImportError(reason or "プレイリストかアルバムのリンクではありません", "url")


def parse_spotify_url(text: str) -> SpotifyRef:
    """Spotify のプレイリスト・アルバムのリンクを読む。読めなければ SpotifyImportError (kind "url")。

    受け付ける形: https://open.spotify.com/playlist/<id> (http・スキーム無し・/intl-ja/ などの言語・
    /embed/・?si=… と #… 付きも)、spotify:playlist:<id>、spotify:album:<id>、
    spotify:user:<名前>:playlist:<id> (古い形)。"""
    text = _pick_token(text if isinstance(text, str) else "")
    if not text:
        raise SpotifyImportError("リンクを貼り付けてください", "url")
    lowered = text.lower()
    if lowered.startswith("spotify:"):
        parts = [unquote(p) for p in text.split(":")[1:]]
        if len(parts) >= 3 and parts[0].lower() == "user":
            if len(parts) >= 3 and parts[2].lower() == "collection":
                raise SpotifyImportError(LIKED_SONGS_REASON, "url")
            parts = parts[2:]  # spotify:user:<名前>:playlist:<id>
        if not parts:
            raise SpotifyImportError("Spotify のリンクの形ではありません", "url")
        return _ref(parts[0], parts[1:2])
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise SpotifyImportError("リンクの形ではありません", "url") from None
    if parts.scheme.lower() not in ("http", "https") or not host:
        raise SpotifyImportError("リンクの形ではありません", "url")
    if host in _SHORT_HOSTS:
        raise SpotifyImportError("短縮リンク (spotify.link) は使えません。Spotify の「共有」→「リンクをコピー」で"
                                 "出る open.spotify.com のリンクを貼ってください", "url")
    if host not in ("open.spotify.com", "play.spotify.com"):
        raise SpotifyImportError("Spotify のリンクではありません。open.spotify.com のプレイリストかアルバムの"
                                 "リンクを貼ってください", "url")
    segments = [unquote(s) for s in parts.path.split("/") if s]
    # 言語 (/intl-ja/)・埋め込み (/embed/)・古い形 (/user/<名前>/playlist/<id>) を外す
    while segments and (segments[0].lower().startswith("intl-") or segments[0].lower() in ("embed", "embed-podcast")):
        segments = segments[1:]
    if len(segments) >= 3 and segments[0].lower() == "user":
        if segments[2].lower() == "collection":
            raise SpotifyImportError(LIKED_SONGS_REASON, "url")
        segments = segments[2:]
    if not segments:
        raise SpotifyImportError("プレイリストかアルバムのリンクではありません", "url")
    return _ref(segments[0], segments[1:2])


def is_spotify_id(text: object) -> bool:
    """Spotify の ID の形 (英数字ちょうど 22 文字) か。"""
    return isinstance(text, str) and _SPOTIFY_ID.fullmatch(text) is not None


def check_spotify_url(text: str) -> tuple[SpotifyRef | None, str]:
    """入力中の確かめ: (SpotifyRef, "") か (None, 理由)。空の入力は (None, "") (まだ何も言わない)。"""
    if not (text or "").strip():
        return None, ""
    try:
        return parse_spotify_url(text), ""
    except SpotifyImportError as exc:
        return None, exc.message


# ---------------------------------------------------------------------------
# YouTube への橋渡し (cliamp の external/spotify/bridge.go と同じ形)


def go_fields(text: str) -> list[str]:
    """Go の strings.Fields (Unicode の空白の連なりで区切り、空の語を捨てる)。"""
    return [word for word in _GO_SPACE.split(text or "") if word]


def bridge_query(artists: Iterable[str], title: str) -> str:
    """"ytsearch1:<アーティストを空白で繋いだもの> <曲名>"。空白の連なり・改行・タブは 1 つの空白にし、
    空のアーティスト名は飛ばす (bridge.go の bridgeQuery)。"""
    words: list[str] = []
    for text in [*artists, title]:
        words.extend(go_fields(text))
    return BRIDGE_PREFIX + " ".join(words)


def split_artists(subtitle: str) -> list[str]:
    """埋め込みの頁の曲の subtitle をアーティストの並びに (",\\u00a0" で区切る)。"""
    if not subtitle:
        return []
    return subtitle.split(ARTIST_SEPARATOR)


def bridge_track(spotify_id: str, title: str, artists: Iterable[str], *, duration: int = 0,
                 album: str = "", year: int = 0, track_number: int = 0) -> Track:
    """cliamp の Web API だけの Spotify が返す「YouTube で探して鳴らす曲」と同じ Track (bridged())。

    path は bridge_query、artist はアーティスト名を ", " で繋いだもの (名前はそのまま)、duration は秒、
    meta は {"spotify.id": id, "spotify.bridge": "youtube"}。stream も unplayable も立てない。"""
    artists = list(artists)
    return Track(
        path=bridge_query(artists, title),
        title=title,
        artist=", ".join(artists),
        album=album,
        year=int(year or 0),
        track_number=int(track_number or 0),
        duration=max(0, int(duration or 0)),
        meta=((META_BRIDGE, PLAYBACK_YOUTUBE), (META_ID, spotify_id)),
    )


# ---------------------------------------------------------------------------
# 埋め込みの頁


@dataclass(frozen=True)
class ImportedList:
    """埋め込みの頁から読んだプレイリスト・アルバム。

    tracks は YouTube で探して鳴らす形 (bridge_track)。skipped は曲でない項目 (ポッドキャストの
    エピソード・手元のファイル) の数。truncated は埋め込みの頁の上限 (100 曲) で切れていること、
    total は Spotify での曲数 (分からなければ 0)。"""

    kind: str
    id: str
    name: str
    subtitle: str = ""
    cover_url: str = ""
    tracks: tuple[Track, ...] = ()
    truncated: bool = False
    total: int = 0
    skipped: int = 0
    listed: int = 0  # 頁に載っていた項目の数 (曲でないものを含む)

    @property
    def ref(self) -> SpotifyRef:
        return SpotifyRef(self.kind, self.id)

    @property
    def url(self) -> str:
        return self.ref.url

    @property
    def count(self) -> int:
        return len(self.tracks)

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)


def _shape(detail: str) -> SpotifyImportError:
    return SpotifyImportError("Spotify の頁の形が変わったため読めません (アプリの更新が要ります)", "shape", detail)


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def clean_text(text: str) -> str:
    """対になっていないサロゲート (U+D800〜U+DFFF) を U+FFFD に替える。JSON の "\\ud83d" のような
    壊れた絵文字は json.loads が Python の文字列にしてしまい、UTF-8 にできない (cliamp へ送る・GTK に
    渡す・表を保存するところで UnicodeEncodeError になる)。"""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def _text(value: Any) -> str:
    """頁や表の JSON から取った文字列 (文字列でなければ "")。壊れたサロゲートは U+FFFD にする。"""
    return clean_text(value) if isinstance(value, str) else ""


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) < 1e15:
        return int(value)
    return 0


def _best_image(items: Any, width_key: str) -> str:
    """絵の候補 (url と幅) から、いちばん大きなもの (幅の分からないものは後回し) の URL。"""
    best, best_width = "", -1
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"))
        if not url.startswith("https://"):
            continue
        width = _int(item.get(width_key))
        if width > best_width:
            best, best_width = url, width
    return best


def cover_of(entity: dict) -> str:
    """プレイリストは coverArt.sources、アルバムは visualIdentity.image の絵。無ければ ""。"""
    return (_best_image(_dig(entity, "coverArt", "sources"), "width")
            or _best_image(_dig(entity, "visualIdentity", "image"), "maxWidth"))


def _owner_of(entity: dict) -> str:
    subtitle = _text(entity.get("subtitle")).strip()
    if subtitle:
        return subtitle.replace(ARTIST_SEPARATOR, ", ")
    authors = entity.get("authors")
    names = [_text(a.get("name")).strip() for a in authors if isinstance(a, dict)] if isinstance(authors, list) else []
    return ", ".join(n for n in names if n)


def parse_embed_html(page: str, ref: SpotifyRef | None = None) -> ImportedList:
    """埋め込みの頁 (HTML) を読む。__NEXT_DATA__ の props.pageProps.state.data.entity の名前・
    副題・絵・trackList から、曲を YouTube で探して鳴らす形にする。

    頁が 404 (無い・非公開) なら kind "not-found"、ほかの失敗の頁なら "unavailable"、形が違えば
    "shape"、曲が 1 つも無ければ "empty" の SpotifyImportError。truncated は曲の数が上限 (100) に
    届いているかだけで決める (本当に切れているかは fetch_list が通常の頁の曲数で確かめる)。"""
    if not isinstance(page, str):
        raise _shape("頁が文字列ではありません")
    match = _NEXT_DATA.search(page)
    if match is None:
        raise _shape("__NEXT_DATA__ がありません")
    try:
        data = json.loads(match.group(1))
    except ValueError as exc:
        raise _shape(f"__NEXT_DATA__ を読めません: {exc}") from None
    props = _dig(data, "props", "pageProps")
    if not isinstance(props, dict):
        raise _shape("props.pageProps がありません")
    status = _int(props.get("status"))
    if status == 404:
        raise SpotifyImportError("見つかりません。リンクが正しいか、公開されているかを確かめてください "
                                 "(非公開のプレイリストは取り込めません)", "not-found", f"status {status}")
    if status >= 400:
        raise SpotifyImportError(f"Spotify が頁を返しませんでした ({status})。しばらくしてからもう一度試してください",
                                 "unavailable", f"status {status}")
    entity = _dig(props, "state", "data", "entity")
    if not isinstance(entity, dict):
        raise _shape("state.data.entity がありません")
    items = entity.get("trackList")
    if not isinstance(items, list):
        raise _shape("trackList がありません")
    kind = _text(entity.get("type")).lower()
    if kind not in KINDS:
        kind = ref.kind if ref is not None else "playlist"
    sid = _text(entity.get("id"))
    if not is_spotify_id(sid):
        sid = ref.id if ref is not None else ""
    name = (_text(entity.get("name")) or _text(entity.get("title"))).strip()
    album = name if kind == "album" else ""
    tracks: list[Track] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        uri = _text(item.get("uri"))
        track_id = uri[len("spotify:track:"):] if uri.startswith("spotify:track:") else ""
        title = _text(item.get("title"))
        artists = split_artists(_text(item.get("subtitle")))
        if not is_spotify_id(track_id) or bridge_query(artists, title) == BRIDGE_PREFIX:
            skipped += 1  # エピソード (spotify:episode:)・手元のファイル (spotify:local:)・名前の無いもの
            continue
        duration_ms = _int(item.get("duration"))
        tracks.append(bridge_track(track_id, title, artists, duration=duration_ms // 1000 if duration_ms > 0 else 0,
                                   album=album))
    if not tracks:
        raise SpotifyImportError(f"この{KINDS.get(kind, kind)}には取り込める曲がありません", "empty")
    return ImportedList(
        kind=kind,
        id=sid,
        name=name or KINDS.get(kind, kind),
        subtitle=_owner_of(entity),
        cover_url=cover_of(entity),
        tracks=tuple(tracks),
        truncated=len(items) >= EMBED_TRACK_CAP,
        total=0,
        skipped=skipped,
        listed=len(items),
    )


def _number(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits and len(digits) <= 7 else 0


def parse_total_count(page: str) -> int:
    """通常の頁 (ブラウザでない User-Agent への答え) から全体の曲数。<meta name="music:song_count">、
    無ければ説明 ("Playlist · Spotify · 150 items · 12M saves"、"… · 18 songs") の数。分からなければ 0。"""
    if not isinstance(page, str):
        return 0
    match = _SONG_COUNT.search(page)
    if match is not None:
        content = _META_CONTENT.search(match.group(0))
        if content is not None and _number(content.group(1)):
            return _number(content.group(1))
    for tag in _DESCRIPTION.finditer(page):
        content = _META_CONTENT.search(tag.group(0))
        if content is None:
            continue
        found = _COUNT_WORDS.search(html_lib.unescape(content.group(1)))
        if found is not None and _number(found.group(1)):
            return _number(found.group(1))
    return 0


def with_total(result: ImportedList, total: int) -> ImportedList:
    """全体の曲数を足す。上限まで載っていた頁で、全体が載っていた数より多ければ切れている。
    全体が分からなければ (0)、上限まで載っていたら切れているものとする。"""
    if result.listed < EMBED_TRACK_CAP:
        return dc_replace(result, truncated=False, total=total if total >= result.listed else 0)
    if total <= 0:
        return dc_replace(result, truncated=True, total=0)
    return dc_replace(result, truncated=total > result.listed, total=total)


# ---------------------------------------------------------------------------
# HTTP (worker のスレッドから呼ぶ)


def urlopen(request, timeout):
    """urllib.request.urlopen の包み (試験・撮影で差し替える)。"""
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_text(url: str, *, user_agent: str, timeout: float = FETCH_TIMEOUT, limit: int = MAX_PAGE_BYTES,
               language: str = "en") -> str:
    """頁を取って文字列にする。失敗は SpotifyImportError (network / not-found / rate-limited / unavailable)。

    Accept-Language は英語にする。日本語を頼むとアーティスト名が「マイケル・ジャクソン」のように訳され
    (2026-09 の実測)、cliamp が Web API から作る橋渡しの path ("Michael Jackson …") と違ってしまう
    (YouTube でも原語の名前のほうが当たる)。"""
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": language,
    })
    started = time.monotonic()
    try:
        response = urlopen(request, timeout)
        with response:
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() - started > FETCH_DEADLINE:
                    raise TimeoutError("全体の時間切れ")
                chunk = response.read(min(1 << 16, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise SpotifyImportError("Spotify の頁が大きすぎます", "shape", f"{size} バイトを超えました")
            headers = getattr(response, "headers", None)
            charset = headers.get_content_charset() if headers is not None and hasattr(headers, "get_content_charset") \
                else None
    except SpotifyImportError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise SpotifyImportError("見つかりません。リンクが正しいか、公開されているかを確かめてください",
                                     "not-found", f"HTTP {exc.code}") from None
        if exc.code == 429:
            raise SpotifyImportError("Spotify から回数の制限を受けています。しばらく待ってから、もう一度試してください",
                                     "rate-limited", "HTTP 429") from None
        raise SpotifyImportError(f"Spotify が頁を返しませんでした (HTTP {exc.code})。しばらくしてからもう一度"
                                 "試してください", "unavailable", f"HTTP {exc.code}") from None
    except (TimeoutError, socket.timeout) as exc:
        raise SpotifyImportError("Spotify の応答がありません (時間切れ)", "network", str(exc)) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise SpotifyImportError("Spotify の応答がありません (時間切れ)", "network", str(reason)) from None
        raise SpotifyImportError("ネットワークに繋がりません", "network", str(reason)) from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise SpotifyImportError("ネットワークに繋がりません", "network", str(exc)) from None
    data = b"".join(chunks)
    try:
        return data.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def fetch_list(target: SpotifyRef | str, *, timeout: float = FETCH_TIMEOUT) -> ImportedList:
    """リンク (か SpotifyRef) のプレイリスト・アルバムを取って読む (ネットワークを待つので worker で)。

    埋め込みの頁の曲が上限 (100) に届いていれば、通常の頁から全体の曲数を読んで truncated と
    total を決める (曲数の頁が読めなくても取り込みは続ける)。"""
    ref = target if isinstance(target, SpotifyRef) else parse_spotify_url(target)
    page = fetch_text(ref.embed_url, user_agent=BROWSER_USER_AGENT, timeout=timeout)
    result = parse_embed_html(page, ref)
    if result.listed >= EMBED_TRACK_CAP:
        total = 0
        try:
            total = parse_total_count(fetch_text(ref.url, user_agent=PAGE_USER_AGENT, timeout=timeout))
        except SpotifyImportError as exc:
            log(f"{ref.url} の曲数を読めません: {exc.detail or exc.message}")
        result = with_total(result, total)
    return result


def summary_line(result: ImportedList) -> str:
    """「プレイリスト · Spotify · 50 曲」(取り込む前の見せ方)。"""
    parts = [result.kind_label]
    if result.subtitle:
        parts.append(result.subtitle)
    if result.truncated and result.total > result.count:
        parts.append(f"{result.count} 曲 (全 {result.total} 曲)")
    else:
        parts.append(f"{result.count} 曲")
    return " · ".join(parts)


RESERVED_NAMES = frozenset({"Recently Played"})


def playlist_name_for(result: ImportedList) -> str:
    """取り込むローカルのプレイリストの既定の名前 (Spotify での名前)。cliamp のプレイリスト名に使えない
    "/" と "\\" は全角に替え、空白の連なりは 1 つにする。予約名 ("Recently Played") と "." / ".." は避け、
    長すぎる名前 (名前 + ".toml" が 255 バイトを超えるもの) は文字の切れ目で縮める。"""
    name = " ".join(go_fields(clean_text(result.name).replace("/", "\uff0f").replace("\\", "\uff3c")))
    if not name or name in (".", ".."):
        name = f"Spotify の{result.kind_label}"
    if name in RESERVED_NAMES:
        name += " (Spotify)"
    return fit_playlist_name(name)


def suggest_name(name: str, existing: Iterable[str]) -> str:
    """name と重ならない名前 (「名前 2」「名前 3」…)。重ならなければ name のまま。番号を足すと長さの上限を
    超えるときは name の側を縮める。"""
    taken = set(existing)
    if name not in taken:
        return name
    n = 2
    while fit_playlist_name(name, f" {n}") in taken:
        n += 1
    return fit_playlist_name(name, f" {n}")


TRUNCATED_NOTE = "Spotify の公開ページは 100 曲までです。最初の 100 曲を取り込みました"


def truncated_note(result: "ImportedList | ImportRecord") -> str:
    """切れていたときの知らせ (切れていなければ "")。"""
    if not result.truncated:
        return ""
    count = result.count
    if count == EMBED_TRACK_CAP:
        text = TRUNCATED_NOTE
    else:
        text = f"Spotify の公開ページは {EMBED_TRACK_CAP} 曲までです。読めた {count} 曲を取り込みました"
    return text + (f" (全 {result.total} 曲)" if result.total > count else "")


# ---------------------------------------------------------------------------
# 取り込んだものの表 (アプリの側の保存)


INDEX_VERSION = 1
MAX_TRACKS = 10000  # 覚える橋渡しの path の数 (古いものから忘れる)
MAX_PLAYLISTS = 500
PRUNE_GRACE = 120.0  # 取り込んだばかりのものは一覧に無くても忘れない (一覧の取得と行き違う)
BACKUP_DIR = "playlist-backups"  # 置き換える前の曲の控え (表と同じディレクトリの下)
MAX_BACKUPS = 20


@dataclass(frozen=True)
class ImportRecord:
    """取り込んだローカルのプレイリスト 1 つの記録。"""

    name: str
    url: str
    kind: str = "playlist"
    id: str = ""
    title: str = ""  # Spotify での名前
    subtitle: str = ""  # 作った人 (アルバムならアーティスト)
    cover_url: str = ""
    imported_at: str = ""  # RFC3339 (UTC)
    count: int = 0
    truncated: bool = False
    total: int = 0

    def to_json(self) -> dict:
        out = {"url": self.url, "kind": self.kind, "id": self.id, "title": self.title, "subtitle": self.subtitle,
               "cover": self.cover_url, "imported_at": self.imported_at, "count": self.count}
        if self.truncated:
            out["truncated"] = True
        if self.total:
            out["total"] = self.total
        return out

    @classmethod
    def from_json(cls, name: str, data: Any) -> "ImportRecord | None":
        if not isinstance(data, dict):
            return None
        url = _text(data.get("url"))
        try:
            ref = parse_spotify_url(url)
        except SpotifyImportError:
            return None
        return cls(name=name, url=ref.url, kind=ref.kind, id=ref.id, title=_text(data.get("title")),
                   subtitle=_text(data.get("subtitle")), cover_url=_text(data.get("cover")),
                   imported_at=_text(data.get("imported_at")), count=max(0, _int(data.get("count"))),
                   truncated=data.get("truncated") is True, total=max(0, _int(data.get("total"))))

    @property
    def kind_label(self) -> str:
        return KINDS.get(self.kind, self.kind)

    def imported_at_epoch(self) -> float:
        try:
            return datetime.strptime(self.imported_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0.0


def default_index_path(state_path: str | None = None) -> str:
    """表の置き場。GuiState の state.json と同じディレクトリ (既定 ~/.local/state/cliamp-music)。"""
    if state_path:
        return os.path.join(os.path.dirname(os.path.abspath(state_path)), "spotify-imports.json")
    base = os.environ.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "cliamp-music", "spotify-imports.json")


def _write_json(path: str, payload: Any, *, prefix: str) -> None:
    """payload を JSON にして path に書く (同じディレクトリの一時ファイルに書いてから置き換える)。
    UTF-8 にできない文字 (壊れたサロゲート) が紛れていても書けるよう、そのときは ASCII で書く。"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rfc3339(now: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _IndexData:
    tracks: dict[str, str] = field(default_factory=dict)  # 橋渡しの path → Spotify の曲 ID (古い順)
    playlists: dict[str, ImportRecord] = field(default_factory=dict)


class ImportIndex:
    """取り込んだプレイリストと、橋渡しの path → Spotify の曲 ID の表 (JSON 1 つ)。

    ファイルは初めて使うときに読む (使わない試験や起動で読まない)。壊れていたら空で始める。
    書き込みは一時ファイルに書いてから置き換える。大きさは曲 MAX_TRACKS・プレイリスト
    MAX_PLAYLISTS まで (古いものから忘れる)。main loop から使う (restore は読むだけ)。"""

    def __init__(self, path: str | None = None):
        self.path = path or default_index_path()
        self._data: _IndexData | None = None
        self._lock = threading.Lock()

    # --- 読み書き -----------------------------------------------------------------

    def _loaded(self) -> _IndexData:
        data = self._data
        if data is not None:
            return data
        with self._lock:
            if self._data is None:
                self._data = self._read()
            return self._data

    def _read(self) -> _IndexData:
        data = _IndexData()
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return data
        except (OSError, ValueError) as exc:
            log(f"{self.path} を読めないので空で始めます: {exc}")
            return data
        if not isinstance(raw, dict):
            log(f"{self.path} の形が違うので空で始めます")
            return data
        tracks = raw.get("tracks")
        if isinstance(tracks, dict):
            for path, sid in tracks.items():
                if isinstance(path, str) and path.startswith(BRIDGE_PREFIX) and is_spotify_id(sid):
                    data.tracks[clean_text(path)] = sid
        playlists = raw.get("playlists")
        if isinstance(playlists, dict):
            for name, item in playlists.items():
                name = _text(name)
                record = ImportRecord.from_json(name, item) if name else None
                if record is not None:
                    data.playlists[name] = record
        self._trim(data)
        return data

    @staticmethod
    def _trim(data: _IndexData) -> None:
        while len(data.tracks) > MAX_TRACKS:
            data.tracks.pop(next(iter(data.tracks)))
        while len(data.playlists) > MAX_PLAYLISTS:
            data.playlists.pop(next(iter(data.playlists)))

    def save(self) -> bool:
        """保存する。失敗してもアプリは続ける (False を返してログに書く)。"""
        data = self._loaded()
        payload = {
            "version": INDEX_VERSION,
            "tracks": dict(data.tracks),
            "playlists": {name: record.to_json() for name, record in data.playlists.items()},
        }
        try:
            _write_json(self.path, payload, prefix=".spotify-imports-")
            return True
        except (OSError, TypeError, ValueError) as exc:
            log(f"{self.path} に保存できません: {exc}")
            return False

    # --- 曲 -------------------------------------------------------------------------

    def lookup(self, path: str) -> str | None:
        """橋渡しの path の Spotify の曲 ID (覚えていなければ None)。"""
        if not isinstance(path, str) or not path.startswith(BRIDGE_PREFIX):
            return None
        return self._loaded().tracks.get(path)

    def restore(self, track: Track | None) -> Track | None:
        """meta を失った橋渡しの曲 (ローカルのプレイリスト・履歴・TUI で読み込んだリスト) に、覚えている
        Spotify の曲 ID を付け直す (meta の spotify.id と spotify.bridge)。それ以外はそのまま返す。"""
        if track is None or not track.path.startswith(BRIDGE_PREFIX) or track.meta_get(META_ID):
            return track
        sid = self.lookup(track.path)
        if sid is None:
            return track
        meta = dict(track.meta)
        meta[META_ID] = sid
        meta.setdefault(META_BRIDGE, PLAYBACK_YOUTUBE)
        return dc_replace(track, meta=meta)

    def restore_all(self, tracks: Iterable[Track]) -> list[Track]:
        tracks = list(tracks)
        if not any(t.path.startswith(BRIDGE_PREFIX) for t in tracks):
            return tracks
        return [self.restore(t) for t in tracks]

    def known_count(self, tracks: Iterable[Track]) -> int:
        """tracks のうち、取り込んだ曲 (橋渡しの path がこの表にあるもの) の数。"""
        known = self._loaded().tracks
        return sum(1 for t in tracks if isinstance(t.path, str) and t.path in known)

    # --- プレイリスト -------------------------------------------------------------------

    def get(self, name: str) -> ImportRecord | None:
        """取り込んだローカルのプレイリストの記録 (無ければ None)。"""
        return self._loaded().playlists.get(name) if name else None

    def names(self) -> list[str]:
        return list(self._loaded().playlists)

    def record(self, name: str, result: ImportedList, *, now: float | None = None, save: bool = True) -> ImportRecord:
        """取り込んだ (ローカルのプレイリスト name に result の曲を保存した) ことを覚えて保存する。"""
        data = self._loaded()
        for track in result.tracks:
            sid = track.meta_get(META_ID)
            if sid and track.path.startswith(BRIDGE_PREFIX):
                data.tracks.pop(track.path, None)  # 入れ直して新しい側へ
                data.tracks[track.path] = sid
        record = ImportRecord(name=name, url=result.url, kind=result.kind, id=result.id, title=result.name,
                              subtitle=result.subtitle, cover_url=result.cover_url, imported_at=_rfc3339(now),
                              count=result.count, truncated=result.truncated, total=result.total)
        data.playlists.pop(name, None)
        data.playlists[name] = record
        self._trim(data)
        if save:
            self.save()
        return record

    def confirm(self, name: str, tracks: Iterable[Track], *, now: float | None = None) -> ImportRecord | None:
        """いまの曲 tracks から見て、ローカルのプレイリスト name が取り込んだもののままなら、その記録。

        取り込んだもののまま = 記録があり、曲の半分以上が取り込んだ曲 (known_count)。記録は名前だけで
        引くので、TUI で消して同じ名前で作り直したもの・自分の曲の方が多くなったものは別物として記録を
        忘れ、None を返す (取り込んで PRUNE_GRACE 秒のうちは忘れない。取り込む前に頼んだ曲の一覧が後から
        届くことがある)。曲が空なら決めない (None。記録は残す)。"""
        record = self.get(name)
        tracks = list(tracks)
        if record is None or not tracks:
            return None
        known = self.known_count(tracks)
        if known > 0 and known * 2 >= len(tracks):
            return record
        now = time.time() if now is None else now
        if now - record.imported_at_epoch() > PRUNE_GRACE:
            log(f"「{name}」の曲は Spotify から取り込んだものではなくなったので、取り込みの記録を忘れます "
                f"({known}/{len(tracks)} 曲)")
            self.forget(name)
        return None

    # --- 置き換える前の控え ----------------------------------------------------------------

    @property
    def backup_dir(self) -> str:
        return os.path.join(os.path.dirname(self.path), BACKUP_DIR)

    def backup(self, name: str, tracks: Iterable[Track], *, now: float | None = None) -> str | None:
        """ローカルのプレイリスト name を置き換える前の曲を控える (backup_dir/<時刻>-<名前の要約>.json。
        中身は名前と、cliamp の playlist_add にそのまま渡せる曲の並び)。置き換えに失敗して戻すことも
        できなかったときに、曲を失わないため。書けたらその path、書けなければ None (ログに書く)。
        MAX_BACKUPS を超えたら古いものから消す。"""
        moment = time.time() if now is None else now
        digest = hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:10]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(moment))
        path = os.path.join(self.backup_dir, f"{stamp}-{digest}.json")
        try:
            payload = {"version": 1, "name": name, "saved_at": _rfc3339(moment),
                       "tracks": [t.to_wire() for t in tracks]}
            _write_json(path, payload, prefix=".backup-")
        except (OSError, TypeError, ValueError) as exc:
            log(f"「{name}」の曲を控えられません: {exc}")
            return None
        self._trim_backups()
        return path

    def drop_backup(self, path: str | None) -> None:
        """控えが要らなくなった (置き換えられた・戻せた)。"""
        if not path:
            return
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log(f"{path} を消せません: {exc}")

    def backups(self) -> list[str]:
        """残っている控え (古い順)。"""
        try:
            names = sorted(n for n in os.listdir(self.backup_dir) if n.endswith(".json") and not n.startswith("."))
        except OSError:
            return []
        return [os.path.join(self.backup_dir, n) for n in names]

    def _trim_backups(self) -> None:
        backups = self.backups()
        for path in backups[:max(0, len(backups) - MAX_BACKUPS)]:
            self.drop_backup(path)

    def forget(self, name: str) -> bool:
        """プレイリストの記録を忘れる (曲の ID は残す。別のプレイリストに入れた曲の絵のため)。"""
        data = self._loaded()
        if data.playlists.pop(name, None) is None:
            return False
        self.save()
        return True

    def prune(self, existing: Iterable[str], *, now: float | None = None, grace: float = PRUNE_GRACE) -> list[str]:
        """いまのローカルのプレイリストに無い記録を忘れる (TUI で消した・名前を変えたもの)。取り込んで
        grace 秒のうちのものは残す (取り込む前に頼んだ一覧が後から届くことがあるため)。忘れた名前を返す。"""
        data = self._loaded()
        if not data.playlists:
            return []
        keep = set(existing)
        now = time.time() if now is None else now
        gone = [name for name, record in data.playlists.items()
                if name not in keep and now - record.imported_at_epoch() > grace]
        for name in gone:
            del data.playlists[name]
        if gone:
            self.save()
        return gone
