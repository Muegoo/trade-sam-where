const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'trade-research-browser-'));
const artifactDir = path.join(root, 'artifacts');
let currentStage='setup';
const pythonScript = String.raw`
import sys, threading, time
import uvicorn
from trade_app.server import create_app
app=create_app(mode="simulation", db_path=sys.argv[1])
server=uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical", access_log=False))
thread=threading.Thread(target=server.run, daemon=True)
thread.start()
deadline=time.time()+20
while not server.started and time.time()<deadline: time.sleep(.05)
if not server.started: raise RuntimeError("local test server did not start")
port=server.servers[0].sockets[0].getsockname()[1]
print("READY "+str(port), flush=True)
sys.stdin.readline()
server.should_exit=True
thread.join(timeout=15)
if thread.is_alive(): raise RuntimeError("local test server did not stop through its control channel")
`;

function archive() {
  const base = 1700000000;
  let previous = 100;
  const bars = Array.from({length:122},(_,i)=>{
    const time=base+i*900,close=100+i*.015+((i%8)-4)*.22,open=previous;
    previous=close;return {time,available_at:time+900,open,high:Math.max(open,close)+.5,low:Math.min(open,close)-.5,close};
  });
  const quotes=bars.map(bar=>({time:bar.available_at+1,bid:bar.close-.01,ask:bar.close+.01}));
  quotes.unshift({time:base,bid:100,ask:100.1},{time:base+2,bid:100,ask:100.1},{time:base+3,bid:106,ask:106.1});
  const asOf=quotes.at(-1).time;
  return {
    version: 1, source: 'research-browser-smoke', timezone: 'UTC',
    instrument: {symbol:'TEST',currency:'USD',point:.01,tick_size:.01,contract_size:1,volume_min:.1,volume_max:2,volume_step:.1,stops_level:1,margin_per_lot:50},
    costs: {commission_per_lot_side:0,fee_per_lot_side:0,slippage_points:0,swap_long_per_lot_day:0,swap_short_per_lot_day:0,rollover_hour_utc:1,rollover_weekdays:[0,1,2,3,4],triple_swap_weekday:null},
    quotes,
    bars: {M15:bars,H1:[]},
    evidence: [{id:'known-blackout',kind:'calendar',source:'smoke-fixture',symbol:'TEST',published_at:asOf-100,available_at:asOf-90,fetched_at:asOf-80,event_at:asOf,value:'Known high-impact event',impact:'high'}]
  };
}

class CDP {
  constructor(ws) { this.ws=ws; this.next=0; this.pending=new Map(); this.sessionId=null; }
  static async connect(url) {
    const ws=new WebSocket(url); await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',()=>reject(new Error('Chrome DevTools connection failed')),{once:true});});
    const cdp=new CDP(ws);ws.addEventListener('message',event=>{let message;try{message=JSON.parse(event.data);}catch{return;}const pending=cdp.pending.get(message.id);if(!pending)return;cdp.pending.delete(message.id);if(message.error)pending.reject(new Error(message.error.message||'Chrome command failed'));else pending.resolve(message.result);});
    return cdp;
  }
  command(method,params={},sessionId=this.sessionId) {
    const id=++this.next;return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{this.pending.delete(id);reject(new Error('Chrome command timed out'));},15000);this.pending.set(id,{resolve:value=>{clearTimeout(timer);resolve(value);},reject:error=>{clearTimeout(timer);reject(error);}});this.ws.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
  }
  async evaluate(expression) { const result=await this.command('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error('Page script evaluation failed');return result.result.value; }
  close() { try { this.ws.close(); } catch {} }
}

const waitFor = async (predicate, timeout=20000) => {
  const until=Date.now()+timeout;while(Date.now()<until){if(await predicate())return;await new Promise(resolve=>setTimeout(resolve,150));}throw new Error('Browser interaction timed out');
};
const escArg = value => JSON.stringify(value);

async function main() {
  fs.mkdirSync(artifactDir,{recursive:true});
  const datasetPath=path.join(temp,'dataset.json'),proposalPath=path.join(temp,'proposals.json'),shadowPath=path.join(temp,'shadow-proposals.json');
  fs.writeFileSync(datasetPath,JSON.stringify(archive()));
  fs.writeFileSync(proposalPath,JSON.stringify([{decision_at:1700000001,side:'BUY',kind:'MARKET',entry:null,sl:95,tp:105,lot:.1,strategy:'smoke'}]));
  const server=spawn(path.join(root,'.venv/bin/python'),['-u','-c',pythonScript,path.join(temp,'paper.sqlite')],{cwd:root,stdio:['pipe','pipe','pipe']});
  let serverOutput='';const readReady=new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('local test server did not become ready')),25000);server.stdout.on('data',chunk=>{serverOutput+=chunk.toString();const match=serverOutput.match(/READY (\d+)/);if(match){clearTimeout(timer);resolve(Number(match[1]));}});server.on('exit',code=>{if(code!==0)reject(new Error('local test server exited unexpectedly'));});});
  let chrome=null,cdp=null,serverStopped=false;
  try {
    const port=await readReady,baseUrl=`http://127.0.0.1:${port}`;
    const before=await fetch(`${baseUrl}/api/state`).then(response=>response.json());
    const chromePath='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    chrome=spawn(chromePath,['--headless=new','--no-first-run','--no-default-browser-check','--no-proxy-server','--disable-background-networking','--disable-sync','--metrics-recording-only','--disable-gpu','--remote-debugging-port=0',`--user-data-dir=${path.join(temp,'chrome-profile')}`,'about:blank'],{stdio:['ignore','ignore','ignore']});
    const endpointFile=path.join(temp,'chrome-profile','DevToolsActivePort');
    await waitFor(()=>fs.existsSync(endpointFile),15000);
    const debugPort=Number(fs.readFileSync(endpointFile,'utf8').split('\n')[0]);
    const version=await fetch(`http://127.0.0.1:${debugPort}/json/version`).then(response=>response.json());
    cdp=await CDP.connect(version.webSocketDebuggerUrl);
    const target=await cdp.command('Target.createTarget',{url:'about:blank'});
    const attached=await cdp.command('Target.attachToTarget',{targetId:target.targetId,flatten:true},null);cdp.sessionId=attached.sessionId;
    const exceptions=[],downloadRequests=[];cdp.ws.addEventListener('message',event=>{let message;try{message=JSON.parse(event.data);}catch{return;}if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails?.text||'runtime exception');if(message.method==='Network.requestWillBeSent'&&message.params.request.url.includes('/api/research/datasets/'))downloadRequests.push({url:message.params.request.url,headers:message.params.request.headers||{}});});
    await cdp.command('Page.enable');await cdp.command('Runtime.enable');await cdp.command('DOM.enable');await cdp.command('Network.enable');
    await cdp.command('Network.setBlockedURLs',{urls:['https://fonts.googleapis.com/*','https://fonts.gstatic.com/*']});
    currentStage='initial page';await cdp.command('Page.navigate',{url:baseUrl});
    await waitFor(async()=>cdp.evaluate('document.readyState === "complete"'));
    await waitFor(async()=>cdp.evaluate('Boolean(document.querySelector("#dashboard")) && document.querySelector("#connection").textContent !== "Connecting"'));
    currentStage='open Research';await cdp.evaluate('document.querySelector("[data-page=research]").click()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#research").classList.contains("active") && document.querySelector("#researchJobs .empty")?.textContent.includes("ยังไม่มี")'));
    const weekdayValues=await cdp.evaluate('({weekdays:Array.from(document.querySelectorAll("[name=researchRolloverWeekday]")).map(input=>input.value),triple:Array.from(document.querySelector("#researchTripleSwap").options).filter(option=>option.value).map(option=>option.value)})');assert.deepEqual(weekdayValues,{weekdays:['6','0','1','2','3','4','5'],triple:['6','0','1','2','3','4','5']});
    currentStage='MT5-only collector state';const collector=await cdp.evaluate('({disabled:document.querySelector("#researchCollect").disabled,reason:document.querySelector("#researchCollectionAvailability").innerText})');assert.equal(collector.disabled,true);assert.match(collector.reason,/MT5/i);
    await cdp.evaluate('document.querySelector("#researchForwardBalance").value="10001";document.querySelector("#researchCommission").value="0"');
    await new Promise(resolve=>setTimeout(resolve,3400));
    assert.deepEqual(await cdp.evaluate('({balance:document.querySelector("#researchForwardBalance").value,commission:document.querySelector("#researchCommission").value})'),{balance:'10001',commission:'0'});
    currentStage='dataset import controls';const importNode=await cdp.evaluate('document.querySelector("#researchDatasetFile") ? true : false');assert.equal(importNode,true);
    const documentRoot=await cdp.command('DOM.getDocument');
    const datasetNode=await cdp.command('DOM.querySelector',{nodeId:documentRoot.root.nodeId,selector:'#researchDatasetFile'});
    await cdp.command('DOM.setFileInputFiles',{nodeId:datasetNode.nodeId,files:[datasetPath]});
    currentStage='dataset upload';await cdp.evaluate('document.querySelector("#researchImport").click()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchImportStatus").textContent.includes("นำเข้าสำเร็จ")'));
    await waitFor(async()=>cdp.evaluate('!document.querySelector("#researchRun").disabled'));
    assert.equal(await cdp.evaluate('document.querySelector("#researchDownloadCollectedDataset").disabled'),false);
    currentStage='dataset download token checks';const imported=(await fetch(`${baseUrl}/api/research/status`,{headers:{'X-Trade-Token':before.token}}).then(response=>response.json())).datasets[0];
    const datasetDownload=`${baseUrl}/api/research/datasets/${imported.dataset_hash}/download`;
    assert.equal((await fetch(datasetDownload,{headers:{'X-Trade-Token':before.token}})).status,200);assert.equal((await fetch(datasetDownload)).status,403);
    await cdp.evaluate('document.querySelector("#researchDownloadCollectedDataset").click()');
    await waitFor(async()=>downloadRequests.some(item=>item.url.includes('/download')));
    assert.ok(Object.entries(downloadRequests.find(item=>item.url.includes('/download')).headers).some(([key,value])=>key.toLowerCase()==='x-trade-token'&&value===before.token),'dataset download must carry session token');
    const proposalNode=await cdp.command('DOM.querySelector',{nodeId:documentRoot.root.nodeId,selector:'#researchProposalsFile'});
    await cdp.command('DOM.setFileInputFiles',{nodeId:proposalNode.nodeId,files:[proposalPath]});
    currentStage='replay submission and result';await cdp.evaluate('document.querySelector("#researchRunForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchResult").textContent.includes("Closed trades")'),30000);
    const replay=await cdp.evaluate('({text:document.querySelector("#researchResult").innerText, resultDisabled:document.querySelector("#researchDownloadResult").disabled, traceDisabled:document.querySelector("#researchDownloadTrace").disabled})');
    assert.match(replay.text,/Closed trades/);assert.equal(replay.resultDisabled,false);assert.equal(replay.traceDisabled,false);
    const completed=await fetch(`${baseUrl}/api/research/status`,{headers:{'X-Trade-Token':before.token}}).then(response=>response.json());
    assert.equal(completed.jobs.filter(job=>job.status==='completed'&&job.kind==='replay').length,1);
    currentStage='evaluate insufficient-data result';
    await cdp.evaluate('document.querySelector("#researchKind").value="evaluate";document.querySelector("#researchKind").dispatchEvent(new Event("change"))');
    await cdp.evaluate('document.querySelector("#researchRunForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchResult").textContent.includes("ข้อมูลไม่พอ")'),30000);
    const evaluation=await cdp.evaluate('document.querySelector("#researchResult").innerText');assert.match(evaluation,/ข้อมูลไม่พอ/);assert.match(evaluation,/122 \/ 952 bars/);
    currentStage='A/B comparison with LLM disabled';
    const compareControls=await cdp.evaluate('document.querySelector("#researchKind").value="compare";document.querySelector("#researchKind").dispatchEvent(new Event("change"));({visible:!document.querySelector("#researchWalkForwardWrap").hidden,open:document.querySelector("#researchWalkForwardDetails").open,ids:["researchTrain","researchValidation","researchTest","researchPurge","researchEmbargo"].map(id=>document.querySelectorAll(`#${id}`).length)})');assert.equal(compareControls.visible,true);assert.equal(compareControls.open,true);assert.deepEqual(compareControls.ids,[1,1,1,1,1]);
    await cdp.evaluate('document.querySelector("#researchTrain").value="80";document.querySelector("#researchValidation").value="20";document.querySelector("#researchTest").value="20";document.querySelector("#researchPurge").value="1";document.querySelector("#researchEmbargo").value="1";document.querySelector("#researchCompareStrategy").value="ema";document.querySelector("#researchApiKey").value="smoke-only-dummy";document.querySelector("#researchProvider").value="openai";document.querySelector("#researchProvider").dispatchEvent(new Event("change"));document.querySelector("#researchApiKey").value="smoke-only-dummy";document.querySelector("#researchProvider").value="disabled";document.querySelector("#researchProvider").dispatchEvent(new Event("change"));if(document.querySelector("#researchApiKey").value)throw new Error("provider change retained key");document.querySelector("#researchApiKey").value="smoke-only-dummy";document.querySelector("#researchKind").value="evaluate";document.querySelector("#researchKind").dispatchEvent(new Event("change"));if(document.querySelector("#researchApiKey").value)throw new Error("kind change retained key");document.querySelector("#researchKind").value="compare";document.querySelector("#researchKind").dispatchEvent(new Event("change"));document.querySelector("#researchProvider").value="disabled";document.querySelector("#researchProvider").dispatchEvent(new Event("change"))');
    await cdp.evaluate('document.querySelector("#researchRunForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchResult").textContent.includes("A/B comparison")'),30000);
    const comparison=await cdp.evaluate('document.querySelector("#researchResult").innerText');assert.match(comparison,/A\/B comparison · completed/);assert.match(comparison,/A · Rules\s+เสร็จแล้ว/);assert.match(comparison,/B · Calendar\s+เสร็จแล้ว/);assert.match(comparison,/C · Analyst agreement\s+ประเมินไม่ได้/);assert.match(comparison,/D · Challenger approved\s+ประเมินไม่ได้/);assert.match(comparison,/ปิด provider ไว้/);assert.match(comparison,/LLM decisions 0 · provider calls 0/);assert.match(comparison,/ไม่แทนด้วยศูนย์/);
    const compareRows=await cdp.evaluate('Array.from(document.querySelectorAll("#researchResult .table-wrap table tbody tr")).slice(0,4).map(row=>row.innerText)');assert.match(compareRows[0],/เสร็จแล้ว/);assert.match(compareRows[1],/เสร็จแล้ว/);assert.doesNotMatch(compareRows[0],/เสร็จแล้ว\s+—\s+—/);assert.doesNotMatch(compareRows[1],/เสร็จแล้ว\s+—\s+—/);
    currentStage='comparison screenshot';await cdp.command('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await cdp.evaluate('document.querySelector("#researchResult").scrollIntoView({block:"start"})');await cdp.evaluate('document.fonts.ready');
    const comparisonShot=await cdp.command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifactDir,'research-next-comparison.png'),Buffer.from(comparisonShot.data,'base64'));
    currentStage='shadow result';
    const manifest=completed.datasets[0];
    await cdp.evaluate('document.querySelector("#researchKind").value="shadow";document.querySelector("#researchKind").dispatchEvent(new Event("change"))');
    const asOfField=await cdp.evaluate('document.querySelector("#researchAsOf").value');
    const asOf=Math.floor(Date.parse(asOfField.length===16?`${asOfField}:00Z`:`${asOfField}Z`)/1000);
    assert.equal(asOf,manifest.last_quote_time,'default shadow as-of must preserve dataset quote seconds');
    const snapshot=await fetch(`${baseUrl}/api/research/datasets/${manifest.dataset_hash}/snapshot?as_of=${asOf}`,{headers:{'X-Trade-Token':before.token}}).then(response=>response.json());
    fs.writeFileSync(shadowPath,JSON.stringify([{action:'BUY',strategy:'browser-smoke',created_at:asOf-1,expires_at:asOf+180,snapshot_hash:snapshot.snapshot_hash,evidence_ids:['known-blackout'],reason:'known shadow reason',kind:'MARKET',entry:100,sl:95,tp:105}]));
    await cdp.evaluate('document.querySelector("#researchCalendarBefore").value="1";document.querySelector("#researchCalendarAfter").value="1"');
    const shadowNode=await cdp.command('DOM.querySelector',{nodeId:documentRoot.root.nodeId,selector:'#researchProposalsFile'});
    await cdp.command('DOM.setFileInputFiles',{nodeId:shadowNode.nodeId,files:[shadowPath]});
    await cdp.evaluate('document.querySelector("#researchRunForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchResult").textContent.includes("known shadow reason") || document.querySelector("#researchJobStatus").classList.contains("error")'),30000);
    const shadowText=await cdp.evaluate('document.querySelector("#researchResult").innerText');assert.match(shadowText,/Shadow · not_configured/);assert.match(shadowText,/Action\s+NO_TRADE/);assert.match(shadowText,/Known high-impact event/);
    currentStage='disabled advisor result';
    await cdp.evaluate('document.querySelector("#researchKind").value="advisor";document.querySelector("#researchKind").dispatchEvent(new Event("change"))');
    await cdp.evaluate('document.querySelector("#researchRunForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchResult").textContent.includes("Advisor · not_configured")'),30000);
    const advisorText=await cdp.evaluate('document.querySelector("#researchResult").innerText');assert.match(advisorText,/Advisor · not_configured/);assert.match(advisorText,/NO_TRADE/);
    currentStage='forward shadow start in simulation';
    await cdp.evaluate('document.querySelector("#researchForwardBalance").value="10001";document.querySelector("#researchCommission").value="0";document.querySelector("#researchFee").value="0";document.querySelector("#researchSlippage").value="0";document.querySelector("#researchMargin").value="50";document.querySelector("#researchSwapLong").value="0";document.querySelector("#researchSwapShort").value="0";document.querySelector("#researchRolloverHour").value="1";document.querySelector("#researchNoRolloverDays").checked=true;document.querySelector("#researchNoRolloverDays").dispatchEvent(new Event("change"));document.querySelector("#researchForwardKey").value="smoke-only-dummy";document.querySelector("#researchForwardProvider").value="openai";document.querySelector("#researchForwardProvider").dispatchEvent(new Event("change"));document.querySelector("#researchForwardKey").value="smoke-only-dummy";document.querySelector("#researchForwardProvider").value="disabled";document.querySelector("#researchForwardProvider").dispatchEvent(new Event("change"));if(document.querySelector("#researchForwardKey").value)throw new Error("forward provider change retained key");document.querySelector("#researchForwardForm").requestSubmit()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchForwardBadge").innerText==="running"'),30000);
    const forwardStarted=await cdp.evaluate('({mode:document.querySelector("#researchForwardMode").innerText,status:document.querySelector("#researchForwardStatus").innerText,stopDisabled:document.querySelector("#researchForwardStop").disabled})');assert.match(forwardStarted.mode,/Simulation/);assert.equal(forwardStarted.stopDisabled,false);
    const forwardState=await fetch(`${baseUrl}/api/research/forward`,{headers:{'X-Trade-Token':before.token}}).then(response=>response.json());assert.equal(forwardState.decisions,0,'forward start waits for a new bar');
    await waitFor(async()=>cdp.evaluate('!document.querySelector("#researchForwardDownloadResult").disabled'),15000);
    assert.equal((await fetch(`${baseUrl}/api/research/forward/result`,{headers:{'X-Trade-Token':before.token}})).status,200);
    assert.equal((await fetch(`${baseUrl}/api/research/forward/dataset`,{headers:{'X-Trade-Token':before.token}})).status,200);
    await cdp.evaluate('document.querySelector("#researchForwardForm").scrollIntoView({block:"start"})');await cdp.evaluate('document.fonts.ready');
    const forwardShot=await cdp.command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifactDir,'research-next-forward.png'),Buffer.from(forwardShot.data,'base64'));
    await cdp.evaluate('document.querySelector("#researchForwardStop").click()');
    await waitFor(async()=>cdp.evaluate('document.querySelector("#researchForwardBadge").innerText==="stopped"'),15000);
    currentStage='terminal and risk navigation';await cdp.evaluate('document.querySelector("[data-page=terminal]").click()');assert.equal(await cdp.evaluate('document.querySelector("#terminal").classList.contains("active")'),true);
    await cdp.evaluate('document.querySelector("[data-page=risk]").click()');assert.equal(await cdp.evaluate('document.querySelector("#risk").classList.contains("active")'),true);
    await cdp.evaluate('document.querySelector("[data-page=research]").click()');
    currentStage='desktop screenshot';await cdp.command('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
    await cdp.evaluate('document.fonts.ready');await cdp.evaluate('window.scrollTo(0,0)');
    const desktop=await cdp.command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifactDir,'research-next-desktop.png'),Buffer.from(desktop.data,'base64'));
    currentStage='mobile screenshot and width';await cdp.command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await cdp.evaluate('window.dispatchEvent(new Event("resize"))');
    const overflow=await cdp.evaluate('({width:document.documentElement.scrollWidth,viewport:window.innerWidth})');assert.ok(overflow.width<=overflow.viewport,`mobile page overflow ${overflow.width}>${overflow.viewport}`);
    await cdp.evaluate('document.fonts.ready');
    const mobile=await cdp.command('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(artifactDir,'research-next-mobile.png'),Buffer.from(mobile.data,'base64'));
    assert.equal(exceptions.length,0,`page raised ${exceptions.length} runtime exceptions`);
    const after=await fetch(`${baseUrl}/api/state`).then(response=>response.json());
    assert.equal((after.positions||[]).length,(before.positions||[]).length,'research flow changed positions');
    assert.equal((after.orders||[]).length,(before.orders||[]).length,'research flow changed orders');
    await cdp.command('Browser.close',{},null);cdp.close();cdp=null;
    await new Promise(resolve=>{const timer=setTimeout(resolve,5000);chrome.once('exit',()=>{clearTimeout(timer);resolve();});});
    if(chrome.exitCode===null)throw new Error('Chrome did not close after Browser.close');
    server.stdin.write('\n');await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('test server did not stop gracefully')),18000);server.once('exit',()=>{clearTimeout(timer);serverStopped=true;resolve();});});
    process.stdout.write('Research browser smoke passed: collector simulation gating, dataset import/download token, replay/trace, evaluate insufficiency, A/B comparison with C/D unavailable, shadow, disabled advisor, forward shadow start/stop/export, terminal/risk navigation, mobile width, zero research orders. Screenshots: artifacts/research-next-desktop.png, research-next-mobile.png, research-next-comparison.png and research-next-forward.png\n');
  } finally {
    if(cdp)cdp.close();
    if(chrome&&chrome.exitCode===null){
      // Browser.close is a graceful CDP shutdown; no process signals are sent.
      try{const versionFiles=fs.existsSync(path.join(temp,'chrome-profile','DevToolsActivePort'));if(versionFiles){const debugPort=Number(fs.readFileSync(path.join(temp,'chrome-profile','DevToolsActivePort'),'utf8').split('\n')[0]);await fetch(`http://127.0.0.1:${debugPort}/json/version`).then(r=>r.json()).then(v=>CDP.connect(v.webSocketDebuggerUrl)).then(async browser=>{await browser.command('Browser.close',{},null);browser.close();}).catch(()=>{});}}catch{}
      await new Promise(resolve=>{const timer=setTimeout(resolve,5000);chrome.once('exit',()=>{clearTimeout(timer);resolve();});});
      if(chrome.exitCode===null)process.stderr.write(`Chrome remained open after graceful shutdown; PID ${chrome.pid}. No signal was sent.\n`);
    }
    if(server&&server.exitCode===null&&!serverStopped){server.stdin.write('\n');await new Promise(resolve=>server.once('exit',resolve));}
    fs.rmSync(temp,{recursive:true,force:true});
  }
}

main().catch(error=>{process.stderr.write(`Research browser smoke failed at ${currentStage}: ${String(error.message||error).replace(/https?:\/\/\S+/g,'[local endpoint]').slice(0,300)}\n`);process.exitCode=1;});
