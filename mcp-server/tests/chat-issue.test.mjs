import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

globalThis.escape = value => String(value ?? "").replace(/[&<>]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;"})[c]);
globalThis.field = ({label,control,hint=""}) => `<div>${escape(label)}${control}<p>${hint}</p></div>`;
globalThis.formSection = ({title,sub,body,actions}) => `<section>${escape(title)}${sub}${body}${actions}</section>`;
let renderedModal = "";
globalThis.modalLoad = async options => { renderedModal = options.render(await options.loader()); };
const source = readFileSync(new URL("../admin_static/static/chat-issue.js",import.meta.url),"utf8")
  .replace(/^import .*$/gm,"");
const {parseIssueInput,optionalIssuePayload,issueLinkPayload,issueLinkHtml,issueSyncHtml,
  issuePanelHtml,issuePickerHtml,mountIssuePicker,mountIssuePanel,openIssueLink} = await import("data:text/javascript,"+encodeURIComponent(source));

assert.equal(parseIssueInput(" #42 "),42);
assert.equal(parseIssueInput("https://github.com/team/repo/issues/42/"),"https://github.com/team/repo/issues/42");
assert.equal(parseIssueInput(""),null);
assert.deepEqual(optionalIssuePayload(" "),{});
assert.deepEqual(optionalIssuePayload("42"),{issue:42});
assert.deepEqual(issueLinkPayload("42"),{issue:42,resolution_complete:false});
assert.deepEqual(issueLinkPayload("42",true),{issue:42,resolution_complete:true});
assert.equal(issueLinkPayload("42","true").resolution_complete,false);
for (const value of ["0","-1","1.5","2147483648","https://github.com/team/repo/issues/2147483648","9007199254740992","texto libre",
  "https://github.com/team/repo/pull/42","javascript:alert(1)","http://github.com/team/repo/issues/42",
  "https://github.com.evil.test/team/repo/issues/42","https://user@github.com/team/repo/issues/42",
  "https://github.com:444/team/repo/issues/42","https://github.com/team/repo/issues/42?x=1",
  "https://github.com/team/repo/issues/42#secret"]) assert.throws(()=>parseIssueInput(value),/número de issue/);
assert.throws(()=>issueLinkPayload(""),/Selecciona/);

const issue = {repo:"team/repo",number:42,url:"https://github.com/team/repo/issues/42",title:"<script>objetivo</script>",state:"OPEN"};
assert.match(issueLinkHtml(issue),/href="https:\/\/github.com\/team\/repo\/issues\/42"/);
assert.match(issueLinkHtml(issue),/&lt;script&gt;/);
assert.doesNotMatch(issueLinkHtml({...issue,url:"javascript:alert(1)"}),/href=|<script>/);
assert.doesNotMatch(issueLinkHtml({...issue,repo:"other/repo"}),/href=/);
assert.doesNotMatch(issueLinkHtml({...issue,number:43}),/href=/);
assert.match(issuePickerHtml({id:"picker",value:'"><img src=x>'}),/value="&quot;&gt;&lt;img src=x&gt;"/);
assert.match(issuePickerHtml({id:"picker"}),/<datalist/);

let html = issuePanelHtml({issue_link:issue,resolution_complete:false,allowed_actions:["link_issue","issue_sync"]});
assert.match(html,/Trabajo parcial/);
assert.match(html,/Cerrar un PR sin merge no cierra la issue/);
assert.match(html,/data-issue-action="link"/);
assert.match(html,/data-issue-action="refresh"/);
assert.doesNotMatch(issuePanelHtml({issue_link:issue,allowed_actions:[]}),/data-issue-action=/);
html = issueSyncHtml({enabled:false,status:"synced",issue,observed:{issue_state:"OPEN"},events:[]});
assert.match(html,/No configurada/);
assert.match(html,/Estado observado en GitHub: OPEN/);
assert.doesNotMatch(html,/badge ok|>Sincronizada</);
html = issueSyncHtml({enabled:false,status:"error",error:"Conflicto manual",events:[]},{canRetry:true});
assert.match(html,/badge err/);
assert.match(html,/>Error</);
assert.match(html,/Política desactivada/);
assert.match(html,/Revisar y reintentar/);
html = issueSyncHtml({enabled:false,status:"uncertain",error:"Resultado pendiente",events:[]},{canRetry:true});
assert.match(html,/Resultado incierto/);
assert.match(html,/Política desactivada/);
assert.doesNotMatch(html,/data-issue-action="retry"/);
assert.match(issueSyncHtml({enabled:true,status:"pending",events:[]}),/Pendiente/);
html = issueSyncHtml({enabled:true,status:"error",error:"<script>error</script>",observed:{projects:[{project_id:"P1",status:"In review"}]},events:[{kind:"review",status:"failed",error:"missing item"}]});
assert.match(html,/role="alert"/);
assert.match(html,/In review/);
assert.match(html,/missing item/);
assert.doesNotMatch(html,/<script>/);
await openIssueLink({convId:"c",projectSlug:"p",task:{issue_link:issue,resolution_complete:false}});
assert.doesNotMatch(renderedModal,/type="checkbox" checked/);
assert.match(renderedModal,/avances parciales/);
await openIssueLink({convId:"c",projectSlug:"p",task:{issue_link:issue,resolution_complete:true}});
assert.match(renderedModal,/type="checkbox" checked/);

function element(extra={}) {
  const handlers = new Map();
  return {innerHTML:"",textContent:"",disabled:false,value:"",...extra,
    addEventListener:(name,handler)=>handlers.set(name,handler),
    removeEventListener:(name,handler)=>{if(handlers.get(name)===handler)handlers.delete(name);},
    fire:(name,...args)=>handlers.get(name)?.(...args),handlers};
}
const deferred = () => { let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject}; };
const settle = async () => { await Promise.resolve(); await Promise.resolve(); };
const status=element(),retry=element(),list=element(),input=element({value:"42"});
input.parentElement={querySelector:selector=>selector.includes("status")?status:retry};
const root={querySelector:selector=>selector==="#picker"?input:selector==="#picker-options"?list:null};
let requests=[];
globalThis.apiRoot=(url,options)=>{const request=deferred();requests.push({url,options,...request});return request.promise;};
let changed="";
const picker=mountIssuePicker(root,{id:"picker",projectSlug:"first",onChange:value=>{changed=value;}});
assert.equal(requests[0].url,"/projects/first/issues");
picker.setProject("second");
assert.equal(input.value,"");
assert.equal(changed,"");
requests[1].resolve({issues:[issue]}); await settle();
assert.match(list.innerHTML,/team\/repo\/issues\/42/);
requests[0].resolve({issues:[{...issue,title:"stale-project"}]}); await settle();
assert.doesNotMatch(list.innerHTML,/stale-project/);
input.value="https://github.com/team/repo/issues/42";input.fire("input");
assert.equal(changed,input.value);
retry.fire("click");requests[2].reject(new Error("503"));await settle();
assert.match(status.textContent,/503/);
assert.equal(input.value,"https://github.com/team/repo/issues/42");
assert.equal(retry.disabled,false);
retry.fire("click");picker.destroy();requests[3].resolve({issues:[{...issue,title:"after-dispose"}]});await settle();
assert.doesNotMatch(list.innerHTML,/after-dispose/);

requests=[];
const region=element(),refresh=element();
const panelRoot={querySelector:selector=>selector==="[data-issue-sync]"?region:selector.includes('"refresh"')?refresh:null};
const busyStates=[];
const controller=mountIssuePanel(panelRoot,{convId:"c/id",projectSlug:"p",task:{issue_link:issue},onBusyChange:value=>busyStates.push(value)});
assert.equal(requests[0].url,"/conversations/c%2Fid/issue-sync");
assert.equal(requests[0].options,undefined); // Abrir el panel solo lee el estado guardado.
requests[0].resolve({enabled:false,status:"disabled",issue});await settle();
refresh.fire("click");
assert.deepEqual(busyStates,[true]);
refresh.fire("click"); // No se duplica una sincronización en vuelo.
assert.equal(requests.length,2);
assert.deepEqual(requests[1].options,{method:"POST",body:{action:"refresh"}});
requests[1].reject(new Error("GitHub no disponible"));await settle();
assert.match(region.innerHTML,/GitHub no disponible/);
assert.equal(refresh.disabled,false);
assert.deepEqual(busyStates,[true,false]);
refresh.fire("click");controller.destroy();requests[2].resolve({enabled:true,status:"synced"});await settle();
assert.doesNotMatch(region.innerHTML,/>Sincronizada</);

// Revisar y reintentar puede escribir con política activa: cancelar no envía POST.
requests=[];
const retryControl=element();
retryControl.closest=()=>retryControl;
const retryRegion=element({querySelector:()=>retryControl});
const retryRoot={querySelector:selector=>selector==="[data-issue-sync]"?retryRegion:null};
const retryController=mountIssuePanel(retryRoot,{convId:"retry",projectSlug:"p",
  task:{issue_link:issue,allowed_actions:["issue_sync"]}});
requests[0].resolve({enabled:true,status:"error",events:[],error:"Conflicto"});await settle();
const confirmations=[];
globalThis.confirmModal=async options=>{confirmations.push(options);return false;};
retryRegion.fire("click",{target:retryControl});await settle();
assert.equal(requests.length,1);
assert.equal(retryControl.disabled,false);
assert.match(confirmations[0].body,/puede aplicar el mapeo/);
globalThis.confirmModal=async ()=>true;
retryRegion.fire("click",{target:retryControl});
retryRegion.fire("click",{target:retryControl});await settle();
assert.equal(requests.length,2);
assert.deepEqual(requests[1].options,{method:"POST",body:{action:"retry"}});
requests[1].resolve({enabled:true,status:"pending",events:[]});await settle();
retryController.destroy();

// Cambiar una issue exige volver a declarar resolución completa y envía el dato explícito.
const modalInput=element({value:issue.url,focus() {}}),modalList=element(),modalStatus=element(),modalRetry=element();
const completion=element({checked:true}),save=element(),output=element();
modalInput.parentElement={querySelector:selector=>selector.includes("status")?modalStatus:modalRetry};
const form={querySelector:selector=>selector==="#issue-link-input"||selector==='[name="issue"]'?modalInput
  :selector==="#issue-link-input-options"?modalList:selector==='[name="resolution_complete"]'?completion:save};
globalThis.document={getElementById:id=>id==="issue-link-form"?form:output};
globalThis.modalLoad=async options=>{renderedModal=options.render(await options.loader());options.after();};
globalThis.isModalOpen=()=>true;
globalThis.closeModal=()=>{};
globalThis.toast=()=>{};
const savedCalls=[];
globalThis.apiRoot=async (url,options)=>{savedCalls.push({url,options});return options?{mode:"write",issue_link:{...issue,number:43}}:{issues:[]};};
let savedTask;
await openIssueLink({convId:"c",projectSlug:"p",task:{issue_link:issue,resolution_complete:true},onSaved:next=>{savedTask=next;}});
modalInput.value="43";modalInput.fire("input");
assert.equal(completion.checked,false);
completion.checked=true;
await form.onsubmit({preventDefault() {}});
assert.deepEqual(savedCalls[1],{url:"/conversations/c/issue",options:{method:"POST",body:{issue:43,resolution_complete:true}}});
assert.equal(savedTask.issue_link.number,43);
assert.equal(modalInput.disabled,false);
const beforeInvalid=savedCalls.length;
modalInput.value="texto libre";
await form.onsubmit({preventDefault() {}});
assert.equal(savedCalls.length,beforeInvalid);
assert.equal(modalInput.value,"texto libre");
assert.match(output.textContent,/No se pudo guardar/);
// Un refresco conserva el historial desplegado mientras reemplaza su contenido.
requests=[];
globalThis.apiRoot=(url,options)=>{const request=deferred();requests.push({url,options,...request});return request.promise;};
const history=element({open:true,dataset:{issueSection:"history"}});
const historyRegion=element({querySelectorAll:selector=>selector.includes("[open]")
  ? (history.open?[history]:[]) : [history]});
let historyHtml="";
Object.defineProperty(historyRegion,"innerHTML",{
  get:()=>historyHtml,set:value=>{historyHtml=value;history.open=false;},
});
const historyRefresh=element();
const historyController=mountIssuePanel({querySelector:selector=>selector==="[data-issue-sync]"?historyRegion
  :selector.includes('"refresh"')?historyRefresh:null},{convId:"history",task:{issue_link:issue}});
requests[0].resolve({enabled:true,status:"pending",events:[{state:"pending"}]});await settle();
assert.equal(history.open,true);
historyRefresh.fire("click");
requests[1].resolve({enabled:true,status:"synced",events:[{state:"applied"}]});await settle();
assert.equal(history.open,true,"la sincronización no cierra el historial que la persona está leyendo");
historyController.destroy();
console.log("ok - common issue parsing, explicit completion, safe links, sync states, stale loads, retry and open history preservation");

