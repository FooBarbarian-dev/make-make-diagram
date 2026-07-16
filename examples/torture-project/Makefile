# Torture fixture for the report UI's overflow policy.
#
# Run:  pipeview examples/torture-project -o examples/out
#
# Every value here is deliberately hostile to layout: a 200-char unbroken
# variable value, a 150-char include path, a 300-char one-line recipe, and
# a very long target name. The acceptance bar: nothing escapes its
# container at 1280px and 1920px viewport widths.

# 150-char include path that does not exist (also exercises the
# unresolved-file state and the diagnostics list).
include vendor/third_party/extremely/deeply/nested/directory/structure/that/keeps/going/far/beyond/any/reasonable/depth/for/testing/label/overflow/torture2.mk

# 200-char unbroken variable value.
TORTURE_FLAGS := -I/opt/toolchains/embedded/arm-none-eabi/include:-L/opt/toolchains/embedded/arm-none-eabi/lib:--sysroot=/opt/sysroots/cortex-m4-hardfp/usr:-DCONFIG_TORTURE_FIXTURE_MODE=aggressive:-fstack-protector-al

## Build inside the toolchain container (300-char one-line recipe)
torture-build:
	docker run --rm --volume "$(PWD):/workspace" --workdir /workspace --env TORTURE_FLAGS="$(TORTURE_FLAGS)" --env BUILD_ID=torture-00000001 registry.example.internal:5000/build-images/toolchain:v3.14.159 sh -c "set -eux; ./configure --prefix=/usr/local --enable-everything && make -j8 all && make check"

## Exercise node-label truncation in the graph view
generate-extremely-long-artifact-name-for-testing-node-label-truncation-behavior-in-the-dependency-graph-view: torture-build
	@echo "long name handled"

.PHONY: torture-build generate-extremely-long-artifact-name-for-testing-node-label-truncation-behavior-in-the-dependency-graph-view
