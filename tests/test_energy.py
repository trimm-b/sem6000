import pytest

from sem6000.energy import Integrator


def fixed_clock():
    """A clock driven by the test rather than the wall."""
    state = {"t": 0.0}

    def now():
        return state["t"]

    return state, now


def test_constant_load_integrates_exactly():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    # 3600 W held for one hour == 3600 Wh.
    it.add(3600.0)
    state["t"] = 3600.0
    it.add(3600.0)
    assert it.energy_wh == pytest.approx(3600.0)
    assert it.energy_kwh == pytest.approx(3.6)


def test_trapezoid_averages_across_a_ramp():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(0.0)
    state["t"] = 3600.0
    it.add(100.0)
    # Mean of 0 and 100 over an hour.
    assert it.energy_wh == pytest.approx(50.0)


def test_single_sample_yields_no_energy():
    it = Integrator()
    it.add(1000.0)
    assert it.energy_wh == 0.0
    assert it.samples == 1


def test_no_samples_is_all_zero():
    it = Integrator()
    assert it.energy_wh == 0.0
    assert it.mean_w == 0.0
    assert it.elapsed_s == 0.0


def test_tracks_peak_and_elapsed():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    for w in (10.0, 250.0, 30.0):
        it.add(w)
        state["t"] += 60.0
    assert it.peak_w == 250.0
    assert it.elapsed_s == pytest.approx(120.0)


def test_mean_power_is_time_weighted():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(100.0)
    state["t"] = 3600.0
    it.add(100.0)
    assert it.mean_w == pytest.approx(100.0)


def test_load_dropping_to_zero_is_handled():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(100.0)
    state["t"] = 3600.0
    it.add(0.0)  # plug switched off
    assert it.energy_wh == pytest.approx(50.0)


def test_joule_conversion():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(1.0)
    state["t"] = 3600.0
    it.add(1.0)
    assert it.energy_joules == pytest.approx(3600.0)


def test_cost_calculation():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(1000.0)
    state["t"] = 3600.0
    it.add(1000.0)
    assert it.cost(0.32) == pytest.approx(0.32)


def test_reset_clears_everything():
    state, now = fixed_clock()
    it = Integrator(time_source=now)
    it.add(500.0)
    state["t"] = 3600.0
    it.add(500.0)
    it.reset()
    assert it.energy_wh == 0.0 and it.samples == 0 and it.peak_w == 0.0


def test_rejects_negative_power():
    with pytest.raises(ValueError):
        Integrator().add(-1.0)


def test_rejects_time_going_backwards():
    it = Integrator()
    it.add(10.0, at=100.0)
    with pytest.raises(ValueError, match="non-decreasing"):
        it.add(10.0, at=50.0)
