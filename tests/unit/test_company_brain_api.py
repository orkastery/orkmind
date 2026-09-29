from unittest.mock import patch

from orkmind.cli.company_brain import API, request, service_request


def test_major_and_authorship_rejected_before_io():
    with patch('orkmind.cli.company_brain.psycopg.connect',side_effect=AssertionError('unexpected I/O')):
        for bad in [None,dict(schema='v2',operation='get'),dict(schema=API,operation='get',principal='human'),dict(schema=API,operation='shell')]:
            assert service_request(bad)['state']=='conflict'
        assert request(dict(schema=API,operation='capabilities'))['state']=='ok'
        with patch.dict('os.environ',{},clear=True):
            assert service_request(dict(schema=API,operation='ingest',payload={}))['error']=='brain.configuration.missing'



def test_store_error_never_echoes_connection_or_content():
    class Broken:
        def get(self,*args): raise RuntimeError('SENTINEL-secret')
    result=request(dict(schema=API,operation='get',payload=dict(tenant_id='synthetic',id='prod-example')),Broken())
    assert result['state']=='unavailable'
    assert 'SENTINEL' not in str(result)


def test_head_is_a_bounded_authenticated_store_operation():
    class Store:
        def head(self,tenant,id):
            assert (tenant,id)==('synthetic','prod-example')
            return dict(state='ok',active=True,sequence=3,source_version=2,source_hash='a'*64)
    result=request(dict(schema=API,operation='head',payload=dict(tenant_id='synthetic',id='prod-example')),Store())
    assert result==dict(schema=API,state='ok',active=True,sequence=3,source_version=2,source_hash='a'*64)
    assert request(dict(schema=API,operation='head',payload=dict(tenant_id='synthetic')),Store())['state']=='conflict'


def test_cli_plan_envelope_calls_real_migration_with_batch_id():
    import json
    from pathlib import Path
    from click.testing import CliRunner
    from orkmind.cli.company_brain import brain
    from contextlib import nullcontext
    catalog=json.loads((Path(__file__).resolve().parents[1]/'fixtures/company-brain-v1.json').read_text())['catalog']
    class Row:
        def __init__(self,value): self.value=value
        def fetchone(self): return self.value
    class Connection:
        autocommit=True
        def execute(self,sql,args):
            if 'brain_transport_principals' in sql: return Row(dict(principal_id='writer',kind='service',revoked=False))
            if 'brain_grants' in sql:
                return Row(dict(tenant_id='synthetic',principal_id='writer',kind='service',revoked=False,
                    actions=['migrate'],resources=[e['id'] for e in catalog],source_instances=['factory-synthetic'],fields=[]))
            assert 'SELECT * FROM brain_projection' in sql
            return Row(None)
    envelope=dict(schema='orkmind.company-brain-admin/v1',operation='plan',payload=dict(
        entities=catalog,batch_id='cli-batch',source_hash='a'*64,observed_at='2026-09-13T20:00:00Z'))
    with patch.dict('os.environ',dict(ORKMIND_DATABASE_URL='test-placeholder',ORKMIND_BRAIN_TENANT='synthetic')):
        with patch('orkmind.cli.company_brain.psycopg.connect',return_value=nullcontext(Connection())):
            result=CliRunner().invoke(brain,['migration'],input=json.dumps(envelope))
    assert result.exit_code==0,result.output
    result=json.loads(result.output)
    assert result['result']['batch_id']=='cli-batch'
    assert len(result['result']['operations'])==3


def test_transport_principal_is_database_authenticated_not_an_argument():
    from orkmind.cli.company_brain import transport_principal
    class Row:
        def fetchone(self): return dict(principal_id='person',kind='human',revoked=False)
    class Connection:
        def execute(self,sql,args):
            assert 'database_role=session_user' in sql
            assert args==('synthetic',)
            return Row()
    principal=transport_principal(Connection(),'synthetic')
    assert (principal.id,principal.kind,principal.authenticated)==('person','human',True)


def test_api_s5_capabilities_declare_history_and_context_mode():
    result=request(dict(schema=API,operation='capabilities'))
    assert 'history' in result['operations'] and result['human_context_required']==['get','query','history']
    assert result['selection_modes']==['selection','context']


def test_api_s5_history_payload_is_closed_and_bounded():
    calls=[]
    class Store:
        def history(self,tenant,id,limit,offset):
            calls.append((tenant,id,limit,offset))
            return dict(state='ok',id=id,active=True,count=0,versions=[])
    ok=dict(tenant_id='synthetic',id='prod-example')
    assert request(dict(schema=API,operation='history',payload=ok),Store())['state']=='ok'
    assert request(dict(schema=API,operation='history',payload=dict(ok,limit=1000,offset=100000)),Store())['state']=='ok'
    assert calls==[('synthetic','prod-example',100,0),('synthetic','prod-example',1000,100000)]
    for bad in [None,dict(tenant_id='synthetic'),dict(ok,principal='owner'),dict(ok,limit=0),dict(ok,limit=1001),dict(ok,limit=True),
                dict(ok,offset=-1),dict(ok,offset=100001),dict(ok,limit='10'),dict(ok,id=''),dict(ok,id='x'*161),dict(ok,tenant_id=7)]:
        assert request(dict(schema=API,operation='history',payload=bad),Store())==dict(schema=API,state='conflict',error='brain.api.invalid')
    assert len(calls)==2


def test_api_s5_history_and_context_errors_are_codes_only():
    class Broken:
        def history(self,*args): raise RuntimeError('SENTINEL-secret')
        def query(self,selection): raise ValueError('SENTINEL-secret' if selection['limit']==2 else 'brain.context.invalid')
    history=request(dict(schema=API,operation='history',payload=dict(tenant_id='synthetic',id='prod-example')),Broken())
    assert history==dict(schema=API,state='unavailable',error='brain.store.unavailable')
    selection=dict(schema='orkmind.company-brain-selection/v1',tenant_id='synthetic',mode='context',limit=1,offset=0,
                   facets=dict(ids=['prod-example'],kinds=[],workspace_ids=[],source_instances=[]))
    assert request(dict(schema=API,operation='query',payload=selection),Broken())==dict(schema=API,state='conflict',error='brain.context.invalid')
    leaked=request(dict(schema=API,operation='query',payload=dict(selection,limit=2)),Broken())
    assert leaked==dict(schema=API,state='conflict',error='brain.api.invalid') and 'SENTINEL' not in str(leaked)
