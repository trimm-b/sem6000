from sem6000.prometheus import _State
from sem6000.protocol import Measurement

ADDR = "AA:BB:CC:DD:EE:FF"


def sample(on=True, power=26.2, current=0.234):
    return Measurement(
        on=on,
        power_w=power,
        voltage_v=231,
        current_a=current,
        frequency_hz=50,
        energy_wh=0,
        energy_available=False,
    )


def metrics(text):
    """Parse exposition text into {name{labels}: value}."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.rpartition(" ")
        out[name] = float(value)
    return out


def test_reports_down_before_first_poll():
    m = metrics(_State().render(ADDR))
    assert m[f'sem6000_up{{address="{ADDR}"}}'] == 0
    # No stale readings may be exposed before we have any.
    assert not any("power_watts" in k for k in m)


def test_reports_values_after_poll():
    s = _State()
    s.set(sample(), energy_wh=1.5)
    m = metrics(s.render(ADDR))
    labels = f'{{address="{ADDR}"}}'
    assert m[f"sem6000_up{labels}"] == 1
    assert m[f"sem6000_power_watts{labels}"] == 26.2
    assert m[f"sem6000_socket_on{labels}"] == 1
    assert m[f"sem6000_energy_watthours_total{labels}"] == 1.5


def test_failure_flips_up_and_counts():
    s = _State()
    s.set(sample(), 0.0)
    s.fail()
    m = metrics(s.render(ADDR))
    labels = f'{{address="{ADDR}"}}'
    assert m[f"sem6000_up{labels}"] == 0
    assert m[f"sem6000_poll_errors_total{labels}"] == 1
    # The last known reading is still exposed, alongside up=0.
    assert m[f"sem6000_power_watts{labels}"] == 26.2


def test_power_factor_omitted_when_socket_is_off():
    """Switched off, the device reports 0 A, so there is no ratio to publish -
    and no misleading 0.0 either."""
    s = _State()
    s.set(sample(on=False, power=0.0, current=0.0), 0.0)
    assert "sem6000_power_factor" not in s.render(ADDR)


def test_power_factor_of_zero_is_published_for_a_reactive_load():
    """Zero real power with current still flowing is a true PF of 0, not a
    missing value."""
    s = _State()
    s.set(sample(power=0.0, current=0.2), 0.0)
    assert f'sem6000_power_factor{{address="{ADDR}"}} 0.0000' in s.render(ADDR)


def test_every_metric_has_help_and_type():
    s = _State()
    s.set(sample(), 1.0)
    text = s.render(ADDR)
    names = {line.split("{")[0] for line in text.splitlines() if not line.startswith("#")}
    for name in names:
        assert f"# HELP {name} " in text, f"{name} missing HELP"
        assert f"# TYPE {name} " in text, f"{name} missing TYPE"


def test_counter_names_end_in_total():
    s = _State()
    s.set(sample(), 1.0)
    for line in s.render(ADDR).splitlines():
        if line.startswith("# TYPE ") and line.endswith(" counter"):
            assert line.split()[2].endswith("_total")
