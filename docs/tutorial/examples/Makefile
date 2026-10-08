CXX ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Werror
BUILD_DIR ?= .build
NAMES = sum process fork_exec redirect error_pipe usage group allocate
TARGETS = $(addprefix $(BUILD_DIR)/,$(NAMES))

.PHONY: all check
all: $(TARGETS)

$(BUILD_DIR)/%: %.cpp
	mkdir -p $(BUILD_DIR)
	$(CXX) $(CXXFLAGS) $< -o $@

check: all
	python3 verify.py
