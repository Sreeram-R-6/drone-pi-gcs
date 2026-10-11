from pathlib import Path
from types import SimpleNamespace
import unittest
import curses
from unittest.mock import Mock, patch
from flightcheck.config import load
from flightcheck.checks import Check
from flightcheck.telemetry import Store
from flightcheck.terminal import Terminal


class SensorDisplayTests(unittest.TestCase):
    def test_readiness_shows_pending_work_and_required_failures_only(self):
        ui = object.__new__(Terminal)
        ui.preflight_pending = 'Stationary sensor sample: 12s remaining'
        ui.view_state = 'FULL PREFLIGHT'
        ui.checks = [Check('GPS','FAIL',{'drift_m':10},'drift <= 8','drift_m 10 > 8'),
                     Check('firmware identity','UNKNOWN',None,'review firmware UID'),
                     Check('optional review','UNKNOWN',False,'review',required=False),
                     Check('battery','PASS',16,'adequate')]
        text = '\n'.join(ui.readiness_lines())
        for expected in ('12s remaining','FAIL: GPS','drift_m 10 > 8',
                         'UNKNOWN: firmware identity','review firmware UID','Final Enter'):
            self.assertIn(expected,text)
        self.assertNotIn('optional review',text)
        self.assertNotIn('battery',text)

    def test_ready_panel_prompts_for_enter(self):
        ui = object.__new__(Terminal)
        ui.preflight_pending = ''
        ui.view_state = 'READY / PRESS ENTER'
        ui.checks = []
        self.assertEqual(ui.readiness_lines(),['Press Enter: final fresh recheck, then start'])

    def test_p_page_lists_failure_stats_limits_and_named_health_reason(self):
        ui = object.__new__(Terminal)
        ui.transport = SimpleNamespace(store=SimpleNamespace(faults=['PreArm: Compass unhealthy']))
        ui.checks = [Check('GPS quality/stability','FAIL',
                          {'drift_m':10.17,'rms_m':2.05,'offsets_ne_m':[(0,0)]*150},
                          'drift <= 8 m','Failed: drift_m 10.17 > 8.00'),
                     Check('onboard sensor health','FAIL',{'healthy':0},
                           'required sensors healthy','Unhealthy: compass'),
                     Check('battery','PASS',16,'voltage adequate')]
        text = '\n'.join(ui.issue_lines(90))
        for expected in ('drift_m: 10.17','rms_m: 2.05','Expected: drift <= 8 m',
                         'Unhealthy: compass','FC STATUSTEXT: PreArm: Compass unhealthy',
                         '150 samples (full data in report)'):
            self.assertIn(expected,text)
        self.assertNotIn('battery',text)
        self.assertNotIn('(0, 0)',text)

    def test_p_page_scroll_close_and_hidden_controls_cannot_activate(self):
        screen = Mock()
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        ui.receive_only = True
        screen.getch.side_effect = [ord('P'),curses.KEY_NPAGE,ord('a'),10,-1]
        ui.keys()
        self.assertTrue(ui.show_issues)
        self.assertEqual(ui.issue_scroll,10)
        self.assertIsNone(ui.manual_action)
        self.assertFalse(ui.menu_requested)
        screen.getch.side_effect = [27,-1]
        ui.keys()
        self.assertFalse(ui.show_issues)
        self.assertFalse(ui.quit_requested)

    def test_failures_first_passes_last_without_mutating_checks(self):
        ui = object.__new__(Terminal)
        ui.checks = [Check(str(i),state,0,'test') for i,state in enumerate(
            ('PASS','UNKNOWN','FAIL','PASS','WARN','FAIL'))]
        original = list(ui.checks)
        self.assertEqual([c.state for c in ui.ordered_checks()],
                         ['FAIL','FAIL','WARN','UNKNOWN','PASS','PASS'])
        self.assertEqual([c.name for c in ui.ordered_checks()],['2','5','4','1','0','3'])
        self.assertEqual(ui.checks,original)
    def test_home_height_and_compensated_flow_without_mission(self):
        screen = Mock()
        screen.getmaxyx.return_value = (40,120)
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        c = load(Path(__file__).parents[1]/'config.example.toml')
        store = Store()
        store.put('HEARTBEAT',{'base_mode':0},100)
        store.put('GLOBAL_POSITION_INT',{'relative_alt':1500},100)
        store.put('OPTICAL_FLOW',{'flow_comp_m_x':0.2,'flow_comp_m_y':-0.1,'quality':180},100)
        ui.config = c
        ui.transport = SimpleNamespace(store=store,c=c,mode=lambda:'STABILIZE')
        ui.small = lambda:False
        ui.panel = lambda *a,**kw:(0,0,10,38)
        ui.row = Mock()
        ui.put = ui.bar = ui.map_panel = ui.mission_panel = ui.checks_panel = Mock()
        with patch('flightcheck.terminal.time.monotonic',return_value=100):
            ui.draw(force=True)
        lines = [call.args[2] for call in ui.row.call_args_list]
        self.assertTrue(any('Home alt' in s and '1.50' in s for s in lines))
        self.assertTrue(any('opt_m_x' in s and 'opt_m_y' in s and '0.20' in s and '-0.10' in s for s in lines))
        self.assertTrue(any('opt_qua 180 /255' in s for s in lines))
        self.assertFalse(any('waiting for baseline' in s for s in lines))
        self.assertEqual(list(ui.altitudes),[1.5])
        self.assertIsNone(ui.mission)
