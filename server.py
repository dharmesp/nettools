"""Tools Manager launcher: tiny local backend + app-mode (no address bar) window launcher.

Run:  python server.py [--no-open]
Stdlib only. Data lives in ./data (shortcuts.json + icons/).
"""
import collections
import ctypes
import datetime
import io
import json
import mimetypes
import os
import random
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from html.parser import HTMLParser

import donate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", int(os.environ.get("TOOLS_PORT", 8000))
ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
DATA_DIR = os.environ.get("TOOLS_DATA_DIR") or os.path.join(ROOT, "data")
ICON_DIR = os.path.join(DATA_DIR, "icons")
DB_FILE = os.path.join(DATA_DIR, "shortcuts.json")
CAT_FILE = os.path.join(DATA_DIR, "categories.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.json")        # {"events": [{id, title, start, minutes}]}: your own calendar events
COMMANDS_FILE = os.path.join(DATA_DIR, "commands.json")   # {"groups": [{id, name, items: [{id, label, text}]}]}
OUTAGE_FILE = os.path.join(DATA_DIR, "outages.json")         # when the gateway / internet dropped, and for how long
NET_TARGETS_FILE = os.path.join(DATA_DIR, "net-targets.json")  # extra hosts you chose to watch in the network monitor
SOUND_DIR = os.path.join(DATA_DIR, "sounds")                # your own alert sounds: down / warn / up / reminder
MENU_FILE = os.path.join(DATA_DIR, "menu.json")            # {"dividers": [category names]}: a line is drawn under each of these
MAIN_URL = f"http://localhost:{PORT}/"
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_ICON_BYTES = 2 * 1024 * 1024
EXT_BY_TYPE = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/svg+xml": ".svg",
               "image/x-icon": ".ico", "image/vnd.microsoft.icon": ".ico"}

db_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def load_db():
    try:
        with open(DB_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_json(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def save_db(items):
    write_json(DB_FILE, items)


def load_cats():
    try:
        with open(CAT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_dividers():
    try:
        with open(MENU_FILE, encoding="utf-8") as f:
            return [d for d in json.load(f).get("dividers", []) if isinstance(d, str)]
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        return []


def save_dividers(dividers, cats=None):
    """Keep only dividers that belong to a real category (no duplicates), and save them."""
    cats = load_cats() if cats is None else cats
    keep = []
    for d in dividers:
        if d in cats and d not in keep:
            keep.append(d)
    write_json(MENU_FILE, {"dividers": keep})
    return keep


def clean_cat(raw):
    return " ".join(str(raw or "").split())[:40]


def ensure_category(name):
    """Add name to the category list if new. Caller holds db_lock."""
    if name:
        cats = load_cats()
        if name not in cats:
            write_json(CAT_FILE, cats + [name])


# ---------------------------------------------------------------- icon / title discovery
class HeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.metas, self.title, self._in_title = [], {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and a.get("href"):
            self.links.append(a)
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content"):
                self.metas.setdefault(key, a["content"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for ctx in (None, ssl._create_unverified_context()):  # 2nd try: corporate proxy certs
        try:
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                return r.read(limit), r.headers.get_content_type(), r.geturl()
        except ssl.SSLError:
            continue
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLError):
                continue
            raise
    raise OSError("TLS failure")


def largest_size(sizes):
    nums = [int(n) for n in re.findall(r"(\d+)x\d+", sizes or "")]
    return max(nums) if nums else 0


def discover(page_url):
    """Return (title, [candidate icon urls best-first])."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(page_url))
    title, apple, icons, og = "", [], [], []
    try:
        body, _, final = http_get(page_url, 512 * 1024)
        p = HeadParser()
        p.feed(body.decode("utf-8", "replace"))
        title = re.split(r"\s[·|\-–—:]\s", " ".join(p.title.split()))[0][:60]
        for l in p.links:
            rel = (l.get("rel") or "").lower()
            href = urllib.parse.urljoin(final, l["href"])
            if "apple-touch-icon" in rel:
                apple.append((largest_size(l.get("sizes")), href))
            elif "icon" in rel:
                icons.append((largest_size(l.get("sizes")), href))
        for k in ("og:image", "twitter:image"):
            if k in p.metas:
                og.append(urllib.parse.urljoin(final, p.metas[k]))
    except Exception:
        pass  # page may need login; fall back to /favicon.ico
    ordered = [u for _, u in sorted(apple, reverse=True)]
    ordered += [u for _, u in sorted(icons, reverse=True)]
    ordered += og + [origin + "/favicon.ico"]
    return title, list(dict.fromkeys(ordered))


def sniff_ext(data, ctype):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if b"<svg" in data[:2048].lower():
        return ".svg"
    return None


def fetch_icon(candidates, shortcut_id):
    """Download first candidate that is really an image; return stored filename or None."""
    os.makedirs(ICON_DIR, exist_ok=True)
    for url in candidates:
        try:
            if os.path.isfile(url):  # local image picked by the user
                with open(url, "rb") as f:
                    data, ctype = f.read(MAX_ICON_BYTES), ""
            else:
                data, ctype, _ = http_get(url, MAX_ICON_BYTES)
        except Exception:
            continue
        ext = sniff_ext(data, ctype)
        if not ext or len(data) < 100:
            continue
        remove_icons(shortcut_id)
        name = f"{shortcut_id}-{int(time.time())}{ext}"
        with open(os.path.join(ICON_DIR, name), "wb") as f:
            f.write(data)
        return name
    return None


def remove_icons(shortcut_id):
    if not os.path.isdir(ICON_DIR):
        return
    for fn in os.listdir(ICON_DIR):
        if fn.startswith(shortcut_id + "-"):
            try:
                os.remove(os.path.join(ICON_DIR, fn))
            except OSError:
                pass


# ---------------------------------------------------------------- browser launching
def find_browser():
    env = os.environ.get("APP_BROWSER")
    if env and os.path.exists(env):
        return env
    pf = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
          os.environ.get("LOCALAPPDATA")]
    rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for rel in rels:
        for base in pf:
            if base and os.path.exists(os.path.join(base, rel)):
                return os.path.join(base, rel)
    return shutil.which("msedge") or shutil.which("chrome")


def run_bat(path, hidden):
    """Start a .bat/.cmd in its own folder; visible console unless hidden."""
    flags = 0x08000000 if hidden else 0x00000010  # CREATE_NO_WINDOW | CREATE_NEW_CONSOLE
    subprocess.Popen(["cmd.exe", "/c", path], cwd=os.path.dirname(path),
                     creationflags=flags, close_fds=True)


def reachable(url):
    u = urllib.parse.urlparse(url)
    try:
        with socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=1):
            return True
    except OSError:
        return False


def open_when_ready(url, width, height, timeout=90):
    """After a bat starts a local tool, open its window as soon as the port answers."""
    end = time.time() + timeout
    while time.time() < end:
        if reachable(url):
            time.sleep(0.5)
            open_app_window(url, width, height)
            return
        time.sleep(1)


def open_app_window(url, width=1280, height=800):
    exe = find_browser()
    if not exe:
        raise RuntimeError("Edge/Chrome not found (set APP_BROWSER to the exe path)")
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, f"--app={url}", f"--window-size={width},{height}"],
                     creationflags=flags, close_fds=True)


# ---------------------------------------------------------------- validation
def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL is required")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    u = urllib.parse.urlparse(raw)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError("Only http/https URLs are allowed")
    return raw


def validate_bat(raw):
    p = os.path.normpath(str(raw or "").strip().strip('"'))
    if not os.path.isabs(p):
        raise ValueError("Enter the full path to the .bat file (e.g. C:\\tools\\start.bat)")
    if os.path.splitext(p)[1].lower() not in (".bat", ".cmd"):
        raise ValueError("Only .bat or .cmd files are allowed")
    if not os.path.isfile(p):
        raise ValueError(f"File not found: {p}")
    return p


def icon_source(raw):
    """An icon is either a local image file or an http(s) URL."""
    raw = raw.strip().strip('"')
    return raw if os.path.isfile(raw) else normalize_url(raw)


def clamp(v, default, lo=300, hi=4000):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- network monitor: latency to the gateway and to Google DNS
class _IcmpOptions(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _IcmpEchoReply(ctypes.Structure):          # ICMP_ECHO_REPLY from iphlpapi
    _fields_ = [("Address", ctypes.c_uint32), ("Status", ctypes.c_uint32), ("RoundTripTime", ctypes.c_uint32),
                ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                ("Options", _IcmpOptions)]


_icmp_dll = None


def _icmp_lib():
    global _icmp_dll
    if _icmp_dll is None and sys.platform == "win32":
        lib = ctypes.WinDLL("iphlpapi", use_last_error=True)
        lib.IcmpCreateFile.restype = ctypes.c_void_p
        lib.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
        lib.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_ushort,
                                     ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        lib.IcmpSendEcho.restype = ctypes.c_uint32
        _icmp_dll = lib
    return _icmp_dll


def icmp_ping_ms(ip, timeout_ms=1000):
    """One ICMP echo through Windows' own API (no ping.exe, no console flash). Returns milliseconds or None."""
    lib = _icmp_lib()
    if not lib or not ip:
        return None
    handle = lib.IcmpCreateFile()
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        return None
    try:
        data = b"ToolsMgr"
        size = ctypes.sizeof(_IcmpEchoReply) + len(data) + 8
        buf = ctypes.create_string_buffer(size)
        addr = struct.unpack("<L", socket.inet_aton(ip))[0]
        t0 = time.perf_counter()
        n = lib.IcmpSendEcho(handle, addr, data, len(data), None, buf, size, timeout_ms)
        ms = (time.perf_counter() - t0) * 1000
        if n == 0 or _IcmpEchoReply.from_buffer(buf).Status != 0:
            return None
        return round(ms, 2)
    finally:
        lib.IcmpCloseHandle(handle)


def tcp_ping_ms(ip, port, timeout=0.8):
    """Time to open a TCP connection: a stand-in for ping when a firewall blocks ICMP."""
    t0 = time.perf_counter()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            pass
    except OSError:
        return None
    return round((time.perf_counter() - t0) * 1000, 2)


def find_gateway():
    """The default gateway with the best (lowest) metric, read from the routing table; None when offline."""
    try:
        out = subprocess.run(["route", "print", "-4", "0.0.0.0"], capture_output=True, text=True, timeout=6,
                             stdin=subprocess.DEVNULL, creationflags=0x08000000).stdout
    except Exception:
        return None
    best = None
    for line in out.splitlines():
        m = re.match(r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+\d+\.\d+\.\d+\.\d+\s+(\d+)\s*$", line)
        if m and (best is None or int(m.group(2)) < best[0]):
            best = (int(m.group(2)), m.group(1))
    return best[1] if best else None


def _run(cmd, timeout=6):
    """Run a Windows command with no console window and return its text output ('' on any problem)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=0x08000000).stdout
    except Exception:
        return ""


class _IpAddrString(ctypes.Structure):
    pass


_IpAddrString._fields_ = [("Next", ctypes.POINTER(_IpAddrString)), ("IpAddress", ctypes.c_char * 16),
                          ("IpMask", ctypes.c_char * 16), ("Context", ctypes.c_ulong)]


class _FixedInfo(ctypes.Structure):
    _fields_ = [("HostName", ctypes.c_char * 132), ("DomainName", ctypes.c_char * 132),
                ("CurrentDnsServer", ctypes.POINTER(_IpAddrString)), ("DnsServerList", _IpAddrString),
                ("NodeType", ctypes.c_uint), ("ScopeId", ctypes.c_char * 260), ("EnableRouting", ctypes.c_uint),
                ("EnableProxy", ctypes.c_uint), ("EnableDns", ctypes.c_uint)]


def dns_servers():
    """The DNS servers Windows is using, in order (read straight from the network API, so it works in any language)."""
    try:
        lib = ctypes.WinDLL("iphlpapi")
        size = ctypes.c_ulong(0)
        lib.GetNetworkParams(None, ctypes.byref(size))                 # first call only reports how big the answer is
        buf = ctypes.create_string_buffer(size.value)
        if lib.GetNetworkParams(buf, ctypes.byref(size)) != 0:
            return []
        info = ctypes.cast(buf, ctypes.POINTER(_FixedInfo)).contents
        out, cur = [], ctypes.pointer(info.DnsServerList)
        while cur and len(out) < 8:
            ip = cur.contents.IpAddress.decode("ascii", "ignore")
            if ip and ip != "0.0.0.0" and ip not in out:
                out.append(ip)
            cur = cur.contents.Next
        return out
    except Exception:
        return []


def dns_query_ms(server, timeout=1.0):
    """How long the DNS server takes to answer one real lookup (a raw query, so Windows' own cache does not hide it)."""
    name = random.choice(("www.google.com", "www.microsoft.com", "www.cloudflare.com", "www.amazon.com"))
    qid = random.randrange(65536)
    query = (struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
             + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        s.sendto(query, (server, 53))
        data, _ = s.recvfrom(1024)
        ms = (time.perf_counter() - t0) * 1000
        if len(data) < 12 or struct.unpack(">H", data[:2])[0] != qid:
            return None
        return round(ms, 2)
    except OSError:
        return None
    finally:
        s.close()


def wifi_info():
    """Name, signal and channel of the connected Wi-Fi network, or None on a cable / when Wi-Fi is off.
    Reads `netsh wlan show interfaces`, whose labels are in English; on another Windows language this just shows nothing."""
    info = {}
    for line in _run(["netsh", "wlan", "show", "interfaces"]).splitlines():
        m = re.match(r"^\s+([A-Za-z][A-Za-z0-9 ./()-]*?)\s*:\s*(.+?)\s*$", line)
        if m:
            info.setdefault(m.group(1), m.group(2))
    if not info.get("SSID") or info.get("State", "connected").lower() != "connected":
        return None
    sig = re.match(r"(\d+)", info.get("Signal", ""))
    return {"ssid": info["SSID"][:40], "signal": int(sig.group(1)) if sig else None, "channel": info.get("Channel"),
            "radio": info.get("Radio type"), "band": info.get("Band"), "rate": info.get("Receive rate (Mbps)")}


VPN_WORDS = ("anyconnect", "secure client", "globalprotect", "pangp", "forticlient", "fortinet", "wireguard", "openvpn",
             "tap-windows", "wintun", "juniper", "pulse secure", "nordlynx", "zscaler", "vpn", "ikev2", "l2tp", "sstp",
             "pptp", "check point", "sonicwall", "netextender")


def vpn_adapters():
    """Names of connected network adapters that look like a VPN (a best guess from the adapter descriptions)."""
    found, desc, up, has_ip = [], None, True, False

    def close():
        if desc and up and has_ip and any(w in desc.lower() for w in VPN_WORDS) and desc not in found:
            found.append(desc[:60])

    for line in _run(["ipconfig", "/all"], 10).splitlines():
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):      # a new adapter starts
            close(); desc, up, has_ip = None, True, False
            continue
        m = re.match(r"^\s+(Description|Media State|IPv4 Address)[ .]*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "Description":
            desc = val.strip()
        elif key == "Media State":
            up = "disconnected" not in val.lower()
        else:
            has_ip = True
    close()
    return found


class _FileTime(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32), ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64), ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64), ("ullAvailExtendedVirtual", ctypes.c_uint64)]


def pc_health(prev_times):
    """CPU %, memory % and free disk % of the PC. prev_times is what the last call returned (CPU needs two readings)."""
    out, times = {}, prev_times
    try:
        k32 = ctypes.WinDLL("kernel32")
        idle, kern, user = _FileTime(), _FileTime(), _FileTime()
        if k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            def v(x): return (x.hi << 32) | x.lo
            times = (v(idle), v(kern) + v(user))                       # kernel time already includes idle time
            if prev_times and times[1] > prev_times[1]:
                out["cpu"] = round(100 * (1 - (times[0] - prev_times[0]) / (times[1] - prev_times[1])))
        mem = _MemStatus(); mem.dwLength = ctypes.sizeof(_MemStatus)
        if k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            out["mem"] = int(mem.dwMemoryLoad)
        drive = os.environ.get("SystemDrive", "C:") + "\\"
        du = shutil.disk_usage(drive)
        out["disk"] = drive[:2]; out["disk_free"] = round(100 * du.free / du.total)
    except Exception:
        pass
    return out, times


HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def clean_targets(raw):
    """Validate the extra hosts you chose to watch (up to 4). Returns a clean list or raises ValueError."""
    if not isinstance(raw, list) or len(raw) > 4:
        raise ValueError("You can watch up to 4 extra targets")
    out, used, hosts = [], set(), set()
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("Targets are not valid")
        host = str(r.get("host") or "").strip().lower()
        if not HOST_RE.match(host):
            raise ValueError(f'"{host or "(empty)"}" is not a valid IP address or host name')
        if host in hosts:
            raise ValueError(f'"{host}" is in the list twice')
        hosts.add(host)
        name = " ".join(str(r.get("name") or "").split())[:40] or host
        out.append({"id": _new_id(r.get("id"), used), "name": name, "host": host})
    return out


def load_net_targets():
    try:
        with open(NET_TARGETS_FILE, encoding="utf-8") as f:
            return clean_targets(json.load(f).get("targets", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
        return []


def load_outages():
    try:
        with open(OUTAGE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return [e for e in raw if isinstance(e, dict) and e.get("target") in ("gateway", "internet")
                and isinstance(e.get("start"), (int, float))][:200]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def resolve_ipv4(host):
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def series_stats(pts):
    ok = [p[1] for p in pts if p[1] is not None]
    jitter = sum(abs(a - b) for a, b in zip(ok, ok[1:])) / (len(ok) - 1) if len(ok) > 1 else None
    return {"n": len(pts), "avg": round(sum(ok) / len(ok), 2) if ok else None, "min": min(ok) if ok else None,
            "max": max(ok) if ok else None, "loss": round(100 * (len(pts) - len(ok)) / len(pts), 1) if pts else 0,
            "jitter": round(jitter, 2) if jitter is not None else None}


class NetMonitor:
    """Pings the gateway, Google DNS and any extra hosts you chose once a second, and watches DNS speed, Wi-Fi, VPN and PC load.
    It only works while a page is watching (/api/net was asked in the last few seconds); then it stops by itself."""
    WINDOW = 120                                   # seconds the small chart shows
    KEEP = 3700                                    # seconds of history kept for the big window (1 hour)
    GOOGLE = ("8.8.8.8", "8.8.4.4")                # anycast: the internet routes each to a nearby Google location
    MAX_OUTAGES = 200

    def __init__(self):
        self.lock = threading.Lock()
        self.targets = load_net_targets()
        self.series = {k: collections.deque(maxlen=self.KEEP) for k in ("gateway", "internet", *[t["id"] for t in self.targets])}
        self.gateway, self.target, self.via = None, self.GOOGLE[0], "icmp"
        self.dns, self.wifi, self.sysinfo, self.vpn = {"server": None, "ms": None}, None, {}, []
        self.outages = load_outages()
        self._open, self._nulls, self._first_null = {}, {"gateway": 0, "internet": 0}, {}
        self.last_request = 0.0
        self._threads = {}
        self._icmp_fail, self._tcp_until = 0, 0.0

    # ---- lifecycle
    def active(self):
        return time.time() - self.last_request < 12

    def touch(self):
        self.last_request = time.time()
        wanted = [("gw", self._gateway_loop), ("net", self._internet_loop), ("aux", self._aux_loop)]
        wanted += [("t:" + t["id"], (lambda tid=t["id"]: self._target_loop(tid))) for t in self.targets]
        for name, fn in wanted:
            th = self._threads.get(name)
            if not (th and th.is_alive()):
                self._threads[name] = threading.Thread(target=fn, daemon=True)
                self._threads[name].start()

    # ---- what the page asks for
    def snapshot(self):
        with self.lock:
            now, since = time.time(), time.time() - 86400
            return {"now": now, "window": self.WINDOW, "gateway": self.gateway, "internet": self.target, "via": self.via,
                    "gw": list(self.series["gateway"])[-(self.WINDOW + 30):], "inet": list(self.series["internet"])[-(self.WINDOW + 30):],
                    "dns": dict(self.dns), "wifi": self.wifi, "sys": dict(self.sysinfo), "vpn": list(self.vpn),
                    "outages24": sum(1 for e in self.outages if e["start"] >= since), "down_now": sorted(self._open)}

    def history(self, rng):
        rng = rng if rng in (300, 900, 3600) else 3600
        bucket = {300: 3, 900: 8, 3600: 30}[rng]
        now = time.time(); start = now - rng
        with self.lock:
            meta = [("gateway", "Gateway", self.gateway or ""), ("internet", "Google DNS", self.target)] + \
                   [(t["id"], t["name"], t["host"]) for t in self.targets]
            data = {sid: [p for p in list(self.series.get(sid, ())) if p[0] >= start] for sid, _, _ in meta}
        out = []
        for sid, name, host in meta:
            pts, buckets = data[sid], {}
            for t, ms in pts:
                b = buckets.setdefault(int((t - start) // bucket), [0, 0, 0.0, 0.0])      # count, lost, sum, max
                b[0] += 1
                if ms is None:
                    b[1] += 1
                else:
                    b[2] += ms; b[3] = max(b[3], ms)
            points = []
            for k in sorted(buckets):
                n, lost, s, mx = buckets[k]
                good = n - lost
                points.append([round(start + k * bucket + bucket / 2, 1), round(s / good, 2) if good else None,
                               round(mx, 2) if good else None, round(100 * lost / n)])
            out.append({"id": sid, "name": name, "host": host, "points": points, "stats": series_stats(pts)})
        return {"now": now, "range": rng, "bucket": bucket, "series": out}

    def outage_list(self):
        with self.lock:
            return [dict(e) for e in self.outages]

    def clear_outages(self):
        with self.lock:
            self.outages, self._open = [], {}
        try:
            os.remove(OUTAGE_FILE)
        except OSError:
            pass

    def set_targets(self, targets):
        with self.lock:
            self.targets = targets
            keep = {"gateway", "internet", *[t["id"] for t in targets]}
            for k in list(self.series):
                if k not in keep:
                    del self.series[k]
            for t in targets:
                self.series.setdefault(t["id"], collections.deque(maxlen=self.KEEP))
            write_json(NET_TARGETS_FILE, {"targets": targets})
        if self.active():
            self.touch()

    def reset_config(self):
        with self.lock:
            self.targets = []
            for k in [k for k in self.series if k not in ("gateway", "internet")]:
                del self.series[k]
        for p in (NET_TARGETS_FILE,):
            try:
                os.remove(p)
            except OSError:
                pass
        self.clear_outages()

    # ---- outage log: three lost replies in a row opens an outage; the next reply closes it
    def _save_outages(self):
        write_json(OUTAGE_FILE, self.outages)

    def _track(self, target, ms, t):
        with self.lock:
            if ms is None:
                n = self._nulls[target] = self._nulls[target] + 1
                if n == 1:
                    self._first_null[target] = t
                if n == 3 and target not in self._open:
                    ev = {"target": target, "start": round(self._first_null[target], 1), "end": None}
                    self.outages.insert(0, ev)
                    del self.outages[self.MAX_OUTAGES:]
                    self._open[target] = ev
                    self._save_outages()
            else:
                self._nulls[target] = 0
                ev = self._open.pop(target, None)
                if ev:
                    ev["end"] = round(t, 1)
                    self._save_outages()

    def _finish(self, target):
        """Monitoring stopped (the page was closed): close an outage that is still open so it is not left running forever."""
        with self.lock:
            ev = self._open.pop(target, None)
            if ev:
                ev["end"] = round(time.time(), 1); ev["cut"] = True
                self._save_outages()
            self._nulls[target] = 0

    # ---- the sampling loops
    def _gateway_loop(self):
        checked = 0.0
        while self.active():
            t0 = time.time()
            if t0 - checked > 30 or not self.gateway:                # the gateway changes when you switch Wi-Fi or connect a VPN
                gw = find_gateway()
                with self.lock:
                    self.gateway = gw
                checked = t0
            ms = icmp_ping_ms(self.gateway) if self.gateway else None
            with self.lock:
                self.series["gateway"].append([time.time(), ms])
            self._track("gateway", ms, time.time())
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
        self._finish("gateway")

    def _probe(self, ip):
        """ICMP first; if a firewall blocks it, a TCP connect to Google instead (and remember to skip ICMP for a minute)."""
        if time.time() >= self._tcp_until:
            ms = icmp_ping_ms(ip)
            if ms is not None:
                self.via, self._icmp_fail = "icmp", 0
                return ms
            self._icmp_fail += 1
        for port in (443, 53):
            ms = tcp_ping_ms(ip, port)
            if ms is not None:
                self.via = "tcp"
                if self._icmp_fail >= 5:
                    self._tcp_until = time.time() + 60
                return ms
        return None

    def _pick_target(self):
        """Of the two Google DNS addresses, use the one that answers faster from here."""
        best = None
        for ip in self.GOOGLE:
            got = [m for m in (self._probe(ip) for _ in range(3)) if m is not None]
            if got and (best is None or sum(got) / len(got) < best[0]):
                best = (sum(got) / len(got), ip)
        if best:
            with self.lock:
                self.target = best[1]

    def _internet_loop(self):
        picked = 0.0
        while self.active():
            t0 = time.time()
            if t0 - picked > 300:
                self._pick_target()
                picked = time.time()
            ms = self._probe(self.target)
            with self.lock:
                self.series["internet"].append([time.time(), ms])
            self._track("internet", ms, time.time())
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
        self._finish("internet")

    def _target_loop(self, tid):
        """One of the extra hosts you chose: ICMP, or TCP 443/80 when ICMP is blocked."""
        ip, resolved, icmp_off = None, 0.0, 0.0
        while self.active():
            t0 = time.time()
            tg = next((x for x in self.targets if x["id"] == tid), None)
            if tg is None:
                return                                              # removed from the list
            if not ip or t0 - resolved > 60:
                ip, resolved = resolve_ipv4(tg["host"]), t0
            ms = None
            if ip:
                if t0 >= icmp_off:
                    ms = icmp_ping_ms(ip)
                if ms is None:
                    for port in (443, 80):
                        ms = tcp_ping_ms(ip, port)
                        if ms is not None:
                            icmp_off = t0 + 60                      # ICMP is blocked here: go straight to TCP for a minute
                            break
            with self.lock:
                if tid in self.series:
                    self.series[tid].append([time.time(), ms])
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))

    def _aux_loop(self):
        """DNS speed, Wi-Fi, VPN and PC health: slower things that do not need a reading every second."""
        last, cpu_prev = {"dns": 0.0, "wifi": 0.0, "vpn": 0.0, "sys": 0.0}, None
        while self.active():
            now = time.time()
            if now - last["sys"] >= 2:
                info, cpu_prev = pc_health(cpu_prev)
                with self.lock:
                    self.sysinfo = info
                last["sys"] = now
            if now - last["dns"] >= 5:
                servers = dns_servers()
                ms = dns_query_ms(servers[0]) if servers else None
                with self.lock:
                    self.dns = {"server": servers[0] if servers else None, "ms": ms}
                last["dns"] = time.time()
            if now - last["wifi"] >= (15 if self.wifi else 60):      # netsh costs ~150 ms of CPU per call, so ask rarely; a cable PC almost never
                w = wifi_info()
                with self.lock:
                    self.wifi = w
                last["wifi"] = time.time()
            if now - last["vpn"] >= 60:
                v = vpn_adapters()
                with self.lock:
                    self.vpn = v
                last["vpn"] = time.time()
            time.sleep(1)


NET = None                                    # created below, once the helpers it needs exist


# ---------------------------------------------------------------- backup / restore / reset
BACKUP_APP = "tools-manager-backup"
MAX_BACKUP_BYTES = 64 * 1024 * 1024          # upload size
MAX_UNZIPPED_BYTES = 128 * 1024 * 1024       # guard against zip bombs
ICON_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.(png|jpg|gif|webp|svg|ico)$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ---- alert sounds: one optional audio file per alert type (data/sounds/<kind>.<ext>)
SOUND_KINDS = ("down", "warn", "up", "reminder")
SOUND_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".flac": "audio/flac"}
MAX_SOUND_BYTES = 3 * 1024 * 1024


def sniff_sound(data):
    """What audio format these bytes really are (by content, never by file name), or None."""
    if len(data) < 64:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:3] == b"ID3" or (data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return ".mp3"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[4:8] == b"ftyp":
        return ".m4a"
    if data[:4] == b"fLaC":
        return ".flac"
    return None


def find_sound(kind):
    for ext in SOUND_TYPES:
        p = os.path.join(SOUND_DIR, kind + ext)
        if kind in SOUND_KINDS and os.path.isfile(p):
            return p
    return None


def remove_sound(kind):
    for ext in SOUND_TYPES:
        try:
            os.remove(os.path.join(SOUND_DIR, kind + ext))
        except OSError:
            pass


def save_sound(kind, data):
    if kind not in SOUND_KINDS:
        raise ValueError("Unknown alert type")
    if len(data) > MAX_SOUND_BYTES:
        raise ValueError("That sound file is too big. Please use one under 3 MB.")
    ext = sniff_sound(data)
    if not ext:
        raise ValueError("Please choose a WAV, MP3, OGG, M4A or FLAC audio file (up to 3 MB)")
    os.makedirs(SOUND_DIR, exist_ok=True)
    remove_sound(kind)
    with open(os.path.join(SOUND_DIR, kind + ext), "wb") as f:
        f.write(data)


def sounds_info():
    out = {}
    for k in SOUND_KINDS:
        p = find_sound(k)
        out[k] = {"custom": bool(p), "ext": os.path.splitext(p)[1][1:] if p else "",
                  "size": os.path.getsize(p) if p else 0, "v": int(os.path.getmtime(p)) if p else 0}
    return out


# ---- desktop background picture (one file, data/background.<ext>)
BG_BASE = os.path.join(DATA_DIR, "background")
BG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_BG_BYTES = 15 * 1024 * 1024


def find_bg():
    for ext in BG_TYPES:
        if os.path.isfile(BG_BASE + ext):
            return BG_BASE + ext
    return None


def remove_bg():
    for ext in BG_TYPES:
        try:
            os.remove(BG_BASE + ext)
        except OSError:
            pass


def save_bg(data, ext=None):
    """Store a picture as the background. Only real PNG/JPG/GIF/WEBP data is accepted (checked by content)."""
    ext = ext or sniff_ext(data, "")
    if ext not in BG_TYPES or len(data) > MAX_BG_BYTES or len(data) < 100:
        raise ValueError("Please choose a PNG, JPG, GIF or WEBP picture (up to 15 MB)")
    os.makedirs(DATA_DIR, exist_ok=True)
    remove_bg()
    with open(BG_BASE + ext, "wb") as f:
        f.write(data)


# ---- notes: a small to-do list where every note has a status and a log (data/notes.json)
NOTES_FILE = os.path.join(DATA_DIR, "notes.json")
NOTE_LABEL = {"todo": "To do", "doing": "In progress", "waiting": "Waiting", "done": "Done"}
MAX_LOG_ENTRIES = 1000


def load_notes():
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")   # local time with its UTC offset


def clean_title(raw):
    return " ".join(str(raw or "").split())[:200]


def clean_text(raw):
    return str(raw or "").strip()[:2000]


def log_entry(text, system=False, when=None):
    return {"id": uuid.uuid4().hex[:8], "t": when or now_iso(), "text": text, "sys": system}


DUE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?$")


def clean_due(raw):
    """A due date / event start as 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (your PC's local time). '' means none."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    m = DUE_RE.match(raw)
    if not m:
        raise ValueError("Please give the date and time like 05-Nov-2026 5:00 PM")
    try:
        datetime.datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0), int(m[5] or 0))
    except ValueError:
        raise ValueError("That date or time does not exist")
    return raw


def _safe_due(raw):
    try:
        return clean_due(raw)
    except ValueError:
        return ""


def clean_notes(raw):
    """Validate the notes found in a backup. Returns a clean list or raises ValueError."""
    if not isinstance(raw, list) or len(raw) > 2000:
        raise ValueError("Backup contents are not valid")
    out, used = [], set()
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("Backup contents are not valid")
        nid = r.get("id") if isinstance(r.get("id"), str) and ID_RE.match(r["id"]) and r["id"] not in used \
            else uuid.uuid4().hex[:12]
        used.add(nid)
        log = []
        for e in (r.get("log") if isinstance(r.get("log"), list) else [])[:MAX_LOG_ENTRIES]:
            if isinstance(e, dict) and clean_text(e.get("text")):
                log.append(log_entry(clean_text(e.get("text")), bool(e.get("sys")), str(e.get("t") or "")[:40] or now_iso()))
        out.append({
            "id": nid, "title": clean_title(r.get("title")) or "Untitled",
            "status": r.get("status") if r.get("status") in NOTE_LABEL else "todo",
            "created": str(r.get("created") or now_iso())[:40], "updated": str(r.get("updated") or now_iso())[:40],
            "due": _safe_due(r.get("due")), "link": r.get("link") if isinstance(r.get("link"), str) and ID_RE.match(r["link"]) else "",
            "log": log,
        })
    return out


# ---- commands: groups of copy-able CLI commands
def _new_id(raw, used):
    i = raw if isinstance(raw, str) and ID_RE.match(raw) and raw not in used else uuid.uuid4().hex[:12]
    used.add(i)
    return i


def clean_commands(raw):
    """Validate command groups (from the page or from a backup). Returns a clean list or raises ValueError."""
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError("Commands are not valid")
    out, used, names = [], set(), set()
    for g in raw:
        if not isinstance(g, dict):
            raise ValueError("Commands are not valid")
        name = " ".join(str(g.get("name") or "").split())[:60]
        if not name:
            raise ValueError("Every command group needs a name")
        if name.lower() in names:
            raise ValueError(f'Two groups are called "{name}"')
        names.add(name.lower())
        raw_items = g.get("items") if isinstance(g.get("items"), list) else []
        if len(raw_items) > 500:
            raise ValueError("Too many commands in one group (limit 500)")
        items = []
        for it in raw_items:
            if not isinstance(it, dict):
                raise ValueError("Commands are not valid")
            text = str(it.get("text") or "").replace("\r\n", "\n").strip()[:2000]
            if not text:
                continue
            items.append({"id": _new_id(it.get("id"), used), "label": " ".join(str(it.get("label") or "").split())[:120], "text": text})
        out.append({"id": _new_id(g.get("id"), used), "name": name, "items": items})
    return out


def load_commands():
    try:
        with open(COMMANDS_FILE, encoding="utf-8") as f:
            return clean_commands(json.load(f).get("groups", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
        return []


def starter_commands():
    import starter_commands as sc
    return clean_commands([{"name": n, "items": [{"label": l, "text": t} for l, t in rows]} for n, rows in sc.GROUPS])


def clean_events(raw):
    """Validate calendar events (from the page or a backup). Returns a clean list or raises ValueError."""
    if not isinstance(raw, list) or len(raw) > 2000:
        raise ValueError("Events are not valid")
    out, used = [], set()
    for r in raw:
        if not isinstance(r, dict):
            raise ValueError("Events are not valid")
        title = " ".join(str(r.get("title") or "").split())[:120]
        if not title:
            raise ValueError("Every event needs a title")
        start = clean_due(r.get("start"))
        if not start:
            raise ValueError(f'Event "{title}" needs a date and time')
        try:
            minutes = max(0, min(10080, int(r.get("minutes") or 0)))
        except (TypeError, ValueError):
            minutes = 0
        out.append({"id": _new_id(r.get("id"), used), "title": title, "start": start if "T" in start else start + "T00:00", "minutes": minutes})
    return out


def load_events():
    try:
        with open(EVENTS_FILE, encoding="utf-8") as f:
            return clean_events(json.load(f).get("events", []))
    except (FileNotFoundError, json.JSONDecodeError, ValueError, AttributeError):
        return []


def build_backup():
    with db_lock:
        items, cats, notes, dividers = load_db(), load_cats(), load_notes(), load_dividers()
        commands, events = load_commands(), load_events()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "app": BACKUP_APP, "version": 1, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "shortcuts": len(items), "categories": len(cats)}, indent=2))
        z.writestr("shortcuts.json", json.dumps(items, indent=2))
        z.writestr("categories.json", json.dumps(cats, indent=2))
        z.writestr("notes.json", json.dumps(notes, indent=2))
        z.writestr("menu.json", json.dumps({"dividers": dividers}, indent=2))
        z.writestr("commands.json", json.dumps({"groups": commands}, indent=2))
        z.writestr("events.json", json.dumps({"events": events}, indent=2))
        for it in items:
            icon = it.get("icon")
            if icon and ICON_NAME_RE.match(icon) and os.path.isfile(os.path.join(ICON_DIR, icon)):
                z.write(os.path.join(ICON_DIR, icon), "icons/" + icon)
        bg = find_bg()
        if bg:
            z.write(bg, "background" + os.path.splitext(bg)[1])
        for k in SOUND_KINDS:
            sp = find_sound(k)
            if sp:
                z.write(sp, "sounds/" + k + os.path.splitext(sp)[1])
    return buf.getvalue()


def parse_backup(data):
    """Validate a backup ZIP completely and return (items, categories, {icon filename: bytes}, background, notes).
    background is (extension, bytes) or None; notes is a clean list or None (backup has no notes). Touches nothing."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That file is not a valid ZIP archive")
    names = set(z.namelist())
    if not {"manifest.json", "shortcuts.json"} <= names:
        raise ValueError("This ZIP is not a Tools Manager backup")
    if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED_BYTES:
        raise ValueError("Backup is too large")
    if json.loads(z.read("manifest.json")).get("app") != BACKUP_APP:
        raise ValueError("This ZIP is not a Tools Manager backup")
    raw_items = json.loads(z.read("shortcuts.json"))
    raw_cats = json.loads(z.read("categories.json")) if "categories.json" in names else []
    if not isinstance(raw_items, list) or not isinstance(raw_cats, list) or len(raw_items) > 5000:
        raise ValueError("Backup contents are not valid")

    items, icons, used_ids = [], {}, set()
    for r in raw_items:
        if not isinstance(r, dict):
            raise ValueError("Backup contents are not valid")
        kind = "bat" if r.get("type") == "bat" else "url"
        url = str(r.get("url") or "").strip()
        url = normalize_url(url) if url else ""             # rejects non-http(s) schemes
        path = str(r.get("path") or "").strip() if kind == "bat" else ""
        name = " ".join(str(r.get("name") or "").split())[:200]
        if (kind == "url" and not url) or (kind == "bat" and not path):
            raise ValueError(f'Shortcut "{name or "?"}" is incomplete')
        sid = r.get("id") if isinstance(r.get("id"), str) and ID_RE.match(r["id"]) and r["id"] not in used_ids \
            else uuid.uuid4().hex[:12]
        used_ids.add(sid)
        icon = None
        icon_name = r.get("icon")
        if isinstance(icon_name, str) and ICON_NAME_RE.match(icon_name) and "icons/" + icon_name in names:
            blob = z.read("icons/" + icon_name)
            ext = sniff_ext(blob, "") if len(blob) <= MAX_ICON_BYTES else None
            if ext:                                          # re-checked by content, not by the name in the zip
                icon = f"{sid}-{int(time.time())}{ext}"
                icons[icon] = blob
        items.append({
            "id": sid, "type": kind, "name": name or url or os.path.basename(path), "url": url, "path": path,
            "hidden": bool(r.get("hidden")), "desktop": bool(r.get("desktop")),
            "dx": clamp(r.get("dx"), 0, lo=0, hi=200), "dy": clamp(r.get("dy"), 0, lo=0, hi=200),
            "width": clamp(r.get("width"), 1280), "height": clamp(r.get("height"), 800),
            "category": clean_cat(r.get("category")), "icon": icon,
        })
    cats = []
    for c in [*raw_cats, *(i["category"] for i in items)]:
        c = clean_cat(c)
        if c and c not in cats:
            cats.append(c)
    bg = None
    for ext in BG_TYPES:
        if "background" + ext in names:
            blob = z.read("background" + ext)
            real = sniff_ext(blob, "") if len(blob) <= MAX_BG_BYTES else None
            if real in BG_TYPES:                             # checked by content, not by the file name
                bg = (real, blob)
            break
    notes = clean_notes(json.loads(z.read("notes.json"))) if "notes.json" in names else None
    dividers = None
    if "menu.json" in names:
        raw = json.loads(z.read("menu.json"))
        if isinstance(raw, dict) and isinstance(raw.get("dividers"), list):
            dividers = [clean_cat(x) for x in raw["dividers"] if isinstance(x, str)]
    commands = None
    if "commands.json" in names:
        raw = json.loads(z.read("commands.json"))
        commands = clean_commands(raw.get("groups", []) if isinstance(raw, dict) else None)
    events = None
    if "events.json" in names:
        raw = json.loads(z.read("events.json"))
        events = clean_events(raw.get("events", []) if isinstance(raw, dict) else None)
    sounds = None
    found = {}
    for k in SOUND_KINDS:
        for ext in SOUND_TYPES:
            if f"sounds/{k}{ext}" in names:
                info = z.getinfo(f"sounds/{k}{ext}")
                blob = z.read(f"sounds/{k}{ext}") if info.file_size <= MAX_SOUND_BYTES else b""
                if sniff_sound(blob):                         # checked by content, not by the name in the zip
                    found[k] = blob
                break
    if found:
        sounds = found
    return items, cats, icons, bg, notes, dividers, commands, events, sounds


def wipe_icons():
    if os.path.isdir(ICON_DIR):
        for fn in os.listdir(ICON_DIR):
            try:
                os.remove(os.path.join(ICON_DIR, fn))
            except OSError:
                pass


def apply_backup(items, cats, icons, bg=None, notes=None, dividers=None, commands=None, events=None, sounds=None):
    with db_lock:
        wipe_icons()
        os.makedirs(ICON_DIR, exist_ok=True)
        for fn, blob in icons.items():
            with open(os.path.join(ICON_DIR, fn), "wb") as f:
                f.write(blob)
        write_json(DB_FILE, items)
        write_json(CAT_FILE, cats)
        save_dividers(dividers if dividers is not None else load_dividers(), cats)   # only those that match the restored categories
        if bg:                       # a backup without a background leaves your current background alone
            save_bg(bg[1], bg[0])
        if notes is not None:        # same for notes: an older backup without them keeps the notes you have
            write_json(NOTES_FILE, notes)
        if commands is not None:     # and for commands
            write_json(COMMANDS_FILE, {"groups": commands})
        if events is not None:       # and for calendar events
            write_json(EVENTS_FILE, {"events": events})
        for k, blob in (sounds or {}).items():   # sounds the backup has replace yours; the ones it lacks are left alone
            save_sound(k, blob)


def factory_reset():
    with db_lock:
        wipe_icons()
        remove_bg()
        for k in SOUND_KINDS:
            remove_sound(k)
        NET.reset_config()                        # outage log and extra targets
        for p in (DB_FILE, CAT_FILE, NOTES_FILE, MENU_FILE, COMMANDS_FILE, EVENTS_FILE):
            if os.path.exists(p):
                os.remove(p)


NET = NetMonitor()


# ---------------------------------------------------------------- updates (button only: nothing is contacted until you click)
VERSION_FILE = os.path.join(ROOT, "VERSION")
UPDATE_API = os.environ.get("TOOLS_UPDATE_API") or "https://api.github.com/repos/dharmesp/nettools/releases/latest"
UPDATE_ZIP = os.environ.get("TOOLS_UPDATE_ZIP") or "https://github.com/dharmesp/nettools/archive/refs/tags/{tag}.zip"
UPDATE_TESTING = bool(os.environ.get("TOOLS_UPDATE_API"))      # lets the self-tests use a local fake release server
BACKUP_ROOT = os.path.join(ROOT, "update-backup")
# an update may only replace these (never data/, .venv/ or anything else)
UPDATE_ROOT_FILES = {"server.py", "donate.py", "starter_commands.py", "install.bat", "run.bat", "requirements.txt", "README.md",
                     "readme-marketing.md", "LICENSE", "VERSION", ".gitignore", ".gitattributes"}
UPDATE_DIRS = ("static/", "docs/", "sample-data/")
MAX_UPDATE_BYTES = 40 * 1024 * 1024
MAX_UPDATE_UNZIPPED = 120 * 1024 * 1024
TAG_RE = re.compile(r"^[vV]?\d+(\.\d+){1,3}$")
update_lock = threading.Lock()


def current_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        return v if TAG_RE.match(v) else "0"
    except OSError:
        return "0"


def ver_tuple(v):
    return tuple(int(x) for x in str(v).lstrip("vV").split("."))


def update_fetch(url, limit):
    """Download over HTTPS with the certificate checked (never the relaxed check used for icons: this is code)."""
    if not url.startswith("https://") and not (UPDATE_TESTING and url.startswith("http://127.0.0.1")):
        raise ValueError("Updates are only downloaded over HTTPS")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json, */*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError("No release was found on GitHub yet")
        if e.code in (403, 429):
            raise ValueError("GitHub is limiting requests right now. Please try again in a few minutes.")
        raise ValueError(f"GitHub answered with error {e.code}")
    except (urllib.error.URLError, OSError):
        raise ValueError("Could not reach GitHub. Your network or proxy may block it; you can update by hand from the Releases page.")
    if len(data) > limit:
        raise ValueError("The download is larger than expected, so it was refused")
    return data


def update_check():
    cur = current_version()
    info = {"current": cur, "git": os.path.isdir(os.path.join(ROOT, ".git")), "releases": donate.REPO_URL + "/releases"}
    try:
        rel = json.loads(update_fetch(UPDATE_API, 1 << 20))
    except json.JSONDecodeError:
        raise ValueError("GitHub gave an answer that could not be read")
    tag = str(rel.get("tag_name") or "")
    if not TAG_RE.match(tag):
        raise ValueError("The latest release has an unexpected name")
    info.update(tag=tag, latest=tag.lstrip("vV"), notes=str(rel.get("body") or "")[:3000], newer=ver_tuple(tag) > ver_tuple(cur))
    return info


def parse_update_zip(blob, latest):
    """Validate a release ZIP completely and return {relative path: bytes} of the files an update may replace. Touches nothing."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ValueError("The downloaded file is not a valid ZIP")
    infos = [i for i in z.infolist() if not i.is_dir()]
    if not infos or sum(i.file_size for i in infos) > MAX_UPDATE_UNZIPPED:
        raise ValueError("The downloaded update is empty or too large")
    top = infos[0].filename.split("/")[0]
    files = {}
    for i in infos:
        name = i.filename
        if not name.startswith(top + "/"):
            raise ValueError("The downloaded update has an unexpected layout")
        rel = name[len(top) + 1:]
        if "\\" in rel or ":" in rel or rel.startswith("/") or any(p in ("", ".", "..") for p in rel.split("/")):
            raise ValueError("The downloaded update contains an unsafe file name, so nothing was changed")
        if rel in UPDATE_ROOT_FILES or rel.startswith(UPDATE_DIRS):
            if rel in files:
                raise ValueError("The downloaded update lists the same file twice, so nothing was changed")
            files[rel] = z.read(i)
    for need in ("server.py", "static/index.html", "VERSION"):
        if need not in files:
            raise ValueError(f"The downloaded update is incomplete ({need} is missing), so nothing was changed")
    try:
        got = files["VERSION"].decode("utf-8").strip()
        if not TAG_RE.match(got) or ver_tuple(got) != ver_tuple(latest):
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        raise ValueError("The downloaded update does not match the release it was published as, so nothing was changed")
    try:
        compile(files["server.py"], "server.py", "exec")           # never install a server that cannot even start
    except (SyntaxError, ValueError):
        raise ValueError("The downloaded server code is damaged, so nothing was changed")
    if b"<html" not in files["static/index.html"][:2000].lower():
        raise ValueError("The downloaded page is damaged, so nothing was changed")
    return files


def backup_code(files):
    """Copy the files an update will replace into update-backup/<old version>-<time>/ ; keep the newest three."""
    dest = os.path.join(BACKUP_ROOT, f"{time.strftime('%Y%m%d-%H%M%S')}-v{current_version()}")      # time first, so the names sort oldest to newest
    for rel in files:
        src = os.path.join(ROOT, *rel.split("/"))
        if os.path.isfile(src):
            out = os.path.join(dest, *rel.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(src, out)
    os.makedirs(dest, exist_ok=True)
    keep = sorted(d for d in os.listdir(BACKUP_ROOT) if os.path.isdir(os.path.join(BACKUP_ROOT, d)))
    for old in keep[:-3]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, old), ignore_errors=True)
    return dest


def swap_files(files):
    """Write the new files. If anything fails part-way, every file already changed is put back."""
    done = []
    try:
        for rel in sorted(files):
            target = os.path.join(ROOT, *rel.split("/"))
            old = None
            if os.path.isfile(target):
                with open(target, "rb") as f:
                    old = f.read()
                if old == files[rel]:
                    continue                                 # unchanged: leave it alone (keeps the big starter ZIP from being rewritten)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            tmp = target + ".update-tmp"
            with open(tmp, "wb") as f:
                f.write(files[rel])
            try:
                os.replace(tmp, target)
            except OSError:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            done.append((target, old))
    except Exception:
        for target, old in reversed(done):
            try:
                if old is None:
                    os.remove(target)
                else:
                    with open(target, "wb") as f:
                        f.write(old)
            except OSError:
                pass
        raise
    return len(done)


def schedule_restart():
    """Start the new server (it waits for this one to let go of the port), then stop this one."""
    def work():
        time.sleep(0.6)                                       # let the answer reach the page first
        subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), "--no-open", "--wait"], cwd=ROOT,
                         creationflags=0x00000008 | 0x00000200, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if HTTPD:
            HTTPD.shutdown()
    threading.Thread(target=work, daemon=True).start()


def update_apply():
    with update_lock:
        if os.path.isdir(os.path.join(ROOT, ".git")):
            raise ValueError("This copy was installed with git. Update it by running 'git pull' in its folder.")
        info = update_check()
        if not info["newer"]:
            raise ValueError("You already have the latest version")
        files = parse_update_zip(update_fetch(UPDATE_ZIP.format(tag=info["tag"]), MAX_UPDATE_BYTES), info["latest"])
        backup = backup_code(files)
        try:
            changed = swap_files(files)
        except Exception as e:
            raise ValueError(f"The update could not be installed, so your old version was put back ({type(e).__name__}).")
        schedule_restart()
        return {"ok": True, "from": info["current"], "to": info["latest"], "files": changed, "backup": os.path.relpath(backup, ROOT)}


def public(item):
    out = dict(item)
    out["icon"] = f"/icons/{item['icon']}" if item.get("icon") else None
    return out


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "ToolsManager/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wend(body)

    def wend(self, body):
        try:
            self.wfile.write(body)
        except (ConnectionError, OSError):
            pass

    def guard(self):
        """Block DNS-rebinding and cross-site requests to the local API."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            self.send_json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if self.command != "GET" and origin and urllib.parse.urlparse(origin).netloc not in ALLOWED_HOSTS:
            self.send_json({"error": "bad origin"}, 403)
            return False
        return True

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def serve_file(self, base, rel):
        path = os.path.normpath(os.path.join(base, rel))
        if not path.startswith(base + os.sep) or not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if base == ICON_DIR:                       # icons are images only: never let one run as a page
            self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wend(body)

    def read_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("File is too large")
        return self.rfile.read(n)

    # -- routing
    def do_GET(self):
        if not self.guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/shortcuts":
            with db_lock:
                return self.send_json([public(i) for i in load_db()])
        if path == "/api/categories":
            with db_lock:
                return self.send_json(load_cats())
        if path == "/api/donate":
            return self.send_json({"paypal": donate.PAYPAL_URL, "repo": donate.REPO_URL})
        if path == "/donate-qr.png":               # compiled in (donate.py), not read from disk
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(donate.QR_PNG)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return self.wend(donate.QR_PNG)
        if path == "/api/notes":
            with db_lock:
                return self.send_json(load_notes())
        if path == "/api/version":
            return self.send_json({"version": current_version()})
        if path == "/api/menu":
            with db_lock:
                return self.send_json({"dividers": load_dividers()})
        if path == "/api/commands":
            with db_lock:
                return self.send_json({"groups": load_commands()})
        if path == "/api/events":
            with db_lock:
                return self.send_json({"events": load_events()})
        if path == "/api/commands/starter":
            return self.send_json({"groups": starter_commands()})
        if path == "/api/net":
            NET.touch()                                  # starts the pings if they are not running; they stop on their own when nobody asks
            return self.send_json(NET.snapshot())
        if path == "/api/net/history":
            NET.touch()
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                rng = int((q.get("range") or ["3600"])[0])
            except ValueError:
                rng = 3600
            return self.send_json(NET.history(rng))
        if path == "/api/net/outages":
            return self.send_json(NET.outage_list())
        if path == "/api/net/targets":
            return self.send_json({"targets": NET.targets})
        if path == "/api/sounds":
            with db_lock:
                return self.send_json(sounds_info())
        if path.startswith("/api/sound/"):
            p = find_sound(urllib.parse.unquote(path[len("/api/sound/"):]))
            if not p:
                return self.send_json({"error": "not found"}, 404)
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", SOUND_TYPES[os.path.splitext(p)[1]])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wend(body)
        if path == "/api/background/info":
            p = find_bg()
            return self.send_json({"set": bool(p), "v": int(os.path.getmtime(p)) if p else 0})
        if path == "/api/background":
            p = find_bg()
            if not p:
                return self.send_json({"error": "not found"}, 404)
            with open(p, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", BG_TYPES[os.path.splitext(p)[1]])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Security-Policy", "sandbox")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return self.wend(body)
        if path == "/api/export":
            body = build_backup()
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="tools-manager-backup-{stamp}.zip"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wend(body)
        if path.startswith("/icons/"):
            return self.serve_file(ICON_DIR, urllib.parse.unquote(path[7:]))
        rel = "index.html" if path == "/" else urllib.parse.unquote(path.lstrip("/"))
        self.serve_file(STATIC_DIR, rel)

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        if not self.guard():
            return
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        try:
            if parts == ["api", "import"] and method == "POST":
                try:
                    items, cats, icons, bg, notes, dividers, commands, events, sounds = parse_backup(self.read_body(MAX_BACKUP_BYTES))   # validate everything first
                except (zipfile.BadZipFile, KeyError, TypeError, AttributeError, OverflowError, RuntimeError):
                    raise ValueError("The backup file is damaged or not valid")
                apply_backup(items, cats, icons, bg, notes, dividers, commands, events, sounds)
                return self.send_json({"shortcuts": len(items), "categories": len(cats), "background": bool(bg),
                                       "notes": None if notes is None else len(notes)})
            if parts == ["api", "category-order"] and method == "PUT":     # drag-and-drop: the whole new order
                order = self.read_json().get("order")
                with db_lock:
                    cats = load_cats()
                    if (not isinstance(order, list) or not all(isinstance(c, str) for c in order)
                            or len(set(order)) != len(order) or sorted(order) != sorted(cats)):
                        raise ValueError("The new order does not match your categories")
                    write_json(CAT_FILE, order)
                return self.send_json(order)
            if parts == ["api", "events"] and method == "PUT":              # save all calendar events at once
                body = json.loads(self.read_body(2 * 1024 * 1024) or b"{}")
                events = clean_events(body.get("events") if isinstance(body, dict) else None)
                with db_lock:
                    write_json(EVENTS_FILE, {"events": events})
                return self.send_json({"events": events})
            if parts == ["api", "commands"] and method == "PUT":            # save all command groups at once
                body = json.loads(self.read_body(2 * 1024 * 1024) or b"{}")          # bigger than the usual 64 KB: it holds every command
                groups = clean_commands(body.get("groups") if isinstance(body, dict) else None)
                with db_lock:
                    write_json(COMMANDS_FILE, {"groups": groups})
                return self.send_json({"groups": groups})
            if parts == ["api", "net", "targets"] and method == "PUT":      # the extra hosts to watch
                body = self.read_json()
                targets = clean_targets(body.get("targets"))
                NET.set_targets(targets)
                return self.send_json({"targets": targets})
            if parts == ["api", "net", "outages"] and method == "DELETE":
                NET.clear_outages()
                return self.send_json({"ok": True})
            if parts == ["api", "update", "check"] and method == "POST":    # only when you click the button
                return self.send_json(update_check())
            if parts == ["api", "update", "apply"] and method == "POST":
                return self.send_json(update_apply())
            if parts == ["api", "menu"] and method == "PUT":                # dividers
                d = self.read_json().get("dividers")
                if not isinstance(d, list):
                    raise ValueError("dividers must be a list")
                with db_lock:
                    return self.send_json({"dividers": save_dividers([x for x in d if isinstance(x, str)])})
            if parts[:2] == ["api", "notes"]:
                return self.notes_api(method, parts[2:])
            if len(parts) == 3 and parts[:2] == ["api", "sound"] and method in ("POST", "DELETE"):
                kind = urllib.parse.unquote(parts[2])
                if kind not in SOUND_KINDS:
                    raise ValueError("Unknown alert type")
                with db_lock:
                    if method == "POST":
                        save_sound(kind, self.read_body(MAX_SOUND_BYTES * 3))   # read a bit more than allowed so a too-big file gets a clear message
                    else:
                        remove_sound(kind)
                return self.send_json(sounds_info()[kind])
            if parts == ["api", "background"] and method == "POST":
                with db_lock:
                    save_bg(self.read_body(MAX_BG_BYTES))
                return self.send_json({"ok": True})
            if parts == ["api", "background"] and method == "DELETE":
                with db_lock:
                    remove_bg()
                return self.send_json({"ok": True})
            if parts == ["api", "reset"] and method == "POST":
                factory_reset()
                return self.send_json({"ok": True})
            if parts[:2] == ["api", "hello"] and method == "POST":
                with pages_lock:
                    open_pages.add(str(self.read_json().get("id", ""))[:64])
                return self.send_json({"ok": True})
            if parts[:2] == ["api", "bye"] and method == "POST":
                with pages_lock:
                    open_pages.discard(str(self.read_json().get("id", ""))[:64])
                page_closed()
                return self.send_json({"ok": True})
            if parts[:2] == ["api", "shutdown"] and method == "POST":
                self.send_json({"ok": True})
                return power_off()
            if parts[:2] == ["api", "categories"]:
                return self.categories(method, urllib.parse.unquote(parts[2]) if len(parts) > 2 else None)
            if parts[:2] != ["api", "shortcuts"]:
                return self.send_json({"error": "not found"}, 404)
            sid = parts[2] if len(parts) > 2 else None
            action = parts[3] if len(parts) > 3 else None
            if method == "POST" and sid is None:
                return self.create()
            if method == "PUT" and sid and action is None:
                return self.update(sid)
            if method == "DELETE" and sid and action is None:
                return self.delete(sid)
            if method == "POST" and sid and action == "launch":
                return self.launch(sid)
            if method == "POST" and sid and action == "refresh-icon":
                return self.refresh_icon(sid)
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # keep the server alive on any bug
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    # -- actions
    def notes_api(self, method, rest):
        with db_lock:
            notes = load_notes()
            if method == "POST" and not rest:                                 # new note
                d = self.read_json()
                title = clean_title(d.get("title"))
                if not title:
                    raise ValueError("Please type a title for the note")
                t = now_iso()
                n = {"id": uuid.uuid4().hex[:12], "title": title,
                     "status": d.get("status") if d.get("status") in NOTE_LABEL else "todo",
                     "created": t, "updated": t, "due": clean_due(d.get("due")), "link": "",
                     "log": [log_entry("Note created", True, t)]}
                notes.append(n)
                write_json(NOTES_FILE, notes)
                return self.send_json(n, 201)
            n = next((x for x in notes if x["id"] == (rest[0] if rest else "")), None)
            if n is None:
                return self.send_json({"error": "not found"}, 404)
            t = now_iso()
            if method == "PUT" and len(rest) == 1:                           # rename / change status
                d = self.read_json()
                if "title" in d:
                    title = clean_title(d["title"])
                    if not title:
                        raise ValueError("The title can't be empty")
                    n["title"] = title
                if "status" in d:
                    if d["status"] not in NOTE_LABEL:
                        raise ValueError("Unknown status")
                    if d["status"] != n["status"]:                           # every status change is written to the log
                        n["log"].append(log_entry(f"Status: {NOTE_LABEL[n['status']]} → {NOTE_LABEL[d['status']]}", True, t))
                        n["status"] = d["status"]
                if "due" in d:
                    due = clean_due(d["due"])
                    if due != n.get("due", ""):                              # a changed due date is written to the log too
                        n["log"].append(log_entry(("Due: " + due.replace("T", " ")) if due else "Due date removed", True, t))
                        n["due"] = due
                if "link" in d:
                    link = str(d["link"] or "")
                    if link and not ID_RE.match(link):
                        raise ValueError("Unknown tool")
                    n["link"] = link
                n["updated"] = t
            elif method == "POST" and len(rest) == 2 and rest[1] == "log":   # add a log line
                text = clean_text(self.read_json().get("text"))
                if not text:
                    raise ValueError("Type something to add to the log")
                if len(n["log"]) >= MAX_LOG_ENTRIES:
                    raise ValueError("This note has too many log entries")
                n["log"].append(log_entry(text, False, t))
                n["updated"] = t
            elif method == "DELETE" and len(rest) == 3 and rest[1] == "log":   # remove one log line
                n["log"] = [e for e in n["log"] if e["id"] != rest[2]]
                n["updated"] = t
            elif method == "DELETE" and len(rest) == 1:                      # delete the whole note
                write_json(NOTES_FILE, [x for x in notes if x is not n])
                return self.send_json({"ok": True})
            else:
                return self.send_json({"error": "not found"}, 404)
            write_json(NOTES_FILE, notes)
            return self.send_json(n, 201 if method == "POST" else 200)

    def categories(self, method, name):
        with db_lock:
            cats = load_cats()
            if method == "POST" and name is None:
                new = clean_cat(self.read_json().get("name"))
                if not new:
                    raise ValueError("Category name is required")
                ensure_category(new)
                return self.send_json(load_cats(), 201)
            if name not in cats:
                return self.send_json({"error": "not found"}, 404)
            if method == "PUT":
                new = clean_cat(self.read_json().get("name"))
                if not new:
                    raise ValueError("Category name is required")
                if new != name and new in cats:
                    raise ValueError("A category with that name already exists")
                items = load_db()
                for i in items:
                    if i.get("category") == name:
                        i["category"] = new
                save_db(items)
                renamed = [new if c == name else c for c in cats]
                write_json(CAT_FILE, renamed)
                save_dividers([new if d == name else d for d in load_dividers()], renamed)   # the line stays with the category
                return self.send_json(load_cats())
            if method == "DELETE":  # shortcuts fall back to "Uncategorized"
                items = load_db()
                for i in items:
                    if i.get("category") == name:
                        i["category"] = ""
                save_db(items)
                remaining = [c for c in cats if c != name]
                write_json(CAT_FILE, remaining)
                save_dividers(load_dividers(), remaining)
                return self.send_json(load_cats())
        self.send_json({"error": "not found"}, 404)

    def create(self):
        d = self.read_json()
        kind = "bat" if d.get("type") == "bat" else "url"
        url, path = (d.get("url") or "").strip(), ""
        if kind == "bat":
            path = validate_bat(d.get("path"))
            url = normalize_url(url) if url else ""  # optional: window to open once the tool is up
        else:
            url = normalize_url(url)
        sid = uuid.uuid4().hex[:12]
        title, candidates = discover(url) if url else ("", [])
        if (d.get("icon_url") or "").strip():
            candidates.insert(0, icon_source(d["icon_url"]))
        fallback = os.path.splitext(os.path.basename(path))[0] if kind == "bat" else urllib.parse.urlparse(url).netloc
        item = {
            "id": sid,
            "type": kind,
            "name": (d.get("name") or "").strip() or title or fallback,
            "url": url,
            "path": path,
            "hidden": bool(d.get("hidden")),
            "desktop": False,
            "dx": 0,
            "dy": 0,
            "width": clamp(d.get("width"), 1280),
            "height": clamp(d.get("height"), 800),
            "category": clean_cat(d.get("category")),
            "icon": fetch_icon(candidates, sid),
        }
        with db_lock:
            ensure_category(item["category"])
            items = load_db()
            items.append(item)
            save_db(items)
        self.send_json(public(item), 201)

    def update(self, sid):
        d = self.read_json()
        with db_lock:
            items = load_db()
            item = next((i for i in items if i["id"] == sid), None)
            if not item:
                return self.send_json({"error": "not found"}, 404)
            kind = item.get("type", "url")
            url_changed = False
            if "url" in d:
                raw = (d["url"] or "").strip()
                new_url = normalize_url(raw) if raw or kind == "url" else ""
                url_changed = bool(new_url) and new_url != item["url"]
                item["url"] = new_url
            if kind == "bat" and "path" in d:
                item["path"] = validate_bat(d["path"])
            if "hidden" in d:
                item["hidden"] = bool(d["hidden"])
            if "desktop" in d:  # shown as an icon on the desktop at grid cell (dx, dy)
                item["desktop"] = bool(d["desktop"])
            for k in ("dx", "dy"):
                if k in d:
                    item[k] = clamp(d[k], item.get(k, 0), lo=0, hi=200)
            if (d.get("name") or "").strip():
                item["name"] = d["name"].strip()
            item["width"] = clamp(d.get("width"), item["width"])
            item["height"] = clamp(d.get("height"), item["height"])
            if "category" in d:
                item["category"] = clean_cat(d["category"])
                ensure_category(item["category"])
            icon_url = (d.get("icon_url") or "").strip()
            if icon_url or url_changed:
                cands = [icon_source(icon_url)] if icon_url else []
                if url_changed:
                    cands += discover(item["url"])[1]
                name = fetch_icon(cands, sid)
                if name:
                    item["icon"] = name
            save_db(items)
            self.send_json(public(item))

    def delete(self, sid):
        with db_lock:
            items = load_db()
            save_db([i for i in items if i["id"] != sid])
        remove_icons(sid)
        self.send_json({"ok": True})

    def refresh_icon(self, sid):
        with db_lock:
            items = load_db()
            item = next((i for i in items if i["id"] == sid), None)
            if not item:
                return self.send_json({"error": "not found"}, 404)
            if not item["url"]:
                return self.send_json({"error": "No URL to fetch an icon from - set an icon image in Edit"}, 400)
            name = fetch_icon(discover(item["url"])[1], sid)
            if not name:
                return self.send_json({"error": "No icon could be found for this site"}, 404)
            item["icon"] = name
            save_db(items)
            self.send_json(public(item))

    def launch(self, sid):
        with db_lock:
            item = next((i for i in load_db() if i["id"] == sid), None)
        if not item:
            return self.send_json({"error": "not found"}, 404)
        url, w, h = item["url"], item["width"], item["height"]
        if item.get("type") == "bat":
            if url and reachable(url):  # tool already running: just open its window
                open_app_window(url, w, h)
            else:
                path = validate_bat(item.get("path"))
                run_bat(path, item.get("hidden"))
                if url:
                    threading.Thread(target=open_when_ready, args=(url, w, h), daemon=True).start()
        else:
            open_app_window(url, w, h)
        self.send_json({"ok": True})


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows reuse lets a 2nd server share the port, hiding "already running"


HTTPD = None
open_pages = set()          # ids of dashboard pages currently open (they say hello/bye)
pages_lock = threading.Lock()
WINDOW_TITLE = os.environ.get("TOOLS_WINDOW_TITLE", "Tools Manager")
CLOSE_WINDOW_PS = """
Add-Type @"
using System; using System.Runtime.InteropServices; using System.Text;
public class W {
  public delegate bool CB(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(CB cb, IntPtr l);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static void CloseByTitle(string prefix) {
    EnumWindows((h, l) => { var sb = new StringBuilder(256); GetWindowText(h, sb, 256);
      if (IsWindowVisible(h) && sb.ToString().StartsWith(prefix)) SendMessage(h, 0x10, IntPtr.Zero, IntPtr.Zero);
      return true; }, IntPtr.Zero);
  }
}
"@
[W]::CloseByTitle('%s')
"""


def close_dashboard_windows():
    """Politely close (WM_CLOSE) any window titled like the dashboard; tool popups have other titles."""
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", CLOSE_WINDOW_PS % WINDOW_TITLE],
                       creationflags=0x08000000, timeout=15, capture_output=True)  # CREATE_NO_WINDOW
    except Exception:
        pass


def power_off():
    """Close the dashboard window, then stop the server; run.bat's console ends with the process."""
    def work():
        time.sleep(0.4)             # let the HTTP response get out first
        close_dashboard_windows()
        if HTTPD:
            HTTPD.shutdown()
    threading.Thread(target=work, daemon=True).start()


def page_closed():
    """A dashboard page went away; if none comes back within a few seconds (not just a refresh), shut down."""
    def check():
        with pages_lock:
            if not open_pages:
                power_off()
    t = threading.Timer(3.0, check)
    t.daemon = True
    t.start()


def main():
    global HTTPD
    if sys.stdout is None or sys.stderr is None:   # started with pythonw (no console): keep a log file instead
        os.makedirs(DATA_DIR, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(DATA_DIR, "server.log"), "a", buffering=1, encoding="utf-8")
    httpd = None
    for attempt in range(60 if "--wait" in sys.argv else 1):      # --wait: we were started by an update; the old server is still closing
        try:
            httpd = Server((HOST, PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if httpd is None:
        print(f"Port {PORT} already in use - assuming the server is running; opening window.")
        if "--no-open" not in sys.argv:
            open_app_window(MAIN_URL, 1100, 760)
        return
    HTTPD = httpd
    print(f"Tools Manager {current_version()} running at {MAIN_URL}  (use Shut Down in the app, or close this window, to stop)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.5, open_app_window, (MAIN_URL, 1100, 760)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
    print("Tools Manager stopped.")


if __name__ == "__main__":
    main()
