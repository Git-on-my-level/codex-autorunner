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
