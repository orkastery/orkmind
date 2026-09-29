"""Versioned JSON API. No caller-selected DSN, root, shell, or human identity."""
import hashlib
import json
import os
from pathlib import Path

import click
import psycopg

from orkmind.core.company_brain import validate
from orkmind.core.company_brain_access import Principal
from orkmind.store.company_brain import BrainStore
from psycopg.rows import dict_row

API='orkmind.company-brain-api/v1'
OPERATIONS={'capabilities','ingest','get','query','receipts','head','history'}


def request(value,store=None):
    if not isinstance(value,dict) or set(value)-{'schema','operation','payload'} or value.get('schema')!=API or value.get('operation') not in OPERATIONS:
        return dict(schema=API,state='conflict',error='brain.api.invalid')
    operation=value['operation'];payload=value.get('payload')
    if operation=='capabilities':
        contract=Path(__file__).resolve().parents[1]/'contracts/company-brain.v1.json'
        return dict(schema=API,state='ok',contract_hash=hashlib.sha256(contract.read_bytes()).hexdigest(),operations=sorted(OPERATIONS),
            authority='ork',capture='deterministic',principal_source='authenticated-transport',human_context_required=['get','query','history'],
            selection_modes=['selection','context'])
    if store is None: return dict(schema=API,state='unavailable',error='brain.transport.unavailable')
    try:
        if operation in ('get','receipts','head'):
            key='event_id' if operation=='receipts' else 'id'
            if not isinstance(payload,dict) or set(payload)!={'tenant_id',key} or any(not isinstance(v,str) or not v or len(v)>160 for v in payload.values()):
                raise ValueError('brain.api.invalid')
            result=(store.get(payload['tenant_id'],payload[key]) if operation=='get' else
                    store.receipts(payload['tenant_id'],payload[key]) if operation=='receipts' else
                    store.head(payload['tenant_id'],payload[key]))
        elif operation=='history':
            # Closed payload: tenant_id and id, plus an optional bounded page.
            if (not isinstance(payload,dict) or not {'tenant_id','id'}<=set(payload) or set(payload)-{'tenant_id','id','limit','offset'}
                or any(not isinstance(payload[k],str) or not payload[k] or len(payload[k])>160 for k in ('tenant_id','id'))):
                raise ValueError('brain.api.invalid')
            limit,offset=payload.get('limit',100),payload.get('offset',0)
            if type(limit) is not int or type(offset) is not int or not 1<=limit<=1000 or not 0<=offset<=100000:
                raise ValueError('brain.api.invalid')
            result=store.history(payload['tenant_id'],payload['id'],limit,offset)
        elif operation=='query': result=store.query(validate(payload))
        else:
            event=validate(payload)
            receipt=store.ingest(event)
            indexed=store.index(event)
            result=dict(state='ok',receipt=receipt,indexed=indexed)
        return dict(schema=API,**result)
    except PermissionError: return dict(schema=API,state='forbidden',error='brain.access.forbidden')
    except ValueError as error:
        code=str(error)
        allowed={'brain.api.invalid','brain.contract.invalid','brain.event.conflict','brain.event.sequence-gap','brain.authority.conflict','brain.version.conflict','brain.catalog.parent-missing','brain.catalog.dependency-invalid','brain.receipt.missing','brain.context.invalid'}
        return dict(schema=API,state='conflict',error=code if code in allowed else 'brain.api.invalid')
    except Exception:
        return dict(schema=API,state='unavailable',error='brain.store.unavailable')


def transport_principal(connection, tenant):
    # session_user is the authenticated DB login, unaffected by SET ROLE.
    # No request field/environment variable can name a human principal.
    connection.row_factory=dict_row
    row=connection.execute('SELECT principal_id,kind,revoked FROM brain_transport_principals WHERE tenant_id=%s AND database_role=session_user',
                           (tenant,)).fetchone()
    if row:
        if row['revoked']: raise PermissionError('brain.access.forbidden')
        return Principal(row['principal_id'],tenant,row['kind'],True)
    return Principal('postgres:'+connection.info.user,tenant,'service',True)


def service_request(value):
    # Validate major/operation before connection; resolve identity only after DB auth.
    if not isinstance(value,dict) or value.get('schema')!=API or value.get('operation')=='capabilities': return request(value)
    if set(value)-{'schema','operation','payload'} or value.get('operation') not in OPERATIONS: return request(value)
    dsn=os.environ.get('ORKMIND_DATABASE_URL','')
    tenant=os.environ.get('ORKMIND_BRAIN_TENANT','')
    if not dsn or not tenant: return dict(schema=API,state='unavailable',error='brain.configuration.missing')
    try:
        with psycopg.connect(dsn,autocommit=True,connect_timeout=3) as connection:
            principal=transport_principal(connection,tenant)
            return request(value,BrainStore(connection,principal))
    except PermissionError: return dict(schema=API,state='forbidden',error='brain.access.forbidden')
    except Exception: return dict(schema=API,state='unavailable',error='brain.transport.unavailable')


@click.group()
def brain():
    """Exact Company Brain contracts and scoped factory capture."""


@brain.command('request')
def brain_request():
    """Read one bounded API envelope from stdin; errors contain codes only."""
    try:
        text=click.get_text_stream('stdin').read(2_000_001)
        if len(text)>2_000_000: raise ValueError()
        value=json.loads(text)
        result=service_request(value)
    except Exception: result=dict(schema=API,state='conflict',error='brain.api.invalid')
    click.echo(json.dumps(result,ensure_ascii=False))
    if result['state'] not in ('ok','empty','unknown','withheld'): raise click.exceptions.Exit(1)


@brain.command('migration')
def brain_migration():
    """Administrative envelope, independent of interactive API and its permissions."""
    from orkmind.core.company_brain_migration import BrainMigration
    try:
        text=click.get_text_stream('stdin').read(2_000_001)
        if len(text)>2_000_000: raise ValueError()
        value=json.loads(text)
        if not isinstance(value,dict) or value.get('schema')!='orkmind.company-brain-admin/v1': raise ValueError()
        operation=value.get('operation')
        keys={'plan':{'entities','batch_id','source_hash','observed_at'},'apply':{'plan','expected_hash','current_source_hash'},'rollback':{'batch_id'}}
        if operation not in keys or set(value)!={'schema','operation','payload'} or set(value['payload'])!=keys[operation]: raise ValueError()
        dsn=os.environ.get('ORKMIND_DATABASE_URL','');tenant=os.environ.get('ORKMIND_BRAIN_TENANT','')
        if not dsn or not tenant: raise ValueError()
        with psycopg.connect(dsn,autocommit=True,connect_timeout=3) as connection:
            principal=transport_principal(connection,tenant)
            service=BrainMigration(BrainStore(connection,principal))
            payload=value['payload']
            result=service.plan(**payload) if operation=='plan' else service.apply(**payload) if operation=='apply' else service.rollback(tenant,payload['batch_id'])
        click.echo(json.dumps(dict(state='ok',result=result),ensure_ascii=False))
    except Exception:
        click.echo(json.dumps(dict(state='unavailable',error='brain.migration.unconfirmed')))
        raise click.exceptions.Exit(1)


if __name__=='__main__': brain()
