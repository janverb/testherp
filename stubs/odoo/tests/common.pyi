from typing import Any, Dict, Optional

import sys
import unittest

PY3 = sys.version_info >= (3, 0)

if PY3:
    from urllib.request import OpenerDirector

    ServerProxy = Any
else:
    from urllib2 import OpenerDirector
    from xmlrpclib import ServerProxy

class TreeCase(unittest.TestCase): ...
class BaseCase(TreeCase): ...
class TransactionCase(BaseCase): ...

class HttpCase(TransactionCase):
    opener: OpenerDirector
    xmlrpc_url: str
    xmlrpc_common: ServerProxy
    xmlrpc_db: ServerProxy
    xmlrpc_object: ServerProxy
    def url_open(
        self, url: str, data: Optional[Dict[str, str]] = None, timeout: int = 10
    ) -> object: ...
    def phantom_js(
        self,
        url_path: str,
        code: str,
        ready: str = "window",
        login: Optional[str] = None,
        timeout: int = 60,
        **kw: Any
    ) -> None: ...
    def phantom_run(self, cmd: str, timeout: int) -> None: ...
