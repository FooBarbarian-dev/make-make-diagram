OBJS := a.o b.o

all: lib.a
	@echo done

$(OBJS): %.o: %.c
	cc -c $< -o $@

lib.a out.sym &: $(OBJS)
	ar rcs lib.a $(OBJS)
	touch out.sym

docs:: intro.md
	cat intro.md

docs:: api.md
	cat api.md

same: first
	echo one

same: second
	echo two
