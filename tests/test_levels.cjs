const assert=require('node:assert/strict');
const {suggest}=require('../trade_app/static/levels.js');
const now=1792100000,end=Math.floor(now/900)*900;
function fixture(direction=1){
  const bars=Array.from({length:101},(_,i)=>{
    const close=4000+direction*i;
    return {time:end-(100-i)*900,open:close-direction*.6,close,high:close+1,low:close-1};
  });
  const price=bars.at(-1).close;
  return {connected:true,scope:'test-account',tick:{time:now,bid:price,ask:price+.2},
    info:{point:.01,trade_tick_size:.01,trade_stops_level:20},settings:{reward_ratio:2},bars};
}
function geometry(p,side){
  assert.ok(side==='BUY'?p.sl<p.entry&&p.entry<p.tp:p.tp<p.entry&&p.entry<p.sl);
  for(const price of [p.sl,p.tp])assert.ok(Math.abs(price/.01-Math.round(price/.01))<1e-6);
  assert.ok(p.rr>=2-1e-8);
}
const buy=suggest(fixture(),{side:'BUY',kind:'MARKET'},now);
assert.equal(buy.trend,'UP');assert.equal(buy.multiplier,1.5);geometry(buy,'BUY');
const sell=suggest(fixture(-1),{side:'SELL',kind:'MARKET'},now);
assert.equal(sell.trend,'DOWN');geometry(sell,'SELL');
const counter=suggest(fixture(),{side:'SELL',kind:'MARKET'},now);
assert.equal(counter.multiplier,2);assert.ok(counter.warnings.some(w=>w.includes('สวน')));
const forming=fixture();forming.bars.at(-1).high+=10000;forming.bars.at(-1).close+=5000;
assert.deepEqual(suggest(forming,{side:'BUY',kind:'MARKET'},now),buy,'Forming candle must not affect indicator levels');
const moved=fixture();moved.tick.bid+=10;moved.tick.ask+=10;
const fresh=suggest(moved,{side:'BUY',kind:'MARKET'},now);
assert.equal(fresh.entry,buy.entry+10);assert.notEqual(fresh.tp,buy.tp);
for(const side of ['BUY','SELL'])for(const kind of ['LIMIT','STOP']){
  const snapshot=fixture(),market=side==='BUY'?snapshot.tick.ask:snapshot.tick.bid;
  const direction=(side==='BUY')===(kind==='STOP')?1:-1;
  const plan=suggest(snapshot,{side,kind,entry:market+direction*10},now);geometry(plan,side);
}
assert.throws(()=>suggest(fixture(),{side:'BUY',kind:'LIMIT'},now),/Entry/);
assert.throws(()=>suggest(fixture(),{side:'BUY',kind:'STOP',entry:4090},now),/Stop Entry/);
assert.throws(()=>suggest(fixture(),{side:'BUY',kind:'MARKET'},now+181),/180/);
assert.throws(()=>suggest({...fixture(),bars:fixture().bars.slice(0,30)},{side:'BUY',kind:'MARKET'},now),/80/);
const stale=fixture();stale.bars.forEach(b=>b.time-=3600);
assert.throws(()=>suggest(stale,{side:'BUY',kind:'MARKET'},now),/ประวัติ/);
const minStops=fixture();minStops.info.trade_stops_level=10000;
const distant=suggest(minStops,{side:'BUY',kind:'MARKET'},now);
assert.ok(distant.sl<=minStops.tick.bid-100);geometry(distant,'BUY');
const override=suggest(fixture(),{side:'BUY',kind:'MARKET',slOverride:4080},now);
assert.equal(override.sl,4080);assert.equal(override.tp,4140.6);assert.equal(override.sl_overridden,true);
assert.throws(()=>suggest(fixture(),{side:'BUY',kind:'MARKET',slOverride:4200},now),/SL เดิม/);
const tick25=fixture();tick25.info.trade_tick_size=.25;tick25.tick.ask=4100.25;
const rounded=suggest(tick25,{side:'BUY',kind:'MARKET'},now);
assert.equal(rounded.sl/.25,Math.round(rounded.sl/.25));assert.equal(rounded.tp/.25,Math.round(rounded.tp/.25));
console.log('SL/TP calculations passed: trend, volatility, quote changes, closed bars, Buy/Sell, Market/Limit/Stop, stale data, broker stops, tick rounding, and existing SL.');
