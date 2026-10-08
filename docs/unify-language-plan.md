# py-judge-runner 单语言化方案（待审核）

> 状态：**待审核，尚未动手**
> 范围：**仅 `py-judge-runner/`**
> 结论摘要：建议把唯一的 C 文件 `runner_helper.c` 用 Python 重写，让本包变成纯 Python 实现。已实测确认此前的技术顾虑不存在，剩余代价可控。

---
> **状态更新（移入本仓库时补记）**
> §8 的“第 1 步”已由维护者实现：两条执行路径已合并（`run_case` 新增 `use_cgroup`，
> helper 接受空 cgroup 参数表示不入组），`local_judge.py` 删除自带的降级执行实现，
> 测试从 28 个扩到 55 个。**方案 A（全 Python）未采纳，C helper 保留。**
> §9 的其余决策点仍待定。

## 1. 需求

用户的诉求（原文归纳）：

1. **本包内部代码要统一**——不要 Python 里夹一个 C 文件。
2. 顺带降低安装门槛：`install.sh` 现在要求 `cc` + `make`，纯 Python 后这个依赖消失。
3. 前提约束：**`py-judge-runner/` 是一个独立实现**，和 `judge_server`、`sjudge/` 是分开的两套东西，本次不改动它们。

> 待确认：以上归纳是否准确，是否还有别的目标（例如可读性、贡献门槛、测试统一）。

---

## 2. 范围边界

本文**只讨论 `py-judge-runner/`**。

明确不涉及：

- `sjudge/`（judge_server 内置的 C++ 评测执行核心）
- `judge_server` 的 runner 链路、`/usr/bin/sjudge` 兼容层
- 两边判定语义是否需要统一（这是另一个独立议题，本包视为独立工具）

---

## 3. 现状

### 3.1 文件构成

| 文件 | 行数 | 语言 |
|---|---|---|
| `local_judge.py` | 602 | Python |
| `test_runner.py` | 320 | Python |
| `runner.py` | 308 | Python |
| `memory_cgroup.py` | 70 | Python |
| `examples/delegated.py` | 33 | Python |
| **`runner_helper.c`** | **257** | **C** ← 唯一的"不统一" |
| `Makefile` | 15 | Make |
| 合计 | 1605 | |

Python 合计 1333 行，C 257 行。

### 3.2 问题一：两种语言

`runner_helper.c` 承担 fork/exec 之后的全部准备工作与监控。它存在的**最初动机**是 RSS 基线问题（见 §4.1），但那个动机已随 cgroup 落地而失效。

### 3.3 问题二：两条重复的执行路径（比语言问题更值得处理）

当前包里有**两套**"执行程序并判定"的代码：

| | 隔离路径 | 降级路径 |
|---|---|---|
| 位置 | `runner.py:run_case` → `_invoke_helper` → C helper | `local_judge.py:run_case_degraded` |
| fork/exec | helper 做 | `preexec_fn` + `subprocess` |
| 限额 | helper 里 `setrlimit` | `preexec_fn` 里 `resource.setrlimit` |
| 超时/watchdog | helper 里 `monitor_child` | Python `os.wait4` 轮询 |
| 进程组清理 | helper `kill(-child)` | `os.killpg` |
| 结果 | helper 输出 JSON → `CaseResult` | 直接构造 `CaseResult` |

两条路径的判定都收敛到 `_set_verdict`，但**执行机制各写一遍**。所以本包的"不统一"实际是三件事：两种语言 + 两条重复执行路径 + 隔离/降级的行为分叉。

### 3.4 问题三：安装需要编译器

`install.sh` 在 `DO_BUILD=1`（默认）时要求 `cc`/`gcc`/`clang` 与 `make`，否则直接报错退出。这让"一行 curl 安装"在最小化系统上失败。

---

## 4. 关键事实与实测证据

### 4.1 背景：helper 的最初作用（已失效）

`runner_helper.c` 头部注释写明设计动机：

> Python -> exec helper -> fork -> exec 用户程序。exec 会保留历史 `ru_maxrss`，fork 才会建立新的进程统计。

实测复现（父进程 Python 持有 300 MiB）：

```text
父进程 Python              ru_maxrss = 310 MiB
A. Python --fork+exec--> 被测程序      = 305 MiB   ← 被污染
B. Python --fork+exec--> helper --fork+exec--> 被测程序
   helper 自己              = 305 MiB   ← 自己也被污染
   helper fork 出的被测程序   =   1 MiB   ← 干净
```

即 helper 是一个"RSS 防火墙"：自己吸收污染，换来被测程序的干净基线。

**但本包 README 自己已经写明**：内存判定走 cgroup 的 `memory.peak`，"cgroup 内存计量不依赖这个 RSS 修正"。所以这个最初动机**对内存判定已经失效**，`rss_kb` 只剩诊断价值。

### 4.2 本包已经在使用 `preexec_fn`

```text
local_judge.py:270:  preexec_fn=lambda: _apply_rlimits(limits),
```

降级路径本来就是 `preexec_fn`。因此"`preexec_fn` 有风险"**并不是本包现有的保证**，不能作为反对全 Python 的理由。

### 4.3 实测：`preexec_fn` 里入组不会污染 `memory.peak`

全 Python 路线必须在 `preexec_fn` 里把子进程加入 cgroup，而那一刻子进程还持有父进程的 COW 内存。这曾是唯一的技术疑点，已实测排除：

```text
父进程 Python ru_maxrss = 316 MiB
A. preexec_fn 入组（纯 Python 路线）  memory.peak = 0.2 MiB
B. 经 runner_helper（现状）          memory.peak = 0.5 MiB
```

父进程 316 MiB，纯 Python 路线的峰值只有 **0.2 MiB**。原因：memcg 把继承来的 COW 匿名页记在**原 cgroup（父进程）**头上，子进程入组时不会把父进程的内存划过来。

（父进程数值随运行在 315～316 MiB 之间波动，结论不变。§11 的脚本可直接复现。）

复现方法见 §11。

---

## 5. 方案 A：全 Python（推荐）

### 5.1 职责映射

| 现在（`runner_helper.c`） | 全 Python 实现 | 现状 |
|---|---|---|
| `setpgid(0,0)` + 信号复位 | `Popen(start_new_session=True)` | 现成 |
| `chdir(cwd)` | `Popen(cwd=...)` | 现成 |
| 重定向三个标准流 | `Popen(stdin=/stdout=/stderr=)` | 现成 |
| `open(cgroup.procs)` + `write("0")` | `preexec_fn` 内两行 | 新增（**已实测安全**，§4.3） |
| `apply_limit()` × 5 | `resource.setrlimit()` | 降级路径已有 |
| `setgroups/setgid/setuid` | `os.setgroups([])` / `os.setgid` / `os.setuid` | 新增 |
| `prctl(PR_SET_PDEATHSIG)` | `ctypes` 调 `libc.prctl(1, 9)` | 新增（**必须在 `setuid` 之后**，`setuid` 会清除该标志） |
| `fork` + `execvp` | `subprocess` 自己做 | 省掉 |
| `monitor_child`（wait4 + wall + killpg） | 复用 `local_judge.py:run_case_degraded` 的循环 | 现成 |
| 私有错误管道 | 删除，改用 CPython 自带 errpipe | 见 §5.4 |
| `print_result` JSON | `CaseResult.to_dict()` | 现成 |

关键顺序必须保持（与 C 版一致）：**入组 → 限额 → 降权 → `PR_SET_PDEATHSIG` → exec**。
入组必须在降权之前，否则 `nobody` 没有 `cgroup.procs` 写权限。

### 5.2 目标结构

```text
runner.py          编排 + 判定 + 库 API（删除 helper 调用）
execution.py       新增：唯一的执行入口 _spawn_and_measure(preexec_fn + cgroup 包装可选)
memory_cgroup.py   cgroup 生命周期（不变）
local_judge.py     本地评测 CLI（删除 run_case_degraded，改为调用 execution）
test_runner.py     测试（少量调整）
examples/delegated.py  不变
```

净变化：**删除 257 行 C**；Python 侧因为两条执行路径合并成一条，预计持平或略降；`Makefile` 从 15 行缩到只留 `check`；`install.sh` 删除编译器检查与构建步骤。

### 5.3 收益

| 收益 | 说明 |
|---|---|
| 单语言 | 1605 行 → 全部 Python |
| 消除重复执行路径 | 隔离/降级只差一个 cgroup 包装，不再有两套 fork 逻辑 |
| 安装门槛下降 | `install.sh` 不再需要 `cc`/`make`，`--no-build` 的语义变成默认 |
| 无构建产物 | 不再有 `runner_helper`、不再需要 `.gitignore` 特例 |
| 测试统一 | 单一 `unittest`，`make check` 不再依赖构建 |
| 行为分叉减少 | 隔离与降级共用同一条执行路径，避免只修一边 |

### 5.4 代价与风险

| 代价 | 严重度 | 说明与缓解 |
|---|---|---|
| `preexec_fn` 在多线程下不安全 | 中 | 本包已在用（§4.2）。缓解：在 `runner.py` 文档字符串明确"不要在多线程中并发调用 `run_case`"。**注意**：`judge_server` 是多线程的，但它不使用本包（它用 `sjudge`），所以对本包范围可接受 |
| 错误上报变粗 | 低 | CPython 的 errpipe 会把子进程异常抛回父进程，能拿到**异常类型 + errno**，但拿不到 helper 那种操作名（如 `join cgroup: Permission denied`）。缓解：在 `preexec_fn` 里用可辨识的异常消息包装每个步骤 |
| 降权失败定位变难 | 低 | 同上 |
| 测试调整 | 低 | `test_missing_helper_is_system_error` 删除；`LimitsTests` 中断言 `limits.helper_args()` 的 2 处改为断言 limits 自身；其余是黑盒测 `run_case`，不受影响 |
| 文档返工 | 低 | README 的"构建与权限""读代码的顺序""验证"三节，以及 `install.sh` 的依赖说明都要改 |

---

## 6. 方案 B：保留 C，把边界正式化

承认 `runner_helper.c` 是刻意的 native 边界（类比 CPython 自己的 `_posixsubprocess`），然后：

- 给它定一个稳定 CLI 契约并写进文档
- 纳入 CI（编译 + 单元测试）
- 在文档里写明"为什么不能用 `preexec_fn`"（注意：这条理由在本包范围内已被 §4.2/§4.3 削弱）

**收益**：零风险，不动运行语义。
**代价**：两语言照旧；**而且没有解决 §3.3 的两条重复执行路径**。

---

## 7. 方案对比

| 维度 | A. 全 Python | B. 保留 C |
|---|---|---|
| 语言统一 | ✅ | ❌ |
| 删除重复执行路径 | ✅ | ❌ |
| 安装需编译器 | ❌ 不需要 | ✅ 需要 |
| 无构建产物 | ✅ | ❌ |
| `preexec_fn` 风险 | 引入（但已在用） | 无 |
| 错误信息精度 | 中（类型+errno） | 高（操作名+errno） |
| 运行语义风险 | 低（可用测试兜住） | 无 |
| 工作量 | 中 | 小 |

---

## 8. 推荐与实施计划

**推荐方案 A**，理由：本包定位是**独立的本地评测工具**，优先级是"好装、好读、行为一致"，而不是多线程安全的最高等级；唯一硬门槛（§4.3）已实测排除；还能顺带消除两条重复执行路径。剩余代价是"错误信息少一个操作名"这个量级。

分两步走，中间态可回退：

### 第 1 步：合并执行路径（保留 C 作为可选后端）
- 抽出 `execution.py`，隔离与降级共用同一个 `_spawn_and_measure`
- `local_judge.py` 删除 `run_case_degraded`
- 此步结束时 C helper **仍然可用**，两条路径行为应完全一致
- 验证：`make check` 全绿 + 10 道题 std.cpp 全 AC + WA/TLE/MLE/RE/CE 分支复测

### 第 2 步：删除 C
- 把 `preexec_fn` 补齐到与 C 版同等能力（入组/限额/降权/PDEATHSIG）
- 删除 `runner_helper.c` 与 `Makefile` 的构建目标
- `install.sh` 去掉 `cc`/`make` 依赖
- 更新 README

### 验证清单（两步都跑）

- [ ] `make check` / `python3 -m unittest` 全绿
- [ ] `test_large_python_parent_does_not_cause_mle` 仍然通过（这条正是守卫 §4.3 那个性质的测试）
- [ ] 10 道题自带 `std.cpp` 全部 AC（91 个测试点）
- [ ] WA / TLE / MLE / RE / CE 五个分支现象与现在一致
- [ ] 保护余量边界：135 MiB 程序低于 `memory.max(144MiB)` 能跑完，再按 128 MiB 阈值判 MLE
- [ ] 降级模式（`--no-cgroup`）行为不变
- [ ] `install.sh` 在**没有编译器**的环境下能装成功
- [ ] 运行后无遗留 `case-*` cgroup、无遗留进程

---

## 9. 待审核的决策点

1. **是否采纳方案 A？**（若不采纳，是否至少做 §3.3 的路径合并？）
2. **错误信息精度**能否接受从"操作名 + errno"降到"异常类型 + errno"？
3. 是否接受在 `runner.py` 明确声明"`run_case` 不可在多线程中并发调用"？
4. 删除 C 之后，`rss_kb` 这个诊断字段是否还有保留价值？（现在它只剩诊断用途，且其"可信"正是靠 helper 的两段式 fork）
5. `install.sh` 的 `--no-build` 是否保留为兼容用的空操作，还是直接移除？
6. 第 1 步与第 2 步是否要分两个 PR？

---

## 10. 诚实说明

- §4.1、§4.3 的数字来自本机实测（Linux cgroup v2 + systemd 用户会话 + Python 3.14，父进程 300 MiB），不是推算。
- 本文只覆盖 `py-judge-runner/`；它和 `judge_server`/`sjudge` 的语义差异（例如题目限制是否生效）**不在本文范围**，属独立议题。
- §5.2 的"Python 行数持平或略降"、§5.4 的"严重度"评估属**预判**，实施后应以实际 diff 为准。

---

## 11. 附录：复现 §4.3 的实验

```c
/* /tmp/rss/tiny.c —— 被测程序，什么都不做 */
int main(void) { return 0; }
```

```python
# /tmp/rss/probe3.py
import os, resource, subprocess, sys
from pathlib import Path

PKG = "/home/rainboy/mycode/boxtest-opencode-dev/py-judge-runner"
sys.path.insert(0, PKG)
from memory_cgroup import MemoryCgroup
from runner import Limits, run_case

big = bytearray(300 * 1024 * 1024)          # 模拟大 Python 父进程
for i in range(0, len(big), 4096):
    big[i] = 1
print(f"父进程 Python ru_maxrss = "
      f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MiB", flush=True)

root = Path(os.environ["ROJ_JUDGE_CGROUP_ROOT"])

# A. 纯 Python 路线：preexec_fn 里入组
with MemoryCgroup(root, 0) as group:
    procs = group.procs_path
    def join():
        with open(procs, "w") as fh:
            fh.write("0")
    subprocess.run(["/tmp/rss/tiny"], preexec_fn=join, check=True)
    group.stop()
    print(f"A. preexec_fn 入组  memory.peak = "
          f"{group.memory_result()['memory_peak_bytes']/1024/1024:.1f} MiB", flush=True)

# B. 现状：入组发生在 exec 之后的小 helper 里
result = run_case(["/tmp/rss/tiny"], Path("/dev/null"), Path("/tmp/rss/out.txt"),
                  Limits(time_ms=1000, memory_kb=0), cwd=Path("/tmp/rss"),
                  drop_privileges=False,
                  helper_path=Path(PKG) / "runner_helper")
print(f"B. 经 runner_helper  memory.peak = "
      f"{result.memory_peak_bytes/1024/1024:.1f} MiB", flush=True)
```

运行（需要已委派的 cgroup v2 父目录）：

```bash
cd py-judge-runner
cc -O0 -o /tmp/rss/tiny /tmp/rss/tiny.c
systemd-run --user --quiet --scope -p Delegate=yes -- \
  python3 examples/delegated.py python3 /tmp/rss/probe3.py
```
