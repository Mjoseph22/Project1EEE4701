import os, random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge, Timer
from models.fifo_model import FifoModel

DEPTH, WIDTH = 8, 8


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
        await FallingEdge(self.dut.clk)              # drive on falling edge
        self.dut.wr_en.value, self.dut.rd_en.value = wr_en, rd_en
        self.dut.wr_data.value = wr_data
        await RisingEdge(self.dut.clk)
        await Timer(1, units="ns")                   # sample after the edge


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

    async def cycle(self, wr, rd, data=0):
        await self.drv.cycle(wr, rd, data)
        o = self.mon.sample()
        self.sb.check((wr, rd, data), o)
        return o

    async def drain(self):
        while not self.mon.sample()["empty"]:
            await self.cycle(0, 1)

    async def fill(self, base):
        i = 0
        while not self.mon.sample()["full"]:
            await self.cycle(1, 0, (base + i) & 0xFF); i += 1


async def setup(dut):
    env = Env(dut)
    await env.drv.reset()
    return env


@cocotb.test()
async def test_full_empty(dut):
    env = await setup(dut)
    for _ in range(2): await env.cycle(0, 1)                 # read-while-empty
    await env.fill(0x10)
    assert env.mon.sample()["full"] == 1
    for v in (0xEE, 0xDD): await env.cycle(1, 0, v)          # write-while-full (dropped)
    await env.drain()                                        # proves nothing extra got in
    assert env.mon.sample()["empty"] == 1
    for _ in range(2): await env.cycle(0, 1)                 # over-read ignored
    for v in (0x5A, 0x6B): await env.cycle(1, 0, v)          # stray rd_ptr bump shows here
    await env.drain()


@cocotb.test()
async def test_simultaneous_rw(dut):
    env = await setup(dut)
    for i in range(4): await env.cycle(1, 0, 0x20 + i)
    for i in range(12):
        o = await env.cycle(1, 1, 0x40 + i)                  # r+w mid-level
        assert o["count"] == 4
    await env.drain()                                        # drain exposes corrupted slots
    await env.cycle(1, 1, 0x77); await env.drain()           # r+w on empty
    await env.fill(0x90)
    for i in range(3): await env.cycle(1, 1, 0xB0 + i)       # r+w on full
    await env.drain()


@cocotb.test()
async def test_wrap(dut):
    env = await setup(dut)
    for off in range(DEPTH):                                 # laps from every pointer offset
        for i in range(off): await env.cycle(1, 0, 0x11 * (i + 1))
        for i in range(2 * DEPTH + 1):
            await env.cycle(1, 0, (0x80 + 7 * i + off) & 0xFF)
            await env.cycle(0, 1)
        await env.fill(0x50 + off)
        await env.drain()


@cocotb.test()
async def test_random(dut):
    seed = int(os.environ.get("SEED", 1234))
    rng = random.Random(seed)
    dut._log.info(f"seed={seed}")
    env = await setup(dut)
    p_wr = p_rd = 0.5
    for i in range(1000):
        if i % 40 == 0:                                      # fill-heavy / drain-heavy phases
            p_wr, p_rd = rng.choice([(0.85, 0.25), (0.25, 0.85), (0.6, 0.6)])
        await env.cycle(int(rng.random() < p_wr), int(rng.random() < p_rd), rng.getrandbits(8))
    await env.drain()