# Workflow 使用与扩展手册

这份手册讲"怎么用、怎么改"。设计取舍与作业要求对照见 [`Design.md`](../Design.md)。

## 1. 一句话理解

一条流程 = **一张图**：

- **节点**（`Node`）是要做的事：调一个已有智能体（`agent`）、在工作目录跑一段 Python
  （`command`）、做个判断（`judge`），或者只是标个终点（`end`）；
- **连线**（`Edge`）决定下一步去哪：连线上的 `when` 条件成立才走；指回前面的节点就是循环；
  节点标了 `parallel=True` 时它的多条出边一起走（并行）；
- **共享状态**（`WorkflowState`）是节点之间唯一的通信通道：上一个节点把结果写进某个键，
  下一个节点的任务模板里写 `${那个键}` 就能取到。

## 2. 命令速查

```powershell
D:\Anaconda\python.exe -m agentcode workflow list                  # 列出内置流程
D:\Anaconda\python.exe -m agentcode workflow show mini_se          # 看节点与连线
D:\Anaconda\python.exe -m agentcode workflow run mini_se --input request="写一个计算器"
D:\Anaconda\python.exe -m agentcode workflow run test_pipeline --llm mock
D:\Anaconda\python.exe -m agentcode workflow runs                  # 最近的运行
D:\Anaconda\python.exe -m agentcode workflow log <run_id>           # 某次运行的每个节点
```

网页：起服务后打开 `/workflow`（`agentcode open`）。

## 3. 看懂 `workflow show` 的输出

```
[智能体] design       ② 方案设计  ← react
        └─ 收尾 → implement
[判定]   check        是否全绿
        └─ 没过，回到生成 → generate（最多走 1 次）
        └─ 收尾 → report
```

- `[智能体]` / `[命令]` / `[判定]` / `[终点]` 是节点类型；
- `← react` 表示这个节点交给哪个智能体；
- `└─ <连线的 label> → <目标节点>` 是连线；带"最多走 N 次"的是循环的回边。

## 4. 写一条自己的流程

```python
from agentcode.workflow import Edge, Node, Workflow
from agentcode.workflow.library import register_workflow
from agentcode.workflow.store import save_run
from agentcode.workflow import WorkflowEngine

workflow = Workflow(
    name="doc_pipeline",
    title="文档流水线",
    description="需求 → 写作 → 校对（不过关就再写一轮）",
    nodes=(
        Node(name="outline", title="① 大纲", agent="plan_and_solve",
             task="把需求拆成大纲：${request}", output="outline",
             edges=(Edge("write"),)),
        Node(name="write", title="② 写作", agent="react",
             task="按大纲写正文：${outline}", output="draft",
             edges=(Edge("proof"),)),
        Node(name="proof", title="③ 校对", agent="reflection",
             task="检查下面的正文，指出问题并给出修改后的版本：${draft}",
             output="final",
             # 有严重问题就回到写作，最多回一次；否则收尾
             edges=(
                 Edge("write", when=lambda s: "严重" in str(s.get("final") or ""),
                      max_times=1, label="有严重问题，重写"),
                 Edge("done", label="收尾"),
             )),
        Node(name="done", kind="end", title="完成"),
    ),
)
register_workflow(workflow)

run = WorkflowEngine(workflow, llm_mode="mock").run({"request": "介绍工作流的概念"})
save_run(run)
```

要点：

1. `output` 是"这个节点的产出写进状态的哪个键"，默认是节点名；
2. 条件函数拿到的就是共享状态，返回真值就走这条边；
3. 回边一定配 `max_times`（或节点的 `max_visits`），否则整条流程靠 `max_steps` 兜底，
   会在记录里报"超过最大步数"；
4. 想让流程"带伤继续"，把节点标 `on_error="continue"`，最终状态会是 `partial`；
5. 命令节点跑的是 **Python 代码**（在工作目录里，退出码 0 算成功），
   所以要跑 pytest 就写 `sys.exit(pytest.main([...]))`。

## 5. 换成别的智能体

节点的 `agent` 字段就是项目1 注册表里的名字（`agentcode workflow` 的清单里会列出用到的名字）。
想看全部可用名字：`agentcode list`。想新增一个智能体，看项目1 的
[扩展指南](../README.md#扩展指南)。

## 6. 运行记录在哪

- 默认写在 `traces/workflows/<run_id>.json`（`AGENT_TRACE_DIR` 改根目录），
  `traces/` 不进仓库；
- 记录内容是"一次运行的全部事实"：输入、每个节点的状态/耗时/产出/错误、共享状态快照、
  总步数与总耗时；
- 写入是原子的（先写 `.tmp` 再替换），进程中途被杀不会留下半截 JSON。

## 7. 常见问题

| 现象 | 原因与怎么办 |
| --- | --- |
| 结果里有 `（演示数据）` | 用的是离线脚本模型。加 `--llm openai`（需要 `.env` 里的模型配置） |
| 流程报 "超过最大步数" | 图里有闭环没有出口：给回边加 `max_times`，或改条件 |
| 流程报 "没有走到终点节点" | 某条分支没有出口：检查 `when` 条件是否可能全不成立 |
| `command` 节点失败 | 看 `workflow log` 里该节点的 stderr 与退出码；工作目录在 `AGENT_CODE_ROOT` |
| 网页上节点一直"跑着" | 页面刷新即可：流程在后台线程里跑，跑完的记录会落盘，`workflow runs` 也能查到 |
