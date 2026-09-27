#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""doctor v2 + 原子写 快速回归测试。

对齐 local-agent 的 v2 CLI(无 --drop-reasoning): 只读诊断(新旧消息格式) →
migrate 等长(投影不重置) → strip 等长(投影重置, 因 v2 策略性重置缓存) → 回滚。
附带验证: 原子写不残留 .doctor-tmp、无尾换行保持、备份完整。
运行: python3 scripts/test_doctor_v2.py
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT
DOCTOR = REPO / "codex_session_doctor.py"
HOME = ROOT / "tmp" / "test_doctor_v2"

THREADS_SCHEMA = """
CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
    created_at INTEGER, updated_at INTEGER, source TEXT, model_provider TEXT,
    cwd TEXT, title TEXT, sandbox_policy TEXT, approval_mode TEXT);
"""
HIST_SCHEMA = """
CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT);
CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT,
    rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT);
CREATE TABLE thread_history_projection_state (
    thread_id TEXT PRIMARY KEY, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER);
"""

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def jline(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def run(*args):
    r = subprocess.run([sys.executable, str(DOCTOR), "--codex-home", str(HOME), *args],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def q(sql):
    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def main():
    if HOME.exists():
        subprocess.run(["rm", "-rf", str(HOME)], check=True)
    (HOME / "sessions" / "2026" / "09" / "27").mkdir(parents=True)
    (HOME / "archived_sessions").mkdir()
    HOME.joinpath("config.toml").write_text(
        'model_provider = "custom"\nmodel = "m"\n', encoding="utf-8")
    sess = HOME / "sessions" / "2026" / "09" / "27"

    # a1: 旧 provider=openai, 含 event_msg 旧格式 + 无尾换行
    a1 = [
        {"type": "session_meta", "payload": {"id": "a1", "cwd": "C:\\p1"}},
        {"type": "turn_context", "payload": {"model_provider": "openai", "model": "gpt-5.5"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "旧格式会话"}},
        {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_x1",
            "summary": [], "encrypted_content": "gAAAAAblob1"}},
    ]
    pa1 = sess / "rollout-2026-09-27T10-00-00-a1.jsonl"
    pa1.write_bytes(("\n".join(jline(x) for x in a1)).encode("utf-8"))  # 无尾换行
    # b2: openai, 新格式 response_item + 加密 reasoning
    b2 = [
        {"type": "session_meta", "payload": {"id": "b2", "cwd": "C:\\p2"}},
        {"type": "turn_context", "payload": {"model_provider": "openai", "model": "gpt-5.5"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "新格式会话"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_x2",
            "summary": [], "encrypted_content": "gAAAAAblob22"}},
    ]
    pb2 = sess / "rollout-2026-09-27T11-00-00-b2.jsonl"
    pb2.write_bytes(("\n".join(jline(x) for x in b2) + "\n").encode("utf-8"))
    # c3: codex 迁移重写型 —— session_meta/turn_context 均无 provider(靠 threads 表兜底)
    c3 = [
        {"type": "session_meta", "payload": {"id": "c3", "cwd": "C:\\p3"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "迁移重写的会话"}]}},
    ]
    pc3 = sess / "rollout-2026-09-27T12-00-00-c3.jsonl"
    pc3.write_bytes(("\n".join(jline(x) for x in c3) + "\n").encode("utf-8"))
    orig_a1, orig_b2 = pa1.read_bytes(), pb2.read_bytes()

    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    con.executescript(THREADS_SCHEMA + HIST_SCHEMA)
    for tid, prov in (("a1", "openai"), ("b2", "openai"),
                      ("c3", "custom")):
        con.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (tid, "x", 1, 1, "vscode", prov, "c:/w", "t", "s", "a"))
    con.execute("INSERT INTO thread_history_projection_state VALUES('a1',?,?)",
                (len(orig_a1), 4))
    con.execute("INSERT INTO thread_history_projection_state VALUES('b2',?,?)",
                (len(orig_b2), 4))
    con.execute("INSERT INTO thread_items VALUES('a1','t1','i1',1,1,'{}')")
    con.commit()
    con.close()

    print("== 阶段1: 只读诊断 ==")
    code, out = run()
    check("诊断正常退出", code == 0)
    check("新旧格式首条消息都解析", "旧格式会话" in out and "新格式会话" in out)
    check("provider 兜底生效(patch3)", "provider 兜底" in out and "1 个会话文件的 session_meta" in out)
    check("c3 归入 custom 组可见", "custom" in out and "1 个会话   [可见" in out)
    check("无(未记录)分组", "(未记录)" not in out)

    print("== 阶段2: migrate openai→custom（等长）==")
    code, out = run("--migrate", "openai", "--yes")
    check("migrate 正常退出", code == 0)
    na1 = pa1.read_bytes()
    check("a1 provider 已改写", b'"model_provider":"custom"' in na1)
    check("a1 总字节不变(投影偏移有效)", len(na1) == len(orig_a1),
          f"{len(orig_a1)}→{len(na1)}")
    check("a1 仍无末尾换行", not na1.endswith(b"\n"))
    check("无 .doctor-tmp 残留", not list(sess.glob("*.doctor-tmp")))

    print("== 阶段3: --strip-encrypted（v2 等长剥离）==")
    code, out = run("--strip-encrypted", "--yes")
    check("strip 正常退出", code == 0)
    check("a1 密文已剥离", b"encrypted_content" not in pa1.read_bytes())
    check("a1 rs_ id 已剥离", b"rs_x1" not in pa1.read_bytes())
    check("a1 字节仍等长(行尾补空格)", len(pa1.read_bytes()) == len(orig_a1))
    check("b2 密文已剥离", b"encrypted_content" not in pb2.read_bytes())
    check("b2 字节仍等长", len(pb2.read_bytes()) == len(orig_b2))
    check("投影已重置(v2 策略: 清缓存防密文回流)",
          q("SELECT COUNT(*) FROM thread_history_projection_state")[0][0] == 0)
    strip_bak = sorted(HOME.glob("sessions_backup_*"))[-1]
    check("strip 备份了原始字节",
          (strip_bak / "2026" / "09" / "27" / "rollout-2026-09-27T10-00-00-a1.jsonl").read_bytes()
          == orig_a1.replace(b'"openai"', b'"custom"'))

    print("== 阶段4: --restore-backup 回滚 ==")
    code, out = run("--restore-backup", str(strip_bak))
    check("回滚正常退出", code == 0)
    check("回滚后仍等长(migrate 后状态)", len(pa1.read_bytes()) == len(orig_a1))
    check("无 .doctor-tmp 残留", not list(HOME.rglob("*.doctor-tmp")))

    print(f"\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", *[f"  - {f}" for f in FAIL], sep="\n")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
