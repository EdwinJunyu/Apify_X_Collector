"""IANA local-date scheduling with durable merged catch-up and bounded queues."""
import copy
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, date, time
from pathlib import Path
from zoneinfo import ZoneInfo
from .security import current_sid
from .storage import atomic_bytes
from .validation import canonical, digest, strict_loads, utc_now, parse_utc

NS = 'http://schemas.microsoft.com/windows/2004/02/mit/task'


def due_on(day, clock, zone):
    if not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', clock):
        raise ValueError('INVALID_SCHEDULE_TIME')
    tz = ZoneInfo(zone)
    wall = datetime.combine(day, time.fromisoformat(clock))
    # Fall-back: first occurrence only. Spring gap: first valid minute after gap.
    for minutes in range(181):
        candidate = (wall + timedelta(minutes=minutes)).replace(tzinfo=tz, fold=0)
        if candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return candidate.astimezone(timezone.utc)
    raise ValueError('UNRESOLVABLE_LOCAL_TIME')


def latest_due(now, clock, zone):
    local_day = now.astimezone(ZoneInfo(zone)).date()
    if due_on(local_day, clock, zone) > now:
        local_day -= timedelta(days=1)
    return local_day


class ScheduleEngine:
    def __init__(self, store):
        self.store = store

    def current(self):
        rows = self.store.admin_events('SCHEDULE_STATE')
        return rows[-1] if rows else {'schedule_key': None, 'claimed_day': None, 'missed_days': 0, 'entries': []}

    def save(self, state):
        context = {'source_id': 'x:0', 'policy_id': 'local-admin', 'policy_version': 1, 'purpose': 'local-management'}
        self.store.save_admin('SCHEDULE_STATE', state, context)

    def claim(self, settings, accounts, now=None):
        if not settings['schedule_enabled'] or settings['paused']:
            return self.current()
        now = parse_utc(now or utc_now())
        day = latest_due(now, settings['schedule_local_time'], settings['timezone'])
        key = digest([settings['schedule_local_time'], settings['timezone']])
        old = self.current()
        # Unfinished catch-up always resumes, even if the settings or local day changes.
        if any(entry['status'] in ('PENDING', 'STARTED', 'BLOCKED') for entry in old['entries']):
            return old
        if old['claimed_day'] and day <= date.fromisoformat(old['claimed_day']):
            return old
        missed = max(1, (day - date.fromisoformat(old['claimed_day'])).days) if old['claimed_day'] else 1
        entries = []
        for account in accounts:
            if account['active_status'] == 'ACTIVE':
                rid = uuid.uuid5(uuid.NAMESPACE_URL, str(self.store.root.resolve()) + ':' + day.isoformat() + ':' + account['account_id']).hex
                entries.append({'account_id': account['account_id'], 'request_id': rid, 'status': 'PENDING', 'job_id': None, 'error_code': None})
        if len(entries) > 150:
            raise ValueError('SCHEDULE_ACCOUNT_LIMIT_150')
        result = {'schedule_key': key, 'claimed_day': day.isoformat(), 'missed_days': missed, 'entries': entries}
        self.save(result)
        return result

    def step(self, service, now=None):
        state = self.claim(service.settings, list(service.accounts().values()), now)
        if not service.settings['schedule_enabled'] or service.settings['paused']:
            return
        for entry in state['entries']:
            if entry['status'] == 'STARTED':
                job = service.store.jobs().get(entry['job_id'])
                if job and job['state'] in ('DONE', 'FAILED', 'NEEDS_REVIEW'):
                    entry['status'] = 'DONE' if job['state'] == 'DONE' else 'FAILED'
                    self.save(state)
                return
        for entry in state['entries']:
            if entry['status'] != 'PENDING':
                continue
            try:
                result = service.start_sync({'account_id': entry['account_id'], 'confirm_live': True, 'scheduled': True}, entry['request_id'])
                entry.update({'job_id': result['job_id'], 'status': 'STARTED', 'error_code': None})
            except Exception as exc:
                entry.update({'status': 'BLOCKED', 'error_code': type(exc).__name__})
            self.save(state)
            return


class WindowsTasks:
    def __init__(self, root, private):
        self.root, self.private = Path(root).resolve(), Path(private)
        self.name = 'ACGCollector_' + digest(str(self.root))[:20]

    def xml(self, clock, sid, program=None):
        if not re.fullmatch(r'S-1-[0-9-]+', sid):
            raise ValueError('INVALID_USER_SID')
        time.fromisoformat(clock)
        ET.register_namespace('', NS)
        def node(parent, tag, text=None, **attributes):
            item = ET.SubElement(parent, '{' + NS + '}' + tag, attributes)
            if text is not None:
                item.text = text
            return item
        task = ET.Element('{' + NS + '}Task', {'version': '1.4'})
        registration = node(task, 'RegistrationInfo')
        node(registration, 'Description', 'ACG Collector owned task ' + digest(str(self.root)))
        triggers = node(task, 'Triggers')
        calendar = node(triggers, 'CalendarTrigger')
        node(calendar, 'StartBoundary', '2026-01-01T' + clock + ':00')
        node(calendar, 'Enabled', 'true')
        node(node(calendar, 'ScheduleByDay'), 'DaysInterval', '1')
        logon = node(triggers, 'LogonTrigger')
        node(logon, 'Enabled', 'true')
        node(logon, 'UserId', sid)
        principal = node(node(task, 'Principals'), 'Principal', id='Author')
        node(principal, 'UserId', sid)
        node(principal, 'LogonType', 'InteractiveToken')
        node(principal, 'RunLevel', 'LeastPrivilege')
        settings = node(task, 'Settings')
        node(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
        node(settings, 'DisallowStartIfOnBatteries', 'false')
        node(settings, 'StopIfGoingOnBatteries', 'false')
        node(settings, 'StartWhenAvailable', 'true')
        node(settings, 'ExecutionTimeLimit', 'PT0S')
        action = node(node(task, 'Actions', Context='Author'), 'Exec')
        node(action, 'Command', str(program or sys.executable))
        script = Path(__file__).resolve().parents[1] / 'worker.py'
        node(action, 'Arguments', subprocess.list2cmdline([str(script), '--root', str(self.root), '--scheduled']))
        node(action, 'WorkingDirectory', str(script.parent))
        return ET.tostring(task, encoding='utf-16', xml_declaration=True)

    def register(self, settings):
        if os.name != 'nt':
            raise RuntimeError('WINDOWS_TASK_SCHEDULER_REQUIRED')
        expected = {'America/Vancouver': 'Pacific Standard Time', 'America/Los_Angeles': 'Pacific Standard Time',
                    'America/Toronto': 'Eastern Standard Time', 'UTC': 'UTC', 'Asia/Tokyo': 'Tokyo Standard Time'}
        system = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', '(Get-TimeZone).Id'],
            capture_output=True, text=True, check=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        if expected.get(settings['timezone']) != system:
            raise ValueError('WINDOWS_AND_IANA_TIMEZONE_MUST_MATCH')
        document = self.xml(settings['schedule_local_time'], current_sid())
        # Refuse overwriting another task, even if it happens to have our task name.
        existing = subprocess.run(['schtasks.exe', '/Query', '/TN', self.name, '/XML'], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if existing.returncode == 0:
            old = ET.fromstring(existing.stdout)
            description = old.find('.//{' + NS + '}Description')
            if description is None or description.text != 'ACG Collector owned task ' + digest(str(self.root)):
                raise ValueError('SCHEDULE_TASK_OWNERSHIP_MISMATCH')
        file = self.private / 'schedule.xml'
        atomic_bytes(file, document)
        subprocess.run(['schtasks.exe', '/Create', '/TN', self.name, '/XML', str(file), '/F'],
            capture_output=True, check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        return {'task_name': self.name, 'installed': True}

    def disable(self):
        if os.name != 'nt':
            raise RuntimeError('WINDOWS_TASK_SCHEDULER_REQUIRED')
        # Disable instead of deleting. Only our hash-derived task is ever addressed.
        existing = subprocess.run(['schtasks.exe', '/Query', '/TN', self.name, '/XML'], capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW)
        if existing.returncode != 0:
            return {'disabled': True, 'already_absent': True}
        document = ET.fromstring(existing.stdout)
        description = document.find('.//{' + NS + '}Description')
        if description is None or description.text != 'ACG Collector owned task ' + digest(str(self.root)):
            raise ValueError('SCHEDULE_TASK_OWNERSHIP_MISMATCH')
        subprocess.run(['schtasks.exe', '/Change', '/TN', self.name, '/DISABLE'], capture_output=True,
            check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        return {'disabled': True}
