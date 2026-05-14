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


def _probe(base: str, op: str, *, secret: str, insecure: bool, timeout: float = 15.0):
    """One probe. Returns (wall_ms, server_ms, body_dict)."""
    url = f"{base}{_DEBUG_PATH}?op={op}"
    ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    req = urllib.request.Request(url, method="GET")
    if secret:
        req.add_header("Authorization", f"Bearer {secret}")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    t0 = time.perf_counter()
    with opener.open(req, timeout=timeout) as r:
        raw = r.read(64 * 1024)
    wall_ms = (time.perf_counter() - t0) * 1000.0
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

    # First probe also surfaces auth / deploy errors cleanly.
    try:
        wall, srv, _ = _probe(base, "ping", secret=secret, insecure=insecure)
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"ERROR: HTTP {e.code} from debug.php — {e.reason}\n")
        if e.code == 401:
            sys.stderr.write("  Hint: relay requires a Bearer secret. "
                             "Run `termpilot --set-relay-secret <SECRET>`.\n")
        elif e.code == 404:
            sys.stderr.write("  Hint: debug.php not deployed on this relay yet.\n")
        return 1
    except urllib.error.URLError as e:
        sys.stderr.write(f"ERROR: cannot reach relay — {e.reason}\n")
        return 1
    except Exception as e:
        sys.stderr.write(f"ERROR: unexpected — {e}\n")
        return 1

    ping_wall = [wall]
    ping_server = [srv]
    for _ in range(max(0, ping_count - 1)):
        try:
            w, s, _ = _probe(base, "ping", secret=secret, insecure=insecure)
            ping_wall.append(w)
            ping_server.append(s)
        except Exception as e:
            sys.stderr.write(f"  (ping failed: {e})\n")

    fs_wall, fs_server = [], []
    fs_sub = {}
    for _ in range(max(0, fs_count)):
        try:
            w, s, body = _probe(base, "fs", secret=secret, insecure=insecure)
            fs_wall.append(w)
            fs_server.append(s)
            for k, v in (body.get("timings_ms") or {}).items():
                fs_sub.setdefault(k, []).append(float(v))
        except Exception as e:
            sys.stderr.write(f"  (fs probe failed: {e})\n")

    # Try to grab info block once for context.
    info = None
    try:
        _, _, body = _probe(base, "info", secret=secret, insecure=insecure)
        info = body.get("info")
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
    p50_net = net_stats["p50"] if net_stats else 0
    p50_srv = srv_stats["p50"] if srv_stats else 0
    if p50_net > 500:
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
