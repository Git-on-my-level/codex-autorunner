import { pmaApi } from '$lib/api/client';
import { buildManagedThreadMessagePayload } from '$lib/viewModels/pmaChat';
import {
  buildTicketRepairChatCreatePayload,
  buildTicketRepairPrompt,
  type TicketDetailViewModel
} from '$lib/viewModels/ticket';

export type TicketRepairNavigation = {
  goto: (url: string | URL) => Promise<void>;
  href: (path: string) => string;
};

export async function repairTicketFrontmatterWithPma(
  ticket: TicketDetailViewModel,
  navigation: TicketRepairNavigation,
  setActionStatus: (message: string) => void
): Promise<void> {
  setActionStatus('Creating PMA repair chat...');
  const createResult = await pmaApi.pma.createChat(buildTicketRepairChatCreatePayload(ticket));
  if (!createResult.ok) {
    setActionStatus(createResult.error.message);
    return;
  }
  const sendResult = await pmaApi.pma.sendMessage(
    createResult.data.id,
    buildManagedThreadMessagePayload(buildTicketRepairPrompt(ticket), '', false)
  );
  if (!sendResult.ok) {
    setActionStatus(sendResult.error.message);
    return;
  }
  await navigation.goto(navigation.href(`/chats?chat=${encodeURIComponent(createResult.data.id)}`));
}
