#
#  agent-bridge-mcp - Copyright(c) 2026
#

# Probes for the viewer-avatar path, which is the one place in this stack where
# a producer on one machine and a consumer on another agree on a raw byte count
# and nothing checks that they still agree.
#
# The failure that motivates these tools is silent on BOTH sides:
#
#   * game-feed.js resolves decodeToRgba() to null whenever ffmpeg returns
#     anything other than exactly size*size*4 bytes.
#   * ViewerRegistry.BeginDownload throws the response away unless it is exactly
#     AvatarBytes, and swallows every exception, because "this viewer has no
#     picture" is a normal outcome.
#
# So a size disagreement between the two machines does not raise, log, or fail a
# test. It renders a plain monster, which is indistinguishable from a viewer who
# genuinely has no avatar. These tools exist to turn that into a sentence.

import hashlib
import re
import struct
import zlib
from pathlib import Path

import httpx


def _grab(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


class OutputDenied(Exception):
    pass


class Avatars:
    def __init__(self, cfg: dict, roots: dict[str, Path], output_dir: Path | None = None):
        self.url = (cfg.get("url") or "").rstrip("/")
        self.creator = cfg.get("creator") or ""
        self.token = cfg.get("token") or ""
        self.size = int(cfg.get("avatar_size") or 64)
        self.roots = roots
        # The only place this server ever writes. Resolved once so the check in
        # to_png compares against an absolute path, the same way Files does.
        self.output_dir = Path(output_dir).resolve() if output_dir else None

    # --- the contract, read from both sides rather than remembered ---

    def expectations(self) -> dict:
        game, server = {}, {}

        reg = self.roots.get("rogue-lite")
        if reg:
            p = reg / "game" / "live" / "ViewerRegistry.cs"
            if p.exists():
                t = p.read_text(encoding="utf-8", errors="replace")
                game = {
                    "file": str(p),
                    "AvatarSize": _grab(t, r"AvatarSize\s*=\s*(\d+)"),
                    "MaxViewers": _grab(t, r"MaxViewers\s*=\s*(\d+)"),
                }
                if game.get("AvatarSize"):
                    game["AvatarBytes"] = game["AvatarSize"] ** 2 * 4

        slz = self.roots.get("sluzzygames")
        if slz:
            p = slz / "game-feed.js"
            if p.exists():
                t = p.read_text(encoding="utf-8", errors="replace")
                server = {
                    "file": str(p),
                    "AVATAR_SIZE": _grab(t, r"AVATAR_SIZE\s*=\s*(\d+)"),
                    "AVATAR_SIZE_MAX": _grab(t, r"AVATAR_SIZE_MAX\s*=\s*(\d+)"),
                }
                if server.get("AVATAR_SIZE"):
                    server["bytes_at_default"] = server["AVATAR_SIZE"] ** 2 * 4

        agree = (game.get("AvatarSize") is not None
                 and game.get("AvatarSize") == server.get("AVATAR_SIZE"))
        return {
            "game": game,
            "server": server,
            "agree": agree,
            "verdict": (
                f"Both sides use {game.get('AvatarSize')}px "
                f"({game.get('AvatarBytes')} bytes). Contract holds."
                if agree else
                f"MISMATCH: game expects {game.get('AvatarSize')}px, server default is "
                f"{server.get('AVATAR_SIZE')}px. The game discards every avatar "
                "silently and every viewer renders as a plain monster."
            ),
            "note": "The game requests ?size= explicitly, so a mismatch only bites "
                    "if the server ignores or clamps that parameter.",
        }

    # --- what the server actually returns right now ---

    def _headers(self) -> dict:
        return {"x-game-token": self.token} if self.token else {}

    async def fetch(self, uid: str, size: int = 0, creator: str = "") -> tuple[bytes, dict]:
        if not self.url:
            raise RuntimeError("gifterboard.url is not set in config.json")
        size = int(size or self.size)
        params = {"u": creator or self.creator, "size": str(size), "uid": uid}
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(f"{self.url}/api/game/avatar", params=params,
                                 headers=self._headers())
            meta = {
                "url": str(r.url),
                "status": r.status_code,
                "content_type": r.headers.get("content-type", ""),
                "requested_size": size,
            }
            return r.content, meta

    async def probe(self, uid: str, size: int = 0, creator: str = "") -> dict:
        body, meta = await self.fetch(uid, size, creator)
        size = meta["requested_size"]
        expected = size * size * 4
        got = len(body)

        result = {
            **meta,
            "expected_bytes": expected,
            "received_bytes": got,
            "matches_contract": got == expected,
            "sha256": hashlib.sha256(body).hexdigest()[:16],
        }

        if got == expected:
            alpha = body[3::4]
            rgb_dark = all(b == 0 for b in body[:min(got, 4096)])
            result["alpha"] = {
                "min": min(alpha), "max": max(alpha),
                "fully_opaque": min(alpha) == 255,
                "fully_transparent": max(alpha) == 0,
            }
            result["all_black"] = rgb_dark
            result["verdict"] = (
                "Transparent everywhere - the game will upload an invisible texture."
                if max(alpha) == 0 else
                "All-black pixels - decode produced an empty frame."
                if rgb_dark else
                "Good. Correct length, visible pixels; the game will upload this."
            )
        else:
            first = body[:16]
            looks_like = (
                "JPEG" if first[:2] == b"\xff\xd8" else
                "PNG" if first[:8] == b"\x89PNG\r\n\x1a\n" else
                "WEBP" if first[:4] == b"RIFF" else
                "JSON/text" if first[:1] in (b"{", b"<", b"[") else
                "unknown"
            )
            result["first_bytes_hex"] = first.hex()
            result["looks_like"] = looks_like
            if looks_like in ("JSON/text", "unknown"):
                result["body_preview"] = body[:200].decode("utf-8", errors="replace")
            result["verdict"] = (
                f"BROKEN: got {got} bytes, game requires exactly {expected}. "
                "ViewerRegistry.BeginDownload DISCARDS this without logging, so the viewer "
                "renders as a plain monster, which looks identical to having no avatar. "
                + (f"The body looks like {looks_like}, so the server returned an undecoded "
                   "image rather than raw RGBA."
                   if looks_like not in ("unknown",) else
                   "Body is neither raw RGBA nor a recognisable image.")
            )
        return result

    async def to_png(self, uid: str, out_path: str, size: int = 0, creator: str = "") -> dict:
        body, meta = await self.fetch(uid, size, creator)
        size = meta["requested_size"]
        if len(body) != size * size * 4:
            return {**meta, "written": False,
                    "error": f"body is {len(body)} bytes, not {size * size * 4} - nothing to render"}
        p = self._output_path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rgba_to_png(body, size, size))
        return {**meta, "written": True, "path": str(p), "bytes": p.stat().st_size}

    # This is the one tool that writes, and a caller-supplied path used to be
    # taken as given - which made a read-only bridge able to overwrite any file
    # the server's user could. The PNG framing limited the damage, not the
    # reach. So: resolve first, then require the result to sit under output_dir,
    # and require the name to say .png so it cannot land as a script or source.
    def _output_path(self, out_path: str) -> Path:
        if self.output_dir is None:
            raise OutputDenied("avatar_png is disabled: no output_dir is configured")
        raw = (out_path or "").strip().replace("\\", "/")
        if not raw:
            raise OutputDenied("out_path is empty")
        if not raw.lower().endswith(".png"):
            raise OutputDenied("out_path must end in .png")

        candidate = Path(raw)
        target = (candidate if candidate.is_absolute() else self.output_dir / candidate).resolve()
        if self.output_dir not in target.parents:
            raise OutputDenied(
                f"out_path resolves outside the output directory: {target}. "
                f"Writes are confined to {self.output_dir}"
            )
        return target


# A PNG encoder is ~20 lines of zlib and struct, which is cheaper than making
# this server depend on Pillow just to eyeball a 64x64 face.
def rgba_to_png(rgba: bytes, width: int, height: int) -> bytes:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                   # filter type 0 (None)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))
