# Project-wide configuration
# Variables defined here are overridden in the root Makefile where needed.

CC       ?= gcc
CFLAGS    = -Wall -Werror -O2
LDFLAGS  ?= -lm
TARGET    = myapp
DEPLOY_HOST ?= staging.example.com
