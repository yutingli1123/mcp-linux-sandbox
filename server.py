"""MCP server: a disposable Linux sandbox, one per conversation.

FlowDown (or any MCP client speaking Streamable HTTP) connects over HTTP; each
tool call runs `podman exec` inside a Fedora container identified by a label the
model picks. See README.md for the reasoning and the known gotchas.
"""

import asyncio
import collections
import hashlib
import hmac
import mimetypes
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------- configuration
PODMAN = os.environ.get("SANDBOX_PODMAN", "/usr/bin/podman")
BASE_IMAGE = os.environ.get("SANDBOX_IMAGE", "localhost/sandbox-base:fedora44")

IDLE = int(os.environ.get("SANDBOX_IDLE", 5 * 60))          # recycle idle containers
MAX_SANDBOXES = int(os.environ.get("SANDBOX_MAX", 2))       # concurrent containers
MIN_FREE = int(os.environ.get("SANDBOX_MIN_FREE", 2 * 1024**3))  # refuse below this
MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT", 900))    # cap on timeout_seconds

VOLROOT = Path(os.environ.get(
    "SANDBOX_VOLROOT",
    Path.home() / ".local/share/containers/storage/volumes"))

SIGN_KEY = os.environ.get("MCP_SIGN_KEY", "").encode()
PUBLIC_BASE = os.environ.get("MCP_PUBLIC_BASE", "").rstrip("/")
LINK_TTL = int(os.environ.get("SANDBOX_LINK_TTL", 600))

IMAGE_MAX = int(os.environ.get("SANDBOX_IMAGE_MAX", 1536 * 1024))
IMAGE_FORMATS = {"png", "jpeg", "jpg", "gif", "webp", "bmp"}

HOST = os.environ.get("SANDBOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("SANDBOX_PORT", 8000))

mcp = FastMCP("linux-sandbox")

_lock = threading.Lock()
_last: dict = {}
_active = collections.Counter()   # in-flight calls per key; the reaper leaves those alone


# ------------------------------------------------------------------- helpers
def _p(*args, timeout=300):
    return subprocess.run([PODMAN, *args], capture_output=True,
                          text=True, timeout=timeout)


def _key(label):
    """Map a label the model picked to a container key.

    The label is normalised before hashing, not only before the slug: "Amber
    Otter" and "amber-otter" have to land on the same key, or a model that
    varies its spelling silently gets a second, empty sandbox.
    """
    norm = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
    slug = norm[:24] or "sb"
    return f"{slug}-{hashlib.sha256(norm.encode()).hexdigest()[:6]}"


def _names(key):
    return f"mcpsb-{key}", f"mcpsb-vol-{key}"


def _existing_keys():
    out = _p("ps", "-a", "--filter", "name=mcpsb-",
             "--format", "{{.Names}}").stdout.split()
    return [n[len("mcpsb-"):] for n in out if n.startswith("mcpsb-")]


def _free_bytes():
    """Free space on the filesystem that holds the volumes and writable layers."""
    p = VOLROOT
    while not p.exists() and p != p.parent:
        p = p.parent
    st = os.statvfs(p)
    return st.f_bavail * st.f_frsize


def _workspace(key):
    return VOLROOT / f"mcpsb-vol-{key}" / "_data"


def _resolve(key, rel):
    """Map a /workspace-relative path to a file on the host, or None.

    Both checks matter: the normalisation stops "../.." escapes, and the parents
    check stops symlinks inside the volume from pointing at host files. Do not
    remove either one.
    """
    base = _workspace(key)
    try:
        root = base.resolve()
        target = (base / str(rel).lstrip("/")).resolve()
    except (OSError, ValueError):   # ValueError: an embedded NUL in the path
        return None
    if target != root and root not in target.parents:
        return None
    return target if target.is_file() else None


def _open_beneath(key, rel):
    """Open a regular file under the volume, refusing a symlink in every component.

    _resolve() checks containment first, but it checks at a moment: the volume
    is writable by the sandbox, so any component of the path it approved can be
    swapped for a symlink before the file is opened. O_NOFOLLOW alone only
    guards the last component, so the walk starts at the volume root and
    descends with no symlink followed at any level. Read from the fd this
    returns, not from a path.
    """
    root = _workspace(key)
    parts = [p for p in str(rel).lstrip("/").split("/") if p not in ("", ".")]
    if not parts:
        raise OSError(f"empty path: {rel!r}")
    if any(p == ".." for p in parts):
        raise OSError(f"path escape: {rel!r}")

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nfd
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    finally:
        os.close(fd)

    if not stat.S_ISREG(os.fstat(leaf).st_mode):
        os.close(leaf)
        raise OSError(f"not a regular file: {rel}")
    return leaf


def _mime(path):
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _sign(exp, key, rel):
    return hmac.new(SIGN_KEY, f"{exp}\n{key}\n{rel}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def _link(key, f):
    if not (PUBLIC_BASE and SIGN_KEY):
        return ""
    # Both sides resolved: f comes back canonical from _resolve(), and a
    # symlinked component in VOLROOT would otherwise make the two disagree.
    rel = str(f.relative_to(_workspace(key).resolve()))
    exp = int(time.time()) + LINK_TTL
    return f"{PUBLIC_BASE}/dl/{exp}/{_sign(exp, key, rel)}/{key}/{rel}"


def _create(key):
    name, vol = _names(key)
    _p("volume", "create", vol)
    return _p("run", "-d", "--name", name,
              "--init",
              "--security-opt=no-new-privileges",
              "--pids-limit=256",
              "--memory=1g", "--cpus=2",
              "-v", f"{vol}:/workspace", "-w", "/workspace",
              BASE_IMAGE, "sleep", "infinity")


def _reap():
    while True:
        time.sleep(60)
        try:
            now = time.time()
            for key in list(_last):
                if now - _last.get(key, now) < IDLE:
                    continue
                with _lock:
                    # Re-check under the lock: a call can start while we iterate,
                    # and a call that is still running must not be reaped.
                    if _active.get(key, 0) > 0 or now - _last.get(key, now) < IDLE:
                        continue
                    name, _ = _names(key)
                    if _p("container", "exists", name).returncode == 0:
                        _p("rm", "-f", name)
                    _last.pop(key, None)
        except Exception as e:
            # This thread is the only thing recycling containers, so a podman
            # call that times out must not end the loop.
            print(f"reaper: {e!r}", file=sys.stderr, flush=True)


def _seed():
    now = time.time()
    for key in _existing_keys():
        _last.setdefault(key, now)


# ---------------------------------------------------------------------- tools
@mcp.tool
def run_command(sandbox: str, command: str, timeout_seconds: int = 120) -> str:
    """Run a bash command inside a sandboxed Fedora Linux container.

    `sandbox` is the name of the sandbox to use. Each conversation gets its own
    sandbox: when a new conversation starts, pick a NEW short label (for example
    "amber-otter") and use that same label for every call in that conversation.
    Reusing a label always reaches the same container, so never reuse a label
    from an earlier conversation.

    A label that has no container yet gets a fresh, empty one; do not create a
    sandbox in advance. Files under /workspace persist while that label is in
    use. Packages installed with dnf and running processes live in the container
    and are lost when it is recycled after being idle.

    Put anything the user should keep or look at under /workspace, then use
    present_file to show it. Write commands in bash syntax. Commands are killed
    after `timeout_seconds`; the server clamps large values. Output is truncated
    past 20k characters and the exit code is prefixed to the result.
    """
    timeout_seconds = max(1, min(int(timeout_seconds), MAX_TIMEOUT))
    key = _key(sandbox)
    name, _ = _names(key)
    created = False

    with _lock:
        if _p("container", "exists", name).returncode != 0:
            if _free_bytes() < MIN_FREE:
                return "[sandbox error] host disk almost full, refusing to start a sandbox"
            keys = _existing_keys()
            if len(keys) >= MAX_SANDBOXES:
                # Only idle containers are candidates: _last is stamped when a
                # call starts, so a sandbox running a long command looks like
                # the oldest one and would be killed mid-command.
                idle = [k for k in keys if not _active.get(k, 0)]
                if not idle:
                    return (f"[sandbox error] all {len(keys)} sandboxes are in "
                            f"use; try again when one is free")
                victim = min(idle, key=lambda k: _last.get(k, 0.0))
                _p("rm", "-f", _names(victim)[0])
                _last.pop(victim, None)
            r = _create(key)
            if r.returncode != 0:
                return f"[sandbox error] could not start container: {r.stderr.strip()}"
            created = True
        # Counted, not flagged: two calls can share a key, and the first one to
        # finish must not unmask the other, which would let the reaper kill a
        # command that is still running.
        _active[key] += 1
        _last[key] = time.time()

    try:
        r = subprocess.run(
            [PODMAN, "exec", "-i", name, "timeout", "-k", "5",
             str(timeout_seconds), "bash", "-lc", command],
            capture_output=True, text=True, timeout=timeout_seconds + 20)
    except subprocess.TimeoutExpired:
        return f"[timed out after {timeout_seconds}s; the process was killed]"
    finally:
        with _lock:
            _active[key] -= 1
            if _active[key] <= 0:
                del _active[key]
            _last[key] = time.time()

    out = (r.stdout + r.stderr).strip()
    if len(out) > 20000:
        out = out[:10000] + "\n...[truncated]...\n" + out[-5000:]
    head = f"[sandbox {key}]{' [new]' if created else ''} [exit {r.returncode}]"
    return f"{head}\n{out}" if out else head


@mcp.tool
def present_file(sandbox: str, path: str, caption: str = ""):
    """Hand a file from the sandbox's /workspace to the user.

    This delivers the file, it does not read it back to you: images are attached
    so you and the user can both see them, and every other file comes back as a
    link the user opens in a browser. To read a file's contents yourself use
    run_command (`cat`, `head`, ...) — do not call this for that.

    Use it when the user should actually get the file: plots, screenshots,
    diagrams, rendered pages, scripts, reports you just produced.

    `path` is relative to /workspace (e.g. "out/chart.png"). `caption` is an
    optional single line shown above the file. Only files under /workspace can
    be presented, because that is the directory that persists.
    """
    key = _key(sandbox)
    f = _resolve(key, path)
    if f is None:
        return (f"[present error] no such file under /workspace: {path}\n"
                f"run `ls -la /workspace` in the sandbox to see what is there")

    size = f.stat().st_size
    ext = f.suffix.lower()
    info = f"{caption or f.name}\n{path}  ({size} B)"

    if ext.lstrip(".") in IMAGE_FORMATS and size <= IMAGE_MAX:
        try:
            with os.fdopen(_open_beneath(key, path), "rb") as fh:
                blob = fh.read(IMAGE_MAX + 1)
        except (OSError, ValueError) as e:
            return f"{info}\n[image attach failed: {e}]\n{_link(key, f)}"
        # Bounded read, one byte past the limit, and handed over as bytes so the
        # fd opened and verified above is the only time this file is read.
        if len(blob) <= IMAGE_MAX:
            try:
                fmt = "jpeg" if ext == ".jpg" else ext.lstrip(".")
                return [f"{info}\n[image attached]", Image(data=blob, format=fmt)]
            except Exception as e:
                return f"{info}\n[image attach failed: {e}]\n{_link(key, f)}"

    # Every non-image is delivered as a file: a link, never the contents.
    url = _link(key, f)
    if url:
        return f"{info}\nopen in browser (valid {LINK_TTL // 60} min): {url}"
    return (f"{info}\n[no link available]\n"
            f"set MCP_SIGN_KEY and MCP_PUBLIC_BASE to enable links")


@mcp.tool
def sandbox_info() -> str:
    """Troubleshooting only: list the sandboxes that currently exist, and show
    the raw HTTP headers this client sent (credentials are redacted)."""
    keys = _existing_keys()
    listing = "\n".join(f"  {k}" for k in keys) or "  (none)"
    try:
        from fastmcp.server.dependencies import get_http_headers
        raw = get_http_headers(include_all=True) or {}
        # Anything credential-shaped: this output goes into the conversation,
        # and the sandbox has network access.
        hdrs = str({k: ("<redacted>"
                        if re.search(r"auth|cookie|key|secret|token", k, re.I)
                        else v)
                    for k, v in raw.items()})
    except Exception as e:
        hdrs = f"(unavailable: {e})"
    return (f"running sandboxes ({len(keys)}/{MAX_SANDBOXES}):\n{listing}\n\n"
            f"headers={hdrs}")


# ------------------------------------------------------------------ file links
class _DownloadResponse(Response):
    """Serve an open descriptor, and close it whatever happens.

    The descriptor is opened once, before this response exists, so the bytes
    that get sent are the ones that were verified. Starlette closes neither
    body iterators nor descriptors it did not open, hence the finally: it also
    runs when the client disconnects mid-transfer.
    """

    def __init__(self, fd, size, media_type=None):
        self._fd = fd
        super().__init__(b"", media_type=media_type,
                         headers={"content-length": str(size)})

    async def __call__(self, scope, receive, send):
        try:
            await send({"type": "http.response.start", "status": self.status_code,
                        "headers": self.raw_headers})
            if scope["method"] != "HEAD":
                while True:
                    block = await asyncio.to_thread(os.read, self._fd, 256 * 1024)
                    if not block:
                        break
                    await send({"type": "http.response.body", "body": block,
                                "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            os.close(self._fd)


@mcp.custom_route("/dl/{exp}/{sig}/{key}/{name:path}", methods=["GET"])
async def _download(request):
    """Read-only file serving for links returned by present_file."""
    p = request.path_params
    try:
        exp = int(p["exp"])
    except (TypeError, ValueError):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not SIGN_KEY or exp < time.time():
        return JSONResponse({"detail": "not found"}, status_code=404)
    if not hmac.compare_digest(_sign(exp, p["key"], p["name"]), p["sig"]):
        return JSONResponse({"detail": "not found"}, status_code=404)
    f = _resolve(p["key"], p["name"])
    if f is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    try:
        fd = _open_beneath(p["key"], p["name"])
    except (OSError, ValueError):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return _DownloadResponse(fd, os.fstat(fd).st_size, _mime(f))


threading.Thread(target=_reap, daemon=True).start()

try:
    _seed()
except Exception as e:
    # Nothing works without podman, so exit with the reason rather than a bare
    # traceback: systemd restarts on failure and the message lands in the journal.
    raise SystemExit(f"cannot list existing sandboxes: {e} — is {PODMAN} usable?")

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=HOST, port=PORT)
