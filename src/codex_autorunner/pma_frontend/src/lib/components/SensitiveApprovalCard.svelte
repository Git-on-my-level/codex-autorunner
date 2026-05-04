<script lang="ts">
  import type { SensitiveApprovalRequest } from '$lib/viewModels/domain';
  import { approvalScopeLabel } from '$lib/viewModels/pmaChat';

  let {
    approval,
    onDecision
  }: {
    approval: SensitiveApprovalRequest;
    onDecision?: (approval: SensitiveApprovalRequest, decision: 'approve' | 'decline') => void;
  } = $props();
</script>

<article class={`approval-card ${approval.risk}`}>
  <span class="approval-type">Sensitive CAR approval</span>
  <strong>{approval.title}</strong>
  <p>{approval.description || 'PMA is asking to perform a sensitive CAR control-plane action.'}</p>
  <dl>
    <div>
      <dt>Action</dt>
      <dd>{approval.action}</dd>
    </div>
    <div>
      <dt>Scope</dt>
      <dd>{approvalScopeLabel(approval)}</dd>
    </div>
    <div>
      <dt>Policy</dt>
      <dd>PMA has full permission for normal coding work. Sensitive CAR operations require approval.</dd>
    </div>
  </dl>
  <div class="approval-actions">
    <button type="button" onclick={() => onDecision?.(approval, 'decline')}>Decline</button>
    <button class="danger-action" type="button" onclick={() => onDecision?.(approval, 'approve')}>
      Approve
    </button>
  </div>
</article>
