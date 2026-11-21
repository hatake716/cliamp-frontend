"""YouTube account integration for cliamp-music (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Iterable
from typing import Optional

from .protocol import Track

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
APP_NAME = "cliamp-music"
CRED_FILENAME = "credentials.json"
ACCESS_TOKEN_SAFETY_MARGIN = 60.0
HTTP_TIMEOUT = 15.0
MAX_JSON_BYTES = 1_000_000
AUTHORIZE_TIMEOUT = 150.0
MAX_PAGES = 1000
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_CALLBACK_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>ミュージック</title></head><body>"
    "<h1>ミュージックに戻ってください</h1>"
    "<p>接続結果はアプリに表示されます。このタブは閉じてかまいません。</p></body></html>"
).encode("utf-8")

_AUTH_LOCK = threading.RLock()


class YouTubeAccountError(Exception):
    """YouTube account error with exact confirmed count."""

    def __init__(self, message: str, added: int = 0) -> None:
        super().__init__(message)
        self.added = added


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    timeout = 2

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        state = params.get("state", [""])[0]
        expected_state = getattr(self.server, "expected_state", "")

        # Constant-time comparison for state
        if not secrets.compare_digest(state.encode("utf-8"), expected_state.encode("utf-8")):
            self.send_response(400)
            self.end_headers()
            return

        self.server.result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_CALLBACK_HTML)))
        self.end_headers()
        try:
            self.wfile.write(_CALLBACK_HTML)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):  # noqa: D401
        return


def _xdg_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, APP_NAME, "youtube-account")


def _credentials_path() -> str:
    return os.path.join(_xdg_config_dir(), CRED_FILENAME)


def _is_regular_file(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_credentials() -> dict:
    path = _credentials_path()
    if not _is_regular_file(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_credentials(data: dict) -> None:
    path = _credentials_path()
    dir_path = os.path.dirname(path)
    tmp_path = None
    try:
        os.makedirs(dir_path, mode=0o700, exist_ok=True)
        os.chmod(dir_path, 0o700)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=dir_path,
            prefix=".cred-",
            delete=False,
            encoding="utf-8",
        ) as fh:
            tmp_path = fh.name
            json.dump(data, fh)
        os.replace(tmp_path, path)
        tmp_path = None
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise YouTubeAccountError("認証情報を保存できませんでした") from None
    except BaseException:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


class _TokenCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._path: Optional[str] = None

    def get(self, path: str) -> Optional[str]:
        with self._lock:
            if self._path != path:
                self._access_token = None
                self._expires_at = 0.0
                self._path = path
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            return None

    def set(self, path: str, token: str, expires_in: float) -> None:
        with self._lock:
            self._path = path
            self._access_token = token
            self._expires_at = time.monotonic() + max(0.0, expires_in - ACCESS_TOKEN_SAFETY_MARGIN)

    def invalidate(self) -> None:
        with self._lock:
            self._access_token = None
            self._expires_at = 0.0


_TOKEN_CACHE = _TokenCache()

def _send(request: urllib.request.Request) -> dict:
    opener = urllib.request.build_opener(_NoRedirect())
    is_write = (
        request.full_url.startswith(YOUTUBE_API_BASE + "/")
        and request.get_method() == "POST"
    )
    uncertainty_suffix = "。再試行する前にプレイリストを確認してください"
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096)
        except Exception:
            body = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        err = _http_error(exc.code, body)
        if is_write and (exc.code >= 500 or exc.code == 408):
            err = YouTubeAccountError(str(err) + uncertainty_suffix)
        raise err from None
    except urllib.error.URLError as exc:
        if is_write:
            raise YouTubeAccountError("ネットワーク接続に失敗しました" + uncertainty_suffix) from None
        raise YouTubeAccountError("ネットワーク接続に失敗しました") from None
    except (http.client.HTTPException, OSError):
        if is_write:
            raise YouTubeAccountError("ネットワーク接続に失敗しました" + uncertainty_suffix) from None
        raise YouTubeAccountError("ネットワーク接続に失敗しました") from None
    if len(raw) > MAX_JSON_BYTES:
        if is_write:
            raise YouTubeAccountError("応答が大きすぎます" + uncertainty_suffix)
        raise YouTubeAccountError("応答が大きすぎます")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        if is_write:
            raise YouTubeAccountError("不正な応答です" + uncertainty_suffix) from None
        raise YouTubeAccountError("不正な応答です") from None
    if not isinstance(data, dict):
        if is_write:
            raise YouTubeAccountError("不正な応答です" + uncertainty_suffix)
        raise YouTubeAccountError("不正な応答です")
    return data


def _http_error(code: int, body: bytes) -> YouTubeAccountError:
    reason = ""
    error_code = ""
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                reason = str(err.get("reason", ""))
                error_code = str(err.get("code", ""))
                errors = err.get("errors")
                if isinstance(errors, list) and errors:
                    first = errors[0]
                    if isinstance(first, dict):
                        reason = str(first.get("reason", ""))
            elif isinstance(err, str):
                error_code = err
    except Exception:
        pass
    if code == 400 and error_code == "invalid_grant":
        return YouTubeAccountError("YouTube Music に再接続してください")
    if code == 401:
        return YouTubeAccountError("認証が切れました。再認証してください")
    if code == 403:
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            return YouTubeAccountError("API利用枠を超過しました")
        if reason == "accessNotConfigured":
            return YouTubeAccountError("APIが有効になっていません")
        return YouTubeAccountError("権限がありません")
    if code == 404:
        return YouTubeAccountError("対象が見つかりません")
    if code == 429:
        return YouTubeAccountError("API利用枠を超過しました")
    if code >= 500:
        return YouTubeAccountError("サーバーエラーが発生しました")
    return YouTubeAccountError("リクエストに失敗しました")


def _api(resource: str, params: dict, body: Optional[dict] = None) -> dict:
    token = _get_access_token()
    url = f"{YOUTUBE_API_BASE}/{resource}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    return _send(req)


def _pages(resource: str, params: dict) -> list:
    items: list = []
    token = None
    seen = set()
    for _ in range(MAX_PAGES):
        p = dict(params)
        if token:
            p["pageToken"] = token
        data = _api(resource, p)
        items.extend(data.get("items", []))
        token = data.get("nextPageToken")
        if not token:
            break
        if token in seen:
            raise YouTubeAccountError("ページトークンが重複しています")
        seen.add(token)
    else:
        raise YouTubeAccountError("ページ数が多すぎます")
    return items


def _get_access_token() -> str:
    path = _credentials_path()
    with _AUTH_LOCK:
        cached = _TOKEN_CACHE.get(path)
        if cached:
            return cached
        creds = _load_credentials()
        refresh = creds.get("refresh_token")
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        if not refresh or not client_id or not client_secret:
            raise YouTubeAccountError("認証情報がありません。再認証してください")
        body = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST")
        try:
            data = _send(req)
        except YouTubeAccountError as exc:
            if "認証が切れました" in str(exc):
                _TOKEN_CACHE.invalidate()
            raise
        access = data.get("access_token")
        if not access:
            raise YouTubeAccountError("アクセストークンの取得に失敗しました")
        _TOKEN_CACHE.set(path, access, float(data.get("expires_in", 3600)))
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            creds["refresh_token"] = new_refresh
            _save_credentials(creds)
        return access


def is_connected() -> bool:
    return _is_regular_file(_credentials_path())


def authorize(client_json_path: str) -> None:
    if not _is_regular_file(client_json_path):
        raise YouTubeAccountError("クライアントJSONが見つかりません")
    try:
        with open(client_json_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        raise YouTubeAccountError("クライアントJSONが不正です") from None
    installed = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(installed, dict):
        raise YouTubeAccountError("クライアントJSONが不正です")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not client_id or not client_secret:
        raise YouTubeAccountError("クライアントJSONが不正です")
    redirect_uri, code, verifier = _run_oauth_flow(client_id)
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST")
    data = _send(req)
    if not data.get("refresh_token"):
        raise YouTubeAccountError("リフレッシュトークンの取得に失敗しました")
    if not data.get("access_token"):
        raise YouTubeAccountError("アクセストークンの取得に失敗しました")
    scope = data.get("scope", "")
    if scope and YOUTUBE_SCOPE not in scope.split():
        raise YouTubeAccountError("必要な権限が許可されていません")
    with _AUTH_LOCK:
        _save_credentials({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": data["refresh_token"],
        })
        _TOKEN_CACHE.invalidate()
        _TOKEN_CACHE.set(_credentials_path(), data["access_token"], float(data.get("expires_in", 3600)))


def _run_oauth_flow(client_id: str) -> tuple:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = secrets.token_urlsafe(16)
    try:
        srv = http.server.HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    except OSError:
        raise YouTubeAccountError("認証サーバーの起動に失敗しました") from None
    try:
        srv.result = None  # type: ignore[attr-defined]
        srv.expected_state = state  # type: ignore[attr-defined]
        srv.timeout = 0.5
        port = srv.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"
        try:
            opened = webbrowser.open(auth_url)
        except Exception:
            raise YouTubeAccountError("ブラウザの起動に失敗しました") from None
        if not opened:
            raise YouTubeAccountError("ブラウザの起動に失敗しました")
        deadline = time.monotonic() + AUTHORIZE_TIMEOUT
        while time.monotonic() < deadline:
            srv.handle_request()
            result = srv.result  # type: ignore[attr-defined]
            if result is None:
                continue
            if "error" in result:
                raise YouTubeAccountError("認証がキャンセルされました")
            code = result.get("code", [None])[0]
            if not code:
                raise YouTubeAccountError("認証に失敗しました")
            return redirect_uri, code, verifier
        raise YouTubeAccountError("認証がタイムアウトしました")
    finally:
        srv.server_close()

def list_playlists() -> list[tuple[str, str]]:
    items = _pages("playlists", {
        "part": "snippet",
        "mine": "true",
        "maxResults": "50",
    })
    out: list[tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = it.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        sn = it.get("snippet")
        if not isinstance(sn, dict):
            sn = {}
        title = sn.get("title")
        if not isinstance(title, str):
            title = ""
        out.append((pid, title))
    return out


def list_tracks(playlist_id: str) -> list[Track]:
    if not isinstance(playlist_id, str) or not playlist_id:
        raise YouTubeAccountError("プレイリストIDが不正です")

    items = _pages("playlistItems", {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": "50",
    })

    tracks: list[Track] = []
    for it in items:
        if not isinstance(it, dict):
            continue

        cd = it.get("contentDetails")
        if not isinstance(cd, dict):
            cd = {}
        sn = it.get("snippet")
        if not isinstance(sn, dict):
            sn = {}

        vid = None
        rid = sn.get("resourceId")
        if isinstance(rid, dict):
            cand = rid.get("videoId")
            if isinstance(cand, str) and VIDEO_ID.fullmatch(cand):
                vid = cand

        if not vid:
            cand = cd.get("videoId")
            if isinstance(cand, str) and VIDEO_ID.fullmatch(cand):
                vid = cand

        if not vid:
            continue

        title = sn.get("title")
        if not isinstance(title, str):
            title = ""

        title_cf = title.casefold()
        if title_cf in ("deleted video", "private video", "unavailable video"):
            continue

        artist = sn.get("videoOwnerChannelTitle")
        if not isinstance(artist, str):
            artist = ""

        url = f"https://music.youtube.com/watch?v={vid}"
        tracks.append(Track(url, title=title, artist=artist, stream=False))

    return tracks


def create_playlist(title: str) -> str:
    if not isinstance(title, str) or not title.strip():
        raise YouTubeAccountError("プレイリスト名が不正です")

    body = {
        "snippet": {"title": title},
        "status": {"privacyStatus": "private"},
    }
    data = _api("playlists", {"part": "snippet,status"}, body=body)
    if not isinstance(data, dict):
        raise YouTubeAccountError("プレイリストの作成に失敗しました")

    pid = data.get("id")
    if not isinstance(pid, str) or not pid:
        raise YouTubeAccountError("プレイリストの作成に失敗しました。作成結果を確認してください")

    return pid


def add_tracks(playlist_id: str, video_ids: Iterable[str]) -> int:
    if not isinstance(playlist_id, str) or not playlist_id:
        raise YouTubeAccountError("プレイリストIDが不正です")

    if isinstance(video_ids, str):
        raise YouTubeAccountError("video_idsは文字列ではなく文字列のコレクションを指定してください")

    try:
        vids = tuple(video_ids)
    except TypeError as exc:
        raise YouTubeAccountError("video_idsが不正です") from None

    for vid in vids:
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid):
            raise YouTubeAccountError("不正な動画IDがあります")

    added = 0
    for vid in vids:
        body = {
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {"kind": "youtube#video", "videoId": vid},
            }
        }
        try:
            _api("playlistItems", {"part": "snippet"}, body=body)
        except YouTubeAccountError as exc:
            raise YouTubeAccountError(str(exc), added=added) from None
        added += 1

    return added
