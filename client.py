import asyncio
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
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
LAST_AI_WRL = Path.home() / ".cyberx87_last_ai.wrl"
PLUGINS_DIR = Path("./plugins")
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


def load_x87_plugins(plugin_dir: Path = PLUGINS_DIR) -> dict:
    """Loads human-readable .x87 command definitions from ./plugins/."""
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
            "DESC: Teleport back to Genesis Hub\n"
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
                if in_steps and (line.startswith("  ") or line.startswith("\t")):
                    entry["steps"].append(stripped)
                    continue
                in_steps = False
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    entry[k.strip().lower()] = v.strip()

            cmd_name = entry.get("command", "").lower()
            if cmd_name:
                if not cmd_name.startswith("/"):
                    cmd_name = "/" + cmd_name
                commands[cmd_name] = entry
    return commands


def _safe_floats(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", text)]


def _safe_ints(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"[-+]?\d+", text)]


# --- Universal VRML 1.0 & VRML 2.0/97 (.wrl) Parser ---
def parse_vrml(wrl_text: str):
    scene = {
        "bg": (0.04, 0.01, 0.12),
        "fog": (0.05, 0.02, 0.15),
        "objects": [],
        "portals": [],
        "has_floor": False,
        "raw_len": len(wrl_text or ""),
        "raw_preview": "",
    }

    if not wrl_text or not wrl_text.strip():
        return scene

    is_vrml1 = "#VRML V1.0" in wrl_text[:64]

    clean = re.sub(r"#.*", "", wrl_text)
    non_empty_lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    scene["raw_preview"] = " | ".join(non_empty_lines[:5])[:160]

    bg_m = re.search(r"Background\s*\{[^}]*skyColor\s*\[\s*([^\]]+)\]", clean, re.S)
    if bg_m:
        nums = _safe_floats(bg_m.group(1))
        if len(nums) >= 3:
            scene["bg"] = (nums[0], nums[1], nums[2])

    fog_m = re.search(r"Fog\s*\{[^}]*color\s+([^\n\}]+)", clean)
    if fog_m:
        nums = _safe_floats(fog_m.group(1))
        if len(nums) >= 3:
            scene["fog"] = (nums[0], nums[1], nums[2])

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

    CONTAINER_NODES = {"Group", "Collision", "Billboard", "Switch", "LOD", "Inline"}
    defs_table = {}

    def parse_shape_block(block: str, pos, rot, scale, portal_url):
        diff = (0.45, 0.65, 0.85)
        emis = (0.05, 0.08, 0.12)
        transp = 0.0

        dm = re.search(r"diffuseColor\s+([-+0-9.eE\s]+)", block)
        if dm:
            vals = _safe_floats(dm.group(1))
            if len(vals) >= 3:
                diff = (vals[0], vals[1], vals[2])

        em = re.search(r"emissiveColor\s+([-+0-9.eE\s]+)", block)
        if em:
            vals = _safe_floats(em.group(1))
            if len(vals) >= 3:
                emis = (vals[0], vals[1], vals[2])

        tm = re.search(r"transparency\s+([-+0-9.eE]+)", block)
        if tm:
            vals = _safe_floats(tm.group(1))
            if vals:
                transp = vals[0]

        base_obj = {
            "pos": pos,
            "rot": rot,
            "scale": scale,
            "diff": diff,
            "emis": emis,
            "alpha": max(0.25, 1.0 - transp),
            "portal": portal_url,
        }

        if re.search(r"\bBox\b", block):
            bm = re.search(r"\bBox\s*\{([^}]*)\}", block)
            vals = _safe_floats(bm.group(1)) if bm else []
            sz = (vals[0], vals[1], vals[2]) if len(vals) >= 3 else (2.0, 2.0, 2.0)
            obj = {**base_obj, "kind": "box", "size": sz}
            if sz[0] >= 30 and sz[2] >= 30 and sz[1] <= 1.0:
                scene["has_floor"] = True
            return obj

        if re.search(r"\bCylinder\b", block):
            cm = re.search(r"\bCylinder\s*\{([^}]*)\}", block)
            cbody = cm.group(1) if cm else ""
            rm = re.search(r"radius\s+([^\s\}]+)", cbody)
            hm = re.search(r"height\s+([^\s\}]+)", cbody)
            return {
                **base_obj,
                "kind": "cylinder",
                "radius": _safe_floats(rm.group(1))[0] if rm else 1.0,
                "height": _safe_floats(hm.group(1))[0] if hm else 2.0,
            }

        if re.search(r"\bCone\b", block):
            cm = re.search(r"\bCone\s*\{([^}]*)\}", block)
            cbody = cm.group(1) if cm else ""
            rm = re.search(r"bottomRadius\s+([^\s\}]+)", cbody)
            hm = re.search(r"height\s+([^\s\}]+)", cbody)
            return {
                **base_obj,
                "kind": "cone",
                "radius": _safe_floats(rm.group(1))[0] if rm else 1.0,
                "height": _safe_floats(hm.group(1))[0] if hm else 2.0,
            }

        if re.search(r"\bSphere\b", block):
            sm = re.search(r"\bSphere\s*\{([^}]*)\}", block)
            sbody = sm.group(1) if sm else ""
            rm = re.search(r"radius\s+([^\s\}]+)", sbody)
            return {
                **base_obj,
                "kind": "sphere",
                "radius": _safe_floats(rm.group(1))[0] if rm else 1.0,
            }

        if re.search(r"\bIndexedFaceSet\b", block):
            pm = re.search(r"point\s*\[([^\]]+)\]", block)
            im = re.search(r"coordIndex\s*\[([^\]]+)\]", block)
            if pm:
                raw_pts = _safe_floats(pm.group(1))
                pts = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                if im:
                    idxs = _safe_ints(im.group(1))
                else:
                    idxs = []
                    for n in range(0, len(pts) - 2, 3):
                        idxs.extend([n, n + 1, n + 2, -1])
                return {**base_obj, "kind": "faces", "pts": pts, "idxs": idxs}

        if re.search(r"\bIndexedLineSet\b", block):
            pm = re.search(r"point\s*\[([^\]]+)\]", block)
            im = re.search(r"coordIndex\s*\[([^\]]+)\]", block)
            if pm:
                raw_pts = _safe_floats(pm.group(1))
                pts = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                idxs = _safe_ints(im.group(1)) if im else list(range(len(pts)))
                return {**base_obj, "kind": "lines", "pts": pts, "idxs": idxs}

        if re.search(r"\bElevationGrid\b", block):
            xd_m = re.search(r"xDimension\s+(\d+)", block)
            zd_m = re.search(r"zDimension\s+(\d+)", block)
            xs_m = re.search(r"xSpacing\s+([^\s\}]+)", block)
            zs_m = re.search(r"zSpacing\s+([^\s\}]+)", block)
            hm = re.search(r"height\s*\[([^\]]+)\]", block)
            if xd_m and zd_m and hm:
                scene["has_floor"] = True
                return {
                    **base_obj,
                    "kind": "grid",
                    "xd": int(xd_m.group(1)),
                    "zd": int(zd_m.group(1)),
                    "xs": _safe_floats(xs_m.group(1))[0] if xs_m else 1.0,
                    "zs": _safe_floats(zs_m.group(1))[0] if zs_m else 1.0,
                    "heights": _safe_floats(hm.group(1)),
                }

        if re.search(r"\bExtrusion\b", block):
            sp_m = re.search(r"spine\s*\[([^\]]+)\]", block)
            cs_m = re.search(r"crossSection\s*\[([^\]]+)\]", block)
            spine_pts = [(0, 0, 0), (0, 2, 0)]
            if sp_m:
                raw_sp = _safe_floats(sp_m.group(1))
                if len(raw_sp) >= 6:
                    spine_pts = [(raw_sp[n], raw_sp[n + 1], raw_sp[n + 2]) for n in range(0, len(raw_sp) - 2, 3)]
            cs_pts = [(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)]
            if cs_m:
                raw_cs = _safe_floats(cs_m.group(1))
                if len(raw_cs) >= 4:
                    cs_pts = [(raw_cs[n], raw_cs[n + 1]) for n in range(0, len(raw_cs) - 1, 2)]
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
                child_state = {
                    "pos": state["pos"], "rot": state["rot"], "scale": state["scale"],
                    "diff": state["diff"], "emis": state["emis"], "pts": list(state["pts"]),
                    "portal": state["portal"],
                }
                i = walk_vrml1(i + 2, child_state)
                continue

            if tok == "WWWAnchor" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                end_idx = skip_balanced(i + 1, "{", "}")
                blk = " ".join(tokens[i + 2 : end_idx - 1])
                um = re.search(r'name\s+"([^"]+)"', blk)
                child_state = {**state, "pts": list(state["pts"]), "portal": um.group(1) if um else state["portal"]}
                walk_vrml1(i + 2, child_state)
                i = end_idx
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
                    dm = re.search(r"diffuseColor\s+([-+0-9.eE\s]+)", blk)
                    if dm:
                        v = _safe_floats(dm.group(1))
                        if len(v) >= 3:
                            state["diff"] = (v[0], v[1], v[2])
                    em = re.search(r"emissiveColor\s+([-+0-9.eE\s]+)", blk)
                    if em:
                        v = _safe_floats(em.group(1))
                        if len(v) >= 3:
                            state["emis"] = (v[0], v[1], v[2])
                elif tok == "Coordinate3":
                    pm = re.search(r"point\s*\[([^\]]+)\]", blk)
                    if pm:
                        raw_pts = _safe_floats(pm.group(1))
                        state["pts"] = [(raw_pts[n], raw_pts[n + 1], raw_pts[n + 2]) for n in range(0, len(raw_pts) - 2, 3)]
                elif tok == "Cube":
                    w = _safe_floats(re.search(r"width\s+([^\s\}]+)", blk).group(1))[0] if "width" in blk else 2.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    d = _safe_floats(re.search(r"depth\s+([^\s\}]+)", blk).group(1))[0] if "depth" in blk else 2.0
                    scene["objects"].append({
                        "kind": "box", "size": (w, h, d), "pos": state["pos"], "rot": state["rot"],
                        "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0,
                        "portal": state["portal"],
                    })
                elif tok == "Sphere":
                    r = _safe_floats(re.search(r"radius\s+([^\s\}]+)", blk).group(1))[0] if "radius" in blk else 1.0
                    scene["objects"].append({
                        "kind": "sphere", "radius": r, "pos": state["pos"], "rot": state["rot"],
                        "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0,
                        "portal": state["portal"],
                    })
                elif tok == "Cylinder":
                    r = _safe_floats(re.search(r"radius\s+([^\s\}]+)", blk).group(1))[0] if "radius" in blk else 1.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    scene["objects"].append({
                        "kind": "cylinder", "radius": r, "height": h, "pos": state["pos"], "rot": state["rot"],
                        "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0,
                        "portal": state["portal"],
                    })
                elif tok == "Cone":
                    r = _safe_floats(re.search(r"bottomRadius\s+([^\s\}]+)", blk).group(1))[0] if "bottomRadius" in blk else 1.0
                    h = _safe_floats(re.search(r"height\s+([^\s\}]+)", blk).group(1))[0] if "height" in blk else 2.0
                    scene["objects"].append({
                        "kind": "cone", "radius": r, "height": h, "pos": state["pos"], "rot": state["rot"],
                        "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0,
                        "portal": state["portal"],
                    })
                elif tok == "IndexedFaceSet" and state["pts"]:
                    im = re.search(r"coordIndex\s*\[([^\]]+)\]", blk)
                    idxs = _safe_ints(im.group(1)) if im else list(range(len(state["pts"])))
                    scene["objects"].append({
                        "kind": "faces", "pts": state["pts"], "idxs": idxs, "pos": state["pos"], "rot": state["rot"],
                        "scale": state["scale"], "diff": state["diff"], "emis": state["emis"], "alpha": 1.0,
                        "portal": state["portal"],
                    })

                if state["portal"]:
                    scene["portals"].append({"pos": state["pos"], "url": state["portal"]})

                i = end_idx
                continue
            i += 1
        return i

    def walk_vrml2(i, pos=(0.0, 0.0, 0.0), rot=(0.0, 1.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), portal_url=None, record_list=None):
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

            if tok == "USE" and i + 1 < len(tokens):
                use_name = tokens[i + 1]
                for template in defs_table.get(use_name, []):
                    cloned = {
                        **template,
                        "pos": (
                            pos[0] + template["local_pos"][0] * scale[0],
                            pos[1] + template["local_pos"][1] * scale[1],
                            pos[2] + template["local_pos"][2] * scale[2],
                        ),
                        "scale": (
                            scale[0] * template["local_scale"][0],
                            scale[1] * template["local_scale"][1],
                            scale[2] * template["local_scale"][2],
                        ),
                    }
                    scene["objects"].append(cloned)
                    if record_list is not None:
                        record_list.append(template)
                i += 2
                continue

            if tok == "Transform" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                t_pos, t_rot, t_scale = (0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (1.0, 1.0, 1.0)
                k = i + 2
                depth = 1
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
                                t_rot = (vals[0], vals[1], vals[2], math.degrees(vals[3]))
                        elif tokens[k] == "scale":
                            vals = read_floats_at(k + 1, 3)
                            if len(vals) == 3:
                                t_scale = tuple(vals)
                    k += 1

                new_pos = (
                    pos[0] + t_pos[0] * scale[0],
                    pos[1] + t_pos[1] * scale[1],
                    pos[2] + t_pos[2] * scale[2],
                )
                new_scale = (scale[0] * t_scale[0], scale[1] * t_scale[1], scale[2] * t_scale[2])
                active_rot = t_rot if t_rot[3] != 0.0 else rot
                sub_records = [] if def_name else None
                i = walk_vrml2(i + 2, new_pos, active_rot, new_scale, portal_url, sub_records)
                if def_name and sub_records is not None:
                    defs_table[def_name] = sub_records
                if record_list is not None and sub_records:
                    record_list.extend(sub_records)
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
                        for m in range(k + 1, min(k + 8, len(tokens))):
                            if tokens[m].startswith('"'):
                                p_url = tokens[m].strip('"')
                                break
                    k += 1
                i = walk_vrml2(i + 2, pos, rot, scale, p_url, record_list)
                continue

            if tok in CONTAINER_NODES and i + 1 < len(tokens) and tokens[i + 1] == "{":
                sub_records = [] if def_name else None
                i = walk_vrml2(i + 2, pos, rot, scale, portal_url, sub_records)
                if def_name and sub_records is not None:
                    defs_table[def_name] = sub_records
                if record_list is not None and sub_records:
                    record_list.extend(sub_records)
                continue

            if tok == "Shape" and i + 1 < len(tokens) and tokens[i + 1] == "{":
                end_idx = skip_balanced(i + 1, "{", "}")
                block = " ".join(tokens[i + 2 : end_idx - 1])
                obj = parse_shape_block(block, pos, rot, scale, portal_url)
                if obj:
                    scene["objects"].append(obj)
                    tmpl = {**obj, "local_pos": (0.0, 0.0, 0.0), "local_scale": scale}
                    if def_name:
                        defs_table[def_name] = [tmpl]
                    if record_list is not None:
                        record_list.append(tmpl)
                if portal_url:
                    scene["portals"].append({"pos": pos, "url": portal_url})
                i = end_idx
                continue

            if i + 1 < len(tokens) and tokens[i + 1] == "{":
                i = skip_balanced(i + 1, "{", "}")
                continue

            i += 1
        return i

    if is_vrml1:
        walk_vrml1(0, {
            "pos": (0.0, 0.0, 0.0), "rot": (0.0, 1.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0),
            "diff": (0.5, 0.65, 0.8), "emis": (0.0, 0.0, 0.0), "pts": [], "portal": None,
        })
    else:
        walk_vrml2(0)

    if not scene["objects"]:
        for idx, tok in enumerate(tokens):
            if tok == "Shape" and idx + 1 < len(tokens) and tokens[idx + 1] == "{":
                end_idx = skip_balanced(idx + 1, "{", "}")
                block = " ".join(tokens[idx + 2 : end_idx - 1])
                near_pos = (0.0, 1.0, -8.0)
                for b in range(max(0, idx - 25), idx):
                    if tokens[b] == "translation":
                        vals = read_floats_at(b + 1, 3)
                        if len(vals) == 3:
                            near_pos = tuple(vals)
                obj = parse_shape_block(block, near_pos, (0.0, 1.0, 0.0, 0.0), (1.0, 1.0, 1.0), None)
                if obj:
                    scene["objects"].append(obj)

    return scene


# --- OpenGL Drawing Helpers ---
def draw_box(sx, sy, sz):
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    glBegin(GL_QUADS)
    glNormal3f(0, 0, 1)
    glVertex3f(-hx, -hy, hz); glVertex3f(hx, -hy, hz); glVertex3f(hx, hy, hz); glVertex3f(-hx, hy, hz)
    glNormal3f(0, 0, -1)
    glVertex3f(hx, -hy, -hz); glVertex3f(-hx, -hy, -hz); glVertex3f(-hx, hy, -hz); glVertex3f(hx, hy, -hz)
    glNormal3f(0, 1, 0)
    glVertex3f(-hx, hy, hz); glVertex3f(hx, hy, hz); glVertex3f(hx, hy, -hz); glVertex3f(-hx, hy, -hz)
    glNormal3f(0, -1, 0)
    glVertex3f(-hx, -hy, -hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, -hy, hz); glVertex3f(-hx, -hy, hz)
    glNormal3f(1, 0, 0)
    glVertex3f(hx, -hy, hz); glVertex3f(hx, -hy, -hz); glVertex3f(hx, hy, -hz); glVertex3f(hx, hy, hz)
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

    glDisable(GL_LIGHTING)
    glColor3f(0.0, 0.85, 0.95)
    glBegin(GL_LINES)
    for i in range(-40, 41, 10):
        glVertex3f(i, 0.01, -40); glVertex3f(i, 0.01, 40)
        glVertex3f(-40, 0.01, i); glVertex3f(40, 0.01, i)
    glEnd()
    glEnable(GL_LIGHTING)


def render_object(obj, quadric, now=0.0):
    glPushMatrix()
    glTranslatef(*obj["pos"])
    rx, ry, rz, ang = obj["rot"]
    if ang != 0.0:
        glRotatef(ang, rx, ry, rz)
    glScalef(*obj["scale"])

    r, g, b = obj["diff"]
    er, eg, eb = obj["emis"]
    alpha = obj["alpha"]
    is_portal_membrane = bool(obj.get("portal")) and alpha < 0.9

    if er > 0.05 or eg > 0.05 or eb > 0.05:
        glDisable(GL_LIGHTING)
        pulse = (0.85 + 0.15 * math.sin(now * 4.0)) if is_portal_membrane else 1.0
        glColor4f(
            min(1.0, (r * 0.4 + er) * pulse),
            min(1.0, (g * 0.4 + eg) * pulse),
            min(1.0, (b * 0.4 + eb) * pulse),
            alpha,
        )
    else:
        glEnable(GL_LIGHTING)
        glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION, (r * 0.25, g * 0.25, b * 0.25, 1.0))
        glColor4f(r, g, b, alpha)

    kind = obj["kind"]
    if kind == "box":
        draw_box(*obj["size"])
        if is_portal_membrane:
            glLineWidth(2.0)
            for ring_i, scale_f in enumerate((0.88, 0.62, 0.36)):
                glPushMatrix()
                glTranslatef(0.0, 0.0, 0.11 if ring_i % 2 == 0 else -0.11)
                glRotatef((now * (55.0 if ring_i % 2 == 0 else -80.0)) % 360.0, 0, 0, 1)
                glColor4f(1.0, 1.0, 1.0, 0.75)
                glBegin(GL_LINE_LOOP)
                segs = 28
                rx_r = (obj["size"][0] * 0.48) * scale_f
                ry_r = (obj["size"][1] * 0.46) * scale_f
                for s in range(segs):
                    theta = (2.0 * math.pi * s) / segs
                    glVertex3f(math.cos(theta) * rx_r, math.sin(theta) * ry_r, 0.0)
                glEnd()
                glPopMatrix()
            glLineWidth(1.0)
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
    elif kind == "faces":
        pts = obj["pts"]
        glBegin(GL_POLYGON)
        glNormal3f(0, 1, 0)
        for idx in obj["idxs"]:
            if idx == -1:
                glEnd()
                glBegin(GL_POLYGON)
            elif 0 <= idx < len(pts):
                glVertex3f(*pts[idx])
        glEnd()
    elif kind == "lines":
        glDisable(GL_LIGHTING)
        pts = obj["pts"]
        glBegin(GL_LINE_STRIP)
        for idx in obj["idxs"]:
            if idx == -1:
                glEnd()
                glBegin(GL_LINE_STRIP)
            elif 0 <= idx < len(pts):
                glVertex3f(*pts[idx])
        glEnd()
    elif kind == "extrusion":
        spine = obj["spine"]
        cross = obj["cross"]
        for s_idx in range(len(spine) - 1):
            p0 = spine[s_idx]
            p1 = spine[s_idx + 1]
            glBegin(GL_QUAD_STRIP)
            glNormal3f(0, 0, 1)
            for cx, cz in cross:
                glVertex3f(p0[0] + cx, p0[1], p0[2] + cz)
                glVertex3f(p1[0] + cx, p1[1], p1[2] + cz)
            glEnd()
    elif kind == "grid":
        xd, zd, xs, zs, heights = obj["xd"], obj["zd"], obj["xs"], obj["zs"], obj["heights"]
        glBegin(GL_QUADS)
        glNormal3f(0, 1, 0)
        for z in range(zd - 1):
            for x in range(xd - 1):
                i00 = z * xd + x
                i10 = i00 + 1
                i11 = (z + 1) * xd + (x + 1)
                i01 = (z + 1) * xd + x
                if i11 >= len(heights):
                    continue
                if (x + z) % 2 == 0:
                    glColor4f(max(0.05, r * 0.2), max(0.05, g * 0.2), max(0.1, b * 0.25), alpha)
                else:
                    glColor4f(r, g, b, alpha)
                glVertex3f(x * xs, heights[i00], z * zs)
                glVertex3f((x + 1) * xs, heights[i10], z * zs)
                glVertex3f((x + 1) * xs, heights[i11], (z + 1) * zs)
                glVertex3f(x * xs, heights[i01], (z + 1) * zs)
        glEnd()

    glMaterialfv(GL_FRONT_AND_BACK, GL_EMISSION, (0.0, 0.0, 0.0, 1.0))
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


# --- Network Client Thread (Asyncio WebSockets) ---
class MetaverseClient:
    def __init__(self, signing_key: SigningKey):
        self.sk = signing_key
        self.pubkey_hex = signing_key.verify_key.encode().hex()
        self.server_url = DEFAULT_SERVER
        self.ai_url = DEFAULT_AI_URL
        self.ai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.nick = "NetRunner"
        self.color = "#00ffcc"
        self.role = "OFFLINE"
        self.short_id = ""
        self.world = "hub"
        self.worlds = ["hub"]
        self.scene = parse_vrml("")
        self.peers = {}
        self.plugins = load_x87_plugins()
        self.chat_log = [
            ("SYSTEM", "#00ffcc", "Standalone VRML 1.0 & 2.0 Client Ready."),
            ("HELP", "#ff66cc", "WASD=Move | Mouse=Look (ESC) | E=Portal | T=Chat | /help | /plugins"),
        ]
        self.out_queue = asyncio.Queue()
        self.loop = None
        self.connected = False
        self.reconnect_target = None
        self.teleport_req = None

    def log(self, sender, color, text):
        self.chat_log.append((sender, color, text))
        if len(self.chat_log) > 100:
            self.chat_log.pop(0)

    def send_json(self, payload: dict):
        if self.loop and self.connected:
            self.loop.call_soon_threadsafe(self.out_queue.put_nowait, json.dumps(payload))
        else:
            self.log("NET-ERR", "#ff4444", "Not connected to server.")

    def start_background(self):
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main_net())

    def _apply_world(self, world_name: str, wrl_text: str, prefix: str):
        self.world = world_name
        self.peers.clear()
        new_scene = parse_vrml(wrl_text)
        self.scene = new_scene
        objs = new_scene.get("objects", [])
        self.log(prefix, "#00ff66", f"Loaded '{world_name}': {len(objs)} objects ({new_scene['raw_len']} bytes)")

        if world_name.lower() == "hub":
            self.teleport_req = [0.0, 2.2, 8.0]
            return

        props = [o for o in objs if o["kind"] != "grid" and o.get("size", (0, 0, 0))[0] < 30]
        if props:
            cx = sum(o["pos"][0] for o in props) / len(props)
            cy = sum(o["pos"][1] for o in props) / len(props)
            cz = sum(o["pos"][2] for o in props) / len(props)
            self.teleport_req = [cx, max(2.2, cy + 1.5), cz + 14.0]
        else:
            self.teleport_req = [0.0, 2.0, 12.0]
            if not objs:
                self.log("WARN", "#ffaa00", f"0 objects parsed! Preview: {new_scene['raw_preview']}")

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
                            self.worlds = data.get("worlds", ["hub"])
                            self._apply_world(data.get("world", "hub"), data.get("wrl", ""), "AUTH")
                        elif mtype == "world_data":
                            self._apply_world(data.get("world", "hub"), data.get("wrl", ""), "PORTAL")
                        elif mtype == "worlds_list":
                            self.worlds = data.get("worlds", ["hub"])
                            cur = data.get("current", self.world)
                            formatted = ", ".join(f"*{w}*" if w == cur else w for w in self.worlds)
                            self.log("WORLDS", "#00ff66", f"Available ({len(self.worlds)}): {formatted}")
                        elif mtype == "perms_list":
                            p = data.get("perms", {})
                            admins = [k[:12] + "..." for k in p.get("admins", [])]
                            builders = [k[:12] + "..." for k in p.get("builders", [])]
                            banned = [k[:12] + "..." for k in p.get("banned", [])]
                            self.log("PERMS", "#00ff66", f"Default: {p.get('default_role', 'builder')} | Admins ({len(admins)}): {', '.join(admins) or 'none'}")
                            self.log("PERMS", "#00ffcc", f"Builders ({len(builders)}): {', '.join(builders) or 'none'} | Banned ({len(banned)}): {', '.join(banned) or 'none'}")
                        elif mtype == "role_updated":
                            self.role = data.get("role", "guest").upper()
                            self.log("SYSTEM", "#ff00aa", f"Your server role was updated to: {self.role}")
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


# --- AI Agent World Generator (Background Thread, No Auto-Return Portals) ---
def run_ai_world_gen(client: MetaverseClient, world_name: str, prompt: str):
    def worker():
        endpoint = client.ai_url.strip()
        if not endpoint.endswith("/chat/completions"):
            endpoint = endpoint.rstrip("/")
            if endpoint.endswith("/v1"):
                endpoint += "/chat/completions"
            else:
                endpoint += "/v1/chat/completions"

        client.log("AI-AGENT", "#ff00aa", f"Requesting '{world_name}' from {endpoint}...")
        sys_prompt = (
            "You are a VRML97 (.wrl) scene generator. Output ONLY valid VRML 2.0 code starting with "
            "'#VRML V2.0 utf8'. Do not include markdown fences, comments, portals, Anchor nodes, or explanations. "
            "Build 8 to 15 colorful 3D objects centered around (0, 2, 0) using Transform { translation x y z children [ "
            "Shape { appearance Appearance { material Material { diffuseColor r g b emissiveColor r g b } } "
            "geometry Box { size w h d } } ] } (also use Cylinder, Cone, Sphere)."
        )
        payload = json.dumps({
            "model": "default",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 4096,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if client.ai_api_key:
            headers["Authorization"] = f"Bearer {client.ai_api_key}"

        req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw_body)
                wrl = data["choices"][0]["message"]["content"].strip()
                wrl = re.sub(r"<think>.*?</think>", "", wrl, flags=re.S).strip()
                wrl = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", wrl)
                wrl = re.sub(r"\s*```$", "", wrl).strip()
                if "#VRML V2.0" in wrl:
                    wrl = wrl[wrl.index("#VRML V2.0"):]
                else:
                    wrl = "#VRML V2.0 utf8\n" + wrl

                test_parse = parse_vrml(wrl)
                if len(test_parse["objects"]) == 0:
                    wrl += """
Transform {
  translation 0 2 -8
  children [
    Shape {
      appearance Appearance { material Material { diffuseColor 0.0 1.0 0.8 emissiveColor 0.0 0.6 0.5 } }
      geometry Cylinder { radius 1.5 height 4.0 }
    }
  ]
}
Transform {
  translation 0 4.8 -8
  children [
    Shape {
      appearance Appearance { material Material { diffuseColor 1.0 0.2 0.8 emissiveColor 0.7 0.1 0.5 } }
      geometry Sphere { radius 1.4 }
    }
  ]
}
"""
                    test_parse = parse_vrml(wrl)

                LAST_AI_WRL.write_text(wrl, encoding="utf-8")
                obj_cnt = len(test_parse["objects"])
                client.log("AI-AGENT", "#00ff66", f"Generated {len(wrl)}B ({obj_cnt} objs). Saved to {LAST_AI_WRL.name}")
                client.send_json({"type": "upload", "kind": "world", "name": world_name, "content": wrl})
        except urllib.error.HTTPError as e:
            err_detail = e.read().decode("utf-8", errors="replace")[:120]
            client.log("AI-ERR", "#ff4444", f"HTTP {e.code} from AI endpoint: {err_detail}")
        except urllib.error.URLError as e:
            client.log("AI-ERR", "#ff4444", f"Cannot reach {endpoint} ({e.reason}). Check /aiurl.")
        except Exception as e:
            client.log("AI-ERR", "#ff4444", f"AI generation error: {e}")

    threading.Thread(target=worker, daemon=True).start()


# --- Slash Command Processor ---
def handle_command(client: MetaverseClient, text: str):
    cleaned = " ".join(text.strip().split())
    parts = cleaned.split(" ", 2)
    cmd = parts[0].lower()

    if cmd == "/help":
        client.log("COMMANDS", "#ff66cc", "/worlds | /world <name> | /delworld <name> | /inspect | /tp [x y z] | /plugins | /reload")
        client.log("COMMANDS", "#ff66cc", "/upload <world|avatar> <name> <file.wrl> | /ai <world_name> <prompt> | /aiurl <url> [key]")
        client.log("COMMANDS", "#ff66cc", "/perms | /admin <key> | /unadmin <key> | /grant <key> | /ungrant <key> | /ban <key> | /unban <key>")
        client.log("COMMANDS", "#ff66cc", "/defaultrole <guest|builder> | /nick <name> | /color <#hex> | /connect <ws://host:port> | /key")
    elif cmd == "/plugins":
        if not client.plugins:
            client.log("PLUGINS", "#ffaa00", "No .x87 plugins loaded in ./plugins/")
        else:
            for cname, meta in client.plugins.items():
                client.log("PLUGIN", "#00ff66", f"{cname} — {meta.get('desc', 'Custom command')} [{meta['file']}]")
        client.log("PLUGINS", "#00ffcc", "Server plugins (data/plugins/): /portal <world>, /fedportal <host:port/world>, /pillar, /announce")
    elif cmd == "/reload":
        client.plugins = load_x87_plugins()
        client.log("PLUGINS", "#00ff66", f"Reloaded {len(client.plugins)} custom command(s) from ./plugins/")
    elif cmd in client.plugins:
        spec = client.plugins[cmd]
        args_str = cleaned.split(" ", 1)[1] if " " in cleaned else ""
        ctx = {
            "nick": client.nick,
            "world": client.world,
            "role": client.role,
            "args": args_str,
        }

        def run_step(action: str, val: str):
            rendered = val.format_map({**ctx, **{k: "" for k in ("nick", "world", "role", "args")}})
            if action == "chat":
                client.send_json({"type": "chat", "text": rendered})
            elif action == "emote":
                client.send_json({"type": "chat", "text": f"/emote {rendered}"})
            elif action == "world":
                client.send_json({"type": "join_world", "world": rendered})
            elif action == "connect":
                client.reconnect_target = (rendered, "hub")
            elif action == "ai":
                wname, _, wprompt = rendered.partition(" ")
                if wname and wprompt:
                    run_ai_world_gen(client, wname, wprompt)

        act = spec.get("action", "chat").lower()
        if act == "multi":
            for step_line in spec.get("steps", []):
                s_act, _, s_val = step_line.partition(" ")
                run_step(s_act.lower(), s_val)
        else:
            run_step(act, spec.get("value", ""))
    elif cmd == "/delworld":
        if len(parts) < 2 or not parts[1].strip():
            client.log("ERR", "#ff4444", "Usage: /delworld <world_name>")
            return
        client.send_json({"type": "delete_world", "world": parts[1].strip()})
    elif cmd == "/inspect":
        objs = client.scene.get("objects", [])
        kinds = {}
        for o in objs:
            kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
        client.log("INSPECT", "#00ff66", f"World '{client.world}': {client.scene.get('raw_len', 0)} bytes, {len(objs)} objects {kinds}")
        if objs:
            cx = sum(o["pos"][0] for o in objs) / len(objs)
            cy = sum(o["pos"][1] for o in objs) / len(objs)
            cz = sum(o["pos"][2] for o in objs) / len(objs)
            client.log("INSPECT", "#00ffcc", f"Objects center: ({cx:.1f}, {cy:.1f}, {cz:.1f}) — type /tp to jump there")
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
            coords = _safe_floats(cleaned)
            if len(coords) >= 3:
                client.teleport_req = [coords[0], coords[1], coords[2]]
                client.log("TP", "#00ff66", f"Teleported to ({coords[0]}, {coords[1]}, {coords[2]})")
    elif cmd == "/worlds":
        client.send_json({"type": "list_worlds"})
    elif cmd == "/perms":
        client.send_json({"type": "admin_perm", "action": "list_perms"})
    elif cmd == "/admin" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "grant_admin", "target_key": parts[1]})
    elif cmd == "/unadmin" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "revoke_admin", "target_key": parts[1]})
    elif cmd == "/grant" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "grant_builder", "target_key": parts[1]})
    elif cmd == "/ungrant" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "revoke_builder", "target_key": parts[1]})
    elif cmd == "/ban" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "ban", "target_key": parts[1]})
    elif cmd == "/unban" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "unban", "target_key": parts[1]})
    elif cmd == "/defaultrole" and len(parts) > 1:
        client.send_json({"type": "admin_perm", "action": "set_default", "target_key": parts[1].lower()})
    elif cmd == "/nick" and len(parts) > 1:
        client.nick = parts[1][:24]
        client.log("SYSTEM", "#00ffcc", f"Nickname set to {client.nick} (reconnecting...).")
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
    elif cmd == "/upload":
        sub = cleaned.split(" ", 3)
        if len(sub) < 4:
            client.log("ERR", "#ff4444", "Usage: /upload <world|avatar> <name> <path.wrl>")
            return
        kind, name, fpath = sub[1], sub[2], sub[3]
        p = Path(fpath).expanduser()
        if not p.exists():
            client.log("ERR", "#ff4444", f"File not found: {p}")
            return
        client.send_json({"type": "upload", "kind": kind, "name": name, "content": p.read_text(encoding="utf-8")})
    elif cmd == "/aiurl":
        if len(parts) == 1:
            client.log("AI-URL", "#00ffcc", f"Current AI endpoint: {client.ai_url}")
        else:
            client.ai_url = parts[1]
            if len(parts) >= 3:
                client.ai_api_key = parts[2]
            client.log("AI-URL", "#00ff66", f"AI endpoint set to: {client.ai_url}")
    elif cmd == "/ai":
        if len(parts) < 3:
            client.log("ERR", "#ff4444", "Usage: /ai <world_name> <description of world>")
            return
        run_ai_world_gen(client, parts[1], parts[2])
    else:
        # Forward normal chat and server-side .x87 plugin commands (/portal, /pillar, etc.)
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

    def draw_border(x, y, w, h, rgba):
        glColor4f(*rgba)
        glBegin(GL_LINE_LOOP)
        glVertex2f(x, y); glVertex2f(x + w, y); glVertex2f(x + w, y + h); glVertex2f(x, y + h)
        glEnd()

    def draw_text(x, y, text, color=(0, 255, 204)):
        if not text:
            return
        surf = font.render(text, True, color)
        raw = pygame.image.tostring(surf, "RGBA", True)
        glRasterPos2f(x, y + surf.get_height())
        glDrawPixels(surf.get_width(), surf.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, raw)

    def wrap_line(text: str, max_px: int) -> list[str]:
        if font.size(text)[0] <= max_px:
            return [text]
        lines = []
        current = ""
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

    # Top Status Bar
    draw_rect(0, 0, WINDOW_W, 34, (0.04, 0.02, 0.10, 0.88))
    obj_count = len(client.scene.get("objects", []))
    status = (
        f"cyberX87 | {client.server_url} | World: {client.world} ({obj_count} objs) "
        f"| {client.nick} [{client.role}]"
    )
    while font.size(status)[0] > WINDOW_W - 24 and len(status) > 10:
        status = status[:-4] + "..."
    draw_text(12, 8, status, (0, 255, 204))

    # Portal Prompt
    if near_portal:
        draw_rect(WINDOW_W // 2 - 210, WINDOW_H // 2 + 40, 420, 36, (0.12, 0.02, 0.20, 0.9))
        draw_border(WINDOW_W // 2 - 210, WINDOW_H // 2 + 40, 420, 36, (1.0, 0.2, 0.8, 0.8))
        draw_text(WINDOW_W // 2 - 195, WINDOW_H // 2 + 48, f"Press [E] to enter portal: {near_portal}", (255, 100, 220))

    # Bottom-Left Chat Box
    box_w, box_h = 660, 210
    bx, by = 12, WINDOW_H - box_h - 12
    inner_max_w = box_w - 18
    draw_rect(bx, by, box_w, box_h, (0.04, 0.02, 0.10, 0.85))
    draw_border(bx, by, box_w, box_h, (0.0, 1.0, 0.8, 0.35))

    wrapped_entries = []
    for sender, hex_c, msg in client.chat_log[-12:]:
        hc = hex_c.lstrip("#")
        rgb = (int(hc[0:2], 16), int(hc[2:4], 16), int(hc[4:6], 16)) if len(hc) == 6 else (0, 255, 204)
        for line_str in wrap_line(f"[{sender}] {msg}", inner_max_w):
            wrapped_entries.append((line_str, rgb))

    for idx, (line_str, rgb) in enumerate(wrapped_entries[-8:]):
        draw_text(bx + 9, by + 6 + idx * 20, line_str, rgb)

    # Chat Input Line
    input_y = by + box_h - 28
    draw_rect(bx, input_y, box_w, 28, (0.08, 0.04, 0.18, 0.95))
    draw_border(bx, input_y, box_w, 28, (0.0, 1.0, 0.8, 0.45))
    if chat_mode:
        display_buf = chat_buffer
        while font.size(f"> {display_buf}_")[0] > inner_max_w and len(display_buf) > 1:
            display_buf = display_buf[1:]
        prompt_txt = f"> {display_buf}_"
    else:
        prompt_txt = "Press [T] or [Enter] to chat / run /commands (/worlds, /plugins, /ai)..."
    draw_text(bx + 9, input_y + 4, prompt_txt, (255, 255, 255) if chat_mode else (140, 180, 170))

    glEnable(GL_LIGHTING)
    glEnable(GL_DEPTH_TEST)
    glMatrixMode(GL_PROJECTION)
    glPopMatrix()
    glMatrixMode(GL_MODELVIEW)
    glPopMatrix()


# --- Main Desktop Application Loop ---
def main():
    server_arg = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVER

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
    glEnable(GL_NORMALIZE)
    glEnable(GL_COLOR_MATERIAL)
    glColorMaterial(GL_FRONT_AND_BACK, GL_AMBIENT_AND_DIFFUSE)
    glLightModelfv(GL_LIGHT_MODEL_AMBIENT, (0.45, 0.45, 0.55, 1.0))
    glLightfv(GL_LIGHT0, GL_POSITION, (0.4, 1.0, 0.5, 0.0))
    glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 1.0, 1.0))

    quadric = gluNewQuadric()

    cam_pos = [0.0, 2.2, 8.0]
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

        if client.teleport_req is not None:
            cam_pos = list(client.teleport_req)
            yaw = 0.0
            pitch = 0.0
            client.teleport_req = None

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

        if now - last_move_send > 0.1:
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

        if not client.scene.get("has_floor"):
            draw_checker_floor()
        for obj in list(client.scene.get("objects", [])):
            render_object(obj, quadric, now)

        for peer in list(client.peers.values()):
            render_peer(peer, quadric, now)

        draw_hud(client, font, chat_mode, chat_buffer, near_portal)
        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    main()
