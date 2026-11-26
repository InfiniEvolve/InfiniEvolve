[English](writing-an-adapter.md) | 简体中文

# 编写适配器

适配器是唯一了解你的任务的代码：任务有哪些、会话在哪里运行、成果怎样评估、成果怎样保存和还原。
本页说明框架交给适配器什么，又期望从适配器拿回什么。

内置的适配器 [`infinievolve/local.py`](../infinievolve/local.py) 就是一个写在单个文件里的完整适配器：
每个作业都是本机上的一个进程。可以对照着本页阅读。
[`examples/faibench`](../examples/faibench) 则是一个通过 ssh 把作业放到集群上运行的适配器。

## 适配器是怎样创建的

`campaign.adapter` 指定一个可调用对象。每执行一条 `infinievolve` 命令，它被调用一次，参数是该 campaign 的
`CampaignConfig`，返回值就是适配器对象。通常写成一个类，`__init__` 接收这个配置。

配置里可供你使用的内容：

| 属性 | 含义 |
|---|---|
| `config.name` | campaign 的名字（即目录名） |
| `config.root` | campaign 目录的绝对路径 |
| `config.options` | `options:` 一节，原样 |
| `config.defaults.agent` | `agent:` 一节，原样 |
| `config.for_task(task)` | 该任务的设置：`carry`、`session_minutes`、`hide_scores` 等 |
| `config.max_parallel`、`config.poll_seconds` | 调度器的限制 |

框架不读取 `options:` 和 `agent:` 里的任何内容。请在 `__init__` 里检查它们，缺什么就抛 `ValueError`，
这样错误的配置在提交任何作业之前就会失败。

一个适配器对象只存活于一条命令期间。之后的 `run` 需要用到的东西不要只放在内存里，要放进作业句柄
或写到磁盘上。

### 由多个文件组成的适配器

`path/to/file.py:Name` 只加载这一个文件。如果它所在的目录有 `__init__.py`，该目录会作为一个包加载，
于是 `from .other import thing` 可以使用。否则请把适配器写在一个文件里，旁边的其他文件通过
`Path(__file__).parent` 读取。

## 一次会话的全过程

```
框架                                   适配器                            磁盘：sessions/<sid>/
----                                   ------                            ---------------------
选择起点节点
写出提示词和档案                                                         input/prompt.md
                                                                         input/archive.md      （carry: none 时没有）
                                                                         input/start<后缀>     （从 baseline 出发时没有）
                                                                         input/reference<后缀> （见下文）
                                                                         input/selection.json、input/spec.json
调用 submit_session(spec, dir)   --->  启动作业，返回句柄                input/...  你自己的任何文件
记录句柄                                                                 job.json
反复调用 poll(jobs) 直到结束     --->  说明哪些作业已结束
调用 collect(job, dir)           --->  取回输出，返回 SessionResult      raw/...    你保留的全部内容
归档结果                                                                 derived/scores.json、report.md、artifacts/
                                                                         node.json、manifest.json
```

`input/` 归框架所有，你可以往里加自己作业需要的文件。`raw/` 完全归你：对话记录、日志、评估器输出都放
在这里，因为别处不会记录它们。agent 的对话记录请存为 `raw/agent.jsonl`（它的 stream-json 输出，每行一
个事件）：查看页面从这个文件分析会话。`derived/`、`job.json`、`node.json` 和 `manifest.json` 归框架，不
要写。

会话的成果以字节形式交给你：`spec.start_artifact`（磁盘上同时有 `input/start<后缀>`，后缀由成果类型
决定，默认类型是 `.patch`）。它就是你当初作为该节点的成果返回的内容，所以还原它就是你保存它的逆
过程。`None` 表示"从 baseline 出发"。

### 参考节点

有些测量只有和另一个版本用同样的方式、在同一时间、同一个地方一起测才有意义：把候选版本与参考版本
交替计时就是典型情况。为此，框架会把 campaign 参考节点的成果放在作业输入旁边，文件名是
`input/reference<后缀>`，会话和复测作业都有。参考节点在 baseline 有效时是 baseline，否则是第一个有效
的会话；参考是 baseline 时不写这个文件（你本来就有它），还没有任何有效节点时也不写。用不用它由你
决定：多数评估不需要参考，也不是每一类任务都能成对测量。

## 各个方法

### 描述任务

| 方法 | 何时调用 | 返回 |
|---|---|---|
| `resolve_tasks(selection)` | 每条命令一次 | 任务名：检查后原样返回给定列表，或在 `"all"` 时返回全部任务 |
| `declaration(task)` | baseline 之前，以及循环每次核对时 | 声明（见 README）；baseline 归档后即固定 |
| `instruction(task)` | 每次生成提示词时 | 任务说明，原样贴入提示词 |
| `prompt_text(task)` | 每次生成提示词时 | 一个 `PromptText`（见下文） |
| `default_time_limit_seconds(task)` | 每次会话，除非配置设置了 `session_minutes` | 你的作业应当执行的时间上限 |
| `baseline_problem(task, root)` | 每次会话之前 | baseline 的结果为何说明环境损坏，否则返回 `None` |
| `provenance(tasks)` | 首次运行时一次 | 值得固定下来的版本和摘要；绝不要放密钥 |

`archive_location` 是属性而不是方法：一段说明 agent 能在哪里读到完整档案的文字，会以"……位于
*archive_location*（只读）"的形式贴入提示词。启动 agent 时，请让 `input/archive.md` 在那个位置可读。

`baseline_problem` 拿到的是根 `Node`。它的 `scores` 是字典，字段与 `ScoreRow` 相同（`checks`、
`metrics`、`reasons`）。对于从空桩开始的任务，baseline 通不过 gate 是正常的；只有当结果不可能来自任务
本身时才报告问题。

时间上限由你执行。框架通过 `spec.time_limit_seconds` 把它传给你，但从不告诉 agent。

### 运行作业

| 方法 | 作用 |
|---|---|
| `submit_baseline(task, session_dir, events)` | 开始给未修改的 baseline 打分；返回句柄 |
| `submit_session(spec, session_dir, events)` | 开始一次 agent 会话；返回句柄 |
| `submit_rescore(task, name, artifact, job_dir, events, avoid=())` | 开始给已归档的成果重新打分，不运行 agent。只有声明设置了 `confirm` 时才需要 |
| `poll(jobs, events)` | 返回每个已结束作业的 `job.key` |
| `collect(job, session_dir, events)` | 取回已结束作业的输出，返回 `SessionResult` |

**句柄**是你自己定义的字典，以 JSON 形式保存在 `job.json` 里。它必须足以让一个新进程重新找到这个
作业：进程号加目录，或者集群作业号加远程路径。只使用 JSON 类型。

**`poll`** 一次拿到所有在跑的作业，因此一次查询就能覆盖全部。只有确知作业已结束时才返回它；查不到
状态的作业不算结束。如果 `poll` 抛出异常，调度器只记录一次，保留这些作业，到下一个间隔再试，所以
连接中断不需要特别处理。没有产出结果就结束的作业也算结束：在 `collect` 里说明这一点。

**`job.kind`** 取值为 `"baseline"`、`"session"` 或 `"confirm"`。复测作业的 `session_id` 是
`<sid>-c<n>`（如 `s003-c1`），与它的 `submit_rescore` 收到的 `name` 相同；它的目录是
`sessions/<sid>/confirm/r<n>/`，布局与会话目录一样：你的 `input/` 和 `raw/` 放在那里。`avoid` 列出该
成果此前各次打分的 `timing["where"]`，供能够选择运行位置的适配器使用。

**调度器在你的机器上运行**，campaign 进行多久它就运行多久。它停止后，集群上的作业仍会继续；下一次
`run` 会通过句柄重新接上它们。

## `collect` 返回什么

```python
SessionResult(
    scores=[ScoreRow(label="final", checks={...}, metrics={...}, reasons=[...], extra={...})],
    artifacts={"final": b"..."},
    report="...", timing={...}, usage={...}, stats={...}, flags={...}, error=None)
```

**`scores`**：你评估过的每个版本对应一个 `ScoreRow`。多数适配器只评估一个，即会话结束时的状态，标签
用 `"final"`。如果任务还会给中途的提交打分，就为每一个各返回一行，标签自定；框架会按排名指标归档
其中最好的有效版本。

- `checks`：`{名字: 布尔值}`。声明的 `gate` 列出版本必须通过的那些；缺失的按未通过处理。声明没有写 gate
  时，你报告的每一项检查都必须为真。你知道的检查
  都应报告，不论是否在 gate 里：它们都会被记录。
- `metrics`：`{名字: 数值}`，对应声明里列出的指标。没有测量的可以省略或传 `None`。有效但缺少排名
  指标数值的版本会被归档，但不能作为起点。
- `reasons`：检查未通过的原因，用评估器自己的话；会展示给后续会话。
- `extra`：这次打分中其他值得保留的内容。

**`artifacts`**：`{标签: 字节}`，每个被打分版本的成果，形式由声明的成果类型决定。默认类型是该版本
相对 baseline 的 diff，包含新增文件，可用 `git apply` 还原。baseline 的成果返回空字节。

**`report`**：agent 结尾的报告。`infinievolve.core.prompt.extract_report(texts)` 会从 agent 说过的话
（一段文本，或按顺序排列的各条消息）里找出它。把对话记录转成这些文本是你的工作，因为每种 agent 的
记录格式都不同。

**`timing`**：以秒为单位，键名随你。框架会读取其中三个，其余原样保留：

| 键 | 用途 |
|---|---|
| `agent_seconds` | 后续会话和各表格里的 "agent min" 一列 |
| `queue_seconds` | 调度器的日志行 |
| `where` | 作业运行的位置，会作为 `avoid` 交回给 `submit_rescore` |

框架会补上 `wall_seconds`，即从准备会话到归档的时长。

**`usage`**：agent 报告的 token 和费用。**`stats`**：每次会话的其他任何数据。两者都原样存在节点上并
导出。

**`flags`**：`{名字: 原因}`，用于不能直接采信的结果，例如读了答案的会话。声明的 `flags` 为每个名字
指定级别（未指定时为 `hard`）。

**`error`**：出了问题、但结果仍值得归档时，留在节点上的一条备注。

**`remeasure`**：你不信任这次测量时给出的原因（各轮计时互相矛盾、结果来自错误的硬件）。这份成果随后
会通过 `submit_rescore` 重新打分至少两次，并按聚合值排名。其他情况保持 `None`。

对于 baseline 或复测作业，只会读取 `scores`、`artifacts`（以及 `timing`）。

## 作业出错时

在 `collect` 里抛出：

| 抛出 | 何时 | 循环随后 |
|---|---|---|
| `JobFailed` | 作业在产出结果之前就坏了 | 暂停该任务；下一次 `run` 重新提交同一次会话 |
| `AgentUnavailable(message, retry_at)` | agent 的服务在它开始工作前就拒绝了（限额、鉴权、故障） | 所有任务等待到 `retry_at`，然后重新提交 |
| `HostFault` | 运行的地方坏了，而且你已安排不再使用它 | 立即重新提交 |

抛出其中任何一个之前，请删除（或移走）这次尝试留在 `raw/` 里的内容：会话会用相同的输入重新提交，
失败尝试的任何残留都不能被误当成它的结果。

`collect` 抛出的其他异常一律视为"没取回来"：任务暂停，作业记录保留，下一次 `run` 会再次收集。
`submit_*` 方法抛出的异常会在记录任何内容之前暂停任务。

被 agent 提前结束的会话，或产出了无效成果的会话，都不算失败：照常返回它的结果。

## 事件

`events.emit(type, session_id, **data)` 向 campaign 的 `events.jsonl` 追加一行。发事件是可选的。
适配器可以使用 `job_submitted`、`job_started`、`job_held`、`job_finished`、`collected` 和
`host_excluded`，或任何以 `adapter_` 开头的自定义类型；其他名字属于框架，会被拒绝。在 `poll` 里，
请先把日志绑定到作业所属的任务：`events.for_task(job.task).emit(...)`。

## 提示词中与任务相关的句子

`PromptText` 填充提示词里框架无法代写的部分：

| 字段 | 出现的位置 | 默认值 |
|---|---|---|
| `goal` | 提示词的第一句 | "You are working on the task below." |
| `working` | 第一条规则：怎样工作才能让结果被保存下来 | "Do your work in the directory you are started in." |
| `ranking` | "How your work is ranked: ..." | 由声明生成：排名指标、哪个方向更好、gate、是否复测 |
| `feedback` | 设置时单独成一条规则：会话期间怎样检查成果 | 不出现 |
| `persistence` | 接在 "You decide when the session is over." 之后 | 只要还有值得尝试的想法就继续 |
| `continuing` | 起点是早先会话的成果时，接在起点说明之后 | 在其基础上改进并保持有效 |

在 `ranking` 里说清楚测量的是什么、在什么上面测、噪声有多大。在 `working` 里说清楚你的保存方式所
依赖的一切：如果你取的是相对某个提交的 diff，就要告诉 agent 不要提交。

## 测试适配器

先只跑 baseline：把 `stop.max_sessions` 设为 `1` 然后运行。第一个作业不经过 agent，给未修改的 baseline
打分，会用到 submit、poll、collect 和你的评估器。日志里它那一行应当显示一个有效的结果，数值符合你的
预期。

然后检查只有第二次会话才会用到的往返过程：一次会话保存下来的成果，被还原为下一次会话的起点。保存、
还原到一份全新的副本、评估，再比较分数。自带示例的测试就是这样做的。

`tests/fake_adapter.py` 展示了最小的适配器：不运行任何作业，所有结果都是预先写好的。要对着真实的循环
试一份声明或一份配置，这是最快的办法。

## 自查清单

- `__init__` 会拒绝缺少适配器所需内容的配置。
- 仅凭句柄，新进程就能找到作业。
- `poll` 从不报告它没能查到状态的作业。
- 评估针对的是保存下来的成果，而不是 agent 留在周围的其他东西。
- 还原后的成果得分与保存时相同。
- `collect` 在抛出 `JobFailed`、`AgentUnavailable` 或 `HostFault` 之前清空 `raw/`。
- `provenance`、事件和代码里都没有站点相关的内容或密钥。
