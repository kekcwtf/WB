"""Асинхронные фото из URL карточек WB. API-токен в этот модуль не передаётся."""
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit
from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkDiskCache, QNetworkReply


def allowed_image(url):
    try:
        parsed=urlsplit(url)
        host=parsed.hostname or ''
        return (parsed.scheme=='https' and not parsed.username and not parsed.password and parsed.port in (None,443)
                and any(host==domain or host.endswith('.'+domain) for domain in ('wbbasket.ru','wbstatic.net','wildberries.ru')))
    except ValueError:
        return False


class ProductImages(QObject):
    ready=Signal(str,QPixmap)
    def __init__(self,cache_folder,parent=None):
        super().__init__(parent)
        self.manager=QNetworkAccessManager(self)
        cache=QNetworkDiskCache(self)
        cache.setCacheDirectory(str(Path(cache_folder)))
        cache.setMaximumCacheSize(64*1024*1024)
        self.manager.setCache(cache)
        self.pending=set()
        self.pixmaps=OrderedDict()
        self.failed=set()
    def request(self,url):
        if not allowed_image(url) or url in self.pending or url in self.failed:return
        if url in self.pixmaps:
            self.pixmaps.move_to_end(url)
            self.ready.emit(url,self.pixmaps[url]);return
        self.pending.add(url)
        request=QNetworkRequest(QUrl(url))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setTransferTimeout(15000)
        reply=self.manager.get(request)
        def progress(received,total):
            if received>2*1024*1024 or total>2*1024*1024:reply.abort()
        reply.downloadProgress.connect(progress)
        def finished():
            self.pending.discard(url)
            pixmap=QPixmap()
            status=reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if reply.error()==QNetworkReply.NetworkError.NoError and status==200 and pixmap.loadFromData(reply.readAll()):
                pixmap=pixmap.scaled(48,48)
                self.pixmaps[url]=pixmap
                while len(self.pixmaps)>256:self.pixmaps.popitem(last=False)
                self.ready.emit(url,pixmap)
            else:self.failed.add(url)
            reply.deleteLater()
        reply.finished.connect(finished)
