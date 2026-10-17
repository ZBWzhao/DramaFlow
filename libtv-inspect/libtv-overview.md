# liblib.tv（LibTV）站点内容速览

抓取时间：本次会话；方式：headless Chromium（Playwright 驱动，1440×900，zh-CN）完整渲染 SPA 后取 `document.body.innerText` + 截图。
纯 HTTP 抓取（web_fetch）只能拿到导航骨架，因为站点是 JS 渲染的单页应用。

## 站点定位
- 标题：`LibTV - 专业视频创作工具`（LiblibAI 出品的 AI 视频创作平台，宣称同时面向人与 Agent）
- 主色深色 UI，左侧固定导航 + 顶部促销条 + 右下浮动红包/客服挂件

## 左侧导航
- 首页 / 项目 / 资产 / 插件
- 创作：LibTV 画布、LibTV Agent（BETA）
- 社区：学院（大师开讲）、创作精选（全网爆款）、创作者挑战赛（王者大赛）
- 底部：版本更新记录 v1.5
- 顶部右侧：积分超市、开通会员（限时 45 折）、注册/登录

## 首页
- 顶部倒计时促销条：节后复工｜至尊会员上新：Seedance 2.5 720P 有参低至 0.19 元/秒、无参低至 0.46 元/秒（限时抢购）
- 首屏大按钮：新建画布创作
- 模型入口：Wan 3.0、Vidu Q4 Preview、Seedance 2.5
- 能力入口：视频生成、图片生成、音频生成、剧本生成、智能剪辑
- 特色功能：蜜雪冰城联名投稿活动（进行中）、3D-BOX、剧本原创与改编、导演执导、角色造型室
- 创作精选（带 tab + 搜索）：全部 / LibTV·「X小时之后」AI短片黑客松 / 全网爆款 / 王者无界剧场计划 / 精选画布 / 专业影视 / 短剧漫剧 / 商业广告 / 动漫游戏 / 教育生活 / TV工具箱
  - 作品卡示例：《长生无忌第20集》(小安Anleo)、《王者镇妖司》(粿条)、诡界游记第五集、纸人讨账（全网点赞30万+）、《挺好的》（全网点赞100万+）、宠物猫猫窝、storm2、猫咪vlog 五一游重庆、人民的狼叔（全网点赞100万+）、名次之外 等

## /activity 挑战赛（12 场）
进行中：LibTV × 蜜雪冰城「百变雪王杯」AI创意大赛；LibTV·「X小时之后」AI短片黑客松（500,000+ 现金奖池）；LibTV × 王者荣耀「王者无界剧场计划」（150 万现金奖池）
已颁奖：北电文学系「一场戏的诞生」（8 万+）、「AI重制那头牛」（5 万+）、游可爱·瓦漫节、Skill 导演大师赛（13 万现金+70 万积分）、高德×阿里云「扫街」（15 万）、LibTVC、优酷「精卫计划」、大乱斗 vol.1/vol.2、Seedance 2.0 百万 AI 影像狂飙季

## /academy 学院
- AI 大师公开课：LibTV × 抖音联合出品，首期解读抖音 AI 创作大赛金奖《合龙》，2XLabs 讲「叙事/美术/表演」三心法
- 学习广场 / 图文教程（按阅读时长 5–15min 列出），涉及：画布与节点、Seedance 2.5 与真人素材、故事板/剧情推演/多机位、全景与打光、视频高清与智能续写、音频切分与音色克隆、工作流与资产管理、摄像机控制、视频主体库与运镜预设、脚本节点批量分镜、3D 导演台/Blender 插件

## /plugin 插件（面向 Agent）
- 主张：LibTV Plugin，让你的 Agent 开始创作
- 支持客户端：OpenAI、Claude、Claude Code、OpenClaw、Hermes、CodeBuddy；Plugin / CLI 两种接入
- 安装提示词（站内原文）：`codex plugin marketplace add https://github.com/liblib-ai/marketplace.git --ref main`，然后 `codex plugin add libtv@libtv`，新建任务后完成 LibTV 账户授权
- 展示案例：一句中文需求（丝巾时装广告，画廊/庭院镜头+原创钢琴）→ Agent 建可编辑画布、规划镜头、生成素材

## 需要登录的页面
- /project（我的项目）：显示「暂无项目」+「登录失效，请重新登录」
- /assets（生成历史 / 个人资产库 / 可灵资产库）：微信一键登录 / 手机号 / 团队邮箱
- 未登录无法进入画布与 Agent 实际创作

## 404 的路由
/community、/discover、/explore、/learn、/square、/master、/shop、/member、/agent 均返回 404；/canvas 重定向回首页。

## 截图
- h1.png 首页首屏（促销条、新建画布、模型与能力入口、特色功能、创作精选）
- h2.png 首页创作精选作品流
- page-academy.png 学院页
- page-activity.png 挑战赛列表
