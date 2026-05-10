import { describe, expect, it } from 'vitest';
import {
  buildSlashCommandSuggestions,
  parseSlashCommand,
  slashCommandDisabledReason,
  WEB_SLASH_COMMANDS
} from './slashCommands';

describe('web slash commands', () => {
  it('parses command names, aliases, and arguments', () => {
    expect(parseSlashCommand('/compact keep repo context')?.spec?.id).toBe('compact');
    expect(parseSlashCommand('/compact keep repo context')?.args).toBe('keep repo context');
    expect(parseSlashCommand('/stop')?.spec?.id).toBe('interrupt');
    expect(parseSlashCommand(' /compact nope')).toBeNull();
  });

  it('filters suggestions by command name and aliases', () => {
    const suggestions = buildSlashCommandSuggestions('/cle', {
      hasActiveChat: true,
      hasScopedWorkspace: true,
      isRunning: false,
      queueDepth: 3
    });

    expect(suggestions.map((item) => item.spec.id)).toContain('clearqueue');
  });

  it('keeps the exact command visible while typing arguments', () => {
    const suggestions = buildSlashCommandSuggestions('/cancel 2', {
      hasActiveChat: true,
      hasScopedWorkspace: true,
      isRunning: false,
      queueDepth: 3
    });

    expect(suggestions).toHaveLength(1);
    expect(suggestions[0].spec.id).toBe('cancel');
  });

  it('reports disabled reasons from runtime context', () => {
    const interrupt = WEB_SLASH_COMMANDS.find((command) => command.id === 'interrupt');
    const cancel = WEB_SLASH_COMMANDS.find((command) => command.id === 'cancel');

    expect(
      interrupt &&
        slashCommandDisabledReason(interrupt, {
          hasActiveChat: true,
          hasScopedWorkspace: true,
          isRunning: false,
          queueDepth: 0
        })
    ).toBe('No running turn');
    expect(
      cancel &&
        slashCommandDisabledReason(cancel, {
          hasActiveChat: true,
          hasScopedWorkspace: true,
          isRunning: false,
          queueDepth: 0
        })
    ).toBe('Queue is empty');
  });

  it('keeps web-specific fresh-start and navigation commands discoverable', () => {
    expect(parseSlashCommand('/newt')?.spec?.id).toBe('newt');
    expect(parseSlashCommand('/ctx')?.spec?.id).toBe('contextspace');

    const newt = WEB_SLASH_COMMANDS.find((command) => command.id === 'newt');
    expect(
      newt &&
        slashCommandDisabledReason(newt, {
          hasActiveChat: true,
          hasScopedWorkspace: false,
          isRunning: false,
          queueDepth: 0
        })
    ).toBe('Select a repo or worktree chat first');
  });
});
