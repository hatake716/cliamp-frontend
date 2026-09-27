"""cliamp の IPC (PROTOCOL.md) を扱う純粋な部品。

GTK・GLib に依存しない。データ型、応答の解析、要求の組み立て、表示用の
小道具 (時間・音量の換算、相対時刻など) だけを置き、tests/ から画面なしで試す。

cliamp (Go) の応答は `omitempty` なので、0・空文字・false は省かれて届く。
ここでの解析は「欠けた数値は 0、欠けた真偽は false」として読む。
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlsplit

# 拡張 IPC の版。capabilities と status の api に入る。
API_VERSION = 1

# パッチを当てていない cliamp 1.50.0 が知っているコマンド。
LEGACY_COMMANDS = frozenset({
    "play", "pause", "toggle", "stop", "next", "prev", "volume", "seek",
    "load", "queue", "theme", "vis", "shuffle", "repeat", "mono", "speed",
    "eq", "device", "status", "bands", "plugin.call", "plugin.commands",
})

# 状態系: TUI の Update を通る。すぐ答えるので待ちは短く (5 秒)。
STATE_COMMANDS = frozenset(LEGACY_COMMANDS - {"plugin.call"} | {
    "capabilities", "seek_to", "playlist", "play_index", "replace",
    "enqueue", "queue_edit", "remove",
})

# カタログ系: ネットワークや yt-dlp を待つ。cliamp 側の上限 120 秒より長く待つ。
# plugin.call もダウンロードなどで長くかかるのでこちらに入れる。
CATALOG_COMMANDS = frozenset({
    "providers", "playlists", "tracks", "search", "load_provider", "lyrics",
    "history", "playlist_add", "playlist_delete", "playlist_remove_track",
    "plugin.call",
})

# 拡張 IPC (api 1) のすべてのコマンド。
ALL_COMMANDS = STATE_COMMANDS | CATALOG_COMMANDS

# 状態を変えるコマンド。cliamp は接続ごとに goroutine で処理するので、別々の接続で
# 続けて送ると届く順が入れ替わる。CliampClient はこれらを 1 本の列で順に (前の応答を
# 待ってから) 送る。device は "list" のときだけ読み取り (client が見分ける)。
ORDERED_COMMANDS = frozenset({
    "play", "pause", "toggle", "stop", "next", "prev", "seek", "seek_to", "volume",
    "shuffle", "repeat", "mono", "speed", "eq", "device", "play_index", "replace",
    "enqueue", "queue_edit", "remove", "load", "queue",
})

# イコライザの 10 本の帯域 (cliamp の eq_presets.go と同じ並び)。
EQ_BANDS = ("70", "180", "320", "600", "1K", "3K", "6K", "12K", "14K", "16K")

VOLUME_MIN_DB = -30.0
VOLUME_MAX_DB = 6.0
EQ_MIN_DB = -12.0
EQ_MAX_DB = 12.0
REPEAT_MODES = ("off", "all", "one")

# 添字で指した曲が要求の path と違う (手元の写しが古い) ときの誤り (パッチの ErrStale)。
STALE = "stale"

# local プロバイダーが履歴から作る仮想のプレイリスト (cliamp の history.PlaylistName)。
# 書き込めないので「プレイリストに追加」の候補からは外す。
RECENTLY_PLAYED = "Recently Played"

# ProviderInfo の playback: 曲を YouTube で探して鳴らす (Spotify の接続が Web API だけのとき)。
PLAYBACK_YOUTUBE = "youtube"

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_YT_HOSTS = frozenset({
    "youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com",
})


# ---------------------------------------------------------------------------
# 型の変換 (JSON の値は信用しない。壊れていても 0 / "" / False で読む)


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else 0.0
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return 0.0
        return number if math.isfinite(number) else 0.0
    return 0.0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() == "true"
    return False


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _normalize_meta(meta: Any) -> tuple[tuple[str, str], ...]:
    if not meta:
        return ()
    items: Iterable
    if isinstance(meta, dict):
        items = meta.items()
    else:
        items = meta
    pairs = []
    for item in items:
        try:
            key, value = item
        except (TypeError, ValueError):
            continue
        pairs.append((_as_str(key), _as_str(value)))
    # 並びを揃えて、同じ中身の Track が等しく (同じ hash に) なるようにする。
    return tuple(sorted(pairs))


# ---------------------------------------------------------------------------
# URL の小道具


def youtube_id_of(url: str) -> str | None:
    """YouTube の URL から 11 文字の動画 ID を取り出す。違えば None。

    watch?v= (www / m / music.youtube.com)、youtu.be/<id>、/shorts/<id>、
    /embed/<id>、/live/<id> を受け付ける。
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if host.startswith("www."):
        host = host[4:]
    candidate = ""
    if host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/", 1)[0]
    elif host in _YT_HOSTS:
        path = parts.path.rstrip("/")
        if path == "/watch":
            candidate = parse_qs(parts.query).get("v", [""])[0]
        else:
            segments = path.strip("/").split("/")
            if len(segments) >= 2 and segments[0] in ("shorts", "embed", "live", "v"):
                candidate = segments[1]
    return candidate if _YT_ID.match(candidate or "") else None


def spotify_id_of(path: str) -> str | None:
    """`spotify:track:<id>` か open.spotify.com/track/<id> から曲の ID を取り出す。"""
    if not path or not isinstance(path, str):
        return None
    path = path.strip()
    if path.startswith("spotify:track:"):
        candidate = path[len("spotify:track:"):]
        return candidate if _SPOTIFY_ID.match(candidate) else None
    try:
        parts = urlsplit(path)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and host == "open.spotify.com":
        segments = [s for s in parts.path.split("/") if s]
        # /intl-ja/track/<id> のような言語つきの形もある。
        if "track" in segments:
            i = segments.index("track")
            if i + 1 < len(segments) and _SPOTIFY_ID.match(segments[i + 1]):
                return segments[i + 1]
    return None


def is_url(path: str) -> bool:
    """http(s) の URL か。cliamp の IsURL と違い yt-dlp の検索式は含めない。"""
    return isinstance(path, str) and (path.startswith("http://") or path.startswith("https://"))


def mix_url(video_id: str) -> str:
    """YouTube の曲から作るミックス (ステーション) の URL。"""
    return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"


# ---------------------------------------------------------------------------
# データ型


@dataclass(frozen=True)
class Track:
    """PROTOCOL の TrackInfo。凍結してあるので辞書の鍵や集合に使える。

    meta は (key, value) の組を鍵の順に並べたもの。dict を渡してもよい
    (作るときに並べ直す)。
    """

    path: str
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: int = 0
    track_number: int = 0
    duration: int = 0
    stream: bool = False
    live: bool = False
    feed: bool = False
    unplayable: bool = False
    bookmark: bool = False
    meta: tuple[tuple[str, str], ...] = ()
    queued: int = 0
    played_at: str = ""
    # path が UTF-8 でない (Shift_JIS のファイル名など) ときだけ、cliamp が付ける元のバイト列
    # (base64)。path は表示用 (U+FFFD 入り)。送り返すときも付け、手元のファイルはこれで開く
    path_raw: str = ""

    def __post_init__(self) -> None:
        normalized = _normalize_meta(self.meta)
        if normalized != self.meta:
            object.__setattr__(self, "meta", normalized)

    # --- 派生 ---------------------------------------------------------------

    @property
    def display_title(self) -> str:
        """表示用の曲名。題が無ければ path の末尾 (ファイル名なら拡張子を除く)。"""
        if self.title.strip():
            return self.title
        path = self.path or ""
        if youtube_id_of(path):
            return "YouTube の動画"
        if is_url(path):
            parts = urlsplit(path)
            tail = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
            return tail or parts.hostname or path
        if path.startswith("file://"):
            path = unquote(urlsplit(path).path)
        tail = path.rstrip("/").rsplit("/", 1)[-1]
        stem, dot, ext = tail.rpartition(".")
        if dot and stem and 1 <= len(ext) <= 5 and " " not in ext:
            return stem
        return tail or path

    @property
    def subtitle(self) -> str:
        """「アーティスト — アルバム」。片方だけならそれだけ、無ければ空。"""
        return " — ".join(part for part in (self.artist.strip(), self.album.strip()) if part)

    def meta_get(self, key: str, default: str = "") -> str:
        for k, v in self.meta:
            if k == key:
                return v
        return default

    @property
    def youtube_id(self) -> str | None:
        """YouTube の動画 ID (path が YouTube の URL のときだけ)。yt-dlp の検索式
        ("ytsearch1:…"。YouTube で探して鳴らす Spotify の曲など) は動画が決まっていないので None。"""
        return youtube_id_of(self.path)

    @property
    def spotify_id(self) -> str | None:
        """Spotify の曲 ID。path (spotify:track: / open.spotify.com) か、meta の "spotify.id"
        (YouTube で探して鳴らす Spotify の曲は path が "ytsearch1:…" で、ID は meta にだけある)。"""
        sid = spotify_id_of(self.path)
        if sid:
            return sid
        meta_id = self.meta_get("spotify.id").strip()
        return meta_id if _SPOTIFY_ID.match(meta_id) else None

    @property
    def is_local_file(self) -> bool:
        """手元のファイルか (URL・spotify: ・yt-dlp の検索式ではない)。"""
        path = self.path or ""
        if path.startswith("file://"):
            return True
        return bool(path) and not _SCHEME.match(path)

    @property
    def local_path(self) -> str:
        """手元のファイルの絶対パス。ファイルでなければ空。

        path_raw があれば元のバイト列を戻したもの (UTF-8 でない名前は os.fsdecode の
        surrogateescape の文字列になり、open() でそのまま開ける)。"""
        if not self.is_local_file:
            return ""
        if self.path_raw:
            try:
                return os.fsdecode(base64.b64decode(self.path_raw, validate=True))
            except (binascii.Error, ValueError):
                pass
        if self.path.startswith("file://"):
            return unquote(urlsplit(self.path).path)
        return self.path

    @property
    def web_url(self) -> str | None:
        """「リンクをコピー」「ブラウザで開く」に使う URL。無ければ None。

        Spotify の曲 ID があればその曲の頁 (YouTube で探して鳴らす曲も、探した動画ではなく
        Spotify の曲を指す)。無ければ http(s) の path そのもの。"""
        sid = self.spotify_id
        if sid:
            return f"https://open.spotify.com/track/{sid}"
        if is_url(self.path):
            return self.path
        return None

    # --- 往復 ---------------------------------------------------------------

    def to_wire(self) -> dict:
        """PROTOCOL の TrackInfo (空の値は省く)。from_wire で元に戻る。"""
        wire: dict[str, Any] = {"path": self.path}
        for name in ("title", "artist", "album", "genre"):
            value = getattr(self, name)
            if value:
                wire[name] = value
        for name in ("year", "track_number", "duration"):
            value = getattr(self, name)
            if value:
                wire[name] = value
        for name in ("stream", "live", "feed", "unplayable", "bookmark"):
            if getattr(self, name):
                wire[name] = True
        if self.meta:
            wire["meta"] = dict(self.meta)
        if self.queued:
            wire["queued"] = self.queued
        if self.played_at:
            wire["played_at"] = self.played_at
        if self.path_raw:
            wire["path_raw"] = self.path_raw
        return wire

    @classmethod
    def from_wire(cls, d: Any) -> "Track":
        d = _as_dict(d)
        return cls(
            path=_as_str(d.get("path")),
            title=_as_str(d.get("title")),
            artist=_as_str(d.get("artist")),
            album=_as_str(d.get("album")),
            genre=_as_str(d.get("genre")),
            year=_as_int(d.get("year")),
            track_number=_as_int(d.get("track_number")),
            duration=max(0, _as_int(d.get("duration"))),
            stream=_as_bool(d.get("stream")),
            live=_as_bool(d.get("live")),
            feed=_as_bool(d.get("feed")),
            unplayable=_as_bool(d.get("unplayable")),
            bookmark=_as_bool(d.get("bookmark")),
            meta=_normalize_meta(_as_dict(d.get("meta"))),
            queued=_as_int(d.get("queued")),
            played_at=_as_str(d.get("played_at")),
            path_raw=_as_str(d.get("path_raw")),
        )


def is_youtube_bridge(track: Track | None) -> bool:
    """YouTube で探して鳴らす Spotify の曲か (meta "spotify.bridge" が "youtube")。

    Spotify の接続が Web API だけのとき (無料プランや、librespot に断られる自前の client_id)、
    cliamp の Spotify は曲を path "ytsearch1:<アーティスト> <曲名>" で返し、曲名などは Spotify の
    まま、曲 ID を meta "spotify.id" に入れる。"""
    return track is not None and track.meta_get("spotify.bridge").strip().lower() == "youtube"


@dataclass(frozen=True)
class Source:
    """いま読み込まれているリストの出どころ (PROTOCOL の SourceInfo)。"""

    provider: str = ""
    id: str = ""
    name: str = ""

    def to_wire(self) -> dict:
        return {k: v for k, v in (("provider", self.provider), ("id", self.id), ("name", self.name)) if v}

    @classmethod
    def from_wire(cls, d: Any) -> "Source":
        d = _as_dict(d)
        return cls(_as_str(d.get("provider")), _as_str(d.get("id")), _as_str(d.get("name")))

    def __bool__(self) -> bool:
        return bool(self.provider or self.id or self.name)


def _default_eq() -> list[float]:
    return [0.0] * len(EQ_BANDS)


@dataclass
class Status:
    """status の応答。state は "playing" | "paused" | "stopped" | "offline"。

    seq と stamp は応答には無く、CliampClient が付ける:
    seq は状態の問い合わせの通し番号、stamp は受け取った時刻 (time.monotonic)。
    """

    state: str = "offline"
    track: Track | None = None
    position: float = 0.0
    duration: float = 0.0
    volume: float = 0.0
    index: int = 0
    total: int = 0
    shuffle: bool = False
    repeat: str = "off"
    mono: bool = False
    speed: float = 1.0
    eq_preset: str = ""
    eq: list[float] = field(default_factory=_default_eq)
    gen: int = 0
    source: Source = field(default_factory=Source)
    stream_title: str = ""
    buffering: bool = False
    playback_error: str = ""
    api: int = 0
    seq: int = 0
    stamp: float = 0.0

    @property
    def is_live(self) -> bool:
        """終わりの無い流れ (ラジオなど) か。再生位置の線を出さない。"""
        return bool(self.track and self.track.live)

    @property
    def display_title(self) -> str:
        """再生バーの曲名。ラジオは ICY の曲名を優先する。"""
        if self.stream_title.strip():
            return self.stream_title.strip()
        return self.track.display_title if self.track else ""

    @property
    def playback_problem(self) -> tuple[str, str] | None:
        """いまの曲が再生できなかった理由 (短い日本語, 全文)。無ければ None。

        読み込み中 (やり直している間) は出さない (cliamp も次の開始で消す)。"""
        if self.buffering or self.track is None or not self.playback_error.strip():
            return None
        return describe_playback_error(self.playback_error)

    @property
    def display_subtitle(self) -> str:
        """再生バーの副題。読み込み中は「読み込み中…」、再生できなかったらその理由の短文、
        ラジオは局名。"""
        if self.buffering:
            return "読み込み中…"
        if not self.track:
            return ""
        problem = self.playback_problem
        if problem is not None:
            return problem[0]
        if self.stream_title.strip():
            return self.track.display_title
        return self.track.subtitle


@dataclass
class PlaylistState:
    """playlist の応答 (いまのリストの写し)。"""

    tracks: list[Track] = field(default_factory=list)
    index: int = -1
    total: int = 0
    queue: list[int] = field(default_factory=list)
    up_next: list[int] = field(default_factory=list)
    gen: int = 0
    source: Source = field(default_factory=Source)

    def track_at(self, i: int) -> Track | None:
        return self.tracks[i] if 0 <= i < len(self.tracks) else None


@dataclass(frozen=True)
class ProviderInfo:
    """PROTOCOL の ProviderInfo。playback は曲の鳴らし方 (省かれていれば "")。

    playback が "youtube" なら、そのプロバイダーは自分では鳴らせず、曲を YouTube で探して
    鳴らす (Spotify の接続が Web API だけのとき)。"""

    key: str
    name: str = ""
    search: bool = False
    playlists: bool = False
    virtual: bool = False
    playback: str = ""

    @property
    def plays_via_youtube(self) -> bool:
        return self.playback == PLAYBACK_YOUTUBE


@dataclass(frozen=True)
class PlaylistInfo:
    provider: str
    id: str
    name: str
    track_count: int = 0
    duration: int = 0
    section: str = ""


@dataclass(frozen=True)
class LyricLine:
    t: float
    text: str


@dataclass
class Lyrics:
    lines: list[LyricLine]
    synced: bool

    def index_at(self, position: float) -> int:
        """再生位置 position (秒) のときの今の行。前奏中や同期していなければ -1。"""
        if not self.synced:
            return -1
        current = -1
        for i, line in enumerate(self.lines):
            if line.t <= position + 1e-6:
                current = i
            else:
                break
        return current


@dataclass
class Response:
    """1 つの要求への応答。

    kind: "ok" | "error" (cliamp が失敗を返した) | "offline" (繋がらない) |
    "timeout" (時間切れ) | "unsupported" (拡張の無い cliamp。"unknown command") |
    "cancelled" (新しい要求に置き換えられ、送らずに捨てた。検索の打ち直しなど)。
    """

    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    kind: str = "ok"

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def needs_auth(self) -> bool:
        """プロバイダーのサインインが要る (cliamp の画面でサインインしてもらう)。"""
        return _as_bool(self.data.get("needs_auth"))

    @property
    def message(self) -> str:
        """利用者に見せる短い理由 (日本語)。"""
        if self.ok:
            return ""
        if self.kind == "offline":
            return "cliamp に接続できません"
        if self.kind == "timeout":
            return "cliamp の応答がありません"
        if self.kind == "unsupported":
            return "この cliamp は拡張 IPC に対応していません"
        if self.kind == "cancelled":
            return "取り消しました"
        if self.needs_auth:
            return "サインインが必要です。cliamp の画面でサインインしてください"
        if self.error == "not found":
            return "見つかりません"
        if self.error == STALE:
            return "リストが変わっていたので、もう一度選んでください"
        return describe_catalog_error(self.error) or self.error or "失敗しました"

    @classmethod
    def offline(cls, detail: str = "") -> "Response":
        return cls(False, {}, detail or "cliamp に接続できません", "offline")

    @classmethod
    def timeout(cls, detail: str = "") -> "Response":
        return cls(False, {}, detail or "時間切れ", "timeout")

    @classmethod
    def cancelled(cls, detail: str = "") -> "Response":
        return cls(False, {}, detail or "取り消しました", "cancelled")


# ---------------------------------------------------------------------------
# 要求の組み立てと応答の解析


def _wire_value(value: Any) -> Any:
    if isinstance(value, (Track, Source)):
        return value.to_wire()
    if isinstance(value, dict):
        return {str(k): _wire_value(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_wire_value(v) for v in value]
    return value


def encode_request(cmd: str, **fields: Any) -> bytes:
    """要求 1 行 (末尾に改行)。None の値は省く。Track / Source は TrackInfo / SourceInfo に。

    index=0 のような 0 は省かない (cliamp 側はポインタで受けて 0 を有効値とする)。
    """
    request: dict[str, Any] = {"cmd": cmd}
    for key, value in fields.items():
        if value is None:
            continue
        request[key] = _wire_value(value)
    return json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_response(line: bytes | str) -> Response:
    """応答 1 行を読む。"unknown command" は kind="unsupported" にする。"""
    try:
        if isinstance(line, (bytes, bytearray)):
            line = bytes(line).decode("utf-8")
        data = json.loads(line)
    except (UnicodeDecodeError, ValueError) as exc:
        return Response(False, {}, f"応答を読めません: {exc}", "error")
    if not isinstance(data, dict):
        return Response(False, {}, "応答の形が違います", "error")
    if data.get("ok") is True:
        return Response(True, data)
    error = _as_str(data.get("error"))
    kind = "unsupported" if error.startswith("unknown command") else "error"
    return Response(False, data, error, kind)


def _repeat(value: Any) -> str:
    mode = _as_str(value).strip().lower()
    return mode if mode in REPEAT_MODES else "off"


def _eq(value: Any) -> list[float]:
    bands = [_as_float(v) for v in _as_list(value)][: len(EQ_BANDS)]
    return bands + [0.0] * (len(EQ_BANDS) - len(bands))


def _track_or_none(value: Any) -> Track | None:
    d = _as_dict(value)
    if not d or not _as_str(d.get("path")):
        return None
    return Track.from_wire(d)


def parse_status(d: Any) -> Status:
    """status の応答を Status に。欠けた値は 0 / False、repeat は小文字に。

    duration が 0 (不明) のときは曲の長さ (TrackInfo の duration) で補う。
    """
    d = _as_dict(d)
    track = _track_or_none(d.get("track"))
    state = _as_str(d.get("state")).strip().lower() or "stopped"
    if state not in ("playing", "paused", "stopped"):
        state = "stopped"
    duration = max(0.0, _as_float(d.get("duration")))
    if duration <= 0 and track is not None and track.duration > 0:
        duration = float(track.duration)
    speed = _as_float(d.get("speed"))
    return Status(
        state=state,
        track=track,
        position=max(0.0, _as_float(d.get("position"))),
        duration=duration,
        volume=_as_float(d.get("volume")),
        index=_as_int(d.get("index")),
        total=max(0, _as_int(d.get("total"))),
        shuffle=_as_bool(d.get("shuffle")),
        repeat=_repeat(d.get("repeat")),
        mono=_as_bool(d.get("mono")),
        speed=speed if speed > 0 else 1.0,
        eq_preset=_as_str(d.get("eq_preset")),
        eq=_eq(d.get("eq")),
        gen=max(0, _as_int(d.get("gen"))),
        source=Source.from_wire(d.get("source")),
        stream_title=_as_str(d.get("stream_title")),
        buffering=_as_bool(d.get("buffering")),
        playback_error=_as_str(d.get("playback_error")),
        api=_as_int(d.get("api")),
    )


# --- Spotify の断り (検索の封鎖・回数の制限・利用枠・鳴らせない曲) --------------------------

# Spotify の曲を鳴らすには librespot のセッション (Premium) が要る。Web API だけの接続で
# spotify:track: の曲を始めると、cliamp は "spotify: streaming unavailable…" で断る。
SPOTIFY_STREAMING_UNAVAILABLE = "spotify: streaming unavailable"
SPOTIFY_PREMIUM_REQUIRED = "Spotify の曲の再生には Premium が必要です"
# 持ち主が Premium にした直後も、Spotify がそれを反映するまで (数時間) は同じ 403 が続く
SPOTIFY_PREMIUM_PROPAGATION = "Premium にした直後は、Spotify が反映するまで数時間かかることがあります"
# Spotify が検索の件数を断った。2026 年の規則 (2026-02-11 以降に作ったアプリ、既存のアプリは 03-09 から) では、
# 開発モードのアプリ (自分で登録した client_id) の /v1/search は 1 回 10 件まで (既定 5) で、それを
# 越えると 400 "Invalid limit" (パッチの cliamp は 10 件ずつに分けて頼み、それでも断られたら
# "spotify: search: Spotify refused a page of N results (…): http status 400 …Invalid limit…" と言う)。
# cliamp 1.50.0 の friendlySearchError はこの 400 を "spotify: search blocked — your client_id is too new. …"
# に言い直す (2024-11 の規則の頃の文言。古い cliamp の答えとして今も見分ける)。今の規則で検索そのものが
# 止められるのは、アプリの持ち主が Premium でない (403) か、開発者の利用枠を使い切った (429) ときだけ
# (どちらも別に見分けて言う)。
SPOTIFY_SEARCH_BLOCKED_TITLE = "Spotify では検索できません"
SPOTIFY_SEARCH_BLOCKED = ("Spotify が 1 回の検索の件数を断りました。自分で登録したアプリ (開発モードの client_id) の"
                          "検索は 1 回 10 件までで、パッチを当てた cliamp は 10 件ずつに分けて頼みます (断られたのは、"
                          "cliamp が古いか、Spotify が上限をさらに下げたためです)。検索そのものが止められるのは、"
                          "アプリの持ち主が Premium でないときと、開発者向けの利用枠を使い切ったときだけです。"
                          "プレイリストと保存した曲はそのまま使えます。その間は YouTube で検索してください。")
# パッチは Spotify の Web API の待ち (Retry-After) を 30 秒ほどで打ち切り、それより長く待てと
# 言われたら "spotify: rate limited by Spotify; retry after 24h0m0s" で断る。cliamp 1.50.0 の
# 素の文言 ("spotify: web api rate-limited on /v1/… after 8 retries") も同じ扱い (待ちは不明)。
#
# どれも cliamp の Spotify の文言そのもので決める: 語は文の先頭か、文脈の前置き
# ("spotify: your music: "、"custom streamer: ") の ": " の直後にあるときだけ見る。YouTube の
# 検索の誤りは利用者の語をそのまま繰り返す ("resolving yt-dlp ytsearch20:<語>: …") ので、語に
# "search blocked" や "rate limited by Spotify" が入っていても Spotify の断りとは読まない。
_SPOTIFY_AT = r"(?:^|:\s)"
_SPOTIFY_RATE_LIMITED = re.compile(
    _SPOTIFY_AT + r"spotify: rate limited by spotify(?:;\s*retry after\s+([0-9][0-9.a-zµμ]*))?"
    r"|" + _SPOTIFY_AT + r"spotify: web api rate-limited\b", re.IGNORECASE | re.MULTILINE)
# 開発者の利用枠 (2026-07 から。1 人の開発者の開発モードのアプリはすべて 1 つの枠を分け合う) を使い切ると、
# Spotify は 429 に {"error": {…, "reason": "QUOTA_EXCEEDED"}} を付ける (ふつうの回数の制限とは別)。
# パッチの cliamp は待たずに "spotify: Spotify quota exceeded for this developer account; retry after 1h0m0s"
# (QuotaExceededError。待ちが分からなければ "; retry after …" は無い) で断る。前に文脈が付くこともある
# ("spotify: search: …")。文言は回数の制限と同じく "spotify: " から始まるものを、文の先頭か ": " の直後だけで見る
# (再生の誤りの "custom streamer: spotify: …" も)。"spotify: " の付かない "Spotify quota exceeded" と、本文の reason を
# そのまま包んだ Spotify の誤り ("spotify: search: http status 429 …: {…"reason": "QUOTA_EXCEEDED"…}") は、全体が
# cliamp の Spotify の誤り ("spotify: " で始まる) のときだけ見る (フォルダ名 "x: Spotify quota exceeded" の手元の
# ファイルが無い、語に同じ言葉を含む YouTube の検索の誤り、などは利用枠ではない)。
SPOTIFY_QUOTA_EXCEEDED = "Spotify の開発者向けの利用枠を使い切りました"
_QUOTA_RETRY = r"(?:[^\n]*?\bretry after\s+([0-9][0-9.a-zµμ]*))?"
_SPOTIFY_QUOTA_RE = re.compile(_SPOTIFY_AT + r"spotify: (?:spotify |web api )?quota exceeded\b" + _QUOTA_RETRY,
                               re.IGNORECASE | re.MULTILINE)
_SPOTIFY_QUOTA_BARE_RE = re.compile(_SPOTIFY_AT + r"spotify quota exceeded\b" + _QUOTA_RETRY,
                                    re.IGNORECASE | re.MULTILINE)
_SPOTIFY_QUOTA_REASON_RE = re.compile(r'"reason"\s*:\s*"quota_exceeded"', re.IGNORECASE)
_SPOTIFY_SEARCH_BLOCKED_RE = re.compile(_SPOTIFY_AT + r"spotify: search blocked\b",
                                        re.IGNORECASE | re.MULTILINE)
_SPOTIFY_SEARCH_FAILED_RE = re.compile(_SPOTIFY_AT + r"spotify: search:", re.IGNORECASE | re.MULTILINE)
# Spotify の Web API は、開発者アプリ (client_id) の持ち主が Premium でないと、どの呼び出しにも 403
# "Active premium subscription required for the owner of the app" を返す (2026-09 の実測)。cliamp は
# 本文をそのまま包む ("spotify: your music: http status 403 Forbidden: {…"message": "Active premium …"}")。
# Premium にした後も、Spotify が反映するまで (数時間) は同じ 403 が続く。
SPOTIFY_OWNER_PREMIUM = ("Spotify の開発者アプリの持ち主が Premium でないため、Spotify のライブラリは読めません。"
                         f"{SPOTIFY_PREMIUM_PROPAGATION}。公開プレイリストは「Spotify から取り込む」で使えます")
_SPOTIFY_OWNER_PREMIUM_RE = re.compile(r"active premium subscription required for the owner of the app",
                                       re.IGNORECASE)
# 開発モードのアプリを使えるのは、Dashboard の User Management に登録した利用者 (5 人まで) だけ。登録されて
# いないアカウントには Spotify が 403 "Check settings on developer.spotify.com/dashboard, the user may not be
# registered." を返す。パッチの cliamp の検索は "spotify: search: this Spotify account is not a user of the
# Developer app (…): …" と言い直す (ほかの呼び出しは本文のまま)。
SPOTIFY_NOT_A_USER = ("サインインした Spotify のアカウントが、開発者アプリの利用者に登録されていません。"
                      "developer.spotify.com/dashboard のアプリの「User Management」で足してください (開発モードの"
                      "アプリの利用者は 5 人まで)")
_SPOTIFY_NOT_A_USER_RE = re.compile(r"\bnot a user of the developer app\b|\bthe user may not be registered\b",
                                    re.IGNORECASE)
# cliamp の Spotify の tracks は Web API の 403 をどれも "spotify: playlist not accessible: only playlists you
# own or collaborate on can be loaded" に言い換える (持ち主が Premium でないときも)
SPOTIFY_NOT_ACCESSIBLE = ("このプレイリストは Spotify のライブラリから読めません (読めるのは自分のと共同編集の"
                          "プレイリストだけ)。公開プレイリストなら「Spotify から取り込む」で使えます")
_SPOTIFY_NOT_ACCESSIBLE_RE = re.compile(_SPOTIFY_AT + r"spotify: playlist not accessible\b",
                                        re.IGNORECASE | re.MULTILINE)
_SPOTIFY_STREAMING_RE = re.compile(_SPOTIFY_AT + re.escape(SPOTIFY_STREAMING_UNAVAILABLE) + r"\b",
                                   re.IGNORECASE | re.MULTILINE)
_GO_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]*)?)(ns|us|µs|μs|ms|h|m|s)")
_GO_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "μs": 1e-6,
                      "ns": 1e-9}


def parse_go_duration(text: str) -> float | None:
    """Go の time.Duration.String() の形 ("24h0m0s"・"1m30.5s"・"250ms"・"0s") を秒に。
    読めなければ None。"""
    text = _as_str(text).strip()
    total = 0.0
    pos = 0
    for match in _GO_DURATION_PART.finditer(text):
        if match.start() != pos:
            return None
        total += float(match.group(1)) * _GO_DURATION_UNITS[match.group(2)]
        pos = match.end()
    if pos == 0 or pos != len(text) or not math.isfinite(total):
        return None
    return total


def format_wait(seconds: float) -> str:
    """待ち時間を「24 時間」「1 時間 30 分」「5 分」「30 秒」に (切り上げ)。0 以下・不明は空。"""
    value = _as_float(seconds)
    if value <= 0:
        return ""
    total = math.ceil(value)
    if total < 60:
        return f"{total} 秒"
    minutes = math.ceil(total / 60)
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return f"{minutes} 分"
    return f"{hours} 時間" + (f" {minutes} 分" if minutes else "")


def spotify_rate_limit_wait(text: str) -> float | None:
    """Spotify の回数の制限で断られた誤りなら、待つように言われた秒 (分からなければ 0)。
    違えば None。cliamp の文言 ("spotify: rate limited by Spotify; …") が文の先頭か ": " の
    後ろにあるときだけ (YouTube の検索の誤りが繰り返す語や、曲名・パスの中の語では決めない)。"""
    match = _SPOTIFY_RATE_LIMITED.search(_as_str(text)[:_CLASSIFY_MAX])
    if match is None:
        return None
    wait = parse_go_duration((match.group(1) or "").rstrip("."))
    return wait if wait is not None else 0.0


def spotify_rate_limit_message(wait: float) -> str:
    """回数の制限の説明 (カタログ系の失敗の文)。"""
    span = format_wait(wait)
    return f"Spotify から回数の制限を受けています。{span + 'ほど' if span else 'しばらく'}待ってから、もう一度試してください"


def spotify_quota_wait(text: str) -> float | None:
    """Spotify の開発者の利用枠を使い切った誤り (429 の reason "QUOTA_EXCEEDED") なら、待つように
    言われた秒 (分からなければ 0)。違えば None。パッチの文言 ("spotify: Spotify quota exceeded; …")
    は回数の制限と同じく文の先頭か ": " の後ろにあるときだけ、"spotify: " の付かない "Spotify quota exceeded"
    と本文の reason は cliamp の Spotify の誤り ("spotify: " で始まる) の中にあるときだけ見る (YouTube の
    検索の誤りが繰り返す語や、手元のファイルのパスでは決めない)。"""
    text = _as_str(text)[:_CLASSIFY_MAX]
    spotify = is_spotify_error(text)
    match = _SPOTIFY_QUOTA_RE.search(text)
    if match is None and spotify:
        match = _SPOTIFY_QUOTA_BARE_RE.search(text)
    if match is not None:
        wait = parse_go_duration((match.group(1) or "").rstrip("."))
        return wait if wait is not None else 0.0
    if spotify and _SPOTIFY_QUOTA_REASON_RE.search(text):
        return 0.0
    return None


def is_spotify_quota_exceeded(text: str) -> bool:
    """Spotify の開発者の利用枠を使い切った誤りか (spotify_quota_wait が None でない)。"""
    return spotify_quota_wait(text) is not None


def spotify_quota_message(wait: float) -> str:
    """利用枠を使い切ったときの説明 (カタログ系の失敗の文)。待ちが分かればその長さを言う。"""
    span = format_wait(wait)
    return f"{SPOTIFY_QUOTA_EXCEEDED}。{span + 'ほど' if span else 'しばらく'}してから試してください"


def is_spotify_error(text: str) -> bool:
    """cliamp の Spotify のプロバイダーの誤りか。カタログ系の誤りは IPC が包まないので、
    Spotify のものは必ず "spotify: " で始まる (YouTube の検索の誤りは "resolving yt-dlp …" か
    "yt-dlp: …")。"""
    return _as_str(text).lstrip().lower().startswith("spotify:")


def is_spotify_search_blocked(text: str) -> bool:
    """Spotify に検索 (の件数) を断られた誤りか: cliamp 1.50.0 の friendlySearchError "spotify: search
    blocked — your client_id is too new…"、その元の "spotify: search: http status 400 …Invalid limit…"、
    パッチの "spotify: search: Spotify refused a page of N results (…): …Invalid limit…"。どれも cliamp の
    Spotify の誤り ("spotify: " で始まる) のときだけ。

    古い文言は 2024-11 の規則 (開発モードのアプリの検索の封鎖) の頃のものだが、2026 年の規則では開発モードでも
    1 回 10 件までは検索できる。説明 (SPOTIFY_SEARCH_BLOCKED) は今の規則で書く。持ち主が Premium でない
    (is_spotify_owner_premium_required) と利用枠 (spotify_quota_wait) は別に見分ける。"""
    text = _as_str(text)[:_CLASSIFY_MAX]
    if not is_spotify_error(text):
        return False
    if _SPOTIFY_SEARCH_BLOCKED_RE.search(text):
        return True
    return bool(_SPOTIFY_SEARCH_FAILED_RE.search(text)) and "invalid limit" in text.lower()


def is_spotify_owner_premium_required(text: str) -> bool:
    """Spotify の Web API が「開発者アプリの持ち主が Premium でない」と断った誤りか (403
    "Active premium subscription required for the owner of the app")。cliamp の Spotify の誤り
    ("spotify: " で始まる) のときだけ (YouTube の検索の誤りは利用者の語を繰り返すので見ない)。"""
    text = _as_str(text)[:_CLASSIFY_MAX]
    return is_spotify_error(text) and bool(_SPOTIFY_OWNER_PREMIUM_RE.search(text))


def is_spotify_not_a_user(text: str) -> bool:
    """サインインしたアカウントが開発モードのアプリの利用者 (User Management) に無いと断られた誤りか。
    cliamp の Spotify の誤り ("spotify: " で始まる) のときだけ。"""
    text = _as_str(text)[:_CLASSIFY_MAX]
    return is_spotify_error(text) and bool(_SPOTIFY_NOT_A_USER_RE.search(text))


def is_spotify_not_accessible(text: str) -> bool:
    """cliamp の Spotify の tracks が 403 を言い換えた "spotify: playlist not accessible: …" か。"""
    text = _as_str(text)[:_CLASSIFY_MAX]
    return is_spotify_error(text) and bool(_SPOTIFY_NOT_ACCESSIBLE_RE.search(text))


def describe_catalog_error(text: str) -> str:
    """カタログ系の失敗のうち日本語で言い直せるもの (Spotify の利用枠・回数の制限・持ち主が Premium でない・
    アプリの利用者でない・読めないプレイリスト・検索の件数の断り・鳴らせない曲)。cliamp の Spotify の誤り ("spotify: " で始まる) だけを言い直し、それ以外
    (語を繰り返す YouTube の検索の誤りなど) は空。"""
    text = _as_str(text)
    if not text or not is_spotify_error(text):
        return ""
    # 利用枠はふつうの回数の制限 (429) より先に見る (どちらも 429。待っても枠が戻るまで断られる)
    quota = spotify_quota_wait(text)
    if quota is not None:
        return spotify_quota_message(quota)
    wait = spotify_rate_limit_wait(text)
    if wait is not None:
        return spotify_rate_limit_message(wait)
    if is_spotify_owner_premium_required(text):
        return SPOTIFY_OWNER_PREMIUM
    if is_spotify_not_a_user(text):
        return SPOTIFY_NOT_A_USER
    if is_spotify_not_accessible(text):
        return SPOTIFY_NOT_ACCESSIBLE
    if is_spotify_search_blocked(text):
        return f"{SPOTIFY_SEARCH_BLOCKED_TITLE}。{SPOTIFY_SEARCH_BLOCKED}"
    if _SPOTIFY_STREAMING_RE.search(text[:_CLASSIFY_MAX]):
        return SPOTIFY_PREMIUM_REQUIRED
    return ""


# --- 再生の失敗 (status の playback_error) ---------------------------------------------

PLAYBACK_ERROR_SHORT_MAX = 80  # 知らない誤りを短文にするときの長さ (文字)
PLAYBACK_ERROR_DETAIL_MAX = 1200  # ツールチップに出す全文の長さ (文字)

_HTTP_DENIED = re.compile(r"\b(?:http(?: error| status)?\s*(?:403|429)|403 forbidden|429 too many requests)\b"
                          r"|too many requests|rate.?limit")
_HTTP_GONE = re.compile(r"\bhttp(?: error| status)?\s*(?:404|410)\b|404 not found|410 gone")
_YTDL_PREFIX = re.compile(r"^(?:yt-dlp(?: seek)?:\s*)+", re.IGNORECASE)
_ERROR_PREFIX = re.compile(r"^(?:error|fatal):\s*", re.IGNORECASE)
_EXTRACTOR_PREFIX = re.compile(r"^\[[^\]\s]+\]\s*(?:[\w-]{1,64}:\s+)?")
# 見分けに使わない部分: URL (Go の url.Error は "Get \"https://…\"")、引用符の中 (%q)、
# 手元のパス (os.PathError は "open /music/…/x.flac: no such file or directory")。曲名や
# フォルダ名の語 ("Timeout"、"Spotify Singles"、"Private Video") で理由を取り違えない。
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s\"'<>/?#]*)[^\s\"'<>]*")
_QUOTED = re.compile(r'"[^"\n]*"')
_LOCAL_PATH = re.compile(r"(?<![\w.:/~-])~?/[^\s/:][^\n]*?(?=:\s|:?$)", re.MULTILINE)
# yt-dlp の抽出器 ("ERROR: [soundcloud] 123: …"、"[youtube:tab]")
_EXTRACTOR_TAG = re.compile(r"\[([a-z][a-z0-9_.-]*)(?::[\w.-]+)?\]")
_EXTRACTOR_SERVICES = (("youtube", "YouTube"), ("soundcloud", "SoundCloud"), ("spotify", "Spotify"),
                       ("bandcamp", "Bandcamp"), ("vimeo", "Vimeo"))
# Go の exec.Error ('exec: "yt-dlp": executable file not found in $PATH')
_MISSING_EXEC = re.compile(r'exec: "([^"\n]+)": executable file not found')
_CLASSIFY_MAX = 8000  # 見分けに使う長さ (cliamp は 2 KiB で切る。古い・知らない送り手に備える)


def _any_in(text: str, needles: Iterable[str]) -> bool:
    return any(needle in text for needle in needles)


def _service_of(plain: str, hosts: str) -> str:
    """誤りの出どころ。yt-dlp の抽出器の印 ([soundcloud] など) を先に見る (cliamp の yt-dlp の
    誤りはどれも "yt-dlp: " で始まるので、それだけでは YouTube と決めない)。plain はパスと URL を
    除いた文、hosts は URL のホスト名。"""
    tag = _EXTRACTOR_TAG.search(plain)
    if tag is not None:
        name = tag.group(1)
        return next((label for key, label in _EXTRACTOR_SERVICES if name.startswith(key)), "")
    both = f"{plain} {hosts}"
    if "spotify" in both:
        return "Spotify"
    if "soundcloud" in both:
        return "SoundCloud"
    if _any_in(both, ("youtube", "youtu.be", "ytsearch", "yt-dlp")):
        return "YouTube"
    return ""


def _classify_text(text: str) -> tuple[str, str]:
    """見分けに使う (小文字の文, URL のホスト名)。URL・引用符の中・手元のパスを除く。"""
    low = text[:_CLASSIFY_MAX].lower().replace("\u2019", "'")
    hosts = " ".join(m.group(1) for m in _URL.finditer(low))
    plain = _URL.sub(" ", low)
    plain = _QUOTED.sub(" ", plain)
    plain = _LOCAL_PATH.sub(" ", plain)
    return plain, hosts


def _playback_error_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in _as_str(text).replace("\r", "\n").split("\n") if line.strip()]


def _clean_error_line(lines: list[str]) -> str:
    """知らない誤りの短文: yt-dlp なら ERROR の行 (WARNING の行は飛ばす) を、前置き
    ("yt-dlp: ERROR: [youtube] ID: ") を除いて、長ければ省略する。"""
    if not lines:
        return ""
    pick = next((line for line in lines if "error:" in line.lower()), lines[0])
    line = _YTDL_PREFIX.sub("", pick)
    line = _ERROR_PREFIX.sub("", line)
    line = _EXTRACTOR_PREFIX.sub("", line).strip() or pick
    if len(line) > PLAYBACK_ERROR_SHORT_MAX:
        line = line[: PLAYBACK_ERROR_SHORT_MAX - 1].rstrip() + "…"
    return line


@functools.lru_cache(maxsize=32)
def _describe_playback_error(text: str) -> tuple[str, str]:
    return _describe(text)


def describe_playback_error(text: str) -> tuple[str, str]:
    """cliamp の再生の誤り (status の playback_error) を (短い日本語, 全文) にする。

    短文は再生バーの副題・フルスクリーン・ミニプレーヤー・トーストに、全文はツールチップに出す。
    yt-dlp の誤りは stderr がそのまま来る (WARNING の行が先に並ぶこともある)。知らない誤りは
    最初の意味のある行 (yt-dlp なら ERROR の行) を 80 字ほどに縮めて出す。空なら ("", "")。
    status は 1 秒に何度も届き、再生バー・フルスクリーン・ミニプレーヤーがそれぞれ引くので覚えておく。
    """
    return _describe_playback_error(_as_str(text))


def _describe(text: str) -> tuple[str, str]:
    lines = _playback_error_lines(text)
    if not lines:
        return "", ""
    whole = "\n".join(lines)
    detail = whole
    if len(detail) > PLAYBACK_ERROR_DETAIL_MAX:
        detail = detail[: PLAYBACK_ERROR_DETAIL_MAX - 1].rstrip() + "…"
    # 見分けは切り詰める前の全体で (ERROR の行が長い前置きの後ろにあっても見落とさない)。
    # パスや URL の中の語では決めない (low)。道具の有無だけは引用符の中 ("yt-dlp") も見る (full)。
    full = whole[:_CLASSIFY_MAX].lower()
    low, hosts = _classify_text(whole)
    service = _service_of(low, hosts)

    # Spotify の Web API だけの接続では spotify:track: の曲を鳴らせない (librespot のセッションが無い)。
    # 文言に "login5"・"credentials" などが続いてもサインインの誤りではないので先に見る。
    # 回数の制限も含め、パス・URL・引用符を除いた文 (low) の cliamp の文言の位置だけで決める
    # (フォルダ名 "Rate Limited By Spotify" の手元のファイルが無いのは「ファイルが見つかりません」)
    if _SPOTIFY_STREAMING_RE.search(low):
        return SPOTIFY_PREMIUM_REQUIRED, detail
    quota = spotify_quota_wait(low)
    if quota is not None:
        span = format_wait(quota)
        return f"{SPOTIFY_QUOTA_EXCEEDED} ({span + 'ほど' if span else 'しばらく'}待つ)", detail
    wait = spotify_rate_limit_wait(low)
    if wait is not None:
        span = format_wait(wait)
        return f"Spotify から回数の制限を受けています ({span + 'ほど' if span else 'しばらく'}待つ)", detail
    # サインイン (Spotify のセッション切れ、cliamp の ErrNeedsAuth "sign-in required")
    if service == "Spotify" and _any_in(low, ("sign-in required", "auth", "credential", "token", "login",
                                              "log in", "401", "cliamp spotify reset")):
        return "Spotify へのサインインが必要です (cliamp の端末で)", detail
    if "sign-in required" in low:
        return "サインインが必要です (cliamp の端末で)", detail
    # 非公開・削除 (「Private video. Sign in if you've been granted access」はサインインではない)
    if _any_in(low, ("private video", "video is private", "this video has been removed",
                     "has been removed by the uploader", "account associated with this video has been terminated",
                     "video has been removed", "no longer available")):
        return "この動画は再生できません (非公開か削除)", detail
    # 年齢確認・ボット確認・メンバー限定 (YouTube のサインインか Cookie が要る)
    if _any_in(low, ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users",
                     "not a bot", "sign in to confirm", "--cookies", "cookies-from-browser",
                     "members-only", "join this channel", "login required", "requires authentication")):
        return (f"{service} の" if service and service != "Spotify" else "配信元の") + \
            "サインインが必要な曲です (cliamp の設定で Cookie を使う)", detail
    if _any_in(low, ("in your country", "geo restrict", "geo-restrict", "not available in your region")):
        return "この地域では再生できない動画です", detail
    if _any_in(low, ("video unavailable", "this video is not available", "this video is unavailable",
                     "content isn't available", "content is not available")):
        return "この動画は再生できません (非公開か削除)", detail
    # 一時的な拒否 (HTTP 403 / 429、回数の制限)
    if _HTTP_DENIED.search(low):
        return (f"{service} に" if service else "配信元に") + "一時的に拒否されました", detail
    if _HTTP_GONE.search(low):
        if service == "YouTube":
            return "この動画は再生できません (非公開か削除)", detail
        return "見つかりません (リンクが切れています)", detail
    # ネットワーク・名前解決
    if _any_in(low, ("no such host", "name resolution", "name or service not known", "nodename nor servname",
                     "network is unreachable", "network is down", "no route to host", "connection refused",
                     "connection reset", "i/o timeout", "tls handshake timeout", "unable to download webpage",
                     "failed to resolve", "getaddrinfo", "errno -2]", "errno -3]", "urlopen error",
                     "connection timed out", "dial tcp", "dial udp", "server misbehaving",
                     "unable to connect", "network error")):
        return "ネットワークに繋がりません", detail
    if _any_in(low, ("timed out", "timeout", "deadline exceeded")):
        return "読み込みが時間切れになりました", detail
    # 再生に要る道具
    missing = _MISSING_EXEC.search(full)
    missing_tool = os.path.basename(missing.group(1)) if missing is not None else ""
    if "yt-dlp is required" in full or missing_tool == "yt-dlp":
        return "再生に yt-dlp が要ります", detail
    if "ffmpeg is required" in full or missing_tool in ("ffmpeg", "ffprobe"):
        return "再生に ffmpeg が要ります", detail
    if "no episodes found in feed" in low:
        return "このフィードにはエピソードがありません", detail
    # 手元のファイル
    if "no such file or directory" in low or "cannot find the file" in low:
        return "ファイルが見つかりません", detail
    if "permission denied" in low:
        return "ファイルを読めません (アクセス権がありません)", detail
    if "unsupported url" in low:
        return "この URL は再生できません", detail
    # 読めない形式・壊れたファイル (cliamp の "decode: …"、ffmpeg の誤り)
    if _any_in(low, ("decode", "unsupported", "unknown format", "invalid data found", "not a valid",
                     "no audio", "could not find codec", "format not recognized", "ffmpeg",
                     "unexpected eof", "bad header", "invalid header", "no decoder",
                     "moov atom not found", "end of file")):
        return "このファイルは再生できません", detail
    return _clean_error_line(lines), detail


def playback_error_headline(short: str) -> str:
    """短文から末尾の括弧書き (「(cliamp の設定で Cookie を使う)」などの手当て) を除いた見出し。
    幅の限られたトーストに使う (手当ては再生バーのツールチップとフルスクリーンに出る)。"""
    head = re.sub(r"\s*\([^()]*\)\s*$", "", _as_str(short)).strip()
    return head or _as_str(short).strip()


def playback_error_tooltip(short: str, detail: str) -> str:
    """再生できなかった理由のツールチップ: 短文 (手当てを含む) と cliamp の誤りの全文。"""
    short, detail = _as_str(short).strip(), _as_str(detail).strip()
    if not detail or detail == short:
        return short
    return f"{short}\n\n{detail}" if short else detail


def parse_tracks(d: Any) -> list[Track]:
    """tracks の配列 (応答そのものか、配列だけ) を Track の並びに。path の無いものは捨てる。"""
    items = d if isinstance(d, list) else _as_list(_as_dict(d).get("tracks"))
    return [Track.from_wire(item) for item in items if isinstance(item, dict) and _as_str(item.get("path"))]


def _int_list(value: Any) -> list[int]:
    return [_as_int(v) for v in _as_list(value) if isinstance(v, (int, float)) and not isinstance(v, bool)]


def parse_playlist(d: Any) -> PlaylistState:
    """playlist の応答を PlaylistState に。queue / up_next が省かれていれば空。"""
    d = _as_dict(d)
    tracks = parse_tracks(d)
    total = _as_int(d.get("total")) or len(tracks)
    index = _as_int(d.get("index")) if "index" in d else (0 if tracks else -1)
    return PlaylistState(
        tracks=tracks,
        index=index,
        total=total,
        queue=_int_list(d.get("queue")),
        up_next=_int_list(d.get("up_next")),
        gen=max(0, _as_int(d.get("gen"))),
        source=Source.from_wire(d.get("source")),
    )


def parse_providers(d: Any) -> list[ProviderInfo]:
    items = d if isinstance(d, list) else _as_list(_as_dict(d).get("providers"))
    out = []
    for item in items:
        item = _as_dict(item)
        key = _as_str(item.get("key"))
        if not key:
            continue
        out.append(ProviderInfo(
            key=key,
            name=_as_str(item.get("name")) or key,
            search=_as_bool(item.get("search")),
            playlists=_as_bool(item.get("playlists")),
            virtual=_as_bool(item.get("virtual")),
            playback=_as_str(item.get("playback")).strip().lower(),
        ))
    return out


def parse_playlists(d: Any, provider: str) -> list[PlaylistInfo]:
    items = d if isinstance(d, list) else _as_list(_as_dict(d).get("playlists"))
    out = []
    for item in items:
        item = _as_dict(item)
        pid = _as_str(item.get("id"))
        name = _as_str(item.get("name"))
        if not pid and not name:
            continue
        out.append(PlaylistInfo(
            provider=provider,
            id=pid or name,
            name=name or pid,
            track_count=max(0, _as_int(item.get("track_count"))),
            duration=max(0, _as_int(item.get("duration"))),
            section=_as_str(item.get("section")),
        ))
    return out


def parse_lyrics(d: Any) -> Lyrics:
    """lyrics の応答を Lyrics に。同期した歌詞は時刻の順に並べ直す。"""
    d = _as_dict(d)
    lines = []
    for item in _as_list(d.get("lyrics")):
        item = _as_dict(item)
        lines.append(LyricLine(t=max(0.0, _as_float(item.get("t"))), text=_as_str(item.get("text"))))
    synced = _as_bool(d.get("synced"))
    if synced:
        lines.sort(key=lambda line: line.t)
    return Lyrics(lines=lines, synced=synced)


def parse_devices(text: str) -> list[tuple[str, bool]]:
    """`device list` の応答 (device 欄の改行区切り、既定の sink は先頭 "* ") を (名前, 印) に。"""
    out = []
    for raw in _as_str(text).splitlines():
        if not raw.strip():
            continue
        active = raw.startswith("* ")
        name = raw[2:] if raw[:2] in ("* ", "  ") else raw
        name = name.strip()
        if name:
            out.append((name, active))
    return out


@dataclass(frozen=True)
class Device:
    """出力先 (PulseAudio / PipeWire の sink)。

    name は切り替えに送る sink 名 (`alsa_output.pci-….analog-stereo` など。pactl が受け取る
    のはこれだけ)、label はメニューに出す名前、active は cliamp が「* 」を付けたもの
    (既定の sink。cliamp の流れの行き先とは限らない)。"""

    name: str
    label: str
    active: bool = False


_SINK_KINDS = (
    ("analog-stereo", "アナログ出力"), ("analog-surround", "アナログ出力 (サラウンド)"),
    ("iec958", "デジタル出力 (S/PDIF)"), ("hdmi", "HDMI / DisplayPort"),
    ("usb", "USB オーディオ"),
)


def device_label(name: str) -> str:
    """sink 名から見出しを作る (説明の無い cliamp のための最後の手段)。

    bluez_output.AC_80_… → 「Bluetooth (AC:80:…)」、alsa_output.….hdmi-stereo-extra1 →
    「HDMI / DisplayPort 2」、….analog-stereo → 「アナログ出力」。分からない形はそのまま。"""
    name = _as_str(name).strip()
    lowered = name.lower()
    if lowered.startswith("bluez_output.") or lowered.startswith("bluez_sink."):
        address = name.split(".", 1)[1].split(".", 1)[0].replace("_", ":")
        return f"Bluetooth ({address})" if address else "Bluetooth"
    if not lowered.startswith(("alsa_output.", "alsa_sink.")):
        return name
    profile = lowered.rsplit(".", 1)[-1]
    for key, label in _SINK_KINDS:
        if key in profile:
            match = re.search(r"-extra(\d+)$", profile)
            if match and key == "hdmi":
                return f"{label} {int(match.group(1)) + 1}"
            if "usb" in lowered and key != "usb":
                return f"USB オーディオ ({label})"
            return label
    return name


def parse_device_list(data: Any) -> list[Device]:
    """`device list` の応答を Device の並びに。

    説明付きの `devices` 配列 ({name, description, active}) があればそれを使い、無ければ
    `device` 欄の改行区切り (素の cliamp 1.50.0 とパッチ済みの TUI) を読む。説明が無ければ
    sink 名から見出しを作る (device_label)。同じ見出しが重なれば sink 名を添えて見分ける。"""
    data = _as_dict(data)
    out: list[Device] = []
    items = _as_list(data.get("devices"))
    if items:
        for item in items:
            item = _as_dict(item)
            name = _as_str(item.get("name")).strip()
            if not name:
                continue
            label = _as_str(item.get("description")).strip() or device_label(name)
            out.append(Device(name, label, _as_bool(item.get("active"))))
    else:
        out = [Device(name, device_label(name), active) for name, active in parse_devices(data.get("device"))]
    counts: dict[str, int] = {}
    for device in out:
        counts[device.label] = counts.get(device.label, 0) + 1
    return [Device(d.name, f"{d.label} — {d.name}", d.active) if counts[d.label] > 1 and d.label != d.name else d
            for d in out]


# ---------------------------------------------------------------------------
# 表示の小道具


def db_to_linear(db: float) -> float:
    """音量 dB (-30〜+6) を 0〜1 に。cliamp の MPRIS と同じ換算 (lin = 10^((dB-6)/20))。"""
    db = _as_float(db)
    if db <= VOLUME_MIN_DB:
        return 0.0
    if db >= VOLUME_MAX_DB:
        return 1.0
    return math.pow(10.0, (db - VOLUME_MAX_DB) / 20.0)


def linear_to_db(x: float) -> float:
    """0〜1 を音量 dB に。0 は -30 dB、1 は +6 dB。"""
    x = _as_float(x)
    if x <= 0:
        return VOLUME_MIN_DB
    if x >= 1:
        return VOLUME_MAX_DB
    return max(VOLUME_MIN_DB, 20.0 * math.log10(x) + VOLUME_MAX_DB)


def clamp_volume(db: float) -> float:
    return min(VOLUME_MAX_DB, max(VOLUME_MIN_DB, _as_float(db)))


def volume_fraction(db: float) -> float:
    """音量 dB (-30〜+6) をつまみの位置 0〜1 に (dB に比例。耳の感じ方に近い)。

    再生バーとフルスクリーンの音量のつまみはどちらもこの換算を使う (同じ音量が同じ位置に
    見えるように)。db_to_linear (MPRIS の振幅) はつまみには使わない。"""
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return min(1.0, max(0.0, (_as_float(db) - VOLUME_MIN_DB) / span))


def fraction_to_volume(fraction: float) -> float:
    """つまみの位置 0〜1 を音量 dB に (volume_fraction の逆)。"""
    return VOLUME_MIN_DB + min(1.0, max(0.0, _as_float(fraction))) * (VOLUME_MAX_DB - VOLUME_MIN_DB)


def format_time(secs: float | None) -> str:
    """"3:07" / "1:02:03"。不明 (None・負・無限) は "--:--"。秒は切り捨て。"""
    if secs is None or isinstance(secs, bool):
        return "--:--"
    try:
        value = float(secs)
    except (TypeError, ValueError):
        return "--:--"
    if not math.isfinite(value) or value < 0:
        return "--:--"
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_total(secs: float) -> str:
    """合計の長さ「48 分」「1 時間 12 分」。0 以下は空。"""
    value = _as_float(secs)
    if value <= 0:
        return ""
    minutes = max(1, int(round(value / 60.0)))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} 時間 {minutes} 分"
    if hours:
        return f"{hours} 時間"
    return f"{minutes} 分"


def track_key(track: Track | str | None) -> str:
    """アートワークのキャッシュや重複除去の鍵。

    YouTube は動画 ID (www と music.youtube.com の同じ曲を 1 つに)、
    Spotify は曲 ID、それ以外は path そのもの。
    """
    if track is None:
        return ""
    if isinstance(track, str):
        track = Track(path=track)
    if is_youtube_bridge(track) and track.spotify_id:
        # YouTube で探して鳴らす Spotify の曲は、探した動画ではなく Spotify の曲として数える
        return f"spotify:{track.spotify_id}"
    yid = track.youtube_id
    if yid:
        return f"youtube:{yid}"
    sid = track.spotify_id
    if sid:
        return f"spotify:{sid}"
    return f"path:{track.path}"


def fold_text(text: str) -> str:
    """検索の比較用。NFKC で全角・半角を揃え、大文字小文字を畳む。"""
    return unicodedata.normalize("NFKC", _as_str(text)).casefold()


# cliamp の external/local は <ディレクトリ>/<名前>.toml を開くので、名前に ".toml" を足したものが
# ファイル名の上限 (Linux の NAME_MAX、255 バイト) を超えると "open …: file name too long" で断られる
PLAYLIST_NAME_MAX_BYTES = 255 - len(".toml")


def playlist_name_problem(name: str) -> str:
    """ローカルのプレイリスト名として使えない理由 (日本語の短い文)。使えれば ""。

    cliamp の safePath と同じ条件 (空・"."・".."・"/" と "\\")、予約名 (履歴の仮想プレイリスト)、
    ファイル名の長さ (名前 + ".toml" が 255 バイトまで)。前後の空白は除いて見る。"""
    if not isinstance(name, str):
        return "名前を入れてください"
    name = name.strip()
    if not name:
        return "名前を入れてください"
    if name in (".", ".."):
        return "この名前は使えません"
    if name == RECENTLY_PLAYED:
        return "この名前は cliamp が履歴に使っています"
    if any(ch in name for ch in "/\\"):
        return "この名前は使えません (「/」と「\\」は入れられません)"
    try:
        size = len(name.encode("utf-8"))
    except UnicodeEncodeError:  # 対になっていないサロゲート (cliamp へ送れない)
        return "この名前は使えません"
    if size > PLAYLIST_NAME_MAX_BYTES:
        return "名前が長すぎます (日本語ならおよそ 80 文字まで)"
    return ""


def valid_playlist_name(name: str) -> bool:
    """ローカルのプレイリスト名として使えるか (playlist_name_problem が空か)。"""
    return not playlist_name_problem(name)


def fit_playlist_name(name: str, suffix: str = "") -> str:
    """name + suffix がプレイリスト名の長さの上限 (PLAYLIST_NAME_MAX_BYTES) に収まるよう、name を文字の
    切れ目で縮める (縮めたら末尾に「…」)。収まっていればそのまま。"""
    limit = PLAYLIST_NAME_MAX_BYTES - len(suffix.encode("utf-8", "replace"))
    data = name.encode("utf-8", "replace")
    if len(data) <= limit:
        return name + suffix
    ellipsis = "…"
    room = max(0, limit - len(ellipsis.encode("utf-8")))
    cut = data[:room].decode("utf-8", "ignore").rstrip()
    return cut + ellipsis + suffix


def _parse_iso(iso: str) -> datetime | None:
    text = _as_str(iso).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Go の RFC3339Nano は小数が 9 桁まで。Python は 6 桁までなので切り詰める。
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def relative_time(iso: str, now: datetime | float | None = None) -> str:
    """RFC3339 の時刻を「3 時間前」の形に。読めなければ空。

    now は datetime か UNIX 時刻 (秒)。省けば今。未来 (時計のずれ) は「たった今」。
    """
    moment = _parse_iso(iso)
    if moment is None:
        return ""
    if now is None:
        now_dt = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        now_dt = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    else:
        now_dt = datetime.fromtimestamp(float(now), timezone.utc)
    seconds = (now_dt - moment).total_seconds()
    if seconds < 60:
        return "たった今"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} 分前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 時間前"
    days = hours // 24
    if days == 1:
        return "昨日"
    if days < 7:
        return f"{days} 日前"
    if days < 30:
        return f"{days // 7} 週間前"
    if days < 365:
        return f"{max(1, days // 30)} か月前"
    return f"{days // 365} 年前"
