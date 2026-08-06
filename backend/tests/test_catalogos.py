# backend/tests/test_catalogos.py — Sistema Dono
#
# Cobre:
#   - Gêneros: listagem dos 2 fixos seedados
#   - Categorias: listar, filtro por gênero, criar, duplicata (409)
#   - Fornecedores: listar, filtro ativo, criar, obter, atualizar,
#                   vínculo com categoria
#   - Estilos de Serviço: listar os 7 seedados, criar customizado
#   - Regras de Composição: listar, filtro por gênero, criar, remover,
#                            sanidade dos seeds (quantidade e conteúdo)
#   - Permissões RBAC por rota

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Gêneros
# =====================================================================

@pytest.mark.asyncio
class TestGeneros:

    async def test_listar_generos_retorna_dois_fixos(self, client, token_chef):
        r = await client.get("/generos", headers=auth_headers(token_chef))
        assert r.status_code == 200
        nomes = {g["nome"] for g in r.json()}
        assert nomes == {"ALIMENTICIO", "OPERACIONAL_UTENSILIO"}

    async def test_listar_generos_sem_autenticacao(self, client):
        # Gêneros são leitura pública — sem perfil requerido
        r = await client.get("/generos")
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_generos_possuem_id_e_nome(self, client):
        r = await client.get("/generos")
        for g in r.json():
            assert "id" in g
            assert "nome" in g


# =====================================================================
# Categorias
# =====================================================================

@pytest.mark.asyncio
class TestCategorias:

    async def test_listar_categorias_retorna_seeds(self, client):
        """Seeds carregam 11 categorias (6 ALIMENTICIO + 5 OPERACIONAL_UTENSILIO)."""
        r = await client.get("/categorias")
        assert r.status_code == 200
        assert len(r.json()) == 11

    async def test_listar_categorias_filtro_alimenticio(self, client):
        r = await client.get("/categorias?genero=ALIMENTICIO")
        assert r.status_code == 200
        cats = r.json()
        assert len(cats) == 6
        assert all(c["genero"] == "ALIMENTICIO" for c in cats)

    async def test_listar_categorias_filtro_operacional(self, client):
        r = await client.get("/categorias?genero=OPERACIONAL_UTENSILIO")
        cats = r.json()
        assert len(cats) == 5
        assert all(c["genero"] == "OPERACIONAL_UTENSILIO" for c in cats)

    async def test_categorias_seeds_nomes_esperados(self, client):
        r = await client.get("/categorias?genero=ALIMENTICIO")
        nomes = {c["nome"] for c in r.json()}
        esperados = {
            "Secos e Despensa", "Hortifruti", "Carnes, Aves e Peixes",
            "Laticínios e Frios", "Bebidas", "Congelados",
        }
        assert esperados == nomes

    async def test_criar_categoria_alimenticio(self, client, token_admin):
        r = await client.post(
            "/categorias",
            headers=auth_headers(token_admin),
            json={"nome": "Especiarias", "genero": "ALIMENTICIO"},
        )
        assert r.status_code == 201
        assert r.json()["nome"] == "Especiarias"
        assert r.json()["genero"] == "ALIMENTICIO"
        assert "id" in r.json()

    async def test_criar_categoria_genero_invalido_retorna_400(self, client, token_admin):
        r = await client.post(
            "/categorias",
            headers=auth_headers(token_admin),
            json={"nome": "X", "genero": "GENERO_INEXISTENTE"},
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_criar_categoria_duplicada_retorna_409(self, client, token_admin):
        # "Bebidas" já existe no seed
        r = await client.post(
            "/categorias",
            headers=auth_headers(token_admin),
            json={"nome": "Bebidas", "genero": "ALIMENTICIO"},
        )
        assert r.status_code == 409
        assert err(r) == "CATEGORIA_JA_EXISTE"

    async def test_somente_admin_cria_categoria(self, client, token_chef,
                                                 token_compras, token_gestao):
        payload = {"nome": "Nova Cat", "genero": "ALIMENTICIO"}
        for token in (token_chef, token_compras, token_gestao):
            r = await client.post("/categorias", headers=auth_headers(token), json=payload)
            assert r.status_code == 403

    async def test_leitura_categorias_sem_autenticacao(self, client):
        r = await client.get("/categorias")
        assert r.status_code == 200


# =====================================================================
# Fornecedores
# =====================================================================

@pytest.mark.asyncio
class TestFornecedores:

    async def _criar(self, client, token, nome="Fornecedor Teste",
                     avaliacao=4.5, ativo=True):
        r = await client.post(
            "/fornecedores",
            headers=auth_headers(token),
            json={
                "nome": nome,
                "contato": "(83) 99999-0000",
                "prazo_entrega_medio_dias": 3,
                "condicoes_pagamento": "30 dias",
                "avaliacao": avaliacao,
            },
        )
        assert r.status_code == 201, r.text
        return r.json()

    async def test_criar_fornecedor(self, client, token_admin):
        data = await self._criar(client, token_admin)
        assert data["nome"] == "Fornecedor Teste"
        assert data["avaliacao"] == 4.5
        assert data["ativo"] is True

    async def test_listar_fornecedores(self, client, token_admin):
        await self._criar(client, token_admin, nome="Forn A")
        await self._criar(client, token_admin, nome="Forn B")
        r = await client.get("/fornecedores", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert len(r.json()) == 2

    async def test_listar_fornecedores_filtro_ativo(self, client, token_admin):
        forn = await self._criar(client, token_admin, nome="Forn Ativo")
        # Desativar via PATCH
        await client.patch(
            f"/fornecedores/{forn['id']}",
            headers=auth_headers(token_admin),
            json={"ativo": False},
        )
        await self._criar(client, token_admin, nome="Forn Ativo 2")

        r = await client.get("/fornecedores?ativo=true",
                             headers=auth_headers(token_admin))
        assert all(f["ativo"] for f in r.json())
        assert len(r.json()) == 1

        r = await client.get("/fornecedores?ativo=false",
                             headers=auth_headers(token_admin))
        assert len(r.json()) == 1
        assert r.json()[0]["ativo"] is False

    async def test_obter_fornecedor(self, client, token_admin):
        forn = await self._criar(client, token_admin)
        r = await client.get(f"/fornecedores/{forn['id']}",
                             headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json()["id"] == forn["id"]

    async def test_obter_fornecedor_inexistente_retorna_404(self, client, token_admin):
        r = await client.get("/fornecedores/00000000-0000-0000-0000-000000000000",
                             headers=auth_headers(token_admin))
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_atualizar_fornecedor(self, client, token_admin):
        forn = await self._criar(client, token_admin)
        r = await client.patch(
            f"/fornecedores/{forn['id']}",
            headers=auth_headers(token_admin),
            json={"nome": "Fornecedor Atualizado", "avaliacao": 5.0},
        )
        assert r.status_code == 200
        assert r.json()["nome"] == "Fornecedor Atualizado"
        assert r.json()["avaliacao"] == 5.0
        # campo não enviado deve permanecer
        assert r.json()["contato"] == forn["contato"]

    async def test_atualizar_fornecedor_inexistente_retorna_404(self, client, token_admin):
        r = await client.patch(
            "/fornecedores/00000000-0000-0000-0000-000000000000",
            headers=auth_headers(token_admin),
            json={"nome": "X"},
        )
        assert r.status_code == 404

    async def test_vincular_categoria_fornecedor(self, client, token_admin, conn):
        forn = await self._criar(client, token_admin)
        cat_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Bebidas'"
        )
        r = await client.post(
            f"/fornecedores/{forn['id']}/categorias",
            headers=auth_headers(token_admin),
            params={"categoria_id": str(cat_id)},
        )
        assert r.status_code == 204

        # Idempotente — segunda chamada não deve falhar
        r2 = await client.post(
            f"/fornecedores/{forn['id']}/categorias",
            headers=auth_headers(token_admin),
            params={"categoria_id": str(cat_id)},
        )
        assert r2.status_code == 204

    async def test_somente_compras_e_admin_criam_fornecedor(self, client,
                                                              token_compras,
                                                              token_chef,
                                                              token_gestao):
        payload = {"nome": "X"}
        # compras pode
        r = await client.post("/fornecedores", headers=auth_headers(token_compras),
                              json=payload)
        assert r.status_code == 201

        # chef não pode
        r = await client.post("/fornecedores", headers=auth_headers(token_chef),
                              json=payload)
        assert r.status_code == 403

        # gestao não pode
        r = await client.post("/fornecedores", headers=auth_headers(token_gestao),
                              json=payload)
        assert r.status_code == 403

    async def test_leitura_fornecedores_sem_autenticacao(self, client):
        r = await client.get("/fornecedores")
        assert r.status_code == 200


# =====================================================================
# Estilos de Serviço
# =====================================================================

@pytest.mark.asyncio
class TestEstilosServico:

    async def test_listar_estilos_retorna_7_seedados(self, client):
        r = await client.get("/estilos-servico")
        assert r.status_code == 200
        assert len(r.json()) == 7

    async def test_estilos_seeds_nomes_esperados(self, client):
        r = await client.get("/estilos-servico")
        nomes = {e["nome"] for e in r.json()}
        esperados = {
            "Franco-Americano (Buffet / Self-Service)",
            "À La Carte (Serviço Emprestado / Americano)",
            "À Francesa",
            "À Inglesa Direto",
            "À Inglesa Indireto (Gueridon)",
            "À Russa",
            "À Família (Familiar / Compartilhado)",
        }
        assert esperados == nomes

    async def test_estilos_possuem_descricao_e_dinamica(self, client):
        r = await client.get("/estilos-servico")
        for e in r.json():
            assert "descricao" in e
            assert "dinamica" in e

    async def test_criar_estilo_customizado(self, client, token_admin):
        r = await client.post(
            "/estilos-servico",
            headers=auth_headers(token_admin),
            json={
                "nome": "Estilo Customizado",
                "descricao": "Serviço personalizado para eventos corporativos",
                "dinamica": "Garçons circulam com bandejas",
            },
        )
        assert r.status_code == 201
        assert r.json()["nome"] == "Estilo Customizado"

        # Agora aparece na listagem
        lista = await client.get("/estilos-servico")
        assert len(lista.json()) == 8

    async def test_somente_admin_cria_estilo(self, client, token_chef,
                                              token_compras, token_gestao):
        payload = {"nome": "Estilo X"}
        for token in (token_chef, token_compras, token_gestao):
            r = await client.post("/estilos-servico", headers=auth_headers(token),
                                  json=payload)
            assert r.status_code == 403

    async def test_leitura_estilos_sem_autenticacao(self, client):
        r = await client.get("/estilos-servico")
        assert r.status_code == 200


# =====================================================================
# Regras de Composição
# =====================================================================

@pytest.mark.asyncio
class TestRegrasComposicao:

    async def test_listar_regras_retorna_seeds(self, client):
        """Seeds carregam regras para 9 gêneros de refeição."""
        r = await client.get("/regras-composicao")
        assert r.status_code == 200
        # Total de regras nos seeds: 6+2+6+2+6+6+4+5+3 = 40
        # (Colação passou a ter 3 regras: Frios/Laticínios, Padaria, Bebida Quente)
        assert len(r.json()) == 40

    async def test_listar_regras_filtro_genero(self, client):
        r = await client.get("/regras-composicao?genero_refeicao=Almo%C3%A7o%20Executivo")
        assert r.status_code == 200
        regras = r.json()
        assert len(regras) == 6
        assert all(r["genero_refeicao"] == "Almoço Executivo" for r in regras)

    async def test_regras_almoco_executivo_conteudo(self, client):
        r = await client.get("/regras-composicao?genero_refeicao=Almo%C3%A7o%20Executivo")
        generos = {reg["genero_prato_obrigatorio"] for reg in r.json()}
        esperados = {
            "Entrada", "Prato Principal", "Guarnição",
            "Bebida Quente", "Bebida Fria", "Sobremesa",
        }
        assert generos == esperados

    async def test_regras_colacao_composicao_completa(self, client):
        """Colação tem 3 regras: Frios/Laticínios, Padaria e Bebida Quente."""
        r = await client.get("/regras-composicao?genero_refeicao=Cola%C3%A7%C3%A3o")
        assert len(r.json()) == 3
        generos = {reg["genero_prato_obrigatorio"] for reg in r.json()}
        assert generos == {"Frios/Laticínios", "Padaria", "Bebida Quente"}

    async def test_criar_regra_composicao(self, client, token_admin):
        r = await client.post(
            "/regras-composicao",
            headers=auth_headers(token_admin),
            json={
                "genero_refeicao": "Brunch",
                "genero_prato_obrigatorio": "Padaria",
            },
        )
        assert r.status_code == 201
        assert r.json()["genero_refeicao"] == "Brunch"
        assert r.json()["genero_prato_obrigatorio"] == "Padaria"

    async def test_remover_regra_composicao(self, client, token_admin, conn):
        # Cria uma regra nova para remover (não toca nos seeds)
        r = await client.post(
            "/regras-composicao",
            headers=auth_headers(token_admin),
            json={"genero_refeicao": "Brunch", "genero_prato_obrigatorio": "Frutas"},
        )
        regra_id = r.json()["id"]

        del_r = await client.delete(f"/regras-composicao/{regra_id}",
                                    headers=auth_headers(token_admin))
        assert del_r.status_code == 204

        # Confirma que sumiu
        row = await conn.fetchval(
            "SELECT id FROM regras_composicao WHERE id = $1::uuid", regra_id
        )
        assert row is None

    async def test_somente_admin_cria_regra(self, client, token_chef,
                                             token_compras, token_gestao):
        payload = {"genero_refeicao": "X", "genero_prato_obrigatorio": "Y"}
        for token in (token_chef, token_compras, token_gestao):
            r = await client.post("/regras-composicao", headers=auth_headers(token),
                                  json=payload)
            assert r.status_code == 403

    async def test_somente_admin_remove_regra(self, client, token_admin,
                                               token_chef, conn):
        r = await client.post(
            "/regras-composicao",
            headers=auth_headers(token_admin),
            json={"genero_refeicao": "Brunch", "genero_prato_obrigatorio": "Quentes"},
        )
        regra_id = r.json()["id"]

        # chef não pode remover
        r = await client.delete(f"/regras-composicao/{regra_id}",
                                headers=auth_headers(token_chef))
        assert r.status_code == 403

        # admin pode
        r = await client.delete(f"/regras-composicao/{regra_id}",
                                headers=auth_headers(token_admin))
        assert r.status_code == 204

    async def test_leitura_regras_sem_autenticacao(self, client):
        r = await client.get("/regras-composicao")
        assert r.status_code == 200
