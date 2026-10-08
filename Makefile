CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror
IMAGE ?= roj-local-judge-lite

.PHONY: all check clean docker-image docker-check

all: runner_helper

runner_helper: runner_helper.c
	$(CC) $(CFLAGS) $< -o $@

check: runner_helper
	python3 -m unittest -v test_runner.py test_install.py

# macOS 上无法直接构建 runner_helper（sys/prctl.h 是 Linux 专有），
# 这两条目标把构建与测试放进 Linux 容器。详见 README「在 macOS 上运行」。
docker-image:
	@docker build -t $(IMAGE) -f docker/Dockerfile .

docker-check: docker-image
	@docker run --rm --privileged -e ROJ_JUDGE_CGROUP_ROOT=/sys/fs/cgroup/judge \
	  -v "$(CURDIR):/work:ro" $(IMAGE) \
	  bash -lc 'cp -r /work /judge && cd /judge && make -s && cgroup-init.sh && python3 -m unittest test_runner.py test_install.py'

clean:
	rm -f runner_helper
