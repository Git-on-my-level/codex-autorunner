<script lang="ts">
  import { onMount } from 'svelte';
  import DropdownSelect from '../DropdownSelect.svelte';
  import type { DropdownSelectGroup } from '../DropdownSelect';
  import {
    applyThemePreference,
    isThemePreference,
    readStoredThemePreference,
    type ThemePreference
  } from '$lib/theme';

  let themePreference = $state<ThemePreference>('system');

  onMount(() => {
    themePreference = readStoredThemePreference();
  });

  const themeGroups: DropdownSelectGroup[] = [
    {
      label: 'PMA Hub default',
      options: [
        { value: 'system', label: 'System (match OS)' },
        { value: 'light', label: 'Light' },
        { value: 'dark', label: 'Dark' }
      ]
    },
    {
      label: 'Solarized',
      options: [
        { value: 'solarized-light', label: 'Solarized Light' },
        { value: 'solarized-dark', label: 'Solarized Dark' }
      ]
    },
    {
      label: 'IDE-style',
      options: [
        { value: 'dracula', label: 'Dracula' },
        { value: 'nord', label: 'Nord' },
        { value: 'one-dark', label: 'One Dark' },
        { value: 'github-light', label: 'GitHub Light' },
        { value: 'github-dark', label: 'GitHub Dark' }
      ]
    }
  ];

  function onThemeSelectChange(value: string): void {
    if (!isThemePreference(value)) return;
    themePreference = value;
    applyThemePreference(value);
  }
</script>

<section class="settings-section">
  <h2 class="settings-section-title">Appearance</h2>
  <DropdownSelect
    value={themePreference}
    groups={themeGroups}
    labelText="Theme"
    ariaLabel="Color theme"
    rowClass="theme-select-field"
    searchable={true}
    searchPlaceholder="Search themes"
    onchange={onThemeSelectChange}
  />
</section>
