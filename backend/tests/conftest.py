from __future__ import annotations
import os
os.environ.setdefault("UNIT_TEST_MODE", "1")

import sys
import types
from sqlalchemy.orm import DeclarativeBase

if 'app.core.database' not in sys.modules:
    stub = types.ModuleType('app.core.database')
    class Base(DeclarativeBase):
        pass
    class DummySessionFactory:
        def __call__(self, *args, **kwargs):
            raise RuntimeError('DB factory is intentionally unavailable in deterministic unit tests')
    stub.Base = Base
    stub.AsyncSessionLocal = DummySessionFactory()
    stub.engine = None
    stub.get_db = None
    sys.modules['app.core.database'] = stub
