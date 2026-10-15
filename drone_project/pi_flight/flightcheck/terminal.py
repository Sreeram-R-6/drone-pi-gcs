"""Responsive curses dashboard; rendering never opens a connection."""
from collections import deque
import curses
import math
import textwrap
import time
from .checks import ne_offset
from .routes import ROUTES, coordinates
from .telemetry import Rejected


def relative_home(store, now, age):
    p = store.latest('GLOBAL_POSITION_INT', now, age)
    homes = store.samples['HOME_POSITION']
    if not homes:
        raise Rejected('Home unknown')
    h = homes[-1][1]
    return ne_offset(p['lat']/1e7, p['lon']/1e7, h['latitude']/1e7, h['longitude']/1e7)


def number(value, unit='', precision=2):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return '--'
    return f'{value:.{precision}f}{unit}'


class Terminal:
    def __init__(self, screen):
        self.screen = screen
        self.route = 'hover'
        self.mission_selected = False
        self.transport = self.mission = self.report = self.config = None
        self.blockers = []
        self.messages = deque(maxlen=100)
        self.notice = 'OFFLINE / FLIGHT LOCKED'
        self.action = 'Awaiting preflight'
        self.waypoint = '--'
        self.progress = 0.
        self.selected = 1
        self.request = None
        self.prompt = None
        self.answer = ''
        self.submitted = None
        self.last_draw = 0.
        self.checks = []
        self.show_checks = False
        self.show_issues = False
        self.issue_scroll = 0
        self.check_page = 0
        self.check_cursor = 0
        self.check_page_size = 8
        self.state_seen = None
        self.state_started = time.monotonic()
        self.quit_requested = False
        self.started = time.monotonic()
        self.altitudes = deque(maxlen=60)
        self.history_stamp = None
        self.history_source = None
        self.colors = {}
        self.view_state = 'PREFLIGHT / LOCKED'
        self.receive_only = False
        self.menu_requested = False
        self.manual_action = None
        self.arm_control_active = False
        self.arm_control_exit = False
        self.service_request = None
        self.service_active = False
        self.service_exit = False
        self.reconnect_requested = False
        self.workflow_status = ''
        self.preflight_pending = ''
        screen.keypad(True)
        screen.nodelay(True)
        try:
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                for i, (name, color) in enumerate((('cyan', curses.COLOR_CYAN),
                        ('green', curses.COLOR_GREEN), ('yellow', curses.COLOR_YELLOW),
                        ('red', curses.COLOR_RED), ('muted', curses.COLOR_WHITE)), 1):
                    curses.init_pair(i, color, -1)
                    self.colors[name] = curses.color_pair(i) | (curses.A_DIM if name == 'muted' else 0)
        except curses.error:
            pass

    def flush_input(self):
        curses.flushinp()

    def color(self, name):
        return self.colors.get(name, 0)

    def write(self, text):
        self.messages.extend(line for line in text.splitlines() if line.strip())
        return len(text)

    def flush(self):
        pass

    def put(self, y, x, text, width=None, attr=0):
        h,w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w-1:
            return
        limit = min(w-x-1, width if width is not None else w-x-1)
        if limit > 0:
            try:
                self.screen.addnstr(y,x,str(text).replace('\n',' '),limit,attr)
            except curses.error:
                pass

    def panel(self, y, x, h, w, title, tone='cyan'):
        if h < 3 or w < 6:
            return y+1,x+1,0,0
        attr = self.color(tone)
        self.put(y,x,'╭'+'─'*(w-2)+'╮',w,attr)
        self.put(y+h-1,x,'╰'+'─'*(w-2)+'╯',w,attr)
        for row in range(y+1,y+h-1):
            self.put(row,x,'│',1,attr)
            self.put(row,x+w-1,'│',1,attr)
        self.put(y,x+2,' '+title+' ',w-4,attr | curses.A_BOLD)
        return y+1,x+2,h-2,w-4

    def row(self, rect, index, text, tone=None, bold=False):
        y,x,h,w = rect
        if 0 <= index < h:
            self.put(y+index,x,text,w,(self.color(tone) if tone else 0) | (curses.A_BOLD if bold else 0))

    def wrap(self, rect, index, text, tone=None):
        if rect[3] > 0:
            for offset,line in enumerate(textwrap.wrap(str(text),rect[3]) or ['']):
                self.row(rect,index+offset,line,tone)

    def bar(self, rect, row, fraction, label, tone='green'):
        fraction = max(0.,min(1.,fraction))
        width = max(1,rect[3]-len(label)-9)
        filled = round(width*fraction)
        self.row(rect,row,f"{label} {'━'*filled}{'·'*(width-filled)} {fraction*100:3.0f}%",tone)

    def keys(self):
        while True:
            key = self.screen.getch()
            if key == -1:
                break
            if self.prompt is not None:
                if key == 27:
                    self.submitted = 'CANCEL'
                    return
                if key in (10,13,curses.KEY_ENTER):
                    self.submitted = self.answer
                elif key in (8,127,curses.KEY_BACKSPACE):
                    self.answer = self.answer[:-1]
                elif 32 <= key < 127:
                    self.answer += chr(key)
                continue
            if key in (ord('p'),ord('P')):
                self.show_issues = not self.show_issues
                self.issue_scroll = 0
                continue
            if self.show_issues:
                if key == 27:
                    self.show_issues = False
                elif key in (curses.KEY_DOWN,curses.KEY_NPAGE):
                    self.issue_scroll += 1 if key == curses.KEY_DOWN else 10
                elif key in (curses.KEY_UP,curses.KEY_PPAGE):
                    self.issue_scroll = max(0,self.issue_scroll-(1 if key == curses.KEY_UP else 10))
                elif key == curses.KEY_HOME:
                    self.issue_scroll = 0
                elif key == ord('q'):
                    if self.mission:
                        self.mission.cancel_requested = True
                    else:
                        self.quit_requested = True
                # Diagnostics is read-only: Enter/A/D cannot activate controls
                # that are hidden underneath the page.
                continue
            if self.service_active and key == 27:
                self.service_exit = True
                return
            if self.service_active and key in (10,13,curses.KEY_ENTER):
                continue
            if self.arm_control_active and key in (27,ord('m'),ord('M')):
                self.arm_control_exit = True
                return
            if self.arm_control_active and key in (10,13,curses.KEY_ENTER):
                continue
            if self.show_checks and key in (curses.KEY_UP,curses.KEY_DOWN):
                self.check_cursor = max(0,min(len(self.checks)-1,self.check_cursor+(1 if key == curses.KEY_DOWN else -1)))
                self.check_page = self.check_cursor//self.check_page_size
            elif key in (curses.KEY_LEFT,curses.KEY_RIGHT,curses.KEY_UP,curses.KEY_DOWN):
                self.selected = 1-self.selected
            elif key in (10,13,curses.KEY_ENTER) and self.mission:
                self.request = ('DISARM','RTL')[self.selected]
                self.notice = f'{self.request} requested; awaiting controller gate'
            elif key in (ord('r'),ord('R')) and self.receive_only and not self.mission and not self.arm_control_active:
                self.reconnect_requested = True
                return
            elif key in (ord('f'),ord('F'),ord('l'),ord('L'),ord('x'),ord('X')) and self.receive_only and not self.mission and not self.arm_control_active:
                self.service_request = {'f':'diagnostics','l':'download-logs','x':'erase-logs'}[chr(key).lower()]
                return
            elif key in (ord('a'),ord('A'),ord('d'),ord('D')) and self.receive_only and not self.mission:
                self.manual_action = 'ARM' if key in (ord('a'),ord('A')) else 'DISARM'
                return
            elif key in (ord('m'),ord('M'),10,13,curses.KEY_ENTER) and self.receive_only and not self.mission:
                self.menu_requested = True
                return # Menu confirmation must use a fresh key, never queued Enter.
            elif key in (ord('q'),27):
                if self.mission:
                    self.mission.cancel_requested = True
                    self.notice = ('Stop monitoring requested; onboard mission may continue. Use RC.'
                        if getattr(self.mission, 'started', False) else
                        'Cancel requested; controller recovery/landing follows')
                else:
                    self.quit_requested = True
            elif key == ord('c'):
                self.show_checks = not self.show_checks
            elif key == curses.KEY_NPAGE:
                self.check_page = min(max(0,(len(self.checks)-1)//self.check_page_size),self.check_page+1)
                self.check_cursor = self.check_page*self.check_page_size
            elif key == curses.KEY_PPAGE:
                self.check_page = max(0,self.check_page-1)
                self.check_cursor = self.check_page*self.check_page_size

    def observe(self, transport):
        self.transport = transport
        if self.service_active:
            self.notice = self.workflow_status
        self.keys()
        self.draw()
        if self.quit_requested and not self.receive_only and self.mission is None:
            raise Rejected('Operator cancelled preflight')

    def input_live(self, prompt, transport, guard=None):
        from .startup import confirmation_mode, check_confirmation_state
        expected_mode = confirmation_mode(transport) if guard is None else None
        self.flush_input()
        self.show_issues = False # Never obscure a newly requested confirmation.
        self.prompt,self.answer,self.submitted = prompt,'',None
        try:
            while self.submitted is None:
                transport.pump()
                transport.heartbeat()
                if guard is not None:
                    guard()
                else:
                    check_confirmation_state(transport, expected_mode)
            return self.submitted
        finally:
            self.prompt = None
            self.flush_input()

    def small(self):
        h,w = self.screen.getmaxyx()
        if h >= 24 and w >= 76:
            return False
        self.put(0,0,'DRONE GCS • compact terminal',attr=curses.A_BOLD)
        self.put(2,0,'Resize to at least 76 × 24 (recommended 120 × 40).',attr=self.color('yellow'))
        self.put(4,0,self.notice)
        self.put(6,0,'[M SELECT MISSION]' if self.receive_only else '[1 DISARM • ground only]' if self.selected == 0 else '[2 RTL]',attr=curses.A_REVERSE)
        self.put(8,0,'M missions • A arm • D disarm • Q exit' if self.receive_only else 'Arrows select • Enter request • Q cancel/exit')
        if self.prompt:
            self.put(max(0,h-3),0,self.prompt)
            self.put(max(0,h-2),0,'> '+self.answer)
        self.screen.refresh()
        return True

    def choose(self, transport=None):
        """Live menu pumps telemetry without servicing dashboard control keys."""
        observer = transport.observer if transport else None
        previous = self.route
        accepted = False
        if transport:
            transport.observer = None
        try:
            accepted = self._choose(transport)
            if accepted:
                self.mission_selected = True
                self.receive_only = False
                self.view_state = 'MISSION SELECTED / CHECKING'
                self.workflow_status = self.route.upper()+' selected. Waiting for preflight checks; start locked.'
                self.notice = self.workflow_status
                self.draw(force=True)
            return accepted
        finally:
            if not accepted:
                self.route = previous
            if transport:
                transport.observer = observer

    def _choose(self, transport=None):
        self.flush_input()
        names = list(ROUTES)
        index = names.index(self.route)
        while True:
            if transport:
                transport.pump(.01)
            self.route = names[index]
            self.screen.erase()
            h,w = self.screen.getmaxyx()
            if not self.small():
                header = self.panel(0,0,3,w-1,'DRONE / MISSION CONTROL')
                self.row(header,0,'SELECT MISSION  •  PREFLIGHT REQUIRED  •  ENTER SELECTS, DOES NOT ARM','yellow',True)
                split = max(28,w//3)
                menu = self.panel(3,0,h-6,split,'01 / MISSIONS')
                for i,name in enumerate(names):
                    self.row(menu,1+i*3,f"{'▶' if i == index else ' '} {name.upper()}",'cyan' if i == index else None,i == index)
                    self.row(menu,2+i*3,('Hover + land','Forward + return','Forward / right / back / left')[i],'muted')
                plan = self.panel(3,split,h-6,w-split-1,'02 / FLIGHT TIMELINE')
                items = ['Preflight + confirmations','Enter → fresh recheck','Take off to 2.0 m','Settle 3 s → hold 5 s']
                items += [p.action+' → settle + hold 5 s' for p in ROUTES[self.route]]
                items += ['LAND → automatic disarm','Restore settings → complete']
                for i,item in enumerate(items):
                    self.row(plan,i,f'{i+1:02}  {item}','cyan' if 2 <= i <= 3 else None)
                self.row(plan,len(items)+1,'1 m legs • launch-heading relative • no automatic restart','muted')
                footer = self.panel(h-3,0,3,w-1,'KEYS')
                self.row(footer,0,'↑ ↓ select mission    Enter begin checks    Esc / Q back','cyan')
                self.screen.refresh()
            key = self.screen.getch()
            if key == curses.KEY_UP:
                index = (index-1) % len(names)
            elif key == curses.KEY_DOWN:
                index = (index+1) % len(names)
            elif key in (10,13,curses.KEY_ENTER):
                self.flush_input()
                return True
            elif key in (ord('q'),27):
                return False
            time.sleep(.05)

    def telemetry(self, now):
        fresh = {}
        if self.transport:
            for kind,records in self.transport.store.samples.items():
                if records and now-records[-1][0] <= self.transport.c['link']['max_age_s']:
                    fresh[kind] = records[-1][1]
        return fresh

    def map_panel(self, rect, now, position, heading):
        y,x,h,w = rect
        if h < 3 or w < 5:
            return
        self.row(rect,0,'N ↑  H home  arrow drone  * target','muted')
        gh,gw = h-2,w
        cy,cx = gh//2,gw//2
        scale = max(2.,self.config['mission']['max_radius_m'] if self.config else 2.)
        grid = [[' ']*gw for _ in range(gh)]
        for row in range(gh):
            grid[row][cx] = '┆'
        grid[cy] = ['┄']*gw
        try:
            t = self.transport
            north,east = relative_home(t.store,now,t.c['link']['max_age_s'])
            scale = max(scale,abs(north)*1.2,abs(east)*1.2)
            def plot(n,e,mark):
                row = cy-round(n/scale*max(1,gh//2-1))
                col = cx+round(e/scale*max(1,gw//2-1))
                if 0 <= row < gh and 0 <= col < gw:
                    grid[row][col] = mark
            if self.mission and self.mission.baseline and self.mission.yaw is not None and position:
                for waypoint in ROUTES[self.route]:
                    wn,we,_ = coordinates(self.mission.baseline,self.mission.yaw,waypoint,2)
                    plot(north+wn-position['x'],east+we-position['y'],'*')
            plot(0,0,'H')
            plot(north,east,'↑→↓←'[int((heading+45)%360//90)] if heading is not None else 'D')
            caption = f'N {north:+.2f} E {east:+.2f} m  ±{scale:.1f}m'
        except (AttributeError,KeyError,TypeError,ValueError,RuntimeError):
            caption = 'Position/home unavailable'
            for i,char in enumerate('NO FIX'):
                if 0 <= cx-3+i < gw:
                    grid[cy][cx-3+i] = char
        for i,row in enumerate(grid):
            self.row(rect,i+1,''.join(row),'cyan')
        self.row(rect,h-1,caption,'muted')

    def mission_panel(self, rect, state, now):
        if self.service_active or self.arm_control_active or self.manual_action or not self.mission_selected:
            self.row(rect,0,'NO MISSION LOADED','muted',True)
            self.row(rect,1,'FC MAINTENANCE · Esc returns' if self.service_active else 'ARM CONTROL · A toggles' if self.arm_control_active else 'Manual '+self.manual_action if self.manual_action else 'M / Enter: select a mission','cyan')
            self.wrap(rect,3,self.workflow_status or 'Arm/disarm controls do not load or run a mission.','yellow')
            return
        self.row(rect,0,'MISSION  '+self.route.upper(),'cyan',True)
        self.row(rect,1,'Action    '+(self.action if state == 'ROUTE' else state.replace('_',' ')))
        self.row(rect,2,'Waypoint  '+self.waypoint)
        self.bar(rect,3,self.progress,'Route')
        if not self.mission:
            status = textwrap.wrap(self.workflow_status or 'Waiting for preflight',max(1,rect[3]))
            for i,line in enumerate(status):
                self.row(rect,4+i,line,'yellow')
            row = 5+len(status)
            self.row(rect,row,'NEEDED TO START · P shows full details','cyan',True)
            lines = [part for line in self.readiness_lines()
                     for part in textwrap.wrap(line,max(1,rect[3]))]
            available = max(0,rect[2]-row-1)
            for i,line in enumerate(lines[:available]):
                self.row(rect,row+1+i,line,'yellow')
            if len(lines) > available and available:
                self.row(rect,rect[2]-1,'More requirements: press P','cyan')
            return
        self.row(rect,4,f'Hold      {max(0.,5-(now-self.state_started)):.1f}s remaining' if state == 'HOLD_5_SECONDS' else 'Phase     '+number(now-self.state_started,' s',1),'green' if state == 'HOLD_5_SECONDS' else 'muted')
        self.row(rect,5,'Landing completion observed separately','muted')
        if self.workflow_status and not self.mission:
            self.wrap(rect,4,self.workflow_status,'yellow')
        for i,waypoint in enumerate(ROUTES[self.route]):
            self.row(rect,7+i,f'{i+1:02}  {waypoint.action}')

    def ordered_checks(self):
        priority = {'FAIL':0,'WARN':1,'UNKNOWN':2,'PASS':3}
        return sorted(self.checks,key=lambda check:priority.get(check.state,2))

    def readiness_lines(self):
        lines = []
        pending = getattr(self,'preflight_pending','')
        if pending:
            lines.append('WAIT: '+pending)
        for check in self.ordered_checks():
            if check.required and check.state != 'PASS':
                lines.append(check.state+': '+check.name)
                lines.append('  '+(check.explanation or check.requirement))
        if self.view_state == 'READY / PRESS ENTER':
            lines.append('Press Enter: final fresh recheck, then start')
        elif self.view_state == 'OPERATOR CONFIRMATIONS':
            lines.append('Complete each operator confirmation (type YES)')
        elif self.view_state != 'PREFLIGHT BLOCKED':
            lines.append('Complete live preflight and operator confirmations')
            lines.append('Final Enter required; selection alone never starts')
        elif not lines:
            lines.append(self.workflow_status or 'Preflight stopped; select mission to retry')
        return lines

    def issue_lines(self, width):
        """Readable failure evidence, without hundreds of GPS offset tuples."""
        lines = []
        for check in self.ordered_checks():
            if check.state == 'PASS':
                continue
            label = 'INFO' if not check.required and check.state == 'UNKNOWN' else check.state
            lines.append(f'{label}  {check.name}')
            for text in (check.explanation, 'Expected: '+str(check.requirement)):
                if text:
                    lines.extend(textwrap.wrap(text,max(1,width)))
            measured = check.measured
            if isinstance(measured,dict):
                for name,value in measured.items():
                    if name == 'offsets_ne_m':
                        text = f'{name}: {len(value)} samples (full data in report)'
                    else:
                        text = f'{name}: {value}'
                    lines.extend(textwrap.wrap(text,max(1,width)))
            else:
                lines.extend(textwrap.wrap('Measured: '+str(measured),max(1,width)))
            lines.append('')
        faults = self.transport.store.faults if self.transport else []
        for fault in faults:
            lines.extend(textwrap.wrap('FC STATUSTEXT: '+str(fault),max(1,width)))
        return lines or ['No reported issues.' if self.checks else
                         'No completed checks yet; readiness is not confirmed.']

    def issues_panel(self, h, width):
        rect = self.panel(3,0,h-6,width,'P / ISSUES + SENSOR DIAGNOSTICS','yellow')
        lines = self.issue_lines(rect[3])
        count = max(1,rect[2]-1)
        self.issue_scroll = min(self.issue_scroll,max(0,len(lines)-count))
        self.row(rect,0,f'Lines {self.issue_scroll+1}–{min(len(lines),self.issue_scroll+count)} / {len(lines)} · live check results','cyan')
        for index,line in enumerate(lines[self.issue_scroll:self.issue_scroll+count]):
            self.row(rect,index+1,line,'red' if line.startswith('FAIL') else 'yellow')
        footer = self.panel(h-3,0,3,width,'READ-ONLY DIAGNOSTICS')
        self.row(footer,0,'P / Esc back · ↑↓ scroll · PgUp/PgDn page · Q cancel/exit · flight checks unchanged','cyan')

    def checks_panel(self, rect):
        self.check_page_size = max(1,min(8,rect[2]-2))
        self.check_page = min(self.check_page,max(0,(len(self.checks)-1)//self.check_page_size))
        counts = {s:sum(c.state == s and c.required for c in self.checks) for s in ('PASS','FAIL','WARN','UNKNOWN')}
        optional_unknown = sum(c.state == 'UNKNOWN' and not c.required for c in self.checks)
        self.row(rect,0,f"PASS {counts['PASS']}  FAIL {counts['FAIL']}  ? {counts['WARN']+counts['UNKNOWN']}",'green' if self.checks and not counts['FAIL'] and not counts['WARN'] and not counts['UNKNOWN'] else 'yellow')
        self.row(rect,1,f'Page {self.check_page+1} • PgUp/PgDn • ↑↓'+(f' • {optional_unknown} info' if optional_unknown else ''),'muted')
        if not self.checks:
            self.wrap(rect,2,'No completed checks yet. Live evidence required.','yellow')
        start = self.check_page*self.check_page_size
        for i,check in enumerate(self.ordered_checks()[start:start+self.check_page_size]):
            tone = 'green' if check.state == 'PASS' else 'red' if check.state == 'FAIL' else 'yellow'
            hint = ' [limits unset]' if check.explanation.startswith('Limits unset:') else ''
            state_label = 'INFO' if not check.required and check.state == 'UNKNOWN' else check.state
            self.row(rect,2+i,f'{state_label:7} {check.name}{hint}',tone,self.show_checks and start+i == self.check_cursor)

    def draw(self, force=False):
        now = time.monotonic()
        if not force and now-self.last_draw < .15:
            return
        self.last_draw = now
        self.screen.erase()
        if self.small():
            return
        h,w = self.screen.getmaxyx()
        width = w-1
        state = self.mission.machine.state if self.mission else self.view_state
        if state != self.state_seen:
            self.state_seen,self.state_started = state,now
        data = self.telemetry(now)
        hb,p,a,gps,battery = (data.get(k,{}) for k in ('HEARTBEAT','LOCAL_POSITION_NED','ATTITUDE','GPS_RAW_INT','BATTERY_STATUS'))
        header = self.panel(0,0,3,width,'DRONE / GCS','red' if hb.get('base_mode',0)&128 else 'cyan')
        arm = 'ARMED' if hb.get('base_mode',0)&128 else 'DISARMED' if hb else 'LINK UNKNOWN'
        label = self.manual_action or (self.route.upper() if self.mission_selected else 'NO MISSION')
        self.row(header,0,f"{label}  │  {state.replace('_',' ')}  │  {arm}  │  {time.strftime('%H:%M:%S')}",'yellow' if not hb else 'cyan',True)
        if self.show_issues:
            self.issues_panel(h,width)
            self.screen.refresh()
            return
        heading = math.degrees(a['yaw']) % 360 if a.get('yaw') is not None else None
        height = self.mission.baseline['z']-p['z'] if self.mission and self.mission.baseline and p.get('z') is not None else None
        relative_alt = data.get('GLOBAL_POSITION_INT',{}).get('relative_alt')
        display_height = height if height is not None else relative_alt/1000 if isinstance(relative_alt,(int,float)) else None
        height_source = 'LOCAL_POSITION_NED' if height is not None else 'GLOBAL_POSITION_INT'
        if display_height is not None and self.transport:
            if self.history_source != height_source:
                self.altitudes.clear()
                self.history_stamp = None
                self.history_source = height_source
            stamp = self.transport.store.samples[height_source][-1][0]
            if stamp != self.history_stamp:
                self.history_stamp = stamp
                self.altitudes.append(display_height)
        third = width//3
        telemetry_h = 6 if h < 28 else 8
        flight = self.panel(3,0,telemetry_h,third,'01 / FLIGHT')
        nav = self.panel(3,third,telemetry_h,third,'02 / NAV + SENSORS')
        power = self.panel(3,third*2,telemetry_h,width-third*2,'03 / POWER + LINK')
        self.row(flight,0,'Mode      '+(self.transport.mode() if hb else '--'))
        self.row(flight,1,('Launch alt ' if height is not None else 'Home alt  ')+number(display_height,' m'))
        self.row(flight,2,'Heading   '+number(heading,'°',1))
        self.row(flight,3,'Vel N/E   '+number(p.get('vx'))+' / '+number(p.get('vy'))+' m/s')
        self.row(flight,4,'Vel down  '+number(p.get('vz'),' m/s'))
        spark = ''.join('▁▂▃▄▅▆▇█'[max(0,min(7,round(v/2.5*7)))] for v in list(self.altitudes)[-max(1,flight[3]-8):]) if display_height is not None else ''
        self.row(flight,5,'Alt trend '+(spark or 'no altitude telemetry'),'cyan')
        eph = gps.get('eph')
        self.row(nav,0,f"GPS       fix {gps.get('fix_type','--')} / {gps.get('satellites_visible','--')} sats")
        self.row(nav,1,'HDOP      '+number(eph/100 if isinstance(eph,(int,float)) and eph not in (0,65535) else None))
        self.row(nav,2,'EKF flags '+str(data.get('EKF_STATUS_REPORT',{}).get('flags','--')))
        flow = data.get('OPTICAL_FLOW') or data.get('OPTICAL_FLOW_RAD',{})
        # Mission Planner status aliases, not FC parameter names. RAD integrals
        # cannot be substituted for compensated metre/second velocities.
        flow_text = ('opt_m_x '+number(flow.get('flow_comp_m_x'))+' opt_m_y '+number(flow.get('flow_comp_m_y')))
        flow_quality = 'opt_qua '+str(flow.get('quality','--'))+' /255'
        self.row(nav,3,flow_text)
        distance = data.get('DISTANCE_SENSOR',{}).get('current_distance')
        self.row(nav,4,'Range     '+number(distance/100 if isinstance(distance,(int,float)) else None,' m'))
        self.row(nav,5,flow_quality+' · '+{0:'unknown',1:'on ground',2:'in air',3:'takeoff',4:'landing'}.get(data.get('EXTENDED_SYS_STATE',{}).get('landed_state'),'--'))
        volts = [v for v in (battery.get('voltages') or [])
                 if isinstance(v,(int,float)) and math.isfinite(v) and 0 <= v < 65535]
        remaining,current = battery.get('battery_remaining'),battery.get('current_battery')
        self.row(power,0,'Voltage   '+number(sum(volts)/1000 if volts else None,' V'))
        self.row(power,1,'Current   '+number(current/100 if isinstance(current,(int,float)) and current >= 0 else None,' A'))
        if isinstance(remaining,(int,float)) and 0 <= remaining <= 100:
            self.bar(power,2,remaining/100,'Battery','yellow' if remaining < 30 else 'green')
        else:
            self.row(power,2,'Battery   --','yellow')
        from .rc_link import link_quality
        lq = link_quality(self.transport.store,data.get('RC_CHANNELS',{}),now)[0] if self.transport else None
        rc_link_text = 'RC LQ     '+(number(lq,'%',1) if lq is not None else 'not reported')
        self.row(power,3,rc_link_text)
        records = self.transport.store.samples['HEARTBEAT'] if self.transport else []
        self.row(power,4,'Link age  '+number(now-records[-1][0] if records else None,' s'),'green' if hb else 'yellow')
        self.row(power,5,'Uptime    '+number(now-self.started,' s',0),'muted')
        if telemetry_h == 6:
            # Four interior rows in compact mode: keep the primary measurements.
            for rect,lines in (
                (flight,['Mode '+(self.transport.mode() if hb else '--'),
                         'Alt '+number(height,'m')+' / '+number(heading,'°',0),
                         'N/E '+number(p.get('vx'))+'/'+number(p.get('vy')),
                         'Down '+number(p.get('vz'),' m/s')]),
                (nav,[f"GPS {gps.get('fix_type','--')} / {gps.get('satellites_visible','--')} sats",
                      'HDOP '+number(eph/100 if isinstance(eph,(int,float)) and eph not in (0,65535) else None)+' '+flow_quality,
                      flow_text,
                      'Landed '+{0:'unknown',1:'ground',2:'air',3:'takeoff',4:'landing'}.get(data.get('EXTENDED_SYS_STATE',{}).get('landed_state'),'--')]),
                (power,[number(sum(volts)/1000 if volts else None,'V')+' / '+number(current/100 if isinstance(current,(int,float)) and current >= 0 else None,'A'),
                        'Battery '+number(remaining if isinstance(remaining,(int,float)) and 0 <= remaining <= 100 else None,'%',0),
                        rc_link_text,
                        'Link '+number(now-records[-1][0] if records else None,' s')])):
                for i,line in enumerate(lines):
                    self.row(rect,i,line.ljust(rect[3]))
        log_h = 5 if h >= 34 else 0
        body_y = 3+telemetry_h
        body_h = h-body_y-8-log_h
        if width >= 105:
            left = width*38//100
            mid = (width-left)//2
            map_rect = self.panel(body_y,0,body_h,left,'04 / CHECK DETAILS' if self.show_checks else '04 / HOME MAP')
            mission_rect = self.panel(body_y,left,body_h,mid,'05 / MISSION')
            check_rect = self.panel(body_y,left+mid,body_h,width-left-mid,'06 / CHECKS')
            self.mission_panel(mission_rect,state,now)
            self.checks_panel(check_rect)
        else:
            left = width//2
            map_rect = self.panel(body_y,0,body_h,left,'04 / HOME MAP')
            right = self.panel(body_y,left,body_h,width-left,'05 / CHECKS' if self.show_checks else '05 / MISSION')
            if self.show_checks:
                self.checks_panel(right)
            else:
                self.mission_panel(right,state,now)
        if self.show_checks and width >= 105:
            index = max(0,min(len(self.checks)-1,self.check_cursor))
            if index < len(self.checks):
                check = self.ordered_checks()[index]
                self.row(map_rect,0,check.name,'cyan',True)
                self.row(map_rect,1,check.state,'green' if check.state == 'PASS' else 'yellow')
                self.wrap(map_rect,3,(check.explanation or check.requirement)+'\nMeasured: '+str(check.measured))
            else:
                self.wrap(map_rect,0,'No completed checks yet. Live evidence required.','yellow')
        else:
            self.map_panel(map_rect,now,p,heading)
        bottom = body_y+body_h
        preparing = self.mission_selected and not self.mission and not self.receive_only
        ready = preparing and self.view_state == 'READY / PRESS ENTER'
        control = self.panel(bottom,0,5,width,'MISSION READY / ENTER TO START' if ready else 'MISSION PREFLIGHT / START LOCKED' if preparing else 'MAIN MENU / COMMANDS REQUIRE CONFIRMATION' if self.receive_only else 'CONTROL / ARROWS SELECT • ENTER ACTIVATE','yellow')
        cy,cx,_,cw = control
        first,second = ' 1 DISARM · ground only ',' 2 RETURN TO LAUNCH '
        if self.service_active:
            self.put(cy,cx,' FC MAINTENANCE · FLIGHT CONTROLS LOCKED ',cw,self.color('yellow'))
        elif preparing:
            self.put(cy,cx,' ENTER START MISSION ' if ready else ' WAITING FOR CHECKS ',cw,self.color('green' if ready else 'yellow') | curses.A_REVERSE)
        elif self.receive_only:
            self.put(cy,cx,' ARM MODE · NO MISSION ' if self.arm_control_active else ' M SELECT MISSION ',cw//2,self.color('green') | curses.A_REVERSE | curses.A_BOLD)
            self.put(cy,cx+cw//2,' A TOGGLE ARM / DISARM ' if self.arm_control_active else ' A ARM / D DISARM · gated ',cw//2,self.color('yellow'))
        else:
            self.put(cy,cx,first,min(len(first),cw//2),self.color('red') | (curses.A_REVERSE | curses.A_BOLD if self.selected == 0 else 0))
            self.put(cy,cx+cw//2,second,cw//2,self.color('yellow') | (curses.A_REVERSE | curses.A_BOLD if self.selected == 1 else 0))
        self.row(control,1,'Esc return · Q exit · C details · PgUp/PgDn' if self.service_active else 'Q cancel  •  C checks  •  PgUp/PgDn  •  No arming until checks and final Enter' if preparing else 'M missions · A arm · F checks · L logs · X erase · R reconnect · C details · Q exit' if self.receive_only else 'Q cancel / land  •  C mission / checks  •  PgUp/PgDn check pages','muted')
        faults = self.transport.store.faults if self.transport else []
        self.row(control,2,'FAULT: '+' | '.join(faults[-2:]) if faults else self.notice,'red' if faults else 'yellow')
        if log_h:
            log = self.panel(bottom+5,0,log_h,width,'EVENT LOG')
            for i,message in enumerate(list(self.messages)[-log[2]:]):
                self.row(log,i,message,'muted')
        footer = self.panel(h-3,0,3,width,'CONFIRMATION' if self.prompt else 'STATUS','yellow' if self.prompt else 'cyan')
        text = 'Awaiting operator confirmation • telemetry continues updating' if self.prompt else 'LOCKED: '+', '.join(self.blockers) if self.blockers else 'P issues / sensor stats • Enter selected control • freshness gates active'
        self.row(footer,0,text,'yellow' if self.prompt or self.blockers else 'muted')
        if self.prompt and ready:
            self.row(footer,0,'ALL CHECKS CONFIRMED · Press Enter to start '+self.route.upper()+' · Esc cancels','green',True)
        elif self.prompt:
            modal_w = min(width-6,90)
            lines = textwrap.wrap(self.prompt,modal_w-4)
            modal_h = min(h-4,max(6,len(lines)+4))
            my,mx = (h-modal_h)//2,(width-modal_w)//2
            # Clear the modal area so the underlying map/borders cannot bleed through.
            for row in range(my,my+modal_h):
                self.put(row,mx,' '*modal_w,modal_w)
            modal = self.panel(my,mx,modal_h,modal_w,'OPERATOR CONFIRMATION','yellow')
            for i,line in enumerate(lines):
                self.row(modal,i,line,'yellow',True)
            self.row(modal,modal[2]-1,'> '+self.answer+'▏','cyan')
        self.screen.refresh()
