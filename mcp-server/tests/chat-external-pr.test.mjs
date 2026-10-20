import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

globalThis.escape = value => String(value || "").replace(/[&<>]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;"})[c]);
globalThis.emptyState = ({title,sub}) => `<div>${escape(title)} ${escape(sub)}</div>`;
const source = readFileSync(new URL("../admin_static/static/chat-external-pr.js",import.meta.url),"utf8")
  .replace(/^import .*$/gm,"");
const {externalStateLabel,externalPanelHtml,externalEvidencePayload,openExternalRegistration} = await import("data:text/javascript,"+encodeURIComponent(source));
assert.equal(externalStateLabel("ready"),"Apto en Relay");
assert.equal(externalStateLabel("missing_evidence"),"Falta evidencia");
assert.equal(externalStateLabel("review_pending"),"Revisión pendiente");

const task = {
  mode:"external_pr",title:"Objetivo distinto del título GitHub",risk:"medium",actor_label:"<script>bad</script>",issue_number:7,
  responsible:{relay_actor:"person@example.test",github_login:"person",github_id:12},
  external_pr:{repo:"team/repo",number:1,url:"https://github.com/team/repo/pull/1",
    head_sha:"new-sha",base_sha:"base-sha",state:"OPEN",draft:false,observed_at:"2026-10-20T17:00:00Z",
    author:{id:11,login:"author",type:"User"},
    checks:[{name:"tests",head_sha:"new-sha",conclusion:"success",html_url:"https://github.com/team/repo/actions/runs/1"}],
    reviews:[{id:1,user:{id:11,login:"author",type:"User"},state:"APPROVED",commit_id:"old-sha"},
      {id:2,user:{id:13,login:"bot",type:"Bot"},state:"APPROVED",commit_id:"new-sha"}]},
  assessment:{state:"review_pending",reasons:["Falta aprobación independiente"],policy:{required_checks:["tests"],require_declaration:true}},
  evidence:[{head_sha:"old-sha",summary:"prueba anterior",actor:{github_login:"person"},at:"yesterday"},
    {head_sha:"new-sha",summary:"prueba actual",actor:{github_login:"person"},at:"today"}],
  history:[{kind:"github_snapshot",at:"yesterday",snapshot:{head_sha:"old-sha",base_sha:"base-sha",reviews:[{user:{login:"old-reviewer"},state:"APPROVED",commit_id:"old-sha"}],checks:[]}}],
  allowed_actions:["external_refresh","external_evidence"],
};
let html = externalPanelHtml(task);
assert.match(html,/Revisión pendiente/);
assert.match(html,/Tarea: Objetivo distinto del título GitHub/);
assert.match(html,/Actor Relay: person@example.test/);
assert.match(html,/Autor GitHub verificado: author/);
assert.match(html,/&lt;script&gt;bad&lt;\/script&gt;/);
assert.doesNotMatch(html,/<script>/);
assert.match(html,/Histórica · otro SHA/);
assert.match(html,/Declarada · SHA actual/);
assert.match(html,/Relay no ejecutó estas pruebas/);
assert.match(html,/Checks verificados en GitHub/);
assert.match(html,/no equivale a aprobación humana/);
assert.match(html,/Autor del PR/);
assert.match(html,/Bot \/ identidad no humana/);
assert.match(html,/old-reviewer/);
assert.match(html,/data-external-action="refresh"/);
assert.match(html,/data-external-action="evidence"/);
assert.doesNotMatch(html,/data-task-action=|data-task-track|Publicar PR|worktree/);

html = externalPanelHtml({...task,allowed_actions:[],assessment:{state:"missing_evidence",policy:null},
  verification_error:"No se pudo verificar GitHub",external_pr:{...task.external_pr,url:"javascript:alert(1)",checks:[],reviews:[]}});
assert.doesNotMatch(html,/data-external-action=/);
assert.doesNotMatch(html,/javascript:/);
assert.match(html,/No hay política de evidencia configurada/);
assert.match(html,/No se pudo verificar GitHub/);
assert.match(html,/Sin revisiones verificadas/);
for (const url of ["https://github.com.evil.test/a","https://user@github.com/a","http://github.com/a","https://github.com:444/a"]) {
  assert.doesNotMatch(externalPanelHtml({...task,external_pr:{...task.external_pr,url,checks:[]}}),/href=/);
}

const payload = externalEvidencePayload("conv","new-sha","test result");
assert.deepEqual(externalEvidencePayload("conv","new-sha","test result"),payload);
assert.notEqual(externalEvidencePayload("conv","old-sha","test result").request_id,payload.request_id);
assert.notEqual(externalEvidencePayload("conv","new-sha","different result").request_id,payload.request_id);
assert.equal(payload.head_sha,"new-sha");
assert.equal(payload.action,"evidence");
assert.equal(payload.summary,"test result");
console.log("ok - external PR attribution, exact-SHA evidence/history, GitHub facts and idempotent retry payloads");

const catalogCalls = [];
globalThis.api = async path => {
  catalogCalls.push(path);
  assert.equal(path, 'projects?capability=external_pr');
  return {projects:[{slug:'eligible',name:'Eligible'}]};
};
globalThis.field = ({control}) => control;
globalThis.formSection = ({body}) => body;
globalThis.issuePickerHtml = () => '';
globalThis.modalLoad = async ({loader,render}) => {
  const html = render(await loader());
  assert.match(html, /value="eligible"/);
};
await openExternalRegistration({projectSlug:'eligible'});
assert.deepEqual(catalogCalls, ['projects?capability=external_pr']);
console.log('ok - registration uses the server eligibility catalog without rebuilding grants');

const chatSource = readFileSync(new URL("../admin_static/static/tab-chats.js",import.meta.url),"utf8").replace(/^import .*$/gm,"");
const {conversationTaskMode,externalConversationTitle} = await import("data:text/javascript,"+encodeURIComponent(chatSource));
assert.equal(conversationTaskMode({task_json:{mode:"external_pr"}}),"external_pr");
assert.equal(conversationTaskMode({task_json:'{"mode":"external_pr"}'}),"external_pr");
assert.equal(conversationTaskMode({read_only:true}),"read_only");
assert.equal(externalConversationTitle({task_json:{mode:"external_pr",title:"Objetivo externo"}}),"Objetivo externo");
assert.equal(externalConversationTitle({task_json:'{"mode":"external_pr","title":"Objetivo JSON"}'}),"Objetivo JSON");
assert.equal(externalConversationTitle({task_json:{mode:"write",title:"Cambio"}}),"");
console.log("ok - external conversation mode survives object and JSON metadata");

