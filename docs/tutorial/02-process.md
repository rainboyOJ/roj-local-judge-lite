# 02 · 磁盘上的程序怎样成为进程

[上一章](01-first-run.md) · [目录](README.md) · [下一章：fork、exec、wait](03-fork-exec-wait.md)

编译好的 `sum` 是磁盘上的文件；运行中的一次 `sum` 是进程。同一个文件可以同时运行多次，每次有各自的 PID、内存和退出时刻。评测机要限制的是这次运行，而不是永久修改可执行文件。

## 第一步：观察一次运行的身份

```bash
make -C docs/tutorial/examples
docs/tutorial/examples/.build/process
docs/tutorial/examples/.build/process
```

每次会输出类似：

```text
pid=12340 parent=12000
cwd=/path/to/roj-local-judge-lite
```

数字由系统分配，不必与你看到的一致。一般会看到两次 PID 不同，而父进程和工作目录相同。连续从终端启动时，父进程通常是当前 shell。

[process.cpp](examples/process.cpp) 的关键调用是：

```cpp
getpid();                       // 当前进程的 PID
getppid();                      // 父进程的 PID
getcwd(directory, sizeof(directory)); // 把工作目录写入字符数组
```

这些函数向操作系统查询运行状态。可以先把内核理解成管理进程、内存、文件和权限的程序；应用通过系统接口提出请求。项目使用的接口由 C 库暴露，C++ 同样能够调用。

## 第二步：区分工作目录与程序所在目录

```bash
python3 - <<'PY'
from pathlib import Path
import subprocess

program = Path('docs/tutorial/examples/.build/process').resolve()
subprocess.run([str(program)], cwd='/tmp', check=True)
PY
```

这次可执行文件仍在仓库里，但输出的 `cwd` 应是 `/tmp`。工作目录决定相对路径如何解释，不要求与可执行文件的位置相同。

Python 这几行可以这样读：`Path(...).resolve()` 得到绝对路径；`subprocess.run()` 启动一个程序并等它完成；列表中的第一个字符串是程序路径；`cwd` 指定子进程的工作目录；`check=True` 让非零退出成为 Python 异常。这里不会改变外层终端的目录。

以后读到 `run_case(argv, ..., cwd=work_dir)`，要同时看两类路径：输入输出路径先在调用者所在目录转成绝对路径，命令中的 `./solution` 则相对于提交的工作目录。项目明确约定了这两者的区别。

## 第三步：学习系统调用的失败表示

`getcwd()` 可能失败，例如提供的缓冲区装不下路径。实验检查返回值后调用 `perror("getcwd")`。

许多 POSIX 接口失败时返回 `-1`，并设置 `errno`；有些返回指针的接口用空指针表示失败。`errno` 是失败原因编号，`perror()` 把当前原因翻译为可读消息。**先检查返回值，再读 errno**：成功后它不一定被清零。

这与竞赛题里“输入总满足格式保证”的环境不同。评测器必须处理文件不存在、权限不够、无法创建子进程等情况，否则它自己的错误会被混成选手程序的 RE。

## 回到源码

打开 [runner_helper.c](../../runner_helper.c)，看 `Options`。把它看成这次运行的配置包：

| 字段 | 它控制的环境 |
|---|---|
| `command` | 要运行哪个程序，传哪些参数 |
| `cwd` | 程序从哪里解释相对路径 |
| `input/output/error` | 三条标准流对应哪些文件 |
| `uid/gid/drop_privileges` | 提交用什么身份运行，第 7 章解释 |
| 各资源限制 | 这次运行可以消耗多少资源，第 6 章解释 |

看 `run_child()` 中的 `chdir(options->cwd)`。它在子进程里改变目录；父进程仍能在原来的环境中监控和报告结果。

## 停下来想一想

把父进程的 PID 作为“题目编号”合适吗？把源文件目录当作提交工作目录又有什么问题？

<details>
<summary>参考答案</summary>

PID 只标识某一时刻的进程，退出以后还能被重用，同一道题每次执行也可能有不同 PID。题号属于业务数据。使用独立工作目录能把本次编译产物和输出集中管理，避免多次执行都写到源文件所在目录。

</details>

本章只需要记住：进程有身份，也有环境。下一章要同时保留两个环境——一个负责运行提交，一个负责监控提交。
