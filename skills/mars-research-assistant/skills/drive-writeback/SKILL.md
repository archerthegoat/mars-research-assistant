---
name: drive-writeback
description: 在独立明确确认后初始化 Google Drive 中的投研工作台，或向工作台写入交易想法、已确定方案、决策变更与复盘。用于用户要求初始化工作台、记录交易想法、同步交易方案或复盘时；初始化与写入必须分别提议、分别确认，互不授权。
---

# Drive 写入

同目录的 `capability.json` 定义本 Skill 的公开交付边界与离线验收场景。

```mars-skill-policy
{"delivery":"drive_writeback_with_local_receipt","forbidden_effects":["research","market_data","account_access","broker_write","trade_execution"]}
```

本 Skill 只公开两类能力：初始化投研工作台，以及写入交易想法、已确定方案、决策变更与复盘。
完整研报、估值 JSON、技术证据与 HTML 保留在本地，工作台只通过 artifact ID 或链接引用，
绝不把专题研究正文复制到 Drive。不要重新研究、取数、补全上游缺口、读取账户或执行交易。

每次提议或执行结果先写为活动工作目录 `mars-research/` 中唯一的
`YYYY-MM-DD-drive-writeback[-NN].md`；绝不覆盖已有文件，也不在安装目录保存状态。该回执不
等于 Drive 写入：只有本 Skill 的独立确认规则满足后才能执行对应的 Drive 变更。

## 初始化投研工作台

每次初始化都从 My Drive 根目录精确查找名为 `投研工作台` 的文件夹。读取本次操作实际返回的
Drive ID，并只在本次提议与执行中锁定；不在本地持久化 Drive ID，不写入 Skill 安装目录、
隐藏缓存或配置文件。

- 未找到根目录时，提议创建它。
- 找到多个同名根目录时停止，列出候选 Drive ID，请用户明确选择；不能猜测。

初始化提议必须展示拟创建项、My Drive ID、锁定的目标 Drive ID、唯一初始化 proposal ID 和
尚未执行状态。确认必须明确回指初始化 proposal ID 及目标 Drive ID；含糊确认、其他 proposal
ID 或其他目标均无效。创建后按 ID 读回并核对名称、MIME type 与 parent ID，再报告
`created / existing / failed / pending`。Drive 不可用、权限不足、创建失败或读回失败时保留
原因，不声称成功；结构完整时返回无变更成功，不创建重复对象。

只补缺项；不移动、不覆盖、不重命名、不删除任何已有对象，也不预先创建案例主文档。

## 写入交易想法、方案、决策变更与复盘

工作台用于跨设备记录交易想法与已确认交易方案，不是研究 Markdown 的备份库。一个标的/投资
案例一个主文档，固定四区：交易想法日志、当前有效交易方案、证据与决策变更记录、复盘记录。

- 每个方案使用稳定 `plan_id` 和递增 `revision`，记录父版本、触发证据和变更原因。两个
  设备基于同一父 revision 各自修改时保留两条 revision，生成冲突摘要，要求用户选择保留、
  合并或另起方案，不按时间戳静默覆盖。
- 每次写入先展示目标文档、拟写入区段、`plan_id` 和 base revision，得到用户明确确认后才
  执行；Drive 不可用时写入可见的本地 outbox（状态“待同步”），重试前重新核对父 revision，
  不后台上传。
- 写入前从 My Drive 精确解析并锁定本次操作使用的工作台 Drive ID。工作台尚未初始化时只
  展示初始化提议；初始化确认不授权任何写入，初始化成功后不得自动继续写入，须展示具有
  不同 proposal ID 的写入提议并取得第二次明确确认。
- 取得明确确认后，才使用可用的 Google Drive 工具执行；只更新本 Skill 明确拥有的区段，
  不静默替换整篇文档，不改写用户自有内容。写入后按 ID 读回核验并报告实际结果。Google
  Drive 不可用或写入失败时保留失败原因，不声称已写入。
- 包内 `scripts/drive_workbench.py` 提供该合同的本地模拟（manifest、提议/回执/读取、
  冲突与 outbox），用于离线验证；它对真实 Drive 没有任何副作用，真实写入仍需上述确认
  纪律并读回核验。
