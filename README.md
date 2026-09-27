# codex-session-doctor

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

Diagnose / repair **"old sessions invisible or unresumable after switching providers"** in Codex CLI / Desktop.

> Full root-cause analysis and solutions: **[METHODOLOGY.md](METHODOLOGY.md)**
> Tests: `python tests/test_doctor_v2.py` / `test_rebuild.py` / `test_retag.py` (no third-party dependencies).

## Background

Codex's session list (`codex resume` picker, Desktop thread sidebar) filters sessions by the
`model_provider` recorded in each session file, showing only sessions that match the current
`model_provider` in `~/.codex/config.toml` (confirmed upstream: openai/codex issues #15494, #31625).

Proxy tools such as ccs / CC Switch switch providers by rewriting `model_provider` in
`config.toml`. Old sessions then "disappear" from the list — while the rollout files remain
fully intact under `~/.codex/sessions/`. **No data is lost.**

What this tool does:

1. **Diagnose**: scan all session files, group them by `model_provider`, list which sessions
   are currently hidden together with their first user message, and confirm the data is intact;
2. **Migrate**: rewrite old sessions' provider attribution to the current provider so they
   reappear in the list (automatic backup before rewriting);
3. **Rollback**: restore from any backup with a single command.

## Usage

```bash
python3 codex_session_doctor.py                              # read-only diagnosis (default)
python3 codex_session_doctor.py --migrate <old-provider>     # migrate attribution (auto backup)
python3 codex_session_doctor.py --migrate <old-provider> --yes # migrate without confirmation
python3 codex_session_doctor.py --strip-encrypted            # byte-preserving strip of foreign encrypted reasoning (auto backup)
python3 codex_session_doctor.py --restore-backup <backup-dir> # roll back one migration
```

Use `--codex-home <path>` or the `CODEX_HOME` environment variable to point at the Codex
directory (default `~/.codex`).

## Safety design

- **Read-only by default**: with no arguments it only scans and prints, touching no files
- **Automatic backup before migration**: session files go to `~/.codex/sessions_backup_<timestamp>/`;
  SQLite indexes are backed up as `*.pre-doctor-migrate-<timestamp>.bak`
- **SQLite index sync**: `--migrate` also updates `model_provider` in the `threads` table
  (the real data source for list filtering; upstream basis: `state/migrations/0001_threads.sql`).
  If the provider-name length change moves byte offsets, the projection tables
  (`thread_history_projection_state` etc.) are reset per the official `rollout_migration.rs`
  approach, and Codex re-projects in full on next open
- **Byte-length-preserving rewrite**: processes the file line by line at the raw-byte level;
  `openai`→`custom` (both 6 bytes) keeps every byte offset unchanged; when the new provider name
  is shorter the line is padded with trailing spaces to the original length. Untouched lines
  (invalid UTF-8, line-ending style, missing trailing newline) are preserved verbatim
- **`--strip-encrypted` byte-preserving strip**: surgically deletes `encrypted_content` / `rs_` id
  fields (plus the adjacent comma) and pads the line with trailing spaces to the original byte
  length — `history_base` offsets of `paginated` lineage threads and projection offsets all stay
  intact (v1 deleted whole lines and broke lineage; that mode is retired). Every rewritten line is
  re-parsed for validation; lines that fail validation are kept verbatim
- **Atomic writes**: every file write path uses `tmp + fsync + os.replace`, eliminating the
  `open-truncate → write → close` window on NTFS. That window produced 47 "same-size all-zero"
  session files on 2026-09-27 — the same failure mode as openai/codex issue #26421, where Codex's
  own config.toml was zero-filled
- **One-command rollback**: `--restore-backup` restores any migration
- 50 MB per-file processing cap; oversized files are truncated protectively

## Companion tool: rebuild_from_projection.py

Rebuilds resumable rollout files for damaged / zero-filled sessions from the SQLite projection
cache (`thread_items.item_json`):

```bash
python3 rebuild_from_projection.py <salvage-dir>                  # dry run: output to recovery_rebuilt/ (incl. Markdown archive)
python3 rebuild_from_projection.py <salvage-dir> --install --yes  # after review, write to live (quarantine originals + reset projection + backup)
```

- Rebuilds the conversation backbone (user / assistant messages); tool-call details go into a
  Markdown archive. `paginated` multi-file lineage is merged into a single legacy-mode file
- Writes `model_provider` into session_meta (the CLI resume picker and doctor statistics read the
  rollout itself; a missing field shows as "(unrecorded)")
- Also uses atomic writes; `--install` quarantines **all** all-zero source files (including root /
  continuation lineage files of merged threads; non-zero files are reported but never touched) as
  `*.zeroed-quarantine-<timestamp>`, and backs up SQLite

## Companion tool: retag_provider.py

Backfills `model_provider` into existing rollouts whose session_meta lacks the field (files
rewritten by newer Codex migrations no longer record it):

```bash
python3 retag_provider.py --codex-home ~/.codex --provider custom <files...>  # explicit files
python3 retag_provider.py --codex-home ~/.codex --from-threads               # auto-pair via threads table (dry run by default)
```

Rewrites only the session_meta line and leaves every other byte untouched; a line-length change
triggers a projection reset for that thread; dry run by default, `--yes` to commit. For pure
statistics you don't need it — doctor has a threads-table fallback built in.

## Incident post-mortem (2026-09-27 zero-fill event)

**What happened**: 47 session files became all-`\x00` at their original byte size, with mtime
unchanged; 42 were restored from backups, 5 were rebuilt from the projection cache
(a real-world result of `rebuild_from_projection.py`).

**Root cause** (same pattern as openai/codex issue #26421, where Codex's own config.toml was
zero-filled): `open(O_TRUNC) → write → close` (no FlushFileBuffers) × NTFS lazy write-back =
metadata (size/mtime) hits disk first while data lingers in the page cache; if the write-back is
lost, the disk keeps only allocated-but-unwritten zero blocks. In this incident the exposure was
a bulk non-atomic rewrite performed earlier the same day.

**Lessons (all baked into the tools)**:
1. Every write path must use `tmp + fsync + atomic replace` — data is either fully old or fully new;
2. Back up before rewriting user data: file-level (`sessions_backup_*`) plus SQLite-consistent
   copies (`.bak`);
3. Fully exit the application before bulk-rewriting session files, to avoid two writers racing;
4. Diagnostics must tolerate "metadata present, data corrupt" (doctor skips unparseable lines
   without crashing).

**Forensics notes**: on Windows, `fsutil usn readjournal C:` (admin) inspects USN records to
identify the writer; `chkdsk C: /scan` checks volume health.

## Requirements

- Python 3.8+ (3.11+ parses TOML natively; older versions fall back to a regex parser)
- No third-party dependencies; each tool is a single file

## Known limitations

- **Fully exit Codex** (CLI/Desktop) before `--migrate` / `--strip-encrypted`, or SQLite may be
  locked (the tool skips and warns)
- `archived_sessions/` only participates in diagnosis statistics, never migration; archived rows
  in the threads table keep their original provider
- `--strip-encrypted` only handles reasoning items; a compaction item's `encrypted_content` is a
  required structure and is reported, not stripped
- After stripping, resumed chats no longer carry the old provider's encrypted reasoning history
  (user / assistant messages are fully preserved and coherence is unaffected); if the upstream
  rejects bare reasoning items it still errors — roll back and report the error text
- Migration rewrites only the `model_provider` attribution recorded in the rollout JSONL; the
  session content itself is never modified
- Very old Codex versions without a SQLite index re-read the rollout files after restart and work
  the same way
