import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve()
SERVICE_ROOT = PATH.parents[1]
ROOT = PATH.parents[3]
for entry in {str(ROOT), str(SERVICE_ROOT)}:
    if entry not in sys.path:
        sys.path.append(entry)


class _RetentionResult:
    def __init__(self, *, count=0, rowcount=0):
        self._count = count
        self.rowcount = rowcount

    def scalar_one(self):
        return self._count


class _FakeRetentionSession:
    def __init__(self):
        self.expired = {
            "operation_audit_logs": 1,
            "control_command_history": 1,
            "cloud_sync_outbox": 1,
        }
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        table = next(name for name in self.expired if name in sql)
        if sql.lstrip().upper().startswith("SELECT COUNT"):
            return _RetentionResult(count=self.expired[table])
        deleted = min(int((params or {}).get("limit", 1)), self.expired[table])
        self.expired[table] -= deleted
        return _RetentionResult(rowcount=deleted)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_retention_dry_run_and_delete_batches(monkeypatch):
    from app.services import retention as retention_module

    session = _FakeRetentionSession()
    monkeypatch.setattr(retention_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(retention_module.settings, "DATA_RETENTION_BATCH_SIZE", 1)

    dry_run = await retention_module.run_retention_once(dry_run=True)
    assert dry_run == {
        "operation_audit_logs": 1,
        "control_command_history": 1,
        "cloud_sync_outbox": 1,
    }
    assert session.commits == 0

    deleted = await retention_module.run_retention_once(batch_size=1)
    assert deleted == {
        "operation_audit_logs": 1,
        "control_command_history": 1,
        "cloud_sync_outbox": 1,
    }
    assert session.expired == {
        "operation_audit_logs": 0,
        "control_command_history": 0,
        "cloud_sync_outbox": 0,
    }
    assert session.commits == 6
