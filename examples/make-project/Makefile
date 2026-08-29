# Example multi-directory project for pipeview
#
# Run:  pipeview examples/make-project -o /tmp/pipeview-demo
#
# This Makefile demonstrates docstrings, variable overrides,
# pattern rules, order-only prerequisites, and sub-make recursion.

include config.mk

SRCS := main.c util.c
OBJS := $(SRCS:.c=.o)

# Exported variables reach recipe environments (and sub-makes). The
# directive composes with any assignment flavor — the report shows these
# with an "exported" chip. (This is the construct from the field report
# that motivated the parser audit; see docs/agents/parser-audit.md.)
export BUILD_PROFILE ?= release
export VERBOSE_LOGS = false

## Build the application
build: $(OBJS) | builddir
	$(CC) $(LDFLAGS) -o builddir/$(TARGET) $(OBJS)

## Run the test suite
test: build
	@echo "Running tests with CC=$(CC) CFLAGS=$(CFLAGS)"
	./run_tests.sh

## Remove all build artifacts
clean:
	rm -rf builddir/ *.o
	$(MAKE) -C sub clean

## Deploy the built binary to the staging server
deploy: build test
	@echo "Deploying $(TARGET) to $(DEPLOY_HOST)..."
	scp builddir/$(TARGET) $(DEPLOY_HOST):/opt/bin/

# Pattern rule: compile .c to .o
%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

# Order-only prerequisite: builddir must exist but its timestamp is irrelevant
builddir:
	mkdir -p builddir

## Build the sub-component library
sub:
	$(MAKE) -C sub

.PHONY: build test clean deploy sub
