from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango

from .protocol import format_time
from .track_match import search_candidates


def target_id(track, provider):
    if provider == "spotify":
        return track.spotify_id
    if provider == "youtube":
        return track.youtube_id
    return None


def _candidate_factory() -> Gtk.SignalListItemFactory:
    factory = Gtk.SignalListItemFactory()

    def _setup(_f, item):
        label = Gtk.Label(xalign=0)
        label.set_single_line_mode(True)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(27)
        label.set_hexpand(True)
        item.set_child(label)

    def _bind(_f, item):
        label = item.get_child()
        string_obj = item.get_item()
        text = string_obj.get_string()
        label.set_text(text)
        label.set_tooltip_text(text)

    factory.connect("setup", _setup)
    factory.connect("bind", _bind)
    return factory


class RecordingReview(Gtk.Box):
    def __init__(self, ctx, tracks, provider, on_change):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.ctx = ctx
        self.tracks = tuple(tracks)
        self.provider = provider
        self.on_change = on_change
        self.confirmed = None
        self.active = False
        self.generation = 0
        self.index = 0
        self.decisions = {}
        self.current_candidates = []
        self.current_track = None
        self._build_ui()

    def _build_ui(self):
        self.status = Gtk.Label(label="", wrap=True, xalign=0.0)
        self.status.set_hexpand(True)
        self.append(self.status)

        self.source = Gtk.Label(label="", wrap=True, xalign=0.0)
        self.source.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.source.set_ellipsize(Pango.EllipsizeMode.END)
        self.source.set_lines(3)
        self.source.set_max_width_chars(32)
        self.source.set_hexpand(True)
        self.append(self.source)

        self.dropdown = Gtk.DropDown()
        self.dropdown.set_factory(_candidate_factory())
        self.dropdown.set_list_factory(_candidate_factory())
        self.dropdown.set_hexpand(True)
        self.dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["保存先の録音候補"])
        self.append(self.dropdown)

        self.details = Gtk.Label()
        self.details.set_wrap(True)
        self.details.set_xalign(0)
        self.details.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.details.set_max_width_chars(32)
        self.details.set_ellipsize(Pango.EllipsizeMode.END)
        self.details.set_lines(3)
        self.details.set_visible(False)
        self.append(self.details)

        self.link = Gtk.LinkButton(uri="", label="Spotify で開く" if self.provider == "spotify" else "YouTube Music で開く")
        self.link.set_visible(False)
        self.append(self.link)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.accept = Gtk.Button(label="この録音を選ぶ")
        self.accept.set_sensitive(False)
        self.skip = Gtk.Button(label="スキップ")
        self.retry = Gtk.Button(label="再検索")
        self.retry.set_visible(False)
        row.append(self.accept)
        row.append(self.skip)
        row.append(self.retry)
        self.append(row)

        self.accept.connect("clicked", self._on_accept)
        self.skip.connect("clicked", self._on_skip)
        self.retry.connect("clicked", self._on_retry)
        self.dropdown.connect("notify::selected", self._on_selected)

    def start(self):
        self.active = True
        self.generation += 1
        self.index = 0
        self.decisions = {}
        self._advance()

    def cancel(self):
        self.active = False
        self.generation += 1

    def _advance(self):
        if not self.active:
            return
        while self.index < len(self.tracks):
            track = self.tracks[self.index]
            tid = target_id(track, self.provider)
            if tid:
                self.decisions[self.index] = track
                self.index += 1
                continue
            self._show_current(track)
            return
        self._finish()

    def _show_current(self, track):
        self.current_track = track
        self.current_candidates = []
        self.retry.set_visible(False)
        self.accept.set_sensitive(False)
        self.link.set_visible(False)
        self._set_dropdown([])
        self._update_source()
        self._search()

    def _search(self):
        if not self.active or self.current_track is None:
            return
        self.generation += 1
        gen = self.generation
        index = self.index
        track = self.current_track
        self.status.set_text("検索中…")
        self.accept.set_sensitive(False)
        self.retry.set_visible(False)

        def cb(candidates, error):
            if not self.active or gen != self.generation or self.index != index or self.current_track is not track:
                return
            self._on_search_done(candidates or [], error)

        search_candidates(self.ctx, self.current_track, self.provider, cb)

    def _on_search_done(self, candidates, error):
        self.current_candidates = [] if error else [c for c in candidates if target_id(c, self.provider)]
        self._set_dropdown(self.current_candidates)
        if error:
            self.status.set_text(error)
            self.retry.set_visible(True)
            self.accept.set_sensitive(False)
            return
        if not self.current_candidates:
            self.status.set_text("候補が見つかりませんでした。")
            self.retry.set_visible(True)
            self.accept.set_sensitive(False)
            return
        self.status.set_text("候補から選択してください。")
        self.accept.set_sensitive(False)

    def _set_dropdown(self, candidates):
        self.current_candidates = candidates
        items = ["録音を選んでください"] + [self._candidate_text(c) for c in candidates]
        self.dropdown.set_model(Gtk.StringList.new(items))
        self.dropdown.set_selected(0)
        self._on_selected(self.dropdown, None)

    @staticmethod
    def _candidate_text(track):
        fields = (track.display_title, track.artist, track.album,
                  format_time(track.duration) if track.duration > 0 else "")
        return " · ".join(f for f in fields if f)

    def _update_source(self):
        text = f"元の曲 {self.index + 1}/{len(self.tracks)}\n{self._candidate_text(self.current_track)}"
        self.source.set_text(text)
        self.source.set_tooltip_text(text)

    def _selected_candidate(self):
        idx = self.dropdown.get_selected()
        if idx == 0 or idx < 0 or idx > len(self.current_candidates):
            return None
        return self.current_candidates[idx - 1]

    def _on_selected(self, dropdown, param):
        track = self._selected_candidate()
        if track:
            pieces = (
                format_time(track.duration) if track.duration > 0 else "",
                track.display_title,
                track.artist,
                track.album,
            )
            text = " · ".join(p for p in pieces if p)
            self.details.set_text("保存先の候補　" + text)
            self.details.set_tooltip_text(text)
            self.details.set_visible(True)
        else:
            self.details.set_visible(False)
        self.accept.set_sensitive(self.active and track is not None)
        if track is None:
            self.link.set_uri("")
            self.link.set_visible(False)
        else:
            self.link.set_uri(track.web_url)
            self.link.set_visible(True)

    def _on_accept(self, button):
        if not self.active:
            return
        track = self._selected_candidate()
        if track is None:
            return
        self.decisions[self.index] = track
        self.index += 1
        self._advance()

    def _on_skip(self, button):
        if not self.active:
            return
        self.decisions[self.index] = None
        self.index += 1
        self._advance()

    def _on_retry(self, button):
        if not self.active:
            return
        self._search()

    def _finish(self):
        if not self.active:
            return
        result = []
        for i, track in enumerate(self.tracks):
            chosen = self.decisions.get(i, track)
            if chosen is not None:
                result.append(chosen)
        self.confirmed = tuple(result)
        skipped = len(self.tracks) - len(self.confirmed)
        if skipped == len(self.tracks):
            self.status.set_text(f"追加する曲はありません（{skipped} 曲スキップ）")
        else:
            self.status.set_text(f"追加する曲 {len(self.confirmed)} 曲 ・ スキップ {skipped} 曲")
        self.source.set_text("")
        self.details.set_visible(False)
        self.dropdown.set_visible(False)
        self.accept.set_visible(False)
        self.skip.set_visible(False)
        self.retry.set_visible(False)
        self.link.set_visible(False)
        self.active = False
        if self.on_change:
            self.on_change()
