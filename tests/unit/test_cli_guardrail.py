"""Testes do comando `orkmind guardrail check` (G1).

Mesmo padrao de consumo do `orkmind store info`: saida humana e --json
para automacao. O backend memory dispensa servicos externos.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_cli_store import ENV_MEMORY, rodar


class TestGuardrailCheck:
    def test_saida_humana_em_base_vazia(self, tmp_path: Path) -> None:
        resultado = rodar(
            ["guardrail", "check", "--session-id", "s1"], ENV_MEMORY, tmp_path
        )
        assert resultado.exit_code == 0, resultado.output
        assert "Regras: ok" in resultado.output
        assert "advisory: none" in resultado.output
        assert "Fail-safe: inativo" in resultado.output

    def test_json_traz_o_contrato_completo(self, tmp_path: Path) -> None:
        resultado = rodar(
            ["guardrail", "check", "--session-id", "s1", "--json"],
            ENV_MEMORY,
            tmp_path,
        )
        assert resultado.exit_code == 0, resultado.output
        dados = json.loads(resultado.output)
        assert set(dados) == {"rules", "session", "fail_safe"}
        assert set(dados["rules"]["by_type"]) == {
            "critical", "important", "soft"
        }
        assert dados["session"]["usage_source"] == "indisponivel"

    def test_usage_informado_gera_advisory(self, tmp_path: Path) -> None:
        resultado = rodar(
            [
                "guardrail", "check", "--session-id", "s1",
                "--usage", "0.9", "--json",
            ],
            ENV_MEMORY,
            tmp_path,
        )
        assert resultado.exit_code == 0, resultado.output
        dados = json.loads(resultado.output)
        assert dados["session"]["usage_source"] == "informado"
        assert dados["session"]["advisory"] == "rotate_now"

    def test_session_id_e_obrigatorio(self, tmp_path: Path) -> None:
        resultado = rodar(["guardrail", "check"], ENV_MEMORY, tmp_path)
        assert resultado.exit_code != 0

    def test_grupo_guardrail_registrado(self, tmp_path: Path) -> None:
        resultado = rodar(["guardrail", "--help"], ENV_MEMORY, tmp_path)
        assert resultado.exit_code == 0
        assert "check" in resultado.output
