## Build everything
all: main.o utils.o
	gcc -o app main.o utils.o

main.o: main.c
	gcc -c main.c

utils.o: utils.c
	gcc -c utils.c

## Remove build artifacts
clean:
	rm -f *.o app

.PHONY: all clean
