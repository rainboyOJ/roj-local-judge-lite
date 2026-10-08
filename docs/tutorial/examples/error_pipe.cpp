#include <cerrno>
#include <cstdio>
#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: error_pipe COMMAND [ARGS...]\n");
        return 2;
    }
    int channel[2];
    if (pipe2(channel, O_CLOEXEC) < 0) {
        std::perror("pipe2");
        return 1;
    }
    pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        close(channel[0]);
        execvp(argv[1], argv + 1);
        int number = errno; // 先保存：后面的 write 也可能修改 errno。
        ssize_t written;
        do {
            written = write(channel[1], &number, sizeof(number));
        } while (written < 0 && errno == EINTR);
        _exit(127);
    }
    close(channel[1]); // 父进程不能留着写端，否则 read 可能永远等不到 EOF。
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) continue;
        std::perror("waitpid");
        return 1;
    }
    int number = 0;
    ssize_t count;
    do {
        count = read(channel[0], &number, sizeof(number));
    } while (count < 0 && errno == EINTR);
    close(channel[0]);
    if (count == sizeof(number))
        std::printf("setup errno=%d\n", number);
    else if (count == 0)
        std::puts("no setup error record");
    else {
        std::fprintf(stderr, "invalid error record\n");
        return 1;
    }
    if (WIFEXITED(status)) std::printf("exit=%d\n", WEXITSTATUS(status));
    // 本实验只展示两份证据；项目会进一步把它们转换成 SYSTEM_ERROR 或 RE。
}
