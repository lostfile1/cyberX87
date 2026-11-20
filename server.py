import asyncio
import json
import os
import re
import secrets
import time
import base64
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

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit for .wrl uploads

PORTAL_BLOCK_START = "# === AUTO_PORTALS_START ==="
PORTAL_BLOCK_END = "# === AUTO_PORTALS_END ==="

PORTAL_PALETTE = [
    (0.0, 1.0, 0.75),   # Cyber Cyan-Green
    (1.0, 0.1, 0.75),   # Neon Magenta
    (0.2, 0.65, 1.0),   # Electric Blue
    (0.75, 0.2, 1.0),   # Ultraviolet Purple
    (1.0, 0.65, 0.0),   # Amber Gold
    (0.1, 1.0, 0.35),   # Matrix Emerald
]


def build_portal_gate_vrml(
    target_url: str,
    label: str,
    x: float,
    z: float,
    idx: int = 0,
    y: float = 0.0,
) -> str:
    """Canonical cyberX87 portal archway used across hub, plaza, and plugins."""
    r, g, b = PORTAL_PALETTE[idx % len(PORTAL_PALETTE)]
    dim_r, dim_g, dim_b = round(r * 0.25, 3), round(g * 0.25, 3), round(b * 0.25, 3)
    return f"""
Transform {{
  translation {x:.2f} {y:.2f} {z:.2f}
  children [
    Anchor {{
      url ["{target_url}"]
      description "Portal: {label}"
      children [
        # Step Base Platform
        Transform {{
          translation 0 0.15 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{ diffuseColor {dim_r} {dim_g} {dim_b} emissiveColor {dim_r} {dim_g} {dim_b} }}
              }}
              geometry Box {{ size 3.8 0.3 1.5 }}
            }}
          ]
        }}
        # Left Portal Pillar
        Transform {{
          translation -1.55 2.1 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{ diffuseColor 0.08 0.1 0.16 emissiveColor {r * 0.5:.2f} {g * 0.5:.2f} {b * 0.5:.2f} }}
              }}
              geometry Cylinder {{ radius 0.22 height 3.8 }}
            }}
          ]
        }}
        # Right Portal Pillar
        Transform {{
          translation 1.55 2.1 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{ diffuseColor 0.08 0.1 0.16 emissiveColor {r * 0.5:.2f} {g * 0.5:.2f} {b * 0.5:.2f} }}
              }}
              geometry Cylinder {{ radius 0.22 height 3.8 }}
            }}
          ]
        }}
        # Top Arch Lintel
        Transform {{
          translation 0 4.05 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{ diffuseColor 0.08 0.1 0.16 emissiveColor {r * 0.7:.2f} {g * 0.7:.2f} {b * 0.7:.2f} }}
              }}
              geometry Box {{ size 3.8 0.35 1.1 }}
            }}
          ]
        }}
        # Swirling Portal Energy Membrane
        Transform {{
          translation 0 2.1 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{
                  diffuseColor {r:.2f} {g:.2f} {b:.2f}
                  emissiveColor {r:.2f} {g:.2f} {b:.2f}
                  transparency 0.35
                }}
              }}
              geometry Box {{ size 2.65 3.55 0.16 }}
            }}
          ]
        }}
        # Floating Overhead Signboard
        Transform {{
          translation 0 4.75 0
          children [
            Shape {{
              appearance Appearance {{
                material Material {{ diffuseColor 0.04 0.06 0.12 emissiveColor {r:.2f} {g:.2f} {b:.2f} }}
              }}
              geometry Text {{
                string ["{label}"]
                fontStyle FontStyle {{
                  family ["TYPEWRITER" "SANS"]
                  style "BOLD"
                  size 0.45
                  justify ["MIDDLE" "MIDDLE"]
                }}
              }}
            }}
          ]
        }}
      ]
    }}
  ]
}}"""


DEFAULT_HUB_WRL = """#VRML V2.0 utf8
WorldInfo { title "Genesis Hub" info ["Self-Hosted VRML97 Metaverse"] }
NavigationInfo { type ["WALK" "ANY"] headlight TRUE }
Background {
  skyColor [0.02 0.0 0.08, 0.1 0.0 0.25, 0.0 0.2 0.3]
  skyAngle [1.2, 1.57]
  groundColor [0.05 0.0 0.1, 0.0 0.15 0.2]
  groundAngle [1.57]
}
Fog { color 0.05 0.0 0.15 fogType "EXPONENTIAL" visibilityRange 140 }

# Neon Floor Grid
Transform {
  translation 0 -0.05 0
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.05 0.05 0.12 specularColor 0.0 1.0 1.0 }
      }
      geometry Box { size 120 0.1 120 }
    }
  ]
}

# Central Monolith (Behind the Portal Row)
Transform {
  translation 0 4.5 -22
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.06 0.08 0.15 emissiveColor 0.0 0.45 0.6 }
      }
      geometry Box { size 3.0 9.0 1.2 }
    }
  ]
}
"""

DEFAULT_PLAZA_WRL = (
    """#VRML V2.0 utf8
WorldInfo { title "Cyber Plaza" }
Background { skyColor [0.15 0.02 0.05] }
Transform {
  translation 0 -0.05 0
  children [
    Shape {
      appearance Appearance { material Material { diffuseColor 0.2 0.05 0.1 } }
      geometry Box { size 80 0.1 80 }
    }
  ]
}
"""
    + build_portal_gate_vrml("vrml://LOCAL/hub", "hub", 0.0, -8.0, idx=2)
)


def read_wrl_safe(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def has_vrml_geometry(content: str) -> bool:
    return any(tok in content for tok in (
        "Shape", "Box", "Sphere", "Cylinder", "Cone",
        "IndexedFaceSet", "ElevationGrid", "Extrusion",
        "Separator", "Cube"
    ))


def ensure_valid_vrml_world(world_name: str, content: str) -> str:
    """Ensures uploaded/AI-generated VRML has a valid header and visible 3D shapes without injecting portals."""
    text = content.strip()
    if "#VRML" not in text[:64]:
        text = "#VRML V2.0 utf8\n" + text

    has_prim = bool(re.search(
        r"\b(Box|Sphere|Cylinder|Cone|IndexedFaceSet|ElevationGrid|Extrusion|Cube)\b",
        text,
    ))
    if not has_prim:
        text += f"""
WorldInfo {{ title "{world_name}" }}
Background {{ skyColor [0.05 0.02 0.16, 0.15 0.04 0.28] skyAngle [1.2] }}
Transform {{
  translation 0 2.5 -10
  children [
    Shape {{
      appearance Appearance {{ material Material {{ diffuseColor 0.1 0.9 0.8 emissiveColor 0.05 0.45 0.4 }} }}
      geometry Cone {{ bottomRadius 3.0 height 5.0 }}
    }}
  ]
}}
"""
    return text


def rebuild_hub_portals():
    """Rebuilds a neat row of portal gates in hub.wrl for every other world on the server."""
    hub_path = WORLDS_DIR / "hub.wrl"
    if not hub_path.exists():
        hub_text = DEFAULT_HUB_WRL
    else:
        hub_text = read_wrl_safe(hub_path)

    pattern = re.compile(
        re.escape(PORTAL_BLOCK_START) + r".*?" + re.escape(PORTAL_BLOCK_END),
        re.S,
    )
    hub_text = pattern.sub("", hub_text).rstrip()

    other_worlds = sorted(
        f.stem for f in WORLDS_DIR.glob("*.wrl") if f.stem.lower() != "hub"
    )

    if not other_worlds:
        hub_path.write_text(hub_text + "\n", encoding="utf-8")
        return

    per_row = 6
    spacing_x = 5.2
    spacing_z = 6.5
    base_z = -7.0

    gate_chunks = [f"\n\n{PORTAL_BLOCK_START}"]
    for idx, wname in enumerate(other_worlds):
        row = idx // per_row
        col = idx % per_row
        items_in_row = min(per_row, len(other_worlds) - row * per_row)
        row_width = (items_in_row - 1) * spacing_x
        x = -row_width / 2.0 + col * spacing_x
        z = base_z - row * spacing_z
        gate_chunks.append(build_portal_gate_vrml(f"vrml://LOCAL/{wname}", wname, x, z, idx))

    gate_chunks.append(f"\n{PORTAL_BLOCK_END}\n")
    hub_path.write_text(hub_text + "".join(gate_chunks), encoding="utf-8")
    print(f"[*] Updated hub.wrl with {len(other_worlds)} portals in a neat row.")


def load_server_plugins() -> dict:
    SERVER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    sample = SERVER_PLUGINS_DIR / "world_tools.x87"
    if not sample.exists():
        sample.write_text(
            "# Server-side human-readable plugins (data/plugins/)\n"
            "COMMAND: /portal\n"
            "DESC: Spawn a standard cyberX87 portal gate to a local world (usage: /portal <world_name>)\n"
            "ROLE: builder\n"
            "ACTION: spawn_portal\n"
            "VALUE: vrml://LOCAL/{args}\n"
            "---\n"
            "COMMAND: /fedportal\n"
            "DESC: Spawn a federated portal gate to another server (usage: /fedportal <host:port/world>)\n"
            "ROLE: admin\n"
            "ACTION: spawn_portal\n"
            "VALUE: vrml://{args}\n"
            "---\n"
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
            encoding="utf-8",
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
                if not cname.startswith("/"):
                    cname = "/" + cname
                cmds[cname] = entry
    return cmds


def init_storage():
    WORLDS_DIR.mkdir(parents=True, exist_ok=True)
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    load_server_plugins()

    hub_path = WORLDS_DIR / "hub.wrl"
    if not hub_path.exists() or not has_vrml_geometry(read_wrl_safe(hub_path)):
        hub_path.write_text(DEFAULT_HUB_WRL, encoding="utf-8")
        print("[*] Initialized default data/worlds/hub.wrl")

    plaza_path = WORLDS_DIR / "plaza.wrl"
    if not plaza_path.exists() or not has_vrml_geometry(read_wrl_safe(plaza_path)):
        plaza_path.write_text(DEFAULT_PLAZA_WRL, encoding="utf-8")
        print("[*] Initialized default data/worlds/plaza.wrl")

    rebuild_hub_portals()

    if not PERMS_FILE.exists():
        PERMS_FILE.write_text(json.dumps({
            "default_role": "builder",
            "admins": [],
            "builders": [],
            "banned": []
        }, indent=2))


def load_perms():
    return json.loads(PERMS_FILE.read_text(encoding="utf-8"))


def save_perms(data):
    PERMS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


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
    return perms.get("default_role", "builder")


def sanitize_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in ("_", "-")).strip()
    return cleaned[:32] or "unnamed"


CLIENTS = {}


async def broadcast_world(world: str, payload: dict, exclude_ws=None):
    msg = json.dumps(payload)
    for ws, state in list(CLIENTS.items()):
        if state.get("world") == world and ws != exclude_ws:
            try:
                await ws.send(msg)
            except Exception:
                pass


async def handler(websocket):
    nonce = secrets.token_hex(16)
    await websocket.send(json.dumps({"type": "challenge", "nonce": nonce}))
    client_state = None

    try:
        async for raw_msg in websocket:
            data = json.loads(raw_msg)
            msg_type = data.get("type")

            # 1. Key-Based Authentication
            if msg_type == "auth":
                pubkey_hex = data.get("pubkey", "")
                sig_hex = data.get("signature", "")
                try:
                    vk = VerifyKey(bytes.fromhex(pubkey_hex))
                    vk.verify(nonce.encode("utf-8"), bytes.fromhex(sig_hex))
                except (ValueError, BadSignatureError):
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid cryptographic signature."}))
                    await websocket.close()
                    return

                role = get_role(pubkey_hex)
                if role == "banned":
                    await websocket.send(json.dumps({"type": "error", "msg": "Key is banned from this server."}))
                    await websocket.close()
                    return

                nick = sanitize_name(data.get("nick", f"User_{pubkey_hex[:6]}"))
                color = data.get("color", "#00ffcc")[:7]
                world = sanitize_name(data.get("world", "hub"))
                if not (WORLDS_DIR / f"{world}.wrl").exists():
                    world = "hub"

                client_state = {
                    "pubkey": pubkey_hex,
                    "short_id": pubkey_hex[:8],
                    "nick": nick,
                    "color": color,
                    "role": role,
                    "world": world,
                    "pos": [0.0, 1.7, 5.0],
                    "rot": [0.0, 1.0, 0.0, 0.0],
                    "avatar": data.get("avatar", "default")
                }
                CLIENTS[websocket] = client_state

                worlds_list = [f.stem for f in WORLDS_DIR.glob("*.wrl")]
                avatars_list = [f.stem for f in AVATARS_DIR.glob("*.wrl")]

                wrl_text = read_wrl_safe(WORLDS_DIR / f"{world}.wrl")
                print(f"[AUTH] {nick} ({role}) -> '{world}' ({len(wrl_text)} bytes)")

                await websocket.send(json.dumps({
                    "type": "auth_ok",
                    "role": role,
                    "short_id": client_state["short_id"],
                    "world": world,
                    "wrl": wrl_text,
                    "worlds": worlds_list,
                    "avatars": avatars_list
                }))

                peers = [
                    {k: v for k, v in st.items() if k != "pubkey"}
                    for ws_peer, st in CLIENTS.items()
                    if ws_peer != websocket and st["world"] == world
                ]
                await websocket.send(json.dumps({"type": "peers", "peers": peers}))

                await broadcast_world(world, {
                    "type": "peer_join",
                    "peer": {k: v for k, v in client_state.items() if k != "pubkey"}
                }, exclude_ws=websocket)
                continue

            if not client_state:
                await websocket.send(json.dumps({"type": "error", "msg": "Not authenticated."}))
                continue

            # 2. Movement & Transform Updates
            if msg_type == "move":
                client_state["pos"] = data.get("pos", client_state["pos"])[:3]
                client_state["rot"] = data.get("rot", client_state["rot"])[:4]
                await broadcast_world(client_state["world"], {
                    "type": "peer_move",
                    "short_id": client_state["short_id"],
                    "pos": client_state["pos"],
                    "rot": client_state["rot"]
                }, exclude_ws=websocket)

            # 3. Chat, Emotes & Server-Side .x87 Plugins
            elif msg_type == "chat":
                text = str(data.get("text", ""))[:300].strip()
                if not text:
                    continue
                if text.startswith("/emote "):
                    emote = sanitize_name(text.split(" ", 1)[1])
                    await broadcast_world(client_state["world"], {
                        "type": "emote",
                        "short_id": client_state["short_id"],
                        "nick": client_state["nick"],
                        "emote": emote
                    })
                    continue

                if text.startswith("/"):
                    srv_plugins = load_server_plugins()
                    cmd_tok, _, cmd_args = text.partition(" ")
                    spec = srv_plugins.get(cmd_tok.lower())
                    if spec:
                        req_role = spec.get("role", "guest").lower()
                        if req_role == "admin" and client_state["role"] != "admin":
                            await websocket.send(json.dumps({"type": "error", "msg": f"{cmd_tok} requires admin role."}))
                            continue
                        if req_role == "builder" and client_state["role"] not in ("admin", "builder"):
                            await websocket.send(json.dumps({"type": "error", "msg": f"{cmd_tok} requires builder role."}))
                            continue

                        px, py, pz = client_state["pos"]
                        rendered = spec.get("value", "").format(
                            nick=client_state["nick"],
                            world=client_state["world"],
                            args=cmd_args.strip(),
                            x=round(px, 2),
                            y=round(py, 2),
                            z=round(pz, 2),
                            z_front=round(pz - 4.0, 2),
                        )
                        act = spec.get("action", "broadcast").lower()
                        if act == "spawn_portal":
                            target_url = rendered.strip()
                            label = target_url.split("/")[-1] or "portal"
                            gate_vrml = build_portal_gate_vrml(
                                target_url=target_url,
                                label=label,
                                x=round(px, 2),
                                z=round(pz - 5.0, 2),
                                idx=len(CLIENTS) + len(label),
                            )
                            wpath = WORLDS_DIR / f"{client_state['world']}.wrl"
                            updated = read_wrl_safe(wpath) + f"\n{gate_vrml}\n"
                            wpath.write_text(updated, encoding="utf-8")
                            await broadcast_world(client_state["world"], {
                                "type": "world_data",
                                "world": client_state["world"],
                                "wrl": updated,
                            })
                        elif act == "spawn_vrml":
                            wpath = WORLDS_DIR / f"{client_state['world']}.wrl"
                            updated = read_wrl_safe(wpath) + f"\n{rendered}\n"
                            wpath.write_text(updated, encoding="utf-8")
                            if client_state["world"] == "hub":
                                rebuild_hub_portals()
                                updated = read_wrl_safe(wpath)
                            await broadcast_world(client_state["world"], {
                                "type": "world_data",
                                "world": client_state["world"],
                                "wrl": updated,
                            })
                        elif act == "broadcast":
                            for peer_ws in list(CLIENTS.keys()):
                                try:
                                    await peer_ws.send(json.dumps({"type": "info", "msg": rendered}))
                                except Exception:
                                    pass
                        continue

                await broadcast_world(client_state["world"], {
                    "type": "chat",
                    "short_id": client_state["short_id"],
                    "nick": client_state["nick"],
                    "color": client_state["color"],
                    "text": text
                })

            # 4. World Switching / Portal Traversal
            elif msg_type == "join_world":
                target = sanitize_name(data.get("world", "hub"))
                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                old_world = client_state["world"]
                await broadcast_world(old_world, {
                    "type": "peer_leave",
                    "short_id": client_state["short_id"]
                }, exclude_ws=websocket)

                client_state["world"] = target
                client_state["pos"] = [0.0, 1.7, 5.0]

                wrl_text = read_wrl_safe(wrl_path)
                print(f"[WORLD] {client_state['nick']} -> '{target}' ({len(wrl_text)} bytes)")

                await websocket.send(json.dumps({
                    "type": "world_data",
                    "world": target,
                    "wrl": wrl_text
                }))

                peers = [
                    {k: v for k, v in st.items() if k != "pubkey"}
                    for ws_peer, st in CLIENTS.items()
                    if ws_peer != websocket and st["world"] == target
                ]
                await websocket.send(json.dumps({"type": "peers", "peers": peers}))
                await broadcast_world(target, {
                    "type": "peer_join",
                    "peer": {k: v for k, v in client_state.items() if k != "pubkey"}
                }, exclude_ws=websocket)

            # 4b. List Available Worlds
            elif msg_type == "list_worlds":
                worlds_list = sorted(f.stem for f in WORLDS_DIR.glob("*.wrl"))
                await websocket.send(json.dumps({
                    "type": "worlds_list",
                    "worlds": worlds_list,
                    "current": client_state["world"]
                }))

            # 4c. Delete World (Admin Only) & Rebuild Hub Portals
            elif msg_type == "delete_world":
                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required to delete worlds."}))
                    continue

                target = sanitize_name(data.get("world", ""))
                if not target or target.lower() == "hub":
                    await websocket.send(json.dumps({"type": "error", "msg": "Cannot delete the core 'hub' world."}))
                    continue

                wrl_path = WORLDS_DIR / f"{target}.wrl"
                if not wrl_path.exists():
                    await websocket.send(json.dumps({"type": "error", "msg": f"World '{target}' does not exist."}))
                    continue

                wrl_path.unlink()
                rebuild_hub_portals()
                worlds_list = sorted(f.stem for f in WORLDS_DIR.glob("*.wrl"))
                hub_wrl = read_wrl_safe(WORLDS_DIR / "hub.wrl")
                print(f"[ADMIN] {client_state['nick']} deleted world '{target}'")

                for peer_ws, peer_st in list(CLIENTS.items()):
                    if peer_st["world"] == target:
                        peer_st["world"] = "hub"
                        peer_st["pos"] = [0.0, 1.7, 5.0]
                        try:
                            await peer_ws.send(json.dumps({
                                "type": "info",
                                "msg": f"World '{target}' was deleted by an admin. Returning to hub."
                            }))
                            await peer_ws.send(json.dumps({
                                "type": "world_data",
                                "world": "hub",
                                "wrl": hub_wrl
                            }))
                        except Exception:
                            pass

                await broadcast_world("hub", {
                    "type": "world_data",
                    "world": "hub",
                    "wrl": hub_wrl
                })

                await websocket.send(json.dumps({
                    "type": "worlds_list",
                    "worlds": worlds_list,
                    "current": client_state["world"]
                }))
                await websocket.send(json.dumps({
                    "type": "info",
                    "msg": f"Deleted world '{target}' and updated hub portals."
                }))

            # 5. Upload World or Avatar (.wrl) + Auto-Update Hub Portals Only
            elif msg_type == "upload":
                if client_state["role"] not in ("admin", "builder"):
                    await websocket.send(json.dumps({"type": "error", "msg": "Permission denied: builder role required."}))
                    continue

                kind = data.get("kind")
                name = sanitize_name(data.get("name", "custom"))
                content = data.get("content", "")

                if len(content.encode("utf-8", errors="replace")) > MAX_FILE_SIZE:
                    await websocket.send(json.dumps({"type": "error", "msg": "File exceeds 5MB limit."}))
                    continue
                if "#VRML" not in content[:64]:
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid VRML header (#VRML V2.0 or V1.0 required)."}))
                    continue

                if kind == "world":
                    content = ensure_valid_vrml_world(name, content)
                    (WORLDS_DIR / f"{name}.wrl").write_text(content, encoding="utf-8")
                    rebuild_hub_portals()
                    worlds_list = [f.stem for f in WORLDS_DIR.glob("*.wrl")]

                    updated_hub = read_wrl_safe(WORLDS_DIR / "hub.wrl")
                    await broadcast_world("hub", {
                        "type": "world_data",
                        "world": "hub",
                        "wrl": updated_hub
                    })

                    await websocket.send(json.dumps({
                        "type": "upload_ok",
                        "kind": "world",
                        "name": name,
                        "worlds": worlds_list
                    }))
                elif kind == "avatar":
                    if not has_vrml_geometry(content):
                        await websocket.send(json.dumps({"type": "error", "msg": "Upload rejected: avatar .wrl contains no 3D geometry."}))
                        continue
                    (AVATARS_DIR / f"{name}.wrl").write_text(content, encoding="utf-8")
                    client_state["avatar"] = name
                    await websocket.send(json.dumps({"type": "upload_ok", "kind": "avatar", "name": name}))
                    await broadcast_world(client_state["world"], {
                        "type": "peer_avatar",
                        "short_id": client_state["short_id"],
                        "avatar": name,
                        "wrl": content
                    }, exclude_ws=websocket)

            # 6. Fetch Avatar WRL
            elif msg_type == "get_avatar":
                name = sanitize_name(data.get("name", ""))
                av_path = AVATARS_DIR / f"{name}.wrl"
                if av_path.exists():
                    await websocket.send(json.dumps({
                        "type": "avatar_data",
                        "name": name,
                        "wrl": read_wrl_safe(av_path)
                    }))

            # 7. Admin Permissions Control (Persisted to data/permissions.json)
            elif msg_type == "admin_perm":
                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required."}))
                    continue

                action = data.get("action")
                target_key = data.get("target_key", "").strip()
                perms = load_perms()
                perms.setdefault("admins", [])
                perms.setdefault("builders", [])
                perms.setdefault("banned", [])

                if action == "list_perms":
                    await websocket.send(json.dumps({
                        "type": "perms_list",
                        "perms": perms
                    }))
                    continue

                if action == "grant_admin" and target_key:
                    if target_key not in perms["admins"]:
                        perms["admins"].append(target_key)
                elif action == "revoke_admin" and target_key:
                    if len(perms["admins"]) <= 1 and target_key in perms["admins"]:
                        await websocket.send(json.dumps({"type": "error", "msg": "Cannot remove the last remaining admin."}))
                        continue
                    perms["admins"] = [k for k in perms["admins"] if k != target_key]
                elif action == "grant_builder" and target_key:
                    if target_key not in perms["builders"]:
                        perms["builders"].append(target_key)
                elif action == "revoke_builder" and target_key:
                    perms["builders"] = [k for k in perms["builders"] if k != target_key]
                elif action == "ban" and target_key:
                    if target_key == client_state["pubkey"]:
                        await websocket.send(json.dumps({"type": "error", "msg": "You cannot ban your own active admin key."}))
                        continue
                    perms["admins"] = [k for k in perms["admins"] if k != target_key]
                    perms["builders"] = [k for k in perms["builders"] if k != target_key]
                    if target_key not in perms["banned"]:
                        perms["banned"].append(target_key)
                elif action == "unban" and target_key:
                    perms["banned"] = [k for k in perms["banned"] if k != target_key]
                elif action == "set_default" and target_key in ("guest", "builder"):
                    perms["default_role"] = target_key
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
                                await peer_ws.send(json.dumps({
                                    "type": "role_updated",
                                    "role": new_role
                                }))
                        except Exception:
                            pass

                await websocket.send(json.dumps({
                    "type": "info",
                    "msg": f"Saved permissions to {PERMS_FILE} ({action})."
                }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if websocket in CLIENTS:
            st = CLIENTS.pop(websocket)
            await broadcast_world(st["world"], {
                "type": "peer_leave",
                "short_id": st["short_id"]
            })


async def main():
    init_storage()
    print(f"[*] VRML97 Metaverse Server running on ws://{HOST}:{PORT}")
    print(f"[*] First user to connect is automatically granted ADMIN rights.")
    async with websockets.serve(handler, HOST, PORT, max_size=MAX_FILE_SIZE + 65536):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
