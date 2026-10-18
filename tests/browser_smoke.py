"""Exercise the real UI against an isolated PAPER server; never connect to MT5."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.request

import uvicorn
from playwright.sync_api import sync_playwright
from trade_app.server import create_app


def run():
    artifacts=Path(os.environ.get('TRADE_SCREENSHOT_DIR', 'artifacts'))
    artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        app=create_app(db_path=Path(folder)/'browser.db')
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8011,log_level='error'))
        worker=threading.Thread(target=server.run,daemon=True)
        worker.start()
        try:
            for _ in range(200):
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8011/api/state') as response:
                        state=json.load(response)
                    if state['connected']:break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError('Paper server did not become ready')
            with sync_playwright() as p:
                launch_options={'headless':True}
                if os.environ.get('TRADE_BROWSER_EXECUTABLE'):
                    launch_options['executable_path']=os.environ['TRADE_BROWSER_EXECUTABLE']
                browser=p.chromium.launch(**launch_options)
                page=browser.new_page(viewport={'width':1440,'height':1040},device_scale_factor=1)
                errors=[]
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8011/')
                assert page.title() == 'Trade Sam Where · Trading Workspace'
                assert page.locator('.brand').inner_text().startswith('TSW')
                page.wait_for_function("document.getElementById('accountCards').children.length === 4")
                page.screenshot(path=str(artifacts/'dashboard.png'),full_page=True)
                page.locator('.nav[data-page=terminal]').click()
                page.locator('#monitorHours').select_option('1')
                page.locator('#monitorStart').click()
                page.wait_for_function("document.getElementById('monitorQuick').textContent==='Auto: ON'")
                page.locator('#monitorStop').click()
                page.wait_for_function("document.getElementById('monitorQuick').textContent==='Auto: OFF'")
                for kind in ['MARKET','LIMIT','STOP']:
                    state=page.evaluate("async()=>await(await fetch('/api/state')).json()")
                    price=state['tick']['ask']
                    page.locator('#orderForm select[name=kind]').select_option(kind)
                    if kind!='MARKET':
                        page.locator('#orderForm input[name=entry]').fill(f'{price+(-1 if kind=="LIMIT" else 1):.2f}')
                    page.locator('#orderForm input[name=sl]').fill(f'{price-6:.2f}')
                    page.locator('#orderForm input[name=tp]').fill(f'{price+12:.2f}')
                    page.locator('#orderForm input[name=lot]').fill('.01')
                    page.locator('#orderForm button[type=submit]').click()
                    page.wait_for_function("!document.getElementById('sendOrder').disabled")
                    if kind=='MARKET':page.screenshot(path=str(artifacts/'terminal.png'),full_page=True)
                    page.locator('#sendOrder').click()
                    page.wait_for_function("document.getElementById('notice').textContent.includes('ส่งคำสั่งแล้ว')")
                    if kind!='MARKET':
                        page.locator('.cancel-order').last.click()
                        page.wait_for_function("document.getElementById('notice').textContent.includes('ยกเลิก Pending แล้ว')")
                page.locator('.close-position').first.click()
                assert page.locator('#closeDialog').is_visible()
                page.locator('#closeCancel').click()
                page.locator('.close-position').first.click()
                page.locator('#closeConfirm').click()
                page.wait_for_function("document.querySelectorAll('.close-position').length===0")
                page.locator('.nav[data-page=journal]').click()
                page.locator('#filters select[name=source]').select_option('Manual')
                assert page.locator('#journalTable tbody tr').count()>0
                with page.expect_download() as download_info:
                    page.locator('#exportCsv').click()
                download=download_info.value
                download.save_as(str(artifacts/'journal-smoke.csv'))
                assert 'Manual' in (artifacts/'journal-smoke.csv').read_text(encoding='utf-8-sig')
                page.screenshot(path=str(artifacts/'journal.png'),full_page=True)
                page.locator('.nav[data-page=analytics]').click()
                assert page.locator('.signal-card').count()==6
                page.screenshot(path=str(artifacts/'analytics.png'),full_page=True)
                page.locator('.nav[data-page=risk]').click()
                page.locator('#settingsForm input[name=max_lot]').fill('.05')
                page.locator('#settingsForm textarea[name=magic_names]').fill('999=Test EA')
                page.locator('#settingsForm button[type=submit]').click()
                page.wait_for_function("document.getElementById('notice').textContent.includes('บันทึกค่า')")
                page.locator('#killMain').click()
                page.wait_for_function("document.getElementById('killTitle').textContent.includes('หยุดรับ')")
                page.reload()
                page.wait_for_function("document.getElementById('killTop').textContent.includes('ระบบหยุด')")
                page.locator('.nav[data-page=risk]').click()
                assert page.locator('#settingsForm input[name=max_lot]').input_value()=='0.05'
                page.locator('#killMain').click()
                page.wait_for_function("document.getElementById('killTitle').textContent.includes('พร้อมรับ')")
                page.set_viewport_size({'width':390,'height':844})
                page.locator('.nav[data-page=dashboard]').click()
                page.screenshot(path=str(artifacts/'mobile.png'),full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile overflow'
                page.locator('.nav[data-page=terminal]').click()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Terminal mobile overflow'
                assert not errors,errors
                browser.close()
                print('Browser smoke passed: Monitor start/stop, early Position close dialog, Market, Limit, Stop, cancel, filters, CSV, settings, persistent kill, mobile layout; no JavaScript errors.')
        finally:
            server.should_exit=True
            worker.join(timeout=15)


if __name__=='__main__':run()
