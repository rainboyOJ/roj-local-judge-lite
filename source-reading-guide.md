# 源码阅读指南：从 judge.py 走到受限的提交程序

本文对应 `b0dea84` 版本，关注现有工程的运行方式。行号用于初次定位；代码修改后，请按函数名查找。

先回答最容易混淆的问题：**judge 没有把 executor 本身放进测试点的笼子。executor 留在 manager，进入笼子的是它 fork 出来的子进程。这个子进程完成入组、重定向和限额设置后，通过 exec 变成提交程序。**

这里的“笼子”首先指每个测试点独立的 `case-*` cgroup，用于内存限制、计量和清理后代；CPU、wall 时间等限制还有各自的实现。

如果只想先弄懂入笼过程，直接阅读第 2～5 节，再运行第 9 节的观察实验。

## 1. judge.py 很长，但主线只有几层

不要从 import 一路逐行读到底。先找到“谁组织整次评测”，再跟进“一个测试点怎样执行”。

```text
main()                         解析命令、查找数据、确定执行环境
└── judge_submission()         创建工作目录，编译一次，循环测试点，汇总
    └── judge_case()           确定本测试点的输出路径，要求比较答案
        └── _run_transaction() 管理一次执行的完整生命周期
            ├── MemoryCgroup   创建组、设置内存保护上限、收尾和读统计
            ├── invoke_executor()
            │   └── 启动 executor 进程并读取 JSON 报告
            ├── classify_execution()   根据事实判断 MLE/TLE/RE/OK
            └── compare_output()       执行正常后判断 AC/WA
```

上图大部分是同一 Python 进程中的函数调用。只有标明“启动 executor”的地方才跨到另一个进程。导入 `memory_cgroup.py` 不会产生一个“memory_cgroup 进程”。

建议按下面的顺序读，每次先回答一个问题：

| 次序 | 位置 | 这一遍要看懂什么 |
| --- | --- | --- |
| 1 | [judge.py](judge.py)，`main`，1177 行 | 数据、限制、cgroup 环境怎样交给评测主流程？ |
| 2 | 同文件，`judge_submission`，1123 行 | 为什么只编译一次？谁循环测试点？谁删除工作目录？ |
| 3 | 同文件，`judge_case` / `_run_transaction`，1064 / 989 行 | 一次执行从哪里开始，资源在哪里清理？ |
| 4 | [memory_cgroup.py](memory_cgroup.py)，`MemoryCgroup`，34 行 | case 组怎样建立？哪些文件控制限制和统计？ |
| 5 | [judge.py](judge.py)，`Limits.executor_args` / `invoke_executor`，505 / 672 行 | cgroup 路径怎样传给 C？报告怎样传回来？ |
| 6 | [executor.c](executor.c)，`main` / `run_child`，284 / 164 行 | 哪个进程入组？哪一步开始执行用户代码？ |
| 7 | 同文件，`monitor_child` / `print_result`，221 / 273 行 | 谁负责 wall 超时、等待退出、报告执行事实？ |
| 8 | [judge.py](judge.py)，`classify_execution`，785 行 | 为什么正常退出也可能是 TLE 或 MLE？ |

最后再回头读测试数据查找、命令行参数、格式化输出和错误消息。它们增加了文件长度，却不会改变上面的执行主线。

`execute_program()` 是另一个公共入口：它和 `judge_case()` 共用 `_run_transaction()`，只是没有标准答案，所以不比较输出，正常结果保留为内部状态 `OK`。第 9 节用它观察执行过程。

## 2. 先把“进程树”和“cgroup 树”分开

下面讨论 CLI 自动通过 systemd 委派成功的路径。手工指定 `--cgroup-root` 时，父目录由外部准备，不一定存在名为 `manager` 的组。

### 2.1 进程树回答：谁创建了谁？

执行一个测试点时，核心关系是：

```text
Python：judge.py
└── C：executor
    └── 提交程序：solution（你所说的 main.out）
        └── 提交程序可能创建的后代
```

当前 C++ 编译产物实际叫工作目录中的 `solution`。Python 提交则执行 Python 解释器和提交脚本。对执行器而言，它们都只是一组待执行的命令参数。

### 2.2 cgroup 树回答：谁和谁一起被限制？

同一时刻，分组关系是：

```text
/sys/fs/cgroup/.../run-xxx.scope/       ← 本次委派的父目录 root
├── manager/
│   ├── Python：judge.py
│   └── C：executor
└── case-随机串/                       ← 当前测试点的内存组
    └── 提交程序及留在该组内的后代
```

图中的进程名称表示组内成员，不是实际的文件或子目录。

**进程树中的父子，可以属于 cgroup 树中的兄弟节点。** executor 是提交程序的父进程，并不要求二者始终处于同一个 cgroup。

几个名字的含义也要分清：

| 名称 | 实际是什么 |
| --- | --- |
| `manager` | 项目给一个 cgroup 子目录取的名字，不是额外的管理进程，也不是 Python 类 |
| `root` / `cgroup_root` | 本项目创建 `case-*` 的父目录；自动委派时是 scope 目录，不是 `manager`，也不是系统整个 cgroup 树的根 |
| `MemoryCgroup` | Python 对象，封装对 cgroup 文件的操作 |
| `executor` | `executor.c` 编译出的可执行文件，也是监控提交程序的父进程 |
| C 注释中的 `helper` | 当前 executor 的称呼；不表示还要启动另一个 helper 程序 |

## 3. manager 是怎样出现的？

阅读 `prepare_delegated_scope()`，先看它的四个关键动作。下面省略检查和错误处理：

```python
manager = root / "manager"
manager.mkdir(exist_ok=True)
(manager / "cgroup.procs").write_text(str(os.getpid()))
(root / "cgroup.subtree_control").write_text("+memory")
os.environ["ROJ_JUDGE_CGROUP_ROOT"] = str(root)
```

### 3.1 systemd 先提供可以管理的 scope

当默认 cgroup 目录不可用且允许自动委派时，`main()` 会尝试探测委派能力，然后通过 `run_delegated()` 重跑 CLI。命令结构大致如下，普通用户带 `--user`：

```text
systemd-run --user --scope -p Delegate=yes --
    python3 judge.py --in-scope --
        python3 judge.py <原来的参数> --no-delegate
```

`Delegate=yes` 请求 systemd 委派该组的管理；代码随后还要检查 scope 是否可写、是否只有本进程、是否有 memory controller。不是任意找到一个目录就直接改动。

`try_auto_delegate()` 的探测和真正评测使用不同的新 scope。探测成功后，真实 scope 仍会执行自己的准备检查。

### 3.2 把当前 Python 进程搬到 manager

`--in-scope` 分支调用 `prepare_delegated_scope()`。此时尚未启动 C executor，`os.getpid()` 是这个 Python 进程的 PID。

因此，下面这句搬走的是 Python 自己：

```python
(manager / "cgroup.procs").write_text(str(os.getpid()))
```

源码注释里“把执行器放进 manager”是宽泛的叫法。阅读时应以实际写入的 PID 为准，不能理解成这里已经创建了 C executor。

搬走它是为了随后启用父目录的 `+memory`。这里的 scope 是非根 domain cgroup；它向子组分配 memory 资源时，自身不能直接容纳进程。这是 cgroup v2 的 [No Internal Process Constraint](https://docs.kernel.org/admin-guide/cgroup-v2.html#no-internal-process-constraint)。

```text
搬迁前：scope 自身包含 Python

搬迁后：scope 自身无进程，可开启 +memory
        └── manager 包含 Python
```

“scope 自身无进程”指它自己的 `cgroup.procs` 为空，不是要求所有子组也为空。

### 3.3 换成正式评测程序，继续留在 manager

准备完成后，`run_in_scope()` 用 `os.execvp()` 执行 `--` 后的正式命令。

这里的 exec 替换当前进程运行的程序，不是再 fork 一个子进程。PID 和所在 cgroup 保留，环境变量也传下去，所以正式 `judge.py` 已经位于 manager，并能从 `ROJ_JUDGE_CGROUP_ROOT` 找到 scope 父目录。

`--no-delegate` 避免再次进入自动委派流程。之后编译器和 executor 作为 judge 启动的子进程，初始也位于 manager。

注意：manager 只是不受某个 `case-*` 的限制，仍受祖先 cgroup 的限制。它也不会因为名字叫 manager 就自动获得管理权限；能操作 cgroup 文件，依赖实际的委派和文件权限。

## 4. 创建一个 case，只是准备笼子，还没有把程序放进去

现在回到 `_run_transaction()`。下面是它与入笼直接相关的代码骨架，省略其他参数：

```python
context = MemoryCgroup(root, protection.memory_max_bytes)
with context as group:
    command = [
        str(executor),
        *limits.executor_args(group.procs_path),
        # 降权参数、工作目录、输入输出路径、提交命令……
    ]
    report = invoke_executor(command, ...)
```

实际代码还支持 `isolated=False`，此时使用 `nullcontext()`。

进入 `with` 会调用 `MemoryCgroup.__enter__()`。它创建 `root/case-随机串`，写入配置，并检查后续需要的内核接口是否存在。

| 文件 | 当前代码怎样使用 |
| --- | --- |
| `memory.max` | 写入内存保护上限，单位字节 |
| `memory.swap.max` | 写入 `0`，禁用该组的 swap 额度 |
| `memory.oom.group` | 写入 `1`，启用组级 OOM 处理 |
| `memory.peak` | 执行后读取内存峰值 |
| `memory.events` | 执行后读取 OOM 相关计数 |
| `cgroup.kill` | 收尾时写入 `1`，终止组内进程及后代组内进程 |

这些文件由 cgroup 文件系统提供；`mkdir()` 创建的是一个内核管理的组，接口文件会随之出现，不是 Python 逐个创建出来的普通文本文件。

此时 case 组还是空的。`group.procs_path` 也只是返回路径：

```text
/sys/fs/cgroup/.../run-xxx.scope/case-随机串/cgroup.procs
```

`Limits.executor_args()` 把它作为 `--cgroup-procs <路径>` 传给 C executor；`parse_options()` 按键值对保存到 `options.cgroup_procs`。

所有执行参数都是 `--名字 值` 形式，与出现顺序无关：Python 侧改名或插入参数时不会错位，未识别的选项、缺少必需项、重复选项都会在启动阶段直接报错退出。

**路径传过去，只是告诉 C“稍后往哪里入组”。传参数这一步没有迁移任何进程。**

接下来 `invoke_executor()` 用 `subprocess.Popen()` 启动 executor。executor 继承 judge 的 cgroup，因此仍在 manager。代码中的 `start_new_session=True` 是建立新会话，与进入 case cgroup 是不同操作。

## 5. 真正入笼：C 子进程向 cgroup.procs 写入 0

这是整条执行链最值得仔细读的一段。

### 5.1 fork 后有两个执行 C 代码的进程

`executor.c` 的 `main()` 中：

```c
pid_t child = fork();
if (child < 0) fail("fork");
if (child == 0) {
  close(error_pipe[0]);
  run_child(&options, error_pipe[1], helper_pid);
}
```

假设 judge、executor 和新子进程的 PID 分别是 1000、1001、1002：

| 时刻 | PID 1000 | PID 1001 | PID 1002 |
| --- | --- | --- | --- |
| 启动 executor 后 | judge，manager | executor，manager | 尚不存在 |
| executor fork 后 | judge，manager | executor 父分支，manager | executor 子分支，manager |
| 子分支写入 cgroup.procs 后 | judge，manager | executor 父分支，manager | 执行 `run_child`，case |
| 子分支 exec 后 | judge，manager | executor 监控子进程，manager | 提交程序，case |

刚 fork 出来的 1002 还没有执行你的 `main.out`；它正在执行 executor 的准备代码。

### 5.2 写入者把自己迁入 case

`run_child()` 中的原始代码是：

```c
if (options->cgroup_procs != NULL) {
  int group_fd = open(options->cgroup_procs, O_WRONLY | O_CLOEXEC);
  if (group_fd < 0) setup_failed(error_fd, "open cgroup.procs");
  if (write(group_fd, "0", 1) != 1) setup_failed(error_fd, "join cgroup");
  close(group_fd);
}
```

真正产生迁移的是 `write(group_fd, "0", 1)`。这里的 `0` 表示写入进程自身，即示例中的 PID 1002，不是 PID 为 0 的某个系统进程。fork 时子进程继承父进程的 cgroup，而写入 `cgroup.procs` 可以改变其归属；相关语义见 [cgroup v2 核心接口](https://docs.kernel.org/admin-guide/cgroup-v2.html#core-interface-files)。

这段代码只在子分支执行，因此不会把 executor 父进程 1001 搬进去，也不会把 judge 1000 搬进去。

迁移还发生在 `setuid()` 降权之前：此时子进程继承了执行器的权限，可以访问已经委派的 cgroup 文件；如果先降为 nobody，入组可能会因权限不足失败。

### 5.3 完成准备之后，才 exec 提交程序

`run_child()` 的实际顺序是：

```text
恢复信号处理，建立独立进程组
→ 加入 case cgroup
→ 切换工作目录
→ 重定向 stdin / stdout / stderr
→ 设置栈、文件大小、CPU 等 rlimit
→ 按配置降权
→ 设置父进程死亡信号并检查父进程
→ execvp(提交命令)
```

`execvp()` 把 PID 1002 正在运行的 C 准备程序替换成提交程序，成功后不会返回到下一行；这个 PID 仍留在 case 组中。

因此，在开始执行提交代码之前，它已经入组且设置好限额。若入组、重定向或限额设置失败，会通过错误管道报告并退出，后面的提交命令不会继续执行。

这也解释了为什么不采用“Python 先启动提交程序，再把它搬进 case”的顺序：那会让用户代码有机会在迁移前运行。当前代码让被测子进程自己先完成准备，再 exec。

### 5.4 为什么 executor 要留在外面？

executor 的父分支还要运行 `monitor_child()`：检查 wall 截止时间、处理取消、调用 `wait4()` 收集退出信息，最后输出 JSON。

让这个监控进程留在 manager，可以使它在 case 触发 OOM 或被 `cgroup.kill` 清理时继续负责报告和回收。Python 调度器也不会消耗该测试点的内存额度。

所以当前运行链已经是：

```text
judge.py → executor → 提交程序
```

不存在额外的 `executor.py → helper` 一层。C 中的 fork 是把“留在外面监控”和“进入限制后运行”分成两个进程，并非新增了一层业务模块。

## 6. 限制执行与判定结果，是两个动作

阅读 `Limits`、`make_protection_limits()` 和 `classify_execution()` 时，要分别问：哪个值用于阻止程序失控，哪个值用于最终判题？

以 `Limits(time_ms=1000, memory_kb=128 * 1024)` 为例，其他字段使用默认值：

| 项目 | 实际保护机制 | 最终判定依据 |
| --- | --- | --- |
| 内存 | case 的 `memory.max` 为 144 MiB，即 128 + 16 MiB 余量 | 峰值超过 128 MiB，或出现 OOM 证据，判 MLE |
| CPU 时间 | `RLIMIT_CPU` 的 soft 为 2 秒，hard 为 3 秒 | 报告的原始 CPU 微秒数超过 1000ms，或收到 `SIGXCPU`，判 TLE |
| wall 时间 | executor 看门狗在超过 1500ms 时终止提交进程组 | `timed_out` 为真，作为 TLE 证据 |

CPU soft 值来自 `(1000 + 200 + 999) // 1000`，再由 C 将 hard 值设为 soft 加 1 秒。wall 默认是题目时间加 500ms，它包含等待、I/O 和调度，不等于 CPU 时间。

因此，程序用了 1100ms CPU 后正常退出，仍可判 TLE；内存峰值达到 135 MiB 后正常退出，仍可判 MLE。保护上限的余量没有放宽题目标准。

这些限制并不全部来自 cgroup。当前 `MemoryCgroup` 使用 memory controller；CPU 使用 `setrlimit()`，wall 使用 executor 的监控循环，栈和输出文件大小也使用 rlimit。

这里实现的是资源控制与执行管理。代码没有在这条路径中建立文件系统或网络 namespace；理解“笼子”时，不应把它理解成一个完整容器。

## 7. 程序结束后，谁报告、谁清理、谁判题？

### 7.1 两条报告来源，另加一份用户输出

```text
executor 的 stdout ── JSON ──→ ExecutionReport
case 的内核统计文件 ─────────→ MemoryResult
                              │
                              ▼
                       classify_execution()
                              │
                       MLE / TLE / RE / OK
                              │ 仅 OK 且有标准答案
                              ▼
提交程序输出文件 ───────→ compare_output() ──→ AC / WA
```

`ExecutionReport` 包含 CPU 时间、wall 时间、退出码、信号和 RSS 等事实。`MemoryResult` 包含 cgroup 峰值及 OOM 计数。最终内存判定使用后者，不能拿报告里的 RSS 替代。

提交程序的 stdout 已经重定向到测试点输出文件；executor 自己的 stdout 留给 JSON。因此提交打印任意文本，都不会混进执行报告。

### 7.2 按实际代码理解收尾顺序

`_run_transaction()` 的正常路径是：

```text
进入 with：创建 case 并配置
→ 等待 executor 返回报告
→ group.stop()：终止残余后代并等待 populated=0
→ 读取 memory.peak / memory.events
→ classify_execution()，构造 CaseResult
→ 退出 with：__exit__ 再确保停止，并删除 case 目录
→ 若结果为 OK 且有标准答案，比较输出得到 AC/WA
→ 返回结果
```

注意这里以函数体为准：当前代码是在 `with` 内先做执行判定，退出 `with` 后再比较答案。函数开头“删组 → 判定”的概述没有精确表达这处顺序。

为什么主程序已经退出，还要 `group.stop()`？因为直接子进程退出不代表所有后代都退出。executor 的进程组清理覆盖普通同组后代，cgroup 收尾还能处理通过 `setsid()` 离开原进程组、但仍留在 case cgroup 的后代。

统计必须在删除组之前读取；读出的数值已保存在 Python 对象中，删除目录不会使 `CaseResult` 丢失数据。每个测试点新建随机组，使不同测试点的内存峰值和事件计数分开。

### 7.3 出错时沿同一条边界收尾

`with MemoryCgroup(...)` 让执行失败或取消时也经过清理。启动准备失败、executor 故障或报告无效会作为基础设施错误处理，通常映射为 `SE`；用户程序非零退出则通常判 `RE`，二者由 C 的私有错误管道区分。

清理失败不会被当成正常成功。代码会报告该错误；若原本已有异常，清理诊断附加到原异常。`KeyboardInterrupt` 继续向顶层传播，停止整次评测。

再往外一层，`judge_submission()` 的 `finally` 清理工作目录，除非指定保留。工作目录与 case cgroup 是两类资源，由不同层分别负责。

最终执行判定的优先级在 `classify_execution()` 一处定义：

```text
OOM → wall 超时 → 内存峰值超限 → CPU 超限 / SIGXCPU
    → 其他终止信号 → 非零退出码 → OK
```

所以不能只看到 `SIGKILL` 就认定 MLE，也不能只看到退出码为 0 就认定 AC。

## 8. 第二遍阅读时，再补齐这些支线

| 你想回答的问题 | 去哪里读 |
| --- | --- |
| 测试数据在哪里？题目限制怎么读取？ | `resolve_testdata`、`load_cases`、`load_problem_meta` |
| C++ 和 Python 提交怎样转换成执行命令？ | `detect_language`、`compile_submission` |
| 哪些路径会被拒绝，如何避免覆盖标准答案？ | `_prepare_execution`、`_check_stream_paths`、`_check_readonly_targets` |
| executor 收到哪些参数？ | `Limits.executor_args` 与 `_run_transaction` 拼的命令，对照 C 的 `parse_options` |
| 什么算合法的执行报告？ | `ExecutionReport.from_json` |
| 怎样切换普通比较与外部 checker？ | `resolve_checker`、`compare_output` |
| 没有 cgroup 会发生什么？ | `main` 的执行模式分支、`_run_transaction` 的 `nullcontext` 分支 |
| 取消为什么能停止提交程序？ | `invoke_executor`、C 的信号处理和 `monitor_child`、`MemoryCgroup.__exit__` |

`--no-cgroup` 时，代码不传 `--cgroup-procs`，C 跳过写 `cgroup.procs`；此时没有本项目创建的 case 内存隔离，也没有对应内存统计。CPU 和 wall 保护仍存在，但不能据 RSS 补判 MLE。“不使用 cgroup”在这里指不用项目的 case 组，并不代表进程脱离了系统本来的 cgroup 层级。

## 9. 亲眼验证：executor 在 manager，提交程序在 case

下面的实验不需要题目数据，也不修改源码。在仓库根目录运行，使用现有的 `--in-scope` 准备入口与 `execute_program()`。提交脚本只报告自己及父进程的 cgroup。

需要 Linux cgroup v2 和可用的 systemd 用户委派。普通用户使用下面的 `--user`；root 在可用的系统管理器下运行时去掉它。本实验明确设置 `drop_privileges=False`，便于用当前身份观察进程，不代表 CLI 的默认降权配置。

```bash
make
systemd-run --user --quiet --scope -p Delegate=yes -- python3 judge.py --in-scope -- python3 - <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path
from judge import Limits, Verdict, execute_program


def cgroup_of(pid):
    line = next(s for s in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                if s.startswith('0::'))
    return line.split('::', 1)[1]


root = Path(os.environ['ROJ_JUDGE_CGROUP_ROOT'])
manager = cgroup_of(os.getpid())
print('judge cgroup:', manager)
print('case parent:', root)
previous = set(root.glob('case-*'))
program = '''
import json, os
from pathlib import Path

def cgroup_of(pid):
    line = next(s for s in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                if s.startswith('0::'))
    return line.split('::', 1)[1]

own = cgroup_of(os.getpid())
path = Path('/sys/fs/cgroup') / own.lstrip('/')
print(json.dumps({
    'submission_pid': os.getpid(),
    'submission_cgroup': own,
    'executor_pid': os.getppid(),
    'executor_cgroup': cgroup_of(os.getppid()),
    'memory_max': (path / 'memory.max').read_text().strip(),
}))
'''
with tempfile.TemporaryDirectory(prefix='read-judge-') as directory:
    work = Path(directory)
    source = work / 'observe.py'
    source.write_text(program)
    input_path = work / 'input.txt'
    input_path.write_text('')
    output = work / 'output.txt'
    result = execute_program(
        [sys.executable, str(source)], input_path, output,
        Limits(time_ms=1000, memory_kb=128 * 1024),
        work_dir=work, isolated=True, drop_privileges=False,
    )
    assert result.verdict is Verdict.OK, (result.verdict, result.message)
    facts = json.loads(output.read_text())
    print(json.dumps(facts, indent=2))
    case_path = Path('/sys/fs/cgroup') / facts['submission_cgroup'].lstrip('/')
    assert manager.endswith('/manager')
    assert facts['executor_cgroup'] == manager
    assert case_path.parent == root and case_path.name.startswith('case-')
    assert facts['memory_max'] == str(144 * 1024 * 1024)
    assert not case_path.exists()
    assert set(root.glob('case-*')) == previous
    print('verified: executor in manager; submission in case; case removed')
PY
```

观察三个事实：

1. `judge cgroup` 和 `executor_cgroup` 都以同一个 `/manager` 结尾。
2. `submission_cgroup` 以同级的 `/case-随机串` 结尾，读取到的 `memory_max` 为 `150994944`，即 144 MiB。
3. `execute_program()` 返回时，该 case 目录已经不存在，最后一行断言验证通过。

这里的“judge”是调用库接口的观察脚本进程，承担与正式评测 Python 进程相同的调用角色。实验经过真实的 `MemoryCgroup → executor → 提交程序` 路径，没有模拟入组。

本机已运行验证上述实验。scope 名、随机串和 PID 每次会变化。

## 10. 读完后，试着自己指出这四处代码

不用记住全部函数，先能在源码中找到下面四个动作：

1. **准备管理环境**：`prepare_delegated_scope()` 把当前 Python 进程迁入 manager，并给 scope 启用 `+memory`。
2. **准备测试点资源**：`MemoryCgroup.__enter__()` 创建 case 并设置内存保护上限。
3. **让提交进程入组**：`run_child()` 写入 `cgroup.procs`，然后设置其余限制并 exec。
4. **汇合事实并收尾**：`_run_transaction()` 结合 executor 报告和 cgroup 统计，通过上下文管理清理 case，完成执行判定及答案比较。

这四处连接起来，就是这套代码最主要的控制流程。
