"""一键验收：把整条链路的 demo 与 HTTP 接口全跑一遍，产出报告。

跑法：
    python demo/acceptance.py            # 全部
    python demo/acceptance.py --api-only # 只验后端接口（需要服务已启动）

每一项都会真调模型 / 真发 HTTP，所以耗时以分钟计、会产生 API 费用。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
TOKEN_RE = re.compile(r"total_tokens\D{0,6}(\d+)")

CLI_CASES: list[tuple[str, str, list[str]]] = [
    ("阶段0 · 流式 + tool calling", "demo/smoke.py", ["当前目录里有哪些文件和文件夹？"]),
    ("模块一 · 工具系统 + 路径沙箱", "demo/run_tools.py", []),
    ("模块二 · 委派 + 双通道 + 用量", "demo/run_delegation.py", []),
    ("模块二 · 增量 vs 全量计量", "demo/usage_experiment.py", []),
    ("模块二 · 父中止 → 子中止", "demo/cancel_cascade.py", []),
    ("上下文 · 压缩 + 会话落盘", "demo/compact_demo.py", []),
    ("上下文 · 会话树分支", "demo/branch_demo.py", []),
    ("协作 · 声明式多 Agent 团队", "demo/run_team.py", []),
    ("技能 · 渐进式披露", "demo/skills_demo.py", []),
    ("画布 · 成环防护 + 精确定位", "demo/canvas_cycle.py", []),
    ("事件总线 · 不丢生命周期事件", "demo/events_overflow.py", []),
    ("会话不变量 · 取消/并发/工具集/恢复", "demo/session_invariants.py", []),
    # 注意：这一项最贵（建图约 9-10 万 tokens、20+ 次工具调用），赶时间时用 --cli-range 跳过
    ("产品化 P0 · 短剧生产图", "demo/drama_demo.py", []),
]


def run_cli(label: str, script: str, args: list[str]) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, script, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )
    elapsed = time.perf_counter() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    tokens = TOKEN_RE.findall(output)
    ok = proc.returncode == 0 and "Traceback" not in output
    return {
        "label": label,
        "kind": "cli",
        "target": script,
        "ok": ok,
        "seconds": round(elapsed, 1),
        "tokens": int(tokens[-1]) if tokens else None,
        "tail": [line for line in output.strip().splitlines() if line.strip()][-3:],
    }


def http_call(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def http_stream(path: str, payload: dict) -> tuple[int, dict[str, int], list[str]]:
    """读 NDJSON 流：统计事件类型，返回最后几行（对应前端要消费的东西）。"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    counts: dict[str, int] = {}
    lines: list[str] = []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            counts[event.get("type", "?")] = counts.get(event.get("type", "?"), 0) + 1
            lines.append(text)
    return 200, counts, lines[-3:]


def run_api_cases() -> list[dict]:
    cases: list[dict] = []

    def record(label: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail, ok = fn()
        except Exception as exc:  # noqa: BLE001
            cases.append(
                {
                    "label": label,
                    "kind": "api",
                    "target": label,
                    "ok": False,
                    "seconds": round(time.perf_counter() - started, 1),
                    "tokens": None,
                    "tail": [f"{type(exc).__name__}: {exc}"],
                }
            )
            return
        cases.append(
            {
                "label": label,
                "kind": "api",
                "target": label,
                "ok": ok,
                "seconds": round(time.perf_counter() - started, 1),
                "tokens": None,
                "tail": [detail],
            }
        )

    def get_case(label: str, path: str, expect_key: str):
        def _inner() -> tuple[str, bool]:
            status, body = http_call("GET", path)
            data = json.loads(body)
            return f"HTTP {status}｜{json.dumps(data, ensure_ascii=False)[:110]}", status == 200 and expect_key in data

        record(label, _inner)

    get_case("后端 · GET /api/health", "/api/health", "ok")
    get_case("后端 · GET /api/tools", "/api/tools", "tools")
    get_case("后端 · GET /api/skills", "/api/skills", "skills")
    get_case("后端 · GET /api/teams", "/api/teams", "teams")

    # 用唯一 session_id：复用旧会话时模型会直接用历史回答、不再调工具，验收会假通过
    sid = f"acceptance-{int(time.time())}"

    def agent_stream() -> tuple[str, bool]:
        status, counts, tail = http_stream(
            "/api/agent",
            {"prompt": "用 read_file 读 sample.py，告诉我它有多少行", "session_id": sid},
        )
        ok = (
            status == 200
            and counts.get("done", 0) >= 1
            and counts.get("error", 0) == 0
            and any(k.startswith("tool_execution") for k in counts)
        )
        detail = f"HTTP {status}｜事件 {json.dumps(counts, ensure_ascii=False)}｜末尾 {tail[-1][:90] if tail else ''}"
        return detail, ok

    record("后端 · POST /api/agent（NDJSON 流）", agent_stream)

    get_case("后端 · GET /api/sessions/{id}/stats", f"/api/sessions/{sid}/stats", "main")
    get_case("后端 · GET /api/sessions/{id}/entries", f"/api/sessions/{sid}/entries?limit=5", "entries")
    get_case("后端 · GET /api/sessions/{id}/subagents", f"/api/sessions/{sid}/subagents", "sub_agents")

    # 连线接口：类型不匹配必须被拒 —— 证明校验在服务端（前端只是提示），不是靠自觉
    def connect_validates_types() -> tuple[str, bool]:
        _, body = http_call("GET", "/api/canvas?name=main")
        canvas = json.loads(body)
        texts = [n for n in canvas["nodes"] if n["type"] == "text"]
        prompts = [n for n in canvas["nodes"] if n["type"] == "prompt"]
        if not texts or not prompts:
            return "跳过：main 画布缺少 text/prompt 节点", True
        payload = {
            "canvas": "main",
            "source": texts[0]["id"],
            "source_handle": "text",
            "target": prompts[0]["id"],
            "target_handle": "ref",  # STRING → IMAGE，必须被拒
        }
        try:
            status, body = http_call("POST", "/api/canvas/connect", payload)
            return f"HTTP {status}｜本该被拒却通过了", False
        except urllib.error.HTTPError as exc:
            detail = json.loads(exc.read().decode()).get("detail", "")
            return f"HTTP {exc.code}｜{detail}", exc.code == 400 and "类型不匹配" in detail

    record("后端 · POST /api/canvas/connect（类型校验）", connect_validates_types)

    # 成环防护：把一条既有边的方向反过来接 —— 必然成环，必须被拒。
    # 失败时这条边会真的写进画布（那是真 bug），事后用 demo/canvas_doctor.py 清。
    def connect_rejects_cycle() -> tuple[str, bool]:
        _, body = http_call("GET", "/api/canvas?name=main")
        canvas = json.loads(body)
        spec = canvas.get("node_spec", {})
        nodes = {n["id"]: n for n in canvas["nodes"]}
        existing = {(e["source"], e["target"]) for e in canvas["edges"]}
        for edge in canvas["edges"]:
            src, dst = nodes.get(edge["source"]), nodes.get(edge["target"])
            if not src or not dst or (dst["id"], src["id"]) in existing:
                continue
            out_ports = spec.get(dst["type"], {}).get("out", {})
            in_ports = spec.get(src["type"], {}).get("in", {})
            for out_handle, out_type in out_ports.items():
                for in_handle, in_type in in_ports.items():
                    if out_type != in_type and "*" not in (out_type, in_type):
                        continue  # 类型都不兼容，走不到成环那一步
                    payload = {
                        "canvas": "main",
                        "source": dst["id"],
                        "source_handle": out_handle,
                        "target": src["id"],
                        "target_handle": in_handle,
                    }
                    try:
                        status, _ = http_call("POST", "/api/canvas/connect", payload)
                        return f"HTTP {status}｜反向边本该被拒却通过了（已写进画布，请用 canvas_doctor 清理）", False
                    except urllib.error.HTTPError as exc:
                        detail = json.loads(exc.read().decode()).get("detail", "")
                        return f"HTTP {exc.code}｜{detail[:90]}", exc.code == 400 and "环" in detail
        return "跳过：画布上没有可用于构造环的边对", True

    record("后端 · POST /api/canvas/connect（拒绝成环）", connect_rejects_cycle)
    return cases


def render(cases: list[dict]) -> str:
    lines = [
        "| # | 验收项 | 结果 | 耗时(s) | tokens | 摘要 |",
        "|---|---|---|---|---|---|",
    ]
    for position, case in enumerate(cases, 1):
        number = case.get("index") or position
        summary = case["tail"][-1].replace("|", "/")[:90] if case["tail"] else ""
        lines.append(
            f"| {number} | {case['label']} | {'PASS' if case['ok'] else 'FAIL'} | "
            f"{case['seconds']} | {case['tokens'] if case['tokens'] is not None else '-'} | {summary} |"
        )
    return "\n".join(lines)


def main() -> None:
    api_only = "--api-only" in sys.argv
    with_api = "--with-api" in sys.argv
    cli_range: tuple[int, int] | None = None
    if "--cli-range" in sys.argv:
        raw = sys.argv[sys.argv.index("--cli-range") + 1]
        start, end = (int(x) for x in raw.split(","))
        cli_range = (start, end)

    selected = [(i, case) for i, case in enumerate(CLI_CASES, 1)]
    if cli_range:
        selected = [(i, case) for i, case in selected if cli_range[0] <= i <= cli_range[1]]

    cases: list[dict] = []
    if not api_only:
        for index, (label, script, args) in selected:
            print(f"→ [{index}] {label} …", flush=True)
            case = run_cli(label, script, args)
            case["index"] = index
            print(f"  {'PASS' if case['ok'] else 'FAIL'}（{case['seconds']}s） tokens={case['tokens']}", flush=True)
            cases.append(case)

    if api_only or with_api or not cli_range:
        print("→ 后端接口 …", flush=True)
        cases.extend(run_api_cases())

    passed = sum(1 for c in cases if c["ok"])
    table = render(cases)
    print(f"\n=== 本次验收 {passed}/{len(cases)} 通过 ===\n{table}")

    # 报告按项合并写入，分批跑也不会覆盖前一批的结果
    report_path = ROOT / "acceptance_report.json"
    existing: dict = {}
    if report_path.exists():
        try:
            existing = {c["label"]: c for c in json.loads(report_path.read_text(encoding="utf-8"))["cases"]}
        except (json.JSONDecodeError, KeyError):
            existing = {}
    existing.update({c["label"]: c for c in cases})
    cli_labels = [c[0] for c in CLI_CASES]
    order = [label for label in cli_labels if label in existing] + [
        label for label in existing if label not in cli_labels
    ]
    merged = [existing[label] for label in order]
    report_path.write_text(
        json.dumps({"passed": sum(1 for c in merged if c["ok"]), "total": len(merged), "cases": merged},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (ROOT / "acceptance_report.md").write_text(
        f"# 验收报告（{time.strftime('%Y-%m-%d %H:%M')}）\n\n"
        f"通过 {sum(1 for c in merged if c['ok'])}/{len(merged)}\n\n{render(merged)}\n",
        encoding="utf-8",
    )
    print(f"\n报告已写入 acceptance_report.md / .json｜本次失败项：{[c['label'] for c in cases if not c['ok']]}")


if __name__ == "__main__":
    if os.name == "nt":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
