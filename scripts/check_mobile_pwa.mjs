// Headless browsers cannot open an OS-installed app window. Simulate its
// display-mode signal, then exercise the actual viewport CSS and navigation.
import {chromium, webkit, devices} from "playwright";
import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import {checkMobileRefresh} from "./check_mobile_refresh.mjs";
const base=process.env.HOMESTEAD_URL || "http://127.0.0.1:4173";
await mkdir("release-assets/pages/mobile-pwa",{recursive:true});
const safari=process.env.HOMESTEAD_TEST_BROWSER === "webkit";
const browser=await (safari?webkit:chromium).launch({headless:true});
try {
  for(const installed of [true,false]) {
    const context=await browser.newContext({...devices[safari?"iPhone 13":"Pixel 7"],viewport:{width:412,height:839}});
    await context.addInitScript(({installed,safari})=>{
      window.HOMESTEAD_DEMO=true;
      if(safari) Object.defineProperty(navigator,"standalone",{value:installed});
      const original=window.matchMedia.bind(window);
      window.matchMedia=query=>{
        const result=original(query);
        if(query==="(display-mode: standalone)") Object.defineProperty(result,"matches",{value:installed&&!safari});
        return result;
      };
    },{installed,safari});
    const page=await context.newPage(), errors=[];
    page.on("pageerror",error=>errors.push(error.message));
    await page.goto(`${base}/?demo=1`,{waitUntil:"networkidle"});
    await page.locator("#views .phead").waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.dataset.display),installed?"standalone":"browser");
    assert.equal(await page.locator("#demoBanner").evaluate(el=>el.parentElement===document.body),true,"demo banner stays above the whole site");
    assert.equal(await page.evaluate(()=>document.querySelector("#demoBanner").getBoundingClientRect().bottom<=document.querySelector("#app").getBoundingClientRect().top),true,"demo banner sits above both navigation and content");
    await checkMobileRefresh(page,context,installed);
    if(!installed) {
      assert.equal(await page.evaluate(()=>getComputedStyle(document.body).overflowY),"visible");
      assert.equal(await page.evaluate(()=>getComputedStyle(document.querySelector(".main")).overflowY),"visible");
      await page.evaluate(()=>scrollTo(0,200));
      assert.ok(await page.evaluate(()=>scrollY)>0,"browser document can still scroll");
      await context.close();continue;
    }
    for(const viewport of [{width:412,height:839},{width:360,height:640},{width:844,height:390}]) {
      await page.setViewportSize(viewport);
      for(const view of ["dash","vms","workloads","portal","settings","setup","nodes","storage","network"]) {
        await page.evaluate(view=>go(view),view);
        await page.locator("#views .phead").waitFor();
        await page.waitForTimeout(350);
        const metrics=await page.evaluate(()=>{
          const main=document.querySelector(".main");
          scrollTo(100,10000);
          main.scrollTo(100,100000);
          return {x:scrollX,y:scrollY,rootHeight:document.documentElement.scrollHeight,
            height:innerHeight,rootWidth:document.documentElement.scrollWidth,width:innerWidth,
            paneHeight:main.clientHeight,paneWidth:main.clientWidth,scrollWidth:main.scrollWidth,
            left:main.scrollLeft,top:main.scrollTop,paneScrollHeight:main.scrollHeight,
            overscroll:getComputedStyle(main).overscrollBehavior,
            overscrollSupported:CSS.supports("overscroll-behavior","none"),
            bannerHeight:document.querySelector("#demoBanner").getBoundingClientRect().height,
            headerTop:document.querySelector(".top").getBoundingClientRect().top,
            navBottom:document.querySelector("#bottombar").getBoundingClientRect().bottom};
        });
        assert.equal(metrics.x,0);assert.equal(metrics.y,0);
        assert.equal(metrics.rootWidth,metrics.width);assert.equal(metrics.rootHeight,metrics.height);
        assert.ok(Math.abs(metrics.paneHeight+metrics.bannerHeight-metrics.height)<1,"app fills the space below the demo banner");
        assert.equal(metrics.scrollWidth,metrics.paneWidth);assert.equal(metrics.left,0);
        // Windows WebKit omits native overscroll behavior. Safari has
        // supported it since iOS 16; the viewport bounds are checked here
        // on every engine, and Chromium still checks the CSS value too.
        if(metrics.overscrollSupported) assert.equal(metrics.overscroll,"none");
        else assert.ok(safari && process.platform==="win32","overscroll CSS is required outside Windows WebKit");
        assert.ok(Math.abs(metrics.headerTop-metrics.bannerHeight)<1,`header stays below the site banner when content scrolls: ${view} ${JSON.stringify(metrics)}`);
        assert.ok(Math.abs(metrics.navBottom-metrics.height)<1,"bottom navigation stays in the viewport");
        if(view==="dash") assert.ok(metrics.top>0,"long content remains scrollable");
        await page.evaluate(()=>scrollPageTop());
        assert.equal(await page.locator(".main").evaluate(el=>el.scrollTop),0);
        await page.screenshot({path:`release-assets/pages/mobile-pwa/${view}-${viewport.width}.png`});
      }
      // An open VM dialog has its own scrolling and remains reachable.
      await page.evaluate(()=>vmOpen("lab","ubuntu-test"));
      await page.locator("#mbody .vm-facts").waitFor();
      assert.equal(await page.locator(".modalbox").evaluate(el=>el.scrollWidth<=el.clientWidth),true);
      await page.evaluate(()=>closeModal());
      // Nested horizontal controls must keep their own scrolling.
      const nested=await page.evaluate(()=>{
        const el=document.createElement("div");el.className="tblwrap";el.style.width="120px";
        const child=document.createElement("div");child.style.width="400px";child.textContent="Wide table";
        el.append(child);document.querySelector("#views").append(el);el.scrollLeft=60;
        const value=el.scrollLeft;el.remove();return value;
      });
      assert.equal(nested,60);
      // Sign-in/setup stays reachable when the phone is held sideways.
      await page.evaluate(()=>loginForm(null,true));
      await page.locator("#lg_go").waitFor();
      await page.locator("#lg_go").scrollIntoViewIfNeeded();
      const login=await page.locator("#lg_go").boundingBox();
      assert.ok(login.y>=0 && login.y+login.height<=viewport.height);
      assert.equal(await page.locator("#gate").evaluate(el=>el.scrollWidth<=el.clientWidth),true);
      await page.evaluate(()=>ungate());
      console.log(`Installed mobile layout passed at ${viewport.width}×${viewport.height}`);
    }
    // Short pages do not create a phantom scroll below the app chrome.
    await page.setViewportSize({width:412,height:839});
    await page.evaluate(()=>{document.querySelector("#views").innerHTML='<div class="phead"><h2>Short page</h2></div>';scrollPageTop();});
    assert.equal(await page.locator(".main").evaluate(el=>el.scrollHeight===el.clientHeight),true);
    await page.evaluate(()=>go("dash"));
    await page.locator("#views .phead").waitFor();
    await page.locator(".main").evaluate(el=>el.scrollTop=300);
    await page.evaluate(()=>go("vms"));
    await page.locator("#views h2").filter({hasText:/Virtual machines/}).waitFor();
    assert.equal(await page.locator(".main").evaluate(el=>el.scrollTop),0,"changing pages resets the content pane");
    // The installed product has no demo banner and still fills its viewport.
    await page.locator("#demoBanner").evaluate(el=>el.remove());
    assert.equal(await page.locator(".main").evaluate(el=>el.clientHeight),839);
    assert.equal(await page.locator(".top").evaluate(el=>el.getBoundingClientRect().top),0);
    assert.deepEqual(errors,[]);
    await context.close();
  }
} finally {await browser.close()}
