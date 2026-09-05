import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
const load=(p)=>import(pathToFileURL(join(process.env.CAR_PORTABLE_ROOT,'src',p+'.js')));
const { Store, openDb }=await load('store/db');
const { AttentionService }=await load('attention/service');
const { RequestCard, NativeCard, PageHeader, DecisionError }=await load('surfaces/web/decision_views');
const { Layout }=await load('surfaces/web/layout');
const { LIVE_REFRESH_JS }=await load('surfaces/web/live_refresh');
const owner={workspaceId:'test',clientId:'agent-mini',host:'mac-mini'};
const packet={goal:'Ship the API migration without interrupting integrations',blocker:'An external integration may still call v1',question:'Preserve API v1 for one more release?',why_human:'No existing mandate covers breaking an external interface.',impact:'API migration and SDK release',urgency:'normal',facts:[{statement:'One external integration is still configured for v1.',source:'src/integrations/client.ts:42'}],attempts:['Checked repository decisions and searched active callers.'],uncertainty:['Production usage for the external integration has not been verified.'],recommendation:{answer:'Preserve v1 for this release.',rationale:'Avoids a breaking change while usage is measured.'},options:[{id:'keep',label:'Preserve for one release',answer:'Preserve API v1 compatibility for this release. Add usage measurement before deciding when to remove it.',consequences:'Maintain a small compatibility adapter; revisit removal with usage evidence.'},{id:'remove',label:'Remove v1 now',answer:'Remove API v1 in this release and notify affected integrators.',consequences:'The external integration may break until its owner migrates.'}]};
function fixture(t){const clock={now:()=>new Date('2026-09-04T12:00:00Z')};const store=new Store(openDb(':memory:'),clock);t.after(()=>store.db.close());return new AttentionService(store,{attention:{workspace_id:'test',prepare_seconds:120,max_context_rounds:2,max_active_per_client:1000},telegram:{enabled:false}},{sendNotify(){},sendEscalation(){},sendDigest(){}})}
function render(service,row,opts={}){return String(RequestCard({view:service.view(row),row,canWrite:true,detail:true,...opts}))}
test('decision choices expose exact answer, consequences, uncertainty and evidence before commitment',t=>{
 const s=fixture(t),r=s.raise(owner,'one',packet),html=render(s,r);
 for(const o of packet.options){assert.ok(html.includes(o.answer));assert.ok(html.includes(o.consequences));assert.ok(html.indexOf(o.answer)<html.indexOf('Send reply'))}
 assert.ok(html.indexOf('Before deciding')<html.indexOf('Send reply'));assert.match(html,/Source agent recommends/);assert.match(html,/not independent verification/);
 assert.match(html,/name="expected_revision" value="1"/);assert.match(html,/Applies to this request only/);
 assert.equal((html.match(/action="[^"]+\/answer"/g)||[]).length,1);
 assert.equal((html.match(/type="radio"/g)||[]).length,3);
 assert.match(html,/Write my own answer/);assert.match(html,/Moves to Watching/);
});
test('source-provided HTML remains text in every decision field',t=>{
 const s=fixture(t),evil='<img src=x onerror="window.injected=true">',r=s.raise(owner,'one',{...packet,question:evil,facts:[{statement:evil,source:'javascript:alert(1)'}]});
 const html=render(s,r);assert.equal(html.includes('<img src=x'),false);assert.match(html,/&lt;img/);assert.equal(html.includes('href="javascript:'),false);
});
test('read-only human views contain no actionable forms',t=>{const s=fixture(t),r=s.raise(owner,'one',packet);assert.equal(render(s,r,{canWrite:false}).includes('<form'),false)});
test('expired cards show review instead of a disabled or stale approval',t=>{
 const s=fixture(t),r=s.raise(owner,'one',{...packet,deadline_at:'2026-09-04T11:59:59Z'}),html=render(s,r);
 assert.match(html,/Deadline missed · not approved/);assert.match(html,/review-expiry/);assert.equal(html.includes(`/decisions/${r.id}/answer`),false);
});
test('recorded and received answers never claim the source is already unblocked',t=>{
 const s=fixture(t),r=s.raise(owner,'one',packet),a=s.answer(r.id,1,'human:web',{option_id:'keep'});
 const answered=render(s,s.get(r.id));assert.match(answered,/waiting for receipt/);assert.equal(answered.includes(`/decisions/${r.id}/answer`),false);
 s.acknowledge(owner,r.id,a.id,'received');assert.match(render(s,s.get(r.id)),/waiting for work to resume/);
});
test('native delivery controls are bound to the reply revision and require source evidence',()=>{
 const html=String(NativeCard({row:{id:'esc_one',incident_id:'inc_one',event_type:'attention.question',question:'Proceed?',state:'answered',body:'Context',source_host:'vm',obligation_state:'delivered',reply_state:'uncertain',reply_id:'reply_one',reply_revision:7,last_error:'Unknown receipt',snooze_until:null},canWrite:true}));
 assert.match(html,/expected_revision" value="7/);assert.match(html,/Do not resend/);assert.match(html,/What did you verify at the source/);
});
test('layout keeps counters, accessible labels, local timestamps and protected refresh',t=>{
 const s=fixture(t),r=s.raise(owner,'one',packet);const card=RequestCard({view:s.view(r),row:r,canWrite:true,detail:true});
 const html='<!doctype html>'+String(Layout({title:'Decision',active:'/ui',refreshSeconds:15,navCounts:{needs_you:1,watching:2,handled:3},children:[PageHeader({title:'Needs you',description:'Grounded decisions. Your answer is recorded before delivery.'}),card]}));
 assert.match(html,/Skip to content/);assert.match(html,/role="status" hidden/);assert.match(html,/for="answer-/);assert.match(html,/aria-labelledby=/);assert.match(html,/nav-count/);
 const dir=process.env.CAR_UI_ARTIFACT_DIR;if(dir){mkdirSync(join(dir,'ui'),{recursive:true});writeFileSync(join(dir,'decision.html'),html);writeFileSync(join(dir,'ui/live-refresh.js'),LIVE_REFRESH_JS);
  const exp=s.raise(owner,'expired',{...packet,deadline_at:'2026-09-04T11:00:00Z'});writeFileSync(join(dir,'expired.html'),'<!doctype html>'+String(Layout({title:'Missed decision',active:'/ui',refreshSeconds:15,children:RequestCard({view:s.view(exp),row:exp,canWrite:true,detail:true})})));}
});

test('failed human submissions preserve bounded text as escaped data, not a success',()=>{
 const html=String(DecisionError({message:'The decision changed.',draft:'<script>do not execute</script>',href:'/ui/decisions/req_one'}));
 assert.match(html,/Action not confirmed/);assert.match(html,/Retained here for copying/);assert.match(html,/&lt;script&gt;/);assert.equal(html.includes('<script>'),false);
});
