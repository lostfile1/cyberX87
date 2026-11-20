import asyncio
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pygame
from pygame.locals import *
from OpenGL.GL import *
from OpenGL.GLU import *
import websockets
from nacl.signing import SigningKey

# --- Configuration & Local Key Storage ---
KEY_FILE = Path.home() / ".cyberx87_key.hex"
FRIENDS_CACHE_FILE = Path.home() / ".cyberx87_friends.json"
SERVERS_FILE = Path.home() / ".cyberx87_servers.json"
PROFILE_CACHE_FILE = Path.home() / ".cyberx87_profile.json"
WORLD_CACHE_DIR = Path.home() / ".cyberx87_cache"
WORLD_CACHE_INDEX = WORLD_CACHE_DIR / "index.json"
AVATAR_CACHE_DIR = WORLD_CACHE_DIR / "avatars"
PLUGINS_DIR = Path("./plugins")
EXPORTS_DIR = Path("./exports")
DEFAULT_SERVER = "ws://localhost:8765"

WINDOW_W, WINDOW_H = 1280, 760
MAX_AVATAR_CACHE = 48
MAX_AVATAR_REQ_TRACK = 256
MAX_DISK_CACHE_BYTES = 128 * 1024 * 1024  # 128 MB local world cache cap


def _write_secure_file(path: Path, text: str, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _clean_ident(raw: str, max_len: int = 32, fallback: str = "") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", str(raw or ""))[:max_len]
    return cleaned or fallback


def _clean_hex_color(raw: str, fallback: str = "#00ffcc") -> str:
    col = str(raw or "").strip().lower()
    return col if re.fullmatch(r"#[0-9a-fA-F]{6}", col) else fallback


def load_or_create_key() -> SigningKey:
    if KEY_FILE.exists():
        try:
            os.chmod(KEY_FILE, 0o600)
        except OSError:
            pass
        raw_hex = KEY_FILE.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", raw_hex):
            raise ValueError(f"Corrupted Ed25519 key file at {KEY_FILE}")
        return SigningKey(bytes.fromhex(raw_hex))
    sk = SigningKey.generate()
    _write_secure_file(KEY_FILE, sk.encode().hex(), mode=0o600)
    return sk


def load_local_profile() -> dict:
    default_prof = {"nick": "NetRunner", "color": "#00ffcc", "avatar": "default"}
    if not PROFILE_CACHE_FILE.exists():
        return default_prof
    try:
        data = json.loads(PROFILE_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "nick": _clean_ident(data.get("nick", ""), 24, "NetRunner"),
                "color": _clean_hex_color(data.get("color", "#00ffcc"), "#00ffcc"),
                "avatar": _clean_ident(data.get("avatar", "default"), 32, "default"),
            }
    except Exception:
        pass
    return default_prof


def save_local_profile(nick: str, color: str, avatar: str):
    _write_secure_file(
        PROFILE_CACHE_FILE,
        json.dumps({
            "nick": _clean_ident(nick, 24, "NetRunner"),
            "color": _clean_hex_color(color, "#00ffcc"),
            "avatar": _clean_ident(avatar, 32, "default"),
        }, indent=2),
        mode=0o600,
    )


def load_server_directory() -> dict:
    if not SERVERS_FILE.exists():
        _write_secure_file(SERVERS_FILE, json.dumps({"bookmarks": {}, "recent": []}, indent=2), mode=0o600)
    try:
        data = json.loads(SERVERS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {"bookmarks": {}, "recent": []}
    except Exception:
        data = {"bookmarks": {}, "recent": []}
    data.setdefault("bookmarks", {})
    data.setdefault("recent", [])
    return data


def save_server_directory(data: dict):
    _write_secure_file(SERVERS_FILE, json.dumps(data, indent=2), mode=0o600)


def store_cached_avatar_wrl(avatar_name: str, wrl_text: str):
    clean_n = _clean_ident(avatar_name, 32)
    if not clean_n or clean_n.lower() == "default" or not wrl_text or "\x00" in wrl_text:
        return
    first_line = wrl_text.splitlines()[0].strip() if wrl_text.splitlines() else ""
    if not re.match(r"^#VRML\s+V(1\.0|2\.0)\s+(ascii|utf8)\b", first_line):
        return
    AVATAR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(AVATAR_CACHE_DIR, 0o700)
    except OSError:
        pass
    _write_secure_file(AVATAR_CACHE_DIR / f"{clean_n}.wrl", wrl_text, mode=0o600)


def load_cached_avatar_wrl(avatar_name: str) -> str:
    clean_n = _clean_ident(avatar_name, 32)
    if not clean_n or clean_n.lower() == "default":
        return ""
    fp = (AVATAR_CACHE_DIR / f"{clean_n}.wrl").resolve()
    if fp.parent != AVATAR_CACHE_DIR.resolve() or not fp.is_file():
        return ""
    return fp.read_text(encoding="utf-8", errors="replace")


def normalize_ws_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if not url:
        return ""
    return url if url.startswith(("ws://", "wss://")) else f"ws://{url}"


def _server_cache_slug(server_url: str) -> str:
    parsed = urllib.parse.urlsplit(normalize_ws_url(server_url) or "ws://local:8765")
    host = re.sub(r"[^a-zA-Z0-9._-]", "_", (parsed.hostname or "local").lower())
    port = parsed.port or (443 if parsed.scheme == "wss" else 8765)
    return f"{host}_{port}"[:48]


def _load_cache_index() -> dict:
    WORLD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(WORLD_CACHE_DIR, 0o700)
    except OSError:
        pass
    if not WORLD_CACHE_INDEX.exists():
        _write_secure_file(WORLD_CACHE_INDEX, json.dumps({"entries": {}}, indent=2), mode=0o600)
    try:
        data = json.loads(WORLD_CACHE_INDEX.read_text(encoding="utf-8"))
    except Exception:
        data = {"entries": {}}
    data.setdefault("entries", {})
    return data


def _save_cache_index(data: dict):
    _write_secure_file(WORLD_CACHE_INDEX, json.dumps(data, indent=2), mode=0o600)


def _prune_disk_world_cache(idx_data: dict):
    entries = idx_data.get("entries", {})
    total_bytes = sum(int(e.get("bytes", 0)) for e in entries.values())
    if total_bytes <= MAX_DISK_CACHE_BYTES:
        return
    for key, meta in sorted(entries.items(), key=lambda kv: int(kv[1].get("accessed_at", 0))):
        if total_bytes <= MAX_DISK_CACHE_BYTES:
            break
        fp = WORLD_CACHE_DIR / str(meta.get("file", ""))
        try:
            if fp.is_file():
                fp.unlink(missing_ok=True)
        except Exception:
            pass
        total_bytes -= int(meta.get("bytes", 0))
        entries.pop(key, None)


def store_cached_world(
    server_url: str,
    world_name: str,
    wrl_text: str,
    wrl_hash: str = "",
    desc: str = "",
    radio_url: str = "",
    radio_name: str = "",
    spawn_pos: list[float] | None = None,
    spawn_yaw: float = 0.0,
) -> str:
    if not wrl_text:
        return ""
    digest = hashlib.sha256(wrl_text.encode("utf-8", errors="replace")).hexdigest()
    actual_hash = wrl_hash.lower() if re.fullmatch(r"[0-9a-f]{64}", (wrl_hash or "").lower()) else digest

    slug = _server_cache_slug(server_url)
    safe_world = _clean_ident(world_name, 32, "world")
    fname = f"{slug}__{safe_world}.wrl"
    _write_secure_file(WORLD_CACHE_DIR / fname, wrl_text, mode=0o600)

    idx_data = _load_cache_index()
    now_ts = int(time.time())
    entry = {
        "server": normalize_ws_url(server_url),
        "slug": slug,
        "world": safe_world,
        "file": fname,
        "sha256": actual_hash,
        "bytes": len(wrl_text.encode("utf-8", errors="replace")),
        "description": desc,
        "radio_url": radio_url,
        "radio_name": radio_name,
        "cached_at": now_ts,
        "accessed_at": now_ts,
    }
    if spawn_pos and len(spawn_pos) >= 3:
        entry["spawn_pos"] = [float(spawn_pos[0]), float(spawn_pos[1]), float(spawn_pos[2])]
        entry["spawn_yaw"] = float(spawn_yaw)
    idx_data["entries"][f"{slug}/{safe_world}"] = entry
    _prune_disk_world_cache(idx_data)
    _save_cache_index(idx_data)
    return actual_hash


def get_cached_world(server_url: str, world_name: str) -> dict | None:
    slug = _server_cache_slug(server_url)
    safe_world = _clean_ident(world_name, 32, "world")
    idx_data = _load_cache_index()
    entries = idx_data.get("entries", {})

    entry = entries.get(f"{slug}/{safe_world}")
    if entry is None:
        candidates = [e for e in entries.values() if e.get("world", "").lower() == safe_world.lower()]
        if candidates:
            candidates.sort(key=lambda e: int(e.get("accessed_at", 0)), reverse=True)
            entry = candidates[0]

    if not entry:
        return None

    fp = (WORLD_CACHE_DIR / str(entry.get("file", ""))).resolve()
    if fp.parent != WORLD_CACHE_DIR.resolve() or not fp.is_file():
        return None

    wrl_text = fp.read_text(encoding="utf-8", errors="replace")
    actual_hash = hashlib.sha256(wrl_text.encode("utf-8", errors="replace")).hexdigest()
    if entry.get("sha256") and actual_hash != entry["sha256"]:
        return None

    entry["accessed_at"] = int(time.time())
    _save_cache_index(idx_data)
    return {**entry, "wrl": wrl_text, "sha256": actual_hash}


def list_cached_worlds() -> list[dict]:
    entries = list(_load_cache_index().get("entries", {}).values())
    entries.sort(key=lambda e: int(e.get("accessed_at", 0)), reverse=True)
    return entries


def clear_cached_worlds() -> int:
    idx_data = _load_cache_index()
    entries = idx_data.get("entries", {})
    count = len(entries)
    for meta in entries.values():
        fp = WORLD_CACHE_DIR / str(meta.get("file", ""))
        try:
            if fp.is_file():
                fp.unlink(missing_ok=True)
        except Exception:
            pass
    _save_cache_index({"entries": {}})
    return count


def is_safe_radio_url(url: str) -> tuple[bool, str]:
    if not url or len(url) > 300:
        return False, "Radio stream URL is empty or too long."
    if url.startswith("-") or any(ch in url for ch in ('"', "'", "\n", "\r", "\x00", " ")):
        return False, "Radio stream URL contains unsafe characters."
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return False, "Malformed radio stream URL."
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "Radio stream URL must start with http:// or https://."
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return False, "Blocked local/mDNS radio stream host."
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve radio stream host '{host}'."
    for info in infos:
        try:
            ip_obj = ipaddress.ip_address(info[4][0])
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False, "Blocked private/loopback IP address for radio stream."
        except ValueError:
            return False, "Invalid IP address for radio stream."
    return True, ""


def resolve_safe_export_path(world_name: str, custom_path: str) -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_world = _clean_ident(world_name, 32, "world")
    if not custom_path:
        return (EXPORTS_DIR / f"{safe_world}.wrl").resolve()
    raw = custom_path.strip('"').strip("'")
    if "\x00" in raw or ".." in Path(raw).parts:
        return (EXPORTS_DIR / f"{safe_world}.wrl").resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = EXPORTS_DIR / candidate
    if raw.endswith(("/", "\\")) or (candidate.exists() and candidate.is_dir()):
        candidate = candidate / f"{safe_world}.wrl"
    elif not candidate.suffix:
        candidate = candidate.with_suffix(".wrl")
    return candidate.resolve()


def load_x87_plugins(plugin_dir: Path = PLUGINS_DIR) -> dict:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    sample = plugin_dir / "starter.x87"
    if not sample.exists():
        sample.write_text(
            "# cyberX87 Human-Readable Command Plugin\n"
            "COMMAND: /shrug\n"
            "DESC: Send an ASCII shrug to chat\n"
            "ACTION: chat\n"
            "VALUE: ¯\\_(ツ)_/¯\n"
            "---\n"
            "COMMAND: /flip\n"
            "DESC: Do a spin + jump combo emote\n"
            "ACTION: multi\n"
            "STEPS:\n"
            "  emote spin\n"
            "  emote jump\n"
            "  chat * {nick} lands a cyber-backflip! *\n"
            "---\n"
            "COMMAND: /home\n"
            "DESC: Jump back to Neo-Genesis Hub\n"
            "ACTION: world\n"
            "VALUE: hub\n",
            encoding="utf-8",
        )

    commands = {}
    for file_path in sorted(plugin_dir.glob("*.x87")):
        raw = file_path.read_text(encoding="utf-8", errors="replace")
        for block in raw.split("---"):
            entry = {"file": file_path.name, "steps": []}
            in_steps = False
            for line in block.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.upper().startswith("STEPS:"):
                    in_steps = True
                    continue
                if in_steps and (line.startswith(" ") or line.startswith("\t")):
                    entry["steps"].append(stripped)
                    continue
                in_steps = False
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    entry[k.strip().lower()] = v.strip()

            cmd_name = entry.get("command", "").lower()
            if cmd_name:
                commands[cmd_name if cmd_name.startswith("/") else "/" + cmd_name] = entry
    return commands


def _safe_floats(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", text)]


def _safe_ints(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"[-+]?\d+", text)]


class RadioPlayer:
    def __init__(self):
        self.url = ""
        self.name = ""
        self.muted = False
        self.proc = None
        self._last_restart_try = 0.0

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def set_stream(self, url: str, name: str = ""):
        url = (url or "").strip()
        name = (name or "").strip()
        if url and not is_safe_radio_url(url)[0]:
            self.url = ""
            self.name = ""
            self.stop()
            return
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
        if url == self.url and name == self.name and (self.proc is not None or self.muted or not url):
            return
        self.url = url
        self.name = name or url
        self.stop()
        if self.url and not self.muted:
            self._start_proc()

    def check_health(self, now: float):
        if not self.url or self.muted:
            return
        if self.proc is not None and self.proc.poll() is None:
            return
        self.proc = None
        if now - self._last_restart_try >= 6.0:
            self._last_restart_try = now
            self._start_proc()

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        if self.muted:
            self.stop()
        elif self.url:
            self._last_restart_try = time.time()
            self._start_proc()
        return self.muted

    def _start_proc(self):
        if not self.url or not is_safe_radio_url(self.url)[0]:
            return
        candidates = [
            (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", self.url], "ffplay"),
            (["mpv", "--no-video", "--really-quiet", "--", self.url], "mpv"),
            (["cvlc", "--intf", "dummy", "--quiet", "--http-reconnect", "--", self.url], "cvlc"),
        ]
        for cmd, bin_name in candidates:
            if shutil.which(bin_name):
                try:
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False)
                    return
                except Exception:
                    pass


def _axis_angle_to_quat(ax: float, ay: float, az: float, angle_rad: float) -> tuple[float, float, float, float]:
    norm = math.sqrt(ax * ax + ay * ay + az * az)
    if norm < 1e-8 or abs(angle_rad) < 1e-8:
        return (0.0, 0.0, 0.0, 1.0)
    nx, ny, nz = ax / norm, ay / norm, az / norm
    s = math.sin(angle_rad * 0.5)
    return (nx * s, ny * s, nz * s, math.cos(angle_rad * 0.5))


def _quat_mul(q1, q2):
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return (
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    )


def _quat_rotate_vec(q, v):
    qx, qy, qz, qw = q
    if abs(qx) < 1e-8 and abs(qy) < 1e-8 and abs(qz) < 1e-8:
        return v
    vx, vy, vz = v
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


def _quat_to_gl_axis_angle(q):
    x, y, z, w = q
    w = max(-1.0, min(1.0, w))
    angle_deg = math.degrees(2.0 * math.acos(w))
    s = math.sqrt(max(0.0, 1.0 - w * w))
    if s < 1e-6 or angle_deg < 1e-4:
        return (0.0, 1.0, 0.0, 0.0)
    return (x / s, y / s, z / s, angle_deg)


def _compute_poly_normal(pts: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    if len(pts) < 3:
        return (0.0, 1.0, 0.0)
    ux, uy, uz = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1], pts[1][2] - pts[0][2]
    vx, vy, vz = pts[2][0] - pts[0][0], pts[2][1] - pts[0][1], pts[2][2] - pts[0][2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-8:
        return (0.0, 1.0, 0.0)
    return (nx / length, ny / length, nz / length)


def parse_vrml(wrl_text: str):
    scene = {
        "bg": (0.04, 0.01, 0.12),
        "fog": (0.05, 0.02, 0.15),
        "objects": [],
        "has_floor": False,
        "raw_len": len(wrl_text or ""),
        "raw_preview": "",
        "world_info_desc": "",
        "radio_url": "",
        "radio_name": "",
        "spawn_pos": None,
        "spawn_yaw": 0.0,
    }
    if not wrl_text or not wrl_text.strip():
        return scene

    is_vrml1 = "#VRML V1.0" in wrl_text[:64]
    clean = re.sub(r"#.*", "", wrl_text).replace(",", " ")
    non_empty_lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    scene["raw_preview"] = " | ".join(non_empty_lines[:5])[:160]

    wi_m = re.search(r'WorldInfo\s*\{[^}]*info\s*\[\s*"([^"]+)"', clean, re.S)
    if wi_m and not wi_m.group(1).startswith(("tags:", "radio:")):
        scene["world_info_desc"] = wi_m.group(1).strip()

    ac_m = re.search(r'AudioClip\s*\{([^}]*)\}', clean, re.S)
    if ac_m:
        u_m = re.search(r'url\s*\[\s*"(https?://[^"]+)"', ac_m.group(1))
        d_m = re.search(r'description\s*"([^"]+)"', ac_m.group(1))
        if u_m:
            scene["radio_url"] = u_m.group(1).strip()
            scene["radio_name"] = d_m.group(1).strip() if d_m else scene["radio_url"]

    vp_m = re.search(r"(?:DEF\s+WORLD_SPAWN\s+)?Viewpoint\s*\{([^}]*)\}", clean, re.S)
    if vp_m:
        pm = re.search(r"position\s+([-+0-9.eE\s]+)", vp_m.group(1))
        om = re.search(r"orientation\s+([-+0-9.eE\s]+)", vp_m.group(1))
        if pm:
            pvals = _safe_floats(pm.group(1))
            if len(pvals) >= 3:
                scene["spawn_pos"] = [pvals[0], pvals[1], pvals[2]]
        if om:
            ovals = _safe_floats(om.group(1))
            if len(ovals) >= 4:
                scene["spawn_yaw"] = math.degrees(ovals[3]) % 360.0

    for key, pat in (("bg", r"Background\s*\{[^}]*skyColor\s*\[\s*([^\]]+)\]"), ("fog", r"Fog\s*\{[^}]*color\s+([^\n\}]+)")):
        m = re.search(pat, clean, re.S if key == "bg" else 0)
        if m:
            nums = _safe_floats(m.group(1))
            if len(nums) >= 3:
                scene[key] = (nums[0], nums[1], nums[2])

    tokens = re.findall(r'\{|\}|\[|\]|"[^"]*"|[^\s\{\}\[\],]+', clean)

    def skip_balanced(idx, open_tok="{", close_tok="}"):
        if idx >= len(tokens) or tokens[idx] != open_tok:
            return idx
        depth = 1
        idx += 1
        while idx < len(tokens) and depth > 0:
            if tokens[idx] == open_tok:
                depth += 1
            elif tokens[idx] == close_tok:
                depth -= 1
            idx += 1
        return idx

    def read_floats_at(idx, count):
        out = []
        while idx < len(tokens) and len(out) < count:
            try:
                out.append(float(tokens[idx]))
                idx += 1
            except ValueError:
                break
        return out

    CONTAINER_NODES = {"Group", "Collision", "Billboard", "Switch", "LOD", "Inline", "Anchor", "WWWAnchor"}
    defs_table = {}

    def parse_shape_block(block: str, pos, rot, scale, def_label: str = ""):
        diff = (0.45, 0.65, 0.85)
        emis = (0.05, 0.08, 0.12)
        transp = 0.0
        for target_attr, pat in (("diff", r"diffuseColor\s+([-+0-9.eE\s]+)"), ("emis", r"emissiveColor\s+([-+0-9.eE\s]+)")):
            m = re.search(pat, block)
            if m:
                vals = _safe_floats(m.group(1))
                if len(vals) >= 3:
                    if target_attr == "diff":
                        diff = (vals[0], vals[1], vals[2])
                    else:
                        emis = (vals[0], vals[1], vals[2])
        tm = re.search(r"transparency\s+([-+0-9.eE]+)", block)
        if tm and _safe_floats(tm.group(1)):
            transp = _safe_floats(tm.group(1))[0]

        base_obj = {"pos": pos, "rot": rot, "scale": scale, "diff": diff, "emis": emis, "alpha": max(0.25, 1.0 - transp), "def_name": def_label}

        if re.search(r"\b(Box|Cube)\b", block):
            bm = re.search(r"\b(?:Box|Cube)\s*\{([^}]*)\}", block)
            vals = _safe_floats(bm.group(1)) if bm else []
            sz = (vals[0], vals[1], vals[2]) if len(vals) >= 3 else (2.0, 2.0, 2.0)
            if sz[0] >= 30 and sz[2] >= 30 and sz[1] <= 1.0:
                scene["has_floor"] = True
            return {**base_obj, "kind": "box", "size": sz}
        if re.search(r"\bCylinder\b", block):
            cm = re.search(r"\bCylinder\s*\{([^}]*)\}", block)
            cbody = cm.group(1) if cm else ""
            rm, hm = re.search(r"radius\s+([^\s\}]+)", cbody), re.search(r"height\s+([^\s\}]+)", cbody)
            return {**base_obj, "kind": "cylinder", "radius": _safe_floats(rm.group(1))[0] if rm else 1.0, "height": _safe_floats(hm.group(1))[0] if hm else 2.0}
        if re.search(r"\bCone\b", block):
            cm = re.search(r"\bCone\s*\{([^}]*)\}", block)
            cbody = cm.group(1) if cm else ""
            rm, hm = re.search(r"bottomRadius\s+([^\s\}]+)", cbody), re.search(r"height\s+([^\s\}]+)", cbody)
            return {**base_obj, "kind": "cone", "radius": _safe_floats(rm.group(1))[0] if rm else 1.0, "height": _safe_floats(hm.group(1))[0] if hm else 2.0}
        if re.search(r"\bSphere\b", block):
            sm = re.search(r"\bSphere\s*\{([^}]*)\}", block)
            rm = re.search(r"radius\s+([^\s\}]+)", sm.group(1) if sm else "")
            return {**base_obj, "kind": "sphere", "radius": _safe_floats(rm.group(1))[0] if rm else 1.0}
        if re.search(r"\b(IndexedFaceSet|IndexedLineSet)\b", block):
            kind_name = "faces" if "IndexedFaceSet" in block else "lines"
            pm = re.search(r"point\s*\[([^\]]+)\]", block)
            im = re.search(r"coordIndex\s*\[([^\]]+)\]", block)
            if pm:
                raw_pts = _safe_floats(pm.group(1))
                pts = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                idxs = _safe_ints(im.group(1)) if im else list(range(len(pts)))
                return {**base_obj, "kind": kind_name, "pts": pts, "idxs": idxs}
        if re.search(r"\bElevationGrid\b", block):
            xd_m, zd_m, hm = re.search(r"xDimension\s+(\d+)", block), re.search(r"zDimension\s+(\d+)", block), re.search(r"height\s*\[([^\]]+)\]", block)
            xs_m, zs_m = re.search(r"xSpacing\s+([^\s\}]+)", block), re.search(r"zSpacing\s+([^\s\}]+)", block)
            if xd_m and zd_m and hm:
                scene["has_floor"] = True
                return {**base_obj, "kind": "grid", "xd": int(xd_m.group(1)), "zd": int(zd_m.group(1)), "xs": _safe_floats(xs_m.group(1))[0] if xs_m else 1.0, "zs": _safe_floats(zs_m.group(1))[0] if zs_m else 1.0, "heights": _safe_floats(hm.group(1))}
        if re.search(r"\bExtrusion\b", block):
            sp_m, cs_m = re.search(r"spine\s*\[([^\]]+)\]", block), re.search(r"crossSection\s*\[([^\]]+)\]", block)
            raw_sp = _safe_floats(sp_m.group(1)) if sp_m else []
            spine_pts = [(raw_sp[n], raw_sp[n + 1], raw_sp[n + 2]) for n in range(0, len(raw_sp) - 2, 3)] if len(raw_sp) >= 6 else [(0, 0, 0), (0, 2, 0)]
            raw_cs = _safe_floats(cs_m.group(1)) if cs_m else []
            cs_pts = [(raw_cs[n], raw_cs[n + 1]) for n in range(0, len(raw_cs) - 1, 2)] if len(raw_cs) >= 4 else [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
            return {**base_obj, "kind": "extrusion", "spine": spine_pts, "cross": cs_pts}
        if re.search(r"\bText\b", block):
            return {**base_obj, "kind": "box", "size": (3.2, 0.65, 0.15)}
        return None

    def walk_vrml1(i, state):
        while i < len(tokens):
            tok = tokens[i]
            if tok == "}":
                return i + 1
            if tok == "DEF" and i + 2 < len(tokens):
                i += 2
                tok = tokens[i]
            if tok == "Separator" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                i = walk_vrml1(i + 2, {"pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "pts": list(state["pts"])})
                continue
            if i + 1 < len(tokens) and tokens[i + 1] == "{":
                end_idx = skip_balanced(i + 1, "{", "}")
                blk = " ".join(tokens[i + 2 : end_idx - 1])
                if tok == "Translation":
                    vals = _safe_floats(blk)
                    if len(vals) >= 3:
                        p, s = state["pos"], state["scale"]
                        state["pos"] = (p[0] + vals[0] * s[0], p[1] + vals[1] * s[1], p[2] + vals[2] * s[2])
                elif tok == "Scale":
                    vals = _safe_floats(blk)
                    if len(vals) >= 3:
                        s = state["scale"]
                        state["scale"] = (s[0] * vals[0], s[1] * vals[1], s[2] * vals[2])
                elif tok == "Rotation":
                    vals = _safe_floats(blk)
                    if len(vals) >= 4:
                        state["rot"] = (vals[0], vals[1], vals[2], math.degrees(vals[3]))
                elif tok == "Material":
                    for attr, pat in (("diff", r"diffuseColor\s+([-+0-9.eE\s]+)"), ("emis", r"emissiveColor\s+([-+0-9.eE\s]+)")):
                        m = re.search(pat, blk)
                        if m and len(_safe_floats(m.group(1))) >= 3:
                            v = _safe_floats(m.group(1))
                            state[attr] = (v[0], v[1], v[2])
                elif tok == "Coordinate3":
                    pm = re.search(r"point\s*\[([^\]]+)\]", blk)
                    if pm:
                        raw_pts = _safe_floats(pm.group(1))
                        state["pts"] = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                elif tok == "Cube":
                    w = _safe_floats(re.search(r"width\s+([^\s\}]+)", blk).group(1))[0] if "width" in blk else 2.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    d = _safe_floats(re.search(r"depth\s+([^\s\}]+)", blk).group(1))[0] if "depth" in blk else 2.0
                    scene["objects"].append({"kind": "box", "size": (w, h, d), "pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0, "def_name": ""})
                elif tok == "Sphere":
                    r = _safe_floats(re.search(r"radius\s+([^\s\}]+)", blk).group(1))[0] if "radius" in blk else 1.0
                    scene["objects"].append({"kind": "sphere", "radius": r, "pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0, "def_name": ""})
                elif tok == "Cylinder":
                    r = _safe_floats(re.search(r"radius\s+([^\s\}]+)", blk).group(1))[0] if "radius" in blk else 1.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    scene["objects"].append({"kind": "cylinder", "radius": r, "height": h, "pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0, "def_name": ""})
                elif tok == "Cone":
                    r = _safe_floats(re.search(r"bottomRadius\s+([^\s\}]+)", blk).group(1))[0] if "bottomRadius" in blk else 1.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    scene["objects"].append({"kind": "cone", "radius": r, "height": h, "pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0, "def_name": ""})
                elif tok == "IndexedFaceSet" and state["pts"]:
                    im = re.search(r"coordIndex\s*\[([^\]]+)\]", blk)
                    idxs = _safe_ints(im.group(1)) if im else list(range(len(state["pts"])))
                    scene["objects"].append({"kind": "faces", "pts": state["pts"], "idxs": idxs, "pos": state["pos"], "rot": state["rot"], "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0, "def_name": ""})
                i = end_idx
                continue
            i += 1
        return i

    def walk_vrml2(i, pos=(0.0, 0.0, 0.0), rot_quat=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0), record_list=None, active_def=""):
        while i < len(tokens):
            tok = tokens[i]
            if tok == "}":
                return i + 1
            if tok in ("[", "]"):
                i += 1
                continue
            if tok in ("PROTO", "EXTERNPROTO"):
                i += 2
                if i < len(tokens) and tokens[i] == "[":
                    i = skip_balanced(i, "[", "]")
                if i < len(tokens) and tokens[i] == "{":
                    i = skip_balanced(i, "{", "}")
                continue
            if tok == "ROUTE":
                i += 4
                continue

            def_name = None
            if tok == "DEF" and i + 2 < len(tokens):
                def_name = tokens[i + 1]
                i += 2
                tok = tokens[i]

            inherited_def = def_name or active_def

            if tok == "USE" and i + 1 < len(tokens):
                for template in defs_table.get(tokens[i + 1], []):
                    scaled_local = (template["local_pos"][0] * scale[0], template["local_pos"][1] * scale[1], template["local_pos"][2] * scale[2])
                    rot_offset = _quat_rotate_vec(rot_quat, scaled_local)
                    composed_quat = _quat_mul(rot_quat, template.get("local_quat", (0.0, 0.0, 0.0, 1.0)))
                    cloned = {
                        **template,
                        "def_name": tokens[i + 1],
                        "pos": (pos[0] + rot_offset[0], pos[1] + rot_offset[1], pos[2] + rot_offset[2]),
                        "rot": _quat_to_gl_axis_angle(composed_quat),
                        "scale": (scale[0] * template["local_scale"][0], scale[1] * template["local_scale"][1], scale[2] * template["local_scale"][2]),
                    }
                    scene["objects"].append(cloned)
                    if record_list is not None:
                        record_list.append(template)
                i += 2
                continue

            if tok == "Transform" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                t_pos, t_quat, t_scale = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)
                k, depth = i + 2, 1
                while k < len(tokens) and depth > 0:
                    if tokens[k] == "{":
                        depth += 1
                    elif tokens[k] == "}":
                        depth -= 1
                    elif depth == 1:
                        if tokens[k] == "translation":
                            vals = read_floats_at(k + 1, 3)
                            if len(vals) == 3:
                                t_pos = tuple(vals)
                        elif tokens[k] == "rotation":
                            vals = read_floats_at(k + 1, 4)
                            if len(vals) == 4:
                                t_quat = _axis_angle_to_quat(vals[0], vals[1], vals[2], vals[3])
                        elif tokens[k] == "scale":
                            vals = read_floats_at(k + 1, 3)
                            if len(vals) == 3:
                                t_scale = tuple(vals)
                    k += 1

                rot_t = _quat_rotate_vec(rot_quat, (t_pos[0] * scale[0], t_pos[1] * scale[1], t_pos[2] * scale[2]))
                new_pos = (pos[0] + rot_t[0], pos[1] + rot_t[1], pos[2] + rot_t[2])
                new_scale = (scale[0] * t_scale[0], scale[1] * t_scale[1], scale[2] * t_scale[2])
                sub_records = [] if def_name else None
                i = walk_vrml2(i + 2, new_pos, _quat_mul(rot_quat, t_quat), new_scale, sub_records, inherited_def)
                if def_name and sub_records is not None:
                    defs_table[def_name] = sub_records
                if record_list is not None and sub_records:
                    record_list.extend(sub_records)
                continue

            if tok in CONTAINER_NODES and i + 1 < len(tokens) and tokens[i + 1] == "{":
                sub_records = [] if def_name else None
                i = walk_vrml2(i + 2, pos, rot_quat, scale, sub_records, inherited_def)
                if def_name and sub_records is not None:
                    defs_table[def_name] = sub_records
                if record_list is not None and sub_records:
                    record_list.extend(sub_records)
                continue

            if tok == "Shape" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                end_idx = skip_balanced(i + 1, "{", "}")
                obj = parse_shape_block(" ".join(tokens[i + 2 : end_idx - 1]), pos, _quat_to_gl_axis_angle(rot_quat), scale, inherited_def)
                if obj:
                    scene["objects"].append(obj)
                    tmpl = {**obj, "local_pos": (0.0, 0.0, 0.0), "local_quat": rot_quat, "local_scale": scale}
                    if def_name:
                        defs_table[def_name] = [tmpl]
                    if record_list is not None:
                        record_list.append(tmpl)
                i = end_idx
                continue

            if i + 1 < len(tokens) and tokens[i + 1] == "{":
                i = skip_balanced(i + 1, "{", "}")
                continue
            i += 1
        return i

    if is_vrml1:
        walk_vrml1(0, {"pos": (0.0, 0.0, 0.0), "rot": (0.0, 1.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0), "diff": (0.5, 0.65, 0.8), "emis": (0.0, 0.0, 0.0), "pts": []})
    else:
        walk_vrml2(0)

    return scene


def get_object_aabb(obj: dict) -> tuple[float, float, float, float, float, float] | None:
    px, py, pz = obj["pos"]
    sx, sy, sz = obj["scale"]
    rx, ry, rz, ang_deg = obj.get("rot", (0.0, 1.0, 0.0, 0.0))
    kind = obj["kind"]

    def _rotated_box_aabb(hx: float, hy: float, hz: float):
        if abs(ang_deg) < 1e-3:
            return (px - hx, px + hx, py - hy, py + hy, pz - hz, pz + hz)
        q = _axis_angle_to_quat(rx, ry, rz, math.radians(ang_deg))
        rpts = [_quat_rotate_vec(q, c) for c in (
            (-hx, -hy, -hz), (hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz),
            (-hx, -hy, hz), (hx, -hy, hz), (-hx, hy, hz), (hx, hy, hz),
        )]
        return (
            min(px + p[0] for p in rpts), max(px + p[0] for p in rpts),
            min(py + p[1] for p in rpts), max(py + p[1] for p in rpts),
            min(pz + p[2] for p in rpts), max(pz + p[2] for p in rpts),
        )

    if kind == "box":
        bx, by, bz = obj["size"]
        return _rotated_box_aabb(abs(bx * sx) * 0.5, abs(by * sy) * 0.5, abs(bz * sz) * 0.5)
    if kind in ("cylinder", "cone"):
        r = abs(obj["radius"] * max(sx, sz))
        return _rotated_box_aabb(r, abs(obj["height"] * sy) * 0.5, r)
    if kind == "sphere":
        r = abs(obj["radius"] * max(sx, sy, sz))
        return (px - r, px + r, py - r, py + r, pz - r, pz + r)
    if kind in ("faces", "lines") and obj.get("pts"):
        return (
            min(px + p[0] * sx for p in obj["pts"]), max(px + p[0] * sx for p in obj["pts"]),
            min(py + p[1] * sy for p in obj["pts"]), max(py + p[1] * sy for p in obj["pts"]),
            min(pz + p[2] * sz for p in obj["pts"]), max(pz + p[2] * sz for p in obj["pts"]),
        )
    if kind == "extrusion" and obj.get("spine"):
        cross = obj.get("cross") or [(-0.5, -0.5), (0.5, 0.5)]
        max_cx = max(abs(c[0]) for c in cross)
        max_cz = max(abs(c[1]) for c in cross)
        return (
            min(px + (p[0] - max_cx) * sx for p in obj["spine"]), max(px + (p[0] + max_cx) * sx for p in obj["spine"]),
            min(py + p[1] * sy for p in obj["spine"]), max(py + p[1] * sy for p in obj["spine"]),
            min(pz + (p[2] - max_cz) * sz for p in obj["spine"]), max(pz + (p[2] + max_cz) * sz for p in obj["spine"]),
        )
    return None


def raycast_aabb(origin, direction, box, max_dist=60.0):
    min_x, max_x, min_y, max_y, min_z, max_z = box
    bounds = ((min_x, max_x), (min_y, max_y), (min_z, max_z))
    normals_neg = ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, -1.0))
    normals_pos = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    tmin, tmax = 0.0, max_dist
    hit_normal = (0.0, 1.0, 0.0)

    for axis in range(3):
        orig, d = origin[axis], direction[axis]
        bmin, bmax = bounds[axis]
        if abs(d) < 1e-8:
            if orig < bmin or orig > bmax:
                return None
        else:
            inv_d = 1.0 / d
            t1, t2 = (bmin - orig) * inv_d, (bmax - orig) * inv_d
            n1, n2 = normals_neg[axis], normals_pos[axis]
            if t1 > t2:
                t1, t2, n1, n2 = t2, t1, n2, n1
            if t1 > tmin:
                tmin, hit_normal = t1, n1
            if t2 < tmax:
                tmax = t2
            if tmin > tmax:
                return None

    if tmin < 0.0 or tmin > max_dist:
        return None
    return tmin, (origin[0] + direction[0] * tmin, origin[1] + direction[1] * tmin, origin[2] + direction[2] * tmin), hit_normal


def find_looked_at_object(cam_pos, yaw_deg, pitch_deg, objects, max_dist=60.0):
    yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
    cp = math.cos(pr)
    look_dir = (-math.sin(yr) * cp, math.sin(pr), -math.cos(yr) * cp)
    best_hit = None
    best_dist = max_dist + 1.0

    for idx, obj in enumerate(objects):
        if obj["kind"] == "grid":
            continue
        box = get_object_aabb(obj)
        if box is None or ((box[1] - box[0]) >= 60 and (box[5] - box[4]) >= 60 and (box[3] - box[2]) <= 0.5):
            continue
        res = raycast_aabb(cam_pos, look_dir, box, max_dist=max_dist)
        if res is not None and res[0] < best_dist:
            best_dist = res[0]
            best_hit = ({**obj, "_index": idx, "_aabb": box}, res[0], res[1], res[2])
    return best_hit


def compute_surface_placement_coords(shape: str, scale: float, hit_pt, hit_norm) -> tuple[float, float, float]:
    s = max(0.2, min(20.0, scale))
    extents = {
        "pillar": (0.45 * s, 2.0 * s, 0.45 * s),
        "sphere": (1.0 * s, 1.0 * s, 1.0 * s),
        "cone": (1.1 * s, 1.25 * s, 1.1 * s),
        "platform": (2.0 * s, 0.15 * s, 2.0 * s),
        "wall": (2.25 * s, 1.5 * s, 0.2 * s),
        "arch": (1.85 * s, 1.9 * s, 0.4 * s),
        "box": (1.0 * s, 1.0 * s, 1.0 * s),
    }
    ex, ey, ez = extents.get(shape, (1.0 * s, 1.0 * s, 1.0 * s))
    nx, ny, nz = hit_norm
    pad = abs(nx) * ex + abs(ny) * ey + abs(nz) * ez + 0.08
    return (
        round(hit_pt[0] + nx * pad, 2),
        round(max(0.0, hit_pt[1] + (ny * pad if ny < 0 else (0.0 if ny > 0 else -ey * 0.5))), 2),
        round(hit_pt[2] + nz * pad, 2),
    )


def sample_elevation_grid_y(obj: dict, wx: float, wz: float) -> float | None:
    px, py, pz = obj["pos"]
    sx, sy, sz = obj["scale"]
    xs, zs = obj["xs"] * sx, obj["zs"] * sz
    if xs <= 0 or zs <= 0:
        return None
    ix, iz = int(math.floor((wx - px) / xs)), int(math.floor((wz - pz) / zs))
    if 0 <= ix < obj["xd"] - 1 and 0 <= iz < obj["zd"] - 1:
        idx = iz * obj["xd"] + ix
        if idx < len(obj["heights"]):
            return py + obj["heights"][idx] * sy
    return None


def resolve_spawn_clearance(cam_pos: list[float], objects: list[dict]):
    eye_h, radius = 1.8, 0.45
    feet_y = cam_pos[1] - eye_h
    for _ in range(8):
        lifted = False
        for o in objects:
            if o["kind"] == "grid":
                continue
            box = get_object_aabb(o)
            if box is None:
                continue
            min_x, max_x, min_y, max_y, min_z, max_z = box
            if (max_x - min_x) >= 30 and (max_z - min_z) >= 30 and max_y <= 0.2:
                continue
            if cam_pos[0] + radius > min_x and cam_pos[0] - radius < max_x and cam_pos[2] + radius > min_z and cam_pos[2] - radius < max_z:
                if feet_y + eye_h > min_y + 0.05 and feet_y < max_y - 0.02:
                    feet_y = max_y
                    lifted = True
        if not lifted:
            break
    cam_pos[1] = feet_y + eye_h


def step_player_physics(cam_pos, dx, dz, vel_y, jump_pressed, dt, objects):
    eye_h, radius, step_height = 1.8, 0.45, 0.65
    dt = min(dt, 0.05)
    colliders = []
    grids = []
    for o in objects:
        if o["kind"] == "grid":
            grids.append(o)
        else:
            box = get_object_aabb(o)
            if box is not None:
                colliders.append(box)

    orig_feet_y = cam_pos[1] - eye_h
    feet_y = orig_feet_y
    for axis, delta in ((0, dx), (2, dz)):
        if delta == 0.0:
            continue
        cand_x = cam_pos[0] + (delta if axis == 0 else 0.0)
        cand_z = cam_pos[2] + (delta if axis == 2 else 0.0)
        blocked = False
        step_target_y = feet_y
        for min_x, max_x, min_y, max_y, min_z, max_z in colliders:
            if cand_x + radius > min_x and cand_x - radius < max_x and cand_z + radius > min_z and cand_z - radius < max_z:
                if orig_feet_y + eye_h > min_y + 0.05 and orig_feet_y < max_y - 0.02:
                    if max_y - orig_feet_y <= step_height:
                        step_target_y = max(step_target_y, max_y)
                    else:
                        cx_box = 0.5 * (min_x + max_x)
                        cz_box = 0.5 * (min_z + max_z)
                        cur_dist_sq = (cam_pos[0] - cx_box) ** 2 + (cam_pos[2] - cz_box) ** 2
                        new_dist_sq = (cand_x - cx_box) ** 2 + (cand_z - cz_box) ** 2
                        if new_dist_sq <= cur_dist_sq:
                            blocked = True
                            break
        if not blocked:
            feet_y = step_target_y
            if axis == 0:
                cam_pos[0] = cand_x
            else:
                cam_pos[2] = cand_z

    ground_y = 0.0
    for g in grids:
        gy = sample_elevation_grid_y(g, cam_pos[0], cam_pos[2])
        if gy is not None and ground_y < gy <= feet_y + step_height:
            ground_y = gy
    for min_x, max_x, min_y, max_y, min_z, max_z in colliders:
        if cam_pos[0] + radius > min_x and cam_pos[0] - radius < max_x and cam_pos[2] + radius > min_z and cam_pos[2] - radius < max_z:
            if ground_y < max_y <= feet_y + step_height:
                ground_y = max_y

    on_ground = feet_y <= ground_y + 0.03 and vel_y <= 0.0
    if on_ground:
        feet_y = ground_y
        vel_y = 7.5 if jump_pressed else 0.0
    else:
        vel_y -= 18.0 * dt
        feet_y += vel_y * dt
        for min_x, max_x, min_y, max_y, min_z, max_z in colliders:
            if cam_pos[0] + radius > min_x and cam_pos[0] - radius < max_x and cam_pos[2] + radius > min_z and cam_pos[2] - radius < max_z:
                if vel_y > 0 and feet_y + eye_h > min_y and feet_y < min_y:
                    feet_y = min_y - eye_h
                    vel_y = 0.0
        if feet_y <= ground_y:
            feet_y = ground_y
            vel_y = 0.0
            on_ground = True

    cam_pos[1] = feet_y + eye_h
    return vel_y, on_ground


def draw_box(sx, sy, sz):
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    glBegin(GL_QUADS)
    glNormal3f(0, 0, 1); glVertex3f(-hx, -hy, hz); glVertex3f(hx, -hy, hz); glVertex3f(hx, hy, hz); glVertex3f(-hx, hy, hz)
    glNormal3f(0, 0, -1); glVertex3f(hx, -hy, -hz); glVertex3f(-hx, -hy, -hz); glVertex3f(-hx, hy, -hz); glVertex3f(hx, hy, -hz)
    glNormal3f(0, 1, 0); glVertex3f(-hx, hy, hz); glVertex3f(hx, hy, hz); glVertex3f(hx, hy, -hz); glVertex3f(-hx, hy, -hz)
    glNormal3f(0, -1, 0); glVertex3f(-hx, -hy, -hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, -hy, hz); glVertex3f(-hx, -hy, hz)
    glNormal3f(1, 0, 0); glVertex3f(hx, -hy, hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, hy, -hz); glVertex3f(hx, hy, hz)
    glNormal3f(-1, 0, 0); glVertex3f(-hx, -hy, -hz); glVertex3f(-hx, -hy, hz); glVertex3f(-hx, hy, hz); glVertex3f(-hx, hy, -hz)
    glEnd()


def draw_aabb_wireframe(box, rgba=(0.0, 1.0, 0.85, 0.9)):
    min_x, max_x, min_y, max_y, min_z, max_z = box
    pad = 0.04
    min_x, max_x = min_x - pad, max_x + pad
    min_y, max_y = min_y - pad, max_y + pad
    min_z, max_z = min_z - pad, max_z + pad

    glDisable(GL_LIGHTING)
    glLineWidth(2.0)
    glColor4f(*rgba)
    for y_val in (min_y, max_y):
        glBegin(GL_LINE_LOOP)
        glVertex3f(min_x, y_val, min_z); glVertex3f(max_x, y_val, min_z)
        glVertex3f(max_x, y_val, max_z); glVertex3f(min_x, y_val, max_z)
        glEnd()
    glBegin(GL_LINES)
    for xv, zv in ((min_x, min_z), (max_x, min_z), (max_x, max_z), (min_x, max_z)):
        glVertex3f(xv, min_y, zv); glVertex3f(xv, max_y, zv)
    glEnd()
    glLineWidth(1.0)
    glEnable(GL_LIGHTING)


def draw_checker_floor():
    tile, half = 2.0, 20
    glBegin(GL_QUADS)
    glNormal3f(0, 1, 0)
    for x in range(-half, half):
        for z in range(-half, half):
            glColor4f(*(0.06, 0.06, 0.12, 1.0) if (x + z) % 2 == 0 else (0.55, 0.60, 0.68, 1.0))
            x0, z0 = x * tile, z * tile
            glVertex3f(x0, 0.0, z0); glVertex3f(x0 + tile, 0.0, z0); glVertex3f(x0 + tile, 0.0, z0 + tile); glVertex3f(x0, 0.0, z0 + tile)
    glEnd()

    glDisable(GL_LIGHTING)
    glColor3f(0.0, 0.85, 0.95)
    glBegin(GL_LINES)
    for i in range(-40, 41, 10):
        glVertex3f(i, 0.01, -40); glVertex3f(i, 0.01, 40)
        glVertex3f(-40, 0.01, i); glVertex3f(40, 0.01, i)
    glEnd()
    glEnable(GL_LIGHTING)


def render_object(obj, quadric):
    glPushMatrix()
    glTranslatef(*obj["pos"])
    rx, ry, rz, ang = obj["rot"]
    if ang != 0.0:
        glRotatef(ang, rx, ry, rz)
    glScalef(*obj["scale"])

    r, g, b = obj["diff"]
    er, eg, eb = obj["emis"]
    alpha = obj["alpha"]

    if er > 0.05 or eg > 0.05 or eb > 0.05:
        glDisable(GL_LIGHTING)
        glColor4f(min(1.0, r * 0.4 + er), min(1.0, g * 0.4 + eg), min(1.0, b * 0.4 + eb), alpha)
    else:
        glEnable(GL_LIGHTING)
        glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION, (r * 0.25, g * 0.25, b * 0.25, 1.0))
        glColor4f(r, g, b, alpha)

    kind = obj["kind"]
    if kind == "box":
        draw_box(*obj["size"])
    elif kind == "cylinder":
        glRotatef(-90, 1, 0, 0)
        glTranslatef(0, 0, -obj["height"] / 2.0)
        gluCylinder(quadric, obj["radius"], obj["radius"], obj["height"], 20, 1)
    elif kind == "cone":
        glRotatef(-90, 1, 0, 0)
        glTranslatef(0, 0, -obj["height"] / 2.0)
        gluCylinder(quadric, obj["radius"], 0.0, obj["height"], 20, 1)
    elif kind == "sphere":
        gluSphere(quadric, obj["radius"], 18, 18)
    elif kind in ("faces", "lines"):
        if kind == "lines":
            glDisable(GL_LIGHTING)
        pts = obj["pts"]
        poly_groups = []
        cur_poly = []
        for idx in obj["idxs"]:
            if idx == -1:
                if cur_poly:
                    poly_groups.append(cur_poly)
                    cur_poly = []
            elif 0 <= idx < len(pts):
                cur_poly.append(pts[idx])
        if cur_poly:
            poly_groups.append(cur_poly)

        if kind == "faces":
            for poly in poly_groups:
                if len(poly) < 3:
                    continue
                glBegin(GL_TRIANGLES)
                glNormal3f(*_compute_poly_normal(poly))
                v0 = poly[0]
                for idx_v in range(1, len(poly) - 1):
                    glVertex3f(*v0)
                    glVertex3f(*poly[idx_v])
                    glVertex3f(*poly[idx_v + 1])
                glEnd()
        else:
            for poly in poly_groups:
                if len(poly) < 2:
                    continue
                glBegin(GL_LINE_STRIP)
                for v in poly:
                    glVertex3f(*v)
                glEnd()
    elif kind == "extrusion":
        spine, cross = obj["spine"], obj["cross"]
        for s_idx in range(len(spine) - 1):
            p0, p1 = spine[s_idx], spine[s_idx + 1]
            glBegin(GL_QUADS)
            for c_idx in range(len(cross) - 1):
                cx0, cz0 = cross[c_idx]
                cx1, cz1 = cross[c_idx + 1]
                v0 = (p0[0] + cx0, p0[1], p0[2] + cz0)
                v1 = (p0[0] + cx1, p0[1], p0[2] + cz1)
                v2 = (p1[0] + cx1, p1[1], p1[2] + cz1)
                v3 = (p1[0] + cx0, p1[1], p1[2] + cz0)
                glNormal3f(*_compute_poly_normal([v0, v1, v2]))
                glVertex3f(*v0); glVertex3f(*v1); glVertex3f(*v2); glVertex3f(*v3)
            glEnd()
    elif kind == "grid":
        xd, zd, xs, zs, heights = obj["xd"], obj["zd"], obj["xs"], obj["zs"], obj["heights"]
        glBegin(GL_QUADS)
        glNormal3f(0, 1, 0)
        for z in range(zd - 1):
            for x in range(xd - 1):
                i00 = z * xd + x
                i10, i11, i01 = i00 + 1, (z + 1) * xd + (x + 1), (z + 1) * xd + x
                if i11 >= len(heights):
                    continue
                glColor4f(*(max(0.05, r * 0.2), max(0.05, g * 0.2), max(0.1, b * 0.25), alpha) if (x + z) % 2 == 0 else (r, g, b, alpha))
                glVertex3f(x * xs, heights[i00], z * zs)
                glVertex3f((x + 1) * xs, heights[i10], z * zs)
                glVertex3f((x + 1) * xs, heights[i11], (z + 1) * zs)
                glVertex3f(x * xs, heights[i01], (z + 1) * zs)
        glEnd()

    glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION, (0.0, 0.0, 0.0, 1.0))
    glEnable(GL_LIGHTING)
    glPopMatrix()


def draw_cyber_grid_overlay():
    glDisable(GL_LIGHTING)
    glLineWidth(1.0)
    glColor4f(0.0, 0.85, 0.90, 0.28)
    glBegin(GL_LINES)
    for i in range(-80, 81, 4):
        glVertex3f(i, 0.015, -80); glVertex3f(i, 0.015, 80)
        glVertex3f(-80, 0.015, i); glVertex3f(80, 0.015, i)
    glEnd()
    glEnable(GL_LIGHTING)


def _scene_geom_signature(scene: dict, scene_objs: list[dict], world_name: str = "") -> tuple:
    h = hashlib.blake2b(digest_size=8)
    h.update(b"1" if scene.get("has_floor") else b"0")
    h.update(world_name.lower().encode("utf-8", errors="replace"))
    for o in scene_objs:
        pos, rot, sc, diff, emis = o.get("pos", (0, 0, 0)), o.get("rot", (0, 1, 0, 0)), o.get("scale", (1, 1, 1)), o.get("diff", (0, 0, 0)), o.get("emis", (0, 0, 0))
        h.update(f"{o.get('kind')}|{pos[0]:.3f},{pos[1]:.3f},{pos[2]:.3f}|{rot[3]:.2f}|{sc[0]:.2f},{sc[1]:.2f},{sc[2]:.2f}|{diff[0]:.2f},{diff[1]:.2f},{diff[2]:.2f}|{emis[0]:.2f},{emis[1]:.2f},{emis[2]:.2f}|{o.get('alpha', 1.0):.2f};".encode("utf-8", errors="replace"))
    return (len(scene_objs), bool(scene.get("has_floor")), world_name.lower(), h.digest())


class SceneDisplayListCache:
    def __init__(self):
        self.list_id = 0
        self.compiled_sig = None

    def clear(self):
        if self.list_id:
            try:
                glDeleteLists(self.list_id, 1)
            except Exception:
                pass
            self.list_id = 0
            self.compiled_sig = None

    def render_static_scene(self, scene: dict, scene_objs: list[dict], quadric, world_name: str = ""):
        sig = _scene_geom_signature(scene, scene_objs, world_name)
        if self.compiled_sig != sig or not self.list_id:
            self.clear()
            new_id = 0
            list_open = False
            try:
                new_id = glGenLists(1)
                if new_id:
                    glNewList(new_id, GL_COMPILE)
                    list_open = True
                    if not scene.get("has_floor"):
                        draw_checker_floor()
                    elif world_name.lower() == "hub":
                        draw_cyber_grid_overlay()
                    for obj in scene_objs:
                        render_object(obj, quadric)
                    glEndList()
                    list_open = False
                    self.list_id = new_id
                    self.compiled_sig = sig
            except Exception:
                if list_open:
                    try:
                        glEndList()
                    except Exception:
                        pass
                if new_id:
                    try:
                        glDeleteLists(new_id, 1)
                    except Exception:
                        pass
                self.list_id = 0
                self.compiled_sig = None

        if self.list_id:
            glCallList(self.list_id)
        else:
            if not scene.get("has_floor"):
                draw_checker_floor()
            elif world_name.lower() == "hub":
                draw_cyber_grid_overlay()
            for obj in scene_objs:
                render_object(obj, quadric)


def build_normalized_avatar_mesh(wrl_text: str) -> dict | None:
    parsed = parse_vrml(wrl_text)
    objs = [o for o in parsed.get("objects", []) if o["kind"] != "grid" and o.get("size", (0, 0, 0))[0] < 20]
    if not objs:
        return None
    aabbs = [b for b in (get_object_aabb(o) for o in objs) if b is not None]
    if not aabbs:
        return None

    min_x, max_x = min(b[0] for b in aabbs), max(b[1] for b in aabbs)
    min_y, max_y = min(b[2] for b in aabbs), max(b[3] for b in aabbs)
    min_z, max_z = min(b[4] for b in aabbs), max(b[5] for b in aabbs)

    span_y = max(0.05, max_y - min_y)
    max_horiz = max(max(0.05, max_x - min_x), max(0.05, max_z - min_z))
    scale_f = 1.85 / span_y
    if max_horiz * scale_f > 1.4:
        scale_f = 1.4 / max_horiz

    return {
        "objects": objs,
        "offset": (-0.5 * (min_x + max_x), -min_y, -0.5 * (min_z + max_z)),
        "scale": scale_f,
        "height": span_y * scale_f,
    }


def render_peer(peer, quadric, now, avatar_meshes: dict):
    glPushMatrix()
    px, py, pz = peer.get("pos", [0.0, 1.0, 0.0])
    rot = peer.get("rot", [0.0, 1.0, 0.0, 0.0])
    yaw_deg = math.degrees(rot[3]) if len(rot) >= 4 else 0.0

    extra_y = spin = tilt = 0.0
    emote = peer.get("emote")
    emote_until = peer.get("emote_until", 0)
    if emote and now < emote_until:
        t = (emote_until - now) * 6.0
        if emote == "jump":
            extra_y = abs(math.sin(t)) * 1.4
        elif emote in ("spin", "dance"):
            spin = (t * 180.0) % 360.0
        elif emote == "wave":
            tilt = math.sin(t * 2.0) * 25.0

    glTranslatef(px, py + extra_y, pz)
    glRotatef(yaw_deg + spin, 0, 1, 0)
    glRotatef(tilt, 0, 0, 1)

    av_name = peer.get("avatar", "default")
    mesh = avatar_meshes.get(av_name) if av_name and av_name != "default" else None
    if mesh and mesh.get("objects"):
        sc = mesh["scale"]
        glScalef(sc, sc, sc)
        glTranslatef(*mesh["offset"])
        for obj in mesh["objects"]:
            render_object(obj, quadric)
        glPopMatrix()
        return

    hex_c = peer.get("color", "#00ffcc").lstrip("#")
    r, g, b = (int(hex_c[0:2], 16) / 255.0, int(hex_c[2:4], 16) / 255.0, int(hex_c[4:6], 16) / 255.0) if len(hex_c) == 6 else (0.0, 1.0, 0.8)

    glDisable(GL_LIGHTING)
    glColor4f(r, g, b, 0.85)
    glPushMatrix()
    glRotatef(-90, 1, 0, 0)
    gluCylinder(quadric, 0.45, 0.0, 1.4, 16, 1)
    glPopMatrix()

    glColor4f(0.95, 0.95, 1.0, 1.0)
    glPushMatrix()
    glTranslatef(0.0, 1.65, 0.0)
    gluSphere(quadric, 0.28, 14, 14)
    glPopMatrix()
    glEnable(GL_LIGHTING)
    glPopMatrix()


def project_peer_nametags(peers: dict, avatar_meshes: dict, cam_pos: list[float], max_dist: float = 45.0):
    try:
        model = glGetDoublev(GL_MODELVIEW_MATRIX)
        proj = glGetDoublev(GL_PROJECTION_MATRIX)
        view = glGetIntegerv(GL_VIEWPORT)
    except Exception:
        return []

    projected = []
    for peer in list(peers.values()):
        px, py, pz = peer.get("pos", [0.0, 0.0, 0.0])
        dist = math.sqrt((px - cam_pos[0]) ** 2 + (py - cam_pos[1]) ** 2 + (pz - cam_pos[2]) ** 2)
        if dist > max_dist:
            continue
        av_name = peer.get("avatar", "default")
        mesh = avatar_meshes.get(av_name) if av_name and av_name != "default" else None
        head_y = py + (mesh["height"] + 0.35 if mesh else 2.25)
        try:
            sx, sy, sz = gluProject(px, head_y, pz, model, proj, view)
        except Exception:
            continue
        if 0.0 < sz < 1.0 and 0 <= sx <= WINDOW_W and 0 <= sy <= WINDOW_H:
            hc = peer.get("color", "#00ffcc").lstrip("#")
            rgb = (int(hc[0:2], 16), int(hc[2:4], 16), int(hc[4:6], 16)) if len(hc) == 6 else (0, 255, 204)
            label = f"{peer.get('nick', 'Peer')} [{av_name}]" if av_name and av_name != "default" else peer.get("nick", "Peer")
            projected.append((dist, float(sx), float(WINDOW_H - sy), label, rgb))

    projected.sort(key=lambda item: item[0], reverse=True)
    return [(sx, sy, label, rgb) for _, sx, sy, label, rgb in projected]


class MetaverseClient:
    def __init__(self, signing_key: SigningKey):
        self.sk = signing_key
        self.pubkey_hex = signing_key.verify_key.encode().hex()
        prof = load_local_profile()
        self.server_url = DEFAULT_SERVER
        self.prev_server = None
        self.node_name = "cyberX87"
        self.nick = prof["nick"]
        self.color = prof["color"]
        self.role = "OFFLINE"
        self.short_id = ""
        self.avatar = prof["avatar"]
        self.world = "hub"
        self.world_desc = ""
        self.world_descriptions = {}
        self.worlds = ["hub"]
        self.avatars = []
        self.avatar_meshes = {}
        self.avatar_requested = {}
        self.scene = parse_vrml("")
        self.peers = {}
        self.plugins = load_x87_plugins()
        self.radio = RadioPlayer()
        self.fly_mode = False
        self.offline_mode = False
        self.inspect_mode = False
        self.crt_mode = False
        self.looked_at_hit = None
        self.cam_pos_ref = [0.0, 4.05, 13.5]
        self.cam_yaw_ref = 0.0
        self.cam_pitch_ref = 0.0
        self.unstuck_req = False
        self.vel_y = 0.0
        self.chat_log = [
            ("SYSTEM", "#00ffcc", "cyberX87 VRML97 Browser v2.03 Ready."),
        ]
        self.out_queue = asyncio.Queue()
        self.loop = None
        self.active_ws = None
        self.connected = False
        self.reconnect_target = None
        self.teleport_req = None
        self.teleport_yaw = None
        self.running = True

    def persist_profile(self):
        save_local_profile(self.nick, self.color, self.avatar)

    def log(self, sender, color, text):
        self.chat_log.append((sender, color, re.sub(r"[\x00-\x1f\x7f]", " ", str(text))))
        if len(self.chat_log) > 100:
            self.chat_log.pop(0)

    def _trigger_ws_reconnect(self):
        if self.loop and not self.loop.is_closed() and self.active_ws is not None:
            ws_ref = self.active_ws
            try:
                self.loop.call_soon_threadsafe(
                    lambda w=ws_ref: asyncio.create_task(w.close()) if not w.closed else None
                )
            except RuntimeError:
                pass

    def send_json(self, payload: dict):
        if self.loop and not self.loop.is_closed() and self.connected and self.short_id:
            try:
                self.loop.call_soon_threadsafe(self.out_queue.put_nowait, json.dumps(payload))
            except RuntimeError:
                self.log("NET-ERR", "#ff4444", "Connection loop closing.")
        elif self.connected and not self.short_id:
            self.log("NET", "#ffaa00", "Authenticating with server, try again in a moment.")
        elif self.offline_mode:
            self.log("OFFLINE", "#ffaa00", "Offline mode - type /online to reconnect.")
        else:
            self.log("NET-ERR", "#ff4444", "Not connected to server.")

    def request_join_world(self, world_name: str):
        clean_w = _clean_ident(world_name, 32, "hub")
        if self.offline_mode or not self.connected:
            if self.load_offline_world(clean_w):
                return
            if self.offline_mode:
                self.log("CACHE-MISS", "#ff4444", f"World '{clean_w}' is not in local cache (~/.cyberx87_cache/). Type /cache to list.")
            return
        cached = get_cached_world(self.server_url, clean_w)
        payload = {"type": "join_world", "world": clean_w}
        if cached and cached.get("sha256"):
            payload["cached_hash"] = cached["sha256"]
        self.send_json(payload)

    def load_offline_world(self, world_name: str = "hub") -> bool:
        clean_w = _clean_ident(world_name, 32, "hub")
        entry = get_cached_world(self.server_url, clean_w)
        if not entry:
            return False
        self._apply_world(
            entry["world"],
            entry["wrl"],
            "CACHE-OFFLINE",
            entry.get("description", ""),
            entry.get("radio_url", ""),
            entry.get("radio_name", ""),
            from_cache=True,
            spawn_pos=entry.get("spawn_pos"),
            spawn_yaw=float(entry.get("spawn_yaw", 0.0) or 0.0),
        )
        return True

    def enter_offline_mode(self, world_name: str = ""):
        self.offline_mode = True
        self.role = "OFFLINE-CACHE"
        self.peers.clear()
        self._trigger_ws_reconnect()
        target_w = world_name.strip() or self.world or "hub"
        if not self.load_offline_world(target_w):
            cached_all = list_cached_worlds()
            if cached_all:
                self.load_offline_world(cached_all[0]["world"])
            else:
                self.log("CACHE", "#ffaa00", "Local world cache is empty. Connect online first to cache worlds.")

    def exit_offline_mode(self):
        if not self.offline_mode:
            self.log("NET", "#00ffcc", "Already in online mode.")
            return
        self.offline_mode = False
        self.reconnect_target = (self.server_url, self.world or "hub")
        self._trigger_ws_reconnect()
        self.log("NET", "#00ff66", f"Reconnecting online to {self.server_url}...")

    def _store_avatar_mesh(self, av_name: str, mesh: dict, wrl_text: str = ""):
        if wrl_text:
            store_cached_avatar_wrl(av_name, wrl_text)
        if av_name in self.avatar_meshes:
            self.avatar_meshes.pop(av_name, None)
        elif len(self.avatar_meshes) >= MAX_AVATAR_CACHE:
            self.avatar_meshes.pop(next(iter(self.avatar_meshes)), None)
        self.avatar_meshes[av_name] = mesh

    def _ensure_avatar_loaded(self, av_name: str):
        if not av_name or av_name == "default" or av_name in self.avatar_meshes:
            return
        cached_wrl = load_cached_avatar_wrl(av_name)
        if cached_wrl:
            mesh = build_normalized_avatar_mesh(cached_wrl)
            if mesh:
                self._store_avatar_mesh(av_name, mesh)
                return
        now = time.time()
        if now - self.avatar_requested.get(av_name, 0.0) < 15.0:
            return
        if len(self.avatar_requested) >= MAX_AVATAR_REQ_TRACK:
            self.avatar_requested.pop(next(iter(self.avatar_requested)), None)
        self.avatar_requested[av_name] = now
        self.send_json({"type": "get_avatar", "name": av_name})

    def hop_to_server(self, target_url: str, target_world: str = "hub"):
        norm_url = normalize_ws_peer_client_url(target_url)
        if not norm_url:
            self.log("ERR", "#ff4444", "Invalid WebSocket server URL (expected ws:// or wss://).")
            return
        self.persist_profile()
        self.offline_mode = False
        self.peers.clear()
        self.radio.stop()
        if self.server_url and self.server_url != norm_url:
            self.prev_server = (self.server_url, self.world or "hub")
        self.reconnect_target = (norm_url, target_world or "hub")
        self._trigger_ws_reconnect()

    def start_background(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main_net())
        except Exception as exc:
            if self.running:
                self.log("NET-ERR", "#ff4444", f"Network loop error: {exc}")
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                self.loop.close()
            except Exception:
                pass

    def _record_recent_server(self, url: str, node_name: str):
        sdir = load_server_directory()
        recent = [r for r in sdir.get("recent", []) if r.get("url") != url]
        recent.insert(0, {"url": url, "node_name": node_name, "last_seen": int(time.time())})
        sdir["recent"] = recent[:12]
        save_server_directory(sdir)

    def _apply_world(
        self,
        world_name: str,
        wrl_text: str,
        prefix: str,
        desc: str = "",
        radio_url: str = "",
        radio_name: str = "",
        wrl_hash: str = "",
        from_cache: bool = False,
        spawn_pos: list[float] | None = None,
        spawn_yaw: float = 0.0,
        in_place: bool = False,
    ):
        was_same_world = (self.world == world_name and self.connected and not from_cache)
        self.world = world_name
        new_scene = parse_vrml(wrl_text)
        self.scene = new_scene
        resolved_desc = desc or new_scene.get("world_info_desc", "")
        self.world_desc = resolved_desc
        self.world_descriptions[world_name] = resolved_desc

        eff_spawn_pos = spawn_pos if (spawn_pos and len(spawn_pos) >= 3) else new_scene.get("spawn_pos")
        eff_spawn_yaw = float(spawn_yaw) if (spawn_pos and len(spawn_pos) >= 3) else float(new_scene.get("spawn_yaw", 0.0) or 0.0)

        if not from_cache:
            store_cached_world(
                self.server_url, world_name, wrl_text, wrl_hash=wrl_hash,
                desc=resolved_desc, radio_url=radio_url, radio_name=radio_name,
                spawn_pos=eff_spawn_pos, spawn_yaw=eff_spawn_yaw,
            )

        r_url = radio_url or new_scene.get("radio_url", "")
        r_name = radio_name or new_scene.get("radio_name", "") or r_url
        self.radio.set_stream(r_url, r_name)

        if in_place or (was_same_world and prefix == "WORLD"):
            return

        self.vel_y = 0.0
        objs = new_scene.get("objects", [])
        self.peers.clear()
        cache_tag = " [from SHA-256 disk cache]" if from_cache else ""
        self.log(prefix, "#00ff66", f"Loaded '{world_name}': {len(objs)} objects ({new_scene['raw_len']} bytes){cache_tag}")
        if resolved_desc:
            self.log("ABOUT", "#00ffcc", resolved_desc)
        if r_url and self.radio.url:
            state_lbl = "MUTED - /mute to unmute" if self.radio.muted else "tuning in"
            self.log("RADIO", "#ff66cc", f"Station: {r_name} ({state_lbl})")

        if eff_spawn_pos and len(eff_spawn_pos) >= 3:
            self.teleport_req = [float(eff_spawn_pos[0]), float(eff_spawn_pos[1]) + 1.8, float(eff_spawn_pos[2])]
            self.teleport_yaw = eff_spawn_yaw
            return

        self.teleport_yaw = 0.0
        if world_name.lower() == "hub":
            self.teleport_req = [0.0, 4.05, 13.5]
            return

        props = [o for o in objs if o["kind"] != "grid" and o.get("size", (0, 0, 0))[0] < 30]
        if props:
            self.teleport_req = [
                sum(o["pos"][0] for o in props) / len(props),
                max(2.2, sum(o["pos"][1] for o in props) / len(props) + 1.5),
                sum(o["pos"][2] for o in props) / len(props) + 14.0,
            ]
        else:
            self.teleport_req = [0.0, 2.0, 12.0]
        if not objs:
            self.log("WARN", "#ffaa00", f"0 objects parsed! Preview: {new_scene['raw_preview']}")

    async def _main_net(self):
        while self.running:
            if self.offline_mode:
                self.connected = False
                self.short_id = ""
                await asyncio.sleep(0.3)
                continue

            target_world = "hub"
            if self.reconnect_target:
                self.server_url, target_world = self.reconnect_target
                self.reconnect_target = None

            self.avatar_meshes.clear()
            self.avatar_requested.clear()
            self.log("NET", "#00ffcc", f"Connecting to {self.server_url}...")
            try:
                async with websockets.connect(self.server_url, max_size=6 * 1024 * 1024, ping_interval=20, ping_timeout=20) as ws:
                    self.active_ws = ws
                    self.connected = True
                    self.short_id = ""
                    while not self.out_queue.empty():
                        self.out_queue.get_nowait()

                    async def sender_task():
                        while True:
                            await ws.send(await self.out_queue.get())

                    send_fut = asyncio.create_task(sender_task())
                    try:
                        async for raw in ws:
                            try:
                                data = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if not isinstance(data, dict):
                                continue
                            mtype = data.get("type")

                            if mtype == "challenge":
                                nonce_str = str(data.get("nonce", ""))
                                if not re.fullmatch(r"[0-9a-fA-F]{16,128}", nonce_str):
                                    self.log("NET-ERR", "#ff4444", "Server sent invalid challenge nonce.")
                                    break
                                sig = self.sk.sign(nonce_str.encode("utf-8")).signature.hex()
                                auth_msg = {
                                    "type": "auth", "pubkey": self.pubkey_hex, "signature": sig,
                                    "nick": self.nick, "color": self.color, "avatar": self.avatar, "world": target_world,
                                }
                                if self.avatar and self.avatar.lower() != "default":
                                    av_wrl = load_cached_avatar_wrl(self.avatar)
                                    if av_wrl:
                                        auth_msg["avatar_wrl"] = av_wrl
                                await ws.send(json.dumps(auth_msg))

                            elif mtype == "auth_ok":
                                self.role = data.get("role", "guest").upper()
                                self.short_id = data.get("short_id", "")
                                desired_nick = self.nick
                                if data.get("nick"):
                                    if data["nick"] != desired_nick:
                                        self.log("ID", "#ffaa00", f"Nickname '{desired_nick}' in use; joined as '{data['nick']}'.")
                                    self.nick = data["nick"]
                                prev_av = self.avatar
                                self.avatar = data.get("avatar", self.avatar or "default")
                                self.persist_profile()
                                if data.get("avatar_synced") and self.avatar != "default" and self.avatar != prev_av:
                                    self.log("AVATAR", "#00ff66", f"Carried over & synced 3D avatar as '{self.avatar}' on {self.server_url}.")
                                self.node_name = data.get("node_name", "cyberX87 Node")
                                self.worlds = data.get("worlds", ["hub"])
                                self.avatars = data.get("avatars", [])
                                self._record_recent_server(self.server_url, self.node_name)
                                self._apply_world(
                                    data.get("world", "hub"), data.get("wrl", ""), "AUTH",
                                    data.get("description", ""), data.get("radio_url", ""), data.get("radio_name", ""),
                                    wrl_hash=data.get("wrl_hash", ""), spawn_pos=data.get("spawn_pos"),
                                    spawn_yaw=float(data.get("spawn_yaw", 0.0) or 0.0),
                                )

                            elif mtype == "federation_list":
                                plist = data.get("peers", [])
                                self.log("FEDERATION", "#00ff66", f"This Node: {data.get('node_name', self.node_name)} - {data.get('node_desc', '')}")
                                if not plist:
                                    self.log("FEDERATION", "#00ffcc", "No linked peer servers. Admins can run /linkserver <ws://host:port> [label]")
                                else:
                                    for p in plist:
                                        if p.get("online"):
                                            self.log("PEER-NODE", "#00ff66", f"  [ONLINE] {p['label']} ({p['url']}) - {p.get('online_count', 0)} users | worlds: {', '.join(p.get('worlds', [])[:5])}")
                                        else:
                                            self.log("PEER-NODE", "#ffaa00", f"  [offline] {p['label']} ({p['url']})")

                            elif mtype == "world_cached":
                                w_name = data.get("world", "hub")
                                cached = get_cached_world(self.server_url, w_name)
                                if cached and cached.get("sha256") == data.get("wrl_hash", ""):
                                    self._apply_world(
                                        w_name, cached["wrl"], "CACHE-HIT",
                                        data.get("description", ""), data.get("radio_url", ""), data.get("radio_name", ""),
                                        wrl_hash=cached["sha256"], from_cache=True,
                                        spawn_pos=data.get("spawn_pos") or cached.get("spawn_pos"),
                                        spawn_yaw=float(data.get("spawn_yaw", cached.get("spawn_yaw", 0.0)) or 0.0),
                                    )
                                else:
                                    await ws.send(json.dumps({"type": "join_world", "world": w_name}))

                            elif mtype == "world_data":
                                self._apply_world(
                                    data.get("world", "hub"), data.get("wrl", ""), "WORLD",
                                    data.get("description", ""), data.get("radio_url", ""), data.get("radio_name", ""),
                                    wrl_hash=data.get("wrl_hash", ""), spawn_pos=data.get("spawn_pos"),
                                    spawn_yaw=float(data.get("spawn_yaw", 0.0) or 0.0), in_place=bool(data.get("in_place", False)),
                                )

                            elif mtype == "world_info":
                                w = data.get("world", self.world)
                                owner = data.get("owner")
                                cbs = data.get("co_builders", [])
                                wls = data.get("whitelist", [])
                                players = data.get("players", [])
                                sp_pos = data.get("spawn_pos", [0.0, 2.25, 13.5])
                                self.log("WORLD-INFO", "#00ff66", f"=== {data.get('title', w)} ({w}) [{'PRIVATE' if data.get('private') else 'PUBLIC'}] - {max(1, int(data.get('bytes', 0)) // 1024)} KB, {int(data.get('snapshots_count', 0))} backups ===")
                                self.log("ABOUT", "#00ffcc", f"Description: {data.get('description') or '(none set)'}" + (f" | Tags: {', '.join(data.get('tags', []))}" if data.get("tags") else ""))
                                self.log("SPAWN", "#00ffcc", f"Spawn Point [{'CUSTOM' if data.get('spawn_custom') else 'DEFAULT'}]: ({sp_pos[0]:.2f}, {sp_pos[1]:.2f}, {sp_pos[2]:.2f}) yaw={float(data.get('spawn_yaw', 0.0) or 0.0):.0f} deg")
                                if data.get("is_system") and not owner:
                                    self.log("OWNER", "#00ffcc", "Owner: Neo-Genesis Grid (Core System World)")
                                elif owner:
                                    self.log("OWNER", "#00ff66", f"Owner: {owner['nick']} ({owner['short_id']}){' [YOU]' if owner['pubkey'] == self.pubkey_hex else ''}{' [ONLINE]' if owner.get('online') else ''} | PubKey: {owner['pubkey']}")
                                else:
                                    self.log("OWNER", "#ffaa00", "Owner: (unclaimed)")
                                self.log("BUILDERS", "#00ffcc", f"Co-Builders ({len(cbs)}): " + (", ".join(f"{c['nick']} ({c['short_id']}){'*' if c.get('online') else ''}" for c in cbs) or "none"))
                                if data.get("private"):
                                    self.log("WHITELIST", "#ff66cc", f"Allowed Visitors ({len(wls)}): " + (", ".join(f"{v['nick']} ({v['short_id']})" for v in wls) or "none"))
                                if data.get("radio_url"):
                                    self.log("RADIO", "#ff66cc", f"Radio Station: {data.get('radio_name') or data['radio_url']} ({data['radio_url']})")
                                self.log("ONLINE-HERE", "#8888ff", f"Players Here ({len(players)}): " + (", ".join(f"{p['nick']} ({p['short_id']})" for p in players) or "none"))

                            elif mtype == "world_acl_info":
                                w = data.get("world", self.world)
                                self.log("WORLD-ACL", "#00ff66", f"World '{w}' visibility: {'PRIVATE' if data.get('private') else 'PUBLIC'}")
                                self.log("CO-BUILDERS", "#00ffcc", "Co-Builders: " + (", ".join(f"{c['nick']} ({c['short_id']})" for c in data.get("co_builders", [])) or "none"))
                                self.log("WHITELIST", "#ff66cc", "Whitelist: " + (", ".join(f"{v['nick']} ({v['short_id']})" for v in data.get("whitelist", [])) or "none"))

                            elif mtype == "world_desc_updated":
                                w, d = data.get("world", self.world), data.get("description", "")
                                r_url, r_name = data.get("radio_url", ""), data.get("radio_name", "")
                                self.world_descriptions[w] = d
                                if w == self.world:
                                    self.world_desc = d
                                    self.radio.set_stream(r_url, r_name)
                                    self.log("RADIO", "#ff66cc" if r_url else "#ffaa00", f"Station set: {r_name or r_url}" if (r_url and self.radio.url) else "World radio station turned off.")
                                if d:
                                    self.log("ABOUT", "#00ffcc", f"[{w}] {d}")

                            elif mtype == "world_backups_list":
                                snaps = data.get("backups", [])
                                if not snaps:
                                    self.log("BACKUPS", "#ffaa00", f"No backups found for '{data.get('world', self.world)}'. Use /backup to create one.")
                                else:
                                    self.log("BACKUPS", "#00ff66", f"Snapshots for '{data.get('world', self.world)}' ({len(snaps)}):")
                                    for s in snaps[:10]:
                                        self.log("SNAP", "#00ffcc", f"  {s['file']} ({s['bytes']} bytes)")

                            elif mtype == "world_export_data":
                                w = data.get("world", self.world)
                                wrl_out = str(data.get("wrl", ""))
                                out_wrl = resolve_safe_export_path(w, str(data.get("save_path", "")))
                                _write_secure_file(out_wrl, wrl_out, mode=0o600)
                                _write_secure_file(out_wrl.with_suffix(".meta.json"), json.dumps(data.get("meta", {}), indent=2), mode=0o600)
                                self.log("EXPORT", "#00ff66", f"Exported '{w}' -> {out_wrl} ({len(wrl_out)} bytes)")

                            elif mtype == "worlds_list":
                                self.worlds = data.get("worlds", ["hub"])
                                descs = data.get("descriptions", {})
                                self.world_descriptions.update(descs)
                                cur = data.get("current", self.world)
                                self.log("WORLDS", "#00ff66", f"Available ({len(self.worlds)}) - jump with /world <name>:")
                                for w in self.worlds:
                                    c_tag = " [cached]" if get_cached_world(self.server_url, w) else ""
                                    w_d = descs.get(w, "")
                                    self.log("WORLD", "#00ffcc", f"  {'*' if w == cur else '-'} {w}{c_tag}{f': {w_d}' if w_d else ''}")

                            elif mtype == "avatars_list":
                                self.avatars = data.get("avatars", [])
                                if not self.avatars:
                                    self.log("AVATARS", "#ffaa00", "No custom .wrl avatars uploaded yet. Use /upload avatar <name> <file.wrl>")
                                else:
                                    self.log("AVATARS", "#00ff66", f"Available 3D avatars (active: {data.get('current', self.avatar)}): {', '.join(self.avatars)}")

                            elif mtype == "avatar_data" and data.get("name") and data.get("wrl"):
                                mesh = build_normalized_avatar_mesh(data["wrl"])
                                if mesh:
                                    self._store_avatar_mesh(data["name"], mesh, wrl_text=data["wrl"])

                            elif mtype == "peer_avatar":
                                sid, av_name, av_wrl = data.get("short_id"), data.get("avatar", "default"), data.get("wrl", "")
                                if sid in self.peers:
                                    self.peers[sid]["avatar"] = av_name
                                if av_name and av_name != "default":
                                    mesh = build_normalized_avatar_mesh(av_wrl) if av_wrl else None
                                    if mesh:
                                        self._store_avatar_mesh(av_name, mesh, wrl_text=av_wrl)
                                    else:
                                        self._ensure_avatar_loaded(av_name)

                            elif mtype == "perms_list":
                                p = data.get("perms", {})
                                cur_cnt = data.get("current_world_count", max(0, len(self.worlds) - 1))
                                max_tot = f"{cur_cnt}/{p.get('max_total_worlds')}" if p.get("max_total_worlds") else f"{cur_cnt}/unlimited"
                                max_usr = p.get("max_worlds_per_user", 0) or "unlimited"
                                self.log("PERMS", "#00ff66", f"Default: {p.get('default_role', 'builder')} | Worlds: {max_tot} total, {max_usr}/user")
                                self.log("PERMS", "#00ffcc", f"Admins ({len(p.get('admins', []))}): {', '.join(k[:12] + '...' for k in p.get('admins', [])) or 'none'} | Builders ({len(p.get('builders', []))}): {', '.join(k[:12] + '...' for k in p.get('builders', [])) or 'none'} | Banned: {len(p.get('banned', []))}")

                            elif mtype == "friends_list":
                                flist, plist = data.get("friends", []), data.get("pending", [])
                                _write_secure_file(FRIENDS_CACHE_FILE, json.dumps(flist, indent=2), mode=0o600)
                                if not flist:
                                    self.log("FRIENDS", "#00ffcc", "No friends added yet. Use /addfriend <nick|id|pubkey>")
                                else:
                                    self.log("FRIENDS", "#00ff66", f"Friends ({len(flist)}): " + " | ".join(
                                        f"{f['nick']} ({f['short_id']}) [ONLINE in {f['world']}]" if f.get("online") else f"{f['nick']} ({f['short_id']}) [offline]"
                                        for f in flist
                                    ))
                                if plist:
                                    self.log("REQUESTS", "#ff66cc", f"Pending ({len(plist)}): " + ", ".join(f"{p['nick']} ({p['short_id']})" for p in plist) + " - type /accept <id>")

                            elif mtype == "whisper":
                                self.log(f"PM from {data.get('from_nick')}" if data.get("incoming") else f"PM to {data.get('to_nick')}", "#ff66ff" if data.get("incoming") else "#cc88ff", data.get("text", ""))

                            elif mtype == "teleport":
                                if data.get("pos") and len(data["pos"]) >= 3:
                                    self.teleport_req = [float(data["pos"][0]), float(data["pos"][1]), float(data["pos"][2])]
                                if data.get("msg"):
                                    self.log("TELEPORT", "#00ff66", data["msg"])

                            elif mtype == "role_updated":
                                self.role = data.get("role", "guest").upper()
                                self.log("SYSTEM", "#ff00aa", f"Your server role was updated to: {self.role}")

                            elif mtype == "peers":
                                for p in data.get("peers", []):
                                    self.peers[p["short_id"]] = p
                                    self._ensure_avatar_loaded(p.get("avatar", "default"))

                            elif mtype == "peer_join":
                                p = data["peer"]
                                self.peers[p["short_id"]] = p
                                p_av, p_wrl = p.get("avatar", "default"), str(data.get("avatar_wrl", ""))
                                if p_av and p_av != "default" and p_wrl:
                                    mesh = build_normalized_avatar_mesh(p_wrl)
                                    if mesh:
                                        self._store_avatar_mesh(p_av, mesh, wrl_text=p_wrl)
                                    else:
                                        self._ensure_avatar_loaded(p_av)
                                else:
                                    self._ensure_avatar_loaded(p_av)
                                self.log("WORLD", "#8888ff", f"{p['nick']} ({p['short_id']}) joined.")

                            elif mtype == "peer_leave":
                                self.peers.pop(data.get("short_id"), None)

                            elif mtype == "peer_move":
                                sid = data.get("short_id")
                                if sid in self.peers:
                                    self.peers[sid]["pos"] = data.get("pos", [0, 0, 0])
                                    self.peers[sid]["rot"] = data.get("rot", [0, 1, 0, 0])

                            elif mtype == "chat":
                                self.log(data.get("nick", "?"), data.get("color", "#00ffcc"), data.get("text", ""))

                            elif mtype == "emote":
                                sid, em = data.get("short_id"), data.get("emote", "wave")
                                if sid in self.peers:
                                    self.peers[sid]["emote"] = em
                                    self.peers[sid]["emote_until"] = time.time() + 2.5
                                self.log("*", "#ff66cc", f"{data.get('nick')} performs /{em}")

                            elif mtype == "upload_ok":
                                kind_up, name_up = data.get("kind"), data.get("name", "")
                                self.log("UPLOAD", "#00ff66", f"Uploaded {kind_up}: {name_up}")
                                if data.get("worlds"):
                                    self.worlds = data["worlds"]
                                if kind_up == "world":
                                    await ws.send(json.dumps({"type": "join_world", "world": name_up}))
                                elif kind_up == "avatar":
                                    self.avatar = name_up
                                    self.persist_profile()
                                    if name_up not in self.avatars:
                                        self.avatars.append(name_up)
                                    if data.get("wrl"):
                                        store_cached_avatar_wrl(name_up, str(data["wrl"]))

                            elif mtype in ("error", "info"):
                                if data.get("equipped_avatar"):
                                    self.avatar = str(data["equipped_avatar"])
                                    self.persist_profile()
                                if data.get("avatar_wrl"):
                                    store_cached_avatar_wrl(self.avatar, str(data["avatar_wrl"]))
                                self.log("SERVER", "#ffaa00", data.get("msg", ""))

                            if self.reconnect_target or self.offline_mode or not self.running:
                                break
                    finally:
                        self.active_ws = None
                        self.connected = False
                        self.short_id = ""
                        send_fut.cancel()
                        try:
                            await send_fut
                        except asyncio.CancelledError:
                            pass
            except Exception as e:
                self.active_ws = None
                self.connected = False
                self.short_id = ""
                if self.reconnect_target or self.offline_mode or not self.running:
                    continue
                self.role = "OFFLINE"
                if not self.scene.get("objects") and self.load_offline_world(self.world or "hub"):
                    self.log("CACHE", "#00ff66", f"Server unreachable ({e}) - loaded '{self.world}' from local cache while retrying.")
                elif self.running:
                    self.log("NET-ERR", "#ff4444", f"Disconnected ({e}). Retrying in 4s (or type /offline)...")
            if self.running and not self.offline_mode and not self.reconnect_target:
                await asyncio.sleep(4)


def normalize_ws_peer_client_url(raw_url: str) -> str:
    url = normalize_ws_url(raw_url)
    if not url or len(url) > 200 or any(ch in url for ch in ('"', "'", "\n", "\r", "\x00", " ")):
        return ""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'wss' else 8765)}"


WORLD_TEMPLATES: dict[str, dict] = {
    "flat": {
        "label": "Blank Neon Grid",
        "desc": "Clean 120x120 dark cyber-grid floor ready for /place building",
    },
    "cybercity": {
        "label": "Cyberpunk Megacity",
        "desc": "Neon-trimmed skyscrapers, glowing walkways, and central plaza",
    },
    "forest": {
        "label": "Emerald Pine Forest",
        "desc": "Grassy clearing surrounded by tall pine trees and warm sky",
    },
    "desert": {
        "label": "Solar Dunes & Pyramids",
        "desc": "Golden sandstone pyramids under an amber desert horizon",
    },
    "crystal": {
        "label": "Arctic Crystal Cavern",
        "desc": "Glacial ice floor with translucent cyan & violet crystal spires",
    },
    "fortress": {
        "label": "Obsidian Citadel",
        "desc": "Walled stone keep with four corner watchtowers and courtyard",
    },
    "island": {
        "label": "Tropical Atoll",
        "desc": "Sandy palm islands rising out of a deep turquoise sea",
    },
    "space": {
        "label": "Orbital Station Deck",
        "desc": "Deep-space platform ringed by glowing reactor spheres and beacons",
    },
}


def build_template_vrml_world(template_key: str, world_name: str, custom_desc: str = "") -> str:
    """Builds a clean, deterministic VRML97 (.wrl) world from a named template."""
    tkey = _clean_ident(template_key, 24, "flat").lower()
    if tkey not in WORLD_TEMPLATES:
        tkey = "flat"

    tmeta = WORLD_TEMPLATES[tkey]
    title_str = re.sub(r'["\r\n\x00]', " ", (custom_desc or tmeta["label"] or world_name)).strip()[:64]
    desc_str = re.sub(r'["\r\n\x00]', " ", (custom_desc or tmeta["desc"])).strip()[:200]
    seed = sum((idx + 1) * ord(ch) for idx, ch in enumerate(f"{world_name}:{tkey}")) or 42

    themes = {
        "flat": ("0.02 0.04 0.09, 0.05 0.12 0.20", "0.04 0.06 0.11", "0.00 0.95 0.85", "1.00 0.15 0.75"),
        "cybercity": ("0.03 0.01 0.12, 0.18 0.04 0.30", "0.06 0.07 0.14", "0.00 1.00 0.85", "1.00 0.14 0.75"),
        "forest": ("0.14 0.36 0.56, 0.35 0.62 0.78", "0.11 0.34 0.14", "0.18 0.72 0.25", "0.52 0.34 0.16"),
        "desert": ("0.42 0.18 0.07, 0.75 0.38 0.14", "0.62 0.40 0.18", "0.92 0.64 0.24", "0.98 0.38 0.10"),
        "crystal": ("0.06 0.18 0.38, 0.24 0.48 0.70", "0.68 0.82 0.92", "0.35 0.92 1.00", "0.70 0.50 1.00"),
        "fortress": ("0.12 0.10 0.20, 0.28 0.20 0.34", "0.24 0.24 0.28", "0.85 0.68 0.22", "0.75 0.20 0.25"),
        "island": ("0.05 0.28 0.52, 0.30 0.65 0.85", "0.04 0.32 0.58", "0.92 0.78 0.42", "0.15 0.85 0.75"),
        "space": ("0.01 0.01 0.05, 0.09 0.02 0.20", "0.18 0.20 0.28", "0.10 0.95 0.80", "1.00 0.52 0.05"),
    }
    sky, ground_c, accent1, accent2 = themes[tkey]

    chunks = [
        "#VRML V2.0 utf8",
        f'WorldInfo {{ title "{title_str}" info ["{desc_str}"] }}',
        'NavigationInfo { type ["WALK" "ANY"] headlight TRUE }',
        f"Background {{ skyColor [{sky}] skyAngle [1.2] }}",
        f"Transform {{ translation 0 -0.05 0 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {ground_c} emissiveColor 0.02 0.02 0.04 }} }} geometry Box {{ size 120 0.1 120 }} }} ] }}",
    ]

    if tkey == "flat":
        # Raised starter pad + 4 corner beacon markers so spawn orientation is immediately clear
        chunks.append("Transform { translation 0 0.08 0 children [ Shape { appearance Appearance { material Material { diffuseColor 0.08 0.14 0.22 emissiveColor 0.02 0.20 0.26 } } geometry Box { size 10 0.16 10 } } ] }")
        for bx, bz in ((-4.5, -4.5), (4.5, -4.5), (-4.5, 4.5), (4.5, 4.5)):
            chunks.append(f"Transform {{ translation {bx} 0.65 {bz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent1} emissiveColor {accent1} }} }} geometry Cylinder {{ radius 0.14 height 1.1 }} }} ] }}")
    elif tkey == "fortress":
        for cx, cz in ((-14, -22), (14, -22), (-14, 4), (14, 4)):
            h = 8.5
            chunks.append(f"Transform {{ translation {cx} {h * 0.5:.2f} {cz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.28 0.28 0.34 emissiveColor 0.04 0.04 0.06 }} }} geometry Cylinder {{ radius 2.1 height {h:.2f} }} }} ] }}")
            chunks.append(f"Transform {{ translation {cx} {h + 1.2:.2f} {cz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent1} emissiveColor {accent1} }} }} geometry Cone {{ bottomRadius 2.5 height 2.5 }} }} ] }}")
        chunks.append("Transform { translation 0 2.0 -22 children [ Shape { appearance Appearance { material Material { diffuseColor 0.24 0.24 0.30 emissiveColor 0.04 0.04 0.06 } } geometry Box { size 24 4.0 1.2 } } ] }")
        chunks.append("Transform { translation -14 2.0 -9 children [ Shape { appearance Appearance { material Material { diffuseColor 0.24 0.24 0.30 emissiveColor 0.04 0.04 0.06 } } geometry Box { size 1.2 4.0 24 } } ] }")
        chunks.append("Transform { translation 14 2.0 -9 children [ Shape { appearance Appearance { material Material { diffuseColor 0.24 0.24 0.30 emissiveColor 0.04 0.04 0.06 } } geometry Box { size 1.2 4.0 24 } } ] }")
        chunks.append(f"Transform {{ translation 0 1.4 -9 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent2} emissiveColor {accent2} }} }} geometry Sphere {{ radius 1.4 }} }} ] }}")
    elif tkey == "island":
        chunks.append(f"Transform {{ translation 0 0.25 0 children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent1} emissiveColor 0.06 0.05 0.02 }} }} geometry Cylinder {{ radius 7.5 height 0.5 }} }} ] }}")
        for idx in range(9):
            ang = (2.0 * math.pi * idx) / 9.0
            rad = 12.0 + ((idx * 5 + seed) % 10)
            ix, iz = round(math.cos(ang) * rad, 2), round(math.sin(ang) * rad - 4.0, 2)
            r_isl = round(2.8 + (idx % 3) * 1.1, 2)
            chunks.append(f"Transform {{ translation {ix} 0.25 {iz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent1} emissiveColor 0.06 0.05 0.02 }} }} geometry Cylinder {{ radius {r_isl} height 0.5 }} }} ] }}")
            h_palm = round(3.2 + ((idx * 3 + seed) % 4), 2)
            chunks.append(f"Transform {{ translation {ix} {0.5 + h_palm * 0.5:.2f} {iz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.45 0.28 0.12 emissiveColor 0.05 0.02 0.01 }} }} geometry Cylinder {{ radius 0.28 height {h_palm:.2f} }} }} ] }}")
            chunks.append(f"Transform {{ translation {ix} {0.5 + h_palm + 0.6:.2f} {iz} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent2} emissiveColor {accent2} }} }} geometry Cone {{ bottomRadius 1.8 height 1.5 }} }} ] }}")
    else:
        for idx in range(14):
            angle = (2.0 * math.pi * idx) / 14.0
            radius = 11.0 + ((idx * 7 + seed) % 12)
            x = round(math.cos(angle) * radius, 2)
            z = round(math.sin(angle) * radius - 4.0, 2)
            h = round(3.2 + ((idx * 5 + seed) % 9), 2)
            col = accent1 if idx % 2 == 0 else accent2
            if tkey == "forest":
                chunks.append(f"Transform {{ translation {x} {h * 0.35:.2f} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.42 0.26 0.12 emissiveColor 0.08 0.04 0.02 }} }} geometry Cylinder {{ radius 0.45 height {h * 0.7:.2f} }} }} ] }}")
                chunks.append(f"Transform {{ translation {x} {h * 0.85:.2f} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {accent1} emissiveColor 0.03 0.14 0.05 }} }} geometry Cone {{ bottomRadius 2.4 height {h * 0.65:.2f} }} }} ] }}")
            elif tkey == "desert":
                chunks.append(f"Transform {{ translation {x} {h * 0.5:.2f} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {col} emissiveColor 0.18 0.08 0.02 }} }} geometry Cone {{ bottomRadius {round(h * 1.35 * 0.65, 2)} height {h} }} }} ] }}")
            elif tkey == "crystal":
                chunks.append(f"Transform {{ translation {x} {h * 0.5:.2f} {z} rotation 0.2 1.0 0.15 {round((idx * 0.45) % 3.14, 2)} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {col} emissiveColor 0.10 0.28 0.35 transparency 0.2 }} }} geometry Cone {{ bottomRadius 1.3 height {h} }} }} ] }}")
            elif tkey == "space":
                chunks.append(f"Transform {{ translation {x} {round(2.5 + (idx % 4) * 1.6, 2)} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {col} emissiveColor {col} }} }} geometry Sphere {{ radius {round(1.1 + (idx % 3) * 0.5, 2)} }} }} ] }}")
            else:  # cybercity
                w = round(2.4 + ((idx + seed) % 3) * 0.9, 2)
                chunks.append(f"Transform {{ translation {x} {h * 0.5:.2f} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor 0.10 0.12 0.22 emissiveColor 0.03 0.04 0.08 }} }} geometry Box {{ size {w} {h} {w} }} }} ] }}")
                chunks.append(f"Transform {{ translation {x} {h + 0.45:.2f} {z} children [ Shape {{ appearance Appearance {{ material Material {{ diffuseColor {col} emissiveColor {col} }} }} geometry Box {{ size {round(w * 1.06, 2)} 0.35 {round(w * 1.06, 2)} }} }} ] }}")

    return "\n".join(chunks) + "\n"


def create_world_from_template(client: MetaverseClient, world_name: str, template_key: str = "flat", custom_desc: str = ""):
    clean_wname = _clean_ident(world_name, 32, "")
    if not clean_wname:
        client.log("ERR", "#ff4444", "Specify a valid world name (letters, numbers, '_', '-').")
        return
    if clean_wname.lower() == "hub":
        client.log("ERR", "#ff4444", "Cannot overwrite core 'hub' world. Choose another world name.")
        return
    tkey = _clean_ident(template_key, 24, "flat").lower()
    if tkey not in WORLD_TEMPLATES:
        avail = ", ".join(WORLD_TEMPLATES.keys())
        client.log("ERR", "#ff4444", f"Unknown template '{template_key}'. Available: {avail}")
        return

    tmeta = WORLD_TEMPLATES[tkey]
    desc_out = (custom_desc.strip() or tmeta["desc"])[:200]
    wrl = build_template_vrml_world(tkey, clean_wname, desc_out)
    client.log("CREATE", "#00ff66", f"Creating '{clean_wname}' from template '{tkey}' ({tmeta['label']})...")

    if client.offline_mode or not client.connected:
        store_cached_world(client.server_url, clean_wname, wrl, desc=desc_out)
        client.load_offline_world(clean_wname)
        return

    client.send_json({
        "type": "upload",
        "kind": "world",
        "name": clean_wname,
        "filename": f"{clean_wname}.wrl",
        "description": desc_out,
        "content": wrl,
    })


def _extract_placed_id_and_nums(sub_tokens: list[str], looked_at_hit, expected_num_count: int | None = None):
    target_id = ""
    nums = []
    for tok in sub_tokens:
        if not target_id and re.fullmatch(r"[0-9a-fA-F]{4,8}", tok):
            if expected_num_count is None or len(sub_tokens) > expected_num_count:
                target_id = tok.lower()
                continue
        try:
            val = float(tok)
            if math.isfinite(val):
                nums.append(val)
        except ValueError:
            pass
    if not target_id and looked_at_hit is not None:
        def_n = looked_at_hit[0].get("def_name", "")
        if def_n.startswith("PLACED_"):
            target_id = def_n.split("_", 1)[1].lower()
    return target_id, nums


def handle_command(client: MetaverseClient, text: str):
    cleaned = " ".join(text.strip().split())
    parts = cleaned.split(" ", 2)
    cmd = parts[0].lower()

    if cmd == "/help":
        client.log("COMMANDS", "#ff66cc", "Jump: /worlds | /world <name> | /tp [x y z] | /spawn | /peers | /hop <node> [world] | /back")
        client.log("COMMANDS", "#ff66cc", "Build: /place <shape> [color] [scale] | /moveobj <dx dy dz> | /rotateobj <deg> | /paint <color>")
        client.log("COMMANDS", "#ff66cc", "Edit: /scaleobj <mult> | /cloneobj [dx dy dz] | /delobj [id] | /undo | /setspawn | /clearspawn")
        client.log("COMMANDS", "#ff66cc", "Worlds: /templates | /createworld <name> [template] [desc] | /upload | /info | /inspect | [I]=Scan")
    elif cmd in ("/quit", "/exit"):
        client.running = False
    elif cmd == "/crt":
        client.crt_mode = not client.crt_mode
        client.log("DISPLAY", "#00ff66" if client.crt_mode else "#ffaa00", f"90s CRT scanlines {'ENABLED' if client.crt_mode else 'DISABLED'}.")
    elif cmd == "/setspawn":
        px, py, pz = client.cam_pos_ref
        client.send_json({
            "type": "world_edit", "action": "set_spawn", "world": client.world,
            "x": round(px, 2), "y": round(py - 1.8, 2), "z": round(pz, 2), "yaw": round((-client.cam_yaw_ref) % 360.0, 1),
        })
    elif cmd == "/clearspawn":
        client.send_json({"type": "world_edit", "action": "clear_spawn", "world": client.world})
    elif cmd == "/spawn":
        sp = client.scene.get("spawn_pos")
        if sp and len(sp) >= 3:
            client.teleport_req = [float(sp[0]), float(sp[1]) + 1.8, float(sp[2])]
            client.teleport_yaw = float(client.scene.get("spawn_yaw", 0.0) or 0.0)
            client.log("SPAWN", "#00ff66", f"Returned to custom spawn ({sp[0]:.1f}, {sp[1]:.1f}, {sp[2]:.1f}).")
        elif client.world.lower() == "hub":
            client.teleport_req = [0.0, 4.05, 13.5]
            client.teleport_yaw = 0.0
            client.log("SPAWN", "#00ff66", "Returned to Neo-Genesis Hub spawn.")
        else:
            client.teleport_req = [0.0, 2.2, 12.0]
            client.teleport_yaw = 0.0
            client.log("SPAWN", "#00ff66", "Returned to default world spawn.")
    elif cmd == "/info":
        target_w = parts[1].strip() if len(parts) > 1 and parts[1].strip() else client.world
        if client.offline_mode or not client.connected:
            cached = get_cached_world(client.server_url, target_w)
            objs = client.scene.get("objects", []) if target_w == client.world else []
            if cached:
                client.log("WORLD-INFO", "#00ff66", f"=== {cached['world']} [OFFLINE CACHE] - {max(1, int(cached.get('bytes', 0)) // 1024)} KB, {len(objs)} objs ===")
                client.log("ABOUT", "#00ffcc", f"Description: {cached.get('description') or '(none)'} | SHA-256: {cached.get('sha256', '')[:16]}...")
                if cached.get("radio_url"):
                    client.log("RADIO", "#ff66cc", f"Radio: {cached.get('radio_name') or cached['radio_url']}")
            else:
                client.log("WORLD-INFO", "#ffaa00", f"Offline mode: '{target_w}' has no cached metadata.")
            return
        client.send_json({"type": "get_world_info", "world": target_w})
    elif cmd == "/place":
        sub = cleaned.split()
        shape = sub[1].lower() if len(sub) >= 2 else "box"
        if shape not in ("box", "pillar", "sphere", "cone", "arch", "platform", "wall"):
            client.log("ERR", "#ff4444", "Usage: /place <box|pillar|sphere|cone|arch|platform|wall> [color] [scale]")
            return
        color_arg, scale_arg = "cyan", 1.0
        for tok in sub[2:]:
            try:
                val = float(tok)
                if math.isfinite(val):
                    scale_arg = max(0.2, min(20.0, val))
            except ValueError:
                color_arg = tok
        if client.inspect_mode and client.looked_at_hit is not None:
            px, py, pz = compute_surface_placement_coords(shape, scale_arg, client.looked_at_hit[2], client.looked_at_hit[3])
        else:
            rad = math.radians(client.cam_yaw_ref)
            cx, cy, cz = client.cam_pos_ref
            px, py, pz = round(cx - math.sin(rad) * 4.5, 2), round(max(0.0, cy - 1.8), 2), round(cz - math.cos(rad) * 4.5, 2)
        client.send_json({
            "type": "world_edit", "action": "place", "world": client.world,
            "shape": shape, "x": px, "y": py, "z": pz, "scale": scale_arg, "color": color_arg, "yaw": round((-client.cam_yaw_ref) % 360.0, 1),
        })
    elif cmd == "/moveobj":
        target_id, nums = _extract_placed_id_and_nums(cleaned.split()[1:], client.looked_at_hit, 3)
        if not target_id or len(nums) < 3:
            client.log("ERR", "#ff4444", "Usage: Aim at a placed object with [I] and run /moveobj <dx> <dy> <dz> (or /moveobj <id> <dx> <dy> <dz>)")
            return
        client.send_json({"type": "world_edit", "action": "mutate_placed", "world": client.world, "op": "move", "tag_id": target_id, "dx": nums[0], "dy": nums[1], "dz": nums[2]})
    elif cmd == "/rotateobj":
        target_id, nums = _extract_placed_id_and_nums(cleaned.split()[1:], client.looked_at_hit, 1)
        if not target_id or not nums:
            client.log("ERR", "#ff4444", "Usage: Aim at a placed object with [I] and run /rotateobj <degrees> (or /rotateobj <id> <degrees>)")
            return
        client.send_json({"type": "world_edit", "action": "mutate_placed", "world": client.world, "op": "rotate", "tag_id": target_id, "yaw_delta": nums[0]})
    elif cmd == "/paint":
        sub = cleaned.split()
        target_id, color_val = "", ""
        if len(sub) >= 3 and re.fullmatch(r"[0-9a-fA-F]{4,8}", sub[1]):
            target_id, color_val = sub[1].lower(), sub[2]
        elif len(sub) >= 2:
            color_val = sub[1]
        if not target_id and client.looked_at_hit is not None and client.looked_at_hit[0].get("def_name", "").startswith("PLACED_"):
            target_id = client.looked_at_hit[0]["def_name"].split("_", 1)[1].lower()
        if not target_id or not color_val:
            client.log("ERR", "#ff4444", "Usage: Aim at a placed object with [I] and run /paint <color|#hex> (or /paint <id> <color>)")
            return
        client.send_json({"type": "world_edit", "action": "mutate_placed", "world": client.world, "op": "paint", "tag_id": target_id, "color": color_val})
    elif cmd == "/scaleobj":
        target_id, nums = _extract_placed_id_and_nums(cleaned.split()[1:], client.looked_at_hit, 1)
        if not target_id or not nums:
            client.log("ERR", "#ff4444", "Usage: Aim at a placed object with [I] and run /scaleobj <multiplier> (or /scaleobj <id> <multiplier>)")
            return
        client.send_json({"type": "world_edit", "action": "mutate_placed", "world": client.world, "op": "scale", "tag_id": target_id, "scale_mult": nums[0]})
    elif cmd == "/cloneobj":
        target_id, nums = _extract_placed_id_and_nums(cleaned.split()[1:], client.looked_at_hit, None)
        if not target_id:
            client.log("ERR", "#ff4444", "Usage: Aim at a placed object with [I] and run /cloneobj [dx dy dz] (or /cloneobj <id> [dx dy dz])")
            return
        client.send_json({
            "type": "world_edit", "action": "mutate_placed", "world": client.world, "op": "clone", "tag_id": target_id,
            "dx": nums[0] if len(nums) >= 1 else 1.5, "dy": nums[1] if len(nums) >= 2 else 0.0, "dz": nums[2] if len(nums) >= 3 else 0.0,
        })
    elif cmd == "/delobj":
        tag_id = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
        if not tag_id and client.looked_at_hit is not None and client.looked_at_hit[0].get("def_name", "").startswith("PLACED_"):
            tag_id = client.looked_at_hit[0]["def_name"].split("_", 1)[1]
        if not tag_id:
            client.log("ERR", "#ff4444", "Aim at a placed object with [I] and run /delobj, or pass /delobj <id>.")
            return
        client.send_json({"type": "world_edit", "action": "delete_placed", "world": client.world, "tag_id": tag_id})
    elif cmd == "/undo":
        client.send_json({"type": "world_edit", "action": "undo", "world": client.world})
    elif cmd == "/cache":
        entries = list_cached_worlds()
        if not entries:
            client.log("CACHE", "#ffaa00", f"No cached worlds yet ({WORLD_CACHE_DIR}). Visit worlds online or use /openwrl <file.wrl>.")
        else:
            client.log("CACHE", "#00ff66", f"Local World Cache ({len(entries)} worlds, {sum(int(e.get('bytes', 0)) for e in entries) // 1024} KB total):")
            for e in entries[:12]:
                client.log("CACHED", "#00ffcc", f"  {e['world']} ({max(1, int(e.get('bytes', 0)) // 1024)} KB, sha256:{e['sha256'][:10]}) - {e['server']}")
    elif cmd == "/clearcache":
        client.log("CACHE", "#00ff66", f"Cleared {clear_cached_worlds()} cached world(s) from {WORLD_CACHE_DIR}.")
    elif cmd == "/offline":
        client.enter_offline_mode(parts[1].strip() if len(parts) > 1 and parts[1].strip() else "")
    elif cmd == "/online":
        client.exit_offline_mode()
    elif cmd == "/openwrl":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /openwrl <path/to/scene.wrl>")
            return
        p = Path(cleaned.split(" ", 1)[1].strip('"').strip("'")).expanduser()
        if not p.is_file():
            client.log("ERR", "#ff4444", f"Local .wrl file not found: {p}")
            return
        w_stem = _clean_ident(p.stem, 32, "local_scene")
        store_cached_world(client.server_url, w_stem, p.read_text(encoding="utf-8", errors="replace"), desc=f"Local file: {p.name}")
        client.offline_mode = True
        client.role = "OFFLINE-LOCAL"
        client._trigger_ws_reconnect()
        client.load_offline_world(w_stem)
    elif cmd == "/acl":
        client.send_json({"type": "world_acl", "action": "list", "world": parts[1].strip() if len(parts) > 1 and parts[1].strip() else client.world})
    elif cmd == "/privateworld":
        if len(parts) < 2 or parts[1].lower() not in ("on", "off", "true", "false", "1", "0"):
            client.log("ERR", "#ff4444", "Usage: /privateworld <on|off>")
            return
        client.send_json({"type": "world_acl", "action": "set_private", "world": client.world, "private": parts[1].lower() in ("on", "true", "1")})
    elif cmd in ("/allowbuilder", "/removebuilder", "/allowvisitor", "/revokevisitor"):
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", f"Usage: {cmd} <nick|short_id|pubkey>")
            return
        act_map = {"/allowbuilder": "add_builder", "/removebuilder": "remove_builder", "/allowvisitor": "allow_visitor", "/revokevisitor": "revoke_visitor"}
        client.send_json({"type": "world_acl", "action": act_map[cmd], "world": client.world, "target": parts[1].strip()})
    elif cmd == "/avatars":
        client.send_json({"type": "list_avatars"})
    elif cmd == "/avatar":
        if len(parts) < 2 or not parts[1].strip():
            client.log("AVATAR", "#00ffcc", f"Current avatar: {client.avatar} (usage: /avatar <name|default> or /avatars)")
            return
        target_av = _clean_ident(parts[1].strip(), 32, "default")
        client.avatar = target_av
        client.persist_profile()
        client.send_json({"type": "set_avatar", "name": target_av})
    elif cmd == "/servers":
        sdir = load_server_directory()
        bmarks, recent = sdir.get("bookmarks", {}), sdir.get("recent", [])
        if not bmarks:
            client.log("SERVERS", "#00ffcc", "No saved bookmarks. Use /bookmark [ws://host:port] [name]")
        else:
            client.log("SERVERS", "#00ff66", f"Bookmarks ({len(bmarks)}):")
            for bname, burl in bmarks.items():
                client.log("BOOKMARK", "#00ffcc", f"  {bname} -> {burl}")
        if recent:
            client.log("RECENT", "#8888ff", "Recent: " + ", ".join(f"{r.get('node_name', 'node')} ({r.get('url')})" for r in recent[:5]))
    elif cmd == "/bookmark":
        sub = cleaned.split(" ", 2)
        if len(sub) == 1:
            url_arg, name_arg = client.server_url, _clean_ident(client.node_name, 24, "node")
        elif len(sub) == 2:
            if sub[1].startswith(("ws://", "wss://")) or ":" in sub[1]:
                url_arg = sub[1]
                name_arg = _clean_ident(urllib.parse.urlsplit(normalize_ws_url(url_arg)).hostname or "node", 24, "node")
            else:
                url_arg, name_arg = client.server_url, _clean_ident(sub[1], 24, "node")
        else:
            url_arg, name_arg = sub[1], _clean_ident(sub[2], 24, "node")
        norm_u = normalize_ws_peer_client_url(url_arg)
        if not norm_u:
            client.log("ERR", "#ff4444", "Invalid WebSocket URL for bookmark.")
            return
        sdir = load_server_directory()
        sdir["bookmarks"][name_arg] = norm_u
        save_server_directory(sdir)
        client.log("SERVERS", "#00ff66", f"Bookmarked '{name_arg}' -> {norm_u} (jump with /hop {name_arg})")
    elif cmd == "/unbookmark":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /unbookmark <bookmark_name|url>")
            return
        target_b = parts[1].strip()
        sdir = load_server_directory()
        bmarks = sdir.get("bookmarks", {})
        removed = next((k for k, v in list(bmarks.items()) if k.lower() == target_b.lower() or v == target_b), None)
        if removed:
            bmarks.pop(removed, None)
            save_server_directory(sdir)
            client.log("SERVERS", "#00ff66", f"Removed bookmark '{removed}'.")
        else:
            client.log("ERR", "#ff4444", f"Bookmark '{target_b}' not found.")
    elif cmd == "/hop":
        sub = cleaned.split(" ", 2)
        if len(sub) < 2 or not sub[1].strip():
            client.log("ERR", "#ff4444", "Usage: /hop <bookmark_name|ws://host:port> [world]")
            return
        bmarks = load_server_directory().get("bookmarks", {})
        matched_url = next((v for k, v in bmarks.items() if k.lower() == sub[1].strip().lower()), sub[1].strip())
        client.hop_to_server(matched_url, sub[2].strip() if len(sub) >= 3 else "hub")
    elif cmd == "/back":
        if not client.prev_server:
            client.log("INFO", "#ffaa00", "No previous server to return to.")
            return
        prev_url, prev_w = client.prev_server
        client.prev_server = None
        client.hop_to_server(prev_url, prev_w)
    elif cmd == "/peers":
        client.send_json({"type": "federation", "action": "list"})
    elif cmd == "/linkserver":
        sub = cleaned.split(" ", 3)
        if len(sub) < 2 or not sub[1].strip():
            client.log("ERR", "#ff4444", "Usage: /linkserver <ws://host:port> [label] [default_world]")
            return
        client.send_json({"type": "federation", "action": "link", "url": sub[1].strip(), "label": sub[2].strip() if len(sub) >= 3 else "", "default_world": sub[3].strip() if len(sub) >= 4 else "hub"})
    elif cmd == "/unlinkserver":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /unlinkserver <ws://host:port|label>")
            return
        client.send_json({"type": "federation", "action": "unlink", "url": parts[1].strip()})
    elif cmd == "/nodename":
        if len(parts) < 2 or not parts[1].strip():
            client.log("FEDERATION", "#00ffcc", f"Current node name: {client.node_name}")
            return
        client.send_json({"type": "federation", "action": "set_identity", "node_name": cleaned.split(" ", 1)[1].strip()})
    elif cmd in ("/maxworlds", "/userworldlimit"):
        if len(parts) < 2:
            client.send_json({"type": "admin_perm", "action": "list_perms"})
            return
        if not parts[1].strip().isdigit():
            client.log("ERR", "#ff4444", f"Usage: {cmd} <count> (0 = unlimited)")
            return
        client.send_json({"type": "admin_perm", "action": "set_max_worlds" if cmd == "/maxworlds" else "set_user_world_limit", "limit": int(parts[1].strip())})
    elif cmd == "/backup":
        sub = cleaned.split(" ", 2)
        if len(sub) >= 3:
            target_w, lbl = sub[1], sub[2]
        elif len(sub) == 2:
            target_w, lbl = (sub[1], "") if (sub[1] in client.worlds or sub[1] == client.world) else (client.world, sub[1])
        else:
            target_w, lbl = client.world, ""
        client.send_json({"type": "world_backup", "action": "create", "world": target_w, "label": lbl})
    elif cmd == "/backups":
        client.send_json({"type": "world_backup", "action": "list", "world": parts[1].strip() if len(parts) > 1 and parts[1].strip() else client.world})
    elif cmd == "/restore":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /restore <snapshot_file.wrl> [world_name]")
            return
        sub = cleaned.split(" ", 2)
        client.send_json({"type": "world_backup", "action": "restore", "world": sub[2].strip() if len(sub) >= 3 else client.world, "file": sub[1].strip()})
    elif cmd == "/export":
        sub = cleaned.split(" ", 2)
        if len(sub) == 1:
            client.send_json({"type": "world_backup", "action": "export", "world": client.world, "save_path": ""})
        elif len(sub) == 2:
            arg1 = sub[1].strip('"').strip("'")
            if arg1.endswith((".wrl", "/", "\\")) or "/" in arg1 or "\\" in arg1:
                client.send_json({"type": "world_backup", "action": "export", "world": client.world, "save_path": arg1})
            else:
                client.send_json({"type": "world_backup", "action": "export", "world": arg1, "save_path": ""})
        else:
            client.send_json({"type": "world_backup", "action": "export", "world": sub[1].strip(), "save_path": sub[2].strip('"').strip("'")})
    elif cmd == "/fly":
        if len(parts) > 1 and parts[1].lower() in ("on", "true", "1", "off", "false", "0"):
            client.fly_mode = parts[1].lower() in ("on", "true", "1")
        else:
            client.fly_mode = not client.fly_mode
        client.vel_y = 0.0
        if not client.fly_mode:
            client.unstuck_req = True
        client.log("MOVE", "#00ff66" if client.fly_mode else "#00ffcc", "Fly mode ENABLED (Space=Up, Shift=Down, noclip)." if client.fly_mode else "Fly mode DISABLED (gravity & collision active).")
    elif cmd == "/radio":
        if len(parts) < 2 or not parts[1].strip():
            client.log("RADIO", "#00ffcc" if client.radio.url else "#ffaa00", f"[{'MUTED' if client.radio.muted else 'PLAYING'}] {client.radio.name} ({client.radio.url}) - /mute to toggle" if client.radio.url else "No station set. Usage: /radio <http_stream_url> [Station Name] | /radio off")
            return
        arg_rest = cleaned.split(" ", 1)[1].strip()
        if arg_rest.lower() in ("off", "stop", "clear", "none"):
            client.send_json({"type": "set_world_meta", "world": client.world, "radio_url": "", "radio_name": ""})
            return
        r_parts = arg_rest.split(" ", 1)
        ok_url, err_msg = is_safe_radio_url(r_parts[0].strip())
        if not ok_url:
            client.log("ERR", "#ff4444", err_msg)
            return
        client.send_json({"type": "set_world_meta", "world": client.world, "radio_url": r_parts[0].strip(), "radio_name": r_parts[1].strip() if len(r_parts) > 1 else r_parts[0].strip()})
    elif cmd == "/mute":
        is_muted = client.radio.toggle_mute()
        client.log("RADIO", "#ffaa00" if is_muted else "#00ff66", "Radio muted." if is_muted else f"Radio unmuted ({client.radio.name or 'no stream'}).")
    elif cmd == "/desc":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ABOUT" if client.world_desc else "INFO", "#00ffcc" if client.world_desc else "#ffaa00", f"'{client.world}': {client.world_desc}" if client.world_desc else "No description set. Usage: /desc [world] <description>")
            return
        sub = cleaned.split(" ", 2)
        if len(sub) >= 3 and sub[1] in client.worlds:
            client.send_json({"type": "set_world_meta", "world": sub[1], "description": sub[2]})
        else:
            client.send_json({"type": "set_world_meta", "world": client.world, "description": cleaned.split(" ", 1)[1].strip()})
    elif cmd == "/worldtitle":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /worldtitle <display title>")
            return
        client.send_json({"type": "set_world_meta", "world": client.world, "title": cleaned.split(" ", 1)[1].strip()})
    elif cmd == "/worldtags":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /worldtags <tag1, tag2, ...>")
            return
        client.send_json({"type": "set_world_meta", "world": client.world, "tags": [t.strip() for t in cleaned.split(" ", 1)[1].split(",") if t.strip()]})
    elif cmd == "/friends":
        client.send_json({"type": "friend_action", "action": "list"})
    elif cmd in ("/addfriend", "/accept", "/unfriend", "/goto") and len(parts) > 1:
        f_act = {"/addfriend": "add", "/accept": "accept", "/unfriend": "remove", "/goto": "goto"}[cmd]
        client.send_json({"type": "friend_action", "action": f_act, "target": parts[1]})
    elif cmd == "/msg":
        if len(parts) < 3:
            client.log("ERR", "#ff4444", "Usage: /msg <friend_nick|id> <message>")
            return
        client.send_json({"type": "friend_action", "action": "whisper", "target": parts[1], "text": parts[2]})
    elif cmd == "/plugins":
        if not client.plugins:
            client.log("PLUGINS", "#ffaa00", "No .x87 plugins loaded in ./plugins/")
        else:
            for cname, meta in client.plugins.items():
                client.log("PLUGIN", "#00ff66", f"{cname} - {meta.get('desc', 'Custom command')} [{meta['file']}]")
        client.log("PLUGINS", "#00ffcc", "Server plugins (data/plugins/): /pillar, /announce")
    elif cmd == "/reload":
        client.plugins = load_x87_plugins()
        client.log("PLUGINS", "#00ff66", f"Reloaded {len(client.plugins)} custom command(s) from ./plugins/")
    elif cmd in client.plugins:
        spec = client.plugins[cmd]
        ctx = {
            "nick": client.nick,
            "world": client.world,
            "role": client.role,
            "args": re.sub(r"[\x00-\x1f\x7f\{\}]", "", cleaned.split(" ", 1)[1] if " " in cleaned else ""),
        }

        def run_step(action: str, val: str):
            rendered = val.format_map({**ctx, **{k: "" for k in ("nick", "world", "role", "args")}})
            if action == "chat":
                client.send_json({"type": "chat", "text": rendered.lstrip("/")})
            elif action == "emote":
                client.send_json({"type": "chat", "text": f"/emote {rendered}"})
            elif action == "world":
                client.request_join_world(rendered)
            elif action == "connect":
                client.hop_to_server(rendered, "hub")
            elif action == "fly":
                client.fly_mode = not client.fly_mode
                client.vel_y = 0.0
                if not client.fly_mode:
                    client.unstuck_req = True
            elif action == "quit":
                client.running = False
            elif action in ("createworld", "template"):
                wname, _, wrest = rendered.partition(" ")
                wtpl, _, wdesc = wrest.partition(" ")
                if wname:
                    create_world_from_template(client, wname, wtpl or "flat", wdesc)

        if spec.get("action", "chat").lower() == "multi":
            for step_line in spec.get("steps", []):
                s_act, _, s_val = step_line.partition(" ")
                run_step(s_act.lower(), s_val)
        else:
            run_step(spec.get("action", "chat").lower(), spec.get("value", ""))
    elif cmd == "/delworld":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /delworld <world_name>")
            return
        client.send_json({"type": "delete_world", "world": parts[1].strip()})
    elif cmd == "/inspect":
        if client.looked_at_hit is not None:
            obj, dist, _, _ = client.looked_at_hit
            px, py, pz = obj["pos"]
            r, g, b = obj["diff"]
            def_lbl = f" DEF={obj['def_name']}" if obj.get("def_name") else ""
            client.log("TARGET", "#00ff66", f"Object #{obj['_index']} ({obj['kind']}{def_lbl}) at ({px:.2f}, {py:.2f}, {pz:.2f}) dist={dist:.1f}m RGB=({r:.2f},{g:.2f},{b:.2f})")
            return
        objs = client.scene.get("objects", [])
        kinds = {}
        for o in objs:
            kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
        client.log("INSPECT", "#00ff66", f"World '{client.world}': {client.scene.get('raw_len', 0)} bytes, {len(objs)} objects {kinds}")
        if client.world_desc:
            client.log("ABOUT", "#00ffcc", client.world_desc)
        if client.radio.url:
            client.log("RADIO", "#ff66cc", f"{client.radio.name} ({client.radio.url})")
        if objs:
            client.log("INSPECT", "#00ffcc", f"Objects center: ({sum(o['pos'][0] for o in objs)/len(objs):.1f}, {sum(o['pos'][1] for o in objs)/len(objs):.1f}, {sum(o['pos'][2] for o in objs)/len(objs):.1f}) - press [I] to toggle crosshair raycast")
        else:
            client.log("INSPECT", "#ffaa00", f"Preview: {client.scene.get('raw_preview', '(empty)')}")
    elif cmd == "/tp":
        if len(parts) == 1:
            objs = client.scene.get("objects", [])
            if objs:
                target = next((o for o in objs if o["kind"] != "grid" and o.get("size", (0, 0, 0))[0] < 30), objs[0])
                tx, ty, tz = target["pos"]
                client.teleport_req = [tx, max(2.0, ty + 2.0), tz + 12.0]
                client.log("TP", "#00ff66", f"Teleported near object at ({tx:.1f}, {ty:.1f}, {tz:.1f})")
            else:
                client.teleport_req = [0.0, 2.0, 12.0]
        else:
            coords = [v for v in _safe_floats(cleaned) if math.isfinite(v)]
            if len(coords) >= 3:
                client.teleport_req = [max(-5000.0, min(5000.0, coords[0])), max(-500.0, min(5000.0, coords[1])), max(-5000.0, min(5000.0, coords[2]))]
                client.log("TP", "#00ff66", f"Teleported to ({client.teleport_req[0]}, {client.teleport_req[1]}, {client.teleport_req[2]})")
            else:
                client.log("ERR", "#ff4444", "Usage: /tp [x y z]")
    elif cmd == "/worlds":
        if client.offline_mode or not client.connected:
            entries = list_cached_worlds()
            if not entries:
                client.log("OFFLINE", "#ffaa00", "No cached worlds available offline.")
            else:
                client.log("OFFLINE-WORLDS", "#00ff66", f"Cached Worlds ({len(entries)}):")
                for e in entries:
                    d_str = f": {e['description']}" if e.get("description") else ""
                    client.log("WORLD", "#00ffcc", f"  {'*' if e['world'] == client.world else '-'} {e['world']}{d_str}")
            return
        client.send_json({"type": "list_worlds"})
    elif cmd == "/perms":
        client.send_json({"type": "admin_perm", "action": "list_perms"})
    elif cmd in ("/admin", "/unadmin", "/grant", "/ungrant", "/ban", "/unban", "/defaultrole") and len(parts) > 1:
        p_act = {
            "/admin": "grant_admin", "/unadmin": "revoke_admin",
            "/grant": "grant_builder", "/ungrant": "revoke_builder",
            "/ban": "ban", "/unban": "unban", "/defaultrole": "set_default",
        }[cmd]
        client.send_json({"type": "admin_perm", "action": p_act, "target_key": parts[1].lower() if cmd == "/defaultrole" else parts[1]})
    elif cmd == "/nick" and len(parts) > 1:
        clean_n = _clean_ident(parts[1].strip(), 24)
        if not clean_n:
            client.log("ERR", "#ff4444", "Nickname must contain letters, numbers, '_' or '-'.")
            return
        client.nick = clean_n
        client.persist_profile()
        client.log("SYSTEM", "#00ffcc", f"Username saved as '{client.nick}' (reconnecting...).")
        client.reconnect_target = (client.server_url, client.world)
        client._trigger_ws_reconnect()
    elif cmd == "/color" and len(parts) > 1:
        col_s = parts[1].strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", col_s):
            client.color = col_s.lower()
            client.persist_profile()
            client.reconnect_target = (client.server_url, client.world)
            client._trigger_ws_reconnect()
        else:
            client.log("ERR", "#ff4444", "Usage: /color <#rrggbb>")
    elif cmd == "/world" and len(parts) > 1:
        client.request_join_world(parts[1])
    elif cmd == "/key":
        client.log("PUBKEY", "#00ff66", f"{client.pubkey_hex} (short ID: {client.short_id})")
    elif cmd == "/upload":
        sub = cleaned.split(" ", 4)
        if len(sub) < 4:
            client.log("ERR", "#ff4444", "Usage: /upload <world|avatar> <name> <path.wrl> [description]")
            return
        kind, name, fpath = sub[1].lower(), sub[2], sub[3].strip('"').strip("'")
        if kind not in ("world", "avatar"):
            client.log("ERR", "#ff4444", "Upload type must be 'world' or 'avatar'.")
            return
        p = Path(fpath).expanduser()
        if p.suffix.lower() != ".wrl":
            client.log("ERR", "#ff4444", "Only VRML (.wrl) files can be uploaded for avatars and worlds.")
            return
        if not p.is_file():
            client.log("ERR", "#ff4444", f"File not found: {p}")
            return
        raw_bytes = p.read_bytes()
        if b"\x00" in raw_bytes:
            client.log("ERR", "#ff4444", "Binary files are not allowed; upload a text VRML (.wrl) file.")
            return
        wrl_content = raw_bytes.decode("utf-8", errors="replace")
        first_line = wrl_content.splitlines()[0].strip() if wrl_content.splitlines() else ""
        if not re.match(r"^#VRML\s+V(1\.0|2\.0)\s+(ascii|utf8)\b", first_line):
            client.log("ERR", "#ff4444", "Invalid VRML header: file must start with '#VRML V2.0 utf8' or '#VRML V1.0 ascii'.")
            return
        parsed_check = parse_vrml(wrl_content)
        if not parsed_check.get("objects"):
            client.log("ERR", "#ff4444", "Upload blocked: no valid VRML 3D geometry found in file.")
            return
        if kind == "avatar":
            store_cached_avatar_wrl(name, wrl_content)
        payload = {"type": "upload", "kind": kind, "name": name, "filename": p.name, "content": wrl_content}
        if len(sub) >= 5:
            payload["description"] = sub[4]
        client.send_json(payload)
    elif cmd == "/templates":
        client.log("TEMPLATES", "#00ff66", "Available World Templates (use /createworld <name> <template> [description]):")
        for tkey, tmeta in WORLD_TEMPLATES.items():
            client.log("TEMPLATE", "#00ffcc", f"  {tkey:<10} - {tmeta['label']}: {tmeta['desc']}")
    elif cmd == "/createworld":
        sub = cleaned.split(" ", 3)
        if len(sub) < 2 or not sub[1].strip():
            avail = ", ".join(WORLD_TEMPLATES.keys())
            client.log("ERR", "#ff4444", f"Usage: /createworld <world_name> [template] [description] (templates: {avail})")
            return
        w_name = sub[1].strip()
        t_key = sub[2].strip() if len(sub) >= 3 and sub[2].strip() else "flat"
        w_desc = sub[3].strip() if len(sub) >= 4 else ""
        create_world_from_template(client, w_name, t_key, w_desc)
    else:
        client.send_json({"type": "chat", "text": text})


def draw_hud(client: MetaverseClient, font, chat_mode: bool, chat_buffer: str, nametags=None, now: float = 0.0, fps: float = 60.0):
    glMatrixMode(GL_PROJECTION)
    glPushMatrix()
    glLoadIdentity()
    glOrtho(0, WINDOW_W, WINDOW_H, 0, -1, 1)
    glMatrixMode(GL_MODELVIEW)
    glPushMatrix()
    glLoadIdentity()

    glDisable(GL_DEPTH_TEST)
    glDisable(GL_LIGHTING)
    glDisable(GL_FOG)
    glPixelZoom(1.0, 1.0)

    def draw_rect(x, y, w, h, rgba):
        glColor4f(*rgba)
        glBegin(GL_QUADS)
        glVertex2f(x, y); glVertex2f(x + w, y); glVertex2f(x + w, y + h); glVertex2f(x, y + h)
        glEnd()

    def draw_bevel_panel(x, y, w, h, bg_rgba=(0.06, 0.04, 0.14, 0.88), raised=True):
        draw_rect(x, y, w, h, bg_rgba)
        hi = (0.45, 0.85, 0.95, 0.85) if raised else (0.02, 0.01, 0.06, 0.95)
        lo = (0.02, 0.01, 0.06, 0.95) if raised else (0.45, 0.85, 0.95, 0.85)
        inner_hi = (0.85, 0.25, 0.85, 0.55) if raised else (0.05, 0.15, 0.22, 0.8)
        glLineWidth(2.0)
        glBegin(GL_LINES)
        glColor4f(*hi); glVertex2f(x, y); glVertex2f(x + w, y); glVertex2f(x, y); glVertex2f(x, y + h)
        glColor4f(*lo); glVertex2f(x, y + h); glVertex2f(x + w, y + h); glVertex2f(x + w, y); glVertex2f(x + w, y + h)
        glEnd()
        glLineWidth(1.0)
        glBegin(GL_LINE_LOOP)
        glColor4f(*inner_hi)
        glVertex2f(x + 2, y + 2); glVertex2f(x + w - 2, y + 2); glVertex2f(x + w - 2, y + h - 2); glVertex2f(x + 2, y + h - 2)
        glEnd()

    def draw_title_bar(x, y, w, h, title_text, right_badge=""):
        glBegin(GL_QUADS)
        glColor4f(0.05, 0.08, 0.42, 0.96); glVertex2f(x, y); glVertex2f(x, y + h)
        glColor4f(0.45, 0.06, 0.42, 0.96); glVertex2f(x + w, y + h); glVertex2f(x + w, y)
        glEnd()
        draw_text(x + 7, y + 2, title_text, (255, 255, 255))
        if right_badge:
            draw_text(x + w - font.size(right_badge)[0] - 8, y + 2, right_badge, (0, 255, 220))

    def draw_text(x, y, text, color=(0, 255, 204)):
        if not text:
            return
        surf = font.render(text, True, color)
        if surf.get_width() <= 0 or surf.get_height() <= 0:
            return
        raw = pygame.image.tostring(surf, "RGBA", True)
        glRasterPos2f(x, y + surf.get_height())
        glDrawPixels(surf.get_width(), surf.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, raw)

    def wrap_line(text: str, max_px: int) -> list[str]:
        if font.size(text)[0] <= max_px:
            return [text]
        lines, current = [], ""
        for word in text.split(" "):
            candidate = f"{current} {word}".strip() if current else word
            if font.size(candidate)[0] <= max_px:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = ""
                if font.size(word)[0] > max_px:
                    chunk = ""
                    for ch in word:
                        if font.size(chunk + ch)[0] <= max_px:
                            chunk += ch
                        else:
                            lines.append(chunk)
                            chunk = ch
                    current = chunk
                else:
                    current = word
        if current:
            lines.append(current)
        return lines or [""]

    draw_bevel_panel(0, 0, WINDOW_W, 52, (0.08, 0.06, 0.16, 0.92), raised=True)
    net_label = "OFFLINE" if client.offline_mode else client.server_url.replace("ws://", "").replace("wss://", "")
    vrml_loc = f"vrml://{net_label}/{client.world}.wrl"
    pulse_chars = ("[*]", "[+]", "[o]", "[+]")
    spinner = pulse_chars[int(now * 4) % len(pulse_chars)] if client.connected else ("[OFF]" if client.offline_mode else "[...]")
    draw_title_bar(2, 2, WINDOW_W - 4, 21, f"cyberX87 // {client.node_name}", f"{spinner} {int(fps)} FPS")

    draw_bevel_panel(6, 26, WINDOW_W - 12, 22, (0.02, 0.01, 0.06, 0.95), raised=False)
    desc_suffix = f" - {client.world_desc}" if client.world_desc else ""
    radio_suffix = f" | [{'MUTE' if client.radio.muted else 'FM'}: {client.radio.name}]" if client.radio.url else ""
    flags = f"{' [FLY]' if client.fly_mode else ''}{' [SCAN]' if client.inspect_mode else ''}"
    loc_line = f"{vrml_loc} ({len(client.scene.get('objects', []))} objs){desc_suffix}{radio_suffix} | {client.nick}{f'#{client.short_id}' if client.short_id else ''} [{client.role}]{flags}"
    while font.size(loc_line)[0] > WINDOW_W - 26 and len(loc_line) > 12:
        loc_line = loc_line[:-4] + "..."
    draw_text(12, 29, loc_line, (255, 200, 70) if client.offline_mode else (0, 255, 204))

    comp_w, comp_h = 250, 70
    comp_x, comp_y = WINDOW_W - comp_w - 12, 60
    draw_bevel_panel(comp_x, comp_y, comp_w, comp_h, (0.05, 0.03, 0.13, 0.88), raised=True)
    draw_title_bar(comp_x + 2, comp_y + 2, comp_w - 4, 20, "NAV-SENSOR", f"{len(client.peers)} PEERS")
    px, py, pz = client.cam_pos_ref
    heading = (-client.cam_yaw_ref) % 360.0
    cardinal = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((heading + 22.5) // 45) % 8]
    draw_text(comp_x + 10, comp_y + 26, f"XYZ {px:6.1f}, {py - 1.8:5.1f}, {pz:6.1f}", (0, 255, 204))
    draw_text(comp_x + 10, comp_y + 45, f"HDG {int(heading):03d} deg [{cardinal}]", (255, 110, 220))

    if client.inspect_mode:
        card_w, card_h = 470, 74
        ix, iy = WINDOW_W - card_w - 12, comp_y + comp_h + 8
        draw_bevel_panel(ix, iy, card_w, card_h, (0.04, 0.02, 0.12, 0.92), raised=True)
        draw_title_bar(ix + 2, iy + 2, card_w - 4, 20, "INSPECTOR [I]", "SCAN")
        if client.looked_at_hit is not None:
            obj, dist, _, _ = client.looked_at_hit
            ox, oy, oz = obj["pos"]
            r, g, b = obj["diff"]
            def_name = obj.get("def_name", "")
            placed_id = def_name.split("_", 1)[1] if def_name.startswith("PLACED_") else ""
            draw_text(ix + 10, iy + 26, f"#{obj['_index']} {obj['kind'].upper()}{f' [{placed_id}]' if placed_id else ''} {dist:.1f}m RGB({r:.2f},{g:.2f},{b:.2f})"[:56], (0, 255, 160))
            draw_text(ix + 10, iy + 46, f"({ox:.1f}, {oy:.1f}, {oz:.1f}) | {'/moveobj /rotateobj /paint /scaleobj /delobj' if placed_id else '/place <shape> [color] [scale]'}"[:56], (255, 140, 220))
        else:
            draw_text(ix + 10, iy + 32, "Aim crosshair at an object or use /place <shape>.", (160, 200, 190))

    if nametags:
        for nx, ny, label, rgb in nametags:
            tw, th = font.size(label)
            bx_t = max(4, min(WINDOW_W - tw - 14, int(nx - tw // 2 - 5)))
            by_t = max(58, min(WINDOW_H - th - 14, int(ny - th // 2)))
            draw_bevel_panel(bx_t, by_t, tw + 10, th + 6, (0.04, 0.02, 0.10, 0.84), raised=True)
            draw_text(bx_t + 5, by_t + 3, label, rgb)

    box_w, box_h = 660, 220
    bx, by = 12, WINDOW_H - box_h - 12
    inner_max_w = box_w - 20
    draw_bevel_panel(bx, by, box_w, box_h, (0.04, 0.02, 0.10, 0.88), raised=True)
    draw_title_bar(bx + 2, by + 2, box_w - 4, 20, f"COMMS // #{client.world}", "[T] CHAT")

    wrapped_entries = []
    for sender, hex_c, msg in client.chat_log[-10:]:
        hc = hex_c.lstrip("#")
        rgb = (int(hc[0:2], 16), int(hc[2:4], 16), int(hc[4:6], 16)) if len(hc) == 6 else (0, 255, 204)
        for line_str in wrap_line(f"<{sender}> {msg}", inner_max_w):
            wrapped_entries.append((line_str, rgb))

    for idx, (line_str, rgb) in enumerate(wrapped_entries[-7:]):
        draw_text(bx + 10, by + 26 + idx * 20, line_str, rgb)

    input_y = by + box_h - 32
    draw_bevel_panel(bx + 6, input_y, box_w - 12, 26, (0.02, 0.01, 0.05, 0.96), raised=False)
    if chat_mode:
        display_buf = chat_buffer
        while font.size(f"C:\\X87> {display_buf}_")[0] > inner_max_w - 8 and len(display_buf) > 1:
            display_buf = display_buf[1:]
        prompt_txt = f"C:\\X87> {display_buf}{'_' if int(now * 2.5) % 2 == 0 else ' '}"
    else:
        prompt_txt = "[T] Chat | /worlds | /world <name> | /hop <node> | /help"
    draw_text(bx + 12, input_y + 4, prompt_txt, (255, 255, 255) if chat_mode else (140, 185, 175))

    if client.crt_mode:
        glColor4f(0.0, 0.0, 0.0, 0.12)
        glBegin(GL_LINES)
        for sy in range(0, WINDOW_H, 3):
            glVertex2f(0, sy); glVertex2f(WINDOW_W, sy)
        glEnd()

    cx, cy = WINDOW_W // 2, WINDOW_H // 2
    glColor4f(*(0.0, 1.0, 0.75, 0.95) if client.inspect_mode else (0.0, 1.0, 0.85, 0.55))
    glBegin(GL_LINES)
    glVertex2f(cx - 10, cy); glVertex2f(cx - 3, cy); glVertex2f(cx + 3, cy); glVertex2f(cx + 10, cy)
    glVertex2f(cx, cy - 10); glVertex2f(cx, cy - 3); glVertex2f(cx, cy + 3); glVertex2f(cx, cy + 10)
    glEnd()
    if client.inspect_mode:
        glBegin(GL_LINE_LOOP)
        glVertex2f(cx - 12, cy - 12); glVertex2f(cx + 12, cy - 12); glVertex2f(cx + 12, cy + 12); glVertex2f(cx - 12, cy + 12)
        glEnd()

    glEnable(GL_LIGHTING)
    glEnable(GL_DEPTH_TEST)
    glMatrixMode(GL_PROJECTION)
    glPopMatrix()
    glMatrixMode(GL_MODELVIEW)
    glPopMatrix()


def main():
    raw_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    start_offline = raw_arg.lower() in ("--offline", "offline")
    server_arg = DEFAULT_SERVER if (not raw_arg or start_offline) else normalize_ws_url(raw_arg)

    client = MetaverseClient(load_or_create_key())
    client.server_url = server_arg
    if start_offline:
        client.enter_offline_mode(sys.argv[2] if len(sys.argv) > 2 else "hub")
    client.start_background()

    pygame.init()
    pygame.display.set_caption("cyberX87 // Standalone VRML97 Metaverse Client")
    pygame.display.set_mode((WINDOW_W, WINDOW_H), DOUBLEBUF | OPENGL)
    font = pygame.font.SysFont("Courier New", 15, bold=True)

    glEnable(GL_DEPTH_TEST)
    glEnable(GL_BLEND)
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    glEnable(GL_LIGHTING)
    glEnable(GL_LIGHT0)
    glEnable(GL_NORMALIZE)
    glEnable(GL_COLOR_MATERIAL)
    glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
    glLightModelfv(GL_LIGHT_MODEL_AMBIENT, (0.45, 0.45, 0.55, 1.0))
    glLightfv(GL_LIGHT0, GL_POSITION, (0.4, 1.0, 0.5, 0.0))
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 1.0, 1.0))

    quadric = gluNewQuadric()
    display_cache = SceneDisplayListCache()

    cam_pos = [0.0, 4.05, 13.5]
    yaw = pitch = 0.0
    mouse_grabbed = True
    pygame.event.set_grab(True)
    pygame.mouse.set_visible(False)

    chat_mode = False
    chat_buffer = ""
    last_move_send = 0.0
    clock = pygame.time.Clock()

    try:
        while client.running:
            dt = min(clock.tick(60) / 1000.0, 0.05)
            now = time.time()
            client.radio.check_health(now)

            if client.teleport_req is not None:
                cam_pos = list(client.teleport_req)
                yaw = -float(client.teleport_yaw) if client.teleport_yaw is not None else 0.0
                client.teleport_yaw = None
                pitch = 0.0
                client.vel_y = 0.0
                if not client.fly_mode:
                    resolve_spawn_clearance(cam_pos, list(client.scene.get("objects", [])))
                client.teleport_req = None

            if client.unstuck_req:
                resolve_spawn_clearance(cam_pos, list(client.scene.get("objects", [])))
                client.unstuck_req = False

            client.cam_pos_ref = list(cam_pos)
            client.cam_yaw_ref = yaw
            client.cam_pitch_ref = pitch

            scene_objs = list(client.scene.get("objects", []))
            client.looked_at_hit = find_looked_at_object(cam_pos, yaw, pitch, scene_objs) if client.inspect_mode else None

            for event in pygame.event.get():
                if event.type == QUIT:
                    client.running = False
                elif event.type == KEYDOWN:
                    if chat_mode:
                        if event.key == K_ESCAPE:
                            chat_mode, chat_buffer = False, ""
                            if mouse_grabbed:
                                pygame.event.set_grab(True)
                                pygame.mouse.set_visible(False)
                        elif event.key == K_RETURN:
                            if chat_buffer.strip():
                                handle_command(client, chat_buffer)
                            chat_buffer, chat_mode = "", False
                            if mouse_grabbed:
                                pygame.event.set_grab(True)
                                pygame.mouse.set_visible(False)
                        elif event.key == K_BACKSPACE:
                            chat_buffer = chat_buffer[:-1]
                        elif event.unicode and event.unicode.isprintable():
                            chat_buffer += event.unicode
                    else:
                        if event.key == K_ESCAPE:
                            mouse_grabbed = not mouse_grabbed
                            pygame.event.set_grab(mouse_grabbed)
                            pygame.mouse.set_visible(not mouse_grabbed)
                        elif event.key == K_i:
                            client.inspect_mode = not client.inspect_mode
                        elif event.key in (K_t, K_RETURN, K_SLASH):
                            chat_mode = True
                            chat_buffer = "/" if event.key == K_SLASH else ""
                            pygame.event.set_grab(False)
                            pygame.mouse.set_visible(True)
                        elif event.key in (K_1, K_2, K_3, K_4):
                            em_name = {K_1: "wave", K_2: "dance", K_3: "spin", K_4: "jump"}[event.key]
                            client.send_json({"type": "chat", "text": f"/emote {em_name}"})
                elif event.type == MOUSEMOTION and mouse_grabbed and not chat_mode:
                    dx, dy = event.rel
                    yaw -= dx * 0.18
                    pitch = max(-85.0, min(85.0, pitch - dy * 0.18))

            if not chat_mode:
                keys = pygame.key.get_pressed()
                speed = (12.0 if client.fly_mode else 7.5) * dt
                rad = math.radians(yaw)
                fx, fz = -math.sin(rad), -math.cos(rad)
                rx, rz = math.cos(rad), -math.sin(rad)

                move_dx = move_dz = 0.0
                if keys[K_w] or keys[K_UP]:
                    move_dx += fx * speed; move_dz += fz * speed
                if keys[K_s] or keys[K_DOWN]:
                    move_dx -= fx * speed; move_dz -= fz * speed
                if keys[K_a] or keys[K_LEFT]:
                    move_dx -= rx * speed; move_dz -= rz * speed
                if keys[K_d] or keys[K_RIGHT]:
                    move_dx += rx * speed; move_dz += rz * speed

                if client.fly_mode:
                    cam_pos[0] += move_dx; cam_pos[2] += move_dz
                    if keys[K_SPACE]:
                        cam_pos[1] += speed
                    if keys[K_LSHIFT]:
                        cam_pos[1] -= speed
                else:
                    client.vel_y, _ = step_player_physics(cam_pos, move_dx, move_dz, client.vel_y, bool(keys[K_SPACE]), dt, scene_objs)

            if client.connected and client.short_id and now - last_move_send > 0.1:
                last_move_send = now
                client.send_json({
                    "type": "move",
                    "pos": [round(cam_pos[0], 3), round(cam_pos[1] - 1.8, 3), round(cam_pos[2], 3)],
                    "rot": [0.0, 1.0, 0.0, round(math.radians(yaw), 3)],
                })

            bg = client.scene.get("bg", (0.04, 0.01, 0.12))
            glClearColor(bg[0], bg[1], bg[2], 1.0)
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

            glMatrixMode(GL_PROJECTION)
            glLoadIdentity()
            gluPerspective(65.0, WINDOW_W / float(WINDOW_H), 0.1, 250.0)

            glMatrixMode(GL_MODELVIEW)
            glLoadIdentity()
            glRotatef(-pitch, 1, 0, 0)
            glRotatef(-yaw, 0, 1, 0)
            glTranslatef(-cam_pos[0], -cam_pos[1], -cam_pos[2])
            glLightfv(GL_LIGHT0, GL_POSITION, (0.4, 1.0, 0.5, 0.0))

            display_cache.render_static_scene(client.scene, scene_objs, quadric, client.world)
            if client.looked_at_hit is not None:
                draw_aabb_wireframe(client.looked_at_hit[0]["_aabb"])
            for peer in list(client.peers.values()):
                render_peer(peer, quadric, now, client.avatar_meshes)

            draw_hud(client, font, chat_mode, chat_buffer, project_peer_nametags(client.peers, client.avatar_meshes, cam_pos), now, clock.get_fps())
            pygame.display.flip()
    finally:
        client.running = False
        client.radio.stop()
        display_cache.clear()
        try:
            gluDeleteQuadric(quadric)
        except Exception:
            pass
        pygame.quit()


if __name__ == "__main__":
    main()
