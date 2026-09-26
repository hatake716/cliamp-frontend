# ミュージック (cliamp フロントエンド) 設計

cliamp (端末の音楽プレイヤー) を操作する GTK4 + libadwaita + PyGObject の
アプリ。見た目と操作は macOS 27 の「ミュージック」に合わせる。cliamp は
systemd のユーザーサービスとして tmux の中で TUI のまま常駐しており、この
アプリはその IPC (`PROTOCOL.md`) だけを通して操作する。再生そのものは
cliamp が行い、アプリを閉じても音楽は止まらない。

参考にした画面: Apple の Music User Guide (macOS 27、`support.apple.com/guide/music`)
の画面写真。数値は写真からの実測を GTK 向けに丸めた調整値で、Apple の仕様ではない。

## 1. 全体の約束

- アプリ ID `org.nixos.Music`、表示名「ミュージック」、実行ファイル `cliamp-music`。
- Python パッケージ `cliamp_music` (`python -m cliamp_music`)。文字列・ログ・
  コメントは日本語 (gettext は使わない)。ログは `print("cliamp-music: …", file=sys.stderr)`。
- 配色は暗色のみ (`Adw.ColorScheme.FORCE_DARK`)。このデスクトップは暗色固定で、
  全体の `~/.config/gtk-4.0/gtk.css` が USER 優先度 (800) で上書きしてくるため、
  アプリの CSS は **`Gtk.STYLE_PROVIDER_PRIORITY_USER + 1`** で読み、すべての
  規則をアプリの窓のクラス (`window.music`) の下に閉じ込める。
- アプリの窓 (メイン・ミニプレーヤー・イコライザ) はすべて CSS クラス `music` を持つ。
- GTK の罠 (このリポジトリの利用者の環境で実際に踏んだもの):
  - `Adw.HeaderBar` の `pack_start`/`pack_end` は縮まずに重なる。伸縮させたい
    塊は `set_title_widget` に、自然幅を大きく申告する包み (`widgets.FillWidth`) で入れる。
  - `Gtk.Label.set_markup` は壊れたマークアップで黙って空になる。曲名・
    アーティスト名は必ず `GLib.markup_escape_text` するか `set_text` を使う
    (曲名には `&`, `<`, `*` が普通に入る)。
  - `Adw.OverlaySplitView` は畳んだとき `.sidebar-pane` を外して `.background` を
    付ける。見た目は自前のクラス (`.music-sidebar`) で当てる。
  - GTK 4.22 の symbolic アイコンは塗りつぶしを強制するので、線だけの SVG は
    塊になる。自作アイコンは塗りの図形で描く。
  - GTK の CSS に backdrop-filter は無い。ガラスは「ほぼ不透明の面 + 上端の
    明るい線 + 下端の影 + 外側の影」で表す。影のぼかしは角丸の半径より小さく。
- 外部とのやりとり (IPC・HTTP・ファイル) は GTK のスレッドで待たない。
  スレッドで行い、結果は `GLib.idle_add` で戻す。

## 2. ファイル構成

```
cliamp_music/
  __init__.py      APP_ID, APP_NAME, VERSION, HERE (パッケージのディレクトリ)
  __main__.py      main() を呼ぶだけ
  app.py           MusicApp (Adw.Application)、CSS の読み込み、アクションとショートカット、--self-check
  protocol.py      純粋 (GTK/GLib 非依存)。データ型・解析・要求の組み立て・小道具
  client.py        CliampClient: ソケット通信 (スレッド)、状態の定期取得
  store.py         PlayerStore (GObject): 再生状態の唯一の置き場と操作
  catalog.py       Catalog: providers / playlists / tracks / search / history / lyrics / ローカルのプレイリスト編集
  artwork.py       ArtworkLoader: アートワークの取得・切り抜き・キャッシュ・代わりの絵
  radio.py         RadioBrowser: Radio Browser API (局の一覧・検索)
  state.py         GuiState: アプリ自身の保存値 (~/.local/state/cliamp-music/state.json)
  context.py       AppContext: 上の部品の束と、ページから使う共通の操作
  widgets.py       デザインの部品 (Artwork, TrackRow, カード, 棚, ボタン…)
  window.py        MusicWindow: 骨格 (サイドバー | ナビゲーション | 右パネル) と再生バーの重ね合わせ
  sidebar.py       Sidebar
  playerbar.py     PlayerBar
  panels.py        LyricsPanel, QueuePanel (右パネル)
  pages/
    __init__.py    create_page(ctx, page_id, **params) -> Adw.NavigationPage
    home.py        HomePage
    search.py      SearchPage
    radio.py       RadioPage
    recent.py      RecentPage (最近再生した項目)
    nowplaying.py  NowPlayingListPage (再生中のリスト)
    playlists.py   PlaylistsPage (すべてのプレイリスト), PlaylistDetailPage
  fullscreen.py    FullscreenPlayer
  miniplayer.py    MiniPlayer
  equalizer.py     EqualizerWindow
  style/
    base.css       色・文字・共通部品 (widgets.py の見た目)
    shell.css      サイドバー・再生バー・右パネル
    pages.css      各ページ
    player.css     フルスクリーン・ミニプレーヤー・イコライザ
  icons/hicolor/scalable/actions/music-*-symbolic.svg   自作の記号アイコン
  icons/hicolor/scalable/apps/org.nixos.Music.svg       アプリのアイコン
data/org.nixos.Music.desktop
tests/             unittest。fake_cliamp.py は偽の cliamp (api 1 を実装したソケットのサーバー)
scripts/shoot.py   Xvfb で偽の cliamp に繋いで各画面を PNG に撮る
patches/cliamp-1.50.0-gui-ipc.patch
package.nix / cliamp.nix / flake.nix / module.nix
```

CSS は `style/` の 4 ファイルをこの順に 1 つずつ読む (後のものが勝つ)。
アイコンは `icons/` を `Gtk.IconTheme.add_search_path` で足す。

## 3. モジュールの約束 (並行して書くための境界)

### protocol.py (純粋)

```python
@dataclass(frozen=True)
class Track:
    path: str
    title: str = ""; artist: str = ""; album: str = ""; genre: str = ""
    year: int = 0; track_number: int = 0; duration: int = 0
    stream: bool = False; live: bool = False; feed: bool = False
    unplayable: bool = False; bookmark: bool = False
    meta: tuple[tuple[str, str], ...] = ()   # 凍結のため (key, value) の組
    queued: int = 0; played_at: str = ""
    # 派生
    display_title -> str        # title、無ければ path の末尾
    subtitle -> str             # "artist — album" / "artist" / ""
    meta_get(key, default="") -> str
    youtube_id -> str | None    # watch?v= / youtu.be/ / music.youtube.com
    spotify_id -> str | None    # spotify:track:<id>
    is_local_file -> bool
    to_wire() -> dict           # PROTOCOL の TrackInfo (空の値は省く)
    from_wire(d) -> Track       # classmethod

@dataclass(frozen=True)
class Source: provider: str = ""; id: str = ""; name: str = ""

@dataclass
class Status:
    state: str            # "playing" | "paused" | "stopped" | "offline"
    track: Track | None; position: float; duration: float
    volume: float         # dB (-30〜+6)
    index: int; total: int; shuffle: bool
    repeat: str           # "off" | "all" | "one" (小文字に正規化)
    mono: bool; speed: float; eq_preset: str; eq: list[float]
    gen: int; source: Source; stream_title: str; buffering: bool; api: int

@dataclass
class PlaylistState:
    tracks: list[Track]; index: int; total: int
    queue: list[int]; up_next: list[int]; gen: int; source: Source

@dataclass(frozen=True)
class ProviderInfo: key: str; name: str; search: bool; playlists: bool; virtual: bool

@dataclass(frozen=True)
class PlaylistInfo: provider: str; id: str; name: str; track_count: int = 0; duration: int = 0; section: str = ""

@dataclass(frozen=True)
class LyricLine: t: float; text: str

@dataclass
class Lyrics: lines: list[LyricLine]; synced: bool

@dataclass
class Response:
    ok: bool; data: dict; error: str = ""
    kind: str = "ok"      # "ok" | "error" | "offline" | "timeout" | "unsupported"

def encode_request(cmd: str, **fields) -> bytes     # None の値は省く。末尾に \n
def decode_response(line: bytes) -> Response        # "unknown command" は kind="unsupported"
def parse_status(d) -> Status; parse_playlist(d) -> PlaylistState
def parse_tracks(d) -> list[Track]; parse_providers(d) -> list[ProviderInfo]
def parse_playlists(d, provider) -> list[PlaylistInfo]; parse_lyrics(d) -> Lyrics
def db_to_linear(db) -> float; linear_to_db(x) -> float   # cliamp の MPRIS と同じ換算 (lin = 10^((dB-6)/20))
def format_time(secs) -> str          # "3:07" / "1:02:03" / 不明 "--:--"
def mix_url(video_id) -> str          # https://www.youtube.com/watch?v=<id>&list=RD<id>
def track_key(track) -> str           # アートワークや重複除去の鍵
STATE_COMMANDS / CATALOG_COMMANDS     # タイムアウトの振り分けに使う
EQ_BANDS = ("70", "180", "320", "600", "1K", "3K", "6K", "12K", "14K", "16K")
```

### client.py

```python
class CliampClient(GObject.Object):
    # シグナル: "connection-changed" (bool connected), "status" (object Status)
    def __init__(self, socket_path: str | None = None)   # 既定 ~/.config/cliamp/cliamp.sock、環境変数 CLIAMP_MUSIC_SOCKET が優先
    socket_path: str
    connected: bool; api: int            # api 0 = 拡張なし
    capabilities: dict                   # capabilities の応答 (commands, eq_presets)
    def request(self, cmd, callback=None, *, timeout=None, **fields) -> None
        # 1 要求 1 接続。スレッドプール (最大 4) で送り、callback(Response) を main loop で呼ぶ。
        # timeout 既定: 状態系 5 秒、カタログ系 130 秒。
    def start(self) -> None              # 状態の定期取得を始める (専用スレッド・専用接続)
    def stop(self) -> None
    def set_poll_interval(self, seconds: float) -> None   # 表示中 0.4 秒、隠れているとき 1.5 秒
    def probe(self) -> None              # capabilities を取り直す (再接続時に自動)
```

### store.py

```python
class PlayerStore(GObject.Object):
    # シグナル (引数なし):
    #  "status-changed"     状態が変わった (位置の更新を含む。0.4 秒ごと程度)
    #  "track-changed"      再生中の曲 (path と index) が変わった
    #  "state-changed"      playing/paused/stopped/offline が変わった
    #  "playlist-changed"   リストの写し (self.playlist) を取り直した
    #  "history-changed"    self.history を取り直した
    #  "connection-changed" 接続や api が変わった
    def __init__(self, client: CliampClient)
    status: Status; playlist: PlaylistState; history: list[Track]
    connected: bool; api: int; supports(cmd) -> bool
    def position_now(self) -> float      # 最後の status から経過時間で補間した位置 (再生中のみ進む)
    def current_track(self) -> Track | None
    # 操作 (すぐ手元の状態を楽観的に変え、次の status で本物に合わせる)
    def toggle(); play(); pause(); stop(); next(); prev()
    def seek_to(seconds); seek_by(delta)
    def set_volume_db(db); volume_step(delta_db)
    def set_shuffle(on: bool); set_repeat(mode: str); cycle_repeat()
    def set_speed(x: float); set_eq_preset(name); set_eq_band(index, db)
    def list_devices(callback(list[tuple[str, bool]]))   # (名前, 使用中)
    def set_device(name)
    def play_index(i); replace(tracks, index=0, source=None)
    def enqueue(tracks, mode="next"); queue_edit(mode, index=None, to=None); remove(index)
    def refresh_playlist(); refresh_history()
```

`gen` が変わったら store が自分で `playlist` を取り直して `playlist-changed` を出す。
`track-changed` のときは `history` も (少し遅らせて) 取り直す。

### catalog.py

```python
class Catalog:
    def __init__(self, client: CliampClient)
    def providers(self, callback(list[ProviderInfo] | Response))
    def playlists(self, provider, callback(list[PlaylistInfo] | Response))
    def tracks(self, provider, id, callback(list[Track] | Response))
    def search(self, provider, query, callback(list[Track] | Response), limit=25)
    def lyrics(self, artist, title, callback(Lyrics | None))
    def history(self, callback(list[Track] | Response), limit=50)
    def load(self, provider, id, index=0, name="", callback=None)   # load_provider
    def playlist_add(self, name, tracks, callback=None)
    def playlist_delete(self, name, callback=None)
    def playlist_remove_track(self, name, index, callback=None)
```

失敗は `Response` (kind 付き) をそのまま callback に渡す。結果は短時間
覚えておく (検索は同じ語で 10 分、プレイリスト一覧は 5 分)。

### artwork.py

```python
class ArtworkLoader:
    def __init__(self, cache_dir: str | None = None)   # 既定 ~/.cache/cliamp-music/artwork
    def request(self, subject, size: int, callback(Gdk.Texture)) -> Handle   # handle.cancel()
        # subject: Track / URL 文字列 / ("placeholder", key)
        # callback は必ず 1 回 (取れなければ代わりの絵で) main loop で呼ぶ
    def placeholder(self, key: str, size: int, kind="track") -> Gdk.Texture  # すぐ返る。色は key の hash
```

曲からの絵の求め方 (純粋な関数 `art_sources(track) -> list[str]` に切り出して試験する):
1. `meta["art"]` があればそれ (http(s):// または file://)。
2. YouTube の曲: `https://i.ytimg.com/vi/<id>/hqdefault.jpg` (480x360 の上下に黒帯。
   16:9 の中央 270x270 を切り抜く)。大きな表示 (>= 300px) では `sddefault.jpg` を先に試す。
3. Spotify: `https://open.spotify.com/oembed?url=https://open.spotify.com/track/<id>` の
   `thumbnail_url` (認証不要)。
4. 手元のファイル: 埋め込みの絵 (mutagen があれば) → 同じフォルダの cover/folder.(jpg|png)。
5. 取れなければ代わりの絵 (色のグラデーション + 音符)。
取得した絵は正方形に切り抜き、最大 600px で PNG としてキャッシュする。

### radio.py

```python
class RadioBrowser:
    def top(self, callback(list[Track] | str), limit=40)             # stations/topvote
    def by_country(self, code, callback, limit=40)                   # stations/bycountrycodeexact/<code>?order=votes
    def search(self, query, callback, limit=60)                      # stations/byname/<q>?order=votes
```

局は `Track(path=url_resolved, title=name, stream=True, live=True,
meta=(("art", favicon), ("radio.country", ...), ("radio.tags", ...), ("radio.codec", ...), ("radio.bitrate", ...)))`
として返す (そのまま `replace` できる)。失敗は日本語の理由の文字列。

### state.py

```python
class GuiState:   # ~/.local/state/cliamp-music/state.json。壊れていたら既定値で起動
    recent_searches: list[str]   # 新しい順、最大 12
    search_scope: str            # "youtube" など
    right_panel: str             # "" | "lyrics" | "queue"
    last_page: str
    window_width: int; window_height: int; window_maximized: bool
    def add_recent_search(q); def save()
```

### context.py

```python
class AppContext:
    app; window; client; store; catalog; artwork; radio; state
    def navigate(self, page_id: str, **params)   # サイドバーの項目 = 根を差し替え、それ以外 = push
    def toast(self, text: str)
    def play_tracks(self, tracks, index=0, source=None)       # store.replace + 失敗時のトースト
    def play_now(self, track)                                  # enqueue(mode="now")
    def load_provider(self, provider, id, index=0, name="")
    def start_station(self, track)                             # YouTube の曲からミックスを読み込む
    def track_menu(self, track, *, index=None, context="") -> tuple[Gio.MenuModel, Gio.ActionGroup]
        # 「…」メニュー。行ごとに action group ("track") を差し込んで使う。
        # 項目: 次に再生 / 最後に再生 / プレイリストに追加 ▸ (既存のローカル一覧 + 新規プレイリスト…) /
        #       ステーションを作成 (YouTube の曲) / リンクをコピー (URL の曲) / ブラウザで開く /
        #       context が "nowplaying" なら「リストから削除」、"local:<name>" なら「プレイリストから削除」、
        #       "queue" なら「待ち行列から外す」
```

### ページ

すべてのページは `Adw.NavigationPage` を継承し、`__init__(self, ctx, **params)`。
中身は `Adw.ToolbarView` (上に透明な `Adw.HeaderBar`、`extend-content-to-top-edge`)
+ スクロール。スクロールの下端には再生バーの分 (96px) の余白を取る。
`refresh()` を持ち、Ctrl+R で呼ばれる。ページ ID:

| ID | 引数 | クラス |
|---|---|---|
| `search` | — | SearchPage |
| `home` | — | HomePage |
| `radio` | — | RadioPage |
| `recent` | — | RecentPage |
| `nowplaying` | `reveal=False` (真なら再生中の行までスクロール) | NowPlayingListPage |
| `playlists` | — | PlaylistsPage |
| `playlist` | `provider`, `id`, `name` | PlaylistDetailPage |

## 4. 画面

### 窓の骨格 (window.py)

```
Adw.ApplicationWindow.music.music-window   既定 1180x760、最小 760x520
└ Adw.ToastOverlay
  └ Gtk.Stack root ("main" / "fullscreen")
    ├ main: Adw.OverlaySplitView (.music-split)   760sp 以下で畳む
    │   ├ sidebar: Sidebar (幅 220)
    │   └ content: Gtk.Box (横)
    │       ├ Gtk.Overlay (.music-content)
    │       │   ├ Adw.NavigationView (ページ)
    │       │   ├ 下端のフェード (再生バーの後ろを地の色へ溶かす帯、高さ 110)
    │       │   └ PlayerBar (中央下、下余白 14、最大幅 780、左右の余白 28)
    │       └ Gtk.Revealer (右から滑り込む、幅 300) → Gtk.Stack (LyricsPanel / QueuePanel)
    └ fullscreen: FullscreenPlayer
```

未接続のとき: ナビゲーションの上に全面の空状態「cliamp に接続できません」と
「cliamp を起動」ボタン (`systemctl --user start cliamp.service`)。拡張の無い
cliamp (api 0) のときは上端に細い帯「この cliamp は拡張 IPC に対応していません。
再生の操作だけ使えます」。

### サイドバー (sidebar.py)

macOS 27 の形: 窓の端まで続く帯 (浮かない)、赤い記号、選択は灰色の面と太字。

```
[信号]                                ← 透明なヘッダー (窓の操作ボタンだけ)
 🔍 検索
 ⌂  ホーム
 📻 ラジオ
ライブラリ                              ← 11px/600 の見出し
 🕘 最近再生した項目
 ≣  再生中のリスト
プレイリスト
 ▦  すべてのプレイリスト
 ♫  <ローカルのプレイリスト…>
 ♫  <Spotify などのプレイリスト…>      (プロバイダーが使えるときだけ。取得に失敗したら黙って出さない)
──
 ● cliamp · 再生中 / 停止 / 未接続     ← 下端の状態表示。押すと接続の詳細 (ソケット、api、プロバイダー)
```

### 再生バー (playerbar.py)

画面下に浮かぶガラスのカプセル (高さ 56、角は完全な丸)。左から:

1. シャッフル (オンで赤い記号 + 薄い赤の丸)、前へ、再生/一時停止 (大きめ)、次へ、
   リピート (オフ → すべて → 1 曲、1 曲のときは「1」の印)
2. アートワーク 36px (角 5)。押すとメニュー: 「ミニプレーヤー」「フルスクリーンプレーヤー」
3. 曲名 (13px/600) と「アーティスト — アルバム」(12px、副次色)。ラジオは ICY の曲名を
   曲名に、局名を副題に。読み込み中は副題を「読み込み中…」
4. その下に細い再生位置の線 (3px、ホバーで 5px とつまみ)。ドラッグで `seek_to`。
   ホバーで経過時間と残り時間を小さく出す。ライブ配信では線を出さず「ライブ」
5. 「…」: 再生速度 ▸ (0.5〜2.0)、イコライザ…、再生中のリストを表示、リンクをコピー、ブラウザで開く
6. 別の塊: 歌詞 (右パネル)、次に再生 (右パネル)、出力先 (`device list` の一覧)、音量 (押すと横のスライダーのポップオーバー)

狭い幅では 6 の出力先と 5 を先に隠し、次に 2 の副題を隠す。

### 右パネル (panels.py、幅 300、サイドバーと同じ地)

- 歌詞: 22px/700 の行。今の行は明るく、他は 3 次色。今の行を上から 1/3 に保って
  滑らかに送る。行を押すとその時刻へ `seek_to`。同期していない歌詞は全行同じ色で
  送らない。無ければ「歌詞が見つかりません」。取得はパネルを開いているときだけ。
- 次に再生: 上に 2 つの横長カプセル「シャッフル」「リピート」(オンで赤の塗り)。
  その下に「履歴」(最近再生した 10 曲) → 「次に再生」(見出しの右に赤い「消去」=
  待ち行列を空にする)。待ち行列 (`queue`) の曲、続いて `up_next` の曲。行は 48px
  (絵 38px 角 4、曲名 13px、副題 11px、「…」)。行をダブルクリックで `play_index`。
  最初に開いたときは「次に再生」の見出しまでスクロールしておく。

### ホーム (pages/home.py)

大見出し「ホーム」(34px/700)。棚 (見出し 17px/700、横スクロール、右端に丸い「›」):
1. 「おすすめ」: 縦長のカード (220x290、角 12)。最近再生した YouTube の曲から
   「<アーティスト> のステーション」(押すと `start_station`)、「再生中のリスト」
   (リストがあれば)、「cliamp ラジオ」。絵の下半分に白い文字 (小さな上見出し + 太字の題)。
2. 「最近再生した項目 ›」(→ `recent`): 正方形 170px のカード (角 7)、下に曲名と
   アーティスト。押すと履歴を `replace` してその曲から再生。
3. 「プレイリスト ›」(→ `playlists`)。
4. 「ラジオ局」: Radio Browser の日本の人気局。

### 検索 (pages/search.py)

ツールバーの中央に検索欄 (カプセル、幅 380、赤いフォーカスの輪)、右に範囲の切り替え
(`Adw.ToggleGroup`): 「YouTube」「Spotify」(使えるときだけ)「ライブラリ」(ローカルの
プレイリストと履歴から探す)。Enter か 0.6 秒の入力停止で検索。
- 入力前: 「最近の検索」(押せる丸いチップ、右に「消去」) と「カテゴリーを探す」
  (16:9、角 8 のタイル、色の組はカテゴリーごとに固定。J-POP、アニメ、シティポップ、
  ロック、ヒップホップ、ジャズ、クラシック、エレクトロニック、Lo-fi、作業用BGM、
  K-POP、90年代)。タイルを押すとその語で検索。
- 結果: 左に「トップの結果」(大きな絵 + 曲名 + アーティスト + 再生ボタン)、右に
  「曲」の最初の 4 行。その下に「曲」の全件 (40px の絵、曲名、アーティスト、時間、「…」)。
  行のダブルクリック/Enter で結果全体を `replace` してその曲から再生。
- 検索中はスピナー、失敗は理由を空状態で出す。

### ラジオ (pages/radio.py)

大見出し「ラジオ」、ツールバーに局の検索欄。棚/格子:
「cliamp ラジオ」(`playlists radio` の `l:`/`f:` の局 → `load_provider radio <id>`)、
「日本の人気局」「世界の人気局」(Radio Browser)。局のタイルは favicon を色の地の中央に
置く (favicon は小さく荒いものが多いので全面に引き伸ばさない)。検索結果は格子。

### ライブラリ

- 最近再生した項目 (recent.py): 見出し + 「▶ 再生」カプセル + シャッフルの丸。
  行: 絵 40px、曲名、アーティスト、「3 時間前」、時間。
- 再生中のリスト (nowplaying.py): 詳細ページの形 (下)。題は `source.name` か
  「再生中のリスト」。行は番号付き、再生中の行は番号の代わりに赤い動くバー。
  ダブルクリックで `play_index`。リストが変われば作り直す。
- すべてのプレイリスト (playlists.py): プレイリストのカードの格子 (ローカル + 各プロバイダー)。
- プレイリストの詳細: 左上に 250px の絵 (角 10、柔らかい影。先頭 4 曲の 2x2 か代わりの絵)、
  右に題 (26px/700)、提供元 (26px/400、赤)、情報の行「12 曲 · 48 分」(11px/600 副次色)、
  ボタン: シャッフルの丸 (34px) / 「▶ 再生」カプセル (34x120) / 「…」の丸。
  下に曲の行 (43px、番号・曲名・時間・「…」、区切り線は曲名の列から)。

### フルスクリーンプレーヤー (fullscreen.py、Shift+Ctrl+F、Esc で戻る)

背景はアートワークを大きくぼかして暗くしたもの。左の列に 360px の絵、曲名、
「アーティスト — アルバム」、再生位置 (経過 / 残り)、操作の列 (シャッフル・前・
再生・次・リピート)、音量。右に大きな歌詞 (34px/800、今の行から離れるほど薄く)
か次に再生。左上にガラスのカプセル (✕ / ミニプレーヤー)、右下に「歌詞 | 次に再生」の切り替え。

### ミニプレーヤー (miniplayer.py、Shift+Ctrl+M)

別の小さな窓 (320x320)。アートワークが全面、マウスを乗せると下からぼかしの帯に
曲名・位置・操作が出る。「…」で横長 (400x110) との切り替え。

### イコライザ (equalizer.py、Ctrl+Alt+E)

小さな窓。プリセットの選択 (`eq_presets` + 「カスタム」)、10 本の縦のスライダー
(−12〜+12 dB、`EQ_BANDS` のラベル)。動かすと 80ms まとめて `eq` を送る。

## 5. 見た目の基準値 (style/base.css の変数)

| 変数 | 値 | 用途 |
|---|---|---|
| `--m-canvas` | `rgb(28, 29, 33)` | 本文の地 (App Store の自作テーマと同じ) |
| `--m-sidebar` | `rgb(41, 43, 50)` | サイドバーと右パネルの地 |
| `--m-key` | `#fa2d48` | Music の赤 (塗り) |
| `--m-key-text` | `#fa586a` | 暗い地の上の赤い文字・記号 |
| `--m-label` | `rgba(255,255,255,0.92)` | 本文 |
| `--m-secondary` | `rgba(235,235,245,0.60)` | 副次 |
| `--m-tertiary` | `rgba(235,235,245,0.32)` | 3 次 |
| `--m-fill` | `rgba(118,118,128,0.24)` | 丸ボタン・「再生」カプセルの地 |
| `--m-separator` | `rgba(255,255,255,0.09)` | 区切り線 |
| `--m-selected` | `rgba(255,255,255,0.115)` | サイドバーの選択 |
| `--m-hover` | `rgba(255,255,255,0.05)` | 行のホバー |
| `--m-glass` | `rgba(46,47,54,0.90)` | 再生バーなどのガラス面 |

文字: 大見出し 34px/700、詳細の題 26px/700、棚の見出し 17px/700、本文 13px、
副題 12px、情報 11px/600、上見出し 10px/600 大文字。数字は `font-feature-settings: "tnum"`。
字体はシステム (Geist)。

角: 詳細の絵 10、格子の絵 7、行の絵 4、タイル 8、カード 12、ボタンはカプセルか円。

アクセント: libadwaita の `--accent-bg-color` / `--accent-color` もアプリの中では赤にする。

## 6. キー操作 (Command → Ctrl)

| キー | 動作 |
|---|---|
| Space | 再生/一時停止 (文字入力中は除く) |
| Ctrl+→ / Ctrl+← | 次へ / 前へ |
| Shift+Ctrl+→ / ← | 10 秒進む / 戻る (Ctrl+Alt+矢印は GNOME の作業領域の切り替えと重なるため使わない) |
| Ctrl+↑ / Ctrl+↓ | 音量 ±2 dB |
| Ctrl+. | 停止 |
| Ctrl+L | 再生中の曲をリストで表示 |
| Ctrl+F | 検索 |
| Ctrl+Alt+U | 次に再生 (右パネル) |
| Shift+Ctrl+L | 歌詞 (右パネル) |
| Shift+Ctrl+F | フルスクリーンプレーヤー (Esc で戻る) |
| Shift+Ctrl+M | ミニプレーヤー |
| Ctrl+Alt+E | イコライザ |
| Ctrl+R | ページを更新 |
| Ctrl+0 | メインの窓 |
| Ctrl+W / Ctrl+Q | 窓を閉じる / 終了 |
