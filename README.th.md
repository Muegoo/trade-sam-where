# Trade Sam Where Trading Workspace · MT5 Demo

เว็บแอปสำหรับติดตามพอร์ตทองคำ ส่งคำสั่ง Demo ด้วยปุ่มหรือ Auto Monitor ปิด Position ก่อนถึง TP บันทึกการเทรด และควบคุมความเสี่ยง รันบน Windows เครื่องเดียวกับ MetaTrader 5

มี 5 หน้า: **Overview**, **Trading Terminal**, **Trading Journal**, **AI & Analytics** และ **Risk Management** พร้อมโหมด Paper จำลองสำหรับลองใช้งานโดยไม่เชื่อม MT5

## เปิด Dashboard

ใช้ Python **3.12 64-bit** เปิด PowerShell ในโฟลเดอร์นี้:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m trade_app.server --mode simulation
```

เปิด **http://127.0.0.1:8000** หยุดเซิร์ฟเวอร์ด้วย `Ctrl+C`

เมื่อเปิด MT5 และล็อกอินบัญชี Demo แล้ว ให้หยุดโหมดจำลองและเปลี่ยนเป็น:

```powershell
.\.venv\Scripts\python.exe -m trade_app.server --mode mt5 --symbol XAUUSD
```

ตรวจชื่อทองใน Market Watch เช่น `XAUUSDm` หรือ `GOLD` และเปิด Algo Trading / อนุญาต Python Trading หากต้องการส่งคำสั่ง แอปใช้บัญชีที่ล็อกอินอยู่ใน MT5 ไม่เก็บรหัสผ่าน บัญชีเงินจริงส่งคำสั่งผ่านแอปไม่ได้

ตัวช่วยเปิดแอป: `.\start-dashboard.ps1` หรือ `.\start-dashboard.ps1 -Mode mt5 -Symbol XAUUSD` หาก PowerShell บล็อก `.ps1` ใช้คำสั่ง Python ข้างต้น

อ่าน [รายละเอียดฟีเจอร์ กติกากลยุทธ์ Risk และวิธีทดสอบ](docs/dashboard.md)

## สคริปต์แจ้งสัญญาณเดิม

สคริปต์ `gold_demo_alert.py` อ่านแท่ง H1 ที่ปิดแล้วของ XAU/USD จาก MT5 และใช้โมเดล logistic regression เรียนรู้ความสัมพันธ์ของราคาในอดีต เพื่อแสดง `WATCH_BUY`, `WATCH_SELL` หรือ `WAIT` ระหว่าง **19:00–22:00 น. วันจันทร์–ศุกร์ เวลาไทย** สคริปต์นี้ **ไม่มีโค้ดส่งออร์เดอร์** และจะหยุดหาก MT5 ไม่ได้เชื่อมต่อบัญชีเดโม ส่วน Dashboard สแกนตลอดเวลาที่เปิดแอปและส่งคำสั่งได้เมื่อผู้ใช้กดปุ่ม

## เตรียมใช้งาน

1. ติดตั้ง Python 3.12 แบบ 64 บิตและ MetaTrader 5 บน Windows
2. เปิด MT5 และล็อกอิน **บัญชีเดโม** ของโบรกเกอร์
3. ดูชื่อทองที่แท้จริงใน Market Watch เช่น `XAUUSD` หรือ `XAUUSDm`
4. เปิด PowerShell ในโฟลเดอร์นี้ แล้วรัน:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe gold_demo_alert.py --symbol XAUUSD --once
.\.venv\Scripts\python.exe gold_demo_alert.py --symbol XAUUSD
```

`--once` ตรวจการเชื่อมต่อและวิเคราะห์หนึ่งครั้งแม้อยู่นอกช่วงเวลา; คำสั่งสุดท้ายจะเฝ้าดูช่วงเวลาที่กำหนด โปรแกรมต้องเปิดค้างไว้ ไฟล์ `signals.csv` เก็บผลแต่ละแท่งที่ประเมิน ไม่มีการส่งคำสั่งซื้อขาย

## ขอบเขตของโมเดล

- ใช้ข้อมูลราคาย้อนหลังของโบรกเกอร์ใน MT5 และทดสอบกับช่วงข้อมูลที่ใหม่กว่า 250 แท่ง
- แสดงสัญญาณเฝ้าดูเฉพาะเมื่อผลทดสอบช่วงหลังมีสัญญาณอย่างน้อย 20 ครั้งและทายทิศทางถูกอย่างน้อย 55%; มิฉะนั้นแสดง `WAIT`
- ค่า `up_probability` เป็นค่าจากโมเดลที่ยังไม่ได้ปรับเทียบความน่าจะเป็น และ **ไม่ใช่โอกาสทำกำไร**
- ผลทดสอบนี้ยังไม่รวม spread, slippage, ค่าธรรมเนียม หรือการกำหนดจุดตัดขาดทุน จึงยังไม่เพียงพอสำหรับเปิดออร์เดอร์จริง

ก่อนพัฒนาต่อ ให้ตรวจชื่อสัญลักษณ์ทองและขนาดสัญญาจากโบรกเกอร์ แล้วทดสอบย้อนหลังแบบรวมต้นทุนและทดลองเดโมต่อเนื่อง
