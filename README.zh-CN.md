# codex-session-doctor

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

诊断 / 修复 **Codex CLI / Desktop 切换供应商后旧会话不可见、不可续** 的问题。

> 问题根因与解决方案的完整分析见 **[METHODOLOGY.zh-CN.md](METHODOLOGY.zh-CN.md)**；
> 测试：`python tests/test_doctor_v2.py` / `test_rebuild.py` / `test_retag.py`（无需第三方依赖）。

## 背景

Codex 的会话列表（`codex resume` 选择器、Desktop 左侧线程列表）按会话文件中记录的
`model_provider` 过滤，只显示与当前 `~/.codex/config.toml` 中 `model_provider` 一致的会话
（官方确认：openai/codex issue #15494、#31625）。

ccs / CC Switch 等工具切换供应商时会重写 `config.toml` 的 `model_provider`，旧会话因此从
列表中"消失"——但 rollout 文件仍完整保留在 `~/.codex/sessions/` 下，**数据并没有丢**。

本工具做的事：

1. **诊断**：扫描全部会话文件，按 `model_provider` 分组，列出哪些会话当前被隐藏、各自的首条用户消息，确认数据完好；
2. **迁移**：把旧 provider 的会话归属改写为当前 provider，让它们重新出现在列表里（改写前自动备份）；
3. **回滚**：从任意一次备份一键恢复。

## 用法

```bash
python3 codex_session_doctor.py                              # 只读诊断（默认）
python3 codex_session_doctor.py --migrate <旧provider>       # 迁移归属（自动备份）
python3 codex_session_doctor.py --migrate <旧provider> --yes # 迁移并跳过确认
python3 codex_session_doctor.py --strip-encrypted            # 字节等长剥离外来加密 reasoning（自动备份）
python3 codex_session_doctor.py --restore-backup <备份目录>   # 回滚一次迁移
```

用 `--codex-home <路径>` 或环境变量 `CODEX_HOME` 指定 Codex 目录（默认 `~/.codex`）。

## 安全设计

- **默认只读**：不加参数仅扫描与打印，不修改任何文件
- **迁移前自动备份**：会话备份到 `~/.codex/sessions_backup_<时间戳>/`；SQLite 索引备份为 `*.pre-doctor-migrate-<时间戳>.bak`
- **SQLite 索引同步**：`--migrate` 自动同步 `threads` 表的 `model_provider`（列表过滤的真正数据源，源码依据 `state/migrations/0001_threads.sql`）；若新旧 provider 名长度不同导致行字节偏移变化，按官方 `rollout_migration.rs` 的方式重置 `thread_history_projection_state` 等投影表，Codex 下次打开时全量重建
- **字节级等长改写**：按原始字节逐行处理，`openai`→`custom`（同为 6 字节）时所有字节偏移不变；provider 名变短时行尾空格补齐等长。未改动的行（含非法 UTF-8、行尾风格、末尾换行）原样保留
- **`--strip-encrypted` 字节等长剥离**：外科手术式删除 `encrypted_content`/`rs_` id 字段后行尾补空格到原字节长度——`paginated` 血缘线程的 `history_base` 偏移、投影偏移全部保持不变（v1 整行删除曾致血缘断裂，已废弃）；每行改写后重新解析校验，不过则原样保留
- **原子写入**：所有文件写路径走 `tmp + fsync + os.replace`，杜绝 `open-truncate→写→关` 在 NTFS 上的延迟写回窗口（该窗口曾于 2026-09-27 造成 47 个会话文件"同尺寸全零"，与 openai/codex issue #26421 同一失败模式——Codex 自身 config.toml 的已知问题）
- **一键回滚**：`--restore-backup` 恢复任意一次迁移
- 单文件 50MB 处理上限，超大文件自动截断保护

## 配套工具：rebuild_from_projection.py

从 SQLite 投影缓存（`thread_items.item_json`）重建损坏/清零会话文件的可续聊 rollout：

```bash
python3 rebuild_from_projection.py recovery_projection_5            # dry-run：产物到 recovery_rebuilt/（含 Markdown 存档）
python3 rebuild_from_projection.py recovery_projection_5 --install --yes  # 验收后写入 live（隔离原件+重置投影+备份）
```

- 重建对话主干（用户/助手消息），工具调用细节进 Markdown 存档；`paginated` 多文件血缘合并为单文件 legacy 模式
- session_meta 写入 `model_provider`（CLI resume 选择器/doctor 统计读 rollout 自身，缺字段会显示"(未记录)"）
- 同样使用原子写入；`--install` 自动隔离**全部**全零源文件（含合并线程的根/续接血缘文件，非全零文件只提示不碰）为 `*.zeroed-quarantine-<时间戳>`，并备份 SQLite

## 配套工具：retag_provider.py

给 session_meta 缺 `model_provider` 的存量 rollout 补写该字段（新版 Codex 迁移重写的文件不再记录该字段）：

```bash
python3 retag_provider.py --codex-home ~/.codex --provider custom <文件...>   # 指定文件
python3 retag_provider.py --codex-home ~/.codex --from-threads               # 按 threads 表自动配对（默认 dry-run）
```

只重写 session_meta 行、其余行字节不动；行长变化自动重置该线程投影；默认 dry-run，`--yes` 才落盘。诊断显示无需动文件——doctor 已内置 threads 表兜底。

## 故障档案（2026-09-27 清零事件）

**现象**：47 个会话文件变为同字节数全 `\x00`，mtime 不变；42 个从备份恢复，5 个从投影缓存重建（`rebuild_from_projection.py` 的真实战果）。

**根因**（openai/codex issue #26421 同模式，Codex 官方自己的 config.toml 也中过此招）：
`open(O_TRUNC) → write → close`（无 FlushFileBuffers）× NTFS 延迟写回 = 元数据（尺寸/mtime）先行落盘、数据滞留页缓存；写回丢失时磁盘只剩已分配的零块。本事件中 11:33 的批量非原子改写是暴露源。

**教训（已全部固化到工具）**：
1. 所有写路径必须 `tmp + fsync + 原子替换`——数据要么完整旧、要么完整新；
2. 改写用户数据前先做文件级备份（doctor 的 `sessions_backup_*`）+ SQLite 一致性备份（`.bak`）；
3. 批量改写会话文件前完全退出应用，避免双写方竞争；
4. 诊断统计对"元数据在、数据坏"的文件要有免疫力（doctor 对解析失败行直接跳过，不崩溃）。

**取证备忘**：Windows 侧可用 `fsutil usn readjournal C:`（管理员）查 USN 记录判断写入方；`chkdsk C: /scan` 查卷健康。

## 环境要求

- Python 3.8+（3.11+ 原生解析 TOML；旧版本自动退化为正则解析）
- 无第三方依赖，单文件即可运行

## 已知限制

- `--migrate` / `--strip-encrypted` 执行前请**完全退出 Codex**（CLI/Desktop），否则 SQLite 可能被锁（工具会跳过并提示）
- `archived_sessions/` 归档目录只参与诊断统计，不参与迁移；threads 表中归档行的 provider 保持原值
- `--strip-encrypted` 只处理 reasoning 项；compaction 项的 encrypted_content 为必填结构，仅报告不自动处理
- 剥离加密 reasoning 后，续聊时模型不携带旧供应商加密的历史思维链（用户/助手消息完整保留，不影响会话连贯）；若上游不接受无密文的裸 reasoning 项仍会报错，此时回滚并回报报错原文
- 迁移改写的是 rollout JSONL 中 `model_provider` 字段的归属记录，不改变会话内容本身
- 极旧版本 Codex 无 SQLite 索引时，重启后列表直接读 rollout 文件，同样生效
