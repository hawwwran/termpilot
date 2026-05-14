<?php
/**
 * termpilot relay v2 — blind-relay edition.
 *
 * The server stores opaque ciphertext blobs and the bare minimum public
 * metadata needed to operate (timestamps, cols/rows for layout, byte counts).
 * Title / cwd / cmd / output / input / transcript are end-to-end encrypted
 * by the wrapper and the browser; the server cannot read them.
 *
 * Endpoints (?op=):
 *   POST register     create a session
 *   POST output       append output records (encrypted)
 *   GET  output       long-poll output records since_seq
 *   POST input        append input records (encrypted, from browser)
 *   GET  input        long-poll input records since_seq (wrapper consumes)
 *   POST resize       update public cols/rows
 *   POST heartbeat    keep-alive
 *   POST close        mark session closed
 *   GET  sessions     list ALL sessions with markers (clients try-decrypt)
 *   GET  meta         fetch encrypted_meta blob for a session
 *
 * Auth: Bearer RELAY_SECRET. This is NOT the encryption secret — it's a
 * shared HTTP gate to prevent random POSTs / DoS. Anyone holding it can
 * still see only ciphertext.
 *
 * Storage:
 *   data/<sid>/meta.public.json   { cols, rows, started_at, last_seen,
 *                                  closed, marker_b64 }
 *   data/<sid>/meta.bin           encrypted_meta (raw bytes)
 *   data/<sid>/out.records        concatenated raw record blobs
 *   data/<sid>/out.idx            uint32 LE per record: start byte offset
 *   data/<sid>/in.records, in.idx  same shape
 */

declare(strict_types=1);

ini_set('display_errors', '0');
error_reporting(E_ALL);
ignore_user_abort(false);
@header_remove('X-Powered-By');

// Everything we create lives under data/; default to owner-only. Each
// mkdir/file_put_contents site still passes/sets an explicit mode, but a
// process-wide umask catches any path that forgets, and forces 0600/0700
// even on shared hosts whose default PHP umask is 022.
umask(0077);

@header('Cache-Control: no-store');
@header('X-Content-Type-Options: nosniff');
@header('Referrer-Policy: same-origin');

// ---- Config ---------------------------------------------------------------

$configPath = __DIR__ . '/config.php';
if (file_exists($configPath)) {
    require $configPath;
}
// RELAY_SECRET is OPTIONAL. When set, it acts as a Bearer-auth spam
// gate — content secrecy comes from per-token AES-GCM regardless,
// and op_close / op_push_notify are token-bound via trigger_secret
// (see "Trigger secret" in ARCHITECTURE.md). Without it, any internet
// stranger who finds the URL can register fake sessions, write
// encrypted noise to existing ones, and otherwise fill data/ — a
// DoS / disk-spam concern, not a confidentiality break. Recommended
// for any non-throwaway deployment.

$DATA_DIR = __DIR__ . '/data';
if (!is_dir($DATA_DIR)) { @mkdir($DATA_DIR, 0700, true); @chmod($DATA_DIR, 0700); }
if (!is_dir($DATA_DIR)) { json_err(500, 'data/ not writable'); }

// Belt-and-suspenders: one-shot walk of data/ on the first request after
// a fresh upload, fixing any files/dirs that an earlier (umask=022) version
// of this relay left world-readable. Sentinel-gated so subsequent requests
// pay nothing.
ensure_data_perms($DATA_DIR);

// Tunables
// LONG_POLL_SECS: how long the relay's records GET will block waiting
// for new data before returning empty. Each long-poll occupies one
// PHP-FPM worker for its entire dwell time (relay polls the idx file
// every $POLL_INTERVAL_US — there's no kernel-level wake on shared
// hosting). On hosts with low pm.max_children (5-10 typical), high
// values starve workers: a contending POST queues for up to this many
// seconds at the FastCGI gateway before a worker frees up. Was 25;
// dropped to 5 after observing 9.7s p95 wall-time on debug.php?op=ping
// while real session traffic was active — the diagnostic was queued
// behind two long-polls (wrapper input + browser output).
$LONG_POLL_SECS  = 5;
$POLL_INTERVAL_US = 100_000;
$MAX_CHUNK_BYTES = 256 * 1024;
$DEFAULT_LIMIT   = 100;
$MAX_LIMIT       = 500;
$MAX_BLOB_BYTES  = 1_048_576; // 1 MB cap per encrypted record
$ALIVE_TTL_SECS  = defined('ALIVE_TTL_SECS') ? (int)ALIVE_TTL_SECS : 300;
// GC defaults; the gc op also accepts overrides in the request body.
$GC_CLOSED_AGE_SECS = defined('GC_CLOSED_AGE_SECS') ? (int)GC_CLOSED_AGE_SECS : 7 * 24 * 3600;
$GC_STALE_AGE_SECS  = defined('GC_STALE_AGE_SECS')  ? (int)GC_STALE_AGE_SECS  : 30 * 24 * 3600;

// ---- Auth -----------------------------------------------------------------

$op = $_GET['op'] ?? '';
$method = $_SERVER['REQUEST_METHOD'];

// auth_required is queryable without auth — clients use it to know
// whether to render the bearer-secret input field.
if ($op === 'auth_required') { json_ok(['required' => auth_required()]); }

// op=gc skips the RELAY_SECRET gate because it has its own ADMIN_SECRET
// auth (a separate, higher-privilege secret). Both are sent via Bearer
// and only one header fits, so the admin path bypasses the RELAY_SECRET
// check rather than overload the same secret with two roles.
if ($op !== 'gc' && !check_auth()) { json_err(401, 'unauthorized'); }

try {
    switch ($op) {
        case 'register':  require_method('POST'); op_register($DATA_DIR, $MAX_BLOB_BYTES); break;
        case 'output':    if ($method === 'POST') op_output_post($DATA_DIR, $MAX_BLOB_BYTES);
                          else                    op_records_get_lp($DATA_DIR, 'out', $LONG_POLL_SECS, $POLL_INTERVAL_US, $MAX_CHUNK_BYTES, $DEFAULT_LIMIT, $MAX_LIMIT);
                          break;
        case 'input':     if ($method === 'POST') op_input_post($DATA_DIR, $MAX_BLOB_BYTES);
                          else                    op_records_get_lp($DATA_DIR, 'in', $LONG_POLL_SECS, $POLL_INTERVAL_US, $MAX_CHUNK_BYTES, $DEFAULT_LIMIT, $MAX_LIMIT);
                          break;
        case 'resize':    require_method('POST'); op_resize($DATA_DIR); break;
        case 'heartbeat': require_method('POST'); op_heartbeat($DATA_DIR); break;
        case 'close':     require_method('POST'); op_close($DATA_DIR); break;
        case 'sessions':  require_method('GET');  op_sessions($DATA_DIR, $ALIVE_TTL_SECS); break;
        case 'meta':      require_method('GET');  op_meta($DATA_DIR); break;
        case 'gc':        require_method('POST'); op_gc($DATA_DIR, $GC_CLOSED_AGE_SECS, $GC_STALE_AGE_SECS); break;
        case 'push_pubkey':      require_method('GET');  op_push_pubkey($DATA_DIR); break;
        case 'push_subscribe':   require_method('POST'); op_push_subscribe($DATA_DIR); break;
        case 'push_unsubscribe': require_method('POST'); op_push_unsubscribe($DATA_DIR); break;
        case 'push_notify':      require_method('POST'); op_push_notify($DATA_DIR); break;
        default:          json_err(404, 'unknown op');
    }
} catch (Throwable $e) {
    @error_log('termpilot relay: ' . $e->getMessage() . ' at ' . $e->getFile() . ':' . $e->getLine());
    json_err(500, 'internal error');
}

// ---- Operations -----------------------------------------------------------

function op_register(string $dataDir, int $maxBlob): void {
    $body = json_in();
    $sid = require_sid($body);
    $cols = clamp_int((int)($body['cols'] ?? 80), 20, 500);
    $rows = clamp_int((int)($body['rows'] ?? 24), 8, 200);
    $encMeta = require_b64_blob($body, 'encrypted_meta', $maxBlob);
    $encMarker = require_b64_blob($body, 'encrypted_marker', $maxBlob);
    // trigger_id is a public verifier (SHA-256 of trigger_secret which is
    // HMAC(token, "termpilot:trigger:v1")). Required so we can later
    // gate op_close on proof of token possession instead of trusting
    // RELAY_SECRET alone — anyone who has RELAY_SECRET (a shared HTTP
    // gate) but NOT the device token must not be able to close other
    // people's sessions.
    $triggerIdHex = require_hex_64($body, 'trigger_id_hex');

    $dir = $dataDir . '/' . $sid;
    $existing = null;
    if (is_dir($dir)) {
        // Re-registering the same SID is allowed (e.g. wrapper reconnect),
        // but only if the marker matches — otherwise it's an attack.
        // Compare raw decoded bytes (hash_equals on the b64 string would
        // false-409 on benign whitespace/padding differences).
        $existing = @json_decode((string)@file_get_contents($dir . '/meta.public.json'), true);
        if (is_array($existing) && isset($existing['marker_b64'])) {
            $existingRaw = base64_decode((string)$existing['marker_b64'], true);
            if ($existingRaw === false || !hash_equals($existingRaw, $encMarker)) {
                json_err(409, 'session exists with different marker');
            }
        }
        // trigger_id must also match — once a session is bound to a token's
        // trigger_id, re-register from a different token (same RELAY_SECRET,
        // different device token) is squatting.
        if (is_array($existing) && !empty($existing['trigger_id_hex'])) {
            if (!hash_equals((string)$existing['trigger_id_hex'], $triggerIdHex)) {
                json_err(409, 'session exists with different trigger_id');
            }
        }
    } else {
        if (!@mkdir($dir, 0700, true)) { json_err(500, 'mkdir failed'); }
        @chmod($dir, 0700);
    }

    @file_put_contents($dir . '/meta.bin', $encMeta);
    @chmod($dir . '/meta.bin', 0600);
    write_public_meta($dir, [
        'id' => $sid,
        'cols' => $cols, 'rows' => $rows,
        'started_at' => time(),
        'last_seen' => time(),
        'closed' => false,
        'marker_b64' => base64_encode($encMarker),
        'trigger_id_hex' => $triggerIdHex,
    ]);
    // Initialise empty records files so reads don't 404 before writes.
    foreach (['out', 'in'] as $stream) {
        $rec = $dir . "/$stream.records";
        $idx = $dir . "/$stream.idx";
        if (!file_exists($rec)) { @file_put_contents($rec, ''); @chmod($rec, 0600); }
        if (!file_exists($idx)) { @file_put_contents($idx, ''); @chmod($idx, 0600); }
    }
    json_ok(['session_id' => $sid]);
}

function op_output_post(string $dataDir, int $maxBlob): void {
    op_records_post($dataDir, 'out', $maxBlob);
}
function op_input_post(string $dataDir, int $maxBlob): void {
    op_records_post($dataDir, 'in', $maxBlob);
}

function op_records_post(string $dataDir, string $stream, int $maxBlob): void {
    $body = json_in();
    $sid = require_sid($body);
    $dir = session_dir_or_404($dataDir, $sid);
    $records = $body['records'] ?? null;
    if (!is_array($records)) { json_err(400, 'records[] required'); }
    if (count($records) > 100) { json_err(413, 'too many records (max 100)'); }
    $rec = $dir . "/$stream.records";
    $idx = $dir . "/$stream.idx";

    // Single lockfile per (sid, stream). Replaces the prior dual-flock
    // pattern (which could AB/BA deadlock between concurrent writers and
    // silently degrade to no exclusion on NFS-backed hosts where flock
    // returns false). Fail loudly if flock won't take.
    $lockPath = $dir . "/.$stream.lock";
    $lh = @fopen($lockPath, 'c');
    if ($lh === false) { json_err(500, 'lock open failed'); }
    @chmod($lockPath, 0600);
    if (!@flock($lh, LOCK_EX)) {
        @fclose($lh);
        json_err(500, 'lock acquire failed');
    }

    $rh = null; $ih = null;
    $added = 0;
    $count_after = 0;
    try {
        $rh = @fopen($rec, 'ab');
        $ih = @fopen($idx, 'ab');
        if ($rh === false || $ih === false) {
            json_err(500, 'open failed');
        }
        clearstatcache(true, $rec);
        clearstatcache(true, $idx);
        $offset = (int)(@filesize($rec) ?: 0);
        $expected = (int)((@filesize($idx) ?: 0) / 4);

        foreach ($records as $r) {
            if (!is_array($r) || !isset($r['blob']) || !is_string($r['blob'])) continue;
            $seq = isset($r['seq']) ? (int)$r['seq'] : -1;
            $blob = base64_decode($r['blob'], true);
            if ($blob === false || strlen($blob) === 0 || strlen($blob) > $maxBlob) continue;
            // Strict ordering: seq MUST equal the next position. This prevents
            // accidental duplicates from retried POSTs (after a timeout when the
            // server actually got the first attempt).
            if ($seq !== $expected) {
                @fclose($rh); @fclose($ih); $rh = $ih = null;
                @flock($lh, LOCK_UN); @fclose($lh); $lh = null;
                http_response_code(409);
                @header('Content-Type: application/json');
                echo json_encode([
                    'error' => 'seq_conflict',
                    'expected_seq' => $expected,
                    'got_seq' => $seq,
                ]);
                return;
            }
            // Records-first, idx-second: until the 4-byte idx entry is
            // committed, readers don't see the record. If fwrite to
            // records is short or idx fails, ftruncate the records file
            // back to $offset so a dangling tail can't survive.
            $written = @fwrite($rh, $blob);
            if ($written === false || $written !== strlen($blob)) {
                if ($written !== false && $written > 0) {
                    @ftruncate($rh, $offset);
                }
                break;
            }
            @fflush($rh);
            $idxBytes = @fwrite($ih, pack('V', $offset));
            if ($idxBytes === false || $idxBytes !== 4) {
                @ftruncate($rh, $offset);
                break;
            }
            @fflush($ih);
            $offset += $written;
            $expected++;
            $added++;
        }
        $count_after = $expected;
    } finally {
        if ($rh) @fclose($rh);
        if ($ih) @fclose($ih);
        if ($lh) { @flock($lh, LOCK_UN); @fclose($lh); }
    }
    touch_public_meta($dir);
    json_ok([
        'ok' => true,
        'added' => $added,
        'total' => $count_after,
        'next_seq' => $count_after,
    ]);
}

function op_records_get_lp(string $dataDir, string $stream, int $deadlineSecs, int $sleepUs, int $maxChunk, int $defaultLimit, int $maxLimit): void {
    $sid = $_GET['session'] ?? '';
    if ($sid === '' || !preg_match('/^[a-f0-9]{6,64}$/', $sid)) { json_err(400, 'bad session'); }
    $dir = session_dir_or_404($dataDir, $sid);
    $sinceSeq = max(0, (int)($_GET['since_seq'] ?? 0));
    $limit = clamp_int((int)($_GET['limit'] ?? $defaultLimit), 1, $maxLimit);

    // Heartbeat: GET on input is the wrapper's polling — counts as alive.
    if ($stream === 'in') touch_public_meta($dir);

    @set_time_limit($deadlineSecs + 5);
    $rec = $dir . "/$stream.records";
    $idx = $dir . "/$stream.idx";
    $deadline = microtime(true) + $deadlineSecs;
    while (true) {
        clearstatcache(true, $idx);
        $totalRecords = (int)(filesize_safe($idx) / 4);
        if ($totalRecords > $sinceSeq) {
            $endSeq = min($totalRecords, $sinceSeq + $limit);
            $payload = read_records($rec, $idx, $sinceSeq, $endSeq, $maxChunk);
            json_ok($payload);
            return;
        }
        if (connection_aborted()) return;
        if (microtime(true) >= $deadline) {
            json_ok(['records' => [], 'next_seq' => $sinceSeq, 'total' => $totalRecords]);
            return;
        }
        usleep($sleepUs);
    }
}

function read_records(string $recordsFile, string $idxFile, int $sinceSeq, int $endSeq, int $maxChunk): array {
    $totalRecords = (int)(filesize_safe($idxFile) / 4);
    if ($sinceSeq >= $totalRecords) {
        return ['records' => [], 'next_seq' => $sinceSeq, 'total' => $totalRecords];
    }
    $endSeq = min($endSeq, $totalRecords);
    $ih = @fopen($idxFile, 'rb');
    $rh = @fopen($recordsFile, 'rb');
    if ($ih === false || $rh === false) {
        if ($ih) @fclose($ih);
        if ($rh) @fclose($rh);
        json_err(500, 'open read failed');
    }
    @fseek($ih, $sinceSeq * 4);
    $needOffsets = $endSeq - $sinceSeq + 1;
    $rawOff = (string)@fread($ih, $needOffsets * 4);
    @fclose($ih);
    $offsets = [];
    for ($i = 0; $i + 4 <= strlen($rawOff); $i += 4) {
        $u = unpack('V', substr($rawOff, $i, 4));
        $offsets[] = $u[1];
    }

    clearstatcache(true, $recordsFile);
    $recordsSize = filesize_safe($recordsFile);
    $records = [];
    $bytesDelivered = 0;
    $delivered = 0;
    // Guard against a partial idx write (writer crashed between two
    // 4-byte entries): never index beyond the offsets we actually read.
    $loopCount = min($endSeq - $sinceSeq, count($offsets));
    for ($i = 0; $i < $loopCount; $i++) {
        $start = $offsets[$i];
        $end = ($i + 1 < count($offsets)) ? $offsets[$i + 1] : $recordsSize;
        $len = max(0, $end - $start);
        @fseek($rh, $start);
        $blob = $len > 0 ? (string)@fread($rh, $len) : '';
        $records[] = ['seq' => $sinceSeq + $i, 'blob' => base64_encode($blob)];
        $bytesDelivered += $len;
        $delivered++;
        if ($bytesDelivered >= $maxChunk) break;
    }
    @fclose($rh);
    return [
        'records' => $records,
        'next_seq' => $sinceSeq + $delivered,
        'total' => $totalRecords,
    ];
}

function op_resize(string $dataDir): void {
    $body = json_in();
    $sid = require_sid($body);
    $dir = session_dir_or_404($dataDir, $sid);
    $meta = read_public_meta($dir);
    // Trigger-secret gate (same shape as op_close): RELAY_SECRET alone is
    // insufficient — a co-tenant holding it could otherwise mutate any
    // session's cols/rows. The legacy carve-out covers sessions registered
    // before trigger_id_hex became mandatory in op_register; new sessions
    // always carry the verifier.
    if (!empty($meta['trigger_id_hex'])) {
        $triggerSecretHex = require_hex_64($body, 'trigger_secret_hex');
        if (!verify_trigger_secret($triggerSecretHex, (string)$meta['trigger_id_hex'])) {
            json_err(401, 'bad trigger_secret');
        }
    }
    $meta['cols'] = clamp_int((int)($body['cols'] ?? ($meta['cols'] ?? 80)), 20, 500);
    $meta['rows'] = clamp_int((int)($body['rows'] ?? ($meta['rows'] ?? 24)), 8, 200);
    $meta['last_seen'] = time();
    write_public_meta($dir, $meta);
    json_ok(['ok' => true, 'cols' => $meta['cols'], 'rows' => $meta['rows']]);
}

function op_heartbeat(string $dataDir): void {
    $body = json_in();
    $sid = require_sid($body);
    $dir = session_dir_or_404($dataDir, $sid);
    $meta = read_public_meta($dir);
    // Trigger-secret gate: prevents a RELAY_SECRET-holder from artificially
    // keeping someone else's session alive past its natural GC point.
    // (The GET-input long-poll path also bumps last_seen and is not gated
    // — gating heartbeat closes the explicit-endpoint hole; gating the
    // poll would require a per-request secret in a URL we'd rather not
    // log everywhere. Documented imperfection.)
    if (!empty($meta['trigger_id_hex'])) {
        $triggerSecretHex = require_hex_64($body, 'trigger_secret_hex');
        if (!verify_trigger_secret($triggerSecretHex, (string)$meta['trigger_id_hex'])) {
            json_err(401, 'bad trigger_secret');
        }
    }
    touch_public_meta($dir);
    json_ok(['ok' => true]);
}

function op_close(string $dataDir): void {
    $body = json_in();
    $sid = require_sid($body);
    $dir = session_dir_or_404($dataDir, $sid);
    $meta = read_public_meta($dir);
    // op_close requires proof of token possession via trigger_secret_hex.
    // RELAY_SECRET alone is NOT sufficient — see op_register comment.
    //
    // Legacy carve-out: sessions registered before the trigger-secret
    // hardening rollout have no trigger_id_hex on disk. Allow closing
    // those without the secret check — the regime in force when they
    // were registered didn't have one. New sessions always carry the
    // verifier (op_register makes it mandatory), so this branch is
    // unreachable for anything created post-deploy.
    if (!empty($meta['trigger_id_hex'])) {
        $triggerSecretHex = require_hex_64($body, 'trigger_secret_hex');
        if (!verify_trigger_secret($triggerSecretHex, (string)$meta['trigger_id_hex'])) {
            json_err(401, 'bad trigger_secret');
        }
    }
    $meta['closed'] = true;
    $meta['closed_at'] = time();
    write_public_meta($dir, $meta);
    json_ok(['ok' => true]);
}

function op_sessions(string $dataDir, int $aliveTtl): void {
    // Returns ALL non-closed sessions, with `alive` flag (true if seen
    // within $aliveTtl seconds). The browser uses this so that a session
    // briefly offline (PC sleep, wifi blip) doesn't disappear from the UI;
    // it shows as offline until the wrapper reappears.
    $now = time();
    $out = [];
    foreach (glob($dataDir . '/*', GLOB_ONLYDIR) ?: [] as $dir) {
        $meta = read_public_meta($dir);
        if (!$meta) continue;
        if (!empty($meta['closed'])) continue; // Hide hard-closed sessions
        $idx_out = filesize_safe($dir . '/out.idx');
        $idx_in  = filesize_safe($dir . '/in.idx');
        $last_seen = (int)($meta['last_seen'] ?? 0);
        $out[] = [
            'id' => $meta['id'] ?? basename($dir),
            'cols' => $meta['cols'] ?? 80,
            'rows' => $meta['rows'] ?? 24,
            'started_at' => $meta['started_at'] ?? 0,
            'last_seen' => $last_seen,
            'alive' => ($now - $last_seen) <= $aliveTtl,
            'offline_secs' => max(0, $now - $last_seen),
            'out_count' => (int)($idx_out / 4),
            'in_count'  => (int)($idx_in / 4),
            'marker' => $meta['marker_b64'] ?? '',
        ];
    }
    json_ok(['sessions' => $out, 'alive_ttl' => $aliveTtl]);
}

function op_gc(string $dataDir, int $defaultClosedAge, int $defaultStaleAge): void {
    // Admin-only: requires Bearer of ADMIN_SECRET (separate from RELAY_SECRET).
    // Removes session dirs that are:
    //   - closed AND closed_at older than closed_age_secs (default 7d), OR
    //   - last_seen older than stale_age_secs (default 30d), OR
    //   - orphaned (no readable meta.public.json — usually leftovers from
    //     a partial register that never wrote meta).
    if (!check_admin_auth()) { json_err(401, 'admin auth required'); }
    $body = json_in();
    $closedAge = max(0, (int)($body['closed_age_secs'] ?? $defaultClosedAge));
    $staleAge  = max(0, (int)($body['stale_age_secs']  ?? $defaultStaleAge));
    $dryRun    = !empty($body['dry_run']);
    $now = time();
    $removed = []; $kept = 0;
    $realData = @realpath($dataDir);
    foreach (glob($dataDir . '/*', GLOB_ONLYDIR) ?: [] as $dir) {
        $name = basename($dir);
        // Treat only hex-shaped basenames as session dirs. This skips
        // data/push/ (the per-token-hash subscription tree), data/.cache/
        // siblings, and any co-tenant-injected dirs that would otherwise
        // hit the orphan branch and be wiped.
        if (!preg_match('/^[a-f0-9]{6,64}$/', $name)) {
            $kept++;
            continue;
        }
        // Realpath sanity: refuse to recurse into a dir whose canonical
        // path escapes $dataDir (defense-in-depth against symlink races
        // in case the data/ dir is ever writeable by a co-tenant).
        $real = @realpath($dir);
        if ($realData === false || $real === false
            || strncmp($real, $realData . DIRECTORY_SEPARATOR, strlen($realData) + 1) !== 0) {
            $kept++;
            continue;
        }
        $meta = read_public_meta($dir);
        $reason = null;
        if (!$meta) {
            // Be conservative: only treat orphans as removable if old enough
            // (otherwise we might race a register that's mid-write).
            $age = $now - (int)@filemtime($dir);
            if ($age > 3600) $reason = 'orphan';
        } elseif (!empty($meta['closed']) && (int)($meta['closed_at'] ?? 0) > 0
                  && $now - (int)$meta['closed_at'] > $closedAge) {
            $reason = 'closed-stale';
        } elseif ((int)($meta['last_seen'] ?? 0) > 0
                  && $now - (int)$meta['last_seen'] > $staleAge) {
            $reason = 'last-seen-stale';
        }
        if ($reason !== null) {
            $removed[] = ['id' => $name, 'reason' => $reason];
            if (!$dryRun) rrmdir($dir);
        } else {
            $kept++;
        }
    }
    json_ok([
        'removed' => $removed,
        'removed_count' => count($removed),
        'kept' => $kept,
        'dry_run' => $dryRun,
    ]);
}

function rrmdir(string $dir): void {
    if (!is_dir($dir)) return;
    // Top-level: if $dir is itself a symlink to a directory, is_dir
    // returns true (follows the link). Unlink the link instead of
    // recursing through whatever the target points at — otherwise GC
    // could wipe filesystem locations far from data/.
    if (is_link($dir)) { @unlink($dir); return; }
    foreach (scandir($dir) ?: [] as $f) {
        if ($f === '.' || $f === '..') continue;
        $p = $dir . '/' . $f;
        if (is_dir($p) && !is_link($p)) rrmdir($p);
        else @unlink($p);
    }
    @rmdir($dir);
}

function check_admin_auth(): bool {
    if (!defined('ADMIN_SECRET') || ADMIN_SECRET === '' || ADMIN_SECRET === 'CHANGE_ME') {
        return false; // GC is disabled until ADMIN_SECRET is configured.
    }
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if ($hdr === '' && function_exists('apache_request_headers')) {
        $h = apache_request_headers();
        $hdr = $h['Authorization'] ?? ($h['authorization'] ?? '');
    }
    if (!preg_match('/^Bearer\s+(.+)$/', $hdr, $m)) return false;
    return hash_equals(ADMIN_SECRET, trim($m[1]));
}

function op_meta(string $dataDir): void {
    $sid = $_GET['session'] ?? '';
    if ($sid === '' || !preg_match('/^[a-f0-9]{6,64}$/', $sid)) { json_err(400, 'bad session'); }
    $dir = session_dir_or_404($dataDir, $sid);
    $blob = @file_get_contents($dir . '/meta.bin');
    if ($blob === false) { json_err(404, 'no meta'); }
    json_ok(['encrypted_meta' => base64_encode($blob)]);
}

// ---- Push notifications --------------------------------------------------
//
// Plaintext-on-relay model: the relay holds the raw subscription endpoints
// (FCM/Mozilla/Apple URLs are opaque tokens — no PII) and triggers
// content-less Web Push messages on demand. Payloads are deliberately
// content-free; the SW shows a generic "needs attention" notification
// and the user opens the app to see what's actually being asked.
//
// Storage:
//   data/vapid.json              { public_b64u, private_pem } — auto-generated
//   data/push/<token_hash>/<sub_id>.json  — one file per browser subscription
// token_hash = SHA-256(token_bytes), hex-encoded; computed identically by
// the wrapper (Python) and the browser (WebCrypto), so neither side has to
// agree on a separate identifier with the relay.

function op_push_pubkey(string $dataDir): void {
    $vapid = vapid_keypair_load_or_generate($dataDir . '/vapid.json');
    json_ok(['public_b64u' => $vapid['public_b64u']]);
}

function op_push_subscribe(string $dataDir): void {
    $body = json_in();
    $tokenHash = (string)($body['token_hash'] ?? '');
    if (!preg_match('/^[a-f0-9]{64}$/', $tokenHash)) { json_err(400, 'bad token_hash'); }
    $endpoint = (string)($body['endpoint'] ?? '');
    $err = validate_push_endpoint($endpoint);
    if ($err !== null) { json_err(400, $err); }
    $keys = $body['keys'] ?? null;
    if (!is_array($keys) || empty($keys['p256dh']) || empty($keys['auth'])) {
        json_err(400, 'bad keys');
    }
    $p256dh = (string)$keys['p256dh'];
    $auth = (string)$keys['auth'];
    if (strlen($p256dh) > 200 || strlen($auth) > 100) { json_err(400, 'keys too large'); }
    // trigger_id binds this token_hash → trigger_id_hex so op_push_notify
    // can later require proof of token possession via trigger_secret_hex.
    $triggerIdHex = require_hex_64($body, 'trigger_id_hex');

    $pushParent = $dataDir . '/push';
    if (!is_dir($pushParent)) { @mkdir($pushParent, 0700, true); @chmod($pushParent, 0700); }
    $dir = $pushParent . '/' . $tokenHash;
    if (!is_dir($dir)) { @mkdir($dir, 0700, true); @chmod($dir, 0700); }
    if (!is_dir($dir)) { json_err(500, 'push dir not writable'); }

    // First subscriber for this token_hash establishes the trigger_id.
    // Subsequent subscribers must match it (anyone holding the same
    // device token derives the same one). Squatting a token_hash with
    // a different trigger_id is rejected.
    $tidPath = $dir . '/.trigger_id';
    if (file_exists($tidPath)) {
        $stored = trim((string)@file_get_contents($tidPath));
        if (!preg_match('/^[a-f0-9]{64}$/', $stored)
            || !hash_equals($stored, $triggerIdHex)) {
            json_err(401, 'trigger_id mismatch');
        }
    } else {
        @file_put_contents($tidPath, $triggerIdHex);
        @chmod($tidPath, 0600);
    }

    // De-dupe: if this exact endpoint is already subscribed, return its id.
    foreach (glob($dir . '/*.json') ?: [] as $f) {
        $j = json_decode(@file_get_contents($f) ?: '', true);
        if (is_array($j) && ($j['endpoint'] ?? '') === $endpoint) {
            json_ok(['id' => (string)$j['id']]);
        }
    }

    $subId = bin2hex(random_bytes(16));
    $rec = [
        'id' => $subId,
        'endpoint' => $endpoint,
        'keys' => ['p256dh' => $p256dh, 'auth' => $auth],
        'created' => time(),
        'last_status' => 0,
        'last_attempt' => 0,
    ];
    $tmp = $dir . '/' . $subId . '.json.tmp';
    @file_put_contents($tmp, json_encode($rec, JSON_UNESCAPED_SLASHES));
    @chmod($tmp, 0600);
    @rename($tmp, $dir . '/' . $subId . '.json');
    json_ok(['id' => $subId]);
}

function op_push_unsubscribe(string $dataDir): void {
    $body = json_in();
    $tokenHash = (string)($body['token_hash'] ?? '');
    $subId = (string)($body['id'] ?? '');
    if (!preg_match('/^[a-f0-9]{64}$/', $tokenHash)) { json_err(400, 'bad token_hash'); }
    if (!preg_match('/^[a-f0-9]{32}$/', $subId)) { json_err(400, 'bad id'); }
    $dir = $dataDir . '/push/' . $tokenHash;
    // Trigger-secret gate: stops a RELAY_SECRET-holder from kicking
    // someone else's browser off push by guessing/learning their sub_id.
    // If no .trigger_id is on disk yet, no one has ever subscribed under
    // this token_hash — the call is a no-op anyway, so let it pass.
    $tidPath = $dir . '/.trigger_id';
    if (file_exists($tidPath)) {
        $triggerSecretHex = require_hex_64($body, 'trigger_secret_hex');
        $stored = trim((string)@file_get_contents($tidPath));
        if (!verify_trigger_secret($triggerSecretHex, $stored)) {
            json_err(401, 'bad trigger_secret');
        }
    }
    $f = $dir . '/' . $subId . '.json';
    if (file_exists($f)) { @unlink($f); }
    json_ok(['ok' => true]);
}

function op_push_notify(string $dataDir): void {
    $body = json_in();
    $tokenHash = (string)($body['token_hash'] ?? '');
    if (!preg_match('/^[a-f0-9]{64}$/', $tokenHash)) { json_err(400, 'bad token_hash'); }
    $triggerSecretHex = require_hex_64($body, 'trigger_secret_hex');
    $dir = $dataDir . '/push/' . $tokenHash;
    if (!is_dir($dir)) { json_ok(['sent' => 0, 'removed' => 0]); }
    // trigger_id was persisted at first push_subscribe (one per token_hash).
    // Require it: if no subscriber has ever been registered for this hash
    // we have no verifier to compare against — but then there's also no
    // one to notify, so 200 with sent=0 is harmless.
    $tidPath = $dir . '/.trigger_id';
    if (!file_exists($tidPath)) { json_ok(['sent' => 0, 'removed' => 0]); }
    $stored = trim((string)@file_get_contents($tidPath));
    if (!verify_trigger_secret($triggerSecretHex, $stored)) {
        json_err(401, 'bad trigger_secret');
    }

    $vapid = vapid_keypair_load_or_generate($dataDir . '/vapid.json');
    $sub = vapid_sub_for_request();
    $sent = 0; $removed = 0; $failed = 0;
    foreach (glob($dir . '/*.json') ?: [] as $f) {
        $rec = json_decode(@file_get_contents($f) ?: '', true);
        if (!is_array($rec) || empty($rec['endpoint'])) continue;
        $aud = endpoint_origin((string)$rec['endpoint']);
        if ($aud === '') continue;
        try {
            $jwt = vapid_jwt($aud, $sub, (string)$vapid['private_pem']);
        } catch (Throwable $e) {
            $failed++;
            continue;
        }
        $resp = send_vapid_push((string)$rec['endpoint'], $jwt, (string)$vapid['public_b64u']);
        $rec['last_status'] = (int)$resp['code'];
        $rec['last_attempt'] = time();
        // 404/410 mean the subscription is permanently dead per RFC 8030.
        if ($resp['code'] === 404 || $resp['code'] === 410) {
            @unlink($f); $removed++;
            continue;
        }
        @file_put_contents($f, json_encode($rec, JSON_UNESCAPED_SLASHES));
        @chmod($f, 0600);
        if ($resp['code'] >= 200 && $resp['code'] < 300) { $sent++; }
        else { $failed++; }
    }
    json_ok(['sent' => $sent, 'removed' => $removed, 'failed' => $failed]);
}

// ---- VAPID helpers --------------------------------------------------------

function vapid_keypair_load_or_generate(string $path): array {
    // Fast path: keypair already on disk.
    $existing = _vapid_read_if_valid($path);
    if ($existing !== null) return $existing;

    // Slow path: generate under exclusive lock so concurrent first-requests
    // don't race two keypairs (whichever rename wins replaces the other,
    // and subscriptions tied to the loser's public key go silently dead —
    // signature verification at the push service mismatches the key the
    // browser cached at subscribe time).
    $lockPath = dirname($path) . '/.vapid.lock';
    $lh = @fopen($lockPath, 'c');
    if ($lh) { @chmod($lockPath, 0600); @flock($lh, LOCK_EX); }
    try {
        // Re-check after acquiring the lock — another worker may have
        // written the keypair while we waited.
        $existing = _vapid_read_if_valid($path);
        if ($existing !== null) return $existing;

        $res = openssl_pkey_new([
            'curve_name' => 'prime256v1',
            'private_key_type' => OPENSSL_KEYTYPE_EC,
        ]);
        if (!$res) { json_err(500, 'vapid keygen failed'); }
        $privPem = '';
        if (!openssl_pkey_export($res, $privPem)) {
            if (PHP_VERSION_ID < 80000 && is_resource($res)) @openssl_free_key($res);
            json_err(500, 'vapid export failed');
        }
        $det = openssl_pkey_get_details($res);
        if (PHP_VERSION_ID < 80000 && is_resource($res)) @openssl_free_key($res);
        if (!$det || empty($det['ec']['x']) || empty($det['ec']['y'])) {
            json_err(500, 'vapid details unavailable');
        }
        // Pad to 32 bytes (PHP can strip leading zero bytes).
        $x = str_pad($det['ec']['x'], 32, "\x00", STR_PAD_LEFT);
        $y = str_pad($det['ec']['y'], 32, "\x00", STR_PAD_LEFT);
        $pubRaw = "\x04" . $x . $y;  // 65-byte uncompressed P-256 point.
        $pubB64u = b64url_encode($pubRaw);
        $rec = ['public_b64u' => $pubB64u, 'private_pem' => $privPem, 'created' => time()];
        $tmp = $path . '.tmp';
        @file_put_contents($tmp, json_encode($rec));
        @chmod($tmp, 0600);
        @rename($tmp, $path);
        return $rec;
    } finally {
        if ($lh) { @flock($lh, LOCK_UN); @fclose($lh); }
    }
}

function _vapid_read_if_valid(string $path): ?array {
    $j = @file_get_contents($path);
    if ($j === false || $j === '') return null;
    $a = json_decode($j, true);
    if (is_array($a) && !empty($a['public_b64u']) && !empty($a['private_pem'])) {
        return $a;
    }
    return null;
}

function vapid_jwt(string $aud, string $sub, string $privPem): string {
    $now = time();
    $hdr  = b64url_encode(json_encode(['typ' => 'JWT', 'alg' => 'ES256']));
    $body = b64url_encode(json_encode([
        'aud' => $aud,
        'exp' => $now + 12 * 3600,
        // iat is RFC 7519 optional but Apple Web Push rejects JWTs that
        // lack it. Cost: zero.
        'iat' => $now,
        'sub' => $sub,
    ]));
    $signing = $hdr . '.' . $body;
    $sig = vapid_sign_es256($signing, $privPem);
    return $signing . '.' . b64url_encode($sig);
}

// PHP's openssl_sign emits a DER-encoded ECDSA signature. JWT (RFC 7515)
// requires the raw 64-byte form: r (32) || s (32). DER is:
//   30 LL 02 RL <r> 02 SL <s>
// where r/s may be 33 bytes when the high bit is set (DER prepends 0x00).
function vapid_sign_es256(string $data, string $privPem): string {
    $key = openssl_pkey_get_private($privPem);
    if (!$key) { throw new RuntimeException('bad vapid private key'); }
    try {
        $sig = '';
        if (!openssl_sign($data, $sig, $key, OPENSSL_ALGO_SHA256)) {
            throw new RuntimeException('vapid sign failed');
        }
        return der_ecdsa_to_jose($sig);
    } finally {
        // openssl_free_key is a no-op (and removed) on PHP 8+; OpenSSLAsymmetricKey
        // is GC'd. On PHP 7 the resource leaks unless freed explicitly.
        if (PHP_VERSION_ID < 80000 && is_resource($key)) @openssl_free_key($key);
    }
}

function der_ecdsa_to_jose(string $der): string {
    $len = strlen($der);
    if ($len < 8 || $der[0] !== "\x30") { throw new RuntimeException('bad DER'); }
    $i = 1;
    $b = ord($der[$i++]);
    if ($b & 0x80) { $i += $b & 0x7f; }  // long form, skip the length bytes
    if ($der[$i++] !== "\x02") { throw new RuntimeException('bad DER r'); }
    $rlen = ord($der[$i++]);
    $r = substr($der, $i, $rlen); $i += $rlen;
    if ($der[$i++] !== "\x02") { throw new RuntimeException('bad DER s'); }
    $slen = ord($der[$i++]);
    $s = substr($der, $i, $slen);
    $r = ltrim($r, "\x00");
    $s = ltrim($s, "\x00");
    if (strlen($r) > 32 || strlen($s) > 32) { throw new RuntimeException('r/s overflow'); }
    return str_pad($r, 32, "\x00", STR_PAD_LEFT) . str_pad($s, 32, "\x00", STR_PAD_LEFT);
}

function send_vapid_push(string $endpoint, string $jwt, string $pubB64u): array {
    // Re-validate at send time AND resolve our own IPs that we then pin
    // into curl via CURLOPT_RESOLVE. Without the pin, there's a TOCTOU
    // window between validate_push_endpoint's DNS lookup and curl's own
    // resolution — a DNS rebinder controlling a push hostname could slip
    // a private IP through that window. Pinning forces curl to connect
    // to the addresses we already filtered.
    $ips = _resolve_safe_ips_for_send($endpoint);
    if ($ips === null) {
        return ['code' => 0, 'body' => 'endpoint rejected at send time'];
    }
    $p = @parse_url($endpoint);
    $host = strtolower((string)($p['host'] ?? ''));
    $port = isset($p['port']) ? (int)$p['port'] : 443;
    if ($host === '') {
        return ['code' => 0, 'body' => 'endpoint rejected at send time'];
    }
    // CURLOPT_RESOLVE format: "HOST:PORT:IP[,IP,...]". Pass one entry that
    // lists every validated IP so curl falls forward through them if any
    // one connection fails.
    $resolveEntries = [$host . ':' . $port . ':' . implode(',', $ips)];
    $ch = curl_init($endpoint);
    if ($ch === false) { return ['code' => 0, 'body' => '']; }
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => '',
        CURLOPT_HTTPHEADER => [
            "Authorization: vapid t={$jwt}, k={$pubB64u}",
            'TTL: 60',
            'Content-Length: 0',
        ],
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 10,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_RESOLVE => $resolveEntries,
    ]);
    $body = curl_exec($ch);
    $code = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    return ['code' => $code, 'body' => is_string($body) ? $body : ''];
}

// Resolve an endpoint's host to a list of "safe" IPs (not private,
// not reserved, both v4 and v6). Returns null if validate_push_endpoint
// rejects the URL OR if any resolved address fails the safety filter
// (fail-closed — one private record in a recordset is treated as the
// whole hostname being compromised).
function _resolve_safe_ips_for_send(string $url): ?array {
    if (validate_push_endpoint($url) !== null) return null;
    $p = @parse_url($url);
    if (!$p || empty($p['host'])) return null;
    $host = strtolower((string)$p['host']);
    return _resolve_host_safe_ips($host);
}

// Resolve $host via DNS_A | DNS_AAAA and return [ip, ...] only if every
// returned address passes _push_ip_safe. Returns null on resolve failure
// or any unsafe IP. PUSH_ALLOW_INSECURE_DEV bypasses the IP safety check
// so the test suite's mock server on 127.0.0.1 isn't filtered out.
function _resolve_host_safe_ips(string $host): ?array {
    $dev = defined('PUSH_ALLOW_INSECURE_DEV') && PUSH_ALLOW_INSECURE_DEV;
    // Host might already be a literal IP (the test mock uses 127.0.0.1);
    // dns_get_record returns nothing useful for IP literals so handle
    // them inline.
    if (filter_var($host, FILTER_VALIDATE_IP)) {
        if (!$dev && !_push_ip_safe($host)) return null;
        return [$host];
    }
    $records = @dns_get_record($host, DNS_A | DNS_AAAA);
    if (!is_array($records) || empty($records)) return null;
    $ips = [];
    foreach ($records as $r) {
        $ip = $r['ip'] ?? $r['ipv6'] ?? null;
        if (!$ip) continue;
        // Fail-closed (production): any private/reserved record poisons
        // the whole hostname for this request. A rebinder could otherwise
        // return [safe, unsafe] and rely on curl picking the unsafe one.
        if (!$dev && !_push_ip_safe($ip)) return null;
        $ips[] = $ip;
    }
    return empty($ips) ? null : $ips;
}

function endpoint_origin(string $url): string {
    $p = @parse_url($url);
    if (!$p || empty($p['host'])) return '';
    $scheme = $p['scheme'] ?? 'https';
    $host = $p['host'];
    $port = isset($p['port']) ? ":{$p['port']}" : '';
    return "{$scheme}://{$host}{$port}";
}

// Approved push-service hostnames. Without an allowlist, any
// RELAY_SECRET-holder could subscribe an internal URL (cloud metadata
// services, in-VPC Redis/memcached, link-local, etc.) and turn the
// relay into an SSRF proxy from the shared host's network position.
// Entries are either an exact hostname or a leading "*." for any
// non-empty subdomain match. Wrapped in a function (rather than a
// top-level `const`) so it's hoisted and reachable from the dispatch
// switch above without ordering games.
function push_host_allowlist(): array {
    return [
        'fcm.googleapis.com',                  // Chrome / Edge / Firefox-on-Android
        'updates.push.services.mozilla.com',   // Firefox
        'web.push.apple.com',                  // Safari / iOS / macOS
        '*.notify.windows.com',                // Edge Legacy / WNS
    ];
}

// Return null on success, or an error string. Called from
// op_push_subscribe (gate at storage time) and send_vapid_push
// (gate at send time, defends against DNS rebinding).
function validate_push_endpoint(string $url): ?string {
    if ($url === '') return 'endpoint required';
    if (strlen($url) > 1024) return 'endpoint too long';
    // Reject any '@' anywhere — defense against parse_url ambiguity on
    // 'https://user@evil.com@fcm.googleapis.com/...' which different
    // PHP versions resolve to different hosts. Legitimate push
    // endpoints never contain '@'.
    if (strpos($url, '@') !== false) return 'endpoint contains @';
    $p = @parse_url($url);
    if (!$p || empty($p['host'])) return 'endpoint unparseable';
    if (!empty($p['user']) || !empty($p['pass'])) return 'endpoint userinfo';

    // Test-only escape hatch: a config flag explicitly named INSECURE
    // turns off all strict checks so the test suite can use a local
    // mock push server. Production deployments MUST NOT set this.
    if (defined('PUSH_ALLOW_INSECURE_DEV') && PUSH_ALLOW_INSECURE_DEV) {
        return null;
    }

    $scheme = $p['scheme'] ?? '';
    if ($scheme !== 'https') return 'endpoint scheme must be https';
    $host = strtolower((string)$p['host']);
    $port = isset($p['port']) ? (int)$p['port'] : 443;
    if ($port !== 443) return 'endpoint port must be 443';
    if (!_push_host_allowed($host)) return 'endpoint host not in allowlist';
    // dns_get_record covers BOTH A and AAAA — gethostbynamel was v4-only
    // and would happily skip an AAAA pointing at fc00::/7 or fe80::/10,
    // letting a rebinder slip a ULA / link-local v6 through.
    $records = @dns_get_record($host, DNS_A | DNS_AAAA);
    if (!is_array($records) || empty($records)) return 'endpoint host unresolvable';
    $any = false;
    foreach ($records as $r) {
        $ip = $r['ip'] ?? $r['ipv6'] ?? null;
        if (!$ip) continue;
        $any = true;
        if (!_push_ip_safe($ip)) return 'endpoint host resolves to a private/reserved IP';
    }
    if (!$any) return 'endpoint host unresolvable';
    return null;
}

function _push_host_allowed(string $host): bool {
    foreach (push_host_allowlist() as $pat) {
        if (strpos($pat, '*.') === 0) {
            $suffix = substr($pat, 1);  // ".notify.windows.com"
            if (strlen($host) > strlen($suffix)
                && substr($host, -strlen($suffix)) === $suffix) {
                return true;
            }
        } elseif ($host === $pat) {
            return true;
        }
    }
    return false;
}

function _push_ip_safe(string $ip): bool {
    // FILTER_FLAG_NO_PRIV_RANGE: rejects 10/8, 172.16/12, 192.168/16, fc00::/7
    // FILTER_FLAG_NO_RES_RANGE: rejects 0/8, 127/8, 169.254/16, ::1, fe80::/10, etc.
    return (bool)filter_var(
        $ip,
        FILTER_VALIDATE_IP,
        FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE
    );
}

function vapid_sub_for_request(): string {
    // VAPID `sub` claim — RFC 8292 requires a mailto: or https: URI. Push
    // services don't actually contact it; it's an informational field.
    $host = $_SERVER['HTTP_HOST'] ?? 'localhost';
    $host = preg_replace('/[^a-z0-9.\-]/i', '', $host);
    return 'mailto:termpilot-relay@' . ($host !== '' ? $host : 'localhost');
}

function b64url_encode(string $s): string {
    return rtrim(strtr(base64_encode($s), '+/', '-_'), '=');
}

// ---- Helpers --------------------------------------------------------------

function auth_required(): bool {
    return defined('RELAY_SECRET') && RELAY_SECRET !== '' && RELAY_SECRET !== 'CHANGE_ME';
}

function check_auth(): bool {
    if (!auth_required()) return true;
    $hdr = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if ($hdr === '' && function_exists('apache_request_headers')) {
        $h = apache_request_headers();
        $hdr = $h['Authorization'] ?? ($h['authorization'] ?? '');
    }
    if (!preg_match('/^Bearer\s+(.+)$/', $hdr, $m)) return false;
    return hash_equals(RELAY_SECRET, trim($m[1]));
}

function require_method(string $m): void {
    if ($_SERVER['REQUEST_METHOD'] !== $m) { json_err(405, 'method not allowed'); }
}

function require_sid(array $body): string {
    $sid = (string)($body['session_id'] ?? $_GET['session'] ?? '');
    if ($sid === '' || !preg_match('/^[a-f0-9]{6,64}$/', $sid)) { json_err(400, 'bad session id'); }
    return $sid;
}

function require_hex_64(array $body, string $key): string {
    $v = (string)($body[$key] ?? '');
    if (!preg_match('/^[a-f0-9]{64}$/', $v)) { json_err(400, "$key required (64 lowercase hex)"); }
    return $v;
}

function verify_trigger_secret(string $secretHex, string $expectedIdHex): bool {
    if (!preg_match('/^[a-f0-9]{64}$/', $secretHex)) return false;
    if (!preg_match('/^[a-f0-9]{64}$/', $expectedIdHex)) return false;
    $secret = @hex2bin($secretHex);
    $expected = @hex2bin($expectedIdHex);
    if ($secret === false || $expected === false) return false;
    return hash_equals($expected, hash('sha256', $secret, true));
}

function require_b64_blob(array $body, string $key, int $maxBytes): string {
    if (!isset($body[$key]) || !is_string($body[$key])) {
        json_err(400, "$key required");
    }
    $raw = base64_decode($body[$key], true);
    if ($raw === false) { json_err(400, "$key not base64"); }
    if (strlen($raw) === 0) { json_err(400, "$key empty"); }
    if (strlen($raw) > $maxBytes) { json_err(413, "$key too large"); }
    return $raw;
}

function session_dir_or_404(string $dataDir, string $sid): string {
    $dir = $dataDir . '/' . $sid;
    if (!is_dir($dir)) { json_err(404, 'no session'); }
    return $dir;
}

function read_public_meta(string $dir): array {
    $j = @file_get_contents($dir . '/meta.public.json');
    if ($j === false) return [];
    $a = json_decode($j, true);
    return is_array($a) ? $a : [];
}

function write_public_meta(string $dir, array $meta): void {
    $tmp = $dir . '/meta.public.json.tmp';
    $json = json_encode($meta, JSON_UNESCAPED_SLASHES);
    if ($json === false) return;
    // Don't @-suppress the write: disk-full / perms errors should land in
    // error_log rather than silently producing an empty meta.public.json.
    // The rename below is still suppressed because PHP can emit a notice
    // when the source doesn't exist (which only happens if the write
    // above failed — already logged).
    $bytes = file_put_contents($tmp, $json);
    if ($bytes === false) return;
    @chmod($tmp, 0600);
    @rename($tmp, $dir . '/meta.public.json');
}

function touch_public_meta(string $dir): void {
    $meta = read_public_meta($dir);
    $meta['last_seen'] = time();
    write_public_meta($dir, $meta);
}

function filesize_safe(string $path): int {
    clearstatcache(true, $path);
    $s = @filesize($path);
    return $s === false ? 0 : (int)$s;
}

function clamp_int(int $v, int $min, int $max): int {
    if ($v < $min) return $min;
    if ($v > $max) return $max;
    return $v;
}

function json_in(): array {
    // Cap incoming body. 10 MB is well over the legit ceiling: a single
    // record blob is capped at 1 MB and op_records_post caps the array
    // length at 100, so a fully-loaded POST is ~1.4 MB after JSON+b64
    // overhead. Anything bigger is a DoS attempt or a misuse.
    $cl = (int)($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($cl > 10 * 1024 * 1024) { json_err(413, 'request body too large'); }
    $raw = file_get_contents('php://input');
    if ($raw === '' || $raw === false) return [];
    if (strlen($raw) > 10 * 1024 * 1024) { json_err(413, 'request body too large'); }
    $a = json_decode($raw, true);
    if (!is_array($a)) { json_err(400, 'bad json'); }
    return $a;
}

function ensure_data_perms(string $dataDir): void {
    // Sentinel-gated: only run once per deploy. The file is harmless if
    // deleted by hand — it'll just walk again on the next request.
    $sentinel = $dataDir . '/.perms-v1';
    if (file_exists($sentinel)) return;
    $stack = [$dataDir];
    $budget = 5000;  // guard against runaway loops on huge data dirs.
    while ($stack && $budget-- > 0) {
        $d = array_pop($stack);
        @chmod($d, 0700);
        foreach (scandir($d) ?: [] as $f) {
            if ($f === '.' || $f === '..') continue;
            $p = $d . '/' . $f;
            if (is_link($p)) continue;
            if (is_dir($p)) { $stack[] = $p; }
            else { @chmod($p, 0600); }
        }
    }
    @file_put_contents($sentinel, (string)time());
    @chmod($sentinel, 0600);
}

function json_ok(array $obj): void {
    @header('Content-Type: application/json');
    echo json_encode($obj);
    exit;
}

function json_err(int $code, string $msg): void {
    http_response_code($code);
    @header('Content-Type: application/json');
    echo json_encode(['error' => $msg]);
    exit;
}
