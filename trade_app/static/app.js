'use strict';
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = (v, digits=2) => Number(v ?? 0).toLocaleString('en-US', {minimumFractionDigits:digits,maximumFractionDigits:digits});
const money = (v) => `${number(v)} <small>${esc(state?.account?.currency || 'USD')}</small>`;
const cls = (v) => v > 0 ? 'positive' : v < 0 ? 'negative' : '';
const localTime = (t, options={}) => new Date(t*1000).toLocaleString('th-TH', {timeZone:'Asia/Bangkok',...options});
const row = (label, value, color='') => `<div class="row"><span>${esc(label)}</span><b class="${color}">${value}</b></div>`;
const badge = (signal) => `<span class="badge ${signal==='BUY'?'positive':signal==='SELL'?'negative':'muted'}">${esc(signal)}</span>`;
let state = null, page = 'dashboard', previewData = null, previewBody = null, requestId = null, previewAt = 0;
let settingsLoaded = false, journalPage=0, filteredRows=[], noticeTimer, sending=false, polling=false;
let orderRevision=0;
let levelPlan=null,autoLevelScope=null;
let closingPosition=null,closeRequestId=null;
const names={smc:'SMC Liquidity Sweep + BOS',supply:'Supply / Demand Retest',ema:'EMA Trend Pullback',bollinger:'Bollinger Bands Pullback',asian:'Asian Range Breakout',ai:'AI Playbook'};
const executionLabels={WAIT:'รอเงื่อนไข',READY:'รอตรวจ Risk',HOLDING:'ถือ Position/Pending',DISABLED:'ปิดกลยุทธ์',PAUSED:'Monitor หยุด',WAIT_NEW_BAR:'รอแท่งใหม่',BLOCKED:'บล็อก',RISK_BLOCKED:'Risk บล็อก',EVALUATED:'ประเมินแท่งนี้แล้ว',SENT:'ส่งแล้ว',REJECTED:'Broker ปฏิเสธ'};
function strategyName(magic){const key=Object.keys(names).find((key,i)=>26001+i===magic);return state.settings.magic_names[String(magic)]||(key?names[key]:[0,26000].includes(magic)?'Manual':`EA #${magic}`);}
const headings={dashboard:['Overview','ภาพรวมพอร์ต','ติดตามบัญชี ผลการเทรด และความเสี่ยงแบบเรียลไทม์'],terminal:['Trading Terminal','Trading Terminal','วางแผน XAUUSD ตรวจความเสี่ยง แล้วส่งคำสั่ง Demo'],journal:['Trading Journal','บันทึกการเทรด','ติดตามทุก Deal แยกกลยุทธ์และ EA พร้อมส่งออกข้อมูล'],analytics:['AI & Analytics','กลยุทธ์และการวิเคราะห์','สแกนโอกาสและดูประสิทธิภาพจากผลการเทรดจริงในบัญชี'],risk:['Risk Management','ควบคุมความเสี่ยง','กำหนดขอบเขตของระบบและหยุดคำสั่งใหม่ด้วย Kill Switch'],research:['Research','Research workspace','นำเข้าข้อมูลย้อนหลังและตรวจผลจำลองใน session นี้']};

function notice(message, error=false){
  const el=$('notice'); el.textContent=message; el.classList.add('visible'); el.style.color=error?'var(--red)':'var(--gold)';
  clearTimeout(noticeTimer); noticeTimer=setTimeout(()=>el.classList.remove('visible'),15000);
}
async function api(url, method='GET', body){
  const response=await fetch(url,{method,headers:{'Content-Type':'application/json', ...(method==='GET'?{}:{'X-Trade-Token':state?.token || ''})},...(body===undefined?{}:{body:JSON.stringify(body)})});
  let data; const text=await response.text(); try{data=JSON.parse(text);}catch{data={detail:text};}
  if(!response.ok){const detail=data.detail;throw new Error(Array.isArray(detail)?detail.map(x=>`${x.loc.join('.')}: ${x.msg}`).join('; '):detail || 'คำขอไม่สำเร็จ');}
  return data;
}
function navigate(next){
  page=next; document.querySelectorAll('.page').forEach(el=>el.classList.toggle('active',el.id===page));
  document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  const h=headings[page]; $('crumb').textContent=h[0]; $('title').textContent=h[1]; $('subtitle').textContent=h[2];
  render(); window.scrollTo({top:0,behavior:'smooth'});
}
document.querySelectorAll('[data-page]').forEach(el=>el.addEventListener('click',()=>navigate(el.dataset.page)));

function table(headers, rows){
  return rows.length?`<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(c=>`<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty">ยังไม่มีข้อมูลในช่วงนี้</div>';
}
function card(label, value, foot, color='', icon='◈'){
  return `<article class="card"><div class="card-label">${esc(label)}<span>${icon}</span></div><div class="card-value ${color}">${value}</div><div class="card-foot">${esc(foot)}</div></article>`;
}
function render(){
  if(!state) return;
  const connected=state.connected;
  $('statusDot').classList.toggle('online',connected); $('connection').textContent=connected?'Connected · 3s refresh':'Disconnected';
  $('modeLabel').textContent=state.mode==='simulation'?'PAPER SIMULATION':'MT5 · DEMO TERMINAL';
  $('errorBanner').hidden=connected;
  $('errorBanner').textContent=state.error || 'เชื่อมต่อข้อมูลไม่ได้';
  const kill=state.settings?.kill_switch;
  $('killTop').textContent=kill?'⏹ ระบบหยุดอยู่':'⏹ Kill Switch';
  $('killMain').textContent=kill?'ปิด Kill Switch เพื่อรับคำสั่งใหม่':'เปิด Kill Switch';
  $('killTitle').textContent=kill?'หยุดรับคำสั่งใหม่แล้ว':'ระบบพร้อมรับคำสั่ง';
  renderMonitor();
  if(!settingsLoaded&&state.settings){loadSettings();settingsLoaded=true;}
  if(!connected){$('accountBadge').textContent='OFFLINE'; $('sendOrder').disabled=true; return;}
  const a=state.account;
  $('accountBadge').textContent=state.mode==='simulation'?'ข้อมูลจำลอง · PAPER ACCOUNT':`${a.is_demo?'DEMO':'READ ONLY'} · ${a.login} · ${a.server}`;
  $('lastUpdate').textContent=`อัปเดต ${localTime(state.updated,{hour:'2-digit',minute:'2-digit',second:'2-digit'})}`;
  if(previewData&&previewData.scope!==state.scope)invalidatePreview();
  if(page==='dashboard')renderDashboard();
  if(page==='terminal')renderTerminal();
  if(page==='journal')renderJournal();
  if(page==='analytics')renderAnalytics();
  if(page==='risk')renderRisk();
  if(page==='research')window.renderResearch?.(state);
  if(kill || Date.now()-previewAt>30000)$('sendOrder').disabled=true;
}
$('curveMode').onchange=()=>{if(state?.connected)renderDashboard();};
function riskRows(){
  const a=state.account,s=state.settings;
  const used=Math.max(0,-state.day_net)+state.positions.reduce((sum,p)=>sum+Math.max(0,-p.profit),0);
  return row('Risk ต่อไม้',`${number(s.risk_percent)}%`)+row('ขาดทุนวันนี้ + Floating Loss',`${money(used)} / ${number(s.daily_loss)}`,used>=s.daily_loss?'negative':'')+row('Positions + Pending',`${state.positions.length+state.orders.length} / ${s.max_orders}`)+row('Spread',`${number(state.spread,1)} / ${s.max_spread} points`,state.spread>s.max_spread?'negative':'positive')+row('Lot สูงสุด',number(s.max_lot))+row('Kill Switch',s.kill_switch?'เปิด · หยุดคำสั่งใหม่':'ปิด',s.kill_switch?'negative':'positive');
}
function renderDashboard(){
  const a=state.account,m=state.analytics.summary;
  $('accountCards').innerHTML=card('Balance',money(a.balance),'ยอดคงเหลือในบัญชี','','◫')+card('Equity',money(a.equity),'Balance + Credit + Floating P&L','','⌁')+card('Credit',money(a.credit),'เครดิตจากโบรกเกอร์','','◇')+card('Floating P&L',money(a.profit),`${state.positions.length} open positions`,cls(a.profit),'↗');
  $('metricCards').innerHTML=card('Win rate',`${number(m.win_rate,1)}<small>%</small>`,`${m.trades} completed position lifecycles`)+card('Profit factor',m.profit_factor===null?(m.gross_profit>0?'∞':'—'):number(m.profit_factor),'กำไรสุทธิที่เป็นบวก / ขาดทุนสุทธิ')+card('Trade P&L drawdown',money(m.drawdown),'จากกำไรสุทธิสะสม','negative')+card('Equity drawdown',`${number(state.analytics.equity_drawdown_percent)}<small>%</small>`,'จาก Equity ที่เก็บตั้งแต่เปิดแอป','negative');
  const percent=Math.max(0,Math.min(100,100*state.day_net/state.settings.daily_target));
  $('goal').innerHTML=`<div class="big-number ${cls(state.day_net)}">${money(state.day_net)}</div><p>เป้ากำไร ${money(state.settings.daily_target)} / วัน</p><div class="progress"><i style="width:${percent}%"></i></div>${row('ความคืบหน้า',`${number(percent,0)}%`)}${row('วันที่',esc(localTime(Date.now()/1000,{year:'numeric',month:'short',day:'numeric'})))}`;
  $('riskBrief').innerHTML=row('Risk ต่อไม้',`${number(state.settings.risk_percent)}%`)+row('ออเดอร์ที่ใช้งาน',`${state.positions.length+state.orders.length} / ${state.settings.max_orders}`)+row('ระบบ',state.settings.kill_switch?'หยุดรับคำสั่ง':'พร้อมใช้งาน',state.settings.kill_switch?'negative':'positive');
  $('watchlist').innerHTML=state.signals.slice(0,4).map(s=>`<div class="watch-row"><div>${esc(s.name)}<p>${esc(s.signal==='WAIT'?'กำลังรอเงื่อนไข':`Entry ${number(s.entry)}`)}</p></div>${badge(s.signal)}</div>`).join('');
  if($('curveMode').value==='equity'){
    $('curveDescription').textContent='Equity ที่บันทึกตั้งแต่เริ่มใช้งาน';
    lineChart('equityChart',state.curve.map(x=>({time:x.time,value:x.equity})), '#e5bb72');
    $('equityCaption').textContent=state.curve.length<2?'กำลังเก็บข้อมูล Equity ทุก 30 วินาที กราฟจะปรากฏเมื่อมีอย่างน้อย 2 จุด':`เก็บตั้งแต่ ${localTime(state.curve[0].time)} · ${state.curve.length} ตัวอย่าง · ไม่ใช่ Equity ย้อนหลังจาก MT5`;
  }else{
    let cumulative=0;const trades=state.analytics.closed_trades,points=trades.length?[{time:trades[0].opened,value:0},...trades.map(t=>({time:t.time,value:cumulative+=t.net}))]:[];
    $('curveDescription').textContent='กำไรสุทธิสะสมจาก Position ที่ปิดครบ';
    lineChart('equityChart',points,'#e5bb72');
    $('equityCaption').textContent='Realized P&L รวมต้นทุน · ไม่รวม Floating P&L, เงินฝากและเงินถอน';
  }
  barChart('dailyChart',state.analytics.daily.slice(-14));
}
function renderTerminal(){
  $('lotInput').min=state.info.volume_min;
  $('lotInput').step=state.info.volume_step;
  const form=$('orderForm');
  if(levelPlan&&levelPlan.scope!==state.scope){levelPlan=null;$('levelSuggestion').hidden=true;}
  // Prefill only an untouched, empty market plan. Never replace values on price polls.
  if(autoLevelScope!==state.scope){autoLevelScope=state.scope;if(!form.elements.sl.value&&!form.elements.tp.value&&form.elements.kind.value==='MARKET')fillLevels('both',true);}
  $('symbolTitle').innerHTML=`${esc(state.symbol)} <span class="badge muted">M15</span>`;
  $('quote').innerHTML=`Bid <b class="positive">${number(state.tick.bid)}</b> &nbsp; Ask <b>${number(state.tick.ask)}</b>`;
  $('spreadBadge').textContent=`${number(state.spread,1)} points`;
  $('exposureCount').textContent=`${state.positions.length} POSITIONS · ${state.orders.length} PENDING`;
  const rows=[...state.positions.map(p=>['POSITION',p]),...state.orders.map(o=>['PENDING',o])].map(([kind,p])=>[
    `#${esc(p.ticket)}`,esc(p.symbol),badge((p.type%2===0?'BUY':'SELL')+(kind==='PENDING'?(p.type<4?' LIMIT':' STOP'):'')),esc(kind),`${esc(strategyName(p.magic))}<br><small>Magic ${esc(p.magic)}</small>`,number(p.volume),number(p.price_open),number(p.sl),number(p.tp),`<span class="${cls(p.profit)}">${kind==='POSITION'?number(p.profit):'—'}</span>`,kind==='PENDING'?`<button class="text-button cancel-order" data-ticket="${p.ticket}">ยกเลิก</button>`:`<button class="button danger close-position" data-ticket="${p.ticket}" ${state.account.is_demo?'':'disabled'}>ปิด Position</button>`]);
  $('positions').innerHTML=table(['Ticket','Symbol','Side','Type','Strategy / Magic','Lot','Entry','SL','TP','P&L','Action'],rows);
  document.querySelectorAll('.cancel-order').forEach(el=>el.onclick=async()=>{try{el.disabled=true;await api(`/api/orders/${el.dataset.ticket}?scope=${encodeURIComponent(state.scope)}`,'DELETE');notice('ยกเลิก Pending แล้ว');await refresh();}catch(e){notice(e.message,true);}finally{el.disabled=false;}});
  document.querySelectorAll('.close-position').forEach(el=>el.onclick=()=>{
    const position=state.positions.find(p=>p.ticket===Number(el.dataset.ticket));if(!position)return;
    closingPosition={...position,scope:state.scope};closeRequestId=crypto.randomUUID();
    $('closeDetails').textContent=`#${position.ticket} · ${position.symbol} · ${position.type===0?'BUY':'SELL'} · ${number(position.volume)} Lot · Floating P&L ${number(position.profit)} ${state.account.currency}`;
    $('closeDialog').showModal();
  });
  priceChart();
}
function renderMonitor(){
  const m=state.monitor;
  $('monitorQuick').textContent=m?.enabled?'Auto: ON':'Auto: OFF';
  $('monitorQuick').style.color=m?.enabled?'var(--green)':'var(--muted)';
  $('monitorBadge').textContent=m?.enabled?'AUTO RUNNING':'OFF';
  $('monitorBadge').className=`badge ${m?.enabled?'positive':'muted'}`;
  $('monitorStart').disabled=!m||m.enabled||!state.connected||!state.account?.is_demo||state.settings.kill_switch;
  $('monitorStop').disabled=!m?.enabled;
  $('monitorMessage').textContent=m?.message||'รีสตาร์ทเซิร์ฟเวอร์เพื่อโหลด Auto Monitor เวอร์ชันใหม่';
  $('monitorCloseExpiry').disabled=!!(m?.enabled||m?.close_on_expiry);
  if(m?.enabled||m?.close_on_expiry)$('monitorCloseExpiry').checked=m.close_on_expiry;
  $('monitorExpiry').textContent=m?.enabled||m?.close_on_expiry?`Session สิ้นสุด ${localTime(m.expires)} · สูงสุด ${m.max_positions} ไม้รวม Position/Pending ทั้งบัญชี · ${m.close_on_expiry?'ครบเวลาส่งปิด Position ของแอป':'ครบเวลาหยุดเปิดไม้ใหม่'}`:'';
  $('monitorEvents').innerHTML=table(['เวลา','เหตุการณ์','กลยุทธ์','ผล'],(m?.events||[]).map(e=>[esc(localTime(e.time)),esc(e.message),esc(names[e.strategy]||'—'),esc(e.result?`#${e.result.order} · ${e.result.message}`:e.lot?`${number(e.lot)} Lot`:'—')]));
  $('strategyExecution').innerHTML=table(['Strategy','สัญญาณ','การส่งคำสั่ง','ถืออยู่','ตัวอย่างที่เปิด','เหตุผล'],(m?.strategy_status||[]).map(s=>[esc(s.name),badge(s.signal),esc(executionLabels[s.status]||s.status),`${s.open_count} / 1`,s.samples,`<div class="strategy-reason">${esc(s.reason)}</div>`]));
}
$('monitorQuick').onclick=()=>{navigate('terminal');$('monitorStart').scrollIntoView({behavior:'smooth',block:'center'});};
$('monitorStart').onclick=async()=>{try{$('monitorStart').disabled=true;await api('/api/monitor/start','POST',{scope:state.scope,hours:Number($('monitorHours').value),close_on_expiry:$('monitorCloseExpiry').checked});notice('เริ่ม Auto Monitor แล้ว รอสัญญาณจากแท่งใหม่');await refresh();}catch(e){notice(e.message,true);}finally{renderMonitor();}};
$('monitorStop').onclick=async()=>{try{$('monitorStop').disabled=true;await api('/api/monitor/stop','POST');notice('หยุด Auto แล้ว Position เดิมยังคงอยู่');await refresh();}catch(e){notice(e.message,true);}finally{renderMonitor();}};
$('closeCancel').onclick=()=>$('closeDialog').close();
$('closeConfirm').onclick=async()=>{
  if(!closingPosition)return;const p=closingPosition;$('closeConfirm').disabled=true;$('closeCancel').disabled=true;
  try{
    const result=await api(`/api/positions/${p.ticket}/close`,'POST',{scope:p.scope,request_id:closeRequestId,identifier:p.identifier||p.ticket,volume:p.volume,side:p.type});
    notice(result.ok?`ส่งปิด Position #${p.ticket} แล้ว · ปริมาณที่ปิด ${number(result.volume)} Lot · ${result.message}`:`ปิดไม่สำเร็จ: ${result.message}`,!result.ok);
    $('closeDialog').close();closingPosition=null;await refresh();
  }catch(e){notice(e.message,true);$('closeDialog').close();closingPosition=null;}finally{$('closeConfirm').disabled=false;$('closeCancel').disabled=false;}
};
function filterParams(){return new URLSearchParams([...new FormData($('filters'))].filter(([,v])=>v));}
function renderJournal(){
  const select=$('filterStrategy'),current=select.value;
  const strategies=[...new Set(state.journal.map(r=>r.strategy))].sort();
  const signature=strategies.join('|');
  if(select.dataset.signature!==signature){select.innerHTML='<option value="">ทั้งหมด</option>'+strategies.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');select.value=current;select.dataset.signature=signature;}
  const f=Object.fromEntries(filterParams());
  filteredRows=state.journal.filter(r=>(!f.start||r.local_time.slice(0,10)>=f.start)&&(!f.end||r.local_time.slice(0,10)<=f.end)&&(!f.hour_start||r.local_time.slice(11,16)>=f.hour_start)&&(!f.hour_end||r.local_time.slice(11,16)<=f.hour_end)&&(!f.strategy||r.strategy===f.strategy)&&(!f.source||r.source===f.source)).slice().reverse();
  const pages=Math.max(1,Math.ceil(filteredRows.length/25));journalPage=Math.min(journalPage,pages-1);
  $('journalSummary').innerHTML=`${filteredRows.length} deals · กำไรสุทธิ <b class="${cls(filteredRows.reduce((s,r)=>s+r.net,0))}">${money(filteredRows.reduce((s,r)=>s+r.net,0))}</b>`;
  $('journalTable').innerHTML=table(['Deal / Position','วัน เวลา (ไทย)','Symbol','Side','Entry','Lot','Price','Strategy / EA','Magic','Costs','Net P&L'],filteredRows.slice(journalPage*25,(journalPage+1)*25).map(r=>[
    `${r.ticket} <small>/ ${r.position_id}</small>`,esc(r.local_time.slice(0,19).replace('T',' ')),esc(r.symbol),badge(r.side),esc(r.entry_label),number(r.volume),number(r.price),`${esc(r.strategy)}<br><small>${esc(r.source)}</small>`,esc(r.magic),number(r.commission+r.swap+r.fee),`<b class="${cls(r.net)}">${number(r.net)}</b>`]));
  $('pageInfo').textContent=`${journalPage+1} / ${pages}`;$('prevPage').disabled=journalPage===0;$('nextPage').disabled=journalPage>=pages-1;
  $('exportCsv').href=`/api/journal.csv?${filterParams()}`;
}
$('filters').addEventListener('input',()=>{journalPage=0;if(state?.connected)renderJournal();});
$('filters').addEventListener('reset',()=>setTimeout(()=>{journalPage=0;renderJournal();},0));
$('prevPage').onclick=()=>{journalPage--;renderJournal();};$('nextPage').onclick=()=>{journalPage++;renderJournal();};
function renderAnalytics(){
  $('signalCards').innerHTML=state.signals.map((s,i)=>`<article class="signal-card ${s.enabled?'':'disabled'}"><div class="signal-top"><span class="signal-icon">${['⌁','▱','∿','◎','↗','✧'][i]}</span>${badge(s.signal)}</div><h3>${esc(s.name)}</h3><p>${esc(s.reason)}</p><div class="caption">แท่งปิด ${localTime(s.time)} · ${s.key==='ai'?'H1':'M15'}</div>${s.signal!=='WAIT'?`${row('Entry',number(s.entry))}${row('SL / TP',`${number(s.sl)} / ${number(s.tp)}`)}`:''}<button class="button secondary use-signal" data-key="${s.key}" ${s.signal==='WAIT'?'disabled':''}>ใช้แผนนี้ใน Terminal ↗</button></article>`).join('');
  document.querySelectorAll('.use-signal').forEach(el=>el.onclick=()=>useSignal(el.dataset.key));
  $('strategyTable').innerHTML=table(['Strategy','ปิดครบ (ตัวอย่าง)','เปิดอยู่','Floating P&L','Win rate','Profit factor','Net ปิดแล้ว','Expectancy','P&L stddev','Drawdown','Loss streak'],state.analytics.strategies.map(s=>[esc(s.strategy),s.trades,s.open_count??0,`<span class="${cls(s.floating_pnl)}">${number(s.floating_pnl)}</span>`,s.trades?`${number(s.win_rate,1)}%`:'—',s.profit_factor===null?(s.gross_profit?'∞':'—'):number(s.profit_factor),`<b class="${cls(s.net)}">${number(s.net)}</b>`,s.trades?number(s.expectancy):'—',s.trades?number(s.stddev):'—',`<span class="negative">${number(s.drawdown)}</span>`,s.max_loss_streak]));
  const model=state.signals.find(s=>s.key==='ai')?.model||{},m=state.analytics.summary;
  $('modelStats').innerHTML=row('Validation',model.validated?'ผ่านเกณฑ์':'ยังไม่ผ่านเกณฑ์',model.validated?'positive':'negative')+row('Direction P(up)',model.up_probability===undefined?'—':`${number(model.up_probability*100,1)}%`)+row('Validation signals',model.validation_signals??'—')+row('Direction accuracy',model.validation_accuracy===undefined?'—':`${number(model.validation_accuracy*100,1)}%`)+row('เกณฑ์ขั้นต่ำ','20 signals · accuracy ≥ 55%');
  $('consistency').innerHTML=row('กำไรสุทธิที่ปิดครบ',money(m.net),cls(m.net))+row('คาดหวังต่อ Position',money(m.expectancy),cls(m.expectancy))+row('ส่วนเบี่ยงเบน P&L',money(m.stddev))+row('แพ้ติดต่อกันสูงสุด',`${m.max_loss_streak} positions`)+row('Equity drawdown',money(state.analytics.equity_drawdown),'negative');
}
function renderRisk(){ $('riskMonitor').innerHTML=riskRows(); }
function loadSettings(){
  const f=$('settingsForm'),s=state.settings;
  for(const [key,value] of Object.entries(s)){if(f.elements[key]&&key!=='magic_names')f.elements[key].value=value;}
  f.elements.magic_names.value=Object.entries(s.magic_names).map(([k,v])=>`${k}=${v}`).join('\n');
  $('strategyToggles').innerHTML=Object.entries(names).map(([k,v])=>`<label><input type="checkbox" name="enabled_strategies" value="${k}" ${s.enabled_strategies.includes(k)?'checked':''}>${esc(v)}</label>`).join('');
}
$('settingsForm').onsubmit=async(event)=>{
  event.preventDefault();const fd=new FormData(event.target),body={};
  for(const key of ['max_lot','risk_percent','daily_loss','max_orders','max_spread','daily_target','playbook_threshold','reward_ratio','asian_start','asian_end'])body[key]=Number(fd.get(key));
  body.enabled_strategies=fd.getAll('enabled_strategies');body.magic_names={};
  for(const line of String(fd.get('magic_names')).split('\n').filter(x=>x.trim())){const i=line.indexOf('=');if(i<1){notice('ชื่อ EA ใช้รูปแบบ 12345=ชื่อ EA ทีละบรรทัด',true);return;}body.magic_names[line.slice(0,i).trim()]=line.slice(i+1).trim();}
  const button=event.target.querySelector('button[type=submit]');button.disabled=true;
  try{state.settings=await api('/api/settings','PUT',body);invalidatePreview();notice('บันทึกค่าความเสี่ยงและกลยุทธ์แล้ว');await refresh();}catch(e){notice(e.message,true);}finally{button.disabled=false;}
};
async function toggleKill(){
  if(!state)return;const active=!state.settings.kill_switch;
  $('killTop').disabled=$('killMain').disabled=true;
  try{const result=await api('/api/kill','POST',{active});state.settings.kill_switch=result.active;invalidatePreview();notice((result.active?`หยุดคำสั่งใหม่แล้ว ยกเลิก Pending ${result.cancelled.length} รายการ`:'ปิด Kill Switch แล้ว')+(result.errors.length?` · ${result.errors.join('; ')}`:''),!!result.errors.length);await refresh();}catch(e){notice(e.message,true);}finally{$('killTop').disabled=$('killMain').disabled=false;}
}
$('killTop').onclick=toggleKill;$('killMain').onclick=toggleKill;
function invalidatePreview(){orderRevision++;previewData=null;previewBody=null;requestId=null;$('sendOrder').disabled=true;$('preview').innerHTML='<p>คำนวณและตรวจความเสี่ยงก่อนส่งคำสั่ง</p>';if(page==='terminal'&&state?.connected)priceChart();}
document.querySelectorAll('[data-lot-delta]').forEach(button=>button.addEventListener('click',()=>{
  const input=$('lotInput'),step=Number(state?.info?.volume_step)||.01,min=Number(state?.info?.volume_min)||step;
  const base=input.value===''?0:Number(input.value),delta=Number(button.dataset.lotDelta);
  const next=Math.max(min,Math.round(((Number.isFinite(base)?base:0)+delta)/step)*step);
  const digits=Math.max(2,(step.toFixed(8).replace(/0+$/,'').split('.')[1]||'').length);
  input.value=next.toFixed(digits);input.dispatchEvent(new Event('input',{bubbles:true}));
}));
$('lotAuto').addEventListener('click',()=>{$('lotInput').value='';$('lotInput').dispatchEvent(new Event('input',{bubbles:true}));});
function fillLevels(target='both',silent=false){
  try{
    const form=$('orderForm'),params={side:form.elements.side.value,kind:form.elements.kind.value,entry:form.elements.entry.value};
    if(target==='tp'&&form.elements.sl.value)params.slOverride=Number(form.elements.sl.value);
    const plan=TradeLevels.suggest(state,params);
    if(target!=='tp')form.elements.sl.value=numberRaw(plan.sl,state.info.trade_tick_size||state.info.point);
    if(target!=='sl')form.elements.tp.value=numberRaw(plan.tp,state.info.trade_tick_size||state.info.point);
    levelPlan=plan;
    const trend={UP:'ขาขึ้น',DOWN:'ขาลง',RANGE:'Sideways'}[plan.trend];
    const summary=$('levelSuggestion');summary.hidden=false;
    summary.innerHTML=`${row('แนวโน้ม M15',esc(trend),plan.trend==='UP'?'positive':plan.trend==='DOWN'?'negative':'gold')}${row('ATR14 · Wilder',number(plan.atr,state.info.digits))}${row('Entry อ้างอิง',number(plan.entry,state.info.digits))}<p>${target==='sl'?'เติมเฉพาะ SL · TP เดิมคงอยู่':target==='tp'?'เติมเฉพาะ TP โดยอิง SL เดิมหรือ SL ที่คำนวณ':'เติม SL และ TP แล้ว'} · ${esc(localTime(plan.time,{hour:'2-digit',minute:'2-digit',second:'2-digit'}))}</p><p>${plan.sl_overridden?'TP ใช้ระยะ SL เดิม':`SL ใช้ ${plan.multiplier} ATR หรือขอบราคา 20 แท่ง + buffer 0.25 ATR แล้วเลือกจุดที่ห่างกว่า`} · TP ใช้ R:R ที่ตั้งไว้ ${number(state.settings.reward_ratio)}</p>${plan.warnings.map(w=>`<p class="level-warning">${esc(w)}</p>`).join('')}<p>แผนตามกฎ EMA20/50 + ATR ไม่ใช่การรับรองผล · กดแนะนำใหม่เพื่อใช้ราคาล่าสุด</p>`;
    invalidatePreview();
  }catch(error){if(!silent)notice(error.message,true);else{$('levelSuggestion').hidden=false;$('levelSuggestion').textContent=error.message;}}
}
$('suggestLevels').onclick=()=>fillLevels();$('suggestSL').onclick=()=>fillLevels('sl');$('suggestTP').onclick=()=>fillLevels('tp');
$('orderForm').addEventListener('input',event=>{
  invalidatePreview();const form=$('orderForm'),kind=form.elements.kind.value;
  $('entryField').hidden=kind==='MARKET';form.elements.entry.required=kind!=='MARKET';
  if(['side','kind','entry'].includes(event.target.name)){
    const generated=levelPlan&&Number(form.elements.sl.value)===levelPlan.sl&&Number(form.elements.tp.value)===levelPlan.tp;
    levelPlan=null;$('levelSuggestion').hidden=true;
    if(generated){form.elements.sl.value='';form.elements.tp.value='';if(kind==='MARKET'||Number(form.elements.entry.value)>0)fillLevels('both',true);}
  }else if(['sl','tp'].includes(event.target.name)){
    levelPlan=null;$('levelSuggestion').hidden=true;
  }
  if(state?.connected&&page==='terminal')priceChart();
});
$('orderForm').onsubmit=async(event)=>{
  event.preventDefault();if(!state?.connected){notice('ยังไม่เชื่อมต่อข้อมูล',true);return;}
  const fd=new FormData(event.target),body={side:fd.get('side'),kind:fd.get('kind'),strategy:fd.get('strategy'),sl:Number(fd.get('sl')),tp:Number(fd.get('tp')),lot:fd.get('lot')?Number(fd.get('lot')):null,entry:fd.get('kind')==='MARKET'?null:Number(fd.get('entry'))};
  const button=event.target.querySelector('button[type=submit]');button.disabled=true;invalidatePreview();const revision=orderRevision;
  try{const p=await api('/api/preview','POST',body);if(revision!==orderRevision)return;previewData=p;previewBody={...body,lot:p.lot};requestId=crypto.randomUUID();previewAt=Date.now();
    $('preview').innerHTML=row('Lot',number(p.lot))+row('Entry',number(p.entry))+row('Risk ที่ SL',`${money(p.loss)} · ${number(p.risk_percent)}%`,'negative')+row('กำไรที่ TP',money(p.reward),'positive')+row('Risk : Reward',`1 : ${number(p.rr)}`)+row('Margin',money(p.margin))+(p.errors.length?`<ul class="preview-errors">${p.errors.map(e=>`<li>${esc(e)}</li>`).join('')}</ul>`:'<p class="positive">ผ่านค่าความเสี่ยง · ส่งได้ภายใน 30 วินาที</p>');
    $('sendOrder').disabled=!p.allowed;priceChart();
  }catch(e){notice(e.message,true);}finally{button.disabled=false;}
};
$('sendOrder').onclick=async()=>{
  if(!previewData?.allowed||!previewBody||sending)return;
  if(Date.now()-previewAt>30000){invalidatePreview();notice('Preview หมดอายุ กรุณาคำนวณใหม่',true);return;}
  sending=true;$('sendOrder').disabled=true;
  try{const result=await api('/api/orders','POST',{...previewBody,scope:previewData.scope,request_id:requestId});notice(result.ok?`ส่งคำสั่งแล้ว #${result.order} · ${result.message} · Lot ${number(result.volume)}`:`Broker ปฏิเสธ: ${result.message}`,!result.ok);invalidatePreview();await refresh();}catch(e){notice(e.message,true);invalidatePreview();}finally{sending=false;}
};
function useSignal(key){const s=state.signals.find(x=>x.key===key);if(!s||s.signal==='WAIT')return;navigate('terminal');levelPlan=null;$('levelSuggestion').hidden=true;const f=$('orderForm');f.elements.side.value=s.signal;f.elements.kind.value='MARKET';f.elements.strategy.value=s.key;const tick=state.info.trade_tick_size||state.info.point;f.elements.sl.value=numberRaw(s.sl,tick);f.elements.tp.value=numberRaw(s.tp,tick);f.elements.lot.value='';$('entryField').hidden=true;f.elements.entry.required=false;invalidatePreview();notice('ใส่ SL/TP จากสัญญาณแล้ว ตรวจราคาปัจจุบันและกดคำนวณก่อนส่ง');}
function numberRaw(value,tick){return (Math.round(value/tick)*tick).toFixed(state.info.digits);}

// Small, dependency-free charts with native canvas. No external quote provider.
function canvasSetup(id){
  const canvas=$(id),rect=canvas.getBoundingClientRect();if(!rect.width)return null;
  const dpr=window.devicePixelRatio||1;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.font='10px Inter, sans-serif';return {canvas,ctx,w:rect.width,h:rect.height};
}
function chartGrid(ctx,w,h,min,max,pad={left:8,right:65,top:18,bottom:28}){
  const width=w-pad.left-pad.right,height=h-pad.top-pad.bottom;
  const y=v=>pad.top+(max-v)/(max-min||1)*height;
  ctx.lineWidth=1;for(let i=0;i<=4;i++){const value=min+(max-min)*i/4,yy=y(value);ctx.strokeStyle='#243040';ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(w-pad.right,yy);ctx.stroke();ctx.fillStyle='#74849a';ctx.textAlign='left';ctx.fillText(number(value),w-pad.right+8,yy+3);}
  return {y,width,height,pad};
}
function lineChart(id,points,color){
  const chart=canvasSetup(id);if(!chart)return;const {ctx,w,h}=chart;
  if(points.length<2){ctx.fillStyle='#8794a8';ctx.fillText('ยังไม่มีจุดข้อมูลกราฟเพียงพอ',16,h/2);return;}
  const vals=points.map(p=>p.value),lo=Math.min(...vals),hi=Math.max(...vals),offset=Math.max((hi-lo)*.15,1);
  const {y,width,pad}=chartGrid(ctx,w,h,lo-offset,hi+offset),x=i=>pad.left+i/(points.length-1)*width;
  const gradient=ctx.createLinearGradient(0,0,0,h);gradient.addColorStop(0,color+'30');gradient.addColorStop(1,color+'00');
  ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(x(i),y(p.value)):ctx.moveTo(x(i),y(p.value)));ctx.lineTo(x(points.length-1),h-pad.bottom);ctx.lineTo(x(0),h-pad.bottom);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();
  ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(x(i),y(p.value)):ctx.moveTo(x(i),y(p.value)));ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();
  ctx.fillStyle='#74849a';ctx.textAlign='left';ctx.fillText(localTime(points[0].time,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}),pad.left,h-5);ctx.textAlign='right';ctx.fillText(localTime(points.at(-1).time,{hour:'2-digit',minute:'2-digit'}),w-pad.right,h-5);
}
function barChart(id,points){
  const chart=canvasSetup(id);if(!chart)return;const {ctx,w,h}=chart;if(!points.length)return;
  const values=points.map(p=>p.net),lo=Math.min(0,...values),hi=Math.max(1,...values),range=hi-lo;
  const {y,width,pad}=chartGrid(ctx,w,h,lo-range*.1,hi+range*.1),step=width/points.length;
  points.forEach((p,i)=>{const x=pad.left+step*i+step*.25;ctx.fillStyle=p.net>=0?'#5ed7ad':'#f5808a';ctx.fillRect(x,Math.min(y(0),y(p.net)),step*.5,Math.max(1,Math.abs(y(p.net)-y(0))));if(i%Math.ceil(points.length/7)===0){ctx.fillStyle='#74849a';ctx.textAlign='center';ctx.fillText(p.day.slice(5),x+step*.25,h-5);}});
}
let candleHover=null;
function priceChart(){
  const chart=canvasSetup('priceChart');if(!chart||!state?.bars?.length)return;const {canvas,ctx,w,h}=chart;
  const displayedPlan=previewData||(levelPlan?{entry:levelPlan.entry,sl:Number($('orderForm').elements.sl.value),tp:Number($('orderForm').elements.tp.value)}:null);
  const bars=state.bars.slice(-85),levels=displayedPlan?[['ENTRY',displayedPlan.entry,'#e5bb72'],['SL',displayedPlan.sl,'#f5808a'],['TP',displayedPlan.tp,'#5ed7ad']].filter(l=>Number.isFinite(l[1])&&l[1]>0):[];
  const lo=Math.min(...bars.map(b=>b.low),...levels.map(l=>l[1])),hi=Math.max(...bars.map(b=>b.high),...levels.map(l=>l[1])),offset=(hi-lo)*.1||1;
  const {y,width,pad}=chartGrid(ctx,w,h,lo-offset,hi+offset),step=width/bars.length,x=i=>pad.left+(i+.5)*step;
  bars.forEach((b,i)=>{const color=b.close>=b.open?'#5ed7ad':'#f5808a';ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x(i),y(b.high));ctx.lineTo(x(i),y(b.low));ctx.stroke();ctx.fillRect(x(i)-step*.29,Math.min(y(b.open),y(b.close)),step*.58,Math.max(1,Math.abs(y(b.close)-y(b.open))));if(i%17===0){ctx.fillStyle='#74849a';ctx.textAlign='center';ctx.fillText(localTime(b.time,{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}),x(i),h-5);}});
  const drawLevel=(name,value,color)=>{ctx.setLineDash([4,4]);ctx.strokeStyle=color;ctx.beginPath();ctx.moveTo(pad.left,y(value));ctx.lineTo(w-pad.right,y(value));ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.textAlign='left';if(name==='BID')ctx.fillText(number(value),w-pad.right+8,y(value)+3);else ctx.fillText(`${name} ${number(value)}`,pad.left+8,y(value)-5);};
  levels.forEach(l=>drawLevel(...l));
  drawLevel('BID',state.tick.bid,'#8cafff');
  const markers=state.journal.filter(d=>d.symbol===state.symbol&&d.time>=bars[0].time&&d.time<bars.at(-1).time+900).map(d=>({time:d.time,price:d.price,buy:d.type===0,exit:d.entry!==0}));
  for(const m of markers){const i=bars.findIndex((b,i)=>m.time>=b.time&&m.time<(bars[i+1]?.time??b.time+900));if(i<0)continue;const xx=x(i),yy=y(m.price);ctx.fillStyle=m.exit?'#e5bb72':m.buy?'#5ed7ad':'#f5808a';ctx.beginPath();ctx.moveTo(xx,yy+(m.buy?-7:7));ctx.lineTo(xx-4,yy+(m.buy?1:-1));ctx.lineTo(xx+4,yy+(m.buy?1:-1));ctx.closePath();ctx.fill();}
  // Show current closed-candle strategy markers, distinct from journal fills.
  state.signals.filter(s=>s.signal!=='WAIT'&&s.time>=bars[0].time&&s.time<=bars.at(-1).time).forEach(s=>{const i=bars.findIndex(b=>b.time===s.time);if(i<0)return;ctx.strokeStyle='#e5bb72';ctx.beginPath();ctx.arc(x(i),y(s.entry),6,0,Math.PI*2);ctx.stroke();});
  candleHover={bars,x,step,pad};canvas.onmousemove=event=>{const r=canvas.getBoundingClientRect(),i=Math.max(0,Math.min(bars.length-1,Math.floor((event.clientX-r.left-pad.left)/step))),b=bars[i];$('chartHover').textContent=`${localTime(b.time)} · O ${number(b.open)} · H ${number(b.high)} · L ${number(b.low)} · C ${number(b.close)}`;};
}
async function refresh(){
  if(polling)return;polling=true;
  try{state=await api('/api/state');render();}catch(e){if(state)state.connected=false;$('errorBanner').hidden=false;$('errorBanner').textContent=`ติดต่อแอปไม่ได้: ${e.message}`;$('sendOrder').disabled=true;$('connection').textContent='Disconnected';$('statusDot').classList.remove('online');}finally{polling=false;}
}
window.addEventListener('resize',()=>{if(state?.connected)render();});
setInterval(()=>{$('clock').textContent=localTime(Date.now()/1000,{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit'});if(previewData&&Date.now()-previewAt>30000){$('sendOrder').disabled=true;$('preview').querySelector('p')?.replaceChildren(document.createTextNode('Preview หมดอายุ กรุณาคำนวณใหม่'));}},1000);
refresh();setInterval(refresh,3000);
