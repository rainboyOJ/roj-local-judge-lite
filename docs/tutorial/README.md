# 从一份 A+B 提交读懂本地评测机

你已经会在 Linux 下用 C++ 解题，会写循环、函数和简单的 Python 脚本。这份教程从这里出发：把一份提交交给另一个程序运行，观察操作系统做了什么，再回到 roj-local-judge-lite 的源码。

读完后，你应当能说明一份提交如何启动、输入从哪里来、超时后谁来终止它、内存怎样计量，以及最后为何得到 AC、WA、TLE、MLE 或 RE。遇到实现问题时，也应当知道先去哪个函数寻找答案。

## 怎样使用这份教程

每章遵循同一条路线：先提出评测中的具体问题，再运行小实验，解释观察到的现象，然后阅读项目中的对应函数。章末的问题附有参考答案；先自己推一遍，再展开答案。

所有命令默认在**仓库根目录**执行。前七章不要求配置 cgroup；第八章才进入真实的内存管理。不想按顺序读，也可以直接跳到下面的「独立专题」。系统编程实验用 C++17 编写，调用的 Linux 接口与项目 C helper 相同。实验只展示当前要学习的机制，完整的错误处理和清理要回到项目阅读。

第一次先执行：

```bash
make
make -C docs/tutorial/examples
```

第一条构建项目的 C helper，第二条把实验编译到 `docs/tutorial/examples/.build/`。实验不写进提交的题目数据目录。主线按普通用户运行；不需要为了学习前几章切换成 root。

## 阅读路线

| 章节 | 要回答的问题 | 读完能进入的源码 |
|---|---|---|
| [01 跑通一次 A+B](01-first-run.md) | 判题机比直接运行程序多做了什么？ | `local_judge.main` 的整体结构 |
| [02 程序变成进程](02-process.md) | 谁在运行代码？PID、工作目录是什么？ | helper 的 `Options`、`run_child` |
| [03 创建、替换、回收](03-fork-exec-wait.md) | 怎样启动提交，同时留下监控者？ | helper 的 `main`、`execvp` |
| [04 输入输出怎样接上文件](04-file-descriptors.md) | `cin` 为什么能读测试点？ | `redirect_stream`、`_check_stream_paths` |
| [05 退出状态与错误管道](05-status-and-pipe.md) | 退出 127 就代表启动失败吗？ | `SetupError`、`check_setup`、`print_result` |
| [06 时间与资源限制](06-time-and-limits.md) | 程序睡一秒，算用了一秒 CPU 吗？ | `Limits`、`apply_limit`、`monitor_child` |
| [07 信号、权限与清理](07-signals-and-cleanup.md) | 杀掉一个 PID，为什么可能没清理干净？ | `kill_submission`、降权、异常清理 |
| [08 cgroup 怎样管理内存](08-cgroup-memory.md) | 瞬间申请后释放的内存还测得到吗？ | `MemoryCgroup`、`examples/delegated.py` |
| [09 用 Python 组织这些能力](09-python-api.md) | Python 与 C 之间传什么，谁负责判定？ | `run_case`、`_invoke_helper`、`_set_verdict` |
| [10 串起完整评测流程](10-judge-pipeline.md) | 怎样从源文件得到整道题的结果？ | `local_judge.py` 的编译、比对与汇总 |
| [附录 运行环境与测试](appendix-environment-and-tests.md) | 安装、Docker、测试分别解决什么问题？ | `install.sh`、`docker/`、测试文件 |

这条路线会多次进入同一个文件。例如第 3 章先理解 helper 的父子分支，第 6 章才读懂父进程里的监控循环。第一次不必强迫自己看懂整个文件。

## 独立专题

下面这一份**不依赖上面的主线**，可以单独读，也可以完全不读 01～10。它的主题是：不用 root，自己动手管住一个程序的内存。

| 文档 | 要回答的问题 | 读完能做什么 |
|---|---|---|
| [普通用户也能管内存](cgroup-user-memory.md) | 不用 root，怎样给程序套上「最多 256MB」的笼子？ | 手动 → 流程 → 脚本 → 监听 OOM，四层递进，最后能写出自己的限内存脚本 |

它是第 8 章的「动手版」：第 8 章讲原理（内核怎么记账、为什么峰值抹不掉），这份文档让你亲手把同一件事做四遍，从一条条敲命令一直做到自动判定 MLE。

## 检验自己的理解

读完后，关掉教程，用源码回答这五件事：

1. 画出 Python、helper、提交三个进程，以及每条输入输出的去向。
2. 解释“用户程序自己退出 127”与“根本找不到可执行文件”为什么是不同结果。
3. 按默认配置算出 CPU 保护上限、wall 截止时间和内存保护上限。
4. 说明正常结束、超时、Ctrl+C 分别经过哪些清理步骤。
5. 找到从 `OK` 变成 `AC` 或 `WA` 的那行代码。

需要查某个系统接口的完整定义时，文中链接指向 Linux man-pages 或内核文档。教程负责建立理解，手册负责补全边界条件。

从 [第 1 章](01-first-run.md) 开始。
