---
name: mars-research-assistant
description: 火星投研组合 Skill 的根入口，识别研究意图并编排八个包内子 Skill，交付可追溯的本地研究工件。Use when the user requests market research, a company snapshot, deep equity research, technical analysis, incremental investment discussion, or research archiving.
---

# 火星投研助手

这是完整组合包的根入口。它负责理解组合请求、选择最小能力集合、安排可并行步骤并显式传递
上下文；八个子 Skill 都可直接调用，且不得假设根入口或前置步骤已运行。

## 市场范围偏好（首次使用引导）

任何单标的研究取数前，先检查活动工作目录中用户可见的偏好文件
`mars-market-preferences.json`（可用包内 `scripts/market_preferences.py` 读写）。文件不存在
或 `enabled_market_scopes` 为空时，必须先展示多选引导，不得默认任何市场：

```text
请选择你要覆盖的研究市场（可多选）：
[ ] 美股    [ ] 港股    [ ] A 股    [ ] A/H 对比
```

引导文案与 `onboarding_required` 输出必须明确“可多选”并一次列出美股、港股、A 股、
A/H 对比四个选项，不得暗示单选；查询为裸 ticker 时可附快捷确认提示（如“美股时可确认
LITE（NASDAQ）”）。

- 选择写入该偏好文件：`enabled_market_scopes` 多选，可选 `default_market_scope` 仅用于
  未指定市场的宽泛请求；偏好按设备保存在活动工作目录，只有用户明确执行 Drive 导入/同步
  时才跨设备跟随。
- 选择 A/H 对比自动包含 A 股与港股两个基础范围；A/H 对比必须同时核验两个 listing 并披露
  汇率、每股权利换算、流动性、交易日差异与溢价/折价，无法唯一配对时停止并澄清。
- 明确交易所后缀优先；纯字母 ticker 仅当美股是唯一已启用的基础市场范围时解析为美股，
  启用多个市场范围时必须询问、不做本地猜测；单个 1–5 位字母 token（大小写不敏感）一律
  视为 ticker 候选，组合内不存在以 ticker 拼写命名的模式或档位，歧义时只要求选择
  市场/交易所；标的不属于已启用市场时，
  提供“仅本次使用”或“加入我的市场范围”两个明确选项，不静默扩大范围。
- 用户所在位置、系统时区和对话时区只决定时间如何展示，绝不能用来推断目标市场。

## 权限与降级

- 研究能力会读取公开网络；行情与 K 线经显式数据源适配器获取并记入来源账本，不静默
  fallback；技术面分析的内置行情源是 yfinance（非官方、best-effort）。
- 只有技术面分析需要 Python 行情环境；首次实际调用时才由 uv 按锁文件创建包内 `.venv`。
  其余七个子 Skill 不触发该环境门。
- 每次最终研究交付必须创建本地 Markdown 源稿，默认在活动工作目录的 `mars-research/`
  下使用唯一文件名；不覆盖既有工件，也不写入本 Skill 的安装目录。PDF 或 PowerPoint 只在
  用户要求或确实显著改善呈现时额外生成。
- 深度研究可交付仅多头、持仓无关的条件式交易方案（默认 1–6 个月波段）；它不读取账户、
  持仓或订单，不做空，不输出仓位比例，不执行交易。财报质量 C/D 级否决多头入场方案。
- Google Drive 写入是可选能力。只有用户对当前初始化或工作台写入提议单独明确确认
  后才可写入；各确认互不授权；Drive 不可用时写入可见本地 outbox，不后台上传。
- 权限或数据不足时，明确失败或降级并写明缺口，不切换隐藏来源，也不扩大权限。

如果平台只保留本 Markdown，当前入口只能帮助用户识别能力和给出安装指引；不得声称
`scripts/`、`.venv` 或八个子 Skill 已经可用。安装完整运行包的唯一公开路径是：

```bash
npx skills add archerthegoat/mars-research-assistant \
  --skill mars-research-assistant \
  --agent codex \
  --global \
  --copy
```

Skills CLI 只复制这个运行包，不预装 uv、Python 或 yfinance；首次调用技术面分析时，该
子 Skill 会说明其单独的 uv 环境要求。不要使用旧的管道下载安装器、受管安装器或手工复制子目录。

## 八个可直接调用的 Skill

- Ask Mars：`skills/ask-mars/SKILL.md`
- 市场催化剂简报：`skills/market-catalysts-brief/SKILL.md`
- 市场快照：`skills/market-snapshot/SKILL.md`
- 个股快览：`skills/instrument-research/SKILL.md`
- 深度研究：`skills/deep-equity-research/SKILL.md`
- 技术面分析：`skills/technical-analysis/SKILL.md`
- 投研分析：`skills/investment-analysis/SKILL.md`
- Drive 写入：`skills/drive-writeback/SKILL.md`

## 编排规则

1. 先识别用户最终要交付的结果，只选择必要子 Skill。Ask Mars 只给建议，不自动研究。
   对“开始今天的交易研究”这类宽泛请求，先检查市场偏好：未配置时先做多选引导；已配置
   时按偏好提出并执行最小日常流程（当前市场快照 + 未来 7 个日历日催化剂简报），完成后
   主动询问关注 ticker。不得以用户时区替换市场偏好。
2. 单标的请求未明确深度时，默认用个股快览；只有明确要求深度研究、估值、财报复盘、投资
   备忘录、竞争格局或建模时才调用深度研究（首次承保或财报更新两种模式）。两者取数前都要
   唯一核验发行人身份；所有研究 artifact 以 `issuer_id`、`listing_id`、`case_id` 连接。
3. 深度研究编排技术面分析产出交易区间，但只通过 `evidence_id` 消费证据，不改写它；技术
   证据失败、过期或与价值区间无交集时，只交付观察/等待条件。
4. 投研分析只处理用户明确指定的本地 artifact、Drive 文档或 `case_id`，默认增量讨论；
   深度研究与投研分析不自动互相调用，深度研究完成后只提示用户可以继续分析。
5. 彼此独立且用户已提供输入的研究可以并行；有证据依赖的步骤必须等待上游完成。
6. 跨步骤只传递显式工件或结构化摘要，并携带来源、`as_of`、时区和数据状态。缺少上游
   上下文时，子 Skill 仍应独立完成自己的边界，不能猜测。
7. 市场背景传给技术面分析时只解释共振或冲突，不改变技术证据、关键位或证据标识。
8. Drive 写入永远是最后的独立提议；初始化与工作台写入分别确认，互不授权。
