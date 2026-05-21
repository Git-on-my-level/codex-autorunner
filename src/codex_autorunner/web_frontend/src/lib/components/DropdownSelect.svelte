<script lang="ts" module>
  export type DropdownSelectOption = {
    value: string;
    label: string;
    detail?: string;
    badge?: string;
    triggerBadge?: string;
    disabled?: boolean;
  };

  export type DropdownSelectGroup = {
    label?: string;
    options: DropdownSelectOption[];
  };

  export function dropdownSearchTerms(query: string): string[] {
    return query
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);
  }

  export function dropdownSearchMatches(fields: Array<string | undefined>, terms: string[]): boolean {
    if (terms.length === 0) return true;
    const haystack = fields.filter(Boolean).join(' ').toLowerCase();
    return terms.every((term) => haystack.includes(term));
  }
</script>

<script lang="ts">
  let {
    value = $bindable(''),
    options = [],
    groups = [],
    labelText,
    ariaLabel,
    rowClass = '',
    disabled = false,
    searchable = false,
    searchPlaceholder = 'Search',
    placeholder = 'Select',
    emptyText = 'No options',
    onchange = undefined
  }: {
    value?: string;
    options?: DropdownSelectOption[];
    groups?: DropdownSelectGroup[];
    labelText?: string;
    ariaLabel: string;
    rowClass?: string;
    disabled?: boolean;
    searchable?: boolean;
    searchPlaceholder?: string;
    placeholder?: string;
    emptyText?: string;
    onchange?: ((value: string) => void) | undefined;
  } = $props();

  let open = $state(false);
  let query = $state('');
  let activeIndex = $state(0);
  let triggerEl = $state<HTMLButtonElement | null>(null);
  let panelEl = $state<HTMLDivElement | null>(null);
  let searchEl = $state<HTMLInputElement | null>(null);

  const sourceGroups = $derived.by<DropdownSelectGroup[]>(() => {
    if (groups.length > 0) return groups;
    return [{ options }];
  });

  const flatOptions = $derived(sourceGroups.flatMap((group) => group.options));
  const selectableOptions = $derived(flatOptions.filter((option) => !option.disabled));
  const selectedOption = $derived(flatOptions.find((option) => option.value === value) ?? null);
  const fieldClass = $derived(['dropdown-select-field', rowClass].filter(Boolean).join(' '));
  const selectedLabel = $derived(selectedOption?.label ?? (value ? value : placeholder));
  const selectedBadge = $derived(selectedOption?.triggerBadge ?? selectedOption?.badge ?? '');

  const visibleGroups = $derived.by<DropdownSelectGroup[]>(() => {
    const terms = dropdownSearchTerms(query);
    if (terms.length === 0) return sourceGroups;
    return sourceGroups
      .map((group) => {
        const groupMatches = dropdownSearchMatches([group.label], terms);
        const filtered = groupMatches
          ? group.options
          : group.options.filter((option) =>
              dropdownSearchMatches([group.label, option.label, option.detail, option.badge, option.value], terms)
            );
        return { ...group, options: filtered };
      })
      .filter((group) => group.options.length > 0);
  });

  const visibleFlat = $derived(visibleGroups.flatMap((group) => group.options));
  const activeOption = $derived(visibleFlat[activeIndex] ?? null);

  $effect(() => {
    if (activeIndex > visibleFlat.length - 1) activeIndex = Math.max(0, visibleFlat.length - 1);
  });

  function openPanel(): void {
    if (disabled || selectableOptions.length === 0) return;
    open = true;
    query = '';
    const idx = visibleFlat.findIndex((option) => option.value === value);
    activeIndex = idx >= 0 ? idx : Math.max(0, visibleFlat.findIndex((option) => !option.disabled));
    queueMicrotask(() => {
      if (searchable) searchEl?.focus();
      else panelEl?.focus();
    });
  }

  function closePanel(): void {
    open = false;
    query = '';
  }

  function togglePanel(): void {
    if (open) closePanel();
    else openPanel();
  }

  function selectOption(option: DropdownSelectOption): void {
    if (option.disabled) return;
    if (option.value !== value) {
      value = option.value;
      onchange?.(value);
    }
    closePanel();
    triggerEl?.focus();
  }

  function moveActive(delta: number): void {
    if (visibleFlat.length === 0) return;
    let next = activeIndex;
    for (let i = 0; i < visibleFlat.length; i += 1) {
      next = Math.min(visibleFlat.length - 1, Math.max(0, next + delta));
      if (!visibleFlat[next]?.disabled) break;
    }
    activeIndex = next;
    queueMicrotask(() => panelEl?.querySelector('[data-active="true"]')?.scrollIntoView({ block: 'nearest' }));
  }

  function handleTriggerKeydown(event: KeyboardEvent): void {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openPanel();
    }
  }

  function handlePanelKeydown(event: KeyboardEvent): void {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      moveActive(1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      moveActive(-1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      if (activeOption) selectOption(activeOption);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closePanel();
      triggerEl?.focus();
    }
  }

  function onWindowPointerDown(event: PointerEvent): void {
    if (!open) return;
    const target = event.target as Node | null;
    if (!target) return;
    if (triggerEl?.contains(target) || panelEl?.contains(target)) return;
    closePanel();
  }
</script>

<svelte:window onpointerdown={onWindowPointerDown} />

<label class={fieldClass}>
  {#if labelText}
    <span>{labelText}</span>
  {/if}
  <div class="dropdown-select-control">
    <button
      bind:this={triggerEl}
      type="button"
      class="dropdown-select-trigger"
      class:open
      aria-label={ariaLabel}
      aria-haspopup="listbox"
      aria-expanded={open}
      {disabled}
      onclick={togglePanel}
      onkeydown={handleTriggerKeydown}
    >
      {#if selectedBadge}
        <span class="dropdown-select-badge">{selectedBadge}</span>
      {/if}
      <span class="dropdown-select-label">{selectedLabel}</span>
      <svg class="dropdown-select-chevron" viewBox="0 0 12 12" aria-hidden="true">
        <path
          d="M2.5 4.5 6 8l3.5-3.5"
          fill="none"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </button>

    {#if open}
      <div
        bind:this={panelEl}
        class="dropdown-select-panel"
        role="listbox"
        aria-label={ariaLabel}
        tabindex="-1"
        onkeydown={handlePanelKeydown}
      >
        {#if searchable}
          <div class="dropdown-select-search">
            <svg viewBox="0 0 14 14" aria-hidden="true">
              <circle cx="6" cy="6" r="4" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M9 9l3.5 3.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
            </svg>
            <input
              bind:this={searchEl}
              type="text"
              placeholder={searchPlaceholder}
              bind:value={query}
              spellcheck="false"
              autocomplete="off"
              onkeydown={handlePanelKeydown}
            />
          </div>
        {/if}

        <div class="dropdown-select-list">
          {#each visibleGroups as group, groupIndex (`${group.label ?? 'group'}-${groupIndex}`)}
            {#if group.label}
              <div class="dropdown-select-group-label">{group.label}</div>
            {/if}
            {#each group.options as option (option.value)}
              <button
                type="button"
                class="dropdown-select-option"
                class:selected={option.value === value}
                data-active={option.value === activeOption?.value}
                role="option"
                aria-selected={option.value === value}
                disabled={option.disabled}
                onclick={() => selectOption(option)}
                onmouseenter={() => (activeIndex = visibleFlat.findIndex((candidate) => candidate.value === option.value))}
              >
                <span class="dropdown-select-option-copy">
                  <span class="dropdown-select-option-label">{option.label}</span>
                  {#if option.detail}
                    <span class="dropdown-select-option-detail">{option.detail}</span>
                  {/if}
                </span>
                {#if option.badge}
                  <span class="dropdown-select-option-badge">{option.badge}</span>
                {/if}
              </button>
            {/each}
          {/each}

          {#if visibleFlat.length === 0}
            <div class="dropdown-select-empty">{emptyText}</div>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</label>

<style>
  .dropdown-select-field {
    min-width: 0;
  }

  .dropdown-select-control {
    position: relative;
    min-width: 0;
  }

  .dropdown-select-trigger {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    min-height: 30px;
    padding: 0 var(--space-2);
    font-size: var(--font-size-1);
    text-align: left;
    border-radius: 7px;
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
    color: var(--color-ink);
    cursor: pointer;
    transition:
      border-color var(--transition-fast),
      box-shadow var(--transition-fast),
      background-color var(--transition-fast);
  }

  .dropdown-select-trigger:hover {
    border-color: var(--color-border);
  }

  .dropdown-select-trigger:focus-visible,
  .dropdown-select-trigger.open {
    outline: none;
    border-color: var(--color-accent);
    box-shadow: var(--shadow-focus);
  }

  .dropdown-select-trigger:disabled {
    cursor: not-allowed;
    opacity: 0.65;
  }

  .dropdown-select-badge,
  .dropdown-select-option-badge {
    flex: none;
    font-size: var(--font-size-0);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--color-ink-muted);
    background: var(--color-surface-muted);
    border-radius: var(--radius-2);
    padding: 1px 5px;
  }

  .dropdown-select-label {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 500;
  }

  .dropdown-select-chevron {
    flex: none;
    width: 12px;
    height: 12px;
    color: var(--color-ink-faint);
    transition: transform var(--transition-fast);
  }

  .dropdown-select-trigger.open .dropdown-select-chevron {
    transform: rotate(180deg);
  }

  .dropdown-select-panel {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    z-index: 40;
    display: flex;
    flex-direction: column;
    max-height: 300px;
    background: var(--color-surface);
    border: 1px solid var(--color-border-subtle);
    border-radius: var(--radius-4);
    box-shadow: var(--shadow-2);
    overflow: hidden;
  }

  .dropdown-select-search {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2) var(--space-3);
    border-bottom: 1px solid var(--color-border-subtle);
    color: var(--color-ink-faint);
  }

  .dropdown-select-search svg {
    flex: none;
    width: 13px;
    height: 13px;
  }

  .dropdown-select-search input {
    flex: 1;
    min-width: 0;
    border: none;
    background: transparent;
    padding: 0;
    font-size: var(--font-size-1);
    color: var(--color-ink);
  }

  .dropdown-select-search input:focus {
    outline: none;
    box-shadow: none;
  }

  .dropdown-select-search input::placeholder {
    color: var(--color-ink-faint);
  }

  .dropdown-select-list {
    overflow-y: auto;
    padding: var(--space-2);
  }

  .dropdown-select-group-label {
    padding: var(--space-2) var(--space-2) var(--space-1);
    font-size: var(--font-size-0);
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--color-ink-faint);
  }

  .dropdown-select-option {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2);
    border: none;
    border-radius: var(--radius-3);
    background: transparent;
    color: var(--color-ink-soft);
    font-size: var(--font-size-1);
    text-align: left;
    cursor: pointer;
  }

  .dropdown-select-option[data-active='true'] {
    background: var(--color-surface-muted);
  }

  .dropdown-select-option.selected {
    color: var(--color-accent);
  }

  .dropdown-select-option.selected[data-active='true'] {
    background: var(--color-accent-soft);
  }

  .dropdown-select-option:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .dropdown-select-option-copy {
    display: flex;
    flex: 1;
    min-width: 0;
    flex-direction: column;
    gap: 1px;
  }

  .dropdown-select-option-label,
  .dropdown-select-option-detail {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dropdown-select-option-label {
    font-weight: 500;
  }

  .dropdown-select-option-detail {
    font-size: var(--font-size-0);
    color: var(--color-ink-faint);
  }

  .dropdown-select-empty {
    padding: var(--space-3) var(--space-2);
    font-size: var(--font-size-1);
    color: var(--color-ink-faint);
    text-align: center;
  }
</style>
