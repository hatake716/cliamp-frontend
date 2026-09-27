# cliamp GUI IPC 拡張 (api 1)

cliamp 1.50.0 の IPC (`~/.config/cliamp/cliamp.sock`、改行区切りの JSON) に、
GUI が必要とするコマンドを **足す** パッチの仕様。パッチは
`patches/cliamp-1.50.0-gui-ipc.patch`。既存のコマンドと、その応答の形は
一切変えない (Open-Voice が `cliamp status` の平文出力と `play`/`pause`/
`next`/`prev`/`shuffle`/`repeat` を使っているため)。

- 1 行 1 要求、1 行 1 応答。1 本の接続で複数の要求を順に送ってよい。
- cliamp が終わるとき (TUI の `q`、daemon の SIGTERM など) は、開いている
  接続をサーバー側から閉じる。受け手はいつでも EOF や EPIPE を受けうるものと
  して扱い、繋ぎ直すこと (閉じないと、接続を保って status を送り続ける GUI が
  いる限り cliamp が終われなかった)。
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
| `meta` | {string: string} | ProviderMeta (例 `spotify.id`、`navidrome.id`。YouTube へ橋渡しした Spotify の曲は `spotify.id` と `spotify.bridge`。下の「Spotify の Web API だけの接続」) |
| `queued` | int | `playlist` の応答だけ。「次に再生」の待ち行列での 1 始まりの位置 |
| `played_at` | string (RFC3339) | `history` の応答だけ |
| `path_raw` | string (base64) | Path が UTF-8 でないときだけ付く、Path の元のバイト列 |

`path` は常に正しい UTF-8 (表示用)。ファイル名が UTF-8 でない (Windows の zip
から出した Shift_JIS の名前など) と JSON では U+FFFD に置き換わって元に戻せない
ので、そのときだけ元のバイト列を `path_raw` に base64 (標準、パディング付き) で
入れる。受け手は `path_raw` もそのまま送り返すこと (`path_raw` があれば cliamp は
`path` ではなくそちらを使う。壊れた `path_raw` の曲は受け付けない)。手元の
ファイルを開くときも `path_raw` を戻したバイト列を使う。

GUI から cliamp へ曲を渡すとき (`replace` / `enqueue` / `playlist_add`) も
同じ形。`replace` / `enqueue` で送った TrackInfo は、`playlist` / `status` でそのまま
戻る (`stream` / `live` / `meta` / `path_raw` を含めて往復で失われない)。GUI が自分で
作る曲 (Radio Browser の局など) は `{"path": url, "title": 名前, "stream": true, "live": true}`。

ただしファイルに書くもの (`playlist_add` のローカルのプレイリストの TOML、TUI が記録する
`history` の history.toml) は往復で欠ける:

- ローカルのプレイリストに残るのは path (UTF-8 でない名前もそのまま)・title・artist・album・
  genre・year・track_number・duration・feed・bookmark だけ。`history` はさらに feed と
  bookmark も持たない。
- `live`・`meta`・`unplayable` は残らない。`stream` は持ち越さず、読み戻すときに path が
  URL (http(s) か yt-dlp の検索式) かで決め直す。
- そのため GUI はラジオの局 (live) を「プレイリストに追加」させない (読み戻すと局の絵も
  「ライブ」の印も失う)。

path の無い曲 (空白だけも) を送ると `replace: track N has no path` のように誤りになる
(N は要求の中の添字)。`stream` の無い http(s) の URL で yt-dlp が再生しないもの (YouTube・
SoundCloud・Bandcamp などでないもの) には cliamp が `stream: true` を立てる。

### SourceInfo

いま読み込まれているリストの出どころ。`{"provider": "spotify", "id": "...", "name": "表示名"}`。
GUI が `replace` / `load_provider` で渡したもの。TUI 側でプロバイダーの
プレイリストを読み込んだときも、分かる範囲で埋める (分からなければ省く)。

### ProviderInfo

`{"key": "spotify", "name": "Spotify", "search": true, "playlists": true, "virtual": false}`
(Web API だけの接続の Spotify は `..., "virtual": false, "playback": "youtube"}`)

- `search`: そのキーで `search` を呼んで、そのプロバイダーの検索になるか
  (`provider.Searcher` を実装しているもの、`yt` / `youtube` / `ytmusic` / `soundcloud` の
  キー、疑似プロバイダー `youtube`)。radio などは `search` を呼べば YouTube の検索へ退避する
  (下) が、その局の検索ではないので false。
- `playlists`: `playlists` / `tracks` が意味を持つか。
- `virtual`: cliamp のプロバイダー一覧には無いが、IPC が用意する疑似プロバイダー。
- `playback` (string、省略可): そのプロバイダーの曲を別の所から鳴らしているときだけ、その名前。
  いまは Spotify が Web API だけで繋がっているときの `"youtube"` だけ (下の「Spotify の Web API
  だけの接続」)。Premium で librespot から鳴らしているとき、ほかのプロバイダー、疑似プロバイダーには
  付かない。`providers` のたびにセッションのいまの状態から求めるので、Spotify のセッションが
  まだ無いうち (起動後、spotify の `playlists` / `tracks` / `search` を 1 度も呼んでいないうち) は、
  保存された資格情報が Web API だけのものでも付かない。GUI は spotify のカタログ系が初めて
  成功した後に `providers` を取り直すこと。
- ProviderInfo の真偽は omitempty を付けない (false の真偽も省かない)。`playback` だけは
  無いときに省く。local の名前は `Local`。

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
`provider` を省いたら `youtube`。`url` は検索できない (`provider url does not support search`)。
yt-dlp の検索結果は `live` を持たない (24 時間の配信も、長さ 0 のふつうの曲として来る)。

radio の `l:0` は組み込みの cliamp radio。`tracks` では中の M3U を 15 本ほどの配信に
展開する (再生すると 15 局のリストになり、曲名は Lofi などの配信の名前)。

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
| `path` (既存) | string | 省略可。`play_index` / `remove` / `queue_edit` (`add`/`remove`/`move`) / `playlist_remove_track` の確かめ用。下の「添字の確かめ」 |
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
| `gen` | uint64 | `status`, `playlist`, `replace`, `remove`, `enqueue`, `queue_edit`, `load_provider` (リストが変わるたびに増える世代番号) |
| `source` | SourceInfo | `status`, `playlist` |
| `stream_title` | string | `status` (ICY の曲名。ラジオで曲名が変わる) |
| `buffering` | bool (ポインタ) | `status` (yt-dlp・流れの曲の読み込み待ち、ポッドキャストのフィードの展開中) |
| `playback_error` | string | `status` (いまの曲の最後の開始が失敗した理由。下の「再生の失敗」) |
| `providers` | [ProviderInfo] | `providers` |
| `playlists` | [PlaylistInfo] | `playlists` |
| `lyrics` | [LyricLine] | `lyrics` |
| `synced` | bool (ポインタ) | `lyrics` |
| `needs_auth` | bool | カタログ系 (`playlist.ErrNeedsAuth` のとき。ブラウザでのサインインは IPC からは始めない) |

`status` の `track` は拡張した TrackInfo になる。`status` の `position` は、
yt-dlp のシーク待ちの間はシーク先の値を返す (TUI のつまみと同じ)。

`gen` について: 曲の追加・削除・並べ替え・差し替え、待ち行列 (「次へ」で待ち行列から
取ったときを含む)、ブックマーク、シャッフル (混ぜ直しを含む)、リピートの変更で増える。
再生が次の曲へ進んだだけでは増えない (`index` と `up_next` は変わる)。GUI は gen が
手元の写しと同じなら曲の中身は同じとみなし、`playlist` を `limit: 1` で取って
index / queue / up_next / source だけを入れ替える。

読み込み中 (`buffering: true`) の `state` / `position` / `duration`:

- 止まった状態から yt-dlp・流れの曲を始めたときは `state: "stopped"`。GUI は
  stopped + buffering を「読み込み中」として扱う。
- 鳴っている曲から next / prev / play_index で yt-dlp・流れの曲へ移るとき、TUI は前の曲を
  止めずに裏で読み込む。その間 `track` と `index` は新しい曲だが、`state` は `"playing"`、
  `position` と `duration` はまだ鳴っている前の曲のもの (Player が前の流れを読むため)。
  GUI は「最後に読み込みの終わった曲から変わった buffering」と、自分の next / prev /
  play_index の後の buffering を切り替え中とみなし、位置 0・長さは新しい曲の `duration` と
  して見せ、シークさせない (この間の `seek_to` は前の曲に効いて失われる)。TUI が URL や
  フィードを読むだけのときも buffering は立つが、そのときの曲は変わっていないので
  切り替え中ではない。
- HTTP の流れ (ポッドキャストの音声、長さの分かる URL の曲) の `seek_to` は、応答した後で
  繋ぎ直す (`streamSeekAbsolute`)。繋ぎ直すまで `position` は前の位置のまま止まる。GUI は
  応答の後も、届く位置がシーク先に着くまで (最大 3 秒) シーク先を見せる。

### 再生の失敗 (`playback_error`)

`status` の `playback_error` は、**いまの曲の最後の開始が失敗した** ときだけ付く、その理由の
文言 (cliamp の誤りそのまま。英語。yt-dlp の誤りは stderr 全体で、WARNING の行が先に並ぶことも
ある)。2 KiB を越えるときは文字の途中で切らずに縮め、末尾を `…` にする。失敗していなければ
omitempty で省かれる。`cliamp status` の平文の出力には出さない (`--json` には出る)。

立つとき (どれもその曲の開始の失敗):

- yt-dlp・HTTP の流れの曲の開始の失敗 (`streamPlayedMsg` の誤り。TUI の `ERR: yt-dlp: ERROR: …`)。
  例: `yt-dlp: ERROR: [youtube] x8VYWazR5mE: Sign in to confirm your age. …`
- 手元のファイルなどの同期の Play の失敗。例: `open source: open /x.flac: no such file or directory`、
  `decode: …`。Spotify のセッション切れ (`…: sign-in required`) も含む (TUI はサインインの画面を
  出して ERR を出さないが、GUI には理由を渡す)。
- ポッドキャストのフィード (`feed: true`) の展開の失敗と、エピソードが無いとき
  (`no episodes found in feed`)。
- yt-dlp の曲のシーク (yt-dlp の起こし直し) の失敗 (`yt-dlp seek: …`)。音が止まったままになるため。
  このときだけは、後のシークが成功すれば (音が戻れば) 消える。
- Web API だけで繋がった Spotify で `spotify:track:` の曲 (履歴、前に保存したローカルのプレイリスト、
  Premium だったころの写しなど) を始めたとき。文言は
  `custom streamer: spotify: streaming unavailable (this Spotify connection is Web API only; Spotify Premium is required to stream Spotify tracks)`。
  受け手は `spotify: streaming unavailable` を含むかで見分け、「Spotify の曲を Spotify から鳴らすには
  Premium が要る」と伝える (サインインの問題ではないので `needs_auth` にはならず、TUI もサインインの
  画面を出さない)。

消えるとき: 次の開始の時点 (同じ曲のやり直し、next / prev / play_index / replace / enqueue、止まった
曲の toggle など。読み込み中は出ない)、gapless で次の曲へ進んだとき。**止めても (stop) 消えない**
(いまの曲はまだ失敗した曲)。失敗した曲がいまの曲でなくなれば (止まっている間に remove や別の
経路で今の曲が変わった) 出さない。

立たないもの: TUI の `ERR:` のうち再生と関係の無いもの (設定の保存、プロバイダーやプレイリストの
読み込み、歌詞、出力先)、流れの途中の切断 (TUI が自分で繋ぎ直す。繋ぎ直しの開始が失敗すれば
立つ)、追い越された開始 (`player.ErrSuperseded`)、HTTP の流れのシークの失敗 (前の流れに戻る)。

`state` は、止まった状態から始めて失敗すれば `stopped`。鳴っている曲から next / play_index などで
別の曲へ移って失敗したときは、TUI は開始の前に前の曲を止めないので、前の曲が鳴り続けて `playing`
のまま (`track` は失敗した曲) になりうる。その前の曲が終わると TUI は次の曲へ進む (その開始で
`playback_error` は消える)。`replace` と止まっているときの開始は先に止めるので `stopped` になる。
daemon (`--daemon`) も同じ意味で返す (開始の失敗と yt-dlp のシークの失敗)。

GUI は文言を短い日本語にまとめ (`protocol.describe_playback_error`)、再生バーの副題 (琥珀色)・
フルスクリーン・ミニプレーヤーに出し、全文はツールチップに出す。新しい失敗 (曲と文言の組) ごとに
1 度だけトーストを出す。

## コマンド

### 状態系 (TUI の Update / daemon の Send で処理。3 秒で応答)

| コマンド | 要求 | 応答 | 動作 |
|---|---|---|---|
| `capabilities` | — | `api`, `commands`, `eq_presets` | 拡張の有無と版。サーバーだけで答える |
| `seek_to` | `value` (秒) | `{ok}` | 絶対位置へシーク。TUI では `playback.SetPositionMsg` (非同期の `seekAbsolute`) を使い、Update を止めない。負の値は `seek_to requires a non-negative position` |
| `playlist` | `limit` (省略で全件。先頭から) | `tracks`, `index`, `total`, `queue`, `up_next`, `gen`, `source` | いまのリストの写し。`up_next` はシャッフル中の回り込みを含めない: シャッフルとリピート (すべて) では今の一巡の残りだけで、尽きると cliamp は混ぜ直して続ける (GUI は「このあとシャッフルし直して続けて再生します」と書く)。待ち行列から鳴っている曲はリスト上の後ろの位置にあれば含む。再生できない曲は含めない |
| `play_index` | `index`, `path` | `{ok}` | TUI の「行で Enter」と同じ (`SetIndex` → `playCurrentTrack`)。範囲外は `index out of range`。待ち行列からは外さず、リストの位置をその曲へ移す (待ち行列の曲に使うと、その曲がもう 1 度鳴り、間のリストの曲が飛ぶ。GUI は待ち行列の曲には `queue_edit remove` で前の曲を外してから `next` を送る) |
| `replace` | `tracks`, `index` (既定 0), `source` | `total`, `gen` | リストを差し替え、`index` の曲から再生。シャッフル中は選んだ曲を先頭にして残りを混ぜる。`plMgrLoadAndPlay` と同じく Stop・ClearPreload を先に行う |
| `enqueue` | `tracks`, `mode` = `next` (既定) / `end` / `now` | `total` | 曲はどれもリストの末尾に足す (既存の曲の添字は動かない)。再生順は下のとおり。`next`: いまの曲のすぐ後ろ (待ち行列より後) に、足した順に並べる。待ち行列は使わず、`queue` ではなく `up_next` に出る (待ち行列に入れると、待ち行列で 1 度、リストの順でもう 1 度鳴っていた)。ただし 1 曲リピート中は順送りで進まないので、従来どおり待ち行列に入れる (TUI の `q`)。止まっていればその曲から鳴らす。`end`: 末尾に足す (TUI の `a`)。止まっていれば先頭の曲から鳴らす。`now`: 先頭の曲をすぐ再生し、残りは足した順にそのすぐ後ろで鳴る (TUI の Enter と同じ `playTrackImmediate`)。シャッフル中の `now` と止まっているときの `end` は、足した曲を再生順でいまの曲のすぐ後ろへ置いてから鳴らすので、これから鳴るはずだった曲は飛ばされない。空のリストへの `next` は `end` と同じ。応答は `total` と `gen`。知らない mode は `enqueue: unknown mode "x"` |
| `queue_edit` | `mode` = `add` / `remove` / `clear` / `move`, `index`, `to`, `path` | `queue`, `gen` | `add`/`remove`: リスト上の添字 `index` の曲を待ち行列へ入れる/外す (入っている曲の `add` は何もしない)。`move`: 待ち行列の位置 `index` を位置 `to` へ (`path` は位置 `index` の曲の path。同じ位置なら何もせず成功)。`clear`: 空にする。変更後は先読み (gapless) をやり直す |
| `remove` | `index`, `path` | `total`, `gen` | リストから 1 曲消す。今の曲は再生中・一時停止中・読み込み中は消せない (`cannot remove the current track`)。止まっていれば消せ、今の曲は次の曲に移る。待ち行列の曲が鳴っている間に、その前に鳴ったリストの曲を消しても、次に鳴る曲は変わらない |

`replace` / `play_index` / `enqueue` は、daemon でも再生の準備 (yt-dlp なら数秒) を
待たずに答える。同じ接続の次の要求は、その変更のあとの状態を見る。
読み込み中 (`buffering`) に別の曲を選んだり止めたりしたときは、あとの操作が
勝つ (遅れて準備のできた前の曲や、前の曲のシークが鳴り出すことはない)。
`feed: true` の曲 (ポッドキャストのフィード) は、鳴らすときにエピソードの一覧へ
展開してリストごと置き換える。展開を待つ間は `status` の `buffering` が立ち、その間に
別の曲を選ぶ (`replace`、`play_index`、止まっているときの `enqueue` など) と
展開は取り消される。

#### 添字の確かめ

`play_index` / `remove` / `queue_edit` (`add`/`remove`/`move`) /
`playlist_remove_track` は曲を添字で指すので、GUI の写しが古い (TUI や
Open-Voice がリストを動かした、待ち行列の曲が鳴り始めて位置が詰まった) と
別の曲を操作してしまう。要求に、その添字で見えていた曲の `path` (受け取った
TrackInfo の `path` のまま) を付けると、cliamp はその曲か確かめ、違えば何も
変えずに `{"ok": false, "error": "stale"}` を返す。受け手はリストを取り直して
から選び直すこと (古い添字で送り直さない)。`path` を省けば確かめない (従来どおり)。
パッチの古い cliamp は `path` を無視する。

### カタログ系 (IPC の接続ごとの goroutine で処理。Update を通らない。最大 120 秒)

| コマンド | 要求 | 応答 |
|---|---|---|
| `providers` | — | `providers` |
| `playlists` | `provider` | `playlists` / `needs_auth` (`radio` は TUI の局検索の状態に関わらず、検索していないときと同じ一覧 (`l:` 登録局、`f:` お気に入り、`c:` TUI が読み込んだ Radio Browser の局) を返す) |
| `tracks` | `provider`, `id` | `tracks` / `needs_auth` |
| `search` | `provider` (省略で `youtube`), `query`, `limit` (既定 25、最大 50) | `tracks` |
| `load_provider` | `provider`, `id`, `index`, `name` | `total`, `gen` (`tracks` の結果で `replace` する。`source` は `{provider, id, name}`。`index` が範囲外なら何も替えずに `index out of range`)。プロバイダーに取り直させて添字で選ぶので、GUI は見えている並びがあるときは `replace` で送る (見た後でリストが変わっていても、見た曲を鳴らすため) |
| `lyrics` | `artist`, `title` | `lyrics`, `synced` / `error: "not found"` |
| `history` | `limit` (既定 50) | `tracks` (新しい順、`played_at` 付き) |
| `playlist_add` | `name`, `tracks` | `{ok}` (ローカルのプレイリスト `~/.config/cliamp/playlists/<name>.toml` に追加。無ければ作る。TOML に残る欄は上の「型」を参照。`<name>.toml` が 255 バイト (NAME_MAX) を超える名前は `open …/<name>.toml: file name too long`。GUI は名前 250 バイトまでに抑える) |
| `playlist_delete` | `name` | `{ok}` (無ければ `remove …/<name>.toml: no such file or directory`) |
| `playlist_remove_track` | `name`, `index`, `path` | `{ok}` (`path` は上の「添字の確かめ」)。**最後の曲を外すと、プレイリストのファイルごと消える** (external/local の RemoveTrack)。その後の `tracks` は `open …/<name>.toml: no such file or directory`、`playlists` にも出ない |

ラジオの `.m3u` / `.pls` の中継 URL は、TUI と同じく `tracks` の中で実体の
URL へ展開する。

誤りの文言 (GUI はトーストにそのまま出す): 引数の欠け (`playlists requires a provider`、
`tracks requires a provider and an id`、`lyrics requires an artist or a title`、
`playlist_add requires a name`、`playlist_remove_track requires a name and an index` など)、
`unknown provider: x`、`provider youtube has no playlists`、`invalid playlist name "a/b"`、
`"Recently Played" is a virtual history playlist and cannot be modified`、
`track index N out of range`。tests/test_conformance.py が偽と本物の両方で確かめる。
Spotify の Web API が長い待ちを求めたとき (下) は `… spotify: rate limited by Spotify; retry after 24h0m0s`、
開発者の割り当てを使い切ったとき (下) は `… spotify: Spotify quota exceeded for this developer account; retry after 1h0m0s`。

### Spotify の Web API だけの接続 (`playback: "youtube"`)

cliamp の Spotify は、サインインで得た OAuth のトークンから go-librespot のセッション
(再生用) を作る。Spotify がこの資格情報を **拒んだ** とき、つまり

- login5 の `INVALID_CREDENTIALS` / `UNKNOWN_IDENTIFIER` (自分で登録した Developer アプリの
  client_id のトークンで起きる)、
- アクセスポイントの `BadCredentials` / `PremiumAccountRequired` (Free のアカウントで返ることがある)

のときは、トークンを捨てずに **Web API だけ** で繋ぐ (以前は接続ごと失敗し、`playlists` は
何も返さなかった)。ネットワークの誤り、待ち時間切れ、login5 の `TRY_AGAIN_LATER` /
`TOO_MANY_ATTEMPTS` / `TIMEOUT` / `UNKNOWN_ERROR` などは拒否ではないので、従来どおり
その誤りで失敗する。Premium で librespot が繋がるときの動きと保存の形は変わらない。
見るのはセッションを作る時点の拒否だけ: Free のアカウントでも librespot のセッションが
作れてしまったとき (組み込みの共有 client_id で起きた) は Web API だけの接続にならず、曲は
`spotify:track:` のまま、鳴らすときに従来どおり失敗する。

- 保存: `~/.config/cliamp/spotify_credentials.json` に `web_only: true`、リフレッシュトークン、
  `device_id`、分かれば `user_id` を書く (`username` は空、`data` は null)。次からの起動はブラウザを開かず
  リフレッシュトークンで同じ接続に戻る (librespot は試し直さない。Premium にしたら
  `cliamp spotify reset` してサインインし直す)。Spotify がリフレッシュトークンを断ったら
  (`invalid_grant`、`invalid_client` などトークン窓口の 4xx。408・429・5xx・通信の失敗は一時的な
  ものとして資格情報を残し、次の Web API 呼び出しで取り直す) 資格情報を消して `needs_auth`。
  起動後に断られたときも以後の呼び出しは `needs_auth` になり、TUI のサインインがその接続を
  置き換える。Spotify がリフレッシュトークンを替えたら書き戻す。最初の呼び出しが同時に
  いくつ来ても、保存したリフレッシュトークンで戻すのは 1 回だけ。
- `playlists` / `tracks` / `search` は Web API から取る (「Your Music」= お気に入りの曲、自分の・
  共同編集のプレイリスト)。曲はすべて **YouTube への橋渡し** になる:

  ```json
  {"path": "ytsearch1:Queen David Bowie Under Pressure", "title": "Under Pressure",
   "artist": "Queen, David Bowie", "album": "Hot Space", "year": 1982, "track_number": 11,
   "duration": 248, "meta": {"spotify.id": "2aoo2jlRnM3A0NyLQqMN2f", "spotify.bridge": "youtube"}}
  ```

  `path` は `ytsearch1:` + アーティスト名を空白でつないだもの + 空白 + 曲名 (空白の連なり・
  改行・タブは 1 つの空白にし、空のアーティスト名は飛ばす)。yt-dlp が YouTube の最初の候補を
  鳴らす (ほかの `ytsearch` の曲と同じく `buffering`、yt-dlp のシーク、yt-dlp の誤り)。
  `title` / `artist` (`, ` 区切り) / `album` / `year` / `track_number` / `duration` は Spotify の
  もの (`search` の結果にも `track_number` が付く。Premium の `spotify:track:` の検索結果には
  従来どおり付かない)。`unplayable` は付かない (Spotify の地域制限は YouTube には関係しない)。
  `stream` も付かない。Spotify の曲の ID が要るときは
  `meta` の `spotify.id` を使う (TUI の「Spotify のプレイリストへ追加」もそうする)。
- `search` は Premium の接続と同じく、下の「Spotify の Development Mode の規則」どおり 10 件ずつの
  頁をつなぐ (自分で登録した Development Mode のアプリでも検索できる。以前の文言
  `spotify: search blocked — …` はもう出ない)。
- `spotify:track:` の曲は鳴らせない (上の「再生の失敗」の `spotify: streaming unavailable`)。

開発者アプリ (client_id) の持ち主が Premium でないと、Spotify の Web API はどの呼び出しにも 403
`{"error": {"status": 403, "message": "Active premium subscription required for the owner of the app"}}` を
返す (2026-09 の実測。無料プランのアカウントで作ったアプリ。Premium にしてから Spotify が気づくまで
数時間かかることがある)。cliamp は本文をそのまま包むので、`playlists` は
`spotify: your music: http status 403 Forbidden: {…}`、`search` は `spotify: search: http status 403 Forbidden:
{…}`、`tracks` は `spotify: list tracks: http status 403 Forbidden: {…}` になる (上流の `tracks` はどの 403 も
`spotify: playlist not accessible: only playlists you own or collaborate on can be loaded` に言い換えて本文を
消していた。パッチはこの 403 だけは言い換えない。ほかの 403 は従来どおり `playlist not accessible`)。
GUI は "Active premium subscription required for the owner of the app" を含む Spotify の誤りを日本語に言い直し、
公開プレイリストの取り込み (下の「GUI 側の約束」) を勧める (偽の cliamp は `--spotify-owner-premium` でこの形を返す)。

Web API の 429 (待ってほしい): `Retry-After` が 30 秒以下なら待って繰り返す (最大 8 回。
`Retry-After` が無ければ 1, 2, 4 … 秒、30 秒で頭打ち)。30 秒を越える待ちを求められたら
待たずに `spotify: rate limited by Spotify; retry after <長さ>` (Go の time.Duration の書き方、
例 `24h0m0s`) で失敗する。`playlists` なら `spotify: your music: spotify: rate limited by Spotify;
retry after 24h0m0s` のように前に文脈が付く。Premium の接続でも同じ (以前は 24 時間待ち続け、
要求が 120 秒で切れるまで返らなかった)。

ただし 429 の本文が `{"error": {…, "reason": "QUOTA_EXCEEDED"}}` のとき (2026 年 7 月から。同じ開発者の
Development Mode のアプリはすべて 1 つの割り当てを分け合い、それを使い切った) は、待っても直らないので
`Retry-After` の長さに関わらず繰り返さず、`spotify: Spotify quota exceeded for this developer account; retry
after <長さ>` (`Retry-After` が無いか読めなければ `; retry after …` の無い
`spotify: Spotify quota exceeded for this developer account`) で失敗する。前に文脈が付くのは上と同じ
(`spotify: search: spotify: Spotify quota exceeded …`)。`reason` が無い・ほかの値・JSON でない本文の 429 は
上の普通の回数の制限として扱う。

### Spotify の Development Mode の規則 (2026 年 2 月から)

自分で登録した client_id (Development Mode のアプリ) には、Spotify が 2026 年 2 月 (新しいアプリは 2 月 11 日、
既存のアプリは 3 月 9 日) から次の規則を当てている。パッチはこれに合わせる (Premium の接続でも Web API だけの
接続でも同じ):

- `/v1/search` の `limit` は 10 まで (既定は 5、越えると 400 `Invalid limit`)。`search` は `offset` を 0, 10,
  20 … と進めて 10 件ずつ (最後は残りの数) 求め、`limit` (1〜50 に丸める) 件までつなぐ。`limit` 25 (`search` の
  既定) なら 10・10・5 の 3 回、TUI の Ctrl+F (20 件) なら 2 回。求めた数より短い頁が来るか、Spotify の
  `total` に達したらそこで止める。同じ曲 (Spotify の ID) は最初の 1 つだけ残し、順は Spotify のまま (頁を
  足して埋め合わせはしないので、件数は `limit` より少ないことがある)。2 頁目以降が失敗したらそれまでの結果を
  返す (誤りにしない。cliamp のログに警告が残る)。この短い結果は完全な結果と見分けられない (応答に印は
  無い)。ただし、しばらくどの呼び出しも同じに断られる誤り (上の利用枠の使い切り・30 秒を越える回数の制限・
  サインイン切れ (`needs_auth`)・持ち主が Premium でない 403) は、2 頁目以降でもその誤りで返す (GUI が
  成功として 10 分覚えたり、利用枠の断りを覚え損ねたりしないように)。1 頁目が失敗したらその誤り。結果の形 (Web API だけの接続の
  橋渡し、Premium の `spotify:track:`) は変わらない。
- 上流は 400 `Invalid limit` を「client_id が新しすぎて検索が塞がれている」(`spotify: search blocked — your
  client_id is too new …`) と言い換えていたが、原因は件数だった (TUI は 20 件を 1 回で求めていた)。この文言は
  もう出ない。それでも `Invalid limit` が返ったら (Spotify が上限をさらに下げたとき)
  `spotify: search: Spotify refused a page of 10 results (cliamp asks for at most 10 per request, the limit for
  Development Mode apps since February 2026; Spotify may have lowered it again): http status 400 Bad Request: {…}`
  (偽の cliamp は `--spotify-search-refused` で Spotify の検索だけをこの形で断る)。
  サインインしたアカウントがアプリの利用者に登録されていない 403 (`… the user may not be registered.`) は
  `spotify: search: this Spotify account is not a user of the Developer app (add it under User Management at
  developer.spotify.com/dashboard): http status 403 Forbidden: {…}`。ほかの誤りは従来どおり `spotify: search: …`。
- Spotify のプレイリストへの追加は `POST /v1/playlists/{id}/items` (`{"uris": […]}`)、作成は
  `POST /v1/me/playlists` (`{"name": …, "public": false}`。利用者 ID を `/v1/me` に尋ねなくなった) を使う。
  取り除かれた `POST /v1/playlists/{id}/tracks` と `POST /v1/users/{id}/playlists` は使わない。使うのは TUI の
  「Spotify のプレイリストへ追加」「新しいプレイリスト」だけで、IPC のコマンドは Spotify のプレイリストを
  書き換えない (`playlist_add` はローカルのプレイリスト)。
- ほかの呼び出し (`GET /v1/me` の `id`、`GET /v1/me/tracks`、`GET /v1/me/playlists`、
  `GET /v1/playlists/{id}/items`) はこの変更の影響を受けない。プレイリストの中身が読めるのは自分の・共同編集の
  ものだけ (従来どおり、それ以外は `playlists` に出さない)。

### 既存の `device` (出力先)

`device list` の `device` は改行区切りの PulseAudio / PipeWire の sink 名
(`alsa_output.pci-….analog-stereo`、`bluez_output.…` など。`pactl list sinks` の Name)。
先頭の `* ` は **既定の sink** の印で、cliamp の流れの行き先ではない (切り替えは
`pactl move-sink-input` で流れを動かし、既定の sink は変えないため)。切り替えには sink 名を
送る。一覧と切り替えには cliamp の PATH に `pactl` が要る (無いと
`list devices: pactl: exec: "pactl": executable file not found in $PATH`。cliamp.nix が
pulseaudio の pactl を足す)。GUI は説明の無い sink 名から見出しを作り (アナログ出力・
HDMI / DisplayPort・Bluetooth など)、印は GUI で選んだ出力先を優先する。応答に説明付きの
`devices` 配列 (`[{"name", "description", "active"}]`) があればその説明を使う
(いまのパッチは返さない)。

### daemon (`--daemon`) での扱い

状態系はすべて daemon でも同じ意味で動く。daemon に無い機能 (履歴の記録、
Lua、可視化) に依存するものは `{ok:false, error:"not supported in daemon mode"}`
を返す (待ち時間切れにしない)。カタログ系は daemon でも同じ実装を使う。
daemon の `seek_to` もすぐ答え、シークしている間の `status` の `position` は
シーク先を返す。続けて送った `seek_to` は最後のシーク先に落ち着く。

## GUI 側の約束

- 状態の問い合わせ (`status`) は専用の接続で行い、カタログ系の長い要求と
  同じ接続に並べない (1 本の接続の要求は順番に処理されるため)。
- cliamp は接続ごとの goroutine で要求を処理するので、別々の接続で続けて送った要求は
  届く順が入れ替わる。GUI は状態を変える要求 (toggle / next / seek_to / queue_edit /
  replace など) を 1 本の列で、前の応答を待ってから順に送る。読み取りとカタログ系は
  別の列で並べて送る (遅い検索の後ろに一時停止を並ばせない)。
- 既存の `seek` (相対) は yt-dlp の曲で TUI を数秒止めるので使わない。`seek_to` を使う。
- 既存の `volume` は **絶対値** (dB、-30〜+6)。ヘルプの「adjust」は誤り。
- TUI では `play` は停止中に何もしない。停止中の再生は `toggle` を送る。
- GUI は MPRIS の名前を取らない (Open-Voice が MPRIS のプレイヤー数を前提に
  一時停止と再開をしているため)。
- 「Spotify から取り込む」(GUI だけの機能。パッチは関わらない): GUI は Spotify の公開の埋め込み用の頁から
  プレイリスト・アルバムの曲を読み、Web API だけの接続と同じ橋渡しの形 (`ytsearch1:…`、meta の `spotify.id` と
  `spotify.bridge`) で `playlist_add` する。ローカルのプレイリストの TOML には meta が残らないので、GUI は
  橋渡しの path → Spotify の曲 ID の表を自分で持ち、読み戻した曲 (`tracks`・`playlist`・`status`・`history`) に
  meta を付け直して扱う。置き換え (「Spotify から更新」) は `playlist_delete` のあと `playlist_add` (cliamp に
  置き換えの命令は無い。足せなければ前の曲を `playlist_add` で戻し、戻せなければ GUI の状態のディレクトリに控える)。
