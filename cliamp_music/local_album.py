"""再生中のローカル音源と同じリリースの曲を、アルバムのフォルダから順に読み込む。"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

try:
    import mutagen
except ModuleNotFoundError as error:
    if error.name != "mutagen":
        raise
    mutagen = None

from .protocol import Track

_AUDIO_SUFFIXES = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".aac", ".aacp",
    ".m4b", ".alac", ".wma", ".opus", ".webm",
}
_DISC_RE = re.compile(r"^(?:cd|disc|disk)[\s._-]*(\d{1,3})$", re.IGNORECASE)


def _tag_values(tags, key):
    v = tags.get(key, [])
    if isinstance(v, str):
        v = [v]
    return tuple(s.strip() for s in v if isinstance(s, str) and s.strip())


def _first_tag(tags, key):
    vals = _tag_values(tags, key)
    return vals[0] if vals else ""


def _parse_int(value):
    base = value.split("/")[0].strip()
    if not base.isdecimal() or len(base) > 10:
        return 0
    return int(base)


def _natural_key(text):
    return [int(p) if p.isdecimal() else p for p in re.split(r"(\d+)", text.casefold())]


def can_resolve_local(track: Track) -> bool:
    if mutagen is None or not track or not track.is_local_file or track.live or track.stream or track.unplayable:
        return False
    if track.path.startswith("file://"):
        try:
            if urlsplit(track.path).netloc.lower() not in ("", "localhost"):
                return False
        except ValueError:
            return False
    p = Path(track.local_path)
    return p.is_absolute() and p.suffix.lower() in _AUDIO_SUFFIXES


def _album_directories(current: Path):
    root = current.parent.parent if _DISC_RE.fullmatch(current.parent.name) else current.parent
    yield root
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if _DISC_RE.fullmatch(child.name):
            yield child


def _read_track(path: Path):
    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in _AUDIO_SUFFIXES:
        return None
    try:
        audio = mutagen.File(path, easy=True)
    except (mutagen.MutagenError, OSError, ValueError):
        return None
    if audio is None or not audio.tags or audio.info is None:
        return None
    tags = audio.tags
    artist = _first_tag(tags, "artist") or ""
    album = _first_tag(tags, "album") or ""
    albumartists = _tag_values(tags, "albumartist") or _tag_values(tags, "artist")
    mbid = _first_tag(tags, "musicbrainz_albumid") or ""
    date = _first_tag(tags, "date") or ""
    if not album or not albumartists:
        return None
    key = (album.casefold(), tuple(a.casefold() for a in albumartists), mbid.casefold(), date)
    disc = _parse_int(_first_tag(tags, "discnumber"))
    if not disc:
        m = _DISC_RE.fullmatch(path.parent.name)
        disc = int(m.group(1)) if m and m.group(1) else 1
    n = _parse_int(_first_tag(tags, "tracknumber"))
    raw = os.fsencode(path)
    display = raw.decode("utf-8", "replace")
    raw64 = base64.b64encode(raw).decode("ascii") if display != str(path) else ""
    title = _first_tag(tags, "title") or Path(display).stem
    track = Track(
        path=display,
        path_raw=raw64,
        title=title,
        artist=artist,
        album=album,
        track_number=n,
        duration=int(audio.info.length or 0),
    )
    return (key, disc, n, track)


def resolve_local_album(track: Track) -> tuple[Track, ...]:
    if not can_resolve_local(track):
        raise ValueError("ローカルファイルとして解決できません")
    current = Path(track.local_path)
    if current.parent.is_symlink():
        raise ValueError("リンク先のフォルダからはアルバムを追加できません")
    current_data = _read_track(current)
    if current_data is None:
        raise ValueError("音源のアルバム名とアルバムアーティストを確認できませんでした")
    current_key = current_data[0]
    rows: list[tuple[int, int, list, str, Track]] = []
    seen: set[tuple[int, int]] = set()
    included = False
    try:
        files = (item for folder in _album_directories(current) for item in folder.iterdir())
        for item in files:
            if item == current:
                data = current_data
            else:
                data = _read_track(item)
            if data is None:
                continue
            key, disc, number, song = data
            if key != current_key:
                continue
            if number > 0 and (disc, number) in seen:
                raise ValueError("同じディスク・曲番号が複数あり、アルバムを特定できません")
            seen.add((disc, number))
            rows.append((disc, number if number > 0 else 10**9, _natural_key(item.name), item.name, song))
            if item == current:
                included = True
    except OSError:
        raise ValueError("アルバム内のファイルを確認できませんでした")
    if not included:
        raise ValueError("再生中の曲をアルバム内で確認できませんでした")
    rows.sort(key=lambda row: row[:-1])
    return tuple(row[-1] for row in rows)
