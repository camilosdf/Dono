# backend/tests/test_previsoes.py — Sistema Dono
#
# Testes de integração para o módulo de previsão de consumo (Fase 4).
# Cobre as rotas:
#   - GET /previsoes/insumos
#   - GET /previsoes/consumo
#   - GET /previsoes/resumo/{insumo_id}
#   - GET /previsoes/comparacao
#
# Os testes preparam dados de insumo e movimentações históricas, executam
# a função SQL fn_atualizar_previsoes_consumo para gerar previsões, e
# validam as respostas da API.
#
# Depende das fixtures do conftest.py: client, token_admin, token_gestao, token_compras, conn.

import uuid
from datetime import date, timedelta

import pytest
from tests.conftest import auth_headers, err


@pytest.mark.asyncio
class TestPrevisoesAPI:

    async def _setup_insumo_com_historico(self, client, token_admin, conn):
        """Cria um insumo de teste, um lote e movimentações de consumo nos últimos 30 dias.
        Retorna o ID do insumo criado."""
        # 1. Cria a categoria
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
        assert cat_id is not None, "Categoria 'Carnes, Aves e Peixes' não encontrada nos seeds"

        # 2. Cria o insumo
        ins_r = await client.post(
            "/insumos",
            headers=auth_headers(token_admin),
            json={"nome": "Insumo Previsão Teste", "categoria_id": str(cat_id), "unidade": "KG"}
        )
        assert ins_r.status_code == 201
        ins_id = ins_r.json()["id"]

        # 3. Cria um lote para o insumo
        lote_r = await client.post(
            f"/insumos/{ins_id}/lotes",
            headers=auth_headers(token_admin),
            json={"valor_aquisicao": 10.0, "data_aquisicao": "2025-01-01", "quantidade": 100}
        )
        assert lote_r.status_code == 201
        lote_id = lote_r.json()["id"]

        # 4. Insere movimentações de consumo nos últimos 30 dias (10, 8, 12, etc.)
        # Cria um padrão: consumo médio ~10kg/dia com variação
        for i in range(1, 31):
            quantidade = 8 + (i % 5)  # varia entre 8 e 12
            data_consumo = date.today() - timedelta(days=i)
            await conn.execute(
                """INSERT INTO movimentacoes_estoque (lote_insumo_id, insumo_id, quantidade, tipo, criado_em)
                   VALUES ($1, $2, $3, 'BAIXA_EXECUCAO', $4)""",
                uuid.UUID(lote_id),
                uuid.UUID(ins_id),
                quantidade,
                data_consumo
            )

        return ins_id

    async def _gerar_previsoes(self, conn, insumo_id=None):
        """Chama a função SQL que gera previsões para todos os insumos ou um específico.
        Por simplicidade, gera para todos, mas pode ser filtrado.
        """
        # Para garantir que a tabela previsoes_consumo seja populada
        await conn.execute("SELECT fn_atualizar_previsoes_consumo(10, 30)")

    # -----------------------------------------------------------------
    # Testes dos endpoints
    # -----------------------------------------------------------------

    async def test_listar_insumos_com_previsao(self, client, token_admin, token_gestao, token_compras, conn):
        """GET /previsoes/insumos deve retornar lista de insumos com previsão para data atual."""
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        await self._gerar_previsoes(conn)

        # Testa com token de gestão
        r = await client.get("/previsoes/insumos", headers=auth_headers(token_gestao))
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) > 0
        # Verifica se o insumo criado está na lista
        insumo_encontrado = any(item["insumo_id"] == ins_id for item in data)
        assert insumo_encontrado, f"Insumo {ins_id} não encontrado na lista de insumos com previsão"

        # Testa com token de compras (também tem permissão)
        r2 = await client.get("/previsoes/insumos", headers=auth_headers(token_compras))
        assert r2.status_code == 200

    async def test_listar_insumos_com_previsao_data_especifica(self, client, token_gestao, conn):
        """Pode filtrar por data_referencia (data específica)."""
        # Como não controlamos a data, apenas verificamos se a rota aceita o parâmetro
        data_alvo = date.today() + timedelta(days=5)
        r = await client.get(
            f"/previsoes/insumos?data_referencia={data_alvo.isoformat()}",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 200
        # Não falha mesmo que não haja dados

    async def test_consultar_previsoes_com_filtros(self, client, token_gestao, token_admin, conn):
        """GET /previsoes/consumo com filtros de insumo, período e versão."""
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        await self._gerar_previsoes(conn)

        hoje = date.today()
        data_fim = hoje + timedelta(days=10)

        # Filtro por insumo
        r = await client.get(
            f"/previsoes/consumo?insumo_id={ins_id}",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] > 0
        assert all(item["insumo_id"] == ins_id for item in data["items"])

        # Filtro por período
        r2 = await client.get(
            f"/previsoes/consumo?data_inicio={hoje.isoformat()}&data_fim={data_fim.isoformat()}",
            headers=auth_headers(token_gestao)
        )
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["total"] > 0

        # Combinação: insumo + período
        r3 = await client.get(
            f"/previsoes/consumo?insumo_id={ins_id}&data_inicio={hoje.isoformat()}&data_fim={data_fim.isoformat()}",
            headers=auth_headers(token_gestao)
        )
        assert r3.status_code == 200
        data3 = r3.json()
        assert data3["total"] > 0

    async def test_consultar_previsoes_sem_filtro(self, client, token_gestao, conn):
        """Sem filtros, deve retornar as previsões mais recentes para todos os insumos."""
        # Apenas verifica se a rota responde 200, mesmo que não haja dados
        r = await client.get("/previsoes/consumo", headers=auth_headers(token_gestao))
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data

    async def test_resumo_previsao_insumo(self, client, token_gestao, token_admin, conn):
        """GET /previsoes/resumo/{insumo_id} retorna resumo estatístico."""
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        await self._gerar_previsoes(conn)

        r = await client.get(
            f"/previsoes/resumo/{ins_id}?dias=7",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["insumo_id"] == ins_id
        assert data["dias_previstos"] == 7
        assert data["total_previsto"] > 0
        # total_real pode ser None ou 0
        assert "total_real" in data
        assert "acuracia" in data

    async def test_resumo_previsao_insumo_inexistente(self, client, token_gestao):
        """Retorna 404 se insumo não tiver previsões."""
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = await client.get(
            f"/previsoes/resumo/{fake_id}?dias=7",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 404
        assert err(r) == "PREVISAO_NAO_ENCONTRADA"

    async def test_comparacao_previsao_real(self, client, token_gestao, token_admin, conn):
        """GET /previsoes/comparacao retorna comparação entre previsão e real para um dia."""
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        await self._gerar_previsoes(conn)

        # Escolhe um dia que tem previsão (ex.: amanhã, já que hoje pode não ter)
        dia_alvo = date.today() + timedelta(days=1)

        r = await client.get(
            f"/previsoes/comparacao?insumo_id={ins_id}&data_referencia={dia_alvo.isoformat()}",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["insumo_id"] == ins_id
        assert data["data_referencia"] == dia_alvo.isoformat()
        assert "previsto" in data
        assert "real" in data
        assert "diferenca" in data
        # percentual e acuracia podem ser None se real==0, mas devem existir

    async def test_comparacao_sem_previsao(self, client, token_gestao, token_admin, conn):
        """Se não houver previsão para a data, retorna 404."""
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        # Não gera previsões para garantir que não haja
        dia_alvo = date.today() + timedelta(days=100)
        r = await client.get(
            f"/previsoes/comparacao?insumo_id={ins_id}&data_referencia={dia_alvo.isoformat()}",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 404
        assert err(r) == "PREVISAO_NAO_ENCONTRADA"

    async def test_periodo_invalido_retorna_400(self, client, token_gestao):
        """data_fim anterior a data_inicio deve retornar 400."""
        hoje = date.today()
        r = await client.get(
            f"/previsoes/consumo?data_inicio={hoje.isoformat()}&data_fim={hoje - timedelta(days=1)}",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 400
        assert err(r) == "PERIODO_INVALIDO"

    async def test_permissoes_previsoes(self, client, token_chef, token_compras, token_gestao):
        """Apenas COMPRAS, GESTAO e ADMIN podem acessar as rotas de previsão."""
        endpoints = [
            ("GET", "/previsoes/insumos"),
            ("GET", "/previsoes/consumo"),
        ]
        # Chef não deve ter acesso
        for method, path in endpoints:
            if method == "GET":
                r = await client.get(path, headers=auth_headers(token_chef))
                assert r.status_code == 403, f"Chef acessou {path} indevidamente"

        # Compras e Gestão devem ter acesso
        for token in [token_compras, token_gestao]:
            for method, path in endpoints:
                if method == "GET":
                    r = await client.get(path, headers=auth_headers(token))
                    assert r.status_code in (200, 404), f"Perfil {token} não conseguiu acessar {path}"

    # -----------------------------------------------------------------
    # Testes de integração com o worker (opcional)
    # -----------------------------------------------------------------

    async def test_forecast_worker_roda_sem_erro(self, client, token_admin, conn):
        """Testa se a função fn_atualizar_previsoes_consumo executa sem erro."""
        # Cria insumo e histórico
        ins_id = await self._setup_insumo_com_historico(client, token_admin, conn)
        # Executa a função
        try:
            await conn.execute("SELECT fn_atualizar_previsoes_consumo(10, 30)")
        except Exception as e:
            pytest.fail(f"fn_atualizar_previsoes_consumo falhou: {e}")

        # Verifica se a tabela foi populada
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM previsoes_consumo WHERE insumo_id = $1",
            uuid.UUID(ins_id)
        )
        assert count > 0, "Nenhuma previsão foi inserida para o insumo"