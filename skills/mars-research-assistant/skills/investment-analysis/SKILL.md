---
name: investment-analysis
description: 在用户明确指定本地研究 artifact、Drive 文档或 case_id 后，做轻量增量投研讨论并输出讨论卡 Markdown + JSON 与方案 revision 提议。Use when the user asks for incremental investment discussion on named artifacts without rerunning deep research. 不广泛搜索，不重跑深度研究，不自动调用其他 skill。
---

# 投研分析

同目录的 `capability.json` 定义交付边界与离线验收场景。

```mars-skill-policy
{"delivery":"local_markdown_discussion_card","forbidden_effects":["broad_search","rerun_deep_research","auto_invoke_other_skills","account_access","broker_write","trade_execution","drive_write_without_confirmation","full_library_scan"]}
```

本 Skill 消费用户明确指定的已有研究输入，做轻量增量讨论，输出讨论卡 Markdown 与结构化
JSON（`scripts/render_discussion_card.py`，纯标准库、离线可复算）。

## 明确输入

- 必须接收用户明确指定的本地 artifact 路径、Drive 文档 ID 或已登记 `case_id` 之一。
- 用户未指定时，只查当前工作区 `mars-research/drive-workbench/manifest.json` 的已登记
  case，列出候选请用户选择；不扫描整个 Drive 或本地目录（不全库扫描）。
- 默认只增量处理用户提供的新材料（指定公告、财报、行情或新证据），不重跑深度研究、
  估值或技术图表。

## 讨论卡内容

讨论卡至少记录：输入声明、新证据（逐项来源与 `as_of`）、假设变化、论点状态
（`strengthened/unchanged/weakened/falsified`）、反方论证、待验证事项、置信度、
方案 revision 提议（如有）。无增量证据且无假设变化时，显式输出“维持原方案/暂无动作”，
论点状态必须为 `unchanged`。引用既有 artifact 时只读取其 identity/摘要字段，不重算、
不校验其内部数字。

## 方案 revision 与升级提议

- 方案调整只输出 revision 提议（`plan_id`、`base_revision`、变更摘要、触发证据），
  注明需经 Drive 工作台 `confirm` 才生效；本 Skill 不直接调用工作台、不写入 Drive。
- 写入 Drive 沿用 drive-writeback 的确认纪律：先展示目标与拟写入内容，用户明确确认
  后才写入。
- 深度研究或技术分析的升级提议只做文字建议，不执行；只有用户明确要求或预设升级
  条件触发时才提出。

## 边界

不广泛搜索、不自动调用其他 skill；不访问账户、持仓、券商或订单，不执行交易，不输出
交易指令词。本地 artifact 合同：`mars-research/` 目录下唯一文件名，排他创建，绝不覆盖。
