# DramaFlow 制作链路修复 · 设计文档

> 日期：2026-10-20 · 范围：链路审查 P0+P1 · 基线：`feat/visual-canvas-v2`
> 目标：让系统「正确走完短剧的制作流程」——有声成片、返工可控、一致性有链、策划可迭代。
> 底层兜底（鉴权/扣费/任务持久化）与 P3 结构重构（双管线合并、schema 收敛、ref 编号重构）明确不在本期。

## 0. 背景一句话

链路骨架（剧本→分集→资产→片段→形象图→首帧→视频→选版→合成）是对的，但有四个链路级缺陷：声音链路断头（成片必然无声）、无失效传播（改上游下游不知道）、生成即采用（选版权被剥夺）、produce 无迭代模型（重跑即垃圾堆积）。本方案按「薄语义地基 → 纵向切片」顺序修复。

## 1. Phase 0 · 清理 WIP（前置，半天）

1. `git diff` 逐文件检视现有 16 个未提交文件（子 Agent 技能清单）：能否 import、有无半截实现、是否与本次修复的文件重叠
2. 给出「完成 / 还原」逐文件建议，**由用户拍板**后落地
3. 验收：`git status` 干净

## 2. Phase 1 · 采用语义 + stale 标记（1-2 天，语义地基）

### 2.1 显式采用

**规则：首次生成自动采用；重生成永不自动采用。**

- 抽共享函数 `core/canvas.py::apply_generated_take(gen, take)`：仅当该 gen 节点此前无任何 take 时设 `current_take_id` 并更新 `outputs.video`；否则新 take 只追加进 `takes[]`
- 两条写路径（制片台 `studio.py` 完成回写、画布 `app.py` worker 回写）统一改调此函数；顺手统一两条路径 take 记录的 `shot_no`/`mode` 语义
- `_segment_view` 增加 `latest_take_id`；前端迭代版本列表对「最新 ≠ 当前」打「新」徽标 + 「采用」按钮（复用现有 `select_take` 接口）
- 批量生成不受影响（只跑未出片段，全为首次生成）

### 2.2 stale 指纹

- 纯函数进 `core/studio_rules.py`：`shots_hash(shots)`（分镜结构化字段稳定序列化哈希）、`refs_hash(refs)`（(asset_id, active_look_id, image_path) 集合哈希）
- 写入：生成首帧时存进 `data.keyframe`；生成视频时存进 take
- 读取：`_segment_view` 现算比对，产出 `keyframe_stale` / `video_stale` + 原因（分镜已变更 / 形象已更换）
- 兼容：无指纹的旧产物显示「无指纹」，不算过期（避免上线即全体飘红）
- UI：片段行「过期」徽标 + 原因 tooltip；左栏筛选加「过期」；**不做自动重生成**，Phase 3 首帧墙消费该字段

## 3. Phase 2 · 声音链路（3-4 天，分角色对白驱动）

### 3.1 声音来源

TTS 服务，非视频模型。图像一致性靠参考图，声音一致性靠固定音色 ID。底座已有：`core/tts.py::TTSRegistry`（mock + OpenAI-compatible，env 配置真端点），compose worker 已支持混音。v1 不做口型同步，台词由字幕承载。

### 3.2 数据流

```
shots[].dialogue{text, speaker, duration_s}          ← schema 扩展 speaker 字段
  → produce 指令更新（要求写 speaker=资产 id）；lint 校验 speaker ∈ 本镜 subjects
     （老画布回退：按「名字：」前缀从 text 解析）
  → POST /assemble 冻结 dialogue_audio 计划进审批：
     逐句 {clip_index, text, speaker_asset_id, voice, speed}（args_hash 覆盖）
  → compose worker：
     逐句 tts_registry.synthesize → ffprobe 测实际时长
     → 按 clip 边界对齐拼接对白轨（句长超 clip 时长先 1.0→1.3x 提速压入，仍超则
       在 clip 末尾截断并在结果中告警——lint 的台词时长校验保证这是少数）
     → 对白轨与旁白轨混音 → 按真实音频时间生成 .srt → ffmpeg 硬压字幕
```

### 3.3 音色管理

- 资产 `voice_id` 从展示字段变为消费字段：资产页角色卡加「音色」选择
- 新端点 `GET /api/tts/voices`：`registry.describe()` + 音色清单（openai-compatible 的清单来自 `TTS_VOICES` env 逗号分隔，默认内置一组；mock 忽略音色）
- 未设置音色 → 默认音色 + 冻结计划中标注 warning

### 3.4 失败语义

逐句 TTS 失败 → 合成失败并指明第几句（真实 provider 不静默降级）；mock 供全链路测试。

### 3.5 UI

预览页合成确认框显示「对白 N 句 · 角色 M 个 · 预计 X 秒」；合成产物即有声成片，页内播放/下载不变。

## 4. Phase 3 · 一致性链 + 首帧墙（2-3 天）

### 4.1 全身→特写参考链

- `generate_look` 支持 `based_on`（默认 `looks[0]` 正面全身）：该 look 有图时作为 `references` 传给生图（`_media.generate` 已支持 references）
- produce 指令与资产页「批量生成全部形象」遵守顺序：先全身（无参考）→ 后特写（参考全身）
- `skills/drama_craft/SKILL.md` 同步更新

### 4.2 locked 真锁

`generate_look` 遇 `look.locked` → 409；前端按钮禁用 + 显式「解锁」动作。locked 从只写字段变为执行字段。

### 4.3 首帧墙

- 新端点 `GET /api/studio/episodes/{no}/storyboard`：按段序返回 `{segment_no, keyframe_thumb, staleness, has_video, dialogue_summary}`
- 制作 tab 加「列表 / 首帧墙」切换：竖屏缩略图网格，格子含镜号/时长/台词首句/状态徽标（无首帧/过期/已出片），点击定位到中间详情列
- 「补齐缺的 N 张首帧」批量按钮；全部就绪后引导「批量出片」（软门，保留强制绕过）

## 5. Phase 4 · produce 迭代模型（2-3 天，upsert）

- upsert 键 = `(episode_no, segment_no)`：
  - 已有段 → 分镜节点 `data.shots` 原位替换；`rendered_by≠human` 的 prompt 重渲染；**video_gen 与 takes 全保留**
  - 资产按 `(kind, name)` 匹配 → 更新字段不重建
  - 新计划外的旧段 → `data.orphaned=true`，UI 进「已下线片段」分组（有 take 永不物理删除）
- `_repair_links` 的 `setdefault` 改确定性匹配，多余分镜输出警告清单
- 并发沿用现有 produce 锁（409）

## 6. Phase 5 · 裂缝批量修复（1 天，各项独立）

| # | 修复 | 做法 |
|---|---|---|
| 1 | lint 假门 | 删 `and not body.duration` 旁路；改显式 `force: true` 字段（前端「忽略校验，强制生成」按钮改传它） |
| 2 | 9:16 硬编码 | `_keyframe_prompt` 首行按项目 aspect_ratio 映射（竖屏/横屏/方形） |
| 3 | 30s 静默截断 | 片段时长合计 ≤30 进 lint（error）；generate 超限 422 而非钳制 |
| 4 | @名字 24 字截断 | 正则上限 24→64 |
| 5 | 切集正则 | 扩展「第N节/Episode N/EP N」；切出 0 或 1 集的长剧本给出可疑告警 |
| 6 | 审批执行切版 | 审批冻结计划记录各 clip 的 `current_take_id`；执行时校验 outputs 路径一致，不一致 409 要求重新审批 |
| 7 | 粒度口径统一 | 制片台方案/演示脚本文档改为「一镜一段 3-8s」 |

## 7. 测试与验收

每 Phase 一个 `demo/` invariants 脚本（mock 全链路，沿用现有回归风格）：

| Phase | 脚本 | 关键断言 |
|---|---|---|
| P1 | `takes_invariants.py` | 重生成不换当前版；指纹随分镜/形象变化；旧产物不误报 |
| P2 | `composition_invariants.py` 扩展 | 对白计划顺序与偏移；srt 时间轴；混音不打乱 clip 序 |
| P3 | `look_chain_invariants.py` | 特写请求带全身参考；locked 拒绝；首帧墙端点字段 |
| P4 | `produce_upsert_invariants.py` | 二次 produce 无重复段；takes 保留；孤儿标记 |
| P5 | 并入上列脚本 | 各裂缝的回归断言 |

**人工终验**：按 `docs/演示脚本.md` 从新建项目走到成片，产出**有声**竖屏 mp4（mock TTS 亦可），全程无静默替换、无静默截断。

## 8. 明确不做（本期）

双策划管线合并、production data schema 收敛、ref 编号绑定资产身份（P3 结构项）；VLM 一致性抽查、BGM、口型同步（二期）；鉴权/扣费/任务持久化（底层兜底，用户明确后置）。

## 9. 里程碑合计

P0 半天 → P1 1-2 天 → P2 3-4 天 → P3 2-3 天 → P4 2-3 天 → P5 1 天，**约 2.5-3 周**。每 Phase 结束独立可演示：P1 后可放心重跑生成，P2 后成片有声，P3 后一致性链与首帧墙可用，P4 后剧本可迭代，P5 收尾。
