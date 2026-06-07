import { ensureAutomationWorkspaceLoaded } from '$lib/data';
import type { PageLoad } from './$types';

export const load: PageLoad = ({ depends }) =>
  ensureAutomationWorkspaceLoaded({ depends, blocking: false });
