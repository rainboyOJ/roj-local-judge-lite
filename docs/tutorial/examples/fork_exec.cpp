#include <cerrno>
#include <cstdio>
#include <sys/wait.h>
#include <unistd.h>

int main() {
    int value = 7;
    pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        value = 99; // 子进程修改自己的变量，不会改掉父进程中的 value。
        std::printf("before exec: pid=%ld value=%d\n", static_cast<long>(getpid()), value);
        std::fflush(stdout); // exec 不替旧程序冲刷用户态的 stdio 缓冲区。
        execl("/bin/sh", "sh", "-c", "printf 'after exec: pid=%s\n' \"$$\"; exit 7",
              static_cast<char *>(nullptr));
        std::perror("exec"); // exec 成功不会返回；能走到这里说明启动失败。
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) continue;
        std::perror("waitpid");
        return 1;
    }
    std::printf("parent value=%d\n", value);
    if (WIFEXITED(status)) std::printf("exit=%d\n", WEXITSTATUS(status));
}
