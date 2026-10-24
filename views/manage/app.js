const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
let state, selectedPage, selectedRecord, offset = 0, loading = 0, latestSeen = null;
let activeTab = 'memory';
const kinds = {automatic:'自动整理',nickname_tool:'昵称维护',manual:'手动编辑',migration:'升级迁移',baseline:'初始内容',external:'外部保存',restore:'恢复记忆',legacy_backup:'旧备份导入'};
function node(tag, text, cls) {const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls; return el;}
function nicknameRow(value='') {const row=node('div',undefined,'nickname-row'),input=node('input'),remove=node('button','删除');input.value=value;input.setAttribute('aria-label','昵称条目');remove.type='button';remove.setAttribute('aria-label','删除昵称');remove.onclick=()=>row.remove();row.append(input,remove);$('nicknames').append(row);return input;}
function nicknameValues(value) {return Array.isArray(value)?value:String(value||'').split('\n').filter(line=>line.trim());}
function date(text) {try{return new Intl.DateTimeFormat('zh-CN',{timeZone:state?.time_zone||'Asia/Shanghai',dateStyle:'medium',timeStyle:'medium'}).format(new Date(text));}catch{return text;}}
async function api(method, endpoint, body={}) {const r=method==='GET'?await bridge.apiGet(endpoint,body):await bridge.apiPost(endpoint,body); return r?.status==='ok'?r.data:r;}
function payload(extra={}) {return {persona_id:state.persona_id,...extra};}
function result(row) {return row.state==='failed'?'整理失败':row.state==='prepared'?'保存待确认':row.kind==='legacy_backup'?'旧备份':row.kind==='baseline'?'初始内容':row.changed?'已更新':'无变化';}
function tab(name) {activeTab=name;for(const el of document.querySelectorAll('.panel'))el.hidden=el.id!==name;for(const el of document.querySelectorAll('nav button'))el.classList.toggle('active',el.dataset.tab===name);}
async function load() {
  const generation=++loading;
  try {
    const next=await api('GET','state',{persona_id:$('persona').value||''});
    if(generation!==loading)return;
    state=next;
    $('persona').replaceChildren(...state.personas.map(id=>{const option=node('option',id);option.value=id;return option;}));$('persona').value=state.persona_id;
    $('character').value=state.memory.sections.persona;$('preferences').value=state.memory.sections.output_preferences;$('other').value=state.memory.sections.other;$('nicknames').replaceChildren();for(const name of nicknameValues(state.memory.sections.nicknames))nicknameRow(name);
    $('stats').textContent='正式回复常驻资料 '+state.protected_bytes+' 字节 · 昵称和其他 '+state.memory_bytes+' / '+state.settings.long_term_memory_max_bytes+' 字节'+(state.decision_budget?'\n判断总预算 '+state.decision_budget+' 字节（还包含判断指令和请求封装）':'');
    $('budget-actions').replaceChildren();
    if(state.decision_budget){
      $('stats').textContent+='\n判断常驻记忆 '+(state.decision_memory_bytes??state.protected_bytes)+' 字节，扣除后剩余 '+Math.max(0,state.decision_budget-(state.decision_memory_bytes??state.protected_bytes))+' 字节，仍需容纳判断指令、请求封装和近期消息。';
      const link=node('a','打开插件页调整回复判断预算');link.href='/#/extension/plugins';link.target='_top';$('budget-actions').append(link);
    }
    if(state.decision_budget&&(state.decision_memory_bytes??state.protected_bytes)>=state.decision_budget)$('stats').textContent+='\n常驻记忆已超过判断预算，请调整内容或判断预算。';
    $('progress').textContent=state.progress.map(p=>p.origin+'：待整理 '+p.pending+' / '+state.settings.auto_memory_message_interval+' 条').join('；')||'暂时没有待整理消息';
    $('capacity').textContent=state.catalog.page_count+' / '+state.catalog.max_pages+' 页 · 目录 '+state.catalog.catalog_bytes+' 字节';
    $('pages').replaceChildren();
    for(const page of state.catalog.pages){const card=node('article',undefined,'card');card.append(node('h3',page.title),node('p',page.description),node('p',page.page_id,'muted'));const button=node('button','打开页面');button.onclick=()=>openPage(page.page_id);card.append(button);$('pages').append(card);}
    if(!state.catalog.pages.length)$('pages').append(node('p','记事本还是空的。','muted'));
    $('auto-enabled').checked=state.settings.auto_memory_enabled;
    for(const [id,key] of [['interval','auto_memory_message_interval'],['max-pages','notebook_max_pages'],['timeout','auto_memory_timeout'],['memory-bytes','long_term_memory_max_bytes'],['catalog-bytes','notebook_catalog_max_bytes']])$(id).value=state.settings[key];
    await loadRecords();$('status').textContent='已载入';
  }catch(e){$('status').textContent=e.message;}
}
async function loadRecords(){
  const persona=state.persona_id, start=offset;
  const r=await api('GET','records',payload({offset}));if(state.persona_id!==persona||offset!==start)return;
  if(offset===0)latestSeen=r.records[0]?r.records[0].id+':'+r.records[0].state:null;
  $('record-list').replaceChildren();
  for(const row of r.records){const article=node('article',undefined,'record'),body=node('div');body.append(node('strong',result(row)),node('p',date(row.created_at)+' · '+(kinds[row.kind]||row.kind),'muted'));if(row.error)body.append(node('p',row.error));const button=node('button','查看全文与来源');button.onclick=()=>openRecord(row.id);article.append(body,button);$('record-list').append(article);}
  if(!r.records.length)$('record-list').append(node('p','暂无记忆记录。','muted'));
  $('previous').disabled=offset===0;$('next').disabled=r.records.length<30;$('record-offset').textContent='第 '+(Math.floor(offset/30)+1)+' 页';
}
async function openPage(id){
  try{selectedPage=id?await api('GET','page',payload({page_id:id})):null;$('page-heading').textContent=id?'编辑页面':'新建页面';$('page-title').value=selectedPage?.title||'';$('page-description').value=selectedPage?.description||'';$('page-content').value=selectedPage?.content||'';$('page-status').textContent='';$('delete-page').hidden=!id;$('page-editor').showModal();}catch(e){$('status').textContent=e.message;}
}
async function openRecord(id){
  try{selectedRecord=await api('GET','records',payload({record_id:id}));const r=selectedRecord;$('record-meta').textContent=date(r.created_at)+' · '+(kinds[r.kind]||r.kind)+' · '+result(r);$('record-memory').replaceChildren();for(const [title,text] of [['性格和人设',r.memory.sections.persona],['回复偏好',r.memory.sections.output_preferences],['昵称',nicknameValues(r.memory.sections.nicknames).map(name=>'• '+name).join('\n')],['其他',r.memory.sections.other]])$('record-memory').append(node('h3',title),node('pre',text||'（空）'));
    if(r.legacy_text)$('record-memory').append(node('h3','旧备份全文（时间为导入时间）'),node('pre',r.legacy_text));
    $('sources').replaceChildren();for(const source of r.sources){const section=node('article',undefined,'source');section.append(node('p',(source.sender_name||source.role||'')+' · '+(source.sender_id||'')+' · '+(source.time?date(source.time):''),'muted'),node('p',source.origin||'','muted'),node('pre',source.text||''));if(source.parts?.length){const details=node('details'),summary=node('summary','原消息结构');details.append(summary,node('pre',JSON.stringify(source.parts,null,2)));section.append(details);}$('sources').append(section);}if(!r.sources.length)$('sources').append(node('p','这条记录没有聊天原文来源（例如手动编辑或升级前的内容）。','muted'));
    $('include-owner').checked=false;$('restore-memory').hidden=r.state!=='committed';$('record-status').textContent='';$('record-detail').showModal();
  }catch(e){$('status').textContent=e.message;}
}
for(const button of document.querySelectorAll('nav button'))button.onclick=()=>tab(button.dataset.tab);
$('refresh').onclick=load;
$('persona').onchange=()=>{offset=0;latestSeen=null;load();};
$('new-page').onclick=()=>openPage(null);
$('add-nickname').onclick=()=>nicknameRow().focus();
$('save-memory').onclick=async()=>{try{await api('POST','memory',payload({stamp:state.stamp,sections:{persona:$('character').value,output_preferences:$('preferences').value,nicknames:[...$('nicknames').querySelectorAll('input')].map(input=>input.value.trim()).filter(Boolean),other:$('other').value}}));await load();$('status').textContent='记忆已保存';}catch(e){$('status').textContent=e.message;}};
$('save-page').onclick=async()=>{try{await api('POST','page',payload({action:selectedPage?'update':'create',...(selectedPage?{page_id:selectedPage.page_id,stamp:selectedPage.stamp}:{}),title:$('page-title').value,description:$('page-description').value,content:$('page-content').value}));$('page-editor').close();await load();$('status').textContent='页面已保存';}catch(e){$('page-status').textContent=e.message;}};
$('delete-page').onclick=async()=>{if(!confirm('删除这页记事本？'))return;try{await api('POST','page',payload({action:'delete',page_id:selectedPage.page_id,stamp:selectedPage.stamp}));$('page-editor').close();await load();$('status').textContent='页面已删除';}catch(e){$('page-status').textContent=e.message;}};
$('restore-memory').onclick=async()=>{try{await api('POST','restore',payload({stamp:state.stamp,record_id:selectedRecord.id,include_owner:$('include-owner').checked}));$('record-detail').close();await load();$('status').textContent='所选记忆已恢复';}catch(e){$('record-status').textContent=e.message;}};
$('save-settings').onclick=async()=>{try{const body={auto_memory_enabled:$('auto-enabled').checked};for(const [id,key] of [['interval','auto_memory_message_interval'],['max-pages','notebook_max_pages'],['timeout','auto_memory_timeout'],['memory-bytes','long_term_memory_max_bytes'],['catalog-bytes','notebook_catalog_max_bytes']]){const value=Number($(id).value);if(!Number.isSafeInteger(value)||value<1)throw Error('配置必须是正整数');body[key]=value;}await api('POST','settings',body);await load();$('status').textContent='配置已保存';}catch(e){$('status').textContent=e.message;}};
$('previous').onclick=()=>{offset=Math.max(0,offset-30);loadRecords().catch(e=>$('status').textContent=e.message);};
$('next').onclick=()=>{offset+=30;loadRecords().catch(e=>$('status').textContent=e.message);};
try{if(!bridge)throw Error('请从 AstrBot 插件详情打开此页面。');await bridge.ready();await load();}catch(e){$('status').textContent=e.message;}
// Read only result metadata; never replace a user's in-progress form.
setInterval(async()=>{if(!state)return;try{
  const persona=state.persona_id;
  const r=await api('GET','records',payload({offset:0,limit:1}));if(state.persona_id!==persona)return;
  const row=r.records[0];
  const key=row?row.id+':'+row.state:null;
  if(row&&key!==latestSeen){latestSeen=key;if(row.kind==='automatic'&&row.state==='committed')$('status').textContent='记忆整理完成：'+result(row)+' · '+date(row.created_at);}
  if(activeTab==='records'&&!document.querySelector('dialog[open]'))await loadRecords();
}catch(e){$('status').textContent=e.message;}},15000);
