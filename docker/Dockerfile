# Linux 执行环境：roj-local-judge-lite 依赖 Linux（sys/prctl.h、cgroup v2），
# macOS 上无法直接构建 C helper，这里用容器补齐。
#
# 构建：docker build -t roj-local-judge-lite -f docker/Dockerfile .
# 通常不用手动构建，docker/judge.sh 会在镜像缺失时自动构建。
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make python3 \
 && rm -rf /var/lib/apt/lists/*

COPY docker/cgroup-init.sh /usr/local/bin/cgroup-init.sh
RUN chmod +x /usr/local/bin/cgroup-init.sh

WORKDIR /work
