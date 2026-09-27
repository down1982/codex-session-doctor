#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rebuild_from_projection 端到端测试。

构造 2 个线程的迷你 salvage 数据（含跨文件重叠条目）+ 假 CODEX_HOME（全零 live 文件
+ threads/投影四表），验证 dry-run 产物与 --install 全链路（隔离/原子写/投影重置/
rollout_path 更新/备份）。运行: python3 scripts/test_rebuild.py
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT
TOOL = REPO / "rebuild_from_projection.py"
WORK = ROOT / "tmp" / "test_rebuild"
SALV = WORK / "recovery_projection_x"
HOME = WORK / "fake_home"

THREADS_SCHEMA = """
CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
    created_at INTEGER, updated_at INTEGER, source TEXT, model_provider TEXT,
    cwd TEXT, title TEXT, sandbox_policy TEXT, approval_mode TEXT);
"""
HIST_SCHEMA = """
CREATE TABLE thread_turns (thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER, status TEXT);
CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, rollout_ordinal INTEGER);
CREATE TABLE thread_history_projection_state (thread_id TEXT PRIMARY KEY,
    next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER);
"""

T_MERGED = "aaaa1111-0000-4000-8000-000000000001"
T_SINGLE = "aaaa1111-0000-4000-8000-000000000002"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


def jline(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def srow(ordinal, turn, item):
    return jline({"ordinal": ordinal, "turn_id": turn, "item_type": item["type"],
                  "item_json": json.dumps(item, ensure_ascii=False)})


def build():
    if WORK.exists():
        shutil.rmtree(WORK)
    SALV.mkdir(parents=True)
    HOME.mkdir(parents=True)

    um = {"type": "userMessage", "id": f"{T_MERGED}-u1", "clientId": "c1",
          "content": [{"type": "text", "text": "检查服务器网络连通性\n", "text_elements": []}]}
    am = {"type": "agentMessage", "id": f"{T_MERGED}-a1", "text": "我会核实当前模型标识。",
          "phase": "commentary"}
    ce = {"type": "commandExecution", "id": f"{T_MERGED}-ce1", "command": "ping 10.0.0.1",
          "exitCode": 0, "aggregatedOutput": "..."}
    um2 = {"type": "userMessage", "id": f"{T_MERGED}-u2", "clientId": "c2",
           "content": [{"type": "text", "text": "第二问", "text_elements": []}]}
    am2 = {"type": "agentMessage", "id": f"{T_MERGED}-a2", "text": "第二答", "phase": "final"}

    f_root = SALV / f"rollout-2026-09-20T16-51-52-{T_MERGED}_aaaa1111-0000-4000-8000-000000000003.jsonl.salvage.jsonl"
    f_root.write_text("\n".join([
        srow(7, "t1", um), srow(8, "t1", am), srow(9, "t1", ce), srow(10, "t2", um2)]) + "\n", encoding="utf-8")
    f_cont = SALV / f"rollout-2026-09-20T18-21-39-{T_MERGED}_aaaa1111-0000-4000-8000-000000000004.jsonl.salvage.jsonl"
    f_cont.write_text("\n".join([
        srow(7, "t1", um), srow(8, "t1", am),          # 重叠条目(投影覆盖交叠)
        srow(11, "t2", am2)]) + "\n", encoding="utf-8")  # 新条目

    us = {"type": "userMessage", "id": f"{T_SINGLE}-u1", "clientId": "c3",
          "content": [{"type": "text", "text": "测试消息1", "text_elements": []}]}
    asr = {"type": "agentMessage", "id": f"{T_SINGLE}-a1", "text": "你好，我是测试助手。", "phase": "final"}
    rs = {"type": "reasoning", "id": "rs_dead", "summary": [], "content": []}
    f_single = SALV / f"rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl.salvage.jsonl"
    f_single.write_text("\n".join([srow(9, "t1", us), srow(10, "t1", rs), srow(11, "t1", asr)]) + "\n",
                        encoding="utf-8")

    meta = [
        {"id": T_MERGED, "created_at": 1789899699, "updated_at": 1789901124,
         "created_at_ms": 1789899699000, "updated_at_ms": 1789901124000,
         "model": "demo-model-1", "model_provider": "custom", "source": "vscode",
         "cwd": "\\\\?\\C:\\Users\\demo\\_Workspace\\Projects\\demo", "cli_version": "0.155.0-alpha.9.2",
         "originator": "Codex Desktop", "title": "服务器连通性测试",
         "git_sha": "abc123", "git_branch": "main",
         "rollout_path": f"C:\\Users\\demo\\.codex\\sessions\\2026\\09\\20\\rollout-2026-09-20T18-21-39-{T_MERGED}_aaaa1111-0000-4000-8000-000000000004.jsonl"},
        {"id": T_SINGLE, "created_at": 1790327623, "updated_at": 1790392573,
         "created_at_ms": 1790327623000, "updated_at_ms": 1790392573000,
         "model": "demo/gpt-x", "model_provider": "custom", "source": "vscode",
         "cwd": "\\\\?\\C:\\Users\\demo\\_Workspace\\Projects", "cli_version": "0.155.0-alpha.16.4",
         "originator": "Codex Desktop", "title": "问候语/模型身份",
         "rollout_path": f"\\\\?\\C:\\Users\\demo\\.codex\\sessions\\2026\\09\\25\\rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl"},
    ]
    (SALV / "_threads_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                              encoding="utf-8")

    # live: 全零文件 + sqlite
    d20 = HOME / "sessions" / "2026" / "09" / "20"
    d25 = HOME / "sessions" / "2026" / "09" / "25"
    d20.mkdir(parents=True)
    d25.mkdir(parents=True)
    (d20 / f"rollout-2026-09-20T18-21-39-{T_MERGED}_aaaa1111-0000-4000-8000-000000000004.jsonl").write_bytes(b"\x00" * 4096)
    (d20 / f"rollout-2026-09-20T16-51-52-{T_MERGED}_aaaa1111-0000-4000-8000-000000000003.jsonl").write_bytes(b"\x00" * 2048)
    (d25 / f"rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl").write_bytes(b"\x00" * 1024)
    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    con.executescript(THREADS_SCHEMA + HIST_SCHEMA)
    con.execute("INSERT INTO threads VALUES(?,?,?,?,'vscode','custom','c:/w','t','s','a')",
                (T_MERGED, f"C:\\x\\rollout-2026-09-20T18-21-39-{T_MERGED}_x.jsonl", 1, 2))
    con.execute("INSERT INTO threads VALUES(?,?,?,?,'vscode','custom','c:/w','t2','s','a')",
                (T_SINGLE, f"C:\\x\\rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl", 1, 2))
    con.execute("INSERT INTO thread_turns VALUES(?,?,?,?)", (T_MERGED, "t1", 7, "completed"))
    con.execute("INSERT INTO thread_items VALUES(?,?,?,?)", (T_MERGED, "t1", "i1", 7))
    con.execute("INSERT INTO thread_history_projection_state VALUES(?,?,?)", (T_MERGED, 4096, 9))
    con.commit()
    con.close()


def run(*args):
    r = subprocess.run([sys.executable, str(TOOL), *args],
                       capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def main():
    print("== 构造测试环境 ==")
    build()
    print("\n== 阶段1: dry-run 重建 ==")
    code, out = run(str(SALV))
    print(out)
    check("dry-run 正常退出", code == 0)
    out_dir = WORK / "recovery_rebuilt"
    merged = out_dir / f"rollout-2026-09-20T16-51-52-{T_MERGED}.jsonl"
    single = out_dir / f"rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl"
    check("合并线程产物存在(根式文件名)", merged.exists())
    check("单文件线程产物存在", single.exists())
    lines = [json.loads(l) for l in merged.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("首行为 session_meta", lines[0]["type"] == "session_meta")
    sm = lines[0]["payload"]
    check("session_meta 必填字段齐全",
          all(sm.get(k) for k in ("session_id", "id", "timestamp", "cwd", "originator", "cli_version")))
    check("session_meta 含 model_provider(patch1)", sm.get("model_provider") == "custom")
    check("session_id == 线程 id", sm["session_id"] == T_MERGED)
    check("省略 history_mode(默认 legacy)", "history_mode" not in sm)
    check("省略 history_base(无血缘依赖)", "history_base" not in sm)
    msgs = [l["payload"] for l in lines if l["type"] == "response_item"]
    check("重叠条目已去重(4 条消息非 6)", len(msgs) == 4, f"实际 {len(msgs)}")
    check("按 ordinal 排序(u1,a1,u2,a2)",
          [m["content"][0]["text"].strip() for m in msgs]
          == ["检查服务器网络连通性", "我会核实当前模型标识。", "第二问", "第二答"])
    check("commandExecution 未进 rollout", all(m.get("role") for m in msgs))
    sl = [json.loads(l) for l in single.read_text(encoding="utf-8").splitlines() if l.strip()]
    sm2 = [l["payload"] for l in sl if l["type"] == "response_item"]
    check("单文件线程 2 条消息(reasoning 跳过)", len(sm2) == 2)
    check("用户消息映射 input_text", sm2[0]["role"] == "user"
          and sm2[0]["content"][0]["type"] == "input_text")
    check("助手消息映射 output_text", sm2[1]["role"] == "assistant"
          and sm2[1]["content"][0]["type"] == "output_text")
    check("Markdown 存档存在", (out_dir / f"{T_MERGED}.md").exists()
          and (out_dir / f"{T_SINGLE}.md").exists())
    md = (out_dir / f"{T_MERGED}.md").read_text(encoding="utf-8")
    check("MD 含对话与工具摘要", "检查服务器" in md and "ping 10.0.0.1" in md)
    check("live 未被触碰(仍全零)",
          (HOME / "sessions/2026/09/20" / f"rollout-2026-09-20T18-21-39-{T_MERGED}_aaaa1111-0000-4000-8000-000000000004.jsonl").read_bytes() == b"\x00" * 4096)

    print("\n== 阶段2: --install 安装 ==")
    code, out = run(str(SALV), "--codex-home", str(HOME), "--install", "--yes")
    print(out)
    check("install 正常退出", code == 0)
    tgt_merged = HOME / "sessions/2026/09/20" / f"rollout-2026-09-20T16-51-52-{T_MERGED}.jsonl"
    tgt_single = HOME / "sessions/2026/09/25" / f"rollout-2026-09-25T17-13-43-{T_SINGLE}.jsonl"
    check("合并线程已写入 live", tgt_merged.exists()
          and tgt_merged.read_bytes() == merged.read_bytes())
    check("单文件线程已写入 live", tgt_single.exists()
          and tgt_single.read_bytes() == single.read_bytes())
    check("原全零文件已隔离(重命名)", list((HOME / "sessions/2026/09/25").glob("*.zeroed-quarantine-*")))
    check("合并血缘源(根文件16-51-52)已隔离(patch2)",
          list((HOME / "sessions/2026/09/20").glob("rollout-2026-09-20T16-51-52-*.zeroed-quarantine-*")))
    check("合并血缘源(续接18-21-39)已隔离(patch2)",
          list((HOME / "sessions/2026/09/20").glob("rollout-2026-09-20T18-21-39-*.zeroed-quarantine-*")))
    check("无残留 tmp 文件", not list(HOME.rglob("*.rebuild-tmp")))
    con = sqlite3.connect(HOME / "thread_history_1.sqlite")
    q = lambda s: con.execute(s).fetchall()
    check("投影四表已清空该线程",
          q(f"SELECT COUNT(*) FROM thread_history_projection_state WHERE thread_id='{T_MERGED}'")[0][0] == 0
          and q(f"SELECT COUNT(*) FROM thread_items WHERE thread_id='{T_MERGED}'")[0][0] == 0)
    rp = q(f"SELECT rollout_path FROM threads WHERE id='{T_MERGED}'")[0][0]
    check("合并线程 rollout_path 已更新(指向新根式文件)",
          f"rollout-2026-09-20T16-51-52-{T_MERGED}.jsonl" in rp and "18-21-39" not in rp, rp)
    rp2 = q(f"SELECT rollout_path FROM threads WHERE id='{T_SINGLE}'")[0][0]
    check("单文件线程 rollout_path 未变( basename 相同)", "17-13-43" in rp2, rp2)
    con.close()
    check("sqlite 已备份", list(HOME.glob("*.pre-rebuild-*.bak")))

    print(f"\n{'='*50}\n结果: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        print("失败项:", *[f"  - {f}" for f in FAIL], sep="\n")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
