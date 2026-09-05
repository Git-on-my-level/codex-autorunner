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
  .mail-row-selected,.mail-row[aria-current="true"]{background:var(--accent-soft)}
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
  .mailbox-reader .decision-message{max-width:860px;margin:0 auto;padding:28px 36px 48px;border:0;border-radius:0;box-shadow:none;gap:20px}
  .message-sender{display:flex;justify-content:space-between;align-items:start;gap:18px;padding-bottom:24px;border-bottom:1px solid var(--border);font-size:12px}
  .message-sender>div{display:grid;gap:3px;min-width:0}
  .message-sender strong{color:var(--strong);font-size:13px;font-weight:600;overflow-wrap:anywhere}
  .message-sender span{color:var(--muted);overflow-wrap:anywhere}
  .message-sender time{color:var(--faint);font-size:11px;text-align:right;max-width:155px}
  .decision-message .decision-meta{gap:10px;font-size:11px}
  .decision-message .badge{min-height:0;padding:0;border:0;border-radius:0;background:none;font-size:11px;font-weight:500;color:var(--muted);white-space:normal}
  .decision-message .badge.urgent,.decision-message .badge.failed,.decision-message .badge.uncertain,.decision-message .badge.expired{color:var(--critical)}
  .decision-message h2{font-size:24px;font-weight:600;line-height:1.35;letter-spacing:-.035em;max-width:36ch}
  .decision-message p{line-height:1.7;font-size:14px}
  .decision-message .message-intro{font-size:15px;line-height:1.75}
  .decision-message .recommendation{border:0;border-left:2px solid var(--border-strong);border-radius:0;background:none;padding:0 0 0 17px}
  .decision-message .recommendation p{margin-top:6px}
  .decision-message .eyebrow{font-size:11px;font-weight:500;letter-spacing:0}
  .decision-message .recommended-answer{font-weight:600}
  .decision-message .decision-uncertainty{border:0;border-radius:0;padding:0}
  .decision-message .decision-uncertainty strong{font-size:12px;font-weight:600;color:var(--strong)}
  .decision-message .decision-uncertainty p{margin-top:5px;color:var(--muted);font-size:13px}
  .decision-message .context-warning{font-size:12px;border-radius:4px;padding:10px 12px}
  .decision-message .answer-record{padding:20px 0;border:0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);border-radius:0;background:none}
  .decision-message .answer-record .muted{font-size:12px}
  .decision-message details{border:0;border-top:1px solid var(--border);border-radius:0;background:none;margin:0}
  .decision-message summary{padding:12px 0;font-size:12px;font-weight:500;color:var(--muted)}
  .decision-message details[open] summary{border-bottom:0}
  .decision-message .details-body{padding:4px 0 16px}
  .decision-message .details-body p{font-size:13px}
  .decision-message .decision-composer{border-top:1px solid var(--border);padding-top:22px}
  .decision-message .reply-heading{font-size:14px;font-weight:600;margin-bottom:18px}
  .decision-message .decision-options{display:grid;grid-template-columns:1fr;gap:0;border:1px solid var(--border);border-radius:6px;overflow:hidden}
  .decision-message .option-card{border:0;border-bottom:1px solid var(--border);border-radius:0;padding:16px;gap:7px;grid-template-columns:minmax(0,1fr);background:var(--raised)}
  .decision-message .option-card:last-child{border-bottom:0}
  .decision-message .option-card h3{font-size:13px;font-weight:600}
  .decision-message .option-card p{font-size:12px;line-height:1.65}
  .decision-message .option-card button{font-size:12px;min-height:32px;margin-top:4px;background:var(--surface)}
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
  @media(min-width:1200px){.decision-message .option-card{grid-template-columns:minmax(0,1fr) auto;column-gap:24px}.decision-message .option-card h3,.decision-message .option-card p{grid-column:1}.decision-message .option-card button{grid-column:2;grid-row:1/4;align-self:center;max-width:190px;margin:0}}
  .mailbox-main>.refresh-paused{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:30;width:max-content;max-width:90vw;margin:0;padding:9px 14px;border:1px solid var(--border-strong);background:var(--surface);box-shadow:var(--shadow);color:var(--muted);font-size:12px}
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
    .mailbox-reader .decision-message{padding:24px 20px 48px;gap:20px}.decision-message h2{font-size:22px}
    .message-sender{padding-bottom:20px}.message-sender time{max-width:110px}.decision-message .answer-form textarea{font-size:16px}
    .decision-message .option-card button{min-height:44px}.decision-message .option-card{padding:16px}.decision-message summary{min-height:44px;display:list-item}
    .mailbox-reader>.notice{margin:12px 20px 0}.mailbox-main>.refresh-paused{bottom:12px}
  }
`;
