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
{ lib, cliamp }:

lib.throwIfNot (cliamp.version == "1.50.0")
  "cliamp が ${cliamp.version} になった。patches/cliamp-1.50.0-gui-ipc.patch を新しい版に当て直し、cliamp.nix の版の確認を合わせること"
  (
    cliamp.overrideAttrs (old: {
      patches = (old.patches or [ ]) ++ [ ./patches/cliamp-1.50.0-gui-ipc.patch ];
    })
  )
