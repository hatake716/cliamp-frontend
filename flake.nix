{
  description = "ミュージック — cliamp を macOS 27 のミュージック風に操作する GTK フロントエンド";

  # 利用側 (/etc/nixos) は inputs.nixpkgs.follows で自分の nixpkgs を渡す。
  # 単体で使うときの既定は同じ nixos-26.05 ブランチ。
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system:
        f nixpkgs.legacyPackages.${system});
      systemOf = pkgs: pkgs.stdenv.hostPlatform.system;

      # 画面を使う試験を Xvfb の中で回す。利用者の画面には繋がない。
      uiTests = pkgs:
        let app = self.packages.${systemOf pkgs}.cliamp-music;
        in pkgs.runCommand "cliamp-music-ui-tests"
          {
            nativeBuildInputs = [
              app.pythonEnv
              pkgs.xvfb-run
              pkgs.dbus
              # buildInputs の typelib を GI_TYPELIB_PATH へ入れるための setup hook
              pkgs.gobject-introspection
            ];
            buildInputs = [
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.glib
              pkgs.gdk-pixbuf
              pkgs.librsvg
              pkgs.pango
              pkgs.graphene
              pkgs.harfbuzz
            ];
            FONTCONFIG_FILE = pkgs.makeFontsConf { fontDirectories = [ pkgs.dejavu_fonts ]; };
          } ''
          cp -r ${self}/cliamp_music ${self}/tests .
          chmod -R u+w .
          export HOME="$TMPDIR/home" XDG_CACHE_HOME="$TMPDIR/cache" XDG_STATE_HOME="$TMPDIR/state"
          export XDG_RUNTIME_DIR="$TMPDIR/run"
          mkdir -p "$HOME" "$XDG_RUNTIME_DIR"
          chmod 700 "$XDG_RUNTIME_DIR"
          export PYTHONDONTWRITEBYTECODE=1
          export XDG_DATA_DIRS="${pkgs.gsettings-desktop-schemas}/share/gsettings-schemas/${pkgs.gsettings-desktop-schemas.name}:${pkgs.gtk4}/share/gsettings-schemas/${pkgs.gtk4.name}:${pkgs.adwaita-icon-theme}/share:${pkgs.hicolor-icon-theme}/share"
          export GDK_PIXBUF_MODULE_FILE="${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
          export GDK_BACKEND=x11 GSK_RENDERER=cairo GDK_DEBUG=no-portals
          export ADW_DISABLE_PORTAL=1 GTK_A11Y=none GIO_USE_VFS=local
          unset WAYLAND_DISPLAY
          xvfb-run -a -s "-screen 0 1600x1200x24" \
            dbus-run-session --config-file=${pkgs.dbus}/share/dbus-1/session.conf -- \
            python3 -m unittest discover -s tests -v
          touch $out
        '';
    in
    {
      overlays = {
        # アプリだけを足す。
        default = final: _prev: {
          cliamp-music = final.callPackage ./package.nix { };
        };
        # pkgs.cliamp に GUI 用の IPC 拡張 (patches/) を当てる。
        cliamp = _final: prev: {
          cliamp = import ./cliamp.nix { inherit (prev) lib cliamp; };
        };
      };

      packages = forAllSystems (pkgs: rec {
        cliamp-music = pkgs.callPackage ./package.nix { };
        cliamp = import ./cliamp.nix { inherit (pkgs) lib cliamp; };
        default = cliamp-music;
      });

      nixosModules.default = import ./module.nix self;

      checks = forAllSystems (pkgs: {
        inherit (self.packages.${systemOf pkgs}) cliamp cliamp-music;
        ui = uiTests pkgs;
      });

      # 開発用。`nix develop path:.` で GTK/libadwaita の typelib と Python、
      # 試験用の Xvfb、cliamp を組み立てる Go の道具が揃う。
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          nativeBuildInputs = [
            pkgs.gobject-introspection
            pkgs.xvfb-run
            pkgs.xorg-server
            pkgs.dbus
            pkgs.go
            pkgs.pkg-config
            pkgs.desktop-file-utils
            pkgs.librsvg
            (pkgs.python3.withPackages (ps: [ ps.pygobject3 ps.mutagen ]))
          ];
          buildInputs = [
            pkgs.gtk4
            pkgs.libadwaita
            pkgs.glib
            pkgs.librsvg
            pkgs.gdk-pixbuf
            pkgs.gsettings-desktop-schemas
            pkgs.adwaita-icon-theme
            pkgs.hicolor-icon-theme
            # cliamp (cgo) の組み立てに要るもの
            pkgs.alsa-lib
            pkgs.flac
            pkgs.libogg
            pkgs.libvorbis
          ];
          shellHook = ''
            export XDG_DATA_DIRS="${pkgs.gsettings-desktop-schemas}/share/gsettings-schemas/${pkgs.gsettings-desktop-schemas.name}:${pkgs.gtk4}/share/gsettings-schemas/${pkgs.gtk4.name}:${pkgs.adwaita-icon-theme}/share:${pkgs.hicolor-icon-theme}/share''${XDG_DATA_DIRS:+:$XDG_DATA_DIRS}"
            export GDK_PIXBUF_MODULE_FILE="${pkgs.librsvg}/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache"
          '';
        };
      });
    };
}
