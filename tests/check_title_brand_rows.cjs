const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const html=fs.readFileSync('web/index.html','utf8');
const source=html.slice(html.indexOf('async function applyTitleBrand(action)'),html.indexOf('// ---------- proxy:'));
const menu={open:true};
const ctx={tab:'bestbuy',selected:new Set(['r1']),edits:{r1:'Edited',r2:'Keep'},lastSig:'old',
  $:()=>menu,syncSelection(){},okResults:()=>[{id:'r1'},{id:'r2'}],poll:async()=>{},
  uiConfirm:async message=>{ctx.message=message;return ctx.confirm;},confirm:true,
  alert:message=>{ctx.alerted=message;},api:async(path,body)=>{ctx.sent={path,body};return {ids:['r1'],changed:1,over:1,skipped:0};}};
vm.createContext(ctx);vm.runInContext(source,ctx);
(async()=>{
  await ctx.applyTitleBrand('add');
  assert.equal(ctx.sent.path,'/api/title-brand');
  assert.deepEqual(Array.from(ctx.sent.body.ids),['r1']);
  assert.equal(ctx.sent.body.titles.r1,'Edited');
  assert.equal(ctx.edits.r1,undefined);assert.equal(ctx.edits.r2,'Keep');
  assert.match(ctx.alerted,/1 tiêu đề vượt 80/);assert.equal(menu.open,false);
  ctx.selected.clear();await ctx.applyTitleBrand('remove');
  assert.deepEqual(Array.from(ctx.sent.body.ids),['r1','r2']);
  assert.equal(ctx.sent.body.action,'remove');assert.match(ctx.message,/2 dòng trong danh sách/);
  ctx.confirm=false;ctx.sent=null;await ctx.applyTitleBrand('add');assert.equal(ctx.sent,null);
  assert.match(html,/fix.hidden = !over/);assert.match(html,/input.classList.toggle\("over", over\)/);
  console.log('PASS: selected/all scope, edits, overlength alert, cancel, existing red/AI warning');
})().catch(e=>{console.error(e);process.exitCode=1;});
