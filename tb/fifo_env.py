"""Layered FIFO environment: driver, monitor, reference-queue scoreboard.

Grown from our HW4 Driver / Monitor / Scoreboard. Each layer has one job:

  FifoDriver      turns one cycle of stimulus into pin activity (owns clock + reset)
  FifoMonitor     rebuilds what happened that cycle from the pins alone
  FifoScoreboard  predicts with the reference queue (FifoModel) and compares
  FifoEnv         wires the three together; optional coverage collector hangs off it

The monitor never asks the driver what it drove. If it did, a driver bug
would hide itself.
"""
import os
from collections import deque
from dataclasses import dataclass
from typing import Optional

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from models.fifo_model import FifoModel

WIDTH = int(os.environ.get("FIFO_WIDTH", 8))
DEPTH = int(os.environ.get("FIFO_DEPTH", 8))
MASK = (1 << WIDTH) - 1


@dataclass
class Stim:
    """One clock cycle of stimulus. rst=1 drives rst_n low for this edge."""
    wr_en: int = 0
    rd_en: int = 0
    wr_data: int = 0
    rst: int = 0

    def check_legal(self):
        assert self.wr_en in (0, 1) and self.rd_en in (0, 1) and self.rst in (0, 1), \
            f"illegal stimulus {self}"
        assert 0 <= self.wr_data <= MASK, f"wr_data 0x{self.wr_data:x} wider than {WIDTH} bits"


@dataclass
class Observed:
    """What one clock cycle looked like from the pins alone."""
    cycle: int
    rst: int
    wr_en: int
    rd_en: int
    wr_data: int
    count: Optional[int]
    full: Optional[int]
    empty: Optional[int]
    rd_data: Optional[int]      # None when X/Z


def _int(sig):
    v = sig.value
    return int(v) if v.is_resolvable else None


class FifoDriver:
    """Drives on the falling edge, so every input is stable at the rising edge."""

    def __init__(self, dut, period_ns=10):
        self.dut = dut
        cocotb.start_soon(Clock(dut.clk, period_ns, units="ns").start())

    async def reset(self, cycles=3):
        d = self.dut
        d.wr_en.value = 0
        d.rd_en.value = 0
        d.wr_data.value = 0
        d.rst_n.value = 0
        for _ in range(cycles):
            await RisingEdge(d.clk)
        d.rst_n.value = 1

    async def drive(self, stim):
        d = self.dut
        await FallingEdge(d.clk)
        d.rst_n.value = 0 if stim.rst else 1
        d.wr_en.value = stim.wr_en
        d.rd_en.value = stim.rd_en
        d.wr_data.value = stim.wr_data
        await RisingEdge(d.clk)
        await Timer(1, units="ns")              # let the registered outputs settle


class FifoMonitor:
    """Samples inputs and outputs 1 ns after the rising edge. The inputs are
    still the values the edge saw (the driver only moves them on falling edges)."""

    def __init__(self, dut):
        self.dut = dut
        self.cycle = 0

    def capture(self):
        d = self.dut
        self.cycle += 1
        return Observed(self.cycle,
                        int(not _int(d.rst_n)), _int(d.wr_en), _int(d.rd_en),
                        _int(d.wr_data), _int(d.count), _int(d.full),
                        _int(d.empty), _int(d.rd_data))


class FifoScoreboard:
    """Reference-queue scoreboard. Every cycle it checks count, full and empty
    against the model, checks the flag equations of spec rule 5 directly on
    the DUT's own outputs, and checks the FWFT head whenever the model is not
    empty. The first divergence is logged with the recent history."""

    def __init__(self, width=WIDTH, depth=DEPTH):
        self.model = FifoModel(width, depth)
        self.depth = depth
        self.checked = 0
        self.history = deque(maxlen=16)
        self.stats = dict(writes=0, reads=0, dropped_writes=0, ignored_reads=0, resets=0)

    def update(self, obs):
        m = self.model
        if obs.rst:
            m.reset()
            do_wr = do_rd = False
            self.stats["resets"] += 1
        else:
            do_wr, do_rd = m.step(obs.wr_en, obs.wr_data, obs.rd_en)
            self.stats["writes"] += do_wr
            self.stats["reads"] += do_rd
            self.stats["dropped_writes"] += bool(obs.wr_en) and not do_wr
            self.stats["ignored_reads"] += bool(obs.rd_en) and not do_rd
        self.history.append(obs)

        errs = []
        if obs.count != m.count:
            errs.append(f"count {obs.count} != model {m.count}")
        if obs.full != int(m.full):
            errs.append(f"full {obs.full} != model {int(m.full)}")
        if obs.empty != int(m.empty):
            errs.append(f"empty {obs.empty} != model {int(m.empty)}")
        if obs.count is not None:                       # spec rule 5, model-free
            if obs.full != int(obs.count == self.depth):
                errs.append(f"full={obs.full} but count={obs.count}")
            if obs.empty != int(obs.count == 0):
                errs.append(f"empty={obs.empty} but count={obs.count}")
        if not m.empty and obs.rd_data != m.head:       # FWFT head, valid only when !empty
            errs.append(f"rd_data {_hx(obs.rd_data)} != head {_hx(m.head)}")

        if errs:
            lines = [f"first divergence at cycle {obs.cycle}: " + "; ".join(errs),
                     "  last cycles (cycle rst wr rd wr_data | count full empty rd_data):"]
            for o in self.history:
                lines.append(f"    {o.cycle:>6} {o.rst} {o.wr_en} {o.rd_en} {_hx(o.wr_data)} | "
                             f"{o.count} {o.full} {o.empty} {_hx(o.rd_data)}")
            cocotb.log.error("\n".join(lines))
            raise AssertionError(f"cycle {obs.cycle}: " + "; ".join(errs))
        self.checked += 1
        return do_wr, do_rd


def _hx(v):
    return "--" if v is None else f"0x{v:02x}"


class FifoEnv:
    """The stack. `cycle()` is the only way stimulus reaches the DUT."""

    def __init__(self, dut, coverage=None):
        self.dut = dut
        self.drv = FifoDriver(dut)
        self.mon = FifoMonitor(dut)
        self.sb = FifoScoreboard()
        self.cov = coverage

    @property
    def model(self):
        return self.sb.model

    async def start(self):
        await self.drv.reset()
        self.sb.model.reset()

    async def cycle(self, wr=0, rd=0, data=0, rst=0):
        stim = Stim(int(wr), int(rd), int(data), int(rst))
        stim.check_legal()
        pre_count = self.model.count
        await self.drv.drive(stim)
        obs = self.mon.capture()
        do_wr, do_rd = self.sb.update(obs)
        if self.cov is not None:
            self.cov.sample(pre_count, obs, do_wr, do_rd)
        return obs

    # Loop bounds come from the model, never from the DUT's flags: a mutant
    # whose `empty` never rises must fail the scoreboard, not hang the run.
    async def fill(self, base=0):
        i = 0
        while not self.model.full:
            await self.cycle(1, 0, (base + i) & MASK)
            i += 1

    async def drain(self):
        while not self.model.empty:
            await self.cycle(0, 1)
