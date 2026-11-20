import datetime as dt
import importlib
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def kb(tmp_path, monkeypatch):
    monkeypatch.setenv("KLARBLOCK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("KLARBLOCK_HOSTS", str(tmp_path / "hosts"))
    monkeypatch.setenv("KLARBLOCK_INSTALL", str(tmp_path / "install"))
    monkeypatch.setenv("KLARBLOCK_DOWNLOADS", str(tmp_path / "Downloads"))
    sys.path.insert(0, str(Path(__file__).parent))
    import klarblock
    return importlib.reload(klarblock)


def test_parse_blocklist_hosts_and_plain(kb):
    text = """# kommentar
0.0.0.0 example-porn.com
127.0.0.1 a.example.net b.example.net  # inline
0.0.0.0 localhost
plain-domain.org
::1 ip6-localhost
not a domain!!
"""
    assert kb.parse_blocklist(text) == {"example-porn.com", "a.example.net", "b.example.net", "plain-domain.org"}


def test_is_blocked_subdomains(kb):
    d = {"example.com"}
    assert kb.is_blocked("example.com", d)
    assert kb.is_blocked("de.cdn.example.com", d)
    assert kb.is_blocked("EXAMPLE.com.", d)
    assert not kb.is_blocked("notexample.com", d)
    assert not kb.is_blocked("com", d)


def test_hosts_apply_keeps_user_lines_and_is_idempotent(kb):
    kb.HOSTS_FILE.write_text("127.0.0.1 meinserver.local\n")
    domains = {f"site{i}.com" for i in range(20)}
    kb.apply_hosts(domains)
    kb.apply_hosts(domains)
    text = kb.HOSTS_FILE.read_text()
    assert text.count(kb.MARK_BEGIN) == 1
    assert "127.0.0.1 meinserver.local" in text
    assert kb.hosts_intact(domains)
    # max 9 Domains pro Zeile
    lines = [l for l in text.splitlines() if l.startswith("127.0.0.1 site")]
    assert all(len(l.split()) <= 10 for l in lines) and len(lines) == 3
    # Manipulation wird erkannt
    kb.HOSTS_FILE.write_text(text.replace("site3.com", ""))
    assert not kb.hosts_intact(domains)
    kb.remove_hosts()
    assert kb.HOSTS_FILE.read_text() == "127.0.0.1 meinserver.local\n"


def test_cooldown(kb):
    cfg = kb.new_config(["a", "b", "c"], cooldown_hours=24)
    assert not kb.can_disable(cfg)
    cfg["disable_requested_at"] = (kb.now() - dt.timedelta(hours=23)).isoformat()
    assert not kb.can_disable(cfg)
    cfg["disable_requested_at"] = (kb.now() - dt.timedelta(hours=25)).isoformat()
    assert kb.can_disable(cfg)


def test_typed_matches(kb):
    assert kb.typed_matches("Ich will  frei sein.", "ich will frei sein.")
    assert not kb.typed_matches("Ich will frei sein.", "ich will frei sein")
    assert not kb.typed_matches("", "")


def test_build_domain_set_adds_bypass_and_www(kb):
    s = kb.build_domain_set({"example.com"})
    assert {"example.com", "www.example.com", "torproject.org", "dist.torproject.org"} <= s


def _start_watcher(kb, domains):
    kb.save_blocklist(domains)
    hits = []
    w = kb.Watcher(hits.append, ports=(0, 0))
    # zwei freie Ports: erster als HTTP behandeln, zweiter als TLS
    w.ports = ()
    servers = []
    for is_http in (True, False):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        srv.settimeout(1)
        servers.append(srv.getsockname()[1])
        threading.Thread(target=w._accept_loop, args=(srv, is_http), daemon=True).start()
    return w, hits, servers


def test_listener_detects_https_sni_and_http_host(kb):
    w, hits, (http_port, tls_port) = _start_watcher(kb, {"blocked-site.com", "www.blocked-site.com"})
    # HTTPS: echter TLS-Handshake mit SNI
    ctx = ssl.create_default_context()
    with socket.create_connection(("127.0.0.1", tls_port)) as s:
        try:
            ctx.wrap_socket(s, server_hostname="www.blocked-site.com")
        except (ssl.SSLError, OSError):
            pass
    # HTTP
    with socket.create_connection(("127.0.0.1", http_port)) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: blocked-site.com\r\n\r\n")
        resp = s.recv(4096)
    assert b"403" in resp and "gesperrt".encode() in resp
    # nicht gesperrte Seite loest nichts aus
    with socket.create_connection(("127.0.0.1", http_port)) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: wikipedia.org\r\n\r\n")
        s.recv(4096)
    time.sleep(0.5)
    w.stop.set()
    assert sorted(hits) == ["blocked-site.com", "www.blocked-site.com"]


def test_download_watcher_deletes_tor_installer(kb):
    dl = kb.downloads_dir()
    dl.mkdir()
    (dl / "tor-browser-windows-x86_64-portable-14.0.exe").write_bytes(b"x")
    (dl / "Rechnung.pdf").write_bytes(b"x")
    hits = []
    w = kb.Watcher(hits.append)
    w.check_downloads()
    assert [p.name for p in dl.iterdir()] == ["Rechnung.pdf"]
    assert hits and hits[0].startswith("Download:")


def test_real_stevenblack_list_parses(kb):
    pytest.importorskip("urllib.request")
    try:
        domains = kb.download_blocklists(timeout=30)
    except RuntimeError:
        pytest.skip("kein Netz")
    assert len(domains) > 50000
    assert "pornhub.com" in domains
