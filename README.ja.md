# codex-session-doctor

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

Codex CLI / Desktop における**「プロバイダ切替後に古いセッションが一覧から見えなくなる・再開できない」**問題の診断 / 修復ツール。

> 根本原因と解決策の詳細は **[METHODOLOGY.ja.md](METHODOLOGY.ja.md)** を参照。
> テスト: `python tests/test_doctor_v2.py` / `test_rebuild.py` / `test_retag.py`(サードパーティ依存なし)。

## 背景

Codex のセッション一覧(`codex resume` のセレクタ、Desktop のスレッドサイドバー)は、各セッションファイルに
記録された `model_provider` でフィルタリングされ、`~/.codex/config.toml` の現在の `model_provider` と
一致するものだけを表示します(上流で確認済み: openai/codex issue #15494、#31625)。

ccs / CC Switch などのプロキシーツールは `config.toml` の `model_provider` を書き換えてプロバイダを
切り替えるため、切替前のセッションは一覧から「消えます」。ただし rollout ファイルは
`~/.codex/sessions/` 以下に完全に残っています。**データは失われていません。**

このツールが行うこと:

1. **診断**: すべてのセッションファイルを走査し、`model_provider` ごとにグループ化。現在非表示に
   なっているセッションとその最初のユーザーメッセージを一覧表示し、データが無事であることを確認;
2. **移行**: 古いセッションの provider 所属を現在の provider に書き換え、一覧に再表示
   (書き換え前に自動バックアップ);
3. **ロールバック**: 任意のバックアップからワンコマンドで復元。

## 使い方

```bash
python3 codex_session_doctor.py                              # 読み取り専用診断(デフォルト)
python3 codex_session_doctor.py --migrate <旧provider>       # 所属を移行(自動バックアップ)
python3 codex_session_doctor.py --migrate <旧provider> --yes # 確認をスキップして移行
python3 codex_session_doctor.py --strip-encrypted            # 他プロバイダ由来の暗号化 reasoning を等長バイトで剥離(自動バックアップ)
python3 codex_session_doctor.py --restore-backup <バックアップdir> # 移行をロールバック
```

`--codex-home <path>` または環境変数 `CODEX_HOME` で Codex ディレクトリを指定できます
(デフォルト `~/.codex`)。

## 安全設計

- **デフォルトは読み取り専用**: 引数なしだと走査と表示のみで、ファイルは一切変更しません
- **移行前の自動バックアップ**: セッションは `~/.codex/sessions_backup_<timestamp>/` へ、
  SQLite インデックスは `*.pre-doctor-migrate-<timestamp>.bak` として退避
- **SQLite インデックス同期**: `--migrate` は `threads` テーブルの `model_provider` も更新します
  (一覧フィルタの真のデータソース。上流根拠: `state/migrations/0001_threads.sql`)。provider 名の
  長さ変化でバイトオフセットがずれる場合は、公式 `rollout_migration.rs` の手法で投影テーブル
  (`thread_history_projection_state` 等)をリセットし、Codex が次回オープン時に全体を再投影します
- **等長バイト書き換え**: 生バイト単位で行ごとに処理。`openai`→`custom`(いずれも 6 バイト)なら
  すべてのバイトオフセットは不変。新しい provider 名が短い場合は行末に空白を詰めて元の長さに合わせます。
  未変更の行(不正 UTF-8、改行スタイル、末尾改行の有無)はそのまま保存
- **`--strip-encrypted` 等長剥離**: `encrypted_content` / `rs_` id フィールド(と隣接カンマ)を
  外科的に削除し、行末に空白を詰めて元のバイト長に戻します —— `paginated` 血縁スレッドの
  `history_base` オフセットも投影オフセットもすべて不変(v1 の行削除は血縁を壊したため廃止)。
  書き換えた各行は再パースで検証し、不合格ならそのまま保持
- **アトミック書き込み**: すべての書き込みパスは `tmp + fsync + os.replace` を使用し、NTFS での
  `open-truncate → write → close` の窓を排除。この窓により 2026-09-27 に 47 個のセッションファイルが
  「同サイズの全ゼロ」になりました —— openai/codex issue #26421(Codex 自身の config.toml でも発生)
  と同一の失敗モードです
- **ワンコマンドロールバック**: `--restore-backup` で任意の移行を復元
- 1 ファイル 50 MB の処理上限。超大ファイルは保護的に切り捨て

## 付属ツール: rebuild_from_projection.py

破損・ゼロ埋めされたセッションファイルを、SQLite 投影キャッシュ(`thread_items.item_json`)から
再開可能な rollout として再構築します:

```bash
python3 rebuild_from_projection.py <salvage-dir>                  # ドライラン: recovery_rebuilt/ に出力(Markdown アーカイブ含む)
python3 rebuild_from_projection.py <salvage-dir> --install --yes  # 確認後に live へ書き込み(原本隔離 + 投影リセット + バックアップ)
```

- 会話の本体(ユーザー / アシスタントメッセージ)を再構築し、ツール呼び出しの詳細は Markdown
  アーカイブへ。`paginated` の複数ファイル血縁は単一の legacy モードファイルに統合
- session_meta に `model_provider` を書き込みます(CLI の resume セレクタや doctor の統計は
  rollout 自身を読むため、フィールド欠落は「(未記録)」表示になります)
- こちらもアトミック書き込み。`--install` は**すべての**全ゼロソースファイルを
  `*.zeroed-quarantine-<timestamp>` として隔離します(統合スレッドのルート / 継続血縁ファイルを含む。
  非ゼロのファイルは表示のみで触りません)。SQLite もバックアップ

## 付属ツール: retag_provider.py

session_meta に `model_provider` を持たない既存 rollout へのバックフィル用(新しい Codex の
マイグレーションが書き換えたファイルはこのフィールドを記録しなくなります):

```bash
python3 retag_provider.py --codex-home ~/.codex --provider custom <files...>  # ファイル指定
python3 retag_provider.py --codex-home ~/.codex --from-threads               # threads テーブルで自動ペア(デフォルトはドライラン)
```

session_meta 行のみを書き換え、他のバイトは一切触りません。行長が変わった場合はそのスレッドの
投影をリセット。デフォルトはドライランで、`--yes` で確定。統計だけなら不要 —— doctor には
threads テーブルによるフォールバックが組み込まれています。

## 障害記録(2026-09-27 ゼロ埋め事件)

**現象**: 47 個のセッションファイルが元のバイト数のまますべて `\x00` に変化(mtime 不変)。
42 個はバックアップから復元、5 個は投影キャッシュから再構築(`rebuild_from_projection.py` の
実戦成果)。

**根本原因**(openai/codex issue #26421 と同一パターン。Codex 自身の config.toml も被害例あり):
`open(O_TRUNC) → write → close`(FlushFileBuffers なし)× NTFS の遅延書き戻し = メタデータ
(サイズ/mtime)が先にディスクへ、データはページキャッシュに残留。書き戻しが失われると、
ディスクには「割り当て済みだが未書き込み」のゼロブロックだけが残ります。本件の露出源は
同日早朝に行われた非アトミックな一括書き換えでした。

**教訓(すべてツールに組み込み済み)**:
1. すべての書き込みパスは `tmp + fsync + アトミック置換` —— データは「完全に旧」か「完全に新」;
2. ユーザーデータの書き換え前にはファイル単位バックアップ(`sessions_backup_*`)と
   SQLite 一貫性コピー(`.bak`)の両方を確保;
3. セッションファイルの一括書き換え前にはアプリを完全終了し、二重書き込みの競合を回避;
4. 診断は「メタデータあり・データ破損」に耐えること(doctor はパース失敗行をスキップし、
   クラッシュしません)。

**フォレンジック備忘**: Windows では `fsutil usn readjournal C:`(管理者)で USN 記録を調べて
書き込み元を特定できます。`chkdsk C: /scan` でボリューム健全性を確認。

## 要件

- Python 3.8+(3.11+ は TOML をネイティブ解析。旧バージョンは正規表現パーサーにフォールバック)
- サードパーティ依存なし。各ツールは単一ファイル

## 既知の制限

- `--migrate` / `--strip-encrypted` の実行前には Codex(CLI/Desktop)を**完全に終了**してください。
  さもないと SQLite がロックされることがあります(ツールはスキップして警告します)
- `archived_sessions/` は診断統計のみに参加し、移行対象外。threads テーブルのアーカイブ行の
  provider は元の値を保持
- `--strip-encrypted` が扱うのは reasoning 項のみ。compaction 項の `encrypted_content` は必須
  構造のため、報告のみで自動処理しません
- 剥離後の再開チャットには旧プロバイダの暗号化 reasoning 履歴が付与されません
  (ユーザー / アシスタントメッセージは完全に保持され、会話のつながりには影響しません)。上流が
  暗号化なしの裸 reasoning 項を拒否する場合は依然エラーになります —— ロールバックして
  エラー本文を報告してください
- 移行が書き換えるのは rollout JSONL 内の `model_provider` 所属記録のみで、セッション内容自体は
  一切変更しません
- SQLite インデックスを持たない非常に古い Codex では、再起動後に rollout ファイルを直接読み直すため
  同様に有効です
