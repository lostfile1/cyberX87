import asyncio
import json
import math
import os
import re
import sys
import threading
import time
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
DEFAULT_SERVER = "ws://localhost:8765"
DEFAULT_AI_URL = "http://localhost:8080/v1/chat/completions"

WINDOW_W, WINDOW_H = 1280, 760


def load_or_create_key() -> SigningKey:
    if KEY_FILE.exists():
        raw = bytes.fromhex(KEY_FILE.read_text().strip())
        return SigningKey(raw)
    sk = SigningKey.generate()
    KEY_FILE.write_text(sk.encode().hex())
    return sk


# --- Lightweight VRML97 (.wrl) Parser & OpenGL Renderer ---
def parse_vrml(wrl_text: str):
    """
    Parses VRML97 geometry primitives (Box, Sphere, Cylinder, Cone, IndexedLineSet,
    ElevationGrid, Transform, Anchor, Background, Fog) into renderable primitives.
    """
    scene = {
        "bg": (0.04, 0.01, 0.12),
        "fog": (0.05, 0.02, 0.15),
        "objects": [],
        "portals": [],
    }

    # Extract Background skyColor (first RGB triple)
    bg_m = re.search(r"Background\s*\{[^}]*skyColor\s*\[\s*([0-9.\s]+)", wrl_text, re.S)
    if bg_m:
        nums = [float(x) for x in bg_m.group(1).split()[:3]]
        if len(nums) == 3:
            scene["bg"] = tuple(nums)

    # Extract Fog color
    fog_m = re.search(r"Fog\s*\{[^}]*color\s+([0-9.\s]+)", wrl_text, re.S)
    if fog_m:
        nums = [float(x) for x in fog_m.group(1).split()[:3]]
        if len(nums) == 3:
            scene["fog"] = tuple(nums)

    # Recursive block walker for Transform, Anchor, and Shape
    def strip_comments(txt):
        return re.sub(r"#.*", "", txt)

    clean = strip_comments(wrl_text)
    tokens = re.findall(r'\{|\}|\[|\]|"[^"]*"|\S+', clean)

    def parse_vec3(idx):
        return (float(tokens[idx]), float(tokens[idx + 1]), float(tokens[idx + 2]))

    def parse_rot(idx):
        return (
            float(tokens[idx]),
            float(tokens[idx + 1]),
            float(tokens[idx + 2]),
            math.degrees(float(tokens[idx + 3])),
        )

    def walk(i, pos=(0.0, 0.0, 0.0), rot=(0.0, 1.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), portal_url=None):
        while i < len(tokens):
            tok = tokens[i]
            if tok == "}":
                return i + 1

            if tok == "Transform" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                t_pos, t_rot, t_scale = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (1.0, 1.0, 1.0)
                j = i + 2
                depth = 1
                # First scan immediate fields before children
                k = j
                while k < len(tokens) and depth > 0:
                    if tokens[k] == "{":
                        depth += 1
                    elif tokens[k] == "}":
                        depth -= 1
                    elif depth == 1:
                        if tokens[k] == "translation":
                            t_pos = parse_vec3(k + 1)
                        elif tokens[k] == "rotation":
                            t_rot = parse_rot(k + 1)
                        elif tokens[k] == "scale":
                            t_scale = parse_vec3(k + 1)
                    k += 1

                # Combine translation (approximate hierarchy)
                new_pos = (
                    pos[0] + t_pos[0] * scale[0],
                    pos[1] + t_pos[1] * scale[1],
                    pos[2] + t_pos[2] * scale[2],
                )
                new_scale = (scale[0] * t_scale[0], scale[1] * t_scale[1], scale[2] * t_scale[2])
                i = walk(i + 2, new_pos, t_rot if t_rot[3] != 0 else rot, new_scale, portal_url)
                continue

            if tok == "Anchor" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                p_url = portal_url
                k = i + 2
                depth = 1
                while k < len(tokens) and depth > 0:
                    if tokens[k] == "{":
                        depth += 1
                    elif tokens[k] == "}":
                        depth -= 1
                    elif depth == 1 and tokens[k] == "url":
                        # Look for quoted string
                        for m in range(k + 1, min(k + 6, len(tokens))):
                            if tokens[m].startswith('"'):
                                p_url = tokens[m].strip('"')
                                break
                    k += 1
                i = walk(i + 2, pos, rot, scale, p_url)
                continue

            if tok == "Shape" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                # Extract block text for this Shape
                start = i + 2
                depth = 1
                k = start
                while k < len(tokens) and depth > 0:
                    if tokens[k] == "{":
                        depth += 1
                    elif tokens[k] == "}":
                        depth -= 1
                    k += 1
                block = " ".join(tokens[start : k - 1])

                diff = (0.5, 0.5, 0.6)
                emis = (0.0, 0.0, 0.0)
                transp = 0.0

                dm = re.search(r"diffuseColor\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)", block)
                if dm:
                    diff = (float(dm.group(1)), float(dm.group(2)), float(dm.group(3)))
                em = re.search(r"emissiveColor\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)", block)
                if em:
                    emis = (float(em.group(1)), float(em.group(2)), float(em.group(3)))
                tm = re.search(r"transparency\s+([0-9.eE+-]+)", block)
                if tm:
                    transp = float(tm.group(1))

                obj = {
                    "pos": pos,
                    "rot": rot,
                    "scale": scale,
                    "diff": diff,
                    "emis": emis,
                    "alpha": max(0.15, 1.0 - transp),
                    "portal": portal_url,
                }

                if "Box" in block:
                    bm = re.search(r"Box\s*\{\s*size\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)", block)
                    sz = (float(bm.group(1)), float(bm.group(2)), float(bm.group(3))) if bm else (2.0, 2.0, 2.0)
                    obj["kind"] = "box"
                    obj["size"] = sz
                    scene["objects"].append(obj)
                elif "Cylinder" in block:
                    rm = re.search(r"radius\s+([0-9.eE+-]+)", block)
                    hm = re.search(r"height\s+([0-9.eE+-]+)", block)
                    obj["kind"] = "cylinder"
                    obj["radius"] = float(rm.group(1)) if rm else 1.0
                    obj["height"] = float(hm.group(1)) if hm else 2.0
                    scene["objects"].append(obj)
                elif "Cone" in block:
                    rm = re.search(r"bottomRadius\s+([0-9.eE+-]+)", block)
                    hm = re.search(r"height\s+([0-9.eE+-]+)", block)
                    obj["kind"] = "cone"
                    obj["radius"] = float(rm.group(1)) if rm else 1.0
                    obj["height"] = float(hm.group(1)) if hm else 2.0
                    scene["objects"].append(obj)
                elif "Sphere" in block:
                    rm = re.search(r"radius\s+([0-9.eE+-]+)", block)
                    obj["kind"] = "sphere"
                    obj["radius"] = float(rm.group(1)) if rm else 1.0
                    scene["objects"].append(obj)
                elif "IndexedLineSet" in block:
                    pm = re.search(r"point\s*\[([^\]]+)\]", block)
                    im = re.search(r"coordIndex\s*\[([^\]]+)\]", block)
                    if pm and im:
                        raw_pts = [float(x) for x in pm.group(1).replace(",", " ").split()]
                        pts = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                        idxs = [int(x) for x in im.group(1).replace(",", " ").split()]
                        obj["kind"] = "lines"
                        obj["pts"] = pts
                        obj["idxs"] = idxs
                        scene["objects"].append(obj)

                if portal_url:
                    scene["portals"].append({"pos": pos, "url": portal_url})

                i = k
                continue

            i += 1
        return i

    walk(0)
    return scene


# --- OpenGL Drawing Helpers ---
def draw_box(sx, sy, sz):
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    glBegin(GL_QUADS)
    # Front
    glNormal3f(0, 0, 1)
    glVertex3f(-hx, -hy, hz); glVertex3f(hx, -hy, hz); glVertex3f(hx, hy, hz); glVertex3f(-hx, hy, hz)
    # Back
    glNormal3f(0, 0, -1)
    glVertex3f(hx, -hy, -hz); glVertex3f(-hx, -hy, -hz); glVertex3f(-hx, hy, -hz); glVertex3f(hx, hy, -hz)
    # Top
    glNormal3f(0, 1, 0)
    glVertex3f(-hx, hy, hz); glVertex3f(hx, hy, hz); glVertex3f(hx, hy, -hz); glVertex3f(-hx, hy, -hz)
    # Bottom
    glNormal3f(0, -1, 0)
    glVertex3f(-hx, -hy, -hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, -hy, hz); glVertex3f(-hx, -hy, hz)
    # Right
    glNormal3f(1, 0, 0)
    glVertex3f(hx, -hy, hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, hy, -hz); glVertex3f(hx, hy, hz)
    # Left
    glNormal3f(-1, 0, 0)
    glVertex3f(-hx, -hy, -hz); glVertex3f(-hx, -hy, hz); glVertex3f(-hx, hy, hz); glVertex3f(-hx, hy, -hz)
    glEnd()


def draw_checker_floor():
    tile = 2.0
    half = 20
    glBegin(GL_QUADS)
    glNormal3f(0, 1, 0)
    for x in range(-half, half):
        for z in range(-half, half):
            if (x + z) % 2 == 0:
                glColor4f(0.06, 0.06, 0.12, 1.0)
            else:
                glColor4f(0.55, 0.60, 0.68, 1.0)
            x0, z0 = x * tile, z * tile
            glVertex3f(x0, 0.0, z0)
            glVertex3f(x0 + tile, 0.0, z0)
            glVertex3f(x0 + tile, 0.0, z0 + tile)
            glVertex3f(x0, 0.0, z0 + tile)
    glEnd()

    # Glowing cyan grid lines
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
        glColor4f(min(1.0, r * 0.3 + er), min(1.0, g * 0.3 + eg), min(1.0, b * 0.3 + eb), alpha)
    else:
        glEnable(GL_LIGHTING)
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
    elif kind == "lines":
        glDisable(GL_LIGHTING)
        glBegin(GL_LINE_STRIP)
        for idx in obj["idxs"]:
            if idx == -1:
                glEnd()
                glBegin(GL_LINE_STRIP)
            elif 0 <= idx < len(obj["pts"]):
                glVertex3f(*obj["pts"][idx])
        glEnd()

    glEnable(GL_LIGHTING)
    glPopMatrix()


def render_peer(peer, quadric, now):
    glPushMatrix()
    px, py, pz = peer.get("pos", [0.0, 1.0, 0.0])
    emote = peer.get("emote")
    emote_until = peer.get("emote_until", 0)
    extra_y = 0.0
    spin = 0.0
    tilt = 0.0

    if emote and now < emote_until:
        t = (emote_until - now) * 6.0
        if emote == "jump":
            extra_y = abs(math.sin(t)) * 1.4
        elif emote in ("spin", "dance"):
            spin = (t * 180.0) % 360.0
        elif emote == "wave":
            tilt = math.sin(t * 2.0) * 25.0

    glTranslatef(px, py + extra_y, pz)
    glRotatef(spin, 0, 1, 0)
    glRotatef(tilt, 0, 0, 1)

    hex_c = peer.get("color", "#00ffcc").lstrip("#")
    if len(hex_c) == 6:
        r = int(hex_c[0:2], 16) / 255.0
        g = int(hex_c[2:4], 16) / 255.0
        b = int(hex_c[4:6], 16) / 255.0
    else:
        r, g, b = 0.0, 1.0, 0.8

    # Body Cone
    glDisable(GL_LIGHTING)
    glColor4f(r, g, b, 0.85)
    glPushMatrix()
    glRotatef(-90, 1, 0, 0)
    gluCylinder(quadric, 0.45, 0.0, 1.4, 16, 1)
    glPopMatrix()

    # Head Sphere
    glColor4f(0.95, 0.95, 1.0, 1.0)
    glPushMatrix()
    glTranslatef(0.0, 1.65, 0.0)
    gluSphere(quadric, 0.28, 14, 14)
    glPopMatrix()

    glEnable(GL_LIGHTING)
    glPopMatrix()


# --- Network Client Thread (Asyncio WebSockets) ---
class MetaverseClient:
    def __init__(self, signing_key: SigningKey):
        self.sk = signing_key
        self.pubkey_hex = signing_key.verify_key.encode().hex()
        self.server_url = DEFAULT_SERVER
        self.nick = "NetRunner"
        self.color = "#00ffcc"
        self.role = "OFFLINE"
        self.short_id = ""
        self.world = "hub"
        self.worlds = ["hub"]
        self.scene = parse_vrml("")
        self.peers = {}
        self.chat_log = [
            ("SYSTEM", "#00ffcc", "Standalone Python VRML97 Client Ready."),
            ("HELP", "#ff66cc", "WASD=Move | Mouse=Look (ESC toggle) | E=Portal | T=Chat | /help"),
        ]
        self.out_queue = asyncio.Queue()
        self.loop = None
        self.connected = False
        self.reconnect_target = None

    def log(self, sender, color, text):
        self.chat_log.append((sender, color, text))
        if len(self.chat_log) > 100:
            self.chat_log.pop(0)

    def send_json(self, payload: dict):
        if self.loop and self.connected:
            self.loop.call_soon_threadsafe(self.out_queue.put_nowait, json.dumps(payload))

    def start_background(self):
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_net())

    async def _main_net(self):
        while True:
            target_world = "hub"
            if self.reconnect_target:
                self.server_url, target_world = self.reconnect_target
                self.reconnect_target = None

            self.log("NET", "#00ffcc", f"Connecting to {self.server_url}...")
            try:
                async with websockets.connect(self.server_url, max_size=6 * 1024 * 1024) as ws:
                    self.connected = True
                    # Drain stale messages
                    while not self.out_queue.empty():
                        self.out_queue.get_nowait()

                    async def sender_task():
                        while True:
                            msg = await self.out_queue.get()
                            await ws.send(msg)

                    send_fut = asyncio.create_task(sender_task())

                    async for raw in ws:
                        data = json.loads(raw)
                        mtype = data.get("type")

                        if mtype == "challenge":
                            sig = self.sk.sign(data["nonce"].encode("utf-8")).signature.hex()
                            await ws.send(json.dumps({
                                "type": "auth",
                                "pubkey": self.pubkey_hex,
                                "signature": sig,
                                "nick": self.nick,
                                "color": self.color,
                                "world": target_world,
                            }))
                        elif mtype == "auth_ok":
                            self.role = data.get("role", "guest").upper()
                            self.short_id = data.get("short_id", "")
                            self.world = data.get("world", "hub")
                            self.worlds = data.get("worlds", ["hub"])
                            self.peers.clear()
                            self.scene = parse_vrml(data.get("wrl", ""))
                            self.log("SYSTEM", "#00ff66", f"Authenticated as {self.role} ({self.short_id}) in '{self.world}'")
                        elif mtype == "world_data":
                            self.world = data.get("world", "hub")
                            self.peers.clear()
                            self.scene = parse_vrml(data.get("wrl", ""))
                            self.log("PORTAL", "#ff00aa", f"Entered world '{self.world}'")
                        elif mtype == "peers":
                            for p in data.get("peers", []):
                                self.peers[p["short_id"]] = p
                        elif mtype == "peer_join":
                            p = data["peer"]
                            self.peers[p["short_id"]] = p
                            self.log("WORLD", "#8888ff", f"{p['nick']} joined.")
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
                            sid = data.get("short_id")
                            em = data.get("emote", "wave")
                            if sid in self.peers:
                                self.peers[sid]["emote"] = em
                                self.peers[sid]["emote_until"] = time.time() + 2.5
                            self.log("*", "#ff66cc", f"{data.get('nick')} performs /{em}")
                        elif mtype == "upload_ok":
                            self.log("UPLOAD", "#00ff66", f"Uploaded {data.get('kind')}: {data.get('name')}")
                            if data.get("worlds"):
                                self.worlds = data["worlds"]
                            if data.get("kind") == "world":
                                await ws.send(json.dumps({"type": "join_world", "world": data["name"]}))
                        elif mtype in ("error", "info"):
                            self.log("SERVER", "#ffaa00", data.get("msg", ""))

                        if self.reconnect_target:
                            break

                    send_fut.cancel()
            except Exception as e:
                self.connected = False
                self.role = "OFFLINE"
                self.log("NET-ERR", "#ff4444", f"Disconnected ({e}). Retrying in 4s...")
                await asyncio.sleep(4)


# --- AI Agent World Generator (Background Thread) ---
def run_ai_world_gen(client: MetaverseClient, world_name: str, prompt: str, endpoint: str = DEFAULT_AI_URL):
    def worker():
        client.log("AI-AGENT", "#ff00aa", f"Asking LLM at {endpoint} to build '{world_name}'...")
        sys_prompt = (
            "You are a VRML97 (.wrl) generator. Output ONLY valid VRML 2.0 starting with "
            "'#VRML V2.0 utf8'. No markdown fences. Include Background, lighting, Box/Cylinder/Cone/Sphere "
            "shapes, and an Anchor with url [\"vrml://LOCAL/hub\"] as a return portal."
        )
        payload = json.dumps({
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
        }).encode("utf-8")

        req = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                wrl = data["choices"][0]["message"]["content"].strip()
                wrl = re.sub(r"^```[a-z]*\n?", "", wrl, flags=re.I)
                wrl = re.sub(r"```$", "", wrl).strip()
                if not wrl.startswith("#VRML V2.0"):
                    wrl = "#VRML V2.0 utf8\n" + wrl
                client.send_json({"type": "upload", "kind": "world", "name": world_name, "content": wrl})
        except Exception as e:
            client.log("AI-ERR", "#ff4444", f"AI generation failed: {e}")

    threading.Thread(target=worker, daemon=True).start()


# --- Slash Command Processor ---
def handle_command(client: MetaverseClient, text: str):
    parts = text.strip().split(" ", 2)
    cmd = parts[0].lower()

    if cmd == "/help":
        client.log("COMMANDS", "#ff66cc", "/nick <name> | /color <#hex> | /world <name> | /connect <ws://host:port>")
        client.log("COMMANDS", "#ff66cc", "/upload <world|avatar> <name> <file.wrl> | /ai <world_name> <prompt>")
        client.log("COMMANDS", "#ff66cc", "/emote <wave|dance|spin|jump> | /grant <pubkey> | /ban <pubkey> | /key")
    elif cmd == "/nick" and len(parts) > 1:
        client.nick = parts[1][:24]
        client.log("SYSTEM", "#00ffcc", f"Nickname set to {client.nick} (reconnects to apply).")
        client.reconnect_target = (client.server_url, client.world)
    elif cmd == "/color" and len(parts) > 1:
        client.color = parts[1][:7]
        client.reconnect_target = (client.server_url, client.world)
    elif cmd == "/world" and len(parts) > 1:
        client.send_json({"type": "join_world", "world": parts[1]})
    elif cmd == "/connect" and len(parts) > 1:
        client.reconnect_target = (parts[1], "hub")
    elif cmd == "/key":
        client.log("PUBKEY", "#00ff66", client.pubkey_hex)
    elif cmd == "/grant" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "grant_builder", "target_key": parts[1]})
    elif cmd == "/ban" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "ban", "target_key": parts[1]})
    elif cmd == "/upload" and len(parts) == 3:
        sub = text.strip().split(" ", 3)
        if len(sub) < 4:
            client.log("ERR", "#ff4444", "Usage: /upload <world|avatar> <name> <path.wrl>")
            return
        kind, name, fpath = sub[1], sub[2], sub[3]
        p = Path(fpath).expanduser()
        if not p.exists():
            client.log("ERR", "#ff4444", f"File not found: {p}")
            return
        client.send_json({"type": "upload", "kind": kind, "name": name, "content": p.read_text(encoding="utf-8")})
    elif cmd == "/ai" and len(parts) >= 3:
        run_ai_world_gen(client, parts[1], parts[2])
    else:
        client.send_json({"type": "chat", "text": text})


# --- HUD 2D Text Overlay Renderer ---
def draw_hud(client: MetaverseClient, font, chat_mode: bool, chat_buffer: str, near_portal: str | None):
    glMatrixMode(GL_PROJECTION)
    glPushMatrix()
    glLoadIdentity()
    glOrtho(0, WINDOW_W, WINDOW_H, 0, -1, 1)
    glMatrixMode(GL_MODELVIEW)
    glPushMatrix()
    glLoadIdentity()

    glDisable(GL_DEPTH_TEST)
    glDisable(GL_LIGHTING)

    def draw_rect(x, y, w, h, rgba):
        glColor4f(*rgba)
        glBegin(GL_QUADS)
        glVertex2f(x, y); glVertex2f(x + w, y); glVertex2f(x + w, y + h); glVertex2f(x, y + h)
        glEnd()

    def draw_text(x, y, text, color=(0, 255, 204)):
        surf = font.render(text, True, color)
        raw = pygame.image.tostring(surf, "RGBA", True)
        glRasterPos2f(x, y + surf.get_height())
        glDrawPixels(surf.get_width(), surf.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, raw)

    # Top Status Bar
    draw_rect(0, 0, WINDOW_W, 34, (0.04, 0.02, 0.10, 0.88))
    status = (
        f"cyberX87 Desktop | {client.server_url} | World: {client.world} "
        f"({', '.join(client.worlds)}) | {client.nick} [{client.role}]"
    )
    draw_text(12, 8, status, (0, 255, 204))

    # Portal Prompt
    if near_portal:
        draw_rect(WINDOW_W // 2 - 210, WINDOW_H // 2 + 40, 420, 36, (0.12, 0.02, 0.20, 0.9))
        draw_text(WINDOW_W // 2 - 195, WINDOW_H // 2 + 48, f"Press [E] to enter portal: {near_portal}", (255, 100, 220))

    # Bottom-Left Chat Box
    box_w, box_h = 560, 210
    bx, by = 12, WINDOW_H - box_h - 12
    draw_rect(bx, by, box_w, box_h, (0.04, 0.02, 0.10, 0.85))

    visible_lines = client.chat_log[-8:]
    for idx, (sender, hex_c, msg) in enumerate(visible_lines):
        hc = hex_c.lstrip("#")
        rgb = (int(hc[0:2], 16), int(hc[2:4], 16), int(hc[4:6], 16)) if len(hc) == 6 else (0, 255, 204)
        draw_text(bx + 8, by + 6 + idx * 20, f"[{sender}] {msg}"[:74], rgb)

    # Chat Input Line
    draw_rect(bx, by + box_h - 28, box_w, 28, (0.08, 0.04, 0.18, 0.95))
    prompt_txt = f"> {chat_buffer}_" if chat_mode else "Press [T] or [Enter] to chat / run /commands..."
    draw_text(bx + 8, by + box_h - 24, prompt_txt, (255, 255, 255) if chat_mode else (140, 180, 170))

    glEnable(GL_LIGHTING)
    glEnable(GL_DEPTH_TEST)
    glMatrixMode(GL_PROJECTION)
    glPopMatrix()
    glMatrixMode(GL_MODELVIEW)
    glPopMatrix()


# --- Main Desktop Application Loop ---
def main():
    if len(sys.argv) > 1:
        server_arg = sys.argv[1]
    else:
        server_arg = DEFAULT_SERVER

    sk = load_or_create_key()
    client = MetaverseClient(sk)
    client.server_url = server_arg
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
    glEnable(GL_COLOR_MATERIAL)
    glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
    glLightfv(GL_LIGHT0, GL_POSITION, (0.4, 1.0, 0.5, 0.0))
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.9, 0.95, 1.0, 1.0))

    quadric = gluNewQuadric()

    # Player Camera State
    cam_pos = [0.0, 1.8, 12.0]
    yaw = 0.0
    pitch = 0.0
    mouse_grabbed = True
    pygame.event.set_grab(True)
    pygame.mouse.set_visible(False)

    chat_mode = False
    chat_buffer = ""
    last_move_send = 0.0
    clock = pygame.time.Clock()

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        now = time.time()

        # Check proximity to any portal in the current VRML world
        near_portal = None
        for p in client.scene.get("portals", []):
            px, py, pz = p["pos"]
            dist = math.hypot(cam_pos[0] - px, cam_pos[2] - pz)
            if dist < 4.5:
                near_portal = p["url"]
                break

        for event in pygame.event.get():
            if event.type == QUIT:
                running = False

            elif event.type == KEYDOWN:
                if chat_mode:
                    if event.key == K_ESCAPE:
                        chat_mode = False
                        chat_buffer = ""
                    elif event.key == K_RETURN:
                        if chat_buffer.strip():
                            handle_command(client, chat_buffer)
                        chat_buffer = ""
                        chat_mode = False
                    elif event.key == K_BACKSPACE:
                        chat_buffer = chat_buffer[:-1]
                    else:
                        if event.unicode and event.unicode.isprintable():
                            chat_buffer += event.unicode
                else:
                    if event.key == K_ESCAPE:
                        mouse_grabbed = not mouse_grabbed
                        pygame.event.set_grab(mouse_grabbed)
                        pygame.mouse.set_visible(not mouse_grabbed)
                    elif event.key in (K_t, K_RETURN, K_SLASH):
                        chat_mode = True
                        chat_buffer = "/" if event.key == K_SLASH else ""
                    elif event.key == K_e and near_portal:
                        # Traverse portal (local world or remote server)
                        stripped = near_portal.replace("vrml://", "")
                        host_part, _, world_part = stripped.partition("/")
                        target_w = world_part or "hub"
                        if host_part == "LOCAL":
                            client.send_json({"type": "join_world", "world": target_w})
                        else:
                            client.reconnect_target = (f"ws://{host_part}", target_w)
                    elif event.key == K_1:
                        client.send_json({"type": "chat", "text": "/emote wave"})
                    elif event.key == K_2:
                        client.send_json({"type": "chat", "text": "/emote dance"})
                    elif event.key == K_3:
                        client.send_json({"type": "chat", "text": "/emote spin"})
                    elif event.key == K_4:
                        client.send_json({"type": "chat", "text": "/emote jump"})

            elif event.type == MOUSEMOTION and mouse_grabbed and not chat_mode:
                dx, dy = event.rel
                yaw -= dx * 0.18
                pitch = max(-85.0, min(85.0, pitch - dy * 0.18))

        # Movement (WASD + Space/Shift)
        if not chat_mode:
            keys = pygame.key.get_pressed()
            speed = 8.0 * dt
            rad = math.radians(yaw)
            fx, fz = -math.sin(rad), -math.cos(rad)
            rx, rz = math.cos(rad), -math.sin(rad)

            if keys[K_w] or keys[K_UP]:
                cam_pos[0] += fx * speed
                cam_pos[2] += fz * speed
            if keys[K_s] or keys[K_DOWN]:
                cam_pos[0] -= fx * speed
                cam_pos[2] -= fz * speed
            if keys[K_a] or keys[K_LEFT]:
                cam_pos[0] -= rx * speed
                cam_pos[2] -= rz * speed
            if keys[K_d] or keys[K_RIGHT]:
                cam_pos[0] += rx * speed
                cam_pos[2] += rz * speed
            if keys[K_SPACE]:
                cam_pos[1] += speed
            if keys[K_LSHIFT]:
                cam_pos[1] = max(1.2, cam_pos[1] - speed)

        # Broadcast position at 10Hz
        if now - last_move_send > 0.1:
            last_move_send = now
            client.send_json({
                "type": "move",
                "pos": [round(cam_pos[0], 3), round(cam_pos[1] - 1.8, 3), round(cam_pos[2], 3)],
                "rot": [0.0, 1.0, 0.0, round(math.radians(yaw), 3)],
            })

        # --- Render Frame ---
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

        # Draw floor & VRML scene objects
        draw_checker_floor()
        for obj in client.scene.get("objects", []):
            render_object(obj, quadric)

        # Draw connected multiplayer avatars
        for peer in list(client.peers.values()):
            render_peer(peer, quadric, now)

        # Draw 2D HUD overlay
        draw_hud(client, font, chat_mode, chat_buffer, near_portal)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
