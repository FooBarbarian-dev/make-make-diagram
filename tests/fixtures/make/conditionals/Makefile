DEBUG ?= 0

ifeq ($(DEBUG),1)
CFLAGS := -g -O0
else
CFLAGS := -O2
endif

ifdef VERBOSE
VFLAG := -v
endif

ifndef CC
CC := gcc
endif

ifneq ($(OS),Windows)
PLATFORM := unix
endif

all:
	$(CC) $(CFLAGS) $(VFLAG) -o app main.c

.PHONY: all
