// Startup. This file must be LAST in manifest.json's
// `background.scripts` so every listener above is registered and
// every handler is defined before we open the initial SSEs.
//
// The watchdog (in bg-watchdog.js) may also call ensureAllConnected
// on its first tick, but running it here makes the SSEs come up
// immediately on extension load instead of waiting up to 500ms.

ensureAllConnected();