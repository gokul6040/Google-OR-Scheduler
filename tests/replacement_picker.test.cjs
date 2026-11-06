// Run: node --test tests/replacement_picker.test.cjs
// Execute the production controller against deferred requests and a minimal DOM.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync('web/app.js','utf8');
const controller = source.slice(source.indexOf('function showReplacementCandidates('),source.indexOf('\ninitialize();'));
const tick = () => new Promise(resolve=>setImmediate(resolve));
const opening = id=>({appointmentId:id,date:'2026-10-28',time:'13:55',therapy:'Therapy',branch:'Test'});
const candidate = {patient_id:7,patient_name:'Test patient',patient_code:'TEST'};

function setup() {
  const nodes = new Map();
  class Element {
    constructor() {
      this.dataset={}; this.listeners={}; this.checked=true; this.disabled=false; this.value='';
      const classes=new Set();
      this.classList={contains:c=>classes.has(c),add:c=>classes.add(c),toggle:(c,on)=>on?classes.add(c):classes.delete(c)};
    }
    set innerHTML(value) {
      this.html=value;
      if(this===drawer) {
        nodes.clear();
        for(const [,id] of value.matchAll(/id="([^"]+)"/g)) nodes.set('#'+id,new Element());
        nodes.set('[data-close-appointment]',new Element());
      }
      this.patients=[...value.matchAll(/data-replacement-patient="(\d+)"/g)].map(([,id])=>{
        const button=new Element(); button.dataset.replacementPatient=id; return button;
      });
      for(const key of ['data-retry-patients','data-retry-opening']) {
        if(value.includes(key)) nodes.set('['+key+']',new Element());
      }
    }
    get innerHTML() { return this.html||''; }
    addEventListener(name,fn) { this.listeners[name]=fn; }
    setAttribute(name,value) { this[name]=value; }
    scrollIntoView() {}
    click() { return this.listeners.click?.({target:this}); }
  }
  const drawer=new Element(), requests=[];
  let uid=0;
  const sandbox={
    console,setTimeout,clearTimeout,newRequestId:()=>`request-${++uid}`,
    ensureAppointmentDrawer:()=>drawer,workflowWorkspace:false,
    openDrawer:()=>drawer.classList.add('open'),closeDrawers:()=>{},
    escapeHtml:String,initials:()=>'',icon:()=>'',parseLocalDate:v=>v,formatDate:v=>v,
    errorState:message=>`Error: ${message}`,state:{schedule:{therapists:[{id:2,departmentId:3,locationId:4}],departments:[],locations:[],patients:[]}},
    $:selector=>nodes.get(selector),
    $$:(selector,root)=>selector==='[data-replacement-patient]'?(root.patients||[]):[],
    api:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
  };
  vm.createContext(sandbox); vm.runInContext(controller,sandbox);
  return {open:sandbox.showReplacementCandidates,requests,get:s=>nodes.get(s)};
}

test('suggestions first; context is reused; double-click starts one preview',async()=>{
  const ui=setup(); ui.open([opening(1)]);
  assert.match(ui.get('#replacementPatientList').innerHTML,/Loading suggestions/);
  ui.requests[0].resolve({...opening(1),visitSessions:1,candidates:[candidate]}); await tick();
  assert.equal(ui.requests.length,1);
  assert.equal(ui.get('#replacementSuggested').classList.contains('active'),true);
  const button=ui.get('#replacementPatientList').patients[0];
  button.click(); button.click();
  assert.equal(ui.requests.length,2);
  assert.equal(JSON.parse(ui.requests[1].options.body).patientId,7);
  ui.requests[1].resolve({slots:[{time:'13:55'}],warnings:[],preview_token:'token'}); await tick();
  assert.equal(ui.get('#replacementConfirm').disabled,false);
  assert.equal(button['aria-pressed'],'true');
});

test('empty suggestions still allow searching and previewing any patient',async()=>{
  const ui=setup(); ui.open([opening(1)]);
  ui.requests[0].resolve({...opening(1),visitSessions:1,candidates:[]}); await tick();
  assert.match(ui.get('#replacementPatientList').innerHTML,/No matching suggestions/);
  ui.get('#replacementAll').click();
  ui.requests[1].resolve({patients:[{id:7,name:'Test patient',code:'TEST',active:true}]}); await tick();
  ui.get('#replacementPatientList').patients[0].click();
  assert.match(ui.requests[2].url,/1\/reservation-preview$/);
});

test('selection only previews; explicit repeated confirmation submits one reservation',async()=>{
  const ui=setup(); ui.open([opening(1)]);
  ui.requests[0].resolve({...opening(1),visitSessions:1,candidates:[candidate]}); await tick();
  ui.get('#replacementPatientList').patients[0].click();
  ui.requests[1].resolve({slots:[{time:'13:55'}],warnings:[],whole_visit:true,preview_token:'token'}); await tick();
  assert.equal(ui.requests.length,2,'selecting a patient must not save');
  ui.get('#replacementConfirm').click(); ui.get('#replacementConfirm').click();
  assert.equal(ui.requests.length,3);
  assert.match(ui.requests[2].url,/1\/reserve$/);
  const body=JSON.parse(ui.requests[2].options.body);
  assert.equal(body.patientId,7); assert.equal(body.previewToken,'token');
  assert.equal(body.mode,'without_messaging');
});

test('older context cannot overwrite the newly selected opening',async()=>{
  const ui=setup(); ui.open([opening(1),opening(2)]);
  ui.get('#replacementOpening').listeners.change({target:{value:'1'}});
  ui.requests[1].resolve({...opening(2),visitSessions:1,candidates:[candidate]}); await tick();
  ui.requests[0].resolve({...opening(1),visitSessions:3,candidates:[]}); await tick();
  ui.get('#replacementPatientList').patients[0].click();
  assert.match(ui.requests[2].url,/2\/reservation-preview$/);
});

test('retry recovers context; an old list failure cannot erase new results',async()=>{
  const ui=setup(); ui.open([opening(1)]);
  ui.requests[0].reject(new Error('offline')); await tick();
  ui.get('[data-retry-opening]').click();
  ui.requests[1].resolve({...opening(1),visitSessions:1,candidates:[candidate]}); await tick();
  ui.get('#replacementAll').click(); ui.get('#replacementSuggested').click();
  ui.requests[3].resolve({candidates:[candidate]}); await tick();
  ui.requests[2].reject(new Error('old failed search')); await tick();
  assert.equal(ui.get('#replacementPatientList').patients.length,1);
  assert.doesNotMatch(ui.get('#replacementPatientList').innerHTML,/old failed search/);
});

test('identifiers work when randomUUID is unavailable',()=>{
  const helper=source.slice(source.indexOf('function newRequestId()'),source.indexOf('\nfunction assistantMemorySessionId()'));
  const ctx={Uint8Array,crypto:{getRandomValues:b=>require('node:crypto').randomFillSync(b)}};
  vm.createContext(ctx); vm.runInContext(helper,ctx);
  assert.match(ctx.newRequestId(),/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.notEqual(ctx.newRequestId(),ctx.newRequestId());
});

test('a stale patient preview cannot enable confirmation or replace the current review',async()=>{
  const ui=setup(); ui.open([opening(1)]);
  ui.requests[0].resolve({...opening(1),visitSessions:1,candidates:[candidate,{...candidate,patient_id:8,patient_name:'Second patient'}]}); await tick();
  const buttons=ui.get('#replacementPatientList').patients;
  buttons[0].click(); buttons[1].click();
  ui.requests[1].resolve({slots:[{time:'13:55'}],warnings:[],preview_token:'old'}); await tick();
  assert.equal(ui.get('#replacementConfirm').disabled,true);
  assert.match(ui.get('#replacementReview').innerHTML,/Second patient/);
  ui.requests[2].resolve({slots:[{time:'13:55'}],warnings:[],preview_token:'new'}); await tick();
  assert.equal(ui.get('#replacementConfirm').disabled,false);
  assert.match(ui.get('#replacementReview').innerHTML,/Second patient/);
});

test('empty-slot patient can be previewed after supplying a missing doctor',async()=>{
  const ui=setup();
  ui.open([{...opening(1),emptySlot:{date:'2026-10-28',slot:6,therapistId:null,departmentId:3,locationId:4}}]);
  ui.requests[0].resolve({patients:[{id:7,name:'Test patient',code:'TEST',active:true}]}); await tick();
  ui.get('#replacementPatientList').patients[0].click();
  assert.match(ui.get('#replacementReview').innerHTML,/Choose a doctor/);
  assert.equal(ui.requests.length,1);
  ui.get('#emptySlotDoctor').value='2';
  ui.get('#emptySlotDoctor').listeners.change();
  assert.equal(ui.requests.length,2);
  assert.equal(ui.requests[1].url,'/api/calendar/reservation-preview');
  assert.equal(JSON.parse(ui.requests[1].options.body).therapistId,2);
  ui.get('#emptySlotTherapy').value='9';
  ui.get('#emptySlotTherapy').listeners.change();
  assert.equal(ui.requests.length,3,'changing slot details supersedes the pending preview');
  assert.equal(JSON.parse(ui.requests[2].options.body).departmentId,9);
  ui.requests[1].resolve({slots:[{time:'13:55'}],warnings:[],preview_token:'old'}); await tick();
  assert.equal(ui.get('#replacementConfirm').disabled,true);
});
