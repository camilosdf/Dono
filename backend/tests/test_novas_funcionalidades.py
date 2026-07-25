# backend/tests/test_novas_funcionalidades.py
import asyncio
import uuid

import asyncpg
import pytest
from tests.conftest import auth_headers, err


@pytest.mark.asyncio
class TestAuditoria:

    async def test_movimentacao_tem_auditoria(self, client, token_admin, token_chef, conn):
        """Verifica se a execução de uma refeição preenche usuario_id, ip_origem e user_agent."""
        # 1. Setup: insumo, prato, refeição confirmada
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo Audit", "categoria_id": str(cat_id), "unidade": "KG"})
        ins_id = ins_r.json()["id"]
        await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 10, "data_aquisicao": "2025-01-01", "quantidade": 100})

        prato_r = await client.post("/pratos", headers=auth_headers(token_chef),
                                    json={"nome": "Prato Audit", "genero_prato": "Prato Principal",
                                          "rendimento_base_porcoes": 5,
                                          "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                             "peso_bruto": 2, "fator_correcao": 1}]})
        prato_id = prato_r.json()["id"]

        ref_r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                                  json={"genero_refeicao": "Almoço Executivo", "data": "2025-01-10",
                                        "horario_inicio": "12:00", "horario_fim": "15:00", "qtd_pessoas": 5})
        ref_id = ref_r.json()["id"]
        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))

        # 2. Executa a refeição (deve disparar INSERT com auditoria)
        await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))

        # 3. Verifica a movimentação gerada
        mov = await conn.fetchrow(
            """SELECT usuario_id, ip_origem, user_agent FROM movimentacoes_estoque
               WHERE refeicao_id = $1 LIMIT 1""",
            uuid.UUID(ref_id)
        )
        # O token_chef pertence ao usuário "Chef Teste" (criado em conftest).
        # O ID deve estar presente, não nulo.
        assert mov is not None
        assert mov["usuario_id"] is not None
        assert mov["ip_origem"] is not None # O cliente de teste sempre usa "test"
        assert mov["user_agent"] is not None


@pytest.mark.asyncio
class TestWorkerHardening:

    async def test_worker_retry_e_bloqueio(self, db_pool):
        """Simula um evento com erro e verifica retry + bloqueio após 3 falhas."""
        # 1. Insere um evento com payload inválido (ex.: UUID malformado) para forçar erro
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO eventos_dominio (tipo, payload)
                   VALUES ('PrecoAtualizado', '{"insumo_id": "uuid-invalido"}')"""
            )
            
            # 2. Chama o worker 4 vezes (deveria falhar nas 3 primeiras e bloquear na 4ª)
            for i in range(4):
                await conn.execute("SELECT fn_processar_eventos_pendentes();")

            # 3. Verifica o estado do evento
            ev = await conn.fetchrow(
                """SELECT id, tentativas, processado, ultimo_erro, bloqueado_em
                   FROM eventos_dominio WHERE payload->>'insumo_id' = 'uuid-invalido'"""
            )
            # Após 4 chamadas, deveria ter tentado 3 vezes (a 4ª chamada já encontra bloqueado)
            # e ter bloqueado_em preenchido
            assert ev["processado"] is True
            assert ev["tentativas"] == 3
            assert ev["bloqueado_em"] is not None
            assert "FALHA_PERMANENTE" in ev["ultimo_erro"]


@pytest.mark.asyncio
class TestPerdasEAjustes:

    async def test_registrar_perda_com_lote_especifico(self, client, token_admin, token_compras, conn):
        # Setup: insumo + lote
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo Perda", "categoria_id": str(cat_id), "unidade": "KG"})
        ins_id = ins_r.json()["id"]
        
        lote_r = await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                                   json={"valor_aquisicao": 15, "data_aquisicao": "2025-01-01", "quantidade": 10})
        lote_id = lote_r.json()["id"]

        # 2. Registra perda de 2kg via API (compras tem permissão)
        payload = {
            "insumo_id": ins_id,
            "quantidade": 2.0,
            "tipo_perda": "VALIDADE",
            "observacao": "Lote venceu antes do uso",
            "lote_id": lote_id
        }
        r = await client.post("/movimentacoes/perda", headers=auth_headers(token_compras), json=payload)
        assert r.status_code == 201, r.text

        # 3. Verifica no banco
        mov = await conn.fetchrow(
            "SELECT quantidade, tipo, tipo_perda_id, observacao, usuario_id FROM movimentacoes_estoque WHERE insumo_id = $1",
            uuid.UUID(ins_id)
        )
        assert mov["quantidade"] == 2.0
        assert mov["tipo"] == "AJUSTE_MANUAL"
        assert mov["observacao"] == "Lote venceu antes do uso"
        assert mov["usuario_id"] is not None  # auditoria

        # 4. Verifica se o estoque do lote diminuiu (10 - 2 = 8)
        lote_atual = await conn.fetchval("SELECT quantidade_disponivel FROM lotes_insumo WHERE id = $1", uuid.UUID(lote_id))
        assert lote_atual == 8.0

    async def test_registrar_perda_sem_lote_aplica_fefo(self, client, token_compras, token_admin, conn):
        # Setup: um insumo com dois lotes (validades diferentes)
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo FEFO", "categoria_id": str(cat_id), "unidade": "KG"})
        ins_id = ins_r.json()["id"]
        
        # Lote 1: vence amanhã (será consumido primeiro)
        await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 10, "data_aquisicao": "2025-01-01", "quantidade": 5, "data_validade": "2025-01-10"})
        # Lote 2: vence depois
        await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 12, "data_aquisicao": "2025-01-02", "quantidade": 5, "data_validade": "2025-02-10"})

        # 2. Perda de 6kg (sem lote_id) -> deve consumir 5 do lote1 e 1 do lote2
        payload = {"insumo_id": ins_id, "quantidade": 6.0, "tipo_perda": "PRODUCAO"}
        r = await client.post("/movimentacoes/perda", headers=auth_headers(token_compras), json=payload)
        assert r.status_code == 201

        # 3. Verifica os lotes
        lotes = await conn.fetch("SELECT quantidade_disponivel FROM lotes_insumo WHERE insumo_id = $1 ORDER BY data_validade", uuid.UUID(ins_id))
        # Lote1: 5 - 5 = 0; Lote2: 5 - 1 = 4
        assert lotes[0]["quantidade_disponivel"] == 0.0
        assert lotes[1]["quantidade_disponivel"] == 4.0

    async def test_relatorio_consumo_inclui_perdas(self, client, token_admin, token_gestao, token_compras, conn):
        # 1. Cria uma perda (para ter dados)
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Bebidas'")
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo Relatorio", "categoria_id": str(cat_id), "unidade": "L"})
        ins_id = ins_r.json()["id"]
        await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 5, "data_aquisicao": "2025-01-01", "quantidade": 10})

        payload = {"insumo_id": ins_id, "quantidade": 3.0, "tipo_perda": "QUEBRA"}
        await client.post("/movimentacoes/perda", headers=auth_headers(token_compras), json=payload)

        # 2. Acessa o relatório de consumo
        r = await client.get("/relatorios/consumo", headers=auth_headers(token_gestao))
        assert r.status_code == 200
        data = r.json()

        # 3. Verifica se o campo "perdas" existe e contém a perda registrada
        assert "perdas" in data
        assert len(data["perdas"]) > 0
        perda = data["perdas"][0]
        assert perda["tipo_perda_nome"] == "QUEBRA"
        assert perda["quantidade_perdida"] == 3.0