#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rebuild_from_projection — 从 SQLite 投影抢救数据重建可续聊的 rollout 会话文件

背景（2026-09-27 清零事件）:
  47 个会话文件在 NTFS 延迟写回窗口中变为同尺寸全 \\x00（元数据已落盘、数据块未写入，
  与 openai/codex issue #26421 同一失败模式）。其中 5 个无文件级备份，已从
  thread_items.item_json 投影缓存提取为 *.salvage.jsonl（本工具的输入）。

  投影数据是 Desktop app-server 的 UI 条目格式（userMessage/agentMessage/...），
  与 rollout 的 {"timestamp","type","payload"} 信封不同，本工具做两件事:
  1. 转换重建: userMessage/agentMessage → response_item message（对话主干），
     恢复为 legacy 单文件 rollout（省略 history_mode → 默认 legacy）；
  2. 可读存档: 同步导出 Markdown（含 commandExecution/mcpToolCall 摘要）。

格式依据（openai/codex main, 2026-09）:
  - rollout/src/recorder.rs RolloutLineRef: {"timestamp": String, "ordinal"?: u64,
    ...RolloutItemWire flatten {"type","payload"}}
  - history/src/rollout_payload.rs: type ∈ session_meta/response_item/turn_context/...
  - protocol/src/protocol.rs SessionMeta: 必填 session_id/id/timestamp/cwd/originator/
    cli_version，其余（source/history_mode/history_base/...）均有默认或可缺省
  - list_threads.rs: rollout 缺 provider 时回退默认 provider → 重建文件可省 turn_context

用法:
  python rebuild_from_projection.py <salvage目录> [--codex-home <路径>] [--install] [--yes]
    默认: 只输出到 <salvage目录>/../recovery_rebuilt/（不动 live 会话）
    --install: 原子写入 CODEX_HOME/sessions/...（隔离原全零文件），
               重置该线程投影四表并按需更新 threads.rollout_path
"""
import argparse
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

SALVAGE_RE = re.compile(
    r"^rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})"
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?:_[0-9a-fA-F-]{36})?\.jsonl\.salvage\.jsonl$")

# 投影条目 → rollout response_item 的映射表（仅对话主干；其余类型跳过并计数）
SKIP_TYPES = ("reasoning", "commandExecution", "mcpToolCall",
              "fileChange", "contextCompaction")


def iso_z(unix_ts):
    dt = datetime.datetime.fromtimestamp(int(unix_ts), tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def unix_seconds(v):
    """threads 元数据的 created_at 为秒、created_at_ms 为毫秒，统一为秒。"""
    v = v or 0
    return v / 1000 if v > 10 ** 12 else v


def is_all_zero(p: Path) -> bool:
    """整文件是否全 \\x00（清零事故特征）。"""
    if not p.exists() or p.stat().st_size == 0:
        return False
    with open(p, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return True
            if chunk.count(b"\x00") != len(chunk):
                return False


def atomic_write_bytes(path: Path, data: bytes):
    """tmp + fsync + 原子替换。杜绝 open-truncate-写-关 的 NTFS 延迟写回窗口
    （本次清零事故与 codex issue #26421 的共同根因模式）。"""
    tmp = path.with_name(path.name + ".rebuild-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_salvage(salvage_dir: Path):
    """返回 {thread_id: {"rows": [...去重排序...], "earliest_ts": str, "files": [names]}}"""
    threads = {}
    for p in sorted(salvage_dir.glob("*.salvage.jsonl")):
        m = SALVAGE_RE.match(p.name)
        if not m:
            print(f"  ! 文件名不识别，跳过: {p.name}")
            continue
        ts, tid = m.group(1), m.group(2).lower()
        rec = threads.setdefault(tid, {"rows": {}, "earliest_ts": ts, "files": []})
        rec["files"].append(p.name)
        if ts < rec["earliest_ts"]:
            rec["earliest_ts"] = ts
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                item = json.loads(row["item_json"])
            except Exception:
                continue
            iid = item.get("id") or f"turn-{row.get('turn_id')}-ord-{row.get('ordinal')}"
            # 同一 item 可能被多个续接文件的投影重复覆盖 → 按 id 去重，保留最小 ordinal
            old = rec["rows"].get(iid)
            if old is None or row.get("ordinal", 0) < old.get("ordinal", 1 << 60):
                rec["rows"][iid] = {"ordinal": row.get("ordinal", 0),
                                    "turn_id": row.get("turn_id"),
                                    "item_type": row.get("item_type"),
                                    "item": item}
    return threads


def convert_items(rows):
    """投影条目 → (rollout response_item payload 列表, 跳过统计, md 片段列表)"""
    payloads, skipped, md_lines = [], {}, []
    for row in sorted(rows, key=lambda r: r["ordinal"]):
        it, t = row["item"], row["item_type"]
        if t == "userMessage":
            parts = [c for c in (it.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
            if not parts:
                skipped["userMessage(空)"] = skipped.get("userMessage(空)", 0) + 1
                continue
            payloads.append({"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": c["text"]} for c in parts]})
            for c in parts:
                md_lines.append(("user", c["text"]))
        elif t == "agentMessage":
            text = it.get("text")
            if not text:
                skipped["agentMessage(空)"] = skipped.get("agentMessage(空)", 0) + 1
                continue
            msg = {"type": "message", "role": "assistant",
                   "content": [{"type": "output_text", "text": text}]}
            if it.get("id"):
                msg["id"] = it["id"]
            payloads.append(msg)
            md_lines.append(("assistant", text))
        elif t == "commandExecution":
            skipped[t] = skipped.get(t, 0) + 1
            cmd = (it.get("command") or "").strip().splitlines()
            md_lines.append(("tool", f"$ {cmd[0][:160] if cmd else '?'}"
                                    f"  (exit={it.get('exitCode')})"))
        elif t == "mcpToolCall":
            skipped[t] = skipped.get(t, 0) + 1
            md_lines.append(("tool", f"[mcp] {it.get('server')}.{it.get('tool')}"
                                    f"  ({it.get('status')})"))
        else:
            skipped[t] = skipped.get(t, 0) + 1
    return payloads, skipped, md_lines


def rebuild_thread(tid, meta, rows, earliest_ts):
    """生成重建的 rollout 字节流与 markdown。"""
    m = meta.get(tid)
    if not m:
        return None, None, None
    payloads, skipped, md_lines = convert_items(list(rows.values()))
    if not payloads:
        return None, skipped, None
    created = unix_seconds(m.get("created_at_ms") or m.get("created_at"))
    session_meta = {
        "session_id": tid, "id": tid,
        "timestamp": iso_z(created),
        "cwd": m.get("cwd") or ".",
        "originator": m.get("originator") or "Codex Desktop",
        "cli_version": m.get("cli_version") or "0.0.0",
    }
    # patch(15:2x): provider 必须写进 session_meta —— 列表 provider 过滤在 SQLite
    # threads 表,但 CLI resume 选择器/doctor 统计读 rollout 自身,缺字段会显示"(未记录)"
    if m.get("model_provider"):
        session_meta["model_provider"] = m["model_provider"]
    if m.get("source"):
        session_meta["source"] = m["source"]
    if m.get("git_sha") or m.get("git_branch"):
        session_meta["git"] = {"sha": m.get("git_sha") or "",
                               "branch": m.get("git_branch") or ""}
    lines = [{"timestamp": session_meta["timestamp"], "type": "session_meta",
              "payload": session_meta}]
    updated = unix_seconds(m.get("updated_at_ms") or m.get("updated_at")) or created
    span = max(updated - created, 1)
    for i, p in enumerate(payloads):
        ts = created + int(span * (i + 1) / (len(payloads) + 1))
        lines.append({"timestamp": iso_z(ts), "type": "response_item", "payload": p})
    data = ("\n".join(json.dumps(l, ensure_ascii=False, separators=(",", ":"))
                      for l in lines) + "\n").encode("utf-8")

    md = [f"# {m.get('title') or m.get('name') or tid}", "",
          f"- thread: `{tid}`", f"- 模型: {m.get('model')} ({m.get('model_provider')})",
          f"- 时间: {iso_z(created)} ~ {iso_z(updated)}",
          f"- 重建自投影缓存；对话 {len(payloads)} 条；"
          f"跳过: {json.dumps(skipped, ensure_ascii=False)}", "", "---", ""]
    for role, text in md_lines:
        if role == "user":
            md += ["> **用户**", "", text, ""]
        elif role == "assistant":
            md += ["**助手**", "", text, ""]
        else:
            md.append(f"  - {text}")
    return data, skipped, "\n".join(md)


def sqlite_install(home: Path, updates):
    """updates: [(thread_id, new_rollout_path_abs)] → 备份后重置投影四表 + 更新 rollout_path"""
    dbs = sorted({p for p in home.rglob("*")
                  if p.suffix in (".sqlite", ".db", ".sqlite3")
                  and not p.name.endswith(".bak")
                  and not any("backup" in part.lower()
                              for part in p.relative_to(home).parts)})
    if not dbs:
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
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
            touched = False
            for tid, new_path in updates:
                qm = "?"
                for tbl in ("thread_items", "thread_realtime_items",
                            "thread_turns", "thread_history_projection_state"):
                    if tbl in tables:
                        con.execute(f"DELETE FROM {tbl} WHERE thread_id = {qm}", (tid,))
                        touched = True
                if "threads" in tables and new_path:
                    cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
                    if "rollout_path" in cols:
                        con.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                                    (new_path, tid))
                        touched = True
            if not touched:
                con.close()
                continue
            bak = db.with_name(f"{db.name}.pre-rebuild-{ts}.bak")
            src = sqlite3.connect(str(db))
            dst = sqlite3.connect(str(bak))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            con.commit()
            print(f"  {rel}: 投影已重置 / rollout_path 已更新（备份 {bak.name}）")
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


def main():
    ap = argparse.ArgumentParser(
        description="从投影抢救数据重建可续聊的 rollout 会话文件")
    ap.add_argument("salvage_dir", help="含 *.salvage.jsonl 与 _threads_meta.json 的目录")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME")
                    or str(Path.home() / ".codex"), help="Codex 目录（--install 时使用）")
    ap.add_argument("--install", action="store_true",
                    help="写入 live sessions/（默认仅输出到 recovery_rebuilt/）")
    ap.add_argument("--yes", action="store_true", help="install 时跳过确认")
    args = ap.parse_args()

    salvage_dir = Path(args.salvage_dir).expanduser().resolve()
    meta_path = salvage_dir / "_threads_meta.json"
    if not salvage_dir.exists() or not meta_path.exists():
        sys.exit(f"salvage 目录无效（需含 _threads_meta.json）: {salvage_dir}")
    meta = {}
    for m in json.load(open(meta_path, encoding="utf-8")):
        meta[m["id"].lower()] = m

    threads = load_salvage(salvage_dir)
    if not threads:
        sys.exit("未找到可用的 *.salvage.jsonl")

    out_dir = salvage_dir.parent / "recovery_rebuilt"
    out_dir.mkdir(parents=True, exist_ok=True)
    home = Path(args.codex_home).expanduser()
    install_plan = []
    print(f"==== 重建 {len(threads)} 个线程 → {out_dir} ====")
    for tid, rec in sorted(threads.items()):
        data, skipped, md = rebuild_thread(tid, meta, rec["rows"], rec["earliest_ts"])
        if data is None:
            print(f"  ! {tid}: 无可重建的对话条目（跳过 {skipped}），仅导出存档检查")
            continue
        single = len(rec["files"]) == 1
        base = (rec["files"][0][:-len(".salvage.jsonl")] if single
                else f"rollout-{rec['earliest_ts']}-{tid}.jsonl")
        (out_dir / base).write_bytes(data)          # 产物目录,非 live,普通写即可
        (out_dir / f"{tid}.md").write_text(md or "", encoding="utf-8")
        n_msg = data.count(b'"type":"response_item"')
        print(f"  + {base}")
        print(f"    对话 {n_msg} 条 | 输入文件 {len(rec['files'])} 个"
              f"{'（合并为单文件 legacy 模式）' if not single else ''}"
              f" | 跳过 {json.dumps(skipped, ensure_ascii=False)}")
        if args.install:
            m = meta[tid]
            orig = m.get("rollout_path") or ""
            dm = re.search(r"(\d{4})[\\/](\d{2})[\\/](\d{2})", orig)
            date_dir = f"{dm.group(1)}{os.sep}{dm.group(2)}{os.sep}{dm.group(3)}" if dm else \
                f"{rec['earliest_ts'][:4]}{os.sep}{rec['earliest_ts'][5:7]}{os.sep}{rec['earliest_ts'][8:10]}"
            target = home / "sessions" / date_dir / base
            new_abs = str(target.resolve()) if not orig.startswith("\\\\?\\") \
                else "\\\\?\\" + str(target.resolve())
            # patch(15:2x): 血缘源文件(全零原件)也纳入隔离清单 —— 合并线程的根/续接
            # 文件名 ≠ 新建目标名, 此前只隔离了同名目标, 源文件被留在 live 里
            sources = []
            for fname in rec["files"]:
                fm = SALVAGE_RE.match(fname)
                st = fm.group(1) if fm else rec["earliest_ts"]
                sdir = f"{st[:4]}{os.sep}{st[5:7]}{os.sep}{st[8:10]}"
                sources.append(home / "sessions" / sdir / fname[:-len(".salvage.jsonl")])
            install_plan.append((tid, target, new_abs, orig, base, sources))

    if not args.install:
        print(f"\n完成（dry-run）。产物在 {out_dir}，人工核对后加 --install 写入 live。")
        return
    if not install_plan:
        sys.exit("没有可安装的重建产物。")
    print(f"\n==== 安装 {len(install_plan)} 个线程到 {home} ====")
    if not args.yes:
        ans = input("将隔离原全零文件并写入重建文件，确认? [yes/N]: ").strip().lower()
        if ans != "yes":
            sys.exit("已取消。")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for tid, target, new_abs, orig, base, sources in install_plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        for src in sources:
            if src == target or not src.exists():
                continue
            if is_all_zero(src):
                qz = src.with_name(f"{src.name}.zeroed-quarantine-{ts}")
                os.replace(src, qz)
                print(f"  - 血缘源(全零)已隔离: {qz.name}")
            else:
                print(f"  ! 血缘源非全零,不隔离仅提示: {src.name}")
        if target.exists():
            suffix = ".zeroed-quarantine" if is_all_zero(target) else ".pre-rebuild-replace"
            qz = target.with_name(f"{target.name}{suffix}-{ts}")
            os.replace(target, qz)
            print(f"  - 原文件已隔离: {qz.name}")
        atomic_write_bytes(target, (out_dir / base).read_bytes())
        print(f"  + 已写入: {target.relative_to(home)}")
    print("\nSQLite 索引同步:")
    # 仅当新文件名与 threads.rollout_path 原 basename 不同（合并线程）才需要更新路径
    sqlite_install(home, [(tid, new_abs if base not in orig else "")
                          for tid, _, new_abs, orig, base, _src in install_plan])
    print("\n完成。请完全退出并重启 Codex 后在列表中核对这 "
          f"{len(install_plan)} 个线程（如列表未刷新，重启一次 Desktop）。")


if __name__ == "__main__":
    main()
