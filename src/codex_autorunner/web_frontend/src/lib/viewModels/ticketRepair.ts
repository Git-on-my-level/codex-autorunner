import { pmaApi } from '$lib/api/client';
import type { TicketDetailViewModel } from '$lib/viewModels/ticket';
import {
  buildManagedThreadCreatePayload,
  buildManagedThreadMessagePayload,
  type PmaChatScopeOption
} from '$lib/viewModels/pmaChat';

function stringField(raw: Record<string, unknown>, key: string): string | null {
  const value = raw[key];
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function repairChatScope(ticket: TicketDetailViewModel): PmaChatScopeOption {
  const parentRepoId =
    stringField(ticket.raw, 'repo_id') ??
    stringField(ticket.raw, 'base_repo_id') ??
    stringField(ticket.frontmatter, 'repo_id') ??
    stringField(ticket.frontmatter, 'base_repo_id');
  if (ticket.workspaceKind === 'worktree' && ticket.workspaceId) {
    return {
      id: `worktree:${ticket.workspaceId}`,
      kind: 'worktree',
      label: ticket.workspaceId,
      detail: `Worktree · ${parentRepoId ?? ticket.workspaceId}`,
      workspaceRoot: stringField(ticket.raw, 'workspace_root') ?? ticket.workspacePathLabel ?? '.',
      resourceId: ticket.workspaceId,
      parentRepoId,
      scopeUrn: parentRepoId
        ? `worktree:${parentRepoId}/${ticket.workspaceId}`
        : `filesystem:${encodeURIComponent(stringField(ticket.raw, 'workspace_root') ?? ticket.workspacePathLabel ?? '.')}`
    };
  }
  if (ticket.workspaceKind === 'repo' && ticket.workspaceId) {
    return {
      id: `repo:${ticket.workspaceId}`,
      kind: 'repo',
      label: ticket.workspaceId,
      detail: `Repo · ${ticket.workspaceId}`,
      resourceKind: 'repo',
      resourceId: ticket.workspaceId,
      scopeUrn: `repo:${ticket.workspaceId}`
    };
  }
  return { id: 'local', kind: 'local', label: 'Local hub', detail: 'Current workspace', scopeUrn: 'hub' };
}

function buildRepairPrompt(ticket: TicketDetailViewModel): string {
  const raw = ticket.raw;
  const hubRoot = stringField(raw, 'hub_root') ?? '(hub root from the serving CAR instance)';
  const workspaceRoot = stringField(raw, 'workspace_root') ?? ticket.workspacePathLabel ?? '(unknown workspace root)';
  const ticketPath = ticket.pathLabel ?? '(unknown ticket path)';
  const errors = ticket.errors.length ? ticket.errors.map((err) => `- ${err}`).join('\n') : '- Frontmatter validation failed';
  return `Please repair this CAR ticket frontmatter and lint the ticket queue.\n\nHub root: ${hubRoot}\nWorkspace root: ${workspaceRoot}\nTicket path: ${ticketPath}\nAbsolute ticket path: ${workspaceRoot}/${ticketPath}\n\nValidation errors:\n${errors}\n\nRequirements:\n- Edit only the ticket file unless linting reveals directly related ticket metadata issues.\n- Fix the YAML frontmatter so the ticket can run.\n- Preserve the ticket body content.\n- Run: python3 .codex-autorunner/bin/lint_tickets.py from the workspace root.\n- Report exactly what changed and the lint result.`;
}

export type RepairTicketFrontmatterNavigation = {
  goto: (url: string | URL) => Promise<void>;
  href: (path: string) => string;
};

export async function repairTicketFrontmatterWithPma(
  ticket: TicketDetailViewModel,
  navigation: RepairTicketFrontmatterNavigation,
  setActionStatus: (message: string) => void
): Promise<void> {
  setActionStatus('Creating PMA repair chat...');
  const createResult = await pmaApi.pma.createChat(
    buildManagedThreadCreatePayload('codex', repairChatScope(ticket), `Repair ${ticket.numberLabel} frontmatter`)
  );
  if (!createResult.ok) {
    setActionStatus(createResult.error.message);
    return;
  }
  const sendResult = await pmaApi.pma.sendMessage(
    createResult.data.id,
    buildManagedThreadMessagePayload(buildRepairPrompt(ticket), '', false)
  );
  if (!sendResult.ok) {
    setActionStatus(sendResult.error.message);
    return;
  }
  await navigation.goto(navigation.href(`/chats?chat=${encodeURIComponent(createResult.data.id)}`));
}
