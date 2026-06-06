import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const pageSource = readFileSync(fileURLToPath(new URL('./+page.svelte', import.meta.url)), 'utf-8');

describe('services page preview link security', () => {
  it('opens preview links without exposing window.opener or referrer', () => {
    expect(pageSource).toContain("window.open(href(serviceOpenUrl(service)), '_blank', 'noopener,noreferrer')");
  });

  it('does not persist hub access tokens in browser storage', () => {
    const forbidden = [
      /localStorage\.setItem\([^)]*(hub|auth|access).*token/i,
      /sessionStorage\.setItem\([^)]*(hub|auth|access).*token/i
    ];
    for (const pattern of forbidden) {
      expect(pageSource).not.toMatch(pattern);
    }
  });
});
