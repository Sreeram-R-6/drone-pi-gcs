"""Optional Chromium geofence tests with fake telemetry/operations, never real MAVLink."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

import web_dashboard
from geofence_settings import SCHEMA


class FakeFences:
    def __init__(self):
        self.job={'state':'idle'};self.requests=[];self.number=0
        self.telemetry={'connected':True,'armed':False,'fence_supported':True,
            'position':{'lat':12.3456789,'lon':76.1234567,'relative_alt_m':0.12},
            'home':{'lat':12.3456789,'lon':76.1234567},'gps_fix':3,'satellites':12,
            'settings':{'FENCE_ENABLE':0,'FENCE_TYPE':7,'FENCE_ACTION':1,'FENCE_MARGIN':2,'FENCE_ALT_MAX':100}}
    def status(self):return {'telemetry':self.telemetry,'job':self.job,'settings_schema':SCHEMA}
    def launch(self,operation,data):
        self.requests.append((operation,data));self.number+=1;key=str(self.number)
        self.job={'id':key,'state':'complete','operation':operation,'ok':True,'message':'Simulated verification complete',
            'upload_verified':operation=='upload','applied':data.get('changes',{}),'settings':{**self.telemetry['settings'],**data.get('changes',{})},'plan':data.get('plan') if operation=='upload' else {'version':1,'fences':[]}}
        return {'accepted':True,'job_id':key}
    def close(self):pass


@unittest.skipUnless(importlib.util.find_spec('playwright'),'Install playwright and Chromium for browser tests')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start();cls.browser=cls.playwright.chromium.launch(headless=True,args=['--no-sandbox'])
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.playwright.stop()
    def setUp(self):
        self.fences=FakeFences();self.server=web_dashboard.DashboardServer(('127.0.0.1',0),{'sites':[]},fences=self.fences)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.context=self.browser.new_context(viewport={'width':1280,'height':900},accept_downloads=True,has_touch=True)
        self.page=self.context.new_page();self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        # Tile outages must not stop local drawing or MAVLink-facing UI.
        self.page.route('https://tile.openstreetmap.org/**',lambda route:route.abort())
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/geofence')
        self.page.wait_for_function('() => telemetry.connected===true')
    def tearDown(self):
        self.context.close();self.server.shutdown();self.thread.join();self.server.server_close();self.assertEqual(self.errors,[])
    def add_circle(self):
        self.page.locator('details').click();self.page.locator('#radius').fill('30');self.page.locator('#add-circle').click()
        self.page.wait_for_function('() => snapshot().fences.length===1')
    def test_coordinates_manual_circle_inclusion_edit_and_local_clear(self):
        self.assertIn('12.3456789',self.page.locator('#coordinates').inner_text())
        self.add_circle();self.assertEqual(self.page.evaluate('snapshot().fences[0].radius'),30)
        check=self.page.locator('#boundaries input[type=checkbox]');check.uncheck()
        self.assertFalse(self.page.evaluate('snapshot().fences[0].inclusion'))
        self.page.locator('#boundaries button',has_text='Edit').click()
        self.assertTrue(self.page.evaluate('boundaries.getLayers()[0].pm.enabled()'))
        self.page.locator('#cancel-draw').click()
        self.page.on('dialog',lambda dialog:dialog.accept());self.page.locator('#clear').click()
        self.assertEqual(self.page.evaluate('snapshot().fences.length'),0);self.assertEqual(self.fences.requests,[])
    def test_upload_review_cancel_then_explicit_verified_upload(self):
        self.add_circle();self.page.locator('#upload').click();self.page.locator('#upload-cancel').click()
        self.assertEqual(self.fences.requests,[])
        self.page.locator('#upload').click();self.page.locator('#upload-confirm').click()
        self.page.wait_for_function("() => job.state==='complete' && !dirty")
        operation,data=self.fences.requests[0]
        self.assertEqual(operation,'upload');self.assertTrue(data['confirmed']);self.assertFalse(data['enable_after'])
        self.assertEqual(data['plan']['fences'][0]['center'],[12.3456789,76.1234567])
    def test_upload_completion_and_settings_review(self):
        self.add_circle();self.page.locator('#upload').click();self.page.locator('#upload-confirm').click()
        self.page.locator('#result-dialog').wait_for(state='visible')
        self.assertIn('Fence upload complete',self.page.locator('#result-title').inner_text())
        self.assertIn('Saved fence enable: disabled',self.page.locator('#result-text').inner_text())
        self.page.locator('#result-close').click()
        self.fences.telemetry['settings']['WP_SPD']=6
        self.page.wait_for_function("() => document.getElementById('input-WP_SPD').disabled===false")
        self.page.locator('#input-WP_SPD').fill('1')
        self.page.locator('#settings-save').click()
        self.assertIn('WP_SPD: 6 → 1 m/s',self.page.locator('#settings-review').inner_text())
        self.page.locator('#settings-cancel').click();self.assertEqual(len(self.fences.requests),1)
        self.page.locator('#settings-save').click();self.page.locator('#settings-confirm').click()
        self.page.locator('#result-dialog').wait_for(state='visible')
        self.assertEqual(self.fences.requests[-1],('settings',{'changes':{'WP_SPD':1},'expected':{'WP_SPD':6},'confirmed':True}))
        self.assertIn('Settings saved and verified',self.page.locator('#result-title').inner_text())

    def test_mobile_safety_controls_and_partial_failure(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.fences.telemetry['settings']['WP_SPD']=6
        self.page.wait_for_function("() => document.getElementById('input-WP_SPD').disabled===false")
        self.page.locator('#input-WP_SPD').fill('1')
        self.page.locator('#settings-save').click()
        self.assertTrue(self.page.locator('#settings-dialog').is_visible())
        original=self.fences.launch
        def fail(operation,data):
            accepted=original(operation,data)
            self.fences.job.update(state='error',error='Second setting readback failed',applied={'WP_SPD':1})
            return accepted
        self.fences.launch=fail
        self.page.locator('#settings-confirm').click()
        self.page.locator('#result-dialog').wait_for(state='visible')
        self.assertIn('needs attention',self.page.locator('#result-title').inner_text())
        self.assertIn('Verified settings: WP_SPD',self.page.locator('#result-text').inner_text())
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),390)
        self.page.locator('#result-close').click()
        self.fences.telemetry['armed']=True
        self.page.wait_for_function("() => document.getElementById('input-WP_SPD').disabled")
        self.assertTrue(self.page.locator('#settings-save').is_disabled())

    def test_polygon_draw_tools_and_editable_points(self):
        self.page.locator('#polygon').click();self.assertTrue(self.page.evaluate('map.pm.Draw.Polygon.enabled()'))
        box=self.page.locator('#map').bounding_box()
        for x,y in [(150,180),(350,180),(350,330),(150,180)]:
            self.page.mouse.click(box['x']+x,box['y']+y)
        self.page.wait_for_function('() => snapshot().fences.length===1')
        self.assertEqual(self.page.evaluate('snapshot().fences[0].vertices.length'),3)
        self.page.locator('#boundaries button',has_text='Edit').click()
        self.assertTrue(self.page.evaluate('boundaries.getLayers()[0].pm.enabled()'))
        self.page.evaluate("boundaries.getLayers()[0].setLatLngs([[12.3454,76.123],[12.3455,76.124],[12.3465,76.124]]);boundaries.getLayers()[0].fire('pm:edit')")
        self.assertEqual(self.page.evaluate('snapshot().fences[0].vertices[0][0]'),12.3454)
    def test_mobile_layout_export_import_and_reload_draft(self):
        self.page.set_viewport_size({'width':390,'height':844});self.add_circle()
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
        with self.page.expect_download() as download:self.page.locator('#export').click()
        exported=json.loads(Path(download.value.path()).read_text())
        self.assertEqual(exported['geoFence']['circles'][0]['circle']['radius'],30)
        self.assertEqual(exported['mission']['items'],[])
        self.page.reload();self.page.wait_for_function('() => snapshot().fences.length===1')
        self.page.locator('#import').set_input_files({'name':'fence.plan','mimeType':'application/json','buffer':json.dumps(exported).encode()})
        self.page.wait_for_function("() => document.getElementById('message').textContent.includes('Imported boundaries')")
        self.assertEqual(self.page.evaluate('snapshot().fences[0].radius'),30)
    def test_touch_circle_drawing_and_resize_handles(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.locator('#circle').click();self.page.locator('#map').scroll_into_view_if_needed()
        box=self.page.locator('#map').bounding_box()
        self.page.touchscreen.tap(box['x']+110,box['y']+150)
        self.page.touchscreen.tap(box['x']+230,box['y']+150)
        self.page.wait_for_function('() => snapshot().fences.length===1')
        self.assertEqual(self.page.evaluate('snapshot().fences[0].kind'),'circle')
        self.assertGreater(self.page.evaluate('snapshot().fences[0].radius'),1)
        self.page.locator('#boundaries button',has_text='Edit').click()
        self.assertTrue(self.page.evaluate('boundaries.getLayers()[0].pm.enabled()'))
    def test_other_operator_result_does_not_overwrite_unsaved_draft(self):
        self.add_circle()
        self.fences.job={'id':'another-browser','state':'complete','operation':'read','message':'Other operator read',
            'plan':{'version':1,'fences':[]},'settings':self.fences.telemetry['settings']}
        self.page.evaluate('poll()')
        self.page.wait_for_function("() => job.id==='another-browser'")
        self.assertEqual(self.page.evaluate('snapshot().fences.length'),1)
        self.assertTrue(self.page.evaluate('dirty'))

    def test_armed_or_disconnected_vehicle_cannot_change_fence(self):
        self.add_circle();position=self.fences.telemetry['position']
        self.fences.telemetry.update(gps_fix=1,position=None)
        self.page.evaluate('poll()');self.page.wait_for_function('() => telemetry.gps_fix===1')
        self.assertTrue(self.page.locator('#enable').is_disabled())
        self.assertFalse(self.page.locator('#upload').is_disabled())
        self.page.locator('#enable-after').check();self.assertTrue(self.page.locator('#upload').is_disabled())
        self.page.locator('#enable-after').uncheck()
        self.fences.telemetry.update(gps_fix=3,position=position,armed=True)
        self.page.evaluate('poll()');self.page.wait_for_function('() => telemetry.armed===true')
        self.assertTrue(self.page.locator('#upload').is_disabled());self.assertTrue(self.page.locator('#enable').is_disabled())
        self.fences.telemetry['connected']=False;self.page.evaluate('poll()');self.page.wait_for_function('() => telemetry.connected===false')
        self.assertTrue(self.page.locator('#read').is_disabled())
        self.assertEqual(self.fences.requests,[])


if __name__=='__main__':unittest.main()
