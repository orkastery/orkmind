"""D8/D9: PostgreSQL exact projection, transactional inbox/history/receipts/outbox."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib import resources

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from orkmind.core.company_brain import digest, validate
from orkmind.core.company_brain_access import Principal, allows, project_fields

# B4.2/D2: the context package carries the citable state of an entity; description, criteria and aliases stay behind get.
CONTEXT_SCHEMA = 'orkmind.company-brain-context/v1'
CONTEXT_FIELDS = ('kind','version','title','status','parent_id','depends_on','owner','source','observed_at','recorded_at')
# D7: closed gap codes. The last three describe a cited item; the first three replace content.
CONTEXT_GAPS = ('entity.unknown','entity.withheld','citation.incomplete','owner.unresolved','observed.unknown','recorded.unknown')
CITATION = ('authority','instance','source_ref','source_hash','source_version','location')
KIND_ORDER = {'prod':0,'proj':1,'init':2}


def _kind(id):
    return KIND_ORDER.get(id.split('-',1)[0])


def _cited(source):
    """A citation counts only whole; a projected-away or partial source is never content.
    Same test as the factory core applies, so both assembly paths drop the same items."""
    if not isinstance(source,dict) or any(not isinstance(source.get(k),str) or not source.get(k) for k in CITATION if k!='source_version'):
        return False
    return re.fullmatch(r'[a-f0-9]{64}',source['source_hash']) is not None and type(source.get('source_version')) is int


class BrainStore:
    def __init__(self, connection, principal=None, fault=None):
        if not connection.autocommit:
            raise ValueError('brain.store.autocommit-required')
        self.connection = connection
        self.connection.row_factory = dict_row
        self.principal = principal
        self.fault = fault or (lambda _: None)

    def initialize(self):
        # Explicit administrative operation; opening a query never migrates.
        # The schema ships inside the package: an install from PyPI has no repository migrations/.
        sql = resources.files('orkmind.store').joinpath('company_brain_v1.sql').read_text('utf-8')
        with self.connection.transaction():
            self.connection.execute(sql)

    def _authorize(self, event, action):
        if not isinstance(self.principal,Principal):
            raise PermissionError('brain.access.forbidden')
        grant = self.connection.execute('SELECT * FROM brain_grants WHERE tenant_id=%s AND principal_id=%s AND acl_ref=%s',
            (event['tenant_id'],self.principal.id,event['acl_ref'])).fetchone()
        if not grant or not allows(self.principal,grant,action,event['tenant_id'],event['aggregate_id'],event['source']['instance']):
            raise PermissionError('brain.access.forbidden')
        return grant

    def _authorize_current(self, event, current, action):
        if not current:
            return
        prior=dict(tenant_id=current['tenant_id'],aggregate_id=current['id'],acl_ref=current['acl_ref'],
                   source=dict(instance=current['source_instance']))
        self._authorize(prior,action)
        if (current['producer_id'] != event['producer_id'] or
            current['source_instance'] != event['source']['instance'] or current['acl_ref'] != event['acl_ref']):
            raise ValueError('brain.authority.conflict')

    def next_sequence(self, tenant, aggregate):
        # Inbox is append-only, even when compensation restores an older source version.
        row=self.connection.execute('SELECT COALESCE(MAX(sequence),0)+1 AS next FROM brain_inbox WHERE tenant_id=%s AND aggregate_id=%s',
                                    (tenant,aggregate)).fetchone()
        return row['next']

    def query(self, raw):
        selection = validate(raw)
        if selection['schema'] != 'orkmind.company-brain-selection/v1':
            raise ValueError('brain.contract.invalid')
        if not isinstance(self.principal,Principal) or not self.principal.authenticated or self.principal.kind != 'human' or self.principal.tenant_id != selection['tenant_id']:
            return dict(state='forbidden',items=[])
        if selection['mode'] == 'context':
            return self.context(selection)
        facets=selection['facets']
        required={field for facet,field in [('ids','id'),('kinds','kind'),('workspace_ids','workspace_ids'),('source_instances','source')]
                  if facets[facet]}
        grants=self.connection.execute('SELECT * FROM brain_grants WHERE tenant_id=%s AND principal_id=%s',
                                       (selection['tenant_id'],self.principal.id)).fetchall()
        grants=[g for g in grants if any(allows(self.principal,g,'query',selection['tenant_id'],r,s)
                for r in g['resources'] for s in g['source_instances'])]
        if not grants: return dict(state='forbidden',items=[])
        # The refusal depends on grants, not matching row existence or hidden values.
        if any(not required.issubset(set(g['fields'])) for g in grants):
            return dict(state='forbidden',items=[],error='brain.selection.fields-forbidden')
        # ACL predicates precede pagination, fields, counts, and all output.
        rows = self.connection.execute("""SELECT p.*, row_to_json(g) AS grant FROM brain_projection p
          JOIN brain_grants g ON g.tenant_id=p.tenant_id AND g.acl_ref=p.acl_ref
          WHERE p.tenant_id=%s AND g.principal_id=%s AND g.kind='human' AND NOT g.revoked
          AND (g.expires_at IS NULL OR g.expires_at > clock_timestamp())
          AND g.actions @> %s AND g.resources ? p.id AND g.source_instances ? p.source_instance
          AND p.state <> 'deleted' ORDER BY p.id""", (selection['tenant_id'],self.principal.id,Jsonb(['query']))).fetchall()
        found=[]
        # Authorize every predicate before consulting its value, counting or paging.
        # Refuse the whole selection so an invisible value cannot influence output.
        if any(not required.issubset(set(row['grant'].get('fields',[]))) or
               (row['state']=='withheld' and required) for row in rows):
            return dict(state='forbidden',items=[],error='brain.selection.fields-forbidden')
        for row in rows:
            grant=row['grant']
            if not allows(self.principal,grant,'query',row['tenant_id'],row['id'],row['source_instance']): continue
            body=row['body'] or {}
            if facets['ids'] and row['id'] not in facets['ids']: continue
            if facets['kinds'] and body.get('kind','assertion') not in facets['kinds']: continue
            if facets['workspace_ids'] and not set(facets['workspace_ids']).intersection(body.get('workspace_ids',[])): continue
            if facets['source_instances'] and row['source_instance'] not in facets['source_instances']: continue
            projected=project_fields(body,[grant])
            found.append(dict(state='withheld') if row['state']=='withheld' else dict(state='ok',entity=projected))
        start=selection['offset']; items=found[start:start+selection['limit']]
        return dict(state='ok' if items else 'empty',items=items,count=len(found))

    def context(self, selection):
        """B4.2: citable context package for requested ids plus visible parents (D2-D8).

        Each id resolves with get semantics (D3): absent, deleted and ungranted are the same
        entity.unknown; withheld keeps only the id. Parents come only from cited items, through the
        projected parent_id and a strictly higher kind (D4). One read-only snapshot (D6)."""
        facets=selection['facets'];ids=facets['ids']
        if (not ids or facets['kinds'] or facets['workspace_ids'] or facets['source_instances'] or selection['offset'] != 0
            or len(ids) > selection['limit'] or any(_kind(i) is None for i in ids)):
            raise ValueError('brain.context.invalid')
        tenant=selection['tenant_id'];requested=sorted(set(ids));resolved={}
        with self.connection.transaction():
            self.connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            self.fault('context-snapshot')
            grants={g['acl_ref']:g for g in self.connection.execute('SELECT * FROM brain_grants WHERE tenant_id=%s AND principal_id=%s',
                                                                    (tenant,self.principal.id)).fetchall()}
            pending=requested
            while pending:
                rows={r['id']:r for r in self.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id = ANY(%s)',
                                                                 (tenant,pending)).fetchall()}
                parents=set()
                for id in pending:
                    entry=resolved[id]=self._context_entry(rows.get(id),grants,tenant,id)
                    parent=entry.get('parent_id') if isinstance(entry,dict) else None
                    if isinstance(parent,str) and _kind(parent) is not None and _kind(parent) < _kind(id): parents.add(parent)
                pending=sorted(parents-set(resolved))
        items,gaps=[],[]
        for id,entry in resolved.items():
            if entry is None: gaps.append(dict(id=id,code='entity.unknown'))
            elif entry=='withheld': items.append(dict(id=id,state='withheld'));gaps.append(dict(id=id,code='entity.withheld'))
            elif entry=='uncited': gaps.append(dict(id=id,code='citation.incomplete'))
            else:
                items.append(dict(id=id,state='ok',entity=entry))
                owner=entry.get('owner')
                if not isinstance(owner,dict) or owner.get('principal') is None: gaps.append(dict(id=id,code='owner.unresolved'))
                if entry.get('observed_at') is None: gaps.append(dict(id=id,code='observed.unknown'))
                if entry.get('recorded_at') is None: gaps.append(dict(id=id,code='recorded.unknown'))
        # Code point order on ASCII identifiers: the same order the factory core computes.
        items.sort(key=lambda i:(_kind(i['id']),i['id']))
        gaps.sort(key=lambda g:(g['id'],g['code']))
        body=dict(schema=CONTEXT_SCHEMA,tenant_id=tenant,requested=requested,items=items,gaps=gaps)
        # D8: the digest covers what was said and where it came from; nothing time-dependent is inside.
        return dict(state='ok' if items else 'empty',context=dict(body,digest=digest(body)))

    def _context_entry(self, row, grants, tenant, id):
        """None (unknown), 'withheld', 'uncited' or the projected context fields of one entity."""
        if not row or row['state']=='deleted' or (row['body'] or {}).get('schema') != 'orkmind.company-brain-entity/v1':
            return None
        grant=grants.get(row['acl_ref'])
        if not grant or not allows(self.principal,grant,'get',tenant,id,row['source_instance']): return None
        if row['state']=='withheld': return 'withheld'
        body=project_fields(row['body'] or {},[grant])
        entity={k:body[k] for k in CONTEXT_FIELDS if k in body}
        return entity if _cited(entity.get('source')) else 'uncited'

    def get(self, tenant, id):
        if not isinstance(self.principal,Principal) or not self.principal.authenticated or self.principal.kind != 'human' or self.principal.tenant_id != tenant:
            return dict(state='forbidden')
        row=self.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
        if not row or row['state']=='deleted': return dict(state='unknown')
        event=dict(tenant_id=tenant,aggregate_id=id,acl_ref=row['acl_ref'],source=dict(instance=row['source_instance']))
        try: grant=self._authorize(event,'get')
        except PermissionError: return dict(state='unknown')
        if row['state']=='withheld': return dict(state='withheld')
        return dict(state='ok',entity=project_fields(row['body'],[grant]))

    def history(self, tenant, id, limit=100, offset=0):
        """B4.2/D9-D10: append-only versions of one entity, in sequence order, each with its origin.

        Needs its own history action (default deny): without it the answer equals an absent entity.
        A deleted entity keeps its trail, the tombstone being its last version; withheld hides it all.
        Without the source field no version is citable, so the whole reading is refused. Origin metadata
        (event, producer, cycle, recorded_at) always comes; body fields, version included, follow the grant."""
        if not isinstance(self.principal,Principal) or not self.principal.authenticated or self.principal.kind != 'human' or self.principal.tenant_id != tenant:
            return dict(state='forbidden')
        with self.connection.transaction():
            self.connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
            self.fault('history-snapshot')
            row=self.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
            if not row: return dict(state='unknown')
            try: grant=self._authorize(dict(tenant_id=tenant,aggregate_id=id,acl_ref=row['acl_ref'],source=dict(instance=row['source_instance'])),'history')
            except PermissionError: return dict(state='unknown')
            if row['state']=='withheld': return dict(state='withheld')
            if 'source' not in grant['fields']: return dict(state='forbidden',error='brain.history.fields-forbidden')
            count=self.connection.execute('SELECT count(*) AS n FROM brain_history WHERE tenant_id=%s AND aggregate_id=%s',(tenant,id)).fetchone()['n']
            rows=self.connection.execute("""SELECT i.sequence, i.event, h.after_image, h.recorded_at,
                (r.body->>'materialized_version')::bigint AS version, b.event_id IS NOT NULL AS rolled_back
              FROM brain_history h JOIN brain_inbox i ON i.tenant_id=h.tenant_id AND i.event_id=h.event_id
              LEFT JOIN brain_receipts r ON r.tenant_id=h.tenant_id AND r.event_id=h.event_id AND r.stage='materialized'
              LEFT JOIN brain_rolled_back_events b ON b.tenant_id=h.tenant_id AND b.event_id=h.event_id
              WHERE h.tenant_id=%s AND h.aggregate_id=%s ORDER BY i.sequence, i.event_id LIMIT %s OFFSET %s""",
                (tenant,id,limit,offset)).fetchall()
        versions=[]
        for r in rows:
            event=r['event'];cycle=event.get('cycle') or {}
            versions.append(dict(sequence=r['sequence'],event_id=event['id'],operation=event['operation'],
                version=r['version'] if 'version' in grant['fields'] else None,
                source=event['source'],producer_id=event['producer_id'],thread_id=cycle.get('thread_id'),phase=cycle.get('phase'),
                recorded_at=r['recorded_at'].astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),
                rolled_back=r['rolled_back'],entity=project_fields(r['after_image'],[grant]) if r['after_image'] else None))
        return dict(state='ok',id=id,active=row['state']=='active',count=count,versions=versions)

    def head(self, tenant, id):
        """Minimal authenticated producer head used to join migration and capture."""
        row=self.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s',(tenant,id)).fetchone()
        if not row: return dict(state='unknown')
        event=dict(tenant_id=tenant,aggregate_id=id,acl_ref=row['acl_ref'],source=dict(instance=row['source_instance']))
        try: self._authorize(event,'ingest')
        except PermissionError: return dict(state='forbidden')
        body=row['body'] or {}
        source=body.get('source') or {}
        return dict(state='ok',active=row['state']!='deleted',sequence=row['sequence'],
                    source_version=source.get('source_version'),source_hash=source.get('source_hash'))

    def receipts(self,tenant,eid):
        if not isinstance(self.principal,Principal) or self.principal.tenant_id != tenant or not self.principal.authenticated:
            return dict(state='forbidden',items=[])
        inbox=self.connection.execute('SELECT event FROM brain_inbox WHERE tenant_id=%s AND event_id=%s',(tenant,eid)).fetchone()
        if not inbox: return dict(state='unknown',items=[])
        try: self._authorize(inbox['event'],'receipts')
        except PermissionError: return dict(state='unknown',items=[])
        rows=self.connection.execute('SELECT body FROM brain_receipts WHERE tenant_id=%s AND event_id=%s ORDER BY id',(tenant,eid)).fetchall()
        return dict(state='ok',items=[r['body'] for r in rows])

    def ingest(self, raw):
        event = validate(raw)
        if event['schema'] != 'orkmind.company-brain-event/v1':
            raise ValueError('brain.contract.invalid')
        tenant, eid = event['tenant_id'], event['id']
        with self.connection.transaction():
            self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (tenant,))
            self._authorize(event, 'ingest')
            if self.connection.execute('SELECT event_id FROM brain_rolled_back_events WHERE tenant_id=%s AND event_id=%s',(tenant,eid)).fetchone():
                raise ValueError('brain.event.rolled-back')
            current = self.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s FOR UPDATE',
                (tenant,event['aggregate_id'])).fetchone()
            self._authorize_current(event,current,'ingest')
            old = self.connection.execute('SELECT event_hash,event_id FROM brain_inbox WHERE tenant_id=%s AND producer_id=%s AND source_event_id=%s',
                (tenant,event['producer_id'],event['source_event_id'])).fetchone()
            if old:
                if old['event_hash'] != digest(event):
                    raise ValueError('brain.event.conflict')
                return self._receipt(tenant, old['event_id'], 'materialized')
            expected = self.next_sequence(tenant,event['aggregate_id'])
            if event['sequence'] != expected:
                raise ValueError('brain.event.sequence-gap')
            if current and current['producer_id'] != event['producer_id']:
                raise ValueError('brain.authority.conflict')
            payload = event['payload']
            version = payload['version'] if payload else (current['version'] + 1 if current else 1)
            if current and current['state']!='deleted' and version <= current['version']:
                raise ValueError('brain.version.conflict')
            if payload and payload['schema'].endswith('entity/v1'):
                self._references(tenant,payload)
            receipt = self._persist(event,current,version)
        self.fault('committed')
        return receipt

    def _persist(self,event,current,version):
        """Caller holds the tenant lock and transaction; rollback also appends effects."""
        tenant,eid,payload=event['tenant_id'],event['id'],event['payload']
        self.connection.execute('INSERT INTO brain_inbox(tenant_id,producer_id,source_event_id,event_id,aggregate_id,sequence,event_hash,event) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
            (tenant,event['producer_id'],event['source_event_id'],eid,event['aggregate_id'],event['sequence'],digest(event),Jsonb(event)))
        self.fault('inbox')
        body = Jsonb(payload) if payload else None
        state = 'active' if payload else 'deleted'
        self.connection.execute('INSERT INTO brain_projection(tenant_id,id,version,sequence,producer_id,event_id,acl_ref,source_instance,body,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,id) DO UPDATE SET version=EXCLUDED.version,sequence=EXCLUDED.sequence,producer_id=EXCLUDED.producer_id,event_id=EXCLUDED.event_id,acl_ref=EXCLUDED.acl_ref,source_instance=EXCLUDED.source_instance,body=EXCLUDED.body,state=EXCLUDED.state',
            (tenant,event['aggregate_id'],version,event['sequence'],event['producer_id'],eid,event['acl_ref'],event['source']['instance'],body,state))
        self.connection.execute('INSERT INTO brain_history(tenant_id,event_id,aggregate_id,before_image,after_image) VALUES(%s,%s,%s,%s,%s)',
            (tenant,eid,event['aggregate_id'],Jsonb(current) if current else None,body))
        self.fault('projection')
        previous = None
        for stage in ('received','validated','materialized'):
            receipt = self._write_receipt(event,stage,version,previous)
            previous = receipt['id']
        self.connection.execute('INSERT INTO brain_outbox(tenant_id,event_id) VALUES(%s,%s)', (tenant,eid))
        self.fault('receipt')
        return receipt

    def _references(self, tenant, entity):
        ids = ([entity['parent_id']] if entity['parent_id'] else []) + entity['depends_on']
        for id in ids:
            row = self.connection.execute("SELECT body FROM brain_projection WHERE tenant_id=%s AND id=%s AND state='active'", (tenant,id)).fetchone()
            if not row or not row['body']:
                raise ValueError('brain.catalog.parent-missing')
            if id in entity['depends_on'] and row['body'].get('parent_id') != entity['parent_id']:
                raise ValueError('brain.catalog.dependency-invalid')

    def _write_receipt(self, event, stage, version, previous):
        rid = 'receipt-' + digest([event['tenant_id'],event['id'],stage])
        result = dict(schema='orkmind.company-brain-receipt/v1',tenant_id=event['tenant_id'],id=rid,event_id=event['id'],stage=stage,result='ok',attempt=1,
            materialized_version=version,recorded_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'),previous_receipt_id=previous,error=None)
        self.connection.execute('INSERT INTO brain_receipts(tenant_id,id,event_id,stage,body) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,event_id,stage) DO NOTHING',
            (event['tenant_id'],rid,event['id'],stage,Jsonb(result)))
        return self._receipt(event['tenant_id'],event['id'],stage)

    def _receipt(self, tenant, eid, stage):
        if self.connection.execute('SELECT event_id FROM brain_rolled_back_events WHERE tenant_id=%s AND event_id=%s',(tenant,eid)).fetchone():
            raise ValueError('brain.event.rolled-back')
        row = self.connection.execute('SELECT body FROM brain_receipts WHERE tenant_id=%s AND event_id=%s AND stage=%s', (tenant,eid,stage)).fetchone()
        if not row:
            raise ValueError('brain.receipt.missing')
        return row['body']

    def index(self, raw):
        event = validate(raw)
        with self.connection.transaction():
            self._authorize(event,'ingest')
            inbox = self.connection.execute('SELECT event_hash FROM brain_inbox WHERE tenant_id=%s AND event_id=%s', (event['tenant_id'],event['id'])).fetchone()
            if not inbox or inbox['event_hash'] != digest(event):
                raise ValueError('brain.event.conflict')
            previous = self._receipt(event['tenant_id'],event['id'],'materialized')
            for stage in ('indexed','retrievable'):
                previous = self._write_receipt(event,stage,previous['materialized_version'],previous['id'])
            self.connection.execute("UPDATE brain_outbox SET state='complete',attempts=attempts+1 WHERE tenant_id=%s AND event_id=%s",(event['tenant_id'],event['id']))
        return previous
