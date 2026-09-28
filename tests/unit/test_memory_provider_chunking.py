"""Chunking semantico estruturado do Memory Provider nativo."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orkmind.memory_provider.config import ChunkingOptions
from orkmind.memory_provider.services.chunking import chunk_document, html_to_markdown
from orkmind.memory_provider.tokens import estimate_tokens, head_by_tokens, tail_by_tokens

FRASE = "A rede neutra da operadora atende provedores regionais com SLA contratual definido. "


def _paragrafo(frases: int, marca: str) -> str:
    return "".join(f"{marca} {n}: {FRASE}" for n in range(frases)).strip()


PLAYBOOK = f"""# Playbook SDLC

Introducao curta do playbook de engenharia.

## Planejamento

{_paragrafo(30, "Planejamento")}

## Revisao de codigo

{_paragrafo(4, "Revisao")}

### Checklist

- Testes passam no CI
- Migracao tem rollback
- Mudanca documentada

## Deploy

```bash
make build
make deploy ENV=prod
```
"""


class TestTokens:
    def test_vazio_custa_zero(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("   \n ") == 0

    def test_espaco_e_gratis_e_palavra_arredonda_para_cima(self) -> None:
        assert estimate_tokens("de") == 1
        assert estimate_tokens("contrato") == 2
        assert estimate_tokens("contrato   de") == estimate_tokens("contrato de") == 3

    def test_head_e_tail_respeitam_orcamento_e_nao_cortam_palavra(self) -> None:
        texto = FRASE * 20
        cabeca, proximo = head_by_tokens(texto, 25)
        assert 0 < estimate_tokens(cabeca) <= 25
        assert texto.startswith(cabeca) and proximo == len(cabeca)
        assert not texto[proximo].isalnum() or not cabeca[-1].isalnum()

        cauda = tail_by_tokens(texto, 25)
        assert 0 < estimate_tokens(cauda) <= 25
        assert texto.endswith(cauda)

    def test_paginacao_percorre_o_texto_inteiro_sem_perder_nada(self) -> None:
        texto = FRASE * 50
        paginas, offset = [], 0
        while offset < len(texto):
            pagina, offset = head_by_tokens(texto, 40, offset=offset)
            paginas.append(pagina)
        assert "".join(paginas) == texto

    def test_peca_maior_que_o_orcamento_ainda_avanca(self) -> None:
        pagina, proximo = head_by_tokens("anticonstitucionalissimamente fim", 1)
        assert pagina == "anticonstitucionalissimamente" and proximo == len(pagina)


class TestChunking:
    def test_texto_vazio_nao_gera_chunk(self) -> None:
        assert chunk_document("  \n\n ") == []

    def test_nenhum_chunk_passa_do_maximo(self) -> None:
        options = ChunkingOptions(max_tokens=120, min_tokens=16)
        chunks = chunk_document(PLAYBOOK, title="Playbook SDLC", options=options)
        assert len(chunks) > 4
        assert [c.index for c in chunks] == list(range(len(chunks)))
        for chunk in chunks:
            assert chunk.token_count == estimate_tokens(chunk.content)
            assert chunk.token_count <= options.max_tokens

    def test_titulo_e_fronteira_dura(self) -> None:
        options = ChunkingOptions(max_tokens=120, min_tokens=16)
        chunks = chunk_document(PLAYBOOK, title="Playbook SDLC", options=options)
        for chunk in chunks:
            if "Planejamento 0:" in chunk.content or "Planejamento 29:" in chunk.content:
                assert "Revisao" not in chunk.content
        planejamento = [c for c in chunks if c.heading_path[-1] == "Planejamento"]
        assert len(planejamento) > 1
        assert all(c.heading_path == ("Playbook SDLC", "Planejamento") for c in planejamento)

    def test_h1_igual_ao_titulo_nao_duplica_no_caminho(self) -> None:
        chunks = chunk_document(PLAYBOOK, title="Playbook SDLC")
        assert all(c.heading_path.count("Playbook SDLC") == 1 for c in chunks)

    def test_subsecao_carrega_o_caminho_completo(self) -> None:
        options = ChunkingOptions(max_tokens=120, min_tokens=0)
        chunks = chunk_document(PLAYBOOK, title="Playbook SDLC", options=options)
        checklist = next(c for c in chunks if "Migracao tem rollback" in c.content)
        assert checklist.heading_path == ("Playbook SDLC", "Revisao de codigo", "Checklist")
        assert checklist.section_level == 3 and checklist.has_list

    @pytest.mark.parametrize("ratio", [0.10, 0.12, 0.15])
    def test_overlap_entre_chunks_da_mesma_secao_fica_na_faixa(self, ratio: float) -> None:
        options = ChunkingOptions(max_tokens=200, min_tokens=16, overlap_ratio=ratio)
        texto = "## Analise\n\n" + _paragrafo(60, "Item")
        chunks = chunk_document(texto, title="OPEX", options=options)
        assert len(chunks) >= 3
        for anterior, atual in zip(chunks, chunks[1:]):
            # O inicio do chunk atual repete o final do anterior.
            comum = next(
                atual.content[:n]
                for n in range(len(atual.content), 0, -1)
                if anterior.content.endswith(atual.content[:n])
            )
            overlap = estimate_tokens(comum)
            # Faixa contratual sempre; e nunca acima do alvo, salvo o passo
            # de uma palavra que o piso de 10% exige quando alvo == piso.
            assert options.max_tokens * 0.10 <= overlap <= options.max_tokens * 0.15
            assert overlap <= max(options.max_tokens * ratio, options.max_tokens * 0.10 + 2)

    def test_sem_overlap_entre_secoes_diferentes(self) -> None:
        options = ChunkingOptions(max_tokens=200, min_tokens=0)
        texto = f"## Primeira\n\n{_paragrafo(8, 'Alfa')}\n\n## Segunda\n\n{_paragrafo(8, 'Beta')}"
        chunks = chunk_document(texto, options=options)
        segunda = next(c for c in chunks if c.heading_path[-1] == "Segunda")
        assert segunda.content.startswith("## Segunda")
        assert "Alfa" not in segunda.content

    def test_bloco_de_codigo_grande_e_dividido_com_cerca_completa(self) -> None:
        linhas = "\n".join(f"resultado_{n} = processar(entrada_{n})" for n in range(80))
        texto = f"## Script\n\n```python\n{linhas}\n```\n"
        chunks = chunk_document(texto, options=ChunkingOptions(max_tokens=150, min_tokens=0))
        com_codigo = [c for c in chunks if c.has_code]
        assert len(com_codigo) > 1
        for chunk in com_codigo:
            assert chunk.content.count("```") == 2
            assert chunk.token_count <= 150
        assert "resultado_0 " in com_codigo[0].content
        assert "resultado_79 " in com_codigo[-1].content

    def test_tabela_grande_repete_o_cabecalho(self) -> None:
        linhas = "\n".join(f"| CT-2024/{n:04d} | Fornecedor {n} | {n * 1000} |" for n in range(60))
        texto = f"## Contratos\n\n| Contrato | Fornecedor | Valor |\n|---|---|---|\n{linhas}\n"
        chunks = chunk_document(texto, options=ChunkingOptions(max_tokens=160, min_tokens=0))
        tabelas = [c for c in chunks if c.has_table]
        assert len(tabelas) > 1
        assert all("| Contrato | Fornecedor | Valor |" in c.content for c in tabelas)
        todas = "\n".join(c.content for c in tabelas)
        assert all(f"CT-2024/{n:04d}" in todas for n in range(60))

    def test_secao_minuscula_e_fundida_com_a_vizinha(self) -> None:
        texto = "## Escopo\n\nCurto.\n\n## Premissas\n\n" + _paragrafo(3, "Premissa")
        chunks = chunk_document(texto, title="Doc", options=ChunkingOptions(min_tokens=48))
        assert len(chunks) == 1
        assert "## Escopo" in chunks[0].content and "## Premissas" in chunks[0].content
        assert chunks[0].heading_path == ("Doc",)

    def test_nada_do_documento_se_perde(self) -> None:
        options = ChunkingOptions(max_tokens=120, min_tokens=16)
        chunks = chunk_document(PLAYBOOK, title="Playbook SDLC", options=options)
        tudo = " ".join(c.content for c in chunks)
        for n in range(30):
            assert f"Planejamento {n}:" in tudo
        for trecho in ("Introducao curta", "Migracao tem rollback", "make deploy ENV=prod"):
            assert trecho in tudo

    def test_html_vira_estrutura_markdown(self) -> None:
        html = (
            "<html><head><style>p{color:red}</style></head><body><h1>Politica</h1>"
            "<h2>Acesso</h2><p>Todo acesso &eacute; nominal.</p>"
            "<ul><li>MFA obrigatorio</li><li>Senha rotacionada</li></ul>"
            "<script>alert(1)</script></body></html>"
        )
        markdown = html_to_markdown(html)
        assert "## Acesso" in markdown and "- MFA obrigatorio" in markdown
        assert "alert" not in markdown and "color:red" not in markdown
        chunks = chunk_document(html, title="Politica", content_format="html")
        assert chunks and "Todo acesso é nominal." in chunks[0].content

    def test_overlap_fora_da_faixa_10_15_e_rejeitado(self) -> None:
        for ratio in (0.05, 0.2):
            with pytest.raises(ValidationError):
                ChunkingOptions(overlap_ratio=ratio)
