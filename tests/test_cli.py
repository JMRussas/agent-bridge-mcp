#
#  agent-bridge-mcp - Copyright(c) 2026
#

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.cli

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


def test_an_agent_credential_counts_as_a_credential():
    # S1: "no token and no agents" is the open condition, not "no token".
    refuse_open_bind("0.0.0.0", "", {"sisyphus": {"token": "abc"}})
    refuse_open_bind("0.0.0.0", "CHANGE_ME", {"sisyphus": {"token": "abc"}})
    with pytest.raises(SystemExit, match="refusing to bind"):
        refuse_open_bind("0.0.0.0", "", {})


def test_agent_instructions_drop_link_local_and_lead_with_the_route_address(monkeypatch):
    from agent_bridge import cli
    monkeypatch.setattr(cli, "lan_addresses", lambda: ["192.168.1.10", "172.28.0.1"])
    text = cli.registration(Config({"token": "abc", "self_name": "x"}))
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


# --- agent -------------------------------------------------------------------------

def test_agent_add_generates_a_credential_and_prints_its_commands(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "fenrir"])
    capsys.readouterr()
    assert main(["agent", "add", "Sisyphus", "--config", str(cfg)]) == 0
    data = json.loads(cfg.read_text())
    token = data["agents"]["sisyphus"]["token"]
    assert HEX64.match(token) and token != data["token"]
    out = capsys.readouterr().out
    assert token in out and "?agent=sisyphus" in out and "For 'sisyphus'" in out
    assert data["token"] not in out                    # the admin token is not handed to an agent
    # Both runtimes, and the token never on Codex's command line.
    assert "claude mcp add" in out and "codex mcp add" in out
    assert f"FENRIR_BRIDGE_TOKEN = \"{token}\"" in out and "--bearer-token-env-var FENRIR_BRIDGE_TOKEN" in out


def test_agent_add_keeps_a_description_and_local_prints_loopback(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "fenrir"])
    capsys.readouterr()                                  # init prints the LAN address itself
    assert main(["agent", "add", "rl-codex", "--description", "Codex CLI in D:/Git/Rogue-Lite",
                 "--local", "--config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8791/mcp" in out and "192.168" not in out
    assert "rl-codex: Codex CLI in D:/Git/Rogue-Lite" in out
    assert json.loads(cfg.read_text())["agents"]["rl-codex"]["description"] == "Codex CLI in D:/Git/Rogue-Lite"
    # Rotating keeps the description; list shows it.
    main(["agent", "add", "rl-codex", "--rotate", "--config", str(cfg)])
    assert json.loads(cfg.read_text())["agents"]["rl-codex"]["description"] == "Codex CLI in D:/Git/Rogue-Lite"
    capsys.readouterr()
    main(["agent", "list", "--config", str(cfg)])
    assert "rl-codex" in capsys.readouterr().out.splitlines()[0]


def test_the_old_peers_key_is_refused_with_a_pointer(tmp_path: Path):
    with pytest.raises(SystemExit, match="'peers' is now 'agents'"):
        Config({"peers": {"a": {"token": "abc"}}})


def test_a_role_name_cannot_contain_the_instance_separator():
    with pytest.raises(SystemExit, match="instance"):
        Config({"agents": {"rl-claude#1": {"token": "abc"}}})


def test_a_description_is_one_line_and_capped():
    cfg = Config({"agents": {"a": {"token": "abc", "description": "  two\n lines " + "x" * 400}}})
    d = cfg.agents["a"]["description"]
    assert d.startswith("two lines x") and len(d) == 200


def test_agent_add_refuses_a_duplicate_unless_rotating(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    main(["agent", "add", "a", "--config", str(cfg)])
    first = json.loads(cfg.read_text())["agents"]["a"]["token"]
    assert main(["agent", "add", "a", "--config", str(cfg)]) == 1
    assert "--rotate" in capsys.readouterr().err
    assert main(["agent", "add", "a", "--rotate", "--config", str(cfg)]) == 0
    assert json.loads(cfg.read_text())["agents"]["a"]["token"] != first


def test_agent_add_refuses_a_reserved_name_before_writing(tmp_path: Path):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg), "--self-name", "fenrir"])
    before = cfg.read_text()
    with pytest.raises(SystemExit, match="reserved"):
        main(["agent", "add", "fenrir", "--config", str(cfg)])
    assert cfg.read_text() == before


def test_agent_commands_tolerate_a_null_agents_key(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    data = json.loads(cfg.read_text()); data["agents"] = None
    cfg.write_text(json.dumps(data))
    assert main(["agent", "list", "--config", str(cfg)]) == 0
    assert main(["agent", "add", "a", "--config", str(cfg)]) == 0
    assert list(json.loads(cfg.read_text())["agents"]) == ["a"]


def test_agent_show_list_remove(tmp_path: Path, capsys):
    cfg = tmp_path / "config.json"
    main(["init", "--config", str(cfg)])
    main(["agent", "add", "b", "--config", str(cfg)])
    main(["agent", "add", "a", "--config", str(cfg)])
    capsys.readouterr()
    assert main(["agent", "list", "--config", str(cfg)]) == 0
    assert capsys.readouterr().out.split() == ["a", "b"]
    assert main(["agent", "show", "a", "--config", str(cfg)]) == 0
    assert json.loads(cfg.read_text())["agents"]["a"]["token"] in capsys.readouterr().out
    assert main(["agent", "remove", "a", "--config", str(cfg)]) == 0
    assert list(json.loads(cfg.read_text())["agents"]) == ["b"]
    assert main(["agent", "remove", "a", "--config", str(cfg)]) == 1
    assert main(["agent", "show", "--config", str(cfg)]) == 1    # needs a name


def test_no_subcommand_means_serve(monkeypatch):
    seen = {}
    monkeypatch.setattr("agent_bridge.server.serve",
                        lambda c, h, p: seen.update(config=c, host=h, port=p) or 0)
    assert main(["--host", "127.0.0.1", "--port", "1"]) == 0
    assert seen == {"config": None, "host": "127.0.0.1", "port": 1}


# --- nothing project-specific in the core (G1, G2) ---------------------------
#
# A config that names no roots, logs or commands must produce a bridge that
# names none: the instructions string, the tool list and the defaults all come
# from the config, never from wherever this was first deployed.

def test_instructions_name_only_what_the_config_names():
    from agent_bridge.logs import Logs
    from agent_bridge.server import _instructions
    cfg = Config({"self_name": "hub", "token": "t", "mailbox_store": ""})
    text = _instructions(cfg, Logs({}))
    assert "hub" in text
    for word in ("logs_read", "bridge_run", "root", "/", "\\"):
        assert word not in text.split("bridge_capabilities")[0]
    assert "logs_" not in text and "bridge_run" not in text

    cfg = Config({"self_name": "hub", "token": "t", "mailbox_store": "",
                  "description": "the build box", "roots": {"repo-a": "."},
                  "logs": {"names": ["app.log"]},
                  "exec": {"enabled": True, "commands": {"build": {"argv": ["make"]}}}})
    text = _instructions(cfg, Logs(cfg.roots, names=cfg.logs["names"]))
    assert "the build box" in text and "repo-a" in text
    assert "app.log" in text and "build" in text


async def test_no_logs_block_means_no_log_tools():
    from agent_bridge.server import build

    async def tools(data):
        app = build(Config({"token": "t", "mailbox_store": "", **data}))
        return {t.name for t in await app.state.mcp.list_tools()}

    bare = await tools({})
    assert not any(t.startswith(("logs_", "avatar_")) for t in bare), bare
    assert {"bridge_send", "bridge_inbox", "bridge_read", "bridge_run"} <= bare
    with_logs = await tools({"logs": {"names": ["app.log"]}})
    assert {"logs_list", "logs_read"} <= with_logs


def test_old_avatar_keys_are_refused_with_a_pointer(tmp_path):
    with pytest.raises(SystemExit, match="docs/examples"):
        Config({"token": "t", "gifterboard": {"url": "x"}})
    with pytest.raises(SystemExit, match="docs/examples"):
        Config({"token": "t", "output_dir": "out"})


def test_logs_config_is_validated():
    with pytest.raises(SystemExit, match="filenames, not paths"):
        Config({"token": "t", "logs": {"names": ["bin/app.log"]}})
    with pytest.raises(SystemExit, match="list of filenames"):
        Config({"token": "t", "logs": {"names": "app.log"}})


def test_self_name_defaults_to_the_hostname():
    import socket
    assert Config({"token": "t"}).self_name == socket.gethostname().lower()
