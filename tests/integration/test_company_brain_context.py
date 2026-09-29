"""B4.2: modo context da seleção, montado no servidor, com banco isolado."""
import copy
import json
import sys
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from orkmind.cli.company_brain import API, request
from orkmind.core.company_brain import digest
from orkmind.core.company_brain_access import Principal
from orkmind.core.company_brain_migration import BrainMigration
from orkmind.store.company_brain import CONTEXT_FIELDS, CONTEXT_GAPS, BrainStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_company_brain_store import connection, EVENT, TransactionTestStore

# Exige um Postgres isolado (ORKMIND_TEST_DATABASE_URL) e falha sem ele, de proposito.
pytestmark = pytest.mark.integration

GOLDEN = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/company-brain-context-v1.json').read_text())
PERSON = Principal('person', 'synthetic', 'human', True)
FIELDS = sorted(EVENT['payload'])
TABLES = ('brain_inbox', 'brain_projection', 'brain_history', 'brain_receipts', 'brain_outbox', 'brain_grants',
          'brain_migration_batches', 'brain_rolled_back_events', 'brain_transport_principals')


def entity(id, parent=None, acl='grant-factory', **extra):
    e = copy.deepcopy(EVENT['payload'])
    source = dict(e['source'], source_ref='portfolio.json#' + id, location='id:' + id, source_hash=digest(id))
    e.update(id=id, kind=id.split('-', 1)[0], parent_id=parent, aliases=[dict(system='ork', instance='factory-synthetic', id=id)],
             source=source, acl_ref=acl, **extra)
    return e


def seed(connection, *entities, tenant='synthetic'):
    store = TransactionTestStore(connection)
    migration = BrainMigration(store)
    for e in entities:
        store.ingest(migration._event(e, 'context', store.next_sequence(tenant, e['id'])))
    return [e['id'] for e in entities]


def grant(connection, acl, resources, fields, actions=('get',), principal='person', kind='human', tenant='synthetic'):
    connection.execute('INSERT INTO brain_grants(tenant_id,principal_id,acl_ref,kind,actions,resources,source_instances,fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
        (tenant, principal, acl, kind, Jsonb(list(actions)), Jsonb(list(resources)), Jsonb(['factory-synthetic', 'orkastery']), Jsonb(list(fields))))


def context(ids, **over):
    q = dict(schema='orkmind.company-brain-selection/v1', tenant_id='synthetic', mode='context', limit=max(len(ids), 1), offset=0,
             facets=dict(ids=list(ids), kinds=[], workspace_ids=[], source_instances=[]))
    q.update(over)
    return q


def tombstone(connection, id):
    store = TransactionTestStore(connection)
    ev = dict(schema='orkmind.company-brain-event/v1', tenant_id='synthetic', id='event-tomb-' + id, producer_id='ork', aggregate_id=id,
              sequence=store.next_sequence('synthetic', id), source_event_id='tomb:' + id, source=entity(id)['source'], operation='tombstone',
              payload=None, payload_hash=digest(None), cycle=None, evidence_refs=[], occurred_at=None, observed_at=None, acl_ref='grant-factory')
    store.ingest(ev)


def family(connection):
    return seed(connection, entity('prod-example'), entity('prod-other'), entity('proj-alpha', 'prod-example'),
                entity('proj-beta', 'prod-other'), entity('init-alpha-one', 'proj-alpha'), entity('init-alpha-two', 'proj-alpha'),
                entity('init-beta-one', 'proj-beta'))


def test_context_s1_requested_plus_visible_parents_in_stable_order(connection):
    grant(connection, 'grant-factory', family(connection), FIELDS)
    store = BrainStore(connection, PERSON)
    result = store.query(context(['init-beta-one', 'init-alpha-one', 'init-alpha-one']))
    pack = result['context']
    assert result['state'] == 'ok' and pack['schema'] == 'orkmind.company-brain-context/v1' and pack['tenant_id'] == 'synthetic'
    assert pack['requested'] == ['init-alpha-one', 'init-beta-one']
    assert [i['id'] for i in pack['items']] == ['prod-example', 'prod-other', 'proj-alpha', 'proj-beta', 'init-alpha-one', 'init-beta-one']
    # O pacote leva só os campos citáveis: descrição, critérios e aliases ficam no get.
    for item in pack['items']:
        assert item['state'] == 'ok' and set(item['entity']) == set(CONTEXT_FIELDS)
    assert 'init-alpha-two' not in json.dumps(pack)


def test_context_s1_digest_is_canonical_and_reproducible(connection):
    grant(connection, 'grant-factory', family(connection), FIELDS)
    store = BrainStore(connection, PERSON)
    first, second = (store.query(context(['init-alpha-two']))['context'] for _ in range(2))
    body = {k: v for k, v in first.items() if k != 'digest'}
    assert first == second and first['digest'] == digest(body)
    connection.execute("UPDATE brain_projection SET body=jsonb_set(body,'{title}','\"outro\"') WHERE id='proj-alpha'")
    assert store.query(context(['init-alpha-two']))['context']['digest'] != first['digest']


def test_context_s2_citation_withheld_unknown_and_item_gaps(connection):
    ids = seed(connection, entity('prod-example', observed_at=None), entity('proj-alpha', 'prod-example'),
               entity('init-alpha-one', 'proj-alpha', recorded_at='2026-09-29T12:00:01Z'),
               entity('init-alpha-two', 'proj-alpha', acl='acl-restricted'), entity('init-alpha-secret', 'proj-alpha'),
               entity('init-alpha-gone', 'proj-alpha'), entity('init-alpha-hidden', 'proj-alpha'))
    tombstone(connection, 'init-alpha-gone')
    connection.execute("UPDATE brain_projection SET state='withheld' WHERE id='init-alpha-secret'")
    grant(connection, 'grant-factory', [i for i in ids if i != 'init-alpha-hidden'], FIELDS)
    grant(connection, 'acl-restricted', ['init-alpha-two'], [f for f in FIELDS if f != 'source'])
    pack = BrainStore(connection, PERSON).query(context(['init-alpha-one', 'init-alpha-two', 'init-alpha-secret', 'init-alpha-gone',
                                                         'init-alpha-hidden', 'init-alpha-absent']))['context']
    items = {i['id']: i for i in pack['items']}
    assert list(items) == ['prod-example', 'proj-alpha', 'init-alpha-one', 'init-alpha-secret']
    assert items['init-alpha-secret'] == dict(id='init-alpha-secret', state='withheld')
    for item in (i for i in pack['items'] if i['state'] == 'ok'):
        assert set(item['entity']['source']) == {'authority', 'instance', 'source_ref', 'source_hash', 'source_version', 'location'}
    gaps = {}
    for g in pack['gaps']:
        assert g['code'] in CONTEXT_GAPS
        gaps.setdefault(g['id'], []).append(g['code'])
    # Sem citação inteira não há conteúdo, só a lacuna; ausente, apagado e sem concessão são iguais.
    assert gaps['init-alpha-two'] == ['citation.incomplete'] and 'init-alpha-two' not in items
    assert gaps['init-alpha-secret'] == ['entity.withheld']
    assert gaps['init-alpha-gone'] == gaps['init-alpha-hidden'] == gaps['init-alpha-absent'] == ['entity.unknown']
    assert gaps['init-alpha-one'] == ['owner.unresolved']
    assert gaps['prod-example'] == ['observed.unknown', 'owner.unresolved', 'recorded.unknown']
    assert gaps['proj-alpha'] == ['owner.unresolved', 'recorded.unknown']


def test_context_s2_uncited_and_withheld_items_pull_no_parents(connection):
    ids = seed(connection, entity('prod-example'), entity('proj-alpha', 'prod-example'), entity('init-alpha-two', 'proj-alpha', acl='acl-restricted'),
               entity('prod-other'), entity('proj-beta', 'prod-other'), entity('init-beta-secret', 'proj-beta'))
    connection.execute("UPDATE brain_projection SET state='withheld' WHERE id='init-beta-secret'")
    grant(connection, 'grant-factory', ids, FIELDS)
    grant(connection, 'acl-restricted', ['init-alpha-two'], [f for f in FIELDS if f != 'source'])
    pack = BrainStore(connection, PERSON).query(context(['init-alpha-two', 'init-beta-secret']))['context']
    assert pack['items'] == [dict(id='init-beta-secret', state='withheld')]
    assert pack['gaps'] == [dict(id='init-alpha-two', code='citation.incomplete'), dict(id='init-beta-secret', code='entity.withheld')]



def test_context_s2_citation_needs_the_same_shape_the_factory_core_checks(connection):
    # O contrato já barra isso na ingestão; um corpo corrompido no banco também não vira conteúdo.
    ids = seed(connection, entity('prod-example'), entity('prod-other'), entity('prod-third'))
    grant(connection, 'grant-factory', ids, FIELDS)
    connection.execute("""UPDATE brain_projection SET body=jsonb_set(body,'{source,source_hash}','"not-a-sha256"') WHERE id='prod-other'""")
    connection.execute("""UPDATE brain_projection SET body=jsonb_set(body,'{source,source_version}','"1"') WHERE id='prod-third'""")
    pack = BrainStore(connection, PERSON).query(context(ids))['context']
    assert [i['id'] for i in pack['items']] == ['prod-example']
    assert [g for g in pack['gaps'] if g['code'] == 'citation.incomplete'] == [
        dict(id='prod-other', code='citation.incomplete'), dict(id='prod-third', code='citation.incomplete')]


def test_context_s3_read_only_snapshot_leaves_every_table_intact(connection):
    grant(connection, 'grant-factory', family(connection), FIELDS)
    counts = lambda: {t: connection.execute(f'SELECT count(*) AS n FROM {t}').fetchone()['n'] for t in TABLES}
    before, seen = counts(), {}

    def probe(stage):
        if stage == 'context-snapshot':
            seen['read_only'] = connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']
            seen['isolation'] = connection.execute('SHOW transaction_isolation').fetchone()['transaction_isolation']
    BrainStore(connection, PERSON, fault=probe).query(context(['init-alpha-one', 'init-beta-one']))
    assert seen == dict(read_only='on', isolation='repeatable read')
    assert counts() == before
    assert connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'off'


def test_context_s3_refuses_principal_tenant_and_invalid_selection(connection):
    grant(connection, 'grant-factory', family(connection), FIELDS)
    store = BrainStore(connection, PERSON)
    for principal in (Principal('person', 'synthetic', 'service', True), Principal('person', 'synthetic', 'human', False),
                      Principal('person', 'other', 'human', True), None):
        assert BrainStore(connection, principal).query(context(['init-alpha-one']))['state'] == 'forbidden'
    assert store.query(context(['init-alpha-one'], tenant_id='other'))['state'] == 'forbidden'
    base = context(['init-alpha-one'])
    for bad in (context([]), context(['init-alpha-one'], offset=1), context(['init-alpha-one', 'init-alpha-two'], limit=1),
                context(['event-one']), dict(base, facets=dict(base['facets'], kinds=['init'])),
                dict(base, facets=dict(base['facets'], workspace_ids=['workspace-a'])),
                dict(base, facets=dict(base['facets'], source_instances=['factory-synthetic']))):
        with pytest.raises(ValueError, match='brain.context.invalid'):
            store.query(bad)
        assert request(dict(schema=API, operation='query', payload=bad), store) == dict(schema=API, state='conflict', error='brain.context.invalid')


def test_context_s3_revocation_and_hidden_parent_link(connection):
    ids = family(connection)
    grant(connection, 'grant-factory', ids, [f for f in FIELDS if f != 'parent_id'])
    store = BrainStore(connection, PERSON)
    # Sem o campo parent_id na concessão, o vínculo não aparece e não puxa pai.
    assert [i['id'] for i in store.query(context(['init-alpha-one']))['context']['items']] == ['init-alpha-one']
    connection.execute('UPDATE brain_grants SET revoked=true')
    result = store.query(context(['init-alpha-one']))
    assert result['state'] == 'empty' and result['context']['items'] == []
    assert result['context']['gaps'] == [dict(id='init-alpha-one', code='entity.unknown')]


def test_context_golden_response_matches_the_shared_fixture(connection):
    seeded = GOLDEN['seed']
    seed(connection, *seeded['entities'], tenant=seeded['principal']['tenant_id'])
    for g in seeded['grants']:
        grant(connection, g['acl_ref'], g['resources'], g['fields'], actions=g['actions'], principal=seeded['principal']['id'],
              tenant=seeded['principal']['tenant_id'])
    for id in seeded['withheld']:
        connection.execute("UPDATE brain_projection SET state='withheld' WHERE id=%s", (id,))
    principal = Principal(seeded['principal']['id'], seeded['principal']['tenant_id'], 'human', True)
    result = request(dict(schema=API, operation='query', payload=GOLDEN['request']), BrainStore(connection, principal))
    assert result == GOLDEN['response']
