const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('web/index.html', 'utf8');
const action = html.slice(html.indexOf('async function aiTitles(restore)'), html.indexOf('// ---------- proxy:'));
const label = html.slice(html.indexOf('function updateAiTitleButton()'), html.indexOf('async function applyBulk()'));
const btn = {dataset: {busy: 'false'}};
let rows = [{id:'normal',title:'Normal'}, {id:'long',title:'x'.repeat(81)}, {id:'warn',title:'Short',title_warn:'Check'}];
const ctx = {selected:new Set(),tab:'bestbuy',edits:{},TITLE_MAX:80,okResults:()=>rows,
  syncSelection(){},$:()=>btn, updateExportState(){},poll(){},saveSettings:async()=>{},
  uiConfirm:async msg=>{ctx.message=msg;return ctx.confirm;},alert:msg=>{ctx.message=msg;},
  api:async(path,body)=>{ctx.sent=body;},confirm:true};
vm.createContext(ctx);
vm.runInContext(action + label, ctx);
(async()=>{
  await ctx.aiTitles(false);
  assert.deepEqual(Array.from(ctx.sent.ids), ['long','warn']);
  assert.match(ctx.message, /2 tiêu đề đang cảnh báo/);
  ctx.selected = new Set(['normal','long']);
  ctx.updateAiTitleButton();
  assert.match(btn.textContent, /\(2\)/);
  await ctx.aiTitles(false);
  assert.deepEqual(Array.from(ctx.sent.ids), ['normal','long']);
  assert.match(ctx.message, /2 dòng đã chọn/);
  ctx.confirm=false;ctx.sent=null;
  await ctx.aiTitles(false);
  assert.equal(ctx.sent,null);
  ctx.selected.clear(); rows=[rows[0]];
  await ctx.aiTitles(false);
  assert.equal(ctx.sent,null);
  assert.match(ctx.message,/Không có tiêu đề/);
  ctx.updateAiTitleButton();
  assert.equal(btn.textContent,'✨ Sửa tiêu đề AI');
  btn.dataset.busy='true';ctx.updateAiTitleButton();
  assert.equal(btn.textContent,'✨ AI đang viết…');
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  scripts.forEach(s=>new vm.Script(s[1]));
  console.log('PASS: warning scope, selected scope/count, confirmation/cancel, empty state, busy label, script syntax');
})().catch(e=>{console.error(e);process.exitCode=1;});
