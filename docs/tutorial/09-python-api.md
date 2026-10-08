# 09 · 用 Python 把执行、资源和判定组织起来

[上一章](08-cgroup-memory.md) · [目录](README.md) · [下一章：完整评测流程](10-judge-pipeline.md)

现在已经知道 helper 如何创建、限制和回收提交。回到 Python 时，重点从“系统调用怎样工作”转成“这些能力按什么顺序组合”。这一章最终要读懂整个 `run_case()`。

## 第一步：从调用者写下的一次运行开始

运行 [api_demo.py](examples/api_demo.py)：

```bash
python3 docs/tutorial/examples/api_demo.py
```

应看到 `verdict: OK` 的 JSON 和 `user output: 5`。实验使用临时输入输出目录，结束时自动删除；helper 仍需在仓库根目录先 `make`。

核心调用是：

```python
result = run_case(
    [str(executable)], input_path, output_path,
    Limits(time_ms=1000),
    cwd=work, use_cgroup=False, drop_privileges=False,
)
```

这里 `executable` 是实验中 sum 的绝对路径。前几个是位置参数，后几个是关键字参数，可以直接看出各值的含义。函数定义中的单独 `*` 表示其后的参数必须按名字传递，避免把 UID、GID、布尔选项传错位置。

实验固定保留当前身份以减少环境差异。项目 API 的默认策略仍是：root 降权，普通用户保留身份；这与是否启用 cgroup 无关。

## 第二步：把陌生 Python 语法映射到已有经验

打开 [runner.py](../../runner.py)，先读 `Limits` 和 `CaseResult`。

| 语法 | 读法 | 在项目里的用途 |
|---|---|---|
| `@dataclasses.dataclass` | 自动生成构造函数等常见方法的数据类 | 类似一个带默认值的配置或结果结构体 |
| `time_ms: int = 1000` | 带类型提示的字段及默认值 | 提示给读者和工具，运行时仍需 validate |
| `Optional[Path]` | 可以是 Path，也可以是 None | 没提供路径时采用默认规则 |
| `None` | 没有提供具体值 | 不等于 False；降权选项用 None 表示自动决定 |
| `Verdict(str, enum.Enum)` | 枚举成员同时兼容字符串值 | `Verdict.OK.value` 得到 `"OK"` |
| `dataclasses.asdict(result)` | 把数据类转成字典 | 准备 JSON 输出 |
| `dataclasses.replace(result, **fields)` | 复制对象并替换若干字段 | 把 cgroup 统计补进 helper 结果 |

读 `validate()` 时注意 `type(value) is int`。Python 的 `bool` 是 `int` 的子类，使用精确类型检查能拒绝把 `True` 当成 1ms。范围检查也在这里完成，避免把配置问题送到 C 里才发现。

## 第三步：把 run_case 分成五段读

**第一段准备输入。** 检查命令非空、限额和身份参数合法，转成绝对文件路径，拒绝标准流别名，并选择工作目录与 helper 路径。配置错误会抛出 `ValueError`，需要调用者处理。

**第二段决定能力。** `use_cgroup=True` 时构造 `MemoryCgroup`，否则使用 `nullcontext()`。后者仍能写成 with，但不会创建或清理资源，并返回 None。

```python
context = MemoryCgroup(root, limits.memory_max_bytes()) if use_cgroup else nullcontext()
with context as group:
    # 同一份启动代码
    ...
```

这句条件表达式可读成“条件满足时取左值，否则取右值”。同一个 with 中只有一次 `_invoke_helper()`；禁用 cgroup 没有再走第二份 Python 启动逻辑。

**第三段组装参数。** `*limits.helper_args(...)` 把列表展开为多个命令行参数，后面接降权选项、身份、路径和用户命令。`*argv` 也是列表展开。与它不同，`**字典` 展开成按名字传递的参数。

两种模式传给 C 的差别是第二个内部参数：启用 cgroup 时是 `cgroup.procs` 的绝对路径，禁用时是空字符串。`Popen` 收的是数组，空字符串会作为一个真实参数保留，不会被 shell 吞掉。

**第四段获取事实。** helper 返回退出与资源报告；若有 cgroup，再停止整组、读取内存统计、合并到结果里。with 退出完成清理。

**第五段做判定。** 调用 `_set_verdict()`，最终补齐输出路径并返回。启动或管理过程的 `OSError/RuntimeError` 会被转换成 SYSTEM_ERROR；`KeyboardInterrupt` 不在这个捕获列表里，清理后继续向上传播。

## 第四步：跟进 Popen 和 communicate

`_invoke_helper()` 的 `Popen` 启动进程后返回一个管理对象，`communicate()` 等待并收取 stdout/stderr。两者组合让 Python 可以在等待异常时找到 helper 并终止它。捕获 stdout/stderr 的 PIPE 是 Python 与 helper 的通道，不是第 5 章父子间的私有 setup 管道。

`text=True` 把收到的字节解码成字符串。`json.loads(stdout)` 再将 JSON 字符串转成字典，`CaseResult(**字典)` 按字段名创建结果对象。解析失败或 helper 异常退出是工具故障。

这里没有在 Python 的 fork 子进程里执行 `preexec_fn` 回调；需要 exec 前完成的动作由独立 C helper 实现。`subprocess` 的不同调用方式可查 [Python 官方文档](https://docs.python.org/3/library/subprocess.html)。

另一个容易忽略的函数是 `_child_env()`。默认只保留 PATH 和必要的语言环境，再设置 HOME、TMPDIR 与 Python I/O 配置；`inherit_env=True` 才复制完整环境。`HOME=工作目录` 只影响程序读取的环境变量，不等于文件系统访问被隔离。

## 第五步：按优先级判定

现在逐行读 `_set_verdict()`。它是一条有先后顺序的判断链：

| 优先检查的事实 | 结果 |
|---|---|
| cgroup 出现 OOM 事件或 OOM kill | MLE |
| wall 看门狗触发 | TLE |
| 内存峰值超过题目阈值 | MLE |
| 原始 CPU 微秒数超时，或收到 SIGXCPU | TLE |
| 其他终止信号 | RE |
| 非零退出码 | RE |
| 以上都没有 | OK |

工具启动或管理失败已在外层处理为 SYSTEM_ERROR，不进入这条正常判定链。顺序有意义：如果同时有 OOM 证据和 SIGKILL，必须先归为 MLE，不能只看信号就判 RE。

关闭 cgroup 时，内存字段和事件默认是 0，于是不会靠 RSS 或 stderr 猜 MLE。默认 `use_cgroup=True` 的库调用遇到坏目录则返回 SYSTEM_ERROR；自动降级属于 CLI 的策略，下一章才会遇到。

## 不启动进程也能验证边界

```bash
python3 - <<'PY'
from runner import CaseResult, Limits, _set_verdict

for cpu_us in (1000000, 1000001):
    result = CaseResult(cpu_time_us=cpu_us)
    _set_verdict(result, Limits(time_ms=1000))
    print(cpu_us, result.verdict.value)
PY
```

预期为 `1000000 OK`、`1000001 TLE`。本实验为了学习直接调用了下划线开头的内部函数；正常使用者只需要公开 API `run_case()`。

## 停下来想一想

`nullcontext()` 只是少创建一个组，为什么能删除一整套降级执行循环？

<details>
<summary>参考答案</summary>

两种模式的进程启动、CPU 限制、wall 监控和退出回收本来就是相同机制。变化只在外围的 cgroup 生命周期及内存数据。把可选能力包在统一执行过程外面，就不必为它复制执行器。

</details>
