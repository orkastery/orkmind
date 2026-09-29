"""B4.2: leitura do histórico append-only de uma entidade, com banco isolado."""
import copy
import json
import re
import sys
from pathlib import Path

import pytest

from orkmind.cli.company_brain import API, request
from orkmind.core.company_brain import digest
from orkmind.core.company_brain_access import Principal
from orkmind.core.company_brain_migration import BrainMigration
from orkmind.store.company_brain import BrainStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_company_brain_context import FIELDS, TABLES, entity, grant, tombstone
from test_company_brain_store import connection, TransactionTestStore

# Exige um Postgres isolado (ORKMIND_TEST_DATABASE_URL) e falha sem ele, de proposito.
pytestmark = pytest.mark.integration

PERSON = Principal('person', 'synthetic', 'human', True)
CYCLE = dict(thread_id='thread-example', objective_id=None, objective_hash=None, project_id=None, initiative_ids=[], phase='GO',
             session_id=None, context_hash=None)


def upsert(connection, e, cycle=None):
    store = TransactionTestStore(connection)
    ev = BrainMigration(store)._event(e, 'history-v%d' % e['version'], store.next_sequence('synthetic', e['id']))
    ev.update(cycle=cycle, source_event_id='capture:%s:%d' % (e['id'], e['version']))
    store.ingest(ev)
    return ev['id']


def lifecycle(connection):
    """v1 capturada numa thread, v2 com título novo e depois o tombstone."""
    first = entity('prod-example')
    second = copy.deepcopy(first)
    second.update(version=2, title='Produto renomeado')
    second['source'] = dict(first['source'], source_version=2, source_hash=digest('v2'))
    events = [upsert(connection, first, CYCLE), upsert(connection, second)]
    tombstone(connection, 'prod-example')
    return first, second, events + ['event-tomb-prod-example']


def test_history_s4_versions_in_sequence_with_origin_and_tombstone(connection):
    first, second, events = lifecycle(connection)
    grant(connection, 'grant-factory', ['prod-example'], FIELDS, actions=['history'])
    result = BrainStore(connection, PERSON).history('synthetic', 'prod-example')
    assert (result['state'], result['id'], result['active'], result['count']) == ('ok', 'prod-example', False, 3)
    versions = result['versions']
    assert [v['sequence'] for v in versions] == [1, 2, 3]
    assert [v['event_id'] for v in versions] == events
    assert [v['operation'] for v in versions] == ['upsert', 'upsert', 'tombstone']
    assert [v['version'] for v in versions] == [1, 2, 3]
    assert [v['source']['source_version'] for v in versions] == [1, 2, 1]
    assert versions[0]['source'] == first['source'] and versions[1]['source'] == second['source']
    assert {v['producer_id'] for v in versions} == {'ork'}
    assert [(v['thread_id'], v['phase']) for v in versions] == [('thread-example', 'GO'), (None, None), (None, None)]
    assert [v['rolled_back'] for v in versions] == [False, False, False]
    assert all(re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', v['recorded_at']) for v in versions)
    assert versions[0]['entity'] == first and versions[1]['entity']['title'] == 'Produto renomeado' and versions[2]['entity'] is None
    assert set(versions[0]) == {'sequence', 'event_id', 'operation', 'version', 'source', 'producer_id', 'thread_id', 'phase',
                                'recorded_at', 'rolled_back', 'entity'}


def test_history_s4_migration_rollback_shows_compensation_and_marks_reverted(connection):
    catalog = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/company-brain-v1.json').read_text())['catalog'][:1]
    store = TransactionTestStore(connection)
    migration = BrainMigration(store)
    original = migration.plan(catalog, 'original', 'a' * 64, '2026-09-13T20:00:00Z')
    migration.apply(original, digest(original), 'a' * 64)
    updated = copy.deepcopy(catalog)
    updated[0].update(version=2, title='Updated')
    plan = migration.plan(updated, 'update', 'b' * 64, '2026-09-13T20:00:00Z')
    migration.apply(plan, digest(plan), 'b' * 64)
    assert migration.rollback('synthetic', 'update')['state'] == 'rolled-back'
    grant(connection, 'grant-factory', [catalog[0]['id']], FIELDS, actions=['history'])
    result = BrainStore(connection, PERSON).history('synthetic', catalog[0]['id'])
    versions = result['versions']
    assert (result['active'], result['count']) == (True, 3)
    assert [(v['sequence'], v['operation'], v['version'], v['rolled_back']) for v in versions] == [
        (1, 'upsert', 1, False), (2, 'upsert', 2, True), (3, 'upsert', 1, False)]
    assert versions[2]['entity'] == catalog[0] and versions[1]['entity']['title'] == 'Updated'


def test_history_s4_needs_its_own_action_and_hides_existence_without_it(connection):
    lifecycle(connection)
    store = BrainStore(connection, PERSON)
    assert store.history('synthetic', 'prod-example') == store.history('synthetic', 'prod-absent') == dict(state='unknown')
    grant(connection, 'grant-factory', ['prod-example'], FIELDS, actions=['get', 'query'])
    assert store.history('synthetic', 'prod-example') == dict(state='unknown')
    connection.execute("UPDATE brain_grants SET actions='[\"history\"]'::jsonb")
    assert store.history('synthetic', 'prod-example')['state'] == 'ok'
    for principal in (Principal('person', 'synthetic', 'service', True), Principal('person', 'synthetic', 'human', False),
                      Principal('person', 'other', 'human', True), None):
        assert BrainStore(connection, principal).history('synthetic', 'prod-example') == dict(state='forbidden')
    connection.execute('UPDATE brain_grants SET revoked=true')
    assert store.history('synthetic', 'prod-example') == dict(state='unknown')


def test_history_s4_withheld_and_uncitable_readings_are_refused(connection):
    upsert(connection, entity('prod-example'))
    grant(connection, 'grant-factory', ['prod-example'], [f for f in FIELDS if f != 'source'], actions=['history'])
    store = BrainStore(connection, PERSON)
    assert store.history('synthetic', 'prod-example') == dict(state='forbidden', error='brain.history.fields-forbidden')
    connection.execute("UPDATE brain_projection SET state='withheld'")
    assert store.history('synthetic', 'prod-example') == dict(state='withheld')


def test_history_s4_projection_page_count_and_read_only_snapshot(connection):
    lifecycle(connection)
    grant(connection, 'grant-factory', ['prod-example'], ['id', 'title', 'source'], actions=['history'])
    counts = lambda: {t: connection.execute(f'SELECT count(*) AS n FROM {t}').fetchone()['n'] for t in TABLES}
    before, seen = counts(), {}

    def probe(stage):
        if stage == 'history-snapshot':
            seen['read_only'] = connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']
            seen['isolation'] = connection.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']
    store = BrainStore(connection, PERSON, fault=probe)
    page = store.history('synthetic', 'prod-example', limit=1, offset=1)
    assert seen == dict(read_only='on', isolation='repeatable read') and counts() == before
    assert page['count'] == 3 and [v['sequence'] for v in page['versions']] == [2]
    assert set(page['versions'][0]['entity']) == {'id', 'title', 'source'}
    # A versão é campo do corpo: sem 'version' na concessão, ela não sai nem nos metadados.
    assert page['versions'][0]['version'] is None and page['versions'][0]['event_id'] and page['versions'][0]['recorded_at']
    assert store.history('synthetic', 'prod-example', limit=10, offset=3)['versions'] == []
    envelope = dict(schema=API, operation='history', payload=dict(tenant_id='synthetic', id='prod-example', limit=2))
    result = request(envelope, store)
    assert result['schema'] == API and result['state'] == 'ok' and len(result['versions']) == 2
