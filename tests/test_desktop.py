import copy, importlib.util, json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
if os.name!='nt' and not os.environ.get('DISPLAY'):os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
HAS_QT=importlib.util.find_spec('PySide6') is not None
ROOT=Path(__file__).resolve().parents[1]
if HAS_QT:
    from PySide6.QtWidgets import QApplication,QDialogButtonBox,QLineEdit
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from desktop.service import WorkerService
    from desktop.ipc import Client,launch_worker,endpoint
    from desktop.gui import MainWindow
    from desktop.widgets import DataPage,EditorDialog
    from collector.storage import StorageEngine
    from collector.validation import digest,strict_loads

def wait_ui(milliseconds):
    deadline=time.monotonic()+milliseconds/1000
    while time.monotonic()<deadline:
        QApplication.instance().processEvents()
        time.sleep(.005)

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.private=Path(self.tmp.name)/'private';self.private.mkdir()
        self.store=StorageEngine(self.root);self.addCleanup(self.store.close)
        self.s=WorkerService(self.store,self.private);self.s.load_demo()
    def sync(self):
        jid=self.s.start_sync({'account_id':'mock-artist-1'},'a'*32)['job_id']
        for _ in range(5):self.s.tick()
        return jid
    def test_account_conflict_and_stable_id(self):
        row=self.s.accounts()['mock-artist-1'];new=copy.deepcopy(row);new['current_handle']='changed'
        with self.assertRaisesRegex(ValueError,'CONFLICT'):self.s.save_account({'account':new,'revision':'wrong'})
        self.s.save_account({'account':new,'revision':digest(row)})
        new['account_id']='other'
        with self.assertRaisesRegex(ValueError,'STABLE_ID'):self.s.save_account({'account':new,'revision':None})
    def test_pause_does_not_stop_existing_run(self):
        jid=self.s.start_sync({'account_id':'mock-artist-1'},'b'*32)['job_id'];self.s.pause(True)
        with self.assertRaisesRegex(ValueError,'PAUSED'):self.s.start_sync({'account_id':'mock-artist-1'},'c'*32)
        for _ in range(5):self.s.tick()
        self.assertEqual(self.store.jobs()[jid]['state'],'DONE');self.assertEqual(self.s.snapshot()['state'],'PAUSED')
    def test_start_idempotency_survives_restart(self):
        first=self.sync();s=WorkerService(self.store,self.private)
        again=s.start_sync({'account_id':'mock-artist-1'},'a'*32)['job_id']
        self.assertEqual(first,again);self.assertEqual(len(self.store.jobs()),1)
    def test_review_stages_and_redistribute(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        args={'post_id':pid,'status':'PUBLISHABLE','category':'SFW','reviewer':'test','evidence_uri':'mock://review','notes':''}
        with self.assertRaisesRegex(ValueError,'REVIEW_BEFORE'):self.s.review(args)
        args['status']='REVIEWED';self.s.review(args);args['status']='PUBLISHABLE'
        with self.assertRaises(PermissionError):self.s.review(args)
        policy=copy.deepcopy(self.s.policies()[-1]);policy.update({'operation':'redistribute','permission_state':'PERMITTED','version':2})
        self.s.save_policy({'policy':policy});self.s.review(args)
        self.assertEqual(self.s.posts()[0]['review_status'],'PUBLISHABLE')
    def test_policy_bundle_version_keeps_posts(self):
        self.sync();policy=copy.deepcopy(self.s.policies()[0]);policy['version']=2
        self.s.save_policy({'policy':policy})
        self.assertEqual(len(self.store.current_posts()),3)
        self.assertEqual(set(row['version'] for row in self.s.policies()),{2})
        self.assertEqual(self.s.posts()[0]['rights_status'],'PERMITTED')
    def test_revoke_retain_requires_confirmation(self):
        self.sync();p=copy.deepcopy(self.s.policies()[1]);p.update({'version':2,'permission_state':'REVOKED'})
        with self.assertRaisesRegex(ValueError,'CONFIRM'):self.s.save_policy({'policy':p})
        self.s.save_policy({'policy':p,'confirmation':p['policy_id']});self.assertEqual(self.store.current_posts(),{})
    def test_token_excluded_from_logs_snapshot(self):
        token='placeholder-test-secret';self.s.dispatch('save_token',{'token':token},'d'*32)
        self.assertTrue(self.s.snapshot()['token_configured']);self.assertNotIn(token,str(self.s.snapshot()))
        exported=self.s.export_diagnostics()
        self.assertNotIn(token,Path(exported['path']).read_text())
        self.assertNotIn('APIFY_TOKEN',Path(exported['path']).read_text())
        for p in (self.root/'journal/segments').glob('*.jsonl'):self.assertNotIn(token,p.read_text())
        if os.name!='nt':self.assertEqual((self.private/'.env').stat().st_mode&0o777,0o600)
    def test_limits_and_scheduler_disabled(self):
        settings=copy.deepcopy(self.s.settings);settings['schedule_enabled']=True
        with self.assertRaises(ValueError):self.s.save_settings({'settings':settings,'revision':digest(self.s.settings)})
        settings['schedule_enabled']=False;settings['min_free_bytes']=1
        with self.assertRaises(ValueError):self.s.save_settings({'settings':settings,'revision':digest(self.s.settings)})
    def test_composed_filter_and_paging(self):
        self.sync();result=self.s.query({'kind':'posts','filters':{'post_type':'ORIGINAL','from':'2026-10-24','until':'2026-10-24'},'limit':1})
        self.assertEqual(result['total'],3);self.assertEqual(len(result['rows']),1);self.assertNotIn('text',result['rows'][0])
        with self.assertRaises(ValueError):self.s.query({'kind':'posts','limit':1000})
    def test_deleted_review_notes_purged(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        self.s.review({'post_id':pid,'status':'REVIEWED','category':'SFW','reviewer':'test','evidence_uri':'mock://review','notes':'unique-deleted-review'})
        self.s.delete_post({'post_id':pid,'confirmation':pid})
        for p in (self.root/'journal/segments').glob('*.jsonl'):self.assertNotIn('unique-deleted-review',p.read_text())
    def test_live_requires_confirmation(self):
        self.s.settings['mode']='LIVE'
        with self.assertRaises(PermissionError):self.s.start_sync({'account_id':'mock-artist-1'},'e'*32)
        self.assertEqual(self.store.jobs(),{})
    def test_preview_rechecks_revoked_rights(self):
        self.sync();pid=next(iter(self.store.current_posts().values()))['post_id']
        mid=self.s.queue_media({'post_id':pid,'url':'https://pbs.twimg.com/mock.png','rating':'SFW','evidence_uri':'mock://rating','expires_at':'2027-01-01T00:00:00Z'})['media_id']
        self.s.process_media({'media_id':mid});self.assertTrue(self.s.preview({'media_id':mid})['path'])
        p=copy.deepcopy(self.s.policies()[2]);p.update({'version':2,'permission_state':'REVOKED'});self.s.save_policy({'policy':p})
        with self.assertRaises(PermissionError):self.s.preview({'media_id':mid})

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class QtWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([]);cls.app.setQuitOnLastWindowClosed(False)
    def test_nine_pages_navigation(self):
        class NoClient:
            def call(self,*args):return {'rows':[],'total':0,'offset':0}
        w=MainWindow(NoClient(),polling=False);w.show();wait_ui(60)
        self.assertEqual(w.stack.count(),9);self.assertEqual(w.navigation.accessibleName(),'主导航')
        for i in range(9):w.navigation.setCurrentRow(i);wait_ui(15);self.assertEqual(w.stack.currentIndex(),i)
        w.close();w.pool.waitForDone(1000);wait_ui(30)
    def test_table_selection_sort_clear(self):
        p=DataPage('帖子','说明',[('post_id','ID'),('account_id','账号')]);p.populate({'rows':[{'post_id':'2','account_id':'b'},{'post_id':'1','account_id':'a'}],'total':2})
        p.show();p.table.sortItems(0,Qt.SortOrder.AscendingOrder);p.table.selectRow(0)
        self.assertEqual(p.selected()['post_id'],'1');p.search.setText('query');self.assertEqual(p.query_args('posts')['search'],'query')
        p.search.clear();self.assertEqual(p.offset,0);p.close()
    def test_form_preserves_errors_and_masks_secret(self):
        d=EditorDialog(None,'测试',[('token','Token','secret',''),('mode','模式',['OFFLINE','LIVE'],'OFFLINE')]);d.show();d.widgets['token'].setText('placeholder')
        self.assertEqual(d.widgets['token'].echoMode(),QLineEdit.EchoMode.Password)
        calls=[];d.submitted.connect(calls.append);QTest.mouseClick(d.box.button(QDialogButtonBox.StandardButton.Save),Qt.MouseButton.LeftButton)
        self.assertTrue(d.busy);self.assertEqual(len(calls),1);d.failed('VALIDATION_ERROR')
        self.assertEqual(d.widgets['token'].text(),'placeholder');self.assertFalse(d.busy);self.assertTrue(d.error.isVisible())
        d.original=d.values();d.close()
    def test_native_combo_popup(self):
        d=EditorDialog(None,'模式',[('mode','模式',['OFFLINE','LIVE'],'OFFLINE')]);d.show();c=d.widgets['mode'];c.setFocus();c.showPopup();wait_ui(30)
        self.assertTrue(c.view().isVisible());QTest.keyClick(c,Qt.Key.Key_Escape);c.setCurrentIndex(1);self.assertEqual(d.values()['mode'],'LIVE')
        d.original=d.values();d.close()
    def test_theme_tokens(self):
        from desktop.theme import TOKENS
        for value in TOKENS.values():self.assertIn(value,(ROOT/'DESIGN.md').read_text())

@unittest.skipUnless(HAS_QT,'Install requirements-desktop.txt')
class ProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'data';self.base=Path(self.tmp.name)/'private'
        self.proc=launch_worker(self.root,self.base);self.addCleanup(self.finish);self.client=Client(self.root,self.base,timeout=650)
        for _ in range(50):
            try:self.client.call('snapshot');break
            except Exception:time.sleep(.05)
        else:self.fail('Worker startup failed')
    def finish(self):
        if self.proc.poll() is None:
            try:self.client.call('stop_worker');self.proc.wait(timeout=5)
            except Exception:self.proc.kill();self.proc.wait()
    def test_real_IPC_and_shutdown(self):
        self.client.call('load_demo');self.assertEqual(self.client.call('snapshot')['accounts'],1)
        self.client.call('pause');self.assertEqual(self.client.call('snapshot')['state'],'PAUSED')
        self.client.call('stop_worker');self.assertEqual(self.proc.wait(timeout=5),0)
    def test_bad_token_denied(self):
        p=endpoint(self.root,self.base);original=p.read_bytes();marker=strict_loads(original);marker['token']='wrong';p.write_text(json.dumps(marker))
        try:
            with self.assertRaisesRegex(RuntimeError,'IPC_AUTH_REQUIRED'):self.client.call('snapshot')
        finally:p.write_bytes(original)
    def test_GUI_exit_keeps_worker_running(self):
        self.client.call('load_demo');self.client.call('start_sync',{'account_id':'mock-artist-1'})
        code="from PySide6.QtWidgets import QApplication; from PySide6.QtCore import QTimer; from desktop.gui import MainWindow; from desktop.ipc import Client; import sys; app=QApplication([]); w=MainWindow(Client(sys.argv[1],sys.argv[2]),polling=False); w.show(); QTimer.singleShot(100,w.close); app.exec()"
        result=subprocess.run([sys.executable,'-c',code,str(self.root),str(self.base)],cwd=ROOT,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIsNone(self.proc.poll())
        for _ in range(60):
            status=self.client.call('snapshot')
            if status['active']==0:break
            time.sleep(.08)
        self.assertEqual(status['posts'],3)
    def test_native_GUI_sync_and_review_full_flow(self):
        window=MainWindow(self.client,polling=False);self.addCleanup(window.close);window.show();window.load_demo()
        for _ in range(80):
            wait_ui(50)
            if window.last_snapshot and window.last_snapshot['accounts']==1:break
        window.show_page(1)
        for _ in range(60):
            wait_ui(50)
            if window.pages['accounts'].table.rowCount():break
        window.pages['accounts'].table.selectRow(0);window.start_selected()
        for _ in range(100):
            wait_ui(50);window.refresh_status()
            if window.last_snapshot and window.last_snapshot['posts']==3 and window.last_snapshot['active']==0:break
        window.show_page(3)
        for _ in range(60):
            wait_ui(50)
            if window.pages['posts'].table.rowCount()==3:break
        self.assertEqual(window.pages['posts'].table.rowCount(),3,str(window.last_snapshot)+' '+window.statusBar().currentMessage()+' '+window.pages['posts'].notice.text())
        window.pages['posts'].table.selectRow(0);window.review();dialog=window.dialogs[-1]
        dialog.widgets['reviewer'].setText('QtTest reviewer');dialog.widgets['evidence_uri'].setText('mock://review')
        dialog.widgets['category'].setCurrentText('SFW')
        QTest.mouseClick(dialog.box.button(QDialogButtonBox.StandardButton.Save),Qt.MouseButton.LeftButton)
        for _ in range(60):
            wait_ui(50)
            if not dialog.isVisible():break
        self.assertFalse(dialog.isVisible(),dialog.error.text())
        result=self.client.call('query',{'kind':'posts','filters':{'review_status':'REVIEWED'}})
        self.assertEqual(result['total'],1)
        window.close();window.pool.waitForDone(2000);wait_ui(50)

    def test_duplicate_intent_after_restart(self):
        self.client.call('load_demo');rid='f'*32;first=self.client.call('start_sync',{'account_id':'mock-artist-1'},rid)
        self.client.call('stop_worker');self.proc.wait(timeout=5);self.proc=launch_worker(self.root,self.base)
        for _ in range(50):
            try:self.client.call('snapshot');break
            except Exception:time.sleep(.05)
        again=self.client.call('start_sync',{'account_id':'mock-artist-1'},rid)
        self.assertEqual(first,again);self.assertEqual(self.client.call('snapshot')['jobs'],1)

if __name__=='__main__':unittest.main()
