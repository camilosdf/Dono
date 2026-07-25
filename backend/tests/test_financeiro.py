# backend/tests/test_financeiro.py — Sistema Dono
#
# Testes de integração para o módulo financeiro (contas a pagar e a receber).
#
# Cobre:
#   - CRUD de contas a pagar (criação, listagem, pagamento, cancelamento)
#   - CRUD de contas a receber (criação, listagem, recebimento, cancelamento)
#   - Resumo financeiro (dashboard)
#   - Filtros e paginação
#   - Validações e erros (400, 404, 403)
#   - RBAC (perfis autorizados: ADMIN, GESTAO, COMPRAS)
#
# Depende das fixtures do conftest.py: client, token_admin, token_gestao, token_compras, token_chef, conn.

import uuid
from datetime import date, timedelta

import pytest
from tests.conftest import auth_headers, err

pytestmark = pytest.mark.asyncio


# ---------- Helpers ----------

async def _criar_fornecedor(conn, nome: str = "Fornecedor Teste") -> uuid.UUID:
    """Cria um fornecedor de teste e retorna seu ID."""
    row = await conn.fetchrow(
        "INSERT INTO fornecedores (nome, ativo) VALUES ($1, TRUE) RETURNING id",
        nome
    )
    return row["id"]


async def _criar_conta_pagar(
    conn,
    fornecedor_id: uuid.UUID,
    valor: float = 100.0,
    vencimento: date = None,
    descricao: str = "Conta de teste"
) -> uuid.UUID:
    if vencimento is None:
        vencimento = date.today() + timedelta(days=30)
    row = await conn.fetchrow(
        """INSERT INTO contas_pagar
           (fornecedor_id, descricao, valor_original, data_vencimento, status)
           VALUES ($1, $2, $3, $4, 'PENDENTE')
           RETURNING id""",
        fornecedor_id, descricao, valor, vencimento
    )
    return row["id"]


async def _criar_conta_receber(
    conn,
    valor: float = 200.0,
    vencimento: date = None,
    descricao: str = "Recebimento de teste"
) -> uuid.UUID:
    if vencimento is None:
        vencimento = date.today() + timedelta(days=15)
    row = await conn.fetchrow(
        """INSERT INTO contas_receber
           (descricao, valor_original, data_vencimento, status)
           VALUES ($1, $2, $3, 'PENDENTE')
           RETURNING id""",
        descricao, valor, vencimento
    )
    return row["id"]


# ---------- Testes: Contas a Pagar ----------

class TestContasPagar:

    async def test_criar_conta_pagar(self, client, token_admin, conn):
        """POST /financeiro/contas-pagar deve criar uma conta com sucesso."""
        fornecedor_id = await _criar_fornecedor(conn)
        payload = {
            "fornecedor_id": str(fornecedor_id),
            "descricao": "Compra de insumos",
            "valor_original": 1500.50,
            "data_vencimento": (date.today() + timedelta(days=30)).isoformat(),
            "tipo_despesa": "COMPRA_INSUMO"
        }
        r = await client.post(
            "/financeiro/contas-pagar",
            headers=auth_headers(token_admin),
            json=payload
        )
        assert r.status_code == 201
        data = r.json()
        assert "id" in data
        assert "criado_em" in data

        # Verifica no banco
        row = await conn.fetchrow("SELECT * FROM contas_pagar WHERE id = $1", uuid.UUID(data["id"]))
        assert row["fornecedor_id"] == fornecedor_id
        assert row["descricao"] == "Compra de insumos"
        assert float(row["valor_original"]) == 1500.50
        assert row["status"] == "PENDENTE"

    async def test_criar_conta_pagar_sem_fornecedor(self, client, token_admin):
        """Deve retornar 422 se fornecedor_id for omitido."""
        payload = {"descricao": "Teste", "valor_original": 100, "data_vencimento": date.today().isoformat()}
        r = await client.post(
            "/financeiro/contas-pagar",
            headers=auth_headers(token_admin),
            json=payload
        )
        assert r.status_code == 422

    async def test_listar_contas_pagar(self, client, token_admin, conn):
        """GET /financeiro/contas-pagar deve listar contas com paginação."""
        fornecedor_id = await _criar_fornecedor(conn)
        await _criar_conta_pagar(conn, fornecedor_id, 100.0)
        await _criar_conta_pagar(conn, fornecedor_id, 200.0)

        r = await client.get(
            "/financeiro/contas-pagar",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data
        assert data["total"] >= 2
        assert len(data["items"]) > 0

    async def test_listar_contas_pagar_com_filtro_status(self, client, token_admin, conn):
        """Deve filtrar por status."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 100.0)
        # Paga a conta
        await conn.execute(
            "UPDATE contas_pagar SET status = 'PAGO', valor_pago = 100.0, data_pagamento = now() WHERE id = $1",
            conta_id
        )
        # Cria outra pendente
        await _criar_conta_pagar(conn, fornecedor_id, 200.0)

        r = await client.get(
            "/financeiro/contas-pagar?status=PAGO",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "PAGO"

    async def test_listar_contas_pagar_com_filtro_fornecedor(self, client, token_admin, conn):
        """Deve filtrar por fornecedor."""
        f1 = await _criar_fornecedor(conn, "F1")
        f2 = await _criar_fornecedor(conn, "F2")
        await _criar_conta_pagar(conn, f1, 100.0)
        await _criar_conta_pagar(conn, f2, 200.0)

        r = await client.get(
            f"/financeiro/contas-pagar?fornecedor_id={f1}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert data["items"][0]["fornecedor_id"] == str(f1)

    async def test_listar_contas_pagar_periodo_invalido(self, client, token_admin):
        """data_vencimento_inicio > data_vencimento_fim deve retornar 400."""
        hoje = date.today()
        r = await client.get(
            f"/financeiro/contas-pagar?data_vencimento_inicio={hoje + timedelta(days=10)}&data_vencimento_fim={hoje}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 400
        assert err(r) == "PERIODO_INVALIDO"

    async def test_pagar_conta_total(self, client, token_compras, conn):
        """PATCH /financeiro/contas-pagar/{id}/pagar deve pagar totalmente."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 500.0)

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/pagar",
            headers=auth_headers(token_compras),
            json={"valor_pago": 500.0}
        )
        assert r.status_code == 200

        # Verifica status
        row = await conn.fetchrow("SELECT status, valor_pago FROM contas_pagar WHERE id = $1", conta_id)
        assert row["status"] == "PAGO"
        assert float(row["valor_pago"]) == 500.0

    async def test_pagar_conta_parcial(self, client, token_compras, conn):
        """Pagamento parcial deve marcar como PAGO_PARCIAL."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 500.0)

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/pagar",
            headers=auth_headers(token_compras),
            json={"valor_pago": 200.0}
        )
        assert r.status_code == 200

        row = await conn.fetchrow("SELECT status, valor_pago FROM contas_pagar WHERE id = $1", conta_id)
        assert row["status"] == "PAGO_PARCIAL"
        assert float(row["valor_pago"]) == 200.0

    async def test_pagar_conta_com_valor_excedente(self, client, token_compras, conn):
        """Valor pago > valor original deve retornar 400."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 500.0)

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/pagar",
            headers=auth_headers(token_compras),
            json={"valor_pago": 600.0}
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_pagar_conta_ja_paga(self, client, token_compras, conn):
        """Tentar pagar uma conta já paga deve retornar 400."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 100.0)
        await conn.execute(
            "UPDATE contas_pagar SET status = 'PAGO', valor_pago = 100.0 WHERE id = $1",
            conta_id
        )

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/pagar",
            headers=auth_headers(token_compras),
            json={"valor_pago": 50.0}
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_cancelar_conta_pagar(self, client, token_admin, conn):
        """PATCH /financeiro/contas-pagar/{id}/cancelar deve cancelar a conta."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 300.0)

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/cancelar",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200

        row = await conn.fetchrow("SELECT status FROM contas_pagar WHERE id = $1", conta_id)
        assert row["status"] == "CANCELADO"

    async def test_cancelar_conta_pagar_ja_paga(self, client, token_admin, conn):
        """Cancelar conta já paga deve retornar 404/erro."""
        fornecedor_id = await _criar_fornecedor(conn)
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 100.0)
        await conn.execute(
            "UPDATE contas_pagar SET status = 'PAGO', valor_pago = 100.0 WHERE id = $1",
            conta_id
        )

        r = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/cancelar",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 404

    async def test_contas_pagar_permissao_compras(self, client, token_compras, conn):
        """COMPRAS pode criar e pagar contas."""
        fornecedor_id = await _criar_fornecedor(conn)
        # Criar
        payload = {
            "fornecedor_id": str(fornecedor_id),
            "descricao": "Compra",
            "valor_original": 100,
            "data_vencimento": date.today().isoformat()
        }
        r = await client.post(
            "/financeiro/contas-pagar",
            headers=auth_headers(token_compras),
            json=payload
        )
        assert r.status_code == 201
        conta_id = r.json()["id"]

        # Pagar
        r2 = await client.patch(
            f"/financeiro/contas-pagar/{conta_id}/pagar",
            headers=auth_headers(token_compras),
            json={"valor_pago": 100}
        )
        assert r2.status_code == 200

    async def test_contas_pagar_permissao_chef_negada(self, client, token_chef, conn):
        """CHEF não pode criar contas a pagar."""
        fornecedor_id = await _criar_fornecedor(conn)
        payload = {
            "fornecedor_id": str(fornecedor_id),
            "descricao": "Teste",
            "valor_original": 100,
            "data_vencimento": date.today().isoformat()
        }
        r = await client.post(
            "/financeiro/contas-pagar",
            headers=auth_headers(token_chef),
            json=payload
        )
        assert r.status_code == 403

    async def test_contas_pagar_inexistente(self, client, token_admin):
        """Pagar uma conta que não existe deve retornar 404."""
        fake_id = uuid.uuid4()
        r = await client.patch(
            f"/financeiro/contas-pagar/{fake_id}/pagar",
            headers=auth_headers(token_admin),
            json={"valor_pago": 100}
        )
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"


# ---------- Testes: Contas a Receber ----------

class TestContasReceber:

    async def test_criar_conta_receber(self, client, token_admin, conn):
        """POST /financeiro/contas-receber deve criar com sucesso."""
        payload = {
            "descricao": "Venda de menu executivo",
            "valor_original": 2500.00,
            "data_vencimento": (date.today() + timedelta(days=15)).isoformat(),
            "cliente_nome": "Empresa XYZ"
        }
        r = await client.post(
            "/financeiro/contas-receber",
            headers=auth_headers(token_admin),
            json=payload
        )
        assert r.status_code == 201
        data = r.json()
        assert "id" in data

        row = await conn.fetchrow("SELECT * FROM contas_receber WHERE id = $1", uuid.UUID(data["id"]))
        assert row["descricao"] == "Venda de menu executivo"
        assert float(row["valor_original"]) == 2500.00
        assert row["status"] == "PENDENTE"

    async def test_listar_contas_receber(self, client, token_admin, conn):
        """GET /financeiro/contas-receber deve listar."""
        await _criar_conta_receber(conn, 100.0)
        await _criar_conta_receber(conn, 200.0)

        r = await client.get(
            "/financeiro/contas-receber",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 2

    async def test_receber_conta_total(self, client, token_gestao, conn):
        """PATCH /financeiro/contas-receber/{id}/receber deve receber totalmente."""
        conta_id = await _criar_conta_receber(conn, 600.0)

        r = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/receber",
            headers=auth_headers(token_gestao),
            json={"valor_recebido": 600.0}
        )
        assert r.status_code == 200

        row = await conn.fetchrow("SELECT status, valor_recebido FROM contas_receber WHERE id = $1", conta_id)
        assert row["status"] == "RECEBIDO"
        assert float(row["valor_recebido"]) == 600.0

    async def test_receber_conta_parcial(self, client, token_gestao, conn):
        """Recebimento parcial deve marcar como RECEBIDO_PARCIAL."""
        conta_id = await _criar_conta_receber(conn, 500.0)

        r = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/receber",
            headers=auth_headers(token_gestao),
            json={"valor_recebido": 200.0}
        )
        assert r.status_code == 200

        row = await conn.fetchrow("SELECT status, valor_recebido FROM contas_receber WHERE id = $1", conta_id)
        assert row["status"] == "RECEBIDO_PARCIAL"
        assert float(row["valor_recebido"]) == 200.0

    async def test_receber_conta_valor_excedente(self, client, token_gestao, conn):
        """Valor recebido > valor original deve retornar 400."""
        conta_id = await _criar_conta_receber(conn, 300.0)

        r = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/receber",
            headers=auth_headers(token_gestao),
            json={"valor_recebido": 400.0}
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_cancelar_conta_receber(self, client, token_admin, conn):
        """Cancelar conta a receber."""
        conta_id = await _criar_conta_receber(conn, 100.0)

        r = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/cancelar",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200

        row = await conn.fetchrow("SELECT status FROM contas_receber WHERE id = $1", conta_id)
        assert row["status"] == "CANCELADO"

    async def test_receber_conta_ja_recebida(self, client, token_gestao, conn):
        """Receber conta já recebida deve retornar 400."""
        conta_id = await _criar_conta_receber(conn, 100.0)
        await conn.execute(
            "UPDATE contas_receber SET status = 'RECEBIDO', valor_recebido = 100.0 WHERE id = $1",
            conta_id
        )

        r = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/receber",
            headers=auth_headers(token_gestao),
            json={"valor_recebido": 50.0}
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_contas_receber_permissao_gestao(self, client, token_gestao, conn):
        """GESTAO pode criar e receber contas."""
        payload = {
            "descricao": "Evento",
            "valor_original": 1000,
            "data_vencimento": date.today().isoformat()
        }
        r = await client.post(
            "/financeiro/contas-receber",
            headers=auth_headers(token_gestao),
            json=payload
        )
        assert r.status_code == 201
        conta_id = r.json()["id"]

        r2 = await client.patch(
            f"/financeiro/contas-receber/{conta_id}/receber",
            headers=auth_headers(token_gestao),
            json={"valor_recebido": 1000}
        )
        assert r2.status_code == 200

    async def test_contas_receber_permissao_compras_negada(self, client, token_compras):
        """COMPRAS NÃO pode criar contas a receber."""
        payload = {
            "descricao": "Teste",
            "valor_original": 100,
            "data_vencimento": date.today().isoformat()
        }
        r = await client.post(
            "/financeiro/contas-receber",
            headers=auth_headers(token_compras),
            json=payload
        )
        assert r.status_code == 403


# ---------- Testes: Resumo Financeiro ----------

class TestResumoFinanceiro:

    async def test_resumo_financeiro(self, client, token_admin, conn):
        """GET /financeiro/resumo deve retornar os totais."""
        # Cria algumas contas
        fornecedor_id = await _criar_fornecedor(conn)
        await _criar_conta_pagar(conn, fornecedor_id, 1000.0)  # pendente
        await _criar_conta_receber(conn, 500.0)                 # pendente

        # Cria uma conta atrasada
        conta_id = await _criar_conta_pagar(conn, fornecedor_id, 200.0, vencimento=date.today() - timedelta(days=5))
        # Força status ATRASADO (não automaticamente, mas podemos setar manualmente para teste)
        await conn.execute("UPDATE contas_pagar SET status = 'ATRASADO' WHERE id = $1", conta_id)

        r = await client.get(
            "/financeiro/resumo",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert "total_a_pagar" in data
        assert "total_atrasado_pagar" in data
        assert "total_a_receber" in data
        assert "total_atrasado_receber" in data
        assert "saldo_previsto" in data

        # Verifica valores (aproximados)
        assert data["total_a_pagar"] >= 1000.0  # pelo menos a conta pendente
        assert data["total_atrasado_pagar"] >= 200.0
        assert data["total_a_receber"] >= 500.0
        assert data["saldo_previsto"] <= 500.0  # 500 - 1000 = -500 (ou menos)

    async def test_resumo_financeiro_sem_dados(self, client, token_admin):
        """Resumo sem dados deve retornar zeros."""
        r = await client.get(
            "/financeiro/resumo",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_a_pagar"] == 0.0
        assert data["total_atrasado_pagar"] == 0.0
        assert data["total_a_receber"] == 0.0
        assert data["total_atrasado_receber"] == 0.0
        assert data["saldo_previsto"] == 0.0

    async def test_resumo_permissao_gestao(self, client, token_gestao):
        """GESTAO pode acessar o resumo."""
        r = await client.get(
            "/financeiro/resumo",
            headers=auth_headers(token_gestao)
        )
        assert r.status_code == 200

    async def test_resumo_permissao_chef_negada(self, client, token_chef):
        """CHEF NÃO pode acessar o resumo."""
        r = await client.get(
            "/financeiro/resumo",
            headers=auth_headers(token_chef)
        )
        assert r.status_code == 403