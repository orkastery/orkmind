import copy
import json
import sys
from pathlib import Path

import pytest

from orkmind.core.company_brain import digest
from orkmind.core.company_brain_migration import BrainMigration

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_company_brain_store import connection, TransactionTestStore

# Exige um Postgres isolado (ORKMIND_TEST_DATABASE_URL) e falha sem ele, de proposito.
# A marca so permite que um CI sem banco deixe o modulo de fora com -m "not integration".
pytestmark = pytest.mark.integration


def test_migration_and_compensation_preserve_concurrent_entities(connection):
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog']
    store=TransactionTestStore(connection);migration=BrainMigration(store)
    plan=migration.plan(catalog,'batch-one','a'*64,'2026-09-13T20:00:00Z')
    receipt=migration.apply(plan,digest(plan),'a'*64)
    assert migration.apply(plan,digest(plan),'a'*64)==receipt
    concurrent=copy.deepcopy(catalog[0]);concurrent['id']='prod-concurrent'
    event=migration._event(concurrent,'batch-concurrent',1);store.ingest(event)
    result=migration.rollback('synthetic','batch-one')
    assert result['state']=='rolled-back'
    rows=connection.execute("SELECT id FROM brain_projection WHERE state='active'").fetchall()
    assert rows==[dict(id='prod-concurrent')]
    assert connection.execute('SELECT count(*) AS n FROM brain_history').fetchone()['n']==7


def test_insert_rollback_reinsert_uses_monotonic_inbox_and_compensation_history(connection):
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog'][:1]
    store=TransactionTestStore(connection);migration=BrainMigration(store)
    first=migration.plan(catalog,'first','a'*64,'2026-09-13T20:00:00Z');migration.apply(first,digest(first),'a'*64)
    assert migration.rollback('synthetic','first')['state']=='rolled-back'
    again=migration.plan(catalog,'again','a'*64,'2026-09-13T20:00:00Z');migration.apply(again,digest(again),'a'*64)
    rows=connection.execute('SELECT sequence FROM brain_inbox ORDER BY sequence').fetchall()
    assert [r['sequence'] for r in rows]==[1,2,3]
    current=connection.execute('SELECT * FROM brain_projection').fetchone()
    assert current['sequence']==3 and current['state']=='active' and current['body']==catalog[0]
    assert connection.execute('SELECT count(*) AS n FROM brain_history').fetchone()['n']==3


def test_thirteen_ids_preserve_newer_version_of_same_entity_and_its_references(connection):
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-13.json').read_text())['catalog']
    assert len({e['id'] for e in catalog})==13
    store=TransactionTestStore(connection);migration=BrainMigration(store)
    first=migration.plan(catalog,'thirteen','b'*64,'2026-09-13T20:00:00Z');receipt=migration.apply(first,digest(first),'b'*64)
    assert set(receipt['changed'])=={e['id'] for e in catalog}
    later=copy.deepcopy(next(e for e in catalog if e['id']=='init-i25-company-brain-foundation'))
    later.update(version=2,title='Concurrent edit of the same entity');later['source']['source_version']=2
    store.ingest(migration._event(later,'concurrent',2))
    result=migration.rollback('synthetic','thirteen')
    assert result['state']=='conflict'
    assert set(result['conflicts'])=={'init-i25-company-brain-foundation','proj-company-brain','prod-orkmind'}
    assert set(result['restored'])|set(result['conflicts'])=={e['id'] for e in catalog}
    assert migration.rollback('synthetic','thirteen')==result
    current=connection.execute('SELECT body FROM brain_projection WHERE id=%s',(later['id'],)).fetchone()
    assert current['body']==later
    for id in result['restored']:
        seq=connection.execute('SELECT sequence FROM brain_inbox WHERE aggregate_id=%s ORDER BY sequence',(id,)).fetchall()
        assert [r['sequence'] for r in seq]==[1,2]


def test_apply_replay_requires_current_migration_grant(connection):
    import pytest
    from orkmind.core.company_brain_access import Principal
    from orkmind.store.company_brain import BrainStore
    from test_company_brain_access import grant
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog'][:1]
    grant(connection,[catalog[0]['id']],[],principal='writer',kind='service',actions=['migrate','ingest','rollback'])
    migration=BrainMigration(BrainStore(connection,Principal('writer','synthetic','service',True)))
    plan=migration.plan(catalog,'protected','a'*64,'2026-09-13T20:00:00Z');receipt=migration.apply(plan,digest(plan),'a'*64)
    assert migration.apply(plan,digest(plan),'a'*64)==receipt
    connection.execute('UPDATE brain_grants SET revoked=true')
    with pytest.raises(PermissionError):migration.apply(plan,digest(plan),'a'*64)
    with pytest.raises(PermissionError):migration.rollback('synthetic','protected')


def test_update_rollback_keeps_sequence_while_restoring_previous_business_version(connection):
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog'][:1]
    store=TransactionTestStore(connection);migration=BrainMigration(store)
    original=migration.plan(catalog,'original','a'*64,'2026-09-13T20:00:00Z');migration.apply(original,digest(original),'a'*64)
    updated=copy.deepcopy(catalog);updated[0].update(version=2,title='Updated')
    plan=migration.plan(updated,'update','b'*64,'2026-09-13T20:00:00Z');migration.apply(plan,digest(plan),'b'*64)
    assert migration.rollback('synthetic','update')['state']=='rolled-back'
    row=connection.execute('SELECT * FROM brain_projection').fetchone()
    assert row['body']==catalog[0] and row['sequence']==3
    store.ingest(migration._event(updated[0],'next',4))
    assert store.next_sequence('synthetic',catalog[0]['id'])==5
