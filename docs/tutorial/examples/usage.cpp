#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2 || (std::strcmp(argv[1], "sleep") && std::strcmp(argv[1], "cpu"))) {
        std::fprintf(stderr, "usage: usage sleep|cpu\n");
        return 2;
    }
    auto start = std::chrono::steady_clock::now();
    pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        rlimit cpu{1, 2}; // CPU soft 1 秒、hard 2 秒；只改变这个子进程。
        rlimit core{0, 0};
        if (setrlimit(RLIMIT_CPU, &cpu) < 0 || setrlimit(RLIMIT_CORE, &core) < 0) {
            std::perror("setrlimit");
            _exit(126);
        }
        if (std::strcmp(argv[1], "sleep") == 0) {
            sleep(1);
            _exit(0);
        }
        volatile std::uint64_t counter = 0;
        while (true) {
            ++counter;
            (void)counter; // 显式读取；volatile 使读写不能被优化掉。
        }
    }
    int status;
    rusage usage{};
    while (wait4(child, &status, 0, &usage) < 0) {
        if (errno == EINTR) continue;
        std::perror("wait4");
        return 1;
    }
    double cpu_ms = (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) * 1000.0 + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1000.0;
    double wall_ms = std::chrono::duration<double, std::milli>(
                         std::chrono::steady_clock::now() - start)
                         .count();
    std::printf("cpu_ms=%.1f wall_ms=%.1f\n", cpu_ms, wall_ms);
    if (WIFEXITED(status)) std::printf("exit=%d\n", WEXITSTATUS(status));
    if (WIFSIGNALED(status)) std::printf("signal=%d\n", WTERMSIG(status));
}
