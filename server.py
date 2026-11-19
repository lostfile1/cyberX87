import asyncio
import json
import os
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
PERMS_FILE = DATA_DIR / "permissions.json"

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit for .wrl uploads

DEFAULT_HUB_WRL = """#VRML V2.0 utf8
WorldInfo { title "Genesis Hub" info ["Self-Hosted VRML97 Metaverse"] }
NavigationInfo { type ["WALK" "ANY"] headlight TRUE }
Background {
  skyColor [0.02 0.0 0.08, 0.1 0.0 0.25, 0.0 0.2 0.3]
  skyAngle [1.2, 1.57]
  groundColor [0.05 0.0 0.1, 0.0 0.15 0.2]
  groundAngle [1.57]
}
Fog { color 0.05 0.0 0.15 fogType "EXPONENTIAL" visibilityRange 120 }

# Neon Floor Grid
Transform {
  translation 0 -0.05 0
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.05 0.05 0.12 specularColor 0.0 1.0 1.0 }
      }
      geometry Box { size 100 0.1 100 }
    }
  ]
}

# Central Monolith
Transform {
  translation 0 3 -10
  children [
    Shape {
      appearance Appearance {
        material Material { diffuseColor 0.1 0.1 0.1 emissiveColor 0.0 0.4 0.5 }
      }
      geometry Box { size 2 6 0.5 }
    }
  ]
}

# Portal to Cyber Plaza (Local World)
Transform {
  translation 6 1.5 -8
  children [
    Anchor {
      url ["vrml://LOCAL/plaza"]
      description "Portal: Cyber Plaza"
      children [
        Shape {
          appearance Appearance {
            material Material { emissiveColor 0.0 1.0 0.6 transparency 0.25 }
          }
          geometry Cylinder { radius 1.2 height 3.0 }
        }
      ]
    }
  ]
}
"""

DEFAULT_PLAZA_WRL = """#VRML V2.0 utf8
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
Transform {
  translation -6 1.5 -8
  children [
    Anchor {
      url ["vrml://LOCAL/hub"]
      description "Return to Genesis Hub"
      children [
        Shape {
          appearance Appearance {
            material Material { emissiveColor 0.2 0.6 1.0 transparency 0.25 }
          }
          geometry Cylinder { radius 1.2 height 3.0 }
        }
      ]
    }
  ]
}
"""

def init_storage():
    WORLDS_DIR.mkdir(parents=True, exist_ok=True)
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    if not (WORLDS_DIR / "hub.wrl").exists():
        (WORLDS_DIR / "hub.wrl").write_text(DEFAULT_HUB_WRL, encoding="utf-8")
    if not (WORLDS_DIR / "plaza.wrl").exists():
        (WORLDS_DIR / "plaza.wrl").write_text(DEFAULT_PLAZA_WRL, encoding="utf-8")
    if not PERMS_FILE.exists():
        PERMS_FILE.write_text(json.dumps({
            "default_role": "builder",  # Change to "guest" for restricted public servers
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
        # First user to authenticate automatically becomes server admin
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

# Connected clients: ws -> dict(pubkey, nick, color, world, pos, rot, avatar_wrl, role)
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

                await websocket.send(json.dumps({
                    "type": "auth_ok",
                    "role": role,
                    "short_id": client_state["short_id"],
                    "world": world,
                    "wrl": (WORLDS_DIR / f"{world}.wrl").read_text(encoding="utf-8"),
                    "worlds": worlds_list,
                    "avatars": avatars_list
                }))

                # Send existing peers in this world
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

            # 3. Chat & Emotes
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
                else:
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

                await websocket.send(json.dumps({
                    "type": "world_data",
                    "world": target,
                    "wrl": wrl_path.read_text(encoding="utf-8")
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

            # 5. Upload World or Avatar (.wrl)
            elif msg_type == "upload":
                if client_state["role"] not in ("admin", "builder"):
                    await websocket.send(json.dumps({"type": "error", "msg": "Permission denied: builder role required."}))
                    continue

                kind = data.get("kind")  # "world" or "avatar"
                name = sanitize_name(data.get("name", "custom"))
                content = data.get("content", "")

                if len(content.encode("utf-8")) > MAX_FILE_SIZE:
                    await websocket.send(json.dumps({"type": "error", "msg": "File exceeds 5MB limit."}))
                    continue
                if "#VRML V2.0" not in content[:64]:
                    await websocket.send(json.dumps({"type": "error", "msg": "Invalid VRML97 header (#VRML V2.0 utf8 required)."}))
                    continue

                if kind == "world":
                    (WORLDS_DIR / f"{name}.wrl").write_text(content, encoding="utf-8")
                    worlds_list = [f.stem for f in WORLDS_DIR.glob("*.wrl")]
                    await websocket.send(json.dumps({"type": "upload_ok", "kind": "world", "name": name, "worlds": worlds_list}))
                elif kind == "avatar":
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
                        "wrl": av_path.read_text(encoding="utf-8")
                    }))

            # 7. Admin Permissions Control
            elif msg_type == "admin_perm":
                if client_state["role"] != "admin":
                    await websocket.send(json.dumps({"type": "error", "msg": "Admin privileges required."}))
                    continue
                action = data.get("action")  # grant_builder, revoke_builder, ban, set_default
                target_key = data.get("target_key", "").strip()
                perms = load_perms()

                if action == "grant_builder" and target_key:
                    if target_key not in perms["builders"]:
                        perms["builders"].append(target_key)
                elif action == "revoke_builder" and target_key:
                    perms["builders"] = [k for k in perms["builders"] if k != target_key]
                elif action == "ban" and target_key:
                    if target_key not in perms["banned"]:
                        perms["banned"].append(target_key)
                elif action == "set_default" and target_key in ("guest", "builder"):
                    perms["default_role"] = target_key

                save_perms(perms)
                await websocket.send(json.dumps({"type": "info", "msg": f"Permissions updated ({action})."}))

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
