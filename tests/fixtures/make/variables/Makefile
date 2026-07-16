CC := gcc
CFLAGS = -Wall -Werror
LDFLAGS ?= -lm
SRCS += main.c
RESULT != echo hello

TARGET = app

$(TARGET): main.o
	$(CC) $(LDFLAGS) -o $@ $^

main.o: main.c
	$(CC) $(CFLAGS) -c $<

define MULTI_LINE
line1
line2
endef

foo: BAR := baz
foo:
	echo $(BAR) $(MULTI_LINE)
