"""Spotify と YouTube Music から、利用者が選ぶための候補を取得する。"""
from __future__ import annotations

import functools
import re

from gi.repository import GLib

from .protocol import Response, Track, lyrics_query
from .youtube_library import run_async


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _valid_video_id(video_id: object) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", _clean(video_id)))


def _parse_duration(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = _clean(value)
    if not text:
        return None
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        seconds = 0
        for part in parts:
            if not part.isdigit():
                return None
            seconds = seconds * 60 + int(part)
        return seconds
    except ValueError:
        return None


def _youtube_search(query: str) -> list[Track]:
    import requests
    from ytmusicapi import YTMusic

    with requests.Session() as session:
        session.request = functools.partial(session.request, timeout=12)
        songs = YTMusic(requests_session=session).search(query, filter="songs", limit=10)
    tracks: list[Track] = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        if song.get("isAvailable") is False:
            continue
        video_id = _clean(song.get("videoId"))
        if not _valid_video_id(video_id):
            continue
        title = _clean(song.get("title"))
        if not title:
            continue
        artists = song.get("artists") or []
        artist_names = [
            _clean(artist.get("name"))
            for artist in artists
            if isinstance(artist, dict) and _clean(artist.get("name"))
        ]
        album = song.get("album")
        album_name = _clean(album.get("name")) if isinstance(album, dict) else ""
        duration = _parse_duration(song.get("duration_seconds"))
        if duration is None:
            duration = _parse_duration(song.get("duration"))
        if duration is None:
            duration = 0
        tracks.append(
            Track(
                path=f"https://music.youtube.com/watch?v={video_id}",
                title=title,
                artist=", ".join(artist_names),
                album=album_name,
                duration=duration,
            )
        )
    return tracks


def _candidates(tracks: list[Track], provider: str) -> list[Track]:
    seen: set[str] = set()
    candidates: list[Track] = []
    for track in tracks:
        if not _clean(track.title):
            continue
        if provider == "spotify":
            identifier = _clean(track.spotify_id)
        else:
            identifier = _clean(track.youtube_id)
        if not identifier:
            continue
        if track.live or track.unplayable:
            continue
        if identifier in seen:
            continue
        seen.add(identifier)
        candidates.append(track)
        if len(candidates) >= 5:
            break
    return candidates


def search_candidates(ctx, track: Track, provider: str, callback) -> None:
    title = _clean(track.title)
    if not title or provider not in ("spotify", "youtube"):
        def invalid():
            callback([], "曲名がないか、検索先に対応していません。")
            return GLib.SOURCE_REMOVE
        GLib.idle_add(invalid)
        return

    artist = _clean(track.artist)
    if track.youtube_id and not track.spotify_id:
        artist, title = lyrics_query(track) or (artist, title)
    query = f"{artist} {title}".strip()

    if provider == "spotify":
        def on_catalog(result: object) -> None:
            if isinstance(result, Response):
                callback([], result.message)
                return
            if isinstance(result, list):
                callback(_candidates(result, provider), "")
                return
            callback([], "Spotify検索結果を解析できませんでした。再試行してください。")

        ctx.catalog.search("spotify", query, on_catalog, limit=10)
        return

    def on_youtube(result: object, error: object) -> None:
        if error:
            callback([], "YouTube検索に失敗しました。ネットワークを確認して再試行してください。")
            return
        callback(_candidates(result if isinstance(result, list) else [], provider), "")

    run_async(lambda: _youtube_search(query), on_youtube)
