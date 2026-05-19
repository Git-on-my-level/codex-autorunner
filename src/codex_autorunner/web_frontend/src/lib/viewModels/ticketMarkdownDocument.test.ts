import { describe, expect, it } from 'vitest';
import {
  composeTicketMarkdown,
  defaultTicketPackTicketContent,
  parseTicketMarkdownFields,
  ticketPackListRow,
  ticketPackNumberLabel
} from './ticketMarkdownDocument';

describe('ticketMarkdownDocument', () => {
  it('parses frontmatter and body from ticket markdown', () => {
    const content = `---
title: Weekly sweep
agent: codex
model: gpt-5
done: false
---

Run checks.`;
    const fields = parseTicketMarkdownFields(content, 'TICKET-001.md');
    expect(fields.title).toBe('Weekly sweep');
    expect(fields.agent).toBe('codex');
    expect(fields.model).toBe('gpt-5');
    expect(fields.done).toBe(false);
    expect(fields.body).toBe('Run checks.');
  });

  it('composes ticket markdown with updated settings', () => {
    const content = composeTicketMarkdown({
      title: 'Fix lint',
      agent: 'codex',
      model: '',
      reasoning: 'high',
      done: true,
      body: 'Goal\n- fix lint'
    });
    const fields = parseTicketMarkdownFields(content);
    expect(fields.title).toBe('Fix lint');
    expect(fields.reasoning).toBe('high');
    expect(fields.done).toBe(true);
    expect(fields.body).toContain('fix lint');
  });

  it('builds list row previews for pack tickets', () => {
    const row = ticketPackListRow(
      'TICKET-002.md',
      composeTicketMarkdown({
        title: 'Dependency bump',
        agent: 'codex',
        model: 'gpt-5',
        reasoning: '',
        done: false,
        body: 'Bump deps safely.'
      }),
      1
    );
    expect(row.numberLabel).toBe('#2');
    expect(row.title).toBe('Dependency bump');
    expect(row.modelLabel).toBe('gpt-5');
    expect(row.bodyPreview).toContain('Bump deps');
  });

  it('labels tickets from path when frontmatter omits title', () => {
    expect(ticketPackNumberLabel('queue/TICKET-003.md', 2)).toBe('#3');
    const content = defaultTicketPackTicketContent('TICKET-004.md');
    expect(parseTicketMarkdownFields(content, 'TICKET-004.md').title).toBe('TICKET 004');
    expect(parseTicketMarkdownFields(content, 'TICKET-004.md').agent).toBe('codex');
  });
});
