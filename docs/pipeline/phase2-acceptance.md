# 阶段 2 验收清单与低成本验证法

- 日期：2026-10-25
- 分支：ZBWzhao/phase2-brain（d5840f5..c164e4a，12 提交）
- 对应验收条目：spec《技能层重构设计》第 8.4 节 8 条
- 前提事实：**阶段 2 全链是纯文本链路**——七类产物 JSON + node 质量门脚本，不调用生图/生视频/媒体 API。媒体生成是阶段 3 接缝，阶段 2 代码路径不存在媒体调用。

## 一、三层验证法（按成本从零到低）

### 第 1 层：零成本——自动化端到端（已锁，随时可跑）

```bash
python -m pytest tests/ -q        # 262 passed（需 dramaflow-postgres 容器在 :5432）
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
- `--max-iterations` 默认 20（真模型大纲实测 12 轮过门，16 勉强）；
- **判断标准**：每阶段产物 JSON 落库 + 门报告全过 + gate_log 里能看到工具调用轮数。单阶段几毛钱，六个阶段全冒烟 ≈ 2-3 元级。
- 已知风险（终审标记）：真模型在 chain_repair 两轮纪律下能否一轮产全字段+finish，只有冒烟能证明；art 产物缺 `dynasty` 字段是否影响下游，冒烟时顺带看一眼。

### 第 3 层：真实全链（真正的"全自动出片"验收，成本可控）

网页粘贴小说 → 全链跑通。成本 = 各阶段 token 费（DeepSeek 级单价下估 3-10 元/本，取决于小说长度和门失败重试轮数），**不含任何生图/生视频**。等第 2 层六个阶段都单独过了再跑这个，避免预算浪费在已知问题上。

## 二、浏览器手工验收清单（merge 到 Desktop 后执行）

1. **创作入口**：新建 pipeline-only 项目 → 粘贴小说（≥50 字，不足被挡）→ 直播进度：工具调用/质量门/阶段事件滚动；中途刷新页面 → 重连后 hello 帧恢复状态（已知边界：整页 F5 会丢 run 卡，见第四节）
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
| 5 | 断线重连进度可恢复 | **有条件达成**：SSE 断线重连 ✅（hello 快照）；整页刷新重挂载 ❌（见第四节首条） |
| 6 | 三套脑合并：剧本页单一来源，资产库由管线同步 | ✅ 测试锁定 |
| 7 | 创作阶段 agent 自主迭代；迭代数/门通过率可见 | ✅ stats 接口+抽屉；真模型轮数待冒烟 |
| 8 | 改稿脑提修改→审批→自动体检，写入必须过审批门 | ✅ 测试锁定（agent 无 apply 工具，apply 端点唯一写入口+回滚） |

## 四、快速跟进队列（终审 POSTPONE，均不阻塞使用）

0. **顶栏项目名不随 URL 刷新**（2026-10-25 浏览器验证发现，存量问题非阶段 2 引入）：直接以 hash URL 进入另一项目时，内容区渲染新项目数据，但顶栏 `.pname` 仍显示上一个项目名（如从 署名 直接跳 冒烟-创作入口，顶栏还写着 署名）。经首页点选进入则正常。cosmetic 但有误导性。
1. **runs-list 接口 + 页面刷新重挂载 + 启动清扫孤儿 run**（路由为 hash 模式，F5 本身能回到页面——2026-10-25 实测修正；真正的问题是 run 状态只在内存，刷新后 run 卡消失，gated run 无法批准；服务器重启后卡 running 的 run 同样无法处理）
2. 回滚时 save 再抛异常 → 编辑卡 applying 直至重启；回滚前瞬态窗口可见 gated 内容（无事务，write-then-rewrite）
3. `pipeline_import.prepare()` 契约警告被丢弃（审计线索缺口）；SSE failed 事件 `stage:""` 哨兵值未声明
4. chain_repair 修复轮尾指令是 agent 纪律措辞，真模型可能先跑门再等结果烧掉最后一轮（失败安全，可重试）
5. 四卡退役半程：UI 已切管线产物，冻结区 chat 工具仍写 canvas meta（留待清扫）；studioStore 死导出
6. 同项目换小说重跑保留旧剧本/角色卡（merge 语义正确、UX 未定；DELETE 产物接口是手动出口）
7. 小项：retry 按钮无 in-flight 防抖、非 script apply 无体检门（可加 UI 提示）、`_sessions` 无上限、桥接 art 行文本未截断、重复 import 期 `_RouterLLM()` 实例

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
