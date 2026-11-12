# Reply composer polish — 2026-09-05

The previous footer offered several immediate choice submissions alongside an
unrelated text form. It was unclear whether choosing an option selected it or sent
it, and which button completed the decision.

The composer now uses a single HTML form: select a fully described answer or choose
“Write my own answer”, then explicitly “Send reply”. Nothing is preselected.
The custom editor appears only for a custom answer; switching options preserves its
text in the page. The existing core still resolves option IDs to their exact frozen
answers, checks revisions, and records replies before delivery. No new permission
or lifecycle semantics are introduced.

The footer explains the move to Watching. Withdrawal remains a secondary disclosure,
with its no-rollback limit visible when opened. Refresh continues to protect drafts
and reading position, but its status no longer overlays the mailbox content.

Validation: real local Hono UI inspected in the in-app browser at desktop, 390px dark,
and 320px light widths; no horizontal overflow at either mobile width. Checked radio
selection, custom editor reveal and typing, and a custom reply round trip into
Watching in the isolated in-memory preview. The user's six pending sample decisions
were preserved. Bun tests, portable tests, typecheck, foundation checks, and the
17-check daemon smoke test passed. The fixture browser script was updated for the
new controls; browser interaction in this review used the in-app browser.
