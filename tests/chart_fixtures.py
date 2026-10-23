"""TopCharts の試験用合成データ (日本トップ50プレイリスト形式)。

実際の Spotify の名前・アートは使わない。設定可能な件数・順位順・アーティスト・
有効な i.scdn.co URL の画像で、指定の JSON スキーマに一致する辞書と HTML を
返す。core テスト・UI スモーク・shoot で使う。
"""

from __future__ import annotations

import base64
import json

CHART_URL = "https://open.spotify.com/playlist/37i9dQZEVXbKXQ4mDTEBXq"
CHART_PAGE_URL = "https://open.spotify.com/playlist/37i9dQZEVXbKXQ4mDTEBXq"
_PLAYLIST_KEY = "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq"


def make_chart_json(count: int = 12, rank_order: list[int] | None = None) -> dict:
    """指定のスキーマに一致する decoded_dict (initialState) を返す。

    rank_order: トラックの識別子順序を決定する (source positions are actual ranks 1..N)。
    """
    if rank_order is None:
        rank_order = list(range(1, count + 1))
    items = []
    for i in range(count):
        # rank is always source position (1-based), rank_order determines track identity
        track_id = rank_order[i] if i < len(rank_order) else i + 1
        uri = f"spotify:track:{track_id:022d}"
        items.append({
            "itemV2": {
                "__typename": "TrackResponseWrapper",
                "data": {
                    "__typename": "Track",
                    "uri": uri,
                    "name": f"Synthetic Song {track_id}",
                    "artists": {
                        "items": [
                            {
                                "profile": {"name": f"Synthetic Artist {track_id}"},
                                "uri": f"spotify:artist:{track_id:022d}",
                            }
                        ]
                    },
                    "albumOfTrack": {
                        "name": f"Synthetic Album {track_id}",
                        "coverArt": {
                            "sources": [
                                {"width": 64, "height": 64, "url": f"https://i.scdn.co/image/synthetic{track_id:04d}64"},
                                {"width": 640, "height": 640, "url": f"https://i.scdn.co/image/synthetic{track_id:04d}640"},
                            ]
                        },
                    },
                    "duration": {"totalMilliseconds": 180000 + track_id * 1000},
                },
            }
        })
    return {
        "entities": {
            "items": {
                _PLAYLIST_KEY: {
                    "__typename": "Playlist",
                    "id": "37i9dQZEVXbKXQ4mDTEBXq",
                    "uri": "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq",
                    "name": "Top 50 - Japan",
                    "ownerV2": {
                        "data": {
                            "__typename": "User",
                            "uri": "spotify:user:spotify",
                            "username": "spotify",
                            "name": "Spotify",
                        }
                    },
                    "content": {
                        "__typename": "PlaylistItemsPage",
                        "pagingInfo": {"nextOffset": count},
                        "totalCount": 50,
                        "items": items,
                    },
                }
            }
        }
    }


def make_chart_html(count: int = 12, rank_order: list[int] | None = None) -> str:
    """initialState スクリプトを含む HTML を返す。"""
    data = make_chart_json(count=count, rank_order=rank_order)
    b64 = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    return (
        '<!DOCTYPE html><html><head>'
        f'<script id="initialState" type="text/plain">{b64}</script>'
        '</head><body></body></html>'
    )


def make_chart_fetcher(count: int = 12, rank_order: list[int] | None = None):
    """TopCharts の fetcher 引数に渡せる関数を返す。
    指定 URL のときのみ合成 HTML を返し、それ以外は ValueError。"""
    payload = make_chart_html(count=count, rank_order=rank_order)

    def fetcher(url, *, user_agent=None, timeout=None):
        if url != CHART_URL:
            raise ValueError(f"Unexpected chart URL: {url}")
        return payload

    return fetcher


def make_failure_fetcher():
    """常に失敗する fetcher を返す。"""
    def fetcher(url, *, user_agent=None, timeout=None):
        raise OSError("synthetic network failure")
    return fetcher


def make_empty_fetcher():
    """空のチャートを返す fetcher (has_data=False になる)。"""
    def fetcher(url, *, user_agent=None, timeout=None):
        data = {
            "entities": {
                "items": {
                    _PLAYLIST_KEY: {
                        "__typename": "Playlist",
                        "id": "37i9dQZEVXbKXQ4mDTEBXq",
                        "uri": "spotify:playlist:37i9dQZEVXbKXQ4mDTEBXq",
                        "name": "Top 50 - Japan",
                        "ownerV2": {
                            "data": {
                                "__typename": "User",
                                "uri": "spotify:user:spotify",
                                "username": "spotify",
                                "name": "Spotify",
                            }
                        },
                        "content": {
                            "__typename": "PlaylistItemsPage",
                            "pagingInfo": {"nextOffset": 0},
                            "totalCount": 0,
                            "items": [],
                        },
                    }
                }
            }
        }
        b64 = base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
        return f'<script id="initialState" type="text/plain">{b64}</script>'
    return fetcher
