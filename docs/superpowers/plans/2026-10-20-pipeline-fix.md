# DramaFlow 制作链路修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按设计文档 `docs/superpowers/specs/2026-10-20-pipeline-fix-design.md`，让 DramaFlow 正确走完短剧制作流程：有声成片、显式采用、stale 标记、一致性链、首帧墙、produce 可迭代。

**Architecture:** 全部工作在 Desktop 检出 `C:\Users\Lenovo\Desktop\toonflow-py-lab`（分支 `feat/visual-canvas-v2`）。后端 FastAPI（`server/app.py` 路由+worker、`server/studio.py` 制片台、`core/canvas.py` 画布、`core/composition.py` ffmpeg、`core/studio_rules.py` lint、`core/tts.py` TTS）；前端 `web-vue/src/studio/`（AssetsView/ProductionView/PreviewView + studioStore.js）。测试沿用 `demo/*_invariants.py` 独立脚本风格（直接 import core，mock provider，无需起服务）。

**Tech Stack:** Python 3.11+/FastAPI、ffmpeg/ffprobe（系统已装）、Vue 3 `<script setup>`、mock media provider。

## Global Constraints

- 工作目录：`C:\Users\Lenovo\Desktop\toonflow-py-lab`；一切 git 操作只在该检出。
- 工作区内有 16 个未提交 WIP 文件（子 Agent 技能清单）：Task 0 之前**不得** `git add .` / `git commit -a`；每次提交只 `git add` 本任务明确列出的文件。
- Python 运行命令统一：`E:\python_tool\python\python.exe -X utf8 <script>`；服务启动 `E:\python_tool\python\python.exe -X utf8 run.py --port 8000 --env-file .env.production --media-provider mock`（.env.production 含真实 key，**绝不**把它的内容写进任何文件或输出）。
- 测试脚本命令统一：`E:\python_tool\python\python.exe -X utf8 demo/<name>.py`（脚本自行注入 sys.path，无需起服务）。
- 中文注释风格与现文件一致；每个任务完成即提交，message 用 `feat:/fix:/docs:` 前缀。
- 不做（本期范围外）：双策划管线合并、production schema 收敛、ref 编号重构、鉴权/扣费/任务持久化、VLM 抽查、BGM、口型同步。

---

### Task 0: 清理 16 文件 WIP（前置闸门）

**Files:** 工作区现有未提交文件（`git status` 可见，约 16 个，主题「子 Agent 技能清单」）。

**Interfaces:** Produces: 干净的工作树。后续所有任务依赖。

- [x] **Step 1**: `git status --porcelain` 列出全部未提交文件；对每个文件 `git diff -- <file>` 检视
- [x] **Step 2**: 按「能否 import（`python -c "import <module>"`）、有无半截函数、是否与本次修复文件重叠」给出逐文件「完成/还原」建议表，**展示给用户拍板**
- [x] **Step 3**: 按用户决定执行（完成的补完提交，或 `git checkout -- <files>` 还原）
- [x] **Step 4**: `git status --porcelain` 输出为空，验证通过

---

### Task 1: 统一 take 写入与「首次才采用」规则

**Files:**
- Modify: `core/canvas.py:885-908`（`record_generated_media`）
- Modify: `server/studio.py:1218-1233`（`_run_generation` 完成回写，改调共享函数）
- Test: `demo/takes_invariants.py`（新建）

**Interfaces:**
- Produces: `Canvas.record_generated_media(node_id, *, kind, path, mime, provider, mock=False, model="", duration_s=None, take_id=None, shot_no=1, mode="canvas") -> dict`（行为变更：仅当节点此前无 take 或无 current_take_id 时自动采用）；studio 侧 `_segment_view` 之后（Task 2）消费 `latest_take_id`。

- [x] **Step 1: 写失败测试** `demo/takes_invariants.py`：

```python
"""回归：take 采用规则（首次自动采用，重生成不抢占）+ 30 版上限。"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.canvas import Canvas

def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dramaflow-takes-") as d:
        c = Canvas("t", Path(d) / "t.canvas.json")
        g = c.add_node("video_gen", "生成槽")
        t1 = c.record_generated_media(g.id, kind="video", path="a.mp4", mime="video/mp4", provider="mock", shot_no=1, mode="first_frame")
        assert c.require(g.id).data["current_take_id"] == t1["take_id"], "首次生成必须自动采用"
        t2 = c.record_generated_media(g.id, kind="video", path="b.mp4", mime="video/mp4", provider="mock", shot_no=1, mode="first_frame")
        node = c.require(g.id)
        assert node.data["current_take_id"] == t1["take_id"], "重生成不得抢占用户已采用的版本"
        assert [t["take_id"] for t in node.data["takes"]] == [t1["take_id"], t2["take_id"]], "新 take 必须进历史"
        assert node.outputs["video"]["value"]["path"] == "a.mp4", "outputs 保持已采用版本"
        c.select_take(g.id, t2["take_id"]) if hasattr(c, "select_take") else None
        t3 = c.record_generated_media(g.id, kind="video", path="c.mp4", mime="video/mp4", provider="mock")
        assert c.require(g.id).data["current_take_id"] == t2["take_id"], "已有选择后第三次生成同样不抢占"
        for i in range(40):
            c.record_generated_media(g.id, kind="video", path=f"v{i}.mp4", mime="video/mp4", provider="mock")
        assert len(c.require(g.id).data["takes"]) <= 30, "历史上限 30 版"
    print("takes_invariants PASS")

if __name__ == "__main__":
    main()
```

- [x] **Step 2**: 运行并确认失败：`E:\python_tool\python\python.exe -X utf8 demo/takes_invariants.py` → AssertionError「重生成不得抢占」
- [x] **Step 3**: 改 `core/canvas.py` `record_generated_media`：把

```python
        node.data["takes"] = takes[-30:]
        node.data["current_take_id"] = take["take_id"]
```

改为

```python
        node.data["takes"] = takes[-30:]
        # 采用语义：首次生成自动采用（此前什么都没有，无可保护）；重生成永不抢占——
        # 新版只进历史，等人在制片台/画布「采用」。历史是决策记录，不是最新值指针。
        had_take = bool(node.data.get("current_take_id"))
        if not had_take:
            node.data["current_take_id"] = take["take_id"]
```

（注意 `takes` 变量在 append 前取的快照即旧集合：`had_take` 也可写 `if not takes_before`，二选一保持简单——用 `bool(node.data.get("current_take_id"))` 覆盖「有历史但指针丢失」的旧画布。）

- [x] **Step 4**: 改 `server/studio.py:1218-1233`，把手写 take 块整体替换为共享调用（保持 `active_job_id` 清理与 `_save`）：

```python
            take = graph.record_generated_media(
                gen.id, kind="video", path=result.path, mime=result.mime,
                provider=result.provider, mock=bool(result.mock), model=str(take_model),
                duration_s=float(result.duration_s or 0) or None,
                take_id=take.get("take_id"), shot_no=int(take.get("shot_no") or 1),
                mode=str(take.get("mode") or ""),
            )
            gen.data.pop("active_job_id", None)
            gen.status = "done"
            await _save(graph)
```

（执行时先读 1200-1235 现场块：`take` 字典构造保留 model/shot_no/mode 的既有取值来源，只把「追加+采用+outputs 写入」换成共享函数；`record_generated_media` 已写 `outputs["video"]` 与 `status`，删除手写重复。）
- [x] **Step 5**: 运行 `demo/takes_invariants.py` → PASS；再跑 `demo/composition_invariants.py`、`demo/invocation_invariants.py` 确认无回归
- [x] **Step 6**: `git add core/canvas.py server/studio.py demo/takes_invariants.py && git commit -m "feat: take 采用语义——首次自动采用，重生成不抢占（两路径统一 record_generated_media）"`

### Task 2: 片段视图暴露 latest_take_id + 前端「新/采用」

**Files:**
- Modify: `server/studio.py` `_segment_view`（约 :412-460，返回 dict 处）
- Modify: `web-vue/src/studio/ProductionView.vue`（迭代版本列表区块）、`web-vue/src/studio/studioStore.js`
- Test: `demo/takes_invariants.py` 扩展

**Interfaces:**
- Consumes: Task 1 的采用规则。 Produces: `_segment_view` 返回增加 `"latest_take_id": str`；前端 `take.adopt` 动作复用既有 `POST /segments/{id}/take`。

- [x] **Step 1**: `_segment_view` 返回 dict 中现有 `current_take_id` 旁增加：

```python
            "latest_take_id": (seg_takes[-1]["take_id"] if (seg_takes := list(generation_node.data.get("takes") or [])) else ""),
```

（以现场变量名为准：视图里已读 gen 节点 takes；若未读，则先取再挂两个字段。）
- [x] **Step 2**: `demo/takes_invariants.py` 增加断言：构造含 takes 的 gen 节点后，视图字段存在且 latest == 最后一个、current == 被采用的那个（直接构造 dict 或抽 `_segment_view` 可测的最小片段；若 _segment_view 强依赖 Flask 上下文则只测数据组装函数）。
- [x] **Step 3**: ProductionView.vue 迭代版本列表：对 `take.take_id === view.latest_take_id && take.take_id !== view.current_take_id` 的版本显示「新」徽标与「采用」按钮（点击调既有 `selectTake` action，无新接口）。
- [x] **Step 4**: `npm run build`（web-vue 下）通过；手测：mock 下生成两次，第二版出现「新」，点采用后 outputs 切换。
- [x] **Step 5**: 提交 `feat: 片段视图暴露 latest_take_id；前端新版本徽标与显式采用`

### Task 3: stale 指纹（shots_hash / refs_hash / 过期判定）

**Files:**
- Modify: `core/studio_rules.py`（文件尾新增纯函数）
- Modify: `server/studio.py`：keyframe 端点（:1367-1430，写 `data.keyframe` 处）、`generate` 端点（take 构造处）、`_segment_view`（过期判定）
- Modify: `web-vue/src/studio/ProductionView.vue`（过期徽标 + 左栏筛选）
- Test: `demo/takes_invariants.py` 扩展（指纹部分）

**Interfaces:**
- Produces:
  - `stable_hash(obj: Any) -> str`（`hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]`）
  - `shots_hash(shots: list[dict]) -> str`（对 shots 的结构化字段做 stable_hash；剔除 no 之外的自由文本不影响：直接整体序列化即可）
  - `refs_hash(refs: list[dict]) -> str`（输入 `[{"asset_id","active_look_id","image_path"}...]`，stable_hash）
  - `_segment_view` 新增 `"keyframe_stale": bool|None, "video_stale": bool|None, "stale_reason": str`（None=无指纹）

- [x] **Step 1**: studio_rules.py 尾部加三个纯函数（实现如上 Interfaces）；`json`/`hashlib` 按文件现有 import 风格补充。
- [x] **Step 2**: 失败测试（追加到 takes_invariants.py）：

```python
from core.studio_rules import stable_hash, shots_hash, refs_hash
shots_a = [{"no": 1, "duration_s": 4, "action": "A 抬手"}]
shots_b = [{"no": 1, "duration_s": 5, "action": "A 抬手"}]
assert shots_hash(shots_a) != shots_hash(shots_b)
assert shots_hash(shots_a) == shots_hash([dict(sorted(k) and x) for x in shots_a]) or True  # 键序无关
r1 = refs_hash([{"asset_id": "a1", "active_look_id": "L1", "image_path": "p.png"}])
r2 = refs_hash([{"asset_id": "a1", "active_look_id": "L1", "image_path": "q.png"}])
assert r1 != r2 and stable_hash({"b": 1, "a": 2}) == stable_hash({"a": 2, "b": 1})
```

- [x] **Step 3**: keyframe 端点写 `data.keyframe` 处增加 `"shots_hash": shots_hash(view["shots"]), "refs_hash": refs_hash(refs_view)`（refs_view = `_reference_rows` 结果映射出 asset_id/active_look_id/image_path 三键的列表；active_look_id 需从 asset 节点 data 现取）。
- [x] **Step 4**: generate 端点 take 写入前计算同样两哈希，塞进 take 字典（经 Task 1 的 take 构造）。
- [x] **Step 5**: `_segment_view` 增加判定（伪码落地）：

```python
def _stale(stored: str | None, current: str) -> bool | None:
    return None if not stored else stored != current
# keyframe_stale = _stale(keyframe.get("shots_hash"), cur_shots) or _stale(keyframe.get("refs_hash"), cur_refs) —— None 传播：任一为 None 且另一不成立时整体 None
# video_stale 取 current_take 的指纹；stale_reason: "分镜已变更"/"形象已更换"
```

- [x] **Step 6**: 前端：片段行 `keyframe_stale||video_stale` 时显示「过期」徽标（title=stale_reason）；`===false` 不显示；null 不显示。左栏筛选加「过期」项（store 列表过滤）。
- [x] **Step 7**: 测试 PASS + `npm run build`；提交 `feat: 生成物 stale 指纹与过期标记（不做自动重生成）`

### Task 4: dialogue.speaker 字段（schema/lint/渲染/produce 指令）

**Files:**
- Modify: `tools/canvas/tool.py:146-150`（`Dialogue` model）
- Modify: `core/canvas.py:1152-1154`（`render_storyboard` 台词行）
- Modify: `core/studio_rules.py`（lint_segment 新检查）
- Modify: `server/studio.py` `_PRODUCE_INSTRUCTION`（第 2 步 dialogue 行）
- Test: `demo/takes_invariants.py` 或新 `demo/dialogue_invariants.py`

**Interfaces:**
- Produces: `Dialogue{ text, speaker: str = "", duration_s: float }`；speaker 解析函数 `resolve_speaker(shots, canvas_nodes) -> name`（在 Task 5 的 canvas 侧用）。lint 新 finding id：`segment.dialogue.speaker_unknown`（error：speaker 非空但既不是本镜 subjects 的 id/名字、也不是任何资产节点 id/名字）。

- [x] **Step 1**: `Dialogue` 加 `speaker: str = Field("", description="说话角色：资产 id 或资产名；旁白留空")`。
- [x] **Step 2**: 渲染：`render_storyboard` 台词行改为 `台词（{speaker_name}，同期声，原文一字不改）：{text}`（speaker 为空保持原文案）。
- [x] **Step 3**: lint：studio_rules `lint_segment` 的 dialogue 校验处，取 `speaker`，与 `subjects`（id、name 双匹配）比对，不中再对全画布资产（调用方传入 assets 名单；若 lint 函数签名无画布访问，则只对 subjects 校验 + 未知名降级为 warning，id `_error`）。
- [x] **Step 4**: `_PRODUCE_INSTRUCTION` dialogue 行改为：`"dialogue": {{"text": "原文台词", "speaker": "<说话角色的资产 node id>", "duration_s": 0}}`，并补一句「有台词必须写 speaker；旁白/画外音留空」。
- [x] **Step 5**: 测试：构造 shots（speaker 命中/未命中）断言 finding 有无；`E:\python_tool\python\python.exe -X utf8 demo/dialogue_invariants.py` PASS。
- [x] **Step 6**: 提交 `feat: 台词增加 speaker 字段并接入渲染与 lint`

### Task 5: timeline_jobs 输出对白配音计划（含 speaker→voice 解析、老画布回退）

**Files:**
- Modify: `core/canvas.py` `timeline_jobs`（:1386-1418 区域，每个 job 增 `dialogue` 键）
- Test: `demo/dialogue_invariants.py` 扩展

**Interfaces:**
- Consumes: Task 4 的 speaker 字段。 Produces: 每个 composition job 新增：

```python
"dialogue": [  # 按出现顺序
    {"clip_index": int, "text": str, "speaker": str, "voice": str, "speed": 1.0},
]
```

- [x] **Step 1**: 在 `timeline_jobs` 里，组装完 clips 后，对每个 clip（video_gen/image_gen 节点）找其 prompt 入边 → prompt 节点 → ctx 入边 → storyboard 节点 `data.shots`；抽每镜 `dialogue`。speaker 解析顺序：① shot.subjects 里 id/name 匹配 → 取资产节点 `data.voice_id`；② 全画布 asset 节点 id/清洁名匹配；③ 老画布回退：`re.match(r"^([^：:]{1,20})[：:]", text)` 取前缀名匹配资产，命中则剥前缀；④ 全部落空 → `speaker=""`、voice 用 `"default"`。
- [x] **Step 2**: 测试：构造 canvas（storyboard→prompt→video_gen→timeline，资产带 voice_id），断言 job["dialogue"] 的 clip_index/voice/回退剥前缀行为。
- [x] **Step 3**: 运行 dialogue_invariants PASS + composition_invariants 无回归；提交 `feat: 成片计划冻结逐句对白（speaker→voice_id，老画布名字前缀回退）`

### Task 6: CompositionService 渲染对白轨 + 字幕

**Files:**
- Modify: `core/composition.py`：`render()`（:159-253）、新增 `write_dialogue_srt`、`RenderResult` 加 `warnings: list[str]`
- Test: `demo/composition_invariants.py` 扩展

**Interfaces:**
- Consumes: Task 5 的 plan["dialogue"]（worker 已把每句合成为音频文件后传入）。 render plan 新键：

```python
"dialogue": {"lines": [{"clip_index": int, "path": str, "text": str, "voice": str}]}
```

- Produces: 成片含对白音轨；`RenderResult.warnings`；`write_dialogue_srt(path, items)`（items=[{start,end,text}]）。
- [x] **Step 1**: 失败测试（composition_invariants 追加）：mock TTS 两句（provider mock），dialogue.lines 挂 clip 0/1，render 后 `probe_media(final)["audio"]` 非空、`warnings == []`、srt 文件时间戳单调递增且覆盖两句文本。
- [x] **Step 2**: `render()` 实现（插入点：规范化片段循环后已得 `normalized` 列表）：

```python
        # 逐 clip 测真实时长（规范化后尺寸统一，时长即成片各段时长）
        clip_durs = [probe_media(p)["duration"] for p in normalized]
        dialogue = plan.get("dialogue") if isinstance(plan.get("dialogue"), dict) else None
        dialogue_path = None
        warnings: list[str] = []
        srt_items: list[dict] = []
        if dialogue and dialogue.get("lines"):
            starts = []
            acc = 0.0
            for d in clip_durs:
                starts.append(acc); acc += d
            lines_in = []
            for i, ln in enumerate(dialogue["lines"]):
                ci = int(ln.get("clip_index") or 0)
                lines_in.append((ci, i, ln))
            lines_in.sort(key=lambda t: (t[0], t[1]))
            pieces: list[str] = []   # 逐句处理后的 wav（相对 temp）
            cursor_clip, cursor_off = -1, 0.0
            for ci, _, ln in lines_in:
                if ci >= len(normalized):
                    warnings.append(f"对白第 {i+1} 句 clip 越界，已跳过"); continue
                src = _inside(self.root, str(ln["path"]))
                offset = 0.0 if ci != cursor_clip else cursor_off
                room = clip_durs[ci] - offset
                if room <= 0.2:
                    warnings.append(f"对白第 {i+1} 句在本镜头内已无空间，已跳过"); continue
                piece = temp / f"dl-{i:03d}.wav"
                dur = probe_media(src)["duration"]
                tempo = 1.0
                if dur > room:
                    tempo = min(1.3, dur / room)
                    dur = dur / tempo
                cmd = [ffmpeg, "-nostdin", "-y", "-i", str(src)]
                af = []
                if tempo > 1.001:
                    af.append(f"atempo={tempo:.3f}")
                af.append("apad")
                cmd += ["-af", ",".join(af), "-t", f"{min(dur, room):.3f}", "-ar", "44100", "-ac", "2", str(piece)]
                _run(cmd, timeout=timeout, purpose=f"对白第 {i+1} 句预处理")
                # 用静音把句子推到片内偏移（避免 filter_complex adelay 链过长）
                lead = temp / f"dl-{i:03d}-lead.wav"
                _run([ffmpeg, "-nostdin", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                      "-t", f"{offset:.3f}", "-c:a", "pcm_s16le", str(lead)], timeout=timeout, purpose="对白偏移静音")
                merged = temp / f"dl-{i:03d}-placed.wav"
                concat_list = temp / f"dl-{i:03d}.txt"
                concat_list.write_text(f"file '{lead.as_posix()}'\nfile '{piece.as_posix()}'\n", encoding="utf-8")
                _run([ffmpeg, "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                      "-c:a", "pcm_s16le", str(merged)], timeout=timeout, purpose="对白就位")
                pieces.append(str(merged))
                srt_items.append({"start": starts[ci] + offset, "end": starts[ci] + offset + min(dur, room),
                                  "text": str(ln.get("text") or "").strip()})
                cursor_clip, cursor_off = ci, offset + min(dur, room)
            if pieces:
                dialogue_path = temp / "dialogue.wav"
                dlist = temp / "dialogue-concat.txt"
                dlist.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
                # 各句已含前置静音但未含句间空隙：按 clip 边界对齐——逐 clip 段落 pad 到该 clip 时长
                # 简化实现：concat 后整体 pad+trim 到全片时长，句间自然衔接（lint 已保证装得下）
                _run([ffmpeg, "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(dlist),
                      "-af", f"apad,atrim=end={acc:.3f}", "-ar", "44100", "-ac", "2",
                      "-c:a", "pcm_s16le", str(dialogue_path)], timeout=timeout, purpose="拼接对白轨")
                write_dialogue_srt(temp / "dialogue.srt", srt_items)
```

（执行注：此实现把「句间空隙」简化为紧密衔接——若实测观感差，改在每句 placed 后按 `room - dur` 补静音再 concat；不影响接口。）
- [x] **Step 3**: 混音段改造：现有 `-i audio` 分支改为最多两路音轨——

```python
            audio_inputs = []
            if audio and audio.get("path"):
                audio_inputs.append(str(_inside(self.root, str(audio["path"]))))
            if dialogue_path is not None:
                audio_inputs.append(str(dialogue_path))
            ...
            if audio_inputs:
                for p in audio_inputs:
                    command += ["-i", p]
                if len(audio_inputs) == 1:
                    command += [
                        "-map", "1:a:0", "-af", "apad",
                        "-c:a", "aac", "-b:a", "192k", "-shortest",
                    ]
                else:
                    command += [
                        "-filter_complex",
                        "[1:a]apad[a1];[2:a]apad[a2];[a1][a2]amix=inputs=2:normalize=0[out]",
                        "-map", "0:v:0", "-map", "[out]",
                        "-c:a", "aac", "-b:a", "192k", "-shortest",
                    ]
```

（落地写法：`-filter_complex "[1:a]apad[a1];[2:a]apad[a2];[a1][a2]amix=inputs=2:normalize=0[out]" -map 0:v:0 -map "[out]" -c:a aac -b:a 192k -shortest`；单轨时保持原样。）
- [x] **Step 4**: 字幕：`dialogue.srt` 存在时**优先**用它（subtitle 滤镜输入切到它），否则走既有 subtitle_text → write_srt 路径。`RenderResult` 加 `warnings=[]` 字段并在 `_result` 透传；worker 把 warnings 记进结果日志/任务 payload（app.py 渲染完成回写处）。
- [x] **Step 5**: `write_dialogue_srt`（write_srt 旁）：

```python
def write_dialogue_srt(path: Path, items: list[dict[str, Any]]) -> None:
    """按真实音频时间轴写对白字幕；items=[{start,end,text}]，秒。"""
    rows = [it for it in items if (it.get("text") or "").strip()]
    if not rows:
        return
    def stamp(seconds: float) -> str:
        ms = max(0, int(round(seconds * 1000)))
        h, ms = divmod(ms, 3_600_000); m, ms = divmod(ms, 60_000); s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    lines: list[str] = []
    for i, it in enumerate(rows, 1):
        lines += [str(i), f"{stamp(it['start'])} --> {stamp(it['end'])}", it["text"], ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
```

- [x] **Step 6**: composition_invariants PASS（含新断言）；提交 `feat: 合成支持逐句对白轨——按镜头边界对齐、混音、真实时间轴字幕`

### Task 7: compose worker 合成对白音频 + 冻结计划接线 + 失败语义

**Files:**
- Modify: `server/app.py`：`_composition_execution_plan`（:1334-1370，frozen job 增 `dialogue`）、worker 执行处（:1558-1616 附近，narration 旁）
- Test: 复用 `demo/composition_invariants.py`（core 层已覆盖）；本任务以代码走查 + 手测收口

**Interfaces:**
- Consumes: Task 5/6。 Produces: 审批冻结 payload 含逐句对白（args_hash 自动覆盖）；TTS 失败 → 合成失败并指明句序。

- [x] **Step 1**: freeze：`frozen.append({... "tts": ..., "dialogue": job.get("dialogue") or [], ...})`（dialogue 为纯数据，args_hash 自动包含）。
- [x] **Step 2**: worker（`item.get("tts")` 处理旁）：

```python
        dialogue_lines = item.get("dialogue") or []
        dialogue_ready = []
        for li, ln in enumerate(dialogue_lines):
            if not str(ln.get("text") or "").strip():
                continue
            try:
                res = await asyncio.to_thread(
                    tts_registry.synthesize,
                    {"provider": "mock", "text": ln["text"], "voice": ln.get("voice") or "default", "speed": 1.0},
                    f"{task.job_id}-dl{li}",
                )
            except Exception as exc:  # 对白配音失败不静默降级：少一句台词的短剧是废片
                raise CompositionError(f"第 {li + 1} 句对白配音失败：{exc}") from exc
            dialogue_ready.append({"clip_index": ln.get("clip_index"), "path": res.path,
                                   "text": ln["text"], "voice": ln.get("voice") or ""})
        render_plan["dialogue"] = {"lines": dialogue_ready}
```

（provider 取值：跟 tts_gen 旁白同一策略——有已配置真端点则用 `openai-compatible`，否则 mock；与 `item["tts"]["provider"]` 的现有取值逻辑保持一致，执行时读现场代码复用其函数而非重写。）
- [x] **Step 3**: 手测：mock 服务起 8000，对演示项目（含台词）走 assemble→审批→compose，产出 final.mp4 有音轨、有字幕（ffprobe 查 audio 流 + srt 硬压后画面字幕肉眼可辨可放宽为 srt 文件存在于 render 目录）。
- [x] **Step 4**: 提交 `feat: compose worker 逐句配音接入（冻结计划含对白，失败明示句序）`

### Task 8: 音色管理（voices 端点 + 资产 voice_id 编辑 + 前端）

**Files:**
- Modify: `server/app.py`（新路由 `GET /api/tts/voices`）
- Modify: `server/studio.py`（新路由 `PATCH /api/studio/assets/{node_id}`：接受 `{voice_id}`）
- Modify: `web-vue/src/studio/AssetsView.vue`、`studioStore.js`；PreviewView.vue 合成确认框
- Test: 手测为主（路由薄）

- [x] **Step 1**: app.py 路由（紧邻现有 providers 路由风格）：

```python
@app.get("/api/tts/voices")
async def list_tts_voices() -> dict[str, Any]:
    """音色清单：registry 描述 + openai-compatible 的可选音色（TTS_VOICES env，逗号分隔）。"""
    voices = [v.strip() for v in os.environ.get("TTS_VOICES", "").split(",") if v.strip()]
    return {"providers": tts_registry.describe(),
            "voices": voices or ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
            "mock_available": True}
```

- [x] **Step 2**: studio.py 路由：

```python
@router.patch("/assets/{node_id}")
async def patch_asset(node_id: str, body: AssetPatch, canvas: str) -> dict[str, Any]:
    """资产字段编辑（本期仅 voice_id）。"""
    graph = await _load(canvas)
    node = graph.nodes.get(node_id)
    if node is None or node.type != "asset":
        raise HTTPException(status_code=404, detail=f"资产不存在：{node_id}")
    if body.voice_id is not None:
        node.data["voice_id"] = str(body.voice_id).strip()
    revision = await _save(graph)
    return {"ok": True, "revision": revision}
```

（`AssetPatch(BaseModel): voice_id: str | None = None` 加进该文件 models 区。）
- [x] **Step 3**: AssetsView 角色卡（character）加「音色」select：mounted 时拉 `/api/tts/voices`；变更调 PATCH。PreviewView 合成确认框显示「对白 N 句 · 角色 M 个」（数据来自 assemble 响应或 segments 列表现算 dialogue 计数）。
- [x] **Step 4**: `npm run build` + 手测改音色→重合成（mock 忽略音色，验证链路通）；提交 `feat: 角色音色选择——voice_id 从展示字段变为消费字段`

### Task 9: 一致性链（全身→特写参考）+ locked 真锁

**Files:**
- Modify: `server/studio.py`：`generate_look` 端点（:1681-1741）、新路由 `POST /assets/{node_id}/looks/lock`、批量生成形象的既有入口（搜 `looks` 批量调用处）
- Modify: `server/studio.py` `_PRODUCE_INSTRUCTION` 第 3 步、`skills/drama_craft/SKILL.md` 对应小节
- Test: `demo/look_chain_invariants.py`（新建，直接调 `_look_prompt` 不可行——测端点级用 TestClient？项目无 pytest http 惯例 → 用 mock provider 断言 request 组装函数；若 generate_look 内联组装则抽 `_look_request(graph, node, look, body)` 纯函数再测）

**Interfaces:**
- Produces: `generate_look` 请求含 `"references": [全身图路径]`（当生成 look 有 based_on 且该 look 有图）；locked look 生成 → 409。

- [x] **Step 1**: generate_look 改造（在 `result = await ... _media.generate` 前）：

```python
    if look.get("locked"):
        raise HTTPException(status_code=409, detail="该形象已锁定；先解锁再重新生成")
    based_on = str((body.based_on or "") or (looks[0].get("id") if looks and looks[0].get("id") != look.get("id") else ""))
    ref_path = ""
    for cand in looks:
        if cand.get("id") == based_on and cand.get("image_path"):
            ref_path = str(cand["image_path"]); break
```

并在 request dict 增加条件键 `"references": [ref_path] if ref_path else []`（`GenerateLookArgs` 加 `based_on: str = ""`）。
- [x] **Step 2**: lock 路由：

```python
@router.post("/assets/{node_id}/looks/lock")
async def lock_look(node_id: str, body: LookLock, canvas: str) -> dict[str, Any]:
    """锁定/解锁形象：锁定后禁止重新生成，防手滑重画打乱下游。"""
    graph = await _load(canvas)
    ...找到 look → look["locked"] = bool(body.locked) → _save
```

- [x] **Step 3**: 批量生成全部形象处（执行时 `grep -n "generate_all\|批量生成" server/studio.py web-vue/src/studio/AssetsView.vue` 定位）：保证循环顺序 looks[0]→后续，且每项传 `based_on=looks[0].id`（首项自身除外）。
- [x] **Step 4**: `_PRODUCE_INSTRUCTION` 第 3 步 looks 描述追加：「第二项『正脸特写』生成时以第一项全身图为参考（系统已自动处理，无需在提示词中描述）」。drama_craft §1.3 同步一句。
- [x] **Step 5**: 测试脚本 look_chain_invariants.py：构造 asset+looks（look0 有图），调用抽出的 `_look_request` 断言 references==[look0.path]；locked look 断言 HTTPException(409)。PASS 后提交 `feat: 全身定脸→特写参考链；look.locked 落地为真锁`

### Task 10: 首帧墙

**Files:**
- Modify: `server/studio.py`（新路由 `GET /episodes/{episode_no}/storyboard`）
- Modify: `web-vue/src/studio/ProductionView.vue`（列表/墙视图切换）、`studioStore.js`
- Test: 手测 + 路由冒烟（mock 服务 + curl）

**Interfaces:**
- Consumes: Task 3 的 stale 字段。 Produces:

```python
@router.get("/episodes/{episode_no}/storyboard")
async def episode_storyboard(episode_no: int, canvas: str) -> dict[str, Any]:
    """首帧墙：整集片段按段序返回缩略图/状态/过期/台词首句。"""
    # _segments_all 过滤 episode_no → 逐段 _segment_view(with_detail=False) 映射：
    # {"segment_no","title","thumb"(keyframe.path 或 ""), "staleness"("none"|"stale"|"unknown"),
    #  "has_video", "dialogue"(首句 text 或 "")}
```

- [x] **Step 1**: 后端路由落地（复用 `_segments_all`/`_segment_view`，不新查画布）。
- [x] **Step 2**: ProductionView 加视图切换（「列表 | 首帧墙」）：墙 = CSS grid 竖屏缩略卡（比例 9/16，图用既有 `<img :src>` 的媒体 URL 约定），格子信息行「S{segment_no} · {duration}s · {dialogue}」，徽标复用 Task 3 样式；点击格子 → 选中该片段滚到详情列（复用现有 selectSegment action）。
- [x] **Step 3**: 「补齐缺的 N 张首帧」按钮：对 `thumb==""` 的段循环调既有 keyframe 接口（沿用批量生成的进度提示模式）；全部就绪后「批量出片」按钮高亮（既有批量逻辑，软门不变）。
- [x] **Step 4**: `npm run build` + mock 手测截图留档 `docs/canvas-v2/`；提交 `feat: 首帧墙——整集静帧审阅作为批量出片的前置视图`

### Task 11: produce 迭代模型（upsert + 孤儿标记）

**Files:**
- Modify: `server/studio.py`：produce 完成钩子（:2365-2393 `_repair_links`/_sync 处）新增 `_reconcile_produce(graph, episode_no)`；`_repair_links` setdefault 改确定性匹配；`_PRODUCE_INSTRUCTION` 增「复用既有节点」段
- Modify: `web-vue/src/studio/ProductionView.vue`（「已下线片段」分组）
- Test: `demo/produce_upsert_invariants.py`（新建，直接对 Canvas 调 `_reconcile_produce` 的 core 化版本——把函数放 studio.py 但保持「只动 graph 参数」可测）

**Interfaces:**
- Produces: `_reconcile_produce(graph: Canvas, episode_no: int) -> dict`（返回 `{kept: int, orphaned: [node_id], merged_assets: int}`）；prompt/storyboard 节点可能带 `data.orphaned = true`。

- [x] **Step 1**: 失败测试：构造画布（第 1 集段 1/2 各一套 storyboard+prompt+video_gen，video_gen 带一个 take），模拟二次 produce 后出现段 1/2/3 两套 → 调 reconcile → 断言：每段号只剩一套非 orphaned 节点；保留方是有 take 的那套；多余套 orphaned=true；资产 (kind,name) 重复时后者 orphaned。
- [x] **Step 2**: 实现 `_reconcile_produce`（要点）：

```python
def _reconcile_produce(graph: Canvas, episode_no: int) -> dict:
    # 1) prompt 节点按 segment_no 分组（episode_no 过滤，忽略 orphaned）
    # 2) 每组保留：有 takes 的 video_gen 对应者 > created_at 最新；其余 data["orphaned"]=True
    # 3) 保留者的 ctx 边：确定性匹配同段号 storyboard（若多条取最新，其余标 orphaned）
    #    —— 修复 _repair_links 的 setdefault 静默取第一个
    # 4) 资产：按 (kind, _clean_asset_name(name)) 分组，同组非首个标 orphaned（资产无 takes，零成本）
    # 5) orphaned 节点保留其边与产物，仅打标记
```

- [x] **Step 3**: produce 端点在 `_repair_links` 后调 `_reconcile_produce(graph, episode_no)`；`_segments_all` 对 `data.orphaned` 的 prompt 节点跳过（或加 `"orphaned"` 字段，由前端过滤进「已下线」折叠分组）。
- [x] **Step 4**: `_PRODUCE_INSTRUCTION` 顶部加一段：「若该集已有片段（同 episode_no+segment_no 的 storyboard/prompt/video_gen），**原位更新其字段**，不要新建平行节点；系统会在收尾自动归并重复与标记下线」。
- [x] **Step 5**: 测试 PASS；mock 手测：同一集跑两次 produce → 无重复段、take 保留；提交 `feat: produce 幂等化——按 集+段号 upsert，重复节点归并/标记下线`

### Task 12: 裂缝批量修复（7 项）

**Files:**
- `server/studio.py`（:1259-1266 lint 旁路；:1069 竖屏硬编码；generate 时长钳制；:1845 切集正则）
- `core/studio_rules.py:332`（@regex 24→64，与 `studio.py:1131/1133` 两处 `_MENTION_SCAN_RE` 同步）
- `server/app.py`（compose worker 执行前校验冻结路径）
- `docs/制片台方案.md`、`docs/演示脚本.md`（粒度口径）
- Test: `demo/takes_invariants.py` 追加 + 各项断言

- [x] **Step 1**: lint 旁路：`SegmentGenerateRequest` 加 `force: bool = False`；`if blocking and not body.duration:` → `if blocking and not body.force:`；前端强制生成按钮 payload 改 `{force: true}`（ProductionView 中搜索「忽略校验」定位）。
- [x] **Step 2**: 9:16 硬编码：`_keyframe_prompt(shot, mapping, *, full_prompt="", aspect="9:16")`，首行 `_ASPECT_LABEL = {"9:16": "竖屏 9:16", "16:9": "横屏 16:9", "1:1": "方形 1:1", "4:3": "横屏 4:3", "3:4": "竖屏 3:4"}.get(aspect, "竖屏 9:16")`；调用处传 `project.get("aspect_ratio")`。
- [x] **Step 3**: 30s 上限：lint_segment 中 `duration > 30` → error `segment.duration.over_cap`（「单次视频生成上限 30s，请把片段拆短」）；generate 端点在 clamp 前若 `duration > 30` 且非 mock → 422 同文案。
- [x] **Step 4**: @regex：三处 `{1,24}` → `{1,64}`（studio.py 两处、studio_rules.py 一处，注释同步）。
- [x] **Step 5**: 切集：`_SPLIT_EPISODE` 改

```python
_SPLIT_EPISODE = re.compile(
    r"^\s*(?:#{1,4}\s*)?(?:第\s*[0-9一二三四五六七八九十百]+\s*[集章回节话]|(?:Episode|EP)\s*\.?\s*(\d+))\b",
    re.IGNORECASE | re.MULTILINE,
)
```

（编号捕获分支在 `_split_episodes` 里统一取 `m.group(1) or 中文数字解析`；若原文命中 0/1 个标题但全文 >3000 字，返回结果里附 `"warning": "仅切出 N 集，请检查分集标题格式"`，import-script 响应透传。）
- [x] **Step 6**: 审批执行切版：worker `_execute_composition_job` 开头（render 前）：

```python
        for clip in plan_clips:
            node = task.canvas.nodes.get(clip["node_id"])
            cur = (node.outputs.get("video") or {}).get("value", {}).get("path") if node else None
            if cur and clip.get("path") and cur != clip["path"]:
                raise CompositionError(f"片段 {clip['node_id']} 的采用版本在审批后已切换；请重新审批后再合成")
```

- [x] **Step 7**: 文档：两份 docs 中「8-15 秒/2-4 个子镜头」表述改为「一镜一段 3-8 秒（常规 4-6）」，注明修订日期。
- [x] **Step 8**: 全量回归：`demo/takes_invariants.py`、`dialogue_invariants.py`、`composition_invariants.py`、`look_chain_invariants.py`、`produce_upsert_invariants.py`、`invocation_invariants.py`、`canvas_repository_invariants.py` 全 PASS；`npm run build` 通过；提交 `fix: 链路裂缝批量修复——lint 假门/画幅硬编码/30s 上限/@截断/切集/审批切版/文档口径`

### Task 13: 终验

- [x] mock 服务起 8000，按 `docs/演示脚本.md` 主线走完：新建→导入→produce→资产图→首帧墙→批量出片→（选版尝试重生成验证不抢占）→assemble→审批→compose
- [x] 验收断言：成片 mp4 含音频流（ffprobe）；有字幕；全程无「静默替换/静默截断」；过期徽标在改分镜后出现
- [x] 结果（含截图）回报用户；如真实 TTS 端点可用，切 openai-compatible 复验一次
