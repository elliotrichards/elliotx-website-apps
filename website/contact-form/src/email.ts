import type { ContactMessage } from './validate';

const RESEND_URL = 'https://api.resend.com/emails';

export interface EmailConfig {
  apiKey: string;
  from: string;
  to: string;
}

// Email headers are single-line; strip anything that could start a new one.
function oneLine(value: string): string {
  return value.replace(/[\r\n]+/g, ' ');
}

export async function sendContactEmail(config: EmailConfig, msg: ContactMessage): Promise<void> {
  const res = await fetch(RESEND_URL, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${config.apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      from: config.from,
      to: [config.to],
      // Hitting reply in the inbox goes straight to the sender, not the
      // no-mailbox from address.
      reply_to: oneLine(msg.email),
      subject: `Contact form: ${oneLine(msg.name)}`,
      text: `From: ${msg.name} <${msg.email}>\n\n${msg.message}`,
    }),
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw new Error(`resend returned ${res.status}: ${await res.text()}`);
  }
}
