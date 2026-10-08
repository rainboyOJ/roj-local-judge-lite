#include <cstdio>
#include <unistd.h>

int main() {
    char directory[4096];
    if (getcwd(directory, sizeof(directory)) == nullptr) {
        std::perror("getcwd");
        return 1;
    }
    std::printf("pid=%ld parent=%ld\ncwd=%s\n", static_cast<long>(getpid()),
                static_cast<long>(getppid()), directory);
}
