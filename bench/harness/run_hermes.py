"""Runner do benchmark contra o plugin Hermes (Python).

Instancia o plugin headless, do mesmo jeito que `tests/unit/test_hermes_plugin.py`
faz (import por caminho de arquivo, com stub do contrato do runtime), e mede
por turno o que efetivamente chega ao prompt.

Metricas produzidas:
  M1  injecao constitucional: fracao dos turnos em que TODAS as regras N1
      (priority = critical) apareceram literalmente no prompt
  M2  regras mandatorias: fracao dos turnos com TODAS as mandatorias, mais a
      contagem de omitidas por budget
  M3  fail-safe: 5 cenarios de falha, mais o caso de controle do estado 1
  M4  recuperacao: recall@5 e precision@5 sobre `thematic.json`
  M5  custo: chars injetados e latencia
  M6  diluicao: posicao relativa do bloco de regras ao longo da sessao

Nunca toca banco de producao: usa o banco descartavel do `seed.py`, cuja URL
passa pela guarda do `tests/conftest.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BENCH_DIR.parent
sys.path.insert(0, str(BENCH_DIR / "harness"))

import seed as seed_mod  # noqa: E402

PLUGIN_PATH = REPO_DIR / "integrations" / "hermes" / "orkmind" / "__init__.py"
TURNOS_DE_DILUICAO = (1, 10, 20, 40)


def _instalar_stub_do_runtime() -> None:
    """Stub do contrato `agent.memory_provider` que o Hermes fornece."""
    if "agent.memory_provider" in sys.modules:
        return

    class MemoryProvider:
        def initialize(self, session_id: str, **kwargs: Any) -> None: ...
        def shutdown(self) -> None: ...

    agent_mod = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")
    mp_mod.MemoryProvider = MemoryProvider  # type: ignore[attr-defined]
    agent_mod.memory_provider = mp_mod  # type: ignore[attr-defined]
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = mp_mod


def carregar_plugin() -> Any:
    _instalar_stub_do_runtime()
    spec = importlib.util.spec_from_file_location("orkmind_hermes_bench", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["orkmind_hermes_bench"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class BackendDoBench:
    """Backend real apontado ao banco descartavel, com embedder fake.

    Implementa a superficie que o plugin usa: `get_rules` e `recall`.

    O plugin Hermes e sincrono e chama coroutines por `_run_async`, que cria
    um event loop NOVO a cada chamada. Uma conexao psycopg async fica presa
    ao loop em que nasceu, entao reusar a mesma conexao entre chamadas
    quebraria. A solucao aqui e um loop dedicado num thread proprio: toda I/O
    do backend roda sempre nele, e o loop efemero do plugin apenas espera o
    resultado. Sem isso, a alternativa seria reconectar a cada turno, o que
    poluiria a latencia do M5 com custo de conexao.
    """

    def __init__(self, url: str, semantic_enabled: bool = True) -> None:
        from fake_embedder import FakeEmbedder

        from orkmind.core.config import OrkMindConfig
        from orkmind.core.semantic_layer import SemanticLayer
        from orkmind.store.factory import create_store

        self._cfg = OrkMindConfig(database_url=url, embedding_dim=1024)
        self._store = create_store(self._cfg)
        self._layer = SemanticLayer(
            self._store,
            token_budget=self._cfg.token_budget,
            embedder=FakeEmbedder(),
            semantic_enabled=semantic_enabled,
        )
        self._iniciado = False
        # Interruptores usados pelos cenarios de falha do M3.
        self.derrubado = False
        self.vazio = False
        self.atraso_s = 0.0

        import asyncio
        import threading

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._rodar_loop, name="bench-backend", daemon=True
        )
        self._thread.start()

    def _rodar_loop(self) -> None:
        import asyncio

        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _no_loop_dedicado(self, coro: Any) -> Any:
        """Executa `coro` no loop dedicado e devolve o resultado."""
        import asyncio

        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _garantir(self) -> None:
        if not self._iniciado:
            await self._store.initialize()
            self._iniciado = True

    async def _get_rules_impl(self) -> list[dict[str, Any]]:
        await self._garantir()
        entries = await self._layer.get_mandatory_rules()
        return [self._como_dict(e) for e in entries]

    async def _recall_impl(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        await self._garantir()
        entries = await self._layer.query_for_context(
            tags=kwargs.get("tags"),
            conversation=kwargs.get("context", ""),
            token_budget=kwargs.get("token_budget"),
        )
        return [self._como_dict(e) for e in entries]

    async def get_rules(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.atraso_s:
            import asyncio

            await asyncio.sleep(self.atraso_s)
        if self.derrubado:
            raise RuntimeError("backend indisponivel (cenario de falha do bench)")
        if self.vazio:
            return []
        return self._no_loop_dedicado(self._get_rules_impl())

    async def recall(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.derrubado:
            raise RuntimeError("backend indisponivel (cenario de falha do bench)")
        return self._no_loop_dedicado(self._recall_impl(kwargs))

    def fechar(self) -> None:
        if self._iniciado:
            self._no_loop_dedicado(self._store.close())
            self._iniciado = False
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def close(self) -> None:
        self.fechar()

    @staticmethod
    def _como_dict(entry: Any) -> dict[str, Any]:
        return {
            "id": entry.id,
            "content": entry.content,
            "collection": entry.collection,
            "tags": entry.tags,
            "priority": entry.priority,
            "mandatory": entry.mandatory,
            "injection_risk": getattr(entry, "injection_risk", False),
            "conflict": getattr(entry, "conflict", False),
        }


def _novo_provider(plugin: Any, backend: Any, fail_safe: bool = True) -> Any:
    provider = plugin.OrkMindHermesProvider(fail_safe=fail_safe)
    provider._backend = backend
    provider._initialized = True
    return provider


def medir_injecao(plugin: Any, dados: dict, url: str) -> dict[str, Any]:
    """M1, M2, M5 e M6 sobre os 20 turnos do dataset constitucional."""
    backend = BackendDoBench(url)
    provider = _novo_provider(plugin, backend)

    regras = dados["constitutional"]["regras"]
    criticas = [r for r in regras if r["priority"] == "critical"]
    mandatorias = [r for r in regras if r["mandatory"]]
    turnos = dados["constitutional"]["turnos"]

    m1_ok = m2_ok = 0
    omitidas = 0
    chars: list[int] = []
    latencias: list[float] = []
    diluicao: dict[str, float] = {}

    total_turnos = max(TURNOS_DE_DILUICAO)
    for i in range(1, total_turnos + 1):
        turno = turnos[(i - 1) % len(turnos)]
        inicio = time.perf_counter()
        system_block = provider.system_prompt_block()
        prefetch = provider.prefetch(turno["texto"])
        latencias.append((time.perf_counter() - inicio) * 1000.0)
        prompt = system_block + "\n" + prefetch

        if i <= len(turnos):
            if all(r["content"] in prompt for r in criticas):
                m1_ok += 1
            if all(r["content"] in prompt for r in mandatorias):
                m2_ok += 1
            omitidas += _omitidas_por_budget(system_block)
            chars.append(len(system_block))

        if i in TURNOS_DE_DILUICAO:
            diluicao[f"turno_{i}"] = _fracao_de_regras(system_block, prompt)

    backend.fechar()

    den = len(turnos)
    return {
        "M1": {"num": m1_ok, "den": den, "taxa": round(m1_ok / den, 4)},
        "M2": {
            "num": m2_ok,
            "den": den,
            "taxa": round(m2_ok / den, 4),
            "omitidas_por_budget": omitidas,
        },
        "M5": {
            "chars_injetados_medio": int(statistics.mean(chars)) if chars else 0,
            "latencia_ms_p50": round(statistics.median(latencias), 3),
        },
        "M6": diluicao,
    }


def _omitidas_por_budget(bloco: str) -> int:
    import re

    achado = re.search(r"\((\d+) regra\(s\) omitida\(s\) por budget", bloco)
    return int(achado.group(1)) if achado else 0


def _fracao_de_regras(bloco_de_regras: str, prompt: str) -> float:
    """Que fatia do prompt do turno e ocupada pelo bloco de regras (M6)."""
    if not prompt:
        return 0.0
    return round(len(bloco_de_regras) / len(prompt), 4)


def medir_fail_safe(plugin: Any, url: str) -> dict[str, Any]:
    """M3: os cinco cenarios de falha mais o controle do estado 1."""

    ALERTA = "ALERTA: Regras de Governanca Indisponiveis"
    casos: list[dict[str, Any]] = []

    def _com_carga_previa() -> tuple[Any, Any]:
        backend = BackendDoBench(url)
        provider = _novo_provider(plugin, backend)
        provider.system_prompt_block()  # primeira carga, arma o D-MA9
        return provider, backend

    # 1. backend derrubado apos carga
    provider, backend = _com_carga_previa()
    backend.derrubado = True
    casos.append({
        "caso": "backend derrubado apos carga",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": True,
    })
    backend.fechar()

    # 2. backend vazio apos carga
    provider, backend = _com_carga_previa()
    backend.vazio = True
    casos.append({
        "caso": "backend vazio apos carga",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": True,
    })
    backend.fechar()

    # 3. timeout (atraso alem do aceitavel) apos carga
    provider, backend = _com_carga_previa()
    backend.atraso_s = 0.05
    backend.derrubado = True
    casos.append({
        "caso": "timeout na carga apos carga previa",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": True,
    })
    backend.fechar()

    # 4. backend sem o metodo de regras (equivalente a schema invalido)
    provider, backend = _com_carga_previa()

    class SemGetRules:
        async def get_rules(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise AttributeError("schema invalido: coluna mandatory ausente")

        async def recall(self, **kwargs: Any) -> list[dict[str, Any]]:
            return []

    provider._backend = SemGetRules()
    casos.append({
        "caso": "schema invalido apos carga",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": True,
    })
    backend.fechar()

    # 5. sem pool / backend ausente apos carga
    provider, backend = _com_carga_previa()
    provider._backend = None
    casos.append({
        "caso": "backend ausente apos carga",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": True,
    })
    backend.fechar()

    # Controle: falha ANTES de qualquer carga NAO deve alertar (estado 1).
    backend = BackendDoBench(url)
    backend.derrubado = True
    provider = _novo_provider(plugin, backend)
    casos.append({
        "caso": "controle: falha sem carga previa (nao deve alertar)",
        "alerta_presente": ALERTA in provider.system_prompt_block(),
        "esperado": False,
    })
    backend.fechar()

    acertos = sum(1 for c in casos if c["alerta_presente"] == c["esperado"])
    return {"casos": casos, "num": acertos, "den": len(casos)}


def medir_recuperacao(dados: dict, url: str) -> dict[str, Any]:
    """M4: recall@5 e precision@5 sobre `thematic.json`."""
    import asyncio

    K = 5

    async def _rodar() -> tuple[list[float], list[float]]:
        from fake_embedder import FakeEmbedder

        from orkmind.core.config import OrkMindConfig
        from orkmind.core.semantic_layer import SemanticLayer
        from orkmind.store.factory import create_store

        cfg = OrkMindConfig(database_url=url, embedding_dim=1024)
        store = create_store(cfg)
        await store.initialize()
        layer = SemanticLayer(store, embedder=FakeEmbedder(), semantic_enabled=True)

        recalls: list[float] = []
        precisions: list[float] = []
        sanidade_ok = 0
        try:
            for sessao in dados["thematic"]["sessoes"]:
                consulta = "\n".join(sessao["turnos"])
                encontrados = await layer.search_semantic_intent(consulta, limit=K)
                ids = [e.id for e in encontrados][:K]
                gabarito = set(sessao["relevantes"])
                acertos = len(gabarito & set(ids))
                recalls.append(acertos / len(gabarito) if gabarito else 0.0)
                precisions.append(acertos / K)

            # Sanidade do pipeline: consultando com o texto EXATO de cada
            # memoria, ela precisa aparecer no top-5. Isso nao mede qualidade
            # semantica (o embedder e fake), mede que FTS, vetor, RRF e
            # filtros estao ligados e funcionando ponta a ponta. Se este
            # numero nao for 1.00, o pipeline esta quebrado e nenhuma outra
            # metrica de recuperacao significa nada.
            for memoria in dados["thematic"]["memorias"]:
                encontrados = await layer.search_semantic_intent(
                    memoria["content"], limit=K
                )
                if memoria["id"] in [e.id for e in encontrados]:
                    sanidade_ok += 1
        finally:
            await store.close()
        return recalls, precisions, sanidade_ok

    recalls, precisions, sanidade_ok = asyncio.run(_rodar())
    total_memorias = len(dados["thematic"]["memorias"])
    return {
        "recall_at_5": round(statistics.mean(recalls), 4) if recalls else 0.0,
        "precision_at_5": round(statistics.mean(precisions), 4) if precisions else 0.0,
        "embedder": "fake",
        "sessoes": len(recalls),
        "sanidade_pipeline": {
            "num": sanidade_ok,
            "den": total_memorias,
            "taxa": round(sanidade_ok / total_memorias, 4) if total_memorias else 0.0,
        },
        "nota": (
            "recall_at_5 e precision_at_5 com embedder fake NAO medem qualidade "
            "semantica: o embedder e um hash, textos com o mesmo sentido geram "
            "vetores nao relacionados. O numero honesto aqui e proximo de zero e "
            "isso e esperado. O que esta validado e a ESTRUTURA, medida por "
            "sanidade_pipeline. Numero real depende de embedder real, adiado "
            "para o proximo ciclo. Veja bench/README.md."
        ),
    }


def _sha_do_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_DIR, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return "desconhecido"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="DSN do banco descartavel do bench")
    parser.add_argument("--saida", help="arquivo JSON de saida")
    args = parser.parse_args()

    url = seed_mod.resolver_url(args.url)
    dados = seed_mod.carregar_datasets()
    plugin = carregar_plugin()

    resultado = {
        "runner": "hermes",
        "commit": _sha_do_commit(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": {},
    }
    resultado["metrics"].update(medir_injecao(plugin, dados, url))
    resultado["metrics"]["M3"] = medir_fail_safe(plugin, url)
    resultado["metrics"]["M4"] = medir_recuperacao(dados, url)
    # Bloco de regras canonico, usado pelo M7 (fidelidade cruzada).
    backend = BackendDoBench(url)
    provider = _novo_provider(plugin, backend)
    resultado["bloco_de_regras"] = provider._build_rules_block()
    resultado["guardrail"] = plugin._GUARDRAIL_ANTI_DESTRUICAO
    resultado["fallback"] = plugin._FALLBACK_NO_RULES

    backend.fechar()

    texto = json.dumps(resultado, ensure_ascii=False, indent=2)
    if args.saida:
        Path(args.saida).write_text(texto + "\n", encoding="utf-8")
        print(f"[run_hermes] resultado em {args.saida}")
    else:
        print(texto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
