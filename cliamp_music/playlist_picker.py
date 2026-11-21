"""プレイリストに追加する窓。"""

from __future__ import annotations

from typing import Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from . import youtube_account as account
from .recording_review import RecordingReview, target_id
from .protocol import Track, valid_playlist_name
from .youtube_library import connect_youtube, run_async

_SOURCES = ("このPC", "Spotify", "YouTube Music")
_SOURCE_KEYS = ("local", "spotify", "youtube")


def _refresh(ctx, provider: str) -> None:
    if provider == "spotify":
        ctx.catalog.invalidate(provider)
    ctx.emit("account-playlists-changed", provider)


def _eligible(ctx, source: str, tracks: tuple[Track, ...]) -> tuple[bool, str]:
    """選んだ曲がこのサービスで追加できるか。"""
    if not tracks:
        return False, "追加する曲がありません"
    if source == "local":
        if not ctx.store.supports("playlist_add"):
            return False, "この cliamp はプレイリストに対応していません"
        return True, ""
    if source == "spotify":
        if not ctx.store.supports("spotify_save"):
            return False, "Spotify に保存できません"
        if not all(t.spotify_id or t.youtube_id for t in tracks):
            return False, "Spotify または YouTube Music の曲を選んでください"
        return True, ""
    if source == "youtube":
        if not all(t.spotify_id or t.youtube_id for t in tracks):
            return False, "Spotify または YouTube Music の曲を選んでください"
        return True, ""
    return False, "このサービスでは追加できません"


def present_picker(ctx, tracks: Iterable[Track]) -> Adw.AlertDialog | None:
    """曲順と曲を確定してから保存先を選ぶ。"""
    existing = getattr(ctx, "playlist_picker", None)
    if existing is not None:
        return existing
    tracks = tuple(t for t in tracks if t.path and not t.live and not t.unplayable)
    if not tracks:
        ctx.toast("追加できる曲がありません")
        return None
    dialog = Adw.AlertDialog(heading="プレイリストに追加",
                             body=tracks[0].display_title if len(tracks) == 1 else f"{len(tracks)} 曲")
    dialog.add_response("cancel", "キャンセル")
    dialog.add_response("add", "追加")
    dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("add")
    dialog.set_close_response("cancel")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    box.set_margin_start(6)
    box.set_margin_end(6)

    source = Gtk.DropDown.new_from_strings(list(_SOURCES))
    source.update_property([Gtk.AccessibleProperty.LABEL], ["サービス"])
    source.set_hexpand(True)
    box.append(source)

    dest = Gtk.DropDown()
    dest.update_property([Gtk.AccessibleProperty.LABEL], ["プレイリスト"])
    dest.set_hexpand(True)
    box.append(dest)

    entry = Gtk.Entry()
    entry.set_placeholder_text("新しいプレイリストの名前")
    entry.set_activates_default(True)
    entry.set_visible(False)
    box.append(entry)

    connect = Gtk.Button(label="接続し直す")
    connect.set_halign(Gtk.Align.START)
    connect.set_visible(False)
    box.append(connect)

    status = Gtk.Label(label="", wrap=True, xalign=0.0)
    status.set_visible(False)
    box.append(status)

    review_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.append(review_container)
    review = None
    dialog.set_extra_child(box)

    state = {
        "source": "local",
        "dests": [],
        "closed": False,
        "busy": False,
        "ready": False,
        "local_names": set(),
        "gen": 0,
    }

    def current_source() -> str:
        return _SOURCE_KEYS[source.get_selected()]

    def set_dests(rows: list[tuple[str, str]]) -> None:
        state["dests"] = rows
        names = [name for _id, name in rows] + ["新しいプレイリスト…"]
        dest.set_model(Gtk.StringList.new(names))
        dest.set_selected(0)

    def update_save(*_args) -> None:
        idx = dest.get_selected()
        new = idx == len(state["dests"])
        entry.set_visible(state["ready"] and new)
        valid = state["ready"] and not state["busy"] and 0 <= idx <= len(state["dests"])
        if valid and new:
            name = entry.get_text().strip()
            valid = bool(name) and len(name) <= 150
            if state["source"] == "local":
                valid = valid_playlist_name(name) and name not in state["local_names"]
        if review is not None:
            valid = valid and bool(review.confirmed)
        dialog.set_response_enabled("add", valid)

    def message(text: str) -> None:
        status.set_text(text)
        status.set_visible(bool(text))

    def load(*_args) -> None:
        nonlocal review
        if review is not None:
            review.cancel()
            review_container.remove(review)
            review = None
        state["gen"] += 1
        gen = state["gen"]
        key = state["source"] = current_source()
        state["ready"] = False
        state["dests"] = []
        dest.set_model(None)
        dest.set_visible(False)
        entry.set_text("")
        connect.set_visible(False)
        update_save()
        eligible, reason = _eligible(ctx, key, tracks)
        if not eligible:
            message(reason)
            return
        message("読み込み中…")

        def finish(rows, error=None) -> None:
            nonlocal review
            if state["closed"] or gen != state["gen"]:
                return
            if error or rows is None:
                message(error or "プレイリストを取得できませんでした")
                connect.set_visible(key == "youtube")
                return
            state["ready"] = True
            set_dests(rows)
            dest.set_visible(True)
            connect.set_visible(key == "youtube")
            message("")
            if key in ("spotify", "youtube"):
                missing = [t for t in tracks if not target_id(t, key)]
                if missing:
                    review = RecordingReview(ctx, tracks, key, update_save)
                    review_container.append(review)
                    review.start()
            update_save()

        if key == "local":
            def listed(names):
                if names is None:
                    finish(None)
                    return
                if gen != state["gen"] or state["closed"]:
                    return
                state["local_names"] = set(names)
                managed = set(ctx.imports.auto_sync_names())
                finish([(name, name) for name in names if name not in managed])
            ctx.refresh_local_playlists(then=listed)
        elif key == "spotify":
            def listed(response):
                if response.ok:
                    finish([(p["id"], p["name"]) for p in response.get("playlists", [])])
                else:
                    finish(None, response.message)
            ctx.client.request("spotify_destinations", listed)
        elif account.is_connected():
            connect.set_label("アカウントを接続し直す…")
            run_async(account.list_playlists, finish)
        else:
            connect.set_label("YouTube Music に接続…")
            connect.set_visible(True)
            message("Google アカウントを接続すると保存先を選べます。")

    def on_connect(_button) -> None:
        if state["busy"]:
            return
        state["busy"] = True
        source.set_sensitive(False)
        connect.set_sensitive(False)
        update_save()
        message("Google の認証を待っています…")

        def done(error):
            state["busy"] = False
            if not error:
                _refresh(ctx, "youtube-account")
            if state["closed"]:
                return
            source.set_sensitive(True)
            connect.set_sensitive(True)
            if error:
                message(error)
                update_save()
            else:
                load()
        connect_youtube(ctx.window, done)

    def on_response(_d, response: str) -> None:
        if state["closed"]:
            return
        save_tracks = review.confirmed if review is not None else tracks
        was_valid = dialog.get_response_enabled("add")
        state["closed"] = True
        ctx.playlist_picker = None
        if review is not None:
            review.cancel()
        if response != "add" or state["busy"] or not state["ready"] or not was_valid or not save_tracks:
            return
        source_key = state["source"]
        sel = dest.get_selected()
        if sel > len(state["dests"]):
            return
        new = sel == len(state["dests"])
        name = entry.get_text().strip()
        if new:
            dest_id = ""
        else:
            dest_id, dest_name = state["dests"][sel]
            name = dest_name

        if source_key == "local":
            ctx.add_to_playlist(name, save_tracks)
            return

        def report(provider, added, created, error=""):
            if error:
                note = "プレイリストを作成しました。" if created else ""
                ctx.toast(f"{note}「{name}」に追加を確認できた曲は {added} 曲です。{error}")
            else:
                ctx.toast(f"「{name}」に {added} 曲追加しました")
            if not error or added or created:
                _refresh(ctx, provider)

        if source_key == "spotify":
            def done(response) -> None:
                report("spotify", response.get("added", 0), new and bool(response.get("id")),
                       response.message if not response.ok else "")

            payload = [Track(path="spotify:track:" + t.spotify_id) for t in save_tracks]
            kwargs = {"name": name} if new else {"id": dest_id}
            ctx.client.request("spotify_save", done, tracks=payload, **kwargs)
            return

        def work() -> tuple[int, bool, str]:
            created = False
            try:
                target_id = dest_id
                if new:
                    target_id = account.create_playlist(name)
                    created = True
                return account.add_tracks(target_id, [t.youtube_id for t in save_tracks]), created, ""
            except account.YouTubeAccountError as exc:
                return exc.added, created, str(exc)

        def done(value, error) -> None:
            added, created, detail = value if value is not None else (0, False, error)
            report("youtube-account", added, created, detail)

        run_async(work, done, keep_running=True)

    source.connect("notify::selected", load)
    dest.connect("notify::selected", update_save)
    entry.connect("changed", update_save)
    connect.connect("clicked", on_connect)
    dialog.connect("response", on_response)

    ctx.playlist_picker = dialog
    load()
    parent = ctx.window if isinstance(ctx.window, Gtk.Window) else None
    if parent is not None:
        parent.present()  # ミニプレーヤーからも保存先を広いウインドウで選べる
    dialog.present(parent)
    return dialog
