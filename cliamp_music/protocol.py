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
        return youtube_id_of(self.path)

    @property
    def spotify_id(self) -> str | None:
        return spotify_id_of(self.path) or (self.meta_get("spotify.id") or None)

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
        """「リンクをコピー」「ブラウザで開く」に使う URL。無ければ None。"""
        if is_url(self.path):
            return self.path
        sid = spotify_id_of(self.path)
        if sid:
            return f"https://open.spotify.com/track/{sid}"
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
    key: str
    name: str = ""
    search: bool = False
    playlists: bool = False
    virtual: bool = False


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
        return self.error or "失敗しました"

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


def valid_playlist_name(name: str) -> bool:
    """ローカルのプレイリスト名として使えるか (cliamp の safePath と同じ条件 + 予約名)。"""
    if not isinstance(name, str):
        return False
    name = name.strip()
    if not name or name in (".", "..") or name == RECENTLY_PLAYED:
        return False
    return not any(ch in name for ch in "/\\")


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
