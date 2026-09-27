#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex_session_doctor — 诊断 / 修复「Codex 切换供应商后旧会话不可见、不可续」问题

背景：
  Codex CLI / Desktop 的会话列表（codex resume 选择器、Desktop 左侧线程列表）会按
  会话文件中记录的 model_provider 过滤，只显示与当前 ~/.codex/config.toml 中
  model_provider 一致的会话（官方确认：openai/codex issue #15494、#31625）。
  ccs / CC Switch 等工具切换供应商时会重写 config.toml 的 model_provider，
  旧会话因此从列表中"消失"——但 rollout 文件仍完整保留在 ~/.codex/sessions/ 下，
  数据并没有丢。

用法：
  python3 codex_session_doctor.py                     # 只读诊断（默认）
  python3 codex_session_doctor.py --migrate <旧provider>   # 把旧 provider 的会话归属改写为当前 provider（自动备份）
  python3 codex_session_doctor.py --migrate <旧provider> --yes   # 跳过确认
  python3 codex_session_doctor.py --strip-encrypted    # 字节等长剥离外来 reasoning encrypted_content
  python3 codex_session_doctor.py --restore-backup <备份目录>    # 回滚一次迁移
  可用 --codex-home <路径> 或环境变量 CODEX_HOME 指定 Codex 目录（默认 ~/.codex）
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

MAX_FILE_BYTES = 50 * 1024 * 1024  # 单个 rollout 文件最多处理 50MB
MAX_DETAIL_ROWS = 30               # 明细最多打印条数


def atomic_write_bytes(path: Path, data: bytes):
    """tmp + fsync + 原子替换。

    2026-09-27 清零事件教训: open(w)/write_bytes 是 open-truncate→写→关,
    无 FlushFileBuffers —— NTFS 元数据(尺寸/mtime)先行落盘、数据滞留页缓存,
    写回失败时磁盘上只剩已分配的零块, 文件呈现"同尺寸全 \\x00"(openai/codex
    issue #26421 对 Codex 自身 config.toml 记录了同一失败模式)。
    本工具 11:33 的 migrate 写入正是该窗口的暴露源。改为写临时文件并 fsync
    后原子替换, 数据要么完整旧、要么完整新, 消除中间态。
    """
    tmp = path.with_name(path.name + ".doctor-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

# 字节级"外科手术"删除: 只匹配 "encrypted_content":"..." / "id":"rs_..." 字段及其
# 紧邻的一个逗号(两种逗号位置二选一),不重序列化整行,避免浮点/转义/键序漂移。
# 字符串值内部若出现同名字样必然带反斜杠转义(\"),不会命中此模式。
_RE_ENC_FIELD = re.compile(
    rb'(?:,\s*"encrypted_content"\s*:\s*"(?:[^"\\]|\\.)*")'
    rb'|(?:"encrypted_content"\s*:\s*"(?:[^"\\]|\\.)*"\s*,)')
_RE_RS_ID = re.compile(
    rb'(?:,\s*"id"\s*:\s*"rs_[^"]*")|(?:"id"\s*:\s*"rs_[^"]*",)')


# ---------------------------------------------------------------- config.toml
def parse_config_toml(path: Path):
    """返回 (data, warning)。优先 tomllib，失败则用宽容的正则解析。"""
    if not path.exists():
        return None, "config.toml 不存在"
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        import tomllib  # Python 3.11+
        return tomllib.loads(raw), None
    except ImportError:
        pass
    except Exception as e:  # tomllib 语法报错
        return _regex_parse_toml(raw), f"tomllib 解析失败，已退化为正则解析: {e}"
    return _regex_parse_toml(raw), None


def _regex_parse_toml(raw: str):
    data = {}
    section = None
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^\[([^\]]+)\]$", line)
        if m:
            section = m.group(1)
            cur = data
            for part in section.split("."):
                cur = cur.setdefault(part.strip('"'), {})
            continue
        m = re.match(r'^([A-Za-z0-9_\-"]+)\s*=\s*(.+?)\s*$', line)
        if m:
            key = m.group(1).strip('"')
            val = m.group(2)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif re.match(r"^-?\d+$", val):
                val = int(val)
            elif val in ("true", "false"):
                val = val == "true"
            else:
                val = val.split("#")[0].strip().strip(",")
            target = data if section is None else _last_table(data, section)
            target[key] = val
    return data


def _last_table(data, section):
    cur = data
    for part in section.split("."):
        cur = cur.setdefault(part.strip('"'), {})
    return cur


# ------------------------------------------------------------------- auth.json
def describe_auth(path: Path):
    if not path.exists():
        return "auth.json 不存在（可能从未登录/未配置 key）"
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"auth.json 解析失败: {e}"
    bits = []
    key = d.get("OPENAI_API_KEY")
    if isinstance(key, str) and key:
        bits.append(f"API Key 模式: OPENAI_API_KEY={key[:7]}…(共{len(key)}字符)")
    elif key is None and "OPENAI_API_KEY" in d:
        bits.append("OPENAI_API_KEY 为 null")
    token_fields = [f for f in ("access_token", "refresh_token", "id_token", "account_id") if d.get(f)]
    if token_fields:
        bits.append(f"ChatGPT 登录凭据存在（含 {', '.join(token_fields)}）")
    if not bits:
        bits.append(f"未识别的字段: {sorted(d.keys())}")
    return "；".join(bits)


# --------------------------------------------------------------- session 扫描
class Session:
    __slots__ = ("path", "session_id", "providers", "models", "cwd",
                 "first_user_msg", "mtime", "size", "lines")

    def __init__(self, path: Path):
        self.path = path
        self.session_id = ""
        self.providers = []      # 按出现顺序去重
        self.models = []
        self.cwd = ""
        self.first_user_msg = ""
        self.mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        self.size = path.stat().st_size
        self.lines = 0

    @property
    def provider(self):
        return self.providers[-1] if self.providers else "(未记录)"

    @property
    def model(self):
        return self.models[-1] if self.models else "?"


def _response_item_user_text(content):
    """新版 Codex（Desktop/近期 CLI）把用户消息放在 response_item/message:
    content = [{"type": "input_text"|"text", "text": "..."}]。
    逐段过滤 <environment_context>/<user_instructions> 等注入块,返回可读文本。"""
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, str):
                parts.append(c)
    else:
        return ""
    keep = [p.strip() for p in parts if p.strip() and not p.strip().startswith("<")]
    return " ".join(keep)


def scan_sessions(sessions_dir: Path):
    if not sessions_dir.exists():
        return []
    out = []
    for p in sorted(sessions_dir.rglob("*.jsonl")):
        if not p.is_file():
            continue
        try:
            s = Session(p)
        except OSError:
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s.lines += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    payload = obj.get("payload")
                    if t == "session_meta" and isinstance(payload, dict):
                        s.session_id = payload.get("id") or s.session_id
                        s.cwd = payload.get("cwd") or s.cwd
                    # turn_context（CLI）或 thread 元数据（Desktop）都可能带 provider
                    for holder in (payload if isinstance(payload, dict) else None, obj):
                        if isinstance(holder, dict):
                            mp = holder.get("model_provider")
                            if isinstance(mp, str) and mp and mp not in s.providers:
                                s.providers.append(mp)
                            mo = holder.get("model")
                            if isinstance(mo, str) and mo and mo not in s.models:
                                s.models.append(mo)
                    if (t == "event_msg" and isinstance(payload, dict)
                            and payload.get("type") == "user_message" and not s.first_user_msg):
                        msg = payload.get("message") or payload.get("text") or ""
                        if isinstance(msg, str) and msg and not msg.startswith("<"):
                            s.first_user_msg = msg
                    # 新版格式: 用户消息在 response_item/message 的 content 里
                    if (not s.first_user_msg and t == "response_item"
                            and isinstance(payload, dict)
                            and payload.get("type") == "message"
                            and payload.get("role") == "user"):
                        msg = _response_item_user_text(payload.get("content"))
                        if msg:
                            s.first_user_msg = msg
                    if p.stat().st_size > MAX_FILE_BYTES:
                        break
        except OSError as e:
            print(f"  ! 读取失败 {p}: {e}", file=sys.stderr)
        out.append(s)
    return out


# ---------------------------------------------------------------------- 输出
def short(s, n):
    s = (s or "").replace("\n", " ").replace("\r", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def sqlite_provider_maps(home: Path):
    """扫描 CODEX_HOME 全部 SQLite 索引,返回 (id→provider, basename→provider)。

    新版 Codex 迁移重写的 rollout 其 session_meta 不再记录 model_provider,
    但 threads 表(列表过滤的真正数据源)有 —— 诊断统计据此兜底。只读,不改文件。
    basename 键来自 threads.rollout_path(指向血缘最后一个文件)。
    """
    id_map, name_map = {}, {}
    try:
        import sqlite3
    except ImportError:
        return id_map, name_map
    dbs = sorted({p for p in home.rglob("*")
                  if p.suffix in (".sqlite", ".db", ".sqlite3")
                  and not p.name.endswith(".bak")
                  and not any("backup" in part.lower()
                              for part in p.relative_to(home).parts)})
    for db in dbs:
        try:
            con = sqlite3.connect(str(db), timeout=5)
        except Exception:
            continue
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "threads" not in tables:
                continue
            cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
            if "model_provider" not in cols or "rollout_path" not in cols:
                continue
            for tid, rp, prov in con.execute(
                    "SELECT id, rollout_path, model_provider FROM threads"):
                if tid and prov:
                    id_map.setdefault(str(tid).lower(), prov)
                    if rp:
                        name_map.setdefault(Path(str(rp)).name, prov)
        except Exception:
            pass
        finally:
            try:
                con.close()
            except Exception:
                pass
    return id_map, name_map


def diagnose(home: Path):
    cfg, warn = parse_config_toml(home / "config.toml")
    if warn:
        print(f"[!] {warn}")
    current_provider = None
    current_model = None
    provider_ids = []
    if isinstance(cfg, dict):
        current_provider = cfg.get("model_provider") or "openai(默认)"
        current_model = cfg.get("model") or "(默认)"
        mps = cfg.get("model_providers")
        if isinstance(mps, dict):
            provider_ids = list(mps.keys())

    print("=" * 78)
    print(f"Codex home        : {home}")
    print(f"当前 model_provider: {current_provider}")
    print(f"当前 model        : {current_model}")
    if provider_ids:
        print(f"config 中已定义的 provider: {', '.join(provider_ids)}")
    print(f"auth.json 状态    : {describe_auth(home / 'auth.json')}")

    # SQLite 索引检测（新版/桌面版用 SQLite 做会话索引）
    sqlite_files = [p for p in home.glob("*") if p.suffix in (".sqlite", ".db", ".sqlite3")]
    if sqlite_files:
        print(f"SQLite 会话索引   : 检测到 {', '.join(p.name for p in sqlite_files)}"
              f"（列表可能取自索引，改文件后需重启 Codex 生效）")

    sessions = scan_sessions(home / "sessions")
    archived = scan_sessions(home / "archived_sessions")
    print(f"\n扫描到 {len(sessions)} 个会话文件（sessions/），{len(archived)} 个归档会话（archived_sessions/）")

    if not sessions:
        print("[!] 没有找到任何 rollout 会话文件 —— 若你确实有过会话，请检查 CODEX_HOME 是否正确")
        return current_provider

    # provider 兜底(只读): 新版 codex 迁移重写/重建的 rollout session_meta 无
    # model_provider, 按 threads 表补齐统计口径, 避免"(未记录)"分组失真
    id_map, name_map = sqlite_provider_maps(home)
    if id_map or name_map:
        n_fb = 0
        for s in sessions:
            if s.providers:
                continue
            prov = id_map.get((s.session_id or "").lower()) or name_map.get(s.path.name)
            if prov:
                s.providers.append(prov)
                n_fb += 1
        if n_fb:
            print(f"[provider 兜底] {n_fb} 个会话文件的 session_meta 未记录 model_provider，"
                  f"已按 SQLite threads 表补齐（仅影响诊断显示，未改动文件）。")

    groups = {}
    for s in sessions:
        groups.setdefault(s.provider, []).append(s)

    print("\n按会话记录的 model_provider 分组（即 Codex 的“抽屉”）:")
    for prov in sorted(groups, key=lambda k: -len(groups[k])):
        tag = "可见 ✔" if prov == current_provider else "隐藏 ✘（切到该 provider 才能看到/继续）"
        models = sorted({m for s in groups[prov] for m in s.models})
        print(f"  {prov:<28} {len(groups[prov]):>4} 个会话   [{tag}]  models: {', '.join(models) or '?'}")

    hidden = [s for s in sessions if s.provider != current_provider]
    hidden.sort(key=lambda s: s.mtime, reverse=True)
    if hidden:
        print(f"\n当前被隐藏的会话明细（最近 {min(len(hidden), MAX_DETAIL_ROWS)} / {len(hidden)} 条，按时间倒序）:")
        print(f"  {'修改时间':<17}{'provider/model':<34}首条用户消息")
        for s in hidden[:MAX_DETAIL_ROWS]:
            pm = f"{short(s.provider, 18)}/{short(s.model, 14)}"
            print(f"  {s.mtime.strftime('%Y-%m-%d %H:%M'):<17}{pm:<34}{short(s.first_user_msg or '(无文本)', 40)}")
        print(f"\n结论: 旧会话数据都在，没有丢失。只是会话文件里记录的 model_provider 与当前")
        print(f"      config.toml 的 model_provider={current_provider} 不一致，被列表过滤隐藏。")
    else:
        print("\n结论: 所有会话都属于当前 provider，不存在被隐藏的会话。")
    return current_provider


# -------------------------------------------------------------------- 迁移
def migrate(home: Path, old_provider: str, current_provider: str, assume_yes: bool):
    if old_provider == current_provider:
        sys.exit("旧 provider 与当前 provider 相同，无需迁移。")
    sessions_dir = home / "sessions"
    targets = [s for s in scan_sessions(sessions_dir) if s.provider == old_provider]
    if not targets:
        sys.exit(f"没有找到属于 provider “{old_provider}” 的会话，请核对 provider id（用诊断输出的分组名）。")

    print(f"将把 {len(targets)} 个会话的归属 provider 从 “{old_provider}” 改写为 “{current_provider}”：")
    for s in targets[:10]:
        print(f"  - {s.path.name}  ({s.mtime:%Y-%m-%d %H:%M}, {short(s.first_user_msg, 36)})")
    if len(targets) > 10:
        print(f"  ... 及另外 {len(targets) - 10} 个")

    if not assume_yes:
        ans = input(f"\n确认改写？会先备份到 sessions_backup_* 目录 [yes/N]: ").strip().lower()
        if ans != "yes":
            sys.exit("已取消。")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = home / f"sessions_backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    n_files = n_lines = n_shifted = 0
    affected_ids, reset_ids = [], []
    for s in targets:
        rel = s.path.relative_to(sessions_dir)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s.path, dst)
        # 按原始字节逐行处理: 未改动的行(含非法 UTF-8/行尾风格/末尾换行)原样保留。
        # 这让 SQLite 投影记录的字节偏移在 "openai"→"custom" 这类等长替换下依然有效
        # (见下方 length_ok 判定与 sync_sqlite 的投影重置逻辑)。
        raw = s.path.read_bytes()
        had_nl = raw.endswith(b"\n")
        src_lines = raw[:-1].split(b"\n") if had_nl else raw.split(b"\n")
        out_parts = []
        changed = 0
        length_ok = True
        for raw_line in src_lines:
            if not raw_line.strip():
                out_parts.append(raw_line)
                continue
            try:
                text_line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                out_parts.append(raw_line)      # 非 UTF-8 行原样保留,不做任何改写
                continue
            wrote = raw_line
            try:
                obj = json.loads(text_line)
                if isinstance(obj, dict):
                    replaced = False
                    for holder in [h for h in (obj, obj.get("payload")) if isinstance(h, dict)]:
                        if holder.get("model_provider") == old_provider:
                            holder["model_provider"] = current_provider
                            replaced = True
                    if replaced:
                        changed += 1
                        wrote = json.dumps(obj, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")
                        if len(wrote) < len(raw_line):
                            # 字节等长补齐(行尾空格合法): 即使新旧 provider 名长度不同,
                            # 也保住 paginated history_base / 投影的全部字节偏移
                            wrote = wrote + b" " * (len(raw_line) - len(wrote))
                        elif len(wrote) > len(raw_line):
                            length_ok = False   # 变长无法补齐 → 投影偏移失效,需要重置
            except Exception:
                pass
            out_parts.append(wrote)
        if changed:
            atomic_write_bytes(s.path, b"\n".join(out_parts) + (b"\n" if had_nl else b""))
            n_files += 1
            n_lines += changed
            affected_ids.append(s.session_id)
            if s.session_id and not length_ok:
                reset_ids.append(s.session_id)
            if not length_ok:
                n_shifted += 1

    print(f"\n完成: 改写 {n_files} 个文件、{n_lines} 处 model_provider；备份目录: {backup_dir}")
    if n_shifted:
        print(f"注意: {n_shifted} 个文件改写后行长变化（新旧 provider 名长度不同），")
        print("      SQLite 投影记录的字节偏移已失效，将在下一步重置这些线程的投影。")
    else:
        print("所有改写行字节长度不变，SQLite 投影的字节偏移仍然有效，无需重置投影。")
    sync_sqlite(home, old_provider, current_provider, affected_ids, reset_ids)
    print("\n提示: 已改写 rollout JSONL 并同步 SQLite 索引。请完全退出并重启 Codex")
    print("      （CLI/Desktop）让列表刷新；若仍有异常，可用 --restore-backup 回滚会话文件，")
    print("      SQLite 索引的备份为同目录下 *.pre-doctor-migrate-<时间戳>.bak，覆盖回去即可。")


def sync_sqlite(home: Path, old_provider: str, current_provider: str,
                affected_ids, reset_ids):
    """把 provider 迁移同步进 Codex 的 SQLite 索引（thread_history_1.sqlite 等）。

    依据 openai/codex 源码（2026-09 main）:
    - state/migrations/0001_threads.sql: threads.model_provider 是会话列表 provider
      过滤的数据源（idx_threads_provider 索引），只改 rollout 文件不够。
    - thread-store/src/local/thread_history.rs: thread_history_projection_state 按
      thread 记录 next_rollout_byte_offset/next_rollout_ordinal，增量投影严格校验
      偏移与序号；rollout 变短直接报错，变长会错位。
    - rollout_migration.rs:1027-1030（官方做法）: 偏移与文件不符时删除
      thread_items/thread_realtime_items/thread_turns/thread_history_projection_state
      四表中该 thread 的行，让 Codex 之后从头全量重投影。此处采用同款处理。
    """
    affected_ids = [i for i in dict.fromkeys(affected_ids) if i]
    reset_ids = [i for i in dict.fromkeys(reset_ids) if i]
    dbs = sorted({p for p in home.rglob("*")
                  if p.suffix in (".sqlite", ".db", ".sqlite3")
                  and not p.name.endswith(".bak")
                  and not any("backup" in part.lower()
                              for part in p.relative_to(home).parts)})
    if not dbs:
        print("\nSQLite 索引: 未发现 .sqlite/.db 文件，无需同步（此版本列表直接读 rollout）。")
        return
    try:
        import sqlite3
    except ImportError:
        print(f"\n[!] Python 缺少 sqlite3 模块；请手动把 threads 表中 model_provider="
              f"“{old_provider}”的行改为“{current_provider}”。")
        return

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    print("\nSQLite 索引同步:")
    for db in dbs:
        rel = db.relative_to(home)
        try:
            con = sqlite3.connect(str(db), timeout=5)
        except Exception as e:
            print(f"  ! 打不开 {rel}: {e} —— 若 Codex 正在运行，请完全退出后重跑 --migrate")
            continue
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            marks = ",".join("?" * len(affected_ids)) if affected_ids else None
            need_threads = False
            if marks and "threads" in tables:
                cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                if "model_provider" in cols:
                    need_threads = con.execute(
                        f"SELECT COUNT(*) FROM threads WHERE id IN ({marks}) "
                        "AND model_provider=?",
                        affected_ids + [old_provider]).fetchone()[0] > 0
            qmarks = ",".join("?" * len(reset_ids)) if reset_ids else None
            need_reset = False
            if qmarks and "thread_history_projection_state" in tables:
                need_reset = con.execute(
                    "SELECT COUNT(*) FROM thread_history_projection_state "
                    f"WHERE thread_id IN ({qmarks})", reset_ids).fetchone()[0] > 0
            if not (need_threads or need_reset):
                con.close()
                print(f"  {rel}: 无需改动")
                continue
            # 先做一致性备份（sqlite backup API，兼容 WAL 日志），再动数据
            bak = db.with_name(f"{db.name}.pre-doctor-migrate-{ts}.bak")
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(bak))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            msgs = []
            if need_threads:
                n = con.execute(
                    f"UPDATE threads SET model_provider=? WHERE id IN ({marks}) "
                    "AND model_provider=?",
                    [current_provider] + affected_ids + [old_provider]).rowcount
                msgs.append(f"threads 表 {n} 行 provider 改为“{current_provider}”")
                left = con.execute(
                    "SELECT COUNT(*) FROM threads WHERE model_provider=?",
                    (old_provider,)).fetchone()[0]
                if left:
                    msgs.append(f"另有 {left} 行仍为“{old_provider}”（归档/未迁移会话，未动）")
            if need_reset:
                for tbl in ("thread_items", "thread_realtime_items",
                            "thread_turns", "thread_history_projection_state"):
                    if tbl in tables:
                        con.execute(f"DELETE FROM {tbl} WHERE thread_id IN ({qmarks})",
                                    reset_ids)
                msgs.append(f"重置 {len(reset_ids)} 个线程的投影（Codex 下次打开时全量重建）")
            con.commit()
            print(f"  {rel}: {'；'.join(msgs)}（备份 {bak.name}）")
        except Exception as e:
            print(f"  ! {rel} 处理失败: {e}")
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            try:
                con.close()
            except Exception:
                pass


def reset_sqlite_projections(home: Path, thread_ids, tag="strip"):
    """为指定 thread 重置 SQLite 投影四表（官方 rollout_migration.rs:1027 同款处理）。

    --strip-encrypted 等改写 rollout 字节的操作会使投影记录的
    next_rollout_byte_offset/ordinal 失效，必须删除四表行让 Codex 全量重投影。
    """
    thread_ids = [i for i in dict.fromkeys(thread_ids) if i]
    if not thread_ids:
        return
    try:
        import sqlite3
    except ImportError:
        print("\n[!] Python 缺少 sqlite3 模块，无法重置投影；若重启后列表/续聊异常，"
             "用会话备份回滚并手动删除索引库中这些 thread 的行。")
        return
    dbs = sorted({p for p in home.rglob("*")
                  if p.suffix in (".sqlite", ".db", ".sqlite3")
                  and not p.name.endswith(".bak")
                  and not any("backup" in part.lower()
                              for part in p.relative_to(home).parts)})
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    print("\nSQLite 投影重置:")
    for db in dbs:
        rel = db.relative_to(home)
        try:
            con = sqlite3.connect(str(db), timeout=5)
        except Exception as e:
            print(f"  ! 打不开 {rel}: {e}（Codex 运行中？完全退出后重跑）")
            continue
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            qmarks = ",".join("?" * len(thread_ids))
            need = ("thread_history_projection_state" in tables
                    and con.execute(
                        "SELECT COUNT(*) FROM thread_history_projection_state "
                        f"WHERE thread_id IN ({qmarks})", thread_ids).fetchone()[0] > 0)
            if not need:
                con.close()
                continue
            bak = db.with_name(f"{db.name}.pre-doctor-{tag}-{ts}.bak")
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(bak))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            for tbl in ("thread_items", "thread_realtime_items",
                        "thread_turns", "thread_history_projection_state"):
                if tbl in tables:
                    con.execute(f"DELETE FROM {tbl} WHERE thread_id IN ({qmarks})",
                                thread_ids)
            con.commit()
            print(f"  {rel}: 重置 {len(thread_ids)} 个线程的投影（备份 {bak.name}）")
        except Exception as e:
            print(f"  ! {rel} 处理失败: {e}")
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            try:
                con.close()
            except Exception:
                pass


def strip_encrypted(home: Path, assume_yes: bool):
    """以**字节等长**方式剥离 rollout 中 reasoning 项的外来 encrypted_content。

    根因: Responses API 无服务端存储时（旧版 disable_response_storage / ZDR 模式），
    reasoning 以 encrypted_content 密文返回并写入 rollout；密文绑定创建组织，
    跨供应商续聊时请求原样上行，新上游解不开 → 400 invalid_encrypted_content。

    v2 设计（吸取 v1 血缘断裂教训, 2026-09-27 真机证据: paginated 线程的续接
    rollout 在 session_meta.history_base.end_byte_offset 记录父文件字节位置,文件
    变短即 cutoff 越界）:
    - **字节等长**: 外科手术式删除 "encrypted_content"/"rs_" id 字段后, 行尾补空格
      到原字节长度。行尾空白对 serde_json/JSON 规范合法, 且不移动任何字节偏移
      —— paginated history_base / 投影 / thread_turns 偏移全部保持有效。
    - 不做全行重序列化(避免浮点/转义/键序漂移), 每行改写后重新解析校验,
      校验不过则原样保留并计入 skipped。
    - 投影仍重置: 非因偏移失效, 而是清理 thread_items 缓存里的旧密文副本
      (官方 rollout_migration.rs 同款四表删除, Codex 自动全量重投影)。
    - compaction 项的 encrypted_content 为必填结构, 仅报告不自动处理。
    """
    sessions_dir = home / "sessions"
    sessions = scan_sessions(sessions_dir)
    hits = []
    for s in sessions:
        n_r = n_c = 0
        try:
            with s.path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "response_item":
                        continue
                    p = obj.get("payload")
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "reasoning" and p.get("encrypted_content"):
                        n_r += 1
                    elif p.get("type") == "compaction" and p.get("encrypted_content"):
                        n_c += 1
        except OSError:
            continue
        if n_r or n_c:
            hits.append((s, n_r, n_c))
    if not hits:
        print("\n未发现含 encrypted_content 的会话，无需处理。")
        return
    t_r = sum(h[1] for h in hits)
    t_c = sum(h[2] for h in hits)
    print(f"\n发现 {len(hits)} 个会话含外来加密内容（reasoning {t_r} 处"
          + (f"，compaction {t_c} 处[仅报告，不自动处理]" if t_c else "") + "）")
    if not t_r:
        print("没有需要剥离的 reasoning encrypted_content，无需处理。")
        return
    mode = "字节等长剥离（删 encrypted_content + rs_ id，行尾空格补齐，零偏移移动）"
    print(f"处理方式: {mode}")
    for s, n_r, n_c in hits[:10]:
        print(f"  - {s.path.name}  ({s.mtime:%Y-%m-%d %H:%M}, {short(s.first_user_msg, 36)})")
    if len(hits) > 10:
        print(f"  ... 及另外 {len(hits) - 10} 个")
    if not assume_yes:
        ans = input("\n确认处理？会先备份到 sessions_backup_* 目录 [yes/N]: ").strip().lower()
        if ans != "yes":
            sys.exit("已取消。")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = home / f"sessions_backup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    n_files = n_items = n_skipped = 0
    reset_ids = []
    for s, n_r, n_c in hits:
        if not n_r:
            continue
        rel = s.path.relative_to(sessions_dir)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s.path, dst)
        raw = s.path.read_bytes()
        had_nl = raw.endswith(b"\n")
        src_lines = raw[:-1].split(b"\n") if had_nl else raw.split(b"\n")
        out_parts, changed, skipped = [], 0, 0
        for raw_line in src_lines:
            if not raw_line.strip():
                out_parts.append(raw_line)
                continue
            try:
                text_line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                out_parts.append(raw_line)
                continue
            new_line = raw_line
            try:
                obj = json.loads(text_line)
                if isinstance(obj, dict) and obj.get("type") == "response_item":
                    p = obj.get("payload")
                    if (isinstance(p, dict) and p.get("type") == "reasoning"
                            and p.get("encrypted_content")):
                        cand = _RE_ENC_FIELD.sub(b"", raw_line, count=1)
                        if isinstance(p.get("id"), str) and p["id"].startswith("rs_"):
                            cand = _RE_RS_ID.sub(b"", cand, count=1)
                        ok = False
                        if len(cand) <= len(raw_line):
                            try:
                                v = json.loads(cand.decode("utf-8"))
                                vp = v.get("payload") if isinstance(v, dict) else None
                                ok = (isinstance(vp, dict)
                                      and vp.get("type") == "reasoning"
                                      and not vp.get("encrypted_content")
                                      and not (isinstance(vp.get("id"), str)
                                               and vp["id"].startswith("rs_")))
                            except Exception:
                                ok = False
                        if ok:
                            # 字节等长补齐: 行尾空格对 serde_json 合法,
                            # history_base/投影/turns 的全部字节偏移保持不变
                            new_line = cand + b" " * (len(raw_line) - len(cand))
                            changed += 1
                        else:
                            skipped += 1   # 校验不过, 原样保留
            except Exception:
                pass
            out_parts.append(new_line)
        if changed or skipped:
            atomic_write_bytes(s.path, b"\n".join(out_parts) + (b"\n" if had_nl else b""))
        if changed:
            n_files += 1
            n_items += changed
            if s.session_id:
                reset_ids.append(s.session_id)
        n_skipped += skipped

    print(f"\n完成: 处理 {n_files} 个文件、{n_items} 个 reasoning 项（全部字节等长）；备份目录: {backup_dir}")
    if n_skipped:
        print(f"注意: {n_skipped} 行改写后校验未通过，已原样保留（请回报此数字，非零属异常）")
    if t_c:
        print(f"注意: {t_c} 个 compaction 项含 encrypted_content（结构必填，未自动处理；"
              "如续聊仍报错请回报，需单独设计）")
    reset_sqlite_projections(home, reset_ids)
    print("\n提示: 已字节等长剥离外来加密内容（所有字节偏移保持有效）；投影重置仅为清理")
    print("      缓存中的旧密文副本。请完全退出并重启 Codex 后重试续聊。")
    print("      若仍报 invalid_encrypted_content，用 --restore-backup 回滚后回报完整报错")
    print("      （可能是 compaction 项或上游不接受无密文的裸 reasoning 项，需再评估）。")


def restore_backup(backup_dir: Path, home: Path):
    if not backup_dir.exists():
        sys.exit(f"备份目录不存在: {backup_dir}")
    sessions_dir = home / "sessions"
    n = 0
    for p in backup_dir.rglob("*.jsonl"):
        rel = p.relative_to(backup_dir)
        dst = sessions_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dst, p.read_bytes())  # 原子写,防 NTFS 延迟写回窗口
        n += 1
    print(f"已从 {backup_dir} 恢复 {n} 个会话文件。")
    print("注意: SQLite 索引未回滚；如需一并回滚，用 *.pre-doctor-migrate-<时间戳>.bak")
    print("      覆盖对应 .sqlite 文件（先完全退出 Codex），再重启生效。")


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="诊断/修复 Codex 切换供应商后旧会话不可见的问题")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                    help="Codex 目录（默认 ~/.codex，或环境变量 CODEX_HOME）")
    ap.add_argument("--migrate", metavar="OLD_PROVIDER",
                    help="把属于 OLD_PROVIDER 的会话改写为当前 provider（改写前自动备份）")
    ap.add_argument("--strip-encrypted", action="store_true",
                    help="字节等长剥离外来 reasoning encrypted_content（跨供应商续聊 400 的根因）")
    ap.add_argument("--yes", action="store_true", help="迁移/剥离时跳过确认")
    ap.add_argument("--restore-backup", metavar="DIR", help="从备份目录回滚会话文件")
    args = ap.parse_args()

    home = Path(args.codex_home).expanduser()
    if not home.exists():
        sys.exit(f"Codex 目录不存在: {home}")

    if args.restore_backup:
        restore_backup(Path(args.restore_backup).expanduser(), home)
        return

    current_provider = diagnose(home)

    if args.migrate:
        if not current_provider:
            sys.exit("无法确定当前 model_provider，终止迁移。")
        migrate(home, args.migrate, current_provider, args.yes)

    if args.strip_encrypted:
        strip_encrypted(home, args.yes)


if __name__ == "__main__":
    main()
