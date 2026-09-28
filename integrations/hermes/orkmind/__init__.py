"""Plugin de memoria OrkMind para Hermes.

Camada de memoria semantica estruturada com ontologia tipada,
20 colecoes, 8 dimensoes de tags, busca deterministica por tags
e semantica por texto, camadas de contexto E1/E2/E3 com progressive
loading, regras mandatorias com protecao de regras criticas, e
versionamento append-only com garbage collection.

O OrkMind implementa um modelo de memoria SEMANTICO E ESTRUTURADO
baseado em ontologia tipada e tags multidimensionais - diferente de
abordagens baseadas em hierarquia de arquivos ou URIs de filesystem.
Cada entrada de memoria e classificada em colecao, anotada com tags
determinisicas e pode ser recuperada tanto por busca exata (tags,
colecao, prioridade) quanto por busca semantica (texto livre).

Backend: PostgreSQL + pgvector via container orkmind-postgres.
Config: env ORKMIND_DATABASE_URL ou ~/.orkmind/config.toml.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import unicodedata
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

# G1: o plugin e CONSUMIDOR do modulo orkmind.guardrails. D-MA7 (destaque),
# D-MA9 (fail-safe), D-MA10 (hash) e a verificacao por tipo vivem no core,
# agnosticos de runtime; aqui fica so a integracao com o Hermes.
# O import e tolerante porque a descoberta de plugins nao pode quebrar
# quando o pacote orkmind nao esta instalado; nesse cenario o backend
# tambem nao inicializa e nenhum caminho de governanca chega a rodar.
try:
    from orkmind.guardrails import rules as _guardrails_rules
    from orkmind.guardrails import session as _guardrails_session
except ImportError:  # pragma: no cover - so sem o pacote orkmind
    _guardrails_rules = None
    _guardrails_session = None

logger = logging.getLogger(__name__)

# Colecoes validas do OrkMind (20 colecoes tipadas)
_VALID_COLLECTIONS = (
    "rule", "instruction", "fact", "learning", "preference",
    "decision", "content", "agenda", "contacts", "handoff",
    "roadmap", "files", "docs", "dags", "tools", "users",
    # F1: ontologia expandida
    "session", "artifact", "compliance", "semantic_log",
)

# Colecoes seguras para extracao automatica pelo agente
# (NUNCA rule/instruction mandatory - so humano cria criticas)
_SOFT_COLLECTIONS = ("fact", "preference", "learning", "content")

# Dimensoes de tag validas (8 dimensoes)
_VALID_TAG_DIMS = (
    "skill", "agent", "domain", "project", "situation",
    # F1: ontologia expandida
    "person", "audience", "editors",
)

# Prioridades validas
_VALID_PRIORITIES = ("critical", "high", "medium", "low")

# DSN padrao do container orkmind-postgres. Sem senha no codigo: a senha vem
# de ORKMIND_DATABASE_URL, do config.toml ou do ~/.pgpass do usuario.
_DEFAULT_DSN = "postgresql://orkmind@localhost:5432/orkmind"

# F2: identidade usada quando o Hermes nao informa agent_identity
_DEFAULT_REQUESTER_ID = "anonymous"

# ---------------------------------------------------------------------------
# Governanca de memoria autoritativa (Fase 2.7 - decisoes D-MA)
# ---------------------------------------------------------------------------

# Cabecalho descritivo do OrkMind no system prompt.
_SYSTEM_PROMPT_HEADER = (
    "# OrkMind - Memoria Semantica Estruturada\n"
    "\n"
    "Voce possui acesso ao OrkMind, uma camada de memoria semantica\n"
    "com ontologia tipada. Diferente de memorias baseadas em\n"
    "hierarquia de arquivos, o OrkMind organiza conhecimento em\n"
    "20 colecoes tipadas (rule, instruction, fact, learning,\n"
    "preference, decision, content, agenda, contacts, handoff,\n"
    "roadmap, files, docs, dags, tools, users, session,\n"
    "artifact, compliance, semantic_log) com 8 dimensoes de tags\n"
    "(skill, agent, domain, project, situation, person, audience,\n"
    "editors) para busca DETERMINISTICA e EXATA.\n"
    "\n"
    "Ferramentas disponiveis:\n"
    "- orkmind_recall: recuperar memorias por contexto (tags +\n"
    "  semantica) com progressive loading E1/E2/E3\n"
    "- orkmind_search: busca textual livre, filtravel por colecao\n"
    "- orkmind_store: armazenar nova memoria (so colecoes soft,\n"
    "  source=agent, mandatory=false)\n"
    "- orkmind_rules: consultar regras mandatorias ativas\n"
    "- orkmind_handoff: validar e armazenar o pacote de handoff\n"
    "  quando VOCE decidir rotacionar a sessao (o OrkMind nunca\n"
    "  rotaciona nem encerra sessao)\n"
    "\n"
    "Governanca: regras criticas (mandatory=true, priority=critical)\n"
    "so podem ser criadas por humano autenticado. O agente pode\n"
    "armazenar fatos, preferencias, aprendizados e conteudo,\n"
    "mas NUNCA criar regras mandatorias automaticamente.\n"
)

# D-MA1: fracao do token budget reservada para regras mandatorias.
# O restante (70%) fica disponivel para o contexto recuperado por turno.
MANDATORY_BUDGET_RATIO = 0.3

# Estimativa grosseira usada para orcamento: 1 token ~ 4 caracteres.
CHARS_PER_TOKEN = 4

# D-MA7: separador visual isolando o bloco de regras do restante do prompt.
_RULES_SEPARATOR = "---"

# D-MA7: limites do reforco condensado de regras no prefetch (recencia).
_REINFORCEMENT_MAX_RULES = 5
_REINFORCEMENT_ESSENCE_CHARS = 200

# Budget padrao de tokens quando nao ha configuracao explicita.
_DEFAULT_TOKEN_BUDGET = 4000

# D-MA3: intervalo (em turnos) de atualizacao do manifesto de
# disponibilidade. Evita uma consulta de contagem por turno.
_MANIFEST_REFRESH_TURNS = 10

# D-MA8: intervalo (em turnos) do reforco periodico de conformidade.
# Sob pressao de contexto a frequencia dobra (frequencia adaptativa).
REINFORCEMENT_INTERVAL = 10
REINFORCEMENT_INTERVAL_PRESSURE = 5
_REINFORCEMENT_PRESSURE_USAGE = 0.5

# D-MA11 / P6: limiar padrao de uso de contexto para o advisory de
# rotacao de sessao. Configuravel em ~/.orkmind/config.toml [session].
SESSION_ROTATION_THRESHOLD = 0.65

# G2: limiar de URGENCIA (rotate_now). Configuravel em [session]
# rotate_now_threshold. O OrkMind so SINALIZA; quem decide e executa a
# rotacao e sempre o runtime.
SESSION_ROTATE_NOW_THRESHOLD = 0.85

# D-MA11 / P6: faixa permitida no modo adaptativo. O limiar e derivado
# da fracao reservada as regras mandatorias mais uma margem de seguranca.
ADAPTIVE_RANGE = (0.60, 0.80)
_ADAPTIVE_SAFETY_MARGIN = 0.10

# D-MA11 / Q1: o plugin estima internamente os tokens acumulados da
# sessao. Janela de contexto assumida quando o runtime nao informa.
_DEFAULT_CONTEXT_WINDOW = 200_000

# D-MA11 / Q1: custo estimado da resposta do agente por turno, somado a
# estimativa de tokens da mensagem do usuario e do contexto injetado.
_TURN_RESPONSE_TOKENS = 500

# D-MA11: heuristica de fallback por contagem de turnos.
_MAX_TURNS_ESTIMATE = 90

# Quantos turnos de usuario ancoram a query do prefetch. Ancorar so na
# ultima frase perde o assunto da sessao: "e agora?" nao recupera nada,
# mesmo com a sessao inteira falando de deploy.
_RECALL_WINDOW_TURNS = 3

# D-MA5: guardrail anti-destruicao, sempre presente no system prompt.
# Reforca que o OrkMind e a fonte autoritativa e que nenhuma memoria
# pode ser descartada para abrir espaco.
_GUARDRAIL_ANTI_DESTRUICAO = (
    "\n## Governanca de Memoria\n"
    "IMPORTANTE: Voce NAO deve compactar, resumir destrutivamente, "
    "deletar ou sobrescrever memorias para abrir espaco. Todo conteudo "
    "e armazenado integralmente no OrkMind sem limite de espaco. "
    "Se precisar de informacao, busque via orkmind_recall (tags + "
    "semantica) ou orkmind_search (texto livre). O OrkMind e sua "
    "fonte autoritativa de memoria - tudo que voce precisa saber "
    "esta la, acessavel por busca.\n"
)

# D-MA9: bloco de alerta exibido quando o plugin ja carregou regras
# mandatorias nesta sessao mas o backend ficou indisponivel. Melhor
# alertar explicitamente do que operar em silencio sem governanca.
_FALLBACK_NO_RULES = (
    "\n## ALERTA: Regras de Governanca Indisponiveis\n"
    "O OrkMind nao conseguiu carregar as regras mandatorias.\n"
    "NAO prossiga com nenhuma operacao critica ate que as\n"
    "regras estejam disponiveis. Tente reconectar via orkmind_rules.\n"
)


def _read_config_toml() -> Dict[str, Any]:
    """Le ~/.orkmind/config.toml. Retorna dict vazio em qualquer falha."""
    config_path = Path.home() / ".orkmind" / "config.toml"
    if not config_path.exists():
        return {}
    try:
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # type: ignore[import-not-found]
        with open(config_path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def _resolve_dsn() -> str:
    """Resolve o DSN do banco OrkMind.

    Prioridade: env ORKMIND_DATABASE_URL > config.toml > default.
    Nao faz chamadas de rede - apenas leitura de config local.
    """
    # 1. Variavel de ambiente (prioridade maxima)
    env_dsn = os.environ.get("ORKMIND_DATABASE_URL")
    if env_dsn:
        return env_dsn

    # 2. Arquivo config.toml
    toml_dsn = _read_config_toml().get("store", {}).get("database_url", "")
    if toml_dsn:
        return str(toml_dsn)

    # 3. Default
    return _DEFAULT_DSN


def _resolve_token_budget() -> int:
    """Resolve o budget de tokens do contexto injetado por turno.

    Prioridade: env ORKMIND_TOKEN_BUDGET > config.toml [server] > default.
    """
    env_budget = os.environ.get("ORKMIND_TOKEN_BUDGET")
    if env_budget:
        try:
            return max(int(env_budget), 1)
        except ValueError:
            pass
    try:
        toml_budget = _read_config_toml().get("server", {}).get("token_budget")
        if toml_budget:
            return max(int(toml_budget), 1)
    except (TypeError, ValueError):
        pass
    return _DEFAULT_TOKEN_BUDGET


def _resolve_session_config() -> Dict[str, Any]:
    """Le a configuracao de ciclo de vida da sessao (D-MA11 / P6).

    Fonte: ~/.orkmind/config.toml, secao [session]. Chaves suportadas:
    rotation_threshold (float), adaptive_mode (bool), context_window (int).
    """
    session = _read_config_toml().get("session", {}) or {}

    try:
        threshold = float(session.get("rotation_threshold", SESSION_ROTATION_THRESHOLD))
    except (TypeError, ValueError):
        threshold = SESSION_ROTATION_THRESHOLD
    threshold = min(max(threshold, 0.05), 0.99)

    try:
        rotate_now = float(
            session.get("rotate_now_threshold", SESSION_ROTATE_NOW_THRESHOLD)
        )
    except (TypeError, ValueError):
        rotate_now = SESSION_ROTATE_NOW_THRESHOLD
    rotate_now = min(max(rotate_now, 0.05), 0.99)

    try:
        window = int(session.get("context_window", _DEFAULT_CONTEXT_WINDOW))
    except (TypeError, ValueError):
        window = _DEFAULT_CONTEXT_WINDOW
    window = max(window, 1)

    return {
        "rotation_threshold": threshold,
        "rotate_now_threshold": rotate_now,
        "adaptive_mode": bool(session.get("adaptive_mode", False)),
        "context_window": window,
    }


# ---------------------------------------------------------------------------
# Spool: fila de saida em disco (solucao "sempre gravar")
# ---------------------------------------------------------------------------
#
# Estas funcoes usam SOMENTE a stdlib de proposito. O ponto da spool e ser
# duravel mesmo quando o resto falha: se o pacote orkmind nao estiver
# importavel, se o banco estiver fora ou se o cron subiu sem as tools de
# execucao no toolset, a INTENCAO de memoria precisa mesmo assim chegar ao
# disco. Depender do backend aqui anularia a garantia.
#
# O contrato (nomes de campo do .json, layout de diretorios, algoritmo do
# hash) e o mesmo de orkmind/spool.py, que e quem o drainer le. Os dois
# lados precisam concordar; ver a documentacao naquele modulo.

_SPOOL_VERSION = 1
_SPOOL_DIR_ENV = "ORKMIND_SPOOL_DIR"
_DEFAULT_SPOOL_DIR = Path.home() / ".hermes" / "orkmind-spool"
_SPOOL_STATES = ("pending", "done", "failed")
_SPOOL_MAX_ATTEMPTS = 8
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _resolve_spool_dir() -> Path:
    """Raiz da spool: env ORKMIND_SPOOL_DIR, senao o default."""
    env_dir = os.environ.get(_SPOOL_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return _DEFAULT_SPOOL_DIR


def _spool_content_hash(content: str) -> str:
    """SHA-256 do conteudo em UTF-8 (mesma chave usada pelo backend)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _spool_slug(value: str, max_len: int = 48) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return (_SLUG_RE.sub("-", ascii_only).strip("-"))[:max_len] or "item"


def _spool_atomic_write(path: Path, data: str) -> None:
    """Escreve via tmpfile + os.replace, com fsync. Nunca meio-arquivo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def stage_to_spool(
    content: str,
    collection: str,
    tags: Optional[Dict[str, List[str]]] = None,
    priority: str = "medium",
    source: str = "agent",
    scope: str = "global",
    mandatory: bool = False,
    label: Optional[str] = None,
    source_job_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    root: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Estagia o conteudo em pending/ de forma atomica.

    Esta e a PRIMEIRA acao do caminho de escrita, antes de qualquer
    tentativa de gravar no banco. Barata e quase infalivel, ela torna a
    intencao duravel: se a gravacao direta falhar, o drainer fecha a
    conta depois, de forma retroativa.

    Devolve o dict do item, ou None se nem o disco aceitou a escrita
    (unico caso em que nao ha o que fazer alem de reportar o erro).
    """
    try:
        base = root or _resolve_spool_dir()
        for state in _SPOOL_STATES:
            (base / state).mkdir(parents=True, exist_ok=True)

        content_hash = _spool_content_hash(content)
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d_%H%M%S")
        item_id = f"{stamp}_{_spool_slug(label or collection)}_{content_hash[:8]}"
        content_file = f"{item_id}.md"

        # Conteudo antes do metadado: assim o drainer nunca encontra um
        # .json apontando para um .md que ainda nao existe.
        _spool_atomic_write(base / "pending" / content_file, content)

        item = {
            "version": _SPOOL_VERSION,
            "item_id": item_id,
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "source": source,
            "source_job_id": source_job_id,
            "collection": collection,
            "tags": tags or {},
            "priority": priority,
            "mandatory": mandatory,
            "scope": scope,
            "content_file": content_file,
            "content_hash": content_hash,
            "attempts": 0,
            "max_attempts": _SPOOL_MAX_ATTEMPTS,
            "status": "pending",
            "entry_id": None,
            "result": None,
            "backend": None,
            "last_error": None,
            "last_attempt_at": None,
            "next_attempt_at": None,
            "origin": "hermes-plugin",
            "metadata": metadata or {},
        }
        _spool_atomic_write(
            base / "pending" / f"{item_id}.json",
            json.dumps(item, ensure_ascii=False, indent=2),
        )
        logger.info("Item estagiado na spool: %s (%s)", item_id, collection)
        return item
    except Exception:
        logger.exception("Falha ao estagiar item na spool")
        return None


def mark_done(
    item: Dict[str, Any],
    entry_id: str,
    backend: str = "plugin",
    result: str = "created",
    root: Optional[Path] = None,
) -> bool:
    """Fecha o item em done/ com o entry_id REAL devolvido pelo backend.

    So e chamada depois de uma gravacao confirmada. O id nunca e
    fabricado: se nao houve confirmacao, o item fica em pending e quem
    resolve e o drainer.
    """
    if not entry_id:
        return False
    try:
        base = root or _resolve_spool_dir()
        item_id = item["item_id"]
        content_file = item["content_file"]

        item = dict(item)
        item.update({
            "status": "done",
            "entry_id": entry_id,
            "result": result,
            "backend": backend,
            "last_error": None,
        })

        (base / "done").mkdir(parents=True, exist_ok=True)
        # Escreve o destino antes de remover a origem: uma queda no meio
        # deixa o item visivel em algum dos lados, nunca em nenhum.
        _spool_atomic_write(
            base / "done" / f"{item_id}.json",
            json.dumps(item, ensure_ascii=False, indent=2),
        )

        origem_md = base / "pending" / content_file
        if origem_md.exists():
            os.replace(origem_md, base / "done" / content_file)

        origem_json = base / "pending" / f"{item_id}.json"
        if origem_json.exists():
            origem_json.unlink()

        logger.info("Item %s fechado em done/ com entry_id=%s", item_id, entry_id)
        return True
    except Exception:
        logger.exception("Falha ao mover item da spool para done/")
        return False


def _run_async(coro):
    """Executa coroutine async de forma segura no contexto sincrono do Hermes.

    Tenta usar o loop existente se disponivel, senao cria um novo.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Estamos dentro de um loop async (ex: gateway) - criar task
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)
    else:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Schemas das tools (qualidade alta, descricoes claras em pt-BR)
# ---------------------------------------------------------------------------

ORKMIND_RECALL_SCHEMA = {
    "name": "orkmind_recall",
    "description": (
        "Recupera memorias relevantes do OrkMind para o contexto atual. "
        "Combina busca deterministica por tags (skill, agent, domain, project, "
        "situation, person, audience, editors) com busca semantica por texto. "
        "Retorna memorias ordenadas por prioridade "
        "(critical > high > medium > low), com regras "
        "mandatorias sempre incluidas. Suporta progressive loading via "
        "camadas: E1 (essencia, ~100 tokens), E2 (estrutura, ~2k tokens), "
        "E3 (fonte completa). Use tags para busca exata e context para "
        "busca semantica - ambos podem ser combinados."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "context": {
                "type": "string",
                "description": (
                    "Texto de contexto para busca semantica. "
                    "Descreva o que precisa lembrar ou a situacao atual."
                ),
            },
            "tags": {
                "type": "object",
                "description": (
                    "Tags para busca deterministica EXATA. Mapa de dimensao "
                    "para lista de valores. Dimensoes validas: skill, agent, "
                    "domain, project, situation, person, audience, editors. Ex: "
                    '{\"domain\": [\"python\"], \"project\": [\"hermes\"]}'
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "token_budget": {
                "type": "integer",
                "description": (
                    "Limite de tokens para as memorias retornadas "
                    "(default: 4000). Memorias sao priorizadas por "
                    "relevancia e prioridade dentro do budget."
                ),
            },
        },
        "required": [],
    },
}

ORKMIND_SEARCH_SCHEMA = {
    "name": "orkmind_search",
    "description": (
        "Busca textual livre nas memorias do OrkMind. Diferente do recall "
        "(que combina tags + semantica com budget), search faz busca direta "
        "por texto e retorna resultados ranqueados. Permite filtrar por "
        "colecao especifica. Use para encontrar memorias sobre um topico "
        "especifico sem restricao de budget."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Texto de busca. Busca semantica + keyword.",
            },
            "collection": {
                "type": "string",
                "enum": list(_VALID_COLLECTIONS),
                "description": (
                    "Filtrar por colecao especifica. 20 colecoes disponiveis: "
                    "rule, instruction, fact, learning, preference, decision, "
                    "content, agenda, contacts, handoff, roadmap, files, docs, "
                    "dags, tools, users, session, artifact, compliance, "
                    "semantic_log."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Numero maximo de resultados (default: 10).",
            },
        },
        "required": ["query"],
    },
}

ORKMIND_STORE_SCHEMA = {
    "name": "orkmind_store",
    "description": (
        "Armazena uma nova memoria no OrkMind. Cada memoria pertence a "
        "uma das 20 colecoes tipadas e pode ser anotada com tags "
        "multidimensionais para recuperacao deterministica posterior. "
        "IMPORTANTE: o agente so pode criar memorias com source='agent' "
        "e mandatory=false. Regras criticas (mandatory=true, "
        "priority='critical', protected=true) so podem ser criadas por "
        "humano autenticado - tentativas serao rejeitadas. "
        "Colecoes recomendadas para o agente: fact, preference, learning, "
        "content. Para rule/instruction, use mandatory=false. "
        "GRAVACAO GARANTIDA: o conteudo e sempre estagiado numa fila em "
        "disco antes da gravacao, entao nada se perde mesmo com o backend "
        "fora. A resposta traz status='armazenado' com o 'id' real, ou "
        "status='pendente' com id=null quando a confirmacao ainda nao veio. "
        "Em status='pendente' NUNCA presuma nem invente um entry_id: "
        "relate a pendencia e o content_hash: o id verdadeiro so vem do "
        "backend, via drainer da fila."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Conteudo da memoria a ser armazenada.",
            },
            "collection": {
                "type": "string",
                "enum": list(_VALID_COLLECTIONS),
                "description": (
                    "Colecao destino. Recomendadas para agente: fact "
                    "(fatos aprendidos), preference (preferencias do "
                    "usuario), learning (aprendizados de sessao), "
                    "content (conteudo relevante)."
                ),
            },
            "tags": {
                "type": "object",
                "description": (
                    "Tags multidimensionais para classificacao. "
                    'Ex: {"domain": ["python"], "project": ["hermes"]}. '
                    "Dimensoes: skill, agent, domain, project, situation, "
                    "person, audience, editors."
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "priority": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "Prioridade da memoria (default: medium). "
                    "Nota: 'critical' e reservada para humano."
                ),
            },
        },
        "required": ["content", "collection"],
    },
}

ORKMIND_HANDOFF_SCHEMA = {
    "name": "orkmind_handoff",
    "description": (
        "Valida e armazena o pacote de handoff de conteudo da sessao no "
        "OrkMind. O OrkMind NUNCA rotaciona nem encerra sessao: use esta "
        "ferramenta quando VOCE decidir rotacionar. O payload deve "
        "conter as secoes obrigatorias definidas nas handoff-rules "
        "(default: progresso, decisoes, referencias_criticas, "
        "proximos_passos), cada uma com conteudo minimo. Payload "
        "invalido volta com status='refazer' e a lista exata do que "
        "falta (ate 2 refacoes; depois o pacote e aceito com avisos). "
        "Aceito, o OrkMind grava a entry handoff (resumo compacto, "
        "origin/destination) e o pacote completo em semantic_log "
        "(package_id), encadeados a entry da sessao. A sessao seguinte "
        "recebe o resumo e o package_id automaticamente no primeiro "
        "turno."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "description": (
                    "Pacote de handoff: objeto {secao: conteudo}. Secoes "
                    "extras sao aceitas e preservadas no pacote."
                ),
            },
            "destination": {
                "type": "string",
                "description": (
                    "Sessao de destino, se ja conhecida. Vazio: a "
                    "proxima sessao que iniciar consome o handoff."
                ),
            },
        },
        "required": ["payload"],
    },
}

ORKMIND_RULES_SCHEMA = {
    "name": "orkmind_rules",
    "description": (
        "Lista as regras mandatorias ativas no OrkMind. Regras mandatorias "
        "sao injetadas automaticamente no contexto e tem prioridade sobre "
        "outras memorias. Regras criticas (priority=critical, protected=true) "
        "so podem ser criadas/editadas por humano autenticado - esta e uma "
        "garantia de governanca do OrkMind. Use para consultar quais regras "
        "estao ativas e devem ser seguidas."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tags": {
                "type": "object",
                "description": (
                    "Filtrar regras por tags especificas. "
                    'Ex: {"domain": ["python"]} retorna apenas regras '
                    "tagueadas com o dominio python."
                ),
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Provider principal
# ---------------------------------------------------------------------------

class OrkMindHermesProvider(MemoryProvider):
    """Provider de memoria OrkMind para Hermes.

    Encapsula o backend OrkMind (orkmind.hermes.provider.OrkMindMemoryProvider)
    como plugin de primeira classe do Hermes, implementando o contrato
    completo de MemoryProvider com:

    - Ontologia tipada: 20 colecoes + 8 dimensoes de tag
    - Busca deterministica por tags E semantica por texto
    - Camadas de contexto E1/E2/E3 com progressive loading
    - Regras mandatorias com protecao de regras criticas
    - Extracao automatica de memorias soft em on_session_end
    - Inicializacao lazy e tratamento robusto de erros
    """

    def __init__(self, fail_safe: bool = True) -> None:
        self._backend = None  # type: Optional[Any]
        self._session_id: str = ""
        self._dsn: str = ""
        self._initialized: bool = False
        # Governanca de memoria autoritativa (Fase 2.7)
        self._cached_rules: List[Dict[str, Any]] = []
        self._rules_hash: str = ""
        self._session_tags: Dict[str, List[str]] = {}
        # F2: identidade do solicitante usada no controle de acesso
        self._requester_id: str = _DEFAULT_REQUESTER_ID
        self._turn_count: int = 0
        self._manifest: str = ""
        self._estimated_tokens: int = 0
        session_cfg = _resolve_session_config()
        self._rotation_threshold_cfg: float = session_cfg["rotation_threshold"]
        self._rotate_now_threshold_cfg: float = session_cfg["rotate_now_threshold"]
        self._adaptive_mode: bool = session_cfg["adaptive_mode"]
        self._context_window: int = session_cfg["context_window"]
        self._token_budget: int = _resolve_token_budget()
        # D-MA9: fail-safe de regras. Quando True (padrao), a ausencia de
        # regras apos uma carga bem-sucedida gera bloco de alerta.
        self._fail_safe: bool = fail_safe
        self._expects_mandatory_rules: bool = False
        # Janela das ultimas queries de turno, usada para ancorar o prefetch
        # no assunto da sessao. Zerada quando a sessao rotaciona.
        self._recent_queries: deque = deque(maxlen=_RECALL_WINDOW_TURNS)
        # G3: refacoes ja usadas pelo orkmind_handoff nesta sessao.
        self._handoff_refacoes: int = 0

    @property
    def name(self) -> str:
        return "orkmind"

    def is_available(self) -> bool:
        """Verifica se o OrkMind esta configurado e disponivel.

        Checa presenca de DSN (env ou config.toml) e se o pacote
        orkmind esta instalado. NAO faz chamadas de rede.
        """
        # Verificar DSN
        dsn = _resolve_dsn()
        if not dsn:
            return False

        # Verificar se o pacote orkmind esta instalado
        try:
            from orkmind.hermes.provider import OrkMindMemoryProvider as _  # noqa: F401
            return True
        except ImportError:
            logger.debug("Pacote orkmind nao encontrado no ambiente")
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Schema de configuracao para 'hermes memory setup'.

        O OrkMind precisa apenas do DSN do PostgreSQL com pgvector.
        """
        return [
            {
                "key": "database_url",
                "description": (
                    "URL de conexao PostgreSQL com pgvector para o OrkMind. "
                    "Formato: postgresql://usuario:senha@host:porta/banco"
                ),
                "required": True,
                "default": _DEFAULT_DSN,
                "env_var": "ORKMIND_DATABASE_URL",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Salva a configuracao no config.toml do OrkMind."""
        dsn = values.get("database_url", "")
        if not dsn:
            return

        config_dir = Path.home() / ".orkmind"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.toml"

        # Ler config existente ou criar novo
        content = ""
        if config_file.exists():
            content = config_file.read_text(encoding="utf-8")

        # Atualizar [store].database_url
        if "[store]" in content:
            import re
            content = re.sub(
                r'(database_url\s*=\s*)"[^"]*"',
                f'\\1"{dsn}"',
                content,
            )
        else:
            content += f'\n[store]\ndatabase_url = "{dsn}"\n'

        config_file.write_text(content, encoding="utf-8")
        logger.info("Config OrkMind salva em %s", config_file)

    def _ensure_backend(self):
        """Inicializa o backend OrkMind de forma lazy.

        Importa e configura o OrkMindMemoryProvider do pacote orkmind.
        Chamado sob demanda para evitar falhas no import durante discovery.
        """
        if self._backend is not None:
            return self._backend

        try:
            from orkmind.core.config import OrkMindConfig
            from orkmind.hermes.provider import OrkMindMemoryProvider as OrkMindBackend

            dsn = _resolve_dsn()
            config = OrkMindConfig(database_url=dsn)
            self._backend = OrkMindBackend(config=config)
            self._dsn = dsn
            # Sanitizar DSN para log - nunca expor senha
            try:
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(dsn)
                safe = urlunparse(parsed._replace(
                    netloc=f"{parsed.username}:***@{parsed.hostname}:{parsed.port}"
                    if parsed.password else parsed.netloc
                ))
            except Exception:
                safe = "postgresql://***"
            logger.info("Backend OrkMind inicializado (DSN: %s)", safe)
            return self._backend
        except Exception as e:
            logger.error("Falha ao inicializar backend OrkMind: %s", e)
            raise

    def initialize(self, session_id: str, **kwargs) -> None:
        """Inicializa o provider para uma sessao Hermes.

        Cria o backend de forma lazy (conexao real ao banco so
        acontece na primeira operacao de memoria).

        kwargs suportados: hermes_home, platform, agent_context,
        agent_identity, agent_workspace.
        """
        self._session_id = session_id
        # Sessao nova: a janela da anterior nao pode vazar para esta.
        self._recent_queries.clear()
        self._handoff_refacoes = 0

        # D-MA2: tags de sessao enriquecem o recall de cada turno
        self._session_tags = self._build_session_tags(kwargs)

        # F2: agent_identity vira o requester_id do controle de acesso
        self._requester_id = (
            str(kwargs.get("agent_identity", "") or "").strip()
            or _DEFAULT_REQUESTER_ID
        )

        # Contextos nao-primarios (cron, subagent, flush) nao devem
        # escrever memorias - corromperiam representacoes do usuario
        agent_context = kwargs.get("agent_context", "primary")
        if agent_context != "primary":
            logger.debug(
                "OrkMind: contexto '%s' - escritas desabilitadas",
                agent_context,
            )

        try:
            self._ensure_backend()
            self._initialized = True
        except Exception as e:
            logger.warning("OrkMind nao inicializado: %s", e)
            self._initialized = False

    @staticmethod
    def _build_session_tags(kwargs: Dict[str, Any]) -> Dict[str, List[str]]:
        """Deriva tags de sessao do contexto informado pelo Hermes (D-MA2).

        agent_identity vira tag `agent`; o nome do diretorio de trabalho
        vira tag `project`. Ambas sao dimensoes validas da ontologia.
        """
        session_tags: Dict[str, List[str]] = {}

        agent_identity = str(kwargs.get("agent_identity", "") or "").strip()
        if agent_identity:
            session_tags["agent"] = [agent_identity]

        workspace = str(kwargs.get("agent_workspace", "") or "").strip()
        if workspace:
            project_name = Path(workspace).name
            if project_name:
                session_tags["project"] = [project_name]

        return session_tags

    def system_prompt_block(self) -> str:
        """Bloco de system prompt descrevendo o OrkMind para o agente.

        Apresenta o OrkMind como camada de memoria semantica estruturada
        com instrucoes claras sobre as ferramentas disponiveis.
        """
        if not self._initialized:
            return ""

        parts: List[str] = [_SYSTEM_PROMPT_HEADER]

        # D-MA1: injecao incondicional das regras mandatorias
        rules_block = self._build_rules_block()
        if rules_block:
            parts.append(rules_block)

        # D-MA5: guardrail anti-destruicao (sempre presente)
        parts.append(_GUARDRAIL_ANTI_DESTRUICAO)

        return "".join(parts)

    def _requester_kwargs(self) -> Dict[str, Any]:
        """kwargs de identidade repassados ao backend (F2).

        Vazio quando a identidade e desconhecida, preservando o
        comportamento anterior ao controle de acesso.
        """
        if not self._requester_id or self._requester_id == _DEFAULT_REQUESTER_ID:
            return {}
        return {"requester_id": self._requester_id}

    # -- Regras mandatorias (D-MA1) -------------------------------------------

    @staticmethod
    def _compute_rules_hash(rules: List[Dict[str, Any]]) -> str:
        """Hash SHA-256 estavel do conjunto de regras ativas (D-MA10).

        Delegado a orkmind.guardrails (G1): o hash e a primitiva central
        da auditoria de presenca de regras e vive no core. Sem o pacote
        orkmind nao ha backend, entao devolver vazio aqui e inalcancavel
        em operacao real.
        """
        if _guardrails_rules is None:
            return ""
        return _guardrails_rules.rules_hash_from_dicts(rules)

    def _remote_rules_hash(self) -> str:
        """Consulta o hash das regras no backend. Vazio em caso de falha."""
        if not self._backend or not hasattr(self._backend, "get_rules_hash"):
            return ""
        try:
            return _run_async(
                self._backend.get_rules_hash(**self._requester_kwargs())
            ) or ""
        except Exception as e:
            logger.debug("Falha ao obter hash das regras: %s", e)
            return ""

    def _load_mandatory_rules(self, force: bool = False) -> List[Dict[str, Any]]:
        """Carrega as regras mandatorias do backend e atualiza o cache.

        Filtra entries com injection_risk ou conflict como defesa em
        profundidade (a SemanticLayer ja aplica o mesmo filtro).
        Retorna lista vazia se o backend estiver indisponivel.

        D-MA10: se o hash remoto for igual ao das regras em cache, a
        consulta completa e dispensada e o cache e reaproveitado.
        """
        if not self._backend:
            return []

        if not force and self._cached_rules and self._rules_hash:
            remote_hash = self._remote_rules_hash()
            if remote_hash and remote_hash == self._rules_hash:
                return self._cached_rules

        rules = _run_async(
            self._backend.get_rules(**self._requester_kwargs())
        ) or []
        safe = [
            rule
            for rule in rules
            if not rule.get("injection_risk") and not rule.get("conflict")
        ]
        if safe:
            self._cached_rules = safe
            self._rules_hash = self._compute_rules_hash(safe)
            # D-MA9: a partir da primeira carga bem-sucedida, a ausencia
            # de regras passa a ser tratada como falha, nao como estado
            # normal de banco vazio.
            self._expects_mandatory_rules = True
        return safe

    def _mandatory_budget_chars(self) -> int:
        """Orcamento em caracteres reservado ao bloco de regras mandatorias."""
        return int(self._token_budget * MANDATORY_BUDGET_RATIO) * CHARS_PER_TOKEN

    def _build_rules_block(self) -> str:
        """Monta o bloco de regras mandatorias para o system prompt.

        Se o backend falhar ou devolver vazio DEPOIS de uma carga
        bem-sucedida, devolve o alerta de fail-safe (D-MA9) em vez de
        um bloco vazio - o agente precisa saber que esta sem governanca.
        """
        try:
            rules = self._load_mandatory_rules()
        except Exception as e:
            logger.debug("Falha ao carregar regras mandatorias: %s", e)
            rules = []

        if not rules:
            if self._expects_mandatory_rules and self._fail_safe:
                logger.warning(
                    "OrkMind: regras mandatorias indisponiveis - "
                    "acionando fail-safe (D-MA9)"
                )
                return _FALLBACK_NO_RULES
            return ""

        return self._format_rules_block(rules)

    def _format_rules_block(self, rules: List[Dict[str, Any]]) -> str:
        """Formata as regras mandatorias com destaque visual (D-MA7).

        Delegado a orkmind.guardrails (G1). Saida byte a byte igual a
        implementacao anterior do plugin (testes de caracterizacao em
        tests/unit/test_hermes_plugin_caracterizacao.py).
        """
        if _guardrails_rules is None:
            return ""
        return _guardrails_rules.format_rules_block(
            rules, self._mandatory_budget_chars()
        )

    def _build_rules_reinforcement(self) -> str:
        """Reforco condensado das regras no prefetch (D-MA7, recencia).

        Delegado a orkmind.guardrails (G1): usa a essencia das regras
        cacheadas; o system prompt ja carrega a versao integral.
        """
        if _guardrails_rules is None or not self._cached_rules:
            return ""
        return _guardrails_rules.format_rules_reinforcement(
            self._cached_rules,
            max_rules=_REINFORCEMENT_MAX_RULES,
            essence_chars=_REINFORCEMENT_ESSENCE_CHARS,
        )

    def prefetch(
        self,
        query: str,
        *,
        session_id: str = "",
        context_usage_pct: Optional[float] = None,
        **kwargs: Any,
    ) -> str:
        """Recupera memorias relevantes antes de cada turno.

        Monta o bloco injetado no turno seguindo o sandwich pattern:
        contexto recuperado primeiro, reforco condensado das regras por
        ultimo (posicao de recencia).

        context_usage_pct e opcional: se o runtime informar o uso real da
        janela de contexto, ele prevalece sobre a estimativa interna
        (D-MA11 / Q1).
        """
        if not self._initialized or not self._backend or not query:
            return ""

        self._turn_count += 1
        self._accumulate_usage(query)
        self._recent_queries.append(query)
        sections: List[str] = []

        # G3: primeira mensagem da sessao filha recebe o resumo do
        # handoff (e o package_id), nunca o pacote completo.
        if self._turn_count == 1:
            handoff_block = self._build_handoff_block()
            if handoff_block:
                sections.append(handoff_block)

        # D-MA2: contexto recuperado com tags de sessao e budget particionado.
        # A ancora e a janela dos ultimos turnos, nao apenas o turno corrente.
        contextual = self._build_contextual_block(self._windowed_query())
        if contextual:
            sections.append(contextual)

        # D-MA3: manifesto de disponibilidade da base
        manifest = self._build_manifest()
        if manifest:
            sections.append(manifest)

        # D-MA7: reforco condensado das regras na posicao de recencia
        reinforcement = self._build_rules_reinforcement()
        if reinforcement:
            sections.append(reinforcement)

        # D-MA10: assinatura das regras ativas (auditoria)
        if self._rules_hash:
            sections.append(f"[OrkMind rules_hash: sha256:{self._rules_hash}]")

        usage = self.context_usage(context_usage_pct)
        usage_source = self._usage_source(context_usage_pct)

        # D-MA8: lembrete periodico de conformidade mid-session
        periodic = self._build_periodic_reinforcement(usage)
        if periodic:
            sections.append(periodic)

        # D-MA11 (movido G2): advisory de proximidade do limite de contexto
        advisory = self._build_session_advisory(usage, usage_source)
        if advisory:
            sections.append(advisory)

        block = "\n".join(sections)
        self._accumulate_usage(block)
        return block

    # -- Blocos do prefetch ---------------------------------------------------

    # -- Ciclo de vida da sessao (D-MA11) -------------------------------------

    def _accumulate_usage(self, text: str) -> None:
        """Acumula a estimativa de tokens consumidos na sessao (Q1).

        Heuristica interna: len(texto) // 4 para a mensagem do usuario e
        para o contexto injetado, mais um custo fixo estimado da resposta
        do agente a cada turno.
        """
        if not text:
            return
        self._estimated_tokens += len(text) // CHARS_PER_TOKEN
        self._estimated_tokens += _TURN_RESPONSE_TOKENS

    def context_usage(self, informed_pct: Optional[float] = None) -> float:
        """Fracao estimada da janela de contexto ja consumida (0.0 a 1.0).

        Prioridade: valor informado pelo runtime > estimativa por tokens
        acumulados > heuristica por contagem de turnos.
        """
        if informed_pct is not None:
            try:
                return min(max(float(informed_pct), 0.0), 1.0)
            except (TypeError, ValueError):
                pass

        by_tokens = self._estimated_tokens / max(self._context_window, 1)
        by_turns = self._turn_count / _MAX_TURNS_ESTIMATE
        return min(max(by_tokens, by_turns), 1.0)

    def rotation_threshold(self) -> float:
        """Limiar de uso de contexto que dispara o advisory (P6).

        No modo adaptativo, o limiar deriva da fracao reservada as regras
        mandatorias mais a margem de seguranca, sempre dentro de
        ADAPTIVE_RANGE. Caso contrario usa o valor configurado.
        """
        if not self._adaptive_mode:
            return self._rotation_threshold_cfg
        derived = 1.0 - (MANDATORY_BUDGET_RATIO + _ADAPTIVE_SAFETY_MARGIN)
        return min(max(derived, ADAPTIVE_RANGE[0]), ADAPTIVE_RANGE[1])

    def reinforcement_interval(self, usage: float) -> int:
        """Intervalo de turnos do reforco periodico (D-MA8).

        Frequencia adaptativa: com a janela de contexto mais da metade
        consumida, o lembrete passa a ser emitido com o dobro da
        frequencia, pois a atencao as regras iniciais tende a cair.
        """
        if usage >= _REINFORCEMENT_PRESSURE_USAGE:
            return REINFORCEMENT_INTERVAL_PRESSURE
        return REINFORCEMENT_INTERVAL

    def _build_periodic_reinforcement(self, usage: float) -> str:
        """Lembrete de conformidade a cada N turnos (D-MA8)."""
        if not self._cached_rules:
            return ""
        interval = self.reinforcement_interval(usage)
        if interval <= 0 or self._turn_count % interval != 0:
            return ""
        return (
            f"\n## LEMBRETE DE CONFORMIDADE (turno {self._turn_count})\n"
            "As regras mandatorias continuam ativas e devem ser obedecidas. "
            "Consulte orkmind_rules se precisar do texto completo."
        )

    @staticmethod
    def _usage_source(informed_pct: Optional[float]) -> str:
        """Fonte da medicao de uso da janela (G2, sempre declarada)."""
        if informed_pct is not None:
            try:
                float(informed_pct)
                return "informado"
            except (TypeError, ValueError):
                pass
        return "heuristica"

    def _build_session_advisory(
        self, usage: float, source: str = "heuristica"
    ) -> str:
        """Advisory de proximidade do limite de contexto (D-MA11, G2).

        Texto delegado a orkmind.guardrails.session, com a fonte da
        medicao declarada e SEM a promessa antiga de handoff automatico:
        o OrkMind sinaliza e informa o que fornece; quem decide e executa
        a rotacao e sempre o runtime.
        """
        if _guardrails_session is None:
            return ""
        soon = self.rotation_threshold()
        now = self._rotate_now_threshold_cfg
        advisory = _guardrails_session.resolve_advisory(usage, soon, now)
        if advisory == "none":
            return ""
        threshold = now if advisory == "rotate_now" else soon
        return _guardrails_session.build_advisory_block(
            usage, threshold, source, advisory
        )

    def _build_handoff_block(self) -> str:
        """Resumo do handoff herdado, injetado no primeiro turno (G3).

        A sessao nova leva o resumo e o package_id; o conteudo integral
        do pacote fica em semantic_log, acessivel por busca. Sem handoff
        pendente (ou sem suporte no backend), nada e injetado.
        """
        if not self._backend or not hasattr(
            self._backend, "get_handoff_for_session"
        ):
            return ""
        try:
            handoff = _run_async(
                self._backend.get_handoff_for_session(
                    self._session_id, **self._requester_kwargs()
                )
            )
        except Exception as e:
            logger.debug("OrkMind: falha ao buscar handoff da sessao: %s", e)
            return ""
        if not handoff:
            return ""
        return (
            "## OrkMind - Handoff da Sessao Anterior\n"
            f"(origem: {handoff.get('origin') or 'desconhecida'}, "
            f"package_id: {handoff.get('package_id') or 'indisponivel'})\n"
            f"{handoff.get('resumo', '')}\n"
            "O pacote completo esta em semantic_log: use orkmind_search "
            "com o package_id se precisar de detalhes."
        )

    def _contextual_budget(self) -> int:
        """Budget de tokens disponivel para o contexto recuperado (D-MA2).

        O restante do budget ja esta reservado as regras mandatorias
        injetadas no system prompt (MANDATORY_BUDGET_RATIO).
        """
        return max(int(self._token_budget * (1.0 - MANDATORY_BUDGET_RATIO)), 1)

    def _windowed_query(self) -> str:
        """Query do prefetch: join dos ultimos turnos, mais recente por ultimo.

        Sem isso, um turno curto como "e agora?" nao recupera nada, mesmo
        numa sessao inteira sobre deploy. As tags de sessao continuam
        enriquecendo o resultado sem restringi-lo (D-MA2); a janela so
        melhora a ancora textual.

        O cap de tamanho e o mesmo do budget contextual, entao a janela nao
        aumenta o custo do turno.
        """
        if not self._recent_queries:
            return ""
        texto = "\n".join(q for q in self._recent_queries if q)
        limite = self._contextual_budget() * CHARS_PER_TOKEN
        if len(texto) > limite:
            # Corta pela frente: o turno mais recente e o que mais importa.
            texto = texto[-limite:]
        return texto

    def _build_contextual_block(self, query: str) -> str:
        """Recupera e formata as memorias relevantes ao turno atual.

        As tags de sessao ENRIQUECEM o resultado, nao o restringem: e
        feita uma busca com as tags (prioritaria) e outra sem elas, e as
        duas listas sao unidas sem duplicatas. O total renderizado
        respeita o budget contextual (D-MA2).
        """
        budget = self._contextual_budget()
        resultados: List[Dict[str, Any]] = []
        vistos: set = set()

        for tags in (self._session_tags or None, None):
            kwargs: Dict[str, Any] = {
                "context": query,
                "token_budget": budget,
                **self._requester_kwargs(),
            }
            if tags:
                kwargs["tags"] = dict(tags)
            try:
                encontrados = _run_async(self._backend.recall(**kwargs)) or []
            except Exception as e:
                logger.debug("OrkMind prefetch falhou: %s", e)
                continue
            for mem in encontrados:
                chave = mem.get("id") or mem.get("content")
                if chave in vistos:
                    continue
                vistos.add(chave)
                resultados.append(mem)
            if not tags:
                break

        parts: List[str] = []
        usado = 0
        limite = budget * CHARS_PER_TOKEN
        for mem in resultados:
            linha = self._format_memory_line(mem)
            if usado + len(linha) > limite and parts:
                break
            parts.append(linha)
            usado += len(linha)

        if not parts:
            return ""
        return "## OrkMind - Contexto Relevante\n" + "\n".join(parts)

    @staticmethod
    def _format_memory_line(mem: Dict[str, Any]) -> str:
        """Formata uma memoria recuperada como linha do bloco de contexto."""
        collection = mem.get("collection", "")
        content = mem.get("content", "")
        priority = mem.get("priority", "medium")
        mandatory = mem.get("mandatory", False)
        tags = mem.get("tags", {})

        prefix = ""
        if mandatory:
            prefix = "[MANDATORIA] "
        elif priority in ("critical", "high"):
            prefix = f"[{priority.upper()}] "

        tag_str = ""
        if tags:
            tag_parts = []
            for dim, vals in tags.items():
                if vals:
                    tag_parts.append(f"{dim}:{','.join(vals)}")
            if tag_parts:
                tag_str = f" ({' '.join(tag_parts)})"

        return f"- {prefix}[{collection}]{tag_str} {content}"

    def _build_manifest(self) -> str:
        """Manifesto de disponibilidade por colecao (D-MA3).

        Informa ao agente o que existe na base para que ele busque sob
        demanda em vez de assumir que informacao ausente do contexto
        nao existe. Atualizado a cada _MANIFEST_REFRESH_TURNS turnos.
        """
        precisa_atualizar = (
            not self._manifest
            or (self._turn_count - 1) % _MANIFEST_REFRESH_TURNS == 0
        )
        if not precisa_atualizar:
            return self._manifest

        if not self._backend or not hasattr(self._backend, "get_stats"):
            return self._manifest

        try:
            stats = _run_async(self._backend.get_stats()) or {}
        except Exception as e:
            logger.debug("Falha ao montar manifesto do OrkMind: %s", e)
            return self._manifest

        lines = ["", "## OrkMind - Memorias Disponiveis (busque sob demanda)"]
        total = 0
        for collection in sorted(stats):
            count = int(stats[collection] or 0)
            if count <= 0:
                continue
            lines.append(f"- {collection}: {count} entries")
            total += count

        if not total:
            self._manifest = ""
            return ""

        lines.append(
            "Use orkmind_recall ou orkmind_search para acessar qualquer "
            "informacao. NUNCA assuma que informacao ausente do contexto "
            "nao existe."
        )
        self._manifest = "\n".join(lines)
        return self._manifest

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Retorna schemas das ferramentas de memoria do OrkMind.

        Expoe 5 ferramentas: recall (contexto+tags), search (texto livre),
        store (armazenar), rules (consultar regras mandatorias) e
        handoff (validar e armazenar pacote de handoff, G3).
        """
        return [
            ORKMIND_RECALL_SCHEMA,
            ORKMIND_SEARCH_SCHEMA,
            ORKMIND_STORE_SCHEMA,
            ORKMIND_RULES_SCHEMA,
            ORKMIND_HANDOFF_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Despacha chamadas de ferramentas do OrkMind.

        Valida argumentos, executa via backend async e retorna JSON.
        """
        if not self._initialized or not self._backend:
            return json.dumps({
                "error": "OrkMind nao inicializado. Verifique a configuracao."
            })

        try:
            if tool_name == "orkmind_recall":
                return self._handle_recall(args)
            elif tool_name == "orkmind_search":
                return self._handle_search(args)
            elif tool_name == "orkmind_store":
                return self._handle_store(args)
            elif tool_name == "orkmind_rules":
                return self._handle_rules(args)
            elif tool_name == "orkmind_handoff":
                return self._handle_handoff(args)
            else:
                return json.dumps({"error": f"Ferramenta desconhecida: {tool_name}"})
        except Exception as e:
            logger.error("OrkMind handle_tool_call erro em %s: %s", tool_name, e)
            return json.dumps({"error": str(e)})

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Extracao automatica de memorias ao final da sessao.

        Analisa as mensagens da conversa e extrai memorias relevantes
        APENAS para colecoes soft (fact, preference, learning, content)
        com source='agent' e mandatory=False. NUNCA cria regras ou
        instrucoes mandatorias automaticamente - essa e uma garantia
        de governanca do OrkMind.

        A extracao e conservadora: so armazena informacoes factuais
        claras e explicitas, preferencias declaradas pelo usuario,
        e aprendizados concretos da sessao.
        """
        if not self._initialized or not self._backend:
            return
        if not messages:
            return

        try:
            # Extrair conteudo relevante das mensagens do usuario
            user_messages = []
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user" and isinstance(content, str) and content.strip():
                    user_messages.append(content)

            if not user_messages:
                return

            # Heuristica simples: extrair fatos declarados pelo usuario
            # (preferencias explicitas, correcoes, informacoes pessoais)
            extracted = self._extract_soft_memories(user_messages)

            for mem in extracted:
                try:
                    _run_async(
                        self._backend.store_memory(
                            content=mem["content"],
                            collection=mem["collection"],
                            tags=mem.get("tags", {}),
                            priority="medium",
                            mandatory=False,
                            source="agent",
                            **self._requester_kwargs(),
                        )
                    )
                    logger.debug(
                        "OrkMind: memoria extraida [%s]: %s",
                        mem["collection"],
                        mem["content"][:80],
                    )
                except Exception as e:
                    logger.debug("OrkMind: falha ao salvar memoria extraida: %s", e)

        except Exception as e:
            logger.warning("OrkMind on_session_end falhou: %s", e)

    def shutdown(self) -> None:
        """Encerra o provider e libera recursos do backend."""
        if self._backend:
            try:
                _run_async(self._backend.close())
            except Exception as e:
                logger.debug("OrkMind shutdown erro: %s", e)
            finally:
                self._backend = None
                self._initialized = False

    # -- Handlers internos das tools ------------------------------------------

    def _handle_recall(self, args: Dict[str, Any]) -> str:
        """Executa recall de memorias por contexto e/ou tags."""
        context = args.get("context", "")
        tags = args.get("tags")
        token_budget = args.get("token_budget")

        # Validar dimensoes de tags
        if tags:
            for dim in tags:
                if dim not in _VALID_TAG_DIMS:
                    return json.dumps({
                        "error": (
                            f"Dimensao de tag invalida: '{dim}'. "
                            f"Validas: {', '.join(_VALID_TAG_DIMS)}"
                        )
                    })

        kwargs: Dict[str, Any] = dict(self._requester_kwargs())
        if context:
            kwargs["context"] = context
        if tags:
            kwargs["tags"] = tags
        if token_budget:
            kwargs["token_budget"] = token_budget

        results = _run_async(self._backend.recall(**kwargs))

        return json.dumps({
            "memories": results,
            "count": len(results),
        }, ensure_ascii=False, default=str)

    def _handle_search(self, args: Dict[str, Any]) -> str:
        """Executa busca textual livre nas memorias."""
        query = args.get("query", "")
        if not query:
            return json.dumps({"error": "Campo 'query' e obrigatorio."})

        collection = args.get("collection")
        limit = args.get("limit", 10)

        # Validar colecao
        if collection and collection not in _VALID_COLLECTIONS:
            return json.dumps({
                "error": (
                    f"Colecao invalida: '{collection}'. "
                    f"Validas: {', '.join(_VALID_COLLECTIONS)}"
                )
            })

        results = _run_async(
            self._backend.search(
                query=query,
                collection=collection,
                limit=limit,
                **self._requester_kwargs(),
            )
        )

        return json.dumps({
            "results": results,
            "count": len(results),
            "query": query,
        }, ensure_ascii=False, default=str)

    def _handle_store(self, args: Dict[str, Any]) -> str:
        """Armazena uma nova memoria com validacao de seguranca."""
        content = args.get("content", "")
        if not content:
            return json.dumps({"error": "Campo 'content' e obrigatorio."})

        collection = args.get("collection", "fact")
        if collection not in _VALID_COLLECTIONS:
            return json.dumps({
                "error": (
                    f"Colecao invalida: '{collection}'. "
                    f"Validas: {', '.join(_VALID_COLLECTIONS)}"
                )
            })

        tags = args.get("tags", {})
        priority = args.get("priority", "medium")

        # Validar dimensoes de tags
        if tags:
            for dim in tags:
                if dim not in _VALID_TAG_DIMS:
                    return json.dumps({
                        "error": (
                            f"Dimensao de tag invalida: '{dim}'. "
                            f"Validas: {', '.join(_VALID_TAG_DIMS)}"
                        )
                    })

        # Protecao: agente NAO pode criar memorias critical/mandatory
        if priority == "critical":
            return json.dumps({
                "error": (
                    "Prioridade 'critical' e reservada para humano "
                    "autenticado. Use 'high', 'medium' ou 'low'."
                )
            })

        # Validar prioridade
        if priority not in _VALID_PRIORITIES:
            return json.dumps({
                "error": (
                    f"Prioridade invalida: '{priority}'. "
                    f"Validas: high, medium, low (critical reservada para humano)."
                )
            })

        # --- Caminho de escrita garantido -----------------------------
        #
        # ORDEM: estagiar em disco PRIMEIRO, gravar depois. A spool e
        # barata e quase infalivel; o backend nao. Estagiando antes, a
        # intencao de memoria sobrevive a backend fora do ar, a cron sem
        # as tools de execucao no toolset e a queda do processo no meio.
        # O que falhar aqui vira pendencia do drainer, nunca conteudo
        # orfao.

        # 1. Estagio duravel.
        item = stage_to_spool(
            content=content,
            collection=collection,
            tags=tags,
            priority=priority,
            source="agent",
            mandatory=False,
            label=collection,
            metadata={"origem": "orkmind_store"},
        )
        content_hash = item["content_hash"] if item else _spool_content_hash(content)

        # 2. Tentativa de gravacao direta.
        try:
            entry_id = _run_async(
                self._backend.store_memory(
                    content=content,
                    collection=collection,
                    tags=tags,
                    priority=priority,
                    mandatory=False,  # NUNCA mandatory via agente
                    source="agent",   # SEMPRE source=agent
                    **self._requester_kwargs(),
                )
            )
        except Exception as exc:
            logger.warning("Gravacao direta falhou, item fica na spool: %s", exc)
            entry_id = None
            erro = str(exc)
        else:
            erro = ""

        # 3. Sucesso: fecha o item com o entry_id REAL.
        if entry_id:
            if item:
                mark_done(item, entry_id, backend="plugin", result="created")
            return json.dumps({
                "status": "armazenado",
                "id": entry_id,
                "collection": collection,
                "content_hash": content_hash,
                "message": f"Memoria armazenada com sucesso na colecao '{collection}'.",
            }, ensure_ascii=False)

        # 4. Falha: status honesto, SEM inventar entry_id. O conteudo
        #    esta seguro na fila e o drainer confirma o id de verdade.
        if item:
            return json.dumps({
                "status": "pendente",
                "id": None,
                "collection": collection,
                "content_hash": content_hash,
                "spool_item": item["item_id"],
                "message": (
                    f"Memoria NAO confirmada no banco ainda. O conteudo esta "
                    f"estagiado com seguranca na fila de saida (item "
                    f"{item['item_id']}, colecao '{collection}') e sera gravado "
                    f"pelo drainer assim que o backend responder. O entry_id "
                    f"real so existe apos essa confirmacao - nao presuma um id."
                ),
                "erro": erro,
            }, ensure_ascii=False)

        # 5. Nem o disco aceitou: unico caso de perda possivel, e precisa
        #    ser reportado sem rodeios.
        return json.dumps({
            "status": "erro",
            "id": None,
            "collection": collection,
            "content_hash": content_hash,
            "message": (
                "Falha ao gravar no banco E ao estagiar na fila em disco. "
                "O conteudo NAO esta salvo. Preserve-o por outro meio e "
                "avise o operador."
            ),
            "erro": erro,
        }, ensure_ascii=False)

    def _handle_handoff(self, args: Dict[str, Any]) -> str:
        """Submete o pacote de handoff para validacao e armazenamento (G3).

        O plugin NAO rotaciona sessao: ele repassa o payload ao core, que
        valida contra as handoff-rules e armazena. O contador de refacoes
        e por sessao e zera apos um pacote aceito.
        """
        payload = args.get("payload")
        if not isinstance(payload, dict) or not payload:
            return json.dumps({
                "error": (
                    "Campo 'payload' (objeto {secao: conteudo}) e "
                    "obrigatorio."
                )
            })
        if not hasattr(self._backend, "submit_handoff"):
            return json.dumps({
                "error": "Backend OrkMind sem suporte a handoff (G3)."
            })

        destination = str(args.get("destination", "") or "").strip() or None
        resultado = _run_async(
            self._backend.submit_handoff(
                payload=payload,
                session_id=self._session_id or "sessao-desconhecida",
                destination=destination,
                refacao=self._handoff_refacoes,
                **self._requester_kwargs(),
            )
        )

        if resultado.get("status") == "refazer":
            self._handoff_refacoes = int(
                resultado.get("refacao", self._handoff_refacoes + 1)
            )
        else:
            self._handoff_refacoes = 0

        return json.dumps(resultado, ensure_ascii=False, default=str)

    def _handle_rules(self, args: Dict[str, Any]) -> str:
        """Consulta regras mandatorias ativas."""
        tags = args.get("tags")

        if tags:
            for dim in tags:
                if dim not in _VALID_TAG_DIMS:
                    return json.dumps({
                        "error": (
                            f"Dimensao de tag invalida: '{dim}'. "
                            f"Validas: {', '.join(_VALID_TAG_DIMS)}"
                        )
                    })

        results = _run_async(
            self._backend.get_rules(tags=tags, **self._requester_kwargs())
        )

        return json.dumps({
            "rules": results,
            "count": len(results),
            "note": (
                "Regras mandatorias sao injetadas automaticamente no contexto. "
                "Regras criticas (priority=critical) so podem ser alteradas "
                "por humano autenticado."
            ),
        }, ensure_ascii=False, default=str)

    # -- Extracao automatica de memorias soft ---------------------------------

    @staticmethod
    def _extract_soft_memories(
        user_messages: List[str],
    ) -> List[Dict[str, Any]]:
        """Extrai memorias soft das mensagens do usuario.

        Heuristica conservadora: identifica padroes explicitos de
        declaracao de fatos, preferencias e aprendizados. NAO tenta
        inferir informacoes implicitas - melhor perder uma memoria
        do que armazenar algo incorreto.

        Retorna lista de dicts com 'content', 'collection' e 'tags'.
        So colecoes soft: fact, preference, learning, content.
        """
        extracted: List[Dict[str, Any]] = []

        # Padroes que indicam preferencias explicitas
        _pref_markers = (
            "eu prefiro", "prefiro", "gosto de", "nao gosto de",
            "sempre use", "nunca use", "minha preferencia",
            "quero que voce", "por favor sempre",
        )

        # Padroes que indicam fatos pessoais
        _fact_markers = (
            "eu sou", "meu nome e", "trabalho com", "trabalho na",
            "meu projeto", "uso ", "minha stack",
        )

        for msg in user_messages:
            msg_lower = msg.lower().strip()

            # Ignorar mensagens muito curtas ou comandos
            if len(msg_lower) < 15 or msg_lower.startswith("/"):
                continue

            # Detectar preferencias explicitas
            for marker in _pref_markers:
                if marker in msg_lower and len(msg) < 500:
                    extracted.append({
                        "content": msg.strip(),
                        "collection": "preference",
                        "tags": {},
                    })
                    break

            # Detectar fatos pessoais
            for marker in _fact_markers:
                if msg_lower.startswith(marker) and len(msg) < 500:
                    extracted.append({
                        "content": msg.strip(),
                        "collection": "fact",
                        "tags": {},
                    })
                    break

        # Deduplicar por conteudo
        seen = set()
        unique: List[Dict[str, Any]] = []
        for mem in extracted:
            key = mem["content"]
            if key not in seen:
                seen.add(key)
                unique.append(mem)

        # Limitar a 5 memorias por sessao (conservador)
        return unique[:5]


# ---------------------------------------------------------------------------
# Entry point do plugin
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Registra o OrkMind como memory provider do Hermes."""
    ctx.register_memory_provider(OrkMindHermesProvider())
