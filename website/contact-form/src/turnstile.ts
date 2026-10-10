const SITEVERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';

interface SiteverifyResponse {
  success: boolean;
  hostname?: string;
  'error-codes'?: string[];
}

// Server-side half of the captcha. The widget on the page only produces a
// token; without this check a bot could skip the widget and POST directly.
// Tokens are single-use and expire after 5 minutes, so a replayed token fails.
export async function verifyTurnstile(
  secret: string,
  token: string,
  remoteIp: string | undefined,
  expectedHostname: string | undefined,
): Promise<boolean> {
  const form = new URLSearchParams({ secret, response: token });
  if (remoteIp) form.set('remoteip', remoteIp);

  const res = await fetch(SITEVERIFY_URL, {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`turnstile siteverify returned ${res.status}`);

  const data = (await res.json()) as SiteverifyResponse;
  if (!data.success) {
    console.warn('turnstile rejected token', data['error-codes']);
    return false;
  }
  // Stops a token minted for some other site that happens to share our
  // widget being accepted here. Unset in local dev, where Cloudflare's test
  // keys report hostname "example.com".
  if (expectedHostname && data.hostname !== expectedHostname) {
    console.warn('turnstile hostname mismatch', data.hostname);
    return false;
  }
  return true;
}
