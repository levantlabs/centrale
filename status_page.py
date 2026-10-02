"""The opt-in, read-only status page (task-212, decision-6).

Centrale's dashboard binds 127.0.0.1 only: it has no login, and every
mutating route is unauthenticated, so nothing on a network may reach it.
This module is the one exception, and it is deliberately narrow. When
projects.json's ``statusPage.enabled`` is true, a SECOND, separate HTTP
server listens on ``statusPage.port`` on every interface (or only the one
interface or address ``statusPage.bind`` names) and serves only a
phone-sized Fleet page, the one JSON document it polls and the static
files it loads. It is not a proxy of the dashboard and shares none of its
routes: ``StatusHandler`` is its own class, every path is an exact match
against the allowlist below, everything else is 404 and every method but
GET/HEAD is 405.

No address is ever configured: binding every interface survives a DHCP
change and a machine with several networks, and the links to open are
worked out from the addresses the machine has right now
(``status_links``). Because anything on those networks can reach the
port, a secret key is mandatory. Centrale generates it the first time the
page is turned on and keeps it in its state directory, next to the fleet
journal -- never in projects.json, never in a repo. Every request must
carry ``?key=<key>`` (compared in constant time) or it gets the same 404
as an unknown path.

What it shows is cut from the same fleet snapshot ``GET /api/fleet``
builds, by allowlist (``strip_snapshot``): names, task ids, agent names,
states and times. Never pane text, message text, owner-question text,
error text, settings or any control. The snapshot is taken with
``arm_input=False``, so a phone poll cannot arm the dashboard's reply box,
and it runs no merge gate and no other action.

Imports ``server`` lazily, like spawn.py and harvest.py, so server.py can
import this module from load_config() and main() without a cycle.
"""

from __future__ import annotations

import hmac
import http.server
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import fleet

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

DEFAULT_PORT = 7421
KEY_FILE_ENV = "CENTRALE_STATUS_KEY_FILE"

# The page's own HTML is a template: its asset URLs carry the key.
PAGE_FILE = "phone.html"
KEY_QUERY_PLACEHOLDER = "{{KEY_QUERY}}"

# Exact request path -> (file under static/, Content-Type). The whole
# file surface of the listener; nothing here resolves a request path
# against the filesystem, so no path trick can name another file.
ASSETS = {
    "/static/phone.js": ("phone.js", "text/javascript; charset=utf-8"),
    "/static/dom.js": ("dom.js", "text/javascript; charset=utf-8"),
    "/static/fleet.js": ("fleet.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.svg", "image/svg+xml"),
}
PAGE_PATH = "/"
DATA_PATH = "/api/status"

# A marker in the data document, so --check can tell a running Centrale's
# status page from some other program holding the port.
SNAPSHOT_KIND = "centrale-status"

NEEDS_YOU_LIST_CAP = 20

SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy",
     "default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def validate_port(value):
    """None when `value` is a usable port, else the reason."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        return "must be a whole number from 1 to 65535"
    return None


def validate_bind(value):
    """None when `value` is a usable bind restriction (None or "" for all
    interfaces, else one interface name or IP address), else the reason."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
        return "must be one interface name or IP address, or empty for all interfaces"
    return None


def normalize_config(raw, main_port=None):
    """projects.json's 'statusPage' key -> {"enabled": bool, "port": int,
    "bind": str | None}. Missing or null is off, the default. A present but
    malformed value raises ConfigError, like every other key load_config()
    checks."""
    import server

    if raw is None:
        return {"enabled": False, "port": DEFAULT_PORT, "bind": None}
    if not isinstance(raw, dict):
        raise server.ConfigError('projects.json: \'statusPage\' must be an object, e.g. {"enabled": true, "port": 7421}')
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise server.ConfigError("projects.json: statusPage.enabled must be true or false")
    port = raw.get("port", DEFAULT_PORT)
    err = validate_port(port)
    if err:
        raise server.ConfigError(f"projects.json: statusPage.port {err}")
    if enabled and main_port is not None and port == main_port:
        raise server.ConfigError(f"projects.json: statusPage.port must differ from the dashboard's port {main_port}")
    bind = raw.get("bind")
    err = validate_bind(bind)
    if err:
        raise server.ConfigError(f"projects.json: statusPage.bind {err}")
    return {"enabled": enabled, "port": port, "bind": bind or None}


def status_config(config):
    """The effective statusPage settings of a loaded (or test-built) config."""
    raw = config.get("statusPage") or {}
    return {"enabled": raw.get("enabled") is True, "port": raw.get("port") or DEFAULT_PORT,
            "bind": raw.get("bind") or None}


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------

def key_path():
    """$CENTRALE_STATUS_KEY_FILE if set, else
    $XDG_STATE_HOME/centrale/status-page.key (~/.local/state/... when
    XDG_STATE_HOME is unset): the state directory the fleet journal and
    the delivery log live in, outside every repo and outside projects.json."""
    override = os.environ.get(KEY_FILE_ENV)
    if override:
        return override
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state_home, "centrale", "status-page.key")


def read_key():
    """The stored key, or None when there is none yet."""
    try:
        with open(key_path(), "r", encoding="utf-8") as f:
            key = f.read().strip()
    except FileNotFoundError:
        return None
    return key or None


def ensure_key():
    """The stored key, generating it (owner-only file) the first time.
    Raises OSError when it can be neither read nor written."""
    key = read_key()
    if key:
        return key
    path = key_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    key = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process wrote it between our read and our create.
        existing = read_key()
        if existing:
            return existing
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    return key


def key_matches(given, expected):
    """Constant-time comparison of the request's key with the stored one."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# Addresses and links
# ---------------------------------------------------------------------------

def run_ip_addr():
    """`ip -j addr show`, the patchable boundary the address list reads.
    Returns the completed process, or None when `ip` is not available."""
    try:
        return subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def primary_address():
    """The address this machine would use for an outbound connection, by a
    UDP connect that sends nothing; the fallback where `ip` is missing."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _is_ip(text):
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _reachable(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def interface_addresses():
    """[(interface, address)] for every non-loopback, non-link-local
    address the machine has right now, IPv4 first. Temporary IPv6 privacy
    addresses are left out: they rotate, so a link to one goes stale."""
    proc = run_ip_addr()
    found = []
    if proc is not None and proc.returncode == 0:
        try:
            interfaces = json.loads(proc.stdout or "[]")
        except ValueError:
            interfaces = []
        for iface in interfaces if isinstance(interfaces, list) else []:
            for info in iface.get("addr_info") or []:
                local = info.get("local")
                if info.get("temporary") or not isinstance(local, str) or not _reachable(local):
                    continue
                found.append((iface.get("ifname"), local))
    else:
        primary = primary_address()
        if primary and _reachable(primary):
            found.append((None, primary))
    found.sort(key=lambda pair: ":" in pair[1])
    return found


# Container and virtual bridge interfaces: the listener still binds them
# (binding is unchanged), but no phone can reach their addresses, so a
# link to one only makes the list harder to read (reviewer ruling).
BRIDGE_INTERFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "cni", "flannel", "podman", "lxc", "lxd")


def is_bridge_interface(name):
    return isinstance(name, str) and name.startswith(BRIDGE_INTERFACE_PREFIXES)


def _link(host, port, key):
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/?key={urllib.parse.quote(key, safe='')}"


# task-216: a <hostname>.local name is mDNS, which does not cross a VPN and
# which Android resolves unreliably even on Wi-Fi; Apple devices on the same
# Wi-Fi resolve it reliably. The link stays, labelled; numeric links carry no
# label, here and everywhere the links are shown.
LOCAL_LINK_NOTE = "home Wi-Fi only; works on Apple devices; not over a VPN"


def link_label(url):
    """The note a link carries, or None: only the .local form has one."""
    host = urllib.parse.urlsplit(url).hostname or ""
    return LOCAL_LINK_NOTE if host.endswith(".local") else None


def labelled_link(url):
    """One link as the log and --check print it: the URL, then its note."""
    note = link_label(url)
    return f"{url}  ({note})" if note else url


def status_links(config, key):
    """The ready-made links to the page: one per address it is reachable
    on, then the machine's hostname.local form."""
    settings = status_config(config)
    bind, port = settings["bind"], settings["port"]
    if bind and _is_ip(bind):
        # Bound to one address, that is the only one that answers -- a
        # loopback one included, for the SSH port-forwarding recipe.
        links = [_link(bind, port, key)]
        if not _reachable(bind):
            return links
    else:
        addresses = interface_addresses()
        if bind:
            # Named explicitly, even a bridge is what the owner asked for.
            addresses = [(i, a) for i, a in addresses if i == bind]
        else:
            addresses = [(i, a) for i, a in addresses if not is_bridge_interface(i)]
        links = [_link(address, port, key) for _, address in addresses]
    hostname = socket.gethostname().split(".")[0]
    if hostname and hostname != "localhost":
        links.append(_link(f"{hostname}.local", port, key))
    return links


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def strip_snapshot(snapshot, config):
    """The status page's data: an allowlist over a get_fleet() snapshot.
    Every field is copied by name; nothing is passed through whole, so a
    field added to the fleet snapshot later never reaches the network."""
    projects = [{"name": p.get("name"), "maxAgents": p.get("maxAgents"), "agentCount": p.get("agentCount", 0)}
                for p in snapshot.get("projects") or []]
    agents = [{"project": a.get("project"), "taskId": a.get("taskId"), "agent": a.get("agent"),
               "state": a.get("state"), "stateSince": a.get("stateSince"), "parked": bool(a.get("parked"))}
              for a in snapshot.get("agents") or []]
    history = [{"project": r.get("project"), "taskId": r.get("taskId"), "agent": r.get("agent"),
                "state": r.get("state"), "timestamp": r.get("timestamp")}
               for r in snapshot.get("history") or []]
    needs = sorted(snapshot.get("needsYou") or [],
                   key=lambda i: i.get("since") if isinstance(i.get("since"), (int, float)) else 0)
    return {
        "kind": SNAPSHOT_KIND,
        "timestamp": snapshot.get("timestamp"),
        "window": snapshot.get("window"),
        "refreshIntervalSeconds": config.get("refreshIntervalSeconds"),
        "projects": projects,
        "agents": agents,
        "history": history,
        "needsYou": {
            "count": len(needs),
            "items": [{"kind": i.get("kind"), "project": i.get("project"), "taskId": i.get("taskId"),
                       "agent": i.get("agent"), "since": i.get("since")}
                      for i in needs[:NEEDS_YOU_LIST_CAP]],
            # How many checks could not run, never what they said: the
            # page shows that it does not know, not why.
            "unavailable": len(snapshot.get("needsYouErrors") or []),
        },
        "historyUnavailable": bool(snapshot.get("historyError")),
    }


def status_snapshot(config):
    import server

    return strip_snapshot(server.get_fleet(config, fleet.DEFAULT_WINDOW, arm_input=False), config)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class StatusHandler(http.server.BaseHTTPRequestHandler):
    """The read-only listener's whole request surface. Deliberately not a
    server.Handler subclass: it inherits none of the dashboard's routes."""

    server_version = "Centrale"
    sys_version = ""
    # A slow or idle client cannot hold a thread for long.
    timeout = 15

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        pass

    def __getattr__(self, name):
        # BaseHTTPRequestHandler dispatches to do_<METHOD> and answers 501
        # when there is none; every method other than GET and HEAD gets the
        # same refusal here instead, after the same key check.
        if name.startswith("do_"):
            return self._refuse_method
        raise AttributeError(name)

    # -- responses ---------------------------------------------------------
    def _send(self, status, ctype, body, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS + tuple(extra):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
                pass

    def _send_json(self, status, payload, extra=()):
        self._send(status, "application/json", json.dumps(payload).encode("utf-8"), extra)

    def _not_found(self):
        self._send_json(404, {"error": "not found"})

    # -- gate ----------------------------------------------------------------
    def _allowed(self):
        """True when this request may be answered at all. Otherwise it has
        been answered with the plain 404 an unknown path gets."""
        # A browser labels a request another site's page started; this page
        # never makes one, so such a request is refused like any stranger.
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if fetch_site not in ("", "same-origin", "none"):
            self._not_found()
            return False
        given = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                      keep_blank_values=True).get("key") or []
        if len(given) != 1 or not key_matches(given[0], self.server.key):
            self._not_found()
            return False
        return True

    def _refuse_method(self):
        try:
            if self._allowed():
                self._send_json(405, {"error": "this page is read-only"}, (("Allow", "GET, HEAD"),))
        except Exception:  # pragma: no cover - defensive, never leak tracebacks
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            if not self._allowed():
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == PAGE_PATH:
                self._serve_page()
            elif path == DATA_PATH:
                self._serve_data()
            elif path in ASSETS:
                name, ctype = ASSETS[path]
                self._send(200, ctype, self._read_static(name))
            else:
                self._not_found()
        except Exception:  # pragma: no cover - defensive, never leak tracebacks
            try:
                self._send_json(500, {"error": "internal error"})
            except Exception:
                pass

    def _read_static(self, name):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            return f.read()

    def _serve_page(self):
        query = "?key=" + urllib.parse.quote(self.server.key, safe="")
        body = self._read_static(PAGE_FILE).replace(KEY_QUERY_PLACEHOLDER.encode(), query.encode())
        self._send(200, "text/html; charset=utf-8", body)

    def _serve_data(self):
        import server

        try:
            payload = status_snapshot(self.server.centrale_config)
        except (server.BacklogError, OSError):
            # The reason may name paths or commands; the page only needs to
            # know that this poll did not produce a snapshot.
            self._send_json(503, {"error": "fleet status unavailable right now"})
            return
        self._send_json(200, payload)


class StatusServer(http.server.ThreadingHTTPServer):
    """Bound to every interface (IPv6 and IPv4 at once where the system
    allows it), to one address, or to one interface by name."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config, key, port, bind=None):
        self.centrale_config = config
        self.key = key
        self._device = None
        host = "::"
        if bind:
            if bind in {name for _, name in socket.if_nameindex()}:
                self._device = bind
            else:
                host = bind
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            super().__init__((host, port), StatusHandler)
        except OSError:
            if host != "::" or not socket.has_ipv6:
                raise
            # No IPv6 on this machine: every IPv4 interface instead.
            self.address_family = socket.AF_INET
            super().__init__(("0.0.0.0", port), StatusHandler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6 and self.server_address[0] == "::":
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        if self._device:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self._device.encode())
        super().server_bind()


def describe_bind(settings):
    where = settings["bind"] or "all interfaces"
    return f"port {settings['port']} on {where}"


def bind_failure_message(settings, exc):
    return (
        f"Centrale: the read-only status page did not start: could not listen on {describe_bind(settings)} "
        f"({exc}). The dashboard is running as usual; change statusPage in Settings or projects.json."
    )


# The one running listener of this process, so Settings can turn the page
# on, off or onto another port without a restart.
_state = {"server": None, "spec": None, "error": None}
_state_lock = threading.Lock()


def _stop_locked():
    running = _state["server"]
    _state.update(server=None, spec=None)
    if running is not None:
        running.shutdown()
        running.server_close()


def apply(config, log=None):
    """Make the running listener match the config: start, stop or rebind
    it. A failure is recorded (status()["error"]), printed to `log`
    (stderr) and never raised -- the dashboard is unaffected either way.
    Returns the error text, or None."""
    log = log if log is not None else sys.stderr
    settings = status_config(config)
    spec = (settings["port"], settings["bind"]) if settings["enabled"] else None
    with _state_lock:
        if spec is not None and spec == _state["spec"] and _state["server"] is not None:
            return None
        _stop_locked()
        _state["error"] = None
        if spec is None:
            return None
        try:
            key = ensure_key()
            server = StatusServer(config, key, *spec)
        except (OSError, ValueError) as exc:
            _state["error"] = str(exc)
            print(bind_failure_message(settings, exc), file=log, flush=True)
            return _state["error"]
        _state.update(server=server, spec=spec)
    threading.Thread(target=server.serve_forever, name="centrale-status-page", daemon=True).start()
    print(f"Centrale read-only status page on {describe_bind(settings)}; open it at:", file=log, flush=True)
    for link in status_links(config, key):
        print(f"  {labelled_link(link)}", file=log, flush=True)
    return None


def stop():
    with _state_lock:
        _stop_locked()
        _state["error"] = None


def status(config):
    """What Settings shows: the settings, whether the listener is up, why
    not when it failed, and the links to open."""
    settings = status_config(config)
    with _state_lock:
        running = _state["server"] is not None
        error = _state["error"]
    links = []
    if settings["enabled"]:
        try:
            key = read_key()
        except OSError:
            key = None
        links = status_links(config, key) if key else []
    # `links` stays the plain URL strings; `labelledLinks` is the same list
    # with each link's note (null when it has none), additive for consumers.
    return {**settings, "running": running, "error": error, "links": links,
            "labelledLinks": [{"url": u, "label": link_label(u)} for u in links]}


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

def probe_status_page(port, key, bind=None):
    """True when a Centrale status page already answers on this port."""
    host = "127.0.0.1"
    if bind and _is_ip(bind):
        host = f"[{bind}]" if ":" in bind else bind
    url = f"http://{host}:{port}{DATA_PATH}?key={urllib.parse.quote(key, safe='')}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode("utf-8")).get("kind") == SNAPSHOT_KIND
    except (OSError, ValueError, urllib.error.URLError):
        return False


def doctor_check(config):
    """[(level, message)] for --check; empty when the page is off. Turning
    the page on is what creates the key, so --check creates it too when it
    is missing, to print working links."""
    settings = status_config(config)
    if not settings["enabled"]:
        return []
    try:
        key = ensure_key()
    except OSError as exc:
        return [("WARN", f"read-only status page: cannot read or create its key at {key_path()} ({exc}) -- "
                         "it will not start")]
    try:
        StatusServer(config, key, settings["port"], settings["bind"]).server_close()
    except OSError as exc:
        if not probe_status_page(settings["port"], key, settings["bind"]):
            return [("WARN", f"read-only status page: cannot listen on {describe_bind(settings)} ({exc}) -- "
                             "Centrale will still start, without the status page")]
        result = [("PASS", f"read-only status page: a running Centrale is serving it on {describe_bind(settings)}")]
    else:
        result = [("PASS", f"read-only status page: {describe_bind(settings)} is free to listen on")]
    links = status_links(config, key)
    if not links:
        result.append(("WARN", "read-only status page: this machine has no non-loopback address to link to"))
    for link in links:
        result.append(("PASS", f"read-only status page link: {labelled_link(link)}"))
    return result
