if __name__ == '__main__':
    import os,sys,tempfile,time
    from pathlib import Path
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from desktop.ipc import Client,launch_worker
    from desktop.gui import MainWindow
    app=QApplication([]);app.setQuitOnLastWindowClosed(False)
    def wait_ui(milliseconds):
        deadline=time.monotonic()+milliseconds/1000
        while time.monotonic()<deadline:
            app.processEvents();time.sleep(.005)
    output=Path(__file__).resolve().parents[1]/'docs/ui-evidence';output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory)/'data';base=Path(directory)/'private';process=launch_worker(root,base)
        client=Client(root,base,timeout=2000)
        try:
            for _ in range(50):
                try:client.call('snapshot');break
                except Exception:wait_ui(60)
            client.call('load_demo');client.call('start_sync',{'account_id':'mock-artist-1'})
            window=MainWindow(client,polling=False);window.show()
            for _ in range(60):
                wait_ui(80)
                if client.call('snapshot')['active']==0:break
            window.update_status(client.call('snapshot'));wait_ui(200)
            window.grab().save(str(output/'dashboard.png'))
            for index,name in ((1,'accounts'),(3,'posts'),(4,'media'),(6,'policies'),(8,'logs')):
                window.show_page(index);wait_ui(300)
                window.grab().save(str(output/(name+'.png')))
            window.resize(960,640);window.show_page(3);wait_ui(300)
            window.grab().save(str(output/'posts-960.png'))
            window.resize(1320,860);window.edit_settings();wait_ui(200)
            dialog=window.dialogs[-1];dialog.grab().save(str(output/'settings.png'))
            dialog.original=dialog.values();dialog.close();window.close();window.pool.waitForDone(2000);wait_ui(100)
        finally:
            try:client.call('stop_worker');process.wait(timeout=5)
            except Exception:process.kill();process.wait()
    print(output)
