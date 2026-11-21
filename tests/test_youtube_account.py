"""YouTube Music アカウントの認可・ページング・書き込みを実アカウントなしで検証する。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request

from cliamp_music import youtube_account as yt


class YouTubeAccountTest(unittest.TestCase):
    def test_library_save(self):
        try:
            import requests
            from ytmusicapi import YTMusic
        except ImportError:
            self.skipTest("ytmusicapi is optional")

        reqs = []

        def dispatch(_session, req, **kwargs):
            if req.method == "GET":
                self.assertNotIn("Authorization", req.headers)
                return mock.Mock(status_code=200, text='ytcfg.set({"VISITOR_DATA":"visitor"});')
            self.assertEqual(req.method, "POST")
            self.assertEqual(urllib.parse.urlsplit(req.url).netloc, "music.youtube.com")
            self.assertEqual(urllib.parse.urlsplit(req.url).path, "/youtubei/v1/like/like")
            self.assertEqual(json.loads(req.body)["target"], {"playlistId": "PLtest_1"})
            self.assertEqual(req.headers["Authorization"], "Bearer fixture")
            reqs.append(req)
            return mock.Mock(status_code=200, text='{"responseContext":{}}')

        with (
            mock.patch.object(requests.Session, "send", autospec=True, side_effect=dispatch),
            mock.patch.object(yt, "_get_access_token", return_value="fixture") as token,
            mock.patch.object(YTMusic, "get_library_playlists", return_value=[{"playlistId": "PLtest_1"}]) as read,
        ):
            for bad in ("", "PLtest_1\n", "https://example.invalid/list", "x" * 129):
                with self.subTest(bad=bad):
                    with self.assertRaises(yt.YouTubeAccountError):
                        yt.save_playlist_to_library(bad)
            self.assertFalse(token.called)
            self.assertIsNone(yt.save_playlist_to_library("PLtest_1"))
            read.return_value = []
            with self.assertRaises(yt.YouTubeAccountError):
                yt.save_playlist_to_library("PLtest_1")
        self.assertEqual(len(reqs), 2)

    def test_google_desktop_oauth(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            client = Path(root) / "desktop-client.json"
            client.write_text(json.dumps({"installed": {
                "client_id": "test-id", "client_secret": "test-app",
                "auth_uri": "https://example.invalid", "token_uri": "https://example.invalid",
            }}))
            prompts, callbacks, workers = [], [], []

            def browser(url):
                parts = urllib.parse.urlsplit(url)
                self.assertEqual(parts.hostname, "accounts.google.com")
                query = urllib.parse.parse_qs(parts.query)
                prompts.append(query)
                redirect = query["redirect_uri"][0]
                self.assertEqual(urllib.parse.urlsplit(redirect).hostname, "127.0.0.1")

                def complete():
                    attempts = [
                        redirect.replace("/callback", "/favicon.ico"),
                        redirect + "?state=incorrect&code=ignored",
                        redirect + "?" + urllib.parse.urlencode({"state": query["state"][0], "code": "valid"}),
                    ]
                    for callback in attempts:
                        try:
                            with urllib.request.urlopen(callback, timeout=2) as response:
                                callbacks.append(response.status)
                        except urllib.error.HTTPError as exc:
                            callbacks.append(exc.code)
                            exc.close()

                worker = threading.Thread(target=complete, daemon=True)
                workers.append(worker)
                worker.start()
                return True

            def exchange(request):
                self.assertEqual(request.full_url, "https://oauth2.googleapis.com/token")
                form = urllib.parse.parse_qs(request.data.decode())
                query = prompts[0]
                self.assertEqual(form["redirect_uri"], query["redirect_uri"])
                self.assertEqual(form["code"], ["valid"])
                digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
                self.assertEqual(query["code_challenge"], [challenge])
                self.assertEqual(query["code_challenge_method"], ["S256"])
                self.assertEqual(query["access_type"], ["offline"])
                self.assertEqual(query["scope"], [yt.YOUTUBE_SCOPE])
                return {"access_token": "volatile", "refresh_token": "persistent", "scope": yt.YOUTUBE_SCOPE}

            self.assertFalse(yt.is_connected())
            with mock.patch("webbrowser.open", side_effect=browser), mock.patch.object(
                    yt, "_send", side_effect=exchange), mock.patch.object(yt, "AUTHORIZE_TIMEOUT", 4):
                yt.authorize(client)
            workers[0].join(3)
            self.assertEqual(callbacks, [404, 400, 200])
            saved = Path(root) / "cliamp-music/youtube-account/credentials.json"
            self.assertTrue(yt.is_connected())
            self.assertEqual(json.loads(saved.read_text()), {
                "client_id": "test-id", "client_secret": "test-app", "refresh_token": "persistent",
            })
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)

    def test_lists_owned_playlists_across_pages(self):
        pages = [
            {"items": [{"id": "one", "snippet": {"title": "ランニング"}}], "nextPageToken": "next"},
            {"items": [{"id": "two", "snippet": {"title": "ドライブ"}}]},
        ]
        with mock.patch.object(yt, "_api", side_effect=pages) as api:
            self.assertEqual(yt.list_playlists(), [("one", "ランニング"), ("two", "ドライブ")])
        self.assertEqual(api.call_args_list[0].args[0], "playlists")
        self.assertEqual(api.call_args_list[0].args[1]["mine"], "true")
        self.assertEqual(api.call_args_list[1].args[1]["pageToken"], "next")

    def test_ordered_tracks_and_unavailable_videos(self):
        first, second = "aBCdeFGh-01", "ijkLMno_x02"
        video = {"snippet": {"resourceId": {"videoId": first}, "title": "冬", "videoOwnerChannelTitle": "冬楽団"}}
        pages = [
            {"items": [video, {"snippet": {"title": "Deleted video", "resourceId": {"videoId": first}}},
                       {"snippet": {"title": "broken", "resourceId": {"videoId": "///////////"}}}],
             "nextPageToken": "p2"},
            {"items": [{"contentDetails": {"videoId": second}, "snippet": {"title": "夏"}}, video]},
        ]
        with mock.patch.object(yt, "_api", side_effect=pages):
            tracks = yt.list_tracks("mine")
        self.assertEqual([track.youtube_id for track in tracks], [first, second, first])
        self.assertEqual(tracks[0].path, "https://music.youtube.com/watch?v=" + first)
        self.assertEqual(tracks[0].artist, "冬楽団")
        self.assertFalse(tracks[0].stream)

    def test_creates_private_and_never_replays_partial_insert(self):
        with mock.patch.object(yt, "_api", return_value={"id": "saved"}) as api:
            self.assertEqual(yt.create_playlist("お気に入り"), "saved")
            self.assertEqual(api.call_args.kwargs["body"]["status"]["privacyStatus"], "private")
        ids = ["aBCdeFGh-01", "ijkLMno_x02", "xyZabcde-04"]
        with mock.patch.object(yt, "_api", side_effect=[{"id": "entry"}, yt.YouTubeAccountError("quota")]) as api:
            with self.assertRaises(yt.YouTubeAccountError) as raised:
                yt.add_tracks("saved", iter(ids))
            self.assertEqual(raised.exception.added, 1)
            self.assertEqual([call.kwargs["body"]["snippet"]["resourceId"]["videoId"]
                              for call in api.call_args_list], ids[:2])
        with mock.patch.object(yt, "_api") as api:
            with self.assertRaises(yt.YouTubeAccountError):
                yt.add_tracks("saved", [ids[0], "bad/video!!"])
            api.assert_not_called()

class YouTubeAccountTransportTest(unittest.TestCase):
    def test_invalid_grant_and_quota_are_actionable_and_redacted(self):
        revoked = yt._http_error(400, b'{"error":"invalid_grant","error_description":"secret data"}')
        self.assertIn("再接続", str(revoked))
        self.assertNotIn("secret data", str(revoked))
        quota = yt._http_error(403, b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}')
        self.assertIn("利用枠", str(quota))

    def test_timeout_on_write_reports_indeterminate_result_without_repeating(self):
        req = urllib.request.Request(yt.YOUTUBE_API_BASE + "/playlistItems", data=b"{}")
        with mock.patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaises(yt.YouTubeAccountError) as raised:
                yt._send(req)
        self.assertIn("再試行する前", str(raised.exception))
        opener.return_value.open.assert_called_once()

    def test_refreshes_once_and_saves_rotated_token_before_returning(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as cfg, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": cfg}):
            path = Path(cfg) / "cliamp-music/youtube-account/credentials.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "client_id": "test-client", "client_secret": "test-secret", "refresh_token": "first",
            }))
            def refresh(req):
                self.assertEqual(req.full_url, "https://oauth2.googleapis.com/token")
                self.assertEqual(urllib.parse.parse_qs(req.data.decode())["refresh_token"], ["first"])
                return {"access_token": "ephemeral", "refresh_token": "rotated", "expires_in": 3600}
            with mock.patch.object(yt, "_send", side_effect=refresh) as exchange, ThreadPoolExecutor(3) as pool:
                results = list(pool.map(lambda _: yt._get_access_token(), range(3)))
            self.assertEqual(results, ["ephemeral"] * 3)
            exchange.assert_called_once()
            self.assertEqual(json.loads(path.read_text())["refresh_token"], "rotated")
            self.assertNotIn("ephemeral", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
