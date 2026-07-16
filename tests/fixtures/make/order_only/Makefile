all: main.o | builddir
	gcc -o builddir/app main.o

main.o: main.c | builddir
	gcc -c main.c -o main.o

builddir:
	mkdir -p builddir

.PHONY: all
