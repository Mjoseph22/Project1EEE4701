import os, random
from collections import deque
import cocotb
import vsc
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
from models.fifo_model import FifoModel

DEPTH, WIDTH = 8, 8
SEED = int(os.environ.get("SEED", 1234))
CYCLES = int(os.environ.get("CYCLES", 3000))
FILL, DRAIN = 0, 1


# ---------------- Transaction + constraints ----------------
@vsc.randobj
class FifoTxn:
    def __init__(self):
        self.wr_en = vsc.rand_bit_t(1)
        self.rd_en = vsc.rand_bit_t(1)
        self.wr_data = vsc.rand_bit_t(WIDTH)
        self.count = vsc.uint32_t()      # NOT random: current occupancy, fed each cycle
        self.mode = vsc.uint32_t()       # NOT random: sweep direction (FILL / DRAIN)

    @vsc.constraint
    def shape(self):
        # Every wr_en/rd_en combination is legal: the RTL drops a write while full
        # and ignores a read while empty, so no constraint has to forbid anything.
        with vsc.if_then(self.count == 0):            # empty: push writes, keep over-reads common
            vsc.dist(self.wr_en, [vsc.weight(1, 7), vsc.weight(0, 3)])
            vsc.dist(self.rd_en, [vsc.weight(1, 5), vsc.weight(0, 5)])
        with vsc.else_if(self.count == DEPTH):        # full: push reads, keep over-writes common
            vsc.dist(self.rd_en, [vsc.weight(1, 7), vsc.weight(0, 3)])
            vsc.dist(self.wr_en, [vsc.weight(1, 5), vsc.weight(0, 5)])
        with vsc.else_if(self.mode == FILL):          # mid-range: both enables often, drift to full
            vsc.dist(self.wr_en, [vsc.weight(1, 9), vsc.weight(0, 1)])
            vsc.dist(self.rd_en, [vsc.weight(1, 5), vsc.weight(0, 5)])
        with vsc.else_then:                           # mid-range: both enables often, drift to empty
            vsc.dist(self.wr_en, [vsc.weight(1, 5), vsc.weight(0, 5)])
            vsc.dist(self.rd_en, [vsc.weight(1, 9), vsc.weight(0, 1)])


# ---------------- Driver / Monitor / Scoreboard (from HW4) ----------------
class Driver:
    def __init__(self, dut): self.dut = dut

    async def reset(self):
        cocotb.start_soon(Clock(self.dut.clk, 10, units="ns").start())
        self.dut.wr_en.value = self.dut.rd_en.value = self.dut.wr_data.value = 0
        self.dut.rst_n.value = 0
        for _ in range(3):
            await RisingEdge(self.dut.clk)
        self.dut.rst_n.value = 1

    async def cycle(self, wr_en, rd_en, wr_data=0):
        await FallingEdge(self.dut.clk)
        self.dut.wr_en.value, self.dut.rd_en.value = wr_en, rd_en
        self.dut.wr_data.value = wr_data
        await RisingEdge(self.dut.clk)
        await Timer(1, units="ns")


class Monitor:
    def __init__(self, dut): self.dut = dut

    def sample(self):
        d = self.dut
        return dict(count=int(d.count.value), full=int(d.full.value),
                    empty=int(d.empty.value),
                    rd_data=int(d.rd_data.value) if d.rd_data.value.is_resolvable else None)


class Scoreboard:
    def __init__(self):
        self.m, self.n = FifoModel(width=WIDTH, depth=DEPTH), 0

    def check(self, txn, obs):
        wr_en, rd_en, wr_data = txn
        self.m.step(wr_en=wr_en, wr_data=wr_data, rd_en=rd_en)
        tag = f"cycle {self.n} (wr_en={wr_en} rd_en={rd_en} wr_data=0x{wr_data:02x})"
        assert obs["count"] == self.m.count, f"{tag}: count {obs['count']} != {self.m.count}"
        assert obs["full"] == int(self.m.full), f"{tag}: full {obs['full']} != {int(self.m.full)}"
        assert obs["empty"] == int(self.m.empty), f"{tag}: empty {obs['empty']} != {int(self.m.empty)}"
        if not self.m.empty:
            assert obs["rd_data"] == self.m.head, f"{tag}: rd_data {obs['rd_data']} != {self.m.head}"
        self.n += 1


class Env:
    def __init__(self, dut):
        self.drv, self.mon, self.sb = Driver(dut), Monitor(dut), Scoreboard()

    async def cycle(self, wr, rd, data):
        # legality: 1-bit enables, data in range (any combination is legal for this RTL)
        assert wr in (0, 1) and rd in (0, 1) and 0 <= data < (1 << WIDTH), "illegal stimulus"
        await self.drv.cycle(wr, rd, data)
        self.sb.check((wr, rd, data), self.mon.sample())


# ---------------- Stimulus self-checks ----------------
class StimCoverage:
    def __init__(self):
        self.hist = [0] * (DEPTH + 1)      # pre-edge occupancy histogram
        self.rw = self.rw_full = self.rw_empty = 0
        self.wr_full = self.rd_empty = 0
        self.fills = self.drains = 0
        self.prev = 0

    def sample(self, count, wr, rd):
        self.hist[count] += 1
        if wr and rd:              self.rw += 1
        if wr and rd and count == DEPTH: self.rw_full += 1
        if wr and rd and count == 0:     self.rw_empty += 1
        if wr and count == DEPTH:  self.wr_full += 1
        if rd and count == 0:      self.rd_empty += 1
        if count == DEPTH and self.prev != DEPTH: self.fills += 1
        if count == 0 and self.prev != 0:         self.drains += 1
        self.prev = count

    def check(self, log):
        log.info(f"occupancy histogram 0..{DEPTH}: {self.hist}")
        log.info(f"r+w={self.rw} (on full={self.rw_full}, on empty={self.rw_empty}) "
                 f"write-while-full={self.wr_full} read-while-empty={self.rd_empty} "
                 f"fills={self.fills} drains={self.drains}")
        assert all(h > 0 for h in self.hist), f"not every occupancy reached: {self.hist}"
        assert self.hist[DEPTH] > 0 and self.hist[0] > 0, "never reached full / empty"
        assert self.rw > 0, "never issued simultaneous r+w"
        assert self.wr_full > 0, "never wrote while full"
        assert self.rd_empty > 0, "never read while empty"
        assert self.fills >= 10 and self.drains >= 10, "did not fill and drain many times"


# ---------------- Seeded regression ----------------
@cocotb.test()
async def test_crv(dut):
    random.seed(SEED)                       # pyvsc draws from stdlib random
    dut._log.info(f"SEED={SEED} CYCLES={CYCLES}")
    env = Env(dut)
    await env.drv.reset()
    txn, cov, trace = FifoTxn(), StimCoverage(), deque(maxlen=16)
    mode = FILL
    try:
        for i in range(CYCLES):
            m = env.sb.m                    # model = pre-edge state
            if m.full:
                mode = DRAIN
            elif m.empty:
                mode = FILL
            elif random.random() < 0.03:    # occasional mid-range reversal
                mode ^= 1
            txn.count, txn.mode = m.count, mode
            txn.randomize()
            wr, rd, data = int(txn.wr_en), int(txn.rd_en), int(txn.wr_data)
            cov.sample(m.count, wr, rd)
            trace.append((i, m.count, wr, rd, data))
            await env.cycle(wr, rd, data)
    except AssertionError:
        dut._log.error(f"SEED={SEED} last txns (cycle, count, wr, rd, data): {list(trace)}")
        raise
    cov.check(dut._log)