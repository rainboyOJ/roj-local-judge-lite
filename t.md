# 关于 `local_judge.py` 自动委派里「调用自己两次」的思考

> **后续（已解决）**：文中的两次 exec 已经合并成一次。`--in-scope` 现在是本 CLI
> 自己的一个普通选项，同一个进程准备完父目录后继续走评测流程，不再 exec 第二个自己：
>
> ```
> systemd-run --user --quiet --scope -p Delegate=yes -- \
>     <PY> <SELF> --in-scope --pid 1000 sum.cpp --no-delegate
> ```
>
> 探测也改成直接调用 `prepare_delegated_scope()`（与真实准备同一段代码），
> 不再需要“同一种命令形状”。下文保留当时的分析与取舍过程，供了解背景。

写完 `refactor: drop the delegated.py hop from local_judge auto-delegation`
（commit `0be97e9`）之后，我盯着这段代码又看了一遍：

```python
def _delegated_prefix() -> list[str]:
    return ["systemd-run", *_delegate_mode_args(), "--quiet", "--scope",
            "-p", "Delegate=yes", "--",
            sys.executable, str(Path(__file__).resolve()), "--in-scope", "--"]


def _delegated_command(raw_args: list[str]) -> list[str]:
    return [*_delegated_prefix(), sys.executable, str(Path(__file__).resolve()),
            *raw_args, "--no-delegate"]
```

真实运行展开后是这样（`<SELF>` = `local_judge.py`，`<PY>` = python 解释器）：

```
systemd-run --user --quiet --scope -p Delegate=yes -- \
    <PY> <SELF> --in-scope -- <PY> <SELF> --pid 1000 sum.cpp --no-delegate
```

`<SELF>` 出现了两次。第一反应是「这不是又套娃了吗」，于是有了下面这篇记录。

---

## 1. 为什么会调用两次

这两次 `<SELF>` 是**两个不同的职责**，不是同一个原因写了两遍。

### 第一次：`<SELF> --in-scope` —— 负责「进 scope 里做准备」

`systemd-run` 的 `--` 后面是一条命令，这条命令**必须运行在新建的 scope 内部**，
才能去写那个 scope 的 cgroup 接口文件（`cgroup.procs`、`cgroup.subtree_control`）。

问题在于：我们没有「在 scope 里单独执行一小段准备代码」的独立程序。
`delegated.py` 已经从这条链路里移除了（它现在只服务 `runner.py` / `make check` /
教程）。所以只能**用自己当那个载体** —— `local_judge.py --in-scope` 一进来就执行
`prepare_delegated_scope()`：

- 从 `/proc/self/cgroup` 反推出所在 scope 路径
- 校验它是本进程独占、可写、且暴露 memory controller 的 `.scope`
- 把自己移进 `manager` 叶子组（规避「管理目录不能有进程」的 domain controller 限制）
- 给父目录开 `+memory`，并设置 `ROJ_JUDGE_CGROUP_ROOT`

### 第二次：`<SELF> --pid 1000 ...` —— 才是真正要跑的评测

它必须是一个**全新的进程**，理由是准备动作的**副作用**需要被沿用：

第一份进程在准备完成后，把 scope 根目录写进了自己的环境变量。要「带着这个环境」
重新走一遍完整 CLI（解析参数 → 定位测试数据 → 编译 → 逐点执行），只能 exec 一个
新进程。此时 cgroup 根已就绪，它会直接命中隔离分支。

### 一句话总结

**第一次负责「准备环境」，第二次负责「干活」。**

两者不能合并，因为：

- 准备动作**必须**发生在 `systemd-run` 建立的 scope 内（外部无从准备）
- 评测**必须**发生在准备完成之后

---

## 2. 我踩过的坑

改这个的时候我确实被咬了一次，值得记下来。

**误区一：以为第二次调用是多余的，想让第一个进程准备完后直接继续跑评测。**

做不到。`prepare_delegated_scope()` 在进程启动的最早期执行，那时候：

- 还没解析参数（不知道 `--pid` 是什么）
- 还没定位测试数据
- 还没编译

而这些都是**在同一个作用域里**的后续步骤。

**误区二：想把「准备」插进 `main()` 的中段（step 04 那个位置）。**

也不行。因为 `step 04` 的位置在 `check_cgroup_root(cgroup_root)` **之后** ——
那时候 `isolated=False` 已经算完了，判断顺序反了。要把准备嵌进去，就得把整个
step 04 重构掉。

**误区三（真正实现时遇到的 bug）：`--in-scope` 的 payload 被 argparse 吃掉了。**

第一版实现里，我以为「准备完 exec 回自己」就完事了，于是让 `--in-scope` 走正常的
`parser.parse_args()`。结果探测命令行是：

```
python3 local_judge.py --in-scope -- python3 -c "print('ok')"
```

argparse 看到 `-c` 直接报 `unrecognized arguments`，**在进 `--in-scope` 分支之前
就死了**。真实运行也一样，因为 `--` 后面的东西是「任意命令」，不可能用本 CLI 的
参数集去解析。

所以现在的实现是在 `parse_args()` **之前**拦截：

```python
raw_args = list(sys.argv[1:] if argv is None else argv)

# `--in-scope` 的 payload 是一条任意命令（探测时是 `python -c print(...)`，
# 真实重跑时是 `python local_judge.py <原参数>`），不能用本 CLI 的 parser 去解析，
# 否则第一个 argparse 不认识的开关就会在进 in-scope 分支之前报错。
if "--in-scope" in raw_args:
    return run_in_scope(raw_args)
```

这个坑很有教育意义：**「把 `--` 之后的内容当成一条命令」和「当成自己的参数」
是两种完全不同的契约，混在一起用 argparse 必然出问题。**

---

## 3. 难点：为什么不能更干净

三个候选方案，各有代价。

### 方案 A：保持现状（两次 exec）

**优点**

- 第一次的进程极短命，只做 `mkdir` + 写两个文件，几十毫秒量级，不是性能问题
- 探测和真实执行**共用同一条路径**，不会漂移
  （`try_auto_delegate()` 的探测就是用同一个 `_delegated_prefix()` 拼的，
  只是把 payload 换成一句 `print`）

**缺点**

- argv 里看到自己两次，`ps` 看起来像套娃
- 观感差，下一个读代码的人大概会问同样的问题

### 方案 B：让 `systemd-run` 直接跑评测进程，准备逻辑放在外面

**不可能。** scope 是在 `systemd-run` 启动之后才存在的，外部无从准备。

这条要记下来，因为它是这个设计存在的根本原因 —— 不是「懒得合并」，而是
**时序上做不到**。

### 方案 C：`--in-scope` 后不 exec，直接「就地继续」跑评测

也就是让第一个进程准备完就继续往下走，不再启动第二个进程。这样 argv 里
`<SELF>` 只出现一次。

**技术上可行，但代价很大：**

1. 需要把「准备」的时机从进程启动早期挪到 `main()` 中段（step 04 之前），
   即重构 `check_cgroup_root` / `isolated` 的判定顺序
2. **破坏探测与真实路径的一致性** —— 探测没法「跑到一半就返回」，
   得另写一份轻量探测，又回到最初那种容易漂移的状态
3. `--in-scope` 分支要和正常分支共用 `main()` 的后续代码，控制流变复杂

我选择了不这么做。探测与真实路径一致这一点，我认为比「argv 少一个自己」更重要。

### 方案 D：彻底不自动委派，要求用户自己准备 scope

这正是 README 里「让固定父目录开机后依然存在」那一节做的事 —— 用 systemd
oneshot 服务在开机时建好 `/sys/fs/cgroup/roj-judge`。

**如果默认路径一直存在，`try_auto_delegate()` 根本不会被触发**，
那两次 `<SELF>` 就消失了。

---

## 4. 我的结论

**短期：接受现状。**

两次 exec 不是性能问题，是观感问题。而且第一次进程的开销确实可以忽略。

**长期：按方案 D 走。**

自动委派应该定位成「**给不想配置环境的人兜底，而不是常走的主路径**」。
落实开机服务之后：

- 配了服务的机器 → 直接命中隔离，完全看不到两次 `<SELF>`
- 没配服务的机器 → 自动委派兜底，`<SELF>` 出现两次，但功能正确

这正好是自动委派该有的定位。

**顺带要做的：把「为什么 `<SELF>` 出现两次」写进 `_delegated_prefix()` 的
docstring。** 现在注释只说了「不再经过 `delegated.py`」，没说清两次调用各自的
职责 —— 下一个读到的人（包括三个月后的我）大概率会问同样的问题。

---

## 5. 遗留的隐患

改完之后，scope 准备的规则存在**两份**：

| 位置 | 用途 |
|---|---|
| `local_judge.prepare_delegated_scope()` | 自动委派 |
| `examples/delegated.py` | `runner.py` / `make check` / 教程 |

我在两边的 docstring 都写明了「两处对合法独占 scope 的判定必须保持一致」，
但这是**约定，不是机制**。将来改判定逻辑（比如放宽独占性检查）时，
必须记得同时改两处，否则两条路径会静默地产生不同的行为。

可能的消除方式：让 `delegated.py` 反过来 `import`
`local_judge.prepare_delegated_scope`。代价是示例脚本要依赖仓库根目录的导入路径，
而且 `delegated.py` 作为「独立可复制的小工具」的定位会被削弱。

这件事**还没做**，留在这里备忘。
