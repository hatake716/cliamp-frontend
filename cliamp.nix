# cliamp.nix — ミュージックが使う拡張 IPC (api 1) を当てた cliamp。
#
# nixpkgs の cliamp 1.50.0 に patches/cliamp-1.50.0-gui-ipc.patch を当てる。
# 足すのは IPC のコマンドだけで、TUI のキー操作・CLI・`cliamp status` の
# 平文の出力は変えない (Open-Voice がそれで cliamp を操作しているため)。
# 何をなぜ足すかはパッチの冒頭に書いてある。
#
# パッチは上流のソースの行に結びついているので、nixpkgs の cliamp の版が
# 変わったら評価の時点で止める (当たらないまま気づかずに進めないため)。
# vendorHash は上流のまま使える (go.mod を変えず、vendor に無いパッケージを
# import しない)。ビルドの checkPhase で上流の試験とパッチの試験
# (go test ./...) が走る。
#
# 出力先の一覧と切り替え (`cliamp device`、TUI の出力先の選択、GUI の出力先の
# メニュー) は player/audio_device_linux.go が pactl を呼ぶ。nixpkgs の cliamp は
# ffmpeg と yt-dlp しか PATH に足さず、NixOS + PipeWire では pactl が入らない
# (libpulseaudio には bin/ が無い) ので、pulseaudio の pactl を後ろに足す
# (利用者が自分で入れた pactl があればそちらが先)。
{
  lib,
  stdenv,
  cliamp,
  pulseaudio,
}:

lib.throwIfNot (cliamp.version == "1.50.0")
  "cliamp が ${cliamp.version} になった。patches/cliamp-1.50.0-gui-ipc.patch を新しい版に当て直し、cliamp.nix の版の確認を合わせること。急ぐときは programs.cliamp-music.patchCliamp = false で差し替えを外せる (アプリは再生の操作だけになる)"
  (
    cliamp.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [ ./patches/cliamp-1.50.0-gui-ipc.patch ];
      postInstall =
        (old.postInstall or "")
        + lib.optionalString stdenv.hostPlatform.isLinux ''
          wrapProgram $out/bin/cliamp --suffix PATH : ${lib.makeBinPath [ pulseaudio ]}
        '';
    })
  )
