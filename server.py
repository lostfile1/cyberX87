import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import time
import urllib.parse
from pathlib import Path
import websockets
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

# --- Configuration ---
HOST = "0.0.0.0"
PORT = 8765
DATA_DIR = Path("./data")
WORLDS_DIR = DATA_DIR / "worlds"
AVATARS_DIR = DATA_DIR / "avatars"
SERVER_PLUGINS_DIR = DATA_DIR / "plugins"
PERMS_FILE = DATA_DIR / "permissions.json"
FRIENDS_FILE = DATA_DIR / "friends.json"
WORLD_META_FILE = DATA_DIR / "world_meta.json"
FEDERATION_FILE = DATA_DIR / "federation.json"
BACKUPS_DIR = DATA_DIR / "backups"

MAX_FILE_SIZE = 5 * 1024 * 1024      # 5 MB limit for .wrl worlds
MAX_AVATAR_SIZE = 512 * 1024         # 512 KB limit for .wrl avatars
MAX_AUTO_BACKUPS = 25
AUTH_TIMEOUT_SEC = 10.0
MAX_CONNECTIONS_PER_IP = 5
MAX_JSON_DEPTH = 12
MAX_USED_NONCES = 4096

RATE_LIMITS = {
    "move": (30, 1.0),
    "chat": (6, 4.0),
    "upload": (4, 30.0),
    "world_backup": (6, 15.0),
    "world_acl": (12, 10.0),
    "world_edit": (25, 10.0),
    "friend_action": (10, 10.0),
    "set_world_meta": (10, 10.0),
    "get_world_info": (15, 10.0),
    "rate_world": (10, 15.0),
    "join_world": (15, 10.0),
    "federation": (6, 15.0),
    "get_avatar": (15, 10.0),
    "set_avatar": (10, 10.0),
    "default": (25, 5.0),
}

RADIO_BLOCK_START = "# === WORLD_RADIO_START ==="
RADIO_BLOCK_END = "# === WORLD_RADIO_END ==="
SPAWN_BLOCK_START = "# === WORLD_SPAWN_START ==="
SPAWN_BLOCK_END = "# === WORLD_SPAWN_END ==="

COLOR_NAMES = {
    "cyan": (0.0, 1.0, 0.85),
    "magenta": (1.0, 0.15, 0.75),
    "pink": (1.0, 0.35, 0.80),
    "blue": (0.20, 0.60, 1.0),
    "purple": (0.70, 0.25, 1.0),
    "violet": (0.60, 0.20, 0.95),
    "green": (0.15, 0.95, 0.40),
    "emerald": (0.10, 0.85, 0.45),
    "amber": (1.0, 0.65, 0.0),
    "gold": (1.0, 0.78, 0.15),
    "orange": (1.0, 0.45, 0.10),
    "red": (1.0, 0.20, 0.25),
    "white": (0.95, 0.97, 1.0),
    "silver": (0.72, 0.78, 0.86),
    "dark": (0.12, 0.14, 0.22),
}


def _atomic_write_text(path: Path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _read_json_safe(path: Path, default: dict) -> dict:
    if not path.exists():
        _atomic_write_text(path, json.dumps(default, indent=2))
        return json.loads(json.dumps(default))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return json.loads(json.dumps(default))


def _check_json_depth(obj, depth: int = 0) -> bool:
    if depth > MAX_JSON_DEPTH:
        return False
    if isinstance(obj, dict):
        return all(_check_json_depth(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return all(_check_json_depth(v, depth + 1) for v in obj)
    return True


def _is_finite_num(val) -> bool:
    try:
        return math.isfinite(float(val))
    except (TypeError, ValueError):
        return False


def _validate_stream_url_syntax(url: str) -> tuple[bool, str, urllib.parse.SplitResult | None]:
    if not url or len(url) > 300:
        return False, "Radio stream URL must be between 1 and 300 characters.", None
    if any(ch in url for ch in ('"', "'", "\n", "\r", "\x00", " ")):
        return False, "Radio stream URL contains invalid characters.", None
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, "Malformed radio stream URL.", None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "Radio stream URL must use http:// or https:// with a valid host.", None
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "Localhost and .local stream URLs are blocked.", None
    return True, "", parsed


def _check_resolved_ips(addr_infos) -> tuple[bool, str]:
    for info in addr_infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False, "Private, loopback, or link-local stream addresses are blocked."
        except ValueError:
            return False, "Invalid resolved IP address for stream host."
    return True, ""


async def is_safe_public_stream_url_async(url: str) -> tuple[bool, str]:
    ok_syn, err_syn, parsed = _validate_stream_url_syntax(url)
    if not ok_syn or parsed is None:
        return False, err_syn
    host = parsed.hostname.strip().lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_infos = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Could not resolve stream host '{host}'."
    return _check_resolved_ips(addr_infos)


def validate_ws_peer_url(raw_url: str) -> tuple[bool, str, str]:
    url = (raw_url or "").strip()
    if not url:
        return False, "", "Federated server URL cannot be empty."
    if not url.startswith(("ws://", "wss://")):
        url = f"ws://{url}"
    if len(url) > 200 or any(ch in url for ch in ('"', "'", "\n", "\r", "\x00", " ")):
        return False, "", "Federated server URL contains invalid characters."
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, "", "Malformed federated server URL."
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        return False, "", "Federated server URL must specify a valid host."
    port = parsed.port or (443 if parsed.scheme == "wss" else 8765)
    return True, f"{parsed.scheme}://{parsed.hostname.lower()}:{port}", ""


def sanitize_vrml_script_nodes(wrl_text: str) -> str:
    cleaned = re.sub(
        r"\b(?:DEF\s+[A-Za-z0-9_-]+\s+)?Script\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}",
        "# [STRIPPED_SCRIPT_NODE]",
        wrl_text,
        flags=re.S,
    )
    return re.sub(r'javascript\s*:[^"]*', "", cleaned, flags=re.I)


def sanitize_plugin_arg(raw_arg: str) -> str:
    return re.sub(r'[\r\n\x00\{\}\[\]"]', "", raw_arg).strip()[:120]


def validate_hex_color(raw_color: str) -> str:
    c = str(raw_color or "").strip()
    return c.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", c) else "#00ffcc"


def parse_rgb_color(raw: str) -> tuple[float, float, float]:
    s = str(raw or "").strip().lower()
    if s in COLOR_NAMES:
        return COLOR_NAMES[s]
    hx = s.lstrip("#")
    if re.fullmatch(r"[0-9a-f]{6}", hx):
        return (
            round(int(hx[0:2], 16) / 255.0, 3),
            round(int(hx[2:4], 16) / 255.0, 3),
            round(int(hx[4:6], 16) / 255.0, 3),
        )
    return COLOR_NAMES["cyan"]


def compute_wrl_sha256(wrl_text: str) -> str:
    return hashlib.sha256(wrl_text.encode("utf-8", errors="replace")).hexdigest()


def build_placed_vrml_snippet(
    shape: str,
    x: float,
    y: float,
    z: float,
    scale: float,
    rgb: tuple[float, float, float],
    creator_nick: str,
    tag_id: str | None = None,
    yaw_deg: float = 0.0,
) -> str:
    r, g, b = rgb
    er, eg, eb = round(r * 0.45, 3), round(g * 0.45, 3), round(b * 0.45, 3)
    s = max(0.2, min(20.0, scale))
    tid = (tag_id or secrets.token_hex(3)).lower()
    safe_nick = re.sub(r"[^a-zA-Z0-9_-]", "", creator_nick)[:24] or "builder"
    yaw_rad = round(math.radians(yaw_deg % 360.0), 4)

    if shape == "pillar":
        cy = round(y + 2.0 * s, 3)
        geom = f"Cylinder {{ radius {0.45 * s:.3f} height {4.0 * s:.3f} }}"
    elif shape == "sphere":
        cy = round(y + 1.0 * s, 3)
        geom = f"Sphere {{ radius {1.0 * s:.3f} }}"
    elif shape == "cone":
        cy = round(y + 1.25 * s, 3)
        geom = f"Cone {{ bottomRadius {1.1 * s:.3f} height {2.5 * s:.3f} }}"
    elif shape == "platform":
        cy = round(y + 0.15 * s, 3)
        geom = f"Box {{ size {4.0 * s:.3f} {0.3 * s:.3f} {4.0 * s:.3f} }}"
    elif shape == "wall":
        cy = round(y + 1.5 * s, 3)
        geom = f"Box {{ size {4.5 * s:.3f} {3.0 * s:.3f} {0.4 * s:.3f} }}"
    elif shape == "arch":
        return (
            f"# [PLACED:{tid} by {safe_nick}]\n"
            f"DEF PLACED_{tid} Transform {{\n"
            f"  translation {x:.3f} {y:.3f} {z:.3f}\n"
            f"  rotation 0 1 0 {yaw_rad}\n"
            f"  scale 1.000 1.000 1.000\n"
            f"  children [\n"
            f"    Transform {{ translation {-1.6 * s:.3f} {1.8 * s:.3f} 0 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} emissiveColor {er} {eg} {eb} }} }} geometry Cylinder {{ radius {0.25 * s:.3f} height {3.6 * s:.3f} }} }} ] }}\n"
            f"    Transform {{ translation {1.6 * s:.3f} {1.8 * s:.3f} 0 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} emissiveColor {er} {eg} {eb} }} }} geometry Cylinder {{ radius {0.25 * s:.3f} height {3.6 * s:.3f} }} }} ] }}\n"
            f"    Transform {{ translation 0 {3.75 * s:.3f} 0 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} emissiveColor {r} {g} {b} }} }} geometry Box {{ size {3.8 * s:.3f} {0.35 * s:.3f} {0.8 * s:.3f} }} }} ] }}\n"
            f"  ]\n"
            f"}}"
        )
    else:
        cy = round(y + 1.0 * s, 3)
        geom = f"Box {{ size {2.0 * s:.3f} {2.0 * s:.3f} {2.0 * s:.3f} }}"

    return (
        f"# [PLACED:{tid} by {safe_nick}]\n"
        f"DEF PLACED_{tid} Transform {{\n"
        f"  translation {x:.3f} {cy:.3f} {z:.3f}\n"
        f"  rotation 0 1 0 {yaw_rad}\n"
        f"  scale 1.000 1.000 1.000\n"
        f"  children [\n"
        f"    Shape {{\n"
        f"      appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} emissiveColor {er} {eg} {eb} }} }}\n"
        f"      geometry {geom}\n"
        f"    }}\n"
        f"  ]\n"
        f"}}"
    )


def _find_placed_block_span(wrl_text: str, target_tag_id: str) -> tuple[int, int, str] | None:
    clean_id = re.sub(r"[^a-fA-F0-9]", "", target_tag_id).lower()
    if not clean_id:
        return None
    m = re.search(rf"# \[PLACED:({re.escape(clean_id)})\b", wrl_text, re.I)
    if not m:
        return None
    start_idx = m.start()
    brace_start = wrl_text.find("{", start_idx)
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(wrl_text)):
        if wrl_text[i] == "{":
            depth += 1
        elif wrl_text[i] == "}":
            depth -= 1
            if depth == 0:
                return start_idx, i + 1, m.group(1)
    return None


def remove_placed_snippet(wrl_text: str, target_tag_id: str) -> tuple[str, bool]:
    span = _find_placed_block_span(wrl_text, target_tag_id)
    if span is None:
        return wrl_text, False
    start_idx, end_idx, _ = span
    updated = re.sub(r"\n{3,}", "\n\n", wrl_text[:start_idx] + wrl_text[end_idx:]).strip() + "\n"
    return updated, True


def mutate_placed_snippet(
    wrl_text: str,
    target_tag_id: str,
    op: str,
    dx: float = 0.0,
    dy: float = 0.0,
    dz: float = 0.0,
    yaw_delta_deg: float = 0.0,
    rgb: tuple[float, float, float] | None = None,
    scale_mult: float = 1.0,
) -> tuple[str, bool, str]:
    span = _find_placed_block_span(wrl_text, target_tag_id)
    if span is None:
        return wrl_text, False, f"Placed object '{target_tag_id}' not found."
    start_idx, end_idx, canon_id = span
    block = wrl_text[start_idx:end_idx]

    if op == "move":
        m_tr = re.search(r"\btranslation\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", block)
        if not m_tr:
            return wrl_text, False, "Could not parse object translation."
        nx = max(-500.0, min(500.0, round(float(m_tr.group(1)) + dx, 3)))
        ny = max(-100.0, min(500.0, round(float(m_tr.group(2)) + dy, 3)))
        nz = max(-500.0, min(500.0, round(float(m_tr.group(3)) + dz, 3)))
        new_block = block[: m_tr.start()] + f"translation {nx:.3f} {ny:.3f} {nz:.3f}" + block[m_tr.end() :]
        return wrl_text[:start_idx] + new_block + wrl_text[end_idx:], True, f"Moved [{canon_id}] to ({nx:.2f}, {ny:.2f}, {nz:.2f})."

    if op == "rotate":
        m_rot = re.search(r"\brotation\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", block)
        if m_rot:
            new_deg = (math.degrees(float(m_rot.group(4))) + yaw_delta_deg) % 360.0
            new_rad = round(math.radians(new_deg), 4)
            new_block = block[: m_rot.start()] + f"rotation 0 1 0 {new_rad}" + block[m_rot.end() :]
        else:
            new_deg = yaw_delta_deg % 360.0
            new_rad = round(math.radians(new_deg), 4)
            new_block = re.sub(
                r"(\btranslation\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+)",
                rf"\1\n  rotation 0 1 0 {new_rad}",
                block,
                count=1,
            )
        return wrl_text[:start_idx] + new_block + wrl_text[end_idx:], True, f"Rotated [{canon_id}] to yaw {new_deg:.1f} deg."

    if op == "paint" and rgb is not None:
        r, g, b = rgb
        er, eg, eb = round(r * 0.45, 3), round(g * 0.45, 3), round(b * 0.45, 3)
        new_block = re.sub(
            r"\bdiffuseColor\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+",
            f"diffuseColor {r} {g} {b}",
            block,
        )
        new_block = re.sub(
            r"\bemissiveColor\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+",
            f"emissiveColor {er} {eg} {eb}",
            new_block,
        )
        return wrl_text[:start_idx] + new_block + wrl_text[end_idx:], True, f"Repainted [{canon_id}] to RGB ({r}, {g}, {b})."

    if op == "scale":
        mult = max(0.2, min(5.0, scale_mult))
        header_end = block.find("children")
        header = block[:header_end] if header_end != -1 else block
        body = block[header_end:] if header_end != -1 else ""
        m_sc = re.search(r"\bscale\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", header)
        if m_sc:
            sx = max(0.2, min(20.0, round(float(m_sc.group(1)) * mult, 3)))
            sy = max(0.2, min(20.0, round(float(m_sc.group(2)) * mult, 3)))
            sz = max(0.2, min(20.0, round(float(m_sc.group(3)) * mult, 3)))
            new_header = header[: m_sc.start()] + f"scale {sx:.3f} {sy:.3f} {sz:.3f}" + header[m_sc.end() :]
        else:
            sx = sy = sz = round(mult, 3)
            new_header = re.sub(
                r"(\btranslation\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+)",
                rf"\1\n  scale {sx:.3f} {sy:.3f} {sz:.3f}",
                header,
                count=1,
            )
        return wrl_text[:start_idx] + new_header + body + wrl_text[end_idx:], True, f"Scaled [{canon_id}] to ({sx:.2f}x)."

    if op == "clone":
        new_tid = secrets.token_hex(3)
        cloned_block = re.sub(rf"# \[PLACED:{re.escape(canon_id)}\b", f"# [PLACED:{new_tid}", block, count=1, flags=re.I)
        cloned_block = re.sub(rf"\bDEF PLACED_{re.escape(canon_id)}\b", f"DEF PLACED_{new_tid}", cloned_block, count=1, flags=re.I)
        m_tr = re.search(r"\btranslation\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", cloned_block)
        if m_tr:
            cx = max(-500.0, min(500.0, round(float(m_tr.group(1)) + (dx if (dx or dy or dz) else 1.5), 3)))
            cy = max(-100.0, min(500.0, round(float(m_tr.group(2)) + dy, 3)))
            cz = max(-500.0, min(500.0, round(float(m_tr.group(3)) + dz, 3)))
            cloned_block = cloned_block[: m_tr.start()] + f"translation {cx:.3f} {cy:.3f} {cz:.3f}" + cloned_block[m_tr.end() :]
        return wrl_text.rstrip() + f"\n\n{cloned_block}\n", True, f"Cloned [{canon_id}] -> new object [{new_tid}]."

    return wrl_text, False, f"Unknown object manipulation op '{op}'."


def apply_vrml_spawn_point(wrl_text: str, spawn_pos: list[float] | None, yaw_deg: float = 0.0) -> str:
    spawn_re = re.compile(re.escape(SPAWN_BLOCK_START) + r".*?" + re.escape(SPAWN_BLOCK_END), re.S)
    cleaned = spawn_re.sub("", wrl_text).rstrip()
    if not spawn_pos or len(spawn_pos) < 3:
        return cleaned + "\n"
    sx, sy, sz = float(spawn_pos[0]), float(spawn_pos[1]), float(spawn_pos[2])
    yaw_rad = round(math.radians(yaw_deg % 360.0), 4)
    return (
        f"{cleaned}\n\n{SPAWN_BLOCK_START}\n"
        f"DEF WORLD_SPAWN Viewpoint {{\n"
        f"  position {sx:.3f} {sy:.3f} {sz:.3f}\n"
        f"  orientation 0 1 0 {yaw_rad}\n"
        f'  description "World Spawn"\n'
        f"}}\n"
        f"{SPAWN_BLOCK_END}\n"
    )


def get_world_spawn(world_name: str, w_entry: dict) -> tuple[list[float], float]:
    sp = w_entry.get("spawn_pos")
    yaw = float(w_entry.get("spawn_yaw", 0.0) or 0.0)
    if isinstance(sp, list) and len(sp) >= 3:
        return [float(sp[0]), float(sp[1]), float(sp[2])], yaw
    if world_name.lower() == "hub":
        return [0.0, 2.25, 13.5], 0.0
    return [0.0, 0.4, 12.0], 0.0


def _build_default_hub_wrl() -> str:
    chunks = [
        "#VRML V2.0 utf8",
        'WorldInfo { title "NEO-GENESIS GRID // HUB" info ["Command-Jump Cyberpunk Nexus - /worlds, /world <name>, /peers, /hop <node>, /back"] }',
        'NavigationInfo { type ["WALK" "ANY"] headlight TRUE }',
        """Background {
  skyColor [0.02 0.06 0.11, 0.04 0.16 0.22, 0.06 0.24 0.28]
  skyAngle [1.1, 1.57]
  groundColor [0.02 0.04 0.07, 0.03 0.10 0.14]
  groundAngle [1.57]
}""",
        'Fog { color 0.03 0.14 0.18 fogType "EXPONENTIAL" visibilityRange 155 }',
        """# Cyberpunk Grid City Floor
Transform {
  translation 0 -0.05 0
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.04 0.07 0.11 emissiveColor 0.01 0.06 0.09 specularColor 0.0 1.0 0.9 }
      }
      geometry Box { size 160 0.1 160 }
    }
  ]
}""",
        """# Raised Stepped Command Nexus Dais (Lower Tier)
Transform {
  translation 0 0.35 -10
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.08 0.12 0.18 emissiveColor 0.02 0.14 0.18 }
      }
      geometry Box { size 22.0 0.7 22.0 }
    }
  ]
}""",
        """# Raised Command Nexus Dais (Upper Tier)
Transform {
  translation 0 1.05 -10
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.10 0.15 0.22 emissiveColor 0.03 0.18 0.24 }
      }
      geometry Box { size 17.5 0.7 17.5 }
    }
  ]
}""",
        """# Front Entrance Stairs (4 Steps Leading Onto Dais)
Transform { translation 0 0.18 2.6 children [ Shape { appearance Appearance { material Material { diffuseColor 0.09 0.13 0.19 emissiveColor 0.02 0.18 0.22 } } geometry Box { size 4.6 0.36 1.2 } } ] }
Transform { translation 0 0.52 1.6 children [ Shape { appearance Appearance { material Material { diffuseColor 0.10 0.14 0.20 emissiveColor 0.02 0.22 0.28 } } geometry Box { size 4.6 0.36 1.2 } } ] }
Transform { translation 0 0.88 0.6 children [ Shape { appearance Appearance { material Material { diffuseColor 0.11 0.16 0.22 emissiveColor 0.03 0.26 0.32 } } geometry Box { size 4.6 0.36 1.2 } } ] }
Transform { translation 0 1.22 -0.4 children [ Shape { appearance Appearance { material Material { diffuseColor 0.12 0.18 0.24 emissiveColor 0.04 0.32 0.38 } } geometry Box { size 4.6 0.36 1.2 } } ] }""",
        """# Glowing Cyan Walkways Radiating Across the Dais
Transform { translation 0 1.43 -4.5 children [ Shape { appearance Appearance { material Material { diffuseColor 0.0 0.95 0.95 emissiveColor 0.0 0.85 0.90 } } geometry Box { size 2.2 0.06 9.5 } } ] }
Transform { translation -4.8 1.43 -10.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.0 0.95 0.95 emissiveColor 0.0 0.85 0.90 } } geometry Box { size 7.5 0.06 1.8 } } ] }
Transform { translation 4.8 1.43 -10.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.0 0.95 0.95 emissiveColor 0.0 0.85 0.90 } } geometry Box { size 7.5 0.06 1.8 } } ] }""",
        """# Central Pedestal & Glowing Neon-Cyan Obelisk Spire
Transform { translation 0 2.0 -10.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.08 0.14 0.20 emissiveColor 0.0 0.45 0.55 } } geometry Box { size 3.2 1.2 3.2 } } ] }
Transform { translation 0 6.8 -10.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.0 0.95 0.92 emissiveColor 0.0 0.95 0.92 } } geometry Box { size 1.65 8.6 1.65 } } ] }
Transform { translation 0 12.4 -10.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.1 1.0 1.0 emissiveColor 0.1 1.0 1.0 } } geometry Cone { bottomRadius 1.18 height 2.6 } } ] }""",
    ]

    for ring_y, rad_ring, tube_h in ((14.6, 3.6, 0.18), (15.1, 2.5, 0.16)):
        segs = 20
        for s in range(segs):
            a0 = (2.0 * math.pi * s) / segs
            a1 = (2.0 * math.pi * (s + 1)) / segs
            mx = math.cos((a0 + a1) * 0.5) * rad_ring
            mz = -10.0 + math.sin((a0 + a1) * 0.5) * rad_ring
            seg_len = round(2.0 * rad_ring * math.sin(math.pi / segs) + 0.08, 3)
            yaw_seg = round(-((a0 + a1) * 0.5) + math.pi * 0.5, 4)
            chunks.append(
                f"Transform {{ translation {mx:.3f} {ring_y:.2f} {mz:.3f} rotation 0 1 0 {yaw_seg} "
                f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 1.0 0.15 0.82 emissiveColor 1.0 0.18 0.85 }} }} "
                f"geometry Box {{ size {seg_len} {tube_h} 0.22 }} }} ] }}"
            )

    for orb_i in range(24):
        ang = (2.0 * math.pi * orb_i) / 12.0
        oy = 3.4 + orb_i * 0.44
        ox = round(math.cos(ang) * 2.15, 3)
        oz = round(-10.0 + math.sin(ang) * 2.15, 3)
        chunks.append(
            f"Transform {{ translation {ox} {oy:.2f} {oz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.0 0.95 1.0 emissiveColor 0.0 0.95 1.0 }} }} "
            f"geometry Sphere {{ radius 0.14 }} }} ] }}"
        )

    pylons = [
        (-4.2, 3.6, -6.5, 0.52, 4.4, "0.0 0.95 0.92"),
        (4.2, 3.6, -6.5, 0.52, 4.4, "1.0 0.12 0.78"),
        (-4.2, 3.6, -13.5, 0.52, 4.4, "1.0 0.12 0.78"),
        (4.2, 3.6, -13.5, 0.52, 4.4, "0.0 0.95 0.92"),
    ]
    for px, py, pz, prad, ph, ecol in pylons:
        chunks.append(
            f"Transform {{ translation {px} {py} {pz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.08 0.10 0.16 emissiveColor {ecol} }} }} "
            f"geometry Cylinder {{ radius {prad} height {ph} }} }} ] }}"
        )

    beacons = [(-8.6, 3.4, -2.2), (8.6, 3.4, -2.2), (-8.6, 3.4, -17.8), (8.6, 3.4, -17.8)]
    for bx, by, bz in beacons:
        chunks.append(
            f"Transform {{ translation {bx} {by} {bz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.22 0.15 0.05 emissiveColor 1.0 0.68 0.10 }} }} "
            f"geometry Box {{ size 1.15 4.0 1.15 }} }} ] }}\n"
            f"Transform {{ translation {bx} {by + 2.55} {bz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 1.0 0.78 0.20 emissiveColor 1.0 0.78 0.20 }} }} "
            f"geometry Cone {{ bottomRadius 0.82 height 1.1 }} }} ] }}"
        )

    skyline_towers = [
        (-28.0, 14.0, -34.0, 7.0, 28.0, 7.0, "0.0 0.75 0.85"),
        (-16.5, 11.0, -38.0, 6.0, 22.0, 6.0, "0.85 0.15 0.70"),
        (16.5, 12.5, -38.0, 6.5, 25.0, 6.5, "0.0 0.80 0.85"),
        (29.0, 16.0, -32.0, 7.5, 32.0, 7.5, "0.90 0.18 0.72"),
        (-34.0, 12.0, -12.0, 6.5, 24.0, 6.5, "0.0 0.70 0.82"),
        (34.0, 13.0, -10.0, 6.5, 26.0, 6.5, "0.85 0.14 0.68"),
        (-32.0, 10.0, 12.0, 6.0, 20.0, 6.0, "0.0 0.65 0.78"),
        (32.0, 11.0, 14.0, 6.0, 22.0, 6.0, "0.80 0.16 0.65"),
    ]
    for tx, ty, tz, sx, sy, sz, trim_col in skyline_towers:
        chunks.append(
            f"Transform {{ translation {tx} {ty} {tz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.05 0.08 0.13 emissiveColor 0.02 0.06 0.10 }} }} "
            f"geometry Box {{ size {sx} {sy} {sz} }} }} ] }}\n"
            f"Transform {{ translation {tx} {sy - 2.5} {tz} "
            f"children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {trim_col} emissiveColor {trim_col} }} }} "
            f"geometry Box {{ size {sx + 0.4} 0.55 {sz + 0.4} }} }} ] }}"
        )

    chunks.append("""
Transform { translation -22.2 13.5 -36.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.06 0.10 0.16 emissiveColor 0.0 0.55 0.65 } } geometry Box { size 11.5 1.1 2.2 } } ] }
Transform { translation 22.8 14.5 -35.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.06 0.10 0.16 emissiveColor 0.0 0.55 0.65 } } geometry Box { size 12.5 1.1 2.2 } } ] }
Transform { translation -24.2 11.0 -30.2 children [ Shape { appearance Appearance { material Material { diffuseColor 0.95 0.15 0.75 emissiveColor 0.95 0.15 0.75 } } geometry Box { size 4.6 2.2 0.25 } } ] }
Transform { translation 25.0 13.5 -28.0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.95 0.15 0.75 emissiveColor 0.95 0.15 0.75 } } geometry Box { size 4.8 2.4 0.25 } } ] }
""")
    return "\n".join(chunks) + "\n"


DEFAULT_HUB_WRL = _build_default_hub_wrl()


def read_wrl_safe(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def has_vrml_geometry(content: str) -> bool:
    return any(tok in content for tok in (
        "Shape", "Box", "Sphere", "Cylinder", "Cone",
        "IndexedFaceSet", "IndexedLineSet", "ElevationGrid", "Extrusion",
        "Separator", "Cube",
    ))


def _strip_vrml_anchor_nodes(text: str) -> str:
    """Removes Anchor nodes using brace depth tracking so nested blocks never leave stray braces."""
    out = []
    i = 0
    n = len(text)
    in_quote = False
    while i < n:
        ch = text[i]
        if ch == "\\" and in_quote and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if not in_quote and (text[i : i + 6] == "Anchor" or text[i : i + 3] == "DEF"):
            m = re.match(r"(?:DEF\s+[A-Za-z0-9_-]+\s+)?Anchor\s*\{", text[i:])
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in ("_", "-"))):
                brace_pos = i + m.end() - 1
                depth = 0
                j = brace_pos
                q_inner = False
                while j < n:
                    c2 = text[j]
                    if c2 == "\\" and q_inner and j + 1 < n:
                        j += 2
                        continue
                    if c2 == '"':
                        q_inner = not q_inner
                    elif not q_inner:
                        if c2 == "{":
                            depth += 1
                        elif c2 == "}":
                            depth -= 1
                            if depth == 0:
                                i = j + 1
                                break
                    j += 1
                else:
                    i = n
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _mask_vrml_non_code(text: str) -> str:
    """Strips # comments outside quotes and blanks out string literal contents so structural brace/bracket checks match client precheck."""
    out = []
    in_quote = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and in_quote and i + 1 < n:
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
            out.append('"')
            i += 1
            continue
        if ch == "#" and not in_quote:
            while i < n and text[i] != "\n":
                i += 1
            continue
        if not in_quote:
            out.append(ch)
        i += 1
    return "".join(out)


def validate_strict_vrml_document(content: str, kind: str = "world") -> tuple[bool, str]:
    """Enforces that uploaded worlds and avatars are genuine VRML 1.0 or VRML 2.0/97 (.wrl) files."""
    if not isinstance(content, str) or not content.strip():
        return False, "Upload rejected: empty VRML file."
    if "\x00" in content:
        return False, "Upload rejected: binary files are not allowed; only UTF-8/ASCII VRML (.wrl) files are accepted."

    clean_bom = content.lstrip("\ufeff")
    first_line = next((ln.strip() for ln in clean_bom.splitlines() if ln.strip()), "")
    if not re.match(r"^#VRML\s+V(1\.0|2\.0)\s+(ascii|utf8)\b", first_line):
        return False, "Upload rejected: file must start with '#VRML V2.0 utf8' or '#VRML V1.0 ascii'."

    stripped_body = _mask_vrml_non_code(clean_bom).strip()
    if stripped_body.startswith(("{", "[", "<")):
        return False, "Upload rejected: JSON, XML/X3D, and HTML files are not accepted; upload a VRML (.wrl) file."
    if re.search(r"<\s*(?:!DOCTYPE|html|xml|X3D|COLLADA)\b", stripped_body, re.I):
        return False, "Upload rejected: XML/HTML/X3D markup detected; only VRML (.wrl) files are accepted."

    if stripped_body.count("{") == 0 or stripped_body.count("{") != stripped_body.count("}"):
        return False, "Upload rejected: malformed VRML syntax (unbalanced '{' and '}' braces)."
    if stripped_body.count("[") != stripped_body.count("]"):
        return False, "Upload rejected: malformed VRML syntax (unbalanced '[' and ']' brackets)."

    vrml_nodes = (
        r"\b(Shape|Box|Sphere|Cylinder|Cone|IndexedFaceSet|IndexedLineSet|"
        r"ElevationGrid|Extrusion|Transform|Group|Separator|Cube|Coordinate3|Material|PROTO|EXTERNPROTO)\b"
    )
    if not re.search(vrml_nodes, stripped_body):
        return False, "Upload rejected: no valid VRML 1.0 / VRML97 nodes found."

    if kind == "avatar" and not has_vrml_geometry(stripped_body):
        return False, "Upload rejected: avatar .wrl must contain 3D VRML geometry."

    return True, ""


def load_world_meta() -> dict:
    return _read_json_safe(WORLD_META_FILE, {})


def save_world_meta(data: dict):
    _atomic_write_text(WORLD_META_FILE, json.dumps(data, indent=2))


def can_edit_world(world_name: str, w_entry: dict, pubkey_hex: str, role: str) -> bool:
    if role == "admin":
        return True
    if world_name.lower() == "hub" or role != "builder":
        return False
    owner = w_entry.get("owner", "")
    return bool(owner) and (owner == pubkey_hex or pubkey_hex in w_entry.get("co_builders", []))


def can_visit_world(world_name: str, w_entry: dict, pubkey_hex: str, role: str) -> bool:
    if role == "admin" or world_name.lower() == "hub" or not w_entry.get("private", False):
        return True
    owner = w_entry.get("owner", "")
    return bool(owner) and (
        owner == pubkey_hex
        or pubkey_hex in w_entry.get("co_builders", [])
        or pubkey_hex in w_entry.get("whitelist", [])
    )


def load_federation() -> dict:
    data = _read_json_safe(FEDERATION_FILE, {
        "node_name": "cyberX87 Node",
        "node_desc": "Self-Hosted VRML97 Metaverse Node",
        "peers": {},
    })
    data.setdefault("node_name", "cyberX87 Node")
    data.setdefault("node_desc", "Self-Hosted VRML97 Metaverse Node")
    data.setdefault("peers", {})
    return data


def save_federation(data: dict):
    _atomic_write_text(FEDERATION_FILE, json.dumps(data, indent=2))


def create_world_snapshot(world_name: str, label: str = "") -> str | None:
    wrl_path = WORLDS_DIR / f"{world_name}.wrl"
    if not wrl_path.exists():
        return None
    w_backup_dir = BACKUPS_DIR / world_name
    w_backup_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    clean_lbl = sanitize_name(label) if label.strip() else ""
    base_stem = f"{world_name}_{ts}{f'_{clean_lbl}' if clean_lbl else ''}"

    snap_wrl = w_backup_dir / f"{base_stem}.wrl"
    counter = 1
    while snap_wrl.exists():
        snap_wrl = w_backup_dir / f"{base_stem}_{counter}.wrl"
        counter += 1

    _atomic_write_text(snap_wrl, read_wrl_safe(wrl_path))
    _atomic_write_text(snap_wrl.with_suffix(".meta.json"), json.dumps(load_world_meta().get(world_name, {}), indent=2))

    if clean_lbl == "auto":
        for old_wrl in sorted(w_backup_dir.glob("*_auto*.wrl"), reverse=True)[MAX_AUTO_BACKUPS:]:
            try:
                old_wrl.unlink(missing_ok=True)
                old_wrl.with_suffix(".meta.json").unlink(missing_ok=True)
            except Exception:
                pass
    return snap_wrl.name


def list_world_snapshots(world_name: str) -> list[dict]:
    w_backup_dir = BACKUPS_DIR / world_name
    if not w_backup_dir.exists():
        return []
    return [
        {"file": fp.name, "bytes": fp.stat().st_size, "mtime": int(fp.stat().st_mtime)}
        for fp in sorted(w_backup_dir.glob("*.wrl"), reverse=True)
    ]


def apply_vrml_world_info(
    wrl_text: str,
    title: str,
    desc: str = "",
    tags: list[str] | None = None,
    radio_url: str = "",
    radio_name: str = "",
) -> str:
    safe_title = re.sub(r'["\r\n\x00]', "'", (title or "Untitled")).strip()[:64]
    safe_desc = re.sub(r'["\r\n\x00]', "'", (desc or "")).strip()[:240]
    info_items = []
    if safe_desc:
        info_items.append(f'"{safe_desc}"')
    if tags:
        clean_tags = ", ".join(re.sub(r'["\r\n\x00]', "'", t).strip() for t in tags if t.strip())[:120]
        if clean_tags:
            info_items.append(f'"tags: {clean_tags}"')
    if radio_url:
        label = re.sub(r'["\r\n\x00]', "'", (radio_name or radio_url)).strip()[:80]
        info_items.append(f'"radio: {label}"')

    info_field = f' info [{" ".join(info_items)}]' if info_items else ""
    info_block = f'WorldInfo {{ title "{safe_title}"{info_field} }}'

    if re.search(r"\bWorldInfo\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", wrl_text, re.S):
        out = re.sub(r"\bWorldInfo\s*\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", info_block, wrl_text, count=1, flags=re.S)
    else:
        lines = wrl_text.splitlines()
        if lines and lines[0].startswith("#VRML"):
            out = lines[0] + "\n" + info_block + "\n" + "\n".join(lines[1:])
        else:
            out = "#VRML V2.0 utf8\n" + info_block + "\n" + wrl_text

    out = re.sub(re.escape(RADIO_BLOCK_START) + r".*?" + re.escape(RADIO_BLOCK_END), "", out, flags=re.S).rstrip()
    if radio_url:
        safe_url = re.sub(r'["\r\n\x00]', "", radio_url).strip()
        safe_rname = re.sub(r'["\r\n\x00]', "'", (radio_name or "World Radio")).strip()[:64]
        out += (
            f"\n\n{RADIO_BLOCK_START}\n"
            f"Sound {{\n"
            f"  minFront 1000 maxFront 1000 minBack 1000 maxBack 1000 spatialize FALSE\n"
            f'  source AudioClip {{ url ["{safe_url}"] description "{safe_rname}" loop TRUE }}\n'
            f"}}\n"
            f"{RADIO_BLOCK_END}\n"
        )
    else:
        out += "\n"
    return out


def ensure_valid_vrml_world(world_name: str, content: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    if "#VRML" not in text[:64]:
        text = "#VRML V2.0 utf8\n" + text

    text = sanitize_vrml_script_nodes(text)
    text = _strip_vrml_anchor_nodes(text)
    text = text.replace(",", " ")
    text = re.sub(
        r"\b(diffuseColor|emissiveColor|specularColor|skyColor|translation|rotation|scale|size|radius|height|bottomRadius)\s*:",
        r"\1 ",
        text,
    )
    text = re.sub(r"\bCube\b", "Box", text)
    text = re.sub(r"\bBox\s*\{\s*([-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+)\s*\}", r"Box { size \1 }", text)
    text = re.sub(r"\bSphere\s*\{\s*([-+0-9.eE]+)\s*\}", r"Sphere { radius \1 }", text)
    text = re.sub(r"\bCylinder\s*\{\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\}", r"Cylinder { radius \1 height \2 }", text)
    text = re.sub(r"\bCone\s*\{\s*([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*\}", r"Cone { bottomRadius \1 height \2 }", text)

    open_brackets = text.count("[") - text.count("]")
    if open_brackets > 0:
        text += "\n" + ("]\n" * open_brackets)
    open_braces = text.count("{") - text.count("}")
    if open_braces > 0:
        text += "\n" + ("}\n" * open_braces)
    return text + "\n"


def ensure_hub_world():
    hub_path = WORLDS_DIR / "hub.wrl"
    if not hub_path.exists():
        _atomic_write_text(hub_path, DEFAULT_HUB_WRL)
        print("[*] Initialized Neo-Genesis cyberpunk data/worlds/hub.wrl")
        return
    cur = read_wrl_safe(hub_path)
    if not has_vrml_geometry(cur) or ("AUTO_PORTALS_START" in cur) or ("FED_PORTALS_START" in cur) or ("Anchor {" in cur):
        _atomic_write_text(hub_path, DEFAULT_HUB_WRL)
        print("[*] Updated data/worlds/hub.wrl to Neo-Genesis cyberpunk nexus design")


async def probe_federated_node(peer_url: str, timeout: float = 2.5) -> dict:
    try:
        async with websockets.connect(peer_url, open_timeout=timeout, close_timeout=1.0) as ws:
            await asyncio.wait_for(ws.recv(), timeout=timeout)
            await ws.send(json.dumps({"type": "node_info"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            info = json.loads(raw)
            if info.get("type") == "node_info_ok":
                return {
                    "online": True,
                    "node_name": str(info.get("node_name", ""))[:48],
                    "node_desc": str(info.get("node_desc", ""))[:120],
                    "online_count": int(info.get("online_count", 0)),
                    "worlds": info.get("worlds", [])[:12],
                }
    except Exception:
        pass
    return {"online": False}


def load_server_plugins() -> dict:
    SERVER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    sample = SERVER_PLUGINS_DIR / "world_tools.x87"
    if not sample.exists():
        _atomic_write_text(
            sample,
            "# Server-side human-readable plugins (data/plugins/)\n"
            "COMMAND: /pillar\n"
            "DESC: Spawn a glowing cyan pillar in front of you\n"
            "ROLE: builder\n"
            "ACTION: spawn_vrml\n"
            "VALUE: Transform { translation {x} 2.0 {z_front} children [ Shape { appearance Appearance { material Material { diffuseColor 0.0 1.0 0.85 emissiveColor 0.0 0.7 0.6 } } geometry Cylinder { radius 0.5 height 4.0 } } ] }\n"
            "---\n"
            "COMMAND: /announce\n"
            "DESC: Broadcast a server-wide banner message\n"
            "ROLE: admin\n"
            "ACTION: broadcast\n"
            "VALUE: [ANNOUNCEMENT] {args}\n",
        )

    cmds = {}
    for fp in sorted(SERVER_PLUGINS_DIR.glob("*.x87")):
        raw = fp.read_text(encoding="utf-8", errors="replace")
        for block in raw.split("---"):
            entry = {}
            for line in block.splitlines():
                s = line.strip()
                if not s or s.startswith("#") or ":" not in s:
                    continue
                k, v = s.split(":", 1)
                entry[k.strip().lower()] = v.strip()
            cname = entry.get("command", "").lower()
            if cname:
                cmds[cname if cname.startswith("/") else "/" + cname] = entry
    return cmds


def init_storage():
    for d in (DATA_DIR, WORLDS_DIR, AVATARS_DIR, BACKUPS_DIR, SERVER_PLUGINS_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    load_server_plugins()
    load_friends()
    load_world_meta()
    load_federation()
    ensure_hub_world()
    load_perms()


def load_perms():
    perms = _read_json_safe(PERMS_FILE, {
        "default_role": "builder",
        "admins": [],
        "builders": [],
        "banned": [],
        "max_total_worlds": 0,
        "max_worlds_per_user": 0,
        "user_avatars": {},
        "avatar_owners": {},
        "nick_registry": {},
    })
    perms.setdefault("default_role", "builder")
    perms.setdefault("admins", [])
    perms.setdefault("builders", [])
    perms.setdefault("banned", [])
    perms.setdefault("max_total_worlds", 0)
    perms.setdefault("max_worlds_per_user", 0)
    perms.setdefault("user_avatars", {})
    perms.setdefault("avatar_owners", {})
    perms.setdefault("nick_registry", {})
    return perms


def save_perms(data):
    _atomic_write_text(PERMS_FILE, json.dumps(data, indent=2))


def load_friends() -> dict:
    data = _read_json_safe(FRIENDS_FILE, {"friends": {}, "pending": {}})
    data.setdefault("friends", {})
    data.setdefault("pending", {})
    return data


def save_friends(data: dict):
    _atomic_write_text(FRIENDS_FILE, json.dumps(data, indent=2))


def allocate_unique_nick(desired_nick: str, pubkey_hex: str, exclude_ws=None) -> str:
    base = sanitize_name(desired_nick)[:24] or f"User_{pubkey_hex[:6]}"
    active_nicks = {st["nick"].lower() for ws, st in CLIENTS.items() if ws != exclude_ws}
    if base.lower() not in active_nicks:
        return base
    tagged = f"{base[:19]}_{pubkey_hex[:4]}"
    if tagged.lower() not in active_nicks:
        return tagged
    return f"{base[:15]}_{pubkey_hex[:8]}"


def remember_user_nick(pubkey_hex: str, nick: str):
    perms = load_perms()
    reg = perms.setdefault("nick_registry", {})
    if reg.get(pubkey_hex) != nick:
        reg[pubkey_hex] = nick
        save_perms(perms)


def resolve_user_key(identifier: str, fdata: dict, owner_key: str) -> tuple[str | None, str]:
    ident = identifier.strip()
    if not ident:
        return None, ""
    ident_low = ident.lower()

    if re.fullmatch(r"[0-9a-f]{64}", ident_low):
        for st in CLIENTS.values():
            if st["pubkey"] == ident_low:
                return ident_low, st["nick"]
        reg = load_perms().get("nick_registry", {})
        return ident_low, reg.get(ident_low, ident_low[:8])

    online_matches = [
        st for st in CLIENTS.values()
        if st["short_id"].lower() == ident_low or st["nick"].lower() == ident_low
    ]
    if len(online_matches) == 1:
        return online_matches[0]["pubkey"], online_matches[0]["nick"]
    if len(online_matches) > 1:
        return None, ""

    reg = load_perms().get("nick_registry", {})
    known_keys = (
        set(fdata.get("friends", {}).get(owner_key, []))
        | set(fdata.get("pending", {}).get(owner_key, []))
        | set(reg.keys())
    )
    exact_known = []
    prefix_known = []
    for k in known_keys:
        saved_nick = reg.get(k, "")
        if k == ident_low or (saved_nick and saved_nick.lower() == ident_low):
            exact_known.append((k, saved_nick or k[:8]))
        elif len(ident_low) >= 6 and k.lower().startswith(ident_low):
            prefix_known.append((k, saved_nick or k[:8]))

    if len(exact_known) == 1:
        return exact_known[0]
    if not exact_known and len(prefix_known) == 1:
        return prefix_known[0]
    return None, ""


def get_role(pubkey_hex: str) -> str:
    perms = load_perms()
    if pubkey_hex in perms.get("banned", []):
        return "banned"
    if not perms.get("admins"):
        perms["admins"].append(pubkey_hex)
        save_perms(perms)
        return "admin"
    if pubkey_hex in perms.get("admins", []):
        return "admin"
    if pubkey_hex in perms.get("builders", []):
        return "builder"
    default_r = str(perms.get("default_role", "builder")).strip().lower()
    return default_r if default_r in ("guest", "builder") else "builder"


def sanitize_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in ("_", "-")).strip()
    return cleaned[:32] or "unnamed"


CLIENTS = {}
IP_CONNECTIONS: dict[str, int] = {}
USED_NONCES: dict[str, float] = {}
UPLOAD_LOCK = asyncio.Lock()


def _prune_used_nonces(now: float):
    for n in [k for k, exp in USED_NONCES.items() if exp <= now]:
        USED_NONCES.pop(n, None)
    if len(USED_NONCES) > MAX_USED_NONCES:
        for old_n in list(USED_NONCES.keys())[: len(USED_NONCES) - MAX_USED_NONCES]:
            USED_NONCES.pop(old_n, None)


def _check_rate_limit(buckets: dict, msg_type: str, now: float) -> bool:
    bucket_key = msg_type if msg_type in RATE_LIMITS else "default"
    limit, window = RATE_LIMITS[bucket_key]
    history = buckets.setdefault(bucket_key, [])
    cutoff = now - window
    while history and history[0] < cutoff:
        history.pop(0)
    if len(history) >= limit:
        return False
    history.append(now)
    return True


async def broadcast_world(world: str, payload: dict, exclude_ws=None):
    msg = json.dumps(payload)
    for ws, state in list(CLIENTS.items()):
        if state.get("world") == world and ws != exclude_ws:
            try:
                await ws.send(msg)
            except Exception:
                pass


async def send_world_transition(websocket, client_state: dict, target: str, w_meta: dict, client_hash: str = ""):
    wrl_path = WORLDS_DIR / f"{target}.wrl"
    sp_pos, sp_yaw = get_world_spawn(target, w_meta)
    client_state["world"] = target
    client_state["pos"] = list(sp_pos)
    client_state["rot"] = [0.0, 1.0, 0.0, round(math.radians(-sp_yaw), 3)]

    wrl_text = read_wrl_safe(wrl_path)
    wrl_hash = compute_wrl_sha256(wrl_text)
    w_desc = w_meta.get("description", "")

    if client_hash and client_hash == wrl_hash:
        msg = {
            "type": "world_cached",
            "world": target,
            "wrl_hash": wrl_hash,
            "description": w_desc,
            "radio_url": w_meta.get("radio_url", ""),
            "radio_name": w_meta.get("radio_name", ""),
        }
    else:
        msg = {
            "type": "world_data",
            "world": target,
            "wrl_hash": wrl_hash,
            "description": w_desc,
            "radio_url": w_meta.get("radio_url", ""),
            "radio_name": w_meta.get("radio_name", ""),
            "wrl": wrl_text,
        }
    if w_meta.get("spawn_pos"):
        msg["spawn_pos"] = sp_pos
        msg["spawn_yaw"] = sp_yaw
    await websocket.send(json.dumps(msg))

    peers = [
        {k: v for k, v in st.items() if k != "pubkey"}
        for ws_peer, st in CLIENTS.items()
        if ws_peer != websocket and st["world"] == target
    ]
    await websocket.send(json.dumps({"type": "peers", "peers": peers}))
    await broadcast_world(target, {
        "type": "peer_join",
        "peer": {k: v for k, v in client_state.items() if k != "pubkey"},
    }, exclude_ws=websocket)


def summarize_world_ratings(w_entry: dict) -> tuple[float, int, dict[int, int]]:
    ratings = w_entry.get("ratings")
    dist = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
    if not isinstance(ratings, dict) or not ratings:
        return 0.0, 0, dist
    total = 0
    count = 0
    for item in ratings.values():
        if not isinstance(item, dict):
            continue
        try:
            st = int(item.get("stars", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= st <= 5:
            dist[st] += 1
            total += st
            count += 1
    avg = round(total / count, 2) if count > 0 else 0.0
    return avg, count, dist


def build_world_info_payload(target: str, meta: dict, perms: dict) -> dict:
    wrl_path = WORLDS_DIR / f"{target}.wrl"
    w_entry = meta.get(target, {})
    owner_key = str(w_entry.get("owner", "")).strip()
    nick_reg = perms.get("nick_registry", {})
    online_by_key = {st["pubkey"]: st for st in CLIENTS.values()}

    def _resolve_member(pk: str, fallback_nick: str = "") -> dict:
        on_st = online_by_key.get(pk)
        return {
            "pubkey": pk,
            "short_id": pk[:8] if pk else "",
            "nick": (on_st["nick"] if on_st else "") or nick_reg.get(pk) or fallback_nick or (pk[:8] if pk else "system"),
            "online": bool(on_st),
        }

    sp_pos, sp_yaw = get_world_spawn(target, w_entry)
    avg_rat, cnt_rat, _ = summarize_world_ratings(w_entry)
    return {
        "type": "world_info",
        "avg_rating": avg_rat,
        "rating_count": cnt_rat,
        "world": target,
        "title": w_entry.get("title") or ("Neo-Genesis Grid" if target.lower() == "hub" else target),
        "description": w_entry.get("description", ""),
        "tags": w_entry.get("tags", []),
        "private": bool(w_entry.get("private", False)),
        "is_system": target.lower() == "hub" and not owner_key,
        "owner": _resolve_member(owner_key, w_entry.get("owner_nick", "")) if owner_key else None,
        "co_builders": [_resolve_member(k) for k in w_entry.get("co_builders", [])],
        "whitelist": [_resolve_member(k) for k in w_entry.get("whitelist", [])],
        "radio_url": w_entry.get("radio_url", ""),
        "radio_name": w_entry.get("radio_name", ""),
        "spawn_pos": sp_pos,
        "spawn_yaw": sp_yaw,
        "spawn_custom": bool(w_entry.get("spawn_pos")),
        "created_at": int(w_entry.get("created_at", 0) or 0),
        "updated_at": int(w_entry.get("updated_at", 0) or 0),
        "bytes": wrl_path.stat().st_size if wrl_path.exists() else 0,
        "sha256": compute_wrl_sha256(read_wrl_safe(wrl_path)) if wrl_path.exists() else "",
        "players": [
            {"nick": st["nick"], "short_id": st["short_id"], "role": st["role"]}
            for st in CLIENTS.values()
            if st.get("world") == target
        ],
        "snapshots_count": len(list_world_snapshots(target)),
    }


def _install_transferred_avatar(raw_name: str, raw_wrl: str, pubkey_hex: str, role: str) -> tuple[str, str]:
    av_name = sanitize_name(raw_name)
    if not av_name or av_name.lower() == "default" or not raw_wrl:
        return "default", ""
    if len(raw_wrl.encode("utf-8", errors="replace")) > MAX_AVATAR_SIZE:
        return "default", ""
    ok_vrml, _ = validate_strict_vrml_document(raw_wrl, kind="avatar")
    if not ok_vrml:
        return "default", ""
    clean_wrl = sanitize_vrml_script_nodes(raw_wrl)
    if not has_vrml_geometry(clean_wrl):
        return "default", ""

    perms = load_perms()
    av_owners = perms.setdefault("avatar_owners", {})
    existing_owner = av_owners.get(av_name, "")
    if existing_owner and existing_owner != pubkey_hex and role != "admin":
        av_name = f"{av_name[:20]}_{pubkey_hex[:6]}"
        existing_owner = av_owners.get(av_name, "")
        if existing_owner and existing_owner != pubkey_hex and role != "admin":
            return "default", ""

    if not existing_owner:
        av_owners[av_name] = pubkey_hex
    _atomic_write_text(AVATARS_DIR / f"{av_name}.wrl", clean_wrl)
    perms["user_avatars"][pubkey_hex] = av_name
    save_perms(perms)
    return av_name, clean_wrl


def _link_mutual_friends(fdata: dict, key_a: str, key_b: str):
    fdata["pending"].setdefault(key_a, [])
    fdata["pending"].setdefault(key_b, [])
    fdata["friends"].setdefault(key_a, [])
    fdata["friends"].setdefault(key_b, [])
    fdata["pending"][key_a] = [k for k in fdata["pending"][key_a] if k != key_b]
    fdata["pending"][key_b] = [k for k in fdata["pending"][key_b] if k != key_a]
    if key_b not in fdata["friends"][key_a]:
        fdata["friends"][key_a].append(key_b)
    if key_a not in fdata["friends"][key_b]:
        fdata["friends"][key_b].append(key_a)


async def handler(websocket):
    remote_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    if IP_CONNECTIONS.get(remote_ip, 0) >= MAX_CONNECTIONS_PER_IP:
        await websocket.close(code=1008, reason="Too many concurrent connections from IP.")
        return
    IP_CONNECTIONS[remote_ip] = IP_CONNECTIONS.get(remote_ip, 0) + 1

    nonce = secrets.token_hex(24)
    nonce_issued = time.time()
    client_state = None
    rate_buckets: dict[str, list[float]] = {}

    try:
        await websocket.send(json.dumps({"type": "challenge", "nonce": nonce}))
        try:
            first_raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            await websocket.send(json.dumps({"type": "error", "msg": "Authentication handshake timed out."}))
            await websocket.close(code=1008)
            return

        async def _iter_messages():
            yield first_raw
            async for m in websocket:
                yield m

        async for raw_msg in _iter_messages():
            now = time.time()
            try:
                data = json.loads(raw_msg)
            except (json.JSONDecodeError, TypeError):
                await websocket.send(json.dumps({"type": "error", "msg": "Malformed JSON payload."}))
                continue

            if not isinstance(data, dict) or not _check_json_depth(data):
                await websocket.send(json.dumps({"type": "error", "msg": "Invalid or overly nested JSON payload."}))
                continue

            msg_type = str(data.get("type", ""))

            if msg_type == "node_info" and client_state is None:
                fed = load_federation()
                meta = load_world_meta()
                worlds_list = sorted(
                    f.stem for f in WORLDS_DIR.glob("*.wrl")
                    if not meta.get(f.stem, {}).get("private", False)
                )
                await websocket.send(json.dumps({
                    "type": "node_info_ok",
                    "node_name": fed.get("node_name", "cyberX87 Node"),
                    "node_desc": fed.get("node_desc", ""),
                    "online_count": len(CLIENTS),
                    "worlds": worlds_list,
                    "peers": [
                        {"url": u, "label": m.get("label", u), "default_world": m.get("default_world", "hub")}
                        for u, m in fed.get("peers", {}).items()
                    ],
                }))
                await websocket.close()
                return

            if msg_type == "auth":
                if client_state is not None:
                    await websocket.send(json.dumps({"type": "error", "msg": "Already authenticated."}))
                    continue

                _prune_used_nonces(now)
                if nonce in USED_NONCES or (now - nonce_issued) > AUTH_TIMEOUT_SEC:
                    await websocket.send(json.dumps({"type": "error", "msg": "Authentication challenge expired or replayed."}))
                    await websocket.close(code=1008)
                    return
                USED_NONCES[nonce] = now + 60.0

                pubkey_hex = str(data.get("pubkey", "")).strip().lower()
                sig_hex = str(data.get("signature", "")).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", pubkey_hex) or not re.fullmatch(r"[0-9a-f]{128}", sig_hex):
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid key or signature format."}))
                    await websocket.close(code=1008)
                    return

                try:
                    vk = VerifyKey(bytes.fromhex(pubkey_hex))
                    vk.verify(nonce.encode("utf-8"), bytes.fromhex(sig_hex))
                except (ValueError, BadSignatureError):
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid cryptographic signature."}))
                    await websocket.close(code=1008)
                    return

                role = get_role(pubkey_hex)
                if role == "banned":
                    await websocket.send(json.dumps({"type": "error", "msg": "Key is banned from this server."}))
                    await websocket.close(code=1008)
                    return

                for old_ws, old_st in list(CLIENTS.items()):
                    if old_ws != websocket and old_st["pubkey"] == pubkey_hex:
                        CLIENTS.pop(old_ws, None)
                        await broadcast_world(old_st["world"], {"type": "peer_leave", "short_id": old_st["short_id"]}, exclude_ws=old_ws)
                        try:
                            await old_ws.send(json.dumps({"type": "error", "msg": "Disconnected: your key signed in from another session."}))
                            await old_ws.close(code=1008)
                        except Exception:
                            pass

                saved_nick = load_perms().get("nick_registry", {}).get(pubkey_hex, "")
                nick = allocate_unique_nick(str(data.get("nick") or saved_nick or f"User_{pubkey_hex[:6]}"), pubkey_hex, exclude_ws=websocket)
                remember_user_nick(pubkey_hex, nick)

                color = validate_hex_color(data.get("color", "#00ffcc"))
                meta = load_world_meta()
                world = sanitize_name(str(data.get("world", "hub")))
                if not (WORLDS_DIR / f"{world}.wrl").exists() or not can_visit_world(world, meta.get(world, {}), pubkey_hex, role):
                    world = "hub"

                req_avatar = sanitize_name(str(data.get("avatar", "")))
                carried_wrl = str(data.get("avatar_wrl", ""))
                avatar_synced = False

                if req_avatar and req_avatar.lower() != "default" and carried_wrl:
                    async with UPLOAD_LOCK:
                        installed_name, installed_wrl = _install_transferred_avatar(req_avatar, carried_wrl, pubkey_hex, role)
                        if installed_name != "default" and installed_wrl:
                            req_avatar = installed_name
                            avatar_synced = True
                        else:
                            req_avatar = sanitize_name(load_perms().get("user_avatars", {}).get(pubkey_hex, "default"))
                elif not req_avatar or req_avatar.lower() == "default":
                    req_avatar = sanitize_name(load_perms().get("user_avatars", {}).get(pubkey_hex, "default"))

                if req_avatar != "default" and not (AVATARS_DIR / f"{req_avatar}.wrl").exists():
                    req_avatar = "default"

                w_meta = meta.get(world, {})
                sp_pos, sp_yaw = get_world_spawn(world, w_meta)

                client_state = {
                    "pubkey": pubkey_hex,
                    "short_id": pubkey_hex[:8],
                    "nick": nick,
                    "color": color,
                    "role": role,
                    "world": world,
                    "pos": list(sp_pos),
                    "rot": [0.0, 1.0, 0.0, round(math.radians(-sp_yaw), 3)],
                    "avatar": req_avatar,
                }
                CLIENTS[websocket] = client_state

                worlds_list = [
                    f.stem for f in WORLDS_DIR.glob("*.wrl")
                    if can_visit_world(f.stem, meta.get(f.stem, {}), pubkey_hex, role)
                ]
                avatars_list = sorted(f.stem for f in AVATARS_DIR.glob("*.wrl"))
                fed = load_federation()
                wrl_text = read_wrl_safe(WORLDS_DIR / f"{world}.wrl")
                print(f"[AUTH] {nick} ({role}) -> '{world}' [avatar: {req_avatar}] ({len(wrl_text)} bytes)")

                auth_payload = {
                    "type": "auth_ok",
                    "role": role,
                    "nick": nick,
                    "short_id": client_state["short_id"],
                    "avatar": req_avatar,
                    "avatar_synced": avatar_synced,
                    "node_name": fed.get("node_name", "cyberX87 Node"),
                    "world": world,
                    "wrl_hash": compute_wrl_sha256(wrl_text),
                    "description": w_meta.get("description", ""),
                    "radio_url": w_meta.get("radio_url", ""),
                    "radio_name": w_meta.get("radio_name", ""),
                    "wrl": wrl_text,
                    "worlds": worlds_list,
                    "avatars": avatars_list,
                }
                if w_meta.get("spawn_pos"):
                    auth_payload["spawn_pos"] = sp_pos
                    auth_payload["spawn_yaw"] = sp_yaw
                await websocket.send(json.dumps(auth_payload))

                peers = [
                    {k: v for k, v in st.items() if k != "pubkey"}
                    for ws_peer, st in CLIENTS.items()
                    if ws_peer != websocket and st["world"] == world
                ]
                await websocket.send(json.dumps({"type": "peers", "peers": peers}))

                peer_join_payload = {
                    "type": "peer_join",
                    "peer": {k: v for k, v in client_state.items() if k != "pubkey"},
                }
                if avatar_synced and req_avatar != "default":
                    peer_join_payload["avatar_wrl"] = read_wrl_safe(AVATARS_DIR / f"{req_avatar}.wrl")
                await broadcast_world(world, peer_join_payload, exclude_ws=websocket)

                fdata = load_friends()
                my_friends = set(fdata.get("friends", {}).get(pubkey_hex, []))
                pending_in = fdata.get("pending", {}).get(pubkey_hex, [])
                if pending_in:
                    await websocket.send(json.dumps({
                        "type": "info",
                        "msg": f"You have {len(pending_in)} pending friend request(s). Type /friends to view."
                    }))
                for peer_ws, peer_st in list(CLIENTS.items()):
                    if peer_ws != websocket and peer_st["pubkey"] in my_friends:
                        try:
                            await peer_ws.send(json.dumps({
                                "type": "info",
                                "msg": f"[FRIEND] {nick} ({client_state['short_id']}) came online in '{world}'."
                            }))
                        except Exception:
                            pass
                continue

            if not client_state:
                await websocket.send(json.dumps({"type": "error", "msg": "Authentication required."}))
                await websocket.close(code=1008)
                return

            if not _check_rate_limit(rate_buckets, msg_type, now):
                if msg_type != "move":
                    await websocket.send(json.dumps({"type": "error", "msg": f"Rate limit exceeded for '{msg_type}'. Slow down."}))
                continue

            if msg_type == "move":
                raw_pos = data.get("pos")
                raw_rot = data.get("rot")
                if isinstance(raw_pos, list) and len(raw_pos) >= 3 and all(_is_finite_num(v) for v in raw_pos[:3]):
                    vals = [float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2])]
                    if all(-10000.0 <= v <= 10000.0 for v in vals):
                        client_state["pos"] = vals
                if isinstance(raw_rot, list) and len(raw_rot) >= 4 and all(_is_finite_num(v) for v in raw_rot[:4]):
                    rvals = [float(raw_rot[0]), float(raw_rot[1]), float(raw_rot[2]), float(raw_rot[3])]
                    if all(-1000.0 <= v <= 1000.0 for v in rvals):
                        client_state["rot"] = rvals
                await broadcast_world(client_state["world"], {
                    "type": "peer_move",
                    "short_id": client_state["short_id"],
                    "pos": client_state["pos"],
                    "rot": client_state["rot"],
                }, exclude_ws=websocket)

            elif msg_type == "chat":
                text = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("text", "")))[:300].strip()
                if not text:
                    continue
                if text.startswith("/emote "):
                    await broadcast_world(client_state["world"], {
                        "type": "emote",
                        "short_id": client_state["short_id"],
                        "nick": client_state["nick"],
                        "emote": sanitize_name(text.split(" ", 1)[1]),
                    })
                    continue

                if text.startswith("/"):
                    srv_plugins = load_server_plugins()
                    cmd_tok, _, cmd_args = text.partition(" ")
                    if not cmd_tok or len(cmd_tok) > 32:
                        continue
                    spec = srv_plugins.get(cmd_tok.lower())
                    if spec:
                        req_role = spec.get("role", "guest").lower()
                        if req_role == "admin" and client_state["role"] != "admin":
                            await websocket.send(json.dumps({"type": "error", "msg": f"{cmd_tok} requires admin role."}))
                            continue
                        if req_role == "builder" and client_state["role"] not in ("admin", "builder"):
                            await websocket.send(json.dumps({"type": "error", "msg": f"{cmd_tok} requires builder role."}))
                            continue

                        act = spec.get("action", "broadcast").lower()
                        safe_args = sanitize_plugin_arg(cmd_args)
                        px, py, pz = client_state["pos"]
                        rendered = spec.get("value", "").format(
                            nick=client_state["nick"],
                            world=client_state["world"],
                            args=safe_args,
                            x=round(px, 2),
                            y=round(py, 2),
                            z=round(pz, 2),
                            z_front=round(pz - 4.0, 2),
                        )
                        w_meta = load_world_meta().get(client_state["world"], {})
                        if act == "spawn_vrml":
                            if not can_edit_world(client_state["world"], w_meta, client_state["pubkey"], client_state["role"]):
                                await websocket.send(json.dumps({
                                    "type": "error",
                                    "msg": f"Permission denied: you are not a creator or co-builder of '{client_state['world']}'."
                                }))
                                continue
                            async with UPLOAD_LOCK:
                                wpath = WORLDS_DIR / f"{client_state['world']}.wrl"
                                create_world_snapshot(client_state["world"], label="auto")
                                updated = read_wrl_safe(wpath) + f"\n{sanitize_vrml_script_nodes(rendered)}\n"
                                _atomic_write_text(wpath, updated)
                                wd_msg = {
                                    "type": "world_data",
                                    "world": client_state["world"],
                                    "wrl_hash": compute_wrl_sha256(updated),
                                    "wrl": updated,
                                    "description": w_meta.get("description", ""),
                                    "radio_url": w_meta.get("radio_url", ""),
                                    "radio_name": w_meta.get("radio_name", ""),
                                    "in_place": True,
                                }
                                if w_meta.get("spawn_pos"):
                                    wd_msg["spawn_pos"] = w_meta["spawn_pos"]
                                    wd_msg["spawn_yaw"] = float(w_meta.get("spawn_yaw", 0.0) or 0.0)
                                await broadcast_world(client_state["world"], wd_msg)
                        elif act == "broadcast":
                            for peer_ws in list(CLIENTS.keys()):
                                try:
                                    await peer_ws.send(json.dumps({"type": "info", "msg": rendered[:300]}))
                                except Exception:
                                    pass
                        continue

                await broadcast_world(client_state["world"], {
                    "type": "chat",
                    "short_id": client_state["short_id"],
                    "nick": client_state["nick"],
                    "color": client_state["color"],
                    "text": text,
                })

            elif msg_type == "join_world":
                target = sanitize_name(str(data.get("world", "hub")))
                if not (WORLDS_DIR / f"{target}.wrl").exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue
                w_meta = load_world_meta().get(target, {})
                if not can_visit_world(target, w_meta, client_state["pubkey"], client_state["role"]):
                    await websocket.send(json.dumps({
                        "type": "error",
                        "msg": f"Access denied: '{target}' is a private world (ask the creator to whitelist you)."
                    }))
                    continue
                old_world = client_state["world"]
                if old_world != target:
                    client_state["world"] = target
                    await broadcast_world(old_world, {"type": "peer_leave", "short_id": client_state["short_id"]}, exclude_ws=websocket)
                await send_world_transition(websocket, client_state, target, w_meta, str(data.get("cached_hash", "")).strip().lower())

            elif msg_type == "list_worlds":
                meta = load_world_meta()
                worlds_list = sorted(
                    f.stem for f in WORLDS_DIR.glob("*.wrl")
                    if can_visit_world(f.stem, meta.get(f.stem, {}), client_state["pubkey"], client_state["role"])
                )
                descriptions = {
                    w: f"{'[PRIVATE] ' if meta.get(w, {}).get('private') else ''}{meta.get(w, {}).get('description', '')}".strip()
                    for w in worlds_list
                }
                ratings_map = {}
                for w in worlds_list:
                    avg_r, cnt_r, _ = summarize_world_ratings(meta.get(w, {}))
                    if cnt_r > 0:
                        ratings_map[w] = {"avg": avg_r, "count": cnt_r}
                await websocket.send(json.dumps({
                    "type": "worlds_list",
                    "worlds": worlds_list,
                    "descriptions": descriptions,
                    "ratings": ratings_map,
                    "current": client_state["world"],
                }))

            elif msg_type == "delete_world":
                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required to delete worlds."}))
                    continue
                target = sanitize_name(str(data.get("world", "")))
                if not target or target.lower() == "hub":
                    await websocket.send(json.dumps({"type": "error", "msg": "Cannot delete the core 'hub' world."}))
                    continue
                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                async with UPLOAD_LOCK:
                    create_world_snapshot(target, label="pre_delete")
                    wrl_path.unlink()
                    meta = load_world_meta()
                    meta.pop(target, None)
                    save_world_meta(meta)
                    ensure_hub_world()
                    worlds_list = sorted(f.stem for f in WORLDS_DIR.glob("*.wrl"))
                    hub_meta = meta.get("hub", {})

                    for peer_ws, peer_st in list(CLIENTS.items()):
                        if peer_st["world"] == target:
                            try:
                                await peer_ws.send(json.dumps({"type": "info", "msg": f"World '{target}' was deleted by an admin. Returning to hub."}))
                                await send_world_transition(peer_ws, peer_st, "hub", hub_meta)
                            except Exception:
                                pass

                await websocket.send(json.dumps({
                    "type": "worlds_list",
                    "worlds": worlds_list,
                    "descriptions": {w: meta.get(w, {}).get("description", "") for w in worlds_list},
                    "current": client_state["world"],
                }))
                await websocket.send(json.dumps({"type": "info", "msg": f"Deleted world '{target}'."}))

            elif msg_type == "get_world_info":
                target = sanitize_name(str(data.get("world") or client_state["world"]))
                if not (WORLDS_DIR / f"{target}.wrl").exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue
                meta = load_world_meta()
                if not can_visit_world(target, meta.get(target, {}), client_state["pubkey"], client_state["role"]):
                    await websocket.send(json.dumps({"type": "error", "msg": f"Access denied: '{target}' is a private world."}))
                    continue
                await websocket.send(json.dumps(build_world_info_payload(target, meta, load_perms())))

            elif msg_type == "rate_world":
                action = str(data.get("action", "list")).strip().lower()
                meta = load_world_meta()

                if action == "top":
                    ranked = []
                    for f in WORLDS_DIR.glob("*.wrl"):
                        w_name = f.stem
                        w_entry = meta.get(w_name, {})
                        if not can_visit_world(w_name, w_entry, client_state["pubkey"], client_state["role"]):
                            continue
                        avg_r, cnt_r, _ = summarize_world_ratings(w_entry)
                        if cnt_r > 0:
                            ranked.append({
                                "world": w_name,
                                "title": w_entry.get("title") or w_name,
                                "avg": avg_r,
                                "count": cnt_r,
                                "description": w_entry.get("description", ""),
                            })
                    ranked.sort(key=lambda item: (item["avg"], item["count"], item["world"]), reverse=True)
                    await websocket.send(json.dumps({"type": "world_ratings_top", "worlds": ranked[:15]}))
                    continue

                target = sanitize_name(str(data.get("world") or client_state["world"]))
                if not (WORLDS_DIR / f"{target}.wrl").exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue
                w_entry = meta.get(target, {})
                if not can_visit_world(target, w_entry, client_state["pubkey"], client_state["role"]):
                    await websocket.send(json.dumps({"type": "error", "msg": f"Access denied: '{target}' is a private world."}))
                    continue

                if action == "list":
                    avg_r, cnt_r, dist = summarize_world_ratings(w_entry)
                    nick_reg = load_perms().get("nick_registry", {})
                    raw_ratings = w_entry.get("ratings", {}) if isinstance(w_entry.get("ratings"), dict) else {}
                    reviews = []
                    for pk, r_obj in raw_ratings.items():
                        if not isinstance(r_obj, dict):
                            continue
                        st_val = int(r_obj.get("stars", 0) or 0)
                        if 1 <= st_val <= 5:
                            reviews.append({
                                "short_id": pk[:8],
                                "nick": nick_reg.get(pk) or r_obj.get("nick") or pk[:8],
                                "stars": st_val,
                                "review": str(r_obj.get("review", "")),
                                "updated_at": int(r_obj.get("updated_at", 0) or 0),
                                "is_me": pk == client_state["pubkey"],
                            })
                    reviews.sort(key=lambda r: r["updated_at"], reverse=True)
                    await websocket.send(json.dumps({
                        "type": "world_ratings_info",
                        "world": target,
                        "avg": avg_r,
                        "count": cnt_r,
                        "dist": {str(k): v for k, v in dist.items()},
                        "reviews": reviews[:10],
                    }))
                    continue

                if action == "unrate":
                    async with UPLOAD_LOCK:
                        meta = load_world_meta()
                        w_entry = meta.get(target, {})
                        ratings = w_entry.get("ratings")
                        if not isinstance(ratings, dict) or client_state["pubkey"] not in ratings:
                            await websocket.send(json.dumps({"type": "error", "msg": f"You have not rated '{target}' yet."}))
                            continue
                        ratings.pop(client_state["pubkey"], None)
                        meta[target] = w_entry
                        save_world_meta(meta)
                        avg_r, cnt_r, _ = summarize_world_ratings(w_entry)
                    summary_txt = f" ({avg_r:.1f}/5 from {cnt_r} vote{'s' if cnt_r != 1 else ''})" if cnt_r else " (no ratings yet)"
                    await websocket.send(json.dumps({"type": "info", "msg": f"Removed your rating for '{target}'{summary_txt}."}))
                    continue

                if action == "rate":
                    owner_key = str(w_entry.get("owner", "")).strip()
                    if owner_key and owner_key == client_state["pubkey"]:
                        await websocket.send(json.dumps({"type": "error", "msg": "You cannot rate your own world."}))
                        continue
                    try:
                        stars = int(data.get("stars", 0))
                    except (TypeError, ValueError):
                        stars = 0
                    if stars < 1 or stars > 5:
                        await websocket.send(json.dumps({"type": "error", "msg": "Rating must be between 1 and 5 stars."}))
                        continue
                    review_txt = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("review", ""))).split())[:140]
                    async with UPLOAD_LOCK:
                        meta = load_world_meta()
                        w_entry = meta.get(target, {})
                        ratings = w_entry.setdefault("ratings", {})
                        if len(ratings) >= 5000 and client_state["pubkey"] not in ratings:
                            await websocket.send(json.dumps({"type": "error", "msg": "Rating limit reached for this world."}))
                            continue
                        is_update = client_state["pubkey"] in ratings
                        ratings[client_state["pubkey"]] = {
                            "stars": stars,
                            "review": review_txt,
                            "nick": client_state["nick"],
                            "updated_at": int(time.time()),
                        }
                        meta[target] = w_entry
                        save_world_meta(meta)
                        avg_r, cnt_r, _ = summarize_world_ratings(w_entry)
                    verb = "Updated" if is_update else "Submitted"
                    await websocket.send(json.dumps({
                        "type": "info",
                        "msg": f"{verb} {stars}/5 star rating for '{target}' (now {avg_r:.1f}/5 across {cnt_r} vote{'s' if cnt_r != 1 else ''})."
                    }))
                    continue

                await websocket.send(json.dumps({"type": "error", "msg": "Unknown rating action (expected rate, unrate, list, or top)."}))

            elif msg_type == "world_acl":
                action = str(data.get("action", "list"))
                target = sanitize_name(str(data.get("world") or client_state["world"]))
                if not (WORLDS_DIR / f"{target}.wrl").exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue
                meta = load_world_meta()
                w_entry = meta.get(target, {})
                owner_key = w_entry.get("owner", "")
                if not (client_state["role"] == "admin" or (bool(owner_key) and owner_key == client_state["pubkey"])):
                    await websocket.send(json.dumps({
                        "type": "error",
                        "msg": f"Permission denied: only the creator of '{target}' can manage access control or co-builders."
                    }))
                    continue

                nick_reg = load_perms().get("nick_registry", {})
                w_entry.setdefault("private", False)
                w_entry.setdefault("co_builders", [])
                w_entry.setdefault("whitelist", [])

                if action == "list":
                    def _fmt_keys(klist):
                        return [
                            {
                                "pubkey": k,
                                "short_id": k[:8],
                                "nick": next((st["nick"] for st in CLIENTS.values() if st["pubkey"] == k), "") or nick_reg.get(k) or k[:8],
                            }
                            for k in klist
                        ]
                    await websocket.send(json.dumps({
                        "type": "world_acl_info",
                        "world": target,
                        "private": bool(w_entry.get("private", False)),
                        "co_builders": _fmt_keys(w_entry.get("co_builders", [])),
                        "whitelist": _fmt_keys(w_entry.get("whitelist", [])),
                    }))
                    continue

                if action == "set_private":
                    if target.lower() == "hub":
                        await websocket.send(json.dumps({"type": "error", "msg": "Genesis Hub cannot be made private."}))
                        continue
                    is_priv = bool(data.get("private", True))
                    async with UPLOAD_LOCK:
                        meta = load_world_meta()
                        w_entry = meta.get(target, {})
                        w_entry["private"] = is_priv
                        w_entry.setdefault("co_builders", [])
                        w_entry.setdefault("whitelist", [])
                        meta[target] = w_entry
                        save_world_meta(meta)

                        if is_priv:
                            hub_meta = meta.get("hub", {})
                            for peer_ws, peer_st in list(CLIENTS.items()):
                                if peer_st["world"] == target and not can_visit_world(target, w_entry, peer_st["pubkey"], peer_st["role"]):
                                    await broadcast_world(target, {"type": "peer_leave", "short_id": peer_st["short_id"]}, exclude_ws=peer_ws)
                                    try:
                                        await peer_ws.send(json.dumps({"type": "info", "msg": f"World '{target}' was set to PRIVATE by its creator. Returning to hub."}))
                                        await send_world_transition(peer_ws, peer_st, "hub", hub_meta)
                                    except Exception:
                                        pass

                    await websocket.send(json.dumps({
                        "type": "info",
                        "msg": f"World '{target}' visibility set to {'PRIVATE (whitelist/co-builders only)' if is_priv else 'PUBLIC'}."
                    }))
                    continue

                target_raw = str(data.get("target", "")).strip()
                resolved_key, resolved_nick = resolve_user_key(target_raw, load_friends(), client_state["pubkey"])
                if not resolved_key:
                    await websocket.send(json.dumps({"type": "error", "msg": f"User '{target_raw}' not found or ambiguous (try their 8-char ID or full pubkey)."}))
                    continue

                async with UPLOAD_LOCK:
                    meta = load_world_meta()
                    w_entry = meta.get(target, {})
                    w_entry.setdefault("co_builders", [])
                    w_entry.setdefault("whitelist", [])
                    if action == "add_builder":
                        if len(w_entry["co_builders"]) >= 50:
                            await websocket.send(json.dumps({"type": "error", "msg": "Maximum 50 co-builders per world."}))
                            continue
                        if resolved_key not in w_entry["co_builders"]:
                            w_entry["co_builders"].append(resolved_key)
                        msg_out = f"Granted co-builder access on '{target}' to {resolved_nick} ({resolved_key[:8]})."
                    elif action == "remove_builder":
                        w_entry["co_builders"] = [k for k in w_entry["co_builders"] if k != resolved_key]
                        msg_out = f"Revoked co-builder access on '{target}' from {resolved_nick} ({resolved_key[:8]})."
                    elif action == "allow_visitor":
                        if len(w_entry["whitelist"]) >= 200:
                            await websocket.send(json.dumps({"type": "error", "msg": "Maximum 200 whitelisted visitors per world."}))
                            continue
                        if resolved_key not in w_entry["whitelist"]:
                            w_entry["whitelist"].append(resolved_key)
                        msg_out = f"Whitelisted {resolved_nick} ({resolved_key[:8]}) to visit '{target}'."
                    elif action == "revoke_visitor":
                        w_entry["whitelist"] = [k for k in w_entry["whitelist"] if k != resolved_key]
                        msg_out = f"Removed {resolved_nick} ({resolved_key[:8]}) from '{target}' whitelist."
                    else:
                        await websocket.send(json.dumps({"type": "error", "msg": "Unknown world ACL action."}))
                        continue
                    meta[target] = w_entry
                    save_world_meta(meta)

                await websocket.send(json.dumps({"type": "info", "msg": msg_out}))

            elif msg_type == "world_edit":
                action = str(data.get("action", ""))
                target = sanitize_name(str(data.get("world") or client_state["world"]))
                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                async with UPLOAD_LOCK:
                    meta = load_world_meta()
                    w_entry = meta.get(target, {})
                    if not can_edit_world(target, w_entry, client_state["pubkey"], client_state["role"]):
                        await websocket.send(json.dumps({
                            "type": "error",
                            "msg": f"Permission denied: you need builder rights and owner/co-builder access on '{target}'."
                        }))
                        continue

                    def _world_update_msg(wrl_body: str) -> dict:
                        msg = {
                            "type": "world_data",
                            "world": target,
                            "wrl_hash": compute_wrl_sha256(wrl_body),
                            "description": w_entry.get("description", ""),
                            "radio_url": w_entry.get("radio_url", ""),
                            "radio_name": w_entry.get("radio_name", ""),
                            "wrl": wrl_body,
                            "in_place": True,
                        }
                        if w_entry.get("spawn_pos"):
                            msg["spawn_pos"] = w_entry["spawn_pos"]
                            msg["spawn_yaw"] = float(w_entry.get("spawn_yaw", 0.0) or 0.0)
                        return msg

                    if action in ("set_spawn", "clear_spawn"):
                        cur_wrl = read_wrl_safe(wrl_path)
                        create_world_snapshot(target, label="auto")
                        if action == "set_spawn":
                            raw_coords = (
                                data.get("x", client_state["pos"][0]),
                                data.get("y", client_state["pos"][1]),
                                data.get("z", client_state["pos"][2]),
                                data.get("yaw", 0.0),
                            )
                            if not all(_is_finite_num(v) for v in raw_coords):
                                await websocket.send(json.dumps({"type": "error", "msg": "Invalid spawn coordinates or yaw."}))
                                continue
                            sx = max(-500.0, min(500.0, round(float(raw_coords[0]), 3)))
                            sy = max(-100.0, min(500.0, round(float(raw_coords[1]), 3)))
                            sz = max(-500.0, min(500.0, round(float(raw_coords[2]), 3)))
                            syaw = round(float(raw_coords[3]) % 360.0, 2)
                            w_entry["spawn_pos"] = [sx, sy, sz]
                            w_entry["spawn_yaw"] = syaw
                            updated_wrl = apply_vrml_spawn_point(cur_wrl, [sx, sy, sz], syaw)
                            info_txt = f"Set spawn point for '{target}' to ({sx:.2f}, {sy:.2f}, {sz:.2f}) yaw={syaw:.0f} deg."
                        else:
                            w_entry.pop("spawn_pos", None)
                            w_entry.pop("spawn_yaw", None)
                            updated_wrl = apply_vrml_spawn_point(cur_wrl, None)
                            info_txt = f"Cleared custom spawn point for '{target}' (reverted to default spawn)."

                        w_entry["updated_at"] = int(time.time())
                        meta[target] = w_entry
                        save_world_meta(meta)
                        _atomic_write_text(wrl_path, updated_wrl)
                        await broadcast_world(target, _world_update_msg(updated_wrl))
                        await websocket.send(json.dumps({"type": "info", "msg": info_txt}))

                    elif action == "place":
                        shape = str(data.get("shape", "box")).strip().lower()
                        if shape not in ("box", "pillar", "sphere", "cone", "arch", "platform", "wall"):
                            await websocket.send(json.dumps({"type": "error", "msg": "Supported shapes: box, pillar, sphere, cone, arch, platform, wall."}))
                            continue
                        raw_p = (
                            data.get("x", client_state["pos"][0]),
                            data.get("y", 0.0),
                            data.get("z", client_state["pos"][2] - 4.0),
                            data.get("scale", 1.0),
                            data.get("yaw", 0.0),
                        )
                        if not all(_is_finite_num(v) for v in raw_p):
                            await websocket.send(json.dumps({"type": "error", "msg": "Invalid coordinates or scale."}))
                            continue
                        px = max(-500.0, min(500.0, float(raw_p[0])))
                        py = max(-100.0, min(500.0, float(raw_p[1])))
                        pz = max(-500.0, min(500.0, float(raw_p[2])))
                        scale = max(0.2, min(20.0, float(raw_p[3])))
                        yaw_deg = float(raw_p[4])
                        cur_wrl = read_wrl_safe(wrl_path)
                        if len(cur_wrl.encode("utf-8", errors="replace")) > MAX_FILE_SIZE - 4096:
                            await websocket.send(json.dumps({"type": "error", "msg": "World file size limit reached."}))
                            continue
                        create_world_snapshot(target, label="auto")
                        tag_id = secrets.token_hex(3)
                        snippet = build_placed_vrml_snippet(
                            shape, px, py, pz, scale, parse_rgb_color(str(data.get("color", "cyan"))), client_state["nick"], tag_id=tag_id, yaw_deg=yaw_deg
                        )
                        updated_wrl = cur_wrl.rstrip() + f"\n\n{snippet}\n"
                        _atomic_write_text(wrl_path, updated_wrl)
                        w_entry["updated_at"] = int(time.time())
                        meta[target] = w_entry
                        save_world_meta(meta)
                        await broadcast_world(target, _world_update_msg(updated_wrl))
                        await websocket.send(json.dumps({"type": "info", "msg": f"Placed {shape} [{tag_id}] at ({px:.1f}, {py:.1f}, {pz:.1f}) in '{target}'."}))

                    elif action == "mutate_placed":
                        tag_id = re.sub(r"[^a-fA-F0-9]", "", str(data.get("tag_id", ""))).lower()
                        op = str(data.get("op", "")).strip().lower()
                        if not tag_id or op not in ("move", "rotate", "paint", "scale", "clone"):
                            await websocket.send(json.dumps({"type": "error", "msg": "Invalid object manipulation request (expected tag_id and move/rotate/paint/scale/clone)."}))
                            continue
                        raw_m = (
                            data.get("dx", 0.0),
                            data.get("dy", 0.0),
                            data.get("dz", 0.0),
                            data.get("yaw_delta", 0.0),
                            data.get("scale_mult", 1.0),
                        )
                        if not all(_is_finite_num(v) for v in raw_m):
                            await websocket.send(json.dumps({"type": "error", "msg": "Invalid numeric offset or angle."}))
                            continue
                        dx = max(-200.0, min(200.0, float(raw_m[0])))
                        dy = max(-200.0, min(200.0, float(raw_m[1])))
                        dz = max(-200.0, min(200.0, float(raw_m[2])))
                        yaw_delta = float(raw_m[3])
                        scale_mult = float(raw_m[4])
                        cur_wrl = read_wrl_safe(wrl_path)
                        if op == "clone" and len(cur_wrl.encode("utf-8", errors="replace")) > MAX_FILE_SIZE - 4096:
                            await websocket.send(json.dumps({"type": "error", "msg": "World file size limit reached."}))
                            continue
                        create_world_snapshot(target, label="auto")
                        updated_wrl, ok_mut, status_msg = mutate_placed_snippet(
                            cur_wrl,
                            tag_id,
                            op=op,
                            dx=dx,
                            dy=dy,
                            dz=dz,
                            yaw_delta_deg=yaw_delta,
                            rgb=parse_rgb_color(str(data.get("color", "cyan"))) if op == "paint" else None,
                            scale_mult=scale_mult,
                        )
                        if not ok_mut:
                            await websocket.send(json.dumps({"type": "error", "msg": status_msg}))
                            continue
                        _atomic_write_text(wrl_path, updated_wrl)
                        w_entry["updated_at"] = int(time.time())
                        meta[target] = w_entry
                        save_world_meta(meta)
                        await broadcast_world(target, _world_update_msg(updated_wrl))
                        await websocket.send(json.dumps({"type": "info", "msg": status_msg}))

                    elif action == "delete_placed":
                        tag_id = re.sub(r"[^a-fA-F0-9]", "", str(data.get("tag_id", ""))).lower()
                        if not tag_id:
                            await websocket.send(json.dumps({"type": "error", "msg": "Specify a placed object ID (e.g., /delobj <id> or aim at a placed object with [I])."}))
                            continue
                        cur_wrl = read_wrl_safe(wrl_path)
                        create_world_snapshot(target, label="auto")
                        updated_wrl, removed = remove_placed_snippet(cur_wrl, tag_id)
                        if not removed:
                            await websocket.send(json.dumps({"type": "error", "msg": f"Placed object '{tag_id}' not found in '{target}'."}))
                            continue
                        _atomic_write_text(wrl_path, updated_wrl)
                        w_entry["updated_at"] = int(time.time())
                        meta[target] = w_entry
                        save_world_meta(meta)
                        await broadcast_world(target, _world_update_msg(updated_wrl))
                        await websocket.send(json.dumps({"type": "info", "msg": f"Removed placed object [{tag_id}] from '{target}'."}))

                    elif action == "undo":
                        w_backup_dir = BACKUPS_DIR / target
                        auto_snaps = sorted(w_backup_dir.glob("*_auto*.wrl"), reverse=True) if w_backup_dir.exists() else []
                        if not auto_snaps:
                            await websocket.send(json.dumps({"type": "error", "msg": f"No recent edit snapshot to undo in '{target}'."}))
                            continue
                        latest_snap = auto_snaps[0]
                        restored_text = sanitize_vrml_script_nodes(read_wrl_safe(latest_snap))
                        snap_meta_path = latest_snap.with_suffix(".meta.json")
                        if snap_meta_path.exists():
                            try:
                                prev_meta = json.loads(snap_meta_path.read_text(encoding="utf-8"))
                                if isinstance(prev_meta, dict):
                                    if "spawn_pos" in prev_meta:
                                        w_entry["spawn_pos"] = prev_meta["spawn_pos"]
                                        w_entry["spawn_yaw"] = prev_meta.get("spawn_yaw", 0.0)
                                    else:
                                        w_entry.pop("spawn_pos", None)
                                        w_entry.pop("spawn_yaw", None)
                                    meta[target] = w_entry
                                    save_world_meta(meta)
                            except Exception:
                                pass
                        restored_text = apply_vrml_world_info(
                            restored_text,
                            w_entry.get("title", target),
                            w_entry.get("description", ""),
                            w_entry.get("tags", []),
                            w_entry.get("radio_url", ""),
                            w_entry.get("radio_name", ""),
                        )
                        if w_entry.get("spawn_pos"):
                            restored_text = apply_vrml_spawn_point(restored_text, w_entry["spawn_pos"], float(w_entry.get("spawn_yaw", 0.0) or 0.0))
                        else:
                            restored_text = apply_vrml_spawn_point(restored_text, None)
                        _atomic_write_text(wrl_path, restored_text)
                        try:
                            latest_snap.unlink(missing_ok=True)
                            snap_meta_path.unlink(missing_ok=True)
                        except Exception:
                            pass
                        await broadcast_world(target, _world_update_msg(restored_text))
                        await websocket.send(json.dumps({"type": "info", "msg": f"Undid last change in '{target}' (restored {latest_snap.name})."}))

            elif msg_type == "upload":
                if client_state["role"] not in ("admin", "builder"):
                    await websocket.send(json.dumps({"type": "error", "msg": "Permission denied: builder role required."}))
                    continue

                kind = str(data.get("kind", "")).strip().lower()
                if kind not in ("world", "avatar"):
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid upload kind: expected 'world' or 'avatar'."}))
                    continue

                raw_filename = str(data.get("filename", "")).strip()
                if raw_filename and not raw_filename.lower().endswith(".wrl"):
                    await websocket.send(json.dumps({"type": "error", "msg": "Only .wrl VRML files are allowed for worlds and avatars."}))
                    continue

                name = sanitize_name(str(data.get("name", "custom")))
                content = str(data.get("content", "")).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

                size_limit = MAX_AVATAR_SIZE if kind == "avatar" else MAX_FILE_SIZE
                if len(content.encode("utf-8", errors="replace")) > size_limit:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "msg": f"File exceeds {'512KB avatar' if kind == 'avatar' else '5MB'} limit."
                    }))
                    continue

                ok_vrml, vrml_err = validate_strict_vrml_document(content, kind=kind)
                if not ok_vrml:
                    await websocket.send(json.dumps({"type": "error", "msg": vrml_err}))
                    continue

                if kind == "world":
                    async with UPLOAD_LOCK:
                        meta = load_world_meta()
                        w_entry = meta.get(name, {})
                        existing_owner = w_entry.get("owner", "")
                        is_new_world = not (WORLDS_DIR / f"{name}.wrl").exists()

                        if name.lower() == "hub":
                            await websocket.send(json.dumps({"type": "error", "msg": "Access denied: 'hub' cannot be overwritten via upload."}))
                            continue
                        if not is_new_world and not can_edit_world(name, w_entry, client_state["pubkey"], client_state["role"]):
                            await websocket.send(json.dumps({"type": "error", "msg": f"Access denied: '{name}' belongs to another creator."}))
                            continue

                        if is_new_world and client_state["role"] != "admin":
                            perms = load_perms()
                            max_total = int(perms.get("max_total_worlds", 0) or 0)
                            max_per_user = int(perms.get("max_worlds_per_user", 0) or 0)
                            all_custom_worlds = [f.stem for f in WORLDS_DIR.glob("*.wrl") if f.stem.lower() != "hub"]
                            if max_total > 0 and len(all_custom_worlds) >= max_total:
                                await websocket.send(json.dumps({"type": "error", "msg": f"Server world limit reached ({len(all_custom_worlds)}/{max_total} worlds)."}))
                                continue
                            if max_per_user > 0:
                                user_world_count = sum(1 for w_name in all_custom_worlds if meta.get(w_name, {}).get("owner") == client_state["pubkey"])
                                if user_world_count >= max_per_user:
                                    await websocket.send(json.dumps({"type": "error", "msg": f"Your personal world limit is reached ({user_world_count}/{max_per_user} worlds)."}))
                                    continue

                        if not is_new_world:
                            create_world_snapshot(name, label="auto")

                        content = ensure_valid_vrml_world(name, content)
                        if not existing_owner:
                            w_entry["owner"] = client_state["pubkey"]
                            w_entry["owner_nick"] = client_state["nick"]
                            w_entry["created_at"] = int(time.time())
                        w_entry["updated_at"] = int(time.time())
                        w_entry.setdefault("title", name)
                        w_entry.setdefault("private", False)
                        w_entry.setdefault("co_builders", [])
                        w_entry.setdefault("whitelist", [])
                        if data.get("description") is not None:
                            w_entry["description"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(data["description"])).strip()[:240]
                        if isinstance(data.get("tags"), list):
                            w_entry["tags"] = [sanitize_name(str(t)) for t in data["tags"][:8] if str(t).strip()]
                        meta[name] = w_entry
                        save_world_meta(meta)

                        content = apply_vrml_world_info(
                            content,
                            w_entry.get("title", name),
                            w_entry.get("description", ""),
                            w_entry.get("tags", []),
                            w_entry.get("radio_url", ""),
                            w_entry.get("radio_name", ""),
                        )
                        if w_entry.get("spawn_pos"):
                            content = apply_vrml_spawn_point(content, w_entry["spawn_pos"], float(w_entry.get("spawn_yaw", 0.0) or 0.0))

                        _atomic_write_text(WORLDS_DIR / f"{name}.wrl", content)
                        worlds_list = [
                            f.stem for f in WORLDS_DIR.glob("*.wrl")
                            if can_visit_world(f.stem, meta.get(f.stem, {}), client_state["pubkey"], client_state["role"])
                        ]

                    await websocket.send(json.dumps({"type": "upload_ok", "kind": "world", "name": name, "worlds": worlds_list}))

                elif kind == "avatar":
                    if name.lower() == "default":
                        await websocket.send(json.dumps({"type": "error", "msg": "'default' is a reserved avatar name."}))
                        continue
                    content = sanitize_vrml_script_nodes(content)
                    if not has_vrml_geometry(content):
                        await websocket.send(json.dumps({"type": "error", "msg": "Upload rejected: avatar .wrl contains no 3D geometry."}))
                        continue

                    async with UPLOAD_LOCK:
                        perms = load_perms()
                        av_owners = perms.setdefault("avatar_owners", {})
                        existing_av_owner = av_owners.get(name, "")
                        if existing_av_owner and existing_av_owner != client_state["pubkey"] and client_state["role"] != "admin":
                            await websocket.send(json.dumps({"type": "error", "msg": f"Access denied: avatar '{name}' belongs to another creator."}))
                            continue
                        if not existing_av_owner:
                            av_owners[name] = client_state["pubkey"]
                        _atomic_write_text(AVATARS_DIR / f"{name}.wrl", content)
                        client_state["avatar"] = name
                        perms["user_avatars"][client_state["pubkey"]] = name
                        save_perms(perms)

                    await websocket.send(json.dumps({"type": "upload_ok", "kind": "avatar", "name": name, "wrl": content}))
                    await broadcast_world(client_state["world"], {
                        "type": "peer_avatar",
                        "short_id": client_state["short_id"],
                        "avatar": name,
                        "wrl": content,
                    }, exclude_ws=websocket)

            elif msg_type == "get_avatar":
                name = sanitize_name(str(data.get("name", "")))
                av_path = AVATARS_DIR / f"{name}.wrl"
                if av_path.exists():
                    await websocket.send(json.dumps({"type": "avatar_data", "name": name, "wrl": read_wrl_safe(av_path)}))

            elif msg_type == "set_avatar":
                name = sanitize_name(str(data.get("name", "default")))
                if name.lower() == "default":
                    client_state["avatar"] = "default"
                    perms = load_perms()
                    perms["user_avatars"].pop(client_state["pubkey"], None)
                    save_perms(perms)
                    await websocket.send(json.dumps({"type": "info", "msg": "Reset avatar to default."}))
                    await broadcast_world(client_state["world"], {
                        "type": "peer_avatar",
                        "short_id": client_state["short_id"],
                        "avatar": "default",
                        "wrl": "",
                    }, exclude_ws=websocket)
                    continue

                av_path = AVATARS_DIR / f"{name}.wrl"
                if not av_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"Avatar '{name}' does not exist."}))
                    continue

                av_wrl = read_wrl_safe(av_path)
                client_state["avatar"] = name
                perms = load_perms()
                perms["user_avatars"][client_state["pubkey"]] = name
                save_perms(perms)
                await websocket.send(json.dumps({
                    "type": "info",
                    "msg": f"Equipped 3D avatar '{name}'.",
                    "equipped_avatar": name,
                    "avatar_wrl": av_wrl,
                }))
                await broadcast_world(client_state["world"], {
                    "type": "peer_avatar",
                    "short_id": client_state["short_id"],
                    "avatar": name,
                    "wrl": av_wrl,
                }, exclude_ws=websocket)

            elif msg_type == "list_avatars":
                await websocket.send(json.dumps({
                    "type": "avatars_list",
                    "avatars": sorted(f.stem for f in AVATARS_DIR.glob("*.wrl")),
                    "current": client_state["avatar"],
                }))

            elif msg_type == "admin_perm":
                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required."}))
                    continue

                action = str(data.get("action", ""))
                target_key = str(data.get("target_key", "")).strip().lower()
                perms = load_perms()

                if action == "list_perms":
                    all_custom_worlds = [f.stem for f in WORLDS_DIR.glob("*.wrl") if f.stem.lower() != "hub"]
                    await websocket.send(json.dumps({
                        "type": "perms_list",
                        "perms": perms,
                        "current_world_count": len(all_custom_worlds),
                    }))
                    continue

                key_actions = ("grant_admin", "revoke_admin", "grant_builder", "revoke_builder", "ban", "unban")
                if action in key_actions and not re.fullmatch(r"[0-9a-f]{64}", target_key):
                    await websocket.send(json.dumps({"type": "error", "msg": "Target must be a valid 64-character hex Ed25519 public key."}))
                    continue

                if action == "grant_admin":
                    if target_key not in perms["admins"]:
                        perms["admins"].append(target_key)
                elif action == "revoke_admin":
                    if len(perms["admins"]) <= 1 and target_key in perms["admins"]:
                        await websocket.send(json.dumps({"type": "error", "msg": "Cannot remove the last remaining admin."}))
                        continue
                    perms["admins"] = [k for k in perms["admins"] if k != target_key]
                elif action == "grant_builder":
                    if target_key not in perms["builders"]:
                        perms["builders"].append(target_key)
                elif action == "revoke_builder":
                    perms["builders"] = [k for k in perms["builders"] if k != target_key]
                elif action == "ban":
                    if target_key == client_state["pubkey"]:
                        await websocket.send(json.dumps({"type": "error", "msg": "You cannot ban your own active admin key."}))
                        continue
                    perms["admins"] = [k for k in perms["admins"] if k != target_key]
                    perms["builders"] = [k for k in perms["builders"] if k != target_key]
                    if target_key not in perms["banned"]:
                        perms["banned"].append(target_key)
                elif action == "unban":
                    perms["banned"] = [k for k in perms["banned"] if k != target_key]
                elif action == "set_default" and target_key in ("guest", "builder"):
                    perms["default_role"] = target_key
                elif action in ("set_max_worlds", "set_user_world_limit"):
                    try:
                        limit_val = max(0, min(100000, int(data.get("limit", 0))))
                    except (TypeError, ValueError):
                        await websocket.send(json.dumps({"type": "error", "msg": "Limit must be a non-negative integer (0 = unlimited)."}))
                        continue
                    field_name = "max_total_worlds" if action == "set_max_worlds" else "max_worlds_per_user"
                    perms[field_name] = limit_val
                else:
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid permission action or missing key."}))
                    continue

                save_perms(perms)

                for peer_ws, peer_st in list(CLIENTS.items()):
                    if not target_key or peer_st["pubkey"] == target_key:
                        new_role = get_role(peer_st["pubkey"])
                        peer_st["role"] = new_role
                        try:
                            if new_role == "banned":
                                await peer_ws.send(json.dumps({"type": "error", "msg": "Your key has been banned by an admin."}))
                                await peer_ws.close()
                            else:
                                await peer_ws.send(json.dumps({"type": "role_updated", "role": new_role}))
                        except Exception:
                            pass

                await websocket.send(json.dumps({"type": "info", "msg": f"Saved permissions to {PERMS_FILE} ({action})."}))

            elif msg_type == "friend_action":
                action = str(data.get("action", ""))
                target_raw = str(data.get("target", "")).strip()
                my_key = client_state["pubkey"]
                fdata = load_friends()
                fdata.setdefault("friends", {}).setdefault(my_key, [])
                fdata.setdefault("pending", {}).setdefault(my_key, [])
                nick_reg = load_perms().get("nick_registry", {})
                online_by_key = {st["pubkey"]: (ws_p, st) for ws_p, st in CLIENTS.items()}

                if action == "list":
                    friends_out = [
                        {
                            "pubkey": fk,
                            "short_id": fk[:8],
                            "nick": online_by_key[fk][1]["nick"] if fk in online_by_key else nick_reg.get(fk, fk[:8]),
                            "online": fk in online_by_key,
                            "world": online_by_key[fk][1]["world"] if fk in online_by_key else "",
                        }
                        for fk in fdata["friends"][my_key]
                    ]
                    pending_out = [
                        {
                            "pubkey": pk,
                            "short_id": pk[:8],
                            "nick": online_by_key[pk][1]["nick"] if pk in online_by_key else nick_reg.get(pk, pk[:8]),
                        }
                        for pk in fdata["pending"][my_key]
                    ]
                    await websocket.send(json.dumps({"type": "friends_list", "friends": friends_out, "pending": pending_out}))
                    continue

                target_key, target_nick = resolve_user_key(target_raw, fdata, my_key)
                if not target_key:
                    await websocket.send(json.dumps({"type": "error", "msg": f"User '{target_raw}' not found or ambiguous (try their 8-char ID or full pubkey)."}))
                    continue
                if target_key == my_key:
                    await websocket.send(json.dumps({"type": "error", "msg": "You cannot add yourself as a friend."}))
                    continue

                fdata["friends"].setdefault(target_key, [])
                fdata["pending"].setdefault(target_key, [])

                if action == "add":
                    if target_key in fdata["friends"][my_key]:
                        await websocket.send(json.dumps({"type": "info", "msg": f"{target_nick} is already in your friends list."}))
                        continue
                    if len(fdata["pending"][target_key]) >= 100:
                        await websocket.send(json.dumps({"type": "error", "msg": f"{target_nick}'s pending request inbox is full."}))
                        continue
                    if target_key in fdata["pending"][my_key]:
                        _link_mutual_friends(fdata, my_key, target_key)
                        save_friends(fdata)
                        await websocket.send(json.dumps({"type": "info", "msg": f"You and {target_nick} are now friends!"}))
                        if target_key in online_by_key:
                            await online_by_key[target_key][0].send(json.dumps({"type": "info", "msg": f"[FRIEND] {client_state['nick']} accepted your friend request!"}))
                    else:
                        if my_key not in fdata["pending"][target_key]:
                            fdata["pending"][target_key].append(my_key)
                        save_friends(fdata)
                        await websocket.send(json.dumps({"type": "info", "msg": f"Friend request sent to {target_nick}."}))
                        if target_key in online_by_key:
                            await online_by_key[target_key][0].send(json.dumps({
                                "type": "info",
                                "msg": f"[FRIEND] {client_state['nick']} ({client_state['short_id']}) sent you a friend request! Type /accept {client_state['short_id']}"
                            }))

                elif action == "accept":
                    if target_key not in fdata["pending"][my_key]:
                        await websocket.send(json.dumps({"type": "error", "msg": f"No pending request from '{target_raw}'."}))
                        continue
                    _link_mutual_friends(fdata, my_key, target_key)
                    save_friends(fdata)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Accepted friend request from {target_nick}."}))
                    if target_key in online_by_key:
                        await online_by_key[target_key][0].send(json.dumps({"type": "info", "msg": f"[FRIEND] {client_state['nick']} accepted your friend request!"}))

                elif action == "remove":
                    fdata["friends"][my_key] = [k for k in fdata["friends"][my_key] if k != target_key]
                    fdata["friends"][target_key] = [k for k in fdata["friends"][target_key] if k != my_key]
                    fdata["pending"][my_key] = [k for k in fdata["pending"][my_key] if k != target_key]
                    save_friends(fdata)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Removed {target_nick} from friends."}))

                elif action == "whisper":
                    if target_key not in fdata["friends"][my_key]:
                        await websocket.send(json.dumps({"type": "error", "msg": "You can only whisper mutual friends."}))
                        continue
                    if target_key not in online_by_key:
                        await websocket.send(json.dumps({"type": "error", "msg": f"{target_nick} is currently offline."}))
                        continue
                    pm_text = re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("text", "")))[:300].strip()
                    if pm_text:
                        pkt = {"type": "whisper", "from_nick": client_state["nick"], "to_nick": target_nick, "text": pm_text}
                        await online_by_key[target_key][0].send(json.dumps({**pkt, "incoming": True}))
                        await websocket.send(json.dumps({**pkt, "incoming": False}))

                elif action == "goto":
                    if target_key not in fdata["friends"][my_key]:
                        await websocket.send(json.dumps({"type": "error", "msg": "You can only teleport to mutual friends."}))
                        continue
                    if target_key not in online_by_key:
                        await websocket.send(json.dumps({"type": "error", "msg": f"{target_nick} is currently offline."}))
                        continue
                    _, target_st = online_by_key[target_key]
                    dest_world = target_st["world"]
                    dest_meta = load_world_meta().get(dest_world, {})
                    if not can_visit_world(dest_world, dest_meta, client_state["pubkey"], client_state["role"]):
                        await websocket.send(json.dumps({"type": "error", "msg": f"Cannot teleport: {target_nick} is currently inside private world '{dest_world}'."}))
                        continue
                    fx, fy, fz = target_st["pos"]
                    if client_state["world"] != dest_world:
                        old_w = client_state["world"]
                        client_state["world"] = dest_world
                        await broadcast_world(old_w, {"type": "peer_leave", "short_id": client_state["short_id"]}, exclude_ws=websocket)
                        await send_world_transition(websocket, client_state, dest_world, dest_meta)
                    await websocket.send(json.dumps({
                        "type": "teleport",
                        "pos": [round(fx, 2), round(fy + 2.0, 2), round(fz + 3.0, 2)],
                        "msg": f"Teleported to friend {target_nick} in '{dest_world}'."
                    }))

            elif msg_type == "set_world_meta":
                target = sanitize_name(str(data.get("world") or client_state["world"]))
                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                async with UPLOAD_LOCK:
                    meta = load_world_meta()
                    w_entry = meta.get(target, {})
                    owner_key = w_entry.get("owner", "")
                    if not (client_state["role"] == "admin" or (bool(owner_key) and owner_key == client_state["pubkey"])):
                        await websocket.send(json.dumps({
                            "type": "error",
                            "msg": f"Permission denied: only the creator of '{target}' or an admin can edit its metadata or radio station."
                        }))
                        continue

                    if "description" in data and data["description"] is not None:
                        w_entry["description"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(data["description"])).strip()[:240]
                    if "title" in data and str(data["title"]).strip():
                        w_entry["title"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(data["title"])).strip()[:64]
                    if "tags" in data and isinstance(data["tags"], list):
                        w_entry["tags"] = [sanitize_name(str(t)) for t in data["tags"][:8] if str(t).strip()]
                    if "radio_url" in data and data["radio_url"] is not None:
                        r_url = str(data["radio_url"]).strip()[:300]
                        if r_url:
                            ok_url, err_msg = await is_safe_public_stream_url_async(r_url)
                            if not ok_url:
                                await websocket.send(json.dumps({"type": "error", "msg": err_msg}))
                                continue
                        w_entry["radio_url"] = r_url
                        if not r_url:
                            w_entry["radio_name"] = ""
                    if "radio_name" in data and data["radio_name"] is not None:
                        w_entry["radio_name"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(data["radio_name"])).strip()[:64]
                    w_entry["updated_at"] = int(time.time())
                    meta[target] = w_entry
                    save_world_meta(meta)

                    updated_wrl = apply_vrml_world_info(
                        read_wrl_safe(wrl_path),
                        w_entry.get("title", target),
                        w_entry.get("description", ""),
                        w_entry.get("tags", []),
                        w_entry.get("radio_url", ""),
                        w_entry.get("radio_name", ""),
                    )
                    _atomic_write_text(wrl_path, updated_wrl)

                await broadcast_world(target, {
                    "type": "world_desc_updated",
                    "world": target,
                    "title": w_entry.get("title", target),
                    "description": w_entry.get("description", ""),
                    "tags": w_entry.get("tags", []),
                    "radio_url": w_entry.get("radio_url", ""),
                    "radio_name": w_entry.get("radio_name", ""),
                })
                await websocket.send(json.dumps({"type": "info", "msg": f"Updated metadata for your world '{target}'."}))

            elif msg_type == "world_backup":
                action = str(data.get("action", ""))
                target = sanitize_name(str(data.get("world") or client_state["world"]))
                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                meta = load_world_meta()
                w_entry = meta.get(target, {})
                owner_key = w_entry.get("owner", "")
                if not can_edit_world(target, w_entry, client_state["pubkey"], client_state["role"]):
                    await websocket.send(json.dumps({
                        "type": "error",
                        "msg": f"Permission denied: only the creator or co-builders of '{target}' can backup, restore, or export it."
                    }))
                    continue

                if action == "create":
                    async with UPLOAD_LOCK:
                        snap_name = create_world_snapshot(target, label=str(data.get("label", "")).strip())
                    if not snap_name:
                        await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' no longer exists."}))
                    else:
                        await websocket.send(json.dumps({"type": "info", "msg": f"Created backup snapshot: {snap_name}"}))
                elif action == "list":
                    await websocket.send(json.dumps({"type": "world_backups_list", "world": target, "backups": list_world_snapshots(target)}))
                elif action == "restore":
                    raw_file = str(data.get("file", "")).strip()
                    if not re.fullmatch(r"[A-Za-z0-9_-]+\.wrl", raw_file):
                        await websocket.send(json.dumps({"type": "error", "msg": "Invalid snapshot filename."}))
                        continue
                    backup_root = (BACKUPS_DIR / target).resolve()
                    snap_wrl = (backup_root / raw_file).resolve()
                    if snap_wrl.parent != backup_root or not snap_wrl.exists():
                        await websocket.send(json.dumps({"type": "error", "msg": f"Backup '{raw_file}' not found for '{target}'."}))
                        continue

                    async with UPLOAD_LOCK:
                        create_world_snapshot(target, label="pre_restore")
                        restored_text = sanitize_vrml_script_nodes(read_wrl_safe(snap_wrl))
                        snap_meta_path = snap_wrl.with_suffix(".meta.json")
                        if snap_meta_path.exists():
                            try:
                                restored_meta = json.loads(snap_meta_path.read_text(encoding="utf-8"))
                            except Exception:
                                restored_meta = dict(w_entry)
                            if owner_key:
                                restored_meta["owner"] = owner_key
                            if "ratings" not in restored_meta and isinstance(w_entry.get("ratings"), dict):
                                restored_meta["ratings"] = w_entry["ratings"]
                            restored_meta["updated_at"] = int(time.time())
                            r_url = str(restored_meta.get("radio_url", "")).strip()
                            if r_url and not (await is_safe_public_stream_url_async(r_url))[0]:
                                restored_meta["radio_url"] = ""
                                restored_meta["radio_name"] = ""
                            meta[target] = restored_meta
                            save_world_meta(meta)
                            w_entry = restored_meta

                        restored_text = apply_vrml_world_info(
                            restored_text,
                            w_entry.get("title", target),
                            w_entry.get("description", ""),
                            w_entry.get("tags", []),
                            w_entry.get("radio_url", ""),
                            w_entry.get("radio_name", ""),
                        )
                        if w_entry.get("spawn_pos"):
                            restored_text = apply_vrml_spawn_point(restored_text, w_entry["spawn_pos"], float(w_entry.get("spawn_yaw", 0.0) or 0.0))
                        _atomic_write_text(wrl_path, restored_text)

                    wd_res = {
                        "type": "world_data",
                        "world": target,
                        "wrl_hash": compute_wrl_sha256(restored_text),
                        "description": w_entry.get("description", ""),
                        "radio_url": w_entry.get("radio_url", ""),
                        "radio_name": w_entry.get("radio_name", ""),
                        "wrl": restored_text,
                        "in_place": True,
                    }
                    if w_entry.get("spawn_pos"):
                        wd_res["spawn_pos"] = w_entry["spawn_pos"]
                        wd_res["spawn_yaw"] = float(w_entry.get("spawn_yaw", 0.0) or 0.0)
                    await broadcast_world(target, wd_res)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Restored '{target}' from {raw_file}."}))
                elif action == "export":
                    await websocket.send(json.dumps({
                        "type": "world_export_data",
                        "world": target,
                        "wrl": read_wrl_safe(wrl_path),
                        "meta": w_entry,
                        "save_path": str(data.get("save_path", "")).strip(),
                    }))

            elif msg_type == "federation":
                action = str(data.get("action", "list"))
                fed = load_federation()

                if action == "list":
                    peers_map = fed.get("peers", {})
                    urls = list(peers_map.keys())
                    statuses = await asyncio.gather(*(probe_federated_node(u) for u in urls)) if urls else []
                    out_peers = [
                        {
                            "url": u,
                            "label": peers_map[u].get("label", u),
                            "default_world": peers_map[u].get("default_world", "hub"),
                            "online": st.get("online", False),
                            "remote_name": st.get("node_name", ""),
                            "online_count": st.get("online_count", 0),
                            "worlds": st.get("worlds", []),
                        }
                        for u, st in zip(urls, statuses)
                    ]
                    await websocket.send(json.dumps({
                        "type": "federation_list",
                        "node_name": fed.get("node_name", "cyberX87 Node"),
                        "node_desc": fed.get("node_desc", ""),
                        "peers": out_peers,
                    }))
                    continue

                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required for federation settings."}))
                    continue

                if action == "link":
                    ok_u, norm_url, err_u = validate_ws_peer_url(str(data.get("url", "")))
                    if not ok_u:
                        await websocket.send(json.dumps({"type": "error", "msg": err_u}))
                        continue
                    label = sanitize_name(str(data.get("label", ""))) or sanitize_name(urllib.parse.urlsplit(norm_url).hostname or "node")
                    def_w = sanitize_name(str(data.get("default_world", "hub")))
                    async with UPLOAD_LOCK:
                        fed = load_federation()
                        fed["peers"][norm_url] = {"label": label, "default_world": def_w, "added_at": int(time.time())}
                        save_federation(fed)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Linked server '{label}' ({norm_url}) - jump anytime with /hop {label}."}))

                elif action == "unlink":
                    ok_u, norm_url, _ = validate_ws_peer_url(str(data.get("url", "")))
                    raw_target = str(data.get("url", "")).strip()
                    async with UPLOAD_LOCK:
                        fed = load_federation()
                        removed_key = None
                        for k, v in list(fed.get("peers", {}).items()):
                            if k == norm_url or k == raw_target or v.get("label", "").lower() == raw_target.lower():
                                removed_key = k
                                fed["peers"].pop(k, None)
                                break
                        if not removed_key:
                            await websocket.send(json.dumps({"type": "error", "msg": f"Federated peer '{raw_target}' not found."}))
                            continue
                        save_federation(fed)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Unlinked server ({removed_key})."}))

                elif action == "set_identity":
                    new_name = re.sub(r"[\x00-\x1f\x7f]", " ", str(data.get("node_name", ""))).strip()[:48]
                    if not new_name:
                        await websocket.send(json.dumps({"type": "error", "msg": "Node name cannot be empty."}))
                        continue
                    fed = load_federation()
                    fed["node_name"] = new_name
                    if data.get("node_desc") is not None:
                        fed["node_desc"] = re.sub(r"[\x00-\x1f\x7f]", " ", str(data["node_desc"])).strip()[:160]
                    save_federation(fed)
                    await websocket.send(json.dumps({"type": "info", "msg": f"Updated node identity to '{new_name}'."}))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        IP_CONNECTIONS[remote_ip] = max(0, IP_CONNECTIONS.get(remote_ip, 1) - 1)
        if IP_CONNECTIONS.get(remote_ip, 0) == 0:
            IP_CONNECTIONS.pop(remote_ip, None)
        if websocket in CLIENTS:
            st = CLIENTS.pop(websocket)
            try:
                await broadcast_world(st["world"], {"type": "peer_leave", "short_id": st["short_id"]})
            except Exception:
                pass


async def main():
    init_storage()
    print(f"[*] VRML97 Metaverse Server running on ws://{HOST}:{PORT}")
    print(f"[*] First user to connect is automatically granted ADMIN rights.")
    async with websockets.serve(
        handler,
        HOST,
        PORT,
        max_size=MAX_FILE_SIZE + 65536,
        ping_interval=20,
        ping_timeout=20,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
