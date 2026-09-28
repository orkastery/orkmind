import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from orkmind.core.company_brain import digest
from orkmind.store.company_brain import BrainStore

# Exige um Postgres isolado (ORKMIND_TEST_DATABASE_URL) e falha sem ele, de proposito.
# A marca so permite que um CI sem banco deixe o modulo de fora com -m "not integration".
pytestmark = pytest.mark.integration

EVENT = next(c['value'] for c in json.loads((Path(__file__).resolve().parents[1] / 'fixtures/company-brain-v1.json').read_text())['cases'] if c['name']=='event')


@pytest.fixture
def connection():
    dsn = os.environ.get('ORKMIND_TEST_DATABASE_URL')
    if not dsn:
        pytest.fail('brain.test.database-unavailable: isolated ORKMIND_TEST_DATABASE_URL required; no skip')
    # No fallback to any factory DSN. Only a new schema in the explicit test DB.
    conn = psycopg.connect(dsn, autocommit=True, connect_timeout=3)
    schema = 'brain_c1_test_' + uuid4().hex
    conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(schema)))
    BrainStore(conn).initialize()
    try: yield conn
    finally:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        conn.close()


class TransactionTestStore(BrainStore):
    # T06 transaction-only harness. T07 tests the real authorization boundary.
    def _authorize(self,event,action): pass


@pytest.mark.parametrize('boundary', ['inbox','projection','receipt','committed'])
def test_atomic_receipt_and_replay_after_crash(connection,boundary):
    def crash(stage):
        if stage == boundary: raise RuntimeError('injected-crash')
    store = TransactionTestStore(connection,fault=crash)
    with pytest.raises(RuntimeError,match='injected-crash'): store.ingest(EVENT)
    store.fault = lambda _: None
    receipt = store.ingest(EVENT)
    for _ in range(3): assert store.ingest(copy.deepcopy(EVENT)) == receipt
    for table in ('brain_inbox','brain_projection','brain_history','brain_outbox'):
        assert connection.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 1
    assert store.index(EVENT)['stage']=='retrievable'


def test_conflict_gap_and_default_deny(connection):
    with pytest.raises(PermissionError): BrainStore(connection).ingest(EVENT)
    store=TransactionTestStore(connection);store.ingest(EVENT)
    conflict=copy.deepcopy(EVENT);conflict['payload']['title']='changed';conflict['payload_hash']=digest(conflict['payload'])
    with pytest.raises(ValueError,match='brain.event.conflict'): store.ingest(conflict)
    gap=copy.deepcopy(EVENT);gap.update(id='gap',source_event_id='gap',sequence=3)
    with pytest.raises(ValueError,match='brain.event.sequence-gap'): store.ingest(gap)
