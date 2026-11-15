import {reconciliations} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {StatementState,BatchStatus,SideLabel} from '../constants/enums.js';
import {esc,date,notify} from '../utils/dom.js';
import {parseCSV} from '../utils/csv.js';

const stateClass=s=>'state state-'+(s||'empty');
const carried=e=>e?.carried_forward?' · 该侧最新修订未含此窑，沿用旧值':'';

function sourceCard(side,snap,editable){
 const s=snap.sides?.[side];
 return '<section class="panel source-card"><div class="section-title"><h2>'+esc(SideLabel[side])+'</h2>'+
  '<span>'+(s?'修订 '+esc(s.revision):'未收到')+'</span></div>'+
  (s?'<p class="source-meta">'+esc(s.source_name||'手工录入')+' · '+s.rows+' 个窑次<br>提交于 '+date(s.submitted_at)+'</p>':
      '<p class="source-meta muted">尚未导入。双方都到齐后才能发布分析批次。</p>')+
  '<form data-ingest="'+side+'">'+
  '<label>源表文件<input type="file" name="file" accept=".csv,.json"></label>'+
  '<p class="hint">CSV 表头支持 batch/line/fired/passed 或 窑次/窑线/烧成件数/合格件数，可选 revision/修订号。</p>'+
  '<label>修订号<input name="revision" required maxlength="80" placeholder="例如 R-0042，须高于现行修订" value="'+esc(s?s.revision:'')+'"></label>'+
  '<label>记录 JSON<textarea name="records" rows="4" required placeholder="粘贴 JSON 数组，或选择上方文件自动填充"></textarea></label>'+
  '<button type="submit" class="primary" '+(editable?'':'disabled')+'>导入'+esc(SideLabel[side])+'</button></form></section>';
}

function batchesTable(snap,editable){
 if(!snap.batches?.length)return '<div class="empty">还没有可对账的窑次。请先导入两侧源表。</div>';
 const rows=snap.batches.map(b=>{
  const diff=b.difference?((b.difference.fired?'烧成 '+b.difference.fired+'；':'')+(b.difference.passed?'合格 '+b.difference.passed:'')).replace(/；$/,''):'—';
  const chosen=b.chosen?esc(b.chosen.side_label)+' · '+b.chosen.fired+'/'+b.chosen.passed:'—';
  const action=b.status==='disputed'&&editable?'<button class="button small" data-adjudicate="'+esc(b.batch)+'">裁定差异</button>':
   b.status==='resolved'&&editable?'<button class="text-button" data-adjudicate="'+esc(b.batch)+'">改判</button>':'';
  return '<tr><td><strong>'+esc(b.batch)+'</strong><br><small>'+esc(b.line||'')+'</small></td>'+
   '<td>'+(b.ledger?esc(b.ledger.fired)+' / '+esc(b.ledger.passed)+'<br><small>修订 '+esc(b.ledger.revision)+carried(b.ledger)+'</small>':'<span class="missing-flag">缺失</span>')+'</td>'+
   '<td>'+(b.inspection?esc(b.inspection.fired)+' / '+esc(b.inspection.passed)+'<br><small>修订 '+esc(b.inspection.revision)+carried(b.inspection)+'</small>':'<span class="missing-flag">缺失</span>')+'</td>'+
   '<td>'+esc(diff)+'</td><td>'+chosen+'</td>'+
   '<td><span class="batch-status '+esc(b.status)+'">'+esc(BatchStatus[b.status]||b.status)+'</span>'+
   (b.stale_adjudication?'<br><small class="stale-note">原裁定失效：'+esc(b.stale_adjudication.reason)+'</small>':'')+'</td>'+
   '<td>'+action+'</td></tr>';
 }).join('');
 return '<div class="table-wrap"><table><thead><tr><th>窑次</th><th>出窑台账（烧成/合格）</th><th>终检表（烧成/合格）</th>'+
  '<th>差异（终检−台账）</th><th>采用取值</th><th>状态</th><th></th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}

function adjudicationPanel(){
 return '<form id="adjudicate-form" class="adjudicate-form" hidden>'+
  '<div class="section-title"><h2>裁定差异 · <span id="adjudicate-batch"></span></h2><button type="button" class="text-button" id="cancel-adjudicate">取消</button></div>'+
  '<p class="hint">裁定只处理件数差异；缺失窑次必须先补源表。理由随取值一并留档。</p>'+
  '<div class="parameter-grid"><label>采用哪一侧<input name="side_display" id="adjudicate-context" readonly></label>'+
  '<label>采用来源<select name="side"><option value="ledger">出窑台账</option><option value="inspection">终检表</option></select></label></div>'+
  '<label>裁定理由（至少 4 字）<textarea name="reason" rows="3" required placeholder="例如：终检复称，出窑台账少记 8 件"></textarea></label>'+
  '<button type="submit" class="primary">记录裁定</button></form>';
}

export async function reconcilePage(id){
 const list=await reconciliations.list();
 workspace.reconciliations=list;
 const selectedId=Number(id)||list[0]?.id||null;
 const canWrite=session.user.role!=='viewer',isAdmin=session.user.role==='admin';
 let snap=null,pubs=[];
 if(selectedId){[snap,pubs]=await Promise.all([reconciliations.get(selectedId),reconciliations.publications(selectedId)]);}
 const createForm='<form id="statement-form"><label>新对账单名称<input name="name" required minlength="2" placeholder="例如：11 月第 1 周双源对账"></label>'+
  '<button type="submit" class="primary" '+(canWrite?'':'disabled')+'>建立对账单</button></form>';
 const picker='<div class="statement-list">'+list.map(s=>'<a class="statement-item '+(s.id===selectedId?'active':'')+'" href="#/reconcile/'+s.id+'">'+
  '<strong>'+esc(s.name)+'</strong><span class="'+stateClass(s.state)+'">'+esc(StatementState[s.state]||s.state)+'</span>'+
  '<small>台账 '+esc(s.ledger_revision||'—')+' · 终检 '+esc(s.inspection_revision||'—')+' · 已发布 v'+s.published_version+'</small></a>').join('')+
  (!list.length?'<p class="empty">尚无对账单。</p>':'')+'</div>';
 let detail='<div class="empty">先在左侧建立对账单，再分别导入出窑台账与终检表。</div>';
 if(snap){
  const blockers=snap.blockers?.length?'<ul class="blockers">'+snap.blockers.map(b=>'<li><span>'+esc(b.code)+'</span>'+esc(b.message)+'</li>').join('')+'</ul>':'';
  detail='<div class="recon-head"><div><p class="eyebrow">RECONCILIATION '+selectedId+'</p><h2>'+esc(snap.name)+'</h2></div>'+
   '<span class="'+stateClass(snap.state)+'">'+esc(StatementState[snap.state]||snap.state)+'</span></div>'+
   '<div class="recon-totals">'+[['一致',snap.totals.matched],['已裁定',snap.totals.resolved],['待裁定',snap.totals.disputed],['缺失',snap.totals.missing]].map(([k,v])=>'<div><span>'+k+'</span><strong>'+v+'</strong></div>').join('')+'</div>'+
   (blockers?'<section class="panel"><div class="section-title"><h2>未解决事项</h2><span>留待处理，不阻塞继续收表</span></div>'+blockers+'</section>':'')+
   '<div class="source-grid">'+sourceCard('ledger',snap,canWrite)+sourceCard('inspection',snap,canWrite)+'</div>'+
   adjudicationPanel()+
   '<section class="panel"><div class="section-title"><h2>逐窑对账</h2><span>'+snap.batches.length+' 个窑次</span></div>'+batchesTable(snap,isAdmin)+'</section>'+
   '<section class="panel publish-panel"><div class="section-title"><h2>发布分析批次</h2><span>'+(snap.can_publish?'双方齐备、件数相符或差异已裁定':'尚未满足发布条件')+'</span></div>'+
   '<p class="hint">发布后生成不可变数据批次与复盘任务。请求号与内容指纹双重复用：失败重试不重复建批次；发布后再改源表会另开版本，既有复盘不被改写。</p>'+
   '<button class="primary" id="publish-statement" '+(snap.can_publish&&isAdmin?'':'disabled')+'>发布分析批次 · v'+(snap.published_version+1)+'</button>'+
   (snap.omitted?.length?'<div class="omitted"><h3>不进入分析批次的窑次</h3><ul>'+snap.omitted.map(o=>'<li><strong>'+esc(o.batch)+'</strong>'+esc(o.message)+'</li>').join('')+'</ul></div>':'')+
   '<p class="hint" id="publish-result">'+(snap.can_publish?'':'请先处理上方未解决事项。')+'</p></section>'+
   '<section class="panel"><div class="section-title"><h2>已发布版本</h2><span>'+pubs.length+' 个批次</span></div>'+
   (pubs.length?'<div class="publication-list">'+pubs.map(p=>'<article><div><strong>v'+p.version+' · 批次 #'+p.dataset_id+'</strong>'+
     '<p>台账 '+esc(p.ledger_revision)+' · 终检 '+esc(p.inspection_revision)+' · '+p.batch_count+' 个窑次</p>'+
     '<small>'+esc(p.published_by)+' · '+date(p.published_at)+' · 指纹 '+esc(p.fingerprint.slice(0,16))+'…</small></div>'+
     '<span class="pub-version">v'+p.version+'</span></article>').join('')+'</div>':'<p class="empty">尚未发布。发布后此处保留每个版本的源修订。</p>')+
   '</section>'+
   '<section class="panel" id="trace-panel"><div class="section-title"><h2>来源追溯</h2><span id="trace-batch-label">选择一个窑次</span></div>'+
   '<form id="trace-form" class="trace-form"><label>窑次<input name="batch" required placeholder="例如 QH-004"></label><button type="submit" class="button small">追溯</button></form>'+
   '<div id="trace-result"><p class="empty">按窑次查看每次源表提交、裁定与发布版本。</p></div></section>';
 }
 return heading('03 / TWO-SOURCE RECONCILIATION','双来源对账','出窑台账与终检表分开送达。双方到齐、件数相符或差异经裁定后才可发布分析批次；仅已对齐的数据进入复盘。')+
  '<div class="recon-grid"><aside class="recon-side"><section class="panel"><div class="section-title"><h2>对账单</h2><span>'+list.length+' 份</span></div>'+
  picker+createForm+'</section></aside><div class="recon-main">'+detail+'</div></div>';
}

export function bindReconcile(refresh){
 let currentId=Number(location.hash.split('/')[2])||workspace.reconciliations?.[0]?.id;
 async function selectedSnapshot(){return reconciliations.get(currentId);}

 document.querySelector('#statement-form')?.addEventListener('submit',async e=>{
  e.preventDefault();const form=e.currentTarget,button=form.querySelector('[type=submit]');button.disabled=true;
  try{const s=await reconciliations.create({name:new FormData(form).get('name')});notify('对账单已建立');location.hash='#/reconcile/'+s.id;}
  catch(error){notify(error.message,true);button.disabled=false;}
 });

 document.querySelectorAll('[data-ingest]').forEach(form=>{
  const side=form.dataset.ingest,file=form.querySelector('[name=file]'),area=form.querySelector('[name=records]');
  file.addEventListener('change',async e=>{try{const f=e.target.files[0];if(!f)return;const text=await f.text();
    const records=f.name.endsWith('.csv')?parseCSV(text):JSON.parse(text);
    area.value=JSON.stringify(records,null,2);}catch(error){notify(error.message,true);}});
  form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;
   try{
    const records=JSON.parse(area.value);
    const result=await reconciliations.ingest(currentId,{side,revision:form.querySelector('[name=revision]').value.trim(),
      source_name:file?.files?.[0]?.name||'手工粘贴',records});
    if(result.replayed)notify('该修订与内容此前已导入，未重复记录');
    else if(result.ignored.length)notify('已接收；'+result.ignored.length+' 条迟到旧修订未覆盖现行值',true);
    else if(result.retracted?.length)notify('已导入 '+result.applied.length+' 个窑次，撤回 '+result.retracted.length+' 个未再出现的窑次',true);
    else notify('已导入 '+result.applied.length+' 个窑次');
    await refresh();
   }catch(error){notify(error.message,true);button.disabled=false;}
  });
 });

 const adjForm=document.querySelector('#adjudicate-form');
 document.querySelectorAll('[data-adjudicate]').forEach(button=>button.onclick=async()=>{
  try{
   const snap=await selectedSnapshot();
   const row=snap.batches.find(b=>b.batch===button.dataset.adjudicate);
   adjForm.hidden=false;adjForm.dataset.batch=row.batch;adjForm.dataset.token=snap.revision_token;
   document.querySelector('#adjudicate-batch').textContent=row.batch;
   document.querySelector('#adjudicate-context').value='台账 '+row.ledger.fired+'/'+row.ledger.passed+'（'+row.ledger.revision+'） · 终检 '+row.inspection.fired+'/'+row.inspection.passed+'（'+row.inspection.revision+'）';
   adjForm.scrollIntoView({behavior:'smooth',block:'center'});
  }catch(error){notify(error.message,true);}
 });
 document.querySelector('#cancel-adjudicate')?.addEventListener('click',()=>{adjForm.hidden=true;});
 adjForm?.addEventListener('submit',async e=>{
  e.preventDefault();const button=adjForm.querySelector('[type=submit]');button.disabled=true;
  const f=new FormData(adjForm);
  try{const r=await reconciliations.adjudicate(currentId,{batch:adjForm.dataset.batch,side:f.get('side'),
    reason:f.get('reason'),expected_token:adjForm.dataset.token});
   notify(r.snapshot.can_publish?'裁定已记录，对账单就绪':'裁定已记录');await refresh();}
  catch(error){notify(error.message,true);button.disabled=false;}
 });

 let pendingRequest=null;
 function requestId(){
  const key='recon-request-'+currentId;
  pendingRequest=pendingRequest||sessionStorage.getItem(key)||null;
  if(!pendingRequest){pendingRequest=(crypto.randomUUID?.()||('req-'+Date.now()+'-'+Math.random().toString(16).slice(2))).replace(/[^a-zA-Z0-9-]/g,'').slice(0,36);sessionStorage.setItem(key,pendingRequest);}
  return pendingRequest;
 }
 document.querySelector('#publish-statement')?.addEventListener('click',async e=>{
  const button=e.currentTarget;button.disabled=true;
  try{
   const snap=await selectedSnapshot();
   const result=await reconciliations.publish(currentId,{request_id:requestId(),expected_token:snap.revision_token});
   sessionStorage.removeItem('recon-request-'+currentId);pendingRequest=null;
   const message=result.created
     ?'已发布 v'+result.version+'：数据批次 #'+result.dataset_id+'，复盘任务 #'+result.job_id+' 已排队。'
     :'该请求或同一内容此前已发布 v'+result.version+'，未重复建批次。';
   notify(result.created?'分析批次已发布':'已存在相同发布，未重复创建');
   await refresh();
   const line=document.querySelector('#publish-result');
   if(line)line.textContent=message;
  }catch(error){const line=document.querySelector('#publish-result');
   if(line)line.textContent='发布失败：'+error.message+'。修正后可重试，同一请求不会重复建批次。';
   notify(error.message,true);button.disabled=false;}
 });

 async function showTrace(batch){
  try{
   const t=await reconciliations.trace(currentId,batch);
   document.querySelector('#trace-batch-label').textContent=batch;
   document.querySelector('#trace-result').innerHTML=
    '<div class="trace-current"><span>当前状态</span><strong class="batch-status '+esc(t.current.status)+'">'+esc(BatchStatus[t.current.status]||t.current.status)+'</strong>'+
    (t.current.chosen?'<small>采用 '+esc(t.current.chosen.side_label)+' '+t.current.chosen.fired+'/'+t.current.chosen.passed+' · '+esc(t.current.chosen.reason)+'</small>':'')+'</div>'+
    '<h3>源表提交</h3>'+(t.submissions.length?'<ul class="trace-list">'+t.submissions.map(s=>'<li><strong>'+esc(s.side_label)+' · 修订 '+esc(s.revision)+'</strong>'+
      '<span>'+s.fired+'/'+s.passed+' · '+esc(s.source_name||'手工')+' · '+date(s.received_at)+'</span>'+
      '<em class="trace-outcome '+esc(s.outcome)+'">'+(s.outcome==='applied'?'生效':s.outcome==='ignored'?'迟到未覆盖':'未变更')+'</em></li>').join(''):'<p class="empty">无提交记录。</p>')+
    '<h3>裁定记录</h3>'+(t.adjudications.length?'<ul class="trace-list">'+t.adjudications.map(a=>'<li><strong>采用'+esc(a.side_label)+' '+a.fired+'/'+a.passed+'</strong>'+
      '<span>'+esc(a.reason)+' · '+esc(a.decided_by)+' · '+date(a.decided_at)+'</span>'+
      '<em class="trace-outcome '+(a.status==='active'?'applied':'ignored')+'">'+(a.status==='active'?'有效':'已失效'+(a.stale_reason?'（'+esc(a.stale_reason)+'）':''))+'</em></li>').join(''):'<p class="empty">无裁定。</p>')+
    '<h3>发布版本</h3>'+(t.publications.length?'<ul class="trace-list">'+t.publications.map(p=>'<li><strong>v'+p.version+'</strong>'+
      '<span>采用 '+esc(p.record.source==='matched'?'双方一致':p.record.source==='ledger'?'出窑台账':'终检表')+' '+p.record.fired+'/'+p.record.passed+
      ' · 台账 '+esc(p.ledger_revision)+' · 终检 '+esc(p.inspection_revision)+' · '+date(p.published_at)+'</span></li>').join(''):'<p class="empty">尚未进入发布批次。</p>');
  }catch(error){notify(error.message,true);}
 }
 document.querySelector('#trace-form')?.addEventListener('submit',e=>{e.preventDefault();showTrace(new FormData(e.currentTarget).get('batch').trim());});
}
