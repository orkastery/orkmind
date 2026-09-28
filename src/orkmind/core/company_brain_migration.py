"""D10/D11: scoped optimistic migration; compensations never restore a whole DB."""
from psycopg.types.json import Jsonb

from orkmind.core.company_brain import digest, validate, validate_catalog


class BrainMigration:
    def __init__(self,store): self.store=store

    def _event(self,entity,batch,sequence):
        return dict(schema='orkmind.company-brain-event/v1',tenant_id=entity['tenant_id'],id='event-'+digest([batch,entity['id']]),producer_id='ork',aggregate_id=entity['id'],sequence=sequence,
            source_event_id='migration:'+batch+':'+entity['id'],source=entity['source'],operation='upsert',payload=entity,payload_hash=digest(entity),cycle=None,evidence_refs=[entity['source']['source_ref']],occurred_at=None,observed_at=entity['observed_at'],acl_ref=entity['acl_ref'])

    def plan(self,entities,batch_id,source_hash,observed_at):
        batch=batch_id
        entities=validate_catalog(entities)
        if not entities: raise ValueError('brain.migration.empty')
        aliases={}
        for entity in entities:
            for alias in entity['aliases']:
                key=(alias['system'],alias['instance'],alias['id'])
                if key in aliases and aliases[key]!=entity['id']: raise ValueError('brain.alias.conflict')
                aliases[key]=entity['id']
        ordered=[];pending=list(entities)
        while pending:
            ready=[e for e in pending if all(i in {v['id'] for v in ordered} for i in ([e['parent_id']] if e['parent_id'] else [])+e['depends_on'])]
            if not ready: raise ValueError('brain.catalog.cycle')
            ordered.extend(sorted(ready,key=lambda e:e['id']));pending=[e for e in pending if e not in ready]
        ops=[]
        for entity in ordered:
            event=self._event(entity,batch,1);self.store._authorize(event,'migrate')
            row=self.store.connection.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s',(entity['tenant_id'],entity['id'])).fetchone()
            self.store._authorize_current(event,row,'migrate')
            conflict=row and (row['source_instance']!=entity['source']['instance'] or row['producer_id']!='ork' or (row['state']!='deleted' and row['body']!=entity and row['version']>=entity['version']))
            operation='conflict' if conflict else 'unchanged' if row and row['body']==entity else 'update' if row else 'insert'
            ops.append(dict(id=entity['id'],operation=operation,expected_version=row['version'] if row else None,before_hash=digest(row['body']) if row else None,after=entity,gaps=['owner-unresolved'] if entity['owner']['principal'] is None else []))
        return validate(dict(schema='orkmind.company-brain-migration/v1',tenant_id=entities[0]['tenant_id'],batch_id=batch,source_hash=source_hash,operations=ops,observed_at=observed_at))

    def apply(self,plan,expected_hash,current_source_hash):
        plan=validate(plan)
        if plan['schema']!='orkmind.company-brain-migration/v1' or digest(plan)!=expected_hash or plan['source_hash']!=current_source_hash: raise ValueError('brain.migration.stale')
        if any(op['operation']=='conflict' for op in plan['operations']): raise ValueError('brain.migration.conflict')
        conn=self.store.connection;tenant=plan['tenant_id'];batch=plan['batch_id']
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant,))
            # A receipt is protected readback too: revoke applies to every replay.
            for op in plan['operations']:
                event=self._event(op['after'],batch,1)
                self.store._authorize(event,'migrate')
                current=conn.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s',(tenant,op['id'])).fetchone()
                self.store._authorize_current(event,current,'migrate')
            existing=conn.execute('SELECT * FROM brain_migration_batches WHERE tenant_id=%s AND batch_id=%s',(tenant,batch)).fetchone()
            if existing:
                if existing['plan_hash']!=expected_hash or existing['state']!='applied': raise ValueError('brain.migration.conflict')
                return existing['receipt']
            changes=[]
            for op in plan['operations']:
                self.store._authorize(self._event(op['after'],batch,1),'migrate')
                row=conn.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,op['id'])).fetchone()
                if (row['version'] if row else None)!=op['expected_version'] or (digest(row['body']) if row else None)!=op['before_hash']: raise ValueError('brain.migration.stale')
                if op['operation']=='unchanged': continue
                event=self._event(op['after'],batch,self.store.next_sequence(tenant,op['id']))
                self.store.ingest(event)
                changes.append(dict(id=op['id'],event=event,before=row))
            receipt=dict(state='applied',batch_id=batch,plan_hash=expected_hash,changed=[c['id'] for c in changes])
            conn.execute('INSERT INTO brain_migration_batches(tenant_id,batch_id,plan_hash,state,changes,receipt) VALUES(%s,%s,%s,%s,%s,%s)',(tenant,batch,expected_hash,'applied',Jsonb(changes),Jsonb(receipt)))
        return receipt

    def rollback(self,tenant,batch):
        conn=self.store.connection
        with conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(tenant,))
            row=conn.execute('SELECT * FROM brain_migration_batches WHERE tenant_id=%s AND batch_id=%s FOR UPDATE',(tenant,batch)).fetchone()
            if not row: raise ValueError('brain.migration.unknown')
            for change in row['changes']: self.store._authorize(change['event'],'rollback')
            if row['state']=='rolled-back': return row['receipt']
            conflicts=[];restored=[]
            for change in reversed(row['changes']):
                if conn.execute('SELECT event_id FROM brain_rolled_back_events WHERE tenant_id=%s AND event_id=%s',(tenant,change['event']['id'])).fetchone():
                    restored.append(change['id']);continue
                current=conn.execute('SELECT * FROM brain_projection WHERE tenant_id=%s AND id=%s FOR UPDATE',(tenant,change['id'])).fetchone()
                if not current or current['event_id']!=change['event']['id']:
                    conflicts.append(change['id']);continue
                before=change['before']
                if not before or not before['body']:
                    active=conn.execute("SELECT id,body FROM brain_projection WHERE tenant_id=%s AND state='active'",(tenant,)).fetchall()
                    if any(r['body'] and (r['body'].get('parent_id')==change['id'] or change['id'] in r['body'].get('depends_on',[])) for r in active):
                        conflicts.append(change['id']);continue
                self.store._authorize_current(change['event'],current,'rollback')
                payload=before['body'] if before else None
                compensation=dict(change['event'],id='event-'+digest(['rollback',batch,change['id']]),
                    source_event_id='rollback:'+batch+':'+change['id'],sequence=self.store.next_sequence(tenant,change['id']),
                    payload=payload,payload_hash=digest(payload),operation='upsert' if payload else 'tombstone',
                    source=payload['source'] if payload else change['event']['source'])
                compensation=validate(compensation)
                self.store._persist(compensation,current,before['version'] if before else current['version'])
                if before:
                    conn.execute('UPDATE brain_projection SET state=%s WHERE tenant_id=%s AND id=%s',(before['state'],tenant,change['id']))
                self.store.index(compensation)
                conn.execute('INSERT INTO brain_rolled_back_events(tenant_id,event_id,batch_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(tenant,change['event']['id'],batch))
                conn.execute("UPDATE brain_outbox SET state='rolled-back' WHERE tenant_id=%s AND event_id=%s",(tenant,change['event']['id']))
                restored.append(change['id'])
            receipt=dict(state='conflict' if conflicts else 'rolled-back',batch_id=batch,restored=restored,conflicts=conflicts)
            conn.execute('UPDATE brain_migration_batches SET state=%s,receipt=%s WHERE tenant_id=%s AND batch_id=%s',(receipt['state'],Jsonb(receipt),tenant,batch))
        return receipt
