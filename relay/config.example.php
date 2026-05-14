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

// Optional: tune the live/offline cutoff (seconds since last heartbeat).
// Default 300s (5 min). Sessions older are returned with alive=false; they
// remain visible (not hidden) until auto-GC removes them.
// define('ALIVE_TTL_SECS', 300);

// Optional: auto-GC cutoffs. Every op_close triggers a cleanup pass with
// these thresholds. Defaults shown.
// define('AUTO_GC_CLOSED_SECS', 5 * 60);    // cleanly-closed sessions
// define('AUTO_GC_STALE_SECS',  60 * 60);   // wrapper went silent (might be a network blip)
