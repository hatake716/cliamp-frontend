#!/usr/bin/env python3
"""偽の cliamp。PROTOCOL.md (api 1) をすべて実装した Unix ソケットのサーバー。

本物の cliamp を起動せずに GUI を動かし、試験し、画面を撮るためのもの。
手元に再生器を持ち、再生位置は実時間で進み、曲の終わりでリストの順に次へ進む。
リストの並び (order / pos / 待ち行列) は cliamp の playlist.Playlist をそのまま写した。

本物に合わせた細部:
- 応答は Go の `omitempty` と同じく 0・空文字・false・空の配列を省く
  (ポインタの shuffle / mono / buffering / synced と TrackInfo の path は残す)。
- JSON は Go の json.Marshal と同じく < > & を \\u003c などに逃がし、整数値の
  float は "1" と書く。
- legacy モードでは cliamp 1.50.0 の既存コマンドだけを知っており、
  新しいコマンドには `unknown command: X` を返す。status も拡張前の形。
- `device list` は改行区切りの 1 つの文字列 (使用中の行は "* " で始まる)。
- TUI の `play` は停止中に何もしない。停止中の再生は `toggle`。
- `playlists local` は cliamp と同じく、履歴があれば仮想の "Recently Played" を先頭に返す
  (その後にローカルのプレイリスト「ドライブ」「Focus」)。

試験の道具: `isolate_display()` (利用者の画面に繋がない)、`temp_socket_path()`、
`run_loop(until, timeout)` (GLib の main loop を回す)。FakeCliamp の `requests` に
受けた要求が残り、`delays` で応答を遅らせられる。

単体でも動く:
    python3 tests/fake_cliamp.py --socket /tmp/fake.sock [--legacy] [--art-dir DIR]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_REQUEST = 8 << 20  # 要求 1 行の上限 (パッチ後の cliamp と同じ 8 MiB)

LEGACY_COMMANDS = (
    "play", "pause", "toggle", "stop", "next", "prev", "volume", "seek",
    "load", "queue", "theme", "vis", "shuffle", "repeat", "mono", "speed",
    "eq", "device", "status", "bands", "plugin.call", "plugin.commands",
)
API1_COMMANDS = LEGACY_COMMANDS + (
    "capabilities", "seek_to", "playlist", "play_index", "replace", "enqueue",
    "queue_edit", "remove", "providers", "playlists", "tracks", "search",
    "load_provider", "lyrics", "history", "playlist_add", "playlist_delete",
    "playlist_remove_track",
)
CATALOG = frozenset({
    "providers", "playlists", "tracks", "search", "load_provider", "lyrics",
    "history", "playlist_add", "playlist_delete", "playlist_remove_track",
})

# cliamp の ui/model/eq_presets.go と同じ並び。
EQ_PRESETS = [
    ("Flat", [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    ("Rock", [5, 4, 2, -1, -2, 2, 4, 5, 5, 5]),
    ("Pop", [-1, 2, 4, 5, 4, 1, -1, -1, 1, 2]),
    ("Jazz", [3, 4, 2, 1, -1, -1, 1, 2, 3, 4]),
    ("Classical", [3, 2, 1, 0, -1, -1, 0, 2, 3, 4]),
    ("Bass Boost", [8, 6, 4, 2, 0, 0, 0, 0, 0, 0]),
    ("Treble Boost", [0, 0, 0, 0, 0, 1, 3, 5, 6, 7]),
    ("Vocal", [-2, -1, 1, 4, 5, 4, 2, 0, -1, -2]),
    ("Electronic", [6, 4, 1, -1, -2, 1, 3, 4, 5, 6]),
    ("Acoustic", [3, 3, 2, 0, 1, 2, 3, 3, 2, 1]),
    ("Hip-Hop", [7, 5, 3, 1, -1, -1, 1, 3, 3, 3]),
    ("R&B", [4, 6, 3, 1, -1, 1, 2, 2, 1, 0]),
    ("Loudness", [6, 4, 1, 0, -2, -1, 1, 4, 5, 5]),
    ("Late Night", [5, 3, 1, 0, -2, -1, 0, 2, 3, 3]),
    ("Podcast", [-3, -1, 2, 4, 4, 3, 1, -1, -2, -3]),
    ("Small Speakers", [7, 5, 4, 2, 1, 0, -1, 0, 1, 2]),
]

# 架空の曲。(題, アーティスト, アルバム, 秒, ジャンル, 年, 置き場所)
# 置き場所: "yt" = yt-dlp の検索結果 (www.youtube.com、stream)、
#           "ytm" = ytmusic プロバイダー (music.youtube.com)、"file" = 手元のファイル。
LIBRARY = [
    ("夜明けのバス停", "青い灯台", "港町ラジオ", 214, "J-Pop", 2021, "yt"),
    ("Paper Moon Drive", "Aurora Lane", "Night Cartography", 187, "Indie", 2019, "yt"),
    ("シティライト・ブルース", "真夜中ポスト", "都会の窓", 245, "City Pop", 2020, "ytm"),
    ("Glass Harbor", "Aurora Lane", "Night Cartography", 201, "Indie", 2019, "yt"),
    ("ひこうき雲の手紙", "小春日和", "四季録", 268, "J-Pop", 2018, "yt"),
    ("Neon & Rain", "Kite Theory", "Weather Report", 176, "Electronic", 2022, "yt"),
    ("放課後サイダー", "小春日和", "四季録", 199, "J-Pop", 2018, "ytm"),
    ("Slow Orbit", "Mira Okafor", "Satellites", 305, "Ambient", 2023, "yt"),
    ("雨上がりのプラットホーム", "真夜中ポスト", "都会の窓", 232, "City Pop", 2020, "yt"),
    ("Lo-fi Study Session", "Tape Garden", "Rainy Afternoon", 3600, "Lo-fi", 2024, "yt"),
    ("Sunday Tokyo", "Kite Theory", "Weather Report", 223, "Electronic", 2022, "yt"),
    ("カセットテープの夏", "青い灯台", "港町ラジオ", 251, "J-Pop", 2021, "file"),
    ("Midnight Laundromat", "The Velvet Hours", "Spin Cycle", 198, "Rock", 2017, "yt"),
    ("星を数える", "ミナト・レイ", "星図", 287, "J-Pop", 2025, "yt"),
    ("Coffee & Static", "Tape Garden", "Rainy Afternoon", 142, "Lo-fi", 2024, "yt"),
    ("海辺のアルゴリズム", "ミナト・レイ", "星図", 240, "J-Pop", 2025, "ytm"),
    ("Signal Fire", "Mira Okafor", "Satellites", 262, "Ambient", 2023, "yt"),
    ("東京タワーが見える部屋", "真夜中ポスト", "都会の窓", 276, "City Pop", 2020, "yt"),
    ("Rock & Roll Bakery", "The Velvet Hours", "Spin Cycle", 185, "Rock", 2017, "yt"),
    ("<Untitled> *demo*", "Kite Theory", "B-Sides & Rarities", 133, "Electronic", 2022, "yt"),
    ("桜の坂道", "小春日和", "四季録", 221, "J-Pop", 2018, "file"),
    ("Velvet Highway", "The Velvet Hours", "Spin Cycle", 247, "Rock", 2017, "yt"),
    ("月曜日のジャズ", "喫茶ムーンライト", "珈琲と月", 318, "Jazz", 2016, "yt"),
    ("Blue Hour Waltz", "喫茶ムーンライト", "珈琲と月", 204, "Jazz", 2016, "yt"),
    ("ラジオ体操第三", "港町ブラスバンド", "朝の体操", 180, "Brass", 2015, "yt"),
    ("Aurora (Live at Harbor Hall)", "Aurora Lane", "Live at Harbor Hall", 412, "Indie", 2024, "yt"),
    ("夏の終わりのプレリュード", "ミナト・レイ", "星図", 199, "J-Pop", 2025, "yt"),
    ("Hyperlocal", "Tape Garden", "", 166, "Lo-fi", 2024, "yt"),
    ("A Very Long Song Title That Keeps Going Well Past The Width Of Any Reasonable Row",
     "Mira Okafor", "Satellites (Deluxe Edition)", 351, "Ambient", 2023, "yt"),
    ("おやすみ、ロボット", "青い灯台", "港町ラジオ", 238, "J-Pop", 2021, "yt"),
]

STATIONS = [
    ("cliamp radio", "https://radio.cliamp.stream/streams/lofi.mp3"),
    ("Harbor Jazz FM", "https://stream.example.net/harbor-jazz.mp3"),
    ("Tokyo Lo-fi Radio", "https://stream.example.net/tokyo-lofi.aac"),
    ("Classic 24", "https://stream.example.net/classic24.ogg"),
]
FAVORITES = [("Tokyo Lo-fi Radio", "https://stream.example.net/tokyo-lofi.aac", "128k", "Japan")]

ICY_TITLES = [
    "Tape Garden - Coffee & Static",
    "喫茶ムーンライト - Blue Hour Waltz",
    "Aurora Lane - Glass Harbor",
    "真夜中ポスト - シティライト・ブルース",
]

DEVICES = ["既定の出力", "Built-in Audio アナログステレオ", "HDMI / DisplayPort 2 (ディスプレイ)"]

# 偽の歌詞 (この試験のために書いたもの)。
LYRIC_LINES = [
    "遠くで始発の音がする",
    "窓を少しだけ開けて",
    "昨日の地図を折りたたむ",
    "The kettle hums a quiet tune",
    "We count the streetlights one by one",
    "まだ名前のない朝へ",
    "",
    "ポケットの中の小さな切符",
    "Every corner smells like rain",
    "信号が青に変わるまで",
    "もう一度だけ手を振って",
    "And the city hums along",
]

SEARCH_TEMPLATES = [
    "{q}", "{q} (Official Video)", "{q} - Live", "{q} (Acoustic ver.)",
    "{q} 歌ってみた", "{q} Lyric Video", "【作業用BGM】{q} メドレー", "{q} [Remastered]",
    "{q} (Instrumental)", "{q} - Topic",
]
UPLOADERS = ["Aurora Lane", "青い灯台", "Kite Theory", "真夜中ポスト", "Tape Garden",
             "ミナト・レイ", "Music Channel JP", "Mira Okafor", "喫茶ムーンライト"]

# 値が偽でも省かない欄 (Go のポインタと、omitempty の無い TrackInfo の path)。
_KEEP = frozenset({"ok", "shuffle", "mono", "buffering", "synced", "path"})
# 中身を省かない欄 (Go の map[string]string。値が空でも残る)。
_OPAQUE = frozenset({"meta"})


def fake_youtube_id(seed: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha1(seed.encode()).digest()[:8]).decode().rstrip("=")


def fake_spotify_id(seed: str) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    number = int.from_bytes(hashlib.sha1(seed.encode()).digest(), "big")
    out = []
    for _ in range(22):
        number, rest = divmod(number, 62)
        out.append(alphabet[rest])
    return "".join(out)


def _empty(value) -> bool:
    return value is None or value is False or value == 0 or value == "" or value == [] or value == {}


def omit(value, key: str | None = None):
    """Go の omitempty を真似る。"""
    if isinstance(value, dict):
        if key in _OPAQUE:
            return dict(value)
        out = {}
        for k, v in value.items():
            v = omit(v, k)
            if k not in _KEEP and _empty(v):
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [omit(v) for v in value]
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


def go_json(obj) -> bytes:
    """Go の json.Marshal と同じ見た目の 1 行 (末尾に改行)。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return text.encode("utf-8") + b"\n"


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TRACK_FIELDS = ("path", "title", "artist", "album", "genre", "year", "track_number",
                "duration", "stream", "live", "feed", "unplayable", "bookmark", "meta")


def to_track(info: dict) -> dict:
    """TrackInfo → cliamp の playlist.Track 相当 (queued / played_at は持たない)。"""
    out = {}
    for name in TRACK_FIELDS:
        if name in info:
            out[name] = info[name]
    if isinstance(out.get("meta"), dict):
        out["meta"] = {str(k): str(v) for k, v in out["meta"].items()}
    out["path"] = str(out.get("path") or "")
    return out


class FakePlaylist:
    """cliamp の playlist.Playlist の写し (order / pos / queue / queuedIdx)。"""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.tracks: list[dict] = []
        self.order: list[int] = []
        self.pos = 0
        self.shuffle = False
        self.repeat = "all"
        self.queue: list[int] = []
        self.queued_idx = -1
        self.gen = 1

    def bump(self) -> None:
        self.gen += 1

    def replace(self, tracks: list[dict]) -> None:
        self.tracks = list(tracks)
        self.order = list(range(len(self.tracks)))
        self.pos = 0
        self.queue = []
        self.queued_idx = -1
        if self.shuffle and self.tracks:
            self.do_shuffle()
        self.bump()

    def add(self, *tracks: dict) -> None:
        start = len(self.tracks)
        self.tracks.extend(tracks)
        self.order.extend(range(start, len(self.tracks)))
        if self.shuffle and tracks:
            if start == 0 or self.pos >= len(self.order):
                self.pos = 0
                self.do_shuffle()
            else:
                tail = self.order[self.pos + 1:]
                self.rng.shuffle(tail)
                self.order[self.pos + 1:] = tail
        self.bump()

    def __len__(self) -> int:
        return len(self.tracks)

    def index(self) -> int:
        if not self.order:
            return -1
        if self.queued_idx >= 0:
            return self.queued_idx
        return self.order[self.pos]

    def current(self) -> tuple[dict | None, int]:
        idx = self.index()
        return (self.tracks[idx], idx) if idx >= 0 else (None, -1)

    def playable(self, idx: int) -> bool:
        return 0 <= idx < len(self.tracks) and not self.tracks[idx].get("unplayable")

    def _first_playable(self, start: int, stop: int):
        for i in range(max(0, start), min(stop, len(self.order))):
            if self.playable(self.order[i]):
                return i
        return None

    def _last_playable(self, start: int):
        for i in range(min(start, len(self.order) - 1), -1, -1):
            if self.playable(self.order[i]):
                return i
        return None

    def activate_selected(self) -> bool:
        if not self.order:
            return False
        slot = self.pos if self.playable(self.order[self.pos]) else self._first_playable(self.pos + 1, len(self.order))
        if slot is None and self.repeat == "all":
            slot = self._first_playable(0, self.pos)
        if slot is None:
            return False
        self.pos = slot
        self.queued_idx = -1
        return True

    def next(self) -> bool:
        if not self.tracks:
            return False
        for i, idx in enumerate(self.queue):
            if self.playable(idx):
                self.queue = self.queue[i + 1:]
                self.queued_idx = idx
                return True
        self.queue = []
        if self.repeat == "one":
            if self.playable(self.order[self.pos]):
                self.queued_idx = -1
                return True
            return False
        slot = self._first_playable(self.pos + 1, len(self.order))
        if slot is None and self.repeat == "all":
            if self.shuffle and self.pos + 1 >= len(self.order):
                self.do_shuffle()
                slot = self._first_playable(1, len(self.order))
                if slot is None:
                    slot = self._first_playable(0, 1)
            else:
                slot = self._first_playable(0, len(self.order))
        if slot is None:
            return False
        self.queued_idx = -1
        self.pos = slot
        return True

    def prev(self) -> bool:
        if not self.tracks:
            return False
        slot = self._last_playable(self.pos - 1)
        if slot is None and self.repeat == "all":
            slot = self._last_playable(len(self.order) - 1)
        if slot is None:
            return False
        self.queued_idx = -1
        self.pos = slot
        return True

    def set_index(self, i: int) -> None:
        self.queued_idx = -1
        if i in self.order:
            self.pos = self.order.index(i)

    def start_at(self, i: int) -> None:
        """選んだ曲から始め、シャッフル中は残りを混ぜる (パッチの StartAt)。"""
        self.set_index(i)
        if self.shuffle and self.tracks:
            self.do_shuffle()

    def queue_add(self, i: int) -> None:
        if 0 <= i < len(self.tracks) and i not in self.queue:
            self.queue.append(i)
            self.bump()

    def dequeue(self, i: int) -> bool:
        if i in self.queue:
            self.queue.remove(i)
            self.bump()
            return True
        return False

    def queue_position(self, i: int) -> int:
        return self.queue.index(i) + 1 if i in self.queue else 0

    def clear_queue(self) -> None:
        if self.queue:
            self.queue = []
            self.bump()

    def move_queue(self, a: int, b: int) -> bool:
        if not (0 <= a < len(self.queue) and 0 <= b < len(self.queue)) or a == b:
            return False
        item = self.queue.pop(a)
        self.queue.insert(b, item)
        self.bump()
        return True

    def remove(self, idx: int) -> bool:
        if not 0 <= idx < len(self.tracks):
            return False
        del self.tracks[idx]
        removed_pos = -1
        order = []
        for i, o in enumerate(self.order):
            if o == idx:
                removed_pos = i
                continue
            order.append(o - 1 if o > idx else o)
        self.order = order
        if 0 <= removed_pos < self.pos:
            self.pos -= 1
        self.pos = max(0, min(self.pos, len(self.order) - 1))
        self.queue = [q - 1 if q > idx else q for q in self.queue if q != idx]
        if self.queued_idx == idx:
            self.queued_idx = -1
        elif self.queued_idx > idx:
            self.queued_idx -= 1
        self.bump()
        return True

    def toggle_shuffle(self) -> None:
        self.shuffle = not self.shuffle
        if self.tracks:
            if self.shuffle:
                self.do_shuffle()
            else:
                cur = self.order[self.pos]
                self.order = list(range(len(self.tracks)))
                self.pos = cur
        self.bump()

    def do_shuffle(self) -> None:
        cur = self.order[self.pos]
        others = [i for i in range(len(self.tracks)) if i != cur]
        self.rng.shuffle(others)
        self.order = [cur] + others
        self.pos = 0

    def cycle_repeat(self) -> None:
        self.repeat = {"off": "all", "all": "one", "one": "off"}[self.repeat]

    def upcoming(self, limit: int = 200) -> list[int]:
        """再生順で今の曲の後に来る曲 (待ち行列の曲は除く)。"""
        if not self.order:
            return []
        after = self.order[self.pos + 1:]
        if self.repeat == "all" and not self.shuffle:
            after += self.order[: self.pos]
        current = self.index()
        return [i for i in after if i not in self.queue and i != current][:limit]


class FakeCliamp:
    """偽の cliamp。start() で待ち受け、stop() で閉じる (状態は残るので restart() で戻れる)。

    試験の道具:
      requests  受け取った要求 (dict) の記録。
      delays    {コマンド: 秒}。応答を遅らせる (時間切れの試験)。
      latency   カタログ系のコマンドすべてに足す遅れ。
    """

    def __init__(self, socket_path: str, *, legacy: bool = False, art_dir: str | None = None,
                 spotify_needs_auth: bool = False, latency: float = 0.0, buffer_secs: float = 0.0,
                 initial_state: str = "playing", empty: bool = False, seed: int = 7):
        self.socket_path = socket_path
        self.legacy = legacy
        self.spotify_needs_auth = spotify_needs_auth
        self.latency = latency
        self.buffer_secs = buffer_secs
        self.delays: dict[str, float] = {}
        self.requests: list[dict] = []
        self.lock = threading.RLock()
        self._listener: socket.socket | None = None
        self._conns: set[socket.socket] = set()
        self._rng = random.Random(seed)
        self._art = self._art_urls(art_dir)

        self.library = [self._library_track(i, row) for i, row in enumerate(LIBRARY)]
        self.pl = FakePlaylist(self._rng)
        self.state = "stopped"
        self._pos_base = 0.0
        self._t_base = time.monotonic()
        self._buffering_until = 0.0
        self._scrobbled = False
        self.volume = -6.0
        self.speed = 1.0
        self.mono = False
        self.eq = [0.0] * 10
        self.eq_preset_idx = 0
        self.eq_custom = ""
        self.device = 0
        self.source: dict = {}
        self.history: list[tuple[dict, str]] = []
        self.local_playlists: dict[str, list[dict]] = {
            "ドライブ": [self.library[i] for i in (1, 2, 5, 10, 17, 21, 12)],
            "Focus": [self.library[i] for i in (9, 14, 7, 16, 27, 22)],
        }
        self.spotify_lists = {
            "YOUR MUSIC": ("Your Music", "Library", [self._spotify_track(i) for i in (0, 3, 13, 23, 25)]),
            "37i9dQZF1DXfake0001": ("Chill Mix", "Your playlists", [self._spotify_track(i) for i in (7, 14, 16, 27)]),
            "37i9dQZF1DXfake0002": ("週末のジャズ", "Followed playlists", [self._spotify_track(i) for i in (22, 23)]),
        }
        self._seed_history()
        if not empty:
            self.pl.replace([self.library[i] for i in range(12)])
            self.pl.set_index(2)
            self.source = {"provider": "local", "id": "ドライブ", "name": "ドライブ"}
            if initial_state in ("playing", "paused"):
                self._play_current(time.monotonic())
                self._pos_base = 42.0
                if initial_state == "paused":
                    self.state = "paused"

    # --- データ -------------------------------------------------------------

    @staticmethod
    def _art_urls(art_dir: str | None) -> list[str]:
        if not art_dir:
            return []
        exts = {".png", ".jpg", ".jpeg", ".webp"}
        files = sorted(p for p in Path(art_dir).iterdir() if p.suffix.lower() in exts)
        return [p.resolve().as_uri() for p in files]

    def _with_art(self, track: dict, seed: str) -> dict:
        """--art-dir があれば meta.art に file:// の絵を割り当てる (撮影でネットワークに出ないように)。"""
        if self._art:
            index = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(self._art)
            track["meta"] = {**(track.get("meta") or {}), "art": self._art[index]}
        return track

    def _library_track(self, i: int, row) -> dict:
        title, artist, album, secs, genre, year, where = row
        if where == "file":
            path = f"/home/fake/Music/{artist}/{album}/{i + 1:02d} {title}.flac"
            stream = False
        else:
            vid = fake_youtube_id(f"{title}|{artist}")
            host = "music.youtube.com" if where == "ytm" else "www.youtube.com"
            path = f"https://{host}/watch?v={vid}"
            stream = where == "yt"
        track = {"path": path, "title": title, "artist": artist, "album": album,
                 "genre": genre, "year": year, "track_number": (i % 12) + 1,
                 "duration": secs, "stream": stream}
        if self._art:
            track["meta"] = {"art": self._art[i % len(self._art)]}
        return track

    def _spotify_track(self, i: int) -> dict:
        title, artist, album, secs, genre, year, _ = LIBRARY[i]
        sid = fake_spotify_id(f"spotify|{title}")
        return self._with_art({"path": f"spotify:track:{sid}", "title": title, "artist": artist,
                               "album": album, "year": year, "duration": secs, "track_number": 1}, title)

    def _seed_history(self) -> None:
        now = datetime.now(timezone.utc)
        ago = [timedelta(minutes=10), timedelta(hours=2), timedelta(hours=5), timedelta(days=1, hours=1),
               timedelta(days=2), timedelta(days=4), timedelta(days=9), timedelta(days=40)]
        picks = (13, 0, 5, 22, 2, 9, 18, 25)
        self.history = [(dict(self.library[i]), rfc3339(now - delta)) for i, delta in zip(picks, ago)]

    # --- 待ち受け -------------------------------------------------------------

    def start(self) -> "FakeCliamp":
        with self.lock:
            if self._listener is not None:
                return self
            os.makedirs(os.path.dirname(os.path.abspath(self.socket_path)), exist_ok=True)
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(self.socket_path)
            os.chmod(self.socket_path, 0o600)
            listener.listen(32)
            self._listener = listener
        threading.Thread(target=self._accept_loop, args=(listener,), name="fake-cliamp-accept",
                         daemon=True).start()
        return self

    def stop(self) -> None:
        """待ち受けと、繋がっている接続をすべて閉じる (cliamp の終了にあたる)。"""
        with self.lock:
            listener, self._listener = self._listener, None
            conns, self._conns = self._conns, set()
        if listener is not None:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            listener.close()
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def restart(self) -> None:
        self.stop()
        self.start()

    close = stop

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _accept_loop(self, listener: socket.socket) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with self.lock:
                if self._listener is not listener:
                    conn.close()
                    return
                self._conns.add(conn)
            threading.Thread(target=self._serve, args=(conn,), name="fake-cliamp-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        conn.settimeout(60)  # cliamp と同じく 60 秒黙っている接続は閉じる
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = conn.recv(1 << 20)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while True:
                    newline = buf.find(b"\n")
                    if newline < 0:
                        break
                    line = bytes(buf[:newline])
                    del buf[: newline + 1]
                    if len(line) > MAX_REQUEST:
                        return
                    if not line.strip():
                        continue
                    reply = self.handle_line(line)
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return
                if len(buf) > MAX_REQUEST:
                    return  # Go の Scanner と同じく、長すぎる行で接続を切る
        finally:
            with self.lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    # --- 要求の処理 -------------------------------------------------------------

    def handle_line(self, line: bytes) -> bytes:
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            return go_json({"ok": False, "error": f"invalid JSON: {exc}"})
        cmd = str(req.get("cmd") or "")
        with self.lock:
            self.requests.append(req)
            if len(self.requests) > 20000:  # 長く動かしても膨らみ続けないように
                del self.requests[:-10000]
        delay = self.delays.get(cmd, 0.0) + (self.latency if cmd in CATALOG else 0.0)
        if delay > 0:
            time.sleep(delay)
        return go_json(omit(self.dispatch(req)))

    def commands(self) -> tuple[str, ...]:
        return LEGACY_COMMANDS if self.legacy else API1_COMMANDS

    def dispatch(self, req: dict) -> dict:
        cmd = str(req.get("cmd") or "").lower()
        if cmd not in self.commands():
            return {"ok": False, "error": f"unknown command: {req.get('cmd', '')}"}
        handler = getattr(self, "_cmd_" + cmd.replace(".", "_"))
        with self.lock:
            now = time.monotonic()
            self._tick(now)
            return handler(req, now)

    def requests_for(self, cmd: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r.get("cmd") == cmd]

    # --- 再生器 -----------------------------------------------------------------

    def _buffering(self, now: float) -> bool:
        return self.state == "playing" and now < self._buffering_until

    def position(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if self.state == "playing" and not self._buffering(now):
            return max(0.0, self._pos_base + max(0.0, now - self._t_base) * self.speed)
        return self._pos_base

    def _duration(self) -> float:
        track, _ = self.pl.current()
        if track is None or track.get("live"):
            return 0.0
        return float(track.get("duration") or 0)

    def _play_current(self, at: float) -> None:
        self.state = "playing"
        self._pos_base = 0.0
        self._scrobbled = False
        if self.buffer_secs > 0:
            self._buffering_until = at + self.buffer_secs
            self._t_base = at + self.buffer_secs
        else:
            self._buffering_until = 0.0
            self._t_base = at

    def _freeze(self, now: float) -> None:
        self._pos_base = self.position(now)
        self._t_base = now

    def _tick(self, now: float) -> None:
        """再生位置を今に進め、曲の終わりを過ぎていれば次へ (何曲分でも)。"""
        for _ in range(1000):
            if self.state != "playing":
                return
            duration = self._duration()
            if duration <= 0:
                return
            pos = self.position(now)
            if not self._scrobbled and pos >= duration / 2:
                self._scrobble()
            if pos < duration:
                return
            ended = self._t_base + (duration - self._pos_base) / self.speed
            if self.pl.next():
                self._play_current(ended)
            else:
                self.state = "stopped"
                self._pos_base = 0.0
                self._t_base = now

    def _scrobble(self) -> None:
        """半分まで聞いた曲を履歴へ (cliamp の maybeScrobble と同じ条件)。"""
        self._scrobbled = True
        track, _ = self.pl.current()
        if track is None or track.get("live"):
            return
        now = datetime.now(timezone.utc)
        if self.history and self.history[0][0].get("path") == track.get("path"):
            self.history[0] = (self.history[0][0], rfc3339(now))
        else:
            self.history.insert(0, (dict(track), rfc3339(now)))
            del self.history[200:]

    def _start_track(self, now: float) -> None:
        self._play_current(now)

    # --- 返す形 -----------------------------------------------------------------

    def _trackinfo(self, track: dict, *, queued: int = 0, played_at: str = "") -> dict:
        if self.legacy:
            return {"title": track.get("title", ""), "artist": track.get("artist", ""), "path": track.get("path", "")}
        info = dict(track)
        if queued:
            info["queued"] = queued
        if played_at:
            info["played_at"] = played_at
        return info

    def _stream_title(self, track: dict | None, now: float) -> str:
        if not track or not track.get("live"):
            return ""
        return ICY_TITLES[int(now // 30) % len(ICY_TITLES)]

    def _preset_name(self) -> str:
        if 0 <= self.eq_preset_idx < len(EQ_PRESETS):
            return EQ_PRESETS[self.eq_preset_idx][0]
        return self.eq_custom or "Custom"

    # --- 状態系 -----------------------------------------------------------------

    def _cmd_status(self, req, now):
        track, idx = self.pl.current()
        buffering = self._buffering(now)
        resp = {
            "ok": True,
            "state": "stopped" if buffering else self.state,
            "position": round(self.position(now), 3),
            "duration": self._duration(),
            "volume": self.volume,
            "index": idx,
            "total": len(self.pl),
            "visualizer": "Bars",
            "shuffle": self.pl.shuffle,
            "repeat": self.pl.repeat.capitalize(),
            "mono": self.mono,
            "speed": self.speed,
            "eq_preset": self._preset_name(),
            "theme": {"name": "default"},
        }
        if track is not None:
            resp["track"] = self._trackinfo(track)
        if not self.legacy:
            resp.update({
                "api": 1,
                "eq": list(self.eq),
                "gen": self.pl.gen,
                "source": dict(self.source),
                "stream_title": self._stream_title(track, now),
                "buffering": buffering,
            })
        return resp

    def _cmd_capabilities(self, req, now):
        return {"ok": True, "api": 1, "commands": sorted(API1_COMMANDS),
                "eq_presets": [name for name, _ in EQ_PRESETS]}

    def _cmd_play(self, req, now):
        if self.state == "paused":  # TUI の play は一時停止の解除だけ
            self.state = "playing"
            self._t_base = now
        return {"ok": True}

    def _cmd_pause(self, req, now):
        if self.state == "playing":
            self._freeze(now)
            self.state = "paused"
        return {"ok": True}

    def _cmd_toggle(self, req, now):
        if self._buffering(now):
            return {"ok": True}
        if self.state == "playing":
            self._freeze(now)
            self.state = "paused"
        elif self.state == "paused":
            self.state = "playing"
            self._t_base = now
        elif len(self.pl):
            if self.pl.queued_idx >= 0 or self.pl.activate_selected():
                self._start_track(now)
        return {"ok": True}

    def _cmd_stop(self, req, now):
        self.state = "stopped"
        self._pos_base = 0.0
        self._t_base = now
        self._buffering_until = 0.0
        return {"ok": True}

    def _cmd_next(self, req, now):
        if self.pl.next():
            self._start_track(now)
        else:
            self._cmd_stop(req, now)
        return {"ok": True}

    def _cmd_prev(self, req, now):
        if self.position(now) > 3 and self.state != "stopped":
            self._pos_base = 0.0
            self._t_base = now
        elif self.pl.prev():
            self._start_track(now)
        return {"ok": True}

    def _cmd_volume(self, req, now):
        self.volume = max(-30.0, min(6.0, float(req.get("value") or 0)))
        return {"ok": True}

    def _seek_abs(self, target: float, now: float) -> None:
        duration = self._duration()
        target = max(0.0, target)
        if duration > 0:
            target = min(target, duration)
        self._pos_base = target
        self._t_base = now

    def _cmd_seek(self, req, now):
        self._seek_abs(self.position(now) + float(req.get("value") or 0), now)
        return {"ok": True}

    def _cmd_seek_to(self, req, now):
        self._seek_abs(float(req.get("value") or 0), now)
        return {"ok": True}

    def _cmd_load(self, req, now):
        name = str(req.get("playlist") or "")
        if not name:
            return {"ok": False, "error": "load requires a playlist name"}
        tracks = self._local_tracks(name)
        if tracks is None:
            return {"ok": False, "error": f'playlist "{name}": open {name}.toml: no such file or directory'}
        self.pl.replace(tracks)
        self.source = {"provider": "local", "id": name, "name": name}
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True, "playlist": name, "total": len(tracks)}

    def _cmd_queue(self, req, now):
        path = str(req.get("path") or "")
        if not path:
            return {"ok": False, "error": "queue requires a path"}
        self.pl.add({"path": path, "title": path})
        return {"ok": True}

    def _cmd_theme(self, req, now):
        if not req.get("name"):
            return {"ok": False, "error": "theme requires a name"}
        return {"ok": True}

    def _cmd_vis(self, req, now):
        if not req.get("name"):
            return {"ok": False, "error": "vis requires a mode name"}
        return {"ok": True, "visualizer": "Bars"}

    def _cmd_shuffle(self, req, now):
        name = str(req.get("name") or "").lower()
        if name == "on" and not self.pl.shuffle or name == "off" and self.pl.shuffle or name not in ("on", "off"):
            self.pl.toggle_shuffle()
        return {"ok": True, "shuffle": self.pl.shuffle}

    def _cmd_repeat(self, req, now):
        name = str(req.get("name") or "").lower()
        if name in ("off", "all", "one"):
            self.pl.repeat = name
        else:
            self.pl.cycle_repeat()
        return {"ok": True, "repeat": self.pl.repeat.capitalize()}

    def _cmd_mono(self, req, now):
        name = str(req.get("name") or "").lower()
        self.mono = True if name == "on" else False if name == "off" else not self.mono
        return {"ok": True, "mono": self.mono}

    def _cmd_speed(self, req, now):
        value = float(req.get("value") or 0)
        if value <= 0:
            return {"ok": False, "error": "speed must be positive"}
        self._freeze(now)
        self.speed = max(0.25, min(2.0, value))
        return {"ok": True, "speed": self.speed}

    def _cmd_eq(self, req, now):
        band = int(req.get("band") or 0)
        name = str(req.get("name") or "")
        if band > 0 or (band == 0 and not name):
            if 0 <= band < 10:
                self.eq[band] = max(-12.0, min(12.0, float(req.get("value") or 0)))
                # パッチ後の cliamp では帯域を触るとカスタムになる想定。
                self.eq_preset_idx = -1
                self.eq_custom = ""
            return {"ok": True, "eq_preset": self._preset_name()}
        for i, (preset, bands) in enumerate(EQ_PRESETS):
            if preset.lower() == name.lower():
                self.eq_preset_idx = i
                self.eq = [float(b) for b in bands]
                break
        else:
            self.eq_preset_idx = -1
            self.eq_custom = name
        return {"ok": True, "eq_preset": self._preset_name()}

    def _cmd_device(self, req, now):
        name = str(req.get("name") or "")
        if not name:
            return {"ok": False, "error": "device requires a name (or 'list')"}
        if name.lower() == "list":
            lines = [("* " if i == self.device else "  ") + d for i, d in enumerate(DEVICES)]
            return {"ok": True, "device": "\n".join(lines)}
        if name not in DEVICES:
            return {"ok": False, "error": f"switch device: device {name!r} not found"}
        self.device = DEVICES.index(name)
        return {"ok": True, "device": name}

    def _cmd_bands(self, req, now):
        playing = self.state == "playing"
        bands = [abs(((now * (i + 1) * 1.7) % 2.0) - 1.0) if playing else 0.0 for i in range(10)]
        return {"ok": True, "visualizer": "Bars", "bands": [round(b, 3) for b in bands]}

    def _cmd_plugin_call(self, req, now):
        return {"ok": False, "error": "plugins not enabled"}

    def _cmd_plugin_commands(self, req, now):
        return {"ok": False, "error": "plugins not enabled"}

    def _cmd_playlist(self, req, now):
        limit = int(req.get("limit") or 0)
        count = len(self.pl) if limit <= 0 else min(limit, len(self.pl))
        tracks = [self._trackinfo(self.pl.tracks[i], queued=self.pl.queue_position(i)) for i in range(count)]
        return {"ok": True, "tracks": tracks, "index": self.pl.index(), "total": len(self.pl),
                "queue": list(self.pl.queue), "up_next": self.pl.upcoming(200), "gen": self.pl.gen,
                "source": dict(self.source)}

    @staticmethod
    def _index(req) -> int | None:
        value = req.get("index")
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _cmd_play_index(self, req, now):
        index = self._index(req)
        if index is None:
            return {"ok": False, "error": "play_index requires an index"}
        if not 0 <= index < len(self.pl):
            return {"ok": False, "error": f"index {index} out of range"}
        self.pl.set_index(index)
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True}

    def _tracks_from(self, req) -> list[dict]:
        return [to_track(t) for t in req.get("tracks") or [] if isinstance(t, dict) and t.get("path")]

    def _cmd_replace(self, req, now):
        tracks = self._tracks_from(req)
        if not tracks:
            return {"ok": False, "error": "replace requires tracks"}
        index = self._index(req) or 0
        if not 0 <= index < len(tracks):
            return {"ok": False, "error": f"index {index} out of range"}
        self._cmd_stop(req, now)
        self.pl.replace(tracks)
        self.pl.start_at(index)
        source = req.get("source") if isinstance(req.get("source"), dict) else {}
        self.source = {k: str(source[k]) for k in ("provider", "id", "name") if source.get(k)}
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    def _queue_next(self, track: dict, now: float) -> None:
        self.pl.add(track)
        self.pl.queue_add(len(self.pl) - 1)
        if self.state == "stopped":
            self._cmd_next({}, now)

    def _cmd_enqueue(self, req, now):
        tracks = self._tracks_from(req)
        if not tracks:
            return {"ok": False, "error": "enqueue requires tracks"}
        mode = str(req.get("mode") or "next").lower()
        if mode == "now":
            self._cmd_stop(req, now)
            self.pl.add(tracks[0])
            self.pl.set_index(len(self.pl) - 1)
            if self.pl.activate_selected():
                self._start_track(now)
            for track in tracks[1:]:
                self._queue_next(track, now)
        elif mode == "next":
            for track in tracks:
                self._queue_next(track, now)
        elif mode == "end":
            for track in tracks:
                was_empty = len(self.pl) == 0
                self.pl.add(track)
                if was_empty or self.state == "stopped":
                    self.pl.set_index(len(self.pl) - 1)
                    if self.pl.activate_selected():
                        self._start_track(now)
        else:
            return {"ok": False, "error": f"unknown enqueue mode {mode!r}"}
        return {"ok": True, "total": len(self.pl)}

    def _cmd_queue_edit(self, req, now):
        mode = str(req.get("mode") or "").lower()
        index = self._index(req)
        if mode == "clear":
            self.pl.clear_queue()
        elif mode in ("add", "remove"):
            if index is None:
                return {"ok": False, "error": f"queue_edit {mode} requires an index"}
            if not 0 <= index < len(self.pl):
                return {"ok": False, "error": f"index {index} out of range"}
            if mode == "add":
                self.pl.queue_add(index)
            else:
                self.pl.dequeue(index)
        elif mode == "move":
            to = req.get("to")
            if index is None or not isinstance(to, int) or isinstance(to, bool):
                return {"ok": False, "error": "queue_edit move requires index and to"}
            if not self.pl.move_queue(index, to):
                return {"ok": False, "error": "queue position out of range"}
        else:
            return {"ok": False, "error": f"unknown queue_edit mode {mode!r}"}
        return {"ok": True, "queue": list(self.pl.queue)}

    def _cmd_remove(self, req, now):
        index = self._index(req)
        if index is None:
            return {"ok": False, "error": "remove requires an index"}
        if not 0 <= index < len(self.pl):
            return {"ok": False, "error": f"index {index} out of range"}
        if index == self.pl.index():
            return {"ok": False, "error": "cannot remove the playing track"}
        self.pl.remove(index)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    # --- カタログ系 ---------------------------------------------------------------

    def _cmd_providers(self, req, now):
        return {"ok": True, "providers": [
            {"key": "radio", "name": "Radio", "search": True, "playlists": True},
            {"key": "local", "name": "Local Playlists", "search": True, "playlists": True},
            {"key": "spotify", "name": "Spotify", "search": True, "playlists": True},
            {"key": "youtube", "name": "YouTube", "search": True, "playlists": False, "virtual": True},
        ]}

    def _local_tracks(self, name: str) -> list[dict] | None:
        if name == "Recently Played":
            return [dict(t) for t, _ in self.history]
        if name in self.local_playlists:
            return [dict(t) for t in self.local_playlists[name]]
        return None

    @staticmethod
    def _needs_auth() -> dict:
        return {"ok": False, "error": "sign-in required", "needs_auth": True}

    @staticmethod
    def _unknown_provider(provider: str) -> dict:
        return {"ok": False, "error": f"unknown provider {provider!r}"}

    def _cmd_playlists(self, req, now):
        provider = str(req.get("provider") or "")
        if provider == "radio":
            lists = [{"id": f"l:{i}", "name": name} for i, (name, _) in enumerate(STATIONS)]
            lists += [{"id": f"f:{i}", "name": f"★ {name} [{rate}] · {country}"}
                      for i, (name, _, rate, country) in enumerate(FAVORITES)]
            return {"ok": True, "playlists": lists}
        if provider == "local":
            lists = []
            if self.history:
                tracks = [t for t, _ in self.history]
                lists.append({"id": "Recently Played", "name": "Recently Played", "track_count": len(tracks),
                              "duration": sum(int(t.get("duration") or 0) for t in tracks)})
            for name, tracks in self.local_playlists.items():
                lists.append({"id": name, "name": name, "track_count": len(tracks),
                              "duration": sum(int(t.get("duration") or 0) for t in tracks)})
            return {"ok": True, "playlists": lists}
        if provider == "spotify":
            if self.spotify_needs_auth:
                return self._needs_auth()
            return {"ok": True, "playlists": [
                {"id": pid, "name": name, "section": section, "track_count": len(tracks),
                 "duration": sum(t["duration"] for t in tracks)}
                for pid, (name, section, tracks) in self.spotify_lists.items()]}
        if provider == "youtube":
            return {"ok": True}
        return self._unknown_provider(provider)

    def _resolve_url(self, url: str) -> list[dict] | None:
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(url)
        query = parse_qs(parts.query)
        vid = (query.get("v") or [""])[0]
        if parts.hostname in ("youtu.be",):
            vid = parts.path.strip("/")
        if not vid:
            return None
        seed = next((t for t in self.library if t["path"].endswith("v=" + vid)), None)
        first = dict(seed) if seed else self._with_art({"path": f"https://www.youtube.com/watch?v={vid}",
                                                         "title": f"動画 {vid}", "artist": "YouTube",
                                                         "duration": 200, "stream": True}, vid)
        first["path"] = f"https://www.youtube.com/watch?v={vid}"
        first["stream"] = True
        if not (query.get("list") or [""])[0].startswith("RD"):
            return [first]
        rng = random.Random(vid)
        others = [t for t in self.library if t["path"] != first["path"] and "youtube" in t["path"]]
        rng.shuffle(others)
        mix = [first]
        for track in others[:24]:
            track = dict(track)
            track["path"] = "https://www.youtube.com/watch?v=" + track["path"].rsplit("v=", 1)[1]
            track["stream"] = True
            track.pop("album", None)
            mix.append(track)
        return mix

    def _provider_tracks(self, provider: str, pid: str) -> dict:
        if provider == "radio":
            if pid.startswith("l:") and pid[2:].isdigit() and int(pid[2:]) < len(STATIONS):
                name, url = STATIONS[int(pid[2:])]
            elif pid.startswith("f:") and pid[2:].isdigit() and int(pid[2:]) < len(FAVORITES):
                name, url, _, _ = FAVORITES[int(pid[2:])]
            else:
                return {"ok": False, "error": f"station {pid!r} not found"}
            return {"ok": True, "tracks": [self._with_art({"path": url, "title": name, "stream": True, "live": True},
                                                          name)]}
        if provider == "local":
            tracks = self._local_tracks(pid)
            if tracks is None:
                return {"ok": False, "error": f"open {pid}.toml: no such file or directory"}
            return {"ok": True, "tracks": tracks}
        if provider == "spotify":
            if self.spotify_needs_auth:
                return self._needs_auth()
            if pid not in self.spotify_lists:
                return {"ok": False, "error": f"spotify: playlist {pid!r} not found"}
            return {"ok": True, "tracks": [dict(t) for t in self.spotify_lists[pid][2]]}
        if provider == "url":
            tracks = self._resolve_url(pid)
            if tracks is None:
                return {"ok": False, "error": f"resolve: unsupported URL {pid!r}"}
            return {"ok": True, "tracks": tracks}
        if provider == "youtube":
            return {"ok": False, "error": "youtube has no playlists"}
        return self._unknown_provider(provider)

    def _cmd_tracks(self, req, now):
        return self._provider_tracks(str(req.get("provider") or ""), str(req.get("id") or ""))

    def _cmd_search(self, req, now):
        provider = str(req.get("provider") or "youtube")
        query = str(req.get("query") or "").strip()
        limit = int(req.get("limit") or 25)
        limit = max(1, min(50, limit))
        if not query:
            return {"ok": False, "error": "search requires a query"}
        if "__fail__" in query:
            return {"ok": False, "error": "yt-dlp: exit status 1"}
        if "noresults" in query.lower() or "該当なし" in query:
            return {"ok": True}
        if provider == "local":
            q = query.casefold()
            seen, found = set(), []
            for tracks in self.local_playlists.values():
                for t in tracks:
                    hay = " ".join(str(t.get(k, "")) for k in ("title", "artist", "album")).casefold()
                    if q in hay and t["path"] not in seen:
                        seen.add(t["path"])
                        found.append(dict(t))
            return {"ok": True, "tracks": found[:limit]}
        if provider == "spotify":
            if self.spotify_needs_auth:
                return self._needs_auth()
            rng = random.Random("spotify|" + query)
            tracks = []
            for i in range(limit):
                title = SEARCH_TEMPLATES[i % 4].format(q=query)
                artist = rng.choice(UPLOADERS)
                tracks.append(self._with_art(
                    {"path": f"spotify:track:{fake_spotify_id(query + str(i))}", "title": title,
                     "artist": artist, "album": f"{query} (Single)", "year": 2020 + i % 6,
                     "duration": rng.randint(150, 330), "track_number": 1}, f"{query}|{i}"))
            return {"ok": True, "tracks": tracks}
        if provider not in ("youtube", "radio", "soundcloud"):
            return self._unknown_provider(provider)
        # radio のように Searcher でないプロバイダーは yt-dlp の YouTube 検索へ退避する。
        rng = random.Random("youtube|" + query)
        tracks = []
        for i in range(limit):
            template = SEARCH_TEMPLATES[(i + rng.randint(0, 3)) % len(SEARCH_TEMPLATES)] if i else "{q}"
            tracks.append(self._with_art(
                {"path": f"https://www.youtube.com/watch?v={fake_youtube_id(query + '|' + str(i))}",
                 "title": template.format(q=query), "artist": rng.choice(UPLOADERS),
                 "duration": rng.randint(120, 420), "stream": True}, f"{query}|{i}"))
        return {"ok": True, "tracks": tracks}

    def _cmd_load_provider(self, req, now):
        provider = str(req.get("provider") or "")
        pid = str(req.get("id") or "")
        result = self._provider_tracks(provider, pid)
        if not result.get("ok"):
            return result
        tracks = result.get("tracks") or []
        if not tracks:
            return {"ok": False, "error": "no tracks"}
        index = self._index(req) or 0
        if not 0 <= index < len(tracks):
            index = 0
        self._cmd_stop(req, now)
        self.pl.replace([to_track(t) for t in tracks])
        self.pl.start_at(index)
        self.source = {"provider": provider, "id": pid, "name": str(req.get("name") or pid)}
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True, "total": len(self.pl)}

    def _cmd_lyrics(self, req, now):
        title = str(req.get("title") or "")
        artist = str(req.get("artist") or "")
        if not title:
            return {"ok": False, "error": "lyrics requires a title"}
        pos = next((i for i, t in enumerate(self.library) if t["title"].casefold() == title.casefold()), None)
        kind = pos % 3 if pos is not None else int(hashlib.sha1(f"{artist}|{title}".encode()).hexdigest(), 16) % 3
        if kind == 2:
            return {"ok": False, "error": "not found"}
        synced = kind == 0
        lines = []
        for i, text in enumerate(LYRIC_LINES):
            line = {"t": round(6.0 + i * 7.25, 2) if synced else 0.0, "text": text}
            lines.append(line)
        return {"ok": True, "lyrics": lines, "synced": synced}

    def _cmd_history(self, req, now):
        limit = int(req.get("limit") or 50)
        return {"ok": True, "tracks": [self._trackinfo(t, played_at=at) for t, at in self.history[:limit]]}

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(name) and name not in (".", "..", "Recently Played") and not any(c in name for c in "/\\")

    def _cmd_playlist_add(self, req, now):
        name = str(req.get("name") or "")
        if not self._valid_name(name):
            return {"ok": False, "error": f"invalid playlist name {name!r}"}
        tracks = self._tracks_from(req)
        if not tracks:
            return {"ok": False, "error": "playlist_add requires tracks"}
        self.local_playlists.setdefault(name, []).extend(tracks)
        return {"ok": True}

    def _cmd_playlist_delete(self, req, now):
        name = str(req.get("name") or "")
        if name not in self.local_playlists:
            return {"ok": False, "error": f"playlist {name!r} not found"}
        del self.local_playlists[name]
        return {"ok": True}

    def _cmd_playlist_remove_track(self, req, now):
        name = str(req.get("name") or "")
        index = self._index(req)
        tracks = self.local_playlists.get(name)
        if tracks is None:
            return {"ok": False, "error": f"playlist {name!r} not found"}
        if index is None or not 0 <= index < len(tracks):
            return {"ok": False, "error": "index out of range"}
        del tracks[index]
        return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="偽の cliamp (PROTOCOL.md の api 1) を立てる")
    parser.add_argument("--socket", required=True, help="待ち受けるソケットのパス")
    parser.add_argument("--legacy", action="store_true", help="拡張の無い cliamp 1.50.0 として振る舞う")
    parser.add_argument("--art-dir", help="この中の画像を曲の meta.art (file://) に割り当てる")
    parser.add_argument("--spotify-needs-auth", action="store_true", help="Spotify を未サインインにする")
    parser.add_argument("--latency", type=float, default=0.0, help="カタログ系の応答の遅れ (秒)")
    parser.add_argument("--buffer", type=float, default=0.0, help="曲の読み込み待ち (秒)")
    parser.add_argument("--state", choices=("playing", "paused", "stopped"), default="playing")
    parser.add_argument("--empty", action="store_true", help="再生中のリストを空で始める")
    args = parser.parse_args(argv)

    server = FakeCliamp(args.socket, legacy=args.legacy, art_dir=args.art_dir,
                        spotify_needs_auth=args.spotify_needs_auth, latency=args.latency,
                        buffer_secs=args.buffer, initial_state=args.state, empty=args.empty)
    server.start()
    print(f"fake-cliamp: {args.socket} で待ち受けています (api {0 if args.legacy else 1})",
          file=sys.stderr, flush=True)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    while not done.is_set():
        done.wait(1.0)
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# 試験の小道具 (tests/test_*.py から使う)


def isolate_display() -> None:
    """試験が利用者の画面 (Wayland や :0) に繋がないようにする。GTK を読む前に呼ぶ。

    Xvfb などの私的な番号 (:10 以上) の DISPLAY があればそれだけを使い、
    無ければ画面なし (GDK_BACKEND=x11 で DISPLAY も無し) にする。
    """
    import re

    display = os.environ.get("DISPLAY", "")
    match = re.match(r"^:(\d+)", display)
    os.environ.pop("WAYLAND_DISPLAY", None)
    os.environ["GDK_BACKEND"] = "x11"
    if not (match and int(match.group(1)) >= 10):
        os.environ.pop("DISPLAY", None)


def temp_socket_path(name: str = "cliamp.sock") -> str:
    """Unix ソケットの長さの上限 (108 バイト) に収まる一時的なパス。"""
    import tempfile

    base = tempfile.mkdtemp(prefix="cm-")
    path = os.path.join(base, name)
    if len(path.encode()) > 100:
        base = tempfile.mkdtemp(prefix="cm-", dir="/tmp")
        path = os.path.join(base, name)
    return path


def run_loop(until, timeout: float = 5.0) -> bool:
    """GLib の main loop を until() が真になるか timeout 秒たつまで回す。"""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if until():
            return True
        time.sleep(0.005)
    while context.pending():
        context.iteration(False)
    return bool(until())
