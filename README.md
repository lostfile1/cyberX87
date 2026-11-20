# cyberX87

**cyberX87** is a self-hosted, standalone desktop **VRML 1.0 / VRML97 (`.wrl`)** multiplayer metaverse client and server written in Python. Built with an authentic 1990s retrofuturistic aesthetic, local-first cryptography, SHA-256 disk caching, and human-readable `.x87` command plugins, it lets you host, explore, rate, and collaboratively build 3D worlds without cloud lock-in.

---

## Key Features

* **Standalone 3D VRML Browser (`client.py`):** Renders VRML 1.0 and VRML97 scenes (`Box`/`Cube`, `Cylinder`, `Cone`, `Sphere`, `IndexedFaceSet`, `IndexedLineSet`, `ElevationGrid`, `Extrusion`, `DEF`/`USE`, `Viewpoint`, `Background`, `Fog`, and `AudioClip`) using Pygame and PyOpenGL.
* **Ed25519 Cryptographic Identity:** Generates a local Ed25519 keypair (`~/.cyberx87_key.hex`) and authenticates via challenge-nonce signatures—no passwords or central accounts required. The first user to connect to a fresh server is automatically granted `admin` rights.
* **In-World Building & World Templates:** Create worlds from 8 built-in templates (`flat`, `cybercity`, `forest`, `desert`, `crystal`, `fortress`, `island`, `space`), place primitives (`box`, `pillar`, `sphere`, `cone`, `arch`, `platform`, `wall`) with surface-normal snapping, and move, rotate, paint, scale, clone, or undo changes live.
* **Client-Side Pre-Upload `.wrl` Validation:** Validates `.wrl` worlds and custom 3D avatars locally (`/validate` and automatically on `/upload`) for UTF-8 headers, comment/quote-aware brace & bracket balance, geometry bounds, and size limits (`5 MB` worlds / `512 KB` avatars) before transmitting.
* **World Rating & Discovery System:** Rate worlds `1–5` stars with optional reviews (`/rate`), view rating distributions and feedback (`/ratings`), browse top-rated worlds (`/topworlds`), and inspect world ownership, spawn points, and player counts (`/info`).
* **Offline Mode & SHA-256 Disk Cache:** Automatically caches visited worlds and custom avatars locally (`~/.cyberx87_cache/`) with SHA-256 hash verification, allowing full offline exploration (`/offline`, `/openwrl`).
* **Multi-Node Server Federation:** Bookmark servers (`/bookmark`) and hop seamlessly between self-hosted cyberX87 nodes (`/hop`, `/back`) while carrying your nickname, color, and custom 3D avatar across servers.
* **Human-Readable `.x87` Plugins:** Extend both client (`./plugins/*.x87`) and server (`./data/plugins/*.x87`) with plain-text command macros.
* **90s Retro Extras:** Optional CRT scanline overlay (`/crt`), external audio-player streaming radio per world (`ffplay`, `mpv`, or `cvlc` via `/radio` & `/mute`), rotating session chat logs (`./logs/`), and instant PNG screenshots (`[F2]`/`[F12]`).

---

## Requirements

* **Python:** 3.10+
* **Python Packages:**
  * `pygame`
  * `PyOpenGL`
  * `websockets`
  * `PyNaCl`
* **Optional Audio Backends (for `/radio` streams):** `ffplay` (FFmpeg), `mpv`, or `cvlc` (VLC) on your system `PATH`.

---

## Quick Start

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/<your-username>/cyberX87.git
cd cyberX87
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install pygame PyOpenGL websockets pynacl
```

### 2. Start the Server

```bash
python server.py
```

By default, the server listens on `ws://0.0.0.0:8765` and initializes `./data/` (`worlds/`, `avatars/`, `backups/`, `plugins/`, `permissions.json`, `world_meta.json`, `friends.json`, and `federation.json`).

### 3. Launch the Client

Connect to your local server:

```bash
python client.py
```

Connect to a remote server:

```bash
python client.py ws://example.com:8765
```

Launch directly into offline cache mode:

```bash
python client.py --offline [world_name]
```

---

## Controls

| Key / Input | Action |
| :--- | :--- |
| `W` `A` `S` `D` / Arrow Keys | Move forward, strafe, and backpedal |
| `Mouse Move` | Look around (when mouse is captured) |
| `Space` | Jump (or ascend in `/fly` mode) |
| `Left Shift` | Descend in `/fly` mode |
| `Esc` | Toggle mouse capture / release cursor |
| `T` / `Enter` / `/` | Open chat / command prompt |
| `V` | Cycle camera mode (`1st-Person`, `3rd-Person Chase`, `3rd-Person Front`) |
| `Q` / `E` & `Mouse Wheel` | Orbit and zoom camera (in 3rd-person / front modes) |
| `R` | Reset camera orbit and zoom |
| `I` | Toggle crosshair raycast object inspector & surface placement |
| `1` `2` `3` `4` | Trigger emotes (`wave`, `dance`, `spin`, `jump`) |
| `F2` / `F12` | Save a PNG screenshot to `./screenshots/` |
| `H` | Toggle HUD visibility |

---

## Command Reference

### Navigation & Camera
* `/worlds` — List accessible worlds on the server (or cached worlds offline)
* `/world <name>` — Join a world
* `/spawn` — Teleport to the world's spawn point
* `/tp [x y z]` — Teleport to coordinates (or near the first scene object)
* `/fly [on|off]` — Toggle noclip flight mode
* `/cam [1st|3rd|front]` — Switch camera perspective
* `/camdist <1.5..15.0>` — Set third-person camera distance
* `/hud` — Hide or show the HUD
* `/crt` — Toggle 90s CRT scanlines
* `/screenshot [name]` — Capture a screenshot to `./screenshots/`

### World Creation, Building & Validation
* `/templates` — List available procedural VRML97 world templates
* `/createworld <name> [template] [description]` — Generate and upload a new world from a template
* `/validate <file.wrl> [world|avatar]` — Run pre-upload `.wrl` syntax, geometry, and bounds checks locally
* `/upload <world|avatar> <name> <file.wrl> [description]` — Upload a `.wrl` world or 3D avatar
* `/place <box|pillar|sphere|cone|arch|platform|wall> [color] [scale]` — Place an object in front of you (or snapped to the aimed surface when `[I]` scan mode is active)
* `/moveobj [id] <dx> <dy> <dz>` — Translate a placed object (aim with `[I]` or pass its ID)
* `/rotateobj [id] <degrees>` — Rotate a placed object around the Y axis
* `/paint [id] <color|#hex>` — Change a placed object's diffuse/emissive color
* `/scaleobj [id] <multiplier>` — Scale a placed object
* `/cloneobj [id] [dx dy dz]` — Duplicate a placed object
* `/delobj [id]` — Delete a placed object
* `/undo` — Revert the last world edit from automatic snapshots
* `/setspawn` / `/clearspawn` — Set or reset the world's custom `Viewpoint` spawn

### World Metadata, Ratings & Backups
* `/info [world]` — View world owner, co-builders, ratings, spawn point, file size, and active players
* `/rate <1..5> [comment]` or `/rate <world> <1..5> [comment]` — Rate a world (1–5 stars) with an optional review
* `/unrate [world]` — Remove your rating from a world
* `/ratings [world]` — View rating breakdown and recent player reviews
* `/topworlds` — Show the highest-rated accessible worlds
* `/desc [world] <description>` — Update a world's description
* `/worldtitle <title>` / `/worldtags <t1, t2, ...>` — Update world title or tags
* `/radio <http_stream_url> [Station Name]` / `/radio off` — Set or clear the world's streaming radio station
* `/mute` — Mute or unmute world radio playback
* `/backup [world] [label]` / `/backups [world]` / `/restore <file.wrl> [world]` — Create, list, and restore world snapshots
* `/export [world] [path]` — Export a world `.wrl` and `.meta.json` to `./exports/`

### Access Control, Social & Federation
* `/acl [world]` — View world privacy, co-builders, and visitor whitelist
* `/privateworld <on|off>` — Toggle private access on your world
* `/allowbuilder <user>` / `/removebuilder <user>` — Manage world co-builders
* `/allowvisitor <user>` / `/revokevisitor <user>` — Manage private world whitelist
* `/nick <name>` / `/color <#rrggbb>` — Update your display name or beacon color
* `/avatar <name|default>` / `/avatars` — Equip or list custom 3D `.wrl` avatars
* `/key` — Display your Ed25519 public key and 8-character short ID
* `/friends` / `/addfriend <user>` / `/accept <user>` / `/unfriend <user>` — Manage mutual friends
* `/msg <friend> <message>` / `/goto <friend>` — Whisper or teleport to an online friend
* `/servers` / `/bookmark [url] [name]` / `/unbookmark <name>` — Manage server bookmarks
* `/hop <bookmark|ws://host:port> [world]` / `/back` — Jump between cyberX87 servers
* `/peers` / `/linkserver <url> [label]` / `/unlinkserver <url|label>` / `/nodename <name>` — Manage server federation

### Offline Cache, Chat Logs & Plugins
* `/cache` / `/clearcache` — Inspect or clear `~/.cyberx87_cache/`
* `/offline [world]` / `/online` — Switch between local cache mode and live server mode
* `/openwrl <file.wrl>` — Preview a local `.wrl` file offline
* `/logs` / `/logtail [n]` / `/clearlogs` — Inspect or prune session chat logs in `./logs/`
* `/plugins` / `/reload` — List or reload `.x87` command plugins

---

## Writing `.x87` Command Plugins

Place `.x87` files in `./plugins/` (client) or `./data/plugins/` (server), separating multiple commands with `---`:

```ini
# ./plugins/custom.x87
COMMAND: /greet
DESC: Wave and greet everyone in the current world
ACTION: multi
STEPS:
  emote wave
  chat Hello from {nick} in #{world}!
```

---

## Project Structure

```text
cyberX87/
├── client.py        # Standalone Pygame + PyOpenGL VRML97 browser & WebSocket client
├── server.py        # Async WebSocket metaverse server, world store, ACLs & ratings
├── plugins/         # Client-side .x87 command plugins
├── data/            # Server runtime state (worlds, avatars, backups, metadata)
├── exports/         # Exported .wrl scenes and metadata
├── logs/            # Rotating session chat logs
└── screenshots/     # Captured PNG screenshots
```