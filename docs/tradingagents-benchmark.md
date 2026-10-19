# TradingAgents เทียบกับ trade-sam-where

วันที่ประเมิน: 2026-10-19

## ขอบเขตและข้อสรุป

รายงานนี้เปรียบเทียบความสามารถจากซอร์สโค้ดและเอกสารที่ระบุ โดยแยกสถานะเป็น **มีจริง**, **มีบางส่วน**, **ไม่พบ** และเสนอวิธีประเมินเชิงประจักษ์ในขั้นถัดไป รายงานนี้ไม่ได้วัดกำไร ความเร็ว หรือจัดอันดับผลการเทรด และไม่ได้รัน TradingAgents ในสภาพแวดล้อมจริง

ฝั่งในเครื่องอ้างอิง `trade-sam-where` ที่ commit `0b03430d1dda9b23b0e906b4dcbe3040ff01c854` ฝั่ง TradingAgents อ้างอิง upstream `main` ที่ commit `e8f5503e9b86d5c2710b0c76b1f686703968c9df`, version `0.7.0` / `v0.7.0`; changelog ระบุ release วันที่ 2026-10-16 และสัญญาอนุญาต Apache-2.0 ([release notes](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/CHANGELOG.md#L1), [LICENSE](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/LICENSE#L1))

ข้อสรุปหลัก: `trade-sam-where` มีพื้นฐานสาธิตการสแกน ส่งคำสั่งผ่าน MT5 Demo คุมความเสี่ยง และแสดง analytics จาก deals ที่ Broker รายงานหรือผล simulation อยู่แล้ว ผลเหล่านี้ไม่ใช่หลักฐานว่าระบบผ่านการใช้งานจริงใน production TradingAgents เพิ่มแนวทางค้นคว้าและสังเคราะห์ข้อมูล การถกทิศทาง หน่วยความจำ และ workflow ของ agent ควรนำแนวคิดเหล่านี้มาเป็นชั้นคำแนะนำที่จำกัดขอบเขต โดยคง risk gate เชิงตัวเลขเดิมและให้ Broker ยืนยันคำสั่ง

มีข้อไม่เข้ากันที่ต้องจัดการก่อนเทียบผลโดยตรง:

- TradingAgents แปลง `XAUUSD` เป็น `GC=F` ซึ่งเป็น proxy ของ Gold futures ไม่ใช่ราคา Spot ของ MT5; จึงไม่ใช่สินทรัพย์หรือชุดราคาที่เทียบกันตรง ๆ ([symbol mapping](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/symbols.py#L47), [mapping tests](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tests/test_symbols.py#L21))
- TradingAgents เลือก benchmark จาก override หรือ suffix ของตลาด และใช้ SPY เมื่อไม่มี suffix; config เริ่มต้นไม่มี mapping สำหรับทองคำ ดังนั้น alpha ของ XAUUSD/GC=F อาจเทียบกับ SPY โดยปริยาย ต้องกำหนด benchmark ที่เหมาะกับทองคำและ cash/no-trade reference ก่อนตีความผล ([benchmark resolution](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/memory/settlement.py#L14), [default config](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/default_config.py#L172))
- TradingAgents ประเมินโจทย์ด้วยวันและช่วงถือแบบ close-to-close ขณะที่ระบบนี้สแกน M15 และใช้โมเดล AI บน H1 ที่ปิดแล้ว การแบ่งเวลาและ target horizon จึงต้องกำหนดใหม่ให้ตรงกัน
- ขนาดสถานะจาก TraderProposal ของ TradingAgents เป็นข้อความอิสระ ไม่มีจำนวน lot ที่ตรวจชนิดข้อมูลได้ จึงนำไปส่งคำสั่งไม่ได้โดยตรง ([schema](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/schemas.py#L146))
- backtest ของ TradingAgents เปรียบเทียบคำตอบตาม ticker/date ไม่ได้จำลอง fill, ต้นทุน, เงินสด หรือพอร์ต จึงใช้แทนการ replay คำสั่งตามราคาและเงื่อนไข MT5 ไม่ได้ ([backtest](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/backtest.py#L88))

## ตารางความสามารถ

สถานะ **มีจริง** หมายถึงพบกลไกในโค้ดที่ทำหน้าที่นั้น, **มีบางส่วน** หมายถึงมีองค์ประกอบแต่ยังไม่ครบโจทย์ใช้งาน และ **ไม่พบ** หมายถึงไม่พบความสามารถนั้นในขอบเขตซอร์สที่ตรวจ การอ้างถึงเอกสาร README ของ upstream ใช้เป็นบริบทเท่านั้น เมื่อขัดกับพฤติกรรมในโค้ดให้ยึดโค้ด

| ความสามารถ | trade-sam-where | TradingAgents | ผลต่อการใช้งาน |
|---|---|---|---|
| Stack และขอบเขตระบบ | **มีจริง** — FastAPI, SQLite, HTML/JavaScript; dependencies ระบุ FastAPI, Uvicorn, pandas, httpx, scikit-learn ([requirements](../requirements.txt#L1), [server](../trade_app/server.py#L88), [UI](../trade_app/static/index.html#L23)) | **มีจริง** — workflow ใช้ LangGraph และ Pydantic schema ([graph setup](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L109), [schemas](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/schemas.py#L146)) | โครงสร้างต่างกัน; ไม่จำเป็นต้องย้ายทั้งระบบเพื่อทดลองแนวคิด agent |
| ตัวสแกนเทคนิค | **มีจริง** — SMC, supply/demand, EMA, Bollinger, Asian breakout และ AI classifier ([strategies](../trade_app/strategies.py#L34)) | **มีบางส่วน** — technical analyst สร้างรายงานจากข้อมูลตลาดและเครื่องมือ ไม่ใช่ชุดสแกน M15/กฎเดียวกัน ([graph flow](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L109)) | ควรรายงานผลแยกตามกฎเดิมเป็น baseline |
| Market data | **มีจริง** — แท่งและบัญชี MT5 ในโหมด Broker; simulation แยกต่างหาก ([broker](../trade_app/broker.py#L25), [simulation](../trade_app/broker.py#L131)) | **มีจริง** — ดึงข้อมูลตลาดผ่าน dataflows/vendor รวม Yahoo OHLCV และกรองตามวันอ้างอิง ([OHLCV](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/vendors/yahoo/ohlcv.py#L161)) | ต้องตรึงแหล่งข้อมูลและ symbol ให้ตรงก่อนเทียบ |
| Macro และข่าว | **ไม่พบ** — ไม่มีแหล่งข่าวหรือปฏิทินเศรษฐกิจในระบบปัจจุบัน | **มีจริง** — นักวิเคราะห์ข่าวและข้อมูลมหภาคเป็นขั้นใน workflow ([graph flow](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L109)) | เป็นช่องว่างที่อาจเพิ่มบริบท XAUUSD ได้ หากข้อมูลมี timestamp แบบ point-in-time |
| Social sentiment | **ไม่พบ** | **มีจริง** — social analyst เป็นหนึ่งในนักวิเคราะห์คู่ขนาน ([graph flow](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L109)) | จัดลำดับต่ำสำหรับโจทย์ทองคำ จนกว่าจะพิสูจน์ความเกี่ยวข้องของข้อมูลได้ |
| Fundamental เฉพาะทองคำ | **ไม่พบ** — ไม่มีชุดปัจจัย USD, อัตราดอกเบี้ย, real yield หรือปฏิทินเหตุการณ์ในระบบ | **มีบางส่วน** — มีนักวิเคราะห์ fundamentals แต่ตัวอย่างข้อมูลบริษัทและ filing ไม่เพียงพอแทนปัจจัยทองคำ | ต้องหา vendor ที่มีเวลาเผยแพร่/เวลาเข้าถึงย้อนหลัง; filing บริษัทไม่ใช่ข้อมูลพื้นฐานทองคำ |
| ลำดับ agent | **ไม่พบ** — ปัจจุบันเป็น scanner และ classifier | **มีจริง** — analyst ทำงานคู่ขนาน รวมผลแล้วผ่าน Trader, risk debate และ manager ตามรอบ ([graph setup](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L109), [round routing](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/conditional_logic.py#L12)) | ยืมเฉพาะส่วนวิเคราะห์ที่มี input และ output ชัดเจน |
| Bull/bear debate | **ไม่พบ** | **มีจริง** — bull และ bear researcher แลกเหตุผลก่อนสรุป ([graph setup](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/setup.py#L131)) | ทดลองเป็น challenger ได้ แต่ต้องวัดผลเทียบ analyst เดี่ยว |
| Risk และ sizing เชิงตัวเลข | **มีจริง** — ตรวจข้อมูลสด, spread, SL, lot, margin, daily loss และ exposure ก่อนส่ง ([risk](../trade_app/risk.py#L19)) | **มีบางส่วน** — risk agents ให้คำแนะนำ; ขนาดสถานะของ TraderProposal เป็น free text ไม่ใช่ lot เชิงตัวเลข ([proposal](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/schemas.py#L146)) | ค่าตัวเลขและการอนุมัติคำสั่งต้องอยู่ใน risk gate ปัจจุบัน |
| Broker execution | **มีจริงแบบจำกัด** — MT5 `order_check` / `order_send` ใช้กับบัญชี Demo; บัญชีจริงอ่านข้อมูลได้แต่ระบบไม่อนุญาตให้ส่งคำสั่ง ([broker](../trade_app/broker.py#L25), [order path](../trade_app/broker.py#L64), [Demo risk gate](../trade_app/risk.py#L33)) | **ไม่พบ** — workflow ส่งข้อเสนอ ไม่ใช่ MT5 broker adapter ([portfolio context](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/portfolio.py#L1)) | ไม่ควรอ้างว่าระบบใดทำ autonomous live execution จากหลักฐานนี้ |
| Historical evaluation | **มีบางส่วน** — AI มี holdout ตามลำดับเวลา แต่ไม่พบ walk-forward หรือ execution replay ในเส้นทางนี้ ([AI validation](../gold_demo_alert.py#L33)) | **มีบางส่วน** — hit rate และ mean alpha จากตัวอย่างแยก ticker/date ไม่มี fill ledger หรือต้นทุนจริง ([backtest](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/backtest.py#L88), [test scope](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tests/test_backtest.py#L1)) | ทั้งคู่ยังไม่ทดแทน replay ที่จำลองต้นทุนและการจับคู่คำสั่งได้ |
| Analytics จาก deal / simulation | **มีจริง** — สถิติแยก Magic/strategy คำนวณจาก deals ที่ Broker รายงานและ lifecycle ที่ปิด; โหมด paper แสดงผลจาก simulation ([analytics](../trade_app/analytics.py#L21), [metrics](../trade_app/analytics.py#L37), [simulation](../trade_app/broker.py#L131)) ผลทั้งสองแหล่งไม่ใช่หลักฐาน production | **มีบางส่วน** — settlement เทียบ close-to-close กับ benchmark และสร้าง reflection หลัง horizon ([settlement](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/memory/settlement.py#L52)) | ระบบนี้ยังขาด immutable link ระหว่างสัญญาณ snapshot ถึง fill |
| Memory และ reflection | **ไม่พบ** — ไม่พบวงจรบันทึกผลแล้วสร้าง reflection ให้การวิเคราะห์รอบถัดไป | **มีจริง** — เก็บ reflection เมื่อผลถึงวันสรุปและผ่าน resolved-date gate ([settlement](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/memory/settlement.py#L114), [memory gate](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/memory/log.py#L93)) | ควรผูกความจำกับ fill และต้นทุนที่เกิดขึ้นจริง |
| Proposal ที่ตรวจสอบย้อนหลังได้ | **มีบางส่วน** — มี preview และ idempotency key แต่ยังไม่มี signal-to-fill snapshot แบบ immutable ([send preview](../trade_app/server.py#L240), [store](../trade_app/store.py#L14)) | **มีบางส่วน** — schema ระบุ action, เหตุผล และระดับราคาบางชนิด แต่ sizing เป็นข้อความและ structured wrapper มี fallback เป็นข้อความ ([schema](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/schemas.py#L146), [structured output](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/agents/structured.py#L42)) | ต้องทำ trace ที่เชื่อมข้อเสนอ การตรวจความเสี่ยง คำสั่ง และ deal |
| Checkpoint / resume | **ไม่พบ** — ไม่พบ graph checkpoint สำหรับ workflow วิเคราะห์ | **มีจริง** — SQLite checkpoint แยก signature และล้างเมื่อสำเร็จ ([checkpointer](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/checkpointer.py#L1), [graph use](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/graph/trading_graph.py#L140)) | เพิ่มเมื่อ workflow ใหม่จำเป็นต้อง resume เท่านั้น |
| UI | **มีจริง** — dashboard มี Terminal, journal, analytics และ risk ([UI](../trade_app/static/index.html#L23)) | **ไม่เปรียบเทียบในรายงานนี้** — ไม่ได้ตรวจ CLI หรือ saved report UI | เปรียบเทียบเฉพาะ UI ที่ตรวจใน source scope |
| LLM provider | **ไม่พบ** — ไม่พบ abstraction สำหรับ external LLM provider ใน implementation นี้ ([requirements](../requirements.txt#L1)) | **มีจริง** — มี factory แยก provider ([provider factory](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/llm_clients/factory.py#L6)) | provider abstraction ของ upstream เป็นความสามารถแยกจาก execution |
| Tests | **มีจริง** — CI มี Node/Python test jobs ([CI](../.github/workflows/ci.yml#L5)) | **มีจริง** — มี tests สำหรับ backtest และ workflow ที่รายงานนี้ตรวจ ([backtest tests](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tests/test_backtest.py#L1)) | การมี tests ไม่ได้ยืนยันว่ารันผ่าน; ชุดทดสอบ upstream ไม่ได้ถูกรันในงานนี้ |

## หลักฐานและข้อจำกัดที่พบ

**หมายเหตุการติดตาม implementation:** ตารางเปรียบเทียบด้านบนเป็น baseline จากการตรวจซอร์ส ณ วันที่ในรายงาน และยังคงเป็นภาพย้อนหลังของชุดตรวจครั้งนั้น ความคืบหน้าหลังรายงาน baseline รวมถึง Research API/UI สำหรับ collection, paired comparison และ forward shadow อยู่ใน [Research guide](research.md) และบันทึกอัปเดตท้ายเอกสาร การมีฟีเจอร์หรือ tests ไม่ได้ยืนยันผลตอบแทนหรือความถูกต้องกับบัญชีจริง; ไม่มีการส่งคำสั่งจาก Research

ระบบในเครื่อง refresh ทุก 3 วินาที ใช้แท่ง M15 ที่ปิดแล้วสำหรับ scanner และแท่ง H1 ปิดแล้วสำหรับ AI; มีตัวกรองสัญญาณ 5 กฎกับ logistic classifier ([poll และ timeframe](../trade_app/server.py#L117), [scanner](../trade_app/strategies.py#L34))

โมเดล `gold_demo_alert.py` ใช้ feature 7 ตัว แบ่ง holdout ตามเวลา 250 แถวสุดท้าย ตรวจจำนวนสัญญาณอย่างน้อย 20 และ accuracy อย่างน้อย 55% ก่อน fit ใหม่บนข้อมูลทั้งหมดที่มี ([validation](../gold_demo_alert.py#L33), [gate และ refit](../gold_demo_alert.py#L70)) เป็นการตรวจทิศทางแท่ง H1 ถัดไป ไม่ใช่ walk-forward หรือการทดสอบกำไรสุทธิ ระบบไม่มี purge ตาม target horizon ที่ระบุไว้ใน validation นี้ จึงควรตรวจการทับซ้อนของ label และช่วงถือเมื่อทำ replay

Monitor กันซ้ำตาม session และสัญญาณ ตรวจ stale data, exposure และสัญญาณขัดกัน แต่เมื่อต้องเลือกผู้สมัครพร้อมกันใช้จำนวนตัวอย่างย้อนหลังที่น้อยกว่าเป็นลำดับก่อน ไม่ใช่การจัดอันดับตามผลตอบแทน ([monitor](../trade_app/monitor.py#L81), [ordering](../trade_app/monitor.py#L158))

Paper broker ใช้แท่งสังเคราะห์ที่ขึ้นกับเวลา, spread คงที่ `0.20`, commission และ fill แบบง่าย ไม่ได้จำลอง historical replay หรือ slippage ([simulation broker](../trade_app/broker.py#L131)) เอกสารพอร์ตระบุข้อจำกัดเรื่องต้นทุนและการตรวจผลด้วย ([portfolio caveats](portfolio.md#L23))

ระบบเก็บ settings, equity, requests, simulation, events และ session ใน SQLite แต่ไม่พบ snapshot immutable ที่เชื่อม signal, features, risk verdict, request, broker result และ deals เดียวกัน ([store](../trade_app/store.py#L14)) analytics ใช้ Magic Number แยกกลยุทธ์และคำนวณจากผล realized ([metrics](../trade_app/analytics.py#L21))

ฝั่ง TradingAgents มีเครื่องมือจัดวัน point-in-time และกรองข้อมูลที่ไม่มีวันที่หรือเป็น vintage ปัจจุบันออก ([date window](https://github.com/TauricResearch/TradingAgents/blob/e8f5503e9b86d5c2710b0c76b1f686703968c9df/tradingagents/dataflows/date_window.py#L134)) ความน่าเชื่อถือย้อนหลังยังขึ้นกับ archive ของ vendor; กลไกกรองวันที่ไม่ใช่หลักฐานว่าข้อมูลทุกชุดเป็น vintage ที่สมบูรณ์

## ลำดับงานที่แนะนำ

| ลำดับ | งาน | เกณฑ์รับงานที่วัดได้ |
|---|---|---|
| P0 | Historical replay และ execution model จากราคา MT5 จริง | ระบุ symbol/spec, Bid/Ask พร้อม timestamp, fee/commission/swap, slippage และ session; ตรึง dataset/config hash; กำหนดผลกรณีแท่งเดียวแตะ SL และ TP; replay ชุดเดิมซ้ำได้ผลเดิม |
| P0 | Gold data contract และ point-in-time snapshot | ทุกแถวมี source, instrument จริง, timeframe, timezone/market time และเวลาที่ event เกิด/เผยแพร่/พร้อมใช้/ดึงข้อมูล; กำหนด freshness; ปฏิเสธข้อมูลอนาคตหรือเวลาประกาศไม่ทราบใน backtest |
| P0 | Decision trace | มี trace ID เชื่อมแท่งปิด, snapshot/features, strategy/version, risk verdict, order request, broker result และ deals/net R; แยก unavailable ออกจาก neutral; รักษา idempotency |
| P1 | Walk-forward และ baseline ที่กันข้อมูลรั่วข้ามเวลา | รายงานแยกแต่ละ strategy; purge ตาม target horizon และ embargo ตามช่วงถือ; holdout ไม่ถูกนำมา fit ซ้ำ; ประเมิน net cost, risk และ regime; วัด calibration เมื่อใช้คะแนนแบบความน่าจะเป็น |
| P1 | Analyst แบบคำแนะนำและ event filter | รับบริบท technical ร่วมกับ macro/news ทองคำแบบ point-in-time เช่น USD, rates, real yield, Fed/CPI/NFP เมื่อมี vendor ใช้ได้; challenger ตรวจ proposal; รองรับ WAIT/NO_TRADE พร้อมเหตุผลและหลักฐาน; snapshot เก่าทำให้ proposal หมดอายุ |
| P1 | ขอบเขตการเรียก LLM | เริ่ม manual preview หรือ shadow mode; เรียกหลังแท่งปิดหรือ event แบบ asynchronous พร้อม timeout, cost/cache budget; ห้ามเรียกใน monitor loop 3 วินาที; analyst ล้มเหลวต้องไม่สร้างคำสั่งใหม่จาก AI; ข้อเสนอเป็น typed schema ที่ผ่าน validation และไม่มี free-text fallthrough ไป execution; เนื้อหาข่าว/social ถือเป็นข้อมูลไม่น่าเชื่อถือเพื่อป้องกัน prompt injection |
| P1 | ปฏิทิน blackout และ regime labels | ตั้งช่วงก่อน/หลังข่าวแรงแบบ UTC, calendar และ symbol; แสดงเวลาที่กลับมาทำงานและเหตุผล; จัดกลุ่ม trend/range/volatility เพื่อวัดผล filter โดยไม่อ้างว่าทำกำไร |
| P2 | Outcome memory | สรุปจาก fill ที่ปิดจริงเป็น net R และ horizon; เทียบ proposal กับ risk/fill จริง; ห้ามนำผลที่ยังไม่ resolved ไปเป็นความทรงจำของอนาคต |
| P2 | Provider และ observability | เพิ่ม abstraction เมื่อเริ่ม workflow ใหม่; บันทึก model/prompt/config version, latency และ token/cost budget; checkpoint ใช้เมื่อ workflow ต้อง resume |
| P2 | Portfolio ข้าม symbol | เพิ่มเมื่อขยายเกินหนึ่ง symbol โดยรวม exposure และความเสี่ยงบัญชี ไม่ใช่ตรวจแยกคำสั่งอย่างเดียว |

การเพิ่ม social หรือ equity-company fundamentals ไม่ใช่งานแรกสำหรับทองคำ เพราะชนิดข้อมูลยังไม่ตรง target หลัก ลำดับนี้เป็นการจัดตามความพอดีกับโจทย์ ไม่ใช่ข้อสรุปว่าข้อมูลเหล่านั้นไม่มีประโยชน์ในทุกกรณี

### รายละเอียดผลส่งมอบที่จำเป็น

**Historical replay:** บันทึก bid/ask ที่ใช้ตัดสินใจและลำดับ tick/bar ที่จำลองการออกคำสั่ง แยกค่าที่โบรกเกอร์กำหนดจากสมมติฐาน slippage ของ replay ให้ชัดเจน ระบุวิธีตัดสินกรณีราคาในแท่งเดียวผ่านทั้ง SL และ TP เพื่อให้ replay ซ้ำได้โดยไม่เลือกผลย้อนหลังตามใจ

Replay ต้องใช้ค่าต้นทุนที่สังเกตหรือระบุได้ตาม instrument และบัญชี เช่น commission, swap และ fee; หากค่าใดหาไม่ได้ให้แสดงว่า unavailable และไม่ถือเป็นศูนย์โดยอัตโนมัติ ใช้ข้อมูล quote/time ที่ตรงกับตลาดของ Broker แทนการอนุมานจากราคา proxy

**Decision trace:** เก็บข้อมูลตามลำดับ lifecycle ต่อไปนี้ โดยใช้ trace ID เดียวกัน:

- Snapshot ของแท่งปิด, quote และเวลาที่ดึงข้อมูล
- Feature values, strategy และ version ที่สร้างข้อเสนอ
- ข้อเสนอ/เหตุผลและสถานะ stale หรือ invalid หากมี
- Risk verdict พร้อมค่าที่ทำให้ผ่านหรือปฏิเสธ
- Preview/request ID และ payload เชิงตัวเลขที่ส่งให้ Broker
- Broker response, deal/fill จริง, ค่าธรรมเนียม และผลสุทธิใน R เมื่อปิดครบ

Trace ที่หยุดก่อน Broker response ต้องระบุสถานะ pending/unknown ให้ชัด เพื่อไม่ให้ผลการวิเคราะห์นับ request ที่ยังไม่ทราบผลเป็น fill หรือเป็น no-trade

**Analyst และ event filter:** ข้อมูลประกอบทองคำอย่าง USD, rates, real yield และข่าว Fed/CPI/NFP ต้องมีเวลาพร้อมใช้ย้อนหลังและ freshness limit; ถ้า vendor ไม่มี archive ที่บอกเวลาเหล่านี้ ให้ข้อมูลนั้นใช้ได้เฉพาะการทดลอง forward ที่เวลาเกิดจริง

ช่วง blackout ต้องแสดง calendar, symbol, เวลาก่อน/หลัง event และเวลาอนุญาตครั้งถัดไป ผลการทดลองต้องแยกผลของ blackout ออกจาก analyst และ debate เพื่อระบุว่าการปฏิเสธสัญญาณมาจากกฎใด

**Walk-forward:** รายงาน fold และช่วงตลาดที่ใช้ fit, validate และ test ทุกครั้ง ระบุ horizon ของ label และช่วง embargo พร้อมบันทึกว่าส่วนใดถูกกันออกจาก training; ห้าม refit ด้วยแถวที่อยู่ถัดจากจุดตัดของ fold ที่กำลังทดสอบ

## Protocol สำหรับ benchmark เชิงประจักษ์

| ชุดเปรียบเทียบ | องค์ประกอบ |
|---|---|
| A — rules baseline | รายงาน scanner 5 กฎปัจจุบันแยก strategy |
| B — classifier baseline | logistic AI ปัจจุบัน แยกจากกฎ |
| C — technical analyst | เพิ่ม analyst แบบ typed จากบริบท technical |
| D — macro/news | เพิ่มข้อมูลทองคำและ event filter แบบ point-in-time |
| E — challenger | เพิ่ม bull/bear หรือ challenger ตรวจ proposal; แยกผลจากการเพิ่มข้อมูลใน D |

ทุกชุดใช้ data snapshot เดียวกัน, instrument/spec MT5 เดียวกัน, เงินทุน, risk limit, execution cost และ session เดียวกัน จัด train/validation/test ตามลำดับเวลา ครอบคลุมหลาย regime และ purge/embargo ตาม target กับ holding horizon เลือก benchmark ที่เหมาะกับทองคำ และเทียบกับ buy/hold ของ instrument เดียวกันรวมถึง cash/no-trade control ก่อนแปลผล alpha หากมี LLM ให้ pin prompt/model settings และทำซ้ำหลายรอบโดย cache หลักฐานที่ส่งเข้าโมเดล

รายงาน net expectancy หน่วย R, profit factor, maximum drawdown, จำนวน trade, turnover/exposure, proposal ที่ reject/stale, spread/slippage, การละเมิด data/risk, latency P50/P95 และ token/model cost ต่อการตัดสินใจ วัด calibration เฉพาะเมื่อมีคะแนนความน่าจะเป็น ใช้การเปรียบเทียบแบบ paired ตามช่วงเวลาและรายงานความไม่แน่นอน ไม่ตั้งเป้ากำไรล่วงหน้าโดยไม่มี baseline

ประตูเชิงปฏิบัติการสำหรับชุดทดสอบควรเป็นคำสั่งซ้ำ 0 ครั้ง, การเปิดเผยข้อมูลอนาคต 0 ครั้ง และการข้าม risk gate สำเร็จ 0 ครั้ง พร้อม replay ที่ทำซ้ำได้ เกณฑ์ผลตอบแทนให้กำหนดหลังเห็น baseline และผ่าน shadow/demo กับ forward test ก่อน รายงานนี้ไม่ได้ยืนยันว่าประตูใดผ่านแล้ว

### การเตรียมข้อมูลและการตัดสินใจ

ก่อนสร้างชุดทดลอง ให้ระบุข้อมูลต่อไปนี้ใน manifest เดียวกัน:

- Symbol ของ Broker และ instrument ที่ feed ให้จริง แยกจากชื่อ alias เช่น `XAUUSD` และ proxy `GC=F`
- Timeframe ของแท่ง, timezone, เวลาตลาด และกติกาการเลือกแท่งที่ปิดแล้ว
- เวลาของ event, เวลาเผยแพร่, เวลาเริ่มเข้าถึงได้ และเวลาที่ระบบดึงข้อมูล
- เวลาที่ snapshot ถูกตรึง, vendor/source, version ของ strategy และ feature schema
- Bid/Ask ที่ใช้ตัดสินใจและราคาที่จำลอง fill พร้อมค่า spread, slippage, commission, swap และ fee
- Risk limit, เงินทุนเริ่มต้น, session และเงื่อนไขการถือ/ปิด position

ให้ปฏิเสธ feature ที่มีเวลาพร้อมใช้หลังเวลาตัดสินใจ รวมทั้งแถวที่ไม่ทราบเวลาเผยแพร่ในการทดสอบย้อนหลัง สำหรับข่าวหรือปฏิทิน ให้เก็บทั้งเวลาประกาศตามปฏิทินและเวลาที่ feed เห็นข้อมูลจริง

หนึ่ง sample ควรมี decision time, target/holding horizon, action และผลลัพธ์ตามกติกาเดียวกันทุก variant ถ้า horizon ของ target หรือการถือทับกัน ให้กำหนด purge และ embargo ก่อนแบ่ง fold ไม่ปรับเกณฑ์หลังเห็น test result

ตัวเปรียบเทียบ D และ E ควรเก็บ prompt/model settings, ข้อความ input ที่สร้างจาก snapshot และ output ที่ผ่าน schema เพื่อให้ทำซ้ำได้ การ cache หลักฐานช่วยแยกความเปลี่ยนแปลงของการตัดสินใจออกจากการเปลี่ยนข้อมูลที่ feed ให้โมเดล

Proposal ทุกตัวต้องบันทึกสถานะที่ผ่าน validation, เหตุผล, evidence reference, snapshot time และวันหมดอายุ หากข้อมูล stale หรือไม่มี ให้เป็น reject พร้อมเหตุผล ไม่แปลงความขาดหายเป็น neutral โดยเงียบ ๆ

### การอ่านผลและการเปรียบเทียบ

| กลุ่มตัวชี้วัด | สิ่งที่รายงาน | การอ่านผล |
|---|---|---|
| ผลตอบแทนสุทธิ | expectancy ต่อ R, profit factor, net return หลังต้นทุน | เทียบได้ต่อเมื่อใช้ต้นทุนและ risk rule เดียวกัน |
| ความเสี่ยง | maximum drawdown, exposure, turnover และขนาดตัวอย่าง | รายงานร่วมกับผลตอบแทน ไม่ใช้ win rate เดี่ยว ๆ |
| คุณภาพข้อเสนอ | จำนวน reject/stale, no-trade, data/risk violation | แยกกรณีข้อมูลไม่พร้อมออกจากคำแนะนำที่เป็นกลาง |
| Execution | spread, slippage และความแตกต่างระหว่างราคา signal กับ fill | ใช้ราคาจับคู่และเงื่อนไขของ replay ที่บันทึกไว้ |
| ระบบวิเคราะห์ | latency P50/P95, tokens และค่าใช้จ่ายต่อ decision | เก็บต่อ variant และต่อรอบ LLM ที่ทำซ้ำ |
| ความไม่แน่นอน | paired result ตามช่วงเวลาและช่วงความเชื่อมั่น/การกระจาย | แสดงหลายช่วงตลาด ไม่สรุปจาก sample เดียว |

ค่า calibration ใช้ได้เฉพาะ output ที่มีความหมายเป็นความน่าจะเป็นและมี target ที่นิยามไว้ล่วงหน้า หาก variant เพิ่มทั้ง news และ debate ในเวลาเดียวกัน จะระบุไม่ได้ว่าส่วนใดทำให้ผลเปลี่ยน จึงแยก D กับ E เป็น ablation คนละชุด

### เงื่อนไขก่อนขยับจาก shadow mode

เริ่มด้วยการสร้าง proposal และบันทึกผลโดยไม่ส่งคำสั่งจาก LLM จากนั้นตรวจ trace และ replay ว่า snapshot, risk verdict และผล broker เชื่อมกันครบ

ก่อนเปิดให้มีผลต่อการส่งคำสั่ง ต้องตรวจว่าข้อมูล stale ทำให้ proposal หมดอายุ, output ที่ผิด schema ไม่ผ่าน, analyst timeout ไม่สร้างคำสั่ง, และ retry ไม่สร้าง request ซ้ำ

ตัวเลขสำหรับความสำเร็จด้านผลตอบแทนยังไม่กำหนดในเอกสารนี้ ต้องเห็น A/B baseline, ผลตาม regime และความไม่แน่นอนก่อนตั้งเกณฑ์

ผล shadow/demo เป็นการตรวจพฤติกรรมในสภาพแวดล้อมจำลองหรือทดลองเท่านั้น ยังไม่ยืนยันผลจริงในอนาคต; forward validation ต้องติดตามทั้งความแตกต่างของราคา, ต้นทุนจริงและเหตุการณ์ที่ทำให้ระบบปฏิเสธ proposal

## แผนส่งมอบตาม dependency

1. ตรึง dataset แบบ point-in-time พร้อม decision trace
2. สร้าง replay และรัน baseline A/B
3. ทดลอง analyst กับ event filter ใน shadow mode
4. ทำ ablation และ walk-forward จากนั้นตรวจต่อใน demo forward validation

หากนำโค้ดจาก TradingAgents มาใช้ ให้คงข้อความ Apache-2.0 และ NOTICE ที่เกี่ยวข้องตามเงื่อนไขใบอนุญาต; การนำแนวคิดมาปรับใช้ไม่จำเป็นต้องยก framework ทั้งชุดเข้ามา

## การตรวจสอบตอนเริ่ม benchmark (เก็บประวัติเดิม)

- `node tests/test_levels.cjs` ผ่าน: ครอบคลุม SL/TP, แนวโน้ม/ความผันผวน, quotes, closed bars, buy/sell, market/limit/stop, stale data, stops และ tick rounding
- Python executable `python` ไม่มีใน environment; ลอง `python3 -m unittest discover -s tests -p 'test_*.py'` ด้วย Python 3.10.11 แต่ CI ระบุ Python 3.12 และ import ไม่ผ่านเพราะไม่มี `fastapi` กับ `sklearn` จึง **NOT TESTED** พฤติกรรม Python (มี 3 โมดูล import error ไม่ใช่ผลทดสอบ 3 กรณี)
- ไม่ได้ติดตั้ง dependencies, ไม่ได้เชื่อม MT5 demo, ไม่ได้ดึงข้อมูลภายนอกหรือเรียก LLM เพื่อ benchmark; ไม่ได้รัน upstream tests
- ความพยายามค้นผ่าน Gemini ถูกปฏิเสธด้วย `IneligibleTierError` / `UNSUPPORTED_CLIENT`; ข้อเท็จจริง upstream ในรายงานจึงอ้างจากการตรวจ primary GitHub source ที่ commit ข้างต้น ไม่ได้อ้างผลค้นหา

## อัปเดตการพัฒนา 2026-10-19

หลังการตรวจเริ่มต้น ผู้ใช้อนุมัติ `.venv` และ `requirements.txt`; Python 3.12 พร้อม dependencies แล้ว และ full Python suite ผ่าน 113 tests. เพิ่ม offline datasets, replay และ trace, walk-forward evaluation, calendar filtering, shadow/advisor analysis และ Research UI สำหรับนำเข้าไฟล์ ดูงาน และดาวน์โหลดผล; browser smoke ผ่านการนำเข้าและ replay จริงใน app แบบ local ด้วย Chrome บน desktop/mobile โดยยืนยันว่าไม่มีคำสั่งวิจัยเปลี่ยน positions หรือ orders. การตรวจนี้ยังไม่มี benchmark จาก historical broker dataset, real provider call หรือ MT5 forward validation; upstream tests ไม่ได้รัน. การอัปเดตนี้ไม่เปลี่ยนตาราง baseline ที่ pin ไว้ด้านบน และไม่ได้ยืนยันว่า P2 ทั้งหมดเสร็จ.

การติดตามผลปัจจุบัน: Research UI มี MT5-only historical collection พร้อม report และ dataset download, A/B comparison แบบ paired folds และ forward shadow ที่รอแท่งใหม่และบันทึก virtual decisions โดยไม่มี broker fills. Provider เป็น optional และปิดเป็นค่าเริ่มต้น; API key ไม่ถูกบันทึกลงไฟล์หรือผลลัพธ์ งาน advisor/comparison ใช้ key เฉพาะระหว่าง request ส่วน forward เก็บไว้ในหน่วยความจำชั่วคราวระหว่าง run และล้างเมื่อหยุดหรือสิ้นสุด. Backend Python suite ผ่าน 137 tests และ browser smoke ตรวจ flow ใน simulation รวมถึง completed A/B metrics, C/D unavailable, dataset downloads, forward start/stop, desktop/mobile layout และ positions/orders ที่ไม่เปลี่ยน. ไม่มี MT5 collection, external provider call หรือหลักฐานกำไรจากตลาดจริงในการตรวจนี้.
