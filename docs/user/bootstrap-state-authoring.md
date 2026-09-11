# Bootstrap 状态编写指南(运行时注册操作)

> 本文档记录把一篇论文的复现计划写入项目状态(runtime registration)的**正确顺序**与**实测踩坑**。
> 面向 Supervisor 会话与写扩展的 agent;所有操作均为 Python API(见 `planning/`、`research/`、`analysis/` 模块),
> 只有项目初始化有 CLI(`scripts/reproduce.py init`)。

## 1. 注册顺序(顺序敏感,勿乱)

运行时对"引用完整性"严格检查,**引用目标的记录必须先注册**:

```
1. 来源(sources)            register_source            —— 证据/清单/需求都引用它
2. 证据(evidence)           register_evidence          —— 引用来源
3. 库存条目(inventory)      register_inventory_item    —— 引用来源;linked_inventory_ids 指向的条目须先注册
4. 需求(requirements)       register_requirement       —— 引用已注册的 inventory_items
5. 目标(goals)              register_goal              —— 引用已注册的 requirements(requirement_ids)
6. 验收/统计设计/协议/闭环   register_acceptance / register_statistical_design /
                            register_analysis_record / register_closure_contract
7. 计划 + 冻结              build_plan_v1(root) → freeze_plan(root, plan)
```

**关键(实测验证,2026-09-02):注册顺序与条目 `requirement_ids` 的规则**

1. **items 必须先于 requirements 注册**:`register_requirement` 强制校验其 `inventory_items`
   已注册,否则抛 `UnresolvedItemReferenceError`(源码 docstring:"the authoring order is items
   first, then the requirements that map them")——这是上游一贯行为,不是版本变化。
2. **形式条目(formal_report=true)注册时必须带 `requirement_ids`**:这是审计映射的
   唯一来源;不带 → 条目 `UNMAPPED`,完整度审计(R-AUD-U1)失败、无法冻结。
   非形式条目(探索性记录)不受此约束。
3. **条目可先带 `requirement_ids` 引用尚未注册的需求**:注册时文件快照为 `AMBIGUOUS`,
   **无需删除重来**——完整度审计/冻结对映射做**实时重算**,需求落地后条目自动成为
   `MAPPED`(实测:item 先行 AMBIGUOUS → 注册 requirement → audit PASS/freeze_eligible=True)。
   注册顺序因此为:items(带 requirement_ids)→ requirements(引用 items)→ 其余。

## 2. 冻结前置条件(freeze_plan 的硬门槛)

- 项目阶段已到 `REPRODUCTION_INVENTORY`(主线上);
- 至少 1 条正式报告(fomral_report=true)条目已注册;
- 完整度审计 PASS(100% 形式条目 MAPPED、无 AMBIGUOUS);
- 计划 = 注册状态的确定性派生 `build_plan_v1(root)`(内容不得手工漂移);
- 每个目标引用的 acceptance / analysis_protocol / closure_contract 全部已注册;
- 每个 acceptance 的 `statistical_design_ref` 已注册;
- 每个目标至少被 1 个需求引用(无孤儿目标);需求与目标的 `goal_ids`/`requirement_ids`
  双向引用一致;
- 硬门依赖无环(Kahn 检查)。

## 3. 实测踩坑速查(全部真实发生过)

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | `SchemaValidationError: goal_ids [] should be non-empty` | requirement.goal_ids 不能为空数组 | 注册需求时就填好目标 id(可先于目标注册) |
| 2 | `goal.outputs` 校验失败 | 顶层 outputs 必须是**对象数组**,字符串不行 | 写 `{"artifact": "...", "format": "..."}` |
| 3 | `statistical-design.metrics` 校验失败 | metrics 元素必须是**字符串** | 写 `["slope ~ 0.0927 mg/L per mV"]` |
| 4 | `analysis.methods` 校验失败 | methods 元素必须是**对象** | 写 `{"method": "...", "goal": "..."}` |
| 5 | 条目 `UNMAPPED` 导致审计 FAIL | **形式条目未带 requirement_ids**(最常见);注册顺序错误(items 晚于 requirements) | 见 §1:形式条目必带 requirement_ids;items 先行(快照 AMBIGUOUS 无需删除,审计实时重算) |
| 6 | `project.yaml` 解析失败 "Expecting value" | project.yaml **内容是 JSON**(尽管扩展名 .yaml);用 yaml 库重写会损坏 | 用 `json.load/dump` 读写 |
| 7 | 报告显示"运行 0 成功" | run 的 `lifecycle_state` 须为 `CLOSED`、`scientific_review` 为 `PASS` 才算成功;`RESULT_AVAILABLE`/`UNREVIEWED` 计为未解决 | 注册 run 时直接给 CLOSED+PASS(或走生命周期迁移) |
| 8 | `result.input_artifact_ids` 必须非空 | 结果记录必须引用 ≥1 个已注册 artifact manifest | 先 `ArtifactRegistry(root/"manifests").register(...)`,再注册结果 |
| 11 | 摘要"问题"区段出现"见 OUTCOMES[...]" | 治理/裁决类 decision(REQUIREMENT_CLOSURE 等)被 human_summary 当"论文问题"展示;且 rationale 未展开 | **论文内部不一致/发现用 `GOAL_REVIEW` 记录**;REQUIREMENT_CLOSURE 只做裁决,摘要按 decision_type 过滤(仅展示 GOAL_REVIEW 类) |
| 9 | `AssumptionAsEvidenceError: ASSUMPTION- 前缀` | 假设被注册为 evidence——AC-01 追溯链要求每个 evidence claim 到达 Analysis→Run→Artifact,假设(定义上无 run)永远无法满足,最终会被 finalize 拦截并删除,丢失审计记录(v3 实测) | 假设注册到 `assumptions/`(schema `assumption.schema.json`),不要用 `register_evidence` |
| 10 | `SchemaValidationError` 只有计数 | 旧版错误消息只报"2 schema validation error(s)"不报字段——现已携带完整字段详情(如 `goal_ids: [] should be non-empty`) | 直接读异常消息即可,无需再写 jsonschema 调试脚本 |
| 11 | 报告后发现问题需修正并重跑 finalize | `finalize_project` **无进入相位门槛**(源码不检查 project_phase),且以 `project.finalized:<project_id>` 幂等键记录事件(v5 实证:同一项目连跑 4 次 finalize 只产生 1 条 completed 事件) | 修正产物(报告/结果包/需求 outcome)后**直接重跑**即可;手工把 project.yaml phase 改回 `REPORTING` 仅出于相位语义整洁,**非必需** |
| 12 | 结果包文件名出现 `GOAL-GOAL-*.json` 双前缀 | 包路径拼写时 goal_id 本身已带 `GOAL-` 前缀(如 `GOAL-CAL-FIG5`),生成 `GOAL-{goal_id}.json` 即得双前缀;reader 只按 `GOAL-*.json` glob + 包内 `goal_id` 字段识别(v5 实证双前缀不影响摘要/审计),但命名易歧义 | 约定文件名 = `GOAL-<goal_id>.json`(单前缀);若已生成双前缀文件可留用,不必强改 |

## 3b. 执行脚本通用纪律(v5 实测,跨项目适用)

| 纪律 | 反例(v5 实证) | 做法 |
|---|---|---|
| 数值字段按**命名键**取值,勿依赖 dict/元组顺序 | `ols()` 返回 `dict(slope, intercept, r2)`,按 `b0, b1 = values()` 解包导致斜率/截距互换(服务器产物核对时才发现) | `b1, b0 = o["slope"], o["intercept"]` |
| 确定性复现的 rng 必须在**循环外单例创建** | 列表推导内每次迭代 `np.random.default_rng(SEED)` → 27 个样本噪声全同,确定性检查 maxdiff=14.5 暴露 | `rng = default_rng(SEED)` 放循环外,重建校验用同源新实例 |

## 4. run 状态机(影响报告统计)

`runs/` 下的 Run 记录按 `lifecycle_state` 分类(见 `reporting.audit.run_status`):

- `CLOSED` + `scientific_review=PASS` → **SUCCEEDED**(计入"成功")
- `CANCELLED` / `INVALIDATED` 或 review=FAIL → **FAILED**
- 其余(含 `RESULT_AVAILABLE`、`ANALYZING`、`UNREVIEWED`)→ **UNRESOLVED**

注册结果包(analysis results)时:结果记录需要 `result_id`、`run_ref`(指向 CLOSED 的 run)、
`protocol_version`(冻结后为 `v1`)、`input_artifact_ids`(非空)。

**run/result 必填字段(单源:schema 冻结,不在此复制定义)**

| 记录 | 必填字段(以 `schemas/run.schema.json` / `ResultRecord.__post_init__` 为准) |
|---|---|
| Run | `run_id, goal_id, run_type, lifecycle_state, goal_version`(后三者取冻结枚举,见 §4 状态机) |
| Result | `result_id, analysis_id, protocol_version, run_ref, input_artifact_ids, primary_or_exploratory` |

**工件引用规则(审计解析,条件性)**:
- `result.input_artifact_ids`:**必填且非空**,每一项须在 `manifests/` 注册(否则注册即拒绝)
- `result.output_artifact_ids` **与 `run.artifacts` 非必填**,但**若填写**,每一项须在 `manifests/`
  注册——否则审计包验证报 `UNRESOLVED_RUN_ARTIFACT`(v4 实测:run 引用的 `result-GOAL-*`
  未注册 manifest 导致 AC-02 失败,补 9 个 manifest 后 PASS)

## 5. 结果包 metrics 规范(人读摘要数据源)

`reporting.human_summary` 的"声称值 vs 复现值"表格读取每个运行结果包(JSON)的 `metrics` 数组:

```json
"metrics": [
  {"metric": "calibration_slope", "label": "校准方程斜率",
   "value": 0.0928, "claim": 0.0927, "unit": "mg/L per mV"}
]
```

- `value` = 复现值;`claim` = 论文声称值(可缺省);`label` 可选(展示名);`unit` 可选。
- **没有 metrics 的结果包**不会被解析,生成器回退到展示 `finding` 文本(见说明)——不要为此写正则解析。
- 写执行脚本时把关键数值放进 metrics,人读摘要就会自动产出对照表。

**`status` 语义(判定列图标;写错会被渲染侧降级并告警,不会静默):**

| status | 含义 | 图标 | 判定规则 |
|---|---|---|---|
| `exact` | 精确复现 | ✅ | 偏差 ≤ 声称值有效数字的末位(如 0.092805 vs 0.0927 属 exact,非 within_tolerance) |
| `within_tolerance` | 在冻结验收带内、但偏离论文声称 | ⚠️ | 例:16.99% vs <15% 声称,验收带 ≤17% |
| `over_claim` | 超出验收带 | ❌ | 例:实测值超出冻结验收带 |

摘要在结论行自动统计 ⚠️/❌ 行数(如"复现 8(1 项容差内超差)"),并展示各结果包的 `uncertainty` 说明。

## 6. 审计链路 AC-01 的隐式链接(新会话实测盲区)

`validate_package` 的追溯链(claim → Analysis → Run → Artifact/Evidence)依赖两条**非 schema 必填、但链上必需**的链接:

- **acceptance 记录的 `evidence_refs`**(hop 2):验收标准必须通过 evidence_refs 引用支持它的证据记录,否则 claim → acceptance 断链
- **result 记录的 `requirement_refs`**(hop 4):分析结果必须通过 requirement_refs 引用它支持的需求,否则 requirement → result 断链

注册时留空**不会报错**,直到 `validate_package`/`finalize_project` 报 `TRACE_INCOMPLETE`。注册 acceptance/result 时就把这两个数组填上,避免后期"删记录重注册"(immutable)。

**语义边界(非仅命名约定)**:任何"假设/前提"类 claim——无论 claim_id 是否带 `ASSUMPTION-`
前缀——都必须走 `assumptions/` 记录(schema `assumption.schema.json`),**不得注册为 evidence**:
假设定义上无 run 支撑,AC-01 追溯链(claim → Analysis → Run → Artifact)永远无法满足。
`ASSUMPTION-` 前缀只是运行时用于拦截最常见误用的识别手段;语义规则独立于前缀。

## 7. human summary 的数据源位置与语义

- 对照表读 **`runs/<run_id>/GOAL-*.json`**(按 goal_id 匹配的子目录结构),不是 `analysis/results/`——执行结果包要放进 runs/ 下的运行子目录,摘要才会出现对照表
- "发现论文的问题"区段读 `decisions/` 目录;**没有 decision 记录时摘要显示"未登记问题发现"**(不是"未发现不一致")
- 判定列三态:metric 可带 `"status": "exact" | "within_tolerance" | "over_claim"` → ✅/⚠️/❌;容差内超差(如 16.99% vs <15% 声称)应标 `within_tolerance`,避免 ✅ 误读

## 8. 报告时效(完成门检查项)

`finalize_project` 会校验 `reports/reproduction-report.json` 的 `generated_at` **不早于最后一条需求裁决事件**——报告必须在全部裁决之后渲染,否则拒绝 COMPLETED。流程顺序:裁决全部需求 → 设置 outcome → 渲染报告 → finalize。

**重跑**:发现产物需修正时,先修正来源文件(runs 结果包/compute output 等)与报告再重跑 `finalize_project`——无进入相位门槛且事件幂等(见 §3 坑 11),COMPLETED 状态可直接覆盖为新的 COMPLETED。

## 9. 同一论文多次独立复现时的交叉核对(建议)

同一论文被多次独立复现(v1/v2/v3 场景)时,单项目 skill 无法自动跨项目对比,但建议:

- 后序复现的 finding/decision 中**显式引用前序复现的结果**(如"DEC-004:行 4/5 读数与 v1/v2 不同,像素证据支持 v3")
- 数字化读数等**存在残余不确定性的数值**,在结果包 `uncertainty` 与 CSV 注释中标注(人读摘要 G3 会自动展示)
- 跨运行分歧无法自动裁决时,记录为 decision 并标注"残余不确定性",由 Supervisor/用户决定是否做第四次独立点读

## 10. 图形/截图数据提取的方法原则(适用于含图提取的复现项目)

从论文图/截图中提取数据(数字化)是复现项目常见的最大时间消耗(v1–v4 同一论文四代
各 8–15 轮调试)。以下**方法原则**有通用依据(图像处理/统计事实),可迁移到任何图提取任务;
原则不含任何项目特定坐标/阈值/模板尺寸——这些必须留在项目级文档,不得进入本 skill。

| 原则 | 通用依据 | 用法 |
|---|---|---|
| 像素直读是最终裁判 | 分辨率足够时,像素是唯一 ground truth;任何算法/模板/OCR 的分歧以像素级直读裁决 | 分歧时输出像素 ASCII/裁剪图供裁决,并留档(如 decision) |
| 确定性边界优先于启发式边界 | 完全空白列(零列切分)是确定性边界;谷切分依赖字形内部谷形态(字体/渲染相关),属启发式 | 首选零列切分;谷切分仅作兜底,且谷的判定参数须按图标定 |
| 代表/中位数模板优于均值模板 | 均值对离群样本(破损字形/噪点)敏感,统计通用事实 | 模板库构建:自洽代表(同字互相关最高者)或中位数,而非逐样本均值 |
| 拓扑特征(孔洞数)在有损/抗锯齿渲染下不可靠 | JPEG/抗锯齿破坏像素拓扑("环"漏气),图像处理通用事实 | 用孔洞数前先验证该字体/渲染下孔洞是否闭合;不可靠则放弃该判据 |
| 模板/NCC 分数只作候选项,不作裁决 | 相似字形(0/6/8/9 家族)的 NCC 区分度可能不足,分数阈值是项目相关参数 | 低置信候选项输出到审计(分数+像素),由裁决层(Supervisor 或人工)决定 |
| 数字化不确定度必须进入结果包 `uncertainty` | 人读摘要(G3)自动展示,审计留痕 | 不确定的数字(如第 4-5 位)在 finding/uncertainty 如实标注 |
| **像素↔PDF 坐标映射先验证再写码** | pymupdf clip 渲染的行原点随页面坐标系/版本而异;凭公式假设会整体错位(v5 实测反式相差 66 pt,窗口切片空/倒置) | 写任何换算公式前,用已知锚点数值验证——如取一个词 bbox(已知 pdf 坐标)做 clip 渲染,比较墨迹行落在哪个公式的预测上;结论记入项目文档 |

**跨复现原则**:同一论文多次复现时,后序复现的裁决须引用前序结果交叉核对(见 §9);
像素证据可推翻前序记录(如 v4 DEC-005 以像素修正 v1–v3 的行 9 时间读数),也可支持
前序结论(v1–v4 对截距不一致的独立确认)。

## 11. 阶段推进 API(advance_project_phase)

**阶段推进是运行时操作,有注册级 API,不要手写**:把 `project.yaml` 的 `project_phase`
改掉再自己造事件(旧实操脚本样式)会踩确定性 id/幂等键约定——这是首次验收跑分
(T2)暴露的头号断点。正解是 `planning.phase.advance_project_phase`:

```python
from scientific_reproduction.core.rules.lifecycle import ProjectPhase
from scientific_reproduction.planning.phase import advance_project_phase

advance_project_phase(
    root, ProjectPhase.SOURCE_ACQUISITION,
    actor="supervisor", at="2026-09-05T04:30:00Z",
    reason="论文取证完成",   # reason 可选
)
```

**契约**:

- 签名:`advance_project_phase(root, to_phase, *, actor, at, reason="")`
  (`root`:工作区根;`to_phase` 接受 `ProjectPhase` 枚举或其字符串值;
  `at` 为注入时间戳——本运行时不用墙钟,确定性优先)。
- **规则门**:目标必须是主线上的合法后继(非法组合如 `INITIALIZING→PLAN_FROZEN`)
  → 抛 `IllegalTransitionError`,**任何写入发生之前**。
- **恰好一次**:`project.yaml` 原子重写仅在相位实际移动时发生;事件
  `project.phase.<from>.<to>` 带确定性 id(`generate_id("event", key, project_id)`)
  并以同一个 key 作为幂等键追加——重复提交只回放、不重复。
- **崩溃窗口可恢复**:project.yaml 写入与事件追加是两次独立原子操作,之间崩溃会留下
  "相位已推进、事件缺失"。恢复路径按幂等 claim 解析**原始**迁移 key 重补该事件
  (绝不伪造 `from==to` 的合成事件、绝不重写相位、绝不重复)。唯一需要的人工修复
  = 再调一次该 API(以当前相位为目标)。
- **边界**:未初始化 root → `ProjectNotInitializedError`;未知相位字符串 → `ValueError`
  (附带合法列表);参数类型错误 → `TypeError`。

**主线顺序**(与 `core.rules.lifecycle` 一致;COMPLETED 由 `finalize_project` 专属持有,
不要通过本 API 推进):

```
INITIALIZING → SOURCE_ACQUISITION → REPRODUCTION_INVENTORY → PLANNING → PLAN_AUDIT
→ PLAN_FROZEN → EXECUTING → REPORTING → COMPLETED
```

**init → PLAN_FROZEN 最小端到端示例**(各注册步骤的入参细节见 §1–§2,此处只给骨架;
执行阶段后的 REPORTING 推进由 Supervisor 在产出三件套后调用):

```python
import pathlib
from scientific_reproduction.core.rules.lifecycle import ProjectPhase
from scientific_reproduction.planning.phase import advance_project_phase
from scientific_reproduction.planning.plan import build_plan_v1
from scientific_reproduction.planning.freeze import freeze_plan

root = pathlib.Path("<workspace>")
PH, AT, ACTOR = "2026-09-05T04:30:00Z", "supervisor", "supervisor"  # 示意

# init: CLI(唯一有 CLI 的注册操作)
#   python scripts/reproduce.py init <DOI|PDF|URL> --root <root>
# 之后项目处于 INITIALIZING

advance_project_phase(root, ProjectPhase.SOURCE_ACQUISITION,
                      actor=ACTOR, at=AT, reason="论文已取得")
advance_project_phase(root, ProjectPhase.REPRODUCTION_INVENTORY,
                      actor=ACTOR, at=AT)

# …… §1 注册顺序:来源 → 证据 → 库存 → 需求(带 requirement_ids)→
#     目标 → 验收/统计/分析记录/闭环(记住 §6:acceptance.evidence_refs……)

advance_project_phase(root, ProjectPhase.PLANNING, actor=ACTOR, at=AT)
advance_project_phase(root, ProjectPhase.PLAN_AUDIT, actor=ACTOR, at=AT)

plan = build_plan_v1(root)        # 计划的唯一来源(勿手写漂移)
freeze_plan(root, plan)           # 前置条件见 §2;失败读取 warnings(§6 空 evidence_refs)

advance_project_phase(root, ProjectPhase.PLAN_FROZEN, actor=ACTOR, at=AT)
```

注:多次调用不违反任何约束——每个 advance 都是"推进到目标"的声明,重复调用同目标
幂等无副作用(先按 claim 恢复后返回)。
