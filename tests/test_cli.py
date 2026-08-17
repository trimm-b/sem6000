"""CLI argument parsing.

The global options (--address, --pin, --json) must work both before and after
the subcommand. argparse does not do this by itself, and getting it wrong is
invisible until someone types the natural thing and gets "unrecognized
arguments".
"""

import pytest

from sem6000.cli import build_parser

# Every subcommand, with the positional arguments it requires.
COMMANDS = [
    ["status"],
    ["on"],
    ["off"],
    ["toggle"],
    ["settings"],
    ["sync-time"],
    ["discover"],
    ["watch"],
    ["energy", "60"],
    ["history", "hour"],
    ["led", "on"],
    ["set-limit", "2300"],
    ["set-pin", "1234"],
    ["reset-pin"],
    ["log"],
    ["export"],
]

ADDR = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def parser(monkeypatch):
    # Do not let a real SEM6000_ADDRESS in the environment mask a failure.
    monkeypatch.delenv("SEM6000_ADDRESS", raising=False)
    monkeypatch.delenv("SEM6000_PIN", raising=False)
    return build_parser()


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_address_accepted_after_subcommand(parser, cmd):
    """The regression: 'sem6000 set-pin 1234 --address ...' must work."""
    args = parser.parse_args(cmd + ["--address", ADDR])
    assert args.address == ADDR


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_address_accepted_before_subcommand(parser, cmd):
    args = parser.parse_args(["--address", ADDR] + cmd)
    assert args.address == ADDR


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_pin_accepted_in_either_position(parser, cmd):
    assert parser.parse_args(cmd + ["--pin", "4271"]).pin == "4271"
    assert parser.parse_args(["--pin", "4271"] + cmd).pin == "4271"


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_json_accepted_in_either_position(parser, cmd):
    assert parser.parse_args(cmd + ["--json"]).json is True
    assert parser.parse_args(["--json"] + cmd).json is True


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_defaults_when_flags_absent(parser, cmd):
    args = parser.parse_args(cmd)
    assert args.address is None
    assert args.pin == "0000"
    assert args.json is False


@pytest.mark.parametrize("cmd", COMMANDS, ids=lambda c: c[0])
def test_subcommand_help_documents_global_options(parser, cmd, capsys):
    """The user who runs 'set-pin -h' must be told about --address."""
    with pytest.raises(SystemExit):
        parser.parse_args([cmd[0], "-h"])
    out = capsys.readouterr().out
    assert "--address" in out, f"{cmd[0]} -h does not mention --address"
    assert "--pin" in out, f"{cmd[0]} -h does not mention --pin"


def test_environment_supplies_address(monkeypatch):
    monkeypatch.setenv("SEM6000_ADDRESS", ADDR)
    monkeypatch.setenv("SEM6000_PIN", "9999")
    args = build_parser().parse_args(["status"])
    assert args.address == ADDR
    assert args.pin == "9999"


def test_explicit_flag_beats_environment(monkeypatch):
    """A --address after the subcommand must not be silently ignored in
    favour of the environment value."""
    monkeypatch.setenv("SEM6000_ADDRESS", "11:11:11:11:11:11")
    args = build_parser().parse_args(["status", "--address", ADDR])
    assert args.address == ADDR


def test_environment_survives_a_subcommand_that_omits_the_flag(monkeypatch):
    """SUPPRESS check: the subcommand must not overwrite the env default."""
    monkeypatch.setenv("SEM6000_ADDRESS", ADDR)
    args = build_parser().parse_args(["set-pin", "1234"])
    assert args.address == ADDR


def test_subcommand_specific_options_still_work(parser):
    args = parser.parse_args(["energy", "60", "--price", "0.32", "--interval", "2"])
    assert args.duration == 60 and args.price == 0.32 and args.interval == 2

    args = parser.parse_args(["log", "--sqlite", "p.db", "--csv", "p.csv"])
    assert args.sqlite == "p.db" and args.csv == "p.csv"

    args = parser.parse_args(["export", "--port", "9999"])
    assert args.port == 9999
