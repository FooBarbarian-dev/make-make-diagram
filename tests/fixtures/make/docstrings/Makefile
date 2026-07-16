## Build the project
build: main.o
	gcc -o app main.o

main.o: main.c
	gcc -c main.c

## Run unit tests
test:
	./run_tests.sh

deploy: ## Deploy to production
	./deploy.sh

## Clean build artifacts
clean:
	rm -f *.o app

.PHONY: build test deploy clean
