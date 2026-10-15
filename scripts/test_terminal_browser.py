"""Optional Chromium tests. All terminal input goes to an inert PTY echo process.
Run with: python3 -m unittest discover -s scripts -p test_terminal_browser.py
Requires playwright and its Chromium browser. No SSH, FC or camera access.
"""
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

import pi_terminal as web

ECHO = """import os,sys,tty
 tty.setraw(0)
 os.write(1,b'TERMINAL_TEST_READY\\r\\n')
 while True:
     data=os.read(0,8192)
     if not data:break
     os.write(1,data)
""".replace('\n ', '\n')


class EchoSession(web.TerminalSession):
    def __init__(self):
        super().__init__(command=lambda mode: [sys.executable, '-u', '-c', ECHO])
        self.inputs=[]
    def send(self, data):
        self.inputs.append(data)
        super().send(data)


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Install playwright to run browser tests')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(headless=True, args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()
    def setUp(self):
        self.server=web.TerminalServer(('127.0.0.1',0),session_factory=EchoSession)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.context=self.browser.new_context(viewport={'width':1280,'height':900})
        self.page=self.context.new_page();self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.context.close();self.server.shutdown();self.thread.join();self.server.server_close()
        self.assertEqual(self.errors,[])
    def workspace(self):return next(iter(self.server.sessions.values()))
    def open_mode(self,mode='shell'):
        self.page.goto(self.url)
        self.page.locator('#'+mode).click()
        self.page.wait_for_function("selected()?.connected && selected()?.generation && selected()?.size")
        return self.workspace().get(self.page.evaluate('active'))
    def test_desktop_force_arm_confirmation_never_submits_enter(self):
        session=self.open_mode('mavproxy')
        self.page.locator('.commands summary').click()
        self.page.locator('#force-arm').click()
        self.assertFalse(self.page.locator('#force-arm-confirm').is_disabled())
        self.page.locator('#force-arm-confirm').click()
        self.page.wait_for_function("document.getElementById('message').textContent.includes('21196')")
        self.assertEqual(session.inputs,['long COMPONENT_ARM_DISARM 1 21196 0 0 0 0 0'])
        self.page.locator('#force-arm').click();self.page.locator('#force-arm-cancel').click()
        self.assertEqual(len(session.inputs),1)
    def test_tabs_restart_disconnect_reconnect_and_reload(self):
        first=self.open_mode()
        self.page.locator('#new').click();self.page.locator('#shell').click()
        self.page.wait_for_function("selected()?.connected && selected()?.generation && views.size===2")
        second=self.workspace().get(self.page.evaluate('active'))
        self.assertNotEqual(first.pid,second.pid)
        self.page.on('dialog',lambda dialog:dialog.accept())
        previous=second.pid;self.page.locator('#restart').click()
        self.page.wait_for_function("!busy && selected()?.connected")
        self.assertNotEqual(second.pid,previous);self.assertTrue(first.connected)
        self.page.locator('#disconnect').click();self.page.wait_for_function("!busy && !selected()?.connected")
        self.page.locator('#reconnect').click();self.page.wait_for_function("!busy && selected()?.connected")
        selected_id=self.page.evaluate('active');self.page.reload()
        self.page.wait_for_function("selected()?.connected && views.size===2")
        self.assertEqual(self.page.evaluate('active'),selected_id)
        self.page.locator('#close').click();self.page.wait_for_function('!busy && views.size===1')
        self.assertTrue(first.connected)
    def test_large_unicode_paste_fonts_expand_and_mobile(self):
        self.page.set_viewport_size({'width':390,'height':844})
        session=self.open_mode()
        text='α中🚁'*2000
        self.page.locator('#paste').click();self.page.locator('#paste-text').fill(text);self.page.locator('#paste-send').click()
        self.page.wait_for_function('selected().queue.then(()=>true)')
        self.assertEqual(''.join(session.inputs),text)
        self.assertTrue(all(len(part.encode())<=8192 for part in session.inputs))
        old=self.page.evaluate('fontSize');self.page.locator('#font-up').click()
        self.assertEqual(self.page.evaluate('fontSize'),old+1)
        self.page.locator('#fullscreen').click();self.assertTrue(self.page.locator('.terminal-panel').evaluate("el=>el.classList.contains('expanded')"))
        self.page.keyboard.press('Escape');self.assertFalse(self.page.locator('.terminal-panel').evaluate("el=>el.classList.contains('expanded')"))
        self.page.locator('#clear-screen').click();self.page.locator('#focus-terminal').click()
        self.page.keyboard.type('hello');self.page.wait_for_function('selected().queue.then(()=>true)')
        self.assertTrue(''.join(session.inputs).endswith('hello'))
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
    def test_codex_view_storage_blocked_and_empty_workspace(self):
        self.context.add_init_script("Object.defineProperty(window,'localStorage',{get(){throw new Error('blocked')}})")
        self.page.goto(self.url+'/codex')
        self.page.locator('#codex').click()
        self.page.wait_for_function("selected()?.connected && selected()?.mode==='codex'")
        self.assertFalse(self.page.locator('#force-arm').is_visible())
        self.assertTrue(self.page.locator('#codex').is_visible())
        self.assertEqual(len(self.workspace().terminals),1)
        self.assertEqual(self.workspace().get().inputs,[])
    def test_motor_test_limits_and_confirmation(self):
        session=self.open_mode('mavproxy')
        self.page.locator('.commands summary').click()
        self.page.locator('#throttle').fill('100')
        self.page.locator('#motor-test').click()
        self.assertIn('1–15%',self.page.locator('#message').inner_text())
        self.assertEqual(session.inputs,[])
        self.page.locator('#throttle').fill('5')
        self.page.on('dialog',lambda dialog:dialog.accept())
        self.page.locator('#motor-test').click()
        self.page.wait_for_function("document.getElementById('message').textContent.includes('DO_MOTOR_TEST')")
        self.assertEqual(session.inputs,['long DO_MOTOR_TEST 1 0 5 2 1 0 0'])
    def test_poll_connection_loss_recovers_without_replaying_input(self):
        session=self.open_mode()
        self.page.route('**/api/output?*',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"Test connection lost"}'))
        self.page.wait_for_function('unreachable')
        self.assertTrue(self.page.locator('#paste').is_disabled())
        self.assertIn('connection lost',self.page.locator('#connection').inner_text())
        self.page.unroute('**/api/output?*')
        self.page.wait_for_function('!unreachable')
        self.assertFalse(self.page.locator('#paste').is_disabled())
        self.assertEqual(session.inputs,[])
    def test_codex_embed_creates_own_tab_without_replacing_shell(self):
        first=self.open_mode()
        self.page.goto(self.url+'/codex')
        self.page.wait_for_function('views.size===2 && selected()?.mode===null')
        self.assertTrue(first.connected)
        self.page.locator('#codex').click()
        self.page.wait_for_function("selected()?.connected && selected()?.mode==='codex'")
        self.assertTrue(first.connected)
        self.page.reload();self.page.wait_for_function("selected()?.connected && selected()?.mode==='codex'")
        self.assertEqual(len(self.workspace().terminals),2)

    def test_input_failure_is_visible_and_not_retried(self):
        session=self.open_mode()
        self.page.route('**/api/input',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"Test offline"}'))
        self.page.locator('#focus-terminal').click();self.page.keyboard.type('a')
        self.page.wait_for_function("document.getElementById('message').textContent.includes('never retried')")
        self.assertEqual(session.inputs,[])


if __name__=='__main__':unittest.main()
