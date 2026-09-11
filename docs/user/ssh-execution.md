# 普通 SSH 服务器执行通路(无调度器)

> 计算目标需要跑在远程**普通 SSH 服务器**上(无 Slurm/作业队列)时的指导。
> 面向 Supervisor/Worker 会话;示例中的 `ssh/scp` 命令均用占位符
> (`<user>`/`<host>`/`<port>`/`<server_workdir>`),**具体凭据与主机只出现在会话的
> SSH 配置/命令里,绝不进入工作区与交付物**。

## 1. 为什么不能直接用适配器,以及何时走本文路径

v0.1 的计算适配器层(`adapters/compute/ssh.py`、`slurm_ssh.py`,见
`docs/operations/adapters-slurm.md`)是**构造器注入**设计:远程边界
`SSHTransport` 是纯 ABC,**发行包不附带任何具体传输实现**(不 import paramiko)。
因此:

- 集成方(平台/skill 部署者)注入真实传输后,才可调用 `SSHComputeAdapter` 等;
- **普通 zip 接收者手里的 agent 会话**(任何模型)没有现成该传输 → 正确默认 =
  本文的**会话级 `scp`/`ssh` 通路**:执行仍是确定性脚本,状态仍全部经运行时
  注册表落地(rollback 等同单机)。

只有"计算执行在一个远程账号 + 一个工作目录"这种最简单形态适用本文;有调度器、
多节点、queue visibility 需求 → 让集成方注入传输,回退到适配器。

## 2. 通路(实测可行,一次性同步+长任务轮询)

**阶段 A:本地打包 payload**(确定性,单文件 runner 优先,见 §3b 纪律)

```text
<project>/compute/<run_id>/            # 本地 staging
  runner.py                            # 自足脚本:读入参、算、写 out/ + done.json + result.json
  inputs/                              # 所需输入(小文件;大文件先签 manifest 再传)
```

**阶段 B:pushing**(`scp` 整目录,保留属性):

```bash
scp -P <port> -r <project>/compute/<run_id>/ <user>@<host>:<server_workdir>/<run_id>/
```

**阶段 C:run**(短任务直跑;长任务 `nohup` + 轮询):

```bash
ssh -p <port> <user>@<host> "cd <server_workdir>/<run_id> && nohup python3 runner.py > run.log 2>&1 &"
# 轮询(确定性间隔):看 done.json,不是看日志文本
ssh -p <port> <user>@<host> "test -f <server_workdir>/<run_id>/done.json && echo DONE || echo PENDING"
```

**完成标志只认 `done.json`**:runner 在**最后一步**原子写出 `done.json`
(内容含 `run_id`、`job_id`、`result.json` 的校验和)。`tail run.log` 只是诊断
位置,不是完成信号——"日志像完了"与"真的完了"之间是状态间隙。

**阶段 D:pull + 注册**(结果必须全部收回,服务器目录只是 scratch):

```bash
scp -P <port> -r <user>@<host>:<server_workdir>/<run_id>/out/ <project>/compute/<run_id>/out/
```

然后按注册顺序落盘:

1. 产物进 `manifests/`(ArtifactRegistry)——先注册;结果记录引用它(见
   bootstrap §4 坑 8、§4.2 的 `UNRESOLVED_RUN_ARTIFACT`)。
2. 结果包 `runs/<run_id>/GOAL-<goal_id>.json`(带 `metrics`,人读摘要数据源,
   bootstrap §5);`input_artifact_ids` 指向第 1 步;`protocol_version` 冻结后为 `v1`。
3. run 记录:通过 `core.transitions.transition` 走
   `CREATED → READY → DISPATCHED → RUNNING_EXTERNAL → RESULT_AVAILABLE
   → ANALYZING → SUBMITTED_FOR_REVIEW → CLOSED`;`external` 字段:

```json
"external": {
  "backend": "ssh",
  "job_id": "<deterministic, e.g. generate_id(\"job\", run_id)>",
  "working_directory": "<server_workdir>/<run_id>"
}
```

`lifecycle_state=CLOSED` + `scientific_review=PASS` 才计入"成功"(bootstrap §4、
坑 7)。有监控部署时,Monitor 的 `watched` 条目按同一三元组
(backend + job_id + working_directory)对齐(见 `docs/user/monitor-and-handoff.md` §2)。

## 3. 硬纪律(全部真实情景,违反即返工)

| # | 纪律 | 为什么 | 做法 |
|---|---|---|---|
| 1 | **凭据不进工作区/门禁**:`<user>:<password>@<host>` 永不写进任何项目文件、脚本、skill 交付物;仅出现在本会话 SSH 环境 | 项目可能是审计上传件(检查器可读);适配器 AC-02 同源 | 用 `ssh -p <port> <user>@<host>` 交互式/agent 密钥;相关凭据留 SSH agent/config |
| 2 | **服务器目录=计算 scratch,不是审计工作区** | 工作区是 Single Source of Truth;远程任何手工改动都绕过审计 | 远程只放 payload + 产物;结果全部 pull 回来;绝不在远程改写 `project.yaml` 等状态 |
| 3 | **完成标志契约定死**:runner 必须原子写 `done.json`,其内容须能对照本地(如含 `result.json` 校验和) | "日志显示完成"≠"完成";轮询日志文本会有状态间隙 | runner 模板:见 §2 阶段 C;结果校验和在拉回后核对,不一致即失败 |
| 4 | **job 身份确定**:`job_id` = `generate_id("job", run_id)`,每 run 唯一;重试用 `engineering_retries` 追加记录,不改 job_id | 幂等与审计:同一 job_id 复用于不同 run 会让监视/恢复无法分辨 | 见 bootstrap 坑 12 同源的"命名唯一性"原则 |
| 5 | **重推全量覆盖**:payload 目录反复 push 时,先清空远程该 run 目录(或推入新 attempt 目录)再放结果 | 残留旧产物会级联污染 | `ssh "rm -rf <server_workdir>/<run_id> && mkdir -p ..."`;或 `<run_id>/attempt-2`(与 `engineering_retries` 的 attempt 索引一致) |
| 6 | **失败保留**:run 失败/取消 → `lifecycle_state=CANCELLED`/`INVALIDATED` + `scientific_review=FAIL` 留在项目里 | 科学复现回补纪律;不得因后续成功删改失败 run | 对照 `monitor-and-handoff.md` §3.4 的分类:连接层(transport)vs 任务层(job),决定等重发还是 Supervisor 裁决 |
| 7 | **safe 路径段**:`<server_workdir>/<run_id>` 与产物名避免空格/特殊字符 | 适配器 prepare 阶段的同源校验 | 命名 `[A-Za-z0-9_-]` 子集 |

## 4. 服务器前置检查(一次会话内,不写入交付物)

```bash
ssh -p <port> <user>@<host> "python3 --version && mkdir -p <server_workdir> && df -h <server_workdir>"
```

- python3 版本满足 runner 依赖(3.11+ 若不引第三方库则满足;第三方库按
  `software_environment` 在远程预装,**环境差异记为 run 的 deviation 并写入事件**);
- 目录存在且有写权;
- 带宽/磁盘预检:预计产物总量 < 剩余空间。

## 5. 与运行时视图的关系

- **监控**:纯会话路径无自动 probe;有 Monitor 部署时,监视条目按 §2 external
  三元组注册,`reconcile` 只认显式 `RESULT_AVAILABLE` 信号(默认 probe 恒为
  unknown——AC-02,不会伪造完成),所以手工路径下"完成"由 Supervisor 依据
  done.json + pull 结果判定后推进状态,不依赖自动监视。
- **适配器对照**:若部署方注入传输,`SSHComputeAdapter`(`backend="ssh"`)的
  操作切面(prepare/submit/status/collect/cancel)与本 §2–§3 一一对应,只是
  自动化了——本文是零依赖版本。
- **关联文档**:`bootstrap-state-authoring.md` §4(run 状态机/必填字段)、§5(metrics)、
  §6(AC-01 链接)、`monitor-and-handoff.md` §2–§3(监视/恢复)、
  `adapters-slurm.md` §6(transport vs job 失败分类)、§1(backend 取值)。
