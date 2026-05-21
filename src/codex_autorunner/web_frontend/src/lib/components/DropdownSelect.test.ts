import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import ChatScopePicker from './ChatScopePicker.svelte';
import DropdownSelect, { dropdownSearchMatches, dropdownSearchTerms } from './DropdownSelect.svelte';

describe('DropdownSelect', () => {
  it('renders the shared trigger without falling back to a native select', () => {
    const { body } = render(DropdownSelect, {
      props: {
        value: 'codex',
        labelText: 'agent',
        ariaLabel: 'Agent',
        options: [
          { value: 'codex', label: 'Codex' },
          { value: 'hermes', label: 'Hermes' }
        ]
      }
    });

    expect(body).toContain('dropdown-select-trigger');
    expect(body).toContain('Codex');
    expect(body).not.toContain('<select');
  });

  it('labels the local chat scope with the Hub badge', () => {
    const { body } = render(ChatScopePicker, {
      props: {
        value: 'local',
        scopeOptions: [
          { id: 'local', kind: 'local', label: 'Local hub', detail: 'current workspace', scopeUrn: 'hub' }
        ]
      }
    });

    expect(body).toContain('Hub');
    expect(body).toContain('Local hub');
    expect(body).not.toContain('LOCAL');
  });

  it('splits dropdown search into required terms', () => {
    expect(dropdownSearchTerms(' codex-  retire ')).toEqual(['codex-', 'retire']);
    expect(dropdownSearchMatches(['CODEX-AUTORUNNER', 'codex/retire-lifecycle'], dropdownSearchTerms('codex- retire'))).toBe(true);
    expect(dropdownSearchMatches(['CODEX-AUTORUNNER', 'thread-chat-1'], dropdownSearchTerms('codex- retire'))).toBe(false);
  });
});
