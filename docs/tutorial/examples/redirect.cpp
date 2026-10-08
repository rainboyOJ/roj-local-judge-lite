#include <cerrno>
#include <cstdio>
#include <fcntl.h>
#include <sys/wait.h>
#include <unistd.h>

void redirect(const char *path, int target, int flags) {
    int fd = open(path, flags, 0600);
    if (fd < 0 || dup2(fd, target) < 0) {
        std::perror("redirect");
        _exit(126);
    }
    if (fd != target) close(fd);
}

int main(int argc, char **argv) {
    if (argc != 4) {
        std::fprintf(stderr, "usage: redirect EXECUTABLE INPUT OUTPUT\n");
        return 2;
    }
    pid_t child = fork();
    if (child < 0) {
        std::perror("fork");
        return 1;
    }
    if (child == 0) {
        redirect(argv[2], STDIN_FILENO, O_RDONLY);
        redirect(argv[3], STDOUT_FILENO, O_WRONLY | O_CREAT | O_TRUNC);
        char *command[] = {argv[1], nullptr};
        execv(command[0], command);
        std::perror("exec");
        _exit(127);
    }
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno == EINTR) continue;
        std::perror("waitpid");
        return 1;
    }
    std::puts("parent stdout still points to the terminal");
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
