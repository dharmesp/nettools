"""Tools Manager launcher: tiny local backend + app-mode (no address bar) window launcher.

Run:  python server.py [--no-open]
Stdlib only. Data lives in ./data (shortcuts.json + icons/).
"""
import datetime
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
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


# ---------------------------------------------------------------- backup / restore / reset
BACKUP_APP = "tools-manager-backup"
MAX_BACKUP_BYTES = 64 * 1024 * 1024          # upload size
MAX_UNZIPPED_BYTES = 128 * 1024 * 1024       # guard against zip bombs
ICON_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.(png|jpg|gif|webp|svg|ico)$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")


def build_backup():
    with db_lock:
        items, cats = load_db(), load_cats()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "app": BACKUP_APP, "version": 1, "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "shortcuts": len(items), "categories": len(cats)}, indent=2))
        z.writestr("shortcuts.json", json.dumps(items, indent=2))
        z.writestr("categories.json", json.dumps(cats, indent=2))
        for it in items:
            icon = it.get("icon")
            if icon and ICON_NAME_RE.match(icon) and os.path.isfile(os.path.join(ICON_DIR, icon)):
                z.write(os.path.join(ICON_DIR, icon), "icons/" + icon)
    return buf.getvalue()


def parse_backup(data):
    """Validate a backup ZIP completely and return (items, categories, {icon filename: bytes}). Touches nothing."""
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
    return items, cats, icons


def wipe_icons():
    if os.path.isdir(ICON_DIR):
        for fn in os.listdir(ICON_DIR):
            try:
                os.remove(os.path.join(ICON_DIR, fn))
            except OSError:
                pass


def apply_backup(items, cats, icons):
    with db_lock:
        wipe_icons()
        os.makedirs(ICON_DIR, exist_ok=True)
        for fn, blob in icons.items():
            with open(os.path.join(ICON_DIR, fn), "wb") as f:
                f.write(blob)
        write_json(DB_FILE, items)
        write_json(CAT_FILE, cats)


def factory_reset():
    with db_lock:
        wipe_icons()
        for p in (DB_FILE, CAT_FILE):
            if os.path.exists(p):
                os.remove(p)


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
                    items, cats, icons = parse_backup(self.read_body(MAX_BACKUP_BYTES))   # validate everything first
                except (zipfile.BadZipFile, KeyError, TypeError, AttributeError, OverflowError, RuntimeError):
                    raise ValueError("The backup file is damaged or not valid")
                apply_backup(items, cats, icons)
                return self.send_json({"shortcuts": len(items), "categories": len(cats)})
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
                write_json(CAT_FILE, [new if c == name else c for c in cats])
                return self.send_json(load_cats())
            if method == "DELETE":  # shortcuts fall back to "Uncategorized"
                items = load_db()
                for i in items:
                    if i.get("category") == name:
                        i["category"] = ""
                save_db(items)
                write_json(CAT_FILE, [c for c in cats if c != name])
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
    try:
        httpd = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} already in use - assuming the server is running; opening window.")
        if "--no-open" not in sys.argv:
            open_app_window(MAIN_URL, 1100, 760)
        return
    HTTPD = httpd
    print(f"Tools Manager running at {MAIN_URL}  (use Shut Down in the app, or close this window, to stop)")
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
