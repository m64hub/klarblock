"""KlarBlock - ein kostenloser Selbsthilfe-Blocker gegen Pornografie fuer Windows.

Aufruf:
    KlarBlock.exe            -> Einrichtung (beim ersten Start) bzw. Statusfenster
    KlarBlock.exe run        -> Hintergrundprozess (wird von der Aufgabenplanung gestartet)
    KlarBlock.exe update     -> Blockliste neu laden

Funktionsweise:
  * Gesperrte Domains werden in der Hosts-Datei auf 127.0.0.1 umgeleitet.
  * Der Hintergrundprozess lauscht auf 127.0.0.1:80/443. Jeder Aufruf einer
    gesperrten Seite landet dort, und es erscheint ein Vollbild-Popup mit den
    eigenen Gruenden, von denen einer abgetippt werden muss.
  * Tor & aehnliche Umgehungs-Tools: Download-Seiten gesperrt, Prozesse werden
    beendet, Installer im Download-Ordner geloescht.
  * Deaktivieren nur nach Wartezeit (Standard 24 h) und erneutem Abtippen.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP = "KlarBlock"
VERSION = "0.1.0"
IS_WIN = sys.platform == "win32"

# --------------------------------------------------------------------------- Pfade

def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v) if v else default


DATA_DIR = _env_path("KLARBLOCK_DATA", Path(os.environ.get("ProgramData", "C:/ProgramData")) / APP)
HOSTS_FILE = _env_path(
    "KLARBLOCK_HOSTS",
    Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "drivers" / "etc" / "hosts",
)
INSTALL_DIR = _env_path("KLARBLOCK_INSTALL", Path(os.environ.get("ProgramFiles", "C:/Program Files")) / APP)
CONFIG_FILE = DATA_DIR / "config.json"
BLOCKLIST_FILE = DATA_DIR / "blocklist.txt"
LOG_FILE = DATA_DIR / "versuche.log"

MARK_BEGIN = "# >>> KlarBlock BEGIN (nicht von Hand aendern) >>>"
MARK_END = "# <<< KlarBlock END <<<"
REDIRECT_IP = "127.0.0.1"
DOMAINS_PER_LINE = 9  # Windows verarbeitet grosse Hosts-Dateien so deutlich schneller
INSTANCE_PORT = 47821  # Sperre gegen doppelt laufende Hintergrundprozesse
DEFAULT_COOLDOWN_HOURS = 24
POPUP_COUNTDOWN_SECONDS = 30
POPUP_PAUSE_SECONDS = 20

# Gepflegte, frei lizenzierte Listen (Lizenz siehe README).
BLOCKLIST_SOURCES = [
    # StevenBlack/hosts, Erweiterung "porn" (MIT), ca. 75.000 Domains
    "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/porn-only/hosts",
]

# Umgehungs-Tools: Tor, Proxy-Seiten, Zensur-Umgehung, DNS-over-HTTPS-Server.
BYPASS_DOMAINS = [
    "torproject.org", "www.torproject.org", "dist.torproject.org", "archive.torproject.org",
    "bridges.torproject.org", "tb-manual.torproject.org", "support.torproject.org",
    "community.torproject.org", "gettor.torproject.org", "blog.torproject.org",
    "forum.torproject.org", "aus1.torproject.org", "aus2.torproject.org",
    "torbrowser.de", "tor.eff.org",
    "psiphon.ca", "www.psiphon.ca", "psiphon3.com", "www.psiphon3.com",
    "ultrasurf.us", "www.ultrasurf.us", "getlantern.org", "www.getlantern.org",
    "hide.me", "hidester.com", "www.hidester.com", "croxyproxy.com", "www.croxyproxy.com",
    "kproxy.com", "www.kproxy.com", "proxysite.com", "www.proxysite.com",
    "4everproxy.com", "www.4everproxy.com", "hidemyass.com", "www.hidemyass.com",
    "proxyium.com", "www.proxyium.com", "blockaway.net", "www.blockaway.net",
    "croxyproxy.rocks", "www.croxyproxy.rocks", "plainproxies.com", "proxyscrape.com",
    # DNS-over-HTTPS-Server, ueber die Browser die Hosts-Datei umgehen koennten
    "dns.google", "dns.google.com", "cloudflare-dns.com", "chrome.cloudflare-dns.com",
    "mozilla.cloudflare-dns.com", "one.one.one.one", "dns.quad9.net", "doh.opendns.com",
    "doh.cleanbrowsing.org", "dns.adguard.com", "dns.adguard-dns.com",
]

# Prozesse, die sofort beendet werden (Kleinschreibung).
BLOCKED_PROCESSES = {
    "tor.exe", "obfs4proxy.exe", "lyrebird.exe", "snowflake-client.exe", "conjure-client.exe",
    "psiphon3.exe", "psiphon.exe", "lantern.exe", "ultrasurf.exe",
}

# Dateien im Download-Ordner, die geloescht werden.
BLOCKED_DOWNLOAD_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"^tor-?browser.*", r"^torbrowser.*", r"^tor-win.*\.(zip|tar\.gz)$", r"^tor-expert-bundle.*",
        r"^psiphon.*\.exe$", r"^lantern.*\.exe$", r"^u\d+\.exe$", r"^ultrasurf.*",
    )
]

# Browser-Richtlinien: sicheres DNS (DoH) aus, Tor-Fenster in Brave aus.
# (Pfad unter HKLM, Wertname, Typ "sz"/"dword", Wert)
BROWSER_POLICIES = [
    (r"SOFTWARE\Policies\Google\Chrome", "DnsOverHttpsMode", "sz", "off"),
    (r"SOFTWARE\Policies\Microsoft\Edge", "DnsOverHttpsMode", "sz", "off"),
    (r"SOFTWARE\Policies\BraveSoftware\Brave", "DnsOverHttpsMode", "sz", "off"),
    (r"SOFTWARE\Policies\BraveSoftware\Brave", "TorDisabled", "dword", 1),
    (r"SOFTWARE\Policies\Mozilla\Firefox\DNSOverHTTPS", "Enabled", "dword", 0),
    (r"SOFTWARE\Policies\Mozilla\Firefox\DNSOverHTTPS", "Locked", "dword", 1),
]

STAY_STRONG_TIPS = [
    "Steh jetzt auf und geh für 5 Minuten aus dem Zimmer.",
    "Trink ein Glas kaltes Wasser.",
    "Mach 20 Liegestütze oder Kniebeugen.",
    "Schreib einem Freund eine Nachricht. Egal was.",
    "Geh duschen. Kalt, wenn du dich traust.",
    "Der Drang ist eine Welle. Er geht in 10 bis 20 Minuten vorbei.",
]

# --------------------------------------------------------------------------- Konfiguration

def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def load_config() -> dict | None:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def new_config(reasons: list[str], letter: str = "", cooldown_hours: int = DEFAULT_COOLDOWN_HOURS) -> dict:
    return {
        "version": VERSION,
        "reasons": reasons,
        "letter": letter,
        "installed_at": now().isoformat(),
        "cooldown_hours": cooldown_hours,
        "disable_requested_at": None,
        "active": True,
    }


def disable_ready_at(cfg: dict) -> dt.datetime | None:
    req = cfg.get("disable_requested_at")
    if not req:
        return None
    return dt.datetime.fromisoformat(req) + dt.timedelta(hours=cfg.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS))


def can_disable(cfg: dict, at: dt.datetime | None = None) -> bool:
    ready = disable_ready_at(cfg)
    return ready is not None and (at or now()) >= ready


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def typed_matches(expected: str, typed: str) -> bool:
    return normalize(expected) == normalize(typed) and normalize(expected) != ""


def log_attempt(what: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{now().isoformat()}\t{what}\n")
    except OSError:
        pass


def attempts_today() -> int:
    try:
        today = now().date().isoformat()
        return sum(1 for line in LOG_FILE.read_text(encoding="utf-8").splitlines() if line.startswith(today))
    except OSError:
        return 0


# --------------------------------------------------------------------------- Blockliste

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9-]{2,63}$")
_SKIP = {"localhost", "localhost.localdomain", "local", "broadcasthost", "0.0.0.0", "ip6-localhost"}


def parse_blocklist(text: str) -> set[str]:
    """Liest Listen im Hosts-Format ("0.0.0.0 domain") oder als reine Domainliste."""
    out: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        parts = line.split()
        candidates = parts[1:] if re.match(r"^[\d.:a-f]+$", parts[0]) and len(parts) > 1 else parts[:1]
        for d in candidates:
            d = d.strip(".")
            if d not in _SKIP and _DOMAIN_RE.match(d):
                out.add(d)
    return out


def download_blocklists(sources=BLOCKLIST_SOURCES, timeout: int = 60) -> set[str]:
    domains: set[str] = set()
    errors = []
    for url in sources:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                domains |= parse_blocklist(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    if not domains:
        raise RuntimeError("Blockliste konnte nicht geladen werden:\n" + "\n".join(errors))
    return domains


def with_www(domains) -> set[str]:
    out = set(domains)
    for d in domains:
        if not d.startswith("www.") and d.count(".") == 1:
            out.add("www." + d)
    return out


def build_domain_set(downloaded: set[str]) -> set[str]:
    return with_www(downloaded | set(BYPASS_DOMAINS))


def save_blocklist(domains: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BLOCKLIST_FILE.write_text("\n".join(sorted(domains)) + "\n", encoding="utf-8")


def load_blocklist() -> set[str]:
    try:
        return {l.strip() for l in BLOCKLIST_FILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    except OSError:
        return with_www(BYPASS_DOMAINS)


def is_blocked(host: str, domains: set[str]) -> bool:
    host = host.lower().strip(".")
    if host in domains:
        return True
    # Subdomains gesperrter Domains ebenfalls (z. B. de.example.com)
    parts = host.split(".")
    return any(".".join(parts[i:]) in domains for i in range(1, len(parts) - 1))


# --------------------------------------------------------------------------- Hosts-Datei

def render_hosts_block(domains: set[str]) -> str:
    ordered = sorted(domains)
    lines = [MARK_BEGIN, f"# {len(ordered)} Domains, erzeugt {now().isoformat()}"]
    for i in range(0, len(ordered), DOMAINS_PER_LINE):
        lines.append(REDIRECT_IP + " " + " ".join(ordered[i:i + DOMAINS_PER_LINE]))
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def strip_hosts_block(text: str) -> str:
    pattern = re.compile(re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?", re.S)
    return pattern.sub("", text)


def read_hosts() -> str:
    try:
        return HOSTS_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def write_hosts(text: str) -> None:
    for attempt in range(5):
        try:
            HOSTS_FILE.write_text(text, encoding="utf-8", newline="\r\n" if IS_WIN else "\n")
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(1)


def apply_hosts(domains: set[str]) -> None:
    base = strip_hosts_block(read_hosts()).rstrip("\n")
    write_hosts((base + "\n\n" if base else "") + render_hosts_block(domains))
    flush_dns()


def remove_hosts() -> None:
    write_hosts(strip_hosts_block(read_hosts()).rstrip("\n") + "\n")
    flush_dns()


def hosts_intact(domains: set[str]) -> bool:
    text = read_hosts()
    if MARK_BEGIN not in text or MARK_END not in text:
        return False
    block = text.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0]
    present = set()
    for line in block.splitlines():
        if line.startswith(REDIRECT_IP + " "):
            present.update(line.split()[1:])
    return domains <= present


def flush_dns() -> None:
    if IS_WIN:
        _run(["ipconfig", "/flushdns"])


# --------------------------------------------------------------------------- Windows-Hilfen

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    flags = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=flags)


def is_admin() -> bool:
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def apply_policies(enable: bool) -> None:
    if not IS_WIN:
        return
    import winreg
    for path, name, kind, value in BROWSER_POLICIES:
        try:
            if enable:
                key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE)
                typ = winreg.REG_SZ if kind == "sz" else winreg.REG_DWORD
                winreg.SetValueEx(key, name, 0, typ, value)
                winreg.CloseKey(key)
            else:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, name)
                winreg.CloseKey(key)
        except OSError:
            pass


def run_command() -> str:
    """Befehl, mit dem die Aufgabenplanung den Hintergrundprozess startet."""
    if getattr(sys, "frozen", False):
        return f'"{INSTALL_DIR / (APP + ".exe")}" run'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pyw if pyw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{INSTALL_DIR / "klarblock.py"}" run'


def install_files() -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if getattr(sys, "frozen", False):
        target = INSTALL_DIR / (APP + ".exe")
        if Path(sys.executable).resolve() != target.resolve():
            shutil.copy2(sys.executable, target)
    else:
        shutil.copy2(Path(__file__), INSTALL_DIR / "klarblock.py")


def install_tasks() -> None:
    if not IS_WIN:
        return
    cmd = run_command()
    _run(["schtasks", "/create", "/f", "/tn", APP, "/tr", cmd, "/sc", "onlogon", "/rl", "highest"])
    # Waechter: startet den Prozess neu, falls er beendet wurde
    _run(["schtasks", "/create", "/f", "/tn", APP + "-Waechter", "/tr", cmd,
          "/sc", "minute", "/mo", "5", "/rl", "highest"])


def remove_tasks() -> None:
    if IS_WIN:
        for name in (APP, APP + "-Waechter"):
            _run(["schtasks", "/delete", "/f", "/tn", name])


def start_background() -> None:
    if IS_WIN:
        _run(["schtasks", "/run", "/tn", APP])


def running_processes() -> set[str]:
    if not IS_WIN:
        return set()
    out = _run(["tasklist", "/fo", "csv", "/nh"]).stdout
    return {line.split('","')[0].strip('"').lower() for line in out.splitlines() if line.startswith('"')}


def kill_process(name: str) -> None:
    if IS_WIN:
        _run(["taskkill", "/f", "/im", name])


def downloads_dir() -> Path:
    return _env_path("KLARBLOCK_DOWNLOADS", Path.home() / "Downloads")


# --------------------------------------------------------------------------- Installieren / Entfernen

def install(reasons: list[str], letter: str, cooldown_hours: int, progress=lambda msg: None) -> None:
    progress("Lade Blockliste ...")
    try:
        domains = build_domain_set(download_blocklists())
    except RuntimeError:
        if BLOCKLIST_FILE.exists():
            domains = load_blocklist()
        else:
            raise
    save_blocklist(domains)
    progress(f"{len(domains):,} Domains. Schreibe Hosts-Datei ...".replace(",", "."))
    save_config(new_config(reasons, letter, cooldown_hours))
    apply_hosts(domains)
    progress("Setze Browser-Richtlinien ...")
    apply_policies(True)
    progress("Richte Hintergrundprozess ein ...")
    install_files()
    install_tasks()
    start_background()
    progress("Fertig.")


def uninstall() -> None:
    cfg = load_config() or {}
    cfg["active"] = False
    save_config(cfg)  # laufender Hintergrundprozess beendet sich daraufhin
    remove_tasks()
    remove_hosts()
    apply_policies(False)


def update_blocklist() -> int:
    domains = build_domain_set(download_blocklists())
    save_blocklist(domains)
    apply_hosts(domains)
    return len(domains)


# --------------------------------------------------------------------------- Erkennen von Aufrufen

def parse_sni(data: bytes) -> str | None:
    """Liest den Servernamen (SNI) aus einem TLS-ClientHello."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        pos = 5
        if data[pos] != 0x01:  # ClientHello
            return None
        pos += 4 + 2 + 32  # Handshake-Header, Version, Random
        pos += 1 + data[pos]  # Session-ID
        pos += 2 + int.from_bytes(data[pos:pos + 2], "big")  # Cipher Suites
        pos += 1 + data[pos]  # Kompression
        end = pos + 2 + int.from_bytes(data[pos:pos + 2], "big")
        pos += 2
        while pos + 4 <= min(end, len(data)):
            ext_type = int.from_bytes(data[pos:pos + 2], "big")
            ext_len = int.from_bytes(data[pos + 2:pos + 4], "big")
            pos += 4
            if ext_type == 0:  # server_name
                name_len = int.from_bytes(data[pos + 3:pos + 5], "big")
                return data[pos + 5:pos + 5 + name_len].decode("ascii", "replace").lower()
            pos += ext_len
    except (IndexError, ValueError):
        return None
    return None


def parse_http_host(data: bytes) -> str | None:
    m = re.search(rb"\r\nhost:\s*([^\r\n:]+)", data, re.I)
    return m.group(1).decode("ascii", "replace").lower() if m else None


BLOCK_PAGE = (
    "<!doctype html><meta charset=utf-8><title>Gesperrt</title>"
    "<body style='font-family:sans-serif;background:#111;color:#eee;text-align:center;padding-top:15vh'>"
    "<h1>Diese Seite ist gesperrt.</h1><p>Du hast KlarBlock aus gutem Grund installiert.</p></body>"
).encode("utf-8")


class Watcher:
    """Hintergrundprozess: Listener, Prozess-, Download- und Hosts-Waechter."""

    def __init__(self, on_trigger, ports=(80, 443), interval: float = 3.0):
        self.on_trigger = on_trigger
        self.ports = ports
        self.interval = interval
        self.domains = load_blocklist()
        self.stop = threading.Event()
        self.bound_ports: list[int] = []

    # --- Netzwerk
    def start_listeners(self) -> None:
        for port in self.ports:
            try:
                srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((REDIRECT_IP, port))
                srv.listen(64)
                srv.settimeout(1.0)
                self.bound_ports.append(srv.getsockname()[1])
                threading.Thread(target=self._accept_loop, args=(srv, port == 80), daemon=True).start()
            except OSError:
                pass  # Port belegt: Sperre wirkt trotzdem, nur ohne Popup fuer diesen Port

    def _accept_loop(self, srv: socket.socket, is_http: bool) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn, is_http), daemon=True).start()

    def _handle(self, conn: socket.socket, is_http: bool) -> None:
        try:
            conn.settimeout(3)
            data = conn.recv(8192)
            host = parse_http_host(data) if is_http else parse_sni(data)
            if is_http:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/html; charset=utf-8\r\n"
                             b"Content-Length: " + str(len(BLOCK_PAGE)).encode() + b"\r\nConnection: close\r\n\r\n"
                             + BLOCK_PAGE)
            if host and is_blocked(host, self.domains):
                self.on_trigger(host)
        except OSError:
            pass
        finally:
            conn.close()

    # --- Prozesse, Downloads, Hosts
    def check_processes(self) -> None:
        for name in running_processes() & BLOCKED_PROCESSES:
            kill_process(name)
            self.on_trigger(name)

    def check_downloads(self) -> None:
        d = downloads_dir()
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for p in entries:
            if any(rx.match(p.name) for rx in BLOCKED_DOWNLOAD_PATTERNS):
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                    self.on_trigger(f"Download: {p.name}")
                except OSError:
                    pass

    def check_hosts(self) -> None:
        if not hosts_intact(self.domains):
            try:
                apply_hosts(self.domains)
                self.on_trigger("Hosts-Datei wurde verändert")
            except OSError:
                pass

    def loop(self) -> None:
        tick = 0
        while not self.stop.is_set():
            cfg = load_config()
            if not cfg or not cfg.get("active", True):
                self.stop.set()
                break
            self.check_processes()
            self.check_downloads()
            if tick % 10 == 0:
                self.check_hosts()
            tick += 1
            self.stop.wait(self.interval)


def acquire_instance_lock() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


# --------------------------------------------------------------------------- Oberflaeche (tkinter)

BG, FG, ACCENT, MUTED = "#101418", "#f2f2f2", "#ff5c5c", "#9aa4ad"


def _tk():
    import tkinter as tk
    from tkinter import messagebox
    return tk, messagebox


class NagPopup:
    """Vollbild-Popup, das erst nach Countdown und korrektem Abtippen verschwindet."""

    def __init__(self, root, cfg: dict, what: str, on_close):
        tk, _ = _tk()
        self.on_close = on_close
        reasons = [r for r in cfg.get("reasons", []) if r.strip()] or ["Ich will frei sein."]
        self.target = random.choice(reasons)
        self.remaining = POPUP_COUNTDOWN_SECONDS

        w = self.win = tk.Toplevel(root)
        w.configure(bg=BG)
        w.geometry(f"{w.winfo_screenwidth()}x{w.winfo_screenheight()}+0+0")
        w.attributes("-fullscreen", True)
        w.attributes("-topmost", True)
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        w.bind("<Alt-F4>", lambda e: "break")

        frame = tk.Frame(w, bg=BG)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(frame, text="STOPP.", font=("Segoe UI", 54, "bold"), fg=ACCENT, bg=BG).pack()
        tk.Label(frame, text=f"Gerade wolltest du: {what}", font=("Segoe UI", 14), fg=MUTED, bg=BG).pack()
        n = attempts_today()
        if n > 1:
            tk.Label(frame, text=f"Das ist heute dein {n}. Versuch.", font=("Segoe UI", 14), fg=MUTED, bg=BG).pack()
        tk.Label(frame, text="\nDeshalb hast du KlarBlock installiert:", font=("Segoe UI", 18, "bold"),
                 fg=FG, bg=BG).pack(pady=(10, 4))
        for r in reasons:
            tk.Label(frame, text="•  " + r, font=("Segoe UI", 16), fg=FG, bg=BG, wraplength=900,
                     justify="left").pack(anchor="w", padx=40)
        if cfg.get("letter"):
            tk.Label(frame, text="\nDein Brief an dich selbst:", font=("Segoe UI", 14, "bold"),
                     fg=FG, bg=BG).pack()
            tk.Label(frame, text=cfg["letter"], font=("Segoe UI", 13, "italic"), fg=FG, bg=BG,
                     wraplength=900, justify="left").pack(padx=40)
        tk.Label(frame, text="\n" + random.choice(STAY_STRONG_TIPS), font=("Segoe UI", 14), fg=ACCENT,
                 bg=BG).pack()
        tk.Label(frame, text="\nTipp diesen Grund ab, um weiterzumachen:", font=("Segoe UI", 13),
                 fg=MUTED, bg=BG).pack()
        tk.Label(frame, text=f"„{self.target}“", font=("Segoe UI", 15, "bold"), fg=FG, bg=BG,
                 wraplength=900).pack(pady=4)
        self.entry = tk.Entry(frame, font=("Segoe UI", 15), width=60, justify="center")
        self.entry.pack(pady=6)
        # Abtippen erzwingen: Einfuegen sperren
        for seq in ("<<Paste>>", "<Control-v>", "<Shift-Insert>", "<Button-3>"):
            self.entry.bind(seq, lambda e: "break")
        self.entry.bind("<KeyRelease>", lambda e: self._update())
        self.button = tk.Button(frame, text="", font=("Segoe UI", 14), state="disabled", command=self._close,
                                width=30)
        self.button.pack(pady=8)
        w.after(200, lambda: (w.focus_force(), self.entry.focus_set()))
        self._tick()

    def _tick(self):
        if self.remaining > 0:
            self.remaining -= 1
            self.win.after(1000, self._tick)
        self._update()
        if self.remaining > 0:
            self.win.lift()
            self.win.attributes("-topmost", True)

    def _update(self):
        if self.remaining > 0:
            self.button.config(text=f"Warte noch {self.remaining} s", state="disabled")
        elif typed_matches(self.target, self.entry.get()):
            self.button.config(text="Ich bleibe stark", state="normal")
        else:
            self.button.config(text="Grund genau abtippen", state="disabled")

    def _close(self):
        if self.remaining <= 0 and typed_matches(self.target, self.entry.get()):
            self.win.destroy()
            self.on_close()


def run_background() -> int:
    lock = acquire_instance_lock()
    if lock is None:
        return 0  # laeuft schon
    cfg = load_config()
    if not cfg or not cfg.get("active", True):
        return 0
    tk, _ = _tk()
    import queue

    root = tk.Tk()
    root.withdraw()
    events: queue.Queue[str] = queue.Queue()
    state = {"open": False, "last_closed": 0.0}

    def trigger(what: str):
        log_attempt(what)
        events.put(what)

    def closed():
        state["open"] = False
        state["last_closed"] = time.time()

    def pump():
        what = None
        while not events.empty():
            what = events.get_nowait()
        if what and not state["open"] and time.time() - state["last_closed"] > POPUP_PAUSE_SECONDS:
            state["open"] = True
            NagPopup(root, load_config() or cfg, what, closed)
        if watcher.stop.is_set():
            root.destroy()
            return
        root.after(500, pump)

    watcher = Watcher(trigger)
    watcher.start_listeners()
    threading.Thread(target=watcher.loop, daemon=True).start()
    root.after(500, pump)
    root.mainloop()
    return 0


def _retype_dialog(root, cfg: dict, title: str) -> bool:
    """Modaler Dialog: einen eigenen Grund abtippen. True bei Erfolg."""
    tk, messagebox = _tk()
    reasons = [r for r in cfg.get("reasons", []) if r.strip()] or ["Ich will frei sein."]
    target = random.choice(reasons)
    result = {"ok": False}
    d = tk.Toplevel(root)
    d.title(title)
    d.configure(bg=BG, padx=24, pady=20)
    d.grab_set()
    tk.Label(d, text="Lies deine Gründe noch einmal:", font=("Segoe UI", 12, "bold"), fg=FG, bg=BG).pack(anchor="w")
    for r in reasons:
        tk.Label(d, text="•  " + r, font=("Segoe UI", 11), fg=FG, bg=BG, wraplength=520, justify="left"
                 ).pack(anchor="w")
    tk.Label(d, text=f"\nTipp ab: „{target}“", font=("Segoe UI", 11, "bold"), fg=ACCENT, bg=BG,
             wraplength=520).pack(anchor="w")
    e = tk.Entry(d, width=70)
    e.pack(pady=8)
    for seq in ("<<Paste>>", "<Control-v>", "<Shift-Insert>"):
        e.bind(seq, lambda ev: "break")

    def ok():
        if typed_matches(target, e.get()):
            result["ok"] = True
            d.destroy()
        else:
            messagebox.showerror(APP, "Das stimmt nicht genau überein.", parent=d)

    tk.Button(d, text="Bestätigen", command=ok).pack()
    root.wait_window(d)
    return result["ok"]


def setup_gui() -> int:
    tk, messagebox = _tk()
    root = tk.Tk()
    root.title(f"{APP} einrichten")
    root.configure(bg=BG, padx=28, pady=20)
    root.resizable(False, False)

    tk.Label(root, text=APP, font=("Segoe UI", 26, "bold"), fg=FG, bg=BG).pack(anchor="w")
    tk.Label(root, text=("Sperrt über 75.000 Pornoseiten, Tor und bekannte Umgehungs-Tools.\n"
                         "Wenn du es trotzdem versuchst, zeigt dir KlarBlock deine eigenen Gründe."),
             font=("Segoe UI", 11), fg=MUTED, bg=BG, justify="left").pack(anchor="w", pady=(0, 14))

    tk.Label(root, text="Warum willst du das? (mindestens 3 Gründe, einer pro Zeile)",
             font=("Segoe UI", 11, "bold"), fg=FG, bg=BG).pack(anchor="w")
    reasons_box = tk.Text(root, width=70, height=7, font=("Segoe UI", 11), wrap="word")
    reasons_box.pack(pady=(4, 12))
    tk.Label(root, text="Optional: ein kurzer Brief an dein zukünftiges Ich in einem schwachen Moment",
             font=("Segoe UI", 11, "bold"), fg=FG, bg=BG).pack(anchor="w")
    letter_box = tk.Text(root, width=70, height=4, font=("Segoe UI", 11), wrap="word")
    letter_box.pack(pady=(4, 12))

    row = tk.Frame(root, bg=BG)
    row.pack(anchor="w")
    tk.Label(row, text="Wartezeit zum Deaktivieren (Stunden):", font=("Segoe UI", 11), fg=FG, bg=BG).pack(side="left")
    cooldown = tk.Spinbox(row, from_=24, to=168, increment=24, width=5)
    cooldown.pack(side="left", padx=8)

    status = tk.Label(root, text="", font=("Segoe UI", 10), fg=MUTED, bg=BG)
    status.pack(anchor="w", pady=(10, 0))

    def go():
        reasons = [l.strip() for l in reasons_box.get("1.0", "end").splitlines() if l.strip()]
        if len(reasons) < 3:
            messagebox.showwarning(APP, "Bitte schreib mindestens 3 Gründe, jeden in eine eigene Zeile.")
            return
        if any(len(r) < 8 for r in reasons):
            messagebox.showwarning(APP, "Schreib jeden Grund als ganzen Satz (mindestens 8 Zeichen).")
            return
        btn.config(state="disabled")

        def progress(msg):
            status.config(text=msg)
            root.update()

        try:
            install(reasons, letter_box.get("1.0", "end").strip(), int(cooldown.get()), progress)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP, f"Einrichtung fehlgeschlagen:\n{e}")
            btn.config(state="normal")
            return
        messagebox.showinfo(APP, "KlarBlock ist aktiv. Du schaffst das.")
        root.destroy()

    btn = tk.Button(root, text="Blocker aktivieren", font=("Segoe UI", 12, "bold"), command=go)
    btn.pack(pady=(14, 0), anchor="e")
    root.mainloop()
    return 0


def status_gui(cfg: dict) -> int:
    tk, messagebox = _tk()
    root = tk.Tk()
    root.title(APP)
    root.configure(bg=BG, padx=28, pady=20)
    root.resizable(False, False)

    days = (now() - dt.datetime.fromisoformat(cfg["installed_at"])).days
    tk.Label(root, text=f"{APP} ist aktiv", font=("Segoe UI", 22, "bold"), fg=FG, bg=BG).pack(anchor="w")
    tk.Label(root, text=f"Seit {days} Tagen. Versuche heute: {attempts_today()}", font=("Segoe UI", 11),
             fg=MUTED, bg=BG).pack(anchor="w", pady=(0, 12))
    info = tk.Label(root, text="", font=("Segoe UI", 11), fg=FG, bg=BG, justify="left", wraplength=480)
    info.pack(anchor="w")
    buttons = tk.Frame(root, bg=BG)
    buttons.pack(anchor="w", pady=(14, 0))

    def refresh():
        for b in buttons.winfo_children():
            b.destroy()
        c = load_config() or cfg
        ready = disable_ready_at(c)
        if ready is None:
            info.config(text=f"Deaktivieren ist nur mit {c.get('cooldown_hours', 24)} Stunden Wartezeit möglich.")
            tk.Button(buttons, text="Deaktivierung anfordern", command=request).pack(side="left")
        elif not can_disable(c):
            info.config(text=f"Deaktivierung angefordert. Möglich ab {ready:%d.%m.%Y %H:%M} Uhr.\n"
                             "Bis dahin bleibt alles gesperrt. Du kannst die Anfrage jederzeit zurückziehen.")
            tk.Button(buttons, text="Anfrage zurückziehen", command=cancel).pack(side="left")
        else:
            info.config(text="Die Wartezeit ist vorbei. Du kannst KlarBlock jetzt deaktivieren.")
            tk.Button(buttons, text="Anfrage zurückziehen", command=cancel).pack(side="left")
            tk.Button(buttons, text="Jetzt deaktivieren", command=disable).pack(side="left", padx=8)
        tk.Button(buttons, text="Blockliste aktualisieren", command=update).pack(side="left", padx=8)

    def request():
        c = load_config() or cfg
        if _retype_dialog(root, c, "Deaktivierung anfordern"):
            c["disable_requested_at"] = now().isoformat()
            save_config(c)
            refresh()

    def cancel():
        c = load_config() or cfg
        c["disable_requested_at"] = None
        save_config(c)
        messagebox.showinfo(APP, "Gute Entscheidung.")
        refresh()

    def disable():
        c = load_config() or cfg
        if can_disable(c) and _retype_dialog(root, c, "Deaktivieren"):
            uninstall()
            messagebox.showinfo(APP, "KlarBlock ist deaktiviert. Du kannst es jederzeit wieder einrichten.")
            root.destroy()

    def update():
        try:
            n = update_blocklist()
            messagebox.showinfo(APP, f"Blockliste aktualisiert: {n:,} Domains.".replace(",", "."))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP, str(e))

    refresh()
    root.mainloop()
    return 0


# --------------------------------------------------------------------------- Einstieg

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "run":
        return run_background()
    if IS_WIN and not is_admin():
        _, messagebox = _tk()
        messagebox.showerror(APP, "Bitte starte KlarBlock mit Rechtsklick > 'Als Administrator ausführen'.")
        return 1
    if cmd == "update":
        print(update_blocklist())
        return 0
    cfg = load_config()
    if cfg and cfg.get("active", True):
        return status_gui(cfg)
    return setup_gui()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
