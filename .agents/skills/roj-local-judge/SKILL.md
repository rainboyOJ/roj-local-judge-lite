---
name: roj-local-judge
description: 本地评测一份 C++/Python 提交：编译、跑测试点、cgroup 计量内存，给出 AC/WA/TLE/MLE/RE/SE。当用户要求“评测/测一下这段代码”“跑一下测试点”“看看能不能过”“本地验证一下这个解法”“这题我的程序对不对”时使用。不要用于在线提交、远程判题或造题。
compatibility: 需要 Linux、Python 3.8+、make 和一个 C 编译器（首次要构建 executor）。cgroup 内存计量可选：不可用时自动降级为只限 CPU 与 wall，此时 MLE 判不出来。
---

# 本地评测器（roj-local-judge-lite）

用 `judge.py` 评测一份提交：编译一次，逐个测试点执行，比对答案并汇总。

## 先找到程序本体

按顺序尝试，用第一个存在的：

```sh
ls ~/.local/share/roj-local-judge-lite/judge.py     # install.sh 装在这里
ls ./judge.py                                       # 在源码目录里
```

如果只有源码、还没构建过 `executor`，先跑一次 `make`：

```sh
make        # 在仓库根目录；生成 executor
```

`judge.py` 会自己找同目录的 `executor`。装了启动器的话，`roj-local-judge-lite` 等价于
`python3 ~/.local/share/roj-local-judge-lite/judge.py`。

C++ 用 `g++ -std=c++17 -O2 -DONLINE_JUDGE` 编译（与 judge_server 同一组参数），
Python 用 `py_compile` 做语法检查。

## 三种指定测试点的方式（三选一）

| 你手上有什么 | 用什么 |
| --- | --- |
| 一个包含配对 `.in`/`.out` 的目录 | `--data-dir <dir>` |
| 零散的输入文件（旁边有同名 `.out`） | `--case a.in`（可重复） |
| 标准题库布局：`<testdata>/<pid>/data/` | `--pid 1000 --testdata /path` |

三者**互斥**，混用会在启动时报错。都不给也会报错。

```sh
# 最常见：评测一份代码
python3 judge.py sol.cpp --data-dir /path/to/data

# 单个数据点；答案取同目录同名的 a.out
python3 judge.py sol.cpp --case tests/a.in

# 答案名字不同时按顺序配对
python3 judge.py sol.cpp --case a.in --case b.in --case-out a.exp --case-out b.exp

# 题库布局
python3 judge.py 1000.cpp --pid 1000 --testdata /path/to/testData

# 先看看有哪些题
python3 judge.py --list --testdata /path/to/testData
```

`--case` 与 `--case-out` 的数量必须相同；输入文件旁边找不到同名 `.out` 时会报错并提示
用 `--case-out`，不会静默跳过这个点。

## 让 AI 直接读结果：`--output-format json`

**需要程序化读取结果时一律加这个参数。** 此时 stdout 只有一份 JSON，人看的进度信息
（题目、编译、逐点）全部走 stderr，所以可以安全地管道取值：

```sh
python3 judge.py sol.cpp --data-dir data --output-format json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["verdict"], d["passed"], "/", d["total"])'
```

结构（节选）：

```json
{
  "verdict": "WA",
  "exit_code": 1,
  "passed": 8, "total": 10, "elapsed_s": 0.05,
  "problem": {"label": "1000", "title": "A+B问题", "temporary": false},
  "source": {"path": ".../sol.cpp", "lang": "cpp"},
  "limits": {"time_ms": 1000, "memory_mib": 128, "wall_ms": 1500},
  "io_mode": "stdio",
  "compile": {"ok": true, "output": ""},
  "isolation": {"mode": "cgroup", "detail": "/sys/fs/cgroup/..."},
  "cases": [
    {"verdict": "WA", "name": "problem9",
     "message": "第 1 行：期望 294777，实际 294778",
     "cpu_time_us": 1592, "cpu_time_ms": 2, "real_time_ms": 2,
     "memory_peak_bytes": 524288, "memory_kb": 512, "rss_kb": 3072,
     "oom_events": 0, "oom_kills": 0,
     "timed_out": false, "signal": 0, "exit_code": 0}
  ]
}
```

读的时候注意三件事：

- **`memory_peak_bytes` 可能是 `null`**，表示没有 cgroup、内存未被测量。`null` 不等于 `0`。
- **`isolation.mode` 必须是 `cgroup`，MLE 才可信。** 为 `degraded` 时内存超限不会触发 OOM，
  MLE 永远不会出现——不能把“没有 MLE”读成“内存没问题”。
- **判定用精确值**：`cpu_time_us` 和 `memory_peak_bytes` 才是判据，`cpu_time_ms` / `memory_kb`
  是四舍五入后的展示值。

## 判定怎么读

| 判定 | 含义 |
| --- | --- |
| `AC` | 答案比对通过；全部 AC 时 `exit_code` 为 0 |
| `WA` | 输出与标准答案不同，`message` 里带第一处差异 |
| `TLE` | CPU 时间或 wall 超时 |
| `MLE` | cgroup 内存峰值超限或发生 OOM（降级模式下不会出现） |
| `RE` | 被信号终止，或非零退出码 |
| `SE` | 评测基础设施故障（executor 缺失、cgroup 创建失败等），不是提交的问题 |
| `CE` | 编译失败，编译输出在 `compile.output` |

`exit_code`：`0` 全 AC，`1` 有非 AC，`2` 前置错误或编译失败，`130` 被 Ctrl+C 打断。

**遇到 `SE` 不要当成提交的错**——它表示评测环境有问题，先看 `message`（里面通常带路径），
再按下面的排错一节处理。

## 排错

| 现象 | 处理 |
| --- | --- |
| 提示“未找到 .../config.json，使用默认限制” | 正常。默认 1000ms / 128MiB / stdio；用 `--time` / `--memory` 覆盖 |
| 判定为 `SE`，message 说找不到 executor | 在仓库目录跑 `make` |
| `isolation.mode` 是 `degraded` | 走的是 `--no-cgroup` 或 cgroup 不可用；内存判定缺失，MLE 不可信 |
| 想看某个测试点的实际输出 | 加 `--keep-work-dir`，结尾会打印工作目录，`case-N/_judge.stdout` 是程序输出 |
| 文件 IO 模式（`freopen("apple.in",...)`） | 题目 `config.json` 里写 `{"io":{"mode":"file","input_file":"apple.in","output_file":"apple.out"}}` |
| 结果可疑、想确认隔离生效 | 输出里会打印 `执行 cgroup 隔离，root=...`；降级则打印 `执行降级模式` |

常用覆盖参数：

```sh
--time 2000 --memory 256        # 覆盖题目的限制（ms / MiB）
--checker none                  # 强制按行比较，不用外部 checker
--no-cgroup                     # 跳过 cgroup（MLE 将无法判定）
--keep-work-dir                 # 保留临时目录，便于看输出与编译日志
--lang cpp|python|auto          # 默认按后缀判断
```
