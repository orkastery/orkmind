import sys
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from orkmind.core.company_brain_access import Principal
from orkmind.store.company_brain import BrainStore

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_company_brain_store import connection, EVENT, TransactionTestStore

# Exige um Postgres isolado (ORKMIND_TEST_DATABASE_URL) e falha sem ele, de proposito.
# A marca so permite que um CI sem banco deixe o modulo de fora com -m "not integration".
pytestmark = pytest.mark.integration


def test_real_queries_hide_private_existence_and_immediate_revocation(connection):
    TransactionTestStore(connection).ingest(EVENT)
    person=Principal('person','synthetic','human',True)
    store=BrainStore(connection,person)
    assert store.get('synthetic','prod-example')==store.get('synthetic','absent')==dict(state='unknown')
    connection.execute('INSERT INTO brain_grants(tenant_id,principal_id,acl_ref,kind,actions,resources,source_instances,fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
        ('synthetic','person','grant-factory','human',Jsonb(['get','query']),Jsonb(['prod-example']),Jsonb(['factory-synthetic']),Jsonb(['id','title'])))
    assert store.get('synthetic','prod-example')['entity']==dict(id='prod-example',title=EVENT['payload']['title'])
    assert store.get('other','prod-example')==dict(state='forbidden')
    connection.execute("UPDATE brain_grants SET revoked=true")
    assert store.get('synthetic','prod-example')==dict(state='unknown')


def selection(**facets):
    return dict(schema='orkmind.company-brain-selection/v1',tenant_id='synthetic',mode='selection',limit=20,offset=0,
                facets=dict(dict(ids=[],kinds=[],workspace_ids=[],source_instances=[]),**facets))


def grant(connection,resources,fields,principal='person',kind='human',actions=None,sources=None):
    connection.execute('INSERT INTO brain_grants(tenant_id,principal_id,acl_ref,kind,actions,resources,source_instances,fields) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
        ('synthetic',principal,'grant-factory',kind,Jsonb(actions or ['get','query']),Jsonb(resources),Jsonb(sources or ['factory-synthetic']),Jsonb(fields)))


def populate(connection):
    import copy
    from orkmind.core.company_brain_migration import BrainMigration
    store=TransactionTestStore(connection);migration=BrainMigration(store)
    entities=[]
    for id,kind,parent,workspaces in [('prod-example','prod',None,[]),('prod-other','prod',None,[]),
                                      ('proj-alpha','proj','prod-example',['workspace-a']),('proj-beta','proj','prod-other',['workspace-b'])]:
        e=copy.deepcopy(EVENT['payload']);e.update(id=id,kind=kind,parent_id=parent,workspace_ids=workspaces)
        store.ingest(migration._event(e,'populate',1));entities.append(e)
    return entities


def test_query_facets_or_within_and_across_pagination_and_tenants(connection):
    entities=populate(connection)
    grant(connection,[e['id'] for e in entities],['id','title','kind','workspace_ids','source'])
    store=BrainStore(connection,Principal('person','synthetic','human',True))
    q=selection(kinds=['prod','proj'],workspace_ids=['workspace-a','workspace-b'])
    result=store.query(q);assert result['count']==2
    assert [v['entity']['id'] for v in result['items']]==['proj-alpha','proj-beta']
    q['facets']['ids']=['prod-example','proj-beta'];assert store.query(q)['count']==1
    q=selection();q.update(limit=2,offset=2)
    page=store.query(q);assert page['count']==4
    assert [v['entity']['id'] for v in page['items']]==['proj-alpha','proj-beta']
    q['offset']=20;assert store.query(q)==dict(state='empty',items=[],count=4)
    q=selection(source_instances=['not-granted-source']);assert store.query(q)['count']==0
    q['tenant_id']='other';assert store.query(q)['state']=='forbidden'
    assert BrainStore(connection,Principal('person','synthetic','service',True)).query(selection())['state']=='forbidden'
    assert BrainStore(connection,Principal('person','synthetic','human',False)).query(selection())['state']=='forbidden'
    q=selection();q['mode']='context';assert store.query(q)['error']=='brain.selection.context-unsupported'
    connection.execute('UPDATE brain_grants SET revoked=true')
    assert store.query(selection())['items']==[]


def test_query_hidden_predicates_never_affect_presence_or_counts(connection):
    entities=populate(connection);grant(connection,[e['id'] for e in entities],['title'])
    store=BrainStore(connection,Principal('person','synthetic','human',True))
    for facet,values in [('ids',['prod-example','absent']),('kinds',['prod','assertion']),
                         ('workspace_ids',['workspace-a','absent']),('source_instances',['factory-synthetic','absent'])]:
        results=[store.query(selection(**{facet:[value]})) for value in values]
        assert results[0]==results[1]==dict(state='forbidden',items=[],error='brain.selection.fields-forbidden')


def test_query_withheld_and_empty_are_explicit_and_expiry_is_immediate(connection):
    TransactionTestStore(connection).ingest(EVENT);grant(connection,['prod-example'],['id','title'])
    store=BrainStore(connection,Principal('person','synthetic','human',True))
    connection.execute("UPDATE brain_projection SET state='withheld'")
    assert store.query(selection())==dict(state='ok',items=[dict(state='withheld')],count=1)
    assert store.query(selection(ids=['prod-example']))['state']=='forbidden'
    connection.execute("UPDATE brain_grants SET expires_at=clock_timestamp()-interval '1 second'")
    assert store.query(selection())['items']==[]


def test_existing_origin_and_acl_cannot_be_reassigned(connection):
    import copy
    import pytest
    from orkmind.core.company_brain import digest
    TransactionTestStore(connection).ingest(EVENT)
    grant(connection,['prod-example'],[],principal='writer',kind='service',actions=['ingest'],sources=['factory-synthetic','other'])
    store=BrainStore(connection,Principal('writer','synthetic','service',True))
    for change in ('source','acl'):
        e=copy.deepcopy(EVENT);e.update(id='new-'+change,source_event_id='new-'+change,sequence=2);e['payload']['version']=2
        if change=='source': e['payload']['source']['instance']='other';e['source']=copy.deepcopy(e['payload']['source'])
        else:
            e['acl_ref']=e['payload']['acl_ref']='other-acl'
            connection.execute("INSERT INTO brain_grants SELECT tenant_id,principal_id,'other-acl',kind,actions,resources,source_instances,fields,expires_at,revoked FROM brain_grants WHERE principal_id='writer'")
        e['payload_hash']=digest(e['payload'])
        with pytest.raises(ValueError,match='brain.authority.conflict'):store.ingest(e)
    assert connection.execute('SELECT count(*) AS n FROM brain_inbox').fetchone()['n']==1


def test_producer_head_is_minimal_and_reauthorizes_every_read(connection):
    TransactionTestStore(connection).ingest(EVENT)
    grant(connection,['prod-example'],[],principal='writer',kind='service',actions=['ingest'])
    store=BrainStore(connection,Principal('writer','synthetic','service',True))
    assert store.head('synthetic','absent')==dict(state='unknown')
    assert store.head('synthetic','prod-example')==dict(
        state='ok',active=True,sequence=1,source_version=1,
        source_hash=EVENT['payload']['source']['source_hash'])
    connection.execute("UPDATE brain_grants SET revoked=true WHERE principal_id='writer'")
    assert store.head('synthetic','prod-example')==dict(state='forbidden')
    assert BrainStore(connection,Principal('person','synthetic','human',True)).head(
        'synthetic','prod-example')==dict(state='forbidden')


def test_cli_and_mcp_share_authenticated_transport_and_revoke(connection,monkeypatch):
    import asyncio,json
    from contextlib import nullcontext
    from click.testing import CliRunner
    from orkmind.cli.company_brain import brain,API
    from orkmind.mcp.tools import orkmind_brain
    TransactionTestStore(connection).ingest(EVENT);grant(connection,['prod-example'],['id','title'])
    connection.execute("INSERT INTO brain_transport_principals VALUES('synthetic',session_user,'person','human',false)")
    monkeypatch.setenv('ORKMIND_DATABASE_URL','fixture-connection');monkeypatch.setenv('ORKMIND_BRAIN_TENANT','synthetic')
    monkeypatch.setattr('orkmind.cli.company_brain.psycopg.connect',lambda *a,**k:nullcontext(connection))
    envelope=dict(schema=API,operation='get',payload=dict(tenant_id='synthetic',id='prod-example'))
    result=CliRunner().invoke(brain,['request'],input=json.dumps(envelope))
    assert result.exit_code==0,result.output
    assert json.loads(result.output)['entity']['id']=='prod-example'
    assert asyncio.run(orkmind_brain(dict(schema=API,operation='query',payload=selection())))['count']==1
    assert asyncio.run(orkmind_brain(dict(envelope,principal='owner')))['state']=='conflict'
    connection.execute('UPDATE brain_transport_principals SET revoked=true')
    assert asyncio.run(orkmind_brain(envelope))['state']=='forbidden'
