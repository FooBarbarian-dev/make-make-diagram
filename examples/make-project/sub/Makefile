# Sub-component library build

CC     ?= gcc
CFLAGS ?= -Wall

## Build the support library
libsupport.a: support.o
	ar rcs $@ $^

support.o: support.c
	$(CC) $(CFLAGS) -c $< -o $@

## Clean sub-component artifacts
clean:
	rm -f *.o *.a

.PHONY: clean
