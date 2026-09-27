const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function app() {
  const fields = {};
  let posted;
  const ctx = { console, STATE: { data: { smbUsers: [{user:'lab',has_password:true,shares:['secure']}] } },
    esc: x => String(x ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    $: id => fields[id], icon: () => '', tip: () => '', toast: () => {}, closeModal: () => {}, resetPaint: () => {},
    createVolumePicker: () => ({}), renderVolumeRows: () => {}, readVolumeRows: () => [{kind:'existing',source:'data',path:''}],
    api: async (path, options) => { if (options) { posted = JSON.parse(options.body); throw new Error('stop after request'); }
      return path === '/api/shares/users' ? ctx.STATE.data.smbUsers : { samba_installed:true }; },
    modal: () => { fields['#mbody'] = {innerHTML:''}; },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('web/js/ui.js','utf8'),ctx);
  vm.runInContext(fs.readFileSync('web/js/views-storage.js','utf8'),ctx);
  return {ctx,fields,get posted() {return posted;}};
}
test('new share offers saved users and adding a new account without exposing passwords',async () => {
  const {ctx,fields} = app();
  await ctx.newShare();
  assert.match(fields['#mbody'].innerHTML,/lab · existing user/);
  assert.match(fields['#mbody'].innerHTML,/Add a new user/);
  assert.doesNotMatch(fields['#mbody'].innerHTML,/Leave.*blank.*reuse/);
});
test('existing-user selection hides and clears the password field',() => {
  const {ctx,fields} = app(); let hidden;
  Object.assign(fields, {'#sh_identity':{value:'lab'},'#sh_pub':{checked:false},'#sh_pass':{value:'stale-password'},
    '#sh_new_account':{classList:{toggle:(_,value)=>{hidden=value;}}}});
  ctx.shareAccountHint();
  assert.equal(hidden,true); assert.equal(fields['#sh_pass'].disabled,true); assert.equal(fields['#sh_pass'].value,'');
});
test('creating with a selected account sends reuse mode and never sends a stale password',async () => {
  const a=app();
  Object.assign(a.fields, {'#sh_name':{value:'archive'},'#sh_identity':{value:'lab'},'#sh_user':{value:'ignored'},
    '#sh_pub':{checked:false},'#sh_pass':{value:'stale-password'},'#sh_ro':{checked:false},'#sh_storage':{}});
  await a.ctx.mkShare({});
  assert.equal(a.posted.user,'lab'); assert.equal(a.posted.password,''); assert.equal(a.posted.account_mode,'existing');
});
test('user management lists membership and only offers removal for unused users',async () => {
  const {ctx,fields}=app(); ctx.STATE.data.smbUsers.push({user:'backup',has_password:true,shares:[]});
  await ctx.smbUsers();
  assert.match(fields['#mbody'].innerHTML,/>User</); assert.match(fields['#mbody'].innerHTML,/secure/);
  assert.match(fields['#mbody'].innerHTML,/smbUserRemove\('backup'\)/);
  assert.doesNotMatch(fields['#mbody'].innerHTML,/smbUserRemove\('lab'\)/);
});
