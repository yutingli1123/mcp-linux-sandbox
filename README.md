# mcp-linux-sandbox

A disposable Fedora Linux sandbox exposed as an MCP server, so a chat client
(FlowDown, Claude Desktop, anything speaking Streamable HTTP) can run real shell
commands — `dnf install`, `curl`, `ffmpeg`, Python — and hand files back to you.

Each conversation gets its own container. Files under `/workspace` survive
container recycling; installed packages and running processes do not.

```
MCP client ──Bearer──▶ nginx (TLS)
                         ├── /mcp/<random>/  ──▶ 127.0.0.1:8000  FastMCP
                         └── /dl/<signed>    ──▶ (same)  file links
                                                     │
                                                     ▼
                                    podman exec  mcpsb-<label>
                                                     │
                                        ┌────────────┴────────────┐
                                        ▼                         ▼
                              container writable layer     volume /workspace
                              (packages, /tmp — dies)      (files — survives)
```

## Files

| Path | What it is |
|---|---|
| `server.py` | The MCP server: `run_command`, `present_file`, `sandbox_info`, plus the `/dl/` route |
| `Dockerfile` | Base image for sandbox containers (Fedora + common CLI tools, compilers, Rust, Node) |
| `requirements.txt` | Pinned Python dependencies |
| `env.sh` | Creates `mcp-sandbox.env` and generates its signing key |
| `install.sh` | Idempotent install/update: venv, image, systemd units |
| `selftest.py` | End-to-end checks that speak MCP over HTTP |
| `scripts/cleanup.sh` | Stops leftover containers on service stop |
| `scripts/gc-volumes.sh` | Deletes volumes untouched for `MAX_AGE_DAYS` |
| `systemd/` | `mcp-sandbox.service`, plus the volume-GC service and timer |
| `nginx/mcp-sandbox.conf` | The two `location` blocks to paste into your server |
| `mcp-sandbox.env.example` | Copy to `mcp-sandbox.env`; holds the signing key |

## Install

```bash
sudo dnf install -y podman catatonit        # catatonit is needed by podman --init
./env.sh                                    # creates mcp-sandbox.env, generates the signing key
vim mcp-sandbox.env                         # set MCP_PUBLIC_BASE
./install.sh                                # refuses to run while that is still the placeholder
```

Then wire up nginx — copy both blocks from `nginx/mcp-sandbox.conf` into your
existing TLS `server {}`, replacing the path and token placeholders:

* generate the bearer token with `openssl rand -hex 32`
* on Fedora, `sudo setsebool -P httpd_can_network_connect 1`, or nginx gets 502
* `sudo nginx -t && sudo systemctl reload nginx`

## Client configuration

| Field | Value |
|---|---|
| Endpoint | `https://your.host:8443/mcp/<random-path>/` |
| Headers | `{"Authorization": "Bearer <token>"}` |
| Tool confirmation | **leave it ON** — this is a remote code-execution endpoint |

Add this to the client's system prompt, or the per-conversation isolation does
not happen:

> When you call `run_command`, pass a `sandbox` label. At the start of a new
> conversation pick a NEW short label (for example `amber-otter`) and use that
> same label for every call in the conversation. Never reuse a label from an
> earlier conversation, and do not change it mid-conversation.

Verify with, from the install directory (`install.sh` copies `selftest.py` there):

```bash
cd ~/mcp-linux-sandbox                      # or wherever SANDBOX_HOME points
./venv/bin/python selftest.py --url https://your.host:8443/mcp/<path>/ --token <token>
./venv/bin/python selftest.py --url http://127.0.0.1:8000/mcp    # bypasses nginx
```

## Sandbox labels

Tools take a `sandbox` label, and label → container → volume is one to one: a
label with no container yet gets a fresh, empty one. Labels are normalised, so
`Amber Otter` and `amber-otter` reach the same sandbox.

The label has to come from the model because the client sends one MCP session id
for the whole app rather than one per conversation, and no per-conversation field
exists in the request. It is a convention rather than a guarantee — a model that
reuses a label reaches the old container — but every result echoes
`[sandbox <key>] [new]`, so a collision is visible rather than silent.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `SANDBOX_IDLE` | 300 s | Recycle idle containers. Cleanup only: lowering it loses `dnf` packages sooner, nothing else. |
| `SANDBOX_MAX` | 2 | Concurrent containers. Beyond it the least-recently-used **idle** container is evicted (`rm -f`; the volume stays, so files survive); when every sandbox is mid-command the call is refused instead of killing one. On a 4-core / 3.66 GiB host, `2 × 1 GiB` leaves ~1.6 GiB for nginx, the server and the system. |
| `SANDBOX_MAX_TIMEOUT` | 900 s | Ceiling on the model's `timeout_seconds`. |
| `SANDBOX_MIN_FREE` | 2 GiB | Refuse to start a sandbox when the volume filesystem is nearly full. Checked at container creation only — it will not stop a single large write. |
| `SANDBOX_LINK_TTL` | 600 s | Download links are HMAC-signed with an expiry, so a leaked URL is bounded in time and scope. Long enough to click a link right after it is produced. |
| `SANDBOX_IMAGE_MAX` | 1.5 MiB | Largest image that gets attached rather than linked. An attachment is also sent to the model (~2 MB ≈ 20k tokens). |
| `--memory=1g --cpus=2 --pids-limit=256` | | Verified to land in the cgroup (`memory.max`, `cpu.max`, `pids.max`). |
| `--init` | | Without an init as PID 1, exited children pile up as zombies against `--pids-limit` until `exec` starts failing. |
| `bash -lc` | | Login shell, so `/etc/profile.d` is sourced — that is what puts `cargo` on PATH. |
| `--security-opt=no-new-privileges` | | Keeps setuid binaries from being exploitable. |

Volumes are never deleted by the server. `gc-volumes.sh` deletes them on a daily
timer once nothing in them has been touched for `MAX_AGE_DAYS` (7 days);
`DRY_RUN=1` prints what it would delete instead.

## Security

This endpoint executes arbitrary commands. Treat it accordingly.

* Keep the client's tool-confirmation prompt **enabled**. It is the only human
  gate on what actually runs.
* Anything that can reach `/mcp/` can run code; anything that can reach `/dl/`
  can read the sandbox volume. Keep the bearer token and `MCP_SIGN_KEY` out of
  URLs, screenshots, `.fdmcp`-style exports, cloud drives and git.
* `present_file` serves regular files under `/workspace` and follows no symlink
  in any path component, so a link created inside the sandbox cannot reach host
  files.
* Containers are **rootless** (`podman`, no `sudo`) and can reach the LAN by
  default. If that matters, drop the network (`--network=none`) or move the
  container onto an `--internal` network behind a proxy — but installing packages
  needs network, so pick one.
* Isolation is namespace + seccomp + cgroups, not a VM. It is the right level for
  "stop the model from trashing my host"; it is not gVisor/Kata.

## Limitations

* The sandbox boundary is the model's own label discipline, as described above.
* Concurrent conversations share the container budget. With `SANDBOX_MAX=2`, a
  third conversation evicts the least-recently-used idle container — packages
  go, files stay. If both are mid-command it is refused and has to retry.
* Restarting the service invalidates the client's MCP session; the client has to
  re-verify.
* Image attachments depend on the client. FlowDown attaches MCP `image` content;
  other clients may not.
* `gc-volumes.sh`'s age heuristic (newest mtime in the volume) has not been
  validated on btrfs.

## Verified behaviour

Checked end-to-end against a live deployment (Raspberry Pi, Fedora 44 aarch64,
rootless podman, cgroup v2, behind nginx):

| Check | Result |
|---|---|
| Fedora userland, tools present | ✅ Fedora 44; `curl jq git rg fd ps less python3 gcc cargo node` |
| Network + `dnf install` | ✅ `sqlite` installed and usable |
| Same label reuses container | ✅ same `hostname`, no `[new]` |
| New label, fresh container | ✅ different `hostname`, empty `/workspace` |
| Files survive recycling | ✅ marker file kept its old hostname after rebuild |
| Writable layer is discarded | ✅ `/tmp` contents gone after rebuild |
| Idle reaper | ✅ containers recycled after the idle window |
| LRU eviction at `MAX_SANDBOXES` | ✅ count stayed `2/2`, oldest evicted, its files kept |
| Exit codes | ✅ `false`→1, `exit 42`→42, unknown command→127 |
| Container-side timeout | ✅ rc=124, process actually killed |
| `--init` as PID 1 | ✅ `podman-init`, 0 zombies |
| cgroup limits applied | ✅ `memory.max=1G`, `cpu.max=2 cores`, `pids.max=256` |
| `present_file` image | ✅ rendered as an attachment |
| `present_file` text file | ✅ signed URL, never inlined |
| Path traversal | ✅ rejected |
| Symlink escape | ✅ rejected |
| Oversized image | ✅ falls back to a link |
| Link expiry and signature tampering | ✅ expired and tampered URLs rejected |
| Credential redaction in `sandbox_info` | ✅ `<redacted>` |

Not yet exercised: the over-age branch of `gc-volumes.sh`.
