<?php
declare(strict_types=1);

// Connection-diagnostic endpoint. Lets clients compare wall-clock RTT
// against the PHP-side time-spent for the same request, so the user can
// tell whether perceived slowness is transport (high wall, low server)
// or relay/FS (high server). Gated by the same Bearer secret as
// relay.php — never expose unauthenticated even when RELAY_SECRET is
// absent (the response carries server-config info).

$T0 = microtime(true);

@header_remove('X-Powered-By');
@header('Content-Type: application/json');
@header('Cache-Control: no-store');
@header('X-Content-Type-Options: nosniff');

$configPath = __DIR__ . '/config.php';
if (file_exists($configPath)) {
    require $configPath;
}

function dbg_auth_ok(): bool {
    if (!defined('RELAY_SECRET') || RELAY_SECRET === '' || RELAY_SECRET === 'CHANGE_ME') {
        return true;
    }
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if ($hdr === '' && function_exists('apache_request_headers')) {
        $h = apache_request_headers();
        $hdr = $h['Authorization'] ?? ($h['authorization'] ?? '');
    }
    if (!preg_match('/^Bearer\s+(.+)$/', $hdr, $m)) return false;
    return hash_equals(RELAY_SECRET, trim($m[1]));
}

function dbg_send(int $code, array $body): void {
    http_response_code($code);
    echo json_encode($body, JSON_UNESCAPED_SLASHES);
    exit;
}

if (!dbg_auth_ok()) dbg_send(401, ['error' => 'unauthorized']);

$timings = [];
function dbg_time(string $name, callable $fn) {
    global $timings;
    $t = microtime(true);
    $result = $fn();
    $timings[$name] = round((microtime(true) - $t) * 1000, 3);
    return $result;
}

$op = (string)($_GET['op'] ?? 'ping');

switch ($op) {
case 'ping':
    // No work — measures pure transport + PHP startup overhead.
    break;

case 'fs':
    // Exercise the same filesystem pattern relay.php uses for record
    // append + index write. Catches shared-host I/O issues.
    $base = __DIR__ . '/data';
    if (!is_dir($base)) @mkdir($base, 0700, true);
    if (!is_dir($base) || !is_writable($base)) {
        dbg_send(500, ['error' => 'data/ not writable']);
    }
    $tmp = $base . '/__debug_' . bin2hex(random_bytes(4));
    dbg_time('mkdir', function () use ($tmp) { @mkdir($tmp, 0700); });
    if (!is_dir($tmp)) dbg_send(500, ['error' => 'mkdir failed']);

    $payload = str_repeat('x', 4096);
    dbg_time('write_4k', function () use ($tmp, $payload) {
        @file_put_contents("$tmp/blob", $payload);
    });
    dbg_time('stat', function () use ($tmp) {
        clearstatcache(true, "$tmp/blob");
        @filesize("$tmp/blob");
    });
    dbg_time('read_4k', function () use ($tmp) {
        @file_get_contents("$tmp/blob");
    });
    dbg_time('append_idx_10x', function () use ($tmp) {
        // Mirrors the relay's per-record append: open in append mode,
        // write 4 bytes, fflush. Real relay uses flock; we skip it
        // here so a hung debug request can't wedge a real session.
        $f = @fopen("$tmp/idx", 'cb');
        if (!$f) return;
        for ($i = 0; $i < 10; $i++) {
            fwrite($f, pack('N', $i));
            fflush($f);
        }
        fclose($f);
    });
    dbg_time('cleanup', function () use ($tmp) {
        @unlink("$tmp/blob");
        @unlink("$tmp/idx");
        @rmdir($tmp);
    });
    break;

case 'info':
    // No work — info block is attached below.
    break;

default:
    dbg_send(400, ['error' => "unknown op: $op"]);
}

$server_ms = round((microtime(true) - $T0) * 1000, 3);

$body = [
    'op'              => $op,
    'server_ms'       => $server_ms,
    'timings_ms'      => $timings,
    'server_time_iso' => gmdate('c'),
];

if ($op === 'info' || isset($_GET['info'])) {
    $body['info'] = [
        'sapi'             => PHP_SAPI,
        'php_version'      => PHP_VERSION,
        'opcache'          => function_exists('opcache_get_status')
            ? (bool)(@opcache_get_status(false)['opcache_enabled'] ?? false)
            : false,
        'server_protocol'  => $_SERVER['SERVER_PROTOCOL']  ?? null,
        'request_scheme'   => $_SERVER['REQUEST_SCHEME']   ?? null,
        'host'             => $_SERVER['HTTP_HOST']        ?? null,
        'data_free_bytes'  => @disk_free_space(__DIR__),
        'loadavg'          => function_exists('sys_getloadavg') ? sys_getloadavg() : null,
    ];
}

// Server-Timing — picked up natively by browser DevTools.
$st = [];
foreach ($timings as $k => $v) $st[] = "$k;dur=$v";
$st[] = "total;dur=$server_ms";
@header('Server-Timing: ' . implode(', ', $st));

echo json_encode($body, JSON_UNESCAPED_SLASHES);
