## Build all components
all: local_target
	$(MAKE) -C sub
	$(MAKE) -f other.mk

local_target:
	echo building locally

## Clean all
clean:
	$(MAKE) -C sub clean

.PHONY: all clean local_target
