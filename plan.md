# 实施计划：从 Python runner + C helper 迁移到 judge + C executor

设计依据：[refactor.md](refactor.md)。本文件是待执行清单，编写计划不表示代码或测试已经完成。

目标结构为 `judge.py`、`memory_cgroup.py`、`executor.c`；C 构建产物为 `executor`。移除 `runner.py`，Python 侧调用适配放在 judge 中。

## 工作约定

- 按步骤顺序实施，每步完成对应验证后再进入下一步；建议每步独立提交。
- 改名、协议拆分、职责迁移分开做。中间版本可以暂时保留旧内部函数名，但每步结束时构建和现有测试应能运行。
- 不修改教程 Markdown、教程配套示例、题目数据和用户已有的无关改动。本轮也不安排 README 改写。
- 保留现有评测语义，只改变职责归属和源码入口。不要顺便添加编译隔离、新 checker、并行评测或新的 systemd 流程。
- 不保留长期的 `runner.py` / `local_judge.py` 兼容转发模块。已安装的 shell 命令 `roj-local-judge-lite` 保持名称。
- 本计划中的文件名和函数名用于指导本次迁移；遇到签名调整时优先维护职责和行为契约，不为匹配示意代码增加抽象。

## 步骤 0：记录行为基线

目的：区分重构引入的回归与机器环境、已有代码的问题。

- [ ] 查看工作区状态，记录已有改动；只提交属于重构的文件。
- [ ] 执行 `make` 与 `make check`，记录成功、失败与跳过项。
- [ ] 记录现有 CLI 对 AC、WA、RE、CE、TLE、SE 和取消的输出、退出码。
- [ ] 阅读现有执行测试中的 cgroup、清理、路径、权限、资源边界场景，后续迁移保留这些行为断言。

```bash
make
make check
python3 local_judge.py --list
python3 local_judge.py --pid 1000 --testdata testData --checker none --no-cgroup testData/1000/std.cpp
```

有 systemd 委派能力的普通用户，可额外记录完整 cgroup 基线：

```bash
systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py make check
```

root 使用系统管理器，去掉 `--user`。没有委派环境时明确记为未验证，不能把相关测试跳过当成通过。

**完成条件：** 知道现有行为和测试覆盖边界。这一步不要求改变实现，也不必为了记录结果单独创建代码提交。

## 步骤 1：评测入口改名为 judge.py

建议提交：`refactor: rename local judge entry to judge`

涉及：`local_judge.py`、测试、`install.sh`、`docker/`、`examples/delegated.py` 中有关入口的说明。

- [ ] 将 `local_judge.py` 改名为 `judge.py`，此时仍调用现有 runner。
- [ ] 更新 argparse 的程序名、错误示例、自动委派重新执行的目标及对应测试。
- [ ] 更新测试中的 import、patch 对象和 subprocess 入口。
- [ ] 更新安装源检查、import 冒烟测试、cgroup 探测、列题命令和生成的启动器。
- [ ] 更新 Docker 启动脚本对评测入口的引用。
- [ ] 此步骤不改 runner、C 协议或判定逻辑，不新增兼容入口。

验证：

```bash
make check
python3 judge.py --list
python3 judge.py --pid 1000 --testdata testData --checker none --no-cgroup testData/1000/std.cpp
```

检查 `test_install.py` 临时源码夹具已拷贝 `judge.py`。安装测试必须实际验证新启动器指向新文件，不能只确认启动器存在。

**完成条件：** 原评测入口的行为不变，所有运行代码已能通过新名称进入；runner 暂时保留。

## 步骤 2：C helper 正式改名为 executor

建议提交：`refactor: rename native helper to executor`

涉及：`runner_helper.c`、`runner.py`、`Makefile`、`.gitignore`、安装和测试代码、相关 Docker 脚本。

- [ ] 将 C 源码改名为 `executor.c`，构建目标改为 `executor`。
- [ ] 修改 Python 默认二进制路径；内部变量逐步使用 executor 命名。
- [ ] 更新 Makefile 的 all/check/clean 依赖和目标。
- [ ] 更新 `.gitignore`，排除 `/executor`；处理工作区遗留旧构建产物，避免误用。
- [ ] 更新安装阶段删除旧产物、检查新产物的逻辑。
- [ ] 更新 `test_install.py` 中模拟 make 生成的文件名和安装结果断言。
- [ ] 更新 C 报错、代码内契约说明与测试的二进制路径。
- [ ] 保留现有位置参数、JSON 字段、fork/exec/wait4 和错误管道实现。

验证：先清理已确认是构建产物的旧文件，再运行 `make`、`make check` 与步骤 1 的 A+B 冒烟命令。确认删除新二进制后能从源码重新构建，避免测试依赖遗留 `runner_helper`。

**完成条件：** C 程序只换了名字，执行行为不变；Python 此时仍可通过 runner 调用它。

## 步骤 3：先将执行事实从判定结果中分开

建议提交：`refactor: separate execution reports from verdicts`

涉及：暂时仍在 `runner.py` 中的调用适配与结果类型、执行报告测试。

- [ ] 增加仅包含 C JSON 七个字段的 `ExecutionReport`，不含 verdict 和内存统计。
- [ ] 将 `_invoke_helper()` 收敛为 `invoke_executor()`：构造/接收必要执行参数、启动、等待、处理取消、解析报告。
- [ ] 明确 executor 非零退出和无效报告的异常契约，不把它们伪装成用户程序非零退出。
- [ ] 保留必要字段和类型检查。空对象、缺字段、错误类型不能生成默认成功报告。
- [ ] 现有 `run_case()` 暂时将报告适配到旧 `CaseResult`，继续执行原判定，保持上层可用。
- [ ] C 成功报告协议不变；正常报告中不增加 `verdict` 或 `execution_error` 字段。基础执行故障继续走退出码与 stderr，由 Python 转换成异常。

重点验证：

- [ ] 提交输出任意内容都不会污染 executor JSON；stderr 两条通道仍分离。
- [ ] 提交退出 126/127 与启动/重定向失败仍可区分。
- [ ] 正常退出、信号、wall 超时均返回事实；不是 executor 自身故障。
- [ ] executor 缺失、非零退出、非法 JSON、缺字段得到明确执行错误。
- [ ] Ctrl+C 仍先通知 executor，等待其清理，必要时强杀并回收。

运行针对性报告测试和已有执行测试。新增测试只覆盖协议边界与取消行为，不断言内部函数调用顺序。

**完成条件：** 调用适配已经不判题，旧 judge 流程仍工作；这里的类型和函数将在下一步搬到 judge。

## 步骤 4：把 Python 评测职责迁入 judge，删除 runner.py

建议提交：`refactor: move case orchestration into judge`

此步骤是完整的依赖切换：不要通过 runner 反向导入 judge 来维持旧 API，也不要形成循环导入。

- [ ] 从 judge 移除对 runner 的导入。
- [ ] 将限制配置、报告类型、结果类型、路径与身份校验、环境构造、调用适配、运行事务和判定代码迁入 judge。
- [ ] 保留单一实现，不同时维护新旧两套 `run_case()`。
- [ ] 更新所有运行代码和测试导入，然后删除 `runner.py`。
- [ ] 删除旧 Python runner CLI；把它的“报告和用户输出分离”测试改为直接验证 executor 协议。
- [ ] 更新安装 import 冒烟测试和 `test_install.py` 的源码夹具列表。
- [ ] 暂时保留 `run_case()`、`_set_verdict()` 等内部名称可以接受；本步不同时改写全部流程。

验证：`make check`、列题、A+B 和已有 CLI 结果测试。全仓运行代码中应无 `import runner` 或对 `runner.py` 的 subprocess 调用；教程目录中的引用不计入本轮检查。

**完成条件：** Python → C executor 的调用只存在一处，judge 独立完成原有评测；runner 已移除，功能尚未改变。

## 步骤 5：集中限制计算与纯判定规则

建议提交：`refactor: centralize judge limits and verdict rules`

- [ ] 在 judge 中用 `make_protection_limits()` 集中处理余量、单位换算、0 值和整数范围校验。
- [ ] 明确原始题目阈值与实际保护值，executor 参数只使用后者。
- [ ] 用 `classify_execution(report, memory, limits)` 替代修改结果对象的 `_set_verdict()`；返回执行结局和原因。
- [ ] 判定函数不读文件、不启动进程、不操作 cgroup、不打印。
- [ ] 基础设施故障由事务边界映射为 SE；完整报告按设计中的 OOM → wall → 峰值 → CPU/SIGXCPU → 信号 → 退出码 → OK 分类。
- [ ] 无 cgroup 时用 `memory=None` 表示未测量，绝不使用 RSS 补做 MLE 判定。
- [ ] `ExecutionReport` 只表达事实，`CaseResult` 承载 judge 的结果；展示函数按新结构读取数据。
- [ ] 保留 CPU 毫秒展示舍入和内存 KiB 展示换算；判定仍使用精确值。

针对纯函数进行参数化验证：

| 场景 | 期望 |
|---|---|
| CPU 恰好等于题目阈值 | 不因 CPU 判 TLE |
| CPU 超过阈值 1 微秒 | TLE |
| 内存峰值恰好等于阈值 | 不因峰值判 MLE |
| 内存峰值超过阈值 1 字节 | MLE |
| OOM 与 wall 同时出现 | MLE |
| wall 与峰值超限同时出现，无 OOM | TLE |
| SIGKILL，无 OOM 或其他超限证据 | RE |
| SIGXCPU | TLE |
| 无 cgroup，RSS 很大 | 不因此判 MLE |
| 时间/内存配置为 0 | 对应限制按原规则关闭 |

迁移现有边界测试，补充缺失的混合证据测试，不删除原断言来适应新实现。

**完成条件：** 评测优先级和保护余量各只有一个定义位置，修改规则不需要进入 C 或 cgroup 模块。

## 步骤 6：组织清楚的评测主流程与测试点事务

建议提交：`refactor: make judge workflow and cleanup explicit`

- [ ] 将主流程整理为 `main()` → `judge_submission()` → `judge_case()`。
- [ ] `main()` 保留参数/输入检查和环境选择；`--help`、`--list`、无效输入不会启动提交或不必要地尝试委派。
- [ ] `judge_submission()` 管工作目录、编译一次、逐点调用与汇总；保留 `--keep-work-dir` 和原退出码。
- [ ] `judge_case()` 用 `with MemoryCgroup(...)` 管一个测试点；无 cgroup 使用空上下文，执行调用只写一份。
- [ ] 正常路径严格按 executor 返回 → 停止残留后代 → 读取内存 → 删除组 → 判定 → 比较答案执行。
- [ ] 只有内部 OK 才调用 checker，最终测试点结果不暴露 OK。
- [ ] 将旧 `run_case()` 中已被新主流程替代的组织代码删除，避免层层转发。
- [ ] 确保进入上下文失败、executor 故障、统计失败、删除失败、Ctrl+C 都有明确收尾与错误传播。
- [ ] 清理错误不能吞掉原始错误；Ctrl+C 清理失败时保留取消语义并报告清理问题。
- [ ] `MemoryCgroup` 仍独立实现文件操作，不导入 judge，不返回评测 verdict。

重点验证：

- 正常执行、RE、TLE、MLE 后测试点组无存活进程；可清理时目录已删除。
- 主进程正常退出但留下后台后代时仍清理；有 cgroup 时覆盖 setsid 后代。
- executor 启动失败、报告错误和用户取消都经过资源收尾。
- setup 阶段阻塞仍受 wall 看门狗约束。
- 配置/路径错误发生在执行前，标准流不会因别名关系被意外截断。
- C++/Python 编译、AC/WA/CE/RE/TLE、内存判定和工作目录保留行为仍符合基线。

真实内核行为用现有集成测试验证。难以稳定触发的读统计、删除失败等场景可做边界故障注入，但不能用 mock 成功代替实际 cgroup 清理验证。

**完成条件：** 读者只看 `judge_case()` 就能看出生命周期；只看 `judge_submission()` 就能看出完整评测顺序。

## 步骤 7：共用委派准备逻辑，保持现有启动链

建议提交：`refactor: reuse delegated scope preparation`

- [ ] 环境检查、探测、scope 准备和重启逻辑继续集中在 judge 的环境准备部分。
- [ ] `examples/delegated.py` 通过脚本位置定位仓库根目录，导入 judge 的 `prepare_delegated_scope()`；不依赖调用者当前目录。
- [ ] 删除辅助脚本重复的 scope 独占性、可写性、controller 检查与创建规则。
- [ ] 辅助脚本继续支持任意命令和 `make check`，保留当前退出码传递方式。
- [ ] judge 内的探测与真实运行仍共用准备路径；本轮不减少 exec 次数，不改变 `--in-scope -- <命令>` 语义。
- [ ] 保留 root 与普通用户的 systemd 参数区别、禁止重复委派、显式 cgroup 路径选择和降级行为。

验证委派测试及实际委派下的 `make check`。测试既要覆盖成功，也要覆盖准备失败、无 systemd 环境和不允许再次委派的路径。一次探测成功不是后续创建 scope 永远成功的保证，真实启动错误仍需处理。

**完成条件：** scope 准备规则只有一份；环境准备保持在 judge 一侧，没有侵入 executor。

## 步骤 8：整理测试归属并完成工程验收

建议提交：`test: align coverage with judge and executor boundaries`

测试在前面各步同步迁移，本步负责归组和最后核对，不把所有回归留到本步才发现。

- [ ] 将现有测试按职责整理为 `test_executor.py`、`test_judge.py`、`test_install.py`，更新 Makefile 和 Docker 测试入口。
- [ ] executor 测试断言原始报告、文件重定向、资源保护、信号和进程回收。
- [ ] judge 测试断言限制计算、判定优先级、cgroup 生命周期、完整评测、自动委派与数据发现。
- [ ] 保留原共享测试在有/无 cgroup 两种模式下的行为覆盖，避免重组时丢掉清理、权限、路径和边界场景。
- [ ] 安装测试保留“检查失败不得替换已有安装”的保证，增加或保留启动器目标检查。
- [ ] 搜索运行代码中的旧文件引用、旧构建产物路径与旧 CLI 调用，逐个处理。
- [ ] 从非仓库工作目录运行入口，验证数据查找和 executor 默认路径不依赖当前目录。

最终验证命令：

```bash
make clean
make
make check
python3 judge.py --list
python3 judge.py --pid 1000 --testdata testData --checker none --no-cgroup testData/1000/std.cpp
systemd-run --user --scope -p Delegate=yes -- python3 examples/delegated.py make check
```

在临时安装目录和临时 bin 目录中执行安装冒烟测试，从其他工作目录调用生成的 `roj-local-judge-lite`；不要为了验收覆盖个人正在使用的安装。Docker 可用时执行 `make docker-check` 并验证 Docker 启动脚本的新入口。

委派或 Docker 环境不可用时记录具体未验证范围；涉及真实 cgroup 的验收需在可用环境补齐。

**完成条件：** 所有可用环境的回归通过，未验证范围明确，设计中的模块边界已经落实。最终代码中没有 runner Python 中间层，也没有旧 API 兼容壳。

## 最后一次人工阅读

- [ ] `judge_submission()` 中能直接看到编译、循环与汇总，没有系统调用细节。
- [ ] `judge_case()` 中能直接看到创建、执行、停止、统计、清理、判定与比较。
- [ ] `invoke_executor()` 只处理调用协议和进程通信，不包含题目判定。
- [ ] `classify_execution()` 无副作用，所有结果优先级集中可见。
- [ ] C executor 不读取题目配置、不知道标准答案、不输出评测标签。
- [ ] cgroup 模块只管理自己创建的子组，未测量内存与真实零占用不会混淆。
- [ ] 没有引入额外包、后端接口、继承体系或仅为转发而存在的模块。

## 本轮不做

教程及其配套示例的迁移、README 改写、编译 cgroup 隔离、移除自动委派的中间 exec、新的执行协议或公开 C CLI、Python API 的旧版兼容、并行评测与评分规则调整。
