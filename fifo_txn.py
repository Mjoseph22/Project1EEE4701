"""pyvsc transaction for the FIFO: one clock cycle of stimulus.

Random fields:  wr_en, rd_en, wr_data, and three 0..99 "rolls".
State fields:   mode, bias_data -- NOT random; the sequencer sets them before
                each randomize() (crv/sequences.py).

Why rolls instead of vsc.dist: the pinned pyvsc (0.9.5) applies a `dist`
unconditionally even inside a false `implies` guard (see the requirements.txt
note and setup/verify_env.sh). Hard constraints inside `implies` ARE gated, so
each probability is written as "roll < p  ->  bit == 1". wr_en and rd_en use
independent rolls, so P(simultaneous) = P(wr) * P(rd) inside a mode.

Why solve_order: without it the solver does not draw the rolls uniformly --
we measured FILL's intended 85/15 coming out as 70/28 and every data corner
at ~3.5% instead of 8-10%. Solving the rolls before the bits they control
makes the rolls uniform on 0..99, and the measured rates match the table
below to within a few points (REPORT.md section 3).

Legality: every transaction this class can produce is legal. The enables are
1-bit, wr_data is WIDTH bits, and the spec makes every enable combination
legal in every state (rule 2: a write while full is dropped; rule 3: a read
while empty is ignored). Reset is not a transaction field; the sequencer owns it.
"""
import vsc

FILL, DRAIN, HOLD_FULL, HOLD_EMPTY, MIXED, BURST_WR, BURST_RD, IDLE, HW5_FILL, HW5_DRAIN = range(10)
MODE_NAMES = ["FILL", "DRAIN", "HOLD_FULL", "HOLD_EMPTY", "MIXED",
              "BURST_WR", "BURST_RD", "IDLE", "HW5_FILL", "HW5_DRAIN"]

# Intended (P(wr_en=1), P(rd_en=1)) in percent, per mode.
MODE_PROBS = {
    FILL:       (85, 15),    # net +0.70/cycle: reaches full fast
    DRAIN:      (15, 85),    # net -0.70/cycle: reaches empty fast
    HOLD_FULL:  (65, 40),    # dwell at/near full: over-writes, r+w on full
    HOLD_EMPTY: (40, 65),    # dwell at/near empty: over-reads, r+w on empty
    MIXED:      (70, 70),    # ~49% simultaneous r+w, count random-walks
    BURST_WR:   (100, 0),    # back-to-back writes: long write runs, overflow
    BURST_RD:   (0, 100),    # back-to-back reads: long read runs, underflow
    IDLE:       (15, 15),    # sparse traffic: idle cycles and single-op runs
    HW5_FILL:   (90, 50),    # the HW5 shaping, kept as the "before" baseline
    HW5_DRAIN:  (50, 90),
}

# Data classes, percent of transactions (only when bias_data == 1).
DATA_PCT = [("zero", 10), ("ones", 10), ("alt01", 8), ("alt10", 8)]   # rest: unconstrained


def data_patterns(width):
    mask = (1 << width) - 1
    alt01 = sum(1 << i for i in range(0, width, 2))      # 0x55 for 8 bits
    return {"zero": 0, "ones": mask, "alt01": alt01, "alt10": mask ^ alt01}


@vsc.randobj
class FifoTxn:
    def __init__(self, width=8):
        self.wr_en = vsc.rand_bit_t(1)
        self.rd_en = vsc.rand_bit_t(1)
        self.wr_data = vsc.rand_bit_t(width)
        self.wr_roll = vsc.rand_uint8_t()
        self.rd_roll = vsc.rand_uint8_t()
        self.data_roll = vsc.rand_uint8_t()
        # NOT random: fed by the sequencer each cycle
        self.mode = vsc.uint8_t(FILL)
        self.bias_data = vsc.uint8_t(1)
        # NOT random: width-dependent data corners
        pats = data_patterns(width)
        self.pat_zero = vsc.uint32_t(pats["zero"])
        self.pat_ones = vsc.uint32_t(pats["ones"])
        self.pat_alt01 = vsc.uint32_t(pats["alt01"])
        self.pat_alt10 = vsc.uint32_t(pats["alt10"])

    @vsc.constraint
    def c_order(self):
        vsc.solve_order([self.wr_roll, self.rd_roll, self.data_roll],
                        [self.wr_en, self.rd_en, self.wr_data])

    @vsc.constraint
    def c_rolls(self):
        self.wr_roll < 100
        self.rd_roll < 100
        self.data_roll < 100

    @vsc.constraint
    def c_enables(self):
        for mode, (p_wr, p_rd) in MODE_PROBS.items():
            with vsc.implies(self.mode == mode):
                _bit_with_prob(self.wr_en, self.wr_roll, p_wr)
                _bit_with_prob(self.rd_en, self.rd_roll, p_rd)

    @vsc.constraint
    def c_data(self):
        pats = {"zero": self.pat_zero, "ones": self.pat_ones,
                "alt01": self.pat_alt01, "alt10": self.pat_alt10}
        lo = 0
        with vsc.implies(self.bias_data == 1):
            for name, pct in DATA_PCT:
                with vsc.implies(self.data_roll >= lo):
                    with vsc.implies(self.data_roll < lo + pct):
                        self.wr_data == pats[name]
                lo += pct


def _bit_with_prob(bit, roll, pct):
    """bit == 1 with probability pct/100, given roll uniform on 0..99."""
    if pct <= 0:
        bit == 0
    elif pct >= 100:
        bit == 1
    else:
        with vsc.implies(roll < pct):
            bit == 1
        with vsc.implies(roll >= pct):
            bit == 0
