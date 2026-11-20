cyberX87
A lightweight, self-hosted VRML97 / VRML 1.0 multiplayer metaverse server and desktop OpenGL client built in Python with a 1990s retrofuturistic VRML browser HUD.
Features
* 1990s VRML Browser & Retro Terminal HUD: Chiseled 3D beveled frames, deep-blue-to-magenta title bars, live FPS and node counters, vrml:// URL bar, status flags, XYZ compass widget, object inspector card, IRC-style terminal with cursor release, and toggleable CRT scanlines (/crt).
* Command-Based Navigation & Cryptographic Identity: Travel via slash commands (/worlds, /world, /tp, /hop, /back). Ed25519 signing keys (~/.cyberx87_key.hex), usernames, chat colors, and avatars persist across servers without centralized accounts or passwords.
* Cyberpunk Command Hub: Default central hub featuring ultraviolet/cyan skyboxes, a command dais, neon-cyan obelisk spire with magenta halo ring, flanking pylons, and perimeter megastructure towers.
* In-World Object Manipulation & Custom Spawns: Raycast inspection (I), surface-snapped primitive placement (/place), moving/rotating/repainting/scaling/cloning/deleting objects, custom spawn point setting (/setspawn), and full edit undo (/undo).
* Display List Rendering & Auto-Recovering Radio: Compiles geometry into OpenGL display lists (SceneDisplayListCache), performs async SSRF DNS checks, and automatically restores dropped internet radio streams.
* Template-Based World Creation (/createworld): Instantly generate customized VRML97 scenes using 8 built-in templates: flat (Neon Grid), cybercity (Cyberpunk Megacity), forest (Emerald Pine Forest), desert (Solar Dunes & Pyramids), crystal (Arctic Cavern), fortress (Obsidian Citadel), island (Tropical Atoll), and space (Orbital Station Deck).
* Comprehensive World Info (/info [world]): Inspect titles, descriptions, tags, spawn points, visibility settings, owner/co-builder identity, radio streams, object counts, and active players in one view.
* SHA-256 World Caching & Offline Mode: Automatically caches visited scenes in ~/.cyberx87_cache/ with SHA-256 integrity verification. Explore cached worlds or local .wrl files offline via --offline, /offline, or /openwrl.
* Access Control & Collaborative Building: Toggle private visibility (/privateworld), manage allowed visitor lists, and grant co-builder edit permissions (/allowbuilder). Private worlds are hidden from public listings and friend teleports.
* True 3D VRML Avatars & Nametags: Upload and equip custom VRML avatars with strict header/syntax validation, triangulated polygon rendering, automatic grounding/scaling, and floating 3D nametags.
* Cross-Server Federation: Link peer nodes (data/federation.json), query live node directories (/peers), bookmark servers, and seamlessly hop across metaverse nodes.
* Universal VRML Parser & Renderer: Full parsing support for VRML 2.0/97 (primitives, IndexedFaceSet, ElevationGrid, Extrusion, nested transforms, DEF/USE, environment nodes) and legacy VRML 1.0.
* AABB Collision, Grounding & Flight Toggle: Real-time physics for floors, stairs, and terrain with gravity, jumping, wall sliding, and toggleable free-flight mode (/fly).
* Server Quotas & Permissions: Configurable limits on total worlds and user world quotas, managed via administrative permissions.
* Creator Metadata & Internet Radio Controls: Creators manage world titles, descriptions, tags, and radio streams, integrated directly into VRML WorldInfo and AudioClip nodes.
* Backup, Snapshots & Export: Create server-side snapshots (/backup), restore previous world builds (/restore), and export clean .wrl files locally (/export).
* Mutual Friend System: Manage friend requests, view online presence, send private whispers (/msg), and teleport to online friends (/goto).
* Plugin System (.x87): Extend client functionality and server spawners using plain-text macro scripts with live hot-reloading (/reload).
* Security Hardening: Replay protection nonces, rate limiting, script node stripping, async SSRF IP filtering, path traversal protection, and atomic file persistence.
Installation
pip install pygame PyOpenGL websockets pynacl

(Optional for /radio internet stream playback: install ffplay via ffmpeg, mpv, or vlc.)
Running
1. Start the server (first user to connect is automatically granted admin privileges):


python3 server.py

2. Start the desktop client (online):


python3 client.py ws://localhost:8765

3. Start the desktop client directly in offline exploration mode:
4. Launch standalone world editor tools:


python3 world_editor.py
python3 map_editor.py



python3 client.py --offline [world_name]
Controls & Commands
Key / Command
	Action
	W A S D / Arrow Keys
	Walk (or fly when /fly is active)
	Space / Left Shift
	Jump (walking) or Ascend / Descend (flying)
	Mouse / ESC
	Look around / toggle mouse cursor lock
	I
	Toggle interactive crosshair object inspection & AABB wireframe highlight
	T / Enter / /
	Open chat & command bar
	1 – 4
	Trigger avatar emotes (wave, dance, spin, jump)
	/worlds, /world <name>, /templates, /createworld <name> [template] [desc]
	List accessible worlds, jump directly to a world, view templates, or create new worlds
	/hop <name|url> [world], /back
	Jump to a bookmarked or remote server (and optional world) and return with /back
	/servers, /bookmark [url] [name], /unbookmark <name>
	View or manage saved server bookmarks and recent nodes
	/peers
	Inspect this node's federation directory and live peer node counts
	/linkserver <url> [label] [world], /unlinkserver <url|label>
	Admin: link/unlink federated peer servers in data/federation.json
	/nodename <title>
	Admin: set this server's public federation node name
	/setspawn, /clearspawn, /spawn
	Set/clear the world's spawn point & facing direction where you stand, or return to spawn
	/place <box|pillar|sphere|cone|arch|platform|wall> [color] [scale]
	Place a surface-snapped 3D object at the crosshair target (or 4.5m ahead)
	/moveobj [tag_id] <dx> <dy> <dz>
	Move a placed object by (dx, dy, dz) (aim in [I] mode to omit tag_id)
	/rotateobj [tag_id] <degrees>
	Rotate a placed object's yaw angle in degrees
	/paint [tag_id] <color|#hex>
	Repaint a placed object using a named color or #rrggbb hex
	/scaleobj [tag_id] <multiplier>
	Scale a placed object by a multiplier (0.2–5.0)
	/cloneobj [tag_id] [dx dy dz]
	Duplicate a placed object (defaults to +1.5m on X if offset omitted)
	/delobj [tag_id]
	Remove a specific placed object by tag ID or while aiming at it in [I] mode
	/undo
	Undo the most recent edit or spawn change via automatic snapshot
	/crt
	Toggle 1990s CRT horizontal scanline overlay
	

	

	/cache, /clearcache
	List locally cached worlds (with SHA-256 hashes) or clear ~/.cyberx87_cache/
	/offline [world], /online
	Switch to offline exploration mode or reconnect online
	/openwrl <path/to/scene.wrl>
	Load any local .wrl file directly into the 3D client offline
	/acl, /privateworld <on|off>
	View world ACLs or toggle private invite-only mode
	/allowbuilder <user>, /removebuilder <user>
	Grant or revoke co-builder edit permissions on your world
	/allowvisitor <user>, /revokevisitor <user>
	Whitelist or remove allowed visitors on a private world
	/avatars, /avatar <name|default>
	List uploaded 3D avatars or equip a custom avatar
	

	

	/fly [on|off]
	Toggle free-flight mode vs. grounded gravity & AABB collision
	/tp [x y z]
	Teleport to coordinates or jump near scene objects
	/backup [world] [label], /backups [world]
	Create or list server-side snapshots for a world you own/co-build
	/restore <snapshot.wrl> [world]
	Restore a saved snapshot back into your live world
	/export [world] [output_path.wrl]
	Export your world's .wrl and .meta.json to ./exports/
	/upload <world|avatar> <name> <file.wrl> [desc], /info [world]
	Upload custom 3D content or display detailed information about a world
	

	

	/desc [world] <text>, /worldtitle <title>, /worldtags <t1,t2>
	Edit metadata for a world you created
	/radio <http_url> [name], /radio off, /mute
	Set your world's internet radio stream or toggle local mute
	/friends, /addfriend <user>, /accept <user>, /unfriend <user>
	Manage your mutual friends list
	/msg <friend> <text>, /goto <friend>
	Whisper an online friend or teleport to their location
	/plugins, /reload
	List or reload client .x87 plugins
	/maxworlds <n>, /userworldlimit <n>
	Admin: cap total server worlds or worlds per user (0 = unlimited)
	/perms, /admin, /grant, /ban, /delworld <name>
	Server admin role and world moderation commands
	/nick <name>, /color <#hex>, /key, /quit
	Customize identity, view Ed25519 public key, or exit client