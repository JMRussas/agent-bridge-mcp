#
#  agent-bridge-mcp - Copyright(c) 2026
#

import json
import re
from pathlib import Path

import pytest

from agent_bridge.cli import main
from agent_bridge.config import Config
from agent_bridge.server import refuse_open_bind

HEX64 = re.compile(r"^[0-9a-f]{64}$")


# --- refusing an open bind ------------------------------------------------------
#
# Regression: a copied example config has "CHANGE_ME" in it and the default bind
# is 0.0.0.0. The old behaviour was a log line and then serving anyway.

@pytest.mark.parametrize("token", ["", "CHANGE_ME", "changeme"])
def test_lan_bind_with_a_placeholder_token_is_refused(token):
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind("0.0.0.0", token)
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind("192.168.1.174", token)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "localhost", "::1"])
def test_loopback_without_a_token_is_allowed(host):
    refuse_open_bind(host, "")


def test_a_real_token_binds_anywhere():
    refuse_open_bind("0.0.0.0", "0123456789abcdef")


def test_a_null_token_is_no_token(tmp_path: Path):
    # Regression: "token": null slipped past the placeholder check as None and
    # then authorised() treated it as "no token configured" - an open bind.
    assert Config({"token": None}).token == ""
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind("0.0.0.0", Config({"token": None}).token)


@pytest.mark.parametrize("host", ["::1234", "localhost.lan", "127.0.0.1.evil", "0.0.0.0", "::"])
def test_lookalike_hosts_are_not_loopback(host):
    # Regression: a string-prefix test let "::1234" and "localhost.lan" through.
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind(host, "")


def test_localhost_is_case_insensitive():
    refuse_open_bind("LOCALHOST", "")


def test_a_peer_credential_counts_as_a_credential():
    # S1: "no token and no peers" is the open condition, not "no token".
    refuse_open_bind("0.0.0.0", "", {"sisyphus": {"token": "abc"}})
    refuse_open_bind("0.0.0.0", "CHANGE_ME", {"sisyphus": {"token": "abc"}})
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind("0.0.0.0", "", {})


def test_peer_instructions_drop_link_local_and_lead_with_the_route_address(monkeypatch):
    from agent_bridge import cli
    monkeypatch.setattr(cli, "lan_addresses", lambda: ["192.168.1.10", "172.28.0.1"])
    text = cli.peer_instructions(Config({"token": "abc", "self_name": "x"}))
    assert "http://192.168.1.10:8791/mcp" in text
    assert "also has 172.28.0.1" in text


# --- init / token ----------------------------------------------------------------

def test_init_writes_a_config_with_a_generated_token(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    assert main(["init", "--config", str(cfg), "--self-name", "Demo"]) == 0
    data = json.loads(cfg.read_text())
    assert HEX64.match(data["token"])
    assert data["self_name"] == "demo"
    assert data["exec"]["enabled"] is False
    assert data["roots"] == {}
    out = capsys.readouterr().out
    assert data["token"] in out                       # the peer commands are printed
    assert "claude mcp add" in out and "bearer." in out


def test_config_flag_works_before_the_subcommand(tmp_path: Path):
    cfg = tmp_path / "config.json"
    assert main(["--config", str(cfg), "init"]) == 0
    assert cfg.exists()


def test_init_refuses_to_overwrite_without_force(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    first = json.loads(cfg.read_text())["token"]
    assert main(["init", "--config", str(cfg)]) == 1
    assert json.loads(cfg.read_text())["token"] == first
    assert "already exists" in capsys.readouterr().err
    assert main(["init", "--config", str(cfg), "--force"]) == 0
    assert json.loads(cfg.read_text())["token"] != first


def test_token_rotate_changes_only_the_token(tmp_path: Path):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "keep"])
    before = json.loads(cfg.read_text())
    assert main(["token", "--rotate", "--config", str(cfg)]) == 0
    after = json.loads(cfg.read_text())
    assert after["token"] != before["token"] and HEX64.match(after["token"])
    assert {k: v for k, v in after.items() if k != "token"} == \
           {k: v for k, v in before.items() if k != "token"}


def test_token_without_a_config_points_at_init(tmp_path: Path, capsys):
    assert main(["token", "--config", str(tmp_path / "none.json")]) == 1
    assert "agent-bridge init" in capsys.readouterr().err


# --- peer -------------------------------------------------------------------------

def test_peer_add_generates_a_credential_and_prints_its_commands(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "fenrir"])
    capsys.readouterr()
    assert main(["peer", "add", "Sisyphus", "--config", str(cfg)]) == 0
    data = json.loads(cfg.read_text())
    token = data["peers"]["sisyphus"]["token"]
    assert HEX64.match(token) and token != data["token"]
    out = capsys.readouterr().out
    assert token in out and "?agent=sisyphus" in out and "On 'sisyphus'" in out
    assert data["token"] not in out                    # the admin token is not handed to a peer


def test_peer_add_refuses_a_duplicate_unless_rotating(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    main(["peer", "add", "a", "--config", str(cfg)])
    first = json.loads(cfg.read_text())["peers"]["a"]["token"]
    assert main(["peer", "add", "a", "--config", str(cfg)]) == 1
    assert "--rotate" in capsys.readouterr().err
    assert main(["peer", "add", "a", "--rotate", "--config", str(cfg)]) == 0
    assert json.loads(cfg.read_text())["peers"]["a"]["token"] != first


def test_peer_add_refuses_a_reserved_name_before_writing(tmp_path: Path):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "fenrir"])
    before = cfg.read_text()
    with pytest.raises(SystemExit, match="reserved"):
        main(["peer", "add", "fenrir", "--config", str(cfg)])
    assert cfg.read_text() == before


def test_peer_commands_tolerate_a_null_peers_key(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    data = json.loads(cfg.read_text()); data["peers"] = None
    cfg.write_text(json.dumps(data))
    assert main(["peer", "list", "--config", str(cfg)]) == 0
    assert main(["peer", "add", "a", "--config", str(cfg)]) == 0
    assert list(json.loads(cfg.read_text())["peers"]) == ["a"]


def test_peer_show_list_remove(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    main(["peer", "add", "b", "--config", str(cfg)])
    main(["peer", "add", "a", "--config", str(cfg)])
    capsys.readouterr()
    assert main(["peer", "list", "--config", str(cfg)]) == 0
    assert capsys.readouterr().out.split() == ["a", "b"]
    assert main(["peer", "show", "a", "--config", str(cfg)]) == 0
    assert json.loads(cfg.read_text())["peers"]["a"]["token"] in capsys.readouterr().out
    assert main(["peer", "remove", "a", "--config", str(cfg)]) == 0
    assert list(json.loads(cfg.read_text())["peers"]) == ["b"]
    assert main(["peer", "remove", "a", "--config", str(cfg)]) == 1
    assert main(["peer", "show", "--config", str(cfg)]) == 1    # needs a name


def test_no_subcommand_means_serve(monkeypatch):
    seen = {}
    monkeypatch.setattr("agent_bridge.server.serve",
                        lambda c, h, p: seen.update(config=c, host=h, port=p) or 0)
    assert main(["--host", "127.0.0.1", "--port", "1"]) == 0
    assert seen == {"config": None, "host": "127.0.0.1", "port": 1}
