# ミュージック — cliamp を macOS 27 の「ミュージック」風に操作する GTK のアプリ。
#
# 本体は cliamp_music/ (GTK4 + libadwaita + PyGObject)。Python の環境を
# makeWrapper で包み、`python -m cliamp_music` として起動する。再生そのものは
# 常駐している cliamp が行い、このアプリは IPC (~/.config/cliamp/cliamp.sock)
# だけを通して操作する。拡張したコマンド (PROTOCOL.md) を使うには、cliamp
# 側にも patches/cliamp-1.50.0-gui-ipc.patch を当てる (cliamp.nix)。当てて
# いない cliamp にも繋がり、そのときは再生の操作だけが使える。
{
  lib,
  stdenvNoCC,
  makeWrapper,
  wrapGAppsHook4,
  gobject-introspection,
  desktop-file-utils,
  python3,
  gtk4,
  libadwaita,
  glib,
  gdk-pixbuf,
  librsvg,
  adwaita-icon-theme,
  hicolor-icon-theme,
}:

let
  # mutagen は手元の音声ファイルに埋め込まれたアートワークを読むのに使う。
  # 無くても動く (代わりの絵になる) が、入れておく。
  pythonEnv = python3.withPackages (pythonPackages: [
    pythonPackages.pygobject3
    pythonPackages.mutagen
  ]);
  appId = "org.nixos.Music";
  appDir = "share/${appId}";
in
stdenvNoCC.mkDerivation {
  pname = "cliamp-music";
  version = "0.1.0";

  # 作業ツリーの __pycache__ や撮影の出力を $out へ持ち込まない。
  src = lib.cleanSourceWith {
    src = ./.;
    filter =
      path: type:
      let
        base = baseNameOf path;
        rel = lib.removePrefix (toString ./. + "/") (toString path);
        top = builtins.head (lib.splitString "/" rel);
      in
      base != "__pycache__"
      && !(lib.hasSuffix ".pyc" base)
      && builtins.elem top [
        "cliamp_music"
        "data"
        "tests"
      ];
  };

  nativeBuildInputs = [
    makeWrapper
    wrapGAppsHook4
    # これが無いと gappsWrapperArgs に GI_TYPELIB_PATH が入らず、ランチャー
    # から (環境変数を継承しない状態で) 起動したときに
    # gi.require_version("Gtk", "4.0") で落ちる。
    gobject-introspection
    desktop-file-utils
  ];

  buildInputs = [
    adwaita-icon-theme
    gdk-pixbuf
    glib
    gtk4
    hicolor-icon-theme
    libadwaita
    # 自作の記号アイコン (SVG) を読む gdk-pixbuf の読み込み口。
    librsvg
  ];

  dontBuild = true;
  dontWrapGApps = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/${appDir}"
    cp -r cliamp_music "$out/${appDir}/"

    install -Dm644 data/${appId}.desktop \
      "$out/share/applications/${appId}.desktop"
    install -Dm644 cliamp_music/icons/hicolor/scalable/apps/${appId}.svg \
      "$out/share/icons/hicolor/scalable/apps/${appId}.svg"

    # 置いたはずのものが揃っているか。CSS やアイコンが欠けても窓は開いて
    # しまい、見た目だけが黙って崩れる。
    for file in \
      ${appDir}/cliamp_music/__main__.py \
      ${appDir}/cliamp_music/app.py \
      ${appDir}/cliamp_music/style/base.css \
      ${appDir}/cliamp_music/style/shell.css \
      ${appDir}/cliamp_music/style/pages.css \
      ${appDir}/cliamp_music/style/player.css \
      share/applications/${appId}.desktop \
      share/icons/hicolor/scalable/apps/${appId}.svg
    do
      test -f "$out/$file" || { echo "cliamp-music: $file がありません" >&2; exit 1; }
    done
    test ! -e "$out/${appDir}/cliamp_music/__pycache__"
    desktop-file-validate "$out/share/applications/${appId}.desktop"

    runHook postInstall
  '';

  preFixup = ''
    makeWrapper ${lib.getExe pythonEnv} "$out/bin/cliamp-music" \
      --add-flags "-m cliamp_music" \
      --prefix PYTHONPATH : "$out/${appDir}" \
      "''${gappsWrapperArgs[@]}"
  '';

  # 画面の要らない単体試験と、包んだ実行ファイルの自己診断。画面を使う
  # 試験はここでは飛ばされる (flake の checks.ui が Xvfb で回す)。
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck

    export HOME="$TMPDIR/home"
    mkdir -p "$HOME"
    export XDG_CACHE_HOME="$TMPDIR/cache" XDG_STATE_HOME="$TMPDIR/state"
    # 検査で import したときの __pycache__ を $out に残さない。
    export PYTHONDONTWRITEBYTECODE=1
    unset DISPLAY WAYLAND_DISPLAY
    ${pythonEnv.interpreter} -m unittest discover -s tests -v

    # 表示の無い所でも --self-check は窓を出さずに答える。typelib の不足、
    # CSS の書き損じ、アイコンの欠けはここで分かる。
    "$out/bin/cliamp-music" --self-check

    runHook postInstallCheck
  '';

  passthru = { inherit pythonEnv; };

  meta = {
    description = "macOS 27 Music-style GTK frontend for the cliamp terminal music player";
    homepage = "https://github.com/hatake716/cliamp-frontend";
    license = lib.licenses.mit;
    mainProgram = "cliamp-music";
    platforms = lib.platforms.linux;
  };
}
