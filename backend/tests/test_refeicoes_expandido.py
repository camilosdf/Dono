# backend/tests/test_refeicoes_expandido.py — Sistema Dono
#
# Expansão de cobertura do módulo de refeições, focando nas lacunas
# não cobertas por test_refeicoes.py:
#
#   A) GET /refeicoes — listagem com filtros (data, gênero, status)
#   B) GET /refeicoes/{id} — obter por id, 404
#   C) DELETE /refeicoes/{id}/itens/{item_id} — remover item
#   D) GET /refeicoes/{id}/abc — ABC de refeição (404 sem cálculo)
#   E) Permissões — criar e remover itens
#
# O que JÁ está em test_refeicoes.py e NÃO é repetido aqui:
#   - Fluxo completo PLANEJADA → CONFIRMADA → EXECUTADA → SERVIDA
#   - Cancelar PLANEJADA sem estorno
#   - Cancelar EXECUTADA com estorno de estoque
#   - Cancelar SERVIDA bloqueado
#   - Estoque insuficiente → 422
#   - Composição inválida → 422
#   - Imutabilidade do snapshot após confirmação
#
# Fixtures: client, conn, token_admin, token_chef, token_gestao,
#           token_compras

import uuid
from datetime import date, time, timedelta

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

async def _criar_refeicao(client, token_chef, genero="Almoço Executivo",
                           data=None, pessoas=10):
    """Cria uma refeição via API e retorna o response JSON."""
    data = data or (date.today() + timedelta(days=7)).isoformat()
    r = await client.post(
        "/refeicoes",
        headers=auth_headers(token_chef),
        json={
            "genero_refeicao": genero,
            "data": data,
            "horario_inicio": "12:00",
            "horario_fim": "14:00",
            "qtd_pessoas": pessoas,
        },
    )
    assert r.status_code == 201
    return r.json()


async def _criar_prato_simples(client, token_chef, conn, genero="Prato Principal"):
    """Cria um prato ativo sem itens de receita e o aprova."""
    cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
    nome = f"Prato_{uuid.uuid4().hex[:6]}"
    insumo_id = await conn.fetchval(
        """INSERT INTO insumos (nome, categoria_id, unidade, ativo)
           VALUES ($1, $2, 'KG', TRUE) RETURNING id""",
        f"Ins_{uuid.uuid4().hex[:6]}", cat_id,
    )
    await conn.execute(
        """INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao,
                                     quantidade, quantidade_disponivel)
           VALUES ($1, 10.0, CURRENT_DATE, 999, 999)""",
        insumo_id,
    )
    r = await client.post(
        "/pratos",
        headers=auth_headers(token_chef),
        json={
            "nome": nome,
            "genero_prato": genero,
            "rendimento_base_porcoes": 10,
            "itens_receita": [
                {"insumo_id": str(insumo_id), "tipo": "ALIMENTICIO",
                 "peso_bruto": 0.1, "fator_correcao": 1.0}
            ],
        },
    )
    prato_id = r.json()["id"]
    # Aprovar para ficar ATIVO
    from tests.conftest import auth_headers as ah
    token_admin_header = auth_headers
    await client.patch(f"/pratos/{prato_id}/aprovar",
                       headers=auth_headers(token_chef))
    return prato_id


async def _adicionar_item(client, token_chef, refeicao_id, prato_id):
    """Adiciona um prato à refeição e retorna o item criado."""
    r = await client.post(
        f"/refeicoes/{refeicao_id}/itens",
        headers=auth_headers(token_chef),
        json={"prato_id": prato_id},
    )
    return r


# =====================================================================
# A) GET /refeicoes — listagem com filtros
# =====================================================================

@pytest.mark.asyncio
class TestListarRefeicoes:
    """Testa GET /refeicoes — listagem paginada com filtros."""

    async def test_listar_retorna_estrutura_paginada(self, client, token_chef):
        """GET /refeicoes deve retornar estrutura Page com items, total, page."""
        r = await client.get("/refeicoes", headers=auth_headers(token_chef))
        assert r.status_code == 200
        data = r.json()
        for campo in ("items", "total", "page", "page_size"):
            assert campo in data

    async def test_refeicao_criada_aparece_na_listagem(self, client, token_chef):
        """Refeição criada deve aparecer em GET /refeicoes."""
        ref = await _criar_refeicao(client, token_chef)
        r = await client.get("/refeicoes", headers=auth_headers(token_chef))
        ids = [item["id"] for item in r.json()["items"]]
        assert ref["id"] in ids

    async def test_filtro_por_status_planejada(self, client, token_chef):
        """Filtro status=PLANEJADA deve retornar apenas refeições planejadas."""
        await _criar_refeicao(client, token_chef)
        r = await client.get("/refeicoes?status=PLANEJADA",
                              headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert all(item["status"] == "PLANEJADA" for item in r.json()["items"])

    async def test_filtro_por_genero(self, client, token_chef):
        """Filtro genero_refeicao deve retornar apenas o gênero solicitado."""
        await _criar_refeicao(client, token_chef, genero="Lanche da Manhã")
        r = await client.get(
            "/refeicoes?genero_refeicao=Lanche%20da%20Manh%C3%A3",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 200
        for item in r.json()["items"]:
            assert item["genero_refeicao"] == "Lanche da Manhã"

    async def test_filtro_por_data(self, client, token_chef):
        """Filtro data deve retornar apenas refeições da data informada."""
        data_alvo = (date.today() + timedelta(days=14)).isoformat()
        ref = await _criar_refeicao(client, token_chef, data=data_alvo)

        r = await client.get(f"/refeicoes?data={data_alvo}",
                              headers=auth_headers(token_chef))
        assert r.status_code == 200
        ids = [item["id"] for item in r.json()["items"]]
        assert ref["id"] in ids

    async def test_listar_sem_autenticacao_e_publico(self, client):
        """GET /refeicoes não exige autenticação (rota pública)."""
        r = await client.get("/refeicoes")
        assert r.status_code == 200


# =====================================================================
# B) GET /refeicoes/{id}
# =====================================================================

@pytest.mark.asyncio
class TestObterRefeicao:
    """Testa GET /refeicoes/{id} — obter detalhes de uma refeição."""

    async def test_obter_refeicao_existente(self, client, token_chef):
        """GET por id deve retornar a refeição com todos os campos."""
        ref = await _criar_refeicao(client, token_chef, pessoas=20)
        r = await client.get(f"/refeicoes/{ref['id']}",
                              headers=auth_headers(token_chef))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == ref["id"]
        assert data["qtd_pessoas"] == 20
        assert data["status"] == "PLANEJADA"

    async def test_obter_refeicao_inclui_itens(self, client, token_chef, conn):
        """GET por id deve incluir os itens da refeição."""
        ref = await _criar_refeicao(client, token_chef)
        prato_id = await _criar_prato_simples(client, token_chef, conn)
        await _adicionar_item(client, token_chef, ref["id"], prato_id)

        r = await client.get(f"/refeicoes/{ref['id']}",
                              headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert "itens" in r.json()
        assert len(r.json()["itens"]) == 1

    async def test_obter_refeicao_inexistente_retorna_404(self, client, token_chef):
        """GET com UUID inexistente deve retornar 404."""
        r = await client.get(f"/refeicoes/{uuid.uuid4()}",
                              headers=auth_headers(token_chef))
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_obter_campos_obrigatorios(self, client, token_chef):
        """Resposta deve conter todos os campos do modelo RefeicaoOut."""
        ref = await _criar_refeicao(client, token_chef)
        r = await client.get(f"/refeicoes/{ref['id']}",
                              headers=auth_headers(token_chef))
        data = r.json()
        for campo in ("id", "genero_refeicao", "data", "horario_inicio",
                       "horario_fim", "qtd_pessoas", "status", "itens"):
            assert campo in data, f"Campo '{campo}' ausente"


# =====================================================================
# C) DELETE /refeicoes/{id}/itens/{item_id}
# =====================================================================

@pytest.mark.asyncio
class TestRemoverItemRefeicao:
    """Testa DELETE /refeicoes/{id}/itens/{item_id}."""

    async def test_remover_item_planejada(self, client, token_chef, conn):
        """Remover item de refeição PLANEJADA deve retornar 204 e o item
        deve sumir da refeição."""
        ref = await _criar_refeicao(client, token_chef)
        prato_id = await _criar_prato_simples(client, token_chef, conn)
        r_item = await _adicionar_item(client, token_chef, ref["id"], prato_id)
        assert r_item.status_code == 201
        item_id = r_item.json()["id"]

        r_del = await client.delete(
            f"/refeicoes/{ref['id']}/itens/{item_id}",
            headers=auth_headers(token_chef),
        )
        assert r_del.status_code == 204

        # Verificar que o item foi removido
        r_get = await client.get(f"/refeicoes/{ref['id']}",
                                   headers=auth_headers(token_chef))
        ids_itens = [i["id"] for i in r_get.json()["itens"]]
        assert item_id not in ids_itens

    async def test_remover_item_refeicao_confirmada_retorna_409(
        self, client, token_chef, conn
    ):
        """Remover item de refeição CONFIRMADA deve retornar 409."""
        ref = await _criar_refeicao(client, token_chef)
        prato_id = await _criar_prato_simples(client, token_chef, conn)
        r_item = await _adicionar_item(client, token_chef, ref["id"], prato_id)
        item_id = r_item.json()["id"]

        # Confirmar a refeição
        await client.patch(f"/refeicoes/{ref['id']}/confirmar",
                           headers=auth_headers(token_chef))

        r_del = await client.delete(
            f"/refeicoes/{ref['id']}/itens/{item_id}",
            headers=auth_headers(token_chef),
        )
        assert r_del.status_code == 409
        assert err(r_del) == "REFEICAO_JA_CONFIRMADA"

    async def test_remover_item_refeicao_inexistente_retorna_404(
        self, client, token_chef
    ):
        """Remover item de refeição inexistente deve retornar 404."""
        r = await client.delete(
            f"/refeicoes/{uuid.uuid4()}/itens/{uuid.uuid4()}",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 404

    async def test_compras_nao_pode_remover_item(
        self, client, token_chef, token_compras, conn
    ):
        """Perfil COMPRAS não pode remover itens de refeição."""
        ref = await _criar_refeicao(client, token_chef)
        prato_id = await _criar_prato_simples(client, token_chef, conn)
        r_item = await _adicionar_item(client, token_chef, ref["id"], prato_id)
        item_id = r_item.json()["id"]

        r = await client.delete(
            f"/refeicoes/{ref['id']}/itens/{item_id}",
            headers=auth_headers(token_compras),
        )
        assert r.status_code == 403

    async def test_sem_token_retorna_401(self, client, token_chef, conn):
        """DELETE sem autenticação deve retornar 401."""
        ref = await _criar_refeicao(client, token_chef)
        r = await client.delete(
            f"/refeicoes/{ref['id']}/itens/{uuid.uuid4()}"
        )
        assert r.status_code == 401


# =====================================================================
# D) GET /refeicoes/{id}/abc
# =====================================================================

@pytest.mark.asyncio
class TestAbcRefeicao:
    """Testa GET /refeicoes/{id}/abc — classificação ABC de pratos."""

    async def test_abc_sem_calculo_retorna_404(self, client, token_chef):
        """Refeição sem cálculo ABC deve retornar 404 com código
        ABC_NAO_CALCULADO."""
        ref = await _criar_refeicao(client, token_chef)
        r = await client.get(f"/refeicoes/{ref['id']}/abc",
                              headers=auth_headers(token_chef))
        assert r.status_code == 404
        assert err(r) == "ABC_NAO_CALCULADO"

    async def test_abc_disponivel_apos_confirmar(
        self, client, token_chef, token_admin, conn
    ):
        """ABC deve estar disponível após confirmar a refeição com estoque
        suficiente (trigger fn_recalcular_abc_refeicao dispara na confirmação)."""
        from datetime import date as _date, timedelta as _td

        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        insumo_id = await conn.fetchval(
            """INSERT INTO insumos (nome, categoria_id, unidade, ativo)
               VALUES ($1, $2, 'KG', TRUE) RETURNING id""",
            f"Ins_ABC_{uuid.uuid4().hex[:6]}", cat_id,
        )
        await conn.execute(
            """INSERT INTO lotes_insumo (insumo_id, valor_aquisicao, data_aquisicao,
                                         quantidade, quantidade_disponivel)
               VALUES ($1, 20.0, CURRENT_DATE, 999, 999)""",
            insumo_id,
        )
        r_prato = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={
                "nome": f"PratoABC_{uuid.uuid4().hex[:4]}",
                "genero_prato": "Prato Principal",
                "rendimento_base_porcoes": 5,
                "itens_receita": [
                    {"insumo_id": str(insumo_id), "tipo": "ALIMENTICIO",
                     "peso_bruto": 0.2, "fator_correcao": 1.0}
                ],
            },
        )
        prato_id = r_prato.json()["id"]
        await client.patch(f"/pratos/{prato_id}/aprovar",
                           headers=auth_headers(token_chef))

        ref = await _criar_refeicao(client, token_chef, pessoas=5)
        await _adicionar_item(client, token_chef, ref["id"], prato_id)

        # Confirmar — dispara trigger de ABC
        await client.patch(f"/refeicoes/{ref['id']}/confirmar",
                           headers=auth_headers(token_chef))

        r_abc = await client.get(f"/refeicoes/{ref['id']}/abc",
                                  headers=auth_headers(token_chef))
        assert r_abc.status_code == 200
        itens = r_abc.json()
        assert len(itens) >= 1
        # Campos obrigatórios
        for campo in ("prato_id", "custo", "percentual_acumulado", "classe"):
            assert campo in itens[0]

    async def test_abc_refeicao_inexistente_retorna_404(
        self, client, token_chef
    ):
        """ABC de refeição com UUID inexistente deve retornar 404."""
        r = await client.get(f"/refeicoes/{uuid.uuid4()}/abc",
                              headers=auth_headers(token_chef))
        assert r.status_code == 404


# =====================================================================
# E) Permissões — criar refeição e adicionar itens
# =====================================================================

@pytest.mark.asyncio
class TestPermissoesRefeicoes:
    """Testa controle de acesso nas rotas de refeições."""

    async def test_chef_pode_criar_refeicao(self, client, token_chef):
        """CHEF pode criar refeição."""
        r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_chef),
            json={
                "genero_refeicao": "Almoço Executivo",
                "data": (date.today() + timedelta(days=3)).isoformat(),
                "horario_inicio": "12:00",
                "horario_fim": "14:00",
                "qtd_pessoas": 10,
            },
        )
        assert r.status_code == 201

    async def test_compras_nao_pode_criar_refeicao(self, client, token_compras):
        """COMPRAS não pode criar refeição."""
        r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_compras),
            json={
                "genero_refeicao": "Almoço Executivo",
                "data": (date.today() + timedelta(days=3)).isoformat(),
                "horario_inicio": "12:00",
                "horario_fim": "14:00",
                "qtd_pessoas": 10,
            },
        )
        assert r.status_code == 403

    async def test_gestao_nao_pode_criar_refeicao(self, client, token_gestao):
        """GESTAO não pode criar refeição."""
        r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_gestao),
            json={
                "genero_refeicao": "Almoço Executivo",
                "data": (date.today() + timedelta(days=3)).isoformat(),
                "horario_inicio": "12:00",
                "horario_fim": "14:00",
                "qtd_pessoas": 10,
            },
        )
        assert r.status_code == 403

    async def test_sem_token_criar_retorna_401(self, client):
        """POST /refeicoes sem autenticação deve retornar 401."""
        r = await client.post(
            "/refeicoes",
            json={
                "genero_refeicao": "Almoço Executivo",
                "data": (date.today() + timedelta(days=3)).isoformat(),
                "horario_inicio": "12:00",
                "horario_fim": "14:00",
                "qtd_pessoas": 10,
            },
        )
        assert r.status_code == 401
