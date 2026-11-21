"""検索で選んだプレイリストを、このPCと元のサービスに登録する。"""

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from . import youtube_account
from .youtube_library import run_async, connect_youtube
from .protocol import PlaylistInfo, public_playlist_key


def _offer_connection(ctx, website_url, on_connected, dismiss):
    dialog = Adw.AlertDialog(
        heading="YouTube Music に保存",
        body="このPCへの登録は完了しました。YouTube Music にも保存するには、Google アカウントを接続してください。",
    )
    dialog.add_response("close", "閉じる")
    dialog.add_response("web", "YouTube Music で開く")
    dialog.add_response("connect", "Google アカウントを接続…")
    dialog.set_default_response("connect")
    dialog.set_close_response("close")
    dialog.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)

    def on_response(_dialog, response):
        if response == "web":
            dismiss()
            ctx.open_uri(website_url)
        elif response == "connect":
            connect_youtube(ctx.window, on_connected)
        else:
            dismiss()

    dialog.connect("response", on_response)
    parent = ctx.window if isinstance(ctx.window, Gtk.Window) else None
    dialog.present(parent)


def save_playlist_to_account(ctx, info):
    key = public_playlist_key(info.provider, info.id) if isinstance(info, PlaylistInfo) else None
    if key is None or (key[0] == "spotify" and not re.fullmatch("[A-Za-z0-9]{22}", key[1])):
        ctx.toast("このプレイリストは保存できません。")
        return

    if key in ctx._pending_account_saves:
        ctx.toast("すでに保存処理中です。")
        return

    if not ctx.set_playlist_saved(info, True):
        return

    ctx._pending_account_saves.add(key)
    spotify = key[0] == "spotify"
    name = "Spotify" if spotify else "YouTube Music"
    website_url = f"https://open.spotify.com/playlist/{key[1]}" if spotify else f"https://music.youtube.com/playlist?list={key[1]}"

    def dismiss():
        ctx._pending_account_saves.discard(key)

    def finish(error=None):
        dismiss()
        if error:
            toast = ctx.toast(f"「{info.name}」の {name} への保存を確認できませんでした。{error}", action_uri=website_url)
            if toast is not None:
                toast.set_button_label(f"{name} で開く")
                toast.connect("button-clicked", lambda _t: ctx.open_uri(website_url))
        else:
            ctx.catalog.invalidate("spotify" if spotify else "ytmusic")
            ctx.emit("account-playlists-changed", "spotify" if spotify else "youtube-account")
            ctx.toast(f"「{info.name}」を {name} のライブラリに保存しました")

    def start(operation):
        app = ctx.app
        if app is not None:
            app.begin_account_save()

        def completed(error=None):
            try:
                finish(error)
            finally:
                if app is not None:
                    app.end_account_save()

        try:
            operation(completed)
        except (RuntimeError, OSError):
            completed("ネットワークエラーが発生しました。")

    if spotify:
        if not ctx.store.supports("save_playlist_to_library"):
            finish("ミュージックの接続を確認してください。")
            return
        start(lambda done: ctx.client.request(
            "save_playlist_to_library",
            lambda r: done(None if r.ok else r.message),
            provider="spotify",
            id=key[1],
        ))
    else:
        def google(error=None):
            if error:
                finish(error)
                return
            start(lambda done: run_async(
                lambda: youtube_account.save_playlist_to_library(key[1]),
                lambda _value, err: done(err),
                keep_running=True,
            ))

        if youtube_account.is_connected():
            google()
        else:
            _offer_connection(ctx, website_url, google, dismiss)
