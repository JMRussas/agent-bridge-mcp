#
#  agent-bridge-mcp - Copyright(c) 2026
#

# The command line: `agent-bridge serve|init|token`.
#
# `serve` with no subcommand is the default, so `python -m agent_bridge` and
# bridge.ps1 keep working unchanged. `init` exists because the token used to be
# set by hand-editing a copied example, which is how CHANGE_ME ends up live on a
# LAN interface. `token` prints the exact commands a peer needs, so the string
# never has to be transcribed, and rotates it in one step.

import argparse
import json
import secrets
import socket
import sys
from pathlib import Path

from agent_bridge.config import DEFAULTS, Config, default_config_path


def new_token() -> str:
    # Hex, so it sits inside the WebSocket subprotocol grammar by construction.
    return secrets.token_hex(32)


def lan_addresses() -> list[str]:
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        return []
    return [a for a in addrs if not a.startswith("127.")]


def peer_instructions(cfg: Config, host: str | None = None) -> str:
    addr = host or (lan_addresses() or ["<this-host>"])[0]
    base = f"http://{addr}:{cfg.port}"
    t = cfg.token
    return "\n".join([
        f"On a peer, register this bridge ('{cfg.self_name}') with Claude Code:",
        "",
        f"  claude mcp add --transport http {cfg.self_name} {base}/mcp \\",
        f"    --header \"Authorization: Bearer {t}\"",
        "",
        "Check reachability first (no token needed):",
        "",
        f"  curl {base}/api/health",
        "",
        "Listen for messages live in Claude Code:",
        "",
        f"  Monitor(ws: {{url: \"ws://{addr}:{cfg.port}/notify?agent=<your-name>\",",
        f"               protocols: [\"bridge\", \"bearer.{t}\"]}}, ...)",
        "",
        "Or from a shell already mid-session:",
        "",
        f"  curl -H \"Authorization: Bearer {t}\" \"{base}/api/inbox?agent=<your-name>\"",
    ])


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else default_config_path()
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite it, or "
              f"'agent-bridge token --rotate' to change only the token.", file=sys.stderr)
        return 1

    data = json.loads(json.dumps(DEFAULTS))            # deep copy
    data["self_name"] = (args.self_name or socket.gethostname()).lower()
    data["token"] = new_token()
    data["roots"] = {}
    data["exec"]["enabled"] = False
    data["//roots"] = "name -> absolute path. Files are addressed as 'name:relative/path'."
    data["//exec"] = "Off until you add commands. Each entry is an argv list, keyed by name."

    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    cfg = Config(data)
    print(f"wrote {path}")
    print(f"  self_name: {cfg.self_name}")
    print(f"  token:     {cfg.token}")
    print(f"  bind:      {cfg.host}:{cfg.port}   (exec disabled, no roots yet)")
    print()
    print(peer_instructions(cfg))
    return 0


def cmd_token(args) -> int:
    path = Path(args.config) if args.config else default_config_path()
    if not path.exists():
        print(f"no config at {path}. Run 'agent-bridge init' first.", file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    if args.rotate:
        data["token"] = new_token()
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"rotated. Restart the bridge, then re-run 'claude mcp add' on every peer.\n")
    cfg = Config(data)
    print(peer_instructions(cfg))
    return 0


def cmd_serve(args) -> int:
    from agent_bridge.server import serve
    return serve(args.config, args.host, args.port)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agent-bridge")
    ap.add_argument("--config", default=None, help="path to config.json")
    sub = ap.add_subparsers(dest="cmd")

    # --config is accepted before or after the subcommand. SUPPRESS on the
    # subparser copy, or its None default would overwrite the parent's value.
    def add_config(p):
        p.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    s = sub.add_parser("serve", help="run the bridge (default)")
    add_config(s)
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_serve)

    i = sub.add_parser("init", help="write a config.json with a generated token")
    add_config(i)
    i.add_argument("--self-name", default=None, help="this machine's name (default: hostname)")
    i.add_argument("--force", action="store_true", help="overwrite an existing config")
    i.set_defaults(fn=cmd_init)

    t = sub.add_parser("token", help="print the peer-side commands for the current token")
    add_config(t)
    t.add_argument("--rotate", action="store_true", help="generate a new token first")
    t.set_defaults(fn=cmd_token)

    argv = sys.argv[1:] if argv is None else argv
    # No subcommand (the old spelling, and what bridge.ps1 runs) means serve.
    if not any(a in ("serve", "init", "token", "-h", "--help") for a in argv):
        argv = ["serve", *argv]
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
