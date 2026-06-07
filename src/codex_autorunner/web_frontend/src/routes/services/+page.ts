import { ensureServicesLoaded } from '$lib/data';
import type { PageLoad } from './$types';

export const load: PageLoad = ({ depends }) => ensureServicesLoaded({ depends, blocking: false });
