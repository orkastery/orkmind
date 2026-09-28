"""Grupo `orkmind store` da CLI (F3.8 e F3.9).

`store info` e o caminho oficial para ver uma degradacao antes de
investigar um sintoma. Se ele mentir, R0.3 vira letra morta.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from orkmind.cli.main import cli

ENV_MEMORY = {"ORKMIND_STORE_BACKEND": "memory"}
ENV_PGVECTOR = {
    "ORKMIND_STORE_BACKEND": "pgvector",
    "ORKMIND_DATABASE_URL": "postgresql://localhost/nunca-conecta",
}


def rodar(args: list[str], env: dict[str, str], tmp_path: Path):
    """Roda a CLI sem ler o config real do usuario.

    `load_config` e apontado para um caminho inexistente em tmp_path, entao
    a configuracao vem apenas das variaveis de ambiente do proprio teste.
    """
    from orkmind.core.config import load_config as load_config_real

    runner = CliRunner()
    inexistente = tmp_path / "config-que-nao-existe.toml"
    with patch.dict(os.environ, env, clear=True):
        with patch(
            "orkmind.cli.main.load_config",
            lambda *a, **k: load_config_real(inexistente),
        ):
            return runner.invoke(cli, args)


class TestStoreInfo:
    def test_mostra_o_backend_ativo(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "info"], ENV_MEMORY, tmp_path)
        assert resultado.exit_code == 0, resultado.output
        assert "Backend configurado: memory" in resultado.output
        assert "Backend ativo: memory" in resultado.output

    def test_lista_as_capabilities(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "info"], ENV_MEMORY, tmp_path)
        for campo in ("vector_search", "text_search", "durable", "backfill"):
            assert campo in resultado.output

    def test_mostra_os_avisos_do_backend_volatil(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "info"], ENV_MEMORY, tmp_path)
        assert "Avisos ativos" in resultado.output
        assert "nao persiste entre processos" in resultado.output

    def test_backend_sem_degradacao_diz_isso(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "info"], ENV_PGVECTOR, tmp_path)
        assert resultado.exit_code == 0, resultado.output
        assert "Backend ativo: pgvector" in resultado.output
        assert "nenhum" in resultado.output

    def test_json_traz_capabilities_e_avisos(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "info", "--json"], ENV_MEMORY, tmp_path)
        assert resultado.exit_code == 0, resultado.output
        dados = json.loads(resultado.output)
        assert dados["backend"] == "memory"
        assert dados["capabilities"]["durable"] is False
        assert any("nao persiste" in a for a in dados["avisos"])

    def test_backend_desconhecido_falha_com_a_lista(self, tmp_path: Path) -> None:
        resultado = rodar(
            ["store", "info"], {"ORKMIND_STORE_BACKEND": "cassandra"}, tmp_path
        )
        assert resultado.exit_code != 0
        assert "cassandra" in str(resultado.exception)


class TestStoreExportImport:
    def test_ciclo_pela_cli(self, tmp_path: Path) -> None:
        """Export e import no backend memory, ponta a ponta."""
        dump = tmp_path / "dump.jsonl"
        exportou = rodar(
            ["store", "export", "--out", str(dump)], ENV_MEMORY, tmp_path
        )
        assert exportou.exit_code == 0, exportou.output
        assert "Backend de origem: memory" in exportou.output
        assert "Origem intacta: sim" in exportou.output
        assert dump.exists()

        importou = rodar(
            ["store", "import", "--in", str(dump), "--dry-run"], ENV_MEMORY, tmp_path
        )
        assert importou.exit_code == 0, importou.output
        assert "dry-run (nada foi escrito)" in importou.output

    def test_import_de_arquivo_sem_manifesto_falha(self, tmp_path: Path) -> None:
        ruim = tmp_path / "ruim.jsonl"
        ruim.write_text('{"tipo": "entry"}\n', encoding="utf-8")
        resultado = rodar(
            ["store", "import", "--in", str(ruim)], ENV_MEMORY, tmp_path
        )
        assert resultado.exit_code != 0
        assert "manifesto" in str(resultado.exception)


class TestAjuda:
    def test_grupo_store_esta_registrado(self, tmp_path: Path) -> None:
        resultado = rodar(["store", "--help"], ENV_MEMORY, tmp_path)
        assert resultado.exit_code == 0
        for comando in ("info", "export", "import"):
            assert comando in resultado.output
