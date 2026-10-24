const { chromium } = require('playwright');
const assert = require('node:assert/strict');

let browser;
(async () => {
  browser = await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL||'chrome',headless:true});
  const page = await browser.newPage({viewport:{width:1180,height:920}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    const call=async(method,endpoint,body={})=>{
      const url='/api/v1/plugins/extensions/astrbot_plugin_self_prompt/'+endpoint
        +(method==='GET'?'?'+new URLSearchParams(body):'');
      const response=await fetch(url,{method,headers:{Authorization:'Bearer test-owner','Content-Type':'application/json'},
        ...(method==='POST'?{body:JSON.stringify(body)}:{})});
      const value=await response.json();
      if(!response.ok||value.status==='error')throw Error(value.message||'request failed');
      return value;
    };
    window.AstrBotPluginView={ready:async()=>({}),apiGet:(endpoint,body)=>call('GET',endpoint,body),apiPost:(endpoint,body)=>call('POST',endpoint,body)};
  });
  await page.goto(process.argv[2]+'/preview/');
  const waitStatus=text=>page.waitForFunction(value=>document.getElementById('status').textContent===value,text);
  await waitStatus('已载入');
  const fixed=await page.locator('#fixed').inputValue();
  const persona=await page.locator('#character').inputValue();
  const preferences=await page.locator('#preferences').inputValue();
  for(const value of [' 机器人：阿蓝 ', '成员甲：小甲', '机器人：阿蓝']) {
    await page.locator('#add-nickname').click();
    await page.locator('#nicknames input').last().fill(value);
  }
  await page.locator('#save-memory').click();
  await waitStatus('记忆已保存');
  assert.deepEqual(await page.locator('#nicknames input').evaluateAll(inputs=>inputs.map(input=>input.value)),['机器人：阿蓝','成员甲：小甲']);
  await page.locator('#nicknames input').first().fill('机器人：小蓝');
  await page.locator('#nicknames button').last().click();
  await page.locator('#save-memory').click();
  await waitStatus('记忆已保存');
  await page.locator('#refresh').click();
  await waitStatus('已载入');
  assert.deepEqual(await page.locator('#nicknames input').evaluateAll(inputs=>inputs.map(input=>input.value)),['机器人：小蓝']);
  assert.equal(await page.locator('#fixed').inputValue(),fixed);
  assert.equal(await page.locator('#character').inputValue(),persona);
  assert.equal(await page.locator('#preferences').inputValue(),preferences);
  await page.locator('[data-tab="records"]').click();
  await page.locator('.record').first().getByRole('button',{name:'查看全文与来源'}).click();
  await page.locator('#record-detail').waitFor({state:'visible'});
  assert.match(await page.locator('#record-memory').textContent(),/• 机器人：小蓝/);
  await page.locator('#record-detail .close').click();
  await page.locator('[data-tab="memory"]').click();
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.screenshot({path:require('node:path').join(require('node:os').tmpdir(),'astrbot-nicknames-ui.png'),fullPage:true});
  await page.locator('#nicknames button').click();
  await page.locator('#save-memory').click();
  await waitStatus('记忆已保存');
  await page.locator('#refresh').click();
  await waitStatus('已载入');
  assert.equal(await page.locator('#nicknames input').count(),0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({ui:'passed',operations:['list add','trim and dedup','edit','delete','empty list','save and reload','record display','protected fields preserved','mobile layout'],pageErrors:errors}));
  await browser.close();
})().catch(async error=>{console.error(error);await browser?.close();process.exitCode=1;});
