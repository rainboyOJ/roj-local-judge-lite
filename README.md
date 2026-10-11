# roj-local-judge-lite

## 简要说明

Linux 下的简单代码评测机：本地编译提交、逐个测试点运行、比对答案，给出
AC / WA / TLE / MLE / RE 结论。
基于cgroup（不可用时会自动降级并明确提示）。

仓库自带两道示例题（`1000` A+B问题、`1005` 地球人口承载力估计，各 10 个测试点），
克隆后可以直接用它们试跑，也可以换成自己的 `testData/`。

## 目录

- [简要说明](#简要说明)
- [快速使用](#快速使用)
  - [当作执行器或库](#当作执行器或库)
- [构建与权限](#构建与权限)
  - [没有现成委派目录时](#没有现成委派目录时)
- [在 macOS 上运行](#在-macos-上运行)
- [在 Windows 上运行](#在-windows-上运行)
- [Python 接口](#python-接口)
- [结果与分类](#结果与分类)
- [读代码的顺序](#读代码的顺序)
- [题目配置与输入输出模式](#题目配置与输入输出模式)
  - [配置校验](#配置校验)
  - [文件模式下“未生成输出”的规则](#文件模式下未生成输出的规则)
  - [工作目录布局](#工作目录布局)
- [judge.py 独立使用说明](#judgepy-独立使用说明)
  - [基本用法](#基本用法)
  - [judge.py 执行逻辑](#judgepy-执行逻辑)
- [安装脚本](#安装脚本)
- [其他参数](#其他参数)
- [验证](#验证)

## 快速使用

需要 Linux 系统。克隆后构建一次，就能直接评测：

```bash
git clone https://github.com/rainboyOJ/roj-local-judge-lite.git
cd roj-local-judge-lite
make                                            # 首次构建 C helper
python3 judge.py --pid 1000 solution.cpp  # 跑 testData/1000/data 下的全部测试点
python3 judge.py --list                   # 看本地有哪些题
```

macOS 用户请直接看[在 macOS 上运行](#在-macos-上运行)。

想装到用户目录、在任意项目里直接调用，克隆后跑一次安装脚本：

```bash
./install.sh                                    # 默认装到 ~/.local/share/roj-local-judge-lite
cd 你的项目                                     # 目录下有 testData/ 就行
roj-local-judge-lite --list
roj-local-judge-lite --pid 1000 solution.cpp
```

测试数据靠自动查找：先看当前目录及其上级（你自己项目里的 `testData/` 优先），
再退回包内自带的示例数据，都没有就用 `--testdata` 指定。所以装到用户目录后，
在任意目录都能直接评测包里那两道示例题。

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
完整参数见 [judge.py 独立使用说明](#judgepy-独立使用说明)。

### 当作执行器或库

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
注意：只有 `local_judge.py` 具备 cgroup 不可用时的自动降级。
`runner.py` 可以显式使用 `--no-cgroup`，库调用则传 `use_cgroup=False`：

```bash
python3 runner.py --no-cgroup --input 1.in --output 1.user.out -- ./solution
```

两种模式都需要构建好的 `runner_helper`。关闭 cgroup 后仍有 CPU、wall、栈、
输出和进程数限额，但不限制或计量内存，无法判定 MLE。

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
这个默认路径也必须由部署环境预先准备，runner 不会自行配置系统目录（想让它在
重启后依然存在，见下文「让固定父目录开机后依然存在」）。

### 没有现成委派目录时

三种拿到内存隔离的方式，按省事程度排列：

**1. 普通用户：什么都不用做。** 工具会自动 `systemd-run --user --scope -p Delegate=yes`
起一个临时委派 scope 后重跑自己（需要 `systemd-run` 和 `XDG_RUNTIME_DIR`）：

```bash
python3 judge.py --pid 1000 solution.cpp
# 执行 cgroup 隔离，root=/sys/fs/cgroup/user.slice/.../run-XXXX.scope
```

**2. root：也不用准备目录。** root 没有自己的用户管理器，工具会用系统管理器委派：

```bash
sudo python3 judge.py --pid 1000 solution.cpp
```

不要因为想拿到隔离而手动保留 `XDG_RUNTIME_DIR` 去跑 `systemd-run --user`：`sudo`
默认重置环境，且 root 的 `--user` 管理器通常不存在。委派是 systemd 把一棵子树的
属主交给某个用户/服务，不是 root 权限的产物。

**3. 想要固定的父目录（生产部署、无 systemd、容器）：** 自己准备一次。
`roj-judge` 不会自动创建，因为那属于改动系统级目录：

```bash
sudo mkdir -p /sys/fs/cgroup/roj-judge
echo +memory | sudo tee /sys/fs/cgroup/roj-judge/cgroup.subtree_control
sudo chown "$USER" /sys/fs/cgroup/roj-judge   # 让普通用户也能建子组
```

`cgroup.subtree_control` 里的 `+memory` 是必需的：子组要能计量内存，父目录必须先
为子组启用 memory controller。目录刚建好、里面没有进程，所以这里不受 cgroup v2
“管理目录自身不能有进程”的限制。之后 `ROJ_JUDGE_CGROUP_ROOT=/sys/fs/cgroup/roj-judge`
或默认路径都能直接用。`docker/cgroup-init.sh` 在容器里做的就是同一件事（额外还要把
root 里的进程迁走，因为容器里 `/sys/fs/cgroup` 自己也是管理目录）。

### 让固定父目录开机后依然存在

上面那三条命令重启后就没了：`/sys/fs/cgroup` 是 cgroup2 伪文件系统（tmpfs），每次
开机都是空的。想让默认路径 `/sys/fs/cgroup/roj-judge` 一直可用——从而跳过自动委派、
每次运行都直接走内存隔离——用 systemd 在早期启动阶段建一次：

```bash
sudo tee /etc/systemd/system/roj-judge-cgroup.service >/dev/null <<'EOF'
[Unit]
Description=Prepare delegated cgroup parent for roj-local-judge
DefaultDependencies=no
After=sysinit.target
Before=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'mkdir -p /sys/fs/cgroup/roj-judge \
  && echo +memory > /sys/fs/cgroup/roj-judge/cgroup.subtree_control \
  && chown rainboy /sys/fs/cgroup/roj-judge'

[Install]
WantedBy=sysinit.target
EOF
# 把 rainboy 换成运行评测的用户名（上一条命令里的 chown 目标）。
# 写成 chown %u 是不对的：这是系统服务，默认以 root 运行，%u 会展开成 root，
# 结果普通用户仍然建不了子组；除非同时加 User=rainboy 让 %u 指向该用户。
sudo systemctl daemon-reload
sudo systemctl enable --now roj-judge-cgroup.service
```

验证：

```bash
systemctl status roj-judge-cgroup.service
cat /sys/fs/cgroup/roj-judge/cgroup.subtree_control   # 应包含 memory
```

写好后每次运行都会直接打印 `执行 cgroup 隔离，root=/sys/fs/cgroup/roj-judge`，不再
经过 `systemd-run` 临时 scope。

两点提醒：

- **不要**把这段放进 `.bashrc` 之类的 shell 启动文件。cgroup v2 层次是全局的，不是
  每个终端一份；目录用 `mkdir -p` 幂等虽然无害，但 `chown` 和 `subtree_control` 每次
  重写既没必要也可能报错。
- `tmpfiles.d` 也能建目录（`d /sys/fs/cgroup/roj-judge 0755 <user> <user> -`），但它
  改不了 `cgroup.subtree_control`，`+memory` 仍须靠上面的服务或手动写入。

## 在 macOS 上运行

本仓库依赖 Linux 专有的 `prctl` 与 cgroup v2，在 macOS 上无法直接构建，请用容器运行。

### 1. 安装 Docker 运行时

```bash
brew install --cask orbstack
```

装完打开一次 OrbStack 完成初始化。任何兼容 Docker 的运行时都可以，
Docker Desktop、colima 同样适用。

### 2. 构建镜像

```bash
cd roj-local-judge-lite
docker build -t roj-local-judge-lite -f docker/Dockerfile .
```

镜像里已装好 `g++`、`make`、`python3`。`docker/judge.sh` 首次运行时也会自动构建，
这一步可以跳过。

### 3. 执行

```bash
./docker/judge.sh --testdata ~/data/testData --list
./docker/judge.sh --testdata ~/data/testData --pid 1000 solution.cpp
```

`~/data/testData` 换成你自己的题目数据目录。提交文件与 `--testdata` 指向的路径
按宿主机原路径挂进容器，因此参数写法与本地一致。

脚本默认申请 `--privileged`，并在容器内准备启用 memory controller 的 cgroup v2，
所以内存计量与 MLE 判定是精确的：

```text
执行 cgroup 隔离，root=/sys/fs/cgroup/judge
  #1   problem1     MLE       10ms   80.0MiB   内存峰值 80.0MiB
```

## 在 Windows 上运行

本节命令未在 Windows 上实测。与 macOS 同理，本仓库依赖 Linux 专有的 `prctl` 与
cgroup v2，在 Windows 上无法直接构建，请用容器运行。`docker/judge.sh` 是 bash 脚本，
请在 WSL2 里执行。

### 1. 安装 WSL2

以管理员身份打开 PowerShell，然后：

```powershell
wsl --install
```

完成后重启电脑。详细步骤见
[Microsoft 官方文档](https://learn.microsoft.com/windows/wsl/install)。

### 2. 安装 Docker Desktop

```powershell
winget install --id Docker.DockerDesktop -e
```

也可以从 [docker.com](https://www.docker.com/products/docker-desktop/) 下载安装包。
装完启动 Docker Desktop，在 **Settings → Resources → WSL Integration** 里打开你所用发行版的集成。

### 3. 在 WSL 里构建镜像

```bash
cd ~/roj-local-judge-lite
docker build -t roj-local-judge-lite -f docker/Dockerfile .
```

镜像里已装好 `g++`、`make`、`python3`。`docker/judge.sh` 首次运行时也会自动构建，
这一步可以跳过。

### 4. 执行

```bash
./docker/judge.sh --testdata ~/data/testData --list
./docker/judge.sh --testdata ~/data/testData --pid 1000 solution.cpp
```

`~/data/testData` 换成你自己的题目数据目录。提交文件与 `--testdata` 指向的路径
按原路径挂进容器，因此参数写法与本地一致。

脚本默认申请 `--privileged`，并在容器内准备启用 memory controller 的 cgroup v2，
所以内存计量与 MLE 判定是精确的：

```text
执行 cgroup 隔离，root=/sys/fs/cgroup/judge
  #1   problem1     MLE       10ms   80.0MiB   内存峰值 80.0MiB
```

若提示 cgroup 准备失败（Docker Desktop 的 Linux VM 未提供 cgroup v2），改用降级模式，
它只限 wall 与 CPU，内存显示为 0.0MiB，**MLE 无法判定**：

```bash
./docker/judge.sh --no-cgroup --testdata ~/data/testData --pid 1000 solution.cpp
```

仓库请放在 WSL 自己的文件系统（如 `~/roj-local-judge-lite`）而不是 `/mnt/c/...`：
跨文件系统挂载明显更慢，且能避免文件权限问题。

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

### 内存隔离的信任边界

cgroup 的限制不是防篡改的：**降权后程序改不了自己的 case 组，同身份运行能改**。

实测一个提交去写自己的 `case-*/memory.max`：

| 运行方式 | 程序身份 | 结果 |
| --- | --- | --- |
| root（默认降权） | nobody (65534) | `open(...memory.max, O_WRONLY)` → `Permission denied` |
| 普通用户（默认不降权） | 与 judge 相同 | 写入成功，可把上限改成 `max` |

原因是这些接口文件的属主是**创建组的进程**（也就是 judge），模式是 `0644`。
降权后程序落在 other 位，只能读；同身份时它是属主，可以写。

这意味着：

- **root 默认路径（降权到 nobody）下，提交无法放宽自己的内存限制**，MLE 可信。
- **普通用户默认路径或 `--no-drop-privileges` 下，提交能把自己的 `memory.max` 改成
  `max`，之后不会再触发 OOM，MLE 也就永远判不出来。** 它还能改 `memory.swap.max`、
  `memory.oom.group` 等接口来改变内核行为。

部分行为仍然拦得住：`memory.peak` 是历史最大值、`memory.events` 的 `oom`/`oom_kill`
是单调计数，改小上限不影响已记录的事实；把进程移出 case 组需要有祖先组的写权限，
实测 `/sys/fs/cgroup/cgroup.procs` 对同身份用户也是 `Permission denied`。

这不影响本工具的定位：它是本地自查，不是在线判题机——不依赖它对抗恶意提交。
要真正隔离，需要 cgroup namespace（把 `/sys/fs/cgroup` 视图限制在程序自己的组内），
这不在当前实现范围内。需要可信计时与内存时，用 root 路径让它降权，或部署到
judge_server。

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

## 题目配置与输入输出模式

题目目录下的 `config.json` 声明标题、限制和输入输出模式：

```json
{
  "title": "苹果",
  "time": 1000,
  "memory": 128,
  "io": { "mode": "file", "input_file": "apple.in", "output_file": "apple.out" }
}
```

省略 `io`（或写 `{"io": {"mode": "stdio"}}`）就是默认的标准流模式。

| 模式 | 提交怎么读写 | judge 拿什么比对答案 |
|---|---|---|
| `stdio`（默认） | 标准输入 / 标准输出 | executor 捕获的 stdout |
| `file` | 自己 `freopen("apple.in", "r", stdin)`、`freopen("apple.out", "w", stdout)`，或 Python 直接 `open()` | 提交生成的 `apple.out` |

`file` 模式下每个测试点在 `work/case-N/` 里独立运行，`apple.in` 是当前测试
输入的**副本**（提交改动它不会影响原题目数据），判题只认 `apple.out`；提交
写到标准输出的内容不会被当成答案。

### 配置校验

配置错误在编译前报错并返回 `2`：`mode` 只能是 `stdio`/`file`；`file` 模式必须
同时给出两个文件名；文件名不能含路径分隔符、不能是 `.`/`..`、不能相同、不能
以 `_judge.` 开头（该前缀留给评测器自己的文件）。`stdio` 模式下同时写了文件名
只会提示并忽略，便于随时切换到 `file`。

### 文件模式下“未生成输出”的规则

先按原有规则判执行是否正常，再看答案文件——**超时、超内存、非零退出、基础设施
故障都不会被“输出不存在”盖过**：

| 情况 | 结果 |
|---|---|
| 执行已判为 TLE / MLE / RE / SE | 保留原判定，不看答案文件 |
| 执行正常，但 `apple.out` 不存在 | WA，说明未生成输出文件 |
| `apple.out` 是目录或软链接 | WA，答案必须是普通文件 |
| `apple.out` 与只读文件（原输入 / 标准答案）是硬链接别名 | WA，拒绝自我比较 |
| 文件存在但读取失败 | SE |
| 文件为空 | 正常比较；标准答案也为空时可以 AC |

`RLIMIT_FSIZE`（`--output-limit`）同样限制提交写出的普通文件大小。

### 工作目录布局

`--keep-work-dir` 会保留整个临时目录，两种模式都一样：

```text
work/
├── solution            # C++ 编译产物，只编译一次
├── case-1/
│   ├── apple.in        # file 模式：当前测试输入的副本
│   ├── apple.out       # file 模式：提交生成的答案
│   ├── _judge.stdout   # executor 捕获的标准输出
│   └── _judge.stderr   # executor 捕获的标准错误
└── case-2/ ...
```

`case-*` 目录与 `case-*` cgroup 是两种资源：前者存文件，后者存资源控制接口。
若某个测试点收尾失败，该点目录会保留并打印路径（外层工作目录也不整棵删除），
以免诊断信息被一并清掉。

## judge.py 独立使用说明

`judge.py` 是面向用户的单机评测入口，底层由独立的 C `executor` 执行，不依赖 judge_server。
它可以自动寻找测试数据目录下的用例，对单份代码进行编译、运行测试，并比对答案（提供最终的 AC/WA/TLE/MLE 等结论）。

### 基本用法

```bash
# 默认找当前目录下的 testData/（仓库里就是自带的那两道示例题）
python3 judge.py --pid 1000 solution.cpp

# 指定测试数据目录
python3 judge.py --pid 1000 solution.py --testdata /path/to/testData

# 修改时间限制(1000ms)和空间限制(256MB)
python3 judge.py --pid 1000 solution.cpp --time 1000 --memory 256

# 查看本地有哪些题目、各有多少测试点
python3 judge.py --list
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--pid <编号>` | 题目编号，对应 `testData/<pid>/data` |
| `--list` | 列出可用题目、测试点数量与限制 |
| `--testdata <dir>` | 测试数据根目录，默认依次找 `./testData`、`./../testData`、包内 `testData/` |
| `--time <ms>` / `--memory <MiB>` | 覆盖题目 `config.json` 的 `time` / `memory` |
| `--lang auto\|cpp\|python` | 提交语言，默认按后缀判断 |
| `--checker auto\|none\|<path>` | 输出比较器，默认 `auto` |
| `--no-cgroup` | 跳过 cgroup，直接走降级模式 |
| `--keep-work-dir` | 保留临时工作目录，便于查看输出和编译日志 |

### judge.py 执行逻辑

1. **编译 (Compile)**：C++ 用 `g++ -std=c++17 -O2 -DONLINE_JUDGE`（与 judge_server 同一组参数），Python 用 `py_compile` 做语法检查，把解释型语言统一映射到“编译阶段”。
2. **执行 (Run)**：对 `data` 目录里每个配对的 `.in` 调用 C `executor`（每个测试点一个独立目录）。隔离依次尝试 cgroup v2、`systemd-run` 委派 scope，都不可用时降级为 wall 超时加 `RLIMIT_CPU`，并明确提示 MLE 无法判定。
3. **比对 (Compare)**：把用户输出与同名的标准 `.out` 比较。默认优先用 `/judge/checker/fcmp2`（若已部署），否则按行比较，忽略行尾空白和末尾空行。
4. **汇总 (Summary)**：终端打印每个测试点的耗时、内存和状态（AC / WA / TLE / MLE / RE）。

隔离与降级都走同一条 `judge_case()` 事务，不维护第二套 Python 执行路径。
统一后降级模式也会在正常退出和 Ctrl+C 时清理进程组，应用相同的资源限额；
root 运行时同样默认降权到 nobody，需保证源文件、可执行文件和工作目录可访问。

退出码：`0` 全部 AC，`1` 有非 AC 结果，`2` 编译失败或工具/环境错误。

## 安装脚本

`install.sh` 把本仓库（脚本所在目录）构建后安装到用户目录，并在 `~/.local/bin`
放一个启动器。它不访问网络，先克隆再运行：

```bash
git clone https://github.com/rainboyOJ/roj-local-judge-lite.git
cd roj-local-judge-lite
./install.sh
```

| 参数 | 说明 |
|---|---|
| `--dir <path>` | 安装目录，默认 `~/.local/share/roj-local-judge-lite` |
| `--bin-dir <path>` | 启动器目录，默认 `~/.local/bin` |
| `--testdata <path>` | 只用于安装后的冒烟测试 |
| `--no-build` / `--no-smoke` / `--no-launcher` | 跳过对应步骤 |
| `-f, --force` | 目标目录已存在时覆盖 |

行为说明：

- **安装源是本目录**：不从网络克隆，装哪个版本由你克隆的分支或 tag 决定。
- **失败不留半个目录**：先在同级暂存目录里构建、冒烟，全部成功才整体搬进目标目录。
- **冒烟测试**包含模块导入、`make check`（逻辑用例及无 cgroup 的真实执行）、cgroup 可用性探测，
  以及能找到测试数据时跑一次 `local_judge.py --list`。
- `make check` 失败会停止安装并保留原安装；只有显式传 `--no-smoke` 才跳过冒烟测试。
- 需要 `python3 >= 3.8`；构建时还需要 `make` 和一个 C 编译器（`--no-build` 可跳过）。
- `--no-build` 只跳过构建，执行前仍需自行提供与源码配套的 helper；降级模式也需要它。
- 目标目录已存在时默认报错退出，加 `--force` 才覆盖。

安装脚本本身也在包里，所以装完可以直接用
`~/.local/share/roj-local-judge-lite/install.sh` 重装或装到别的目录。

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

# macOS：在 Linux 容器内跑同一套测试
make docker-check
```

测试覆盖真实 OOM、保护余量内完成后再判超限、微秒/字节边界、瞬时峰值、多个后代的
内存计量、setsid 后代清理、Ctrl+C、setup 失败、以及执行后不遗留子组。
共同的执行测试在两种模式分别运行，覆盖重定向、限额、exec 前阻塞超时、正常退出后的
后台进程清理和中断回收。直接 `make check` 且未提供委派目录时，仍运行逻辑测试和
无 cgroup 的真实执行测试，只有需要 cgroup 的测试跳过。
降权测试需 root，普通用户运行时明确跳过。
