# cliamp GUI IPC 拡張 (api 1)

cliamp 1.50.0 の IPC (`~/.config/cliamp/cliamp.sock`、改行区切りの JSON) に、
GUI が必要とするコマンドを **足す** パッチの仕様。パッチは
`patches/cliamp-1.50.0-gui-ipc.patch`。既存のコマンドと、その応答の形は
一切変えない (Open-Voice が `cliamp status` の平文出力と `play`/`pause`/
`next`/`prev`/`shuffle`/`repeat` を使っているため)。

- 1 行 1 要求、1 行 1 応答。1 本の接続で複数の要求を順に送ってよい。
- 応答は必ず `{"ok": true|false, ...}`。失敗時は `error` に理由。
- `omitempty` のため、数値の 0・空文字・false は **省かれる**。受け手は
  欠けた数値を 0、欠けた真偽を false として読むこと (既存の `index`、
  `position`、`volume` も同じ)。
- 要求 1 行の上限は 8 MiB (既存は 64 KiB だった。`replace` で曲の一覧を
  送るため広げる)。
- パッチを当てていない cliamp は新しいコマンドに
  `{"ok":false,"error":"unknown command: <cmd>"}` を返す。GUI はこれと
  `capabilities` の有無で「拡張なし (基本操作のみ)」と判断する。

## 型

### TrackInfo (既存の `track` を拡張)

既存の `title` / `artist` / `path` はそのまま。以下を追加。

| フィールド | 型 | 元 (`playlist.Track`) |
|---|---|---|
| `path` | string | Path (常に出す) |
| `title` | string | Title |
| `artist` | string | Artist |
| `album` | string | Album |
| `genre` | string | Genre |
| `year` | int | Year |
| `track_number` | int | TrackNumber |
| `duration` | int (秒) | DurationSecs (0 = 不明) |
| `stream` | bool | Stream |
| `live` | bool | Realtime (ラジオなど終わりの無い流れ) |
| `feed` | bool | Feed |
| `unplayable` | bool | Unplayable |
| `bookmark` | bool | Bookmark |
| `meta` | {string: string} | ProviderMeta (例 `spotify.id`、`navidrome.id`) |
| `queued` | int | `playlist` の応答だけ。「次に再生」の待ち行列での 1 始まりの位置 |
| `played_at` | string (RFC3339) | `history` の応答だけ |

GUI から cliamp へ曲を渡すとき (`replace` / `enqueue` / `playlist_add`) も
同じ形。受け取った TrackInfo をそのまま送り返せば元の Track に戻ること
(`stream` / `live` / `meta` を含めて往復で失われない)。GUI が自分で作る曲
(Radio Browser の局など) は `{"path": url, "title": 名前, "stream": true, "live": true}`。

### SourceInfo

いま読み込まれているリストの出どころ。`{"provider": "spotify", "id": "...", "name": "表示名"}`。
GUI が `replace` / `load_provider` で渡したもの。TUI 側でプロバイダーの
プレイリストを読み込んだときも、分かる範囲で埋める (分からなければ省く)。

### ProviderInfo

`{"key": "spotify", "name": "Spotify", "search": true, "playlists": true, "virtual": false}`

- `search`: `search` コマンドが使えるか (`provider.Searcher` を実装しているか、
  または下の yt-dlp 検索への退避が効くか)。
- `playlists`: `playlists` / `tracks` が意味を持つか。
- `virtual`: cliamp のプロバイダー一覧には無いが、IPC が用意する疑似プロバイダー。

疑似プロバイダー:

- `youtube` — 登録された `youtube` プロバイダーが無いときだけ一覧に出す
  (`search: true, playlists: false, virtual: true`)。`search` は
  yt-dlp の `ytsearch<N>:<query>` (TUI の Ctrl+F と同じ `resolve.Remote`)。
- `url` — 一覧には出さない。`tracks {"provider":"url","id":"<URL>"}` で
  URL (YouTube の動画・再生リスト・`list=RD…` のミックス、M3U/PLS、
  ポッドキャストのフィードなど) を `resolve.Remote` で曲にする。

`search` を `provider.Searcher` でないプロバイダー (radio など) に対して
呼んだ場合も、TUI の Ctrl+F と同じく yt-dlp の YouTube 検索へ退避する。
`soundcloud` を指定して Searcher が登録されていなければ `scsearch<N>:` へ退避する。

### PlaylistInfo

`{"id": "...", "name": "...", "track_count": 12, "duration": 2400, "section": "Library"}`
(`playlist.PlaylistInfo` の ID / Name / TrackCount / DurationSecs / Section)。

### LyricLine

`{"t": 12.34, "text": "歌詞の 1 行"}` (`t` は秒。同期していない歌詞は全行 0)。

## 要求の追加フィールド

既存: `cmd`, `value`, `playlist`, `path`, `name`, `band`, `sub`, `args`。追加:

| フィールド | 型 | 用途 |
|---|---|---|
| `index` | int (ポインタ。0 が有効値) | `play_index` / `replace` / `queue_edit` / `remove` / `load_provider` / `playlist_remove_track` |
| `to` | int (ポインタ) | `queue_edit move` の移動先 |
| `provider` | string | カタログ系 |
| `id` | string | カタログ系 |
| `query` | string | `search` |
| `limit` | int | `search` / `history` / `playlist` |
| `artist`, `title` | string | `lyrics` |
| `mode` | string | `enqueue` / `queue_edit` |
| `tracks` | [TrackInfo] | `replace` / `enqueue` / `playlist_add` |
| `source` | SourceInfo | `replace` |

## 応答の追加フィールド

| フィールド | 型 | 出すコマンド |
|---|---|---|
| `api` | int (= 1) | `capabilities`, `status` |
| `commands` | [string] | `capabilities` (既存も含めた全コマンド名) |
| `eq_presets` | [string] | `capabilities` (組み込み EQ プリセット名、表示順) |
| `eq` | [float] (10 個、dB) | `status` (いまの 10 バンドの値) |
| `tracks` | [TrackInfo] | `playlist`, `tracks`, `search`, `history` |
| `queue` | [int] | `playlist`, `queue_edit` (待ち行列の曲の、リスト上の添字を順に) |
| `up_next` | [int] | `playlist` (再生順で現在の曲の後に来る曲の添字。待ち行列に入っている曲は除く。最大 200) |
| `gen` | uint64 | `status`, `playlist`, `replace`, `remove` (リストが変わるたびに増える世代番号) |
| `source` | SourceInfo | `status`, `playlist` |
| `stream_title` | string | `status` (ICY の曲名。ラジオで曲名が変わる) |
| `buffering` | bool (ポインタ) | `status` (yt-dlp などの読み込み待ち) |
| `providers` | [ProviderInfo] | `providers` |
| `playlists` | [PlaylistInfo] | `playlists` |
| `lyrics` | [LyricLine] | `lyrics` |
| `synced` | bool (ポインタ) | `lyrics` |
| `needs_auth` | bool | カタログ系 (`playlist.ErrNeedsAuth` のとき。ブラウザでのサインインは IPC からは始めない) |

`status` の `track` は拡張した TrackInfo になる。`status` の `position` は、
yt-dlp のシーク待ちの間はシーク先の値を返す (TUI のつまみと同じ)。

## コマンド

### 状態系 (TUI の Update / daemon の Send で処理。3 秒で応答)

| コマンド | 要求 | 応答 | 動作 |
|---|---|---|---|
| `capabilities` | — | `api`, `commands`, `eq_presets` | 拡張の有無と版。サーバーだけで答える |
| `seek_to` | `value` (秒) | `{ok}` | 絶対位置へシーク。TUI では `playback.SetPositionMsg` (非同期の `seekAbsolute`) を使い、Update を止めない |
| `playlist` | `limit` (省略で全件) | `tracks`, `index`, `total`, `queue`, `up_next`, `gen`, `source` | いまのリストの写し |
| `play_index` | `index` | `{ok}` | TUI の「行で Enter」と同じ (`SetIndex` → `playCurrentTrack`)。範囲外はエラー |
| `replace` | `tracks`, `index` (既定 0), `source` | `total`, `gen` | リストを差し替え、`index` の曲から再生。シャッフル中は選んだ曲を先頭にして残りを混ぜる。`plMgrLoadAndPlay` と同じく Stop・ClearPreload を先に行う |
| `enqueue` | `tracks`, `mode` = `next` (既定) / `end` / `now` | `total` | `next`: 末尾に足して待ち行列へ (TUI の `q`)。`end`: 末尾に足す (TUI の `a`)。`now`: 先頭の曲をすぐ再生し、残りは待ち行列へ (TUI の Enter と同じ `playTrackImmediate`) |
| `queue_edit` | `mode` = `add` / `remove` / `clear` / `move`, `index`, `to` | `queue` | `add`/`remove`: リスト上の添字 `index` の曲を待ち行列へ入れる/外す。`move`: 待ち行列の位置 `index` を位置 `to` へ。`clear`: 空にする。変更後は先読み (gapless) をやり直す |
| `remove` | `index` | `total`, `gen` | リストから 1 曲消す。再生中の曲は消せない (エラー) |

### カタログ系 (IPC の接続ごとの goroutine で処理。Update を通らない。最大 120 秒)

| コマンド | 要求 | 応答 |
|---|---|---|
| `providers` | — | `providers` |
| `playlists` | `provider` | `playlists` / `needs_auth` |
| `tracks` | `provider`, `id` | `tracks` / `needs_auth` |
| `search` | `provider`, `query`, `limit` (既定 25、最大 50) | `tracks` |
| `load_provider` | `provider`, `id`, `index`, `name` | `total` (`tracks` の結果で `replace` する。`source` は `{provider, id, name}`) |
| `lyrics` | `artist`, `title` | `lyrics`, `synced` / `error: "not found"` |
| `history` | `limit` (既定 50) | `tracks` (新しい順、`played_at` 付き) |
| `playlist_add` | `name`, `tracks` | `{ok}` (ローカルのプレイリスト `~/.config/cliamp/playlists/<name>.toml` に追加。無ければ作る) |
| `playlist_delete` | `name` | `{ok}` |
| `playlist_remove_track` | `name`, `index` | `{ok}` |

ラジオの `.m3u` / `.pls` の中継 URL は、TUI と同じく `tracks` の中で実体の
URL へ展開する。

### daemon (`--daemon`) での扱い

状態系はすべて daemon でも同じ意味で動く。daemon に無い機能 (履歴の記録、
Lua、可視化) に依存するものは `{ok:false, error:"not supported in daemon mode"}`
を返す (待ち時間切れにしない)。カタログ系は daemon でも同じ実装を使う。

## GUI 側の約束

- 状態の問い合わせ (`status`) は専用の接続で行い、カタログ系の長い要求と
  同じ接続に並べない (1 本の接続の要求は順番に処理されるため)。
- 既存の `seek` (相対) は yt-dlp の曲で TUI を数秒止めるので使わない。`seek_to` を使う。
- 既存の `volume` は **絶対値** (dB、-30〜+6)。ヘルプの「adjust」は誤り。
- TUI では `play` は停止中に何もしない。停止中の再生は `toggle` を送る。
- GUI は MPRIS の名前を取らない (Open-Voice が MPRIS のプレイヤー数を前提に
  一時停止と再開をしているため)。
