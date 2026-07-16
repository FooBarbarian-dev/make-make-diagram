CC := gcc
CFLAGS := -Wall

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

%.d: %.c
	$(CC) -MM $< > $@

all: main.o lib.o
	$(CC) -o app $^

.PHONY: all
