import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const load = (name) => import(pathToFileURL(join(process.env.CAR_PORTABLE_ROOT, 'src', name+'.js')));
const { Database } = await import(pathToFileURL(join(process.env.CAR_PORTABLE_ROOT,'shims/sqlite.mjs')));
const { Store, openDb, StaleClaimError } = await load('store/db');
const { SCHEMA_VERSION, APPLICATION_ID } = await load('store/schema');
const { AttentionService } = await load('attention/service');
const { recordAnswer, recordSessionReply, deliverReply, recoverInterruptedReplies, reconcileReply, createReplyWorker } = await load('attention/replies');
const { TriageRepo } = await load('triage/repo');
const { assessPacket } = await load('attention/quality');
const { classifyEvent } = await load('triage/rules');
const {enqueueMessage, deliverOutboxOnce, reconcileTelegramDeliveryProjections} = await load('surfaces/telegram/outbox');
const owner={workspaceId:'test',clientId:'mac',host:'mac-a'};
const bare = () => ({goal:'Ship the migration',blocker:'External compatibility is undecided',question:'Preserve v1?',attempts:[],facts:[],options:[],uncertainty:[],urgency:'normal'});
const complete = () => ({...bare(),why_human:'No migration mandate exists',attempts:['Checked callers'],facts:[{statement:'One external caller exists',source:'src/client.ts:10'}],recommendation:{answer:'Preserve for this release',rationale:'Avoid breakage'},impact:'API and SDK work',options:[{id:'keep',label:'Keep',answer:'Preserve v1 for this release',consequences:'Maintain a compatibility shim'}]});
function fixture(t, overrides={}) {
  const clock={current:new Date('2026-09-04T12:00:00Z'),now(){return this.current},advance(ms){this.current=new Date(this.current.getTime()+ms)}};
  const store=new Store(openDb(':memory:'),clock);
  const config={attention:{workspace_id:'test',prepare_seconds:120,max_context_rounds:2,...overrides},telegram:{enabled:true}};
  const channel={escalations:[],sendEscalation(value){this.escalations.push(value)},sendNotify(){},sendDigest(){}};
  const service=new AttentionService(store,config,channel);
  t.after(()=>store.db.close()); return {clock,store,config,channel,service};
}
const count=(store,table)=>store.db.query(`SELECT COUNT(*) n FROM ${table}`).get().n;
function native(t,{required=true,session=true}={}) {
  const f=fixture(t); const event=f.store.ingestEvent({contract:'car.event.v1',idempotency_key:'native:1',ts:f.clock.now().toISOString(),source:{vendor:'other',host:'host',adapter:'test'},session:session?{vendor:'other',host:'host',native_id:'native-one'}:null,type:'attention.question',severity:'attention',requires_response:required,response_channel:null,title:'Proceed?',body:'Human judgment required',payload:{}},{sourceId:'native:source'});
  const repo=new TriageRepo(f.store);const incident=repo.openIncident({carSessionId:event.car_session_id,openedByEvent:event.event_id,summary:'Proceed?',dedupeClass:'native'});
  const escalation=repo.createEscalation({originEventId:event.event_id,incidentId:incident.id,severity:'attention',question:'Proceed?'});
  repo.setIncidentState(incident.id,'escalated');
  return {...f,event,incident,escalation,repo};
}

test('fresh workspace uses the canonical CAR3 bootstrap',t=>{const {store}=fixture(t);assert.equal(store.db.query('PRAGMA user_version').get().user_version,SCHEMA_VERSION);assert.equal(store.db.query('PRAGMA application_id').get().application_id,APPLICATION_ID);assert.equal(count(store,'human_replies'),0)});
test('unknown pre-release state is rejected without conversion or data loss',t=>{
  const folder=mkdtempSync(join(tmpdir(),'car-schema-'));const file=join(folder,'car.db');t.after(()=>rmSync(folder,{recursive:true,force:true}));
  const db=new Database(file);db.exec("CREATE TABLE retained(value TEXT); INSERT INTO retained VALUES ('evidence'); PRAGMA user_version=8");db.close();
  assert.throws(()=>openDb(file),{name:'SchemaMismatchError'});
  const original=new Database(file);assert.equal(original.query('SELECT value FROM retained').get().value,'evidence');assert.equal(original.query('PRAGMA user_version').get().user_version,8);original.close();
});
test('incomplete requests guide agents without creating premature human cards',t=>{const {service,channel}=fixture(t);const row=service.raise(owner,'one',bare());assert.equal(row.state,'preparing');assert.ok(service.view(row).preparation.context_requests.length>=4);assert.equal(channel.escalations.length,0)});
test('complete request reaches the shared incident/escalation model without an LLM',t=>{const {service,store,channel}=fixture(t);const row=service.raise(owner,'one',complete());assert.equal(row.state,'needs_you');assert.equal(count(store,'incidents'),1);assert.equal(count(store,'escalations'),1);assert.equal(channel.escalations.length,1);assert.equal(store.getEvent(row.event_id).route_state,'escalated')});
test('urgent incomplete requests bypass context preparation immediately',t=>{const {service}=fixture(t);assert.equal(service.raise(owner,'one',{...bare(),urgency:'urgent'}).state,'needs_you')});
test('lost callers cannot hide incomplete packets past preparation deadline',t=>{const {service,clock}=fixture(t);const r=service.raise(owner,'one',bare());clock.advance(120001);service.sweep();assert.equal(service.get(r.id).state,'needs_you')});
test('bounded context rounds prevent endless preparation',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',bare());assert.equal(service.enrich(owner,r.id,1,bare()).state,'preparing');assert.equal(service.enrich(owner,r.id,2,bare()).state,'needs_you')});
test('useful enrichment freezes packet and stale revisions are rejected',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',bare());const next=service.enrich(owner,r.id,1,complete());assert.equal(next.revision,2);assert.equal(next.state,'needs_you');assert.equal(service.enrich(owner,r.id,1,complete()).revision,2);assert.throws(()=>service.enrich(owner,r.id,1,{...complete(),impact:'Changed'}),{code:'revision_conflict'});assert.throws(()=>service.enrich(owner,r.id,2,complete()),{code:'packet_frozen'})});
test('original offline replay remains valid after enrichment',t=>{const {service,store}=fixture(t);const r=service.raise(owner,'one',bare());service.enrich(owner,r.id,1,complete());const replay=service.raise(owner,'one',bare());assert.equal(replay.revision,2);assert.equal(count(store,'attention_requests'),1);assert.equal(count(store,'escalations'),1)});
test('different payload with same idempotency key conflicts',t=>{const {service}=fixture(t);service.raise(owner,'one',bare());assert.throws(()=>service.raise(owner,'one',{...bare(),question:'Other?'}),{code:'idempotency_conflict'})});
test('client/host/workspace isolation applies even for known request ids',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',bare());for(const other of [{...owner,clientId:'other'},{...owner,host:'other'},{...owner,workspaceId:'other'}]) assert.throws(()=>service.owned(r.id,other),{code:'not_found'});assert.equal(service.list({...owner,host:'other'}).length,0);assert.throws(()=>service.raise({...owner,host:'other'},'one',bare()),{code:'identity_changed'})});
test('same idempotency key is scoped per authenticated client',t=>{const {service}=fixture(t);const a=service.raise(owner,'same',complete());const b=service.raise({...owner,clientId:'other'},'same',complete());assert.notEqual(a.id,b.id);assert.equal(service.list(owner).length,1)});
test('context cannot change the question, urgency or deadline underneath a request',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',bare());for(const changed of [{question:'Different?'},{urgency:'urgent'},{deadline_at:'2026-09-05T00:00:00Z'}])assert.throws(()=>service.enrich(owner,r.id,1,{...complete(),...changed}),{code:'identity_changed'})});
test('past-deadline request is history, never an actionable card',t=>{const {service,channel}=fixture(t);const r=service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T11:59:59Z'});assert.equal(r.state,'expired');assert.equal(channel.escalations.length,0)});
test('near deadline bypasses preparation',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',{...bare(),deadline_at:'2026-09-04T12:00:04Z'});assert.equal(r.state,'needs_you')});
test('answer and durable delivery intent are atomic, not resolution',t=>{const {service,store}=fixture(t);const r=service.raise(owner,'one',complete());const reply=service.answer(r.id,1,'human:test',{option_id:'keep'});assert.equal(reply.state,'staged');assert.equal(service.get(r.id).state,'answered');assert.equal(store.db.query('SELECT state FROM incidents WHERE id=?').get(r.incident_id).state,'open');assert.equal(count(store,'human_facts'),1);assert.equal(count(store,'grants'),0)});
test('failure recording a human fact rolls back answer and reply together',t=>{const {service,store}=fixture(t);const r=service.raise(owner,'one',complete());const save=store.recordHumanFact;store.recordHumanFact=()=>{throw new Error('disk fault')};assert.throws(()=>service.answer(r.id,1,'human:test',{text:'Keep'}),/disk fault/);store.recordHumanFact=save;assert.equal(service.get(r.id).state,'needs_you');assert.equal(count(store,'human_replies'),0);assert.equal(store.db.query('SELECT state FROM escalations WHERE id=?').get(r.escalation_id).state,'pending')});
test('same answer replay returns one intent; conflicting answer is rejected',t=>{const {service,store}=fixture(t);const r=service.raise(owner,'one',complete());const a=service.answer(r.id,1,'human:one',{text:'Keep'});const b=service.answer(r.id,1,'human:two',{text:'Keep'});assert.equal(a.id,b.id);assert.equal(count(store,'human_replies'),1);assert.throws(()=>service.answer(r.id,1,'human:two',{text:'Remove'}),{code:'already_answered'})});
test('human options and packet revisions cannot be forged',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',complete());assert.throws(()=>service.answer(r.id,2,'human:test',{text:'Keep'}),{code:'revision_conflict'});assert.throws(()=>service.answer(r.id,1,'human:test',{option_id:'not-real'}),{code:'unknown_option'})});
test('fetching an answer does not acknowledge it; exact receipt precedes source resolution',t=>{const {service,store}=fixture(t);const r=service.raise(owner,'one',complete());const reply=service.answer(r.id,1,'human:test',{text:'Keep'});assert.equal(service.view(service.get(r.id)).answer.delivery,'staged');assert.throws(()=>service.acknowledge(owner,r.id,'wrong','received'),{code:'answer_mismatch'});assert.equal(service.acknowledge(owner,r.id,reply.id,'received').state,'received');assert.equal(store.db.query('SELECT state FROM incidents WHERE id=?').get(r.incident_id).state,'open');assert.equal(service.acknowledge(owner,r.id,reply.id,'resolved','Migration resumed').state,'resolved');assert.equal(service.acknowledge(owner,r.id,reply.id,'resolved').state,'resolved');assert.equal(service.view(service.get(r.id)).answer.eligible_for_receipt,false)});
test('cancellation makes late answer and acknowledgement non-actionable',t=>{const {service}=fixture(t);const r=service.raise(owner,'one',complete());const reply=service.answer(r.id,1,'human:test',{text:'Keep'});service.cancel(owner,r.id,1,'Work superseded');assert.equal(service.view(service.get(r.id)).answer.eligible_for_receipt,false);assert.throws(()=>service.acknowledge(owner,r.id,reply.id,'received'),{code:'request_closed'})});
test('expiry between answer and receipt rejects receipt even before sweeper runs',t=>{const {service,clock}=fixture(t);const r=service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T12:01:00Z'});const reply=service.answer(r.id,1,'human:test',{text:'Keep'});clock.advance(60001);assert.equal(service.view(service.get(r.id)).answer.eligible_for_receipt,false);assert.throws(()=>service.acknowledge(owner,r.id,reply.id,'received'),{code:'request_closed'});service.sweep();assert.equal(service.get(r.id).state,'expired')});
test('received work can finish after its decision deadline',t=>{const {service,clock}=fixture(t);const r=service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T12:01:00Z'});const a=service.answer(r.id,1,'human:test',{text:'Keep'});service.acknowledge(owner,r.id,a.id,'received');clock.advance(60001);service.sweep();assert.equal(service.get(r.id).state,'received');assert.equal(service.acknowledge(owner,r.id,a.id,'resolved').state,'resolved')});
test('stable native escalation identities survive event replay and sent_at awaits a receipt',t=>{const f=native(t);const id=f.repo.createEscalation({id:'esc-stable',originEventId:f.event.event_id,incidentId:f.incident.id,severity:'attention',question:'Proceed?'});f.repo.createEscalation({id:'esc-stable',originEventId:f.event.event_id,incidentId:f.incident.id,severity:'attention',question:'Proceed?'});assert.equal(f.store.db.query('SELECT sent_at FROM escalations WHERE id=?').get(id).sent_at,null);assert.equal(f.store.db.query('SELECT count(*) n FROM escalations WHERE id=?').get(id).n,1)});
test('pending native human reply survives process-style recovery and sends once',async t=>{const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{approval:true}});assert.equal(r.state,'pending');recoverInterruptedReplies(f.store);let sent=0;const bus={deliver:async()=>{sent++;return 'delivered'}};await deliverReply(f.store,bus,r.id);await deliverReply(f.store,bus,r.id);assert.equal(sent,1);assert.equal(f.store.getEvent(f.event.event_id).obligation_state,'delivered');assert.equal(f.store.db.query('SELECT state FROM incidents WHERE id=?').get(f.incident.id).state,'open')});
for (const result of ['queued','degraded'])test(`${result} reply is staged, never resolved`,async t=>{const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});const delivered=await deliverReply(f.store,{deliver:async()=>result},r.id);assert.equal(delivered.state,'staged');assert.equal(f.store.getEvent(f.event.event_id).obligation_state,'staged')});
test('crash during send becomes uncertain and is not automatically replayed',async t=>{const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});f.store.db.query("UPDATE human_replies SET state='delivering' WHERE id=?").run(r.id);assert.equal(recoverInterruptedReplies(f.store),1);let sent=0;const result=await deliverReply(f.store,{deliver:async()=>{sent++;return 'delivered'}},r.id);assert.equal(result.state,'uncertain');assert.equal(sent,0)});
test('transport exception remains uncertain and visible',async t=>{const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});const result=await deliverReply(f.store,{deliver:async()=>{throw new Error('timeout after acceptance')}},r.id);assert.equal(result.state,'uncertain');assert.match(result.last_error,/unknown/)});
test('sessionless legacy question cannot claim delivery',async t=>{const f=native(t,{session:false});const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});const result=await deliverReply(f.store,{deliver:async()=>{throw new Error('must not send')}},r.id);assert.equal(result.state,'failed')});
test('source clearance during native delivery wins the state race',async t=>{const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});const result=await deliverReply(f.store,{deliver:async()=>{f.store.db.query("UPDATE human_replies SET state='resolved' WHERE id=?").run(r.id);f.store.db.query("UPDATE events SET obligation_state='resolved' WHERE id=?").run(f.event.event_id);return 'delivered'}},r.id);assert.equal(result.state,'resolved');assert.equal(f.store.getEvent(f.event.event_id).obligation_state,'resolved')});
test('general instructions also have durable idempotent delivery identities',t=>{const f=native(t);const input={idempotencyKey:'telegram:one',actor:'human:test',carSessionId:f.event.car_session_id,channel:null,payload:{text:'Please investigate'}};assert.equal(recordSessionReply(f.store,input).id,recordSessionReply(f.store,input).id);assert.throws(()=>recordSessionReply(f.store,{...input,payload:{text:'Different'}}),{code:'idempotency_conflict'})});
test('claim lanes prioritize urgency and fence stale workers',t=>{const f=native(t);const urgent=f.store.ingestEvent({contract:'car.event.v1',idempotency_key:'urgent',ts:f.clock.now().toISOString(),source:{vendor:'other',host:'host',adapter:'test'},session:null,type:'attention.error',severity:'urgent',requires_response:false,title:'Urgent',body:'',payload:{}},{sourceId:'urgent'});const normal=f.store.claimEvents(1,1,'normal','normal')[0];assert.equal(normal.id,f.event.event_id);const high=f.store.claimEvents(1,1,'urgent','urgent')[0];assert.equal(high.id,urgent.event_id);f.clock.advance(2000);const reclaimed=f.store.claimEvents(1,60,'new','normal')[0];assert.equal(reclaimed.id,normal.id);assert.throws(()=>f.store.completeEventClaim(normal.id,{owner:'normal',token:normal.route_claim_token},'resolved'),StaleClaimError)});
test('source identity prevents unrelated sessionless incident correlation',t=>{const f=native(t,{session:false});assert.ok(f.repo.findLineageIncident(null,'native','native:source'));assert.equal(f.repo.findLineageIncident(null,'native','other:source'),null)});
test('bounded context guidance permits honest inability instead of fabricated facts',()=>{const packet={...bare(),why_human:'Only human can decide',impact:'Release blocked',cannot_investigate:'No repository access'};assert.equal(assessPacket(packet).length,0)});
test('response-required lifecycle events cannot be silently discarded as noise',t=>{const f=native(t);const row=f.store.getEvent(f.event.event_id);for (const type of ['heartbeat','progress','note','session.ended'])assert.notEqual(classifyEvent({...row,type,severity:'info'},{now:f.clock.now(),grantedRules:[]}).kind,'resolved')});
test('urgent lifecycle events bypass the noise filter',t=>{const f=native(t);const row=f.store.getEvent(f.event.event_id);assert.equal(classifyEvent({...row,type:'heartbeat',severity:'urgent',requires_response:0},{now:f.clock.now(),grantedRules:[]}).kind,'escalate')});


test('active-request quota is checked only for fresh work, never idempotent replay',t=>{
 const {service}=fixture(t); service.config.attention.max_active_per_client=1;
 const r=service.raise(owner,'one',bare());assert.equal(service.raise(owner,'one',bare()).id,r.id);
 assert.throws(()=>service.raise(owner,'two',bare()),{code:'too_many_active_requests'});
 service.cancel(owner,r.id,1,'Superseded');assert.ok(service.raise(owner,'two',bare()));
});
test('listing summaries omit large private evidence and answer bodies',t=>{
 const {service}=fixture(t);const r=service.raise(owner,'one',complete());
 const summary=service.summary(r);assert.equal(summary.id,r.id);assert.equal(summary.question,'Preserve v1?');
 assert.equal(summary.packet,undefined);assert.equal(summary.answer,undefined);
});
test('uncertain delivery can be retried only after explicit human reconciliation',async t=>{
 const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});
 const uncertain=await deliverReply(f.store,{deliver:async()=>{throw new Error('network timeout')}},r.id);
 assert.throws(()=>reconcileReply(f.store,{id:r.id,expectedRevision:uncertain.revision,actor:'human:test',outcome:'not_received_retry',note:''}),{code:'evidence_required'});
 const pending=reconcileReply(f.store,{id:r.id,expectedRevision:uncertain.revision,actor:'human:test',outcome:'not_received_retry',note:'Checked native session; the response never arrived'});
 assert.equal(pending.state,'pending');let sends=0;
 await deliverReply(f.store,{deliver:async()=>{sends++;return 'delivered'}},r.id);assert.equal(sends,1);
});
test('human reconciliation cannot bypass the guided source receipt protocol',t=>{
 const f=fixture(t);const r=f.service.raise(owner,'one',complete());const reply=f.service.answer(r.id,1,'human:test',{text:'Keep'});
 assert.throws(()=>reconcileReply(f.store,{id:reply.id,expectedRevision:reply.revision,actor:'human:test',outcome:'source_confirmed',note:'Assumed it worked'}),{code:'source_owned'});
});
test('failed delivery alerts are durable and emitted once per attempt',async t=>{
 const f=native(t);recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});
 const notifications=[];const worker=createReplyWorker(f.store,{deliver:async()=> 'failed'}, {sendNotify:text=>notifications.push(text)});
 await worker.tick();await worker.tick();assert.equal(notifications.length,1);assert.match(notifications[0],/failed/);
});

test('new wake-up receipt replaces the old Telegram card without replay regression',async t=>{
 const f=native(t);const target={kind:'escalation',escalation_id:f.escalation,incident_id:f.incident.id,car_session_id:f.event.car_session_id};
 let seq=0;const send=async()=>({message_id:String(++seq)});
 const first=enqueueMessage(f.store,target,{text:'initial'},{intentId:'first'});await deliverOutboxOnce(f.store,send);
 enqueueMessage(f.store,target,{text:'wake'},{intentId:'second'});await deliverOutboxOnce(f.store,send);
 const current=()=>f.store.db.query('SELECT telegram_message_id FROM escalations WHERE id=?').get(f.escalation).telegram_message_id;
 assert.equal(current(),'2');f.store.db.query('DELETE FROM kv WHERE key=?').run('tg.delivery_projection.'+first);
 reconcileTelegramDeliveryProjections(f.store);assert.equal(current(),'2');assert.equal(seq,2);
});
test('answered cards are suppressed before an unsent Telegram notification is delivered',async t=>{
 const f=native(t);enqueueMessage(f.store,{kind:'escalation',escalation_id:f.escalation,incident_id:f.incident.id},{text:'needs you'});
 recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});let sends=0;
 await deliverOutboxOnce(f.store,async()=>{sends++;return {message_id:'unexpected'}});assert.equal(sends,0);
});
test('reconciling one reply cannot close another outstanding request in the incident',async t=>{
 const f=native(t);const r=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});
 const delivered=await deliverReply(f.store,{deliver:async()=> 'delivered'},r.id);
 const extra=f.store.ingestEvent({contract:'car.event.v1',idempotency_key:'other-outstanding',ts:f.clock.now().toISOString(),source:{vendor:'other',host:'host',adapter:'test'},session:null,type:'attention.question',severity:'attention',requires_response:true,title:'Another question',body:'',payload:{}},{sourceId:'native:source'});
 f.store.db.query('UPDATE events SET incident_id=? WHERE id=?').run(f.incident.id,extra.event_id);
 reconcileReply(f.store,{id:r.id,expectedRevision:delivered.revision,actor:'human:test',outcome:'source_confirmed',note:'First source confirmed it resumed'});
 assert.notEqual(f.store.db.query('SELECT state FROM incidents WHERE id=?').get(f.incident.id).state,'resolved');
});

const { decisionCounts, REQUEST_CONDITION } = await load('surfaces/web/decision_queries');
test('answer cannot skip durable receipt to report a resolution',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',complete());const answer=f.service.answer(row.id,1,'human:test',{text:'Keep'});
 assert.throws(()=>f.service.acknowledge(owner,row.id,answer.id,'resolved'),{code:'receipt_required'});
 assert.equal(f.service.get(row.id).state,'answered');assert.equal(f.store.getEvent(row.event_id).obligation_state,'answered');
});
test('receipt retries are idempotent even with an unchanged clock',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',complete());const a=f.service.answer(row.id,1,'human:test',{text:'Keep'});
 f.service.acknowledge(owner,row.id,a.id,'received');const before=f.store.db.query('SELECT * FROM human_replies WHERE id=?').get(a.id);
 f.service.acknowledge(owner,row.id,a.id,'received');assert.deepEqual(f.store.db.query('SELECT * FROM human_replies WHERE id=?').get(a.id),before);
});
test('expiry at the answer boundary needs no timer and implies no authorization',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T12:00:01Z'});f.clock.advance(1000);
 assert.throws(()=>f.service.answer(row.id,1,'human:test',{text:'Keep'}));assert.equal(f.service.get(row.id).state,'expired');
 assert.equal(count(f.store,'human_replies'),0);assert.equal(count(f.store,'grants'),0);
});
test('missed decision remains Needs you until reviewed, and remains expired afterwards',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T12:00:01Z'});f.clock.advance(1000);f.service.sweep();
 assert.equal(decisionCounts(f.store,'test').needs_you,1);assert.equal(decisionCounts(f.store,'test').handled,0);
 assert.throws(()=>f.service.reviewExpiry(row.id,1,'human:test',''),{code:'reason_required'});
 const reviewed=f.service.reviewExpiry(row.id,1,'human:test','Asked source for a fresh decision');
 assert.equal(reviewed.state,'expired');assert.ok(reviewed.reviewed_at);assert.equal(decisionCounts(f.store,'test').needs_you,0);assert.equal(decisionCounts(f.store,'test').handled,1);
 f.service.reviewExpiry(row.id,1,'human:test','Ignored duplicate');assert.equal(count(f.store,'human_facts'),1);assert.equal(count(f.store,'grants'),0);
});
test('expiry review is atomic with the human audit fact',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',{...complete(),deadline_at:'2026-09-04T11:00:00Z'});
 f.store.recordHumanFact=()=>{throw new Error('disk fault')};assert.throws(()=>f.service.reviewExpiry(row.id,1,'human:test','Checked'),/disk fault/);
 assert.equal(f.service.get(row.id).reviewed_at,null);
});
test('withdrawal invalidates a received answer but cannot relabel resolved work',t=>{
 const f=fixture(t);const r=f.service.raise(owner,'one',complete());const a=f.service.answer(r.id,1,'human:test',{text:'Keep'});f.service.acknowledge(owner,r.id,a.id,'received');
 assert.throws(()=>f.service.withdraw(r.id,2,'Superseded','human:test'),{code:'revision_conflict'});
 f.service.withdraw(r.id,1,'Work is superseded','human:test');
 assert.equal(f.service.get(r.id).state,'cancelled');assert.equal(f.service.view(f.service.get(r.id)).answer.delivery,'cancelled');
 assert.throws(()=>f.service.acknowledge(owner,r.id,a.id,'resolved'),{code:'request_closed'});
 const other=f.service.raise(owner,'two',complete());const b=f.service.answer(other.id,1,'human:test',{text:'Keep'});f.service.acknowledge(owner,other.id,b.id,'received');f.service.acknowledge(owner,other.id,b.id,'resolved');
 assert.throws(()=>f.service.withdraw(other.id,1,'Too late','human:test'),{code:'request_closed'});
});
test('guidance is executable and permission is not implied by answer availability',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',bare());let v=f.service.view(row);
 assert.equal(v.guidance.code,'add_context');assert.equal(v.guidance.standing_permission,false);
 f.service.enrich(owner,row.id,1,complete());v=f.service.view(f.service.get(row.id));assert.equal(v.guidance.code,'wait_for_human');
 const a=f.service.answer(row.id,2,'human:test',{text:'Keep'});v=f.service.view(f.service.get(row.id));assert.equal(v.guidance.code,'receive_answer');assert.equal(v.guidance.can_apply_answer,false);assert.equal(v.answer.eligible_for_receipt,true);
 f.service.acknowledge(owner,row.id,a.id,'received');v=f.service.view(f.service.get(row.id));assert.equal(v.guidance.can_apply_answer,true);assert.equal(v.guidance.code,'report_resolution');
 f.service.acknowledge(owner,row.id,a.id,'resolved');v=f.service.view(f.service.get(row.id));assert.equal(v.guidance.code,'stop');assert.equal(v.guidance.can_apply_answer,false);
});
test('context retries never increment rounds twice or replace the surfaced evidence',t=>{
 const f=fixture(t);const row=f.service.raise(owner,'one',bare());const enriched=f.service.enrich(owner,row.id,1,complete());
 const replay=f.service.enrich(owner,row.id,1,complete());assert.equal(replay.revision,enriched.revision);assert.equal(replay.preparation_rounds,1);
 assert.throws(()=>f.service.enrich(owner,row.id,2,{...complete(),impact:'Changed after publication'}),{code:'packet_frozen'});
});
test('chronological cursor does not skip requests when ids sort differently from creation time',t=>{
 const f=fixture(t,{max_active_per_client:1000});const all=[];
 for(let i=0;i<220;i++){all.push(f.service.raise(owner,'page-'+i,bare()));if(i%3===0)f.clock.advance(1)}
 const expected=[...all].sort((a,b)=>b.created_at.localeCompare(a.created_at)||b.id.localeCompare(a.id)).map(r=>r.id);
 let cursor;const got=[];for(let round=0;round<5;round++){const rows=f.service.list(owner,cursor);const page=rows.slice(0,100);got.push(...page.map(r=>r.id));if(rows.length<=100)break;cursor=f.service.cursor(page.at(-1))}
 assert.deepEqual(got,expected);assert.equal(new Set(got).size,220);assert.throws(()=>f.service.list(owner,'not-a-cursor'),{code:'invalid_cursor'});
});
test('board totals include native work and do not depend on the first 50 cards',t=>{
 const f=native(t);f.config.attention.max_active_per_client=1000;
 for(let i=0;i<75;i++)f.service.raise(owner,'count-'+i,complete());assert.equal(decisionCounts(f.store,'test').needs_you,76);
});
test('reconciliation rejects a stale revision when timestamps are identical',async t=>{
 const f=native(t);const a=recordAnswer(f.store,{escalationId:f.escalation,actor:'human:test',payload:{text:'Keep'}});
 const sent=await deliverReply(f.store,{deliver:async()=>{throw new Error('unknown')}},a.id);
 const retry=reconcileReply(f.store,{id:a.id,expectedRevision:sent.revision,actor:'human:test',outcome:'not_received_retry',note:'Checked source; absent'});
 assert.equal(retry.updated_at,sent.updated_at);assert.ok(retry.revision>sent.revision);
 assert.throws(()=>reconcileReply(f.store,{id:a.id,expectedRevision:sent.revision,actor:'human:test',outcome:'cancel',note:'Stale page'}),{code:'reply_changed'});
});
test('core rejects ambiguous or empty human answer shapes',t=>{
 const f=fixture(t);const r=f.service.raise(owner,'one',complete());
 assert.throws(()=>f.service.answer(r.id,1,'human:test',{}),{code:'invalid_answer'});
 assert.throws(()=>f.service.answer(r.id,1,'human:test',{text:'Remove',option_id:'keep'}),{code:'invalid_answer'});
});
test('database constraints allow only one answer per guided request and positive revisions',t=>{
 const f=fixture(t),r=f.service.raise(owner,'one',complete()),a=f.service.answer(r.id,1,'human:test',{text:'Keep'});
 assert.throws(()=>f.store.db.query('UPDATE attention_requests SET revision=0 WHERE id=?').run(r.id));
 assert.throws(()=>f.store.db.query('UPDATE human_replies SET revision=0 WHERE id=?').run(a.id));
 assert.throws(()=>f.store.db.query("INSERT INTO human_replies(id,request_id,payload_json,actor,state,created_at,updated_at) VALUES ('duplicate',?, '{}','human:test','staged',?,?)").run(r.id,f.clock.now().toISOString(),f.clock.now().toISOString()));
 assert.equal(count(f.store,'human_replies'),1);
});
test('recognized clean-bootstrap database reopens without reinitializing evidence',t=>{
 const folder=mkdtempSync(join(tmpdir(),'car-reopen-'));t.after(()=>rmSync(folder,{recursive:true,force:true}));const file=join(folder,'car.db');
 const db=openDb(file);db.query("INSERT INTO kv(key,value_json,updated_at) VALUES ('retained','\"yes\"','2026-09-04T00:00:00.000Z')").run();db.close();const reopened=openDb(file);assert.equal(JSON.parse(reopened.query("SELECT value_json FROM kv WHERE key='retained'").get().value_json),'yes');reopened.close();
});
test('unversioned nonempty database is not mistaken for a fresh installation',t=>{
 const folder=mkdtempSync(join(tmpdir(),'car-unknown-'));t.after(()=>rmSync(folder,{recursive:true,force:true}));const file=join(folder,'car.db');
 const db=new Database(file);db.exec('CREATE TABLE unrelated(value TEXT)');db.close();assert.throws(()=>openDb(file),{name:'SchemaMismatchError'});
});

const {terminalOutcome,createEffectExecutor,effectAdapter} = await load('effects/index');
const {createSafetyKernel} = await load('safety/index');
test('native effect outcomes use a closed vocabulary and reject contradictory success',()=>{
 assert.equal(terminalOutcome({ok:false,outcome:'unknown'}),'uncertain');
 assert.equal(terminalOutcome({ok:false,outcome:'ok'}),'uncertain');
 assert.equal(terminalOutcome({ok:false,outcome:'uncertain'}),'uncertain');
 assert.equal(terminalOutcome({ok:true}),'ok');
 assert.equal(terminalOutcome({ok:false}),'failed');
});
test('invalid post-execution outcome is durably uncertain and cannot execute twice',async()=>{
 const kernel=createSafetyKernel();let calls=0;
 const proposal={intent_id:'bad-outcome',type:'notify',args:{text:'hello'},scope:{source_id:'human'},lineage:{source_id:'human',request_id:'r'}};
 const grant=kernel.createGrant({intent_id:'bad-outcome-grant',lineage:proposal.lineage,scope:proposal.scope,effect_type:proposal.type,constraints:{args:proposal.args}});
 const executor=createEffectExecutor({kernel,adapters:[effectAdapter('notify',()=>{calls++;return {ok:false,outcome:'unknown'}})]});
 const result=await executor.execute(proposal,grant.id);assert.equal(result.outcome,'uncertain');assert.equal(result.ok,false);
 const replay=await executor.execute(proposal,grant.id);assert.equal(replay.outcome,'uncertain');assert.equal(calls,1);
});
