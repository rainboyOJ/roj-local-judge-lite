# 附录 · 运行环境、安装、测试与阅读索引

[上一章](10-judge-pipeline.md) · [目录](README.md)

主线解释一次评测怎样完成。这里说明如何让它在不同环境启动、怎样检查理解是否对应真实行为。安装与 Docker 不改变 Python API → C helper → 提交这条执行链。

## 本地安装：从工作区复制一个可运行版本

阅读当前 [install.sh](../../install.sh) 时，从 `SRC` 和 `stage` 开始。它以脚本所在目录作为安装源，复制到临时目录，构建 helper、运行检查，成功后才进入替换目标目录的阶段。

这与旧的“运行脚本时自动下载仓库”不同。现在要安装哪版代码，由本地 checkout 决定。

```bash
./install.sh --help
```

先读帮助即可，无需为了读源码真的安装。常规安装命令是 `./install.sh`；`--dir` 和 `--bin-dir` 改目标位置。`--no-build` 不会把 C helper 的依赖消除，执行前仍需提供配套 helper。

脚本中的 `trap cleanup EXIT` 让脚本离开时删除暂存目录。`make check` 失败会退出；在构建和核心验证阶段失败时，旧安装和启动器仍保持原样。后面的目标目录删除、搬移、写启动器是顺序动作，不能把它描述为对断电或任意文件系统错误都保证原子性的一次事务。

启动器最后使用 `exec python3 ... "$@"`：shell 被 Python 替换，原参数逐个转交。可以用第 3 章的知识解释这行代码。

## Docker：提供 Linux 运行环境

本教程主线面向原生 Linux。macOS 或 Windows 使用者可以参考仓库 [README 的平台说明](../../README.md)，这里仅解释三个文件的分工：

| 文件 | 分工 |
|---|---|
| [docker/Dockerfile](../../docker/Dockerfile) | 准备 Linux、Python、编译器和 make |
| [docker/judge.sh](../../docker/judge.sh) | 准备挂载与参数，启动容器，构建后运行 CLI |
| [docker/cgroup-init.sh](../../docker/cgroup-init.sh) | 在容器环境里准备 memory 控制器和 judge 父组 |

仓库挂载到 `/work` 时是只读的；脚本先复制到 `/judge` 再编译，避免把容器生成的二进制写回宿主工作区。输入的提交和数据路径也需要在容器内可见，挂载就是为容器中的进程提供这些文件视图。

默认 Docker 路径申请 `--privileged` 来配置 cgroup。`--no-cgroup` 路径不申请该选项，并把相同选项交给 Python。Docker Desktop/虚拟机/容器的 cgroup 能力和权限配置仍决定内存路径能否工作，不能只看到镜像构建成功就认为内存实验已验证。

`cgroup-init.sh` 会在容器所见的 cgroup 根下迁移成员、启用控制器；它属于容器启动流程，不是让学生在宿主机上手工运行的普通实验。原生 Linux 教程使用的是经过独占 scope 检查的 `examples/delegated.py`。

## 先验证实验，再验证项目

实验可以统一检查：

```bash
make
make -C docs/tutorial/examples check
```

它验证 A+B、PID 与 exec、重定向、错误管道、CPU/wall 差异、进程组及 Python API。没有委派环境时，真实 cgroup 实验会明确跳过。

有 systemd 用户委派时：

```bash
systemd-run --user --quiet --scope -p Delegate=yes -- \
  python3 examples/delegated.py \
  make -C docs/tutorial/examples check
```

运行项目本身的完整回归测试：

```bash
make check
systemd-run --user --quiet --scope -p Delegate=yes -- \
  python3 examples/delegated.py make check
```

普通模式仍运行真实 helper 测试，只跳过要求 cgroup 的部分；普通用户无法执行的 root 降权测试也会跳过。`OK (skipped=...)` 表示已运行的测试通过，不表示被跳过的能力已经验证。

## 用测试读懂边界条件

打开 [test_runner.py](../../test_runner.py)。`ExecutionTestsMixin` 是一组可复用的测试方法，`NoCgroupRunnerTests` 与 `RunnerTests` 分别设置不同模式来复用它们。这样相同的重定向、限额和清理行为会被两种模式共同检查。

| 想验证的理解 | 找哪个测试 |
|---|---|
| exec 前卡住也受 wall 监控 | `test_wall_timeout_covers_setup_before_exec` |
| 用户退出 126/127 不等于 setup 失败 | `test_user_exit_codes_and_stderr_are_not_setup_or_mle` |
| 正常退出也会清理后台进程 | `test_normal_exit_kills_background_descendants` |
| 刚释放的内存仍留下峰值 | `test_peak_survives_free_and_immediate_exit` |
| cgroup 覆盖 setsid 后代 | `test_cleanup_includes_detached_descendants` |
| 大 Python 父进程不直接造成 MLE | `test_large_python_parent_does_not_cause_mle` |
| 安装失败保留旧版本 | `test_install.InstallTests.test_failed_check_preserves_existing_installation` |

测试中的 `assertEqual` 等断言，是把“应该如此”写成机器可以验证的条件。`TemporaryDirectory` 提供临时环境；`patch` 临时替换查找结果或外部动作，让测试只验证目标分支。安装测试通过替代构建命令制造检查失败，无需真的损坏编译器。

## 遇到现象不一致时

| 现象 | 常见原因 | 下一步 |
|---|---|---|
| 找不到 helper | 项目 C 文件还没编译 | 在仓库根目录 `make` |
| 找不到实验程序 | 实验与项目使用独立构建目录 | `make -C docs/tutorial/examples` |
| cwd/输入路径不对 | 从不同目录运行，或混淆绝对与相对路径 | 回看第 2、4 章，回到仓库根目录执行 |
| 无法连接 systemd 用户 bus | 当前环境没有可用用户会话 | 前七章继续；内存章需已有委派目录或配置好的 Linux 环境 |
| `memory.peak` / `cgroup.kill` 缺失 | 内核或挂载环境不支持项目所需接口 | 保留 SYSTEM_ERROR，核对环境而不是把它解释成提交 MLE |
| 默认 API 返回 SYSTEM_ERROR | 库不自动降级，父组不可用 | 需要无内存模式时明确传 `use_cgroup=False` |
| 降权后 Permission denied | 新身份不能访问某级目录或可执行文件 | 检查整条路径的目录穿越权限及文件权限 |

## 最后画一张自己的图

不看前文，在纸上画出：父子关系、进程组、cgroup、三个标准流、私有错误管道、helper JSON 管道。它们不是同一套关系：父子关系决定谁能 wait，进程组决定组信号发给谁，cgroup 决定资源归账和整组清理范围。

再用这张图解释 AC、WA、CPU TLE、wall TLE、峰值 MLE、OOM MLE、启动失败和 Ctrl+C。每条路线都能找到对应源码与测试时，这份代码就已经具有可追踪的结构了。
