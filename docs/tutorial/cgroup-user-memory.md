# 普通用户也能管内存：一步步用 cgroup 给程序套上「最多 256MB」的笼子

[目录](README.md) · [第 8 章](08-cgroup-memory.md)

这份文档只做一件事：**让你亲手把「某个程序最多只能用 256MB 内存」做出来，全程不用 root。**

同一件事我们做四遍，一层比一层省事：

| 层 | 做什么 | 学到的能力 |
|---|---|---|
| 第一层 | 一条条敲命令 | 看清每一步到底在操作什么 |
| 第二层 | 把命令连成一段流程 | 学会「检查每一步有没有成功」 |
| 第三层 | 写成一个脚本文件 | 参数化、自动清理、自动判定 |
| 第四层 | 用 Python 监听 OOM | 程序被杀的那一刻，怎么当场知道 |

## 先看结果

跑完第三层的脚本，你会看到这样的输出：

```text
  笼子上限    ：256M（268435456 字节）
  申请量      ：400MB

  程序退出码  ：137 （128+9，被 SIGKILL 强杀）
  内存峰值    ：268435456 字节（256 MB）
  笼子上限    ：268435456 字节（256 MB）
  oom         ：1
  oom_kill    ：1

  判定        ：MLE —— cgroup 记录了 OOM 事件（内核把程序杀掉了）
```

意思是：一个想申请 400MB 的程序，被内核在 256MB 处直接杀掉了。而这一切，你不需要 root。

> 说明：下文引用的输出都是真实跑出来的。其中内存数字会有**几十 KB 到几 MB 的浮动**（解释器自身的开销不完全固定），你看到的不必和本文一模一样，只要**形状**相同就说明做对了。

## 需要什么

- Linux（本文在普通桌面用户下测试）
- 有 `systemd`（现代发行版都有）
- 有 `python3`
- **不需要 root 密码**

先花 10 秒确认你的系统够用：

```bash
mount | grep cgroup
```

你会看到类似：

```text
cgroup2 on /sys/fs/cgroup type cgroup2 (rw,nosuid,nodev,noexec,relatime,nsdelegate,...)
```

**只要看到 `cgroup2` 这三个字就行。** 如果看到的是 `tmpfs` 或者一堆 `cgroup ... type cgroup`（没有 2），说明系统用的是旧版 cgroup v1，本文的方法不适用。

再确认第二个前提：

```bash
systemctl --user show user@$(id -u).service -p Delegate
```

```text
Delegate=no
```

**看到 `no` 也没关系。** 本文所有实验在这台机器上就是在 `Delegate=no` 的情况下跑通的 —— 真正起作用的是第一层第 2 步那条命令里的 `-p Delegate=yes`。这一点本文末尾的附录里会解释（网上很多教程在这里说反了）。

---

## 先认识三个东西

动手之前，花两分钟认识三件东西，后面会轻松很多。

**① 这台电脑有多少内存**

```bash
free -h
```

```text
               total        used        free      shared  buff/cache   available
Mem:            23Gi       6.1Gi       461Mi      636Mi        17Gi        16Gi
Swap:          8.0Gi          0B       8.0Gi
```

`total` 是全部内存，`used` 是现在用掉的。我们要做的，是让**某一个程序**只能用其中很小的一块。

**② 先确认「现在没有任何东西拦得住它」**

配套的小程序 `docs/tutorial/examples/mem_eater.py` 会真的占内存（不是只申请地址）：

```bash
python3 docs/tutorial/examples/mem_eater.py 300
```

```text
已申请并触碰 300 MB
```

它张口就要 300MB，系统二话不说给了。**这就是问题所在** —— 默认情况下进程申请内存没有任何上限。

**③ cgroup 这棵树在哪**

```bash
ls /sys/fs/cgroup
```

```text
cgroup.controllers  cgroup.procs  cpu.max  memory.max  system.slice  user.slice  ...
```

`/sys/fs/cgroup` 是一个特殊的目录。里面的文件（比如 `memory.max`）**不是普通文件** —— 往里面写东西就是给内核下命令，读它就是问内核要数据。

先记住三个最重要的：

| 文件名 | 读还是写 | 作用 |
|---|---|---|
| `cgroup.procs` | **写** | 记录这个组里有谁。往里写一个 PID，就把那个进程关进这个组 |
| `memory.max` | **写** | 这个组的内存上限 |
| `memory.current` / `memory.peak` | 读 | 现在用了多少 / 历史上最多用到多少 |

顺便认清三个容易混的词：

| 概念 | 一句话 | 类比 |
|---|---|---|
| **进程** | 正在运行的程序 | 一个人 |
| **cgroup** | 一组进程的集合，带资源统计和限制 | 一个笼子 / 一个房间 |
| **控制器** | cgroup 的一个功能开关（`memory`、`cpu`…） | 房间里的某件设备，要先通电才能用 |

---

# 第一层 · 手动：一条条敲，看清每一步

这一层每一步都只做一件事，做完立刻看结果。**不要跳步**，后面每一步都依赖前一步。

## 第 1 步：确认是 cgroup v2

```bash
mount | grep cgroup
```

看到 `cgroup2 on /sys/fs/cgroup type cgroup2` 就对了。

**为什么**：cgroup 有两代。v2 把内存、CPU 等所有资源都放在**同一棵目录树**里，比 v1 简单得多。本文全程用 v2。

## 第 2 步：向 systemd 申请一块属于你的地

```bash
systemd-run --user --scope -p Delegate=yes -- bash
```

你会看到：

```text
Running as unit: run-p12345-i67890.scope; invocation ID: ...
```

然后**光标停在那里，好像什么都没发生** —— 其实你已经进入了一个新的 shell。

**为什么**：`/sys/fs/cgroup` 归 `root` 所有，你在那里建不了目录。这条命令是请 systemd 帮你**新开一块地**，并把这块地的所有权交给你。

三个部分的意思：

| 部分 | 意思 |
|---|---|
| `--user` | 用**你自己的** systemd（不是系统级的，后者要 root） |
| `--scope` | 给这条命令新开一块 cgroup 地盘，并立刻把命令放进去 |
| `-p Delegate=yes` | **关键**：把这块地盘的**所有权交给你**（这就叫「委派」） |
| `-- bash` | 要运行的命令：一个新 shell |

> ⚠️ **后面所有命令都在这个新 shell 里执行。** 想离开就敲 `exit`。

## 第 3 步：看看我现在在哪

```bash
cat /proc/self/cgroup
```

```text
0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-p12345-i67890.scope
```

**为什么**：路径变了 —— 不再是 `session-3.scope`（你的整个桌面会话），而是一个**只属于你的、崭新的 scope**。

## 第 4 步：把这条路记住

路径每次运行都不一样（中间那串编号是随机的），所以用一张「便签」记住它：

```bash
S=/sys/fs/cgroup$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)
echo $S
```

```text
/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/run-p12345-i67890.scope
```

**为什么**：

- `$(...)` 的意思是「先跑括号里的命令，把它的输出塞到这个位置」
- 里面那个 `awk` 是从 `/proc/self/cgroup` 里把路径抠出来
- `$S` 是一张便签，以后再指这块地，写 `$S` 就行，不用敲那一长串

## 第 5 步：确认这块地真的归我了

```bash
ls -ld $S
```

```text
drwxr-xr-x 2 <你的用户名> <你的用户名> 0 ... /sys/fs/cgroup/.../run-p12345-i67890.scope
```

**为什么**：第 3、4 列写的是**你的用户名**。

对比一下：在没委派的普通终端里，你自己的那个目录长这样：

```text
drwxr-xr-x 2 root root 0 ... /sys/fs/cgroup/user.slice/.../session-3.scope
```

那里写的是 `root root` —— 所以你建不了子目录。这就是委派的效果。

## 第 6 步：试着直接打开「内存开关」—— 失败

cgroup 的功能是按**开关**（正式叫「控制器」）分管理的，内存这件事由一个叫 `memory` 的开关负责。先试着打开它：

```bash
echo +memory > $S/cgroup.subtree_control
```

你会看到：

```text
bash: line 1: echo: write error: Device or resource busy
```

**这不是权限问题**（地已经归你了），是另一条规则。

## 第 7 步：确认「我就待在这块地里」

```bash
cat $S/cgroup.procs
```

```text
1234567
1234572
```

**为什么**：`cgroup.procs` 记着「这个组里有哪些进程」，列出来的是进程号（PID）。第一条就是你自己的 shell。

原来第 6 步失败的原因是这条硬规则：

> **一个要往下分配资源的目录，自己里面不能有普通进程。**

用生活化的说法：**要给房客分资源的房间，自己必须是空的。** 而你现在就站在这块地里。

（可能还会多出一两个 PID，那是 shell 为了执行 `$(cat ...)` 临时 fork 出来的子进程，属于正常现象。）

## 第 8 步：给自己搬个小房间，把大房间腾空

```bash
mkdir $S/manager
echo $$ > $S/manager/cgroup.procs
```

第一行没有输出就是成功。第二行里的两个写法：

- `$$` 是**当前 shell 自己的 PID**
- `>` 是「把左边写进右边」

整句话的意思：**把我自己搬进 `manager` 房间。**

确认大房间空了：

```bash
cat $S/cgroup.procs
```

**什么都输出不出来** —— 空了。

## 第 9 步：再开一次内存开关 —— 成功

```bash
echo +memory > $S/cgroup.subtree_control
cat $S/cgroup.subtree_control
```

```text
memory
```

**同一条命令，刚才失败，现在成功。唯一的变化是：大房间空了。**

从这一刻起，`$S` 下面新建的子目录都带内存限制功能了。

## 第 10 步：造笼子，并设一个 256MB 的上限

```bash
mkdir $S/my_demo
echo 256M > $S/my_demo/memory.max
cat $S/my_demo/memory.max
```

```text
268435456
```

**为什么**：

- `mkdir $S/my_demo` 造出了一个全新的 cgroup 目录，这就是笼子
- `256M` 可以这样简写；内核会把它换算成字节
- `268435456` = 256 × 1024 × 1024，正是 256MB

## 第 11 步：关掉 swap 后门

```bash
echo 0 > $S/my_demo/memory.swap.max
```

**为什么**：不关的话，程序超出 256MB 之后可能被「换出」到硬盘上继续跑，笼子就形同虚设了。

## 第 12 步：把程序放进笼子，看它被杀

先认识一下配套的小程序：

```bash
python3 docs/tutorial/examples/mem_eater.py 12
```

```text
已申请并触碰 12 MB
```

它申请 12MB 就用掉 12MB（会真的去写每一页内存，不是只申请地址）。

现在让它申请 **400MB**，并放进笼子：

```bash
( echo $BASHPID > $S/my_demo/cgroup.procs; exec python3 docs/tutorial/examples/mem_eater.py 400 )
```

你会看到：

```text
Killed
```

**为什么**：它想在 256MB 之上继续申请，内核把它**直接杀掉了**。

这里的两个写法很关键：

| 写法 | 意思 |
|---|---|
| `$BASHPID` | **当前这个子 shell 的 PID**。不能用 `$$` —— `$$` 永远是父 shell 的 PID，写进去就搬错人了 |
| `exec` | 让 python **顶替**这个子 shell，进程号不变，所以 python 一落地就在笼子里 |

外面的一对 `( )` 是「开一个子 shell」，这样被杀的只是子 shell，你的 shell 好好活着。

查一下它的「死因」：

```bash
echo $?
```

```text
137
```

**为什么**：`137` = `128 + 9`。数字 `9` 是 `SIGKILL` 信号的编号，意思是「被强杀」。

## 第 13 步：让程序活着，边跑边看用量

前面那一步，程序是「跑完/被杀之后」我们才去看结果的。现在换个玩法：**让程序活着待几秒，我们盯着用量变化。**

```bash
( echo $BASHPID > $S/my_demo/cgroup.procs; exec python3 docs/tutorial/examples/mem_eater.py 12 5 ) &
sleep 1
cat $S/my_demo/memory.current
```

```text
16244736
```

**为什么**：`memory.current` 是「**此刻**用了多少」，`16244736` 字节 ≈ 15.5MB。

这 15.5MB 里包含：申请的那 12MB、Python 解释器本身、以及几页文件缓存。**cgroup 会把组里所有进程一起算。**

（`&` 表示「丢到后台去跑」，这样它不会占住你的终端；末尾的 `5` 是让程序申请完先保持 5 秒，好让我们有时间观察。）

现在等它自己结束，再量一次：

```bash
wait
cat $S/my_demo/memory.current
```

```text
135168
```

降到约 132KB —— 因为程序已经把内存还回去了。

**`wait` 是必须的**：它会等后台那个程序真正结束。少了这一行，程序可能还活着，下一步读到的数字就不对，最后 `rmdir` 还会报 `Device or resource busy`。

**这里出现了本实验最重要的一课**：`memory.current` 会随着程序的行为**上下波动**，程序一退出就归零。所以：

> **不能靠反复读 `memory.current` 来测量内存峰值。** 你很可能正好错过那零点几秒的高峰。

那该怎么办？答案就是下一步的 `memory.peak`。

## 第 14 步：查成绩 —— 峰值会留下来

程序已经死了，内存也还回去了。但历史最高纪录还在：

```bash
cat $S/my_demo/memory.peak
```

```text
268435456
```

**为什么**：正好等于 256MB，也就是你设的上限。

**这是 cgroup 最有用的一点**：程序什么时候冲上去的、冲到了多少，内核都记着。就算它**立刻把内存还回去**，峰值也不会消失。所以测量内存**不能靠自己一遍遍去读数** —— 你很容易正好错过那零点几秒的高峰。

再看看「事故记录」：

```bash
cat $S/my_demo/memory.events
```

```text
low 0
high 0
max 39
oom 1
oom_kill 1
oom_group_kill 0
sock_throttled 0
```

每一项都是一类事件的**累计次数**。最该记住的两个：

| 名字 | 意思 | 这里 |
|---|---|---|
| `oom` | 发生「内存不足」的次数 | `1` |
| `oom_kill` | 因此**真的杀掉进程**的次数 | `1` |

判定一个程序是不是内存超限（MLE），最硬的证据就是 `oom_kill` 不为 0。

## 第 15 步：清理

```bash
rmdir $S/my_demo
```

没有输出就是成功。

**为什么能删**：规则是「有进程的组不能删」。笼子里的 python 已经被杀，笼子是空的，所以能删。

> 如果这里报 `Device or resource busy`，说明笼子里还有东西活着。先看看 `cat $S/my_demo/cgroup.procs` 是谁，把它搬出去或杀掉再删。

## 第 16 步：离开这块地

```bash
exit
```

**为什么**：回到你原来的 shell。systemd 会把整个 scope 连同里面的一切自动收走，**不需要你再清理**。

---

## 第一层小结

```text
1.  mount | grep cgroup              确认是 cgroup v2
2.  systemd-run --user --scope -p Delegate=yes -- bash
                                    申请一块归你所有的地
3.  cat /proc/self/cgroup            我在哪
4.  S=/sys/fs/cgroup$(awk ...)       记住这条路
5.  ls -ld $S                        地归我了
6.  echo +memory > .../subtree_control   ✗ Device or resource busy
7.  cat $S/cgroup.procs              因为我自己还在里面
8.  mkdir $S/manager; echo $$ > …    搬进小房间，腾空大房间
9.  echo +memory > .../subtree_control   ✓ 成功了
10. mkdir $S/my_demo; echo 256M > …/memory.max
11. echo 0 > …/memory.swap.max       关掉 swap 后门
12. ( echo $BASHPID > …; exec python3 mem_eater.py 400 )   → Killed
13. cat $S/my_demo/memory.current    边跑边看：15.5MB → 132KB（会波动！）
14. cat memory.peak / memory.events  峰值 268435456，oom_kill 1
15. rmdir $S/my_demo                 清理
16. exit                             离开
```

**两个反直觉的地方**（也是整个实验最容易卡住的两处）：

1. **第 6 步会失败**：开内存开关之前，必须先把「自己」从要开开关的目录里搬走。
2. **第 12 步的 `$BASHPID` 不能换成 `$$`**：`$$` 永远是父 shell 的 PID，会把笼子套到错误的人身上。

---

# 第二层 · 连成流程：整段粘贴，学会「检查」

第一层的 15 步太啰嗦了。把命令连起来，整段粘贴就能一次跑完 —— 但**不能只是机械地连起来**，还要加上第一层没有的东西：**检查每一步有没有成功**。

先在**第一层第 2 步那个委派 shell 里**（还没 `exit` 的话就继续用），把下面整段粘贴进去：

```bash
S=/sys/fs/cgroup$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)

# 1. 准备：腾空大房间，打开 memory 开关
mkdir -p "$S/manager"
echo $$ > "$S/manager/cgroup.procs"
echo +memory > "$S/cgroup.subtree_control"

# 2. 造笼子，设 256MB 上限
mkdir "$S/my_demo"
echo 256M > "$S/my_demo/memory.max"
echo 0    > "$S/my_demo/memory.swap.max"

# 3. 放程序进去（被 OOM 杀掉时退出码是 137，不能当成错误中断）
( echo $BASHPID > "$S/my_demo/cgroup.procs"; exec python3 docs/tutorial/examples/mem_eater.py 400 ) || true

# 4. 读成绩并判定
peak=$(cat "$S/my_demo/memory.peak")
kills=$(awk '$1=="oom_kill"{print $2}' "$S/my_demo/memory.events")
echo "peak=$peak oom_kill=$kills"
if [[ $kills -gt 0 ]]; then echo "结果：MLE（有 OOM 事件）"
elif [[ $peak -gt 268435456 ]]; then echo "结果：MLE（峰值超限）"
else echo "结果：OK"; fi

# 5. 清理
rmdir "$S/my_demo" && echo "清理完成"
```

你会看到：

```text
Killed
peak=268435456 oom_kill=1
结果：MLE（有 OOM 事件）
清理完成
```

> 那个 `Killed` 是 shell 在通知你「刚才那条命令被信号杀掉了」，**不是错误**。
> 在交互式 shell 里粘贴时只有 `Killed` 两个字；如果你把这段存成脚本文件运行，
> 行首还会多出脚本名和行号，那是正常现象。

## 这一层比第一层多了什么

**① `peak=$(...)` —— 把命令的输出存进变量**

第一层你是一行行「读」，现在是「读出来存着，后面用」。这就是从「手动」走向「自动」的第一步。

**② `awk '$1=="oom_kill"{print $2}'` —— 从文件里挑出想要的那个数字**

`memory.events` 是一行行的「名字 数字」。这句话的意思是「找到第一列是 `oom_kill` 的那一行，打印第二列」。`$(...)` 再把结果存进 `kills`。

**③ `|| true` —— 别把「被杀」当成「出错」**

这是最容易踩的坑。加上 `set -e`（出错就停）之后，程序被 OOM 杀掉会返回 137，shell 会认为「出错了」而**立刻中止整个流程**，后面的读成绩、清理全都不会执行。

我们要的是：**被杀是预期结果，不是错误。** 所以用 `|| true` 把它「吞掉」。

**④ `if ... elif ... else` —— 自动判定**

第一层是你自己看数字判断；现在让脚本自己判断。判断顺序很重要：

- **先看 `oom_kill`**：有 OOM 事件 → 直接判 MLE
- **再看峰值**：程序跑完了但峰值超限 → 也判 MLE
- 都不是 → OK

**为什么 OOM 要排在前面？** 因为被 OOM 杀掉时，峰值会**停在上限附近**（这里正好 268435456），这个数字已经不代表程序「真的想要多少内存」了。所以有 OOM 事件时，那才是更准确的结论。

## 如果忘了检查会怎样

把上面第 1 段的最后一行改成错误的（比如忘了 `mkdir $S/manager`），你会看到：

```text
write error: Device or resource busy
```

然后第 2 段 `mkdir "$S/my_demo"` 还是「成功」了，但接着：

```text
…/my_demo/memory.max: Permission denied
```

**因为控制器没打开，`memory.max` 这个文件根本不存在。** 报错信息还是 `Permission denied`，跟真正的原因（开关没开）差了十万八千里。

这就是为什么**每一步都要检查成败**：错误会传递，但错误信息经常指向错误的方向。

> 想更严谨的话，把关键的 `echo`、`mkdir` 都加上检查，失败就打印原因并停下（第三层的脚本就是这么做的）。

## 第二层的小结

- 第一层教你「每一步在做什么」，第二层教你「怎么让它们连起来还能对」
- 三个新东西：**存变量**、**挑数字**、**区分「预期结果」和「真错误」**
- 但这段流程只能在**已经委派好的 shell 里**跑。如果你直接在一个新终端里粘贴，第 1 步就会失败。

下一层解决这个问题。

---

# 第三层 · 写成脚本：自己搞定委派、参数和清理

第二层还有三个不方便的地方：

1. 必须**先手动**进到委派 shell 里
2. 上限 256MB 是**写死**在命令里的
3. 中途按 Ctrl+C，笼子会**留在系统里**

第三层把这三件事都解决掉。脚本就是仓库里的：

```bash
bash docs/tutorial/examples/mem_limit_demo.sh
```

## 直接看它跑

默认参数是「上限 256MB，程序申请 400MB」（会被杀掉）：

```bash
bash docs/tutorial/examples/mem_limit_demo.sh
```

```text
当前 cgroup 不能自己建子组：
  /sys/fs/cgroup/user.slice/user-1000.slice/session-3.scope
请 systemd 开一块带委派的新地盘，重新运行本脚本……

═══════════════════════════════════════════════════════════════
 cgroup 内存限制演示
═══════════════════════════════════════════════════════════════
  我是普通用户：<你的用户名>  (uid=1000)
  我的 cgroup ：/sys/fs/cgroup/.../run-p12345-i67890.scope
  笼子上限    ：256M（268435456 字节）
  申请量      ：400MB

现在把程序放进笼子，让它申请 400MB……

───────────────────────────────────────────────────────────────
 成绩
───────────────────────────────────────────────────────────────
  程序退出码  ：137 （128+9，被 SIGKILL 强杀）
  内存峰值    ：268435456 字节（256 MB）
  笼子上限    ：268435456 字节（256 MB）
  oom         ：1
  oom_kill    ：1

  判定        ：MLE —— cgroup 记录了 OOM 事件（内核把程序杀掉了）
───────────────────────────────────────────────────────────────
```

注意**开头那两行**：它发现当前的 cgroup 不能自己建子组，于是**自己又申请了一次委派，然后重新运行自己**。这就是第一层第 2 步的自动化。

## 换个参数：不超限的情况

让程序只申请 100MB，上限还是 256MB：

```bash
bash docs/tutorial/examples/mem_limit_demo.sh --eat 100
```

```text
  程序退出码  ：0
  内存峰值    ：108503040 字节（103 MB）
  笼子上限    ：268435456 字节（256 MB）
  oom         ：0
  oom_kill    ：0

  判定        ：OK —— 没有超过上限
```

**这次程序正常跑完了**（退出码 0），峰值 103MB 没碰到 256MB。对比一下上一个例子 —— 同一个脚本，改一个数字，一个是 OK 一个是 MLE。

## 再换个参数：换个上限

```bash
bash docs/tutorial/examples/mem_limit_demo.sh --limit 64M --eat 200
```

```text
  笼子上限    ：64M（67108864 字节）
  申请量      ：200MB
  程序退出码  ：137 （128+9，被 SIGKILL 强杀）
  内存峰值    ：67108864 字节（64 MB）
  oom_kill    ：1

  判定        ：MLE —— cgroup 记录了 OOM 事件（内核把程序杀掉了）
```

## 脚本比第二层多了什么

**① 自举：发现没被委派，就自己重新运行自己**

```bash
if ! can_manage_here "$S"; then
    exec systemd-run "${user_flag[@]}" --quiet --scope -p Delegate=yes -- \
        bash "$SELF" "${ORIG_ARGS[@]}"
fi
```

判断办法不是去看路径名字，而是**真的动手试一次**：能不能在这里建一个子目录？能建就说明这块地被委派给我们了（试完马上删掉）。

这里有个真实的坑值得说：**`bash "$SELF" "${ORIG_ARGS[@]}"` 用的是 `ORIG_ARGS`，不是 `"$@"`。** 因为前面解析参数的 `while ... shift` 循环会把 `"$@"` 一个个吃掉，等走到这一行时它已经**空了**。用 `"$@"` 的结果是：重新运行后所有参数都变回默认值 —— 你写 `--limit 64M` 却得到 256MB，而且**不报任何错**。

（这个 bug 我第一版就写出来了，三个不同参数跑出一模一样的结果才发现。）

**② `trap cleanup EXIT`：不管怎么退出都清理**

```bash
cleanup() {
    if [[ -d $BOX ]]; then
        echo 1 >"$BOX/cgroup.kill"        # 杀掉笼子里所有进程
        ...                                # 等 populated 归零
        rmdir "$BOX"
    fi
}
trap cleanup EXIT
```

`trap ... EXIT` 的意思是「不管脚本因为什么原因结束（正常跑完、报错退出、你按 Ctrl+C），都执行 `cleanup`」。

**这一点第二层做不到**：第二层的 `rmdir` 写在最后一行，如果中途 Ctrl+C，那行永远不会执行，笼子就留在系统里了。

另外注意 `cleanup` 里用的是 `cgroup.kill` 而不是 `kill`：

| 做法 | 能不能杀掉「逃走的」后代 |
|---|---|
| `kill -9 <PID>` | 不能。程序可以 `setsid` 逃出进程组 |
| `echo 1 > cgroup.kill` | **能**。cgroup 的成员关系逃不掉 |

**③ 参数化**

```bash
--limit 256M      # 笼子上限
--eat 400         # 程序申请多少 MB
--hold 5          # 申请后保持几秒（方便观察）
```

**④ 自动判定 + 打印报告**

和第二层的 `if / elif / else` 是同一个逻辑，只是排版成了表格，并把上限、峰值、退出码都列出来，方便对照。

## 补一个容易忽略的点：判定线和保护上限可以是两个值

前面所有实验里，`memory.max` **既是判定线又是硬上限** —— 同一个数字。所以你只见到两种结局：没碰到上限就是 OK，碰到了就被杀掉、判 MLE。

但真实评测机把这两件事**拆成两个值**，因为它们目的不同：

| | 作用 | 设成多少 |
|---|---|---|
| 判定线 | 超过就判 MLE，这是**评分标准** | 题目给的 128MB |
| 保护上限 `memory.max` | 超过就杀掉，这是**保护机器的保险** | 判定线 + 一点余量 |

**为什么要留余量**：让「只超一点点」的程序能**跑完**，从而拿到精确的峰值数字来判定；只有「超得太狠」才会被硬杀掉。

仓库里的 [cgroup_demo.py](examples/cgroup_demo.py) 就是演示这个的（它是第 8 章的实验）。它把判定线设成 8MiB、余量 16MiB，于是 `memory.max = 24MiB`：

```bash
make -C docs/tutorial/examples      # 它要调用 .build/allocate
systemd-run --user --quiet --scope -p Delegate=yes -- \
  python3 examples/delegated.py python3 docs/tutorial/examples/cgroup_demo.py
```

```text
requested=4MiB   verdict=OK   peak=4.8MiB   exit=0 signal=0 oom=0 oom_kills=0
requested=12MiB  verdict=MLE  peak=12.5MiB  exit=0 signal=0 oom=0 oom_kills=0
requested=64MiB  verdict=MLE  peak=24.0MiB  exit=0 signal=9 oom=1 oom_kills=2
```

**重点看第二行**：判定是 MLE，但 `exit=0`、`signal=0`、`oom=0` —— **程序正常跑完了，一个 OOM 事件都没有**。

因为它的峰值 12.5MiB：

- 越过了 **8MiB 判定线** → 所以判 MLE
- 没碰到 **24MiB 硬上限** → 所以没被杀

这正是「两个阈值」才会产生的现象。

### 这解释了我们判定逻辑里的一个疑问

回头看前面那段判定：

```bash
if [[ $kills -gt 0 ]]; then          echo "MLE（有 OOM 事件）"
elif [[ $peak -gt 268435456 ]]; then echo "MLE（峰值超限）"   # ← 本文的实验走不到这里
else echo "OK"; fi
```

在**本文的 demo 里，第二个分支永远走不到**：因为 `memory.max` 就等于判定线，峰值一旦超过它，内核早就把程序杀了，第一个分支必然先命中。

只有把两个阈值拆开（像 `cgroup_demo.py` 那样），第二个分支才真正有用武之地 —— 它处理的就是上面 12MiB 那种「跑完了但超线」的情况。

**那顺序为什么是 OOM 优先？** 因为被 OOM 杀掉时，峰值会**停在上限附近**（第三行的 24.0MiB 就是 `memory.max` 本身），这个数字已经不代表程序真实想要多少内存了。这时 OOM 事件才是更准确的结论。

> 想看得更细：[第 8 章](08-cgroup-memory.md) 第三步讲的就是这个实验；[memory_cgroup.py](../../memory_cgroup.py) 里的 `memory_max_bytes()` 是「判定线 + 余量」的算法；[runner.py](../../runner.py) 里的 `_set_verdict` 是那段三支判定的真实实现。

## 第三层小结

| 第二层的问题 | 第三层的解法 |
|---|---|
| 必须先手动进委派 shell | **自举**：自己 `systemd-run` 重跑自己 |
| 参数写死 | `--limit` / `--eat` / `--hold` |
| Ctrl+C 会留下垃圾 | `trap cleanup EXIT` + `cgroup.kill` |
| 要自己看数字 | 自动判定并打印报告 |

---

# 第四层 · 用 Python 监听 OOM

## 为什么还需要这一层

回到第二层的输出：

```text
Killed
```

你的启动程序**只看到这四个字**。它不知道：

- 是被 OOM 杀的吗？还是段错误、还是别的信号？
- 是在什么时候被杀的？
- 当时用了多少内存？

第三层的脚本是「**先等程序结束，再去读成绩**」。但很多时候我们想在**它被杀的那一刻**就知道 —— 比如评测机要立刻终止判题、给出 MLE 结论，而不是傻等超时。

## 轮询 memory.events（最简单的办法）

cgroup v2 把「内存不足」这件事记在 `memory.events` 里。所以监听的办法就是：**每隔一小会儿读一次 `oom_kill`，数字变大了就说明出事了。**

配套程序：`docs/tutorial/examples/cgroup_oom_watch.py`

```bash
python3 docs/tutorial/examples/cgroup_oom_watch.py <cgroup目录> [超时秒数]
```

它的核心就这几行：

```python
while True:
    kills = read_counter(box, "oom_kill")     # 读一次
    if kills > first_kills:                   # 数字变大了
        print("检测到 OOM！")
        return 0
    if time.monotonic() - started >= timeout:
        return 1                              # 超时
    time.sleep(0.02)                          # 歇 0.02 秒再读
```

## 完整实验：一边跑程序，一边监听

在**已经委派好的 shell 里**（或者用第三层脚本的 `--hold` 配合），按这个顺序做：

```bash
S=/sys/fs/cgroup$(awk -F: '/^0::/{print $3}' /proc/self/cgroup)
mkdir -p "$S/manager"; echo $$ > "$S/manager/cgroup.procs"
echo +memory > "$S/cgroup.subtree_control"
mkdir "$S/box"
echo 256M > "$S/box/memory.max"
echo 0    > "$S/box/memory.swap.max"

# ① 监视者先在 box 外面跑起来（& 表示丢到后台）
python3 docs/tutorial/examples/cgroup_oom_watch.py "$S/box" 25 &

# ② 等它准备好
sleep 0.3

# ③ 被监视的程序进入 box
( echo $BASHPID > "$S/box/cgroup.procs"; exec python3 docs/tutorial/examples/mem_eater.py 400 ); echo "程序退出码 = $?"
```

你会看到：

```text
开始监听：/sys/fs/cgroup/.../run-p12345-i67890.scope/box
  起始 oom_kill = 0
  起始峰值      = 0 字节（0.0 MB）
  检测到 OOM！用时 0.41 秒
  oom_kill    ：0 → 1
  oom（总次数）：1
  最终峰值    ：268435456 字节（256.0 MB）
程序退出码 = 137
```

**注意 `用时 0.41 秒`** —— 从起始到捕获只花了不到半秒。0.02 秒一次的轮询完全够快。

清理一下：

```bash
rmdir "$S/box"
```

## ⚠️ 最关键的一条：监视者必须待在笼子**外面**

上面第 ① 步，监视者是跑在 `manager` 房间里的（因为你的 shell 在那里），**它不在 `box` 里**。这是故意的。

如果监视者也被关进 `box`，一旦发生「整组 OOM」的处理方式，**监视者会跟着一起被杀掉**，你什么都监听不到 —— 那个负责报信的人先死了。

这正是真实评测项目的分工方式：

```text
委派的 scope
├── manager/        ← 判题程序（监视者）住这里，安全
└── box/            ← 被评测的程序住这里，可能被杀
```

## 另一种办法：inotify（了解即可）

除了轮询，还可以用 `inotify` 让内核在文件变化时**主动通知**你，不用反复读：

```python
from inotify_simple import INotify, flags
inotify = INotify()
inotify.add_watch(str(box / "memory.events"), flags.MODIFY)
inotify.read()          # 阻塞等待，文件一变就返回
```

好处是**完全不轮询**，反应最快、最省 CPU。代价是要装第三方库 `inotify-simple`：

```bash
pip install inotify-simple
```

**本文推荐用轮询版**：零依赖、看得懂、0.02 秒的间隔已经足够快。等你有性能需求了再换 inotify。

## 第四层小结

- OOM 的证据记在 `memory.events` 的 `oom_kill` 上
- 最简单的监听办法：**轮询这个数字**（本文的 `cgroup_oom_watch.py`）
- 想更省 CPU：用 `inotify` 让内核主动通知
- **监视者一定不能和被监视的程序待在同一个笼子里**

---

# 收尾

## 四层对照表

| | 第一层 手动 | 第二层 流程 | 第三层 脚本 | 第四层 Python |
|---|---|---|---|---|
| 委派 | 手动敲 | 手动敲 | **自动自举** | 手动敲 |
| 参数 | 写死在命令里 | 写死 | **`--limit` / `--eat`** | 写死 |
| 出错怎么办 | 你自己看 | **加检查** | **`trap` + 检查** | 轮询 + 超时 |
| 判定 | 你自己看数字 | `if/elif` | `if/elif` + 报告 | 轮询中实时捕获 |
| 适合场景 | 学习、调试 | 临时跑一次 | **反复使用** | 需要「当场知道」 |

## 和项目代码的对应关系

你在这四层里做的事，就是真实评测项目的全部工作：

| 你做的事 | 项目里的对应代码 |
|---|---|
| 第 2 步：申请委派 | `examples/delegated.py` |
| 第 8～11 步：搬进 manager、开 `+memory`、建组、设上限 | `memory_cgroup.py` 的 `__enter__` |
| 第 12 步：把程序放进组 | `runner_helper.c` 的 `run_child`（它写的是 `0`，意思是「我自己」） |
| 第 14 步：读 `memory.peak` / `memory.events` | `memory_cgroup.py` 的 `memory_result` |
| 第 15 步：清理 | `memory_cgroup.py` 的 `stop` 和 `__exit__` |
| 第四层：监听 OOM | `runner.py` 的 `stop()` + `_set_verdict` |

另外，真实项目还会打开一个本文没用的开关：`memory.oom.group = 1`，意思是**超限时整组一起被杀**，防止程序 fork 出一堆子进程各自钻空子。本文的实验里，肇事程序都是单独一个进程，不开它反而让数字更干净（`oom_kill` 是 1 而不是 2）。

## 常见报错表

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `mkdir: Permission denied` | 没被委派（在会话目录里） | 回到第一层第 2 步重新委派 |
| `write error: Device or resource busy`（写 `subtree_control`） | 当前组里还有进程 | 先 `echo $$ > $S/manager/cgroup.procs` 搬走 |
| `memory.max: Permission denied` | 控制器没打开，文件根本不存在 | 先确认 `cat $S/cgroup.subtree_control` 里有 `memory` |
| `rmdir: Device or resource busy` | 组里还有进程 | 先杀掉/搬走，再删 |
| 程序没被杀，反而跑完了 | 申请量没超过上限 | 调大 `--eat` 或调小 `--limit` |
| 笼子套到了错误的进程 | 用了 `$$` 而不是 `$BASHPID` | 子 shell 里必须用 `$BASHPID` |
| 脚本改了参数却没生效 | 自举时用了 `"$@"` 而不是参数副本 | 见第三层的「真实的坑」 |
| 一堆东西乱掉了想重来 | —— | `exit` 退出委派 shell，systemd 会全部收走 |

## 附录 · 与参考对话（`tmp.md`）的三处差异

本文是在一份豆包对话记录（`tmp.md`）的基础上整理的。整理过程中实测发现原文有三处问题，这里列出来，供你对照：

**① 原文的 `echo "+memory" > ../cgroup.subtree_control` 会失败。**

实测：

```text
$ echo +memory > $S/cgroup.subtree_control
bash: echo: write error: Device or resource busy
```

因为执行这条命令的时候，**你自己还在这个组里**。跟着原文做的后果是：控制器没打开 → `memory.max` 这个文件**根本不存在** → 下一步 `echo 256M > memory.max` 报 `Permission denied`，整条链路断掉，而且报错信息和真实原因完全对不上。

**正确做法**：先 `mkdir $S/manager; echo $$ > $S/manager/cgroup.procs` 把自己搬进叶子目录，再开开关。

**② 原文说不做管理员配置（`user@.service` 的 `Delegate=yes`）就用不了，这不准确。**

实测本机：

```bash
$ systemctl --user show user@$(id -u).service -p Delegate
Delegate=no
```

`Delegate=no`，但本文第一到第四层的实验**全部跑通**。

真正起作用的是**每条命令里的 `-p Delegate=yes`**（per-scope 委派），不是用户管理器那个属性。所以本文的写法是：直接用 `-p Delegate=yes`；**只有当 `mkdir` 或写 `subtree_control` 真的报权限不足时**，才去检查 `user@.service` 那个设置。

**③ 原文全程用 `cd` + `../` 相对路径，非常容易搞错。**

原文的流程是 `cd 到 scope 目录` → `mkdir my_demo` → `cd my_demo` → `echo ... > ../cgroup.subtree_control`。

问题在于 `..` 指到哪里，完全取决于你**当前站在哪**。我自己照着写的时候就在这上面摔了：`cd` 之后 `../` 指错了地方，命令「看起来成功了」，其实写进了无关的目录。

本文全程用**绝对路径** `$S/...`，从根本上避免这一类错误：

```bash
echo +memory > $S/cgroup.subtree_control      # 明确指向哪儿，不受 cwd 影响
```

**一条通用建议**：在操作 `/sys/fs/cgroup` 的时候，尽量不要用 `..`。那里的目录层级深、名字相似，一旦指错，你得到的往往不是「找不到文件」，而是一个看起来成功的错误操作。

---

## 相关阅读

- [第 8 章 · 一次提交的内存，怎样交给内核管理](08-cgroup-memory.md) —— 真实评测项目怎么用这套机制
- [第 7 章 · 信号、权限与清理](07-signals-and-cleanup.md) —— 想弄清「杀掉进程」和「清理整组」的区别
- [memory_cgroup.py](../../memory_cgroup.py) —— 本文做的每一件事，在这个文件里都能找到对应代码
