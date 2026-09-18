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
    """This machine's candidate addresses, the one a peer most likely needs first.

    A box with WSL2, Hyper-V or a VPN has several non-loopback addresses and
    the first one gethostbyname_ex returns is often the virtual adapter. The
    address the default route leaves by is the one a LAN peer can reach, so it
    goes first; link-local (169.254.x) is never reachable from anywhere and is
    dropped.
    """
    addrs: list[str] = []
    try:
        # connect() on a UDP socket sends nothing; it only picks the source
        # address the OS would route from.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))              # TEST-NET-1: never answers
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        _, _, more = socket.gethostbyname_ex(socket.gethostname())
    except OSError:
        more = []
    for a in more:
        if a not in addrs and not a.startswith(("127.", "169.254.")):
            addrs.append(a)
    return addrs


def peer_instructions(cfg: Config, host: str | None = None, *,
                      token: str | None = None, name: str = "<your-name>") -> str:
    """The commands a peer runs, with the credential filled in. With `token`
    and `name` these are for one named peer; without, for the admin token."""
    candidates = [host] if host else (lan_addresses() or ["<this-host>"])
    addr = candidates[0]
    base = f"http://{addr}:{cfg.port}"
    t = token or cfg.token
    others = ""
    if len(candidates) > 1:
        others = (f"\n  (this machine also has {', '.join(candidates[1:])}; "
                  f"{addr} is the default-route address, which is usually the right one)")
    whom = f"On '{name}'" if name != "<your-name>" else "On a peer"
    return "\n".join([
        f"{whom}, register this bridge ('{cfg.self_name}') with Claude Code:{others}",
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
        f"  Monitor(ws: {{url: \"ws://{addr}:{cfg.port}/notify?agent={name}\",",
        f"               protocols: [\"bridge\", \"bearer.{t}\"]}}, ...)",
        "",
        "Or from a shell already mid-session:",
        "",
        f"  curl -H \"Authorization: Bearer {t}\" \"{base}/api/inbox?agent={name}\"",
    ])


def _load_data(args) -> tuple[Path, dict] | None:
    path = Path(args.config) if args.config else default_config_path()
    if not path.exists():
        print(f"no config at {path}. Run 'agent-bridge init' first.", file=sys.stderr)
        return None
    return path, json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: dict) -> None:
    Config(data)                                        # refuse before writing, not after
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def cmd_peer(args) -> int:
    loaded = _load_data(args)
    if loaded is None:
        return 1
    path, data = loaded
    peers = data["peers"] = data.get("peers") or {}      # tolerates "peers": null, as Config does
    name = (args.name or "").strip().lower()

    if args.action == "list":
        for n in sorted(peers):
            print(n)
        if not peers:
            print("(no peers; every caller uses the admin token)", file=sys.stderr)
        return 0
    if not name:
        print(f"'peer {args.action}' needs a name", file=sys.stderr)
        return 1

    if args.action == "remove":
        if name not in peers:
            print(f"no peer '{name}'", file=sys.stderr)
            return 1
        del peers[name]
        _save(path, data)
        print(f"removed '{name}'. Restart the bridge; its token no longer works.")
        return 0

    if args.action == "add":
        if name in peers and not args.rotate:
            print(f"peer '{name}' exists. Use --rotate to replace its token, "
                  f"or 'peer show {name}' to print it.", file=sys.stderr)
            return 1
        peers[name] = {**peers.get(name, {}), "token": new_token()}
        _save(path, data)
        print(f"{'rotated' if args.rotate else 'added'} '{name}'. "
              f"Restart the bridge, then on {name}:\n")
    elif name not in peers:                             # show
        print(f"no peer '{name}'", file=sys.stderr)
        return 1

    cfg = Config(data)
    print(peer_instructions(cfg, token=cfg.peers[name]["token"], name=name))
    return 0


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

    t = sub.add_parser("token", help="print the peer-side commands for the admin token")
    add_config(t)
    t.add_argument("--rotate", action="store_true", help="generate a new token first")
    t.set_defaults(fn=cmd_token)

    pr = sub.add_parser("peer", help="manage per-peer credentials")
    add_config(pr)
    pr.add_argument("action", choices=["add", "show", "remove", "list"])
    pr.add_argument("name", nargs="?", default="", help="the peer's name (its mailbox)")
    pr.add_argument("--rotate", action="store_true", help="with add: replace an existing peer's token")
    pr.set_defaults(fn=cmd_peer)

    argv = sys.argv[1:] if argv is None else argv
    # No subcommand (the old spelling, and what bridge.ps1 runs) means serve.
    if not any(a in ("serve", "init", "token", "peer", "-h", "--help") for a in argv):
        argv = ["serve", *argv]
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
