export type NavItem = {
  href: string;
  label: string;
};

/** Top-level hub links. Exact path matches drive default breadcrumbs; see `$lib/breadcrumbs.ts`. */
export const primaryNav: NavItem[] = [
  { href: '/chats', label: 'Chats' },
  { href: '/repos', label: 'Repos' },
  { href: '/automations', label: 'Automations' },
  { href: '/settings', label: 'Settings' }
];

export function isActiveRoute(pathname: string, href: string): boolean {
  if (href === '/chats') {
    return pathname === '/' || pathname === '/chats' || pathname.startsWith('/chats/');
  }
  return pathname === href || pathname.startsWith(`${href}/`);
}
