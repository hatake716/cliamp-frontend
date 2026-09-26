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
- ローカルのプレイリスト (TOML) と履歴 (history.toml) は、Go が書く欄だけを持つ
  (live・meta は残らず、stream は path が URL かで決め直す)。最後の曲を外すと
  プレイリストのファイルごと消える。
- 誤りの文言・引数の確かめ方・enqueue / remove / queue_edit / load_provider の細部は
  ipc/gui.go・ui/model/ipc_gui.go に合わせた (tests/test_conformance.py が本物と比べる)。
- `providers` は radio の search が false、local の名前は "Local"、偽の真偽も省かない。
- radio の既定は素の cliamp と同じ l:0 (cliamp radio、15 の配信に展開) と c:N (カタログ)。
  radios_toml=True (--radios-toml) で利用者の局 l:1..l:3 とお気に入り f:0 も出す。
- `device list` は PulseAudio の sink 名 (alsa_output.… など)。"* " は既定の sink で、
  切り替えても動かない (本物は move-sink-input で切り替えるため)。
  device_descriptions=True で説明付きの devices 配列も返す (パッチの拡張の形)。
- path に `__fail__` を含む曲は鳴らせない: 始めると (buffer_secs があれば読み込みの後で)
  止まり、status の playback_error に本物の文言 (YouTube の曲は yt-dlp の年齢確認の誤り、
  それ以外は手元のファイルが無いときの誤り) が入る。本物と同じく次の開始で消え、止めても
  残り、いまの曲が失敗した曲でなければ出さない。

試験の道具: `isolate_display()` (利用者の画面に繋がない)、`temp_socket_path()`、
`temp_dir(prefix)` (どちらも試験の終わりに消える)、
`run_loop(until, timeout)` (GLib の main loop を回す)。FakeCliamp の `requests` に
受けた要求が残り、`delays` で応答を遅らせられる。`seek_delay` (秒) で seek_to を
本物の HTTP の流れのように遅れて効かせ、`switch_keeps_old=True` で読み込み中の
曲の切り替えを本物と同じく「前の曲の位置と長さのまま playing」にする。

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

# cliamp に組み込みの局 (l:0)。本物は https://radio.cliamp.stream/streams.m3u を読み、
# 中の配信 (15 本) へ展開する (resolveWrapperURLs)。題は M3U の EXTINF。
CLIAMP_RADIO = ("cliamp radio", "https://radio.cliamp.stream/streams.m3u")
CLIAMP_STREAMS = ["Lofi", "Meditative", "Synthwave", "EDM", "Omarchy", "Chillout", "Ambient",
                  "Deep House", "Drum & Bass", "Jazz", "Classical", "Retro", "Focus", "Rock", "Sleep"]
# 利用者の radios.toml の局 (l:1 から)。radios_toml=True のときだけ。
STATIONS = [
    ("Harbor Jazz FM", "https://stream.example.net/harbor-jazz.mp3"),
    ("Tokyo Lo-fi Radio", "https://stream.example.net/tokyo-lofi.aac"),
    ("Classic 24", "https://stream.example.net/classic24.ogg"),
]
FAVORITES = [("Tokyo Lo-fi Radio", "https://stream.example.net/tokyo-lofi.aac", "128k", "Japan")]
# Radio Browser のカタログ (c:N)。TUI がカタログを読んだ後にだけ出る。名前は formatCatalogName の形
CATALOG_STATIONS = [
    ("Jazz Sakura (asia dream radio)", "http://stream.example.net/jazz-sakura", "128k", "Japan"),
    ("SomaFM Groove Salad", "http://stream.example.net/groove-salad", "128k", "The United States Of America"),
    ("Radio Swiss Classic", "http://stream.example.net/swiss-classic", "192k", "Switzerland"),
]

ICY_TITLES = [
    "Tape Garden - Coffee & Static",
    "喫茶ムーンライト - Blue Hour Waltz",
    "Aurora Lane - Glass Harbor",
    "真夜中ポスト - シティライト・ブルース",
]

# PulseAudio / PipeWire の sink (pactl list sinks の Name と Description)。本物の `device list`
# は Name だけを返す (ui/model/update.go の DeviceMsg、daemon.go の handleDevice)。
DEVICES = [
    ("alsa_output.pci-0000_0c_00.4.analog-stereo", "内蔵オーディオ アナログステレオ"),
    ("alsa_output.pci-0000_03_00.1.hdmi-stereo", "Navi 32 HDMI/DP Audio デジタルステレオ (HDMI)"),
    ("bluez_output.AC_80_0A_12_34_56.1", "WH-1000XM5"),
]

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

# path にこれを含む曲は鳴らせない (playback_error の試験と画面写真のため)。
FAIL_MARK = "__fail__"


def playback_error_for(path: str) -> str:
    """鳴らせない曲で本物の cliamp が返す誤り。YouTube の曲は yt-dlp の年齢確認
    (cliamp は yt-dlp の stderr を "yt-dlp: " を付けてそのまま包む)、ほかは手元のファイルが無いとき。"""
    if is_ytdl(path):
        vid = fake_youtube_id(path)[:11]
        return (f"yt-dlp: ERROR: [youtube] {vid}: Sign in to confirm your age. This video may be inappropriate "
                "for some users. Use --cookies-from-browser or --cookies for the authentication. See  "
                "https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for how to manually "
                "pass cookies. Also see  https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies"
                "  for tips on effectively exporting YouTube cookies")
    return f"open source: open {path}: no such file or directory"


# 値が偽でも省かない欄 (Go のポインタと、omitempty の無い TrackInfo の path・LyricLine の t と text)。
_KEEP = frozenset({"ok", "shuffle", "mono", "buffering", "synced", "path", "t", "text"})
# 中身を省かない欄 (Go の map[string]string。値が空でも残る)。
_OPAQUE = frozenset({"meta"})
# 要素の欄を省かない配列 (ProviderInfo は omitempty の無い構造体)。
_OPAQUE_LISTS = frozenset({"providers"})


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
        if key in _OPAQUE_LISTS:
            return [dict(v) if isinstance(v, dict) else v for v in value]
        return [omit(v) for v in value]
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return int(value)
    return value


_YTDL_HOSTS = frozenset({"soundcloud.com", "bandcamp.com", "music.163.com", "bilibili.com", "b23.tv"})


def _search_prefix(path: str, name: str) -> bool:
    if not path.startswith(name):
        return False
    rest = path[len(name):]
    colon = rest.find(":")
    return colon >= 0 and all("0" <= c <= "9" for c in rest[:colon])


def is_url(path: str) -> bool:
    """playlist.IsURL: http(s) か yt-dlp の検索式 (ytsearch[N]: / scsearch[N]:)。"""
    path = path or ""
    return (path.startswith(("http://", "https://")) or _search_prefix(path, "ytsearch")
            or _search_prefix(path, "scsearch"))


def is_ytdl(path: str) -> bool:
    """playlist.IsYTDL: yt-dlp で再生する URL (YouTube・YouTube Music・SoundCloud・Bandcamp など)。"""
    from urllib.parse import urlsplit

    if not is_url(path):
        return False
    if _search_prefix(path, "ytsearch") or _search_prefix(path, "scsearch"):
        return True
    try:
        host = (urlsplit(path).hostname or "").lower()
    except ValueError:
        return False
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if host in ("youtube.com", "youtu.be", "music.youtube.com") or host in _YTDL_HOSTS:
        return True
    return host.endswith((".bilibili.com", ".bandcamp.com"))


def go_quote(text: str) -> str:
    """Go の %q に近い引用 (日本語はそのまま)。"""
    return json.dumps(text, ensure_ascii=False)


# history.toml に書かれる欄 (history.go の writeEntry)。
_HISTORY_FIELDS = ("path", "title", "artist", "album", "genre", "year", "track_number", "duration")
# ローカルのプレイリストの TOML に書かれる欄 (external/local/provider.go の writeTrack)。
_LOCAL_FIELDS = _HISTORY_FIELDS + ("feed", "bookmark")


def _persisted(track: dict, fields: tuple[str, ...]) -> dict:
    """Go が TOML に書いて読み戻したときの形。空の欄は落ち、stream は path が URL かで決め直す。
    UTF-8 でない path (path_raw) は TOML でもそのまま残る。"""
    out = {"path": str(track.get("path") or ""), "title": str(track.get("title") or "")}
    if track.get("path_raw"):
        out["path_raw"] = track["path_raw"]
    for name in fields:
        if name in ("path", "title"):
            continue
        value = track.get(name)
        if value:
            out[name] = value
    if is_url(out["path"]):
        out["stream"] = True
    return out


def history_track(track: dict) -> dict:
    return _persisted(track, _HISTORY_FIELDS)


def local_track(track: dict) -> dict:
    return _persisted(track, _LOCAL_FIELDS)


def go_json(obj) -> bytes:
    """Go の json.Marshal と同じ見た目の 1 行 (末尾に改行)。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    text = (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return text.encode("utf-8") + b"\n"


def rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TRACK_FIELDS = ("path", "title", "artist", "album", "genre", "year", "track_number",
                "duration", "stream", "live", "feed", "unplayable", "bookmark", "meta", "path_raw")


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
        # 待ち行列の曲が鳴っている間に order[pos] が消され、後ろが詰まってきた (まだ鳴って
        # いない曲が pos にある) ときは、次の順送りを pos から始める (playlist の resumeAtPos)
        self.resume_at_pos = False

    def bump(self) -> None:
        self.gen += 1

    def resume_slot(self) -> int:
        """次に順送りで鳴らす order の位置の候補の先頭 (orderResumeSlot)。"""
        if self.resume_at_pos and self.queued_idx >= 0:
            return self.pos
        return self.pos + 1

    def replace(self, tracks: list[dict]) -> None:
        self.tracks = list(tracks)
        self.order = list(range(len(self.tracks)))
        self.pos = 0
        self.queue = []
        self.queued_idx = -1
        self.resume_at_pos = False
        if self.shuffle and self.tracks:
            self.do_shuffle()
        self.bump()

    def add_next(self, *tracks: dict) -> int:
        """AddNext: 末尾に足し、再生順ではいまの曲のすぐ後ろ (待ち行列より後) に並べる。"""
        start = len(self.tracks)
        if not tracks:
            return start
        self.tracks.extend(tracks)
        at = self.resume_slot() if self.order else 0
        self.order[at:at] = list(range(start, len(self.tracks)))
        self.bump()
        return start

    def place_next(self, *idxs: int) -> None:
        """PlaceNext: シャッフル中だけ、idxs を再生順でいまの曲のすぐ後ろへ動かす。"""
        if not self.shuffle or not self.order or not idxs:
            return
        start = self.resume_slot()
        moving = [i for i in dict.fromkeys(idxs) if i in self.order[start:]]
        if not moving:
            return
        self.order = self.order[:start] + moving + [i for i in self.order[start:] if i not in moving]
        self.bump()

    def play_next(self, *tracks: dict) -> int:
        """PlayNext (enqueue next): AddNext。1 曲リピートでは順送りしないので待ち行列へ。"""
        if self.repeat != "one":
            return self.add_next(*tracks)
        start = len(self.tracks)
        self.add(*tracks)
        for i in range(len(tracks)):
            self.queue_add(start + i)
        return start

    def add_now(self, *tracks: dict) -> int:
        """AddNow (enqueue now): 足して (シャッフル中はすぐ後ろへ並べて) 先頭の曲を選ぶ。"""
        start = len(self.tracks)
        self.add(*tracks)
        self.place_next(*range(start, len(self.tracks)))
        self.set_index(start)
        return start

    def add_and_select(self, *tracks: dict) -> int:
        """AddAndSelect (止まっているときの enqueue end): 先頭の曲だけすぐ後ろへ並べて選ぶ。"""
        start = len(self.tracks)
        self.add(*tracks)
        self.place_next(start)
        self.set_index(start)
        return start

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
        self.resume_at_pos = False
        return True

    def next(self) -> bool:
        if not self.tracks:
            return False
        for i, idx in enumerate(self.queue):
            if self.playable(idx):
                self.queue = self.queue[i + 1:]
                self.queued_idx = idx
                self.bump()  # 待ち行列が変わる (Next は gen を増やす)
                return True
        if self.queue:
            self.queue = []
            self.bump()
        if self.repeat == "one":
            if self.playable(self.order[self.pos]):
                self.queued_idx = -1
                self.resume_at_pos = False
                return True
            return False
        slot = self._first_playable(self.resume_slot(), len(self.order))
        if slot is None and self.repeat == "all":
            if self.shuffle and self.resume_slot() >= len(self.order):
                self.do_shuffle()
                self.bump()  # nextShuffleWrap: 混ぜ直すと並びが変わる
                slot = self._first_playable(1, len(self.order))
                if slot is None:
                    slot = self._first_playable(0, 1)
            else:
                slot = self._first_playable(0, len(self.order))
        if slot is None:
            return False
        self.queued_idx = -1
        self.pos = slot
        self.resume_at_pos = False
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
        self.resume_at_pos = False
        return True

    def set_index(self, i: int) -> None:
        self.queued_idx = -1
        self.resume_at_pos = False
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
        """playlist.MoveQueueTo: 範囲外は False、同じ位置は何もせず True (gen も増えない)。"""
        if not (0 <= a < len(self.queue) and 0 <= b < len(self.queue)):
            return False
        if a == b:
            return True
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
        elif removed_pos == self.pos and self.queued_idx >= 0 and self.queued_idx != idx:
            # 待ち行列の曲が鳴っている間に、その前のリストの曲を消した: 詰まってきた次の曲は
            # まだ鳴っていないので、次の順送りは pos から
            self.resume_at_pos = True
        if self.pos >= len(self.order):
            self.resume_at_pos = False
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
        self.bump()  # CycleRepeat はいつも gen を増やす

    def set_repeat(self, mode: str) -> None:
        if mode != self.repeat:
            self.bump()  # SetRepeat は変わったときだけ
        self.repeat = mode

    def upcoming(self, limit: int = 200) -> list[int]:
        """playlist.Upcoming: 再生順で今の位置の後に来る曲。待ち行列の曲と再生できない曲は除く。

        待ち行列から鳴っている曲 (queued_idx) は待ち行列から外れているので、リスト上の
        後ろの位置にあれば含む (本物と同じ)。シャッフル中の回り込みは含めない。"""
        if not self.order:
            return []
        after = self.order[self.resume_slot():]
        if self.repeat == "all" and not self.shuffle:
            after += self.order[: self.pos]
        return [i for i in after if i not in self.queue and self.playable(i)][:limit]


class FakeCliamp:
    """偽の cliamp。start() で待ち受け、stop() で閉じる (状態は残るので restart() で戻れる)。

    試験の道具:
      requests  受け取った要求 (dict) の記録。
      delays    {コマンド: 秒}。応答を遅らせる (時間切れの試験)。
      latency   カタログ系のコマンドすべてに足す遅れ。
    """

    # ローカルのプレイリストの置き場 (誤りの文言に出る。本物は ~/.config/cliamp/playlists)
    PLAYLIST_DIR = "/home/fake/.config/cliamp/playlists"

    def __init__(self, socket_path: str, *, legacy: bool = False, art_dir: str | None = None,
                 spotify_needs_auth: bool = False, latency: float = 0.0, buffer_secs: float = 0.0,
                 initial_state: str = "playing", empty: bool = False, seed: int = 7,
                 radios_toml: bool = False, device_descriptions: bool = False,
                 switch_keeps_old: bool = False):
        self.socket_path = socket_path
        self.legacy = legacy
        self.spotify_needs_auth = spotify_needs_auth
        self.latency = latency
        self.buffer_secs = buffer_secs
        self.radios_toml = radios_toml
        self.device_descriptions = device_descriptions
        # 読み込み中の曲の切り替えで、前の曲の位置・長さ・playing を出し続ける (本物の TUI)
        self.switch_keeps_old = switch_keeps_old
        # seek_to を受け付けてから効くまでの秒 (HTTP の流れの再接続。0 ならすぐ)
        self.seek_delay = 0.0
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
        self.default_device = 0  # 既定の sink ("* ")。GUI の切り替えでは動かない
        self.device = 0  # cliamp の流れがいま出ている sink
        self._seek_pending: tuple[float, float] | None = None  # (効く時刻, 位置)
        self._old: tuple[float, float, float] | None = None  # 読み込み中に出す前の曲 (位置, 時刻, 長さ)
        # 再生の失敗 (本物の ipc.PlaybackFailure): 失敗した曲の path と誤り。_failing は
        # 読み込みの終わりで失敗する開始が進んでいる印
        self.playback_error = ""
        self._error_path = ""
        self._failing = False
        self.source: dict = {}
        self.history: list[tuple[dict, str]] = []
        self.local_playlists: dict[str, list[dict]] = {
            "ドライブ": [local_track(self.library[i]) for i in (1, 2, 5, 10, 17, 21, 12)],
            "Focus": [local_track(self.library[i]) for i in (9, 14, 7, 16, 27, 22)],
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
        self.history = [(history_track(self.library[i]), rfc3339(now - delta)) for i, delta in zip(picks, ago)]

    def _stand_in_art(self, track: dict) -> dict:
        """撮影用 (--art-dir) の絵を読み戻した曲に付け直す。

        本物の履歴とローカルのプレイリストは meta を持たない (絵は path から GUI が探す)。
        撮影ではネットワークに出ないので、手元の絵を同じ path の曲に割り当てて見せる。"""
        if not self._art or track.get("meta"):
            return track
        for i, known in enumerate(self.library):
            if known["path"] == track.get("path"):
                return dict(track, meta={"art": self._art[i % len(self._art)]})
        return track

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
        if self._seek_pending is not None and now < self._seek_pending[0]:
            return self._pos_base  # 繋ぎ直しの間は古い位置のまま止まる (gapless.Replace(nil))
        if self.state == "playing" and not self._buffering(now):
            return max(0.0, self._pos_base + max(0.0, now - self._t_base) * self.speed)
        return self._pos_base

    def _duration(self) -> float:
        track, _ = self.pl.current()
        if track is None or track.get("live"):
            return 0.0
        return float(track.get("duration") or 0)

    def _old_position(self, now: float) -> float:
        """読み込み中に本物が出す、前の曲の (まだ鳴っている) 位置。"""
        pos, base, duration = self._old
        value = pos + max(0.0, now - base) * self.speed
        return min(value, duration) if duration > 0 else value

    def _play_current(self, at: float, keep_old: tuple[float, float, float] | None = None) -> None:
        self._old = keep_old if self.buffer_secs > 0 else None
        self.state = "playing"
        self._pos_base = 0.0
        self._scrobbled = False
        self._seek_pending = None
        self._playing_duration = self._duration()
        # 開始のたびに前の失敗を消す (本物の beginPlay)
        self.playback_error = ""
        self._error_path = ""
        track, _ = self.pl.current()
        self._failing = bool(track and FAIL_MARK in str(track.get("path") or ""))
        if self.buffer_secs > 0:
            self._buffering_until = at + self.buffer_secs
            self._t_base = at + self.buffer_secs
        else:
            self._buffering_until = 0.0
            self._t_base = at
            if self._failing:
                self._fail(at)

    def _fail(self, at: float) -> None:
        """いまの曲の開始が失敗した: 止まり、理由を playback_error に置く。"""
        track, _ = self.pl.current()
        path = str(track.get("path") or "") if track else ""
        self._failing = False
        self.state = "stopped"
        self._pos_base = 0.0
        self._t_base = at
        self._buffering_until = 0.0
        self._old = None
        self.playback_error = playback_error_for(path)
        self._error_path = path

    def _freeze(self, now: float) -> None:
        self._pos_base = self.position(now)
        self._t_base = now

    def _tick(self, now: float) -> None:
        """再生位置を今に進め、曲の終わりを過ぎていれば次へ (何曲分でも)。"""
        if self._seek_pending is not None and now >= self._seek_pending[0]:
            at, target = self._seek_pending
            self._seek_pending = None
            self._seek_abs(target, at)
        if self._failing and self.state == "playing" and not self._buffering(now):
            self._fail(self._buffering_until or now)
        if self._old is not None and not self._buffering(now):
            self._old = None
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
        """半分まで聞いた曲を履歴へ (cliamp の maybeScrobble と history.Record と同じ)。

        history.toml に書く欄だけを残す。直前の行と同じ曲を 5 分以内にもう一度記録したときは
        時刻を新しくし、空の欄は前の行から埋める (mergeTrackMeta)。"""
        self._scrobbled = True
        track, _ = self.pl.current()
        if track is None or track.get("live"):
            return
        now = datetime.now(timezone.utc)
        entry = history_track(track)
        if self.history and self.history[0][0].get("path") == entry["path"]:
            top, at = self.history[0]
            try:
                last = datetime.fromisoformat(at.replace("Z", "+00:00"))
            except ValueError:
                last = None
            if last is not None and now - last < timedelta(minutes=5):
                merged = dict(top)
                merged.update({k: v for k, v in entry.items() if v})
                self.history[0] = (merged, rfc3339(now))
                return
        self.history.insert(0, (entry, rfc3339(now)))
        del self.history[200:]

    def _start_track(self, now: float, previous: tuple[str, float, float] | None = None) -> None:
        """操作 (next / prev / play_index / enqueue now など) で曲を始める。

        previous は操作の前の (状態, 位置, 長さ)。switch_keeps_old なら、本物の TUI と同じく
        読み込み中は前の曲の位置と長さで playing を出し続ける (Stop せずに裏で読み込むため)。"""
        keep = None
        if self.switch_keeps_old and previous is not None and previous[0] == "playing":
            keep = (previous[1], now, previous[2])
        self._play_current(now, keep)

    def _before(self, now: float) -> tuple[str, float, float]:
        """曲を替える操作の前の (状態, 位置, 長さ)。読み込み中は前の曲のものを引き継ぐ。"""
        if self._buffering(now):
            if self._old is not None:
                return ("playing", self._old_position(now), self._old[2])
            return ("loading", 0.0, 0.0)
        return (self.state, self.position(now), getattr(self, "_playing_duration", self._duration()))

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
        state, position, duration = self.state, self.position(now), self._duration()
        if buffering:
            if self._old is not None:
                # 本物の TUI: 前の曲がまだ鳴っていて、位置と長さはその曲のもの
                state, position, duration = "playing", self._old_position(now), self._old[2]
            else:
                state = "stopped"  # 止まった状態から読み込み中
        resp = {
            "ok": True,
            "state": state,
            "position": round(position, 3),
            "duration": duration,
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
                # 本物と同じく、いまの曲が失敗した曲のときだけ
                "playback_error": (self.playback_error
                                   if track is not None and track.get("path") == self._error_path else ""),
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
        self._old = None
        self._seek_pending = None
        self._failing = False  # 読み込み中の開始を取り消す (失敗は残る。本物も stop では消さない)
        return {"ok": True}

    def _cmd_next(self, req, now):
        before = self._before(now)
        if self.pl.next():
            self._start_track(now, before)
        else:
            self._cmd_stop(req, now)
        return {"ok": True}

    def _cmd_prev(self, req, now):
        before = self._before(now)
        if self.position(now) > 3 and self.state != "stopped":
            self._pos_base = 0.0
            self._t_base = now
        elif self.pl.prev():
            self._start_track(now, before)
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
        target = float(req.get("value") or 0)
        if target < 0:
            return {"ok": False, "error": "seek_to requires a non-negative position"}
        if self._buffering(now) and self._old is not None:
            # 読み込み中の seek は前の曲 (まだ鳴っている流れ) に効き、新しい曲では失われる
            self._old = (target, now, self._old[2])
            return {"ok": True}
        if self.seek_delay > 0 and self.state == "playing":
            # 本物の HTTP の流れ: すぐ受け付け、古い位置で止まったまま繋ぎ直し、後で効く
            self._freeze(now)
            self._seek_pending = (now + self.seek_delay, target)
            return {"ok": True}
        self._seek_abs(target, now)
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
            self.pl.set_repeat(name)
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
        names = [sink for sink, _ in DEVICES]
        if name.lower() == "list":
            # "* " は既定の sink (pactl の Default Sink)。cliamp の流れの行き先ではない
            lines = [("* " if i == self.default_device else "  ") + sink for i, sink in enumerate(names)]
            reply = {"ok": True, "device": "\n".join(lines)}
            if self.device_descriptions:
                reply["devices"] = [{"name": sink, "description": text, "active": i == self.default_device}
                                    for i, (sink, text) in enumerate(DEVICES)]
            return reply
        if name not in names:
            return {"ok": False, "error": f"switch device: sink {go_quote(name)} not found"}
        # 本物は cliamp の sink-input を move-sink-input で動かす (既定の sink は変えない)
        self.device = names.index(name)
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
            return {"ok": False, "error": "index out of range"}
        if self._stale(index, req):
            return {"ok": False, "error": "stale"}
        if self._buffering(now) and index == self.pl.index():
            return {"ok": True}  # その曲をいま読み込んでいる (TUI の Enter と同じく何もしない)
        before = self._before(now)
        self.pl.set_index(index)
        if self.pl.activate_selected():
            self._start_track(now, before)
        return {"ok": True}

    def _tracks_from(self, req, cmd: str) -> tuple[list[dict], dict | None]:
        """ipc/gui.go の tracksFromRequest: path の無い曲は誤り。yt-dlp でない http(s) には stream を立てる。"""
        out = []
        for i, item in enumerate(req.get("tracks") or []):
            if not isinstance(item, dict) or not str(item.get("path") or "").strip():
                return [], {"ok": False, "error": f"{cmd}: track {i} has no path"}
            if item.get("path_raw"):
                try:
                    base64.b64decode(str(item["path_raw"]), validate=True)
                except ValueError:
                    return [], {"ok": False, "error": f"{cmd}: track {i} has a bad path_raw"}
            track = to_track(item)
            if not track.get("stream") and is_url(track["path"]) and not is_ytdl(track["path"]):
                track["stream"] = True
            out.append(track)
        return out, None

    def _cmd_replace(self, req, now):
        tracks, error = self._tracks_from(req, "replace")
        if error:
            return error
        if not tracks:
            return {"ok": False, "error": "replace requires tracks"}
        index = self._index(req)
        index = 0 if index is None else index
        if not 0 <= index < len(tracks):
            return {"ok": False, "error": "index out of range"}
        self._cmd_stop(req, now)
        self.pl.replace(tracks)
        self.pl.start_at(index)
        source = req.get("source") if isinstance(req.get("source"), dict) else {}
        self.source = {k: str(source[k]) for k in ("provider", "id", "name") if source.get(k)}
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    def _cmd_enqueue(self, req, now):
        """ui/model/ipc_gui.go の guiEnqueue と同じ並べ方 (playlist/gui.go の PlayNext / AddNow /
        AddAndSelect)。どれも末尾に足す (既存の曲の添字は動かない)。

        next: 待ち行列を使わず、再生順でいまの曲のすぐ後ろに並べる (1 曲リピートだけ待ち行列へ)。
        now: 先頭の曲をすぐ鳴らし、残りは足した順にそのすぐ後ろで鳴る。
        end: 足すだけ。止まっていれば足した先頭の曲から鳴らす。"""
        tracks, error = self._tracks_from(req, "enqueue")
        if error:
            return error
        if not tracks:
            return {"ok": False, "error": "enqueue requires tracks"}
        raw = str(req.get("mode") or "")
        mode = raw.lower() or "next"
        if mode not in ("next", "end", "now"):
            return {"ok": False, "error": f"enqueue: unknown mode {go_quote(raw)}"}
        idle = self.state == "stopped" and not self._buffering(now)
        if mode == "next" and len(self.pl) == 0:
            mode = "end"  # 空のリストに「次に再生」は、末尾に足して先頭から鳴らすのと同じ
        if mode == "now":
            self._cmd_stop(req, now)
            self.pl.add_now(*tracks)
            if self.pl.activate_selected():
                self._start_track(now)
        elif mode == "end":
            if idle:
                self.pl.add_and_select(*tracks)
                if self.pl.activate_selected():
                    self._start_track(now)
            else:
                self.pl.add(*tracks)
        else:
            self.pl.play_next(*tracks)
            if idle:
                self._cmd_next({}, now)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    def _stale(self, index: int, req) -> bool:
        """ipc.PathMismatch: 要求の path が空でなく、その添字の曲の path と違う。"""
        want = str(req.get("path") or "")
        return bool(want) and 0 <= index < len(self.pl) and self.pl.tracks[index].get("path") != want

    def _cmd_queue_edit(self, req, now):
        raw = str(req.get("mode") or "")
        mode = raw.lower()
        index = self._index(req)
        if mode in ("add", "remove"):
            if index is None:
                return {"ok": False, "error": f"queue_edit {mode} requires an index"}
            if not 0 <= index < len(self.pl):
                return {"ok": False, "error": "index out of range"}
            if self._stale(index, req):
                return {"ok": False, "error": "stale"}
            if mode == "add":
                self.pl.queue_add(index)
            else:
                self.pl.dequeue(index)
        elif mode == "move":
            to = req.get("to")
            if index is None or not isinstance(to, int) or isinstance(to, bool):
                return {"ok": False, "error": "queue_edit move requires index and to"}
            if 0 <= index < len(self.pl.queue) and self._stale(self.pl.queue[index], req):
                return {"ok": False, "error": "stale"}
            if not self.pl.move_queue(index, to):
                return {"ok": False, "error": "queue position out of range"}
        elif mode == "clear":
            self.pl.clear_queue()
        else:
            return {"ok": False, "error": f"queue_edit: unknown mode {go_quote(raw)}"}
        return {"ok": True, "queue": list(self.pl.queue), "gen": self.pl.gen}

    def _cmd_remove(self, req, now):
        index = self._index(req)
        if index is None:
            return {"ok": False, "error": "remove requires an index"}
        if not 0 <= index < len(self.pl):
            return {"ok": False, "error": "index out of range"}
        if self._stale(index, req):
            return {"ok": False, "error": "stale"}
        # 消せないのは再生中・一時停止中・読み込み中の今の曲だけ (止まっていれば消せる)
        if index == self.pl.index() and (self.state != "stopped" or self._buffering(now)):
            return {"ok": False, "error": "cannot remove the current track"}
        self.pl.remove(index)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    # --- カタログ系 ---------------------------------------------------------------

    def _cmd_providers(self, req, now):
        # radio は Searcher でない (search は YouTube へ退避するだけ) ので search は false。
        # ProviderInfo は omitempty の無い構造体なので、偽の真偽も省かない (omit の _OPAQUE_LISTS)
        return {"ok": True, "providers": [
            {"key": "radio", "name": "Radio", "search": False, "playlists": True, "virtual": False},
            {"key": "local", "name": "Local", "search": True, "playlists": True, "virtual": False},
            {"key": "spotify", "name": "Spotify", "search": True, "playlists": True, "virtual": False},
            {"key": "youtube", "name": "YouTube", "search": True, "playlists": False, "virtual": True},
        ]}

    def _local_tracks(self, name: str) -> list[dict] | None:
        if name == "Recently Played":
            return [self._stand_in_art(dict(t)) for t, _ in self.history]
        if name in self.local_playlists:
            return [self._stand_in_art(dict(t)) for t in self.local_playlists[name]]
        return None

    def _toml_path(self, name: str) -> str:
        return f"{self.PLAYLIST_DIR}/{name}.toml"

    def _local_name_error(self, name: str) -> dict | None:
        """external/local の予約名と safePath の確かめ (誤りの応答か None)。"""
        if name == "Recently Played":
            return {"ok": False, "error": '"Recently Played" is a virtual history playlist and cannot be modified'}
        if not name or name in (".", "..") or any(c in name for c in "/\\"):
            return {"ok": False, "error": f"invalid playlist name {go_quote(name)}"}
        return None

    @staticmethod
    def _needs_auth() -> dict:
        return {"ok": False, "error": "sign-in required", "needs_auth": True}

    @staticmethod
    def _unknown_provider(provider: str) -> dict:
        return {"ok": False, "error": f"unknown provider: {provider}"}

    def _radio_lists(self) -> list[dict]:
        lists = [{"id": "l:0", "name": CLIAMP_RADIO[0]}]
        if self.radios_toml:
            lists += [{"id": f"l:{i + 1}", "name": name} for i, (name, _) in enumerate(STATIONS)]
            lists += [{"id": f"f:{i}", "name": f"★ {name} [{rate}] · {country}"}
                      for i, (name, _, rate, country) in enumerate(FAVORITES)]
        lists += [{"id": f"c:{i}", "name": f"{name} [{rate}] · {country}"}
                  for i, (name, _, rate, country) in enumerate(CATALOG_STATIONS)]
        return lists

    def _cmd_playlists(self, req, now):
        provider = str(req.get("provider") or "")
        if not provider:
            return {"ok": False, "error": "playlists requires a provider"}
        if provider == "radio":
            return {"ok": True, "playlists": self._radio_lists()}
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
        if provider in ("youtube", "url"):
            return {"ok": False, "error": f"provider {provider} has no playlists"}
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

    def _radio_tracks(self, pid: str) -> list[dict] | None:
        """radio の局の曲。l:0 は組み込みの M3U を 15 本の配信に展開したもの (resolveWrapperURLs)。"""
        def station(name: str, url: str) -> dict:
            return self._with_art({"path": url, "title": name, "stream": True, "live": True}, name)

        if pid == "l:0":
            return [station(name, f"https://radio.cliamp.stream/{name.lower().replace(' & ', '-').replace(' ', '-')}"
                                  f"/stream") for name in CLIAMP_STREAMS]
        kind, _, number = pid.partition(":")
        if not number.isdigit():
            return None
        n = int(number)
        if kind == "l" and self.radios_toml and 1 <= n <= len(STATIONS):
            return [station(*STATIONS[n - 1])]
        if kind == "f" and self.radios_toml and n < len(FAVORITES):
            name, url, _, _ = FAVORITES[n]
            return [station(name, url)]
        if kind == "c" and n < len(CATALOG_STATIONS):
            name, url, _, _ = CATALOG_STATIONS[n]
            return [station(name, url)]
        return None

    def _provider_tracks(self, provider: str, pid: str) -> dict:
        if provider == "radio":
            tracks = self._radio_tracks(pid)
            if tracks is None:
                return {"ok": False, "error": f"station {go_quote(pid)} not found"}
            return {"ok": True, "tracks": tracks}
        if provider == "local":
            tracks = self._local_tracks(pid)
            if tracks is None:
                bad = self._local_name_error(pid) if pid != "Recently Played" else None
                if bad is not None and "invalid" in bad["error"]:
                    return bad
                return {"ok": False, "error": f"open {self._toml_path(pid)}: no such file or directory"}
            return {"ok": True, "tracks": tracks}
        if provider == "spotify":
            if self.spotify_needs_auth:
                return self._needs_auth()
            if pid not in self.spotify_lists:
                return {"ok": False, "error": f"spotify: playlist {go_quote(pid)} not found"}
            return {"ok": True, "tracks": [dict(t) for t in self.spotify_lists[pid][2]]}
        if provider == "url":
            url = pid.strip()
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "error": f"url: not an http(s) URL: {go_quote(url)}"}
            tracks = self._resolve_url(url)
            if tracks is None:
                return {"ok": False, "error": f"resolve: unsupported URL {go_quote(url)}"}
            return {"ok": True, "tracks": tracks}
        if provider == "youtube":
            return {"ok": False, "error": "provider youtube has no playlists"}
        return self._unknown_provider(provider)

    def _cmd_tracks(self, req, now):
        provider = str(req.get("provider") or "")
        pid = str(req.get("id") or "")
        if not provider or not pid:
            return {"ok": False, "error": "tracks requires a provider and an id"}
        return self._provider_tracks(provider, pid)

    def _cmd_search(self, req, now):
        provider = str(req.get("provider") or "") or "youtube"  # 空なら youtube (本物と同じ)
        query = str(req.get("query") or "").strip()
        limit = int(req.get("limit") or 25)
        limit = max(1, min(50, limit))
        if not query:
            return {"ok": False, "error": "search requires a query"}
        if provider == "url":
            return {"ok": False, "error": "provider url does not support search"}
        if provider not in ("youtube", "radio", "soundcloud", "local", "spotify"):
            return self._unknown_provider(provider)
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
                        found.append(self._stand_in_art(dict(t)))
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
        if not provider or not pid:
            return {"ok": False, "error": "load_provider requires a provider and an id"}
        result = self._provider_tracks(provider, pid)
        if not result.get("ok"):
            return result
        tracks = result.get("tracks") or []
        if not tracks:
            return {"ok": False, "error": "no tracks"}
        index = self._index(req)
        index = 0 if index is None else index
        if not 0 <= index < len(tracks):
            return {"ok": False, "error": "index out of range"}  # 再生は止めない・リストは替えない
        self._cmd_stop(req, now)
        self.pl.replace([to_track(t) for t in tracks])
        self.pl.start_at(index)
        self.source = {"provider": provider, "id": pid, "name": str(req.get("name") or pid)}
        if self.pl.activate_selected():
            self._start_track(now)
        return {"ok": True, "total": len(self.pl), "gen": self.pl.gen}

    def _cmd_lyrics(self, req, now):
        title = str(req.get("title") or "")
        artist = str(req.get("artist") or "")
        if not title.strip() and not artist.strip():
            return {"ok": False, "error": "lyrics requires an artist or a title"}
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
        return {"ok": True, "tracks": [self._trackinfo(self._stand_in_art(dict(t)), played_at=at)
                                       for t, at in self.history[:limit]]}

    def _cmd_playlist_add(self, req, now):
        """ipc/gui.go の順で確かめ、external/local の AddTracks と同じく TOML の欄だけを足す。"""
        name = str(req.get("name") or "")
        if not name:
            return {"ok": False, "error": "playlist_add requires a name"}
        tracks, error = self._tracks_from(req, "playlist_add")
        if error:
            return error
        if not tracks:
            return {"ok": False, "error": "playlist_add requires tracks"}
        bad = self._local_name_error(name)
        if bad is not None:
            return bad
        self.local_playlists.setdefault(name, []).extend(local_track(t) for t in tracks)
        return {"ok": True}

    def _cmd_playlist_delete(self, req, now):
        name = str(req.get("name") or "")
        if not name:
            return {"ok": False, "error": "playlist_delete requires a name"}
        bad = self._local_name_error(name)
        if bad is not None:
            return bad
        if name not in self.local_playlists:
            return {"ok": False, "error": f"remove {self._toml_path(name)}: no such file or directory"}
        del self.local_playlists[name]
        return {"ok": True}

    def _cmd_playlist_remove_track(self, req, now):
        name = str(req.get("name") or "")
        index = self._index(req)
        if not name or index is None:
            return {"ok": False, "error": "playlist_remove_track requires a name and an index"}
        bad = self._local_name_error(name)
        if bad is not None:
            return bad
        tracks = self.local_playlists.get(name)
        if tracks is None:
            return {"ok": False, "error": f"open {self._toml_path(name)}: no such file or directory"}
        if not 0 <= index < len(tracks):
            return {"ok": False, "error": f"track index {index} out of range"}
        want = str(req.get("path") or "")
        if want and tracks[index].get("path") != want:
            return {"ok": False, "error": "stale"}
        del tracks[index]
        if not tracks:
            # external/local の RemoveTrack: 空になったプレイリストはファイルごと消える
            del self.local_playlists[name]
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
    parser.add_argument("--radios-toml", action="store_true",
                        help="利用者の radios.toml の局とお気に入りも出す (l:1〜、f:0)")
    parser.add_argument("--device-descriptions", action="store_true",
                        help="device list に説明付きの devices 配列も付ける")
    parser.add_argument("--switch-keeps-old", action="store_true",
                        help="読み込み中の曲の切り替えで前の曲の位置と長さを出す (本物の TUI)")
    args = parser.parse_args(argv)

    server = FakeCliamp(args.socket, legacy=args.legacy, art_dir=args.art_dir,
                        spotify_needs_auth=args.spotify_needs_auth, latency=args.latency,
                        buffer_secs=args.buffer, initial_state=args.state, empty=args.empty,
                        radios_toml=args.radios_toml, device_descriptions=args.device_descriptions,
                        switch_keeps_old=args.switch_keeps_old)
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


def temp_dir(prefix: str = "cm-", parent: str | None = None) -> str:
    """一時ディレクトリ。試験の終わり (プロセスの終わり) に消す。

    後始末を試験ごとにしないのは、絵の読み込みなどの裏のスレッドが試験の後にも
    書き込むことがあるため (消した後に書くと、そのスレッドで例外になる)。"""
    import atexit
    import shutil
    import tempfile

    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    atexit.register(shutil.rmtree, path, True)
    return path


def temp_socket_path(name: str = "cliamp.sock") -> str:
    """Unix ソケットの長さの上限 (108 バイト) に収まる一時的なパス (終わりに消す)。"""
    import tempfile

    base = tempfile.gettempdir()
    if len(os.path.join(base, "cm-xxxxxxxx", name).encode()) > 100:
        base = "/tmp"
    return os.path.join(temp_dir("cm-", base), name)


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
