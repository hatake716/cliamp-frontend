import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from ..protocol import PlaylistInfo
from . import Bindings
from .home import weak_call


class PlaylistSaveButton(Gtk.MenuButton):
    def __init__(self, ctx, info: PlaylistInfo):
        super().__init__()

        self._ctx = ctx
        self._info = info

        self.set_css_classes(["music-circle-menu"])
        self.set_icon_name("music-plus-symbolic")
        self.set_has_frame(False)
        self.set_always_show_arrow(False)

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        self.local_button = Gtk.Button()
        self.local_button.set_css_classes(["flat"])
        self.local_button.connect("clicked", weak_call(self._on_local_clicked))

        self.account_button = Gtk.Button()
        self.account_button.set_css_classes(["flat"])
        self.account_button.connect("clicked", weak_call(self._on_account_clicked))

        box.append(self.local_button)
        box.append(self.account_button)
        popover.set_child(box)
        self.set_popover(popover)

        self._bindings = Bindings(self, on_rebind=self._sync)
        self._bindings.add(ctx, "saved-playlists-changed", self._sync)
        self._sync()

    def _sync(self, *_args):
        ctx = self._ctx
        info = self._info
        provider = info.provider
        playlist_id = info.id
        name = info.name

        is_saved = ctx.is_playlist_saved(provider, playlist_id)

        if is_saved:
            self.local_button.set_label("このPCの保存を解除")
            self.set_icon_name("object-select-symbolic")
            tooltip = f"「{name}」はこのPCに保存済み。保存先を選ぶ"
        else:
            self.local_button.set_label("このPCに保存")
            self.set_icon_name("music-plus-symbolic")
            tooltip = f"「{name}」の保存先を選ぶ"

        service = "Spotify" if provider == "spotify" else "YouTube Music"
        self.account_button.set_label(f"{service} にも保存" if is_saved else f"このPCと {service} に保存")

        self.set_tooltip_text(tooltip)
        self.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])

    def _on_local_clicked(self):
        self.popdown()
        ctx = self._ctx
        info = self._info
        ctx.set_playlist_saved(info, not ctx.is_playlist_saved(info.provider, info.id))

    def _on_account_clicked(self):
        self.popdown()
        self._ctx.save_playlist_to_account(self._info)


def playlist_card_with_save(ctx, card, info: PlaylistInfo) -> Gtk.Overlay:
    overlay = Gtk.Overlay()
    overlay.playlist_info = info
    overlay.card = card
    overlay.art = card.art

    save_button = PlaylistSaveButton(ctx, info)
    overlay.save_button = save_button
    save_button.add_css_class("playlist-card-save")

    overlay.set_child(card)
    overlay.add_overlay(save_button)

    save_button.set_halign(Gtk.Align.END)
    save_button.set_valign(Gtk.Align.START)
    save_button.set_margin_top(8)
    save_button.set_margin_end(8)

    overlay.set_halign(Gtk.Align.START)

    return overlay
