# NixOS モジュール: programs.cliamp-music。
#
# アプリ (cliamp-music) を入れ、既定では pkgs.cliamp を GUI 用の IPC 拡張を
# 当てたもの (cliamp.nix) に overlay で差し替える。差し替えは pkgs.cliamp を
# 使うところすべて (cliamp を常駐させるサービス、端末の cliamp、Open-Voice が
# 呼ぶ cliamp) に及ぶ。拡張は既存のコマンドと `cliamp status` の出力を変えない。
#
# cliamp を常駐させるサービスそのものは、このモジュールでは定義しない
# (利用者の構成がすでに持っているため)。アプリの「cliamp を起動」ボタンは
# `systemctl --user start cliamp.service` を走らせる (CLIAMP_MUSIC_START_COMMAND で替えられる)。
#
# 注意: patchCliamp (既定で有効) は pkgs.cliamp を替えるので、それを使う cliamp.service の
# ユニットも変わり、有効にした最初の `nixos-rebuild switch` (とパッチを直すたび) に
# cliamp が止まって起こし直される (再生が 1 度止まる)。止めたくなければ、何も鳴らして
# いないときに switch するか、`nixos-rebuild boot` にするか、ホストの構成で
# systemd.user.services.cliamp.restartIfChanged = false にして後で自分で
# `systemctl --user restart cliamp` する。起こし直すまでアプリは拡張なし (再生の操作だけ)。
# patchCliamp = false のときは、cliamp.service の PATH に pactl が無いと出力先の一覧が
# 取れない (cliamp.nix が pactl を足すのは差し替えた cliamp だけ)。詳しくは README.md。
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.cliamp-music;
in
{
  options.programs.cliamp-music = {
    enable = lib.mkEnableOption "ミュージック (cliamp を macOS 27 のミュージック風に操作する GTK のアプリ)";

    patchCliamp = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        pkgs.cliamp を GUI 用の IPC 拡張 (PROTOCOL.md) を当てたものに差し替える。
        切るとアプリは再生の操作だけになる (ライブラリ・検索・次に再生・歌詞は使えない)。
        有効にした最初の switch では cliamp.service が起こし直される (README.md)。
      '';
    };

    hideGnomeMusic = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        GNOME の「ミュージック」(gnome-music) を外す。名前 (日本語で「ミュージック」) と、
        WhiteSur などのテーマでのアイコンがこのアプリと重なり、アプリの一覧や検索で
        見分けられないため。
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    nixpkgs.overlays = [ self.overlays.default ] ++ lib.optional cfg.patchCliamp self.overlays.cliamp;
    environment.systemPackages = [ pkgs.cliamp-music ];
    environment.gnome.excludePackages = lib.mkIf cfg.hideGnomeMusic [ pkgs.gnome-music ];
  };
}
