/** Compact server-rendered mailbox shell for the human decision console. */
import type { Child, PropsWithChildren } from "hono/jsx";

export interface MailboxItem {
  id: string;
  href: string;
  source: string;
  time: string;
  datetime?: string;
  subject: string;
  preview: string;
  state: string;
  urgency?: "normal" | "urgent";
  selected?: boolean;
}

export interface MailboxProps {
  title: string;
  refreshHref?: string;
  items: readonly MailboxItem[];
  explicitSelection?: boolean;
  beforeList?: Child;
  afterList?: Child;
  backHref?: string;
  backLabel?: string;
  /** Either pass `reader` or put the rendered reader in `children`. */
  reader?: Child;
  emptyMessage?: string;
}

/**
 * The component owns only mailbox structure. Item links are ordinary GETs, so
 * no row contains a nested form or an implicit mutation. The primary layout
 * supplies the responsive rules: an auto-selected reader is desktop-only,
 * while `.mailbox-selected` marks an explicit mobile selection.
 */
export function Mailbox({
  title,
  refreshHref = "",
  items,
  explicitSelection = false,
  beforeList,
  afterList,
  backHref = "/ui",
  backLabel = "Back to inbox",
  reader,
  emptyMessage = "Nothing in this mailbox.",
  children,
}: PropsWithChildren<MailboxProps>) {
  const renderedReader = reader ?? children;
  const selectedIndex = items.findIndex((item) => item.selected);
  const previous = selectedIndex > 0 ? items[selectedIndex - 1] : undefined;
  const next = selectedIndex >= 0 ? items[selectedIndex + 1] : undefined;
  return <section class={`mailbox${explicitSelection ? " mailbox-selected" : ""}`}>
    {renderedReader && <a class="skip-link reader-skip-link" href="#selected-decision">Skip to selected decision</a>}
    <div class="mailbox-list">
      <div class="mailbox-toolbar" aria-label="Decision mailbox">
        <h1>{title}</h1><a class="button ghost" href={refreshHref} data-manual-refresh>Refresh</a>
      </div>
      {beforeList}
      <ul class="mailbox-rows" aria-label={`${title} items`}>
        {items.map((item) => <li><a class={`mail-row${item.selected ? " mail-row-selected" : ""}`} href={item.href}
          aria-current={item.selected ? "page" : undefined}>
          <span class="mail-row-top"><span class="mail-row-source">{item.source}</span>
            <time class="mail-row-time" datetime={item.datetime ?? item.time} data-format="compact">{item.time}</time></span>
          <span class="mail-row-subject">{item.subject}</span>
          <span class="mail-row-preview">{item.preview}</span>
          {(item.urgency === "urgent" || item.state) && <span class={`mail-row-state${item.urgency === "urgent" ? " urgent" : ""}`}>{item.urgency === "urgent" ? (item.state ? "Urgent · " : "Urgent") : ""}{item.state}</span>}
        </a></li>)}
        {items.length === 0 && <li class="mailbox-empty" role="status">{emptyMessage}</li>}
      </ul>
      {afterList}
    </div>
    {renderedReader && <section class="mailbox-reader" id="selected-decision" aria-label="Selected decision" tabindex={-1}>
      <div class="reader-toolbar">
        <a class="reader-back" href={backHref}>{backLabel}</a>
        <span class="reader-position">{selectedIndex >= 0 ? `${selectedIndex + 1} of ${items.length}` : "Decision"}</span>
        <nav class="reader-navigation" aria-label="Read decisions">
          {previous && <a href={previous.href} aria-label="Previous decision">Previous</a>}
          {next && <a href={next.href} aria-label="Next decision">Next</a>}
          <a href="" data-manual-refresh>Refresh</a>
        </nav>
      </div>
      {renderedReader}
    </section>}
  </section>;
}
