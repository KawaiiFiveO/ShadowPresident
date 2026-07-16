// Edge front door for the Shadow President control panel.
// Deployed as a Cloudflare Worker on the route  shadow.<yourdomain>/*
//
// Runs on Cloudflare's edge, so it answers even when the home machine is off.
// Two jobs:
//   1. Read-only  — only GET/HEAD reach the origin; the panel's controls
//                   (POST /ask, POST /quit, DELETE /memory) are blocked with 403.
//   2. Offline    — when the tunnel/Flask is unreachable, serve a friendly
//                   auto-refreshing page instead of Cloudflare's raw 5xx error.
//
// The game talks to Flask on localhost directly and never passes through here,
// so blocking writes at the edge does not affect automation.

export default {
  async fetch(request) {
    // Read-only gate: reads are GET (HEAD for probes). Everything else is a control.
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("This is a read-only view.", {
        status: 403,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }

    try {
      // fetch(request) goes to the tunnel origin — Cloudflare's loop prevention
      // stops it from re-entering this same Worker. Cloudflare adds Cf-Ray / Cdn-Loop
      // to the origin request, which is how Flask detects the read-only remote view.
      const resp = await fetch(request);

      // Origin/tunnel-down signals: 502 (Flask refused), 504, and the 52x/530
      // family (tunnel has no healthy connection). A genuine app 500 is left alone.
      if (resp.status === 502 || resp.status === 504 || resp.status >= 520) {
        return offlinePage();
      }
      return resp;
    } catch (_e) {
      return offlinePage();
    }
  },
};

function offlinePage() {
  return new Response(OFFLINE_HTML, {
    status: 503,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "retry-after": "30",
      "cache-control": "no-store",
    },
  });
}

const OFFLINE_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Shadow President — offline</title>
<style>
  html,body{height:100%;margin:0}
  body{display:flex;align-items:center;justify-content:center;
       font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
       background:#0d0f14;color:#e6e6e6}
  .card{text-align:center;padding:2rem 2.5rem;max-width:32rem}
  h1{font-size:1.4rem;margin:0 0 .5rem;letter-spacing:.02em}
  p{margin:.4rem 0;color:#9aa0aa;line-height:1.5}
  .dot{display:inline-block;width:.55rem;height:.55rem;border-radius:50%;
       background:#c2453c;margin-right:.5rem;vertical-align:middle}
  .muted{font-size:.85rem;color:#5f6570;margin-top:1.25rem}
</style>
</head>
<body>
  <div class="card">
    <h1><span class="dot"></span>The service isn't running right now</h1>
    <p>The Shadow President server is offline. This page will refresh automatically when it comes back.</p>
    <p class="muted">Retrying every 60 seconds…</p>
  </div>
</body>
</html>`;
