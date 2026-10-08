#include <cerrno>
#include <csignal>
#include <cstdio>
#include <sys/wait.h>
#include <unistd.h>

int main() {
    pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        if (setpgid(0, 0) < 0) _exit(126);
        alarm(3); // 即使实验的父进程异常退出，本实验子进程也只存活几秒。
        while (true) pause();
    }
    // 父进程也设置一次：不需要猜测子进程是否已经执行到 setpgid。
    if (setpgid(child, child) < 0) {
        std::perror("setpgid");
        kill(child, SIGKILL);
        while (waitpid(child, nullptr, 0) < 0 && errno == EINTR) {}
        return 1;
    }
    std::printf("parent group=%ld child group=%ld\n", static_cast<long>(getpgrp()),
                static_cast<long>(getpgid(child)));
    if (kill(-child, SIGKILL) < 0) {
        std::perror("kill group");
        kill(child, SIGKILL);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) continue;
        std::perror("waitpid");
        return 1;
    }
    if (WIFSIGNALED(status)) std::printf("signal=%d\n", WTERMSIG(status));
}
