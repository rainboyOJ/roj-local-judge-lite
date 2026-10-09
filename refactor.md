# 重构设计：judge 组织评测，executor 执行程序

状态：设计已按讨论收敛，代码尚未实施。实施步骤见 [plan.md](plan.md)。

本文替代旧版关于包结构、runner 边界和两次 exec 的方案。范围仅限代码工程结构，不以教程结构或教程兼容性决定代码设计，也不安排教程 Markdown 的修改。

## 1. 目标

让读者能沿着一条主线理解评测：

```text
准备环境与测试数据 → 编译 → 逐点执行 → 读取资源统计 → 判定 → 比较答案 → 汇总
```

模块按实际职责划分：judge 知道什么是题目、限制、答案和评测结果；executor 知道怎样受控地运行一个程序，并报告发生的事实。

衡量重构的标准是职责是否清楚、控制流是否顺畅、规则是否集中。文件更少、函数更短和进程更少本身不是目标。

## 2. 最终代码结构

```text
judge.py             评测入口、环境准备、测试数据、编译、测试点事务、判定与汇总
memory_cgroup.py     单个内存组的创建、配置、停止、统计与删除
executor.c           原 runner_helper.c：程序启动、设限、监控、进程回收与报告
executor             由 executor.c 编译生成，不提交到版本库
examples/delegated.py 委派环境下运行测试或任意命令的辅助入口
```

对应迁移：

| 当前代码 | 最终位置 |
|---|---|
| `local_judge.py` | 改名为 `judge.py`，整理主流程 |
| `runner.py` 的限制口径、结果判定、运行事务 | 并入 `judge.py`，按用途拆成函数 |
| `runner.py` 的 helper 调用 | 收敛为 `judge.py` 中的 `invoke_executor()` |
| `runner.py` 的独立 CLI | 移除，不再保留第二套 Python 参数入口 |
| `runner_helper.c` / `runner_helper` | 改名为 `executor.c` / `executor` |
| `memory_cgroup.py` | 保留，继续封装底层 cgroup 文件操作 |

最终不保留 `runner.py` 或新增 `executor.py`，不建立同名 `judge/` 包，也不增加 manager、backend、通用调度框架等抽象。

这是一次内部 API 和源码入口的迁移：不承诺旧的 `from runner import ...` 或 `python3 runner.py ...` 继续可用。仓库内的运行代码、测试、构建与安装入口必须一起迁移；教程相关文件暂不处理。已安装的命令名 `roj-local-judge-lite` 保持不变，启动器改为调用 `judge.py`。

## 3. 调用关系与进程关系

```text
Python judge 进程
  main → judge_submission → judge_case → invoke_executor
                                             │
                                             ▼
                                      C executor 进程
                                             │ fork
                                             ▼
                                      提交子进程
                                             │ exec
                                             ▼
                                         main.out
```

Python 函数调用不会新增进程。C executor 是提交程序的直接父进程，负责 `wait4`、看门狗和回收。提交子进程的 `exec` 替换程序映像，不新增 PID。

保留 C executor，是选择用 C 集中实现底层进程操作，并非操作系统强制要求额外增加一个 helper。当前这些能力已经主要存在于 C 中，本次保留它们，给执行器一个准确的名字。

cgroup 的范围必须明确：进入测试点内存组的是提交子进程及其后代；Python judge 和 C executor 都留在该组外。

## 4. 职责边界

| 工作 | 负责位置 |
|---|---|
| 解析参数、发现测试数据、读取题目限制 | judge |
| 检查 cgroup 父目录、选择隔离或降级、自动委派 | judge |
| 编译提交、选择 checker、遍历与汇总 | judge |
| 决定 CPU、内存、wall 等保护上限 | judge |
| 安排测试点 cgroup 的创建、统计与收尾时机 | judge |
| 实现 cgroup 配置、kill、等待清空、删除 | MemoryCgroup |
| 启动 executor、接收报告、取消时通知并等待它 | judge 的 invoke_executor |
| fork、标准流重定向、提交进程入组、设限、降权、exec | C executor |
| wall 超时终止、信号处理、wait4、进程组清理 | C executor |
| TLE/MLE/RE/SE 判定与优先级 | judge |
| 比较答案得到 AC/WA | judge |

### 执行保护与评测判定

executor 需要在运行过程中执行保护措施；不能把超时终止推迟到返回报告之后。

judge 决定结果。例如提交正常退出，报告 CPU 使用 1050ms，而题目限制为 1000ms，judge 应判为 TLE。executor 不需要包含 TLE、MLE、AC、WA 等评测标签。

题目阈值、保护余量及换算规则都放在 judge：

- CPU 判定用微秒与题目毫秒阈值比较；保护上限按当前规则加余量后向上取整为秒。
- 内存判定用 cgroup 峰值字节数；保护上限是题目内存加余量。
- wall 是独立的防卡死限制，覆盖提交的启动设置阶段。
- 时间或内存限制为 0、CPU hard limit 的额外一秒等现有行为保留。
- `RLIMIT_*` 的实际设置仍由 executor 子进程完成。可配置限制输入 0 时不覆盖继承值；`RLIMIT_CORE` 仍显式设为 0。

保留现有默认值：CPU 余量 200ms、内存余量 16MiB、自动 wall 余量 500ms、栈 64MiB、输出 64MiB、nproc 默认关闭。此次不调整评分口径。

## 5. Python 内部组织

judge 保留普通函数和少量数据结构，不引入一组互相转发的类。

| 函数 | 负责的完整动作 |
|---|---|
| `main()` | 解析参数、检查输入、处理列题、选择运行环境，然后进入评测 |
| `judge_submission()` | 管理工作目录，编译一次，逐点调用、展示并汇总结果 |
| `judge_case()` | 管理一个测试点的执行、资源收尾、判定和答案比较 |
| `invoke_executor()` | 构造命令、启动 executor、等待并解析执行报告 |
| `classify_execution()` | 根据运行事实、内存统计和题目阈值，返回执行结局及原因 |
| `make_protection_limits()` | 从 judge 的限制配置计算实际保护上限 |

现有数据查找、编译、输出比较、展示、自动委派函数按上述职责归组。`main()` 中的委派重启应明确可见，不能藏在一个看起来只读取配置的函数里。

限制配置可继续使用 `Limits`，但它属于 judge。计算后的保护值可使用一个小型 `ProtectionLimits` 数据结构，字段直接表达 CPU 秒数、wall 毫秒数、内存及其他资源的字节上限。不要为此次重构再增加配置继承体系。

`ExecutionReport` 表示 executor 的执行事实；`CaseResult` 表示 judge 的测试点结果。后者可以持有前者、可选内存统计、判定和原因，不必把所有字段再复制一遍。

## 6. executor 的输入与报告契约

### 输入

executor 接收已经算好的保护值，以及待运行程序及参数、工作目录、输入/输出/stderr 路径、UID/GID、是否降权、可选 `cgroup.procs` 路径。环境变量由 Python 在启动时传入。

它不读取题目配置、不搜索 cgroup 父目录、不决定自动降级，也不计算评分余量。

第一轮保留现有位置参数协议，避免同时重写 C 参数解析：

```text
executor CPU_SECONDS CGROUP_PROCS STACK_BYTES OUTPUT_BYTES NPROC WALL_MS
         DROP_PRIVILEGES UID GID CWD INPUT OUTPUT STDERR PROGRAM [ARG...]
```

上述换行仅为展示。参数使用列表传递，不经过 shell；禁用 cgroup 时 `CGROUP_PROCS` 是空字符串。Python 参数构造与 C 的 `parse_options()` 各集中在一处，其余代码使用命名字段。

路径固定、标准流别名检查、限制和 UID/GID 校验继续在 Python 调用边界执行。提交命令里的相对路径仍相对于提交工作目录；root 默认降权、环境继承选择等行为不变。

### 成功报告

保留当前 JSON 字段，减少协议迁移变量：

| 字段 | 含义 |
|---|---|
| `cpu_time_us` | 用于精确比较的 CPU 微秒数 |
| `cpu_time_ms` | 当前舍入方式得到的展示值 |
| `real_time_ms` | wall 时间 |
| `rss_kb` | wait4 的 RSS，只有辅助诊断用途 |
| `timed_out` | wall 看门狗是否触发 |
| `signal` | 提交因哪个信号终止，未发生时为 0 |
| `exit_code` | 提交正常退出时的退出码 |

JSON 中没有 `verdict`，也没有题目阈值和 cgroup 内存统计。内存统计由 judge 向 `MemoryCgroup` 读取。

### 错误通道与三种退出码

沿用现有双通道约定：executor 的 stdout 是 JSON 报告，提交的 stdout/stderr 是指定文件。executor 自身的 stderr 用于执行基础设施诊断。

1. executor 正常完成监控时返回 0，即使提交非零退出、被信号终止或发生超时。
2. executor 启动、重定向、入组、exec 等失败时返回非零并提供诊断，Python 调用层抛出执行错误，由 judge 映射为 SE。
3. 提交自行退出 126/127 属于提交退出事实，不能冒充启动失败。保留现有 setup 错误管道。
4. JSON 无效、缺少必要字段或类型不符时属于执行报告错误，不能用默认的全零报告判为成功。
5. 用户取消单独传播为 `KeyboardInterrupt` 等取消路径，由顶层给出退出码 130，不转换成某个测试点的 RE。

CLI 的退出码保持现有约定：全部 AC 为 0，完成评测但有非 AC（含测试点 SE）为 1，编译失败或前置错误为 2，Ctrl+C 为 130。

## 7. 一个测试点的生命周期

judge 拥有一次测试点事务；MemoryCgroup 实现资源操作。不要把“拥有生命周期”理解成必须把 cgroup 文件操作复制进 judge。

```text
校验参数并固定路径
    ↓
计算保护限制
    ↓
进入 MemoryCgroup 上下文；无 cgroup 模式使用空上下文
    ↓
invoke_executor：启动 → 监控 → 回收 → 返回事实报告
    ↓
停止 cgroup 中仍存活的后代 → 等待清空 → 读取内存统计
    ↓
退出上下文，删除测试点 cgroup
    ↓
classify_execution
    ↓
执行正常才比较答案，得到 AC / WA
```

必须保留以下边界：

- 每个测试点创建独立内存组，不跨测试点复用累计峰值和事件计数。
- 只有提交主进程退出还不够；读取最终统计前停止组内后代，删除前确保组已清空。
- `with` 覆盖 executor 故障、统计失败和 Ctrl+C；`__enter__()` 创建到一半失败时需自行回滚，因为这时不会调用 `__exit__()`。
- 正常路径的 `stop()` 与上下文退出的兜底停止允许重复调用；不依赖成功路径才运行的清理语句。
- `invoke_executor()` 被取消时先请求 executor 终止并等待，超出宽限后强杀并等待。外层 cgroup 清理负责覆盖脱离原进程组的后代。
- 停止或删除失败要报告 SE 和相关路径，不能静默宣称清理完成；保留可诊断的原始错误。取消期间的清理失败也要可见，并保留取消语义。
- 无 cgroup 模式仍调用同一个 executor。它不访问 cgroup，不测量内存，也不使用 RSS 推断 MLE；进程组清理无法覆盖主动 setsid 的后代，这个限制不在此次重构中改变。

示意代码不是完整实现，异常转换和取消处理按上面的契约补齐：

```python
with context as group:
    report = invoke_executor(..., cgroup_procs=group.procs_path if group else None)
    memory = None
    if group is not None:
        group.stop()
        memory = group.memory_result()

verdict, reason = classify_execution(report, memory, limits)
if verdict == "OK":
    verdict = "AC" if compare_output(...) else "WA"
```

`OK` 仅是 judge 内部表示“可以比较答案”的执行结局，不是 executor 输出，也不是一个测试点的最终结果。

## 8. 判定规则集中在 judge

基础设施故障先走 SE 路径。存在完整报告和有效资源统计时，保留当前优先级：

1. cgroup OOM 事件或 OOM kill：MLE。
2. wall 看门狗触发：TLE。
3. cgroup 峰值严格超过题目内存阈值：MLE。
4. CPU 微秒数严格超过题目时间阈值，或收到 SIGXCPU：TLE。
5. 其他终止信号：RE。
6. 非零提交退出码：RE。
7. 否则为内部 OK，再比较答案得到 AC/WA。

`classify_execution()` 是纯函数，不启动进程、不读文件、不操作 cgroup、不打印。混合证据的优先级在这里一处决定。没有 OOM 证据时不凭 SIGKILL 或 stderr 文本猜测 MLE。

## 9. 环境准备与编译的范围

本轮环境准备仍归 judge，保留现有的 cgroup 检查、自动委派、`--in-scope` 和降级行为。`examples/delegated.py` 复用 judge 的 scope 准备函数，避免两份独占性和 controller 检查规则。

自动委派的 exec 链暂不修改。再次 exec 不会让进程离开 manager cgroup；准备环境后继续执行在技术上可行，但这不是当前重构的必要条件。

编译保持当前独立 subprocess 和超时策略，不新增 compile cgroup，不把题目运行限额直接套给编译器。编译隔离和去除中间 exec 互不构成必然前置关系，均留待独立任务。

## 10. 完成标准

- 阅读 `judge_submission()` 能看懂一份提交从编译到汇总的过程。
- 阅读 `judge_case()` 能看懂一次执行的资源生命周期和最终判定顺序。
- C executor 中没有题目、答案、checker、TLE/MLE/AC/WA 等业务规则。
- Python 中只有一处负责启动 executor 和解析报告，没有第二套执行路径。
- MemoryCgroup 不导入 judge，不决定评测结果；judge 不复制其文件操作细节。
- 无 cgroup 与有 cgroup 的执行、取消、清理行为完成回归验证。
- 构建、安装启动器、Docker、测试和仓库根目录下的辅助脚本均使用新结构。
- 旧 Python runner 已删除；教程 Markdown 及其配套示例的迁移不属于此次验收。
