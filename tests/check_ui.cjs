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
  await page.waitForFunction(()=>document.getElementById('status').textContent==='已载入');
  assert.match(await page.locator('#stats').textContent(),/判断常驻记忆 \d+ 字节/);
  await page.locator('#character').fill('我是测试机器人，昵称阿蓝。');
  await page.locator('#save-memory').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='记忆已保存');
  await page.locator('[data-tab="notebook"]').click();
  await page.locator('#new-page').click();
  await page.locator('#page-title').fill('故障处理流程');
  await page.locator('#page-description').fill('遇到连接失败时读取');
  await page.locator('#page-content').fill('先检查连接，再查询状态。<script>不可执行</script>');
  await page.locator('#save-page').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='页面已保存');
  assert.equal(await page.locator('.card').count(),1);
  await page.getByRole('button',{name:'打开页面',exact:true}).click();
  assert.equal(await page.locator('#page-content').inputValue(),'先检查连接，再查询状态。<script>不可执行</script>');
  await page.locator('#page-content').fill('先检查连接，再确认请求地址。');
  await page.locator('#save-page').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='页面已保存');
  await page.getByRole('button',{name:'打开页面',exact:true}).click();
  page.once('dialog', dialog=>dialog.accept());
  await page.locator('#delete-page').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='页面已删除');
  assert.equal(await page.locator('.card').count(),0);
  await page.locator('[data-tab="settings"]').click();
  await page.locator('#interval').fill('7');
  await page.locator('#max-pages').fill('8');
  await page.locator('#save-settings').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='配置已保存');
  assert.equal(await page.locator('#interval').inputValue(),'7');
  assert.equal(await page.locator('#max-pages').inputValue(),'8');
  await page.locator('[data-tab="records"]').click();
  const unchanged=page.locator('.record').filter({hasText:'无变化'}).filter({hasText:'自动整理'}).first();
  await unchanged.getByRole('button',{name:'查看全文与来源'}).click();
  await page.locator('#record-detail').waitFor({state:'visible'});
  assert.match(await page.locator('#sources').textContent(),/这是本次整理的第一条原文/);
  assert.match(await page.locator('#record-meta').textContent(),/无变化/);
  await page.locator('#restore-memory').click();
  await page.waitForFunction(()=>document.getElementById('status').textContent==='所选记忆已恢复');
  await page.screenshot({path:require('node:path').join(require('node:os').tmpdir(),'astrbot-memory-notebook-ui.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  console.log(JSON.stringify({ui:'passed',operations:['memory save','page create','page read','page edit','page delete','settings persistence','unchanged source view','memory restore','mobile layout'],pageErrors:errors}));
  await browser.close();
})().catch(async error=>{console.error(error);await browser?.close();process.exitCode=1;});
