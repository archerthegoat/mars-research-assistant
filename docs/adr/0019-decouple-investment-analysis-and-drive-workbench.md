# 将投研分析与深度研究解耦，并以 Drive 作为确认式工作台

v1.0.3 新增独立的“投研分析”（`investment-analysis`）skill。它只消费用户明确指定的本地 artifact、Drive 文档或 `case_id`，默认输出轻量讨论卡 Markdown + JSON 和交易方案 revision；它不广泛搜索、不重跑深度研究，也不自动调用其他 skill。Drive 负责跨设备记录想法、当前方案、证据/决策变更和复盘，不是研究 Markdown 备份库。

## Considered Options

- 将投研讨论做成深度研究的内部 mode：调用入口较少，但会把增量讨论、方案状态和数据重跑逻辑塞入重型研究 skill。
- 让 Drive 自动镜像全部本地研究文件：跨设备看似方便，却会制造同步冲突、覆盖风险和不可见外部副作用。
- 独立轻量分析 skill + 明确 artifact 输入 + 一个案例一个 Drive 主文档 + 用户确认写入：职责清晰，跨设备可接续，且可在本地 fixture 中验证。

## Consequences

深度研究完成后只能提示用户可以继续分析，不能自动触发；投研分析没有明确输入时不得全库扫描。每个方案使用稳定 `plan_id` 和追加式 `revision`，多设备父版本冲突必须显式化；Drive 写入前必须确认，写入失败进入可见 outbox，重试前再次核对父 revision。市场偏好默认按设备保存，只有用户明确导入/同步时才跟随 Drive。

Markdown 是规范源稿，HTML 是按需阅读视图；完整研报和 JSON 通过 artifact ID 或链接引用，而不是复制进 Drive 主文档。账户、持仓、券商、订单和后台上传均不属于这两个 skill 的职责。
