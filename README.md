cyberX87
A lightweight, self-hosted VRML97 / VRML 1.0 multiplayer metaverse server and desktop OpenGL client built in Python with a 1990s retrofuturistic VRML browser HUD.
Features
* 1990s VRML Browser & Retro Terminal HUD: Styled after classic 1990s VRML plug-ins (Cosmo Player / Netscape Navigator / Win95 Motif) with chiseled 3D beveled frames, deep-blue-to-magenta gradient window title bars, a streamlined non-redundant HUD layout featuring a vrml:// location URL bar with live FPS counter, node count, user role, and [FLY]/[SCAN] status flags, a compact NAV-SENSOR.SYS XYZ/heading compass widget, a concise INSPECTOR [I] card, an IRC-style COMMS-TERMINAL with a C:\X87> prompt and automatic cursor-lock release while typing, and toggleable CRT scanlines (/crt).
* Command-Based World & Server Teleportation + Seamless Identity/Avatar Carry-Over: All world and server travel is driven directly by slash commands (/worlds, /world <name>, /tp [x y z], /peers, /hop <bookmark|url> [world], /back, /servers, /bookmark, /unbookmark) rather than physical world portals. Your Ed25519 signing key (~/.cyberx87_key.hex), persisted username & chat color (~/.cyberx87_profile.json), and locally cached custom .wrl avatar (~/.cyberx87_cache/avatars/) carry over automatically when hopping between servers.
* Cyberpunk Command Hub (NEO-GENESIS GRID // HUB): Features a dark ultraviolet-and-cyan cyberpunk sky and fog backdrop, a raised command nexus dais, a central neon-cyan obelisk spire with a glowing magenta halo ring, flanking cyan/magenta pylons, amber beacon monoliths, and tall perimeter megastructure towers.
* In-World Object Manipulation & Custom World Spawn Points: Press I to toggle crosshair raycast inspection with rotation-aware AABB wireframe highlighting. World owners and co-builders can place surface-snapped 3D objects (/place <box|pillar|sphere|cone|arch|platform|wall> [color] [scale]), nudge/rotate/repaint/scale/clone/delete any placed object (/moveobj, /rotateobj, /paint, /scaleobj, /cloneobj, /delobj), set or clear the world's custom spawn point and facing direction right where they stand (/setspawn, /clearspawn, /spawn), and undo edits (/undo).
* Display List Rendering & Auto-Recovering Radio: Compiles static world geometry into OpenGL display lists (SceneDisplayListCache), runs DNS SSRF checks asynchronously via asyncio.to_thread, and automatically recovers dropped internet radio streams.
* Passwordless Cryptographic Identity: Clients automatically generate a local Ed25519 signing key (~/.cyberx87_key.hex) and authenticate via single-use challenge-response signatures—no central database or password accounts. Enforces unique active nicknames and single-session key eviction to prevent impersonation.
* Comprehensive World Info (/info [world]): Displays everything about the current (or any accessible) world in one command—title, description, tags, custom/default spawn coordinates, visibility (PUBLIC/PRIVATE), owner nickname/short ID/Ed25519 public key, all co-builders, whitelisted visitors, active radio stream, file size, object count, snapshot count, and players currently inside.
* Local SHA-256 World Caching & Offline Exploration Mode: Automatically caches visited .wrl worlds in ~/.cyberx87_cache/ with SHA-256 integrity hashes (up to 128 MB LRU). Repeat visits skip re-downloading unchanged worlds via world_cached hash verification, and you can explore any cached world or local .wrl file completely offline using python3 client.py --offline [world], /offline [world], /openwrl <file.wrl>, /cache, /clearcache, and /online.
* World Access Control & Builder Co-Authors: World creators can toggle private invite-only visibility (/privateworld <on|off>), manage whitelisted visitors (/allowvisitor, /revokevisitor), and grant co-builder permissions (/allowbuilder, /removebuilder, /acl) so trusted friends can collaboratively build, backup, and restore shared worlds. Private worlds are automatically hidden from /worlds listings for non-members and /goto friend teleports.
* True 3D VRML Avatars & Floating Nametags: Upload custom .wrl avatar models (/upload avatar <name> <file.wrl>) or equip existing ones (/avatars, /avatar <name|default>). Enforces strict VRML-only validation on both client and server (verifying #VRML V2.0 utf8 / #VRML V1.0 ascii headers, balanced braces/brackets, valid VRML geometry nodes, and rejecting binary/glTF/OBJ/XML/JSON files), renders IndexedFaceSet meshes via triangulated polygon rendering, and automatically centers, grounds, scales, rotates, and caps models with chiseled floating nametags.
* Cross-Server Federation & Node Directory: Link peer cyberX87 nodes (data/federation.json) with /linkserver and /unlinkserver, query live peer node player counts and worlds via /peers, bookmark servers locally (/servers, /bookmark, /unbookmark), hop between nodes (/hop), and return to your previous server with /back.
* Universal VRML Parser & Renderer: Parses and renders VRML 2.0/97 (Box, Sphere, Cylinder, Cone, IndexedFaceSet, IndexedLineSet, ElevationGrid, Extrusion, Transform with composed rotations and rotated child offsets, DEF/USE, Viewpoint, Background, Fog) as well as legacy VRML 1.0 (Separator, Cube) scenes.
* AABB Collision, Surface Grounding & Flight Toggle: Walk on floors, platforms, stairs, and ElevationGrid terrain with gravity, jumping (Space), and smooth wall sliding, or toggle free-flight mode anytime with /fly (Space = ascend, Left Shift = descend).
* Server World Quotas: Server admins can cap total custom worlds (/maxworlds <count>) and worlds per user (/userworldlimit <count>), persisted in data/permissions.json.
* AI World Generation (/ai): Connects to any OpenAI-compatible local (llama-server, Ollama) or remote endpoint (/aiurl, /aimodel), sanitizes and repairs VRML97 syntax, features multi-theme procedural 3D world synthesis (fortress/castle, island/ocean, forest, pyramid/desert, crystal/ice, space, and seeded layouts) alongside fallback diagnostics in COMMS, and works seamlessly offline to synthesize and explore local AI worlds.
* Creator-Locked World Metadata & Internet Radio: Only the original uploader/creator of a world can edit its description (/desc [world] <text>), display title (/worldtitle), tags (/worldtags), or embedded internet radio stream (/radio <url> [station_name], /radio off), synced directly into VRML97 WorldInfo and AudioClip nodes. Visitors can locally mute/unmute streams with /mute.
* World Backup, Snapshots & Export: Creators and co-builders can create server-side snapshots (/backup, /backups), restore previous versions (/restore), and export clean .wrl + .meta.json files locally (/export). Overwriting or editing an owned world automatically saves a timestamped backup in data/backups/<world>/.
* Mutual Friend System: Add friends by nickname, short ID, or Ed25519 public key (/addfriend, /accept, /unfriend), view live online status and worlds (/friends), send private whispers (/msg), and teleport directly to online friends (/goto).
* Human-Readable .x87 Plugin System: Drop plain-text .x87 files into ./plugins/ (client macros) or ./data/plugins/ (server VRML spawners and broadcasts) and hot-reload with /reload.
* Security Hardening: Includes single-use nonce replay protection, handshake timeouts, per-IP connection limits, sliding-window rate limits, strict VRML file format validation (enforcing valid #VRML headers and structural sanity while rejecting non-VRML/binary formats), VRML Script node stripping, async SSRF IP filtering on stream URLs, path-traversal guards on backups/exports, and atomic 0600/0700 file persistence.
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
	/worlds, /world <name>
	List accessible worlds or jump directly to a world by command
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
	/info [world]
	Show full world info: owner, co-builders, spawn point, description, tags, radio, ACL, players, and stats
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
	/upload <world|avatar> <name> <file.wrl> [desc]
	Upload a VRML world or custom 3D avatar
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
	/ai <world_name> <prompt>
	Generate a 3D VRML97 world via AI (uploads online or opens offline)
	/aiurl <url> [key], /aimodel <id>
	Configure OpenAI-compatible AI endpoint and model
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