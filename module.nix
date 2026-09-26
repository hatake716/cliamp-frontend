# NixOS モジュール: programs.cliamp-music。
#
# アプリ (cliamp-music) を入れ、既定では pkgs.cliamp を GUI 用の IPC 拡張を
# 当てたもの (cliamp.nix) に overlay で差し替える。差し替えは pkgs.cliamp を
# 使うところすべて (cliamp を常駐させるサービス、端末の cliamp、Open-Voice が
# 呼ぶ cliamp) に及ぶ。拡張は既存のコマンドと `cliamp status` の出力を変えない。
#
# cliamp を常駐させるサービスそのものは、このモジュールでは定義しない
# (利用者の構成がすでに持っているため)。
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
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    nixpkgs.overlays = [ self.overlays.default ] ++ lib.optional cfg.patchCliamp self.overlays.cliamp;
    environment.systemPackages = [ pkgs.cliamp-music ];
  };
}
