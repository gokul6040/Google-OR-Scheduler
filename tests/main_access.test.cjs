// Run: node --test tests/main_access.test.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync('main_workflow/workspace.js', 'utf8');

function run(requiresAccessKey) {
  let dialogs=0, shown=0, removed=0;
  const dialog={open:false,querySelector:()=>({}),addEventListener:()=>{},showModal:()=>shown++};
  const document={
    getElementById:()=>({prepend:()=>{}}),
    createElement:tag=>{ if(tag==='dialog') {dialogs++; return dialog;} return {}; },
    body:{append:()=>{}},addEventListener:()=>{},
  };
  const sessionStorage={removeItem:()=>removed++,getItem:()=>null};
  vm.runInNewContext(source,{window:{therapyWorkspace:{requiresAccessKey,dataSource:'test',apiPrefix:'',live:false}},document,sessionStorage});
  return {dialogs,shown,removed};
}

test('main skips the access-key dialog when its policy requires no key',()=>{
  assert.deepEqual(run(false),{dialogs:0,shown:0,removed:1});
});
test('keyed workspace still displays the access-key dialog',()=>{
  assert.deepEqual(run(true),{dialogs:1,shown:1,removed:0});
});
