#!/usr/bin/env python3
"""End-to-end checks against a running sandbox endpoint.

Speaks MCP Streamable HTTP directly, so it exercises nginx + the server + podman,
not just the Python code.

    ./venv/bin/python selftest.py --url https://host:8443/mcp/<path>/ --token XXXX
    ./venv/bin/python selftest.py --url http://127.0.0.1:8000/mcp --insecure
"""

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
import uuid

PASS, FAIL = "PASS", "FAIL"
results = []


def record(name, ok, detail=""):
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  -- {detail}" if detail else ""))


class Client:
    def __init__(self, url, token, insecure=False, timeout=180):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.sid = None
        self.ctx = ssl._create_unverified_context() if insecure else None

    def call(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers=headers,
            method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout,
                                    context=self.ctx) as r:
            if not self.sid and r.headers.get("Mcp-Session-Id"):
                self.sid = r.headers["Mcp-Session-Id"]
            raw = r.read().decode("utf-8", "replace")
        if notify:
            return None
        # Responses are either a JSON document or an SSE stream.
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return None

    def tool(self, name, args):
        res = self.call("tools/call", {"name": name, "arguments": args})
        if res is None or "result" not in res:
            return None, json.dumps(res)
        parts = []
        for c in res["result"].get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(f"<{c.get('type')}>")
        return res["result"], "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--label-a", default=f"selftest-a-{uuid.uuid4().hex[:6]}")
    ap.add_argument("--label-b", default=f"selftest-b-{uuid.uuid4().hex[:6]}")
    a = ap.parse_args()

    c = Client(a.url, a.token, a.insecure)

    try:
        c.call("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1"},
        })
        c.call("notifications/initialized", {}, notify=True)
    except urllib.error.HTTPError as e:
        record("initialize (handshake)", False, f"HTTP {e.code}")
        print("\ntip: 401 means the bearer token did not match nginx.")
        return 1
    except Exception as e:
        record("initialize (handshake)", False, str(e))
        return 1
    record("initialize (handshake)", True, f"session {c.sid}")

    res = c.call("tools/list", {})
    names = sorted(t["name"] for t in res.get("result", {}).get("tools", []))
    record("tools/list", {"run_command", "present_file"} <= set(names), ", ".join(names))

    _, out1 = c.tool("run_command", {"sandbox": a.label_a, "command": "hostname"})
    _, out2 = c.tool("run_command", {"sandbox": a.label_a, "command": "hostname"})
    h1 = out1.splitlines()[-1] if out1 else ""
    h2 = out2.splitlines()[-1] if out2 else ""
    record("same label reuses the container", h1 == h2 and bool(h1), f"{h1} / {h2}")
    record("first call reports [new]", "[new]" in out1, out1.splitlines()[0] if out1 else "")

    _, outv = c.tool("run_command", {"sandbox": a.label_a.upper(),
                                     "command": "hostname"})
    hv = outv.splitlines()[-1] if outv else ""
    record("label spelling does not fork the sandbox",
           hv == h1 and "[new]" not in outv, f"{h1} / {hv}")

    _, out3 = c.tool("run_command", {"sandbox": a.label_b, "command": "hostname"})
    h3 = out3.splitlines()[-1] if out3 else ""
    record("new label gets a new container", h3 != h1 and bool(h3), f"{h1} -> {h3}")

    _, out4 = c.tool("run_command", {
        "sandbox": a.label_a,
        "command": "echo probe > /workspace/selftest-probe.txt && cat /workspace/selftest-probe.txt",
    })
    record("write to /workspace", "probe" in out4)

    _, out5 = c.tool("present_file", {
        "sandbox": a.label_a, "path": "../../../etc/passwd"})
    record("present_file rejects traversal", "present error" in out5, out5.strip()[:60])

    _, out6 = c.tool("run_command", {
        "sandbox": a.label_a,
        "command": "ln -sfn /etc/passwd /workspace/escape && echo linked"})
    _, out7 = c.tool("present_file", {"sandbox": a.label_a, "path": "escape"})
    record("present_file rejects symlink escape", "present error" in out7,
           out7.strip()[:60])

    _, out8 = c.tool("present_file", {"sandbox": a.label_a, "path": "nope.txt"})
    record("present_file reports missing files", "present error" in out8)

    print()
    bad = [r for r in results if r[0] == FAIL]
    print(f"{len(results) - len(bad)}/{len(results)} passed")
    for _, name, detail in bad:
        print(f"  failed: {name}  {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
