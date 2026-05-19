<script lang="ts">
  import { statusLabel } from '$lib/viewModels/pmaChat';
  import type { WorkStatus } from '$lib/viewModels/domain';

  let { status, title = null }: { status: WorkStatus; title?: string | null } = $props();
</script>

<span class="status-pill {status}" {title}>{statusLabel(status)}</span>
