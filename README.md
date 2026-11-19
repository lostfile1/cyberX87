# cyberX87 // Self-Hosted VRML97 Metaverse

A self-hostable VRML97 metaverse featuring a Python/WebSocket backend (`server.py`), a standalone Python/OpenGL desktop client (`client.py`).

## Features

- **Key-Based Auth:** Ed25519 asymmetric cryptography generated locally (`~/.cyberx87_key.hex` on desktop or `localStorage` in browser); signs a server challenge with no passwords required.  
- **Permissions & Uploads:** `admin`, `builder`, and `guest` roles mapped to public keys. Upload `.wrl` worlds and avatars over WebSockets.  
- **AI Agent World Generation:** Connects to any OpenAI-compatible LLM endpoint (including local `llama-server` / `llama.cpp` on port `8080`) to generate and deploy VRML97 worlds from natural language prompts.  
- **Portals & Federation:** Embed `Anchor` nodes with `url ["vrml://host:port/world_name"]` (or `vrml://LOCAL/world_name`) to jump between local worlds or federated self-hosted servers.  
- **Multiplayer Chat & Emotes:** Real-time spatial position sync, colored chat tags, and `/wave`, `/dance`, `/spin`, `/jump` avatar emotes.

## Quick Start

### 1\. Server Setup (`server.py`)

Install dependencies and start the server:

```sh
pip install websockets pynacl
```

1. **Start the Server:**

```sh
python3 server.py
```

   *(The first client key to connect is automatically granted `admin` permissions in `data/permissions.json`.)*

   

### 2\. Standalone Python Desktop Client (`client.py`)

Install desktop dependencies (on OpenBSD/FreeBSD, `py3-pygame`, `py3-opengl`, `py3-pynacl`, and `py3-websockets` are also available via `pkg_add` / `pkg`):

```sh
python3 -m http.server 8000
```

```sh
pip install pygame PyOpenGL websockets pynacl
python3 client.py
```

To connect directly to a remote server on launch:

```sh
python3 client.py ws://friend-server.example.org:
```

## Desktop Client Controls & Slash Commands

- **Movement:** `W A S D` or Arrow keys to move, `Space` / `Shift` to adjust height, Mouse to look (`ESC` toggles cursor grab).  
- **Portals:** Walk up to a portal (`vrml://LOCAL/plaza` or `vrml://host:port/world`) and press `E` to traverse.  
- **Emotes:** Press `1` (`/wave`), `2` (`/dance`), `3` (`/spin`), or `4` (`/jump`).  
- **Chat & Commands (`T` or `Enter`):**  
- `/nick <name>` — Change display name  
- `/color <#hex>` — Change avatar and chat color  
- `/world <name>` — Switch to another world on the current server  
- `/connect <ws://host:port>` — Connect to another self-hosted server  
- `/upload <world|avatar> <name> <file.wrl>` — Upload a `.wrl` file from disk  
- `/ai <world_name> <prompt>` — Generate and deploy a VRML97 world via your LLM endpoint  
- `/key` — Display your persistent Ed25519 public key  
- `/grant <pubkey>` or `/ban <pubkey>` — Admin permission controls
