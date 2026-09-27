# Codex multi-provider session continuation: root causes and solutions

[English](METHODOLOGY.md) | [简体中文](METHODOLOGY.zh-CN.md) | [日本語](METHODOLOGY.ja.md)

A full post-mortem of three problems that hit Codex CLI / Desktop when provider-switching proxy
tools such as **CC Switch (ccs)** are used: what causes them, how to diagnose, and how to fix.
Everything below was verified on Windows 11 with Codex Desktop / CLI (0.155–0.158), with
source-level references to openai/codex wherever possible.

> Privacy note: this document, the code, and the tests contain no real session content, thread
> IDs, user messages, or personal paths. The sensitive originals are sealed in a private backup.

---

## 0. Background: session storage under a multi-provider reverse proxy

With ccs, Codex only ever talks to a local proxy: `config.toml` contains a single
`model_provider = "custom"` (`base_url = http://127.0.0.1:<port>/v1`). Switching providers =
ccs rewrites the config (or the proxy switches upstream), but session history lives in three places:

| Storage | Contents | Key fields |
|---|---|---|
| `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | Raw session stream (session_meta / turn_context / response_item / event_msg) | `model_provider`, `history_base` |
| `~/.codex/state_5.sqlite` (plus a second set under `~/.codex/sqlite/`) | Thread index, `threads` table | `threads.model_provider`, `threads.rollout_path`, `threads.history_mode` |
| `~/.codex/thread_history_1.sqlite` | **Projection cache** of the rollouts (thread_turns / thread_items / thread_history_projection_state) | `next_rollout_byte_offset`, `next_rollout_ordinal` |

The real data source for list filtering is **`threads.model_provider`** (`list_threads_db`, with
an `idx_threads_provider` index); the `model_provider` inside rollout files affects ID-based
resume validation and the statistics of doctor-style tools. **Both stores must be updated
together — changing only one causes trouble.**

---

## Problem 1: after a provider switch, old sessions "disappear" from the list

**Symptom**: the `codex resume` picker and the Desktop sidebar no longer show any session created
before the switch, while `sessions/` is fully intact.

**Root cause**: the list is filtered by `threads.model_provider` and only shows rows matching the
current config. Old sessions still carry the old attribution (openai) while the config now says
custom → the whole batch is filtered out. Upstream: openai/codex issues **#15494, #31625**.

**Solution (migrate v2)**:
1. **Byte-length-preserving rollout rewrite**: process the file line by line at the raw-byte
   level and replace only the `model_provider` value. If the old and new names are the same
   length (`openai`→`custom`, 6 bytes each), every byte offset stays valid; if the new name is
   shorter, pad the line with trailing spaces (trailing whitespace is legal in JSONL); only a
   longer name falls back to a projection reset.
2. **Sync SQLite**: `UPDATE` the `threads` table by thread id in *both* state databases
   (archived rows untouched); take a SQLite-consistent backup first
   (`.pre-doctor-migrate-*.bak`).
3. **Byte-level details decide success or failure**: untouched lines (invalid UTF-8, `\r\n`
   endings, missing trailing newline) must be preserved verbatim; Windows text-mode writes turn
   every `\n` into `\r\n` (+1 byte per line) — use `write_bytes`.

---

## Problem 2: resuming an old session fails with 400 `invalid_encrypted_content`

**Symptom**: sessions are visible again, but the first message after resume is rejected with a
400 from `proxy → gateway (litellm) → Azure`: *The encrypted content for item rs_… could not be
verified… Encrypted content is tied to the organization that created it.*

**Root cause**: in no-server-storage mode (`disable_response_storage` / ZDR), reasoning is
returned as `encrypted_content` ciphertext and written into the rollout. **The ciphertext is bound
to the organization/deployment that created it.** After a provider switch, the resume request
sends the old ciphertext to the new upstream, which cannot decrypt it → 400. On the Codex side
`Reasoning.encrypted_content` is an `Option<String>`, and the source explicitly supports
"plaintext reasoning" (`history.rs`: plaintext reasoning excluded from replay accounting) —
**stripping the ciphertext is fully legal**.

**Solution (strip v2) and two pitfalls**:
1. **The rewrite must be byte-length-preserving**: deleting fields or lines shortens the file,
   and every continuation rollout of a `paginated` lineage records the byte cutoff it inherited
   in `session_meta.history_base.end_byte_offset` — once a parent shrinks, the child's offset
   passes EOF and resume fails with `invalid paginated history lineage` (worse than the 400).
   Correct approach: surgically delete `"encrypted_content":"…"` / `"id":"rs_…"` (and the
   adjacent comma), then **pad the line with trailing spaces to its original byte length**;
   re-parse every rewritten line for validation and keep any line that fails verbatim.
2. **Also drop the `rs_`-prefixed id**: prevents the server from looking up the old ciphertext by
   id; the id is optional in the protocol too.
3. **Upstream tolerance must be measured**: after stripping, reasoning items are "bare";
   litellm→Azure accepted them on real hardware (resumes pass). A compaction item's ciphertext is
   a required structure and cannot be auto-stripped — report only.
4. **Reset the projection to purge the cache**: even with byte-preserving rewrites, the
   `thread_items.item_json` cache still holds old ciphertext copies; delete the four table rows
   per thread using the official recipe from `rollout_migration.rs:1027-1030` (the reset is an
   official mechanism and risk-free).

---

## Problem 3 (incident post-mortem): session files turn into "same-size all-zero"

**Symptom**: 47 rollout files became 100% `\x00` at their **original byte size**, with **mtime
unchanged**; the same 47 files inside a backup directory were zeroed as well; the Windows event
log contains no storage errors at all.

**Root cause** (same pattern as openai/codex issue **#26421**, where Codex's own config.toml was
zero-filled): `open(O_TRUNC) → write → close` (no FlushFileBuffers) on NTFS — **metadata (size/
mtime) reaches disk first while data lingers in the page cache**; if the lazy write-back is lost,
the disk keeps only "allocated but never written" zero blocks. Incident timeline: 11:33 bulk
non-atomic rewrite (data lands in the page cache) → 12:02:28 a scan reads the full content from
the page cache → 12:02:29-44 per-file backups read the on-disk reality (zeros) → data silently
lost.

**Prevention (atomic writes)**: every write path uses `tmp file + fsync + os.replace` — data is
either fully old or fully new, eliminating the intermediate state. This is exactly the fix the
community suggested to Codex upstream (issue still open).

**Three-step recovery** (all 47 files recovered in the real incident):
1. **File-level backup rollback**: the tool snapshots the original directory structure before
   every rewrite (`sessions_backup_*/`);
2. **Rebuild from the projection cache**: files that never made it into a backup can often be
   recovered from `thread_items.item_json` (sometimes at 100% coverage — compare
   `projection_state.next_rollout_byte_offset` with the file's original size, which zero-fill
   conveniently preserves). Rebuild the conversation backbone ordered by ordinal; when merging a
   lineage, dedupe by item id and note that continuation ordinals continue the thread-wide
   numbering;
3. **The zero file is a measuring stick**: because size is preserved, it tells you exactly how
   much coverage the projection must have for a full rebuild.

---

## Best practices (paid for in scars)

1. **Byte-length preservation is non-negotiable**: any rollout rewrite must be an equal-value
   replacement or space-padded — `history_base`, projection offsets, and
   `thread_turns.rollout_byte_offset` all hang on byte positions.
2. **Atomic writes are non-negotiable**: `tmp + fsync + os.replace`, especially on Windows/NTFS.
3. **Back up before touching anything**: both file-level and SQLite-consistent copies, and the
   backup itself must be atomic.
4. **Fully exit Codex before rewriting files** (CLI/Desktop), to avoid racing writers and
   misaligned projections.
5. **SQLite and rollout are two ledgers**: list filtering reads the threads table, resume
   validation reads the rollout — update both.
6. **Design for idempotence**: rerunning changes nothing; every run leaves a restorable backup.
7. **Read-only by default**: separate diagnosis from mutation; mutations require an explicit
   flag (`--yes`).

## Appendix: upstream references

- openai/codex issues **#15494 / #31625**: session list filtered by provider (problem 1)
- openai/codex issue **#26421**: non-atomic write zero-fills files (problem 3)
- Key source points (2026-09 main): `state/migrations/0001_threads.sql`,
  `rollout/src/state_db.rs` (list_threads_db / read_repair),
  `thread-store/src/local/thread_history.rs` (projection offset validation),
  `core/src/context_manager/history.rs` (legality of plaintext reasoning),
  `rollout/src/rollout_migration.rs:1027-1030` (official projection reset)
