# Composition of gnarly-but-legal constructs. make -pqn must accept this
# file, and pipeview must emit zero "unparseable"-class diagnostics on it.
export PROMPT_CONFIG_UPDATES ?= true
export HTTPS_MODE = false
URL := https://example.com/path?x=1
LDPATH := /usr/lib:/usr/local/lib:/lib
EMPTY =
EMPTY2 = # trailing comment makes an empty value
HASH_VALUE = literal\#hash
SRCS := main.c util.c helper.txt
HAS_C := $(if $(filter %.c,$(SRCS)),yes,no)
NESTED := $(dir ${URL})

define run-tests
echo "running tests"
./test.sh --all
endef

quick: deps ; @echo built via semicolon recipe

deps:
	@echo "$$HOME and $$(pwd) are shell, not make"
	-mkdir -p build
	+echo runs under -n

test:
	$(run-tests)

empty-recipe: ;

.PHONY: quick deps test
