export type NavItem = {
  href: string;
  label: string;
  badge?: string;
};

export const primaryNav: NavItem[] = [
  { href: '/pma', label: 'PMA', badge: 'Primary' },
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/repos', label: 'Repos' },
  { href: '/worktrees', label: 'Worktrees' },
  { href: '/tickets', label: 'Tickets' },
  { href: '/contextspace/local', label: 'Contextspace' },
  { href: '/settings', label: 'Settings' }
];

export function isActiveRoute(pathname: string, href: string): boolean {
  if (href === '/pma') {
    return pathname === '/' || pathname === '/pma' || pathname.startsWith('/pma/');
  }
  if (href === '/contextspace/local') {
    return pathname === href || pathname.startsWith('/contextspace/');
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}
