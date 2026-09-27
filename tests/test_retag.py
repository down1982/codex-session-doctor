#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retag_provider 冒烟测试: 无 provider 的 rollout → 补写 → 仅第1行变化+投影重置。"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT
TOOL = REPO / "retag_provider.py"
HOME = ROOT / "tmp" / "test_retag"

HIST = """
CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT);
CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER);
CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY,
    next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER);
"""
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def jline(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def main():
    if HOME.exists():
        shutil.rmtree(HOME)
    sess = HOME / "sessions" / "2026" / "09" / "27"
    sess.mkdir(parents=True)
    (HOME / "config.toml").write_text('model_provider = "custom"\n', encoding="utf-8")
    lines = [
        {"type": "session_meta", "payload": {"id": "r1", "cwd": "C:\\p"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "内容行不动"}]}},
    ]
    p = sess / "rollout-2026-09-27T13-00-00-r1.jsonl"
    raw = ("\n".join(jline(x) for x in lines) + "\n").encode()
    p.write_bytes(raw)
    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    con.executescript(HIST)
    con.execute("INSERT INTO thread_history_projection_state VALUES('r1',?,?)",
                (len(raw), 2))
    con.commit(); con.close()

    r = subprocess.run([sys.executable, str(TOOL), "--codex-home", str(HOME),
                        "--provider", "custom", str(p), "--yes"],
                       capture_output=True, text=True, timeout=60)
    print(r.stdout + r.stderr)
    check("retag 正常退出", r.returncode == 0)
    new = p.read_bytes()
    check("文件总行数不变", len(new.splitlines()) == 2)
    l1 = json.loads(new.splitlines()[0])
    check("第1行已含 model_provider", l1["payload"].get("model_provider") == "custom")
    check("其余行字节不变", new.splitlines()[1] == raw.splitlines()[1])
    check("无 .rebuild-tmp/doctor-tmp 残留",
          not list(HOME.rglob("*tmp")))
    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    check("投影已重置(行长变化)",
          con.execute("SELECT COUNT(*) FROM thread_history_projection_state").fetchone()[0] == 0)
    con.close()
    # 幂等: 再跑一次 → 已有 provider, 跳过
    r2 = subprocess.run([sys.executable, str(TOOL), "--codex-home", str(HOME),
                         "--provider", "custom", str(p), "--yes"],
                        capture_output=True, text=True, timeout=60)
    check("重复执行跳过(幂等)", "已有 provider" in (r2.stdout + r2.stderr))

    print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
