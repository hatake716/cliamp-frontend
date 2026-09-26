"""「Spotify から取り込む」の窓 (SpotifyImportDialog) と、取り込み・置き換え・更新の手順。

AppContext の open_spotify_import / update_spotify_import から使う。頁の取得 (spotify_import.fetch_list)
は worker のスレッドで行い、結果は GLib.idle_add で main loop に戻す。

窓 (Adw.AlertDialog。どの状態でも高さを変えない。macOS のシートと同じく、打っている欄が動かない):
    Spotify から取り込む
    公開プレイリストかアルバムのリンクを貼り付けてください。…
    [https://open.spotify.com/playlist/…        ]   ← 貼り付けたらすぐ読む。打っているときは少し待って読む
     Spotify の「共有」→「リンクをコピー」のリンク   ← いつも同じ案内
    ┌[絵] Today's Top Hits                ┐        ← 1 つの面 (高さ一定) を状態で差し替える:
    │     プレイリスト · Spotify · 50 曲   │           案内 / 形の誤り・読めなかった理由 (琥珀色、
    └────────────────────────┘           「もう一度読む」) / スピナー / 読めたもの
    プレイリストの名前
    [Today's Top Hits                ]              ← いつもある (読めるまでは押せない)。読めたら
     同じ名前のプレイリストがあります…              Spotify の名前を入れる (打ち替えたら触らない)
                          [キャンセル] [取り込む]

リンクの形の誤りは、打っている途中には出さない (貼り付けたとき・少し手を止めたとき・Enter・欄を離れたとき
に出す)。「取り込む」は読めて名前が使えて cliamp に保存できるときだけ押せる (cliamp との接続が変われば
その場で直す)。押すとローカルのプレイリストの一覧を取り直し、名前が重なれば「置き換える / 別の名前にする」
を尋ねる (cliamp の playlist_add は同じ名前のプレイリストに黙って足すため、先に確かめる)。置き換え
(と「Spotify から更新」) は、今の曲を読んで控え (ImportIndex.backup) てから消して足し直し、足せなければ
読んでおいた曲を戻す (戻せたのを見届けてから知らせる。戻すこともできなければ、控えの場所と「もう一度戻す」
の窓を出す)。保存できたら取り込んだことを表 (ImportIndex) に書き、トースト「「名前」を取り込みました
(N 曲)」(100 曲で切れていればその知らせも) を出して詳細へ移る。

「Spotify から更新」は、読み直した曲と今の曲を比べ、「「名前」の N 曲を、Spotify の M 曲に置き換えます」と
確かめてから置き換える (取り込んでいない曲が混じっていればそのことも)。今の曲が取り込んだものでなくなって
いれば (TUI で消して同じ名前で作り直したなど) 置き換えずに記録を忘れる。
"""

from __future__ import annotations

import os
import threading
import weakref
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk, Pango  # noqa: E402

from . import log  # noqa: E402
from .protocol import Response, Track, playlist_name_problem  # noqa: E402
from .spotify_import import (  # noqa: E402
    EMBED_TRACK_CAP,
    ImportedList,
    SpotifyImportError,
    SpotifyRef,
    check_spotify_url,
    playlist_name_for,
    suggest_name,
    summary_line,
    truncated_note,
)
from . import spotify_import  # noqa: E402
from .widgets import Artwork  # noqa: E402

FETCH_DELAY_MS = 350  # 打ち終えてから読みに行くまで (貼り付けたときは待たない)
PROBLEM_DELAY_MS = 900  # 打っている途中のリンクの形の誤りを出すまで (手を止めたら出す)
PASTE_MIN_CHARS = 8  # 1 度の変化でこれだけ増えたら貼り付けとみなす
FORM_WIDTH = 420
NAME_HINT_HEIGHT = 20  # 名前の下の案内の行の高さ (空でも同じ)
URL_HINT = "Spotify の「共有」→「リンクをコピー」で出る open.spotify.com のリンク"
PLACEHOLDER_TEXT = "リンクを貼ると、ここにプレイリストの名前と曲数が出ます"
LOADING_TEXT = "Spotify から読み込んでいます…"
RETRY_LABEL = "もう一度読む"
IMPORT_TITLE = "Spotify から取り込む"
IMPORT_BODY = "公開プレイリストかアルバムのリンクを貼り付けてください。\n曲は YouTube で探して再生します。"
MISSING_TEXT = "no such file or directory"
# 読み直せば通るかもしれない失敗 (「もう一度読む」を出す)
RETRY_KINDS = frozenset({"network", "rate-limited", "unavailable"})


# ---------------------------------------------------------------------------
# 取得 (worker のスレッド)


def fetch_async(target: SpotifyRef | str, callback: Callable[[ImportedList | SpotifyImportError], object]) -> None:
    """target を worker のスレッドで取って読み、callback(ImportedList か SpotifyImportError) を main loop で呼ぶ。"""

    def deliver(value) -> bool:
        try:
            callback(value)
        except Exception as exc:  # 1 つの取り込みの失敗でアプリを止めない
            import traceback

            log(f"取り込みの結果の処理で例外: {exc}")
            traceback.print_exc()
        return GLib.SOURCE_REMOVE

    def run() -> None:
        try:
            value = spotify_import.fetch_list(target)
        except SpotifyImportError as exc:
            if exc.detail:
                log(f"Spotify から取り込めません ({exc.kind}): {exc.detail}")
            value = exc
        except Exception as exc:  # 思わぬ形の頁など。窓には「形が変わった」と言う
            log(f"Spotify の頁を読めません: {exc!r}")
            value = SpotifyImportError("Spotify の頁の形が変わったため読めません (アプリの更新が要ります)", "shape",
                                       repr(exc))
        GLib.idle_add(deliver, value)

    threading.Thread(target=run, name="cliamp-music-spotify-import", daemon=True).start()


# ---------------------------------------------------------------------------
# 窓


def _label(text: str = "", css: tuple[str, ...] = (), *, wrap: bool = False) -> Gtk.Label:
    label = Gtk.Label()
    label.set_text(text)
    label.set_xalign(0.0)
    if wrap:
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    else:
        label.set_ellipsize(Pango.EllipsizeMode.END)
    for cls in css:
        label.add_css_class(cls)
    return label


def _tile(child: Gtk.Widget, css: str) -> Gtk.Box:
    """面の左の 56px の角丸の升 (記号・スピナー)。"""
    tile = Gtk.Box()
    tile.add_css_class("music-import-tile")
    tile.add_css_class(css)
    tile.set_size_request(56, 56)
    tile.set_valign(Gtk.Align.CENTER)
    tile.set_hexpand(False)  # 中の記号の hexpand を面の側へ伝えない (升が横に伸びる)
    child.set_halign(Gtk.Align.CENTER)
    child.set_valign(Gtk.Align.CENTER)
    child.set_hexpand(True)
    tile.append(child)
    return tile


def _well_page(leading: Gtk.Widget, *texts: Gtk.Widget) -> Gtk.Box:
    page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    page.add_css_class("music-import-well-page")
    page.append(leading)
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    column.set_valign(Gtk.Align.CENTER)
    column.set_hexpand(True)
    for text in texts:
        column.append(text)
    page.append(column)
    return page


class SpotifyImportDialog(Adw.AlertDialog):
    """「Spotify から取り込む」の窓。

    `SpotifyImportDialog(ctx, url="", result=None, name="")`: url を渡すと開いてすぐ読む。result (読めた
    もの) と name を渡すと読み直さずにその状態から始める (「別の名前にする」で開き直すとき)。
    state: "empty" | "invalid" (リンクの形が違う) | "loading" | "error" (読めない) | "ready" (読めた)。
    well (Gtk.Stack) の見えている頁: "placeholder" | "problem" | "loading" | "preview"。problem は
    いま面に出している誤りの文 (出していなければ "")。"""

    __gtype_name__ = "CliampMusicSpotifyImportDialog"

    def __init__(self, ctx, *, url: str = "", result: ImportedList | None = None, name: str = ""):
        super().__init__(heading=IMPORT_TITLE, body=IMPORT_BODY)
        self.add_css_class("music-import-dialog")
        self.ctx = ctx
        self.state = "empty"
        self.result: ImportedList | None = None
        self.error: SpotifyImportError | None = None
        self.problem = ""
        self._ref: SpotifyRef | None = None
        self._serial = 0
        self._timer = 0  # 読みに行くまでの待ち
        self._problem_timer = 0  # 打っている途中の形の誤りを出すまでの待ち
        self._fetching = False  # 読みに行っている (答えを待っている)
        self._last_len = 0
        self._closed = False
        self._presented = False
        # この窓で読めたもの (リンクを打ち直して同じものに戻ったときに読み直さない)
        self._results: dict[SpotifyRef, ImportedList] = {}
        self._name_edited = False
        self._setting_name = False
        self._suppress = False
        self._handlers: list[tuple[GObject.Object, int]] = []

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.add_css_class("music-import-form")
        # リンクが読める幅 (既定の警告の幅では open.spotify.com のリンクと説明の 1 文目が折れる)
        box.set_size_request(FORM_WIDTH, -1)

        self.url_entry = Gtk.Entry()
        self.url_entry.add_css_class("music-import-entry")
        self.url_entry.set_placeholder_text("https://open.spotify.com/playlist/…")
        self.url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self.url_entry.set_input_hints(Gtk.InputHints.NO_SPELLCHECK | Gtk.InputHints.NO_EMOJI)
        self.url_entry.set_activates_default(True)
        self.url_entry.update_property([Gtk.AccessibleProperty.LABEL], ["Spotify のリンク"])
        box.append(self.url_entry)
        self.url_hint = _label(URL_HINT, ("music-import-hint",), wrap=True)
        box.append(self.url_hint)

        # 1 つの面 (高さ一定) に、案内・誤り・読み込み中・読めたもののどれか 1 つ
        self.well = Gtk.Stack()
        self.well.add_css_class("music-import-well")
        self.well.set_vhomogeneous(True)
        self.well.set_hhomogeneous(True)
        self.well.set_interpolate_size(False)
        self.well.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.well.set_transition_duration(120)

        icon = Gtk.Image.new_from_icon_name("music-import-symbolic")
        icon.set_pixel_size(22)
        self.placeholder = _well_page(_tile(icon, "placeholder"),
                                      _label(PLACEHOLDER_TEXT, ("music-import-placeholder-label",), wrap=True))
        self.well.add_named(self.placeholder, "placeholder")

        warning = Gtk.Image.new_from_icon_name("music-warning-symbolic")
        warning.set_pixel_size(22)
        self.problem_label = _label("", ("music-import-problem",), wrap=True)
        self.problem_label.set_lines(3)
        self.problem_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.retry_button = Gtk.Button()
        self.retry_button.set_child(_label(RETRY_LABEL, ("music-import-retry-label",)))
        self.retry_button.add_css_class("music-import-retry")
        self.retry_button.add_css_class("flat")
        self.retry_button.set_halign(Gtk.Align.START)
        self.retry_button.set_tooltip_text("Enter でも読み直せます")
        self.retry_button.set_visible(False)
        self.problem_page = _well_page(_tile(warning, "problem"), self.problem_label, self.retry_button)
        self.well.add_named(self.problem_page, "problem")

        spinner = Adw.Spinner()
        spinner.set_size_request(20, 20)
        self.loading = _well_page(_tile(spinner, "loading"), _label(LOADING_TEXT, ("music-import-loading-label",)))
        self.well.add_named(self.loading, "loading")

        self.preview_art = Artwork(56, 56, radius=6)
        self.preview_art.set_valign(Gtk.Align.CENTER)
        self.preview_title = _label("", ("music-import-preview-title",))
        self.preview_summary = _label("", ("music-import-preview-summary",))
        self.preview_note = _label("", ("music-import-preview-note",))
        self.preview_note.set_visible(False)
        self.preview = _well_page(self.preview_art, self.preview_title, self.preview_summary, self.preview_note)
        self.well.add_named(self.preview, "preview")
        # 高さの見本 (出さない頁)。書き添えは読めてから出すので、隠れている間は Stack の高さに数えられず、
        # 字形によっては読めた途端に面が 2px ほど伸びる。書き添えまでの 3 行 (欧文と和文) を先に数えておく
        sizer = Gtk.Box()
        sizer.set_size_request(56, 56)
        self.well.add_named(_well_page(sizer, *(_label("Ag あ", (css,)) for css in (
            "music-import-preview-title", "music-import-preview-summary", "music-import-preview-note"))), "sizer")
        self.well.set_visible_child_name("placeholder")
        box.append(self.well)

        # 名前 (いつも出しておく。読めるまでは打てない)
        self.name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.name_box.add_css_class("music-import-name")
        self.name_box.append(_label("プレイリストの名前", ("music-import-label",)))
        self.name_entry = Gtk.Entry()
        self.name_entry.add_css_class("music-import-entry")
        self.name_entry.set_placeholder_text("名前")
        self.name_entry.set_activates_default(True)
        self.name_entry.set_sensitive(False)
        self.name_entry.update_property([Gtk.AccessibleProperty.LABEL], ["プレイリストの名前"])
        self.name_box.append(self.name_entry)
        # 1 行 (長い文は省略してツールチップ)。空でも同じ高さを取っておく (窓の高さを変えない。空の行・
        # 欧文の行・和文の行とでは、字形の違いで自然な高さが数 px ずつ違う)
        self.name_hint = _label("", ("music-import-hint", "music-import-name-hint"))
        self.name_hint.set_size_request(-1, NAME_HINT_HEIGHT)
        self.name_box.append(self.name_hint)
        box.append(self.name_box)

        self.set_extra_child(box)
        self.set_prefer_wide_layout(True)
        self.add_response("cancel", "キャンセル")
        self.add_response("import", "取り込む")
        self.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("import")
        self.set_close_response("cancel")
        self.set_response_enabled("import", False)

        me = weakref.ref(self)
        self.url_entry.connect("changed", lambda _e: SpotifyImportDialog._call(me, "_on_url_changed"))
        self.url_entry.connect("activate", lambda _e: SpotifyImportDialog._call(me, "_on_url_activate"))
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c: SpotifyImportDialog._call(me, "_on_url_leave"))
        self.url_entry.add_controller(focus)
        self.retry_button.connect("clicked", lambda _b: SpotifyImportDialog._call(me, "retry"))
        self.name_entry.connect("changed", lambda _e: SpotifyImportDialog._call(me, "_on_name_changed"))
        self.connect("response", SpotifyImportDialog._on_response)
        self.connect("closed", SpotifyImportDialog._on_closed)
        # cliamp との接続・ローカルのプレイリストの一覧が変わったら、「取り込む」と名前の案内を直す
        for source, signal in ((getattr(ctx, "store", None), "connection-changed"),
                               (ctx, "local-playlists-changed")):
            if isinstance(source, GObject.Object) and GObject.signal_lookup(signal, type(source)):
                handler = source.connect(signal, lambda *_a: SpotifyImportDialog._call(me, "_on_outside_changed"))
                self._handlers.append((source, handler))

        if result is not None:
            self._ref = result.ref
            self._results[result.ref] = result
            self._set_url_text(url or result.url)
            self._show_result(result)
            if name:
                self._set_name(name)
                self._name_edited = True
                self._update_name_area()
        elif url:
            self._set_url_text(url)
            self._on_url_changed(immediate=True)
        else:
            self._update_name_area()

    @staticmethod
    def _call(ref, method: str) -> None:
        dialog = ref()
        if dialog is not None:
            getattr(dialog, method)()

    # --- 状態 -------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """閉じたか (窓の子として出したときは "closed" が来る。窓なしで出したときは来ないので、
        出した後で根を失ったかでも見る)。"""
        return self._closed or (self._presented and self.get_root() is None)

    @property
    def ready(self) -> bool:
        return self.state == "ready" and self.result is not None and self._name_ok()

    @property
    def name(self) -> str:
        return self.name_entry.get_text().strip()

    @property
    def fetching(self) -> bool:
        """読みに行って答えを待っているか。"""
        return self._fetching

    def _name_ok(self) -> bool:
        return not playlist_name_problem(self.name)

    def _set_url_text(self, text: str) -> None:
        self._suppress = True
        try:
            self.url_entry.set_text(text)
        finally:
            self._suppress = False
        self._last_len = len(text)

    def _set_name(self, text: str) -> None:
        self._setting_name = True
        try:
            self.name_entry.set_text(text)
        finally:
            self._setting_name = False

    def _cancel_timer(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _cancel_problem_timer(self) -> None:
        if self._problem_timer:
            GLib.source_remove(self._problem_timer)
            self._problem_timer = 0

    def _drop_fetch(self) -> None:
        """読みかけのものの答えを捨てる。"""
        self._serial += 1
        self._fetching = False

    def _update_ready(self) -> None:
        self.set_response_enabled("import", self.ready and self._can_save())

    def _can_save(self) -> bool:
        store = self.ctx.store
        return bool(store.connected and store.supports("playlist_add"))

    def _on_outside_changed(self) -> None:
        if self.closed:
            # 窓なしで出した窓は "closed" が来ないことがあるので、ここでも見張りを外す
            self._disconnect_outside()
            return
        self._update_name_hint()
        self._update_ready()

    def _disconnect_outside(self) -> None:
        for source, handler in self._handlers:
            if source.handler_is_connected(handler):
                source.disconnect(handler)
        self._handlers.clear()

    # --- 面 -----------------------------------------------------------------------

    def _show_page(self, name: str) -> None:
        self.well.set_visible_child_name(name)
        if name == "problem":
            self.well.add_css_class("problem")
        else:
            self.well.remove_css_class("problem")
            self.problem = ""

    def _show_placeholder(self) -> None:
        self._show_page("placeholder")

    def _show_problem(self, text: str, *, retry: bool = False) -> None:
        self.problem = text
        self.problem_label.set_text(text)
        self.problem_label.set_tooltip_text(text)
        self.retry_button.set_visible(retry)
        self._show_page("problem")

    def _show_loading(self) -> None:
        self._show_page("loading")

    # --- リンク -------------------------------------------------------------------

    def _on_url_changed(self, immediate: bool = False) -> None:
        if self._suppress:
            return
        text = self.url_entry.get_text()
        grew = len(text) - self._last_len
        self._last_len = len(text)
        # 貼り付けた (1 度に何文字も増えた) なら、形の誤りはすぐ言い、正しい形ならすぐ読む
        pasted = immediate or grew >= PASTE_MIN_CHARS
        self._cancel_problem_timer()
        ref, reason = check_spotify_url(text)
        if ref is None:
            self._cancel_timer()
            self._drop_fetch()
            self._ref = None
            self.result = None
            self.error = None
            if not reason:
                self.state = "empty"
                self._show_placeholder()
            else:
                self.state = "invalid"
                if pasted:
                    self._show_problem(reason)
                else:
                    # 打っている途中 (22 文字の ID を打ち終えるまでなど) は言わない。手を止めたら言う
                    self._show_placeholder()
                    self._problem_timer = GLib.timeout_add(PROBLEM_DELAY_MS, self._problem_due, weakref.ref(self))
            self._update_name_area()
            return
        if ref == self._ref and self.state == "ready":
            return  # 同じものが読めている (?si= だけ違うなど)
        if ref == self._ref and self.state == "loading":
            if not self._fetching:
                # 同じものを読みに行く前 (打ち続けている。ID の後に "/" や "?si=…" を打った): 待ち直す
                self._schedule_fetch(now=pasted)
            return
        self._cancel_timer()
        self._drop_fetch()
        self._ref = ref
        known = self._results.get(ref)
        if known is not None:
            # この窓でもう読んだもの (貼り直しで一度空になった・前のリンクに戻した)
            self._show_result(known)
            return
        self.result = None
        self.error = None
        self.state = "loading"
        self._show_loading()
        self._update_name_area()
        self._schedule_fetch(now=pasted)

    @staticmethod
    def _problem_due(ref) -> bool:
        dialog = ref()
        if dialog is not None:
            dialog._problem_timer = 0
            dialog._show_pending_problem()
        return GLib.SOURCE_REMOVE

    def _show_pending_problem(self) -> None:
        """打っている途中で出さずにおいた形の誤りを出す。"""
        self._cancel_problem_timer()
        if self.state != "invalid" or self.closed:
            return
        _ref, reason = check_spotify_url(self.url_entry.get_text())
        if reason:
            self._show_problem(reason)

    def _schedule_fetch(self, *, now: bool = False) -> None:
        self._cancel_timer()
        if now:
            self._fetch()
            return
        me = weakref.ref(self)

        def fire() -> bool:
            dialog = me()
            if dialog is not None:
                dialog._timer = 0
                dialog._fetch()
            return GLib.SOURCE_REMOVE

        self._timer = GLib.timeout_add(FETCH_DELAY_MS, fire)

    def _on_url_activate(self) -> None:
        # 読めていれば Enter は既定の応答 (「取り込む」) になる (activates-default)。まだなら待たずに読む
        if self.ready:
            return
        if self.state == "invalid":
            self._show_pending_problem()
        elif self.state == "loading" and not self._fetching:
            self._fetch()
        elif self.state == "error":
            self.retry()

    def _on_url_leave(self) -> None:
        # 欄を離れたら、打っている途中で出さずにおいた形の誤りを出す
        if self.state == "invalid" and not self.problem:
            self._show_pending_problem()

    def retry(self) -> None:
        """読めなかったものを読み直す (「もう一度読む」・Enter)。"""
        if self.state != "error" or self._ref is None or self.closed:
            return
        self.state = "loading"
        self.error = None
        self._show_loading()
        self._update_name_area()
        self._fetch()

    def fetch_now(self) -> None:
        """待たずに読む (試験・撮影用)。"""
        self._cancel_timer()
        if self._ref is not None and self.state == "loading" and not self._fetching:
            self._fetch()

    def _fetch(self) -> None:
        ref = self._ref
        self._cancel_timer()
        if ref is None or self.closed:
            return
        self._serial += 1
        serial = self._serial
        self._fetching = True
        me = weakref.ref(self)

        def done(value) -> None:
            dialog = me()
            if dialog is None or dialog.closed or serial != dialog._serial:
                return
            dialog._fetching = False
            if isinstance(value, SpotifyImportError):
                dialog._show_error(value)
            else:
                dialog._results[ref] = value
                dialog._show_result(value)

        fetch_async(ref, done)

    def _show_error(self, error: SpotifyImportError) -> None:
        self.state = "error"
        self.result = None
        self.error = error
        self._show_problem(error.message, retry=error.kind in RETRY_KINDS)
        self._update_name_area()

    def _show_result(self, result: ImportedList) -> None:
        self.state = "ready"
        self.result = result
        self.error = None
        self.preview_title.set_text(result.name)
        self.preview_title.set_tooltip_text(result.name)
        self.preview_summary.set_text(summary_line(result))
        note = ""
        if result.truncated:
            # 全体の曲数は上の行 (「100 曲 (全 150 曲)」) にある
            note = f"公開ページは {EMBED_TRACK_CAP} 曲までのため、最初の {result.count} 曲だけ取り込みます"
        elif result.skipped:
            note = f"曲でない {result.skipped} 件 (ポッドキャストなど) は取り込みません"
        self.preview_note.set_text(note)
        self.preview_note.set_tooltip_text(note or None)
        self.preview_note.set_visible(bool(note))
        if result.cover_url:
            self.preview_art.set_subject(self.ctx.artwork, result.cover_url,
                                         key_for_placeholder=f"spotify:{result.id}", kind="playlist")
        else:
            self.preview_art.set_subject(self.ctx.artwork, ("placeholder", f"spotify:{result.id}", "playlist"),
                                         kind="playlist")
        self._show_page("preview")
        if not self._name_edited or not self.name:
            # 同じ名前があっても Spotify の名前のまま (取り込み直しなら置き換えたいことが多い)。重なることは
            # 名前の下に書き、「取り込む」の後で置き換えるか別の名前にするかを尋ねる
            self._set_name(playlist_name_for(result))
        self._update_name_area()

    # --- 名前 -------------------------------------------------------------------

    def _existing_names(self) -> list[str]:
        return list(getattr(self.ctx, "local_playlists", []) or [])

    def _on_name_changed(self) -> None:
        if not self._setting_name:
            self._name_edited = True
        self._update_name_hint()
        self._update_ready()

    def _update_name_area(self) -> None:
        """名前の欄は読めたときだけ打てる。読めていないときは (打ち替えていなければ) 空にしておく。"""
        ready = self.state == "ready" and self.result is not None
        self.name_entry.set_sensitive(ready)
        if not ready and not self._name_edited and self.name_entry.get_text():
            self._set_name("")
        self._update_name_hint()
        self._update_ready()

    def _update_name_hint(self) -> None:
        store = self.ctx.store
        text, problem = "", False
        if not store.connected:
            text, problem = "cliamp に接続していません (繋がったら取り込めます)", True
        elif not store.supports("playlist_add"):
            text, problem = "この cliamp はプレイリストを保存できません", True
        elif self.state == "ready":
            name = self.name
            reason = playlist_name_problem(name)
            if reason:
                text, problem = reason, True
            elif name in self._existing_names():
                text = "同じ名前のプレイリストがあります。取り込むときに置き換えるか選べます"
        self.name_hint.set_text(text)
        self.name_hint.set_tooltip_text(text or None)
        if problem:
            self.name_hint.add_css_class("problem")
        else:
            self.name_hint.remove_css_class("problem")

    # --- 閉じる -------------------------------------------------------------------

    @staticmethod
    def _on_response(dialog: "SpotifyImportDialog", response: str) -> None:
        dialog._cancel_timer()
        dialog._cancel_problem_timer()
        if response != "import" or not dialog.ready or dialog.result is None or not dialog._can_save():
            return
        start_import(dialog.ctx, dialog.name, dialog.result, url=dialog.url_entry.get_text().strip())

    @staticmethod
    def _on_closed(dialog: "SpotifyImportDialog") -> None:
        dialog._closed = True
        dialog._cancel_timer()
        dialog._cancel_problem_timer()
        dialog._drop_fetch()
        dialog._disconnect_outside()


def present_dialog(ctx, dialog: Adw.Dialog) -> None:
    """窓に出し、閉じるまで ctx.spotify_import_dialog で掴んでおく (PyGObject は窓が持っている Adw.Dialog
    でも Python の包みを手放すことがあり、手放すと包みに置いた部品や状態が消える)。"""
    ctx.spotify_import_dialog = dialog
    dialog.connect("closed", _forget_dialog, weakref.ref(ctx))
    if isinstance(dialog, SpotifyImportDialog):
        dialog._presented = True
    window = ctx.window if isinstance(ctx.window, Gtk.Widget) else None
    dialog.present(window)


def _forget_dialog(dialog: Adw.Dialog, ctx_ref) -> None:
    ctx = ctx_ref()
    if ctx is not None and getattr(ctx, "spotify_import_dialog", None) is dialog:
        ctx.spotify_import_dialog = None


def open_dialog(ctx, url: str = "", *, result: ImportedList | None = None, name: str = "") -> SpotifyImportDialog:
    """「Spotify から取り込む」の窓を出す (出した窓を返す)。"""
    dialog = SpotifyImportDialog(ctx, url=url, result=result, name=name)
    present_dialog(ctx, dialog)
    if result is not None and name:
        dialog.name_entry.grab_focus()
        dialog.name_entry.select_region(0, -1)
    elif not url:
        dialog.url_entry.grab_focus()
    return dialog


# ---------------------------------------------------------------------------
# 保存


def _send(call: Callable, *args) -> None:
    """catalog の書き換え (最後の引数が callback) を送る。送る前に落ちたら (曲に cliamp へ送れない文字が
    あるなど) 失敗の Response で callback を呼ぶ (取り込み中の印を残したまま黙って止まらない)。"""
    *head, callback = args
    try:
        call(*head, callback)
    except Exception as exc:
        log(f"cliamp に送れません: {exc!r}")
        callback(Response(ok=False, error=f"cliamp に送れません ({type(exc).__name__})", kind="error"))


def _dismiss(toast) -> None:
    """ctx.toast が返したトースト (Adw.Toast。窓が無ければ None) を消す。"""
    dismiss = getattr(toast, "dismiss", None)
    if callable(dismiss):
        dismiss()


def _home_path(path: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "/" and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def start_import(ctx, name: str, result: ImportedList, *, url: str = "") -> None:
    """取り込むと決めた: 一覧を取り直し、名前が重なれば尋ね、重ならなければ保存する。"""
    name = name.strip()

    def listed(names) -> None:
        existing = names if names is not None else ctx.local_playlists
        if name in existing:
            ask_collision(ctx, name, result, url=url, existing=list(existing))
        else:
            save_import(ctx, name, result)

    ctx.refresh_local_playlists(force=True, then=listed)


def ask_collision(ctx, name: str, result: ImportedList, *, url: str = "", existing: list[str] | None = None) -> Adw.AlertDialog:
    """同じ名前のローカルのプレイリストがあるとき: 置き換える / 別の名前にする / キャンセル。"""
    dialog = Adw.AlertDialog(heading=f"「{name}」はもうあります",
                             body=f"置き換えると、いまの「{name}」の曲は、Spotify から取り込んだ {result.count} 曲に"
                                  "替わります。")
    dialog.add_css_class("music-import-collision")
    dialog.add_response("cancel", "キャンセル")
    dialog.add_response("rename", "別の名前にする")
    dialog.add_response("replace", "置き換える")
    dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("rename")
    dialog.set_close_response("cancel")
    taken = existing if existing is not None else ctx.local_playlists

    def on_response(_dialog, response: str) -> None:
        if response == "replace":
            save_import(ctx, name, result, replace=True)
        elif response == "rename":
            open_dialog(ctx, url or result.url, result=result, name=suggest_name(name, taken))

    dialog.connect("response", on_response)
    present_dialog(ctx, dialog)
    return dialog


def replace_local(ctx, name: str, tracks: list[Track],
                  done: Callable[[Response, list[Track], str | None], object]) -> None:
    """ローカルのプレイリスト name の曲を tracks に置き換える (cliamp に置き換えは無いので、今の曲を
    読んで控えてから消して足す)。足せなかったら読んでおいた曲を戻し、戻し終えてから done を呼ぶ。

    done(足したときの Response, 戻せなかった前の曲 (戻せた・前が無ければ空), その控えの path か None)。
    控え (ImportIndex.backup) は、置き換えられた・戻せたら消し、戻せなかったときだけ残す。"""
    catalog = ctx.catalog
    imports = ctx.imports

    def read(old) -> None:
        if isinstance(old, Response):
            if MISSING_TEXT not in old.error:
                done(old, [], None)
                return
            old = []  # 無い (TUI で消した)。そのまま作る
        old = list(old)
        # 消した後で足すことも戻すこともできなかったとき (cliamp が止まった・ディスクが一杯) に曲を失わない
        backup = imports.backup(name, old) if old else None

        def deleted(response: Response) -> None:
            if not response.ok and MISSING_TEXT not in response.error:
                imports.drop_backup(backup)
                done(response, [], None)
                return

            def added(response: Response) -> None:
                if response.ok or not old:
                    imports.drop_backup(backup)
                    done(response, [], None)
                    return
                log(f"「{name}」に取り込めなかったので、前の {len(old)} 曲を戻します: {response.error}")

                def restored(back: Response) -> None:
                    if back.ok:
                        imports.drop_backup(backup)
                        done(response, [], None)
                    else:
                        log(f"「{name}」の前の {len(old)} 曲も戻せません: {back.error}")
                        done(response, old, backup)

                _send(catalog.playlist_add, name, old, restored)

            _send(catalog.playlist_add, name, tracks, added)

        _send(catalog.playlist_delete, name, deleted)

    catalog.tracks("local", name, read, force=True)


def ask_restore(ctx, name: str, old: list[Track], backup: str | None, reason: Response) -> Adw.AlertDialog:
    """置き換えに失敗し、前の曲も戻せなかった: 控えの場所を言い、「もう一度戻す」を出す (トーストでは
    見落とすので窓で)。"""
    body = f"曲を足せず ({reason.message})、前の {len(old)} 曲も戻せませんでした。"
    if backup:
        body += f"\n曲の一覧は {_home_path(backup)} に控えてあります。"
    body += "\ncliamp に繋がっていれば「もう一度戻す」で戻せます。"
    dialog = Adw.AlertDialog(heading=f"「{name}」の曲を戻せませんでした", body=body)
    dialog.add_css_class("music-import-restore")
    dialog.add_response("close", "閉じる")
    dialog.add_response("restore", "もう一度戻す")
    dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("restore")
    dialog.set_close_response("close")

    def restored(response: Response) -> None:
        if response.ok:
            ctx.imports.drop_backup(backup)
            ctx.toast(f"「{name}」の前の {len(old)} 曲を戻しました")
        else:
            where = f" (曲の一覧は {_home_path(backup)} にあります)" if backup else ""
            ctx.toast(f"戻せませんでした: {response.message}{where}")
        ctx.refresh_local_playlists(force=True)

    def on_response(_dialog, response: str) -> None:
        if response == "restore":
            _send(ctx.catalog.playlist_add, name, old, restored)

    dialog.connect("response", on_response)
    present_dialog(ctx, dialog)
    return dialog


def save_import(ctx, name: str, result: ImportedList, *, replace: bool = False, navigate: bool = True,
                verb: str = "取り込み", callback: Callable[[bool], object] | None = None) -> None:
    """result の曲をローカルのプレイリスト name に保存し、取り込んだことを表に書く。

    replace なら今の曲を置き換える (「置き換える」「Spotify から更新」)。できたらトースト
    「「名前」を取り込みました (N 曲)」(verb で「更新しました」など) と、切れていればその知らせを出し、
    navigate なら詳細へ移る。callback(できたか)。途中で思わぬ誤りが出ても、取り込み中の印は外す。"""
    name = name.strip()
    busy: set[str] = ctx.spotify_imports_busy
    if name in busy:
        ctx.toast(f"「{name}」はいま取り込んでいます")
        if callback is not None:
            callback(False)
        return
    busy.add(name)
    tracks = list(result.tracks)
    before = list(ctx.local_playlists)
    failed = "更新できませんでした" if verb == "更新" else "取り込めませんでした"

    def finished(ok: bool) -> None:
        busy.discard(name)
        if callback is not None:
            callback(ok)

    def saved(response: Response, lost: list[Track] | None = None, backup: str | None = None) -> None:
        if not response.ok:
            if lost:
                ask_restore(ctx, name, list(lost), backup, response)
            else:
                ctx.toast(f"{failed}: {response.message}")
            finished(False)
            # 戻した (戻せなかった) 後の一覧 (replace_local は戻し終えてから呼ぶ)
            ctx.refresh_local_playlists(force=True)
            return
        try:
            ctx.imports.record(name, result)
        except Exception as exc:  # 記録は飾り。曲は保存できている
            log(f"取り込みの記録を書けません: {exc!r}")
        done_text = "更新しました" if verb == "更新" else "取り込みました"
        ctx.toast(f"「{name}」を{done_text} ({result.count} 曲)")
        note = truncated_note(result)
        if note:
            ctx.toast(note)

        def listed(names) -> None:
            if names is None or names == before:
                ctx.emit("local-playlists-changed")  # 名前は同じで中身が変わった
            finished(True)
            if navigate:
                ctx.navigate("playlist", provider="local", id=name, name=name)

        ctx.refresh_local_playlists(force=True, then=listed)

    try:
        if replace:
            replace_local(ctx, name, tracks, saved)
        else:
            _send(ctx.catalog.playlist_add, name, tracks, saved)
    except Exception as exc:
        import traceback

        log(f"「{name}」を保存できません: {exc!r}")
        traceback.print_exc()
        ctx.toast(f"{failed}: アプリの誤りです (ログを見てください)")
        finished(False)


def confirm_update(ctx, name: str, current: list[Track], result: ImportedList,
                   answer: Callable[[bool], object]) -> Adw.AlertDialog:
    """「Spotify から更新」で置き換える前に確かめる: 「「名前」の N 曲を、Spotify の M 曲に置き換えます」。
    取り込んでいない曲 (自分で足した曲) が混じっていれば、それが外れることも言う。answer(置き換えるか)。"""
    count = len(current)
    others = count - ctx.imports.known_count(current)
    body = f"「{name}」の {count} 曲を、Spotify の{result.kind_label}「{result.name}」の {result.count} 曲に置き換えます。"
    if others > 0:
        body += f"\nこのうち {others} 曲は Spotify から取り込んだ曲ではないため、プレイリストから外れます。"
    if result.truncated:
        body += f"\nSpotify の公開ページは {EMBED_TRACK_CAP} 曲までのため、最初の {result.count} 曲だけ取り込みます。"
    dialog = Adw.AlertDialog(heading="Spotify から更新しますか？", body=body)
    dialog.add_css_class("music-import-update")
    dialog.add_response("cancel", "キャンセル")
    dialog.add_response("update", "更新")
    if others > 0:
        dialog.set_response_appearance("update", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
    else:
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("update")
    dialog.set_close_response("cancel")
    answered = []

    def reply(ok: bool) -> None:
        if not answered:
            answered.append(ok)
            answer(ok)

    dialog.connect("response", lambda _d, response: reply(response == "update"))
    dialog.connect("closed", lambda _d: reply(False))  # 応答なしで閉じた (force_close など)
    present_dialog(ctx, dialog)
    return dialog


def update_import(ctx, name: str, callback: Callable[[bool], object] | None = None) -> bool:
    """「Spotify から更新」: 取り込んだときのリンクから読み直し、今の曲と比べて確かめてから置き換える
    (名前はそのまま)。始めたら True (記録が無い・取り込み中なら False)。callback(置き換えたか)。

    今の曲が取り込んだもののままでなければ (TUI で消して同じ名前で作り直したなど) 置き換えずに記録を
    忘れる。Spotify と同じ曲なら置き換えずに「もう最新です」。読み込み中のトーストは結果の前に消す
    (出したままだと、後のトーストが 4 秒待たされる)。"""
    record = ctx.imports.get(name)
    if record is None:
        ctx.toast(f"「{name}」は Spotify から取り込んだプレイリストではありません")
        return False
    busy: set[str] = ctx.spotify_imports_busy
    if name in busy:
        ctx.toast(f"「{name}」はいま取り込んでいます")
        return False
    busy.add(name)
    loading = ctx.toast(f"「{name}」を Spotify から読み込んでいます…")
    if callable(getattr(loading, "set_timeout", None)):
        loading.set_timeout(0)  # 読み終えるまで出しておく (消すのはこちら)
    got: dict[str, object] = {}

    def finish(ok: bool, text: str = "") -> None:
        _dismiss(loading)
        busy.discard(name)
        if text:
            ctx.toast(text)
        if callback is not None:
            callback(ok)

    def both() -> None:
        if "fetched" not in got or "current" not in got:
            return
        value, current = got["fetched"], got["current"]
        if isinstance(value, SpotifyImportError):
            finish(False, f"更新できませんでした: {value.message}")
            return
        if isinstance(current, Response):
            if MISSING_TEXT in current.error:
                ctx.forget_import(name)
                finish(False, f"「{name}」が見つかりません (削除されたか、名前が変わりました)")
                ctx.refresh_local_playlists(force=True)
            else:
                finish(False, f"更新できませんでした: {current.message}")
            return
        current = list(current)
        if current and ctx.imported_record(name, current) is None:
            # 記録だけ残って中身が別物。置き換えない (記録は imported_record が忘れた)
            finish(False, f"「{name}」はもう Spotify から取り込んだプレイリストではないため、更新しません")
            ctx.emit("local-playlists-changed")  # 取り込んだものとしての見せ方を外す
            return
        if [t.path for t in current] == [t.path for t in value.tracks]:
            try:
                ctx.imports.record(name, value)  # 名前・絵・曲数の記録を新しく
            except Exception as exc:
                log(f"取り込みの記録を書けません: {exc!r}")
            finish(True, f"「{name}」はもう最新です ({value.count} 曲)")
            return
        _dismiss(loading)

        def answered(ok: bool) -> None:
            if not ok:
                finish(False)
                return
            busy.discard(name)  # save_import が改めて印を付ける
            save_import(ctx, name, value, replace=True, navigate=False, verb="更新", callback=callback)

        confirm_update(ctx, name, current, value, answered)

    def fetched(value) -> None:
        got["fetched"] = value
        both()

    def read(value) -> None:
        got["current"] = value
        both()

    try:
        fetch_async(record.url, fetched)
        ctx.catalog.tracks("local", name, read, force=True)
    except Exception as exc:
        log(f"「{name}」を更新できません: {exc!r}")
        finish(False, "更新できませんでした: アプリの誤りです (ログを見てください)")
    return True
