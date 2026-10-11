import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flightcheck.manual import ground_guard, toggle_ground_arm
from flightcheck.telemetry import Rejected, LinkLost
from flightcheck.terminal import Terminal
import terminal_gcs


class ManualTests(unittest.TestCase):
    def test_toggle_uses_observed_state_and_never_force(self):
        for starting in (False,True):
            t = Mock()
            t.c = {'link':{'max_age_s':2},'mission':{'command_timeout_s':1}}
            t.mode.return_value = 'STABILIZE'
            t.store.latest.return_value = {'landed_state':1}
            state = [starting]
            t.armed.side_effect = lambda:state[0]
            t.command.side_effect = lambda *args,**kwargs:state.__setitem__(0,not starting)
            self.assertEqual(toggle_ground_arm(t),not starting)
            self.assertEqual(t.command.call_args.args,(400,[0 if starting else 1,0]))
            self.assertFalse(t.flight_authorized)

    def test_arm_mode_enter_never_selects_mission_and_escape_returns(self):
        keys = iter([10,27,-1])
        screen = SimpleNamespace(keypad=lambda _:None,nodelay=lambda _:None,getch=lambda:next(keys))
        with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
            ui = Terminal(screen)
        ui.receive_only = True
        ui.arm_control_active = True
        ui.keys()
        self.assertTrue(ui.arm_control_exit)
        self.assertFalse(ui.menu_requested)
        self.assertFalse(ui.quit_requested)
    def test_manual_arguments_do_not_request_mission(self):
        for action,flag in [('ARM','--arm-only'),('DISARM','--disarm-only')]:
            args = terminal_gcs.manual_args(['--monitor','--config','example.toml'],action)
            self.assertIn(flag,args)
            self.assertNotIn('--flight',args)
            self.assertNotIn('--monitor',args)
    def test_main_menu_keys_only_request_workflow(self):
        for key, action in [('a','ARM'),('A','ARM'),('d','DISARM'),('D','DISARM')]:
            keys = iter([ord(key),10,-1])
            screen = SimpleNamespace(keypad=lambda _:None,nodelay=lambda _:None,getch=lambda:next(keys))
            with patch('curses.curs_set'),patch('curses.has_colors',return_value=False):
                ui = Terminal(screen)
            ui.receive_only = True
            ui.keys()
            self.assertEqual(ui.manual_action,action)
            self.assertIsNone(ui.request)
            self.assertFalse(ui.mission_selected)
            self.assertEqual(screen.getch(),10)

    def test_disarm_requires_fresh_ground_and_unchanged_mode(self):
        t = Mock()
        t.c = {'link':{'max_age_s':1}}
        t.mode.return_value = 'STABILIZE'
        t.store.latest.return_value = {'landed_state':1}
        ground_guard(t,'STABILIZE')
        for state in [0,2,3,4]:
            t.store.latest.return_value = {'landed_state':state}
            with self.assertRaises(Rejected):
                ground_guard(t,'STABILIZE')
        t.store.latest.side_effect = LinkLost('stale')
        with self.assertRaises(LinkLost):
            ground_guard(t,'STABILIZE')
        t.mode.return_value = 'LOITER'
        with self.assertRaises(Rejected):
            ground_guard(t,'STABILIZE')
