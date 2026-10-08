# py-judge-runner

Linux 单程序资源执行器：启动已存在的程序，重定向标准流，用 cgroup v2 管理内存，
用 `wait4` 统计 CPU，用 wall-clock 看门狗防止卡死，最后返回 JSON 结果。
不编译提交、不比较答案。`OK` 只表示正常执行。

## 目录

- [快速使用](#快速使用)
  - [A. 评测我自己的代码](#a-评测我自己的代码最常用)
  - [B. 当作执行器或库](#b-把-runner-当作执行器或库)
- [先理解两个限制](#先理解两个限制)
- [构建与权限](#构建与权限)
- [Python 接口](#python-接口)
- [结果与分类](#结果与分类)
- [读代码的顺序](#读代码的顺序)
- [local_judge.py 独立使用说明](#local_judgepy-独立使用说明)
  - [基本用法](#基本用法)
  - [local_judge.py 执行逻辑](#local_judgepy-执行逻辑)
- [安装脚本](#安装脚本)
- [其他参数](#其他参数)
- [验证](#验证)

## 快速使用

先看你要做哪件事，两条路径互不影响。

### A. 评测我自己的代码（最常用）

不用启 `judge_server`，也不要求事先配好 cgroup。

**方式一：就用本仓库里的这份**

```bash
cd roj-local-judge-lite
make                                            # 首次构建 C helper
python3 local_judge.py --pid 1000 solution.cpp  # 跑 testData/1000/data 下的全部测试点
python3 local_judge.py --list                   # 看本地有哪些题
```

**方式二：一行命令装到用户目录，随处可用**

```bash
curl -fsSL https://raw.githubusercontent.com/rainboyOJ/roj-local-judge-lite/master/install.sh | bash
```

装完得到 `~/.local/share/py-judge-runner/` 和启动器 `~/.local/bin/py-judge-runner`
（安装脚本会自己克隆、构建、跑冒烟测试；详细参数见 [安装脚本](#安装脚本)）：

```bash
cd 你的项目                # 目录下有 testData/ 就行
py-judge-runner --list
py-judge-runner --pid 1000 solution.cpp
```

装出来的包不含题目数据，所以测试数据靠自动查找：依次看包上级目录、当前目录、
当前目录的上级，都没有就用 `--testdata` 指定。

上面两种方式的输出一样：

```text
题目 1000  A+B问题
提交 solution.cpp（cpp）
限制 CPU 1000ms / 内存 128MiB / wall 1500ms
执行 cgroup 隔离，root=/sys/fs/cgroup/...
比较 内置按行比较（未找到 /judge/checker/fcmp2）

编译通过
  #1   problem1     AC         2ms    0.8MiB
  #2   problem2     AC         1ms    0.5MiB
  ...
结果：AC  通过 10/10  用时 0.05s
```

`--pid` 对应 `testData/<pid>/data`。隔离默认依次尝试 cgroup v2、
`systemd-run` 委派 scope，都不可用时降级为 wall 超时加 `RLIMIT_CPU` 并明确提示。
完整参数见 [local_judge.py 独立使用说明](#local_judgepy-独立使用说明)。

### B. 把 runner 当作执行器或库

自己控制输入输出和判定时直接用 `runner.py`。它默认要求**已委派且启用 memory
controller 的 cgroup v2 父目录**，否则直接返回 `SYSTEM_ERROR`（不会退回无内存
限制的运行）。最省事的方式是用自带示例包一层：

```bash
systemd-run --user --quiet --scope -p Delegate=yes -- \
  python3 examples/delegated.py python3 runner.py \
  --input 1.in --output 1.user.out --time 1000 --memory 128 -- ./solution
```

或者先自己准备好委派目录（见 [构建与权限](#构建与权限)），再直接调用：

```bash
python3 runner.py --cgroup-root /sys/fs/cgroup/your-delegated-parent \
  --input 1.in --output 1.user.out --time 1000 --memory 128 -- ./solution
```

结果是一份 JSON：

```json
{
  "verdict": "OK",
  "cpu_time_us": 1265,
  "cpu_time_ms": 1,
  "real_time_ms": 9,
  "memory_kb": 768,
  "memory_peak_bytes": 786432,
  "oom_events": 0,
  "timed_out": false,
  "exit_code": 0,
  "message": ""
}
```

也可以在 Python 里当成库调用，见 [Python 接口](#python-接口)。
注意：只有 A（`local_judge.py`）具备 cgroup 不可用时的自动降级。
`runner.py` 可以显式使用 `--no-cgroup`，库调用则传 `use_cgroup=False`：

```bash
python3 runner.py --no-cgroup --input 1.in --output 1.user.out -- ./solution
```

两种模式都需要构建好的 `runner_helper`。关闭 cgroup 后仍有 CPU、wall、栈、
输出和进程数限额，但不限制或计量内存，无法判定 MLE。

## 先理解两个限制

**题目阈值用于最终判定，保护上限用于阻止失控。** 两者分开，让稍微超限的程序
可以完成执行，再根据真实统计判断；严重超限则由内核或看门狗终止。

| 资源 | 默认题目阈值 | 默认保护设置 | 最终判定 |
|---|---|---|---|
| CPU | 1000ms | 加 200ms，再向上取整到秒：soft 2秒，hard 3秒 | 原始 CPU 微秒数 > 1000000，或 SIGXCPU → TLE |
| 内存 | 128MiB | 加 16MiB：`memory.max = 144MiB` | `memory.peak > 128MiB` 或 OOM 事件 → MLE |
| wall | 独立保护限制 | 默认 CPU 题目阈值 + 500ms，即 1500ms | 看门狗触发 → TLE |

CPU soft 到期发 SIGXCPU，hard 比 soft 多一秒用于兜底。`RLIMIT_CPU` 只支持整数秒，
所以“200ms 余量”不会产生精确的 1200ms 内核截止时刻。wall 包括 I/O、调度、等待，
与 CPU 是不同的量；可用 `--wall-time` 单独设置。

内存采用 **cgroup 整个提交的内存口径**，包含后代进程、文件缓存和部分内核内存。
共享页按内核的归属记账，不是把各进程 RSS 简单相加。关闭本组 swap，避免换出后
内存计量口径变化。上级 cgroup 的限制仍会生效，应给执行器及各提交留足总资源。

`RLIMIT_AS` 与 `--as-factor` 已删除，避免虚拟地址空间先触顶而使实际内存判定失真。
`rss_kb` 仍保留作诊断，但不用于 MLE 判定。

## 构建与权限

需要 Linux、Python 3.8+，无第三方 Python 依赖。启用 cgroup 时还需要 cgroup v2，
以及 `memory.peak`、`memory.swap.max`、`memory.oom.group`、`cgroup.kill` 接口。
首次构建 helper 需要 C 编译器和 make，运行时不需要编译器。

```bash
cd roj-local-judge-lite
make
```

启用 cgroup 时，调用者必须提供**已委派、可创建子组、且为子组启用 memory controller** 的父目录。
runner 只创建和清理自己命名为 `case-*` 的子组，不修改其他组。
没有权限或接口不完整时返回 `SYSTEM_ERROR`，不会退回 RSS 或无内存限制的运行。

本机支持 systemd 用户委派，可通过示例启动一个专用 scope：

```bash
systemd-run --user --scope -p Delegate=yes -- \
  python3 examples/delegated.py python3 runner.py \
  --input 1.in --output 1.user.out --time 1000 --memory 128 -- ./solution
```

`examples/delegated.py` 先把自身及随后启动的执行器放入 `manager` 叶子组，再为 scope
父目录启用 memory controller。这样执行器与各提交是兄弟组，执行器自己的内存
不会计入提交。该脚本仅用于自身独占的新 scope。

生产部署可由服务管理器预先提供委派目录，然后直接执行：

```bash
python3 runner.py --cgroup-root /sys/fs/cgroup/your-delegated-parent \
  --input 1.in --output 1.user.out \
  --time 1000 --cpu-slack-ms 200 --memory 128 --memory-slack 16 -- ./solution
```

也可设置环境变量 `ROJ_JUDGE_CGROUP_ROOT`。未指定时使用 `/sys/fs/cgroup/roj-judge`；
这个默认路径也必须由部署环境预先准备，runner 不会自行配置系统目录。

## Python 接口

```python
from pathlib import Path
from runner import Limits, run_case

result = run_case(
    ["./solution", "argument"],
    Path("1.in"),
    Path("1.user.out"),
    Limits(
        time_ms=1000,
        cpu_slack_ms=200,
        memory_kb=128 * 1024,
        memory_slack_kb=16 * 1024,
    ),
    cwd=Path("/work"),
    cgroup_root=Path("/sys/fs/cgroup/your-delegated-parent"),
)
print(result.to_dict())
```

`use_cgroup` 默认为 `True`，cgroup 不可用时返回 `SYSTEM_ERROR`。
显式传 `use_cgroup=False` 时忽略 `cgroup_root`，执行仍经过同一个 helper。
此时 `memory_kb`、`memory_peak_bytes`、OOM 计数均为 0，含义是**未测量**，
不是程序没有使用内存；`rss_kb` 仍可作辅助诊断。

文件路径相对于调用者当前目录，命令中的 `./solution` 相对于 `cwd`，命令名支持 PATH。
参数原样传递；不把命令拼成 shell 字符串。stdout 写入 `--output`，stderr 默认是
`OUTPUT.err`，可用 `--stderr` 覆盖。三个标准流不能指向同一文件。

root 默认设置重定向及限制后降权至 nobody（UID/GID 65534），清除附加组；
普通用户默认不降权。可用 `--uid`、`--gid`、`--no-drop-privileges` 控制。
降权后的用户必须能访问 cwd 和可执行文件。默认最小环境，`--inherit-env` 才继承全部环境。

## 结果与分类

```json
{
  "verdict": "OK",
  "cpu_time_us": 2500,
  "cpu_time_ms": 3,
  "real_time_ms": 4,
  "memory_kb": 2048,
  "memory_peak_bytes": 2097152,
  "rss_kb": 3072,
  "oom_events": 0,
  "oom_kills": 0,
  "timed_out": false,
  "signal": 0,
  "exit_code": 0,
  "message": "",
  "output_path": "/work/1.user.out",
  "stderr_path": "/work/1.user.out.err"
}
```

以上数值仅为字段示例。CPU 判断使用微秒，内存判断使用字节，避免展示时的取整影响
边界判定。`memory_kb` 是向上取整到 KiB 的 cgroup 峰值；`rss_kb` 是 `wait4` 的辅助统计。
`oom_events` 和 `oom_kills` 分别来自该次新建组的 `memory.events` 中 `oom` 和 `oom_kill`。

判定顺序：启动或管理失败 → SYSTEM_ERROR；OOM 证据 → MLE；wall 超时 → TLE；
内存峰值超题目阈值 → MLE；CPU 超题目阈值或 SIGXCPU → TLE；其他信号/非零退出 → RE；
否则 OK。多个原因同时发生时采用上述顺序。
即使程序捕获了分配失败，OOM 事件也不会消失。没有 OOM 证据时，不根据单独的 SIGKILL
或用户可控的 stderr 文案猜测 MLE。

CLI 退出码：OK 为 0，TLE/MLE/RE 为 1，SYSTEM_ERROR/参数错误为 2，中断为 130。
参数错误写 stderr，不产生 JSON。

## 读代码的顺序

本包采用 **Python API + 独立 C helper**。两种模式只有 cgroup 能力不同，执行流程相同：

```text
local_judge.py：编译 → 调用 run_case → 比对答案
                          ↓
runner.py：校验配置 → 可选的 cgroup 生命周期 → 收集资源 → 判定
                          ↓
runner_helper：fork → 父进程监控 / 子进程准备并 exec
```

1. `runner.py: run_case()`：准备参数；有 cgroup 时进入 `MemoryCgroup`，否则进入无操作的 `nullcontext`。
2. `memory_cgroup.py`：创建子组、设置保护上限、停止全部后代、读取峰值/OOM、删除子组。
3. `runner_helper.c: run_child()`：建进程组 → 可选入 cgroup → 重定向 → 限额 → 降权 → exec。
4. `monitor_child()`：一个 `wait4` 循环处理完成、wall 超时和取消，连 exec 前的准备阶段也受监控。
5. `runner.py: _set_verdict()`：按原始题目阈值判定。

Python 启动独立的小型 C helper，再由 helper fork 用户程序，避免辅助 RSS 统计受
Python 父进程大内存影响。cgroup 内存计量不依赖这个 RSS 修正，也不需要 `/proc` 采样。

helper 的 stdout 专门传 JSON 资源报告，用户 stdout 写入输出文件。另有一条私有管道
只报告 setup 失败的操作名和 errno；exec 成功时写端自动关闭。因此用户自己退出 126/127
仍是 RE，不会被误判为工具启动失败。helper 只报告事实，最终 verdict 由 Python 决定。

正常退出、wall 超时和 Ctrl+C 都由 helper 清理提交进程组并回收直接子进程。
启用 cgroup 时，正常、超时、setup 失败、Ctrl+C 还会经过 cgroup 的退出清理。`cgroup.kill` 覆盖包括
`setsid` 后代在内的整个组，等待 `populated=0` 后才删除目录。每次创建新组，峰值与事件
自然从零开始，不需要重置历史计数。清理超时会报告错误并保留目录供排查。
禁用 cgroup 时只清理同一进程组，主动通过 `setsid` 脱离的后代不在此保证内。

这仍是资源执行器，不是完整安全沙箱；文件访问、网络以及阻止恶意操作自身权限可访问的
cgroup 需要外部隔离。CPU 仍由直接子进程的 `wait4`/`RLIMIT_CPU` 管理，不使用 cgroup CPU
配额，也不把它当成任意进程树的 CPU 总额。

## local_judge.py 独立使用说明

本目录提供独立的 `runner.py` 底层执行器和面向开发者的单机测试脚本 `local_judge.py`，不依赖 judge_server。
它可以自动寻找测试数据目录下的用例，对单份代码进行编译、运行测试，并比对答案（提供最终的 AC/WA/TLE/MLE 等结论）。

### 基本用法

```bash
# 默认在仓库的 testData/ 下找题目 1000 的数据（相对本目录即 ../testData）
python3 local_judge.py --pid 1000 solution.cpp

# 指定测试数据目录
python3 local_judge.py --pid 1000 solution.py --testdata /path/to/testData

# 修改时间限制(1000ms)和空间限制(256MB)
python3 local_judge.py --pid 1000 solution.cpp --time 1000 --memory 256

# 查看本地有哪些题目、各有多少测试点
python3 local_judge.py --list
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--pid <编号>` | 题目编号，对应 `testData/<pid>/data` |
| `--list` | 列出可用题目、测试点数量与限制 |
| `--testdata <dir>` | 测试数据根目录，默认本目录的 `../testData` |
| `--time <ms>` / `--memory <MiB>` | 覆盖题目 `config.json` 的 `time` / `memory` |
| `--lang auto\|cpp\|python` | 提交语言，默认按后缀判断 |
| `--checker auto\|none\|<path>` | 输出比较器，默认 `auto` |
| `--no-cgroup` | 跳过 cgroup，直接走降级模式 |
| `--keep-work-dir` | 保留临时工作目录，便于查看输出和编译日志 |

### local_judge.py 执行逻辑

1. **编译 (Compile)**：C++ 用 `g++ -std=c++17 -O2 -DONLINE_JUDGE`（与 judge_server 同一组参数），Python 用 `py_compile` 做语法检查，把解释型语言统一映射到“编译阶段”。
2. **执行 (Run)**：对 `data` 目录里每个配对的 `.in` 调用底层 `runner.py`。隔离依次尝试 cgroup v2、`systemd-run` 委派 scope，都不可用时降级为 wall 超时加 `RLIMIT_CPU`，并明确提示 MLE 无法判定。
3. **比对 (Compare)**：把用户输出与同名的标准 `.out` 比较。默认优先用 `/judge/checker/fcmp2`（若已部署），否则按行比较，忽略行尾空白和末尾空行。
4. **汇总 (Summary)**：终端打印每个测试点的耗时、内存和状态（AC / WA / TLE / MLE / RE）。

隔离与降级都调用 `run_case()`，不再维护 Python `preexec_fn` 执行路径。
统一后降级模式也会在正常退出和 Ctrl+C 时清理进程组，应用相同的资源限额；
root 运行时同样默认降权到 nobody，需保证源文件、可执行文件和工作目录可访问。

退出码：`0` 全部 AC，`1` 有非 AC 结果，`2` 编译失败或工具/环境错误。

## 安装脚本

`install.sh` 从 GitHub 克隆本仓库、构建 C helper、跑冒烟测试，
最后安装到用户目录并在 `~/.local/bin` 放一个启动器：

```bash
# 默认装 master 到 ~/.local/share/py-judge-runner
curl -fsSL https://raw.githubusercontent.com/rainboyOJ/roj-local-judge-lite/master/install.sh | bash

# 带参数：`| bash` 时参数要放在 -s -- 后面
curl -fsSL <同上> | bash -s -- --ref v0.1.0 --force
```

| 参数 | 说明 |
|---|---|
| `--ref <ref>` | 安装的分支、tag 或 commit，默认 `master` |
| `--dir <path>` | 安装目录，默认 `~/.local/share/py-judge-runner` |
| `--bin-dir <path>` | 启动器目录，默认 `~/.local/bin` |
| `--repo <owner/name>` | 仓库，便于装自己的 fork |
| `--mirror <prefix>` / `--no-mirror` | 镜像前缀，默认 `https://gh-proxy.com`，直连失败才回退 |
| `--testdata <path>` | 只用于安装后的冒烟测试 |
| `--no-build` / `--no-smoke` / `--no-launcher` | 跳过对应步骤 |
| `-f, --force` | 目标目录已存在时覆盖 |

行为说明：

- **不做交互提问**：`curl | bash` 时 stdin 是脚本本身，所有选择只能由参数决定。
- **失败不留半个目录**：先在同级暂存目录里克隆、构建、冒烟，全部成功才整体搬进目标目录。
- **冒烟测试**包含模块导入、`make check`（逻辑用例及无 cgroup 的真实执行）、cgroup 可用性探测，
  以及能找到测试数据时跑一次 `local_judge.py --list`。
- 需要 `git` 和 `python3 >= 3.8`；构建时还需要 `make` 和一个 C 编译器（`--no-build` 可跳过）。
- `--no-build` 只跳过构建，执行前仍需自行提供与源码配套的 helper；降级模式也需要它。
- 目标目录已存在时默认报错退出，加 `--force` 才覆盖。

安装脚本本身也在包里，所以装完可以直接用
`~/.local/share/py-judge-runner/install.sh` 重装或装到别的目录。

## 其他参数

`--stack` 默认 64MiB，`--output-limit` 默认单文件 64MiB，`--nproc` 默认关闭。
始终禁用 core dump。CPU 和内存设为 0 时关闭自身对应保护上限；CPU 为 0 且未显式设置
wall 时也关闭自动 wall 限制。即使内存不限，默认仍建立 cgroup，用于统计和清理后代；
只有显式传 `--no-cgroup` / `use_cgroup=False` 才关闭 cgroup。
零值不会取消继承的外部限制；`--nproc` 是 UID 范围的限制，不是单提交进程配额。

## 验证

```bash
# 无需 cgroup：逻辑测试及真实 helper 执行测试
make check

# 本机真实 cgroup 集成测试
systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py make check

# 已有委派目录时
ROJ_JUDGE_CGROUP_ROOT=/path/to/delegated-parent make check
```

测试覆盖真实 OOM、保护余量内完成后再判超限、微秒/字节边界、瞬时峰值、多个后代的
内存计量、setsid 后代清理、Ctrl+C、setup 失败、以及执行后不遗留子组。
共同的执行测试在两种模式分别运行，覆盖重定向、限额、exec 前阻塞超时、正常退出后的
后台进程清理和中断回收。直接 `make check` 且未提供委派目录时，仍运行逻辑测试和
无 cgroup 的真实执行测试，只有需要 cgroup 的测试跳过。
降权测试需 root，普通用户运行时明确跳过。
