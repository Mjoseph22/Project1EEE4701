# Project 1 -- cocotb flow for the layered FIFO verification environment.
#   make                              # whole suite, Icarus (default)
#   make SIM=verilator
#   make SEED=7                       # pick the CRV seed (default 1)
#   make FIFO_DEPTH=5 FIFO_WIDTH=16 SIM_BUILD=sim_build_d5   # other parameters
#   make DUT=/path/to/fifo.sv         # override the DUT (used by the mutation harness)
#   make CRAFT=/path/to/craft-vv-student   # if the packet is not next to Project1/
TOPLEVEL_LANG ?= verilog
SIM           ?= icarus

ROOT  := $(abspath $(CURDIR)/..)
CRAFT ?= $(firstword $(wildcard $(ROOT)/../craft-vv-student) $(wildcard $(ROOT)/craft-vv-student))
ifeq ($(strip $(CRAFT)),)
$(error cannot find craft-vv-student: put Project1/ next to it, or pass CRAFT=/path/to/craft-vv-student)
endif

DUT ?= $(CRAFT)/code/dut/fifo.sv
VERILOG_SOURCES = $(DUT)
TOPLEVEL = fifo
MODULE   = test_fifo

# The testbench reads the same two numbers, so DUT and model always agree.
FIFO_WIDTH ?= 8
FIFO_DEPTH ?= 8
export FIFO_WIDTH FIFO_DEPTH

ifeq ($(SIM),icarus)
COMPILE_ARGS += -Pfifo.WIDTH=$(FIFO_WIDTH) -Pfifo.DEPTH=$(FIFO_DEPTH)
endif
# fifo.sv compares a narrow count against the 32-bit int DEPTH; Verilator makes
# that width warning fatal by default. Waive it (compile-only), as the packet does.
ifeq ($(SIM),verilator)
COMPILE_ARGS += -Wno-WIDTHEXPAND -GWIDTH=$(FIFO_WIDTH) -GDEPTH=$(FIFO_DEPTH)
endif

export PYTHONPATH := $(CURDIR):$(ROOT):$(CRAFT)/code/lib:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
