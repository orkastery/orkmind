-- Memory Provider nativo - DDL canonico.
--
-- Alvo: PostgreSQL 15+ (validado em 17) com pgvector >= 0.5 (iterative scan do
-- HNSW exige >= 0.8). O arquivo e um TEMPLATE: `db/migrations.py` substitui
--   {{embedding_dim}}  dimensao do vetor (default 1536, maximo 2000 no HNSW)
--   {{fts_config}}     configuracao de full-text search (default portuguese)
-- por valores validados antes de executar. Nao ha nome de schema aqui: o
-- isolamento vem do search_path da conexao.
--
-- Tudo e idempotente (IF NOT EXISTS / OR REPLACE): rodar duas vezes nao muda
-- nada. Tres tabelas sao append-only por TRIGGER, nao por convencao:
-- chat_history, wiki_document_versions e (com excecao da desativacao)
-- core_blocks. Compactacao destrutiva nao e uma opcao que o banco aceite.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Metadados do proprio schema (versao, dimensao e idioma com que foi criado)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_provider_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Guarda generica de append-only
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION memory_provider_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% e append-only: % proibido', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Tier 1 - Core Memory
-- ---------------------------------------------------------------------------
-- Blocos pinados no prompt. Nunca expiram e nunca sao podados: nao existe
-- coluna de TTL e o trigger abaixo recusa DELETE. Trocar o texto de um bloco
-- nao edita a linha: desativa a versao vigente e insere a proxima, entao o
-- que o agente ja leu continua auditavel.

CREATE TABLE IF NOT EXISTS core_blocks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      TEXT NOT NULL CHECK (length(agent_id) > 0),
    label         TEXT NOT NULL
                  CHECK (label IN ('persona', 'system_invariants', 'user_profile')),
    -- '' = bloco global do agente; em user_profile, o id do usuario.
    scope_key     TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL CHECK (length(content) > 0),
    content_hash  CHAR(64) NOT NULL,
    version       INT NOT NULL DEFAULT 1 CHECK (version >= 1),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_by    TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at TIMESTAMPTZ,
    CONSTRAINT core_blocks_scope_ck CHECK (label = 'user_profile' OR scope_key = ''),
    CONSTRAINT core_blocks_version_uq UNIQUE (agent_id, label, scope_key, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS core_blocks_active_uq
    ON core_blocks (agent_id, label, scope_key) WHERE is_active;

CREATE OR REPLACE FUNCTION core_blocks_guard() RETURNS trigger AS $$
BEGIN
    -- A unica mutacao aceita e aposentar a versao vigente.
    IF ROW(NEW.id, NEW.agent_id, NEW.label, NEW.scope_key, NEW.content,
           NEW.content_hash, NEW.version, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.agent_id, OLD.label, OLD.scope_key, OLD.content,
           OLD.content_hash, OLD.version, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'core_blocks e imutavel: crie uma nova versao em vez de editar'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.is_active AND NOT OLD.is_active THEN
        RAISE EXCEPTION 'core_blocks: versao aposentada nao volta a ser ativa'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER core_blocks_guard_update
    BEFORE UPDATE ON core_blocks
    FOR EACH ROW EXECUTE FUNCTION core_blocks_guard();

CREATE OR REPLACE TRIGGER core_blocks_no_delete
    BEFORE DELETE ON core_blocks
    FOR EACH ROW EXECUTE FUNCTION memory_provider_append_only();

CREATE OR REPLACE TRIGGER core_blocks_no_truncate
    BEFORE TRUNCATE ON core_blocks
    FOR EACH STATEMENT EXECUTE FUNCTION memory_provider_append_only();

-- ---------------------------------------------------------------------------
-- Tier 2 - Recall Memory
-- ---------------------------------------------------------------------------
-- Buffer append-only. A janela curta que vai para o orquestrador e uma VISAO
-- (ultimos N turnos); os turnos antigos ficam aqui, integros, para auditoria
-- e para busca lexica. `seq` da a ordem total; `turn_index` sobe a cada
-- mensagem de papel `user`.

CREATE TABLE IF NOT EXISTS chat_history (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq            BIGINT GENERATED ALWAYS AS IDENTITY,
    session_id     TEXT NOT NULL CHECK (length(session_id) > 0),
    agent_id       TEXT NOT NULL,
    user_id        TEXT,
    turn_index     INT NOT NULL CHECK (turn_index >= 1),
    role           TEXT NOT NULL
                   CHECK (role IN ('user', 'assistant', 'tool_call', 'tool_result')),
    content        TEXT NOT NULL,
    tool_calls     JSONB,
    token_estimate INT NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- left(): um tool_result de varios MB estouraria o limite de 1 MB do
    -- tsvector e derrubaria o INSERT. O conteudo integro continua em `content`.
    tsv            tsvector GENERATED ALWAYS AS
                   (to_tsvector('{{fts_config}}'::regconfig, left(content, 100000))) STORED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chat_history_seq_uq UNIQUE (seq)
);

CREATE INDEX IF NOT EXISTS chat_history_session_idx
    ON chat_history (session_id, turn_index DESC, seq);
CREATE INDEX IF NOT EXISTS chat_history_user_idx
    ON chat_history (user_id, seq DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS chat_history_tsv_gin
    ON chat_history USING gin (tsv);

CREATE OR REPLACE TRIGGER chat_history_append_only
    BEFORE UPDATE OR DELETE ON chat_history
    FOR EACH ROW EXECUTE FUNCTION memory_provider_append_only();

CREATE OR REPLACE TRIGGER chat_history_no_truncate
    BEFORE TRUNCATE ON chat_history
    FOR EACH STATEMENT EXECUTE FUNCTION memory_provider_append_only();

-- ---------------------------------------------------------------------------
-- Tier 3 - Wiki Memory: documento integro
-- ---------------------------------------------------------------------------
-- `wiki_documents` e a CABECA (revisao vigente); o id e estavel entre
-- revisoes, entao o meta-indice e os chunks apontam para ele sem quebrar.
-- Toda revisao, inclusive a primeira, e copiada por trigger para
-- `wiki_document_versions`, que e append-only.

CREATE TABLE IF NOT EXISTS wiki_documents (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug               TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9_-]*$'),
    title              TEXT NOT NULL CHECK (length(title) > 0),
    doc_type           TEXT NOT NULL CHECK (doc_type ~ '^[a-z][a-z0-9_]*$'),
    source_url_or_path TEXT,
    content_format     TEXT NOT NULL DEFAULT 'markdown'
                       CHECK (content_format IN
                              ('markdown', 'text', 'html', 'pdf_text', 'docx_text')),
    raw_content        TEXT NOT NULL,
    content_hash       CHAR(64) NOT NULL,
    version            INT NOT NULL DEFAULT 1 CHECK (version >= 1),
    -- Sem DEFAULT de proposito: quem ingere classifica.
    access_level       TEXT NOT NULL
                       CHECK (access_level IN ('OPERATIONAL', 'EXECUTIVE', 'SYSTEM_ADMIN')),
    -- '{}' = vale para a empresa toda.
    department_scope   TEXT[] NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding_model    TEXT,
    chunk_count        INT NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
    updated_by         TEXT NOT NULL DEFAULT 'system',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS wiki_documents_hash_idx ON wiki_documents (content_hash);
CREATE INDEX IF NOT EXISTS wiki_documents_scope_idx
    ON wiki_documents (status, access_level, doc_type);
CREATE INDEX IF NOT EXISTS wiki_documents_dept_gin
    ON wiki_documents USING gin (department_scope);

CREATE TABLE IF NOT EXISTS wiki_document_versions (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id        UUID NOT NULL REFERENCES wiki_documents (id) ON DELETE RESTRICT,
    version            INT NOT NULL,
    slug               TEXT NOT NULL,
    title              TEXT NOT NULL,
    doc_type           TEXT NOT NULL,
    source_url_or_path TEXT,
    content_format     TEXT NOT NULL,
    raw_content        TEXT NOT NULL,
    content_hash       CHAR(64) NOT NULL,
    access_level       TEXT NOT NULL,
    department_scope   TEXT[] NOT NULL,
    status             TEXT NOT NULL,
    metadata           JSONB NOT NULL,
    recorded_by        TEXT NOT NULL,
    recorded_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT wiki_document_versions_uq UNIQUE (document_id, version)
);

CREATE OR REPLACE FUNCTION wiki_documents_guard() RETURNS trigger AS $$
BEGIN
    -- Campo auditado so muda junto com a revisao. Isso vale para SQL manual
    -- tambem: baixar a classificacao de um estudo sem deixar rastro nao passa.
    IF ROW(NEW.slug, NEW.title, NEW.doc_type, NEW.source_url_or_path, NEW.content_format,
           NEW.raw_content, NEW.content_hash, NEW.access_level, NEW.department_scope,
           NEW.status, NEW.metadata)
       IS DISTINCT FROM
       ROW(OLD.slug, OLD.title, OLD.doc_type, OLD.source_url_or_path, OLD.content_format,
           OLD.raw_content, OLD.content_hash, OLD.access_level, OLD.department_scope,
           OLD.status, OLD.metadata)
       AND NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'wiki_documents: campo auditado so muda com version = version + 1'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.version NOT IN (OLD.version, OLD.version + 1) THEN
        RAISE EXCEPTION 'wiki_documents: version so avanca de um em um (% -> %)',
            OLD.version, NEW.version
            USING ERRCODE = 'restrict_violation';
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION wiki_documents_record_version() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.version <> OLD.version THEN
        INSERT INTO wiki_document_versions (
            document_id, version, slug, title, doc_type, source_url_or_path,
            content_format, raw_content, content_hash, access_level,
            department_scope, status, metadata, recorded_by
        ) VALUES (
            NEW.id, NEW.version, NEW.slug, NEW.title, NEW.doc_type, NEW.source_url_or_path,
            NEW.content_format, NEW.raw_content, NEW.content_hash, NEW.access_level,
            NEW.department_scope, NEW.status, NEW.metadata, NEW.updated_by
        );
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER wiki_documents_guard_update
    BEFORE UPDATE ON wiki_documents
    FOR EACH ROW EXECUTE FUNCTION wiki_documents_guard();

CREATE OR REPLACE TRIGGER wiki_documents_versioning
    AFTER INSERT OR UPDATE ON wiki_documents
    FOR EACH ROW EXECUTE FUNCTION wiki_documents_record_version();

CREATE OR REPLACE TRIGGER wiki_document_versions_append_only
    BEFORE UPDATE OR DELETE ON wiki_document_versions
    FOR EACH ROW EXECUTE FUNCTION memory_provider_append_only();

CREATE OR REPLACE TRIGGER wiki_document_versions_no_truncate
    BEFORE TRUNCATE ON wiki_document_versions
    FOR EACH STATEMENT EXECUTE FUNCTION memory_provider_append_only();

-- ---------------------------------------------------------------------------
-- Tier 3 - Wiki Memory: fragmentos indexados
-- ---------------------------------------------------------------------------
-- Derivados e descartaveis: sempre da para refazer a partir de raw_content.
-- Por isso, e so por isso, aqui existe DELETE (reingestao e aposentadoria).
-- Classificacao e escopo NAO sao copiados para ca: a fonte unica e
-- wiki_documents, via JOIN, para que chunk e documento nunca discordem.

CREATE TABLE IF NOT EXISTS wiki_chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id   UUID NOT NULL REFERENCES wiki_documents (id) ON DELETE CASCADE,
    chunk_index   INT NOT NULL CHECK (chunk_index >= 0),
    heading_path  TEXT NOT NULL DEFAULT '',
    content       TEXT NOT NULL CHECK (length(content) > 0),
    token_count   INT NOT NULL CHECK (token_count > 0),
    embedding     vector({{embedding_dim}}) NOT NULL,
    metadata_tags JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Peso A para o caminho de titulos, B para o corpo.
    tsv           tsvector GENERATED ALWAYS AS (
                      setweight(to_tsvector('{{fts_config}}'::regconfig, heading_path), 'A')
                      || setweight(to_tsvector('{{fts_config}}'::regconfig, content), 'B')
                  ) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT wiki_chunks_doc_index_uq UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS wiki_chunks_embedding_hnsw
    ON wiki_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Equivale ao gin(to_tsvector('portuguese', content)) do desenho original; a
-- coluna gerada evita recalcular o tsvector a cada consulta e ainda pondera
-- o caminho de titulos.
CREATE INDEX IF NOT EXISTS wiki_chunks_tsv_gin
    ON wiki_chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS wiki_chunks_tags_gin
    ON wiki_chunks USING gin (metadata_tags);

-- ---------------------------------------------------------------------------
-- A teia: links entre documentos (wikilinks e backlinks)
-- ---------------------------------------------------------------------------
-- O que separa um vault de uma pilha de documentos. Duas decisoes moldam a
-- tabela:
--
-- 1. `target_document_id` e NULO quando o alvo ainda nao existe, e isso NAO e
--    erro: apontar para uma nota que falta escrever e uso normal de vault, e
--    a lista desses alvos e por si so um mapa do que falta documentar. O link
--    se resolve sozinho quando a nota nasce.
-- 2. `context` guarda o trecho ao redor. Backlink sem contexto obriga a abrir
--    a nota de origem so para descobrir por que ela aponta para ca.

CREATE TABLE IF NOT EXISTS wiki_links (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_document_id UUID NOT NULL REFERENCES wiki_documents (id) ON DELETE CASCADE,
    target_slug        TEXT NOT NULL CHECK (length(target_slug) > 0),
    -- SET NULL, nao CASCADE: apagar o alvo desfaz a resolucao, nao a aresta.
    target_document_id UUID REFERENCES wiki_documents (id) ON DELETE SET NULL,
    kind               TEXT NOT NULL DEFAULT 'wikilink'
                       CHECK (kind IN ('wikilink', 'embed', 'markdown')),
    alias              TEXT,
    anchor             TEXT,
    context            TEXT NOT NULL DEFAULT '',
    position           INT NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT wiki_links_uq UNIQUE NULLS NOT DISTINCT
        (source_document_id, target_slug, kind, anchor)
);

CREATE INDEX IF NOT EXISTS wiki_links_source_idx ON wiki_links (source_document_id, position);
-- Backlinks: a consulta mais quente da teia.
CREATE INDEX IF NOT EXISTS wiki_links_target_idx
    ON wiki_links (target_document_id) WHERE target_document_id IS NOT NULL;
-- Pendentes: alimenta tanto a resolucao tardia quanto o painel "falta escrever".
CREATE INDEX IF NOT EXISTS wiki_links_pendentes_idx
    ON wiki_links (target_slug) WHERE target_document_id IS NULL;

-- ---------------------------------------------------------------------------
-- Tags semanticas
-- ---------------------------------------------------------------------------
-- Tabela propria em vez de JSONB porque tag aqui e coisa NAVEGAVEL: listar
-- todas com contagem, filtrar por uma, subir e descer hierarquia. A barra
-- separa niveis (`rede/backbone` esta sob `rede`), no espirito SKOS
-- broader/narrower que o roadmap ja adotava como referencia. A hierarquia e
-- resolvida na consulta (`tag = x OR tag LIKE x || '/%'`), sem denormalizar.

CREATE TABLE IF NOT EXISTS wiki_document_tags (
    document_id UUID NOT NULL REFERENCES wiki_documents (id) ON DELETE CASCADE,
    tag         TEXT NOT NULL CHECK (tag ~ '^[a-z0-9][a-z0-9_/-]*$'),
    source      TEXT NOT NULL DEFAULT 'inline'
                CHECK (source IN ('frontmatter', 'inline', 'ingest')),
    PRIMARY KEY (document_id, tag)
);

CREATE INDEX IF NOT EXISTS wiki_document_tags_tag_idx
    ON wiki_document_tags (tag text_pattern_ops);

-- ---------------------------------------------------------------------------
-- Meta-indice ("indice de indices")
-- ---------------------------------------------------------------------------
-- Nivel 0 dominio, nivel 1 no tematico, nivel 2 apontador de documento. O
-- agente navega aqui antes de pagar uma busca vetorial. Nos de nivel 0 e 1
-- tem classificacao propria, porque o NOME de um projeto pode ser sigiloso;
-- no nivel 2 vale a classificacao do documento apontado.

CREATE TABLE IF NOT EXISTS meta_index (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id    UUID REFERENCES meta_index (id) ON DELETE RESTRICT,
    level        SMALLINT NOT NULL CHECK (level IN (0, 1, 2)),
    key          TEXT NOT NULL,
    path         TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    access_level TEXT NOT NULL DEFAULT 'OPERATIONAL'
                 CHECK (access_level IN ('OPERATIONAL', 'EXECUTIVE', 'SYSTEM_ADMIN')),
    document_id  UUID REFERENCES wiki_documents (id) ON DELETE CASCADE,
    position     INT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT meta_index_root_ck CHECK ((level = 0) = (parent_id IS NULL)),
    CONSTRAINT meta_index_pointer_ck CHECK ((level = 2) = (document_id IS NOT NULL)),
    CONSTRAINT meta_index_key_ck CHECK (level = 2 OR key ~ '^[A-Z0-9][A-Z0-9_]*$'),
    CONSTRAINT meta_index_sibling_uq UNIQUE NULLS NOT DISTINCT (parent_id, key),
    CONSTRAINT meta_index_path_uq UNIQUE (path)
);

CREATE INDEX IF NOT EXISTS meta_index_parent_idx ON meta_index (parent_id, position, key);
CREATE INDEX IF NOT EXISTS meta_index_document_idx
    ON meta_index (document_id) WHERE document_id IS NOT NULL;

CREATE OR REPLACE FUNCTION meta_index_guard() RETURNS trigger AS $$
DECLARE
    parent meta_index%ROWTYPE;
BEGIN
    IF NEW.parent_id IS NULL THEN
        NEW.path := NEW.key;
    ELSE
        SELECT * INTO parent FROM meta_index WHERE id = NEW.parent_id;
        IF parent.level <> NEW.level - 1 THEN
            RAISE EXCEPTION 'meta_index: no de nivel % exige pai de nivel %, recebeu %',
                NEW.level, NEW.level - 1, parent.level
                USING ERRCODE = 'check_violation';
        END IF;
        NEW.path := parent.path || '/' || NEW.key;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER meta_index_guard_write
    BEFORE INSERT OR UPDATE ON meta_index
    FOR EACH ROW EXECUTE FUNCTION meta_index_guard();
