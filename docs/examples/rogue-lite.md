# Worked example: the two-machine game setup this bridge began as

The bridge was first built for one deployment, and nothing from it remains in
`src/`. This page keeps the setup as an example of what the configuration
keys are for, and of the kind of question a bridge answers without anyone
asking another agent.

## The deployment

Two machines on one LAN. `FENRIR` holds the *source* of both halves of a
system: `Rogue-Lite`, a C# game client, and `Sluzzygames`, the Node server
(`GifterBoard`) it talks to. `SISYPHUS` is where GifterBoard and its `ffmpeg`
actually *run*. The bridge runs on FENRIR; a Claude Code session on SISYPHUS
connects to it over MCP, and two local sessions (a Claude Code and a Codex CLI
in the game repo) connect over loopback.

Three agents, one credential each:

```powershell
agent-bridge agent add sisyphus  --description "Claude Code on SISYPHUS, runs GifterBoard"
agent-bridge agent add rl-claude --description "Claude Code in D:/Git/Rogue-Lite" --local
agent-bridge agent add rl-codex  --description "Codex CLI in D:/Git/Rogue-Lite" --local
```

## The config

```json
{
  "self_name": "fenrir",
  "description": "Rogue-Lite client and Sluzzygames server source; GifterBoard runs on SISYPHUS",
  "roots": {
    "rogue-lite":  "D:/Git/Rogue-Lite",
    "sluzzygames": "D:/Git/Sluzzygames"
  },
  "logs": {
    "names":     ["engine.log", "diag.log"],
    "exe_names": ["RogueLite.exe", "roguelite.exe", "RogueLite.dll"],
    "skip_dirs": ["library", "assets", "noz"]
  },
  "exec": {
    "enabled": true,
    "commands": {
      "viewers":    { "root": "rogue-lite",  "argv": ["dotnet", "run", "--project", "platform/cli", "--", "viewers"] },
      "autoplay":   { "root": "rogue-lite",  "argv": ["dotnet", "run", "--project", "platform/cli", "--", "autoplay"], "max_args": 2 },
      "build":      { "root": "rogue-lite",  "argv": ["dotnet", "build", "RogueLite.sln"] },
      "git-status": { "root": "*",           "argv": ["git", "status", "--short", "--branch"], "env": { "GIT_TERMINAL_PROMPT": "0" } },
      "git-log":    { "root": "*",           "argv": ["git", "log", "--oneline", "-20"] },
      "node-check": { "root": "sluzzygames", "argv": ["node", "--check", "game-feed.js"] },
      "ffmpeg-ver": { "root": "*",           "argv": ["ffmpeg", "-hide_banner", "-version"] }
    }
  }
}
```

Why each block is there:

- **`logs.names`**: the game writes `engine.log` beside its executable, which
  is inside `bin/` and `dist/`, exactly where `bridge_read` refuses to go.
  Naming the file lets `logs_read` serve it without opening build output.
- **`logs.exe_names`**: an old published binary predates the code that writes
  `engine.log`, so "no log" was being read as "the live feed never started".
  Naming the executables makes `logs_list` date each log against the build
  beside it and flag builds that lack one.
- **`logs.skip_dirs`**: the engine checkout and the asset tree are large and
  contain no logs.
- **`viewers` / `autoplay`**: small CLI entry points in the game, exposed by
  name so a remote agent can ask "who is connected" without a command line.

## The question that started it: the avatar contract

The client hard-codes `AvatarSize` in `ViewerRegistry.cs`; the server
hard-codes `AVATAR_SIZE` in `game-feed.js`; ffmpeg on SISYPHUS decodes each
viewer's picture to exactly `size * size * 4` bytes of raw RGBA. If the two
constants drift, *nothing fails*: the server's decoder resolves `null` on any
other length, the client discards any response that is not exactly the
expected byte count and swallows every exception, and the game draws a plain
monster, indistinguishable from a viewer who has no avatar.

The first version of the bridge shipped three tools for this: `avatar_contract`
read both constants and said whether they agreed; `avatar_probe` fetched one
avatar from the running server and reported received length against required
length, sniffing what the body actually was when wrong; `avatar_png` wrote the
decoded pixels to a file so a human could look. They were removed from the core
in Sprint 2 (roadmap G2) because they are about this project, not about
bridging; the last commit that contains them is `616fda5`. With the bridge as
it is now, the same question is answered with `bridge_grep` for the two
constants and an allowlisted `curl`-style command for the probe, which is how a
project-specific check should be expressed: in config, not in `src/`.
