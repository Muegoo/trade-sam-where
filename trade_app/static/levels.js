/* Price-level proposals only. Execution and risk limits are checked by the server. */
(function(root){
  'use strict';
  function ema(values, period){
    const alpha=2/(period+1), result=[values[0]];
    for(let i=1;i<values.length;i++)result.push(alpha*values[i]+(1-alpha)*result[i-1]);
    return result;
  }
  function suggest(snapshot, order, now=Date.now()/1000){
    if(!snapshot?.connected)throw new Error('ยังไม่เชื่อมต่อข้อมูลราคา');
    const {tick,info}=snapshot;
    // Match the execution guard; broker server timestamps can lead the local clock.
    if(!tick||!info||!Number.isFinite(tick.time)||now-tick.time>180||!(tick.ask>tick.bid&&tick.bid>0))throw new Error('ราคายังไม่พร้อมหรือเก่าเกิน 180 วินาที รอ tick ใหม่ก่อน');
    if(snapshot.updated!==undefined&&(!Number.isFinite(snapshot.updated)||now-snapshot.updated>15))throw new Error('ข้อมูลบนหน้าจอเก่าแล้ว รอเชื่อมต่อและอัปเดตราคาก่อน');
    if(!['BUY','SELL'].includes(order.side)||!['MARKET','LIMIT','STOP'].includes(order.kind))throw new Error('เลือก Side และ Order type ให้ถูกต้อง');
    // Exclude the forming M15 bar, including it would change indicators intrabar.
    const bars=(snapshot.bars||[]).slice(0,-1);
    if(bars.length<80)throw new Error('ต้องมีแท่ง M15 ที่ปิดแล้วอย่างน้อย 80 แท่ง');
    if(bars.some((b,i)=>![b.open,b.high,b.low,b.close,b.time].every(Number.isFinite)||b.low<=0||b.high<Math.max(b.open,b.close,b.low)||b.low>Math.min(b.open,b.close)|| (i>0&&b.time<=bars[i-1].time)))throw new Error('ข้อมูลแท่งราคาไม่ถูกต้อง');
    const last=bars.at(-1);
    // Last closed bar must be adjacent to the current quote; reject stale history.
    if(tick.time<last.time+900||tick.time-last.time>1800)throw new Error('ประวัติ M15 ยังตามราคาไม่ทัน รอข้อมูลแท่งล่าสุด');
    const closes=bars.map(b=>b.close),fast=ema(closes,20),slow=ema(closes,50);
    const tr=bars.slice(1).map((b,i)=>Math.max(b.high-b.low,Math.abs(b.high-bars[i].close),Math.abs(b.low-bars[i].close)));
    let atr=tr.slice(0,14).reduce((a,b)=>a+b,0)/14;
    for(let i=14;i<tr.length;i++)atr=(atr*13+tr[i])/14;
    if(!Number.isFinite(atr)||atr<=0)throw new Error('ATR ยังไม่เพียงพอสำหรับแนะนำระยะ SL');
    const point=Number(info.point),size=Number(info.trade_tick_size)||point;
    if(!(point>0&&size>0))throw new Error('ไม่พบ tick size ของ Broker');
    const round=(value,up)=>Number(((up?Math.ceil(value/size-1e-8):Math.floor(value/size+1e-8))*size).toFixed(8));
    const buy=order.side==='BUY',entry=order.kind==='MARKET'?(buy?tick.ask:tick.bid):Number(order.entry);
    if(!Number.isFinite(entry)||entry<=0)throw new Error('กรอก Entry ของ Pending ก่อนแนะนำ SL/TP');
    const stops=Math.max(0,Number(info.trade_stops_level)||0)*point+size;
    const market=buy?tick.ask:tick.bid;
    if(order.kind==='LIMIT'&&!(buy?entry<=market-stops:entry>=market+stops))throw new Error('Limit Entry ต้องอยู่ต่ำกว่า Ask สำหรับ Buy หรือสูงกว่า Bid สำหรับ Sell และห่างตาม stop level');
    if(order.kind==='STOP'&&!(buy?entry>=market+stops:entry<=market-stops))throw new Error('Stop Entry ต้องอยู่สูงกว่า Ask สำหรับ Buy หรือต่ำกว่า Bid สำหรับ Sell และห่างตาม stop level');
    if(Math.abs(entry/size-Math.round(entry/size))>1e-6)throw new Error(`Entry ต้องตรง tick size ${size}`);
    const slope=fast.at(-1)-fast.at(-4),difference=fast.at(-1)-slow.at(-1);
    const trend=difference>atr*.1&&slope>0?'UP':difference<-atr*.1&&slope<0?'DOWN':'RANGE';
    const aligned=(buy&&trend==='UP')||(!buy&&trend==='DOWN'),counter=trend!=='RANGE'&&!aligned;
    const multiplier=aligned?1.5:counter?2:1.75;
    const recent=bars.slice(-20),support=Math.min(...recent.map(b=>b.low)),resistance=Math.max(...recent.map(b=>b.high));
    const reference=order.kind==='MARKET'?(buy?tick.bid:tick.ask):entry;
    const rawSL=buy?Math.min(entry-multiplier*atr,support-.25*atr,reference-stops):Math.max(entry+multiplier*atr,resistance+.25*atr,reference+stops);
    let sl=round(rawSL,!buy);
    if(order.slOverride!==undefined){
      sl=Number(order.slOverride);
      if(!Number.isFinite(sl)||sl<=0||!(buy?sl<entry&&sl<=reference-stops:sl>entry&&sl>=reference+stops)||Math.abs(sl/size-Math.round(sl/size))>1e-6)throw new Error('SL เดิมไม่ถูกต้องสำหรับ Entry นี้ กรุณาแก้ SL หรือเลือกแนะนำทั้ง SL/TP');
    }
    const risk=Math.abs(entry-sl),ratio=Number(snapshot.settings?.reward_ratio);
    if(!(ratio>0&&ratio<=20))throw new Error('ตั้งค่า Reward : Risk ก่อนแนะนำ TP');
    const tp=round(buy?Math.max(entry+risk*ratio,reference+stops):Math.min(entry-risk*ratio,reference-stops),buy);
    if(!(sl>0&&tp>0&&risk>0&&(buy?sl<entry&&entry<tp:tp<entry&&entry<sl)))throw new Error('คำนวณระดับราคาที่เหมาะสมไม่ได้');
    const warnings=[];
    if(counter)warnings.push('Side ที่เลือกสวนแนวโน้ม M15');
    if(trend==='RANGE')warnings.push('แนวโน้มยังไม่ชัดเจน / Sideways');
    if(buy&&resistance>entry&&resistance<tp)warnings.push('มี high ของ 20 แท่งล่าสุดก่อนถึง TP');
    if(!buy&&support<entry&&support>tp)warnings.push('มี low ของ 20 แท่งล่าสุดก่อนถึง TP');
    return {scope:snapshot.scope,side:order.side,kind:order.kind,entry,sl,tp,rr:Math.abs(tp-entry)/risk,
      atr,ema20:fast.at(-1),ema50:slow.at(-1),trend,aligned,multiplier,support,resistance,
      time:tick.time,bar_time:last.time,warnings,sl_overridden:order.slOverride!==undefined};
  }
  const api={suggest};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.TradeLevels=api;
})(typeof globalThis!=='undefined'?globalThis:this);
