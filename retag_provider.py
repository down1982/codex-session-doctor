#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
retag_provider — 给 session_meta 缺 model_provider 的 rollout 补写该字段

背景(2026-09-27): 新版 Codex 迁移/重写的 rollout 在 session_meta 里不再记录
model_provider; SQLite threads 表(列表过滤数据源)不受影响, 但 CLI resume 选择器、
doctor 统计等直接读 rollout 的路径会显示"(未记录)"。本脚本在文件内补齐字段。

行为:
- 只重写 session_meta 所在行(插入 "model_provider"), 其余行字节原样保留;
- 行长变化使 SQLite 投影的字节偏移失效 → 自动重置该线程投影四表
  (官方 rollout_migration.rs 同款, Codex 下次打开时全量重投影);
- 全程原子写(tmp+fsync+replace, 清零事故后标准); 默认 dry-run, --yes 才落盘。

用法:
  python retag_provider.py --codex-home <dir> --provider custom f1.jsonl [f2...]
  python retag_provider.py --codex-home <dir> --from-threads        # 扫 sessions/ 自动配对
  (两者都可加 --yes 执行; --from-threads 默认上限 200 个文件)
"""
import argparse
import json
import os
import sys
from pathlib import Path

from codex_session_doctor import (atomic_write_bytes, reset_sqlite_projections,
                                  sqlite_provider_maps)


def load_head(raw: bytes):
    """返回 (head_bytes_until_after_first_line, line1_text_or_None, obj1)。"""
    nl = raw.find(b"\n")
    first = raw if nl < 0 else raw[:nl]
    rest = b"" if nl < 0 else raw[nl + 1:]
    try:
        obj = json.loads(first.decode("utf-8"))
    except Exception:
        return None, None, None
    return (first, rest), first, obj


def retag_file(path: Path, provider: str):
    """返回 (thread_id, err)。thread_id 为 None 表示无需/无法处理。"""
    raw = path.read_bytes()
    parts, first, obj = load_head(raw)
    if obj is None or not isinstance(obj, dict):
        return None, f"首行不是合法 JSON"
    if obj.get("type") != "session_meta" or not isinstance(obj.get("payload"), dict):
        # session_meta 偶不在首行: 最多再看前 3 个非空行
        return None, "首行不是 session_meta(本脚本保守处理, 只认首行)"
    p = obj["payload"]
    if p.get("model_provider"):
        return None, None          # 已有, 无需处理
    tid = p.get("session_id") or p.get("id") or ""
    p["model_provider"] = provider
    new_first = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    rest = parts[1]
    data = new_first + (b"\n" + rest if rest or raw.endswith(b"\n") else b"")
    atomic_write_bytes(path, data)
    return (str(tid) or None), None


def main():
    ap = argparse.ArgumentParser(description="补写 session_meta.model_provider")
    ap.add_argument("files", nargs="*", help="目标 rollout .jsonl(与 --from-threads 二选一)")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME")
                    or str(Path.home() / ".codex"))
    ap.add_argument("--provider", help="要写入的 provider id")
    ap.add_argument("--from-threads", action="store_true",
                    help="扫描 sessions/ 中缺字段的文件, 按 threads 表自动配对 provider")
    ap.add_argument("--yes", action="store_true", help="执行(默认 dry-run)")
    ap.add_argument("--limit", type=int, default=200, help="--from-threads 的文件数上限")
    args = ap.parse_args()

    home = Path(args.codex_home).expanduser()
    if args.from_threads:
        if args.files:
            sys.exit("--from-threads 与位置参数文件列表互斥")
        id_map, name_map = sqlite_provider_maps(home)
        if not (id_map or name_map):
            sys.exit("threads 表不可读(无 SQLite 索引或无 model_provider 列)")
        plan = []
        for p in sorted((home / "sessions").rglob("*.jsonl")):
            if len(plan) >= args.limit:
                print(f"... 达到 --limit {args.limit}, 停止扫描")
                break
            try:
                _, _, obj = load_head(p.read_bytes())
            except OSError:
                continue
            if (isinstance(obj, dict) and obj.get("type") == "session_meta"
                    and isinstance(obj.get("payload"), dict)
                    and not obj["payload"].get("model_provider")):
                pp = obj["payload"]
                prov = (id_map.get(str(pp.get("session_id") or pp.get("id") or "").lower())
                        or name_map.get(p.name))
                if prov:
                    plan.append((p, prov))
        if not plan:
            print("sessions/ 中没有需要补 provider 的文件(或 threads 表配不上)。")
            return
        print(f"--from-threads: 命中 {len(plan)} 个文件:")
        for p, prov in plan[:20]:
            print(f"  - {p.name}  → {prov}")
        if len(plan) > 20:
            print(f"  ... 及另外 {len(plan) - 20} 个")
        if not args.yes:
            print("\n(dry-run, 加 --yes 执行)")
            return
        tids = []
        for p, prov in plan:
            tid, err = retag_file(p, prov)
            if err:
                print(f"  ! {p.name}: {err}")
            elif tid:
                tids.append(tid)
                print(f"  + {p.name} → {prov}")
        reset_sqlite_projections(home, tids, tag="retag")
        print("\n完成。请完全退出并重启 Codex 后核对列表/统计。")
        return

    if not args.files:
        sys.exit("请给文件列表, 或用 --from-threads")
    if not args.provider:
        sys.exit("请用 --provider 指定要写入的 provider id")
    if not args.yes:
        print("(dry-run) 将处理:")
        for f in args.files:
            print(f"  - {f}  → {args.provider}")
        print("加 --yes 执行。")
        return
    tids = []
    for f in args.files:
        p = Path(f).expanduser()
        tid, err = retag_file(p, args.provider)
        if err:
            print(f"  ! {p.name}: {err}")
        elif tid:
            tids.append(tid)
            print(f"  + {p.name} → {args.provider}")
        else:
            print(f"  = {p.name}: 已有 provider, 跳过")
    reset_sqlite_projections(home, tids, tag="retag")
    print("\n完成。请完全退出并重启 Codex 后核对列表/统计。")


if __name__ == "__main__":
    main()
