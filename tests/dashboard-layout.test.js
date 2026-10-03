const test=require("node:test"),assert=require("node:assert/strict");
const dashboard=require("../web/js/dashboard.js");
test("dashboard preserves reading order and rejects duplicate or unknown widgets",()=>{
  assert.deepEqual(dashboard.normalize({version:1,items:[{id:"portal",width:6,height:360},{id:"compute",width:999,height:-1},{id:"portal"},{id:"constructor"},{id:"missing"}]}),[
    {id:"portal",width:6,height:360},{id:"compute",width:4,height:0}]);
});
test("an empty dashboard is intentional; unsupported or malformed versions use safe defaults",()=>{
  assert.deepEqual(dashboard.normalize({version:1,items:[]}),[]);
  for(const value of [null,{}, {version:2,items:[]},{version:1,items:null}])assert.deepEqual(dashboard.normalize(value),dashboard.defaults());
});
test("node widgets accept narrower widths while invalid history sizes use defaults",()=>{
  assert.deepEqual(dashboard.normalize({version:1,items:[{id:"nodes",width:4,height:520},{id:"history",width:4,height:400}]}),[
    {id:"nodes",width:4,height:520},{id:"history",width:12,height:0}]);
});
test("default layouts are fresh independent objects",()=>{
  const changed=dashboard.defaults();changed[0].width=12;changed.reverse();
  assert.equal(dashboard.defaults()[0].id,"compute");assert.equal(dashboard.defaults()[0].width,4);
});

test("new health widgets are optional and retain sizes in saved layouts",()=>{
 const ids=["health","workloads","containers","vms","backups","updates","jobs"];
 assert.equal(dashboard.defaults().length,7);
 assert.deepEqual(dashboard.normalize({version:1,items:ids.map(id=>({id,width:4,height:360}))}).map(w=>w.id),ids);
});

test("node display settings survive normalization with a compact default",()=>{
 for(const width of [4,6,8,12])assert.deepEqual(dashboard.normalize({version:1,items:[{id:"nodes",width,height:0,display:"detailed"}]}),[{id:"nodes",width,height:0,display:"detailed"}]);
 assert.deepEqual(dashboard.normalize({version:1,items:[{id:"nodes",width:6,height:0,display:"unknown"}]}),[{id:"nodes",width:6,height:0}]);
});

test("resource list widgets can save wide short layouts",()=>{
 assert.deepEqual(dashboard.normalize({version:1,items:[{id:'containers',width:12,height:240},{id:'vms',width:4,height:240}]}),[{id:'containers',width:12,height:240},{id:'vms',width:4,height:240}]);
});


test("desktop gaps preserve order without overlaps, including narrower cards",()=>{
 const items=[{id:'compute',width:4},{id:'storage',width:4,column:9},{id:'containers',width:6,column:3,newRow:true},{id:'portal',width:4}];
 assert.deepEqual(dashboard.positions(items),[{row:1,column:1},{row:1,column:9},{row:2,column:3},{row:2,column:9}]);
 assert.deepEqual(dashboard.positions([{width:8},{width:4,column:2}]),[{row:1,column:1},{row:2,column:2}]);
});
test("widget filters distinguish all groups from none and preserve desktop placement",()=>{
 const items=[{id:'containers',width:4,height:240,column:9,newRow:true,groups:[],status:'attention'},
 {id:'portal',width:6,height:360,groups:['Media'],display:'tiles'}];
 assert.deepEqual(dashboard.normalize({version:1,items}),items);
 assert.deepEqual(dashboard.normalize({version:1,items:[{id:'containers',width:12,height:0,column:9,groups:['Media','Media',false],status:'bogus'}]}),[{id:'containers',width:12,height:0,groups:['Media']}]);
});


test("resource filters combine group selection and state without losing unknown metrics",()=>{
 const insights=require('../web/js/insights.js');
 const rows=[{name:'media',group:'Media',desired:1,ready:1},{name:'stopped',desired:0},{name:'broken',group:'Media',desired:1,ready:0}];
 assert.deepEqual(insights.filteredRows('containers',rows,{groups:['Media'],status:'attention'}).map(r=>r.name),['broken']);
 assert.deepEqual(insights.filteredRows('containers',rows,{groups:[]}),[]);
 assert.deepEqual(insights.filteredRows('containers',rows,{groups:['']}).map(r=>r.name),['stopped']);
 assert.equal(insights.filteredRows('containers',rows,{status:'running'})[0].cpu,null);
 assert.deepEqual(insights.filteredRows('vms',[{name:'vm',status:'Starting'}],{status:'attention'}).map(r=>r.name),['vm']);
});
