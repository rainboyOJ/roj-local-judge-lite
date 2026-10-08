CC ?= cc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra -Werror

.PHONY: all check clean

all: runner_helper

runner_helper: runner_helper.c
	$(CC) $(CFLAGS) $< -o $@

check: runner_helper
	python3 -m unittest -v test_runner.py

clean:
	rm -f runner_helper
