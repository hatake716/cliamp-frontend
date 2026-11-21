"""YouTube Music の認証とライブラリ取得を GTK の外で実行する。"""

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from . import youtube_account


def run_async(work, callback):
    """結果かエラーを一度だけメインスレッドに戻す。"""
    def wrapper():
        try:
            value = work()
            error_text = None
        except Exception as e:
            value = None
            error_text = str(e)

        def idle_callback():
            callback(value, error_text)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(idle_callback)

    thread = threading.Thread(target=wrapper, daemon=True)
    thread.start()


def connect_youtube(parent, callback):
    """デスクトップ用の設定ファイルを選び、既定のブラウザーで認証する。"""
    dialog = Gtk.FileDialog()
    dialog.set_title("Google デスクトップ OAuth クライアントの JSON を選択")

    filter_json = Gtk.FileFilter()
    filter_json.set_name("JSON ファイル")
    filter_json.add_pattern("*.json")
    filters = Gio.ListStore.new(Gtk.FileFilter)
    filters.append(filter_json)
    dialog.set_filters(filters)

    def on_response(source, result):
        try:
            file = source.open_finish(result)
        except GLib.Error as e:
            if e.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED) or e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                callback("キャンセルしました")
            else:
                callback(str(e))
            return

        if file is None:
            callback("キャンセルしました")
            return

        path = file.get_path()
        if not path:
            callback("ファイルパスを取得できませんでした")
            return

        run_async(lambda: youtube_account.authorize(path), lambda _value, error: callback(error))

    dialog.open(parent, None, on_response)
