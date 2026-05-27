export type SettingsSectionId = 'memory' | 'general' | 'integrations' | 'agents';

export type SettingsSectionDescriptor = {
  id: SettingsSectionId;
  label: string;
  description: string;
};

export const SETTINGS_SECTIONS: SettingsSectionDescriptor[] = [
  {
    id: 'memory',
    label: 'PMA memory',
    description: 'Hub-wide notes the agent reads at the start of every chat.'
  },
  {
    id: 'general',
    label: 'General',
    description: 'Appearance, hub updates, and runtime status.'
  },
  {
    id: 'integrations',
    label: 'Integrations',
    description: 'Telegram, Discord, GitHub, notifications, voice transcription.'
  },
  {
    id: 'agents',
    label: 'Agents & Runner',
    description: 'Default models, runner overrides, ticket flow.'
  }
];

export function isSettingsSectionId(value: string | null | undefined): value is SettingsSectionId {
  return (
    value === 'memory' ||
    value === 'general' ||
    value === 'integrations' ||
    value === 'agents'
  );
}

export function settingsSectionLabel(id: SettingsSectionId): string {
  return SETTINGS_SECTIONS.find((section) => section.id === id)?.label ?? 'Settings';
}
