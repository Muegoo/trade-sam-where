'use strict';
(() => {
  const $ = id => document.getElementById(id);
  function clearProviderKeys(){for(const id of ['researchApiKey','researchForwardKey'])if($(id))$(id).value='';}
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const strategies = [['smc','SMC Sweep + BOS'],['supply','Supply / Demand'],['ema','EMA Pullback'],['bollinger','Bollinger Pullback'],['asian','Asian Breakout'],['ai','AI Playbook']];
  let sessionToken = null, datasets = [], jobs = [], selectedHash = '', selectedJob = null, selectedResult = null, selectedTrace = null, loading = false, loaded = false, statusPromise = null, pollTimer = null, forwardPromise = null, forwardResultPromise = null, forwardStatus = null, forwardResult = null, collectedHash = '', collectionAvailable = false;
  const setStatus = (id, message, kind='') => { const el=$(id); if(el){el.textContent=message;el.className=`research-status${kind?` ${kind}`:''}`;}if(kind==='error')clearProviderKeys(); };
  function apiError(data, fallback){
    const detail=data?.detail;
    if(Array.isArray(detail))return detail.map(item=>`${Array.isArray(item.loc)?item.loc.join('.'):''}: ${item.msg||'invalid value'}`).join('; ');
    return typeof detail==='string'?detail:fallback;
  }
  async function request(url, options={}){
    const response=await fetch(url,{...options,headers:{'X-Trade-Token':sessionToken,...(options.headers||{})}});
    const raw=await response.text();let data;try{data=JSON.parse(raw);}catch{data={detail:raw};}
    if(!response.ok)throw new Error(apiError(data,`คำขอไม่สำเร็จ (${response.status})`));
    return data;
  }
  const chosenDataset=()=>datasets.find(item=>item.dataset_hash===selectedHash)||null;
  const weekdayInputs=()=>[...document.querySelectorAll('[name="researchRolloverWeekday"]')];
  function collectCosts(){
    const fields=[['researchCommission','commission_per_lot_side',0],['researchFee','fee_per_lot_side',0],['researchSlippage','slippage_points',0],['researchSwapLong','swap_long_per_lot_day',null],['researchSwapShort','swap_short_per_lot_day',null]];
    const costs={};
    for(const [id,key,minimum] of fields){const input=$(id),raw=input.value.trim(),value=Number(raw);if(!raw||!Number.isFinite(value)||(minimum!==null&&value<minimum)){fieldError(input,'กรอกต้นทุนให้ครบและใช้ตัวเลขที่ถูกต้อง');return null;}costs[key]=value;}
    const hour=$('researchRolloverHour'),hourValue=Number(hour.value);if(hour.value.trim()===''||!Number.isInteger(hourValue)||hourValue<0||hourValue>23){fieldError(hour,'ระบุ rollover hour UTC ตั้งแต่ 0 ถึง 23');return null;}costs.rollover_hour_utc=hourValue;
    const days=weekdayInputs().filter(input=>input.checked).map(input=>Number(input.value)),none=$('researchNoRolloverDays');
    if((!days.length&&!none.checked)||(days.length&&none.checked)){fieldError(none,'เลือกวัน rollover อย่างน้อยหนึ่งวัน หรือยืนยันว่าไม่มี');return null;}
    costs.rollover_weekdays=days;
    const triple=$('researchTripleSwap').value;costs.triple_swap_weekday=triple===''?null:Number(triple);
    const marginInput=$('researchMargin'),margin=Number(marginInput.value);if(!marginInput.value.trim()||!Number.isFinite(margin)||margin<=0){fieldError(marginInput,'ระบุ margin per lot มากกว่า 0');return null;}
    return {costs,margin_per_lot:margin};
  }
  function utcEpoch(id,label){
    const input=$(id),value=input.value;if(!value){fieldError(input,`ระบุ${label} (UTC)`);return null;}
    const epoch=Math.floor(Date.parse(value.length===16?`${value}:00Z`:`${value}Z`)/1000);
    if(!Number.isSafeInteger(epoch)||epoch<=0){fieldError(input,`ระบุ${label}เป็นเวลา UTC ที่ถูกต้อง`);return null;}return epoch;
  }
  function dateUTC(epoch){
    const d=new Date(Number(epoch)*1000);if(!Number.isFinite(d.getTime()))return '—';
    return new Intl.DateTimeFormat('th-TH',{timeZone:'UTC',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(d)+' UTC';
  }
  function populateDatasets(){
    const select=$('researchDatasetSelect'), previous=selectedHash || select.value;
    select.innerHTML=datasets.length?datasets.map(d=>`<option value="${esc(d.dataset_hash)}">${esc(d.source)} · ${esc(d.instrument?.symbol)} · ${esc(d.dataset_hash.slice(0,12))}</option>`).join(''):'<option value="">ยังไม่มี dataset</option>';
    if(datasets.some(d=>d.dataset_hash===previous))selectedHash=previous;
    else selectedHash=datasets[0]?.dataset_hash||'';
    select.value=selectedHash;select.disabled=!datasets.length;
    updateDatasetDetails();$('researchRun').disabled=!selectedHash||loading;
  }
  function updateDatasetDetails(){
    const d=chosenDataset();$('researchDatasetDetails').textContent=d?`${d.source} · ${d.instrument?.symbol} · ${Object.entries(d.counts?.bars||{}).map(([k,v])=>`${k} ${v}`).join(' / ')} · quotes ${d.counts?.quotes??'—'} · ${dateUTC(d.first_quote_time)} ถึง ${dateUTC(d.last_quote_time)}`:'นำเข้าไฟล์เพื่อดูแหล่งข้อมูล ช่วงเวลา และจำนวนแท่ง';
    $('researchRun').disabled=!d||loading;
    $('researchDownloadCollectedDataset').disabled=!d;
  }
  function renderJobs(){
    const target=$('researchJobs');
    if(!jobs.length){target.innerHTML='<div class="empty">ยังไม่มีงานวิจัย</div>';return;}
    target.innerHTML=`<table><thead><tr><th>ชนิด</th><th>สถานะ</th><th>Dataset</th><th>รายละเอียด</th></tr></thead><tbody>${jobs.map(j=>`<tr><td>${esc(j.kind)}</td><td><span class="badge ${j.status==='completed'?'positive':j.status==='failed'?'negative':'muted'}">${esc(j.status)}</span></td><td>${esc(j.dataset_hash?.slice(0,12)||'—')}</td><td><button type="button" class="text-button research-job-open" data-job="${esc(j.id)}" ${j.status==='completed'?'':'disabled'}>ดูผล</button>${j.error?`<p class="research-error">${esc(j.error)}</p>`:''}</td></tr>`).join('')}</tbody></table>`;
  }
  function renderCollectionAvailability(collection={}){
    const available=collection.available===true,badge=$('researchCollectionBadge');collectionAvailable=available;
    badge.textContent=available?'MT5 พร้อมใช้งาน':'MT5 ยังไม่พร้อม';badge.className=`badge ${available?'positive':'muted'}`;
    $('researchCollectionAvailability').textContent=available?'เชื่อมต่อ MT5 Demo แล้ว · เลือกช่วงย้อนหลังไม่เกิน 31 วัน':collection.reason||'ต้องเชื่อมต่อ MT5 Demo เพื่อดึงประวัติจริง';
    $('researchCollect').disabled=!available||loading;
  }
  async function loadStatus(force=false){
    if(statusPromise)return statusPromise;
    if(loaded&&!force)return;
    statusPromise=(async()=>{
      const data=await request('/api/research/status');datasets=data.datasets||[];jobs=data.jobs||[];loaded=true;
      $('researchProviderStatus').textContent=`Advisor: ${data.provider_status||'—'}`;
      renderCollectionAvailability(data.collection||{});populateDatasets();renderJobs();renderForwardStatus(data.forward||{});await refreshForwardResult();
    })().finally(()=>{statusPromise=null;});
    return statusPromise;
  }
  function kindChanged(){
    const kind=$('researchKind').value,replay=kind==='replay',evaluate=kind==='evaluate',compare=kind==='compare',timed=kind==='shadow'||kind==='advisor',advisor=kind==='advisor';
    $('researchProposalsWrap').hidden=!(replay||kind==='shadow');$('researchProposalsFile').required=replay||kind==='shadow';
    $('researchStrategiesWrap').hidden=!evaluate;$('researchCompareWrap').hidden=!compare;$('researchWalkForwardWrap').hidden=!(evaluate||compare);$('researchWalkForwardDetails').open=compare;$('researchBasics').hidden=timed;$('researchCalendar').hidden=false;
    $('researchAsOfWrap').hidden=!timed;$('researchAdvisorOptions').hidden=!(advisor||compare);$('researchSettingsNote').hidden=timed;
    $('researchProviderHelp').textContent=compare?'A/B ทำงานได้โดยไม่ใช้ provider; C/D จะเป็น unavailable หาก provider ปิด. เมื่อเปิดใช้ ระบบอาจเรียก analyst และ challenger ตามงบสูงสุดที่กำหนด.': 'เมื่อเลือก provider และส่งงาน ระบบจะส่ง snapshot, ข่าวที่นำเข้า และ demo memory ให้ provider ที่เลือก';
    $('researchChallenger').closest('label').hidden=compare;
    $('researchKindHelp').textContent=replay?'Replay จำลองข้อเสนอด้วยข้อมูลและค่า Risk ที่บันทึกเมื่อเริ่มงาน':evaluate?'Evaluate แบ่งข้อมูลตามเวลาเป็น train, validation และ test folds':compare?'เปรียบเทียบ variants ด้วย strategy เดียวและ folds ชุดเดียวกัน':kind==='shadow'?'Shadow ตรวจข้อเสนอ ณ snapshot ที่เลือก โดยไม่ส่งคำสั่ง':'Advisor สร้างข้อเสนอเพื่อประเมินใน shadow เท่านั้น';
    $('researchCalendar').hidden=!(replay||evaluate||compare||kind==='shadow'||advisor);
    if(timed){const d=chosenDataset();if(d?.last_quote_time){const iso=new Date(Number(d.last_quote_time)*1000).toISOString().slice(0,19);$('researchAsOf').value=iso;}}
  }
  function fieldError(field,message){clearProviderKeys();field.setCustomValidity(message);field.reportValidity();field.addEventListener('input',()=>field.setCustomValidity(''),{once:true});}
  async function submitJob(event){
    event.preventDefault();if(loading)return;
    const dataset=chosenDataset();if(!dataset){setStatus('researchJobStatus','กรุณานำเข้าและเลือก dataset ก่อนเริ่มงาน','error');return;}
    const kind=$('researchKind').value,body={dataset_hash:dataset.dataset_hash,kind,options:{}};
    if(kind==='replay'||kind==='evaluate'||kind==='compare'){
      const balance=Number($('researchInitialBalance').value),latency=Number($('researchLatency').value);
      if(!Number.isFinite(balance)||balance<=0){fieldError($('researchInitialBalance'),'กรุณาระบุค่า balance มากกว่า 0');return;}
      if(!Number.isInteger(latency)||latency<0){fieldError($('researchLatency'),'Latency ต้องเป็นจำนวนเต็มตั้งแต่ 0 ขึ้นไป');return;}
      body.options.initial_balance=balance;body.options.latency_seconds=latency;
    }
    if(kind==='replay'||kind==='evaluate'||kind==='compare'||kind==='shadow'||kind==='advisor'){
      body.options.calendar_before_minutes=Number($('researchCalendarBefore').value);body.options.calendar_after_minutes=Number($('researchCalendarAfter').value);
    }
    if(kind==='replay'||kind==='shadow'){
      const file=$('researchProposalsFile').files[0];if(!file){setStatus('researchJobStatus','เลือกไฟล์ proposals JSON ก่อนเริ่มงาน','error');$('researchProposalsFile').focus();return;}
      if(file.size>2*1024*1024){setStatus('researchJobStatus','ไฟล์ proposals มีขนาดเกิน 2 MiB','error');return;}
      body.proposals_json=await file.text();
    }
    if(kind==='evaluate'){
      body.options.strategies=[...$('researchStrategies').querySelectorAll('input:checked')].map(input=>input.value);
      if(!body.options.strategies.length){setStatus('researchJobStatus','เลือกอย่างน้อยหนึ่งกลยุทธ์','error');return;}
      for(const [id,key] of [['researchTrain','train_bars'],['researchValidation','validation_bars'],['researchTest','test_bars'],['researchPurge','purge_bars'],['researchEmbargo','embargo_bars']]){const input=$(id);if(input)body.options[key]=Number(input.value);}
    }
    if(kind==='compare'){
      body.options.strategy=$('researchCompareStrategy').value;
      const budget=Number($('researchCompareBudget').value);
      if(!Number.isInteger(budget)||budget<1||budget>20){fieldError($('researchCompareBudget'),'ระบุงบ LLM decisions ตั้งแต่ 1 ถึง 20');return;}
      body.options.max_llm_decisions=budget;
      for(const [id,key] of [['researchTrain','train_bars'],['researchValidation','validation_bars'],['researchTest','test_bars'],['researchPurge','purge_bars'],['researchEmbargo','embargo_bars']])body.options[key]=Number($(id).value);
    }
    if(kind==='shadow'||kind==='advisor'){
      const value=$('researchAsOf').value;if(!value){setStatus('researchJobStatus','ระบุเวลาประเมินก่อนเริ่มงาน','error');$('researchAsOf').focus();return;}
      const utcValue=value.length===16?`${value}:00Z`:`${value}Z`,epoch=Math.floor(new Date(utcValue).getTime()/1000);if(!Number.isSafeInteger(epoch)||epoch<=0){setStatus('researchJobStatus','เวลา UTC ไม่ถูกต้อง','error');return;}body.options.as_of=epoch;
    }
    if(kind==='advisor'||kind==='compare'){
      const provider=$('researchProvider').value;body.provider=provider;
      if(provider!=='disabled'){
        const model=$('researchModel').value.trim(),key=$('researchApiKey').value;
        if(!model){fieldError($('researchModel'),'ระบุ model ที่ตั้งค่าไว้กับ provider');return;}
        if(!key.trim()){fieldError($('researchApiKey'),'ระบุ API key ของ provider ที่เลือก');return;}
        body.model=model;body.api_key=key;
      }
      if(kind==='advisor'){
        body.options.challenger=$('researchChallenger').checked;body.options.timeout_seconds=Number($('researchTimeout').value);
        const tokens=$('researchTokens').value;if(tokens)body.options.max_output_tokens=Number(tokens);
      }
    }
    if(kind==='evaluate'||kind==='compare')body.options.calendar_before_minutes=Number($('researchCalendarBefore').value),body.options.calendar_after_minutes=Number($('researchCalendarAfter').value);
    loading=true;$('researchRun').disabled=true;setStatus('researchJobStatus','กำลังส่งงานวิจัย…');
    const keyField=$('researchApiKey');let requestBody=JSON.stringify(body);
    if(kind==='advisor'||kind==='compare'){$('researchApiKey').value='';delete body.api_key;}
    try{
      const created=await request('/api/research/jobs',{method:'POST',headers:{'Content-Type':'application/json'},body:requestBody});requestBody='';
      setStatus('researchJobStatus',`เริ่มงานแล้ว · ${created.kind} · ${created.id}`);
      let finishedJob=created;if(created.status==='queued')finishedJob=await pollJob(created.id);
      await loadStatus(true);
      if(finishedJob?.status==='completed'){selectedJob=created.id;await showResult(created.id);}
    }catch(error){setStatus('researchJobStatus',error.message||'เริ่มงานไม่สำเร็จ แก้ข้อมูลแล้วลองอีกครั้ง','error');}
    finally{requestBody='';if(keyField)keyField.value='';loading=false;updateDatasetDetails();}
  }
  async function pollJob(id){
    clearInterval(pollTimer);let attempts=0;
    while(attempts++<240){
      const job=await request(`/api/research/jobs/${encodeURIComponent(id)}`);jobs=jobs.map(item=>item.id===id?job:item);if(!jobs.some(item=>item.id===id))jobs.unshift(job);renderJobs();
      if(job.status==='completed'){setStatus('researchJobStatus',`งานเสร็จแล้ว · ${job.kind}`,'success');return job;}
      if(job.status==='failed'){setStatus('researchJobStatus',`งานล้มเหลว: ${job.error||'ไม่ทราบสาเหตุ'} · แก้ข้อมูลแล้วเริ่มงานใหม่ได้`,'error');throw new Error(job.error||'งานวิจัยล้มเหลว');}
      setStatus('researchJobStatus',`กำลังทำงาน · ${job.status} · ${job.kind}`);await new Promise(resolve=>setTimeout(resolve,700));
    }
    setStatus('researchJobStatus','งานยังไม่เสร็จ สถานะจะอัปเดตเมื่อรีเฟรช','error');return null;
  }
  const fmt=(value,digits=2)=>value===null||value===undefined?'—':Number.isFinite(Number(value))?Number(value).toLocaleString('en-US',{maximumFractionDigits:digits}):'—';
  const metric=(label,value,color='')=>`<div class="row"><span>${esc(label)}</span><b class="${color}">${value}</b></div>`;
  function replayMarkup(result){
    const m=result.metrics||{}, pnl=Number(m.net_pnl), rejected=result.rejected||[];
    return `<h3>Replay · ผลการจำลอง</h3><div class="research-metrics">${metric('Closed trades',fmt(m.closed_trades,0))}${metric('Net P&L',fmt(m.net_pnl),pnl>0?'positive':pnl<0?'negative':'')}${metric('Profit factor',fmt(m.profit_factor))}${metric('Net expectancy (R)',fmt(m.net_expectancy_r))}${metric('Max equity drawdown',fmt(m.max_equity_drawdown))}${metric('Risk breaches',fmt(result.risk_breach_count,0),result.risk_breach_count?'negative':'')}${metric('Rejected proposals',fmt(rejected.length,0))}</div>${hashDetails(result)}`;
  }
  function evaluateMarkup(result){
    const rows=Object.entries(result.strategies||{}).map(([name,item])=>`<tr><td>${esc(name)}</td><td>${esc(item.timeframe||'—')}</td><td>${esc(item.status||'—')}</td><td>${fmt(item.complete_folds,0)}</td><td>${item.status==='insufficient_data'?`ข้อมูลไม่พอ · ${fmt(item.bars_available,0)} / ${fmt(item.bars_required,0)} bars`:`P&L ${fmt(item.summary?.net_pnl_sum)} · trades ${fmt(item.summary?.closed_trades,0)} · worst fold DD ${fmt(item.summary?.worst_fold_drawdown)}`}</td></tr>`).join('');
    return `<h3>Evaluate · ผลตามกลยุทธ์</h3><p class="caption">Drawdown แสดงแยกตาม fold และไม่ใช่ continuous maximum drawdown</p><div class="table-wrap"><table><thead><tr><th>กลยุทธ์</th><th>Timeframe</th><th>สถานะ</th><th>Folds</th><th>ผลสรุปจริง</th></tr></thead><tbody>${rows||'<tr><td colspan="5">ไม่มีผลสำหรับกลยุทธ์ที่เลือก</td></tr>'}</tbody></table></div>${hashDetails(result)}`;
  }
  const compareNames={A_rules:'A · Rules',B_calendar:'B · Calendar',C_analyst_agreement:'C · Analyst agreement',D_challenger_approved:'D · Challenger approved'};
  const statusText={completed:'เสร็จแล้ว',unavailable:'ประเมินไม่ได้',no_data:'ไม่มีข้อมูลที่ใช้ได้',no_candidates:'ไม่มี candidate',data_unavailable:'ข้อมูลไม่พร้อม'};
  const reasonText={provider_not_configured:'ปิด provider ไว้',no_advice_executed:'ไม่มี advice ที่ใช้ได้',no_candidates:'ไม่มี rule candidate',no_data:'ไม่มีข้อมูลที่ใช้ได้'};
  function metricText(variant,key){const value=variant?.metrics?.[key];return variant?.status==='completed'&&value!==null&&value!==undefined?fmt(value):'—';}
  function compareMarkup(result){
    const rows=Object.entries(compareNames).map(([key,label])=>{const item=result.variants?.[key]||{};return `<tr><th>${label}</th><td>${esc(statusText[item.status]||item.status||'—')}</td><td>${metricText(item,'net_pnl_sum')}</td><td>${metricText(item,'closed_trades')}</td><td>${metricText(item,'expectancy_per_closed_trade')}</td><td>${metricText(item,'worst_fold_drawdown')}</td><td>${metricText(item,'valid_folds')}</td><td>${esc(item.reason?reasonText[item.reason]||item.reason:'—')}</td></tr>`;}).join('');
    const paired=Object.entries(result.paired_fold_pnl_delta_vs_A||{}).map(([key,item])=>`<tr><th>${esc(compareNames[key]||key)} − A</th><td>${esc(statusText[item.status]||item.status||'—')}</td><td>${fmt(item.mean)}</td><td>${Array.isArray(item.ci95)?`${fmt(item.ci95[0])} ถึง ${fmt(item.ci95[1])}`:'—'}</td><td>${fmt(item.n,0)}</td></tr>`).join('');
    const folds=(result.folds||[]).map(fold=>`<tr><td>${fmt(fold.fold,0)}</td><td>${esc(statusText[fold.status]||fold.status||'—')}${fold.unavailable_reason?` · ${esc(fold.unavailable_reason)}`:''}</td>${Object.keys(compareNames).map(key=>`<td>${metricText({status:fold.variants?.[key]?'completed':'unavailable',metrics:fold.variants?.[key]},'net_pnl')}</td>`).join('')}</tr>`).join('');
    const calendarNote=result.calendar?.zero_window_note?`<p class="caption">${esc(result.calendar.zero_window_note)} · ปรับ calendar windows หากต้องการให้ B กรอง event</p>`:'';
    return `<h3>A/B comparison · ${esc(result.status||'—')} · provider ${esc(result.provider_status||'not_configured')}</h3><p class="caption">Strategy เดียวกันและ folds ชุดเดียวกัน · drawdown แยกตาม fold ไม่ใช่ continuous portfolio drawdown · folds ที่ข้อมูลไม่พร้อมถูกตัดออกจากช่วงความเชื่อมั่น ไม่แทนด้วยศูนย์</p>${calendarNote}<div class="table-wrap"><table><thead><tr><th>Variant</th><th>สถานะ</th><th>Net P&amp;L</th><th>Trades</th><th>Expectancy / trade</th><th>Worst fold DD</th><th>Valid folds</th><th>เหตุผลเมื่อไม่มีผล</th></tr></thead><tbody>${rows}</tbody></table></div><h4>Paired fold P&amp;L delta เทียบ A</h4><div class="table-wrap"><table><thead><tr><th>Variant</th><th>สถานะ CI</th><th>Mean delta</th><th>95% CI</th><th>Folds</th></tr></thead><tbody>${paired||'<tr><td colspan="5">ไม่มีคู่ fold ที่คำนวณได้</td></tr>'}</tbody></table></div><details class="research-advanced"><summary>ผลแยกตาม fold</summary><div class="table-wrap"><table><thead><tr><th>Fold</th><th>สถานะ</th>${Object.values(compareNames).map(name=>`<th>${name}</th>`).join('')}</tr></thead><tbody>${folds||'<tr><td colspan="6">ไม่มี fold</td></tr>'}</tbody></table></div></details><p class="caption">LLM decisions ${fmt(result.llm_decisions,0)} · provider calls ${fmt(result.provider_calls,0)} · ไม่จัดอันดับผู้ชนะอัตโนมัติ</p>${hashDetails(result)}`;
  }
  function collectionMarkup(result){
    const report=result.collection||{},gaps=report.quote_gaps||{},barGaps=report.bar_gaps||{},instrument=result.dataset?.instrument||{};
    return `<h3>MT5 historical collection · ${esc(instrument.symbol||'—')}</h3><p class="caption">Dataset ${esc(result.dataset_hash||'—')} · ข้อมูล session-only ดาวน์โหลดไว้ก่อนปิดหรือเริ่มแอปใหม่</p><div class="research-metrics">${metric('ช่วงที่ร้องขอ',`${dateUTC(report.requested_range?.start)} ถึง ${dateUTC(report.requested_range?.end)}`)}${metric('เก็บข้อมูลเมื่อ',dateUTC(report.fetched_at))}${metric('Raw ticks',fmt(report.raw_tick_count,0))}${metric('Invalid quotes',fmt(report.invalid_quote_count,0))}${metric('ซ้ำในวินาทีเดียว',fmt(report.same_second_deduplicated_count,0))}${metric('Quotes หลัง sampling',fmt(report.sampled_quote_count,0))}${metric('Gap ภายใน quotes',`${fmt(gaps.internal_count,0)} · สูงสุด ${fmt(gaps.maximum_gap_seconds,0)} วินาที`)}${metric('ขอบช่วงข้อมูล',`ก่อนเริ่ม ${fmt(gaps.first_quote_after_start_seconds,0)}s · ก่อนจบ ${fmt(gaps.last_quote_before_end_seconds,0)}s`)}${metric('จุด/สัญญา/volume',`${fmt(instrument.point)} · ${fmt(instrument.contract_size)} · ${fmt(instrument.volume_min)}–${fmt(instrument.volume_max)} step ${fmt(instrument.volume_step)}`)}</div><div class="table-wrap"><table><thead><tr><th>Timeframe</th><th>แท่ง</th><th>Gap</th><th>Availability</th></tr></thead><tbody>${['M15','H1'].map(frame=>`<tr><td>${frame}</td><td>${fmt(result.dataset?.bars?.[frame]?.length,0)}</td><td>${fmt(barGaps[frame]?.count,0)}</td><td>${esc(barGaps[frame]?.availability_assumption||'—')}</td></tr>`).join('')}</tbody></table></div><p class="caption">${esc(report.instrument_specification||'')}. ${esc(report.cost_assumptions||'')}. ${esc(report.bar_availability||'')}</p>${gaps.boundary?.length?`<p class="caption">Gap เกินเกณฑ์: ${gaps.boundary.map(item=>`${esc(item.boundary)} ${fmt(item.seconds,0)}s`).join(' · ')}</p>`:''}${hashDetails(result)}`;
  }
  function shadowMarkup(result,kind){
    const shadow=result.shadow||result, counts=shadow.counts||{};
    const proposal=result.proposal||result.analyst_proposal||shadow.accepted?.[0]||shadow.rejected?.[0]||{};
    const reasons=[...(proposal.reason?[proposal.reason]:[]),...(shadow.rejected||[]).flatMap(item=>item.errors||[]),...(shadow.accepted||[]).flatMap(item=>item.no_trade_reasons||[])];
    const availability=result.availability||{}, evidence=Object.entries(availability).map(([key,value])=>`${key}: ${value?'available':'missing'}`).join(' · ');
    const calendar=proposal.calendar?proposal.calendar.blocked?'blocked':'clear':'not reported';
    const action=kind==='shadow'?(proposal.status||proposal.action):(proposal.action||proposal.status);
    return `<h3>${kind==='advisor'?'Advisor':'Shadow'} · ${esc(result.provider_status||'—')}</h3><div class="research-metrics">${metric('Action',esc(action||'—'))}${metric('Reason',esc(proposal.reason||reasons.join('; ')||'ไม่มีเหตุผลแนบ'))}${metric('Invalid proposals',fmt(counts.invalid,0),counts.invalid?'negative':'')}${metric('Calendar / veto',esc(calendar))}${metric('Calendar / veto reasons',esc(reasons.join('; ')||'ไม่มี'))}${metric('Evidence',esc(evidence||`${proposal.evidence_ids?.length??0} referenced · availability not reported`))}${metric('Executed orders','0')}${metric('Latency',result.latency_ms===undefined?'unavailable':`${fmt(result.latency_ms,0)} ms`)}${metric('Token usage',result.token_usage?esc(JSON.stringify(result.token_usage)):'unavailable')}${metric('Cost',result.cost===null||result.cost===undefined?'unavailable':esc(String(result.cost)))}</div>${hashDetails(result)}`;
  }
  function hashDetails(result){return `<details class="research-provenance"><summary>Provenance hashes</summary><dl>${Object.entries(result).filter(([key,value])=>key.endsWith('_hash')&&typeof value==='string').map(([key,value])=>`<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')||'<div><dt>Hash</dt><dd>ไม่มีข้อมูล</dd></div>'}</dl></details>`;}
  function renderForwardStatus(status={}){
    forwardStatus=status;
    const active=status.active===true,ready=status.status!=='unavailable';
    const badge=$('researchForwardBadge');badge.textContent=status.status||'ยังไม่เริ่ม';badge.className=`badge ${active?'positive':status.error?'negative':'muted'}`;
    $('researchForwardMode').textContent=status.mode?`${status.mode==='simulation'?'Simulation':'MT5 Demo'} · ${status.symbol||'—'} · ${status.strategy||'—'} · ${status.capturing_quotes?'กำลังเก็บ quote':'หยุดเก็บ quote'}`:ready?'เริ่มได้เฉพาะเมื่อเชื่อมต่อบัญชี Demo หรือ Simulation':'Forward shadow ใช้ไม่ได้ใน session นี้';
    const pending=status.capturing_quotes===true||Number(status.unresolved)>0;
    $('researchForwardStatus').textContent=status.error||`${pending&&!active?'กำลังรอผล quote/decision ที่ค้างอยู่ · ':''}${fmt(status.decisions,0)} decisions · ${fmt(status.quotes,0)} quotes · ${fmt(status.unresolved,0)} รายการรอผล · ข้ามแท่ง ${fmt(status.skipped_bars,0)} · dropped ${fmt(status.dropped_observations,0)}`;
    $('researchForwardStop').disabled=!active;$('researchForwardStart').disabled=!ready||active||pending||loading;
    const hasQuotes=Number(status.quotes)>0;$('researchForwardDownloadResult').disabled=!hasQuotes;$('researchForwardDownloadDataset').disabled=!hasQuotes;
  }
  function forwardMarkup(result){
    const report=result.report||{},decisions=report.decisions||[];
    const rows=decisions.map(row=>`<tr><td>${dateUTC(row.bar_time)}</td><td>${esc(row.side||'—')}</td><td>${esc(row.status||'—')}</td><td>${row.net_pnl===null||row.net_pnl===undefined?'—':fmt(row.net_pnl)}</td><td>${row.net_r===null||row.net_r===undefined?'—':fmt(row.net_r)}</td><td>${esc(row.close_reason||'—')}</td><td>${esc(row.reason||'—')}</td></tr>`).join('');
    return `<h3>${esc(result.source||'forward_shadow')} · ${esc(report.symbol||'—')} · ${esc(result.status||'—')}</h3><div class="research-metrics">${metric('Dataset hash',esc(result.dataset_hash||'—'))}${metric('Mode',esc(report.mode||'—'))}${metric('Actual fills','ไม่มี · virtual decisions เท่านั้น')}${metric('Skipped bars',fmt(report.skipped_bars,0))}${metric('Dropped observations',fmt(report.dropped_observations,0))}</div><p class="caption">${esc(report.availability||'')}</p><p class="caption">${esc(report.historical_specification||'')}</p><div class="table-wrap"><table><thead><tr><th>Bar time (UTC)</th><th>Side</th><th>สถานะ</th><th>Virtual P&amp;L</th><th>Virtual R</th><th>เหตุผลการปิด</th><th>เหตุผล</th></tr></thead><tbody>${rows||'<tr><td colspan="7">ยังไม่มี decision</td></tr>'}</tbody></table></div>`;
  }
  async function refreshForwardResult(){
    if(forwardResultPromise)return forwardResultPromise;
    if(!forwardStatus||Number(forwardStatus.quotes)<1){$('researchForwardDetails').innerHTML='<p class="empty">รอ quote จากแท่งใหม่เพื่อแสดงผล forward</p>';return;}
    forwardResultPromise=(async()=>{try{forwardResult=await request('/api/research/forward/result');$('researchForwardDetails').innerHTML=forwardMarkup(forwardResult);}
      catch(error){$('researchForwardDetails').innerHTML=`<p class="research-error">${esc(error.message||'โหลด forward result ไม่สำเร็จ')}</p>`;}
    })().finally(()=>{forwardResultPromise=null;});return forwardResultPromise;
  }
  function collectionDateAndCosts(){
    const start=utcEpoch('researchCollectStart','เวลาเริ่ม'),end=utcEpoch('researchCollectEnd','เวลาสิ้นสุด');if(start===null||end===null)return null;
    if(start>=end||end>Date.now()/1000||end-start>31*86400){fieldError($('researchCollectEnd'),'ช่วงเวลาต้องเรียงจากเก่าไปใหม่ จบไม่เกินเวลาปัจจุบัน และยาวไม่เกิน 31 วัน');return null;}
    const thresholdInput=$('researchGapThreshold'),threshold=Number(thresholdInput.value);if(!Number.isInteger(threshold)||threshold<1||threshold>86400){fieldError(thresholdInput,'ระบุ gap threshold ตั้งแต่ 1 ถึง 86400 วินาที');return null;}
    const shared=collectCosts();return shared?{start_at:start,end_at:end,costs:shared.costs,margin_per_lot:shared.margin_per_lot,gap_threshold_seconds:threshold}:null;
  }
  async function submitCollection(event){
    event.preventDefault();if(loading||$('researchCollect').disabled)return;
    const body=collectionDateAndCosts();if(!body)return;
    loading=true;$('researchCollect').disabled=true;setStatus('researchCollectStatus','กำลังส่งคำขอเก็บประวัติจาก MT5…');
    try{
      const created=await request('/api/research/collect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      setStatus('researchCollectStatus',`กำลังเก็บข้อมูล · ${created.id}`);
      const job=created.status==='queued'?await pollJob(created.id):created;
      if(job?.status!=='completed')return;
      const result=await request(`/api/research/jobs/${encodeURIComponent(created.id)}/result`);
      collectedHash=result.dataset_hash;selectedHash=result.dataset_hash;await loadStatus(true);populateDatasets();
      $('researchCollectionReport').innerHTML=collectionMarkup(result);$('researchDownloadCollectedDataset').disabled=false;
      setStatus('researchCollectStatus',`เก็บข้อมูลและนำเข้า dataset แล้ว · ${result.dataset_hash}`,'success');
    }catch(error){setStatus('researchCollectStatus',error.message||'เก็บข้อมูลไม่สำเร็จ ตรวจช่วงเวลาและสถานะ MT5 แล้วลองอีกครั้ง','error');}
    finally{loading=false;renderCollectionAvailability({available:collectionAvailable,reason:$('researchCollectionAvailability').textContent});updateDatasetDetails();}
  }
  async function submitForward(event){
    event.preventDefault();if(loading)return;
    const shared=collectCosts();if(!shared)return;
    const strategy=$('researchForwardStrategy').value,balance=Number($('researchForwardBalance').value),latency=Number($('researchForwardLatency').value),horizon=Number($('researchForwardHorizon').value),budget=Number($('researchForwardBudget').value);
    if(!Number.isFinite(balance)||balance<=0){fieldError($('researchForwardBalance'),'ระบุ initial capital มากกว่า 0');return;}
    if(!Number.isInteger(latency)||latency<0){fieldError($('researchForwardLatency'),'Latency ต้องเป็นจำนวนเต็มตั้งแต่ 0 ขึ้นไป');return;}
    if(!Number.isInteger(horizon)||horizon<300||horizon>86400){fieldError($('researchForwardHorizon'),'Horizon ต้องอยู่ระหว่าง 300 ถึง 86400 วินาที');return;}
    if(!Number.isInteger(budget)||budget<1||budget>20){fieldError($('researchForwardBudget'),'ระบุ max provider decisions ตั้งแต่ 1 ถึง 20');return;}
    const before=Number($('researchForwardBefore').value),after=Number($('researchForwardAfter').value);
    if(!Number.isInteger(before)||before<0||before>1440||!Number.isInteger(after)||after<0||after>1440){fieldError($('researchForwardBefore'),'Calendar window ต้องอยู่ระหว่าง 0 ถึง 1440 นาที');return;}
    const provider=$('researchForwardProvider').value,body={strategy,costs:shared.costs,margin_per_lot:shared.margin_per_lot,initial_balance:balance,latency_seconds:latency,horizon_seconds:horizon,calendar_before_minutes:before,calendar_after_minutes:after,provider,max_decisions:budget,challenger:true};
    let keyField=$('researchForwardKey'),requestBody='';
    if(provider!=='disabled'){
      const model=$('researchForwardModel').value.trim(),key=keyField.value;
      if(!model){fieldError($('researchForwardModel'),'ระบุ model ที่ตั้งค่าไว้กับ provider');return;}
      if(!key.trim()){fieldError(keyField,'ระบุ API key ของ provider ที่เลือก');return;}
      body.model=model;body.api_key=key;
    }
    if($('researchForwardEvidence').checked){const dataset=chosenDataset();if(!dataset){setStatus('researchForwardStatus','เลือก dataset ก่อนใช้ evidence','error');return;}body.evidence_dataset_hash=dataset.dataset_hash;}
    requestBody=JSON.stringify(body);keyField.value='';delete body.api_key;
    loading=true;$('researchForwardStart').disabled=true;setStatus('researchForwardStatus','กำลังเริ่ม forward shadow…');
    try{
      await request('/api/research/forward/start',{method:'POST',headers:{'Content-Type':'application/json'},body:requestBody});requestBody='';
      setStatus('researchForwardStatus','เริ่มแล้ว · รอแท่งใหม่ก่อนประเมินสัญญาณ','success');await loadStatus(true);
    }catch(error){setStatus('researchForwardStatus',error.message||'เริ่ม forward shadow ไม่สำเร็จ','error');}
    finally{requestBody='';keyField.value='';loading=false;renderForwardStatus(forwardStatus||{});}
  }
  async function stopForward(){
    $('researchForwardStop').disabled=true;try{await request('/api/research/forward/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});setStatus('researchForwardStatus','หยุดการตัดสินใจใหม่แล้ว · รายการที่รับไว้ยังรอผลตาม horizon','success');await loadStatus(true);}
    catch(error){setStatus('researchForwardStatus',error.message||'หยุด forward shadow ไม่สำเร็จ','error');await loadStatus(true).catch(()=>{});}
  }
  async function showResult(id){
    selectedJob=id;setStatus('researchJobStatus','กำลังโหลดผลลัพธ์…');
    try{
      const [job,result]=await Promise.all([request(`/api/research/jobs/${encodeURIComponent(id)}`),request(`/api/research/jobs/${encodeURIComponent(id)}/result`)]);
      if(job.status!=='completed')throw new Error('ผลลัพธ์ยังไม่พร้อม');selectedResult=result;selectedTrace=null;
      if(job.kind==='replay')selectedTrace=await request(`/api/research/jobs/${encodeURIComponent(id)}/trace`);
      if(job.kind==='collect'){
        selectedHash=result.dataset_hash;collectedHash=result.dataset_hash;await loadStatus(true);populateDatasets();
        $('researchCollectionReport').innerHTML=collectionMarkup(result);$('researchDownloadCollectedDataset').disabled=false;
      }
      $('researchResult').innerHTML=job.kind==='replay'?replayMarkup(result):job.kind==='evaluate'?evaluateMarkup(result):job.kind==='compare'?compareMarkup(result):job.kind==='collect'?collectionMarkup(result):shadowMarkup(result,job.kind);
      $('researchDownloadResult').disabled=false;$('researchDownloadTrace').disabled=!selectedTrace;setStatus('researchJobStatus',`แสดงผล ${job.kind} · ${id}`,'success');
    }catch(error){$('researchResult').innerHTML=`<p class="research-error">${esc(error.message||'โหลดผลลัพธ์ไม่สำเร็จ')}</p>`;$('researchDownloadResult').disabled=true;$('researchDownloadTrace').disabled=true;setStatus('researchJobStatus',error.message||'โหลดผลลัพธ์ไม่สำเร็จ','error');}
  }
  async function download(path,filename){
    try{const response=await fetch(path,{headers:{'X-Trade-Token':sessionToken}});if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(apiError(data,'ดาวน์โหลดไม่สำเร็จ'));}
      const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=filename;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(error){setStatus('researchJobStatus',error.message||'ดาวน์โหลดไม่สำเร็จ','error');}
  }
  async function importDataset(event){
    event.preventDefault();const file=$('researchDatasetFile').files[0];if(!file)return;
    if(file.size>8*1024*1024){setStatus('researchImportStatus','ไฟล์มีขนาดเกิน 8 MiB','error');return;}
    $('researchImport').disabled=true;setStatus('researchImportStatus','กำลังตรวจและนำเข้า dataset…');
    try{
      const raw=await file.text();const response=await request('/api/research/datasets',{method:'POST',headers:{'Content-Type':'application/json'},body:raw});
      setStatus('researchImportStatus',`นำเข้าสำเร็จ · ${response.instrument?.symbol||'dataset'} · ${response.dataset_hash}`,'success');
      await loadStatus(true);selectedHash=response.dataset_hash;populateDatasets();kindChanged();
    }catch(error){setStatus('researchImportStatus',error.message||'นำเข้าไม่สำเร็จ ตรวจ schema แล้วลองอีกครั้ง','error');}
    finally{$('researchImport').disabled=false;}
  }
  function renderResearch(state){
    if(!state?.token)return;
    if(sessionToken!==state.token){sessionToken=state.token;loaded=false;datasets=[];jobs=[];selectedHash='';selectedJob=null;selectedResult=null;selectedTrace=null;forwardStatus=null;forwardResult=null;collectedHash='';$('researchApiKey').value='';$('researchForwardKey').value='';$('researchCollectionReport').innerHTML='<p class="empty">รายงานช่วงข้อมูลจะแสดงหลังเก็บข้อมูลเสร็จ</p>';}
    if(!loaded&&!statusPromise)loadStatus().catch(error=>setStatus('researchJobStatus',error.message||'โหลดสถานะ Research ไม่สำเร็จ','error'));
  }
  $('researchStrategies').innerHTML=strategies.map(([key,label])=>`<label><input type="checkbox" value="${key}" checked> ${esc(label)}</label>`).join('');
  $('researchWalkForwardWrap').innerHTML='<details id="researchWalkForwardDetails" class="research-advanced"><summary>Walk-forward และ execution settings</summary><div class="form-row"><label>Train bars<input id="researchTrain" type="number" min="80" value="600"></label><label>Validation bars<input id="researchValidation" type="number" min="20" value="250"></label></div><div class="form-row"><label>Test bars<input id="researchTest" type="number" min="1" value="100"></label><label>Purge bars<input id="researchPurge" type="number" min="1" value="1"></label></div><label>Embargo bars<input id="researchEmbargo" type="number" min="1" value="1"></label></details>';
  $('researchKind').addEventListener('change',()=>{clearProviderKeys();kindChanged();});$('researchDatasetSelect').addEventListener('change',()=>{selectedHash=$('researchDatasetSelect').value;updateDatasetDetails();kindChanged();});
  $('researchImportForm').addEventListener('submit',importDataset);$('researchRunForm').addEventListener('submit',submitJob);
  $('researchProvider').addEventListener('change',()=>{clearProviderKeys();const enabled=$('researchProvider').value!=='disabled';$('researchModel').disabled=!enabled;$('researchApiKey').disabled=!enabled;});
  $('researchForwardProvider').addEventListener('change',()=>{clearProviderKeys();const enabled=$('researchForwardProvider').value!=='disabled';$('researchForwardModel').disabled=!enabled;$('researchForwardKey').disabled=!enabled;});
  $('researchNoRolloverDays').addEventListener('change',()=>weekdayInputs().forEach(input=>{input.disabled=$('researchNoRolloverDays').checked;if(input.disabled)input.checked=false;}));
  weekdayInputs().forEach(input=>input.addEventListener('change',()=>{if(input.checked)$('researchNoRolloverDays').checked=false;}));
  $('researchCollectForm').addEventListener('submit',submitCollection);$('researchForwardForm').addEventListener('submit',submitForward);
  $('researchForwardStop').addEventListener('click',stopForward);$('researchForwardRefresh').addEventListener('click',()=>loadStatus(true).catch(error=>setStatus('researchForwardStatus',error.message,'error')));
  $('researchRefresh').addEventListener('click',()=>loadStatus(true).catch(error=>setStatus('researchJobStatus',error.message,'error')));
  $('researchJobs').addEventListener('click',event=>{const button=event.target.closest('.research-job-open');if(button)showResult(button.dataset.job);});
  $('researchDownloadResult').addEventListener('click',()=>selectedJob&&download(`/api/research/jobs/${encodeURIComponent(selectedJob)}/result`,`research-${selectedJob}.json`));
  $('researchDownloadTrace').addEventListener('click',()=>selectedJob&&selectedTrace&&download(`/api/research/jobs/${encodeURIComponent(selectedJob)}/trace`,`research-${selectedJob}-trace.json`));
  $('researchDownloadCollectedDataset').addEventListener('click',()=>{const hash=collectedHash||selectedHash;if(hash)download(`/api/research/datasets/${encodeURIComponent(hash)}/download`,`dataset-${hash}.json`);});
  $('researchForwardDownloadResult').addEventListener('click',()=>download('/api/research/forward/result','forward-shadow-result.json'));
  $('researchForwardDownloadDataset').addEventListener('click',()=>download('/api/research/forward/dataset','forward-shadow-dataset.json'));
  kindChanged();$('researchModel').disabled=true;$('researchApiKey').disabled=true;$('researchForwardModel').disabled=true;$('researchForwardKey').disabled=true;
  setInterval(()=>{if(loaded&&$('research').classList.contains('active')&&!statusPromise)loadStatus(true).catch(error=>setStatus('researchJobStatus',error.message,'error'));},3000);
  window.renderResearch=renderResearch;
})();
