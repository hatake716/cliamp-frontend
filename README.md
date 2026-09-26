# ミュージック (cliamp-music)

[cliamp](https://github.com/bjarneo/cliamp) 1.50.0 (端末の音楽プレーヤー) を、
macOS 27 の「ミュージック」風に操作する GTK4 + libadwaita のアプリ
(アプリ ID `org.nixos.Music`、実行ファイル `cliamp-music`)。

再生そのものは常駐している cliamp が行い、このアプリは IPC
(`~/.config/cliamp/cliamp.sock`) だけを通して操作する。ライブラリ・検索・次に再生・
歌詞などを使うには、cliamp 側にも IPC の拡張 (`patches/cliamp-1.50.0-gui-ipc.patch`、
仕様は [PROTOCOL.md](PROTOCOL.md)) を当てる。当てていない cliamp にも繋がり、そのときは
再生の操作だけが使える。画面の設計は [DESIGN.md](DESIGN.md)。

## NixOS で使う

flake の入力に足し、モジュールを読み込む:

```nix
{
  inputs.cliamp-music = {
    url = "git+file:///home/takeshi/git/cliamp-frontend";  # 取り出している枝をそのまま使う
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { nixpkgs, cliamp-music, ... }: {
    nixosConfigurations.nixos = nixpkgs.lib.nixosSystem {
      modules = [
        cliamp-music.nixosModules.default
        {
          programs.cliamp-music.enable = true;
          # programs.cliamp-music.patchCliamp = true;     # 既定。pkgs.cliamp に拡張を当てる
          # programs.cliamp-music.hideGnomeMusic = true;  # GNOME の「ミュージック」を外す (下)
        }
      ];
    };
  };
}
```

- `programs.cliamp-music.enable`: アプリを入れる (`environment.systemPackages`)。
- `patchCliamp` (既定 `true`): overlay で `pkgs.cliamp` を拡張を当てたもの (`cliamp.nix`)
  に差し替える。差し替えは `pkgs.cliamp` を使うところすべて (cliamp を常駐させる
  サービス、端末の cliamp、Open-Voice が呼ぶ cliamp) に及ぶ。拡張は既存のコマンドと
  `cliamp status` の出力を変えない。
- `hideGnomeMusic` (既定 `false`): `environment.gnome.excludePackages` に
  `pkgs.gnome-music` を足す。

cliamp を常駐させるユーザーのサービス (`cliamp.service`) は、このモジュールでは定義しない
(利用者の構成が持っている前提)。未接続の画面の「cliamp を起動」ボタンは
`systemctl --user start cliamp.service` を走らせる。別のやり方で起こすなら、環境変数
`CLIAMP_MUSIC_START_COMMAND` に起こすコマンドを入れる。

### 有効にした最初の switch で cliamp が起こし直される

`patchCliamp` (既定で有効) は `pkgs.cliamp` を替えるので、それを使う `cliamp.service` の
ユニットも変わる。NixOS の switch は変わったユーザーのユニットを止めて起こし直すため、
**有効にした最初の `nixos-rebuild switch` (と、パッチを直すたび) に cliamp が止まり、
再生が 1 度止まる**。避けるには次のどれか:

- 何も鳴らしていないときに switch する。
- `nixos-rebuild boot` にして、次に起動したときに替える。
- ホストの構成で `systemd.user.services.cliamp.restartIfChanged = false;` にし、都合の
  よいときに `systemctl --user restart cliamp` する。

起こし直すまでは古い (拡張の無い) cliamp が動いているので、アプリは上端に
「この cliamp は拡張 IPC に対応していません」の帯を出し、再生の操作だけになる。

nixpkgs の cliamp の版が 1.50.0 から変わると、`cliamp.nix` が評価の時点で止まる
(パッチを当て直すまで)。急ぐときは `programs.cliamp-music.patchCliamp = false;` で
差し替えを外せる (アプリは再生の操作だけになる)。

### 出力先の切り替えと pactl

cliamp の出力先の一覧と切り替え (アプリの出力先のメニュー、TUI の出力先の選択、
`cliamp device`) は `pactl` を呼ぶ。NixOS + PipeWire では pactl が入らないので、
`cliamp.nix` は差し替えた cliamp の PATH の後ろに pulseaudio の pactl を足す。
`patchCliamp = false` のときは差し替えないので、`cliamp.service` の PATH に pactl
(`pkgs.pulseaudio`) を足さないと、出力先のメニューは「pactl が見つかりません」になる。

### GNOME の「ミュージック」と重なる

GNOME の「ミュージック」(gnome-music) も日本語では「ミュージック」と表示され、WhiteSur
などのテーマではアイコンもほぼ同じ赤い四角の音符になる。アプリの一覧や検索で見分けられ
ないので、使わないなら `programs.cliamp-music.hideGnomeMusic = true;` (または
`environment.gnome.excludePackages = [ pkgs.gnome-music ];`) で外す。

## 開発

```sh
nix develop path:.            # Python 3.13 + PyGObject + mutagen、GTK 4 / libadwaita、Xvfb、Go

# 単体試験 (画面を使う試験は私用の Xvfb で。利用者の画面・cliamp には繋がない)
nix develop path:. -c bash -c 'unset WAYLAND_DISPLAY; export GDK_BACKEND=x11 GSK_RENDERER=cairo \
  GDK_DEBUG=no-portals ADW_DISABLE_PORTAL=1 GTK_A11Y=none GIO_USE_VFS=local; \
  xvfb-run -n 97 -s "-screen 0 1600x1200x24" dbus-run-session -- python3 -m unittest discover -s tests'

# 窓を出さない自己診断 (モジュール・CSS・アイコン・gdk-pixbuf の読み込み口)
nix develop path:. -c env PYTHONPATH=. python3 -m cliamp_music --self-check

# 全画面の撮影 (偽の cliamp で。--keys は xdotool で本物のキーも確かめる)
nix shell nixpkgs#xdotool -c nix develop path:. -c \
  xvfb-run -n 97 -s "-screen 0 1600x1100x24" python3 scripts/shoot.py --keys

nix build path:.#cliamp-music   # アプリ (installCheck で画面の要らない試験と --self-check)
nix build path:.#checks.x86_64-linux.ui   # 画面を使う試験を Xvfb の中で
```

`tests/fake_cliamp.py` は PROTOCOL.md を実装した偽の cliamp (本物の振る舞いに合わせて
ある)。`tests/test_conformance.py` は同じ断言を偽と、環境変数
`CLIAMP_MUSIC_REAL_SOCKET` で指した本物のパッチ済み cliamp の両方に当てる (本物は
一時的な HOME で、音は ALSA の null などで鳴らさずに動かすこと)。

## ライセンス

MIT ([LICENSE](LICENSE))。`patches/` のパッチは cliamp (Copyright (c) Bjarne Øverli、
MIT、[patches/LICENSE.cliamp](patches/LICENSE.cliamp)) を変更するもので、差分の文脈として
cliamp のソースの一部を含む。
