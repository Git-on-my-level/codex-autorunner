export type NavItem = {
  href: string;
  label: string;
  badge?: string;
  group: 'primary' | 'support' | 'workspace' | 'system';
  indent?: boolean;
};

export const primaryNav: NavItem[] = [
  { href: '/pma', label: 'PMA', badge: 'Primary', group: 'primary' },
  { href: '/pma-memory', label: 'PMA memory', group: 'primary' },
  { href: '/dashboard', label: 'Dashboard', group: 'support' },
  { href: '/repos', label: 'Repos', group: 'support' },
  { href: '/tickets', label: 'All tickets', group: 'workspace' },
  { href: '/settings', label: 'Settings', group: 'system' }
];

export const navGroupLabels: Record<NavItem['group'], string> = {
  primary: 'Primary',
  support: 'Repos',
  workspace: 'Workspace indexes',
  system: 'System'
};

export function isActiveRoute(pathname: string, href: string): boolean {
  if (href === '/pma') {
    return pathname === '/' || pathname === '/pma' || pathname.startsWith('/pma/');
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}
