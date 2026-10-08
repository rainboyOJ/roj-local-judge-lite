/*
 * Python -> exec helper -> fork -> exec 用户程序。
 *
 * exec 会保留历史 ru_maxrss，fork 才会建立新的进程统计。因此必须先进入这个
 * 小型 helper，再 fork 被测进程。只统计被测进程，不能统计 helper 自己。
 * 仍有 C 子进程 exec 前的小内存基线，但不再受大 Python 父进程的内存影响。
 *
 * 读代码时先分清三个角色：Python 决定策略，helper 监控，被测子进程执行。
 * fork 后父子从同一位置继续，但拥有各自的地址空间；子进程的 chdir、限额、
 * 降权不会改变 helper。exec 则用用户程序替换子进程，不会创建新的 PID。
 * cgroup 是可选能力：不入组时仍走相同的启动、限额、监控与回收流程。
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

struct Options {
  rlim_t cpu_seconds, stack_bytes, output_bytes, nproc;
  long long wall_ms;
  int drop_privileges;
  uid_t uid;
  gid_t gid;
  const char *cwd, *input, *output, *error, *cgroup_procs;
  char **command;
};

struct Result {
  int status;
  int timed_out;
  long long real_time_ms;
  struct rusage usage;
};

/* 私有管道区分 setup 失败与用户程序 exit(126/127)。操作名直接传递，
 * Python 不需要维护另一份错误编号表。这里只传本文件中的固定操作名。 */
struct SetupError {
  char operation[32];
  int number;
};

static volatile sig_atomic_t interrupted = 0;

static void on_signal(int signo) {
  interrupted = signo;
}

static void fail(const char *operation) {
  perror(operation);
  exit(125);
}

static long long monotonic_ms(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) fail("clock_gettime");
  return (long long)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static unsigned long long parse_number(const char *text) {
  char *end;
  errno = 0;
  unsigned long long value = strtoull(text, &end, 10);
  if (*text == '-' || !*text || *end || errno || value >= LLONG_MAX) {
    fprintf(stderr, "invalid helper argument: %s\n", text);
    exit(125);
  }
  return value;
}

static struct Options parse_options(int argc, char **argv) {
  if (argc < 15) {
    fprintf(stderr, "runner_helper must be invoked by runner.py\n");
    exit(125);
  }
  unsigned long long drop = parse_number(argv[7]);
  unsigned long long uid = parse_number(argv[8]);
  unsigned long long gid = parse_number(argv[9]);
  if (drop > 1 || uid >= (uid_t)-1 || gid >= (gid_t)-1) {
    fprintf(stderr, "invalid uid/gid/drop_privileges\n");
    exit(125);
  }
  /* 只有这里依赖参数顺序；其余代码都使用有含义的字段名。 */
  return (struct Options){
      /* 空参数表示显式禁用 cgroup；非空路径打不开时必须报错，不能自动降级。 */
      .cpu_seconds = parse_number(argv[1]),
      .cgroup_procs = *argv[2] ? argv[2] : NULL,
      .stack_bytes = parse_number(argv[3]),
      .output_bytes = parse_number(argv[4]),
      .nproc = parse_number(argv[5]),
      .wall_ms = parse_number(argv[6]),
      .drop_privileges = drop,
      .uid = uid,
      .gid = gid,
      .cwd = argv[10],
      .input = argv[11],
      .output = argv[12],
      .error = argv[13],
      .command = &argv[14],
  };
}

static void setup_failed(int fd, const char *operation) {
  struct SetupError error = {.number = errno};
  snprintf(error.operation, sizeof(error.operation), "%s", operation);
  ssize_t count;
  do {
    count = write(fd, &error, sizeof(error));
  } while (count < 0 && errno == EINTR);
  /* fork 后失败用 _exit：不运行继承的 atexit 回调，也不重复冲刷 stdio 缓冲。 */
  _exit(126);
}

static void apply_limit(int fd, const char *name, int resource, rlim_t amount) {
  /* 0 保持继承值，CORE 例外。CPU 的 hard 多留一秒，先让 soft 发 SIGXCPU。 */
  if (amount == 0 && resource != RLIMIT_CORE) return;
  struct rlimit value = {amount, amount};
  if (resource == RLIMIT_CPU) value.rlim_max++;
  if (setrlimit(resource, &value) < 0) setup_failed(fd, name);
}

static void redirect_stream(int error_fd, const char *name, const char *path, int target, int flags) {
  int fd = open(path, flags, 0600);
  if (fd < 0 || dup2(fd, target) < 0) setup_failed(error_fd, name);
  if (fd != target) close(fd);
}

static void run_child(const struct Options *options, int error_fd, pid_t helper_pid) {
  /* 1. 独立进程组让超时/取消可以一次清理同组后代。恢复 helper 改过的信号。 */
  struct sigaction action = {.sa_handler = SIG_DFL};
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGTERM, &action, NULL) < 0 ||
      sigaction(SIGINT, &action, NULL) < 0 ||
      sigaction(SIGHUP, &action, NULL) < 0) setup_failed(error_fd, "sigaction");
  if (setpgid(0, 0) < 0) setup_failed(error_fd, "setpgid");
  /* 用户程序执行前先入组，随后 exec/分配的内存都由该组记账。
   * 写 0 表示迁移当前进程；helper 和 Python 始终留在组外。
   * 在降权前完成迁移，避免 nobody 没有 cgroup 写权限。 */
  if (options->cgroup_procs != NULL) {
    int group_fd = open(options->cgroup_procs, O_WRONLY | O_CLOEXEC);
    if (group_fd < 0) setup_failed(error_fd, "open cgroup.procs");
    if (write(group_fd, "0", 1) != 1) setup_failed(error_fd, "join cgroup");
    close(group_fd);
  }
  if (chdir(options->cwd) < 0) setup_failed(error_fd, "chdir");

  /* 2. 先打开文件，之后再降权。helper 的 stdout 留作报告，不传给用户程序。 */
  redirect_stream(error_fd, "redirect stdin", options->input, 0, O_RDONLY);
  redirect_stream(error_fd, "redirect stdout", options->output, 1, O_WRONLY | O_CREAT | O_TRUNC);
  redirect_stream(error_fd, "redirect stderr", options->error, 2, O_WRONLY | O_CREAT | O_TRUNC);

  /* 3. 设置限制，然后降权。 */
  apply_limit(error_fd, "RLIMIT_STACK", RLIMIT_STACK, options->stack_bytes);
  apply_limit(error_fd, "RLIMIT_FSIZE", RLIMIT_FSIZE, options->output_bytes);
  apply_limit(error_fd, "RLIMIT_CPU", RLIMIT_CPU, options->cpu_seconds);
  apply_limit(error_fd, "RLIMIT_NPROC", RLIMIT_NPROC, options->nproc);
  apply_limit(error_fd, "RLIMIT_CORE", RLIMIT_CORE, 0);
  if (options->drop_privileges) {
    if (setgroups(0, NULL) < 0) setup_failed(error_fd, "setgroups");
    if (setgid(options->gid) < 0) setup_failed(error_fd, "setgid");
    if (setuid(options->uid) < 0) setup_failed(error_fd, "setuid");
  }

  /* 4. helper 意外死亡时杀掉直接子进程。setuid 会清除这个设置，必须放在其后。
   * 正常取消则由 helper 杀整个组。主动 setsid 逃离进程组需要 cgroup 另行管控。 */
  if (prctl(PR_SET_PDEATHSIG, SIGKILL) < 0) setup_failed(error_fd, "PR_SET_PDEATHSIG");
  if (getppid() != helper_pid) _exit(126);
  /* execvp 自行查 PATH；相对路径自然相对于上面 chdir 后的目录。 */
  execvp(options->command[0], options->command);
  setup_failed(error_fd, "exec");
}

static void kill_submission(pid_t child) {
  kill(-child, SIGKILL);
  kill(child, SIGKILL); /* 同时覆盖子进程尚未建好进程组的情况。 */
}

static struct Result monitor_child(pid_t child, long long start, long long wall_ms) {
  /* wait4 同时返回“如何退出”和“消耗多少资源”。先非阻塞轮询，才能在
   * 子进程还活着时检查 wall 截止时间；发出 SIGKILL 后再阻塞回收，避免僵尸。
   * 此循环从 fork 后就开始，因此子进程卡在 exec 前的准备阶段也受监控。 */
  struct Result result = {0};
  int wait_flags = WNOHANG;
  for (;;) {
    pid_t waited = wait4(child, &result.status, wait_flags, &result.usage);
    if (waited == child) break;
    if (waited < 0) {
      if (errno == EINTR) continue;
      int saved_errno = errno;
      kill_submission(child);
      errno = saved_errno;
      fail("wait4");
    }
    if (interrupted || (wall_ms && monotonic_ms() - start > wall_ms)) {
      result.timed_out = !interrupted;
      kill_submission(child);
      wait_flags = 0; /* 杀掉之后仍走同一个 wait4，阻塞等待回收。 */
      continue;
    }
    /* 轮询只影响 wall 超时的响应速度；内存峰值由 cgroup 记录，不做采样。 */
    struct timespec pause = {0, 1000000};
    nanosleep(&pause, NULL);
  }
  result.real_time_ms = monotonic_ms() - start;
  kill(-child, SIGKILL); /* 正常退出也清理同组后台进程。 */
  return result;
}

static void check_setup(int error_fd) {
  /* exit(126/127) 也可能来自用户程序，不能仅凭退出码认定启动失败。
   * 子进程 setup 失败会写入操作名与 errno；exec 成功会由 CLOEXEC 关闭写端。
   * 因而管道里的错误记录才是 setup 失败的证据，EOF 本身不是错误。 */
  struct SetupError error;
  ssize_t count;
  do {
    count = read(error_fd, &error, sizeof(error));
  } while (count < 0 && errno == EINTR);
  close(error_fd);
  if (count == 0) return; /* exec 成功后 CLOEXEC 自动关闭写端。 */
  if (count == (ssize_t)sizeof(error)) {
    fprintf(stderr, "%s: %s\n", error.operation, strerror(error.number));
  } else {
    fprintf(stderr, "invalid setup error record\n");
  }
  exit(125);
}

static void print_result(const struct Result *result) {
  long long cpu_us = ((long long)result->usage.ru_utime.tv_sec + result->usage.ru_stime.tv_sec)
                    * 1000000 + result->usage.ru_utime.tv_usec + result->usage.ru_stime.tv_usec;
  printf("{\"timed_out\":%s,\"cpu_time_us\":%lld,\"cpu_time_ms\":%lld,\"real_time_ms\":%lld,"
         "\"rss_kb\":%ld,\"signal\":%d,\"exit_code\":%d}\n",
         result->timed_out ? "true" : "false", cpu_us, (cpu_us + 500) / 1000, result->real_time_ms,
         result->usage.ru_maxrss, WIFSIGNALED(result->status) ? WTERMSIG(result->status) : 0,
         WIFEXITED(result->status) ? WEXITSTATUS(result->status) : 0);
}

int main(int argc, char **argv) {
  struct Options options = parse_options(argc, argv);
  struct sigaction action = {.sa_handler = on_signal};
  sigemptyset(&action.sa_mask);
  if (sigaction(SIGTERM, &action, NULL) < 0 ||
      sigaction(SIGINT, &action, NULL) < 0 ||
      sigaction(SIGHUP, &action, NULL) < 0) fail("sigaction");

  int error_pipe[2];
  if (pipe2(error_pipe, O_CLOEXEC) < 0) fail("pipe2");
  pid_t helper_pid = getpid();
  long long start = monotonic_ms();
  pid_t child = fork();
  if (child < 0) fail("fork");
  if (child == 0) {
    close(error_pipe[0]);
    run_child(&options, error_pipe[1], helper_pid);
  }

  close(error_pipe[1]);
  /* 父子双方设置进程组，避免父进程先被调度时子进程尚未分组。 */
  if (setpgid(child, child) < 0 && errno != EACCES && errno != ESRCH) {
    int saved_errno = errno;
    kill_submission(child);
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) {}
    errno = saved_errno;
    fail("setpgid");
  }
  struct Result result = monitor_child(child, start, options.wall_ms);
  check_setup(error_pipe[0]);
  if (interrupted) return 128 + interrupted;
  print_result(&result);
  return 0;
}
