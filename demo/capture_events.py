"""抓一次**真实**的事件流存成 NDJSON，用于前端开发与对齐。

为什么需要它：前端要渲染的是"服务端到底吐了什么"，而不是我们**以为**它吐了什么。
把真实事件抓下来数一数类型和字段，才能避免"照着旧前端抄一遍、结果字段早变了"这种事。

用法：
  python demo/capture_events.py "读 main 画布，告诉我它有多少节点、有没有环"
  python demo/capture_events.py --path /api/team --body '{"team":"dramaTeam","task":"…"}'
  python demo/capture_events.py --url http://127.0.0.1:8000

输出：workspace/events-<时间戳>.ndjson（原样一行一个事件）+ 控制台给类型统计。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取真实 NDJSON 事件流")
    parser.add_argument("prompt", nargs="?", default="读 main 画布，告诉我它有多少节点、有没有环")
    parser.add_argument("--path", default="/api/agent", help="POST 路径（/api/agent 或 /api/team）")
    parser.add_argument("--body", default="", help="直接给完整 body（JSON 字符串），给定时忽略 prompt")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--session", default="", help="session_id（默认每次新建）")
    args = parser.parse_args()

    if args.body:
        body = json.loads(args.body)
    else:
        body = {"prompt": args.prompt}
    if args.session:
        body["session_id"] = args.session

    out_path = WORKSPACE / f"events-{int(time.time())}.ndjson"
    request = urllib.request.Request(
        args.url + args.path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    counts: Counter[str] = Counter()
    fields: dict[str, set[str]] = {}
    samples: dict[str, dict] = {}
    lines: list[str] = []

    with urllib.request.urlopen(request, timeout=600) as response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                counts["<坏行>"] += 1
                continue
            kind = event.get("type", "<无 type>")
            counts[kind] += 1
            fields.setdefault(kind, set()).update(event.keys())
            samples.setdefault(kind, event)

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"共 {len(lines)} 个事件 → {out_path}")
    print(f"\n{'事件类型':<24}{'次数':>6}   字段")
    for kind, count in counts.most_common():
        keys = ", ".join(sorted(fields.get(kind, set())))
        print(f"{kind:<24}{count:>6}   {keys}")

    print("\n每种事件的第一个样本（供前端对齐字段）：")
    for kind, event in samples.items():
        print(f"  {kind}: {json.dumps(event, ensure_ascii=False)[:220]}")

    text_events = [k for k in counts if "delta" in fields.get(k, set())]
    print(f"\n带 delta 字段的事件（流式文本靠它们）：{text_events or '无'}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    main()
