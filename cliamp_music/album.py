"""アルバム解決と選択。"""

from __future__ import annotations

import functools
import threading
from html.parser import HTMLParser

from gi.repository import GLib

from . import spotify_import
from .protocol import Track, Response, youtube_id_of


def can_resolve_album(track: Track) -> bool:
    if not track or track.unplayable or track.live:
        return False
    aid = track.meta_get("spotify.album_id")
    if spotify_import.is_spotify_id(aid):
        return True
    return bool(track.path and (track.spotify_id or track.youtube_id))


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.album = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.album:
            return
        d = {k.lower(): v for k, v in attrs if k}
        if d.get("name") == "music:album" or d.get("property") == "music:album":
            self.album = d.get("content")


def _resolve_spotify_ref(track: Track) -> spotify_import.SpotifyRef:
    aid = track.meta_get("spotify.album_id")
    if spotify_import.is_spotify_id(aid):
        return spotify_import.SpotifyRef("album", aid)
    sid = track.spotify_id
    if not sid:
        raise ValueError("アルバムを特定できません")
    url = f"https://open.spotify.com/track/{sid}"
    try:
        html = spotify_import.fetch_text(url, user_agent=spotify_import.PAGE_USER_AGENT)
    except Exception:
        raise ValueError("Spotify ページの取得に失敗しました")
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:
        raise ValueError("Spotify ページの解析に失敗しました")
    if not p.album:
        raise ValueError("アルバム情報を特定できません")
    try:
        ref = spotify_import.parse_spotify_url(p.album)
    except spotify_import.SpotifyImportError:
        raise ValueError("アルバム URL が不正です")
    if ref.kind != "album":
        raise ValueError("アルバムではありません")
    return ref


def resolve_album(track: Track) -> tuple[Track, ...]:
    if not track:
        raise ValueError("トラックがありません")
    if track.youtube_id and not track.spotify_id:
        return _resolve_youtube(track)
    ref = _resolve_spotify_ref(track)
    try:
        listed = spotify_import.fetch_list(ref)
    except spotify_import.SpotifyImportError as e:
        raise ValueError(e.message) from e
    if listed.id != ref.id or listed.kind != "album" or listed.truncated or listed.skipped or not listed.tracks:
        raise ValueError("アルバムを正しく取得できませんでした")
    if track.spotify_id and not any(t.spotify_id == track.spotify_id for t in listed.tracks):
        raise ValueError("アルバムを正しく取得できませんでした")
    return tuple(listed.tracks)


def _resolve_youtube(track: Track) -> tuple[Track, ...]:
    vid = track.youtube_id
    try:
        from ytmusicapi import YTMusic
        from requests import Session
    except ImportError:
        raise ValueError("YouTube 解決ライブラリがありません")
    with Session() as s:
        s.request = functools.partial(s.request, timeout=12)
        try:
            yt = YTMusic(requests_session=s)
            watch = yt.get_watch_playlist(videoId=vid, limit=1)
            if not watch or "tracks" not in watch:
                raise ValueError("関連トラックがありません")
            item = None
            for t in watch["tracks"]:
                if t.get("videoId") == vid:
                    item = t
                    break
            if not item:
                raise ValueError("動画が見つかりません")
            album_info = item.get("album")
            if not album_info or not album_info.get("id"):
                raise ValueError("アルバム ID がありません")
            album = yt.get_album(album_info["id"])
        except ValueError:
            raise
        except Exception:
            raise ValueError("YouTube アルバムの取得に失敗しました")
    if not album:
        raise ValueError("アルバムデータがありません")
    title = album.get("title", "")
    artists = album.get("artists", [])
    artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    track_count = album.get("trackCount", 0)
    album_tracks = album.get("tracks", [])
    if not album_tracks:
        raise ValueError("トラックがありません")
    result = []
    for idx, t in enumerate(album_tracks):
        if not isinstance(t, dict):
            raise ValueError("トラック形式が不正です")
        if t.get("isAvailable") is False:
            raise ValueError("再生できないトラックがあります")
        v_id = t.get("videoId")
        if not v_id or len(v_id) != 11 or youtube_id_of(f"https://music.youtube.com/watch?v={v_id}") != v_id:
            raise ValueError("動画 ID が不正です")
        t_artists = t.get("artists", [])
        t_artist = ", ".join(a.get("name", "") for a in t_artists if a.get("name")) or artist_name
        result.append(Track(
            path=f"https://music.youtube.com/watch?v={v_id}",
            title=t.get("title", ""),
            artist=t_artist,
            album=title,
            track_number=int(t.get("trackNumber") or idx + 1),
            duration=int(t.get("duration_seconds") or 0),
            stream=True,
        ))
    if track_count and len(result) != track_count:
        raise ValueError("トラック数が一致しません")
    return tuple(result)


def choose_album(ctx, track: Track) -> None:
    if not track:
        return
    ctx.toast("アルバムの曲を読み込んでいます…")

    def _fallback():
        def _worker():
            try:
                res = resolve_album(track)
                err = None
            except ValueError as exc:
                res = None
                err = str(exc)
            except Exception:
                res = None
                err = "アルバム解決に失敗しました"

            def _deliver():
                if not ctx.store.connected:
                    return False
                if err:
                    ctx.toast(err)
                elif res:
                    ctx.choose_playlist(res)
                else:
                    ctx.toast("アルバムが見つかりません")
                return False
            GLib.idle_add(_deliver)
        threading.Thread(target=_worker, daemon=True).start()

    aid = track.meta_get("spotify.album_id")
    if spotify_import.is_spotify_id(aid) and ctx.store and ctx.store.supports("tracks"):
        def _cb(value):
            if isinstance(value, Response) or not value:
                _fallback()
                return
            if not ctx.store.connected:
                return
            contains_song = not track.spotify_id or any(t.spotify_id == track.spotify_id for t in value)
            if contains_song and all(t.path and not t.unplayable and not t.live for t in value):
                ctx.choose_playlist(tuple(value))
            else:
                _fallback()
        try:
            ctx.catalog.tracks("spotify", f"spotify:album:{aid}", _cb)
            return
        except Exception:
            pass
    _fallback()
