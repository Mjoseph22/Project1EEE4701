"""Project 1 -- FIFO verification suite. Every test traces to vplan.md.

  test_reset_values            F1           reset state
  test_overflow_underflow      F2 F3 F5     fill/drain, dropped writes, ignored reads
  test_simultaneous_boundaries F4          r+w at empty, mid, full
  test_wrap_all_offsets        F6 F7        pointer wrap from every offset, FIFO order
  test_reset_mid_traffic       F1 F8        reset at partial / full / offset pointers
  test_crv                     F1-F9        seeded pyvsc CRV to coverage closure + soak

Run:   make            (Icarus)          make SEED=7          make FIFO_DEPTH=5
Env:   SEED (default 1)   CRV_PROFILE closure|baseline   SOAK (cycles past closure)
       MAX_CYCLES   CRV_REQUIRE_CLOSURE (1/0)   RESULTS_DIR (write reports there)
"""
import json
import os
import random
import sys
import time
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):            # tb/ and the repo root
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cocotb

from fifo_env import DEPTH, MASK, WIDTH, FifoEnv
from crv.fifo_txn import MODE_NAMES, MODE_PROBS, FifoTxn
from crv.sequences import ModeSequencer
from coverage_model.fifo_cov import FifoCoverage, burst_bin, data_bin, op_bin, zone

SEED = int(os.environ.get("SEED", 1))
PROFILE = os.environ.get("CRV_PROFILE", "closure")
SOAK = int(os.environ.get("SOAK", 1000))
MAX_CYCLES = int(os.environ.get("MAX_CYCLES", 600 * DEPTH))
REQUIRE_CLOSURE = os.environ.get("CRV_REQUIRE_CLOSURE", "1" if PROFILE == "closure" else "0") == "1"
RESULTS_DIR = os.environ.get("RESULTS_DIR")


async def setup(dut, coverage=None):
    assert len(dut.wr_data) == WIDTH, \
        f"DUT WIDTH={len(dut.wr_data)} but testbench FIFO_WIDTH={WIDTH}"
    env = FifoEnv(dut, coverage)
    await env.start()
    return env


# ------------------------------------------------------------------ directed

@cocotb.test()
async def test_reset_values(dut):
    """F1: after reset count=0, empty=1, full=0, and an idle cycle keeps it."""
    env = await setup(dut)
    obs = await env.cycle(0, 0)
    assert (obs.count, obs.empty, obs.full) == (0, 1, 0), f"post-reset state {obs}"
    obs = await env.cycle(1, 0, 0xA5 & MASK)
    assert (obs.count, obs.empty) == (1, 0) and obs.rd_data == 0xA5 & MASK, \
        f"first write not visible at the head (FWFT): {obs}"


@cocotb.test()
async def test_overflow_underflow(dut):
    """F2 F3 F5: reads while empty are ignored, writes while full are dropped."""
    env = await setup(dut)
    for _ in range(2):
        await env.cycle(0, 1)                               # read-while-empty
    await env.fill(0x10)
    for v in (0xEE, 0xDD, 0x00, MASK):
        await env.cycle(1, 0, v & MASK)                     # write-while-full: dropped
    await env.drain()                                       # nothing extra got in
    for _ in range(3):
        await env.cycle(0, 1)                               # over-read: ignored
    for v in (0x5A, 0x6B):
        await env.cycle(1, 0, v & MASK)                     # a stray rd_ptr bump shows here
    await env.drain()


@cocotb.test()
async def test_simultaneous_boundaries(dut):
    """F4: r+w at empty (only the write happens, rule 3), mid (both, count
    unchanged, rule 4), and full (only the read happens, rule 2)."""
    env = await setup(dut)
    obs = await env.cycle(1, 1, 0x77 & MASK)                # r+w on empty
    assert obs.count == 1, f"r+w on empty: count {obs.count}, expected 1"
    await env.drain()
    for i in range(DEPTH // 2):
        await env.cycle(1, 0, (0x20 + i) & MASK)
    for i in range(3 * DEPTH):
        obs = await env.cycle(1, 1, (0x40 + i) & MASK)      # r+w mid-level
        assert obs.count == DEPTH // 2, f"r+w mid-level changed count to {obs.count}"
    await env.drain()                                       # drain exposes corrupted slots
    await env.fill(0x90)
    obs = await env.cycle(1, 1, 0xAB & MASK)                # r+w on full: write dropped
    assert obs.count == DEPTH - 1 and obs.full == 0, f"r+w on full: {obs}"
    for i in range(DEPTH + 2):
        obs = await env.cycle(1, 1, (0xB0 + i) & MASK)      # r+w at DEPTH-1: both happen
        assert obs.count == DEPTH - 1, f"r+w at near-full: {obs}"
    await env.drain()


@cocotb.test()
async def test_wrap_all_offsets(dut):
    """F6 F7: pointers lap the memory from every starting offset; order holds."""
    env = await setup(dut)
    for off in range(DEPTH):
        for i in range(off):
            await env.cycle(1, 0, (0x11 * (i + 1)) & MASK)
        for i in range(2 * DEPTH + 1):
            await env.cycle(1, 0, (0x80 + 7 * i + off) & MASK)
            await env.cycle(0, 1)
        await env.fill(0x50 + off)
        await env.drain()


@cocotb.test()
async def test_reset_mid_traffic(dut):
    """F1 F8: a reset mid-stream empties the FIFO and leaves no stale state.
    Enables are held high during the reset edge: reset must win."""
    env = await setup(dut)
    for i in range(3):
        await env.cycle(1, 0, (0x30 + i) & MASK)
    await env.cycle(1, 1, 0xC3 & MASK, rst=1)               # reset at partial
    for i in range(2):
        await env.cycle(1, 0, (0x60 + i) & MASK)            # new data must be the head
    await env.drain()

    await env.fill(0x70)
    await env.cycle(1, 0, 0x3C & MASK, rst=1)               # reset at full
    await env.fill(0xA0)
    await env.drain()

    for i in range(DEPTH // 2 + 1):                         # move both pointers off 0
        await env.cycle(1, 0, i)
        await env.cycle(0, 1)
    for i in range(2):
        await env.cycle(1, 0, (0xE0 + i) & MASK)
    await env.cycle(0, 1, rst=1)                            # reset with offset pointers
    for i in range(DEPTH):
        await env.cycle(1, 0, (0x01 + i) & MASK)
    await env.drain()


# ------------------------------------------------------------------- CRV

class StimStats:
    """Measures the distribution the constraints actually induce."""

    def __init__(self):
        self.mode_cycles = Counter()
        self.mode_wr = Counter()
        self.mode_rd = Counter()
        self.ops = Counter()
        self.occ = Counter()
        self.data = Counter()
        self.reset_zone = Counter()
        self.runs = {"write": Counter(), "read": Counter()}
        self._run = (None, 0)

    def add(self, mode, pre_count, wr, rd, data, rst):
        if rst:
            self.reset_zone[zone(pre_count)] += 1
            self._close_run(None)
            return
        name = MODE_NAMES[mode]
        self.mode_cycles[name] += 1
        self.mode_wr[name] += wr
        self.mode_rd[name] += rd
        op = op_bin(wr, rd)
        self.ops[op] += 1
        self.occ[pre_count] += 1
        if wr:
            self.data[data_bin(data)] += 1
        self._close_run(op if op in ("write", "read") else None)

    def _close_run(self, kind):
        k, n = self._run
        if kind == k and kind:
            self._run = (k, n + 1)
            return
        if k and n:
            self.runs[k][burst_bin(n)] += 1
        self._run = (kind, 1 if kind else 0)

    def as_dict(self):
        modes = {}
        for name, n in self.mode_cycles.items():
            want = MODE_PROBS[MODE_NAMES.index(name)]
            modes[name] = {"cycles": n, "share_pct": 100.0 * n / self.total(),
                           "p_wr_meas": 100.0 * self.mode_wr[name] / n, "p_wr_want": want[0],
                           "p_rd_meas": 100.0 * self.mode_rd[name] / n, "p_rd_want": want[1]}
        return {"cycles": self.total(), "ops": dict(self.ops),
                "occupancy": [self.occ[i] for i in range(DEPTH + 1)],
                "data": dict(self.data), "resets": dict(self.reset_zone),
                "write_runs": dict(self.runs["write"]), "read_runs": dict(self.runs["read"]),
                "modes": modes}

    def total(self):
        return sum(self.mode_cycles.values()) or 1

    def report(self):
        t = self.total()
        out = ["", f"  induced distribution over {t} transaction cycles:",
               "  op mix: " + "  ".join(f"{o}={100.0 * self.ops[o] / t:.1f}%"
                                         for o in ("idle", "write", "read", "simul")),
               "  occupancy histogram 0..DEPTH: " + str([self.occ[i] for i in range(DEPTH + 1)]),
               "  data classes (writes): " + str(dict(self.data)),
               "  write runs: " + str(dict(self.runs["write"])) + "   read runs: " + str(dict(self.runs["read"])),
               "  resets by occupancy: " + str(dict(self.reset_zone)),
               "  mode         share   P(wr) meas/want   P(rd) meas/want"]
        for name, d in sorted(self.as_dict()["modes"].items(), key=lambda kv: -kv[1]["cycles"]):
            out.append(f"  {name:<11} {d['share_pct']:5.1f}%   {d['p_wr_meas']:5.1f} / {d['p_wr_want']:<3}"
                       f"        {d['p_rd_meas']:5.1f} / {d['p_rd_want']:<3}")
        return "\n".join(out)


@cocotb.test()
async def test_crv(dut):
    """F1-F9: seeded constrained-random run to coverage closure, then a soak."""
    random.seed(SEED)                                   # pyvsc draws from stdlib random
    t0 = time.time()
    cov = FifoCoverage()
    env = await setup(dut, coverage=cov)
    txn = FifoTxn(WIDTH)
    seq = ModeSequencer(random.Random(SEED * 7919 + 1), DEPTH, PROFILE)
    stats = StimStats()
    dut._log.info(f"CRV seed={SEED} profile={PROFILE} WIDTH={WIDTH} DEPTH={DEPTH} "
                  f"soak={SOAK} max={MAX_CYCLES}")

    closed_at = None
    cycle = 0
    try:
        while cycle < MAX_CYCLES:
            mode, rst = seq.next(env.model.count)
            txn.mode = mode
            txn.bias_data = seq.bias_data
            txn.randomize()
            wr, rd, data = int(txn.wr_en), int(txn.rd_en), int(txn.wr_data)
            stats.add(mode, env.model.count, wr, rd, data, rst)
            await env.cycle(wr, rd, data, rst)
            cycle += 1
            if cycle % 50 == 0:
                cov.checkpoint(cycle)
                if closed_at is None and cov.closed():
                    closed_at = cycle
            if closed_at is not None and cycle >= closed_at + SOAK:
                break
    except AssertionError:
        dut._log.error(f"CRV failure: SEED={SEED} profile={PROFILE} cycle={cycle} "
                       f"mode={MODE_NAMES[seq.mode]} -- rerun with SEED={SEED}")
        raise
    cov.checkpoint(cycle)

    summary = (f"\n  seed={SEED} profile={PROFILE} cycles={cycle} "
               f"closed_at={closed_at} scoreboard_checks={env.sb.checked} "
               f"stats={env.sb.stats} wall={time.time() - t0:.1f}s")
    dut._log.info(summary + cov.report() + "\n\n  convergence:\n" + cov.curve_rows()
                  + "\n  last bins to close: " + str(cov.last_to_close()) + stats.report())

    if RESULTS_DIR:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        tag = f"{PROFILE}_w{WIDTH}_d{DEPTH}_s{SEED}"
        with open(os.path.join(RESULTS_DIR, f"crv_{tag}.json"), "w") as f:
            json.dump({"seed": SEED, "profile": PROFILE, "width": WIDTH, "depth": DEPTH,
                       "cycles": cycle, "closed_at": closed_at, "sb": env.sb.stats,
                       "checks": env.sb.checked, "wall_s": time.time() - t0,
                       "coverage": cov.as_dict(), "stimulus": stats.as_dict()}, f, indent=1)
        with open(os.path.join(RESULTS_DIR, f"coverage_{tag}.txt"), "w") as f:
            f.write(summary + "\n" + cov.report() + "\n\n  convergence:\n" + cov.curve_rows()
                    + "\n  last bins to close: " + str(cov.last_to_close()) + "\n" + stats.report() + "\n")
        cov.export_yaml(os.path.join(RESULTS_DIR, f"coverage_{tag}.yml"))

    if REQUIRE_CLOSURE:
        assert closed_at is not None, \
            f"coverage did not close in {MAX_CYCLES} cycles; holes: {cov.holes()}"
