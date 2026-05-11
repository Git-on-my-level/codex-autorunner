import{p as s}from"./UwleghFi.js";import{b as i,a as l}from"./MjN7mR6z.js";function a(r,e){const o=r[e];return typeof o=="string"&&o.trim()?o.trim():null}function c(r){const e=a(r.raw,"repo_id")??a(r.raw,"base_repo_id")??a(r.frontmatter,"repo_id")??a(r.frontmatter,"base_repo_id");return r.workspaceKind==="worktree"&&r.workspaceId?{id:`worktree:${r.workspaceId}`,kind:"worktree",label:r.workspaceId,detail:`Worktree · ${e??r.workspaceId}`,workspaceRoot:a(r.raw,"workspace_root")??r.workspacePathLabel??".",resourceId:r.workspaceId,parentRepoId:e,scopeUrn:e?`worktree:${e}/${r.workspaceId}`:`filesystem:${encodeURIComponent(a(r.raw,"workspace_root")??r.workspacePathLabel??".")}`}:r.workspaceKind==="repo"&&r.workspaceId?{id:`repo:${r.workspaceId}`,kind:"repo",label:r.workspaceId,detail:`Repo · ${r.workspaceId}`,resourceKind:"repo",resourceId:r.workspaceId,scopeUrn:`repo:${r.workspaceId}`}:{id:"local",kind:"local",label:"Local hub",detail:"Current workspace",scopeUrn:"hub"}}function w(r){const e=r.raw,o=a(e,"hub_root")??"(hub root from the serving CAR instance)",t=a(e,"workspace_root")??r.workspacePathLabel??"(unknown workspace root)",n=r.pathLabel??"(unknown ticket path)",p=r.errors.length?r.errors.map(d=>`- ${d}`).join(`
`):"- Frontmatter validation failed";return`Please repair this CAR ticket frontmatter and lint the ticket queue.

Hub root: ${o}
Workspace root: ${t}
Ticket path: ${n}
Absolute ticket path: ${t}/${n}

Validation errors:
${p}

Requirements:
- Edit only the ticket file unless linting reveals directly related ticket metadata issues.
- Fix the YAML frontmatter so the ticket can run.
- Preserve the ticket body content.
- Run: python3 .codex-autorunner/bin/lint_tickets.py from the workspace root.
- Report exactly what changed and the lint result.`}async function m(r,e,o){o("Creating PMA repair chat...");const t=await s.pma.createChat(i("codex",c(r),`Repair ${r.numberLabel} frontmatter`));if(!t.ok){o(t.error.message);return}const n=await s.pma.sendMessage(t.data.id,l(w(r),"",!1));if(!n.ok){o(n.error.message);return}await e.goto(e.href(`/chats?chat=${encodeURIComponent(t.data.id)}`))}export{m as r};
