<?php
// Optional: copy this file to config.php and set RELAY_SECRET to a
// random value to gate the relay with HTTP Bearer auth. Without it,
// the relay runs OPEN — anyone who finds the URL can register fake
// sessions and write encrypted noise to existing ones (a DoS / disk-
// spam vector, NOT a confidentiality leak: content remains AES-GCM-
// encrypted under per-device tokens regardless).
//
// Generate one with:  openssl rand -hex 32
//
// Strongly recommended for any non-throwaway deployment. The browser
// hides the secret-input field automatically when RELAY_SECRET is
// unset (probed via ?op=auth_required).
// define('RELAY_SECRET', 'CHANGE_ME');

// Optional: separate admin secret for the ?op=gc endpoint. Leave unset
// (or 'CHANGE_ME') to disable GC entirely. Generate with `openssl rand -hex 32`.
// Use from a host-side cron, e.g.:
//   curl -X POST -H "Authorization: Bearer $ADMIN_SECRET" \
//        -H "Content-Type: application/json" -d '{}' \
//        https://your.host/SERVICES/termpilot/relay.php?op=gc
// define('ADMIN_SECRET', 'CHANGE_ME');

// Optional: tune the live/offline cutoff (seconds since last heartbeat).
// Default 300s (5 min). Sessions older are returned with alive=false; they
// remain visible (not hidden) until either ?op=close or GC removes them.
// define('ALIVE_TTL_SECS', 300);

// Optional: GC cutoffs in seconds. Defaults shown.
// define('GC_CLOSED_AGE_SECS', 7 * 24 * 3600);   // 7d after close
// define('GC_STALE_AGE_SECS', 30 * 24 * 3600);   // 30d since last_seen
