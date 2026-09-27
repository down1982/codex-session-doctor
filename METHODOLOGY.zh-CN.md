# Codex 多供应商会话续写:问题根因与解决方案

[English](METHODOLOGY.md) | [简体中文](METHODOLOGY.zh-CN.md) | [日本語](METHODOLOGY.ja.md)

本文复盘一次完整实战:在 **CC Switch(ccs)等反向代理工具切换供应商**的场景下,Codex CLI / Desktop
的旧会话会遇到的三类问题——产生原因、排查方法、修复方案,以及由此沉淀的最佳实践。
所有结论均在 Windows 11 + Codex Desktop/CLI(0.155~0.158)真机验证,并尽量给出 openai/codex
源码级依据。

> 隐私说明:本文与仓库内所有代码/测试均不含任何真实会话内容、线程 ID、用户消息与个人路径,
> 敏感原件封存在私有备份中。

---

## 0. 背景:多供应商反代下的会话存储结构

用 ccs 反代时,Codex 永远只对本地代理说话,`config.toml` 里只有 `model_provider = "custom"`
(`base_url = http://127.0.0.1:<port>/v1`)。切换供应商 = ccs 改写 config 或代理上游,但会话的
历史数据落在两处:

| 存储 | 内容 | 关键字段 |
|---|---|---|
| `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 会话原始流(session_meta / turn_context / response_item / event_msg) | `model_provider`、`history_base` |
| `~/.codex/state_5.sqlite`(及 `~/.codex/sqlite/` 下第二套) | 线程索引,`threads` 表 | `threads.model_provider`、`threads.rollout_path`、`threads.history_mode` |
| `~/.codex/thread_history_1.sqlite` | rollout 的**投影缓存**(thread_turns/thread_items/thread_history_projection_state) | `next_rollout_byte_offset`、`next_rollout_ordinal` |

列表过滤的真正数据源是 **`threads.model_provider`**(`list_threads_db`,带
`idx_threads_provider` 索引);rollout 文件里的 `model_provider` 影响按 ID resume 的校验与
doctor 类工具的统计口径。**两个存储必须一起改,只改一边都会出问题。**

---

## 问题一:切换供应商后,旧会话从列表"消失"

**现象**:`codex resume` 选择器、Desktop 左侧线程列表里,切换 provider 之前的所有会话不见了,
但 `sessions/` 目录数据完好。

**根因**:列表按 `threads.model_provider` 过滤,只显示与当前 config 一致的行。切供应商时旧会话
的归属字段仍是旧值(openai),而 config 已是 custom → 整批被过滤。参考官方 issue
openai/codex **#15494、#31625**。

**方案(migrate v2)**:
1. **字节等长改写 rollout**:按原始字节逐行处理,只替换 `model_provider` 字段值;
   新旧 provider 名等长(如 `openai`→`custom`,同为 6 字节)则所有字节偏移天然不变;
   名字变短时行尾空格补齐到原长(JSONL 行尾空白合法),变长时才走投影重置。
2. **同步 SQLite**:对两套 state 库的 `threads` 表按 thread id 精确 UPDATE(归档行不动);
   改写前用 SQLite backup API 做一致性备份(`.pre-doctor-migrate-*.bak`)。
3. **字节级细节决定成败**:未改动的行(含非法 UTF-8、行尾 `\r\n`、末尾换行缺省)必须原样保留;
   Windows 文本模式写入会把 `\n` 全量换成 `\r\n`(每行 +1 字节)——必须用 `write_bytes`。

---

## 问题二:旧会话续聊报 400 `invalid_encrypted_content`

**现象**:会话可见了,但 resume 后第一句话就被上游 400 拒绝。报错链路
`CC Switch → 上游网关(litellm) → Azure`:*The encrypted content for item rs_… could not be
verified… Encrypted content is tied to the organization that created it.*

**根因**:无服务端存储模式(`disable_response_storage` / ZDR)下,reasoning 以
`encrypted_content` 密文形式随响应返回并写入 rollout;**密文与创建它的组织/部署绑定**。
切换供应商后,续聊请求把旧密文原样发给新上游 → 新上游解不开 → 400。Codex 侧
`Reasoning.encrypted_content` 本就是 `Option<String>`,源码明确支持"明文 reasoning"
(`history.rs`:plaintext reasoning excluded from replay accounting)——**剥掉密文完全合法**。

**方案(strip v2)与两个坑**:
1. **必须字节等长**:直接删字段/删行会缩短文件,而 paginated 血缘线程的每个续接 rollout 在
   `session_meta.history_base.end_byte_offset` 里记录了它继承自父文件的字节截止位——文件一短,
   子文件偏移越过 EOF,resume 直接报 `invalid paginated history lineage`(比 400 更糟)。
   正确做法:外科手术式删除 `"encrypted_content":"…"`/`"id":"rs_…"` 字段(及紧邻逗号)后,
   **行尾补空格到原字节长度**;每行改写后重新 `json.loads` 校验,不过则原样保留。
2. **顺带删 `rs_` 前缀 id**:避免服务端按 id 检索旧密文;协议里 id 同为可缺省。
3. **上游容忍度需实测**:剥离后是"裸 reasoning 项",litellm→Azure 实测接受(真机续聊通过);
   compaction 项的密文是必填结构,不能自动剥,只能报告。
4. **投影重置清理缓存**:即使字节等长,`thread_items.item_json` 缓存里仍有旧密文副本,
   按官方 `rollout_migration.rs:1027-1030` 的姿势删四表行让 Codex 全量重投影(重置是官方
   机制,零风险)。

---

## 问题三(事故复盘):会话文件变成"同尺寸全零"

**现象**:47 个 rollout 文件字节内容 100% 是 `\x00`,但**尺寸与 mtime 完全不变**;
两个备份目录里同 47 个文件同样全零;事件查看器无任何磁盘错误。

**根因**(openai/codex issue **#26421** 同模式,Codex 自己的 config.toml 也中过):
`open(O_TRUNC) → write → close`(无 FlushFileBuffers)在 NTFS 上的结构性窗口——
**元数据(尺寸/mtime)先落盘,数据滞留页缓存**;延迟写回一旦丢失,磁盘只剩"已分配但未写入"
的零块。本事件时间线:11:33 非原子批量改写(数据进了页缓存)→ 12:02:28 扫描从页缓存读到
完整内容 → 12:02:29-44 逐文件备份读到磁盘实况(全零)→ 数据静默丢失。

**预防(原子写)**:所有写路径走 `tmp 临时文件 + fsync + os.replace`——数据要么完整旧、
要么完整新,消灭中间态。这是给 Codex 官方的建议修法(issue 仍 open)。

**恢复三板斧**(本事件 47/47 全部找回):
1. **文件级备份回滚**:工具每次改写前按原目录结构备份(`sessions_backup_*/`);
2. **投影缓存重建**:全零文件若从未进过备份,`thread_items.item_json` 里往往还缓存着对话
   投影(含 100% 覆盖的情况,按 `projection_state.next_rollout_byte_offset / 文件原尺寸`
   判断覆盖度)——按 ordinal 排序重建对话主干,合并血缘时按 item id 去重、ordinal 延续线程
   全局序号;
3. **SQL 脚本取样**:全零文件尺寸不变,是判断投影覆盖度的天然标尺。

---

## 最佳实践清单(踩坑换来的)

1. **字节等长是铁律**:改 rollout 的任何字段,要么等值替换、要么行尾补齐;
   `history_base` / 投影偏移 / `thread_turns.rollout_byte_offset` 全系于字节位置。
2. **原子写是铁律**:`tmp + fsync + os.replace`,尤其 Windows/NTFS。
3. **先备份后动手**:文件级 + SQLite 一致性备份都要有,且备份动作本身也要原子。
4. **改文件前完全退出 Codex**(CLI/Desktop),防双写竞争与投影错位。
5. **SQLite 与 rollout 是两本账**:列表过滤看 threads 表,续聊校验看 rollout,两边都要改。
6. **幂等设计**:重复运行零改动;每次产出可回滚的备份目录。
7. **默认只读**:诊断与写操作分离,写操作必须显式参数(`--yes`)。

## 附:官方依据与进一步阅读

- openai/codex issue **#15494 / #31625**:会话列表按 provider 过滤(本文问题一)
- openai/codex issue **#26421**:非原子写导致文件清零(本文问题三)
- 源码关键点(2026-09 main):`state/migrations/0001_threads.sql`、
  `rollout/src/state_db.rs`(list_threads_db / read_repair)、
  `thread-store/src/local/thread_history.rs`(投影偏移校验)、
  `core/src/context_manager/history.rs`(明文 reasoning 合法性)、
  `rollout/src/rollout_migration.rs:1027-1030`(投影重置官方姿势)
