"""Probe the relay's debug.php endpoint and print a diagnostic report.

The report separates wall-clock RTT from PHP-side time-spent so the user
can tell whether perceived slowness is transport (high wall, low server)
or the relay itself (high server). Shared between the Linux and Windows
wrappers — both expose this via `termpilot --test-connection`."""

import json
import ssl
import sys
import time
import urllib.error
import urllib.request


_DEBUG_PATH = "/debug.php"


def _normalize_base(base_url: str) -> str:
    s = (base_url or "").rstrip("/")
    if s.endswith("/relay.php"):
        s = s[: -len("/relay.php")]
    return s


def _make_session(base: str, *, insecure: bool):
    """Return a single http.client connection we can reuse across probes,
    so we measure relay round-trip time, not TLS handshake cost. urllib's
    opener does NOT pool — it spins up a fresh connection per .open(),
    which adds ~2 RTT of TLS handshake to every probe and masks the
    relay's real behavior."""
    import http.client
    from urllib.parse import urlparse
    u = urlparse(base)
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    port = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, port, context=ctx, timeout=15.0)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=15.0)
    return conn, u.path.rstrip("/")


def _probe(conn, path: str, op: str, *, secret: str):
    """One probe over a reused connection. Returns (wall_ms, server_ms, body_dict)."""
    headers = {"Connection": "keep-alive"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    full_path = f"{path}{_DEBUG_PATH}?op={op}"
    t0 = time.perf_counter()
    conn.request("GET", full_path, headers=headers)
    r = conn.getresponse()
    raw = r.read()
    wall_ms = (time.perf_counter() - t0) * 1000.0
    if r.status != 200:
        raise urllib.error.HTTPError(full_path, r.status, r.reason, dict(r.getheaders()), None)
    body = json.loads(raw.decode("utf-8"))
    return wall_ms, float(body.get("server_ms") or 0.0), body


def _stats(samples):
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    return {
        "min": s[0],
        "p50": s[n // 2],
        "p95": s[max(0, int(round(n * 0.95)) - 1)],
        "max": s[-1],
        "n": n,
    }


def _fmt_line(label: str, st) -> str:
    if not st:
        return f"  {label:<14} (no samples)"
    return (f"  {label:<14} min {st['min']:6.1f}  "
            f"p50 {st['p50']:6.1f}  p95 {st['p95']:6.1f}  "
            f"max {st['max']:6.1f}  ms   (n={st['n']})")


def run(base_url: str, secret: str, *,
        insecure: bool = False, ping_count: int = 10, fs_count: int = 3) -> int:
    base = _normalize_base(base_url)
    if not base:
        sys.stderr.write("termpilot: no relay URL configured.\n")
        return 2

    sys.stdout.write(f"Testing {base}{_DEBUG_PATH} …\n\n")

    try:
        conn, path = _make_session(base, insecure=insecure)
    except Exception as e:
        sys.stderr.write(f"ERROR: cannot reach relay — {e}\n")
        return 1

    # First probe also surfaces auth / deploy errors cleanly. We also
    # treat it as the TLS-handshake warm-up so subsequent probes measure
    # the steady-state RTT (with keep-alive).
    try:
        wall, srv, _ = _probe(conn, path, "ping", secret=secret)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: HTTP {e.code} from debug.php — {e.reason}\n")
        if e.code == 401:
            sys.stderr.write("  Hint: relay requires a Bearer secret. "
                             "Run `termpilot --set-relay-secret <SECRET>`.\n")
        elif e.code == 404:
            sys.stderr.write("  Hint: debug.php not deployed on this relay yet.\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"ERROR: cannot reach relay — {e}\n")
        return 1

    ping_wall = []  # exclude warm-up so handshake doesn't skew the report
    ping_server = []
    for _ in range(max(0, ping_count)):
        try:
            w, s, _ = _probe(conn, path, "ping", secret=secret)
            ping_wall.append(w)
            ping_server.append(s)
        except Exception as e:
            sys.stderr.write(f"  (ping failed: {e})\n")
            try:
                conn.close()
                conn, path = _make_session(base, insecure=insecure)
            except Exception:
                pass

    fs_wall, fs_server = [], []
    fs_sub = {}
    for _ in range(max(0, fs_count)):
        try:
            w, s, body = _probe(conn, path, "fs", secret=secret)
            fs_wall.append(w)
            fs_server.append(s)
            for k, v in (body.get("timings_ms") or {}).items():
                fs_sub.setdefault(k, []).append(float(v))
        except Exception as e:
            sys.stderr.write(f"  (fs probe failed: {e})\n")
            try:
                conn.close()
                conn, path = _make_session(base, insecure=insecure)
            except Exception:
                pass

    # Try to grab info block once for context.
    info = None
    try:
        _, _, body = _probe(conn, path, "info", secret=secret)
        info = body.get("info")
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass

    network = [w - s for w, s in zip(ping_wall, ping_server)]

    sys.stdout.write("Ping (no server work):\n")
    sys.stdout.write(_fmt_line("wall RTT",    _stats(ping_wall))    + "\n")
    sys.stdout.write(_fmt_line("server time", _stats(ping_server))  + "\n")
    sys.stdout.write(_fmt_line("network",     _stats(network))      + "\n")
    sys.stdout.write("\nFS probe (write+read+append+cleanup):\n")
    sys.stdout.write(_fmt_line("wall RTT",    _stats(fs_wall))      + "\n")
    sys.stdout.write(_fmt_line("server time", _stats(fs_server))    + "\n")
    for k in sorted(fs_sub):
        sys.stdout.write(_fmt_line(k, _stats(fs_sub[k])) + "\n")

    if info:
        sys.stdout.write("\nServer info:\n")
        for k in ("sapi", "php_version", "server_protocol", "opcache",
                  "data_free_bytes", "loadavg"):
            if k in info:
                sys.stdout.write(f"  {k:<16} {info[k]}\n")

    sys.stdout.write("\n")
    net_stats = _stats(network)
    srv_stats = _stats(ping_server)
    wall_stats = _stats(ping_wall)
    p50_net = net_stats["p50"] if net_stats else 0
    p95_net = net_stats["p95"] if net_stats else 0
    p50_srv = srv_stats["p50"] if srv_stats else 0
    p50_wall = wall_stats["p50"] if wall_stats else 0
    p95_wall = wall_stats["p95"] if wall_stats else 0
    # Queueing signature: p95 wall is much larger than p50 wall while
    # server-time stays low. That means PHP itself was fast on every
    # probe, but at least one request sat in the FastCGI gateway queue
    # for seconds before a worker freed up. On shared hosting this
    # typically means pm.max_children is exhausted by long-polls.
    if p95_wall > 1000 and p95_wall > p50_wall * 5 and p50_srv < 50:
        sys.stdout.write(
            f"Verdict: RELAY QUEUEING (p50 {p50_wall:.0f} ms, p95 {p95_wall:.0f} ms wall; "
            f"server-time stays at {p50_srv:.1f} ms).\n"
            "  PHP-FPM workers look exhausted — most likely the long-polls from active\n"
            "  sessions are tying up the pool. Either reduce LONG_POLL_SECS in relay.php\n"
            "  or ask the host to raise pm.max_children.\n")
    elif p50_net > 500:
        sys.stdout.write(
            f"Verdict: NETWORK looks slow (p50 RTT-overhead {p50_net:.0f} ms). "
            "The relay is replying quickly; the time is spent over the wire.\n")
    elif p50_srv > 200:
        sys.stdout.write(
            f"Verdict: RELAY looks slow (p50 server-time {p50_srv:.0f} ms). "
            "Shared-host PHP/FS is the bottleneck, not your network.\n")
    else:
        sys.stdout.write("Verdict: connection looks healthy.\n")
    return 0
