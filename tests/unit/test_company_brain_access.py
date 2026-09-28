from dataclasses import replace
from datetime import datetime, timezone, timedelta

from orkmind.core.company_brain_access import Principal, allows, project_fields


def test_principal_not_owner_or_transport_argument_and_revocation():
    principal=Principal('authenticated-human','synthetic','human',True)
    grant=dict(tenant_id='synthetic',principal_id=principal.id,kind='human',revoked=False,expires_at=None,
        actions=['get','query','ingest'],resources=['prod-example'],source_instances=['factory'],fields=['id','title'])
    check=lambda p,g: allows(p,g,'get','synthetic','prod-example','factory')
    assert check(principal,grant)
    for p in [None,dict(id=principal.id),replace(principal,authenticated=False),replace(principal,tenant_id='other'),replace(principal,id='Maestro'),replace(principal,kind='service')]: assert not check(p,grant)
    for changes in [dict(revoked=True),dict(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)),dict(resources=[]),dict(actions=[]),dict(source_instances=[]),dict(tenant_id='other'),dict(kind='service')]: assert not check(principal,{**grant,**changes})
    assert not allows(principal,grant,'ingest','synthetic','prod-example','factory')


def test_properties_and_derived_intersection_do_not_grant_private_fields():
    body=dict(id='prod-example',title='public-synthetic',private='SENTINEL',owner='Maestro')
    assert project_fields(body,[])=={}
    assert project_fields(body,[dict(fields=['id','title']),dict(fields=['id'])])==dict(id='prod-example')
    assert project_fields(body,[dict(fields=['id','title'])])==dict(id='prod-example',title='public-synthetic')
