# Inbox triage ergonomics — 2026-09-05

Replying used to redirect to the answered request, whose canonical mailbox is
Watching. The UI now carries a server-rendered continuation hint. A successful
answer, withdrawal, or missed-deadline review returns to Needs you and opens the
next pending item. The answered request still moves to Watching. A small notice
confirms the action without implying delivery or completion.

The hint cannot select an external redirect. A next item answered elsewhere falls
back to current pending work. An exhausted page returns to the start of the inbox;
an empty inbox remains empty with confirmation. Explicit selection also opens the
reader on mobile. Pagination links use mailbox routes instead of retaining the
previous decision's detail URL.

Keyboard shortcuts are progressive enhancement over the same links and forms:
J/K navigate, 1–9 select a provided option, R opens the custom reply, and
Command/Control+Enter submits the focused text reply form using native validation.
Question mark opens help; Escape closes help or returns to the list. Character
shortcuts can be disabled in the visible help panel. Typing, IME composition,
modifier combinations, and repeated keydown events do not trigger navigation.
Selecting an option never submits it. Permissions have no direct approval hotkey.

The existing draft guard protects shortcut navigation. Browser Back/Reload also
warns about unsent input. The selected list row scrolls into view on navigation,
and blank custom replies are caught by browser validation before submission.
HTML forms and links remain functional without JavaScript; core validation is
unchanged and remains authoritative.

Validation includes real Hono route tests for continuation and recovery, key/draft
regressions, and an isolated live browser run using preset and custom replies to
advance from Release to SDK to CI without leaving Needs you. Local preview sample
data remains separate from these submission checks.
