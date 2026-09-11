# 复现交付规范（Supervisor 写作纪律）

> 目标：任何人全新安装本 skill、空白会话直接跑，都能产出结构自洽、语言一致、
> 语义不混淆的交付物。以下规则不需要记忆——它们是**状态编写与裁决时的纪律**，
> 生成器（`human_summary.py` v2+）对规范化的状态自动渲染出最佳摘要。

## 1. 结果包 metrics 写作规则（最重要）

- **`claim` 只用于论文真正声称过的数值**。对照性/诊断性数值（如"与某方程
  的偏差"）**不得**设 claim——它们是上下文指标，会自动进入"上下文指标"表。
- **有 `claim` 的指标必须给 `status`**（exact / within_tolerance / over_claim）：
  这是"已裁决"的标记；核心对照表只收"claim + status"齐全的行。
  写错了不会崩——生成器会把它隔离到上下文表并在判定说明中提示规则。
- **每个 metric 尽量带中文 label、`unit`、`uncertainty`（结果包级）**；
  无 label 的条目在上下文表按 metric key 显示（可容忍但不推荐）。

## 2. 需求 / 目标 / 假设关联必须按真实映射注册

- `requirement.goal_ids`、`goal.requirement_ids`、`goal.assumption_ids`、
  `assumption.affected_goal_ids`：**只填真实对应**，禁止批量占位全量。
  生成器（如 A2 合成演示徽章）以 `assumption.affected_goal_ids` 为权威链接，
  批量填充会导致徽章误标；正确的反查会兜底，但请从源头写对。
- `assumption.classification=A2_SCIENTIFIC_ASSUMPTION` 时，同时填
  `strict_status_effect`（DISQUALIFIES_PURE_STRICT = 合成替代 → 演示徽章；
  STRICT_WITH_ASSUMPTIONS = 边界注记 → 不触发徽章）。

## 3. GOAL_REVIEW 发现的【】分组标签

- rationale 首行建议带 `【标签】`，生成器按关键词分组（中英均可）：
  - **影响声称**：推翻/refut/exceed/over claim/not reproduced/claim contradict…
  - **论文内部不一致**：内部不一致/inconsist/mismatch/internal contradict/text vs fig…
  - **结构确认**：结构确认/confirm/verified/reproduc/consistent…
  - 其他 → "其他观察"（兜底，不报错）。
- 无标签不报错（落"其他观察"），但分组会粗；请在裁决时写标签。
- 每条 rationale 应**自含上下文**：引用其他编号（DEC-*/REQ-*）时带上含义，
  单条记录脱离报告也能独立读懂；decision_id 形态保持原生，勿重命名。
- **论文内部不一致 = 标注 + 详情成对（强制）**：凡归入"论文内部不一致"组的发现
  条目必须写明"矛盾双方 + 出处 + 独立验证 + 判定"四要素（如：图5 印刷方程
  vs 正文 p.4 引用 + 独立 OLS 重拟佐证 + 判定哪边是笔误），且 `affected_refs`
  至少包含一条被其标注的需求 id——该条的"涉及"清单会注在条目标题下。
  生成器硬校验（`SummaryConsistencyError`）：发现条无 affected_refs，或需求行
  出现「⚠️ 论文内部不一致」但表格外无对应解释，摘要生成会被**阻断**并点名
  缺的记录——此时补数据，不得改渲染器。

## 4. 中文交付物的目标标题

- zh 渲染的核心表分组标题优先读项目根 `goal-labels.zh.json`
  （`goal_id` → 中文标题，`_` 开头键为注释）。缺失回退到 goals 注册 title。
- en 渲染不受影响。该文件是项目数据，非 skill 必需。

## 5. 已注册状态文件不可重命名

- sources/evidence/requirements/goals/acceptance/runs/results/manifests/
  decisions 均为 exactly-once 注册语义：**注册后不改名、不删除重来**。
  需要修正时追加新记录并在 rationale 中说明（finalize 幂等可重放）。

## 6. 交付前自检清单（Supervisor 收尾前逐条过）

- [ ] 语言一致性：zh 交付物无英文标题残留；en 渲染无中文泄漏（guide/模板层已保证，
      检查的是证据文本与包 label 等数据层）。
- [ ] 无口语/非正式标签体进入正式交付物（如"人话解读"式标题）——信息以正式文体
      写入 rationale。
- [ ] 核心对照表无"claim=0 自设目标"类行（写作规则 §1 已防；生成器已隔离兜底）。
- [ ] ❌ 需求/发现的 rationale 中点明"不一致性质"：论文内部不一致 vs 复现偏差。
- [ ] ❌ 论文内部不一致：标注与表格外详述成对（含"涉及"需求清单），
      生成器 SummaryConsistencyError 出现即为数据缺 records，补上再交付。
- [ ] ❌ 发现条目中文一句话 + 出处（页码/图号）标注：zh 交付由
      `decision-labels.zh.json` 提供中文段落，出处写进 label（或 decisions 记录
      `page_refs`）；英文原文保留在 en 版与状态文件。
- [ ] 交付物路径与落点说明完整（本地/服务器/相对路径注明）。
- [ ] 服务器中间产物与本地同步后再交付。

## 7. 平台相关方法说明

- 图数字化中的 Windows OCR 路由（WinRT `Windows.Media.Ocr`）**仅在 Windows 可用**：
  是可选增强，不是必选。通用兜底 = PDF 矢量层 + 多档渲染 + 像素 ASCII 仲裁
  （见"图形/截图数据提取的方法原则"），跨平台成立。

## 8. 语言纪律（P22-C 增补）

- **展示型长文本跟随交付语言**：assumption.rationale、decision.rationale、
  requirement.statement/statement_zh 等会渲染进人读交付物的字段，zh 交付项目
  必须用中文撰写（或提供 statement_zh）。机器 ID 保持原生。
- **检查工具**：finalize 前跑
  `python scripts/check_state_language.py <workspace> --expect zh`
  扫描上述字段的语言一致性，违规清单逐条本地化后再收官。
- **GOAL_REVIEW / A2 假设建议提供 `title`**（人读短标题）；缺省时生成器
  取 rationale 首分句作标题，编号只作尾注（P22-A 语义前置）。
- **需求内容列**：zh 渲染优先 `statement_zh`（作者提供），其次
  `statement`，最后 evidence 摘句。

## 9. 一句话结论与覆盖矩阵（v3/P24-E 增补）

- 摘要以**一句话自然语言结论**开头：默认由状态派生（N 项声明中 X 成立/Y 不成立 + 每个 ❌ 需求一句 + 影响声称/内部不一致发现首条 + 合成演示声明）；
  允许项目根 `summary-override.json` 提供 `conclusion_zh`/`conclusion_en` 整句覆盖（作者写、生成器引用，缺失则用派生默认）。
- **产出覆盖矩阵**每行 = 需求（id+statement_zh ≤60 字）| 结果（含 A2 徽章语义）| 复现说明（closure 理由首句 ≤80 字）；完整理由在附录 A 逐条全文。
- 需求 statement_zh 写作模板："声称请含数值/公式与出处页码"（如 "校准方程 y=0.0927x−1.1268（正文 §3.5）"），使矩阵声称列自带对照感。

## 10. 未复现需求的性质角标（v4/C）

- 需求行出现「⚠️ 论文内部不一致」角标的两种触发：
  1. 归入"论文内部不一致"组的发现其 affected_refs 命中该需求；
  2. 需求 outcome = NOT_REPRODUCED 且其**裁决理由（closure rationale）声明该矛盾属论文自身**（含"论文内部不一致 / 自身公布数据 / 自相矛盾"字样）。
- 写作约束：凡 ❌ 且根因是"论文声称与其自身公布数据/另一处表述矛盾"（而非复现方法问题），closure rationale 必须写入上述字样，使角标与附录 A 一致；数据不可得类 ❌/INCONCLUSIVE 不要写这些字样。

## 11. 发现条目的中文化与出处标注（v4.1/R4）

- zh 交付中「发现与确认」每条以**中文一句一段**呈现：来自项目根 `decision-labels.zh.json`
  （decision_id → 中文段落，与 goal-labels.zh.json 同模式，非 skill 必需）；缺失时
  回退原样英文加本地化注记（可容忍但不推荐）。英文原文不动——它留在状态文件，
  也完整出现在 en 交付物中。
- **每条发现必须自报出处**：优先在 decision 记录写可选 `page_refs`（如
  ["p.4","Fig. 5"]，schema 允许附加字段）；否则生成器从中文 label（优先）与
  rationale 中提取页码/图号（`p.4` / `第4页` / `Fig. 5` / `图5` / `Table 1` / `表1`），
  按（类型, 编号）去重——`图5` 与 `Fig. 5` 同一引用保留先出现形式。
- 写作约束：中文 label 应写明"矛盾出现在哪一页/哪张图"（如「正文 p.4 引用 …
  与 图5 印刷方程…」）；§3 的矛盾四要素与 affected_refs 配对规则不变，
  涉及清单并入同一出处括号。
- 出处是发现级（per-finding）标注，不是需求级——矩阵行的角标仍按 §10 触发。
