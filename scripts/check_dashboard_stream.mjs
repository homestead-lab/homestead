// Verify real dashboard refreshes preserve charts, scroll and focus while
// observed samples and resource bars settle into their next values.
import assert from "node:assert/strict";
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
const output="release-assets/pages/dashboard-stream";
await mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true});
try {
  for(const width of [1440,375]) for(const theme of ["dark","light"]) {
    const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),errors=[];
    page.on("pageerror",e=>errors.push(e.message));
    await page.goto(`${base}/?demo=1`,{waitUntil:"networkidle"});
    await page.locator("#historyCard .spark").first().waitFor();
    await page.evaluate(async theme=>{
      clearInterval(window.__loopTimer);SET.theme=theme;applySettings();
      window.streamSample=0;window.historySize=48;
      const original=window.fetch;
      window.fetch=async(...args)=>{
        const url=new URL(typeof args[0]==="string"?args[0]:args[0].url,location.origin);
        if(url.pathname==="/api/history/long" && window.stallHistory) {
          return new Promise((_resolve,reject)=>args[1]?.signal?.addEventListener("abort",
            ()=>reject(new DOMException("Aborted","AbortError")),{once:true}));
        }
        const response=await original(...args);
        if(!["/api/history","/api/history/long","/api/overview"].includes(url.pathname))return response;
        const data=await response.json(),offset=window.streamSample;
        if(url.pathname==="/api/overview") {
          data.cpu_pct=offset?70:20;data.nodes[0].cpu_pct=offset?70:20;
        } else {
          const long=url.pathname.endsWith("/long"),count=long?window.historySize:120;
          const times=Array.from({length:count},(_,i)=>(1000+i+offset)*(long?300:30));
          data.t=times;
          for(const key of long?["cpu","mem","rx","tx","pods"]:["cpu","mem","net_rx","net_tx"])
            data[key]=times.map((_,i)=>+(30+10*Math.sin((i+offset)/4)+(key==="mem"?15:0)).toFixed(2));
          if(long){data.samples=count;data.since=times[0];data.vol_bad=times.map(()=>0);}
        }
        return new Response(JSON.stringify(data),{status:200,headers:{"Content-Type":"application/json"}});
      };
      await viewDash();
    },theme);
    await page.waitForTimeout(750);
    await page.locator("#historyCard .seg button").first().focus();
    await page.evaluate(()=>{
      window.scrollTo(0,300);document.querySelector(".main").scrollTop=300;
      window.streamRefs={charts:[...document.querySelectorAll(".spark")],path:document.querySelector(".dashcard .ln"),
        bar:document.querySelector(".node-comparison .meter:not(.split) span"),focus:document.activeElement,
        scroll:[scrollY,document.querySelector(".main").scrollTop]};
      streamRefs.before=streamRefs.path.getAttribute("d");streamRefs.barBefore=streamRefs.bar.getBoundingClientRect().width;
      window.streamSample=1;
    });
    await page.evaluate(()=>viewDash());await page.waitForTimeout(150);
    const mid=await page.evaluate(()=>({retained:streamRefs.charts.every((el,i)=>el===document.querySelectorAll(".spark")[i]),
      focus:document.activeElement===streamRefs.focus,scroll:[scrollY,document.querySelector(".main").scrollTop],
      scrolling:sparkAnimations.has(streamRefs.path),firstX:sparkPoints(streamRefs.path.getAttribute("d"))[0][0],
      changed:streamRefs.before!==streamRefs.path.getAttribute("d"),barWidth:streamRefs.bar.getBoundingClientRect().width,
      barBefore:streamRefs.barBefore,overflow:document.documentElement.scrollWidth>innerWidth}));
    assert.equal(mid.retained,true);assert.equal(mid.focus,true);assert.equal(mid.scrolling,true);assert.equal(mid.changed,true);
    assert.ok(mid.firstX<0,"an outgoing sample moves left");assert.ok(mid.barWidth>mid.barBefore,"resource bars transition upward");
    assert.deepEqual(mid.scroll,await page.evaluate(()=>streamRefs.scroll));assert.equal(mid.overflow,false);
    const intermediate=await page.evaluate(()=>streamRefs.path.getAttribute("d"));
    await page.waitForTimeout(650);
    assert.equal(await page.evaluate(()=>sparkAnimations.has(streamRefs.path)),false);
    assert.notEqual(await page.evaluate(()=>streamRefs.path.getAttribute("d")),intermediate);
    const settled=await page.evaluate(()=>streamRefs.path.getAttribute("d"));
    await page.evaluate(()=>viewDash());await page.waitForTimeout(50);
    assert.equal(await page.evaluate(()=>streamRefs.path.getAttribute("d")),settled,"unchanged data does not replay motion");
    assert.equal(await page.evaluate(()=>sparkAnimations.has(streamRefs.path)),false);
    await page.evaluate(()=>{
      window.stallHistory=true;window.streamSample++;
      window.streamSettled=streamRefs.path.getAttribute("d");
      SET.refresh=5;startLoop();
    });
    await page.waitForFunction(()=>streamRefs.path.getAttribute("d")!==window.streamSettled,null,{timeout:9000});
    await page.evaluate(()=>clearInterval(window.__loopTimer));
    await page.waitForTimeout(750);
    assert.equal(await page.evaluate(()=>STATE.busy),false,"saved history must not keep the live poll busy");
    assert.notEqual(await page.evaluate(()=>streamRefs.path.getAttribute("d")),settled,"live charts update while saved history is stalled");
    await page.evaluate(async()=>{window.stallHistory=false;await refresh(true)});
    await page.waitForTimeout(750);
    await page.screenshot({path:`${output}/dashboard-${theme}-${width}.png`,fullPage:true});
    for(const preference of ["off","reduced"]){
      await page.emulateMedia({reducedMotion:preference==="reduced"?"reduce":"no-preference"});
      await page.evaluate(async preference=>{SET.motion=preference==="off"?"off":"on";applySettings();window.streamSample++;await viewDash()},preference);
      assert.equal(await page.evaluate(()=>[...document.querySelectorAll(".spark path")].some(el=>sparkAnimations.has(el))),false);
      const durations=await page.locator(".meter>span").evaluateAll(els=>els.map(el=>getComputedStyle(el).transitionDuration));
      assert.ok(durations.every(duration=>duration==="0s"),"motion preference applies to split storage bars too");
    }
    await page.emulateMedia({reducedMotion:"no-preference"});
    await page.evaluate(async()=>{SET.motion="on";applySettings();window.historySize=2160;await viewDash()});
    await page.waitForTimeout(750);
    await page.evaluate(async()=>{window.streamSample++;await viewDash()});
    await page.waitForTimeout(750);
    assert.equal(await page.evaluate(()=>[...document.querySelectorAll(".spark path")].some(el=>sparkAnimations.has(el))),false);
    assert.deepEqual(errors,[]);console.log(`Dashboard streaming passed: ${theme}, ${width}px, motion on/off/reduced, long history`);
    await context.close();
  }
} finally {await browser.close()}
