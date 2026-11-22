import pytest

from cdx_agent.stats import clopper_pearson, quantiles


def test_quantiles():
    q = quantiles(range(1, 101))
    assert q["p50"] == 50.5
    assert q["p95"] == pytest.approx(95.05)
    assert q["max"] == 100 and q["n"] == 100
    assert quantiles([3])["p95"] == 3
    with pytest.raises(ValueError):
        quantiles([])


def test_clopper_pearson_known():
    assert round(clopper_pearson(20, 20)[0], 4) == 0.8316
    assert clopper_pearson(20, 20)[1] == 1.0
    assert clopper_pearson(0, 20)[0] == 0.0
    assert round(clopper_pearson(0, 20)[1], 4) == 0.1684
    lo, hi = clopper_pearson(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.1871, 0.8129)


def test_clopper_pearson_large_n():
    lo, hi = clopper_pearson(5000, 10000)
    assert 0.48 < lo < 0.5 < hi < 0.52
    lo, hi = clopper_pearson(10000, 10000)
    assert hi == 1.0 and lo > 0.999
