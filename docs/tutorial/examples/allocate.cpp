#include <cstdlib>
#include <iostream>
#include <vector>

int main(int argc, char **argv) {
    int mib = argc == 2 ? std::atoi(argv[1]) : 4;
    if (mib < 1 || mib > 128) return 2;
    {
        std::vector<unsigned char> data(static_cast<std::size_t>(mib) * 1024 * 1024);
        volatile unsigned char *pages = data.data();
        // 实际写入内存，避免只申请虚拟地址而没有触碰物理页。
        for (std::size_t i = 0; i < data.size(); i += 4096) pages[i] = 1;
        std::cout << "touched_mib=" << mib << '\n';
    } // 即使马上释放并退出，cgroup 峰值仍保留这次用量。
}
