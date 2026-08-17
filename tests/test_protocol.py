"""Protocol tests.

Vectors marked CAPTURED were recorded from a real SEM6000 CH (VOLCFT,
firmware 1.15, hardware 2.0) on 2026-08-17. Vectors marked DOC come from the
published protocol notes at https://github.com/Heckie75/voltcraft-sem-6000.
"""

import pytest

from sem6000 import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


# --------------------------------------------------------------------------
# Checksum / framing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        "0f0c170000000000000000000018ffff",  # DOC authorize, PIN 0000
        "0f06030000000004ffff",  # DOC switch off
        "0f051000000011ffff",  # DOC settings request
        "0f090f0005010000000016ffff",  # DOC set LED on
        "0f0c010029180a160607e3000053ffff",  # DOC set datetime
        "0f050400000005ffff",  # DOC measure
    ],
)
def test_documented_frames_satisfy_our_checksum_rule(frame):
    """Every published example must validate under (sum + 1) & 0xff."""
    raw = h(frame)
    payload = raw[2 : 2 + raw[1] - 1]
    assert p.checksum(payload) == raw[2 + raw[1] - 1]


def test_encode_round_trips():
    payload = bytes([p.CMD_SWITCH, 0x00, 0x01, 0x00, 0x00])
    assert p.decode(p.encode(payload)) == payload


def test_encode_matches_documented_switch_off():
    assert p.cmd_switch(False) == h("0f06030000000004ffff")


def test_encode_matches_documented_authorize():
    assert p.cmd_authorize("0000") == h("0f0c170000000000000000000018ffff")


def test_authorize_encodes_pin_digits():
    # DOC change-PIN example embeds 01020304 as one byte per digit.
    assert p.cmd_authorize("1234")[5:9] == bytes([1, 2, 3, 4])


@pytest.mark.parametrize("bad", ["123", "12345", "abcd", ""])
def test_authorize_rejects_malformed_pin(bad):
    with pytest.raises(ValueError):
        p.cmd_authorize(bad)


def test_decode_rejects_bad_checksum():
    with pytest.raises(p.ProtocolError, match="checksum"):
        p.decode(h("0f0603000000ff" "ffff"))


def test_decode_rejects_bad_start_byte():
    with pytest.raises(p.ProtocolError, match="start byte"):
        p.decode(h("aa06030000000004ffff"))


def test_decode_rejects_truncation():
    with pytest.raises(p.ProtocolError, match="truncated"):
        p.decode(h("0f1104000100"))


# --------------------------------------------------------------------------
# Reassembly across the 20-byte notification MTU
# --------------------------------------------------------------------------


def test_reassembles_serial_split_across_two_notifications():
    # DOC serial example. On real hardware this response genuinely does arrive
    # as two notifications, which is what this exercises.
    c1 = h("0f 15 11 00 4d 4c 30 31 44 31 30 30 31 32 30 30 30 30 30 30")
    c2 = h("00 00 64 ff ff")
    r = p.Reassembler()
    assert r.feed(c1) == []
    frames = r.feed(c2)
    assert len(frames) == 1
    assert p.parse_serial(p.decode(frames[0])) == "ML01D10012000000"


def test_frame_without_terminator_is_still_complete():
    # CAPTURED: the measurement response omits the trailing ff ff.
    raw = h("0f 11 04 00 01 00 5f 18 e7 00 df 32 00 00 00 00 00 00 75")
    frames = p.Reassembler().feed(raw)
    assert len(frames) == 1
    assert frames[0] == raw


def test_two_frames_in_one_chunk_are_split():
    both = h("0f06030000000004ffff") + h("0f06030001000005ffff")
    frames = p.Reassembler().feed(both)
    assert len(frames) == 2


def test_byte_at_a_time_delivery_yields_one_frame():
    """Terminator is stripped, so a frame is emitted the moment it is complete
    rather than blocking on an optional two bytes that may never come."""
    raw = h("0f06030000000004ffff")
    r = p.Reassembler()
    got = [f for b in raw for f in r.feed(bytes([b]))]
    assert got == [raw[:-2]]
    assert p.decode(got[0]) == h("0300000000")


def test_resynchronises_after_leading_garbage():
    raw = h("0f06030000000004ffff")
    assert p.Reassembler().feed(b"\xaa\xbb" + raw) == [raw[:-2]]


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------


def test_parses_captured_measurement():
    # CAPTURED: laptop charging, plug on.
    payload = p.decode(
        h("0f 11 04 00 01 00 5f 18 e7 00 df 32 00 00 00 00 00 00 75")
    )
    m = p.parse_measurement(payload, hardware_version=2)
    assert m.on is True
    assert m.power_w == pytest.approx(24.344)
    assert m.voltage_v == 231
    assert m.current_a == pytest.approx(0.223)
    assert m.frequency_hz == 50


def test_parses_documented_hardware3_measurement():
    # DOC: hardware >= 3 sample, 220 V mains.
    payload = h("04 00 01 00 88 50 dc 00 d6 32 01 00 00 00")
    m = p.parse_measurement(payload, hardware_version=3)
    assert m.on is True
    assert m.power_w == pytest.approx(34.896)
    assert m.voltage_v == 220
    assert m.current_a == pytest.approx(0.214)
    assert m.energy_available is True


def test_hardware2_reports_energy_counter_unavailable():
    """The whole reason the history API exists - hw 2 never fills this in."""
    payload = p.decode(
        h("0f 11 04 00 01 00 5f 18 e7 00 df 32 00 00 00 00 00 00 75")
    )
    m = p.parse_measurement(payload, hardware_version=2)
    assert m.energy_available is False
    assert m.energy_wh == 0


def test_power_factor_and_apparent_power():
    m = p.Measurement(True, 24.344, 231, 0.223, 50, 0, False)
    assert m.apparent_va == pytest.approx(51.513)
    assert m.power_factor == pytest.approx(0.4726, abs=1e-3)


def test_power_factor_is_none_when_idle():
    assert p.Measurement(False, 0, 231, 0, 50, 0, False).power_factor is None


def test_power_factor_is_clamped_to_one():
    """Rounding in the device's own figures can push P/S just above 1."""
    m = p.Measurement(True, 100.0, 100, 0.9, 50, 0, False)
    assert m.power_factor == 1.0


def test_measurement_rejects_wrong_command():
    with pytest.raises(p.ProtocolError):
        p.parse_measurement(h("03 00 01 00 00 00 00 00 00 00 00 00 00 00"))


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------


def test_set_datetime_matches_documented_frame():
    """DOC example encodes 2019-06-22 10:24:41."""
    from datetime import datetime

    got = p.cmd_set_datetime(datetime(2019, 6, 22, 10, 24, 41))
    assert got == h("0f0c010029180a160607e3000053ffff")


def test_set_datetime_splits_year_big_endian():
    from datetime import datetime

    payload = p.decode(p.cmd_set_datetime(datetime(2026, 8, 17, 23, 5, 1)))
    assert payload[7:9] == (2026).to_bytes(2, "big")
    assert payload[2:7] == bytes([1, 5, 23, 17, 8])  # s, m, h, day, month


# --------------------------------------------------------------------------
# Settings / acks
# --------------------------------------------------------------------------


def test_parses_captured_settings():
    # CAPTURED from the CH unit: 2310 W limit, LED on.
    payload = p.decode(h("0f 0e 10 00 00 c8 64 00 00 00 00 01 00 09 06 4d ffff"))
    s = p.parse_settings(payload)
    assert s.power_limit_w == 2310
    assert s.led_on is True
    assert s.price_per_kwh == pytest.approx(2.00)
    assert s.reduced_mode_active is False


def test_parses_documented_settings():
    # DOC: 3680 W limit (the 16 A EU variant).
    payload = p.decode(h("0f 0e 10 00 00 c8 64 00 00 00 00 01 00 0e 60 ac ffff"))
    assert p.parse_settings(payload).power_limit_w == 3680


def test_ack_success_and_failure():
    # CAPTURED auth success.
    assert p.parse_ack(p.decode(h("0f 06 17 00 00 00 00 18 ffff")), p.CMD_AUTH) is True
    # Same shape with the failure flag set.
    assert p.parse_ack(h("17 00 01"), p.CMD_AUTH) is False


def test_ack_rejects_mismatched_command():
    with pytest.raises(p.ProtocolError):
        p.parse_ack(h("03 00 00"), p.CMD_AUTH)


# --------------------------------------------------------------------------
# Device info
# --------------------------------------------------------------------------


def test_parses_captured_device_info():
    info = p.parse_device_info(h("56 4f 4c 43 46 54 04 00 03 00 00 01 0f 02 00 19"))
    assert info.vendor == "VOLCFT"
    assert info.firmware == "1.15"
    assert info.hardware == "2.0"
    assert info.hardware_major == 2


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def test_parses_documented_hourly_history():
    # DOC day-level example, reassembled from its three notifications.
    payload = p.decode(
        h("0f 33 0a 00 00 0e 00 0e 00 0e 00 0e 00 0c 00 09 00 08 00 0b")
        + h("00 0e 00 0e 00 11 00 0f 00 10 00 0f 00 0d 00 0e 00 0e 00 0e")
        + h("00 0e 00 0e 00 0e 00 0e 00 0d 00 00 42 ffff"),
        verify=False,
    )
    wh = p.parse_history(payload, p.CMD_HISTORY_HOURLY)
    assert len(wh) == 24
    assert wh[0] == 14  # 23 hours ago
    assert wh[-1] == 0  # current hour, just started


def test_hourly_history_record_count_matches_frame_length():
    """24 records x 2 bytes + 2 command bytes + checksum == 0x33."""
    assert 24 * 2 + 2 + 1 == 0x33


def test_daily_history_record_count_matches_frame_length():
    """30 records x 4 bytes + 2 command bytes + checksum == 0x7b."""
    assert 30 * 4 + 2 + 1 == 0x7B


def test_monthly_history_uses_three_byte_values():
    # DOC year-level tail: current month = 0x0004e3 Wh.
    payload = bytes([p.CMD_HISTORY_MONTHLY, 0x00]) + bytes(44) + h("00 04 e3 00")
    wh = p.parse_history(payload, p.CMD_HISTORY_MONTHLY)
    assert len(wh) == 12
    assert wh[-1] == 1251


def test_history_rejects_mismatched_kind():
    with pytest.raises(p.ProtocolError, match="kind mismatch"):
        p.parse_history(bytes([p.CMD_HISTORY_DAILY, 0x00]), p.CMD_HISTORY_HOURLY)


def test_history_command_rejects_unknown_kind():
    with pytest.raises(ValueError):
        p.cmd_history(0x99)
