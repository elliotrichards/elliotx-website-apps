import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { sendContactEmail } from './email';
import { RateLimiter } from './rateLimit';
import { verifyTurnstile } from './turnstile';
import { parseSubmission } from './validate';

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    console.error(`missing required environment variable ${name}`);
    process.exit(1);
  }
  return value;
}

const port = Number(process.env.PORT) || 8080;
const allowedOrigin = process.env.ALLOWED_ORIGIN || 'https://elliotx.com';
const allowedHost = new URL(allowedOrigin).host;
const turnstileSecret = requireEnv('TURNSTILE_SECRET_KEY');
const turnstileHostname = process.env.TURNSTILE_HOSTNAME || undefined;
const email = {
  apiKey: requireEnv('RESEND_API_KEY'),
  from: requireEnv('CONTACT_FROM'),
  to: requireEnv('CONTACT_TO'),
};

const MAX_BODY_BYTES = 16 * 1024;
// Per instance, in memory: a backstop behind Turnstile, not the main defence.
const limiter = new RateLimiter(5, 60 * 60 * 1000);

function send(res: ServerResponse, status: number, body: object): void {
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(body));
}

function header(req: IncomingMessage, name: string): string | undefined {
  const value = req.headers[name];
  return (Array.isArray(value) ? value[0] : value)?.trim() || undefined;
}

// The visitor's address, for the rate limit and Turnstile's remoteip.
// elliotx.com is served by Firebase Hosting, which calls this service through
// Cloud Run's front end. Each proxy appends the address it received the
// request from to X-Forwarded-For, so the chain ends "<visitor>, <firebase>":
// the last entry is Firebase's egress address (a Google IP, the same for
// every visitor) and the one before it is the visitor. Anything earlier was
// sent by the client and can be forged. CF-Connecting-IP isn't used: with
// Cloudflare set to DNS only, any caller could set it to dodge the limit.
// A caller hitting the *.run.app URL directly can still forge the
// second-from-last entry, which only defeats the rate limit (the host check
// turns most such calls away, and Turnstile still applies).
function clientIp(req: IncomingMessage): string | undefined {
  const hops =
    header(req, 'x-forwarded-for')
      ?.split(',')
      .map((h) => h.trim())
      .filter(Boolean) ?? [];
  return hops.at(-2) || hops.at(-1) || req.socket.remoteAddress || undefined;
}

// The host the visitor addressed. The Google load balancer passes Host
// through unchanged; Firebase Hosting calls the service on its *.run.app
// host and puts the site's host in X-Forwarded-Host. A direct call to the
// *.run.app URL carries neither as elliotx.com, so it's turned away here.
// Both headers can be forged by a script, so this only filters casual
// direct calls; Turnstile (checked against TURNSTILE_HOSTNAME) is the real
// defence.
function requestHost(req: IncomingMessage): string | undefined {
  const forwarded = header(req, 'x-forwarded-host')?.split(',')[0]?.trim();
  return (forwarded || header(req, 'host'))?.toLowerCase();
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  let size = 0;
  const chunks: Buffer[] = [];
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > MAX_BODY_BYTES) throw new RangeError('body too large');
    chunks.push(chunk as Buffer);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

const server = createServer(async (req, res) => {
  const path = new URL(req.url ?? '/', 'http://localhost').pathname;
  if (path !== '/api/contact') {
    send(res, 404, { error: 'not found' });
    return;
  }
  // Same-origin only: the form is served from elliotx.com and posts back to
  // elliotx.com/api/contact through Firebase Hosting, so no CORS headers are
  // sent and other sites' pages can't submit on a visitor's behalf.
  if (req.method !== 'POST') {
    send(res, 405, { error: 'method not allowed' });
    return;
  }
  if (req.headers.origin !== allowedOrigin || requestHost(req) !== allowedHost) {
    // Only header values the caller chose, no body or IP. Lets the logs show
    // what a proxy (load balancer, Firebase Hosting) actually forwards.
    console.warn(
      JSON.stringify({
        message: 'rejected: origin/host mismatch',
        origin: header(req, 'origin') ?? null,
        host: header(req, 'host') ?? null,
        xForwardedHost: header(req, 'x-forwarded-host') ?? null,
        // Hop count only, no addresses: shows the X-Forwarded-For layout
        // the proxies produce without logging visitors' IPs.
        xForwardedForHops: header(req, 'x-forwarded-for')?.split(',').length ?? 0,
      }),
    );
    send(res, 403, { error: 'forbidden' });
    return;
  }
  if (!req.headers['content-type']?.startsWith('application/json')) {
    send(res, 415, { error: 'expected application/json' });
    return;
  }

  let body: unknown;
  try {
    body = await readJson(req);
  } catch (err) {
    send(res, err instanceof RangeError ? 413 : 400, { error: 'invalid request body' });
    return;
  }

  const parsed = parseSubmission(body);
  if (parsed.kind === 'honeypot') {
    send(res, 200, { ok: true });
    return;
  }
  if (parsed.kind === 'invalid') {
    send(res, 400, { error: parsed.error });
    return;
  }

  // Counted after validation so a few typo'd attempts don't lock someone out;
  // only submissions that would cost a Turnstile + email call count.
  const ip = clientIp(req);
  if (!limiter.allow(ip ?? 'unknown')) {
    send(res, 429, { error: 'Too many messages. Please try again later.' });
    return;
  }

  try {
    const human = await verifyTurnstile(turnstileSecret, parsed.token, ip, turnstileHostname);
    if (!human) {
      send(res, 403, { error: 'The captcha check failed. Please try again.' });
      return;
    }
    await sendContactEmail(email, parsed.message);
    send(res, 200, { ok: true });
  } catch (err) {
    console.error(err);
    send(res, 502, { error: 'Your message could not be sent. Please try again later.' });
  }
});

server.listen(port, () => {
  console.log(`listening on ${port}`);
});
