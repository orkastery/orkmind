-- D8/D9: additive projection, never a second writer of the factory.
-- Provisioned by the database administrator, never by a request or an owner label.
-- Application roles get SELECT only; PostgreSQL session_user authenticates the login.
CREATE TABLE IF NOT EXISTS brain_transport_principals (
 tenant_id text NOT NULL, database_role text NOT NULL, principal_id text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('human','service')), revoked boolean NOT NULL DEFAULT false,
 PRIMARY KEY(tenant_id,database_role)
);
REVOKE ALL ON brain_transport_principals FROM PUBLIC;
CREATE TABLE IF NOT EXISTS brain_inbox (
 tenant_id text NOT NULL, producer_id text NOT NULL, source_event_id text NOT NULL,
 event_id text NOT NULL, aggregate_id text NOT NULL, sequence bigint NOT NULL CHECK(sequence > 0),
 event_hash text NOT NULL, event jsonb NOT NULL, received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id, producer_id, source_event_id), UNIQUE(tenant_id,event_id),
 UNIQUE(tenant_id,producer_id,aggregate_id,sequence)
);
CREATE TABLE IF NOT EXISTS brain_projection (
 tenant_id text NOT NULL, id text NOT NULL, version bigint NOT NULL, sequence bigint NOT NULL,
 producer_id text NOT NULL, event_id text NOT NULL, acl_ref text NOT NULL, source_instance text NOT NULL,
 body jsonb, state text NOT NULL CHECK(state IN ('active','withheld','deleted')),
 PRIMARY KEY(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS brain_history (
 tenant_id text NOT NULL, event_id text NOT NULL, aggregate_id text NOT NULL,
 before_image jsonb, after_image jsonb, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(tenant_id,event_id)
);
CREATE TABLE IF NOT EXISTS brain_receipts (
 tenant_id text NOT NULL, id text NOT NULL, event_id text NOT NULL, stage text NOT NULL,
 body jsonb NOT NULL, PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,event_id,stage)
);
CREATE TABLE IF NOT EXISTS brain_outbox (
 tenant_id text NOT NULL, event_id text NOT NULL, state text NOT NULL DEFAULT 'pending',
 attempts integer NOT NULL DEFAULT 0, error text,
 PRIMARY KEY(tenant_id,event_id)
);
CREATE TABLE IF NOT EXISTS brain_grants (
 tenant_id text NOT NULL, principal_id text NOT NULL, acl_ref text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('human','service')), actions jsonb NOT NULL,
 resources jsonb NOT NULL, source_instances jsonb NOT NULL, fields jsonb NOT NULL,
 expires_at timestamptz, revoked boolean NOT NULL DEFAULT false,
 PRIMARY KEY(tenant_id,principal_id,acl_ref)
);
CREATE INDEX IF NOT EXISTS brain_projection_source ON brain_projection(tenant_id,source_instance);
CREATE INDEX IF NOT EXISTS brain_receipts_event ON brain_receipts(tenant_id,event_id);
CREATE TABLE IF NOT EXISTS brain_migration_batches (
 tenant_id text NOT NULL, batch_id text NOT NULL, plan_hash text NOT NULL,
 state text NOT NULL, changes jsonb NOT NULL, receipt jsonb NOT NULL,
 PRIMARY KEY(tenant_id,batch_id)
);
CREATE TABLE IF NOT EXISTS brain_rolled_back_events (
 tenant_id text NOT NULL, event_id text NOT NULL, batch_id text NOT NULL,
 PRIMARY KEY(tenant_id,event_id)
);
