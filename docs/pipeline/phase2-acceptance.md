# 阶段 2 验收清单与低成本验证法

- 日期：2026-10-25
- 分支：ZBWzhao/phase2-brain（d5840f5..c164e4a，12 提交）
- 对应验收条目：spec《技能层重构设计》第 8.4 节 8 条
- 前提事实：**阶段 2 全链是纯文本链路**——七类产物 JSON + node 质量门脚本，不调用生图/生视频/媒体 API。媒体生成是阶段 3 接缝，阶段 2 代码路径不存在媒体调用。

## 一、三层验证法（按成本从零到低）

### 第 1 层：零成本——自动化端到端（已锁，随时可跑）

```bash
python -m pytest tests/ -q        # 269 passed（需 dramaflow-postgres 容器在 :5432）
cd web-vue && npm run build       # 零错误
```

关键测试：六阶段链 e2e（`tests/pipeline/test_pipeline_run.py`）用 **ScriptedLLM + 真 node 质量门 + 真 Postgres** 跑完 novel→outline→⛔确认门→script→characters→art→staging→storyboard，断言每阶段产物落库、确认门不可跳过、失败无脏数据。这证明的是**机器本身正确**，不证明真 LLM 表现。

### 第 2 层：单阶段真实 LLM 冒烟（推荐的真实验证方式，几毛钱级）

不跑全链，逐阶段冒烟——每个阶段独立验证真模型能否在预算轮数内过质量门：

```bash
python -m core.skill_executor outline  <小说.txt> --project smoke1 --provider auto
python -m core.skill_executor script   <小说.txt> --project smoke1 --provider auto
python -m core.skill_executor characters <小说.txt> --project smoke1 --provider auto
# art/staging/storyboard 同理（stage 名来自 STAGE_SPECS）
```

- 写入 `pipeline_test` 库，不碰真实项目数据；
- `--provider auto`：TF_ROUTER 配额不足自动回退 DeepSeek 直连；
- `--max-iterations` 默认 20；
- 已知风险（终审标记）：真模型在 chain_repair 两轮纪律下能否一轮产全字段+finish，只有冒烟能证明；art 产物缺 `dynasty` 字段是否影响下游，冒烟时顺带看一眼。

**2026-10-25 实测结果**（样例《最后一单》2764 字，deepseek-chat，余额耗约 ¥2）：

| 阶段 | 结果 | 证据 |
|---|---|---|
| outline | ✅ 过门落库 | 18 轮 / 7 次门运行；含一次工具误用（data 传成 JSON 字符串）被工具报错纠正的完整自我修正闭环。**组装预算因此从 16 提到 20**（41d6097） |
| script | ❌ 预算内未收敛，**未落库（无脏数据红线保持）** | 20 轮 / 18 次门运行；时长门持续不过：10 集目标 120s，模型持续欠时长（最多 -27.6s/集）。真模型的**时长欠移偏差**是真实缺陷，非 harness 问题 |

script 阶段收敛策略进快速跟进（见第四节第 8 条）。outline 冒烟证明：门即环境反馈的自我修正 + 失败安全（不过门不落库）在真模型上成立——这是作品集的核心叙事，两条都已有一手证据。

**2026-10-25 复冒烟（phase2-fastfollow：Task 1 修复指令"折算给死、补足定向"生效后；deepseek-chat，本组冒烟累计花费 ¥0.21，含首次上游缺失空转的 ¥0.05）**：

| 阶段 | 结果 | 证据 |
|---|---|---|
| script | ✅ **过门落库（上轮未收敛问题收敛）** | 12 轮 / 6 次门运行；时长欠口逐轮单调收窄 −4.2s → −2.7s → −0.2s → 过门（总 270.7s / 目标 270s）。对比上轮 20 轮 / 18 门 / 最深 −27.6s 不收敛：repair_note 的折算口径（每欠 4.5s ≈ 补 20 字对白或 1 动作行）让修复从"微调接近达标的集"变成"定向补欠时最多的集" |
| characters | ❌ 两轮预算内未过，**未落库（失败安全保持）** | 2 轮 / 1 次门运行；第 1 轮产出全字段但 finish 落在第 2 轮（大 JSON 产稿跨轮）→ 门报 6 违规（5 个角色 voice.prompt 全部超 400 字上限，447~516 字——真模型系统性写超；另 1 条引文非逐字）→ 修复轮预算已被产稿占用，**实际未执行**。见第四节第 9 条 |
| art / staging / storyboard | 未跑 | 纪律：任一阶段失败即停；staging/storyboard 上游依赖 cast，characters 未落库即无可验上游 |

复冒烟口径说明：smoke CLI 硬编码连 `pipeline_test` 库，原 `smoke-outline-final` 的 outline 行已不在库中（早前冒烟产物被库清理）；本次从 dramaflow 库 `phase1-acceptance`（同小说《最后一单》真模型 outline，3 集 × 90s）种子移植恢复上游前提。与上轮失败基线（10 集 × 120s）**不是同一份 outline**，script 收敛对比是修复口径生效的证据，非严格同基线对照。

**2026-10-25 复冒烟 II（phase2-fastfollow-2：chain_repair 结构保证 + 小队列批次落地后；deepseek-chat，本次约 ¥0.08）**：

| 阶段 | 结果 | 证据 |
|---|---|---|
| characters | ❌ 仍未落库（失败安全保持） | 2 轮 / 1 次门运行；**结构保证无误伤**：模型 r1 产全 4 键（source/lang/summary/characters）、r2 才 finish——键全合法放行，未发生缺键拒绝（半成品修复问题未再出现）。门在 r2 末运行报 8 违规（5×voice.prompt 403~475 字超 400 上限 + 3×引文非逐字），修复消息已注入但**轮界（min(20,2)+0 拒绝=2）已到，修复轮从未运行**。失败形态从上轮「半成品修复（6 违规）」变为「产稿跨轮占满轮界、修复轮零预算」——deepseek-chat 对 characters 体量无法一轮内产稿+finish，预算按门计救不了轮界先耗尽。候选 a（预算 2→3）待拍板：本次 8 违规全部可修（压 5 段 voice.prompt + 补 3 条逐字引文），若修复轮存在大概率过 |
| 冒烟入口本身 | ✅ 队列 10 实证 | 首跑即被上游快失败拦截（pipeline_test 库 outline 缺失）：「上游产物 outline 不存在——先跑它的阶段冒烟，别空烧 LLM」，**零 LLM 调用退出**；种子移植 outline 后重跑才进入模型循环 |

**2026-10-25 复冒烟 III（预算 2→3 批准落地 9659bb7 后；deepseek-chat，本次约 ¥0.18）**：

| 阶段 | 结果 | 证据 |
|---|---|---|
| characters | ❌ 仍未落库，但**修复机制完全生效** | **3 轮 / 3 次门全用满**：r1 产 4 键+finish 同轮（模型 variance，本轮做到）→ 门 1 多违规；r2 修复轮真跑但 write_draft 把数组编码成 JSON 字符串被工具打回 → 门 2 只剩 1 违规；r3 修正数组+finish → 门 3 同一条依旧。**违规 8 → 1**，预算 3 买到了 2 个真实修复轮（复冒烟 II 的修复轮零预算问题解决）。残留 1 条是**引文保真**（模型 paraphrase 原文当逐字引，两轮修复未自察）——预算买不到的内容口径问题，转队列 11，不再自动重试 |

测试与冒烟共用 pipeline_test 库的操作注记：pytest 各用例 TRUNCATE pipeline_artifacts，跑过测试后移植的 outline 即消失——smoke 前先重移植（快失败会拦截，零成本提醒）。

**gated run 整页刷新重挂载（零成本 CDP 验证，worktree :8001 + 真 Postgres）**：手动插入 `status='gated'` run 行后打开管线页——run 卡 + 「确认，开写剧本」可见 ✅；真实整页刷新后 run 卡仍在（runs-list 重挂载生效，未点击放行）✅。实测注：hash 路由下 Page.navigate 到同一 URL 是同文档导航，F5 语义用 Page.reload 完成并经 window 标记验证确为整页重载。

### 第 3 层：真实全链（真正的"全自动出片"验收）

**已具备全链条件**：零成本层 ScriptedLLM 八阶段端到端全绿；真模型单阶段 outline/script 均已过门落库（script 在修复指令折算化后单调收敛）；确认门不可跳过、失败不落库可重试、服务器重启清扫 interrupted run、整页刷新 run 卡重挂载——代码 + 测试 + 浏览器验证齐备。全链骨架（novel→outline→⛔确认→script→characters→art→staging→storyboard）机器侧成立。

**剩余风险（真实 LLM 全链的已知缺口，均不阻塞骨架）**：
1. chain_repair 预算问题已解决（结构保证 65ad27c + 预算 3 经批准 9659bb7，复冒烟 III 修复轮真跑、违规 8→1）；残留 characters 引文保真口径（队列 11）与 art/staging/storyboard 真模型单阶段证据缺位——真实全链首跑 characters 仍可能停在引文保真上；
2. art/staging/storyboard 缺真模型单阶段证据（本轮停在首败未跑）；
3. router-view 无 `:key`：跨项目同组件导航会复用视图实例，正文可能短暂滞后（顶栏项目名已如实，0a5db07）；其余 cosmetic 小项见队列 7。

成本口径不变：全链 = 各阶段 token 费（DeepSeek 级单价估 3-10 元/本，取决于门失败重试轮数），不含任何生图/生视频。本轮实测：script 12 轮 6 门约 ¥0.05、characters 2 轮 1 门约 ¥0.07——单阶段"几毛钱"的预估成立。

## 二、浏览器手工验收清单（merge 到 Desktop 后执行）

1. **创作入口**：新建 pipeline-only 项目 → 粘贴小说（≥50 字，不足被挡）→ 直播进度：工具调用/质量门/阶段事件滚动；中途刷新页面 → 重连后 hello 帧恢复状态（整页 F5 的 run 卡重挂载已实现并经 CDP 验证——重挂载后 run 卡与「确认，开写剧本」可见；残留边界：重挂载的 stages 明细靠 hello 快照，历史事件不回放）
2. **确认门**：大纲完成后停在确认对话框 → 「查看大纲并确认」→ 确认后自动出剧本；「记录意见」只记录、明确提示不触发自动重写；无任何跳过确认的路径
3. **制片四层**：characters→art→staging→storyboard 依次自动执行，制片台面板状态机 idle/running/failed/done 正确切换
4. **改稿脑三拍**：对剧本提修改 → 提案卡出现 → 审批 → 自动重跑质量门（体检）；**坏提案回滚**：审批一个会让门失败的修改 → 收到 200 `{ok:false, rolled_back:true}` → 「已回滚」toast + 面板刷新回原内容
5. **运行详情**：运行抽屉里可见每阶段迭代数/门通过率（数据来自 stage_status 权威账本）
6. **失败重试**：门最终失败 → 「重试本阶段」→ 上游产物完好，无脏数据
7. **跨项目隔离**：项目 A 的 run 进行中切到项目 B → B 的制片台看不到 A 的 run 卡、确认按钮不可达
8. **资产库桥**：characters/art 产物同步到资产库 → 只增不改不删（非空字段不被覆盖）；再次同步幂等（added=0）

## 三、验收状态（对照 spec 8.4）

| # | 条目 | 状态 |
|---|---|---|
| 1 | 网页粘贴小说自动产出大纲，无 Claude Code 参与 | 代码+测试就绪；真 LLM 待第 2/3 层 |
| 2 | 大纲确认后自动出剧本，门报告前端可见 | 同上 |
| 3 | 制片四环节依次执行，产出分镜投产包 | 同上 |
| 4 | 失败可重试，无脏数据 | ✅ 测试锁定 |
| 5 | 断线重连进度可恢复 | ✅ 达成：SSE 断线重连（hello 快照）+ 整页刷新 runs-list 重挂载（2026-10-25 CDP 实证：gated run F5 后 run 卡与「确认，开写剧本」仍在）；服务器重启清扫 → failed 可重试测试锁定 |
| 6 | 三套脑合并：剧本页单一来源，资产库由管线同步 | ✅ 测试锁定 |
| 7 | 创作阶段 agent 自主迭代；迭代数/门通过率可见 | ✅ stats 接口+抽屉；真模型轮数待冒烟 |
| 8 | 改稿脑提修改→审批→自动体检，写入必须过审批门 | ✅ 测试锁定（agent 无 apply 工具，apply 端点唯一写入口+回滚） |

## 四、快速跟进队列（终审 POSTPONE，均不阻塞使用）

0. **顶栏项目名不随 URL 刷新**（2026-10-25 浏览器验证发现，存量问题非阶段 2 引入）：直接以 hash URL 进入另一项目时，内容区渲染新项目数据，但顶栏 `.pname` 仍显示上一个项目名（如从 署名 直接跳 冒烟-创作入口，顶栏还写着 署名）。经首页点选进入则正常。cosmetic 但有误导性。——**已实现（0a5db07）并于 2026-10-25 晚 CDP 复验通过（fastfollow-2 集成后）：整页切项目早采样即显示新项目名（回退生效），无串项目残留，本条关闭**
1. **runs-list 接口 + 页面刷新重挂载 + 启动清扫孤儿 run**（路由为 hash 模式，F5 本身能回到页面——2026-10-25 实测修正；真正的问题是 run 状态只在内存，刷新后 run 卡消失，gated run 无法批准；服务器重启后卡 running 的 run 同样无法处理）——**已实现（ce4f5ff）并零成本 CDP 验证：gated run 真实刷新后 run 卡仍在**（见第 2 层复冒烟节）
2. 回滚时 save 再抛异常 → 编辑卡 applying 直至重启；回滚前瞬态窗口可见 gated 内容（无事务，write-then-rewrite）
3. `pipeline_import.prepare()` 契约警告被丢弃（审计线索缺口）；SSE failed 事件 `stage:""` 哨兵值未声明
4. chain_repair 修复轮尾指令是 agent 纪律措辞，真模型可能先跑门再等结果烧掉最后一轮（失败安全，可重试）——2026-10-25 复冒烟实证了另一形态：产稿本身跨轮，见第 9 条
5. 四卡退役半程：UI 已切管线产物，冻结区 chat 工具仍写 canvas meta（留待清扫）；studioStore 死导出
6. 同项目换小说重跑保留旧剧本/角色卡（merge 语义正确、UX 未定；DELETE 产物接口是手动出口）
7. 小项：retry 按钮无 in-flight 防抖——已实现（0a5db07）并于 2026-10-25 晚 CDP 复验通过（Fetch 域拦截请求零 LLM 成本：挂起期「重试中…」+disabled，失败后 finally 复位）；其余：非 script apply 无体检门（可加 UI 提示）、`_sessions` 无上限、~~桥接 art 行文本未截断~~（fastfollow-2 d2c7807 已修）、重复 import 期 `_RouterLLM()` 实例
8. **script 时长门真模型收敛策略**（2026-10-25 冒烟实证：deepseek-chat 20 轮未收敛，10 集目标 120s 持续欠 1.3~27.6s）。候选方向：a) 修复轮指令只针对最欠的集做定向加长而非全量重写；b) SKILL.md 补"动作行也算时长"的加长技巧提示；c) outline 阶段加"目标秒数 × 集数 ≤ 源材料可支撑量"的可行性预警；d) 时长门对最后 1-2 轮的边际收敛给部分通过（±15% 放宽到 ±20% 需产品决策）——**已按 a+折算口径落地（e5e56b6），2026-10-25 复冒烟 12 轮 6 门收敛落库，本条关闭**
9. **chain_repair 两轮预算对真模型不足**（2026-10-25 characters 冒烟实证）：大 JSON 产稿跨 2 轮（finish 落在第 2 轮）→ 门报 6 违规（5×voice.prompt 超 400 字上限、1×引文非逐字）→ 修复轮无预算可跑，ok=False 未落库（失败安全保持）。候选：预算 2→3 轮；或要求产稿与 finish 同轮完成（write_draft 分节后当轮 finish）；或门失败后带满预算自动重试一次（设成本上限）。另：voice.prompt 400 字上限被 5/5 角色系统性写超，SKILL.md 的字数口径提示值得前置到产稿指令——**已关闭：b+ 结构保证落地（65ad27c）→ 复冒烟 II 证明还需预算 → 用户批准候选 a，预算 2→3 落地（9659bb7）→ 复冒烟 III：3 门全用、2 修复轮真跑、违规 8→1（见第 2 层复冒烟 III）。残留引文保真问题转队列 11**
10. **smoke CLI 上游缺失快速失败**：冒烟入口对 gate_needs 先查 pipeline_artifacts，缺上游直接报错退出——2026-10-25 实证缺失时 20 轮对门 ENOENT 空转烧了 ¥0.05（skill_executor.py 硬编码 pipeline_test 库，库被清理后上游静默消失）——**已落地（d2c7807）并在复冒烟 II 实证：首跑即被拦截，零 LLM 调用退出，本条关闭**
11. **characters 引文保真**（2026-10-25 复冒烟 III 实证）：模型把 paraphrase 当逐字引文（「但车库进出口走的是独立线路…」非原文片段），2 个修复轮未自察。候选：a) 执行器侧 characters repair_note：「引文必须是小说原文连续逐字片段，找不到就删 quote 字段或换能逐字找到的句子」；b) 门侧模糊匹配放宽（产品决策，涉及阈值红线）。voice.prompt 系统性超长的产稿口径提示（见第 9 条历史注记）可与 a 一并做

## 五、浏览器验证记录（2026-10-25，headless Edge + CDP）

零 LLM 成本完成的 UI 消费端验证（导入《署名》大纲+剧本，另建空项目测创作入口）：

| 检查 | 结果 |
|---|---|
| 后端重启 → 0006 迁移自动应用（schema_migrations 六条全记） | ✅ |
| 《署名》导入：outline 200 / script ep1 200 / script ep2-3 批次 200，**逐集合并契约**三集齐全（ep1 保留 + ep2/3 并入） | ✅ |
| 剧本页：《署名》三集字数/场景头/对白渲染正确；改稿提案空态文案正确；AI 助手"确认才应用"提示可见；零 JS 错误 | ✅ |
| 「管线产物」tab：characters/art/staging/storyboard 四面板空态正确（只导了大纲+剧本） | ✅ |
| 创作入口（无产物项目）：textarea + ≥50 字门（5/50 禁用 → 100/50 启用）+「开始创作」+ 如实确认门文案 | ✅ |
| CreateRunPanel 仅在无产物项目或有活跃 run 时显示（署名有产物 → 入口隐藏，设计行为） | ✅ |
| 顶栏项目名 direct-URL 不刷新 | ⚠️ 存量，见队列 0 |

未验证（需真实 LLM，见第 2/3 层）：run 启动后的直播进度、确认对话框交互、门报告流、改稿三拍、失败重试。

## 六、给面试官的演示口径

架构 = **Workflow 当脊柱，Agent 当关节**：确定性状态机管顺序和门，创作阶段是带 4 工具的 gated agent loop（门即终止条件），填表型阶段链+一次修复，改稿脑写入必过审批门。每次 run 的迭代数/门通过率/工具调用留痕可查——"门即环境反馈的自我修正"是可验证的 Agent 能力，不是套壳工作流。
