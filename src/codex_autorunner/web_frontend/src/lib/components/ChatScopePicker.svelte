<script lang="ts">
  /**
   * Searchable scope picker for a new hub chat. Uses the shared dropdown select so the long
   * repo/worktree catalogue has the same trigger, search, keyboard, and popover behavior as the
   * other picker rows.
   */
  import {
    type PmaChatScopeOption,
    groupPmaChatScopeOptions
  } from '$lib/viewModels/pmaChat';
  import type { DropdownSelectGroup } from './DropdownSelect';
  import DropdownSelect from './DropdownSelect.svelte';

  let {
    scopeOptions = [],
    value = $bindable('local'),
    onChange = undefined,
    disabled = false
  }: {
    scopeOptions?: PmaChatScopeOption[];
    value?: string;
    onChange?: (() => void) | undefined;
    /** When true the picker is read-only — used by deep-linked "+ New chat" flows where the scope is fixed by the entry route. */
    disabled?: boolean;
  } = $props();

  const groupedAll = $derived(groupPmaChatScopeOptions(scopeOptions));
  const selectGroups = $derived.by<DropdownSelectGroup[]>(() => {
    const groups: DropdownSelectGroup[] = [];
    if (groupedAll.local) {
      groups.push({
        options: [
          {
            value: groupedAll.local.id,
            label: groupedAll.local.label,
            detail: groupedAll.local.detail,
            badge: 'Hub',
            triggerBadge: 'Hub'
          }
        ]
      });
    }
    for (const group of groupedAll.groups) {
      groups.push({
        label: group.repoLabel,
        options: [
          ...(group.repo
            ? [
                {
                  value: group.repo.id,
                  label: group.repoLabel,
                  detail: group.repo.detail,
                  badge: 'repo',
                  triggerBadge: 'repo'
                }
              ]
            : []),
          ...group.worktrees.map((worktree) => ({
            value: worktree.id,
            label: worktree.label,
            detail: group.repoLabel,
            badge: 'worktree',
            triggerBadge: 'worktree'
          }))
        ]
      });
    }
    return groups;
  });
</script>

<DropdownSelect
  bind:value
  groups={selectGroups}
  labelText="scope"
  ariaLabel={disabled ? 'Chat scope (locked to route)' : 'Chat scope'}
  rowClass="start-picker-row"
  searchable={true}
  searchPlaceholder="Search repos and worktrees"
  placeholder="Select scope"
  emptyText="No scopes match"
  {disabled}
  onchange={onChange}
/>
