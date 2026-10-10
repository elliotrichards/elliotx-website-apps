export interface ContactMessage {
  name: string;
  email: string;
  message: string;
}

export type ParseResult =
  | { kind: 'ok'; message: ContactMessage; token: string }
  | { kind: 'honeypot' }
  | { kind: 'invalid'; error: string };

export const LIMITS = { name: 100, email: 254, message: 5000 } as const;

// Deliberately loose — the real check is whether a reply ever arrives. This
// only rejects obvious junk before spending a Turnstile call on it.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function field(body: Record<string, unknown>, key: string): string {
  const value = body[key];
  return typeof value === 'string' ? value.trim() : '';
}

export function parseSubmission(body: unknown): ParseResult {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return { kind: 'invalid', error: 'invalid request body' };
  }
  const record = body as Record<string, unknown>;

  // Hidden "website" field: people never see it, naive bots fill in every
  // input they find. Reported to the caller as success so bots get no signal.
  if (field(record, 'website') !== '') return { kind: 'honeypot' };

  const name = field(record, 'name');
  const email = field(record, 'email');
  const message = field(record, 'message');
  const token = field(record, 'token');

  if (!name || name.length > LIMITS.name)
    return { kind: 'invalid', error: 'Please enter your name.' };
  if (email.length > LIMITS.email || !EMAIL_PATTERN.test(email)) {
    return { kind: 'invalid', error: 'Please enter a valid email address.' };
  }
  if (!message || message.length > LIMITS.message) {
    return { kind: 'invalid', error: 'Please enter a message.' };
  }
  if (!token) return { kind: 'invalid', error: 'Please complete the captcha check.' };

  return { kind: 'ok', message: { name, email, message }, token };
}
