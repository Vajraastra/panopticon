"""Private Cherry view/runtime/catalog with real localhost HTTP and a second module."""
import asyncio
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PIL import Image
from PySide6.QtCore import QTimer,Qt
from PySide6.QtWidgets import QApplication,QWidget
from core.paths import ProjectPaths
from core.theme_manager import ThemeManager
from core.event_bus import EventBus
from core.module_window import ModuleWindow
from core.library_service import LibraryService
from modules.cherry.module import CherryModule
from modules.librarian.module import LibrarianModule
from third_party.cherry_dl import config,index,catalog,download_service
from third_party.cherry_dl.templates import _registry
from third_party.cherry_dl.templates.base import ArtistInfo,FileInfo,SiteTemplate


def pump(predicate,seconds=8):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        QApplication.processEvents()
        if predicate():return True
        time.sleep(.003)
    return False


class Dashboard(QWidget):
    def tr(self,key,default=''):return default


class CherryOperational(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.stack=ExitStack();self.entered=threading.Event();self.release=threading.Event();self.hits=[]
        self.addCleanup(self.cleanup)
        payloads={}
        for name,color in (('one.png','red'),('two.png','blue')):
            buffer=io.BytesIO();Image.new('RGB',(32,24),color).save(buffer,format='PNG');payloads['/'+name]=buffer.getvalue()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.hits.append(self.path);owner.entered.set()
                owner.release.wait(5)
                raw=payloads[self.path]
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except ConnectionError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        base='http://127.0.0.1:'+str(self.server.server_port)
        class Template(SiteTemplate):
            name='releasefixture';workers=1
            @classmethod
            def can_handle(cls,url):return url.startswith('releasefixture://')
            async def get_artist_info(self,url):return ArtistInfo('artist','Artist','releasefixture',self.name,url)
            async def iter_files(self,artist,since=None):
                for name in ('one.png','two.png'):
                    yield FileInfo(url=base+'/'+name,filename=name,artist_id='artist',artist_name='Artist',post_id=name,
                                   url_source='releasefixture://'+name)
        self.folder=self.root/'collection'/'portátil ñ';self.db=self.root/'index.db'
        self.cfg=config.UserConfig(download_dir=str(self.root/'collection'),workers=1,
                    network=config.NetworkConfig(delay_min=0,delay_max=0,retries_api=1,retries_file=1))
        for obj,key,value in ((ProjectPaths,'root',self.root),(config,'INDEX_DB',self.db),
            (config,'CONFIG_FILE',self.root/'config.toml'),(config,'SESSION_FILE',self.root/'session.json'),
            (config,'CHERRY_DIR',self.root/'cherry'),(download_service,'INDEX_DB',self.db),(_registry,'_TEMPLATES',[Template])):
            self.stack.enter_context(patch.object(obj,key,return_value=value) if obj is ProjectPaths else patch.object(obj,key,value))
        async def create():
            await catalog.init_catalog(self.folder);await index.init_index(self.db)
            pid=await index.create_profile(self.db,'Artist',self.folder,'releasefixture')
            await index.add_profile_url(self.db,pid,'releasefixture://artist','releasefixture')
            return await index.get_profile(self.db,pid)
        self.profile=asyncio.run(create())
        self.service=LibraryService(self.root/'panopticon.db')
        context=dict(data_service=self.service,event_bus=EventBus(),theme_manager=ThemeManager())
        self.cherry=CherryModule();self.cherry.on_load(context)
        self.librarian=LibrarianModule();self.librarian.on_load(context)
        self.dashboard=Dashboard()
        self.windows=[ModuleWindow(self.dashboard,m,m.get_view()) for m in (self.cherry,self.librarian)]
        for window in self.windows:
            window.setAttribute(Qt.WA_DontShowOnScreen,True);window.show()
        self.assertTrue(pump(lambda:self.cherry.runtime.is_running and bool(self.cherry.view._profiles)))

    def cleanup(self):
        self.release.set()
        if hasattr(self,'cherry'):
            self.cherry.view._bridge.cancel();self.cherry.on_unload()
            self.assertTrue(pump(self.cherry.shutdown_ready))
        if hasattr(self,'librarian'):
            self.librarian.on_unload();self.assertTrue(pump(self.librarian.shutdown_ready))
        if hasattr(self,'windows'):
            for window in self.windows:window.hide();window.deleteLater()
            self.dashboard.deleteLater()
        QApplication.processEvents()
        if hasattr(self,'service'):self.service.close()
        self.server.shutdown();self.server.server_close();self.server_thread.join(3)
        self.assertFalse(self.server_thread.is_alive())
        self.stack.close();self.temp.cleanup()

    def start(self):
        return self.cherry.view._bridge.start(self.profile,workers=1,force_full=True,config=self.cfg)

    def test_download_hide_reopen_other_module_and_receipts_skip_repeat(self):
        bridge=self.cherry.view._bridge;summaries=[];bridge.finished_signal.connect(summaries.append)
        self.assertTrue(self.start());self.assertTrue(pump(self.entered.is_set))
        self.assertFalse(self.start())
        self.windows[0].close();self.assertFalse(self.windows[0].isVisible())
        self.assertTrue(bridge.is_busy);self.assertTrue(self.windows[1].isVisible())
        ticks=[];QTimer.singleShot(0,lambda:ticks.append(True));self.assertTrue(pump(lambda:bool(ticks)))
        self.windows[0].show();self.assertIs(self.cherry.view._bridge,bridge)
        self.release.set();self.assertTrue(pump(lambda:len(summaries)==1 and not bridge.is_busy))
        self.assertEqual(summaries[0].downloaded,2);self.assertEqual(len(self.hits),2)
        rows=self.cherry.runtime.run_sync(catalog.get_all_files(self.folder),timeout=3)
        self.assertEqual(len(rows),2)
        self.assertTrue(self.start());self.assertTrue(pump(lambda:len(summaries)==2 and not bridge.is_busy))
        self.assertEqual(summaries[1].downloaded,0);self.assertEqual(len(self.hits),2)
        portable=self.cherry.runtime.run_sync(catalog.read_profile_meta(self.folder),timeout=3)
        self.assertEqual(portable['display_name'],'Artist')

    def test_module_shutdown_cancels_active_bridge_before_runtime_drain(self):
        bridge=self.cherry.view._bridge;cancelled=[];bridge.cancelled_signal.connect(lambda:cancelled.append(True))
        self.assertTrue(self.start());self.assertTrue(pump(self.entered.is_set))
        self.cherry.on_unload()
        self.assertTrue(pump(lambda:bool(cancelled),seconds=.8),'View shutdown must cancel an active owned bridge')
        self.assertTrue(pump(self.cherry.shutdown_ready))
        pending=asyncio.run(catalog.get_pending_files(self.folder,self.profile['urls'][0]['id']))
        self.assertEqual(len(pending),2)
        self.assertFalse(self.start())


if __name__=='__main__':
    app=QApplication.instance() or QApplication([])
    print('Qt platform:',app.platformName(),flush=True)
    unittest.main()
