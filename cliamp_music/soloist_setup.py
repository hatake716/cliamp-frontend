"""Spotify Soloist の API キー登録と Spotify Connect のペアリング。"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk


DASHBOARD = "https://developer.spotify.com/dashboard/soloist"
DEVICE = "ミュージック"
LAUNCHER = "cliamp-music-soloist"
INSTALLER = "cliamp-music-soloist-install"


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".local/share") / "cliamp-music"


def mark_paired(directory: Path) -> None:
    # バックエンドは Soloist の認証情報を解析せず、この成功済みの印を使う。
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as f:
        temp = Path(f.name)
    try:
        os.replace(temp, directory / "paired")
    finally:
        temp.unlink(missing_ok=True)


class SoloistSetup(Gtk.Window):
    """キーの登録とデバイスのペアリングを順に行う設定ウィンドウ。"""
    def __init__(self, parent=None, on_paired=None):
        super().__init__(title="Spotify の再生設定", default_width=460, resizable=False)
        if parent is not None:
            self.set_transient_for(parent)
            self.set_modal(True)
            self.set_destroy_with_parent(True)
        self.on_paired = on_paired
        self.directory = data_home() / "soloist-session"
        self.executable = data_home() / "soloist" / "soloist"
        self.process = None
        self.poll_source = 0
        self.update_required = False
        self.stage = ""
        self.connect("close-request", self._close_request)
        self.connect("unrealize", lambda *_args: self._cancel())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(20)
        self.set_child(box)
        self.key = Gtk.PasswordEntry(show_peek_icon=True)
        label = Gtk.Label(label="Spotify Soloist の API キー", xalign=0)
        label.set_mnemonic_widget(self.key)
        box.append(label)
        box.append(self.key)
        link = Gtk.LinkButton(uri=DASHBOARD, label="Spotify for Developers でキーを発行")
        link.set_halign(Gtk.Align.START)
        box.append(link)
        self.status = Gtk.Label(xalign=0, wrap=True, max_width_chars=48)
        box.append(self.status)
        self.submit = Gtk.Button(label="ペアリングを開始")
        self.submit.add_css_class("suggested-action")
        self.submit.set_halign(Gtk.Align.END)
        self.submit.connect("clicked", self._start)
        box.append(self.submit)
        self.set_default_widget(self.submit)
        self.key.connect("activate", self._start)
        if self._has_key() and (self.directory / "paired").is_file():
            message = "このPCは Spotify に接続済みです。アカウントを切り替える場合はペアリングし直してください。"
            self.submit.set_label("ペアリングし直す")
        elif self._has_key():
            message = "保存済みのキーを使います。変更するときだけ新しいキーを入力してください。"
        else:
            message = "自分の Spotify Soloist API キーを入力してください。"
        self.status.set_text(message)

    def _has_key(self):
        return (self.directory / "credentials" / "api_key").is_file()

    def _start(self, *_args):
        if self.process is not None:
            return
        if self.stage == "done":
            self.close()
            return
        if not self.key.get_text().strip() and not self._has_key():
            self.status.set_text("Spotify for Developers で発行した Soloist API キーを入力してください。")
            self.key.grab_focus()
            return
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
        except OSError:
            self._fail("Spotify の接続情報を保存するフォルダーを準備できませんでした。")
            return
        self.submit.set_sensitive(False)
        self.key.set_sensitive(False)
        if self.update_required or not os.access(self.executable, os.X_OK):
            self.status.set_text("Spotify 公式の Soloist をインストールしています…")
            installer = shutil.which(INSTALLER)
            if not installer:
                self._fail("Soloist のインストーラーが見つかりません。ミュージックを更新してください。")
                return
            self._run("install", [installer])
        else:
            self._set_key()

    def _run(self, stage, args, secret=None):
        self.stage = stage
        try:
            self.process = subprocess.Popen(
                args, stdin=subprocess.PIPE if secret is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, umask=0o077, start_new_session=True,
            )
            if secret is not None:
                with self.process.stdin as pipe:
                    pipe.write((secret + "\n").encode())
            self.poll_source = GLib.timeout_add(200, self._poll)
        except (OSError, ValueError):
            self._cancel()
            self._fail("Soloist を起動できませんでした。再度お試しください。")

    def _set_key(self):
        key = self.key.get_text().strip()
        if not key:
            self._pair()
            return
        self.key.set_text("")
        if len(key) > 2048 or any(c.isspace() for c in key):
            self._fail("Soloist API キーに余分な文字が含まれています。キーを確認してください。")
            return
        self.status.set_text("Soloist にキーを登録しています…")
        self._run("key", [self._program(), "setup-api-key", "--data-dir", str(self.directory)], key)

    def _program(self):
        # 単独の初回設定は、Nix のランチャーが入る前にも使える。
        return shutil.which(LAUNCHER) or str(self.executable)

    def _pair(self):
        self.status.set_text("同じネットワークにあるスマートフォンやPCで Spotify を開き、"
                             "「デバイスに接続」から「ミュージック」を選んでください。この画面で接続を待っています。")
        self._run("pair", [self._program(), "--pair", "--device-name", DEVICE, "--data-dir", str(self.directory)])

    def _poll(self):
        rc = self.process.poll()
        if rc is None:
            return GLib.SOURCE_CONTINUE
        self.process = None
        self.poll_source = 0
        if rc == 10:
            self.update_required = True
            self._fail("Soloist のバージョンが期限切れです。更新して、もう一度ペアリングしてください。")
            self.submit.set_label("更新してペアリング")
        elif rc != 0:
            errors = {"install": "公式 Soloist のインストールに失敗しました。ネットワークを確認してください。",
                      "key": "Soloist にキーを登録できませんでした。キーを確認してください。",
                      "pair": "接続できませんでした。キーを確認し、このPCで Spotify を再生中の場合は停止して再試行してください。"}
            self._fail(errors[self.stage])
        elif self.stage == "install":
            self.update_required = False
            self._set_key()
        elif self.stage == "key":
            # キーの変更後は、新しい接続が成功した時点で再び再生可能と伝える。
            try:
                (self.directory / "paired").unlink(missing_ok=True)
            except OSError:
                self._fail("接続状態を更新できませんでした。設定の保存先を確認してください。")
                return GLib.SOURCE_REMOVE
            self._pair()
        else:
            try:
                mark_paired(self.directory)
            except OSError:
                self._fail("ペアリングは完了しましたが、設定を保存できませんでした。")
                return GLib.SOURCE_REMOVE
            self.stage = "done"
            self.status.set_text("Spotify に接続しました。ミュージックで聴きたい曲を選んでください。")
            self.submit.set_label("完了")
            self.submit.set_sensitive(True)
            if self.on_paired:
                self.on_paired()
        return GLib.SOURCE_REMOVE

    def _fail(self, message):
        self.stage = ""
        self.status.set_text(message)
        self.key.set_sensitive(True)
        self.submit.set_sensitive(True)

    def _close_request(self, *_args):
        self._cancel()
        self.key.set_text("")
        return False

    def _cancel(self):
        if self.poll_source:
            GLib.source_remove(self.poll_source)
            self.poll_source = 0
        p, self.process = self.process, None
        if p is not None and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            def reap():
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    p.wait()

            threading.Thread(target=reap, daemon=True).start()


def main():
    app = Adw.Application(application_id="org.nixos.Music.SpotifySetup",
                          flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def activate(application):
        window = application.get_active_window() or SoloistSetup()
        window.set_application(application)
        window.present()

    app.connect("activate", activate)
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
