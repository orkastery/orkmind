"""D7: authenticated transport context and grants, independent of owner/LLM/host."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    kind: Literal['human','service']
    authenticated: bool


def allows(principal, grant, action, tenant, resource, source_instance, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(principal,Principal) or not principal.authenticated:
        return False
    if principal.tenant_id != tenant or grant.get('tenant_id') != tenant or grant.get('principal_id') != principal.id:
        return False
    if grant.get('kind') != principal.kind or grant.get('revoked') is not False:
        return False
    expires = grant.get('expires_at')
    if expires is not None:
        if isinstance(expires,str):
            try: expires=datetime.fromisoformat(expires.replace('Z','+00:00'))
            except ValueError: return False
        if not isinstance(expires,datetime) or expires.tzinfo is None or expires <= now:
            return False
    if action not in grant.get('actions',[]) or resource not in grant.get('resources',[]) or source_instance not in grant.get('source_instances',[]):
        return False
    if action in ('get','query','history') and principal.kind != 'human':
        return False
    if action in ('ingest','migrate','rollback') and principal.kind != 'service':
        return False
    return True


def project_fields(body, grants):
    """Derived content uses intersection, never union, of contributing grants."""
    if not grants:
        return {}
    fields = set(grants[0].get('fields',[]))
    for grant in grants[1:]: fields.intersection_update(grant.get('fields',[]))
    return {k:v for k,v in body.items() if k in fields}
