# Codex マルチプロバイダ・セッション再開:根本原因と解決策

[English](METHODOLOGY.md) | [简体中文](METHODOLOGY.zh-CN.md) | [日本語](METHODOLOGY.ja.md)

**CC Switch(ccs)** のようなプロバイダ切替プロキシーを使う Codex CLI / Desktop で発生した
3 つの問題 —— 原因・診断方法・修復手段 —— の完全なポストモーテムです。すべて Windows 11 +
Codex Desktop / CLI(0.155〜0.158)の実機で検証し、可能な限り openai/codex のソースレベルで
根拠を示しています。

> プライバシー注記: 本文書・コード・テストには、実際のセッション内容・スレッド ID・ユーザー
> メッセージ・個人パスは含まれていません。機密原文はプライベートバックアップに封印しています。

---

## 0. 背景:マルチプロバイダ・リバースプロキシー下のセッション保存構造

ccs 経由では Codex は常にローカルプロキシーとだけ通信します。`config.toml` には
`model_provider = "custom"`(`base_url = http://127.0.0.1:<port>/v1`)が 1 つあるだけです。
プロバイダの切替 = ccs が config を書き換える(またはプロキシーが上流を切替える)ことですが、
セッション履歴は 3 箇所に存在します:

| ストレージ | 内容 | 重要フィールド |
|---|---|---|
| `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | セッションの生ストリーム(session_meta / turn_context / response_item / event_msg) | `model_provider`、`history_base` |
| `~/.codex/state_5.sqlite`(`~/.codex/sqlite/` 以下にもう 1 セット) | スレッドインデックス、`threads` テーブル | `threads.model_provider`、`threads.rollout_path`、`threads.history_mode` |
| `~/.codex/thread_history_1.sqlite` | rollout の**投影キャッシュ**(thread_turns / thread_items / thread_history_projection_state) | `next_rollout_byte_offset`、`next_rollout_ordinal` |

一覧フィルタの真のデータソースは **`threads.model_provider`** です(`list_threads_db`、
`idx_threads_provider` インデックス付き)。rollout 側の `model_provider` は ID 指定 resume の
検証や doctor 系ツールの統計に影響します。**両方のストアを一緒に更新する必要があります。
片方だけの変更は問題を起こします。**

---

## 問題 1:プロバイダ切替後、古いセッションが一覧から「消える」

**症状**: `codex resume` セレクタや Desktop サイドバーに、切替前に作成したセッションが一切
表示されない。ただし `sessions/` のデータは完全無事。

**根本原因**: 一覧は `threads.model_provider` でフィルタされ、現在の config に一致する行のみを
表示します。古いセッションは旧所属(openai)を持ったままなので、一括で除外されます。
上流: openai/codex issue **#15494、#31625**。

**解決策(migrate v2)**:
1. **等長バイト書き換え**: 生バイト単位で行ごとに処理し、`model_provider` の値だけを置換。
   旧新の名前が同長(`openai`→`custom`、ともに 6 バイト)ならすべてのバイトオフセットは不変。
   新名が短ければ行末に空白を詰めて等長化(JSONL の行末空白は合法)。長くなる場合のみ
   投影リセットにフォールバック。
2. **SQLite 同期**: 両方の state DB の `threads` テーブルを thread id で正確に UPDATE
   (アーカイブ行は触らない)。事前に SQLite backup API で一貫性バックアップ
   (`.pre-doctor-migrate-*.bak`)を取得。
3. **バイト単位の細部が成否を分ける**: 未変更の行(不正 UTF-8、`\r\n` 行末、末尾改行の有無)は
   そのまま保存。Windows のテキストモード書き込みはすべての `\n` を `\r\n` に変えてしまうので
   (+1 バイト/行)`write_bytes` を使うこと。

---

## 問題 2:古いセッションの再開が 400 `invalid_encrypted_content` で失敗

**症状**: セッションは見えるようになったが、resume 後の最初のメッセージが
`プロキシー → ゲートウェイ(litellm) → Azure` から 400 で拒否される:
*The encrypted content for item rs_… could not be verified… Encrypted content is tied to the
organization that created it.*

**根本原因**: サーバー側ストレージなしモード(`disable_response_storage` / ZDR)では、reasoning は
`encrypted_content` の暗号文として応答に含まれ、rollout に書き込まれます。**暗号文は生成した
組織/デプロイに紐付きます。** プロバイダを切替えると、resume リクエストは旧暗号文をそのまま
新しい上流へ送り、復号できず 400 になります。Codex 側の `Reasoning.encrypted_content` は
もともと `Option<String>` で、ソースは「平文 reasoning」を明示的にサポートしています
(`history.rs`: plaintext reasoning excluded from replay accounting)—— **剥離は完全に合法です**。

**解決策(strip v2)と二つの罠**:
1. **書き換えは等長でなければならない**: フィールドや行を削除するとファイルが短くなり、
   `paginated` 血縁の各継続 rollout は `session_meta.history_base.end_byte_offset` に親ファイルから
   引き継いだバイト位置を記録しています —— 親が縮むと子のオフセットが EOF を超え、resume は
   `invalid paginated history lineage` で失敗します(400 より悪い)。正しい手法: 
   `"encrypted_content":"…"` / `"id":"rs_…"` フィールド(と隣接カンマ)を外科的に削除し、
   **行末に空白を詰めて元のバイト長に戻す**。書き換えた各行は再パースして検証し、
   不合格ならそのまま保持。
2. **`rs_` 接頭辞の id も削除**: サーバーが id で旧暗号文を検索するのを防ぐ。プロトコル上も
   id は省略可能です。
3. **上流の受容性は実測が必要**: 剥離後の reasoning 項は「裸」ですが、litellm→Azure は実機で
   受容しました(再開成功)。compaction 項の暗号文は必須構造のため自動剥離できません ——
   報告のみ。
4. **投影をリセットしてキャッシュを掃除**: 等長書き換えでも `thread_items.item_json` キャッシュには
   旧暗号文のコピーが残っています。公式 `rollout_migration.rs:1027-1030` の手法に従い、
   スレッドごとに 4 テーブルの行を削除して Codex に全体を再投影させます
   (リセットは公式メカニズムでリスクゼロ)。

---

## 問題 3(事件ポストモーテム):セッションファイルが「同サイズの全ゼロ」に

**症状**: 47 個の rollout ファイルが**元のバイト数のまま** 100% `\x00` になり、**mtime は不変**。
バックアップディレクトリ内の同じ 47 ファイルもゼロ化。Windows イベントログにはストレージ
エラーが一切ありません。

**根本原因**(openai/codex issue **#26421** と同一パターン。Codex 自身の config.toml も
ゼロ埋めされた実例あり): NTFS 上の `open(O_TRUNC) → write → close`(FlushFileBuffers なし)——
**メタデータ(サイズ/mtime)が先にディスクへ落ち、データはページキャッシュに残留**します。
遅延書き戻しが失われると、ディスクには「割り当て済みだが未書き込み」のゼロブロックだけが残ります。
事件のタイムライン: 11:33 非アトミックな一括書き換え(データはページキャッシュへ)→
12:02:28 スキャンはページキャッシュから完全な内容を読む → 12:02:29-44 ファイルごとのバックアップは
ディスク上の実態(ゼロ)を読む → データは静かに失われました。

**予防(アトミック書き込み)**: すべての書き込みパスは `tmp ファイル + fsync + os.replace` を
使用 —— データは「完全に旧」か「完全に新」のどちらかで、中間状態を排除します。これこそ
コミュニティが Codex 上流に提案した修復方法です(issue は未だオープン)。

**3 段階の復元**(実事件で 47/47 すべて回収):
1. **ファイル単位バックアップからのロールバック**: ツールは書き換えの前に必ず元のディレクトリ構造で
   スナップショットを取ります(`sessions_backup_*/`)。
2. **投影キャッシュからの再構築**: バックアップに入らなかったファイルでも、
   `thread_items.item_json` に会話の投影が残っていることが多い(投影状態の
   `next_rollout_byte_offset` と元ファイルサイズを比べれば、100% 被覆も判定できます ——
   ゼロ埋めはサイズを保存してくれるので)。ordinal 順に会話本体を再構築し、血縁を統合する際は
   item id で重複排除、継続の ordinal はスレッド全体で通し番号になっていることに注意。
3. **ゼロファイルは物差し**: サイズが保存されているので、完全再構築に必要な投影被覆率を
   正確に判定できます。

---

## ベストプラクティス(傷の数だけ得たもの)

1. **等長バイトは絶対条件**: rollout の書き換えは等値置換か空白詰めのどちらか ——
   `history_base` も投影オフセットも `thread_turns.rollout_byte_offset` もバイト位置に依存しています。
2. **アトミック書き込みは絶対条件**: `tmp + fsync + os.replace`。特に Windows/NTFS では。
3. **触る前にバックアップ**: ファイル単位と SQLite 一貫性コピーの両方。バックアップ動作自体も
   アトミックに。
4. **ファイル書き換え前に Codex を完全終了**(CLI/Desktop)。二重書き込みの競合と投影の
   不整合を避けるため。
5. **SQLite と rollout は二つの帳簿**: 一覧フィルタは threads テーブル、resume 検証は rollout を
   読みます —— 両方更新すること。
6. **冪等に設計**: 再実行で変更ゼロ。毎回復元可能なバックアップを残す。
7. **デフォルトは読み取り専用**: 診断と変更を分離し、変更には明示的フラグ(`--yes`)を要求。

## 付録:上流の参考文献

- openai/codex issue **#15494 / #31625**: セッション一覧の provider フィルタ(問題 1)
- openai/codex issue **#26421**: 非アトミック書き込みによるファイルのゼロ埋め(問題 3)
- ソースの要点(2026-09 main): `state/migrations/0001_threads.sql`、
  `rollout/src/state_db.rs`(list_threads_db / read_repair)、
  `thread-store/src/local/thread_history.rs`(投影オフセット検証)、
  `core/src/context_manager/history.rs`(平文 reasoning の合法性)、
  `rollout/src/rollout_migration.rs:1027-1030`(公式の投影リセット)
