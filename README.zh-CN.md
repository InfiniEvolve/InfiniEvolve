[English](README.md) | 简体中文

# InfiniEvolve

套在 coding agent 会话之外的一层循环。它为每个任务维护一条谱系：每次会话从某次早先会话的成果出发，
能读到之前各次会话试过什么，结束后连同产生的全部内容一起归档。下一次会话从哪里开始、任务何时
不再有进展，由这层循环决定。

框架只负责循环和档案，不了解任务本身：测量什么、哪个方向更好、怎样的结果算有效、一次会话的成果
是什么形式、agent 在哪里运行，都由框架之外提供。

## 快速开始

需要 Python 3.10 或更高版本；示例还需要 `git`。

```sh
git clone https://github.com/xiemeimeiaaa/InfiniEvolve.git && cd InfiniEvolve
uv venv -p 3.11 .venv && uv pip install -p .venv -e .

.venv/bin/infinievolve run examples/local/campaigns/demo
```

示例任务要求写出尽可能短的正确程序，agent 由一个脚本替身扮演，所以几秒钟就能跑完，不需要任何账号。
运行时每归档一次会话打印一行，最后打印汇总：

```
[golf/s000] archived final: bytes 466 (valid); wall 0.0 min; best so far 466
[golf/s001] submitted, start from s000 (best); limit 10 min
[golf/s001] archived final: bytes 286 (valid); agent 0.0 min; wall 0.0 min; best so far 286
...
[golf] stop: best 163 within 0.0 of 163 over the last 2 sessions

| task | ranked by | sessions | valid | baseline | best | best session | agent h | status |
|---|---|---|---|---|---|---|---|---|
| golf | bytes | 7 | 7 | 466 | 163 | s005 | 0.00 | best 163 within 0.0 of 163 over the last 2 sessions |
```

## 产出是什么

一个 campaign 产生的所有内容都在它自己的目录里。常看的部分：

| 想知道 | 看哪里 |
|---|---|
| 每个任务最后的结果 | `summary.md`（`infinievolve report` 也会打印） |
| 某个任务逐次会话的情况：每个指标、当前最好成绩、标记 | `tasks/<task>/archive/matrix.md`（`infinievolve report --task <task>`） |
| 最好的成果本身 | `tasks/<task>/archive/artifacts/<最好的会话>.patch`：相对任务 baseline 的 diff，用 `git apply` 应用 |
| 某次会话收到的提示词 | `tasks/<task>/sessions/<sid>/input/prompt.md` 和 `archive.md` |
| 某次会话自述做了什么、学到什么 | `tasks/<task>/sessions/<sid>/derived/report.md` |
| agent 实际做了什么 | `tasks/<task>/sessions/<sid>/raw/`，内容由适配器决定（示例保留 agent 的输出和评估器的结果） |
| 为什么选了某个起点 | `tasks/<task>/sessions/<sid>/input/selection.json` |
| 按时间顺序发生的所有事 | `events.jsonl` |
| 供分析用的表格和谱系图 | 运行 `infinievolve export` 后的 `exports/`：`nodes.csv`、`scores.csv`、`best_so_far.csv`、`<task>/lineage.{json,dot,mmd}` |

示例的会话矩阵：

```
| session | parent | start | scored | valid | bytes | best so far | flags | agent min | wall min | failed |
|---|---|---|---|---|---|---|---|---|---|---|
| s000 | - | root | final | yes | 466 | 466 | - | - | 0.0 | - |
| s001 | s000 | best | final | yes | 286 | 286 | - | 0.0 | 0.0 | - |
| s002 | s001 | best | final | yes | 228 | 228 | - | 0.0 | 0.0 | - |
...
| s005 | s004 | best | final | yes | 163 | 163 | - | 0.0 | 0.0 | - |
| s006 | s005 | best | final | yes | 163 | 163 | duplicate (soft) | 0.0 | 0.0 | - |
```

任何内容都不会被覆盖：每次会话的输入、原始输出和由此导出的内容都保留，并记录每个文件的校验和，
因此任何结果都能追溯，以后也可以换一种方式重新分析。

## 用在自己的任务上

使用示例里的本地适配器时，除了一个给版本打分的脚本，不需要写任何代码。

1. **把 `examples/local` 复制**到你的项目里。里面有适配器、一个任务目录和一个 campaign。
2. **在 `tasks/<name>/` 里描述任务**：
   - `workspace/`：baseline，也就是每次会话开始时的文件；
   - 一个给单个版本打分的程序。它在成果的一份干净副本里运行，最后一行输出一个 JSON 对象：
     `{"checks": {"tests_pass": true}, "metrics": {"score": 0.82}, "reasons": []}`；
   - `task.yaml`：

     ```yaml
     instruction: |
       要求 agent 做什么。
     time_limit_seconds: 3600
     evaluate: ["{python}", "{task}/evaluate.py"]     # {task} 是本目录
     declaration:
       metrics:
         score: {direction: max, scale: absolute, anchor: baseline}
       gate: [tests_pass]
       rank_by: score
     prompt:
       ranking: by `score`, higher being better, among versions that pass the tests.
     ```
3. **在 `campaigns/<name>/config.yaml` 里指定你的 agent**：设置 `tasks`，并把 `options.agent_command`
   设为一条 shell 命令，它以无交互方式启动你的 agent 并把提示词交给它。命令在 workspace 的一份副本里
   运行，并带有以下环境变量：

   | 变量 | 含义 |
   |---|---|
   | `EVOLVE_PROMPT` | 会话提示词的路径 |
   | `EVOLVE_ARCHIVE` | 早先会话完整档案的路径 |
   | `EVOLVE_WORKSPACE` | 工作副本，也是当前目录 |
   | `EVOLVE_CAMPAIGN` | campaign 目录 |

   agent 在该目录里改动的内容就是它的成果，它输出的内容作为对话记录保留，结尾的报告从中读取。
4. **运行**：`infinievolve run campaigns/<name>`。可以中断后再次运行。

以这种方式启动的 agent 会不经询问地修改文件和执行命令，请在你允许它这样做的地方运行。

当会话需要在本机以外的地方运行、成果不是一组文件改动，或者你想读取 agent 的用量、识别被拒绝的
agent、检查它在会话中做了什么时，再编写自己的适配器（见下文）。

## 工作流程

每个任务独立进行：

1. 先给未修改的 baseline 打一次分，作为谱系的根节点 `s000`。
2. 然后重复：
   1. 检查停止条件；
   2. 选下一次会话的起点节点（`best`、`explore`、`baseline` 三选一）；
   3. 生成会话提示词，内容包括任务说明、起点说明和档案摘要；
   4. 运行一次 agent 会话，然后评估它留下的成果；
   5. 把结果归档为一个新节点。
3. 会话数达到 `max_sessions`，或者最近 `window_k` 次会话没能把最好成绩再提升超过
   `min_improvement`，任务停止。

同一个任务内的会话按顺序进行，因为后一次要读前面的档案。不同任务之间并行，同时在跑的作业数不超过
`max_parallel`。campaign 可以随时中断，再次运行时会接回仍在运行的作业，并重新提交已准备好但还没
启动的会话。

三种选起点的方式：`best` 取排名值最好的有效节点；`explore` 在所有有效节点中抽取，成绩越好越容易
被抽中，已经被很多会话用作起点的节点则更少被抽中，因此较弱的节点仍有机会成为通向更好结果的
垫脚石；`baseline` 总是从未修改的 baseline 出发，档案只作为参考。

## 需要声明的内容

适配器为每个任务给出一份声明，campaign 可以覆盖其中一部分：

```yaml
metrics:                          # 评估器报告的每一个数值
  pass_rate: {direction: max, scale: absolute, anchor: baseline, format: "{:.1%}"}
  tokens:    {direction: min, scale: absolute}
gate: [builds, tests_unchanged]   # 一个版本必须全部通过这些检查才算有效
rank_by: pass_rate                # 用来选起点和判断停止的那一个指标
show: [pass_rate, tokens]         # 后续会话能看到哪些指标
confirm: {samples: 2, aggregate: median}   # 新的最好成绩先复测再采信（可选）
flags: {suspicious_access: hard}  # 适配器所打标记的级别
artifact: patch                   # 会话成果以什么形式归档
```

- **direction** 说明哪个方向更好。**scale** 说明可比较的是差值（`absolute`）还是比值（`ratio`），
  它决定选择起点时如何给候选加权，以及 `min_improvement` 按数量还是按百分比理解。**anchor** 说明
  最好成绩曲线从哪里起算。
- 评估器为它打分的每个版本报告一组具名**检查**。`gate` 中列出的检查全部为真，这个版本才有效；
  没有报告的检查按未通过处理。
- 排名只用一个指标，其余指标照常记录、展示和导出。
- baseline 归档时生效的声明对这条谱系固定不变。要更换声明，需要重置该任务。

## 档案里有什么

```
<campaign>/
  config.yaml  provenance.json  state.json  summary.{json,md}
  events.jsonl                       所有状态变化，带时间戳和任务名
  tasks/<task>/
    task.json                        这条谱系固定下来的声明
    sessions/<sid>/
      input/     prompt.md archive.md start.* selection.json spec.json
      raw/       适配器保留的原始内容（对话记录、日志、评估器输出）
      derived/   scores.json report.md stats.json artifacts/
      job.json node.json manifest.json（每个文件的 sha256）
    archive/     nodes/ artifacts/ archive.jsonl matrix.{json,md}    给下一个 agent 看的视图
  exports/       <task>/lineage.{json,dot,mmd}  nodes.csv scores.csv best_so_far.csv
```

不论什么任务，每个节点的骨架都相同：它在谱系中的位置、归档的成果（`kind`、`fingerprint`、
`summary`）、每个被打分的版本及其检查和指标、是否有效、排名所用的值、agent 的报告、耗时、用量，
以及它的标记。

**标记**表示某个节点的结果不能直接采信。每个标记有一个级别：`soft` 只记录和展示；`hard` 使节点
不能作为起点、不算最好成绩、不进入停止曲线；`void` 表示这次会话实际上没有发生。标记由适配器打出
并声明级别，人也可以用 `infinievolve flag` 添加、用 `infinievolve clear` 解除。框架自己只打两种：
会话成果与早先某个节点完全相同时的 `duplicate`（soft，仅限能够判断这一点的成果类型），以及复测
否定了首次打分所接受的成果时的 `confirmation_failed`（hard）。

**成果类型**描述会话成果的形式。成果的字节内容由适配器产生，也由适配器还原为后续会话的起点；
成果类型负责为提示词生成概括、为完整档案生成展示内容，并在"字节相同即成果相同"成立时给出指纹。
内置 `patch`（相对 baseline 的 diff）和 `text`（单个文档）两种，可以用
`infinievolve.artifacts.register_artifact` 注册新的类型。

## 命令

一个 campaign 就是一个目录，目录里的 `config.yaml` 是它唯一的配置，运行产出也都留在这个目录里。

```sh
infinievolve new campaigns/first            # 生成一份带注释的 config.yaml，供编辑
infinievolve run campaigns/first            # 可以中断后再次运行
infinievolve status campaigns/first
infinievolve report campaigns/first [--task T]
infinievolve export campaigns/first
infinievolve reset campaigns/first --task T                        # 让某个任务从头开始
infinievolve flag  campaigns/first --task T --session S --flag NAME --reason "..." [--level hard|soft|void]
infinievolve clear campaigns/first --task T --session S --reason "..."
```

`prompt.carry` 决定一次会话从之前的会话继承什么，与它从哪个节点出发无关：`full`（表格、每份报告、
每次会话的成果）、`reports`（不含成果）或 `none`（没有档案，提示词也不提档案）。

## 编写适配器

适配器是唯一了解你的任务的代码，它实现 `infinievolve.core.adapter.Adapter`：

| 它说明 | 通过 |
|---|---|
| 有哪些任务 | `resolve_tasks`、`instruction`、`default_time_limit_seconds` |
| 记录什么、按什么排名 | `declaration` |
| 提示词中与任务相关的句子 | `prompt_text` |
| 作业怎么运行 | `submit_baseline`、`submit_session`、`poll`、`collect` |
| 环境是否损坏 | `baseline_problem` |
| 声明了 `confirm` 时，怎样给已归档的成果重新打分 | `submit_rescore` |

作业是异步的：`submit_*` 返回一个句柄，适配器在重启后凭它找回作业；`poll` 说明哪些作业已经结束；
`collect` 返回被打分的各个版本、它们的成果、报告、耗时和标记。三种异常告诉循环一次失败意味着什么：
`JobFailed`（暂停该任务）、`AgentUnavailable`（agent 的服务拒绝了请求，等待后重新提交）和
`HostFault`（运行的地方坏了，立即重新提交）。

campaign 通过以下任一方式指定适配器：已注册的名字、`infinievolve.adapters` 组的 entry point、
`package.module:Name`，或 `path/to/file.py:Name`。

[`examples/local`](examples/local) 是一个写在单个文件里的完整适配器，每个作业都作为本机进程运行，
可以复制后修改。会话在哪里运行、agent 怎么启动、账号怎么管理，都有意留给适配器决定。

提示词由三个来源拼成，因此框架自带的文字不涉及任何一类任务：框架的骨架（会话如何进行、档案、
结尾的报告）、声明（表格的列、报告的字段）和适配器的 `PromptText`。

## 测试

```sh
uv pip install -p .venv pytest && .venv/bin/python -m pytest -q
```

`tests/fake_adapter.py` 是一个不实际运行任何东西的适配器替身；有一个测试会端到端运行本地示例，
需要 `git`。

## 许可证

[MIT](LICENSE)
