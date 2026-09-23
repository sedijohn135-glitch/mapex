from mapex.core import primitives as p
from mapex.core.primitives import Bar
from tests.helpers import mk

# A bullish displacement: quiet bars, a down-close run, then a big bull candle leaving an FVG.
BULL_DISP = [
    (100, 101, 99, 100.5), (100.5, 101, 99.5, 100), (100, 100.8, 99.2, 100.4), (100.4, 101.2, 99.6, 99.8),
    (99.8, 100.2, 98.6, 98.9),  # down-close run (starts at bar 3, CISD open 100.4)
    (98.9, 99.1, 98.0, 98.2),  # down-close; sweeps lows
    (98.2, 104.2, 98.1, 104.0),  # displacement (body 5.8, range 6.1)
    (104.0, 104.8, 103.2, 104.5),  # leaves FVG: low 103.2 > high[5] 99.1
    (104.5, 105.0, 104.0, 104.8),
]


def test_closed_bars_no_lookahead():
    bars = mk([(1, 2, 0, 1)] * 5, start=0)
    assert len(p.closed_bars(bars, "M1", now=4 * 60 + 61)) == 4  # bar 4 closes at 300, +2 s grace
    assert len(p.closed_bars(bars, "M1", now=302)) == 5


def test_atr_and_series():
    bars = mk([(10, 12, 9, 11), (11, 14, 10, 13), (13, 13, 8, 9)])
    # TR: 3 (first bar range excluded when history exists), max(4, 3, 1)=4, max(5, 0, 5)=5
    assert p.atr(bars, 14) == (4 + 5) / 2
    assert p.atr_series(bars)[-1] == (4 + 5) / 2


def test_swings_and_major():
    highs = [1, 3, 2, 5, 4, 9, 4, 6, 3, 7, 2, 8, 1]
    bars = mk([(h - 0.5, h, h - 1, h - 0.5) for h in highs])
    sh = p.swing_highs(bars)
    assert sh == [1, 3, 5, 7, 9, 11]
    assert p.major_swings(bars, sh, "high") == [5]


def test_structure_break_requires_body_close():
    rows = [(10, 11, 9, 10), (10, 12, 9.5, 11), (11, 11.5, 10, 10.5), (10.5, 12.5, 10.2, 11.8), (11.8, 12.2, 11, 12.1)]
    bars = mk(rows)
    brk = p.structure_breaks(bars)
    # swing high 12 @1 confirmed at bar 2; bar 3 wicks to 12.5 but closes 11.8 (no BOS); bar 4 closes 12.1 -> BOS
    assert [(b.idx, b.direction, b.level) for b in brk] == [(4, "buy", 12)]


def test_fvg_strict_and_displacement():
    bars = mk(BULL_DISP)
    f = [z for z in p.fvgs(bars) if z.direction == "buy"]
    assert any(z.idx == 6 and z.low == 99.1 and z.high == 103.2 for z in f)
    atrs = p.atr_series(bars)
    assert p.is_displacement(bars, 6, atrs[6])
    assert not p.is_displacement(bars, 5, atrs[5])
    q, pts = p.displacement_quality(bars, 6, atrs[6])
    assert (q, pts) in {("LARGE", 13), ("MODERATE", 9)}
    assert p.displacement_quality(bars, 6, 100.0) == ("SMALL", 4)


def test_displacement_rejects_wicky_candle():
    rows = [(10, 10.2, 9.8, 10), (10, 13, 7, 11), (11, 12, 10.5, 11.5)]  # body 1, range 6
    bars = mk(rows)
    assert not p.is_displacement(bars, 1, 0.5)


def test_cisd_anchor_is_open_of_first_opposite_candle():
    bars = mk(BULL_DISP)
    assert p.cisd_anchor(bars, 6, "buy") == (100.4, 3)


def test_order_block_needs_bos_and_fvg():
    bars = mk(BULL_DISP)
    atrs = p.atr_series(bars)
    obs = p.order_blocks(bars, atrs, p.structure_breaks(bars))
    assert len(obs) == 1
    ob = obs[0]
    assert ob.kind == "OB" and ob.direction == "buy" and ob.anchor == 100.4 and ob.disp_idx == 6
    assert (ob.low, ob.high) == (98.0, 101.2)


def test_mitigation_and_ifvg():
    rows = BULL_DISP + [(104.8, 105, 103.0, 104.2), (104.2, 106, 104, 105.8)]  # taps FVG (low 103 <= 103.2)
    bars = mk(rows)
    f = [z for z in p.fvgs(bars) if z.idx == 6]
    z = p.mark_mitigation(f[0], bars)
    assert z.mitigated_at == bars[9].t
    iv = p.ifvgs(bars, f)
    assert len(iv) == 1 and iv[0].kind == "IFVG"
    # a body close below CE after the tap kills the inversion
    bars2 = mk(rows + [(105.8, 106, 100, 100.5)])
    assert p.ifvgs(bars2, [z for z in p.fvgs(bars2) if z.idx == 6]) == []


def test_vi_vacuum_void():
    vi = mk([(10, 11, 9.5, 10.8), (11.0, 12, 10.9, 11.8)])  # bodies don't touch, wicks overlap
    assert p.volume_imbalances(vi)[0].kind == "VI"
    gap = mk([(10, 11, 9.5, 10.8), (11.5, 12, 11.2, 11.8)])
    assert p.vacuums(gap)[0].low == 11 and p.vacuums(gap)[0].high == 11.2
    bars = mk(BULL_DISP)
    atrs = p.atr_series(bars)
    v = p.voids(bars, p.fvgs(bars), atrs)
    assert any(z.idx == 6 for z in v)


def test_rejection_block_and_breaker():
    rows = [(10, 10.5, 9.5, 10), (10, 10.4, 9.6, 10.1), (10.1, 10.3, 9.0, 9.4),  # down-close RB candle (h 10.3)
            (9.4, 13.0, 9.35, 12.9), (12.9, 13.5, 11.0, 13.2), (13.2, 13.4, 13.0, 13.3)]
    bars = mk(rows)
    atrs = p.atr_series(bars)
    rb = p.rejection_blocks(bars, atrs)
    assert rb and rb[0].kind == "RB" and rb[0].direction == "buy" and rb[0].anchor == 10.1
    # breaker: a bullish OB later closed through by a bearish displacement
    ob = p.Zone("OB", "buy", 100, 102, 101, 0, 0, 0)
    rows2 = [(103, 104, 102.5, 103.5)] * 3 + [(103.5, 103.6, 97.0, 97.2), (97.2, 97.4, 95, 95.5), (95.5, 96, 94, 94.5)]
    bars2 = [Bar(i * 60, *r) for i, r in enumerate(rows2)]
    atrs2 = [1.0] * len(bars2)
    bb = p.breaker_blocks(bars2, atrs2, [ob])
    assert bb and bb[0].kind == "BB" and bb[0].direction == "sell" and (bb[0].low, bb[0].high) == (100, 102)


def test_bpr_overlap():
    a = p.Zone("FVG", "buy", 10, 12, 11, 0, 1, 1)
    b = p.Zone("FVG", "sell", 11, 13, 12, 60, 5, 5)
    bars = mk([(11, 11.5, 10.5, 11.2)] * 8, start=0)
    out = p.bprs(bars, [a, b])
    assert out and (out[0].low, out[0].high) == (11, 12)
    assert p.inside_bpr(p.Zone("FVG", "buy", 11.2, 11.8, 11.5, 0, 0), out)
