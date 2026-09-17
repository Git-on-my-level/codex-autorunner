/** Email-like shell and reading surface. The server owns selection and state. */
export const MAILBOX_CSS = `
  .app-shell{display:grid;grid-template-columns:184px minmax(0,1fr);background:var(--surface)}
  .app-shell .app-bar{height:100dvh;position:sticky;top:0;background:var(--bg);border:0;border-right:1px solid var(--border);backdrop-filter:none}
  .app-shell .app-bar-inner{height:auto;max-width:none;padding:28px 12px;display:flex;align-items:stretch;flex-direction:column;gap:0}
  .app-shell .brand{padding:0 14px;gap:9px;min-height:32px;font-size:17px;letter-spacing:-.04em}
  .app-shell .brand-version{border:0;background:none;padding:0;font-size:11px;font-weight:450;letter-spacing:0}
  .sidebar-caption{padding:38px 14px 10px;color:var(--faint);font-size:11px;font-weight:550}
  .app-shell nav.primary{display:flex;flex-direction:column;gap:4px}
  .app-shell nav.primary>a{padding:9px 14px;min-height:38px;border-radius:6px;font-size:13px;gap:8px}
  .app-shell nav.primary>a:hover{background:var(--sunken)}
  .app-shell nav.primary>a[aria-current="page"]{background:var(--sunken);font-weight:600}
  .app-shell nav.primary>a[aria-current="page"]::after{display:none}
  .app-shell nav.primary>a:last-child{margin-top:24px}
  .app-shell .nav-count{margin-left:auto;background:none;padding:0;color:var(--muted);font-weight:450;font-size:12px}
  .app-shell main{min-width:0;width:100%;padding:32px 40px;margin:0}
  .app-shell main:not(.mailbox-main)>.decision-card{max-width:900px;padding:24px 0;margin:0;border:0;border-bottom:1px solid var(--border);border-radius:0}
  .app-shell .mailbox-main{padding:0;position:relative}
  .mailbox{display:grid;grid-template-columns:340px minmax(0,1fr);height:100dvh;min-height:0}
  .mailbox-list{min-width:0;overflow-y:auto;overscroll-behavior:contain;border-right:1px solid var(--border);background:var(--surface)}
  .mailbox-rows{list-style:none;margin:0;padding:0}
  .mailbox-toolbar{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:73px;padding:16px 22px;border-bottom:1px solid var(--border);background:var(--surface)}
  .mailbox-toolbar h1{font-size:17px;letter-spacing:-.025em;font-weight:600}
  .mailbox-toolbar .button{min-height:30px;font-size:12px;padding:4px 7px}
  .mail-row{display:grid;gap:5px;padding:18px 22px;border-bottom:1px solid var(--border);color:var(--fg);text-decoration:none;position:relative;min-width:0}
  .mail-row:hover{background:var(--raised);text-decoration:none}
  .mail-row-selected,.mail-row[aria-current="true"],.mail-row[aria-current="page"]{background:var(--sunken);box-shadow:inset 3px 0 0 var(--accent)}
  .mail-row:focus-visible{outline-offset:-3px}
  .mail-row-source{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mail-row-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;min-width:0}
  .mail-row-time{font-size:11px;color:var(--faint);white-space:nowrap;flex-shrink:0}
  .mail-row-subject{font-size:13px;font-weight:600;line-height:1.5;color:var(--strong);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
  .mail-row-preview{font-size:12px;line-height:1.6;color:var(--muted);display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
  .mail-row-state{font-size:11px;color:var(--muted);line-height:1.5}
  .mail-row-state.urgent,.mail-row-state.failed,.mail-row-state.uncertain{color:var(--critical)}
  .mailbox-reader{min-width:0;overflow-y:auto;overscroll-behavior:contain;background:var(--surface)}
  .reader-toolbar{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:73px;padding:16px 36px;border-bottom:1px solid var(--border);background:var(--surface);color:var(--muted);font-size:12px}
  .reader-toolbar a{color:var(--muted);text-decoration:none}
  .reader-toolbar a:hover{text-decoration:underline}
  .reader-toolbar .button{min-height:30px;font-size:12px}
  .reader-back{display:none}.reader-navigation{display:flex;align-items:center;gap:20px}.reader-navigation a{padding:4px 0}.reader-position{font-variant-numeric:tabular-nums}
  .mailbox-reader .decision-message{max-width:860px;margin:0 auto;padding:24px 36px 48px;border:0;border-radius:0;box-shadow:none;gap:16px}
  .message-sender{display:flex;justify-content:space-between;align-items:start;gap:18px;padding-bottom:16px;border-bottom:1px solid var(--border);font-size:12px}
  .message-sender>div{display:grid;gap:3px;min-width:0}
  .message-sender strong{color:var(--strong);font-size:13px;font-weight:600;overflow-wrap:anywhere}
  .message-sender span{color:var(--muted);overflow-wrap:anywhere}
  .message-sender .message-sender-side{display:grid;justify-items:end;gap:4px;min-width:0}
  .message-sender time{color:var(--faint);font-size:11px;text-align:right;max-width:155px}
  .decision-message .decision-meta{justify-content:flex-end;gap:8px;font-size:11px}
  .decision-message .badge{min-height:0;padding:0;border:0;border-radius:0;background:none;font-size:11px;font-weight:500;color:var(--muted);white-space:normal}
  .decision-message .badge.urgent,.decision-message .badge.failed,.decision-message .badge.uncertain,.decision-message .badge.expired{color:var(--critical)}
  .decision-message h2{font-size:24px;font-weight:600;line-height:1.3;letter-spacing:-.02em;max-width:36ch}
  .decision-message p{line-height:1.6;font-size:14px}
  .decision-message .message-intro{font-size:15px;line-height:1.6}
  .decision-message .decision-context{color:var(--fg)}
  .decision-message .decision-impact{color:var(--muted);font-size:13px}
  .decision-message .original-context{margin-top:0}
  .decision-message .recommendation{border:0;border-left:2px solid var(--border-strong);border-radius:0;background:none;padding:0 0 0 17px}
  .decision-message .recommendation p{margin-top:6px}
  .decision-message .eyebrow{font-size:11px;font-weight:500;letter-spacing:0}
  .decision-message .recommended-answer{font-weight:600}
  .decision-message .decision-uncertainty{border:0;border-radius:0;padding:0}
  .decision-message .decision-uncertainty strong{font-size:12px;font-weight:600;color:var(--strong)}
  .decision-message .decision-uncertainty p{margin-top:5px;color:var(--muted);font-size:13px}
  .decision-message .context-warning{font-size:12px;border-radius:4px;padding:10px 12px}
  .decision-message .answer-record{display:grid;gap:6px;padding:12px 14px;border:1px solid var(--border);border-left:2px solid var(--accent);border-radius:6px;background:var(--raised)}
  .decision-message .answer-record .muted{font-size:12px}
  .decision-message details{border:0;border-top:1px solid var(--border);border-radius:0;background:none;margin:0}
  .decision-message summary{padding:10px 0;font-size:12px;font-weight:500;color:var(--muted)}
  .decision-message details[open] summary{border-bottom:0}
  .decision-message .details-body{padding:2px 0 12px}
  .decision-message .details-body p{font-size:13px}
  .decision-message .decision-composer{border-top:1px solid var(--border);padding-top:16px}
  .decision-message .reply-heading{font-size:14px;font-weight:600;margin-bottom:12px}
  .decision-message .answer-form{gap:10px}
  .decision-message .answer-form label{font-size:12px}
  .decision-message .answer-form textarea{border-color:var(--border-strong);font-size:14px;line-height:1.6;min-height:108px;border-radius:6px}
  .decision-message .answer-form small{font-size:11px}
  .mailbox-empty{padding:56px 24px;color:var(--muted);font-size:13px;text-align:center}
  .mailbox-empty h2{font-size:15px;margin-bottom:8px}
  .mailbox-empty p{max-width:34ch;margin:0 auto;line-height:1.7}
  .mailbox-list .notice,.mailbox-list .context-warning{font-size:12px;padding:12px 22px;margin:0;border:0;border-bottom:1px solid var(--border);border-radius:0;background:var(--raised)}
  .mailbox-list .pager{padding:16px 22px;margin:0;justify-content:space-between}
  .mailbox-list .pager .button{min-height:32px;font-size:12px}
  .mailbox-reader>.notice{margin:16px 36px 0;font-size:12px;background:var(--raised);border:1px solid var(--border);border-radius:4px}
  .mailbox-main>.refresh-paused{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
  .mailbox-reader>.triage-confirmation{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 16px;font-size:14px}
  .triage-confirmation strong{font-size:14px}
  .reply-choices{border:0;padding:0;margin:0;min-width:0;display:grid;gap:8px}
  .shortcut-help{border:0;border-radius:0;background:none;font-size:12px}
  .shortcut-help[hidden]{display:none}.shortcut-help summary{padding:10px;color:var(--muted);font-size:11px;white-space:nowrap}
  .shortcut-list{position:fixed;z-index:60;top:64px;right:20px;width:320px;max-width:calc(100vw - 40px);display:grid;gap:12px;padding:20px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);box-shadow:var(--shadow)}.shortcut-list p{margin:0;line-height:1.8}.shortcut-list label{display:flex;align-items:center;gap:8px;font-size:11px}.shortcut-list input{width:16px;height:16px;min-height:0}.shortcut-list small{color:var(--muted);line-height:1.6}
  kbd{font:inherit;font-size:11px;border:1px solid var(--border-strong);border-radius:3px;padding:1px 5px;color:var(--muted)}
  .decision-message .reply-heading{margin:0;padding:0 0 6px;font-size:15px}
  .decision-message .reply-hint{font-size:12px;margin-bottom:8px}
  .decision-message .reply-choice{display:flex;align-items:flex-start;gap:12px;min-width:0;padding:13px 14px;border:0;border-radius:0;cursor:pointer;background:transparent;color:var(--fg)}
  .reply-choice-row{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:start;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--surface)}
  .reply-choice-row:has(input:checked){border-color:var(--accent);background:var(--sunken);box-shadow:inset 0 0 0 1px var(--accent)}
  .reply-choice-row .reply-choice:has(input:checked){background:transparent}
  .customize-answer{display:none}
  .js .customize-answer{display:inline-flex}
  .reply-choice-row .customize-answer{align-self:start;min-height:30px;margin:9px 9px 0 0;padding:3px 6px;font-size:11px;font-weight:500}
  .reply-choice:hover{background:var(--raised)}
  .reply-choice:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
  .reply-choice:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:2px}
  .reply-choice input[type=radio]{flex:0 0 16px;width:16px;height:16px;min-height:0;margin:3px 0 0;accent-color:var(--accent)}
  .reply-choice-copy{display:grid;gap:5px;min-width:0;line-height:1.6}
  .reply-choice-copy strong{font-size:13px;font-weight:600}.reply-tradeoff{font-size:12px}
  .custom-reply{display:none;gap:8px;padding:8px 0 4px}
  .reply-choices:has(.custom-choice input:checked) .custom-reply,.custom-reply.always-visible{display:grid}
  .reply-send{display:flex;align-items:center;gap:16px;margin-top:12px}
  .reply-send .button{min-height:40px;gap:18px;flex-shrink:0}
  .decision-message .reply-send p{font-size:12px;max-width:34ch;line-height:1.5}
  .decision-message .reply-scope{margin-top:0}
  /* A reading surface, not a dashboard: keep secondary provenance in details. */
  .mail-row-preview{display:none}
  .mail-row-subject{font-size:15px;line-height:1.45}
  .mail-row-source,.mail-row-state,.mail-row-time{font-size:13px}
  .message-sender{align-items:center;border:0;padding:0 0 4px}
  .message-sender strong,.decision-message .badge{font-size:14px}
  .decision-message p,.decision-message .message-intro{font-size:16px}
  .decision-message .eyebrow,.decision-message .decision-uncertainty strong{font-size:14px}
  .decision-message .decision-impact,.decision-message .decision-uncertainty p{font-size:15px}
  .decision-message summary{font-size:14px;padding:14px 0}
  .decision-message .reply-heading{font-size:18px;margin-bottom:10px}
  .decision-message .answer-form label,.reply-choice-copy strong{font-size:15px}
  .reply-choice-copy,.reply-tradeoff{font-size:14px;font-weight:400}
  .expiry-review{margin:8px 0 12px}
  .decision-message .expiry-explanation{font-size:15px;color:var(--muted);margin-bottom:12px}
  .decision-message .review-choice{border:1px solid var(--border);border-radius:6px;padding:16px}
  .custom-review{margin-top:16px!important}
  @media(max-width:480px){.reply-send{align-items:flex-start;flex-direction:column;gap:10px}.reply-send .button{width:100%;justify-content:center;min-height:44px}.decision-message .reply-choice{padding:12px}.reply-choice-row{grid-template-columns:minmax(0,1fr)}.reply-choice-row .customize-answer{width:100%;min-height:44px;margin:0;padding:8px 12px 8px 40px;font-size:12px;justify-content:flex-start;border-top:1px solid var(--border);border-radius:0}}

  .settings-content{width:min(100%,780px)}
  .settings-section{padding:18px 0;border-top:1px solid var(--border)}
  .settings-section:first-child{padding-top:0;border-top:0}
  .settings-section h2{margin:0 0 8px;font-size:17px;line-height:1.35;letter-spacing:-.015em}
  .settings-section>p{max-width:70ch;color:var(--muted);font-size:13px;line-height:1.55}
  .settings-section>p+p{margin-top:8px}
  .settings-facts{display:grid;gap:9px;margin:14px 0 0}
  .settings-facts>div{display:grid;grid-template-columns:minmax(120px,max-content) minmax(0,1fr);gap:12px;align-items:baseline}
  .settings-facts dt{color:var(--muted);font-size:12px}
  .settings-facts dd{margin:0;color:var(--strong);font-size:13px;overflow-wrap:anywhere}
  .settings-clients{display:grid;gap:0;margin:12px 0 0;padding:0;list-style:none;border:1px solid var(--border);border-radius:6px;overflow:hidden}
  .settings-clients>li,.settings-client{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;padding:10px 12px;border-bottom:1px solid var(--border)}
  .settings-clients>li:last-child,.settings-client:last-child{border-bottom:0}
  .settings-clients>li strong,.settings-clients>li span,.settings-client strong,.settings-client span{min-width:0;overflow-wrap:anywhere}
  .settings-clients>li strong,.settings-client strong{font-size:13px;color:var(--strong)}
  .settings-clients>li span,.settings-client span{font-size:12px;color:var(--muted)}
  .settings-footer{display:flex;align-items:center;gap:12px;padding-top:18px}
  .draft-navigation{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:40;width:380px;max-width:calc(100vw - 32px);padding:20px;border:1px solid var(--border-strong);border-radius:8px;background:var(--surface);box-shadow:0 8px 32px #0002;font-size:13px}.draft-navigation p{margin:5px 0 16px;color:var(--muted)}.draft-navigation[hidden]{display:none}
  @media(min-width:1500px){.app-shell{grid-template-columns:208px minmax(0,1fr)}.mailbox{grid-template-columns:380px minmax(0,1fr)}}
  @media(max-width:1100px) and (min-width:761px){.app-shell{grid-template-columns:150px minmax(0,1fr)}.mailbox{grid-template-columns:280px minmax(0,1fr)}.app-shell .brand{padding:0 8px}.app-shell nav.primary>a{padding:9px 8px}.mailbox-reader .decision-message{padding:24px}.reader-toolbar{padding:16px 24px}.mail-row{padding:16px 18px}.decision-message h2{font-size:21px}}
  @media(max-width:760px){
    .app-shell{display:block}.app-shell .app-bar{height:auto;position:sticky;top:0;border-right:0;border-bottom:1px solid var(--border)}
    .app-shell .app-bar-inner{height:52px;flex-direction:row;align-items:center;justify-content:space-between;padding:0 16px}
    .app-shell .brand{padding:0;min-height:0;font-size:16px}.sidebar-caption,.app-shell nav.primary{display:none}
    .app-shell .mobile-primary{display:block;margin-left:auto;position:relative}.app-shell .mobile-primary summary{border:0;background:none;min-height:36px;font-size:12px;padding:8px}
    .mobile-primary-links{position:absolute;right:0;top:42px;width:240px;display:grid;padding:6px;border:1px solid var(--border-strong);border-radius:6px;background:var(--surface);box-shadow:0 8px 24px #0002;z-index:40}
    .mobile-primary-links a{display:flex;align-items:center;min-height:44px;padding:10px;color:var(--fg);text-decoration:none}.mobile-primary-links a[aria-current="page"]{background:var(--sunken)}.mobile-primary-links .nav-group-label{padding:8px 10px;color:var(--muted);font-size:11px}
    .app-shell main{padding:24px 20px}.app-shell .mailbox-main{padding:0}
    .mailbox{display:block;height:auto}.mailbox-list{border-right:0;overflow:visible}.mailbox-reader{display:none;overflow:visible}
    .mailbox-selected .mailbox-list{display:none}.mailbox-selected .mailbox-reader{display:block}
    .mailbox-toolbar{position:static;min-height:64px;padding:14px 20px}.mail-row{padding:18px 20px}.mail-row-subject{font-size:14px}.mail-row-preview{font-size:13px}
    .reader-toolbar{position:static;min-height:50px;padding:4px 20px;flex-wrap:wrap;gap:4px 12px}
    .reader-back{display:inline-flex;align-items:center;min-height:44px}.reader-position{display:none}.reader-navigation{gap:16px}.reader-navigation a{padding:10px 0;min-height:44px;display:inline-flex;align-items:center}.mailbox:not(.mailbox-selected) .reader-skip-link{display:none}
    .mailbox-reader .decision-message{padding:20px 20px 40px;gap:16px}.decision-message h2{font-size:22px}
    .message-sender{padding-bottom:12px;gap:12px}.message-sender time{max-width:110px}.message-sender .message-sender-side{justify-items:end}.decision-message .answer-form textarea{font-size:16px}
    .decision-message summary{min-height:44px;display:list-item}
    .mailbox-reader>.notice{margin:12px 20px 0}.mailbox-main>.refresh-paused{bottom:12px}
    .mailbox:not(.mailbox-selected) .mail-row-selected,.mailbox:not(.mailbox-selected) .mail-row[aria-current="page"]{background:transparent;box-shadow:none}
    .settings-section{padding:16px 0}.settings-facts>div{grid-template-columns:1fr;gap:2px}.settings-clients>li,.settings-client{grid-template-columns:1fr;gap:2px}.settings-footer{align-items:flex-start;flex-direction:column}
  }
`;
