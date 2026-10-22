/** Shared chrome for the offline, server-rendered operator console. */
import type { FC, PropsWithChildren } from "hono/jsx";
import { MAILBOX_CSS } from "./mailbox_styles.ts";

export const UI_ROOT = "/ui";

export interface NavigationCounts { needs_you: number; watching: number; handled: number }
const PRIMARY_NAV = [
  { href: UI_ROOT, label: "Needs you" },
  { href: `${UI_ROOT}/watching`, label: "Watching" },
  { href: `${UI_ROOT}/handled`, label: "Handled" },
  { href: `${UI_ROOT}/settings`, label: "Settings" },
];
const SYSTEM_NAV = [
  { href: `${UI_ROOT}/events`, label: "Event inspector" },
  { href: `${UI_ROOT}/runs`, label: "Run inspector" },
];
const CSS = `
  .nav-count{font-variant-numeric:tabular-nums;margin-left:7px;padding:1px 6px;border-radius:10px;background:var(--raised);font-size:11px}
  .eyebrow{font-size:12px;font-weight:650;color:var(--muted);letter-spacing:.02em}
  .recommended-answer{font-weight:650;color:var(--strong)}
  .decision-uncertainty{padding:12px 14px;border:1px solid var(--border-strong);border-radius:8px}
  .decision-uncertainty p{margin-top:5px}
  .refresh-paused{padding:10px 14px;background:var(--warning-soft);border-radius:8px;margin-bottom:12px}
  .refresh-paused[hidden]{display:none}
  .decision-record-link{font-size:13px}
  .decision-card small,.decision-card time{overflow-wrap:anywhere}

  .decision-list{display:grid;gap:20px;max-width:900px}.decision-card{display:grid;gap:14px;padding:24px;margin:16px 0;border:1px solid var(--border);border-radius:12px;background:var(--surface)}.decision-card h2{font-size:21px;line-height:1.4}.decision-card h2 a{color:inherit;text-decoration:none}.decision-card p{margin:0;overflow-wrap:anywhere}.decision-meta{display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted)}.recommendation,.answer-record{padding:16px;border-radius:8px;background:var(--accent-soft);border-left:3px solid var(--accent)}.recommendation p,.answer-record p{margin-top:8px}.decision-options{display:flex;flex-wrap:wrap;gap:8px}.answer-form{display:grid;gap:10px}.answer-form button{justify-self:start}.context-warning{padding:12px;background:var(--warning-soft);border-radius:6px;color:var(--fg)}.preserve-lines{white-space:pre-wrap;overflow-wrap:anywhere}.stack{display:grid;gap:16px}.notice{padding:14px;background:var(--positive-soft);margin-bottom:16px;border-radius:6px}
  @media(max-width:639px){.decision-card{padding:16px}.decision-card h2{font-size:19px}.decision-options{display:grid}.decision-options button{width:100%}.answer-form textarea{font-size:16px}}
  :root {
    color-scheme: light dark;
    --font-ui: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
    --bg: #f8f9fa; --surface: #fff; --raised: #f7f8f9; --sunken: #eef0f2;
    --fg: #35383d; --strong: #191b20; --muted: #686d76; --faint: #6a717b;
    --border: #e8eaed; --border-strong: #d3d7dc; --accent: #303943; --accent-soft: #f0f2f4;
    --positive: #24734c; --positive-soft: #e5f3e9; --warning: #946312; --warning-soft: #f9eed7;
    --critical: #b63b3b; --critical-soft: #fae8e7; --info: #3566a8; --info-soft: #e8eef8;
    --focus: #526f9c; --control-border: #c4c9cf; --on-accent: #fff; --shadow: 0 1px 2px rgba(25,27,23,.045);
    --radius-sm: 4px; --radius-md: 7px; --radius-lg: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181b; --surface: #1c1d21; --raised: #23252a; --sunken: #292c32;
      --fg: #d2d4d8; --strong: #f1f2f4; --muted: #a0a5ae; --faint: #959ca7;
      --border: #2d3036; --border-strong: #424750; --accent: #dde1e7; --accent-soft: #292c32;
      --positive: #7cdea5; --positive-soft: #143626; --warning: #f0bd64; --warning-soft: #3a2c16;
      --critical: #ff8f8a; --critical-soft: #42201f; --info: #8fb8f2; --info-soft: #192c46;
      --focus: #a7bfe3; --control-border: #505762; --on-accent: #191b20; --shadow: 0 1px 2px rgba(0,0,0,.3);
    }
  }
  *,*::before,*::after{box-sizing:border-box}
  html{min-width:320px;background:var(--bg)}
  body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);font:14px/1.5 var(--font-ui);text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-underline-offset:3px}a:hover{text-decoration-thickness:2px}
  :focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:var(--radius-sm)}
  .skip-link{position:fixed;z-index:100;left:16px;top:8px;transform:translateY(-150%);padding:8px 12px;border-radius:var(--radius-md);background:var(--strong);color:var(--surface)}
  .skip-link:focus{transform:translateY(0)}
  .app-bar{position:sticky;top:0;z-index:20;border-bottom:1px solid var(--border);background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(14px)}
  .app-bar-inner{max-width:1220px;height:52px;margin:0 auto;padding:0 32px;display:flex;align-items:center;gap:28px}
  .brand{display:inline-flex;align-items:center;gap:6px;color:var(--strong);text-decoration:none;font-size:14px;font-weight:720;letter-spacing:-.01em;white-space:nowrap}.brand-version{padding:1px 5px;border:1px solid var(--border);border-radius:999px;background:var(--sunken);color:var(--muted);font-size:10px;font-weight:650;letter-spacing:.03em}
  nav.primary{display:flex;align-self:stretch;gap:2px;overflow:visible}nav.primary::-webkit-scrollbar{display:none}
  nav.primary a{position:relative;display:flex;align-items:center;padding:0 10px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:520;white-space:nowrap}
  nav.primary a:hover,nav.primary a[aria-current="page"]{color:var(--strong)}
  nav.primary a[aria-current="page"]::after{content:"";position:absolute;height:2px;left:10px;right:10px;bottom:-1px;background:var(--accent);border-radius:2px 2px 0 0}
  .nav-menu{position:relative;align-self:center;border:0;background:transparent}.nav-menu summary{min-height:32px;padding:6px 10px;border:0;color:var(--muted);font-size:13px;font-weight:520}.nav-menu[open] summary,.nav-menu summary:hover,.nav-menu summary:focus-visible{color:var(--strong);background:var(--sunken)}.nav-menu[open] summary{border-bottom:0}.nav-menu-links{position:absolute;z-index:40;right:0;top:38px;display:grid;width:220px;padding:5px;border:1px solid var(--border-strong);border-radius:var(--radius-md);background:var(--surface);box-shadow:0 12px 32px rgba(0,0,0,.22)}nav.primary .nav-menu-links>a{display:grid;gap:1px;padding:8px 10px;border-radius:var(--radius-sm);color:var(--fg);text-decoration:none;font-size:13px}nav.primary .nav-menu-links>a:hover,nav.primary .nav-menu-links>a[aria-current="page"]{background:var(--accent-soft);color:var(--strong)}.nav-menu-links small{color:var(--muted);font-size:11px;font-weight:450}
  main{width:min(100%,1220px);margin:0 auto;padding:28px 32px 72px}
  h1,h2,h3{color:var(--strong);letter-spacing:-.015em}h1{margin:0;font-size:22px;line-height:1.25;font-weight:680}h2{margin:0;font-size:15px;line-height:1.35;font-weight:650}h3{margin:0;font-size:14px;line-height:1.4;font-weight:630}p{margin:0}
  code,pre{font-family:var(--font-mono);font-size:12.5px}code{overflow-wrap:anywhere}pre{margin:0;white-space:pre-wrap;word-break:break-word}
  .page-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:20px}.page-header-copy{max-width:760px}
  .page-eyebrow{margin-bottom:4px;color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.055em;text-transform:uppercase}.page-description{margin-top:5px;max-width:680px;color:var(--muted)}.page-meta{color:var(--muted);font-size:12px;white-space:nowrap}
  .overview-bar{display:flex;align-items:stretch;gap:0;width:fit-content;max-width:100%;margin:-4px 0 16px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);overflow:hidden}.overview-item{display:grid;grid-template-columns:auto auto;align-items:baseline;justify-content:start;gap:7px;min-width:130px;padding:9px 14px;border-right:1px solid var(--border)}.overview-item:last-child{border-right:0}.overview-value{color:var(--strong);font-size:15px;font-weight:690;font-variant-numeric:tabular-nums}.overview-label{color:var(--muted);font-size:12px}
  .stack{display:grid;gap:16px}.section{margin-top:32px}.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:10px}.section-heading p{color:var(--muted);font-size:13px}
  .panel{border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow)}.panel-header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:16px 18px;border-bottom:1px solid var(--border)}.panel-body{padding:18px}
  .notice{display:grid;gap:5px;padding:14px 16px;border:1px solid var(--border);border-left:3px solid var(--info);border-radius:var(--radius-md);background:var(--info-soft)}.notice.warning{border-left-color:var(--warning);background:var(--warning-soft)}.notice.critical{border-left-color:var(--critical);background:var(--critical-soft)}.notice.positive{border-left-color:var(--positive);background:var(--positive-soft)}.notice strong{color:var(--strong);font-size:13px}.notice p{font-size:13px}
  .muted{color:var(--muted)}.faint{color:var(--faint)}.mono{font-family:var(--font-mono);font-size:12px}
  .mobile-primary{display:none}
  .badge{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid var(--border-strong);border-radius:999px;background:var(--sunken);color:var(--fg);font-size:12px;line-height:16px;font-weight:620;white-space:nowrap}
  .badge.info,.badge.notice,.badge.rules_resolved,.badge.llm_resolved,.badge.in_progress{border-color:color-mix(in srgb,var(--info) 38%,var(--border));background:var(--info-soft);color:var(--info)}
  .badge.attention,.badge.pending,.badge.open,.badge.unanswered,.badge.queued,.badge.suggest{border-color:color-mix(in srgb,var(--warning) 38%,var(--border));background:var(--warning-soft);color:var(--warning)}
  .badge.urgent,.badge.error,.badge.escalated,.badge.rejected,.badge.failed,.badge.uncertain,.badge.dead{border-color:color-mix(in srgb,var(--critical) 38%,var(--border));background:var(--critical-soft);color:var(--critical)}
  .badge.approved,.badge.sent,.badge.granted,.badge.complete,.badge.resolved,.badge.ok,.badge.confirmed{border-color:color-mix(in srgb,var(--positive) 38%,var(--border));background:var(--positive-soft);color:var(--positive)}
  .badge.none,.badge.snoozed,.badge.expired,.badge.archived,.badge.skipped{color:var(--muted)}
  .count{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:20px;padding:0 6px;border-radius:999px;background:var(--sunken);color:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
  .desktop-filter{margin-bottom:14px}.mobile-filter{display:none}.filter-bar{display:flex;flex-wrap:wrap;align-items:end;gap:10px;margin:0;padding:12px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface)}
  .compact-filter{width:fit-content;max-width:100%;margin-bottom:18px;padding:9px 10px}.compact-filter .field{min-width:180px}.runs-heading{margin-top:22px}
  .field{display:grid;gap:4px;min-width:130px}.field.grow{flex:1 1 220px}label,.field-label{color:var(--muted);font-size:12px;font-weight:590}
  input,select,textarea,button{font:inherit}input,select,textarea{width:100%;border:1px solid var(--control-border);border-radius:var(--radius-sm);background:var(--surface);color:var(--fg)}input,select{min-height:36px;padding:7px 9px}textarea{padding:12px;resize:vertical}input::placeholder,textarea::placeholder{color:var(--faint)}
  .button{display:inline-flex;align-items:center;justify-content:center;min-height:36px;padding:7px 12px;border:1px solid var(--control-border);border-radius:var(--radius-sm);background:var(--raised);color:var(--strong);text-decoration:none;font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap}.button:hover{border-color:var(--fg);text-decoration:none}.button.primary{border-color:var(--accent);background:var(--accent);color:var(--on-accent)}.button.ghost{border-color:transparent;background:transparent;color:var(--muted)}.button.danger{color:var(--critical)}.button:disabled{cursor:not-allowed;opacity:.55}
  form.inline{display:inline-flex}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:6px}
  .tabs{display:flex;gap:3px;margin-bottom:14px;padding:3px;width:fit-content;max-width:100%;overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--sunken)}.tabs a{min-height:30px;padding:5px 10px;border-radius:var(--radius-sm);color:var(--muted);text-decoration:none;font-size:12px;font-weight:580;white-space:nowrap}.tabs a:hover{color:var(--strong)}.tabs a.active{background:var(--surface);color:var(--strong);box-shadow:var(--shadow)}
  .table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface)}table{width:100%;border-collapse:collapse}.visually-hidden,caption.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}th,td{padding:11px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}th{background:var(--sunken);color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.025em;text-transform:uppercase}tbody tr{position:relative}tbody tr:hover td{background:var(--raised)}
  .row-title{color:var(--strong);font-weight:610;text-decoration:none}.row-title::after{content:"";position:absolute;inset:0}.row-title:focus-visible{outline:none}.record:focus-within,tbody tr:focus-within td{background:var(--raised);box-shadow:inset 0 0 0 2px var(--focus)}.row-meta{margin-top:3px;color:var(--muted);font-size:12px}.cell-status{width:1%;white-space:nowrap}
  .record-list{display:none;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);overflow:hidden}.record{position:relative;display:grid;gap:7px;padding:13px 14px;border-bottom:1px solid var(--border)}.record:last-child{border-bottom:0}.record:hover{background:var(--raised)}.record-top,.record-meta{display:flex;align-items:flex-start;justify-content:space-between;gap:10px}.record-meta{justify-content:flex-start;align-items:center;flex-wrap:wrap;color:var(--muted);font-size:12px}.record-meta>span+span::before,.record-meta>time+span::before{content:"·";margin-right:8px;color:var(--faint)}
  .empty-state{padding:28px 20px;border:1px dashed var(--border-strong);border-radius:var(--radius-md);background:var(--raised);text-align:center}.empty-state h2{margin-bottom:5px}.empty-state p{max-width:520px;margin:0 auto;color:var(--muted)}.pager{margin-top:14px}
  .detail-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);gap:16px;align-items:start}.decision-panel{padding:20px;border:1px solid color-mix(in srgb,var(--warning) 45%,var(--border));border-radius:var(--radius-lg);background:var(--warning-soft)}.decision-panel .question{max-width:760px;margin:8px 0 14px;color:var(--strong);font-size:18px;line-height:1.4;font-weight:650;letter-spacing:-.01em}.decision-panel .handoff{padding-top:13px;border-top:1px solid color-mix(in srgb,var(--warning) 28%,var(--border));font-size:13px}.summary-copy{color:var(--fg);font-size:15px;line-height:1.65}
  .metadata{display:grid;grid-template-columns:max-content 1fr;gap:8px 14px}.metadata dt{color:var(--muted)}.metadata dd{margin:0;color:var(--strong);overflow-wrap:anywhere}.timeline{display:grid}.timeline-item{display:grid;grid-template-columns:128px minmax(0,1fr);gap:18px;padding:15px 0;border-bottom:1px solid var(--border)}.timeline-item:last-child{border-bottom:0}.timeline-time{color:var(--muted);font-size:12px}.timeline-body{min-width:0}.timeline-body p+p{margin-top:5px}
  details{border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface)}details+details{margin-top:8px}summary{cursor:pointer;padding:12px 14px;color:var(--strong);font-weight:610}details[open] summary{border-bottom:1px solid var(--border)}.details-body{padding:14px}.code-block{overflow-x:auto;padding:12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--sunken)}
  .review-list{display:grid;gap:8px}.review-card{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:center;padding:15px 16px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface)}.review-card h3{margin-bottom:5px}.review-card .actions{justify-content:flex-end}.confidence{font-variant-numeric:tabular-nums}.composer{display:grid;grid-template-columns:minmax(240px,2fr) minmax(150px,1fr) minmax(180px,1fr) auto;gap:10px;align-items:end}
  .authority-strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;margin-bottom:16px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--border);overflow:hidden}.authority-step{padding:13px 14px;background:var(--surface)}.authority-step strong{display:block;color:var(--strong);font-size:13px}.authority-step span{display:block;margin-top:2px;color:var(--muted);font-size:12px}
  .digest-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.digest{min-width:0;border:1px solid var(--border);border-radius:var(--radius-lg);background:var(--surface);box-shadow:var(--shadow)}.digest-header{display:flex;justify-content:space-between;gap:18px;padding:14px 16px;border-bottom:1px solid var(--border)}.digest-content{max-width:76ch;padding:16px}.digest-content h3{margin:16px 0 6px}.digest-content h3:first-child{margin-top:0}.digest-content p+p{margin-top:8px}.digest-content ul{margin:6px 0 10px;padding-left:20px}.digest-content li+li{margin-top:3px}.sign-in{width:min(100%,420px);margin:12vh auto 0}.sign-in .panel-body{display:grid;gap:16px}
  @media(max-width:899px){.app-bar-inner{padding:0 24px}main{padding:26px 24px 64px}.detail-grid{grid-template-columns:1fr}.composer{grid-template-columns:1fr 1fr}.composer .field:first-child{grid-column:1/-1}.review-card{grid-template-columns:1fr}.review-card .actions{justify-content:flex-start}.digest-list{grid-template-columns:1fr}}
  @media(max-width:639px){.app-bar{position:sticky}.app-bar-inner{height:48px;padding:0 16px;gap:12px;align-items:center}.brand{min-height:48px}nav.primary{display:none}.mobile-primary{display:block;margin-left:auto;position:relative;border:0;background:transparent}.mobile-primary summary{min-width:0;min-height:34px;justify-content:space-between;border:1px solid var(--border-strong);border-radius:var(--radius-sm);background:var(--surface);padding:6px 10px;color:var(--strong);font-size:13px}.mobile-primary[open] summary{border-bottom:1px solid var(--border-strong)}.mobile-primary-links{position:absolute;z-index:30;right:0;top:40px;display:grid;min-width:230px;padding:5px;border:1px solid var(--border-strong);border-radius:var(--radius-md);background:var(--surface);box-shadow:0 12px 32px rgba(0,0,0,.28)}.mobile-primary-links .nav-group-label{padding:8px 10px 4px;color:var(--faint);font-size:10px;font-weight:650;letter-spacing:.06em;text-transform:uppercase}.mobile-primary-links a{min-height:40px;display:flex;align-items:center;padding:7px 10px;border-radius:var(--radius-sm);color:var(--fg);text-decoration:none}.mobile-primary-links a[aria-current="page"]{background:var(--accent-soft);color:var(--strong);font-weight:650}main{padding:20px 16px 52px}.page-header{display:grid;gap:6px;margin-bottom:16px}.page-eyebrow{display:none}.page-description{margin-top:4px}.page-meta{white-space:normal}.overview-bar{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));width:100%;margin-top:0;overflow:visible}.overview-item{min-width:0;padding:8px 11px}.incident-overview{grid-template-columns:repeat(2,minmax(0,1fr))}.incident-overview .overview-item{grid-template-columns:1fr;gap:2px}.incident-overview .overview-item:nth-child(2){border-right:0}.incident-overview .overview-item:nth-child(-n+2){border-bottom:1px solid var(--border)}.desktop-filter{display:none}.mobile-filter{display:block;margin-bottom:10px;border:0;background:transparent}.mobile-filter>summary{display:flex;align-items:center;justify-content:space-between;min-height:40px;padding:7px 10px;border:1px solid var(--border);border-radius:var(--radius-md);background:var(--surface);color:var(--strong);font-size:13px}.mobile-filter[open]>summary{border-bottom:1px solid var(--border);border-radius:var(--radius-md)}.mobile-filter>.filter-bar{margin-top:8px;align-items:stretch}.filter-bar .field{flex:1 1 calc(50% - 10px);min-width:0}.filter-bar .field.grow{flex-basis:100%}.filter-bar .button{min-height:44px}.compact-filter{width:100%}.compact-filter .field{flex:1 1 auto;min-width:0}.compact-filter .button{flex:0 0 auto}input,select{min-height:44px}.desktop-table{display:none}.record-list{display:block}.tabs{width:100%}.tabs a{min-height:38px;display:flex;align-items:center;justify-content:center;flex:1}.timeline-item{grid-template-columns:1fr;gap:5px}.decision-panel{padding:15px}.decision-panel .question{font-size:17px}.composer{grid-template-columns:1fr}.composer .field:first-child{grid-column:auto}.button{min-height:44px}.digest-header{display:grid;gap:7px}.authority-strip{grid-template-columns:1fr}.authority-step{padding:11px 13px}summary{min-height:44px;display:flex;align-items:center}}
  @media(forced-colors:active){nav.primary a[aria-current="page"]{text-decoration:underline}}
  @media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important}}
`;

export const Layout: FC<PropsWithChildren<{ title: string; active?: string; refreshSeconds?: number; navCounts?: NavigationCounts; mailbox?: boolean }>> = ({ title, active, refreshSeconds, navCounts, mailbox, children }) => {
  const refreshMs = refreshSeconds ? Math.max(5, Math.min(60, refreshSeconds)) * 1_000 : 0;
  const navCount = (href: string) => !navCounts ? undefined : href === "/ui" ? navCounts.needs_you : href === "/ui/watching" ? navCounts.watching : href === "/ui/handled" ? navCounts.handled : undefined;
  return <html lang="en">
    <head>
      <meta charSet="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
      <meta name="theme-color" content="#f8f9fa" media="(prefers-color-scheme: light)" />
      <meta name="theme-color" content="#17181b" media="(prefers-color-scheme: dark)" />
      <title>{title} — CAR</title><style dangerouslySetInnerHTML={{ __html: CSS + MAILBOX_CSS }} />
      {refreshMs ? <script data-live-refresh="true" src="/ui/live-refresh.js" data-refresh-ms={refreshMs} defer /> : null}
    </head>
    <body class={`app-shell${mailbox ? " mailbox-shell" : ""}`}>
      <a class="skip-link" href="#main-content">Skip to content</a>
      <header class="app-bar"><div class="app-bar-inner">
        <a class="brand" href={UI_ROOT} aria-label="CAR attention router home"><span>CAR</span><span class="brand-version">Workspace</span></a>
        <span class="sidebar-caption">Decisions</span>
        <nav class="primary" aria-label="Primary navigation">{PRIMARY_NAV.map((item) => <a href={item.href} aria-current={active === item.href ? "page" : undefined}>{item.label}{navCount(item.href) !== undefined && <span class="nav-count">{navCount(item.href)}</span>}</a>)}</nav>
        {mailbox && <details class="shortcut-help" id="keyboard-shortcuts" hidden><summary aria-label="Keyboard shortcuts">Shortcuts <kbd>?</kbd></summary>
          <div class="shortcut-list"><strong>Keyboard shortcuts</strong><p><kbd>J</kbd> Next decision <kbd>K</kbd> Previous</p><p><kbd>1–9</kbd> Select an answer</p><p><kbd>R</kbd> Write your own reply</p><p><kbd>⌘ / Ctrl + Enter</kbd> Send focused reply</p><p><kbd>Esc</kbd> Close help or return to list</p><label><input id="letter-shortcuts" type="checkbox" checked/> Enable letter and number shortcuts</label><small>Shortcuts pause while you type. Selecting an answer never sends it.</small></div>
        </details>}
        <details class="mobile-primary"><summary>Menu</summary><nav class="mobile-primary-links" aria-label="Mobile primary navigation"><span class="nav-group-label">Work</span>{PRIMARY_NAV.map((item) => <a href={item.href} aria-current={active === item.href ? "page" : undefined}>{item.label}{navCount(item.href) !== undefined && <span class="nav-count">{navCount(item.href)}</span>}</a>)}<span class="nav-group-label">System</span>{SYSTEM_NAV.map((item) => <a href={item.href} aria-current={active === item.href ? "page" : undefined}>{item.label}{navCount(item.href) !== undefined && <span class="nav-count">{navCount(item.href)}</span>}</a>)}</nav></details>
      </div></header>
      <main id="main-content" class={mailbox ? "mailbox-main" : undefined}><p id="refresh-paused" class="refresh-paused" role="status" hidden>Refresh paused while you read or reply.</p>{children}</main>
      {refreshMs ? <div id="draft-navigation" class="draft-navigation" role="alert" hidden>
        <strong>You have an unsent reply</strong><p>Keep editing, or discard it to continue.</p>
        <div class="actions"><button id="keep-draft" class="button primary" type="button">Keep editing</button><a id="discard-draft" class="button ghost" href="">Discard and continue</a></div>
      </div> : null}
    </body>
  </html>;
};

export function statusBadge(value: string, label?: string) {
  const variant = value.toLowerCase().replace(/[^a-z0-9_-]+/g, "_");
  return <span class={`badge ${variant}`}>{label ?? value.replaceAll("_", " ")}</span>;
}
export function severityChip(severity: string) { return statusBadge(severity); }
export function autonomyChip(autonomy: string) {
  const labels: Record<string, string> = { granted: "Granted", suggest: "Suggest", none: "Off" };
  return statusBadge(autonomy, labels[autonomy] ?? autonomy);
}
