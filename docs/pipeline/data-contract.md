# 管线产物数据契约（阶段 0 定稿）

事实来源优先级：技能 references/schema.md > SKILL.md 产出段落 > examples/*.json 实际键。
本文档是 core/pipeline_artifacts.py 注册表与阶段 1 导入 API 的唯一依据。

## 类型映射表

| 规范名 | 产出技能 | JSON 文件名 | schema 说明文件 | 备注 |
|---|---|---|---|---|
| outline | novel-outline | outline.json | references/schema.md | 工作文件 `outline.json`，最终落地 `<书名>-outline.json`（另产 .md + report.html） |
| script | novel-script | script.json | references/schema.md | 最终落地 `<剧名>-script.json`；一份文件覆盖一个集数区间（一批 ≤ 3 集） |
| characters | novel-characters | cast.json | references/schema.md | 最终落地 `<书名>-cast.json`；注意文件名是 **cast** 不是 characters |
| art | novel-art | art.json | references/schema.md | 最终落地 `<剧名>-art.json` |
| staging | novel-staging | staging.json | references/schema.md | 固定名 `staging.json`（主交付是 staging-report.html，JSON 是工作数据） |
| storyboard | novel-storyboard | storyboard.json | references/schema.md | 最终落地 `<剧名>-storyboard.json` |
| character_refs | character-refs | asset.json | references/schema.md | 阶段 3 出图，先只验收提示词产物；路径 `<输出目录>/<角色名>/asset.json`，**一个角色一个文件**，与其余六类"一项目一文件"不同 |

七类产物的落库 `artifact_type` 用左列规范名；导入校验以各技能 `references/schema.md` 的字段事实为准，字段表见下文各节。

## outline（短剧大纲五件套）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**（7 个技能的 schema.md / 脚本 / 样例均无此键）；可用版本锚点为技能 frontmatter `version: 2.0.0`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 书名/篇名 |
| `lang` | string | 否 | 报告界面语言代码（默认 `zh`），`--lang` 优先于它 |
| `params` | object | 是 | 改编参数：集数、单集时长、题材、改编幅度等（见下） |
| `adaptation` | object | 是 | 改编说明：核心一句话 + 取舍记录（keep/cut/merge/risks） |
| `characters` | object[] | 是 | 人物表，下游 novel-characters 的角色清单 |
| `scenes` | object[] | 是 | 场景表，`S01` 编号全局唯一 |
| `props` | object[] | 否 | 叙事道具表（默认上限 8 件），`P01` 编号 |
| `beats` | object[] | 是 | 爽点表，`episode` 是爽点落集的唯一数据源 |
| `episodes` | object[] | 是 | 分集梗概，`ep` 从 1 连续编号且总数 = `params.episodes` |

- 重要嵌套：
  - `params`：`episodes`（总集数）、`minutesPerEpisode`、`genre`（题材，决定爽点类型）、`adaptMode`（忠实/抽核/借壳 三选一）、`preferences[]`（可选）；`thresholds` 可选覆盖质量门阈值：`maxLeads`(5)/`maxSupport`(10)/`maxFunctional`(10)/`maxBeatGap`(3)/`maxPrimaryScenes`（随集数动态：4+⌈集数/10⌉ 夹在 5–15）
  - `adaptation`：`core`（必填）、`keep`/`cut`/`merge` 每条 `{what, why}`（`keep` 至少 1 条、可带 `evidence` 原文逐字片段；adaptMode 非忠实时 `cut` 不能为空）、`risks` 每条 `{what, plan}`、可选补注 `cutNote`/`mergeNote`
  - `characters[]`：`id`(C01)、`name`、`role`、`tier`（lead/support/functional 三档）、`arc`（lead/support 必填）、`from[]`（非空，改动记录，纯原创写 `["原创"]`）
  - `scenes[]`：`id`(S01)、`name`、`primary`(bool)、`reusePlan`（在全剧只出现一次的场景必填）
  - `props[]`：`id`(P01)、`name`、`function`（必填——这件物件在戏里承载什么）、`beatIds[]`（可选，须指向真实 beats id）
  - `beats[]`：`id`(B01)、`type`（自由文本）、`weight`（major/minor，缺省 minor）、`episode`（落集号）、`setup`、`payoff`
  - `episodes[]`：`ep`、`synopsis`/`hook`/`suspense`（三栏必填、叙述体、禁引号对白）、`sceneIds[]`/`characterIds[]`（必填且指向已登记 id）、`propIds[]`（可选）、`crowdPlan`（characterIds ≥ 3 必写）、`warnings[]`（生成难点）

## script（剧本：场次 + 节拍流）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 2.0.0`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 剧名（seed 从 outline.json 搬来） |
| `episodes` | object[] | 是 | 剧本正文，每集一条（一份文件通常覆盖 ≤ 3 集） |
| `params` | object | 否 | 时长折算参数，省略用默认：`charsPerSecond`(4.5)/`actionSeconds`(2.5)/`tolerance`(0.15)/`maxLineChars`(35)；SKILL.md 另有 `hookWindow`(3) |
| `lang` | string | 否 | 报告界面语言（SKILL.md 提及顶层 `lang` 字段；样例未写） |

- 重要嵌套：
  - `episodes[]`：`ep`、`targetSeconds`（目标秒数，seed 按 minutesPerEpisode×60 算）、`hook`/`cliff`（开场钩子/结尾悬念说明，必填）、`beatsClaimed[]`（认领的大纲爽点 type，字段必须在、可为空数组）、`hookBeat`（`[场, 拍]` 钩子具象认领位置，须落在全集前 hookWindow 拍内）、`scenes[]`
  - `episodes[].scenes[]`：`sceneId`(S01，对账 art.json)、`lighting`（可选，对账 art.json 光照状态）、`characters[]`（C 编号，空镜空数组）、`props[]`（可选，P 编号）、`flow[]`（节拍流，动作与台词交替）
  - `flow[]` 节拍二选一：动作节拍 `{action}`（叙述体，禁引号台词）；台词节拍 `{speaker, line, delivery}`（speaker 是本场角色 id 或 `VO`，line 单句 ≤ 35 字，delivery 可省）；一拍不能两者都有，也不能都没有

## characters（角色设定卡，cast.json）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 2.0.0`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 书名/篇名，报告标题用 |
| `lang` | string | 是 | 报告语言，默认 `zh` |
| `summary` | string | 是 | 故事摘要 3–5 句（缺失会被 validate 判违规） |
| `characters` | object[] | 是 | 角色卡数组，按戏份排序 |
| `ui` | object | 视情况 | 界面文案翻译；`lang` 为内置的 zh/en/ja 时不需要，其他语言必填 |

- 重要嵌套：
  - `characters[]` 卡：`name`（原文最常用称呼）、`aliases[]`（其他称谓）、`importance`（**只能** protagonist/major/supporting/minor）、`oneLiner`（本地语言一句话画像）、`persona`、`image`、`voice`
  - `persona`：`gender`/`ageRange`/`identity`/`appearance`/`personality[]`(3–5 词)/`temperament`/`motivation`/`arc`（本地语言）；`relationships[]` 每条 `{name, relation}`；`evidence[]` **原文逐字引用，永不翻译**
  - `image`（**全英文**、禁人名）：`prompt`（单张人物图）、`promptLocal`（上条的本地语言译文，lang=en 时省略）、`negativePrompt`、`tags[]`（4–8 个检索标签，不打画风标）、`sheet`（16:9 三区设定图版面指令：左约 34% 半身像/右上三视图/右下细节条）
  - `voice`：`timbre`/`pitch`/`pace`/`accent`/`emotion`/`referenceHint`（本地语言）；`prompt`（英文，给 TTS 音色引擎，无 promptLocal 对照——有意设计）

## art（美术设定集：场景 + 叙事道具）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 2.0.0`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 剧名/书名 |
| `scenes` | object[] | 是 | 场景设定数组 |
| `props` | object[] | 否 | 叙事道具数组（3–8 件为宜），大纲无 props 时可留空由模型从原文提取 |
| `dynasty` | string | 未强制 | 文档级朝代/年代（`明`/`北宋`/`民国`），schema.md 正文说明、顶层必填表未列；**自带样例缺失此键** |
| `lang` | string | 否 | 报告界面语言（SKILL.md 提及顶层 `lang` 字段；样例未写） |

- 重要嵌套：
  - `scenes[]`：`id`(S01)、`name`、`primary`(bool)、`summary`（设计意图）、`anchors[]`（一致性锚点 `{name, desc}`，**3–5 个**）、`lighting[]`（光照状态 `{state, prompt}`，**≥1**，每个状态落成完整英文提示词）、`image{prompt, negativePrompt, sheet, tags}`（全英文，negativePrompt 必须禁人——空景硬保证）、`variantOf`（可选，变体母场景 id）、`changes`（variantOf 时必填）、`usage{episodes, beats}`（可选，seed 自动填）
  - `props[]`：`id`(P01)、`name`、`scale`（**枚举**：手持级/桌面级/家具级，对应英文短语必须进提示词）、`summary`（戏剧功能）、`anchors[]`（3–5 个）、`states[]`（状态变体 `{state, prompt}`，**≥1**）、`relatedScenes[]`（可选）、`carriedBy[]`（可选，自由文本）、`image{prompt, negativePrompt, sheet}`（英文，白底无手、禁人禁手）、`usage{episodes, beats}`（可选）

## staging（开场站位）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 1.0.1`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 剧名，seed 从 script.json 搬来 |
| `lang` | string | 是 | 工作语言 `zh`/`en`：东西名、人名、走位句用哪种语言写 |
| `locations` | object[] | 是 | 每个地点一条：从场景图认出的可站可坐的东西 |
| `scenes` | object[] | 是 | 每一场（剧本场次）一条：开场站位 |

- 重要嵌套：
  - `locations[]`：`sceneId`(S01，对账 art.json)、`name`（地点名，报告按它找图）、`readFrom`（`picture` 看图认的 / `prompt` 无图按提示词推算 / null 未认）、`items[]`（**4–20 件**，按从北到南排）
  - `locations[].items[]`：`name`（工作语言，不重复）、`alt`（另一种语言名）、`box`（`[左,上,右,下]` 图上位置 0–1，readFrom=picture 必填）、`plan`（`[西,北,东,南]` 俯视平面 0–1，按透视推算）、`on`(bool 能坐站)、`round`（可选，圆形物）
  - `scenes[]`：`ep`、`scene`（集内第几场，从 1 起）、`sceneId`（在哪个地点）、`lighting`（可选，只给人看）、`cast[]`（`{id, name}` 出场人物，seed 搬来不改）、`places[]`（每人正好一条）、`extras`（群演人数，整数）、`extrasWhere`（群演在哪；extras 为 0 时写 null）、`why`（可选，一句排位理由）
  - `scenes[].places[]`：`id`/`name`（照抄 cast）、`present`(bool 开场在不在)、`item`（本地点 items 之一的名字，不在场 null）、`side`（north/east/south/west/on/null）、`along`（沿该侧 0–1；side=on 时 0.5）、`posture`（sit/stand）、`facing`（八方位 north/northeast/east/southeast/south/southwest/west/northwest）

## storyboard（分镜：段 → 切）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 2.0.0`
- 顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `source` | string | 是 | 剧名 |
| `aspectRatio` | string | 否（合同字段） | 画幅合同，只接受 `16:9`/`9:16`，默认 `16:9`，分镜图与下游生成图共同遵守 |
| `promptLang` | string | 否 | H3 视频提示词语言，默认 `en`（官方口径），可切 `zh` |
| `params` | object | 否 | 时长参数，省略用默认：`maxSegmentSeconds`(15)/`minCutSeconds`(2)/`maxCutSeconds`(5)/`maxOnScreen`(3)/`tolerance`(0.15) |
| `episodes` | object[] | 是 | 每集一条：`{ep, segments[]}` |
| `lang` | string | 否 | 报告界面语言（SKILL.md 提及；优先级 `--lang` > 顶层 `lang` > 默认中文，与 promptLang 互相独立） |

- 重要嵌套：
  - `episodes[].segments[]`（段 = 一次视频生成，≤ maxSegmentSeconds，不跨场）：`id`（段号 `E01-01`，按顺序连号，即素材文件名）、`sceneIndex`（剧本该集第几场，1 起）、`cuts[]`、`h3Prompt`（一段一条 MiniMax H3 视频提示词，结构见 h3-prompt.md）、`blocking`（开场构图：谁左谁右面朝哪，**必填**，composition 门）、`soundscape`（Seedance 音景，中文）、`music`（可选配乐）、`note`（可选）
  - `segments[].cuts[]`（切 = 段内一次剪切，2–5 秒）：`beats`（`[起, 止]` 认领剧本节拍，每拍恰好被一镜认领）、`seconds`、`size`（景别枚举：extreme-wide/wide/medium/close/extreme-close）、`camera`（运镜，H3 官方词表原样字符串）、`characters[]`（画内 C 编号，⊆ 剧本该场人物，超 maxOnScreen 须带 note）、`props[]`（可选）、`frame`（分镜图提示词，中文，**直呼角色名**，景别中文词必须在）、`shot`（镜头正文，中文，**通用身份禁角色名**）、`lens`/`cameraPosition`/`composition`/`eyeline`/`focus`/`stability`（六个构图量化字段**每镜必填**；stability 枚举 stable/slight-shake/handheld）、`sfx`/`lighting`/`recipe`/`note`（可选；recipe 是镜头配方卡 id，挂 --shots 才查）

## character_refs（角色参考图资产，asset.json）

- schema_version 出处与当前值：**上游未定义 schema_version 字段**；版本锚点为技能 frontmatter `version: 2.1.0`
- 顶层字段（产出 `asset.json`，由脚本读写、不要手改 `views`）：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `name` | string | 是 | 角色名，只用于文件名、标识和报告，不进提示词 |
| `source` | string | 否 | 描述来源标注（如 "样例 · 茶山"） |
| `lang` | string | 否 | 确认表与报告语言，缺省按 `zh` |
| `ui` | object | 视情况 | 非内置语言的自译界面文案 |
| `layers` | object | 是 | 角色层：换造型也不变的身份/脸/发/身形/皮肤 |
| `outfits` | object | 是 | 造型层，v1 只有 `default` 一套 |

- 重要嵌套：
  - `layers`：`identity{age, gender, en, text, source}`、`face`/`hair`/`build`/`skin`/`backCue` 各 `{en, text, source}`（en 进提示词——永远英文不写角色名；text 给人看；source 标注 stated/inferred/default 如实标记；build/skin/backCue 可省）
  - `outfits.<造型>`：`label`、`top`/`bottom`（`{en, text, source}`，大头照只用上装）、`details[]`（细节图槽位 `{slot, en, text, source}`，最多 8 张）、`overrides`（该造型要改的角色层）、`look`（画风快照 `{id, label, medium, style, clean, neg}`，medium: photo/drawn）、`upgrades[]`（升档记录 `{tier, reason, at}`）、`views`（每视图 `{current, versions[]}`，重出只追加不删）
  - 视图版本对象：`v`、`id`（`角色/造型/视图/版本`）、`file`、`sha256`、`model`（该张用的模型）、`seed`、`prompt`/`negative`（实际发出的提示词）、`refs[]`（实际挂的参考图 `{view, v, sha256}`）、`notes[]`、`layersHash`/`lookHash`（过期判定指纹）、`gates[]`（检查门结果 `{id, label, ok, detail}`）、`createdAt`、`confirmed`/`confirmedAt`（仅锚点：false 待确认/true 已确认/"auto"）
  - 一致性机制：全组图只有一个根——正面全身锚点 `front-full`（文生图），其余视图只参考锚点（细节图另挂大头照）；文字指纹/画风快照/参考图版本任一变化即标过期，只标记不删除
- 输入样例（**非产物**，`examples/阿禾-intake.json`，验收提示词产物时对照用）顶层字段：

| 字段 | 类型 | 必填 | 语义（一句话） |
|---|---|---|---|
| `name` | string | 是 | 角色名 |
| `lang` | string | 是 | 用户说话的语言（zh/en/ja 内置） |
| `source` | string | 否 | 描述来源标注 |
| `identity` | object | 是 | `{age, gender, en, text, source}`——年龄/性别/年代推不出必须问用户 |
| `face` | object | 是 | 脸部描述 `{en, text, source}` |
| `hair` | object | 是 | 发型描述 `{en, text, source}` |
| `build` | object | 否 | 身形描述 `{en, text, source}` |
| `skin` | object | 否 | 皮肤描述 `{en, text, source}` |
| `outfit` | object | 是 | 服装 `{id, label, top, bottom, details[]}`，top/bottom 各 `{en, text, source}`，details 每条 `{slot, en, text, source}` |

## 存储与导入约定

- Postgres 表 `pipeline_artifacts`，PRIMARY KEY (project_id, artifact_type)，latest-wins upsert；
- schema_version 随产物落库，上游升级时导入层做版本迁移；

## 已知缺口

- **上游产物 JSON 均无 schema_version 字段**：7 个技能的 schema.md、脚本、示例 JSON 里都没有 schema_version/schemaVersion 键（全库检索无命中）。落库的 schema_version 只能由本契约定义——建议初始值取各技能 frontmatter `version`（novel-outline 2.0.0 / novel-script 2.0.0 / novel-characters 2.0.0 / novel-art 2.0.0 / novel-staging 1.0.1 / novel-storyboard 2.0.0 / character-refs 2.1.0），导入层按此校验。
- **character-refs 无输出样例**：`examples/` 只有输入样例 `阿禾-intake.json`，没有 `asset.json` 产物样例，故 asset.json 的字段表只对照 `references/schema.md`、未做实际键交叉核对；出图后端（ComfyUI/OpenAI）阶段 3 才接入，先只验收提示词产物（intake/asset 的 en 字段与 sheet 版面指令）。
- **art.json 样例与 schema.md 有出入**：schema.md 正文说 `dynasty` 是文档级字段（整部剧一个朝代），但顶层必填表未列它、自带样例也缺失此键（样例仅 `props`/`scenes`/`source` 三键）；`lang` 同样只在 SKILL.md 提及、样例未写。导入层对 `dynasty`/`lang` 不宜做硬必填校验，必填性以本文档 art 节标注为准。
- **script.json / storyboard.json 样例键少于 schema**：两份样例分别只有 `episodes`+`source` 与 `aspectRatio`+`episodes`+`source`，缺的全是文档标注"可省略"的键（params/lang/promptLang），与 schema.md 无实质冲突，按可省略处理。
- **outline `reusePlan` 为条件必填**：只有"全剧只出现一次的场景"必须带，自带样例的场景均为可复用场景故未写；导入校验若查此键需结合 `primary` 与分集引用判断，或按可缺省处理。
- **staging 的主交付是 HTML**：`staging-report.html` 是默认主交付，`staging.json` 是工作数据；导入管线只收 JSON，拖动调整后的 JSON 以用户导出覆盖回的版本为准。
