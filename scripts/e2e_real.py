#!/usr/bin/env python3
"""本物のパッチ済み cliamp に繋いで、ミュージックを端から端まで動かすハーネス。

scripts/shoot.py は偽の cliamp (tests/fake_cliamp.py) を相手にするが、こちらは本物の
cliamp (TUI モード、私用の tmux の中) を相手にする。cliamp は呼び出し側が用意する:

- 一時的な HOME (ソケットは $HOME/.config/cliamp/cliamp.sock、108 バイト以内)、
  私用の D-Bus、音は ALSA の file→null などで鳴らさない (実時間で進むこと)。
- ローカルのプレイリスト「ドライブ」(03 に「夜に駆ける / YOASOBI」を含む 5 曲)、
  「Focus」(3 曲目は歌詞の見つからない曲)、「Plain」(長さの無い曲)、history.toml。
- 利用者の cliamp (~/.config/cliamp/cliamp.sock)・既定の tmux サーバー・
  ~/.local/state/cliamp-music などには決して触れない。このハーネスもソケットの場所を
  確かめ、利用者のものなら止まる。

使い方 (私用の Xvfb の上で。DISPLAY は :10 以上):

    DISPLAY=:81 nix develop path:. -c python3 scripts/e2e_real.py \\
        --socket /tmp/cm-e2e.X/.config/cliamp/cliamp.sock --out shots/real \\
        --tmux-socket /tmp/cm-e2e.X/run/tmux.sock --cliamp-bin /nix/store/…-cliamp-1.50.0/bin \\
        --stop-cmd stop.sh --start-cmd 'start.sh <patched>' --legacy-cmd 'stop.sh && start.sh <unpatched>' \\
        --patched-cmd 'stop.sh && start.sh <patched>' [--bus unix:path=…] [--network] [--xdotool PATH]

--network: YouTube の検索と再生 (yt-dlp)、Open-Voice と同じ tmux の Ctrl+F の検索、
LRCLIB の歌詞を本物で行う (音は鳴らないこと)。無ければその場面を飛ばす。

撮った PNG と、場面ごとの TUI の画面 (tmux capture-pane) を --out に置く。確かめたことの
失敗は標準エラーに「e2e: 失敗:」で出し、終了コード 1 にする。
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_ID = "org.nixos.Music.E2E"
FAILURE_PATTERNS = (
    "Theme parser error", "Gtk-CRITICAL", "Gtk-WARNING", "Adwaita-WARNING", "Adwaita-CRITICAL",
    "Adw-WARNING", "Adw-CRITICAL", "GLib-CRITICAL", "GLib-GObject-CRITICAL", "GLib-GObject-WARNING",
    "Gdk-CRITICAL", "Gsk-CRITICAL", "Pango-CRITICAL", "Pango-WARNING",
    "-CRITICAL **", "Failed to set text", "from markup", "Traceback (most recent call last)",
    "を作れません", "操作に失敗", "CSS の誤り", "e2e: 失敗",
)


def _user_socket() -> str:
    home = pwd.getpwuid(os.getuid()).pw_dir
    return os.path.realpath(os.path.join(home, ".config", "cliamp", "cliamp.sock"))


def _private_display() -> bool:
    display = os.environ.get("DISPLAY", "")
    number = display[1:].split(".")[0] if display.startswith(":") else ""
    return number.isdigit() and int(number) >= 10


# --------------------------------------------------------------------------
# 親


def run_parent(args: argparse.Namespace) -> int:
    if not _private_display():
        print("e2e: 私用の Xvfb (DISPLAY=:10 以上) の下で動かしてください", file=sys.stderr)
        return 2
    if os.path.realpath(args.socket) == _user_socket():
        print("e2e: 利用者の cliamp のソケットには繋ぎません", file=sys.stderr)
        return 2
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cm-e2e-app-"))
    try:
        for sub in ("home", "config", "state", "cache", "data", "run"):
            (work / sub).mkdir()
        os.chmod(work / "run", 0o700)
        env = dict(os.environ)
        for name in ("WAYLAND_DISPLAY", "CLIAMP_MUSIC_SOCKET"):
            env.pop(name, None)
        env.update({
            "HOME": str(work / "home"),
            "XDG_CONFIG_HOME": str(work / "config"),
            "XDG_STATE_HOME": str(work / "state"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_DATA_HOME": str(work / "data"),
            "XDG_RUNTIME_DIR": str(work / "run"),
            "GDK_BACKEND": "x11",
            "GSK_RENDERER": "cairo",
            "GDK_DEBUG": "no-portals",
            "GDK_DISABLE": "gl",
            "ADW_DISABLE_PORTAL": "1",
            "GTK_A11Y": "none",
            "NO_AT_BRIDGE": "1",
            "GIO_USE_VFS": "local",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONWARNINGS": "default,ignore:GLib.unix_signal_add_full is deprecated",
            "CLIAMP_MUSIC_APP_ID": APP_ID,
            "CLIAMP_MUSIC_NON_UNIQUE": "1",
            "CLIAMP_MUSIC_START_COMMAND": "false",
        })
        cmd = [sys.executable, str(Path(__file__).resolve()), "--child", *sys.argv[1:]]
        if args.bus:
            env["DBUS_SESSION_BUS_ADDRESS"] = args.bus
        else:
            env.pop("DBUS_SESSION_BUS_ADDRESS", None)
            cmd = ["dbus-run-session", "--"] + cmd
        log_path = out / "app-stderr.log"
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=log, text=True,
                                    start_new_session=True)
            try:
                stdout, _ = proc.communicate(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout, _ = proc.communicate()
                print(f"e2e: 失敗: {args.timeout} 秒で終わりませんでした", file=sys.stderr)
                return 1
        sys.stdout.write(stdout or "")
        stderr = log_path.read_text(encoding="utf-8", errors="replace")
        sys.stderr.write(stderr)
        bad = [line for line in stderr.splitlines() if any(p in line for p in FAILURE_PATTERNS)]
        if bad:
            print(f"e2e: 標準エラーに警告・失敗が {len(bad)} 行あります", file=sys.stderr)
            return 1
        return proc.returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 子


class Until:
    def __init__(self, predicate, timeout: float = 10.0, label: str = ""):
        self.predicate = predicate
        self.timeout = timeout
        self.label = label


def child_main(args: argparse.Namespace) -> int:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Graphene", "1.0")
    gi.require_version("GdkX11", "4.0")
    import warnings

    from gi.repository import GdkX11, Gio, GLib, Graphene, Gtk

    from cliamp_music import app as app_module
    from cliamp_music.protocol import encode_request, fraction_to_volume

    out = Path(args.out)
    sock = args.socket
    cliamp_home = Path(args.cliamp_home) if args.cliamp_home else Path(sock).parents[2]
    problems: list[str] = []
    notes: list[str] = []
    shots: list[str] = []
    result: dict = {}

    # --- 小道具 ----------------------------------------------------------------

    def app():
        return Gio.Application.get_default()

    def win():
        return app().window

    def ctx():
        return app().ctx

    def store():
        return app().ctx.store

    def fail(message: str) -> None:
        problems.append(message)
        print(f"e2e: 失敗: {message}", file=sys.stderr, flush=True)

    def note(message: str) -> None:
        notes.append(message)
        print(f"e2e: 観察: {message}", flush=True)

    def check(condition: bool, message: str) -> bool:
        if not condition:
            fail(message)
        return bool(condition)

    def real(cmd: str, **fields) -> dict:
        """アプリを通さず本物の cliamp に直接聞く (確かめ用)。"""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(130)
        try:
            s.connect(sock)
            s.sendall(encode_request(cmd, **fields))
            buf = bytearray()
            while not buf.endswith(b"\n"):
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"e2e: {exc}"}
        finally:
            s.close()

    def real_status() -> dict:
        return real("status")

    def tui(name: str) -> str:
        if not args.tmux_socket:
            return ""
        proc = subprocess.run(["tmux", "-S", args.tmux_socket, "capture-pane", "-p", "-t", "cliamp"],
                              capture_output=True, text=True)
        text = proc.stdout
        (out / f"tui-{name}.txt").write_text(text, encoding="utf-8")
        return text

    def capture(widget: Gtk.Widget, name: str) -> None:
        width, height = widget.get_width(), widget.get_height()
        if width <= 0 or height <= 0:
            fail(f"{name}: 大きさが 0 です")
            return
        paintable = Gtk.WidgetPaintable.new(widget)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            fail(f"{name}: 描く物がありません")
            return
        renderer = widget.get_native().get_renderer()
        texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
        path = out / f"{name}.png"
        texture.save_to_png(str(path))
        shots.append(name)
        print(f"e2e: {path} ({texture.get_width()}x{texture.get_height()})", flush=True)

    def page():
        return win().current_page()

    def page_id() -> str:
        current = page()
        return getattr(current, "page_id", "") if current is not None else ""

    def descendants(widget):
        child = widget.get_first_child() if widget is not None else None
        while child is not None:
            yield child
            yield from descendants(child)
            child = child.get_next_sibling()

    def texts(widget) -> str:
        return "\n".join(w.get_text() for w in descendants(widget)
                         if isinstance(w, Gtk.Label) and w.get_mapped())

    def rows_of(widget):
        from cliamp_music.widgets import TrackRow

        return [w for w in descendants(widget) if isinstance(w, TrackRow)]

    def run_cmd(command: str):
        """コマンドを裏で走らせる。終わったら .output に標準出力が入る。"""
        proc = subprocess.Popen(["bash", "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        proc.output = ""

        def finished() -> bool:
            if proc.poll() is None:
                return False
            if proc.stdout is not None:
                proc.output = proc.stdout.read().strip()
                proc.stdout.close()
                proc.stdout = None
            return True

        proc.finished = finished
        return proc

    def index_of(path_tail: str) -> int:
        for i, track in enumerate(store().playlist.tracks):
            if track.path.endswith(path_tail):
                return i
        return -1

    xid_cache: dict = {}

    def xid() -> int:
        if "xid" not in xid_cache:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                xid_cache["xid"] = GdkX11.X11Surface.get_xid(win().get_surface())
        return xid_cache["xid"]

    def root_point(widget: Gtk.Widget, fx: float, fy: float) -> tuple[int, int] | None:
        """widget の中の割合 (fx, fy) の点の画面上の座標 (xdotool 用)。"""
        window = win()
        ok, bounds = widget.compute_bounds(window)
        if not ok:
            return None
        sx, sy = window.get_surface_transform()
        geo = subprocess.run([args.xdotool, "getwindowgeometry", "--shell", str(xid())],
                             capture_output=True, text=True).stdout
        pos = dict(line.split("=", 1) for line in geo.split() if "=" in line)
        x = int(pos.get("X", 0)) + sx + bounds.get_x() + fx * bounds.get_width()
        y = int(pos.get("Y", 0)) + sy + bounds.get_y() + fy * bounds.get_height()
        return int(round(x)), int(round(y))

    def ensure_drive():
        """いまのリストを「ドライブ」にする (無ければ読み込む)。"""
        if index_of("/03.flac") < 0:
            ctx().load_provider("local", "ドライブ", 0, "ドライブ")
            yield Until(lambda: index_of("/03.flac") >= 0, 10, "ドライブを読み込めない")

    # --- 場面 ------------------------------------------------------------------

    state: dict = {}

    def scene_start():
        yield Until(lambda: app() is not None and win() is not None and win().get_mapped(), 15, "窓が出ない")
        yield Until(lambda: store().connected, 15, "本物の cliamp に繋がらない")
        check(store().api == 1, f"api が 1 ではありません ({store().api})")
        caps = ctx().client.capabilities
        check("replace" in (caps.get("commands") or []), f"capabilities の commands が変です: {caps}")
        check(len(caps.get("eq_presets") or []) == 16, f"eq_presets が 16 個ではありません: {caps}")
        state["tui_start"] = tui("start")
        # GUI は MPRIS の名前を取らない (共有の私用バスで数える)
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "ListNames", None, GLib.VariantType.new("(as)"),
                                  Gio.DBusCallFlags.NONE, 3000, None)
            names = [n for n in reply.unpack()[0] if n.startswith("org.mpris.")]
            note(f"バスの MPRIS の名前: {names}")
            check(all(n == "org.mpris.MediaPlayer2.cliamp" for n in names),
                  f"cliamp 以外の MPRIS の名前があります: {names}")
        except GLib.Error as exc:
            fail(f"D-Bus の名前を数えられません: {exc}")
        yield 1.0
        capture(win(), "start")

    def scene_load_local():
        """プレイリストの詳細から「ドライブ」を再生する (load_provider)。"""
        window = win()
        window.navigate("playlist", provider="local", id="ドライブ", name="ドライブ")
        yield Until(lambda: page_id() == "playlist", 5, "プレイリストが開かない")
        detail = page()
        yield Until(lambda: len(rows_of(detail)) >= 5, 10, "ドライブの行が出ない")
        yield 0.8
        capture(window, "playlist-drive")
        rows = rows_of(detail)
        durations = [r.track.duration for r in rows]
        check(all(d > 0 for d in durations[:5]), f"ドライブの曲の長さが 0 です: {durations}")
        target = next((r for r in rows if getattr(r, "index", None) == 2), None)
        check(target is not None, "ドライブの 3 行目がありません")
        if target is not None:
            detail._on_row(target)
        yield Until(lambda: real_status().get("track", {}).get("path", "").endswith("/03.flac"), 10,
                    "行から再生しても本物が 03 を再生しない")
        st = real_status()
        check(st.get("source") == {"provider": "local", "id": "ドライブ", "name": "ドライブ"},
              f"本物の source が違います: {st.get('source')}")
        yield Until(lambda: store().status.track is not None and store().status.track.path.endswith("/03.flac"),
                    5, "アプリの status が 03 にならない")
        yield 1.5
        capture(window, "playlist-drive-playing")
        cur = [r for r in rows_of(detail) if r.has_css_class("current") or r.has_css_class("playing")]
        note(f"ドライブの詳細で現在の行の印: {[r.index for r in cur]} (本物の index={st.get('index', 0)})")
        window.navigate("playlist", provider="local", id="Plain", name="Plain")
        yield Until(lambda: page_id() == "playlist" and len(rows_of(page())) >= 2, 10, "Plain の行が出ない")
        yield 0.8
        capture(window, "playlist-plain")
        note("Plain の行の文字: " + " | ".join(texts(r).replace("\n", " / ") for r in rows_of(page())))

    def scene_home():
        window = win()
        window.navigate("home")
        yield Until(lambda: page_id() == "home", 5, "ホームが開かない")
        home = page()
        yield Until(lambda: home.recent.get_visible() and home.playlists.get_visible(), 20,
                    "ホームの最近再生・プレイリストの棚が出ない")
        yield 4.0
        capture(window, "home")
        note(f"ホームの棚の表示: picks={home.picks.get_visible()} recent={home.recent.get_visible()} "
             f"playlists={home.playlists.get_visible()} stations={home.stations.get_visible()}")

    def scene_playlists():
        window = win()
        window.navigate("playlists")
        yield Until(lambda: page_id() == "playlists", 5, "すべてのプレイリストが開かない")
        yield 3.0
        body = texts(page())
        for name in ("ドライブ", "Focus", "Plain"):
            check(name in body, f"すべてのプレイリストに {name} がありません")
        check("Recently Played" not in body, "Recently Played が普通のプレイリストとして出ています")
        note("すべてのプレイリストの文字: " + body.replace("\n", " / ")[:600])
        capture(window, "playlists")
        side = texts(window.sidebar)
        note("サイドバーの文字: " + side.replace("\n", " / ")[:600])

    def scene_recent():
        window = win()
        window.navigate("recent")
        yield Until(lambda: page_id() == "recent", 5, "最近再生した項目が開かない")
        yield 2.5
        real_hist = real("history", limit=50).get("tracks") or []
        rows = rows_of(page())
        note(f"最近再生: 本物の履歴 {len(real_hist)} 件、行 {len(rows)} 行")
        check(len(rows) >= min(len(real_hist), 1), "最近再生に行がありません")
        capture(window, "recent")

    def scene_search_library():
        window = win()
        app().activate_action("search", None)
        yield Until(lambda: page_id() == "search", 5, "検索が開かない")
        search = page()
        scopes = [key for key, _ in search._available_scopes()]
        note(f"検索の範囲: {scopes}")
        state["scopes"] = scopes
        search.set_scope("library")
        search.set_query("Silent")
        yield Until(lambda: search.results.state == "content", 10, "ライブラリの検索の結果が出ない")
        yield 1.0
        titles = [t.display_title for t in search.results_tracks]
        note(f"ライブラリの検索「Silent」: {titles}")
        check(any("Silent Track" in t for t in titles), "ライブラリの検索で Silent Track が見つからない")
        capture(window, "search-library")
        window.set_focus(None)

    def scene_search_spotify():
        window = win()
        search = page()
        if "spotify" not in state.get("scopes", []):
            note("Spotify の範囲が無いので飛ばします")
            return
        search.set_scope("spotify")
        search.set_query("test")
        yield Until(lambda: search.results.state != "loading", 20, "Spotify の検索が終わらない")
        yield 0.8
        body = texts(search)
        note("Spotify の検索: " + body.replace("\n", " / ")[:300])
        check("サインインが必要です" in body, "資格情報の無い Spotify の検索でサインインの案内が出ない")
        capture(window, "search-spotify")

    def scene_search_youtube():
        if not args.network:
            note("--network が無いので YouTube の検索と再生を飛ばします")
            return
        window = win()
        search = page()
        search.set_scope("youtube")
        search.set_query("lofi hip hop")
        yield Until(lambda: search.results.state in ("content", "empty"), 60, "YouTube の検索の結果が出ない")
        yield 2.0
        tracks = search.results_tracks
        note(f"YouTube の検索: {len(tracks)} 件、先頭 {tracks[0].display_title if tracks else None!r} "
             f"duration={tracks[0].duration if tracks else None} stream={tracks[0].stream if tracks else None}")
        capture(window, "search-youtube")
        if not tracks:
            fail("YouTube の検索で何も出ない")
            return
        seen = {"buffering": False, "stopped_buffering": False, "bar_loading": False}
        start = time.monotonic()
        search._play_index(0)

        def watch():
            st = store().status
            if st.buffering:
                seen["buffering"] = True
                if st.state == "stopped":
                    seen["stopped_buffering"] = True
                if win().player_bar.subtitle_label.get_text() == "読み込み中…":
                    seen["bar_loading"] = True
                    if "loading_shot" not in state:
                        state["loading_shot"] = True
                        capture(win(), "search-youtube-loading")
            return st.state == "playing" and not st.buffering and st.track is not None \
                and st.track.path == tracks[0].path

        yield Until(watch, 90, "YouTube の曲が再生にならない")
        note(f"YouTube の曲の読み込み: {time.monotonic() - start:.1f} 秒、観察 {seen}")
        st = real_status()
        note(f"本物の status (YouTube 再生中): state={st.get('state')} duration={st.get('duration')} "
             f"track.duration={st.get('track', {}).get('duration')} source={st.get('source')}")
        yield 3.0
        capture(window, "search-youtube-playing")
        # seek_to (yt-dlp の曲)。TUI を止めないこと
        dur = store().status.duration
        if dur > 60:
            t0 = time.monotonic()
            store().seek_to(dur / 2)
            yield 0.3
            polls = []
            for _ in range(10):
                t = time.monotonic()
                s = real_status()
                polls.append((round(time.monotonic() - t, 3), s.get("position"), s.get("buffering")))
                yield 0.3
            note(f"YouTube の曲で seek_to {dur / 2:.0f}: status の応答時間と位置 {polls} "
                 f"({time.monotonic() - t0:.1f} 秒)")
        store().stop()
        yield Until(lambda: real_status().get("state") == "stopped", 10, "YouTube の曲が止まらない")

    def scene_radio():
        window = win()
        window.navigate("radio")
        yield Until(lambda: page_id() == "radio", 5, "ラジオが開かない")
        yield 6.0
        body = texts(page())
        note("ラジオのページ: " + body.replace("\n", " / ")[:500])
        capture(window, "radio")

    def scene_nowplaying():
        window = win()
        ctx().load_provider("local", "ドライブ", 0, "ドライブ")
        yield Until(lambda: store().status.source.id == "ドライブ" and store().status.state == "playing", 10,
                    "ドライブを読み込めない")
        window.navigate("nowplaying")
        yield Until(lambda: page_id() == "nowplaying" and len(rows_of(page())) >= 5, 10, "再生中のリストの行が出ない")
        yield 1.0
        rows = rows_of(page())
        target = next(r for r in rows if r.index == 3)
        page()._on_row(target)
        yield Until(lambda: real_status().get("index") == 3, 6, "再生中のリストの行で本物の index が 3 にならない")
        yield Until(lambda: store().status.index == 3, 3, "アプリの index が 3 にならない")
        yield 1.5
        capture(window, "nowplaying")

    def scene_queue_panel():
        window = win()
        # 最後の曲が次に来ないよう、先頭の曲に戻してから
        store().play_index(0)
        yield Until(lambda: real_status().get("index", 0) == 0, 5, "先頭に戻らない")
        lib = list(store().playlist.tracks)
        # 1 つずつ応答を待って送る (client は状態を変える要求を 1 本の列で順に送るので、
        # 続けて送っても順は保たれるが、応答ごとに写しを確かめるため)
        acks: list = []
        store().queue_edit("add", 3, callback=acks.append)
        yield Until(lambda: len(acks) == 1, 5, "queue_edit add の応答が無い")
        store().queue_edit("add", 1, callback=acks.append)
        yield Until(lambda: len(acks) == 2, 5, "queue_edit add の応答が無い")
        yield Until(lambda: store().playlist.queue == [3, 1], 5, "queue_edit add が写しに映らない")
        window.show_panel("queue")
        panel = window.queue_panel
        yield Until(lambda: panel.row_count >= 2, 5, "次に再生のパネルに行が出ない")
        yield 1.0
        pl = real("playlist")
        note(f"本物の queue={pl.get('queue')} up_next={pl.get('up_next')}; パネルの行 {panel._row_keys}")
        check([k for k, _i, _t in panel._row_keys][:2] == ["queue", "queue"], "パネルの先頭 2 行が待ち行列ではない")
        capture(window, "queue-panel")
        store().queue_edit("move", 0, 1)
        yield Until(lambda: real("playlist").get("queue") == [1, 3], 5, "queue_edit move が本物に映らない")
        yield Until(lambda: [i for k, i, _t in panel._row_keys if k == "queue"] == [1, 3], 5,
                    "move がパネルに映らない")
        store().queue_edit("remove", 3)
        yield Until(lambda: real("playlist").get("queue") == [1], 5, "queue_edit remove が本物に映らない")
        yield Until(lambda: [i for k, i, _t in panel._row_keys if k == "queue"] == [1], 5,
                    "remove がパネルに映らない")
        # 「次に再生」(メニュー) は曲を末尾に足し、待ち行列ではなく再生順でいまの曲のすぐ後ろ
        # (待ち行列より後) に並べる (パッチの PlayNext。1 曲リピートのときだけ待ち行列へ)
        total = real_status().get("total", 0)
        store().enqueue([lib[4]], "next")
        yield Until(lambda: real_status().get("total", 0) == total + 1, 5, "enqueue next が本物に映らない")
        yield Until(lambda: (real("playlist").get("up_next") or [None])[0] == total, 5,
                    "enqueue next の曲が続きの先頭に来ない")
        yield Until(lambda: (store().playlist.up_next or [None])[0] == total, 5, "enqueue next が写しに映らない")
        note(f"enqueue next 後: 本物 total={real_status().get('total')} queue={real('playlist').get('queue')} "
             f"up_next={real('playlist').get('up_next')}")
        yield 0.8
        capture(window, "queue-panel-2")
        panel.clear_button.emit("clicked")
        yield Until(lambda: not real("playlist").get("queue"), 5, "消去で本物の待ち行列が空にならない")
        yield Until(lambda: not store().playlist.queue and not panel.clear_button.get_visible(), 5,
                    "消去がパネルに映らない")
        # 足した複製を消して 5 曲に戻す
        total = real_status().get("total", 0)
        for i in range(total - 1, 4, -1):
            store().remove(i)
        yield Until(lambda: real_status().get("total") == 5, 5, "足した曲を消せない")
        window.show_panel("")

    def scene_lyrics():
        window = win()
        yield from ensure_drive()
        i = index_of("/03.flac")
        check(i >= 0, "03.flac がリストにありません")
        store().play_index(i)
        yield Until(lambda: store().status.track is not None and store().status.track.path.endswith("/03.flac"), 6,
                    "03 にならない")
        window.show_panel("lyrics")
        panel = window.lyrics_panel
        yield Until(lambda: bool(panel._lines), 30, "夜に駆ける の歌詞が出ない (LRCLIB)")
        store().seek_to(62.0)
        yield Until(lambda: panel._current >= 0, 8, "同期した歌詞の今の行が決まらない")
        yield 2.0
        note(f"歌詞: {len(panel._lines)} 行、同期={panel._lyrics.synced if panel._lyrics else None}、"
             f"今の行={panel._current}")
        capture(window, "lyrics")
        ctx().load_provider("local", "Focus", 2, "Focus")
        yield Until(lambda: store().status.track is not None and store().status.track.path.endswith("/08.flac"),
                    8, "Focus の 3 曲目にならない")
        yield Until(lambda: not panel._lines, 30, "見つからない歌詞で前の歌詞が残る")
        yield 3.0
        note("歌詞の無い曲のパネル: " + texts(panel).replace("\n", " / ")[:200])
        capture(window, "lyrics-notfound")
        window.show_panel("")

    def scene_eq():
        a = app()
        a.activate_action("equalizer", None)
        yield Until(lambda: a.equalizer is not None and a.equalizer.get_mapped(), 5, "イコライザが開かない")
        eq = a.equalizer
        names = eq.preset_names() if callable(getattr(eq, "preset_names", None)) else eq._preset_names
        note(f"イコライザのプリセット: {names}")
        start = real_status()
        note(f"開いた時の本物の eq_preset={start.get('eq_preset')!r} eq={start.get('eq')}")
        yield 0.5
        rock = names.index("Rock") if "Rock" in names else -1
        if check(rock >= 0, "プリセットに Rock がありません"):
            eq.preset_dropdown.set_selected(rock)
            yield Until(lambda: real_status().get("eq_preset") == "Rock", 5, "Rock が本物に映らない")
            yield Until(lambda: eq.faders[0].get_value() == 5, 3, "Rock の値がつまみに映らない")
        eq.faders[2].set_value(-6.0)
        yield Until(lambda: (real_status().get("eq") or [0] * 10)[2] == -6, 5, "つまみの値が本物に映らない")
        st = real_status()
        check(st.get("eq_preset") == "Custom", f"帯域を動かした後の eq_preset が Custom ではない: {st.get('eq_preset')}")
        yield 1.0
        shown = eq.preset_dropdown.get_selected_item()
        note(f"帯域を動かした後の表示: {shown.get_string() if shown else None!r}、本物 {st.get('eq_preset')!r}")
        eq.speed_scale.set_value(1.25)
        yield Until(lambda: real_status().get("speed") == 1.25, 5, "速度が本物に映らない")
        yield 1.0
        capture(eq, "equalizer")
        eq.flat_button.emit("clicked")
        yield Until(lambda: real_status().get("eq") == [0] * 10, 5, "フラットが本物に映らない")
        eq.speed_scale.set_value(1.0)
        yield Until(lambda: real_status().get("speed") == 1, 5, "速度が戻らない")
        eq.close()
        yield 0.5

    def scene_devices():
        bar = win().player_bar
        bar.output.set_active(True)
        yield Until(lambda: bar._device_items.get_n_items() >= 2, 5, "出力先の一覧が出ない")
        yield 0.8
        labels = [bar._device_items.get_item_attribute_value(i, "label", GLib.VariantType.new("s")).get_string()
                  for i in range(bar._device_items.get_n_items())]
        note(f"出力先の一覧 (説明があれば説明、無ければ sink 名から作った見出し): {labels}")
        popover = bar.output.get_popover()
        if popover is not None and popover.get_mapped():
            capture(popover, "devices")
        bar.output.set_active(False)
        bar.actions.activate_action("device", GLib.Variant.new_string("e2e_sink_b"))
        yield 1.5
        log = (cliamp_home / "pactl.log").read_text() if (cliamp_home / "pactl.log").exists() else ""
        check("set-default-sink e2e_sink_b" in log or "move-sink-input" in log,
              f"出力先の切り替えが cliamp に届かない (pactl の記録: {log[-200:]!r})")
        listing = real("device", name="list")
        note(f"切り替え後の device list: {listing.get('device')!r}")
        bar.actions.activate_action("device", GLib.Variant.new_string("e2e_sink_a"))
        yield 1.0

    def scene_volume():
        bar = win().player_bar
        bar.volume_scale.set_value(0.25)
        want = round(fraction_to_volume(0.25), 1)  # フルスクリーンと同じく dB に比例するつまみ
        yield Until(lambda: abs(real_status().get("volume", 0) - want) < 0.05, 5,
                    f"音量 0.25 ({want} dB) が本物に映らない")
        note(f"音量 0.25 → 本物 {real_status().get('volume')} dB")
        store().set_volume_db(-20.0)
        yield Until(lambda: real_status().get("volume") == -20, 5, "音量が -20 に戻らない")

    def scene_seek():
        window = win()
        yield from ensure_drive()
        i = index_of("/03.flac")
        store().play_index(i)
        yield Until(lambda: store().status.track is not None and store().status.track.path.endswith("/03.flac")
                    and store().status.state == "playing", 6, "03 にならない")
        yield 1.0
        progress = window.player_bar.progress
        check(progress.seekable, "再生バーの線がシークできない")
        if not args.xdotool:
            note("xdotool が無いので本物のクリックでのシークを飛ばします")
            return
        subprocess.run([args.xdotool, "windowfocus", "--sync", str(xid())], check=False)
        point = root_point(progress, 0.5, 0.5)
        if not check(point is not None, "再生バーの線の位置が分からない"):
            return
        subprocess.run([args.xdotool, "mousemove", "--sync", str(point[0]), str(point[1])], check=False)
        yield 0.4
        subprocess.run([args.xdotool, "click", "1"], check=False)
        dur = store().status.duration
        yield Until(lambda: abs(real_status().get("position", 0) - dur / 2) < 8, 5,
                    f"線の真ん中を押しても本物の位置が {dur / 2:.0f} 秒付近にならない")
        note(f"線の真ん中のクリック → 本物の位置 {real_status().get('position')} / {dur}")
        subprocess.run([args.xdotool, "mousemove", "--sync", "5", "5"], check=False)

    def scene_modes():
        window = win()
        bar = window.player_bar
        before = real_status()
        up_before = list(store().playlist.up_next)
        bar.shuffle.set_active(not before.get("shuffle"))
        yield Until(lambda: real_status().get("shuffle") is (not before.get("shuffle")), 5,
                    "シャッフルのボタンが本物に映らない")
        yield Until(lambda: store().playlist.up_next != up_before or before.get("shuffle"), 5,
                    "シャッフル後に up_next が取り直されない")
        note(f"シャッフル {before.get('shuffle')}→{real_status().get('shuffle')}: up_next {up_before} → "
             f"{store().playlist.up_next}")
        repeat_before = real_status().get("repeat")
        bar.repeat.emit("clicked")
        yield Until(lambda: real_status().get("repeat") != repeat_before, 5, "リピートのボタンが本物に映らない")
        note(f"リピート {repeat_before} → {real_status().get('repeat')}")
        yield 1.0
        capture(window, "modes")
        # 元に戻す (シャッフル オン、リピート All = 利用者の設定と同じ)
        store().set_shuffle(True)
        store().set_repeat("all")
        yield Until(lambda: real_status().get("shuffle") is True and real_status().get("repeat") == "All", 5,
                    "シャッフル・リピートを戻せない")

    def scene_fullscreen():
        window = win()
        yield from ensure_drive()
        i = index_of("/03.flac")
        store().play_index(i)
        yield Until(lambda: store().status.track is not None and store().status.track.path.endswith("/03.flac"), 6,
                    "03 にならない")
        store().seek_to(70.0)
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: window.fullscreen_shown, 3, "フルスクリーンにならない")
        player = window._fullscreen_player
        player.set_mode("lyrics")
        yield Until(lambda: player.lyrics.lyrics is not None and player.lyrics.current_index >= 0, 20,
                    "フルスクリーンの歌詞が出ない")
        yield 2.0
        capture(window, "fullscreen-lyrics")
        player.set_mode("queue")
        yield 2.0
        capture(window, "fullscreen-queue")
        player.set_mode("lyrics")
        app().activate_action("fullscreen-player", None)
        yield Until(lambda: not window.fullscreen_shown, 3, "フルスクリーンから戻らない")

    def scene_miniplayer():
        a = app()
        a.activate_action("miniplayer", None)
        yield Until(lambda: a.miniplayer is not None and a.miniplayer.get_mapped(), 5, "ミニプレーヤーが開かない")
        mini = a.miniplayer
        mini.set_mode("square")
        mini.set_hover(True, force=True)
        yield 2.0
        capture(mini, "mini-square")
        mini.set_mode("compact")
        yield 1.5
        capture(mini, "mini-compact")
        mini.set_mode("square")
        a.activate_action("miniplayer", None)
        yield 0.8

    def scene_tui():
        text = tui("after-gui")
        base = state.get("tui_start", "")
        # フッターの操作の案内 (いまのフォーカスで変わる) と、画面の上に重なる窓が無いか
        def footer(t: str) -> str:
            lines = [line for line in t.splitlines() if line.strip()]
            return lines[-2] if len(lines) >= 2 else ""

        note(f"TUI のフッター: 始め {footer(base)!r} / GUI 操作の後 {footer(text)!r}")
        check(footer(base) == footer(text), "GUI の操作で TUI のフォーカス (フッターの案内) が変わった")
        for word in ("Search", "Lyrics", "Queue", "Devices", "Keys", "Theme"):
            if f"── {word}" in text or f"[ {word}" in text:
                fail(f"TUI に {word} の重なりが開いている")
        yield 0.1

    def scene_openvoice():
        if not args.network or not args.cliamp_bin or not args.tmux_socket:
            note("--network / --cliamp-bin / --tmux-socket が無いので Open-Voice の流れを飛ばします")
            return
        env = {"HOME": str(cliamp_home), "PATH": os.environ.get("PATH", "")}
        status_text = subprocess.run([os.path.join(args.cliamp_bin, "cliamp"), "status"], env=env,
                                     capture_output=True, text=True, timeout=10)
        (out / "cliamp-status.txt").write_text(status_text.stdout + status_text.stderr, encoding="utf-8")
        note(f"cliamp status (GUI 接続中): rc={status_text.returncode} {status_text.stdout.strip()!r}")
        check(status_text.returncode == 0 and "State:" in status_text.stdout, "cliamp status の平文が変")
        before = real_status().get("track", {}).get("path")
        tmux = ["tmux", "-S", args.tmux_socket, "send-keys", "-t", "cliamp"]
        subprocess.run(tmux + ["C-f"], check=False)
        yield 1.5
        subprocess.run(tmux + ["-l", "lofi hip hop"], check=False)
        yield 0.5
        subprocess.run(tmux + ["Enter"], check=False)
        yield 12.0
        tui("openvoice-results")
        subprocess.run(tmux + ["Enter"], check=False)
        yield Until(lambda: "youtube.com" in (real_status().get("track", {}).get("path") or ""), 40,
                    "tmux の Ctrl+F の流れで YouTube の曲にならない")
        yield Until(lambda: store().status.track is not None and "youtube.com" in store().status.track.path
                    and store().status.state == "playing", 60, "GUI が tmux から再生した曲を映さない")
        yield 2.0
        st = real_status()
        note(f"Open-Voice の流れ: {before} → {st.get('track', {}).get('path')} "
             f"title={st.get('track', {}).get('title')!r} source={st.get('source')}")
        capture(win(), "openvoice-playing")
        tui("openvoice-playing")
        store().stop()
        yield Until(lambda: real_status().get("state") == "stopped", 10, "止まらない")

    def scene_playlist_edit():
        window = win()
        c = ctx()
        yield from ensure_drive()
        name = "E2E 一曲"
        c.catalog.playlist_delete(name)
        yield 0.5
        track = store().playlist.tracks[0]
        c.add_to_playlist(name, [track])
        yield Until(lambda: window.sidebar.find_row(f"playlist:local:{name}") is not None, 10,
                    "追加したプレイリストがサイドバーに出ない")
        window.navigate("playlist", provider="local", id=name, name=name)
        yield Until(lambda: page_id() == "playlist" and len(rows_of(page())) == 1, 10, "1 曲のプレイリストが出ない")
        yield 0.8
        capture(window, "playlist-one")
        # 行のメニューの「プレイリストから削除」と同じ (context local:<名前>)
        c._remove_from_playlist(name, 0)
        yield Until(lambda: name not in [p["id"] for p in real("playlists", provider="local").get("playlists", [])],
                    5, "本物で最後の曲を外してもプレイリストが残る")
        yield 3.0
        body = texts(page())
        note(f"最後の曲を外した後の詳細のページ: {body.replace(chr(10), ' / ')[:300]!r}")
        in_sidebar = window.sidebar.find_row(f"playlist:local:{name}") is not None
        note(f"最後の曲を外した後: サイドバーに残る={in_sidebar}、ctx.local_playlists={c.local_playlists}")
        check(not in_sidebar, "本物では消えたプレイリストがサイドバーに残る")
        capture(window, "playlist-one-removed")
        window.navigate("home")
        yield 0.5

    def scene_radio_play():
        if not args.network:
            note("--network が無いのでラジオの再生を飛ばします")
            return
        window = win()
        window.navigate("radio")
        yield Until(lambda: page_id() == "radio", 5, "ラジオが開かない")
        yield 4.0
        radio = page()
        tiles = [t for t in descendants(radio.cliamp_shelf) if hasattr(t, "playlist_info")]
        check(bool(tiles), "cliamp ラジオのタイルが無い")
        if not tiles:
            return
        radio._on_cliamp(tiles[0])
        yield Until(lambda: store().status.state == "playing" and store().status.track is not None
                    and store().status.track.live, 30, "cliamp ラジオが再生にならない")
        yield 8.0
        st = real_status()
        pl = real("playlist")
        note(f"cliamp ラジオ: 本物 track={st.get('track')} duration={st.get('duration')} "
             f"position={st.get('position')} stream_title={st.get('stream_title')!r} total={pl.get('total')} "
             f"source={st.get('source')}")
        bar = window.player_bar
        note(f"cliamp ラジオの再生バー: 題={bar.title_label.get_text()!r} 副題={bar.subtitle_label.get_text()!r} "
             f"ライブ={bar.live_badge.get_visible()} 線={bar.progress.get_visible()}")
        capture(window, "radio-cliamp-playing")
        stations = [t for t in descendants(radio) if hasattr(t, "track") and getattr(t, "track", None) is not None
                    and not hasattr(t, "playlist_info") and t.track.live]
        if stations:
            radio._on_station(stations[0])
            yield Until(lambda: store().status.track is not None and store().status.track.path == stations[0].track.path
                        and store().status.state == "playing", 40, "Radio Browser の局が再生にならない")
            yield 8.0
            st = real_status()
            note(f"Radio Browser の局: 本物 track={st.get('track')} stream_title={st.get('stream_title')!r} "
                 f"source={st.get('source')}")
            note(f"局の再生バー: 題={bar.title_label.get_text()!r} 副題={bar.subtitle_label.get_text()!r} "
                 f"ライブ={bar.live_badge.get_visible()} 線={bar.progress.get_visible()}")
            capture(window, "radio-browser-playing")
            # 局をプレイリストに足すと live と絵 (meta) が落ちる
            name = "E2E 局"
            ctx().catalog.playlist_delete(name)
            yield 0.5
            ctx().add_to_playlist(name, [stations[0].track])
            yield Until(lambda: bool(real("tracks", provider="local", id=name).get("tracks")), 10, "局を足せない")
            note(f"局をプレイリストに足した後の曲: {real('tracks', provider='local', id=name).get('tracks')}")
            ctx().catalog.playlist_delete(name)
        store().stop()
        yield Until(lambda: real_status().get("state") == "stopped", 10, "ラジオが止まらない")
        yield from ensure_drive()

    def scene_station():
        if not args.network:
            note("--network が無いのでステーションを飛ばします")
            return
        window = win()
        window.navigate("home")
        yield Until(lambda: page_id() == "home" and page().picks.get_visible(), 20, "ホームのおすすめが出ない")
        home = page()
        cards = [c for c in descendants(home.picks) if getattr(c, "pick_kind", "") == "station"]
        if not check(bool(cards), "おすすめにステーションが無い"):
            return
        subject = cards[0].pick_subject
        start = time.monotonic()
        home._on_pick(cards[0])
        yield Until(lambda: (real_status().get("source") or {}).get("provider") == "url", 90,
                    "ステーションが読み込まれない (load_provider url)")
        loaded = time.monotonic() - start
        yield Until(lambda: store().status.state == "playing" and not store().status.buffering, 60,
                    "ステーションが再生にならない")
        gens = []
        for _ in range(8):
            st = real_status()
            gens.append((st.get("gen"), st.get("total")))
            yield 1.0
        note(f"ステーション ({subject.display_title}): 読み込み {loaded:.1f} 秒、source={real_status().get('source')}、"
             f"(gen, total) の推移 {gens}、アプリの写し {len(store().playlist.tracks)} 曲")
        capture(window, "station-playing")
        window.navigate("nowplaying")
        yield 2.0
        capture(window, "station-nowplaying")
        store().stop()
        yield Until(lambda: real_status().get("state") == "stopped", 10, "ステーションが止まらない")
        yield from ensure_drive()

    def scene_restart():
        if not (args.stop_cmd and args.start_cmd):
            note("--stop-cmd / --start-cmd が無いので繋ぎ直しを飛ばします")
            return
        window = win()
        before = real_status()
        proc = run_cmd(args.stop_cmd)
        yield Until(proc.finished, 30, "cliamp を止められない")
        yield Until(lambda: not store().connected, 15, "cliamp を止めても接続のまま")
        yield Until(lambda: window.offline.get_visible(), 5, "未接続の表示が出ない")
        yield 1.5
        check(not app().lookup_action("next").get_enabled(), "未接続なのに「次へ」が使えます")
        capture(window, "disconnected")
        proc = run_cmd(args.start_cmd)
        yield Until(proc.finished, 30, "cliamp を起こせない")
        note(f"起こし直し: {proc.output}")
        yield Until(lambda: store().connected and store().api == 1, 20, "起こし直した cliamp に繋がらない")
        yield Until(lambda: not window.offline.get_visible(), 5, "未接続の表示が消えない")
        yield 2.0
        st = real_status()
        note(f"起こし直す前の本物: state={before.get('state')} total={before.get('total')}; 後: "
             f"state={st.get('state')} total={st.get('total')} track={st.get('track', {}).get('path')}; "
             f"アプリの写し {len(store().playlist.tracks)} 曲、再生バーの曲 "
             f"{win().player_bar.title_label.get_text()!r}")
        window.navigate("home")
        yield 3.0
        capture(window, "reconnected")

    def scene_legacy():
        if not args.legacy_cmd:
            note("--legacy-cmd が無いので拡張なしを飛ばします")
            return
        window = win()
        proc = run_cmd(args.legacy_cmd)
        yield Until(proc.finished, 30, "拡張なしの cliamp が起きない")
        note(f"拡張なし: {proc.output}")
        yield Until(lambda: store().connected and store().api == 0, 20, "拡張なしの cliamp に繋がらない")
        yield Until(lambda: window.banner.get_revealed(), 5, "拡張なしの帯が出ない")
        loaded = real("load", playlist="ドライブ")
        note(f"拡張なしで load: {loaded}")
        yield Until(lambda: store().status.track is not None, 10, "拡張なしで曲が再生バーに出ない")
        yield 1.5
        window.navigate("home")
        yield 2.0
        capture(window, "legacy-home")
        window.navigate("nowplaying")
        yield 2.0
        note("拡張なしの再生中のリスト: " + texts(page()).replace("\n", " / ")[:300])
        capture(window, "legacy-nowplaying")
        index = real_status().get("index", 0)
        store().next()
        yield Until(lambda: real_status().get("index", 0) != index, 5, "拡張なしで次へが効かない")
        store().toggle()
        yield Until(lambda: real_status().get("state") == "paused", 5, "拡張なしで一時停止が効かない")
        store().toggle()
        yield Until(lambda: real_status().get("state") == "playing", 5, "拡張なしで再開が効かない")
        vol = real_status().get("volume")
        store().volume_step(-2.0)
        yield Until(lambda: real_status().get("volume") != vol, 5, "拡張なしで音量が効かない")
        note(f"拡張なしの音量 {vol} → {real_status().get('volume')}")
        store().set_volume_db(-20.0)
        window.show_panel("queue")
        yield 1.0
        capture(window, "legacy-queue")
        window.show_panel("")
        if args.patched_cmd:
            proc = run_cmd(args.patched_cmd)
            yield Until(proc.finished, 30, "パッチ済みに戻らない")
            yield Until(lambda: store().connected and store().api == 1, 20, "パッチ済みの cliamp に戻らない")
            yield Until(lambda: not window.banner.get_revealed(), 5, "パッチ済みに戻っても帯が消えない")
            yield 2.0
            capture(window, "back-to-patched")

    scenes = [scene_start, scene_load_local, scene_home, scene_playlists, scene_recent, scene_search_library,
              scene_search_spotify, scene_search_youtube, scene_radio, scene_nowplaying, scene_queue_panel,
              scene_lyrics, scene_eq, scene_devices, scene_volume, scene_seek, scene_modes, scene_fullscreen,
              scene_miniplayer, scene_tui, scene_openvoice, scene_playlist_edit, scene_radio_play,
              scene_station, scene_restart, scene_legacy]
    if args.only:
        wanted = set(args.only) | {"start"}
        scenes = [s for s in scenes if s.__name__.replace("scene_", "") in wanted]

    def all_steps():
        for scene in scenes:
            name = scene.__name__.replace("scene_", "")
            print(f"e2e: 場面 {name}", flush=True)
            gen = scene()
            while True:
                try:
                    item = next(gen)
                except StopIteration:
                    break
                except Exception as exc:
                    traceback.print_exc()
                    fail(f"{name}: {exc}")
                    break
                yield item

    steps = all_steps()

    def advance() -> bool:
        try:
            item = next(steps)
        except StopIteration:
            finish()
            return GLib.SOURCE_REMOVE
        if isinstance(item, Until):
            deadline = time.monotonic() + item.timeout

            def poll() -> bool:
                try:
                    ok = bool(item.predicate())
                except Exception as exc:
                    fail(f"{item.label}: 待つ条件で例外: {exc}")
                    ok = True
                if ok or time.monotonic() > deadline:
                    if not ok:
                        fail(f"{item.label} ({item.timeout} 秒)")
                    GLib.idle_add(advance)
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            GLib.timeout_add(100, poll)
        else:
            GLib.timeout_add(int(float(item or 0) * 1000), advance)
        return GLib.SOURCE_REMOVE

    def finish() -> None:
        a = app()
        if a is not None:
            a.quit_app()

    def start() -> bool:
        a = app()
        if a is None or a.window is None:
            return GLib.SOURCE_CONTINUE
        result["app"] = a
        win().set_default_size(1180, 760)
        GLib.idle_add(advance)
        return GLib.SOURCE_REMOVE

    GLib.timeout_add(100, start)
    code = app_module.main(["cliamp-music", "--socket", sock, "--page", "home"])
    for message in getattr(result.get("app"), "css_errors", []):
        fail(f"CSS: {message}")
    (out / "notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    (out / "problems.txt").write_text("\n".join(problems) + "\n", encoding="utf-8")
    print(f"e2e: {len(shots)} 枚、観察 {len(notes)} 件、失敗 {len(problems)} 件", flush=True)
    return 1 if problems else code


def main() -> int:
    parser = argparse.ArgumentParser(description="本物の cliamp に繋いでミュージックを端から端まで動かす")
    parser.add_argument("--socket", required=True, help="本物の cliamp (私用) のソケット")
    parser.add_argument("--out", required=True, help="PNG・TUI の画面・観察を置くディレクトリ")
    parser.add_argument("--tmux-socket", help="cliamp の TUI が動いている私用の tmux のソケット")
    parser.add_argument("--cliamp-home", help="cliamp の HOME (既定: ソケットから求める)")
    parser.add_argument("--cliamp-bin", help="cliamp の bin (cliamp status の確かめ用)")
    parser.add_argument("--bus", help="使う D-Bus のアドレス (cliamp と同じ私用のバス)")
    parser.add_argument("--stop-cmd", help="cliamp を止めるコマンド (繋ぎ直しの確かめ)")
    parser.add_argument("--start-cmd", help="パッチ済みの cliamp を起こすコマンド (繋ぎ直しの確かめ)")
    parser.add_argument("--legacy-cmd", help="パッチの無い cliamp に入れ替えるコマンド")
    parser.add_argument("--patched-cmd", help="パッチ済みの cliamp に戻すコマンド")
    parser.add_argument("--xdotool", default=shutil.which("xdotool"), help="xdotool のパス")
    parser.add_argument("--network", action="store_true", help="YouTube・LRCLIB・tmux の検索を本物で行う")
    parser.add_argument("--only", action="append", help="この場面だけ (複数可。start はいつも)")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        return child_main(args)
    return run_parent(args)


if __name__ == "__main__":
    sys.exit(main())
