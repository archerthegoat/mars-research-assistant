---
name: deep-equity-research
description: 为单一、身份可唯一核验的上市公司交付九章承保研究（首次承保或财报更新），含财报质量 A–D、可复算估值、技术证据交集和仅多头、持仓无关的条件式交易方案。Use when the user explicitly requests deep research, valuation, earnings review, an investment memo, competitive analysis, or modelling; not for a quick company snapshot.
---

# 深度研究

同目录的 `capability.json` 定义交付、两种研究模式、估值与决策门合同及离线验收边界。

```mars-skill-policy
{"delivery":"local_underwriting_package","forbidden_effects":["account_access","broker_write","trade_execution","position_sizing","short_selling","drive_write_without_confirmation","silent_provider_fallback"]}
```

只处理一个发行人（A/H 对比为同一发行人两个 listing）。取数前核验公司名称、ticker、
交易所和发行人；不能唯一对应时要求澄清，不创建工件。市场范围按根入口的市场偏好解析；
裸代码歧义必须询问；纯字母 ticker 仅在美股为唯一已启用基础范围时解析为美股，否则必须询问。

输入契约：单个 1–5 位字母 token（大小写不敏感，如 `lite`/`LITE`）一律先视为 ticker
候选，经市场偏好解析；本 Skill 没有 `lite` 等深度档位或模式名，不得把此类 token 当作
模式开关。尚未配置市场偏好时，响应必须明确“市场可多选”并一次列出美股、港股、A 股、
A/H 对比四个可一次多选的选项，不得暗示单选；裸 ticker 可附快捷确认提示（如“美股时可
确认 LITE（NASDAQ）”）。已配置但美股不是唯一已启用基础范围时，只要求用户选择
市场/交易所（可补交易所后缀），不要求先补齐公司名+ticker+交易所；发行人身份核验在
市场确定后、取数前进行。

行情经显式 provider 适配器获取并记入来源账本，不静默 fallback；
事实优先监管披露、交易所和发行人 IR；搜索摘要不是证据。

## 研究模式与基线

- **首次承保**：无既有报告或模型时运行，重建至少三年年度与八个季度基线；不足时记录
  基线缺口，不伪造完整。
- **财报更新**：以变化为主线交付同样九章（预期差、分部/KPI、GAAP/Non-GAAP 桥接、
  现金流与营运资本、电话会、指引、模型与估值变动、对原方案影响）；无旧模型时自动降级
  为首次承保并注明。

## 交付包与九章

交付 `underwriting.md`、`underwriting-inputs.json`、`valuation.json`、
`technical-evidence.json`、`trade-plan.json`；`underwriting.html` 按需生成；
`trade-plan.html` 仅在技术质量门通过且价值/技术区间有交集时生成。每个 artifact 携带
`issuer_id`、`listing_id`、`case_id` 与版本信息；A/H 对比共用一个 `case_id`。

每次研究必须创建一个用户可辨认的唯一研究包目录，命名为
`{listing_id}-{YYYYMMDD}-deep-research-{initial|earnings-update}-{NN}`（交易所后缀和
文件名中的非法字符先规范化）。例如 `LITE-20260806-deep-research-initial-01/`；目录内
沿用上述标准 artifact 文件名。最终回复必须明确写出“产出状态：已完成”、报告名称和该目录，
不能只给出 `rerun`、`case_id` 或裸文件名。

九章固定：研究范围、预注册命题与交易结论；公司、业务模式与价值驱动；行业结构、竞争与
行业专属反证；管理层、治理与资本配置；财务、分部/KPI 与财报质量；预期差、催化剂、
基准率与跟踪清单；可复算估值与“现价定价了什么”；反方论证、事前风险预演与可证伪
条件；来源、数据对账、时间戳、假设与数据缺口。行业 KPI、反证和最低数据要求按
`reference/industry_registry.json` 中目标行业加载。

## 估值、财报质量与交易方案

- 估值一律由 `scripts/dcf.py` 运行并写入 `valuation.json`：三情景概率加权 DCF、反向
  DCF、PVGO、PE/正常化 EPS、EPV、EVA/剩余收益、适用时 SOTP 与可选蒙特卡洛；概率显式且
  合计为 1，终值三查留档；不适用或缺输入显式记录，语言模型不得心算或补造数字。
- PE 只能消费显式的 `forward_eps`、`normalized_eps` 或经复核的 `trailing_eps` 与情景
  `pe_multiple`；亏损/非正 EPS、一次性利润未剔除或缺少来源时标为不适用/条件性输出，不能
  把当前价格反推成目标倍数。PE 与驱动型 DCF 都必须通过各自质量门槛后，才有资格形成基本面
  目标；否则仅作为参考或留档。
- 旧三情景 DCF 为可审计 baseline（`model_role: "baseline"`），其手工给定的现金流
  路径不构成基本面目标。可选的通用驱动型 DCF（`models.dcf.driver_model`）按
  NOPAT = revenue × operating_margin × (1 − tax_rate)、FCF = NOPAT + D&A − capex − ΔNWC
  逐年推导现金流，高增长/过渡/稳定阶段写进显式驱动路径；通用质量门槛
  （usable/conditional/unreliable）决定是否有资格形成“基本面目标/定制 DCF 参考值”，
  未达 usable 时报告只能标注“条件性模型输出/估值模型待重建”，并优先并列 reverse
  DCF 的市场隐含条件。严禁为贴近市价反向调整 WACC、g、FCF 或概率。
- 财报质量由 `scripts/earnings_quality.py` 输出 A–D（应计、Beneish 适用性门、收入确认
  红旗、现金流、审计/治理），规则版本可复核；C/D 确定性否决多头 `entry_plan`，但不产生
  做空建议；数据不足时为暂定级。
- 交易方案由 `scripts/trade_plan.py` 按 `reference/preregistered_rules.json` 的版本化
  预注册规则生成：仅多头、持仓无关，默认 1–6 个月波段，动作仅允许 `entry_plan`、
  `target_plan`、`invalidation_plan`、`watch`。技术证据经 `technical-analysis` 的
  `evidence_id` 消费且不改写；证据失败、过期或价值/技术无交集时不输出价格区间，只输出
  观察/等待条件。
- 结论必须回答核心命题、市场隐含预期、分歧、证伪条件，以及“若今天是现金是否按本方案
  部署”；无独立观点或关键数据缺失时结论为“不产生方案”。

研究 artifact 默认写入活动工作目录 `mars-research/`，唯一文件名，绝不覆盖。写入 Drive
工作台需经 drive-writeback 单独提议与确认；本 Skill 不自动触发投研分析。
