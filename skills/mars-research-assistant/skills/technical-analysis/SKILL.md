---
name: technical-analysis
description: 针对单一标的生成基于已完成日线的中文技术面分析，持久交付 Markdown 与 evidence JSON，并用临时 Lightweight Charts HTML 展示同一证据。Use when the user asks for daily trend, moving averages, auditable key levels, conditional technical scenarios, or a local interactive chart; not for fundamentals or trading execution.
---

# 技术面分析

同目录的 `capability.json` 定义公开交付、数据质量门和离线验收场景。

```mars-skill-policy
{"delivery":"technical_evidence_package","forbidden_effects":["fundamentals","industry_analysis","account_access","broker_write","drive_write","persistent_state","provider_fallback","browser_acceptance"]}
```

只分析一个明确标的的 `1D` 已完成日线。不带市场后缀的 symbol 默认按美股候选解释；明确的
交易所或 yfinance 后缀覆盖默认值，用户时区不得改写 symbol。默认在活动工作目录的
`mars-research/` 选择一个尚不存在的唯一 `output_dir`；用户显式指定其他本地目录时使用该目录，
绝不覆盖既有工件或写入安装目录。

## 工作流

1. 仅在首次实际技术面请求时，从本 Skill 目录运行 `bash scripts/ensure_yfinance_environment.sh`。
   它需要完整 Mars 运行包和已可用的 uv，按包根 `pyproject.toml`/`uv.lock` 创建包内 `.venv`；
   其余六个 Skill 不得触发此环境门。没有完整包或 uv 时停止并说明缺口，不临时拼装未锁定环境。
2. 用包内 Python 调用 `scripts/analyze_with_yfinance.py --symbol SYMBOL --output-dir OUTPUT_DIR`；若要让证据回绑既有案例，同时传入 `--issuer-id`、`--listing-id`、`--case-id` 三个身份参数（必须成组，listing_id 必须等于 symbol）。
   不绕开该脚本另写下载或分析逻辑。
3. 合格时原子交付 `analysis.md` 和 `evidence.json`，共享 `evidence_id`；另在系统临时目录
   创建 `chart.html`，返回其路径和过期时间。聊天仅摘要结果，本地工件承载完整文字。

唯一内置 OHLCV 来源是 yfinance，必须标为非官方、best-effort；不读取 API key，不能切换
Provider。质量门失败后只交付含数据状态和数据缺口的 `analysis.md`，不生成证据 JSON、趋势、
关键位、情景或图表。

`analysis.md` 只能解释 `evidence.json` 中的数值。不要加入基本面、行业、账户、仓位、订单、
交易执行或 Drive 写入；不要把图表调用变成浏览器验收任务。完整质量门、确定性指标、图表、
市场背景和安全边界见 [REFERENCE.md](REFERENCE.md)。
