"""S6 真模型 3 镜小样：一条命令把「资产图 → 首帧 → 视频」跑成真模型产物并验收。

S6 的验收标准（docs/提示词与技能架构.md §6）：
  1. 每镜首帧的主体是角色（视觉判断，脚本只负责把关键帧路径打出来）；
  2. 镜头数 == len(shots)（每段 take 数与本段镜头数一致）；
  3. 单镜 ≤ 6 秒。

用法（默认只打印计划，不花钱）：
  E:\\python_tool\\python\\python.exe -X utf8 demo\\s6_real_sample.py ^
      --canvas story-S6-三镜小样-2c40cde9

确认计划后加 --execute 真跑：
  ... --execute

前提：服务用最新代码启动（改过 server/studio.py 之后必须重启），并且
      POST /api/media/providers/activate {"provider":"tfrouter"} 已生效。

产物：
  - 资产形象图、首帧图、视频都落在 workspace/ 下；
  - 验收报告写到 workspace/s6_report.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KINDS = ("character", "scene", "prop")


def _call(base: str, method: str, path: str, payload: dict | None = None, timeout: float = 300.0) -> dict:
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail", body)
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"HTTP {exc.code} {method} {path}\n  {detail}") from exc


def _q(canvas: str) -> str:
    return "?canvas=" + urllib.parse.quote(canvas)


def _plan_looks(base: str, canvas: str) -> list[dict]:
    assets = _call(base, "GET", "/api/studio/assets" + _q(canvas))
    plan: list[dict] = []
    for kind in KINDS:
        for item in assets.get(kind) or []:
            looks = [look for look in (item.get("looks") or []) if look.get("id")]
            if not looks:
                continue
            active = item.get("active_look_id") or looks[0]["id"]
            look = next((one for one in looks if one.get("id") == active), looks[0])
            plan.append(
                {
                    "kind": kind,
                    "node_id": item["node_id"],
                    "name": item.get("name") or item["node_id"],
                    "look_id": look["id"],
                    "look_name": look.get("name") or "",
                    "image_path": look.get("image_path") or "",
                    "mock": bool(look.get("mock")),
                }
            )
    return plan


def _plan_segments(base: str, canvas: str) -> list[dict]:
    payload = _call(base, "GET", "/api/studio/segments" + _q(canvas))
    return list(payload.get("segments") or [])


def _regenerate_looks(base: str, canvas: str, plan: list[dict], args) -> list[dict]:
    done: list[dict] = []
    for item in plan:
        if item["image_path"] and not item["mock"] and not args.force:
            print(f"  = 资产图已是真模型产物，跳过：{item['name']}")
            done.append(item)
            continue
        print(f"  → 生成资产图 {item['name']}（look {item['look_id']}, {args.image_model}）…", flush=True)
        body = {"look_id": item["look_id"], "model": args.image_model}
        if args.look_size:
            body["size"] = args.look_size
        result = _call(base, "POST", f"/api/studio/assets/{item['node_id']}/generate" + _q(canvas), body)
        look = (result or {}).get("look") or {}
        item = dict(item, image_path=look.get("image_path") or "", model=look.get("model") or "")
        done.append(item)
        print(f"    {item['image_path']}")
    return done


def _make_keyframes(base: str, canvas: str, segments: list[dict], args) -> list[dict]:
    out: list[dict] = []
    for seg in segments:
        node_id = seg["node_id"]
        fresh = _call(base, "GET", f"/api/studio/segments/{node_id}" + _q(canvas))
        keyframe = fresh.get("keyframe") or {}
        if keyframe.get("path") and not keyframe.get("mock") and not args.force:
            print(f"  = 首帧已是真模型产物，跳过：第 {fresh.get('segment_no')} 段")
            out.append({"node_id": node_id, "keyframe": keyframe})
            continue
        print(f"  → 生成第 {fresh.get('segment_no')} 段首帧（{args.image_model}）…", flush=True)
        body = {"model": args.image_model}
        if args.image_size:
            body["size"] = args.image_size
        result = _call(base, "POST", f"/api/studio/segments/{node_id}/keyframe" + _q(canvas), body)
        keyframe = (result or {}).get("keyframe") or {}
        out.append({"node_id": node_id, "keyframe": keyframe})
        print(f"    {keyframe.get('path')}")
    return out


def _wait_job(base: str, job_id: str, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        job = _call(base, "GET", f"/api/studio/jobs/{job_id}", timeout=60)
        status = str(job.get("status") or "")
        if status != last:
            print(f"    job {job_id}: {status}", flush=True)
            last = status
        if status in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(4)
    raise TimeoutError(f"job {job_id} 超过 {timeout_s:.0f}s 未结束")


def _run_videos(base: str, canvas: str, segments: list[dict], args) -> list[dict]:
    out: list[dict] = []
    for seg in segments:
        node_id = seg["node_id"]
        fresh = _call(base, "GET", f"/api/studio/segments/{node_id}" + _q(canvas))
        takes = [take for take in (fresh.get("takes") or []) if take.get("path")]
        if takes and not takes[-1].get("mock") and not args.force:
            print(f"  = 第 {fresh.get('segment_no')} 段已有真模型视频，跳过")
            out.append({"node_id": node_id, "take": takes[-1]})
            continue
        print(f"  → 生成第 {fresh.get('segment_no')} 段视频（{len(fresh.get('shots') or [])} 镜）…", flush=True)
        result = _call(base, "POST", f"/api/studio/segments/{node_id}/generate" + _q(canvas), {})
        job = (result or {}).get("job") or {}
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise RuntimeError(f"没有拿到 job_id：{result}")
        final = _wait_job(base, job_id, args.video_timeout)
        if final.get("status") != "succeeded":
            raise RuntimeError(f"视频生成失败：{final.get('error') or final}")
        take = final.get("take") or final.get("result") or {}
        out.append({"node_id": node_id, "take": take})
        print(f"    {take.get('path')}（{take.get('duration_s')}s, {take.get('model')}）")
    return out


def _verify(base: str, canvas: str) -> tuple[list[dict], bool]:
    segments = _plan_segments(base, canvas)
    rows: list[dict] = []
    ok = True
    for seg in segments:
        detail = _call(base, "GET", f"/api/studio/segments/{seg['node_id']}" + _q(canvas))
        shots = detail.get("shots") or []
        takes = [take for take in (detail.get("takes") or []) if take.get("path")]
        last = takes[-1] if takes else {}
        keyframe = detail.get("keyframe") or {}
        checks = {
            "镜头数 == take 数": len(takes) == len(shots),
            "take 是真模型产物": bool(takes) and not last.get("mock"),
            "首帧是真模型产物": bool(keyframe.get("path")) and not keyframe.get("mock"),
            "首帧模式下生成": str(last.get("mode") or "") == "first_frame" or last.get("mode") is None,
            "单镜 ≤ 6s": float(last.get("duration_s") or 0) <= 6.0,
            "镜长在 3–8s": all(3.0 <= float(shot.get("duration_s") or 0) <= 8.0 for shot in shots),
        }
        if not all(checks.values()):
            ok = False
        rows.append(
            {
                "segment_no": detail.get("segment_no"),
                "node_id": seg["node_id"],
                "shots": len(shots),
                "takes": len(takes),
                "duration_s": last.get("duration_s"),
                "model": last.get("model") or "",
                "video": last.get("path") or "",
                "keyframe": keyframe.get("path") or "",
                "checks": checks,
            }
        )
    return rows, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S6 真模型 3 镜小样")
    parser.add_argument("--base", default="http://127.0.0.1:8010")
    parser.add_argument("--canvas", default="story-S6-三镜小样-2c40cde9")
    parser.add_argument("--image-model", default="全能图片G-2.5")
    parser.add_argument("--look-size", default="1024x1024", help="资产形象图尺寸，空串用系统默认")
    parser.add_argument("--image-size", default="", help="首帧尺寸，空串用系统默认")
    parser.add_argument("--video-model", default="", help="写进项目设置的视频模型，空串不动")
    parser.add_argument("--video-timeout", type=float, default=1800.0)
    parser.add_argument("--force", action="store_true", help="已有真模型产物也重新生成")
    parser.add_argument("--skip-looks", action="store_true")
    parser.add_argument("--skip-keyframes", action="store_true")
    parser.add_argument("--skip-videos", action="store_true")
    parser.add_argument("--report", default="workspace/s6_report.json")
    parser.add_argument("--execute", action="store_true", help="真的花钱跑；不加只打印计划")
    args = parser.parse_args(argv)

    base, canvas = args.base, args.canvas
    print(f"服务 {base}，画布 {canvas}")
    health = _call(base, "GET", "/api/health", timeout=30)
    print(f"  health: {json.dumps(health, ensure_ascii=False)}")

    looks = _plan_looks(base, canvas)
    segments = _plan_segments(base, canvas)
    print(f"\n计划：{len(looks)} 张资产图 → {len(segments)} 张首帧 → {len(segments)} 段视频")
    for item in looks:
        mark = "已是真模型" if item["image_path"] and not item["mock"] else "待生成"
        print(f"  [资产] {item['name']}（{item['kind']}，{item['look_id']}，{mark}）")
    for seg in segments:
        print(f"  [片段] 第 {seg.get('segment_no')} 段 {seg.get('title')}（{seg.get('duration_s')}s，{seg.get('status')}）")

    if not args.execute:
        print("\n这是 dry-run，没有调用任何付费接口。确认无误后加 --execute 真跑。")
        return 0

    providers = _call(base, "GET", "/api/media/providers", timeout=60)
    print(f"\nproviders: {json.dumps(providers, ensure_ascii=False)[:400]}")
    _call(base, "POST", "/api/media/providers/activate", {"provider": "tfrouter"})
    print("已切到 tfrouter")

    if args.video_model:
        _call(base, "PATCH", "/api/studio/project" + _q(canvas), {"video_model": args.video_model})
        print(f"项目视频模型 → {args.video_model}")

    if not args.skip_looks:
        looks = _regenerate_looks(base, canvas, looks, args)
    if not args.skip_keyframes:
        _make_keyframes(base, canvas, segments, args)
    if not args.skip_videos:
        _run_videos(base, canvas, segments, args)

    rows, ok = _verify(base, canvas)
    print("\n验收：")
    for row in rows:
        print(
            f"  第 {row['segment_no']} 段：{row['shots']} 镜 / {row['takes']} take / "
            f"{row['duration_s']}s / {row['model']}"
        )
        for name, passed in row["checks"].items():
            print(f"    {'PASS' if passed else 'FAIL'} {name}")
        print(f"    首帧：{row['keyframe']}")
        print(f"    视频：{row['video']}")

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"canvas": canvas, "ok": ok, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {report}")
    print("S6 验收：" + ("PASS（还需人眼看首帧主体是不是角色）" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
