import { ensureRepoWorktreeIndexLoaded } from '$lib/data';
import type { PageLoad } from './$types';

export const load: PageLoad = ({ depends }) => ensureRepoWorktreeIndexLoaded({ depends });
