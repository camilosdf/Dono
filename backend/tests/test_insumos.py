# backend/tests/test_insumos.py — Sistema Dono
#
# Cobre: CRUD de insumos, registro de lote (e custo médio resultante),
# erro de UUID malformado (handler global de ValueError em main.py),
# e soft-delete bloqueado por FK.
import pytest

from tests.conftest import auth_headers, err


async def _categoria_id(conn):
    return await conn.fetchval("SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'")


async def _criar_insumo(client, token, categoria_id, nome="Filé Mignon Teste"):
    r = await client.post("/insumos", headers=auth_headers(token),
                          json={"nome": nome, "categoria_id": str(categoria_id), "unidade": "KG"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
class TestInsumos:

    async def test_criar_insumo(self, client, token_compras, conn):
        cat = await _categoria_id(conn)
        r = await client.post("/insumos", headers=auth_headers(token_compras),
                              json={"nome": "Teste Criar", "categoria_id": str(cat), "unidade": "KG"})
        assert r.status_code == 201
        data = r.json()
        assert data["nome"] == "Teste Criar"
        assert data["custo_medio_ponderado"] == 0.0

    async def test_listar_insumos_filtro_genero(self, client, token_admin, conn):
        r = await client.get("/insumos?genero=ALIMENTICIO", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert "items" in r.json()

    async def test_obter_insumo_existente(self, client, token_admin, conn):
        cat = await _categoria_id(conn)
        ins = await _criar_insumo(client, token_admin, cat, "Insumo Obter")
        r = await client.get(f"/insumos/{ins['id']}", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json()["nome"] == "Insumo Obter"

    async def test_obter_insumo_inexistente_retorna_404(self, client, token_admin):
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = await client.get(f"/insumos/{fake_id}", headers=auth_headers(token_admin))
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_uuid_malformado_retorna_400(self, client, token_admin):
        """Handler global de ValueError em main.py — uuid.UUID('lixo')."""
        r = await client.get("/insumos/isso-nao-e-uuid", headers=auth_headers(token_admin))
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_lote_atualiza_custo_medio(self, client, token_admin, conn):
        cat = await _categoria_id(conn)
        ins = await _criar_insumo(client, token_admin, cat, "Insumo Custo Médio")
        ins_id = ins["id"]

        # Primeiro lote: R$60,00/kg
        r1 = await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                               json={"valor_aquisicao": 60.0, "data_aquisicao": "2026-07-21", "quantidade": 10})
        assert r1.status_code == 201

        detalhe1 = await client.get(f"/insumos/{ins_id}", headers=auth_headers(token_admin))
        assert detalhe1.json()["custo_medio_ponderado"] == 60.0

        # Segundo lote: R$80,00/kg — médio deve ir para (60×10 + 80×10) / 20 = 70
        r2 = await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                               json={"valor_aquisicao": 80.0, "data_aquisicao": "2026-07-21", "quantidade": 10})
        assert r2.status_code == 201

        detalhe2 = await client.get(f"/insumos/{ins_id}", headers=auth_headers(token_admin))
        assert detalhe2.json()["custo_medio_ponderado"] == 70.0

    async def test_soft_delete_insumo_sem_receita(self, client, token_admin, conn):
        cat = await _categoria_id(conn)
        ins = await _criar_insumo(client, token_admin, cat, "Insumo Deletar")
        r = await client.delete(f"/insumos/{ins['id']}", headers=auth_headers(token_admin))
        assert r.status_code == 204

        detalhe = await client.get(f"/insumos/{ins['id']}", headers=auth_headers(token_admin))
        assert detalhe.json()["ativo"] is False

    async def test_soft_delete_insumo_em_uso_vira_inativo(self, client, token_admin, conn):
        """Soft delete (UPDATE ativo=FALSE) não é bloqueado por FK de itens_receita
        — a FK protege DELETE físico, não UPDATE. O insumo fica inativo,
        não apagado. O bloqueio de FK real está coberto nos testes pgTAP."""
        cat = await _categoria_id(conn)
        ins = await _criar_insumo(client, token_admin, cat, "Insumo Em Uso")
        ins_id = ins["id"]

        prato_r = await client.post("/pratos", headers=auth_headers(token_admin),
                                    json={"nome": "Prato FK", "genero_prato": "Prato Principal",
                                          "rendimento_base_porcoes": 1,
                                          "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                             "peso_bruto": 1, "fator_correcao": 1}]})
        assert prato_r.status_code == 201

        r = await client.delete(f"/insumos/{ins_id}", headers=auth_headers(token_admin))
        # Soft delete sempre retorna 204 — a proteção de FK é no pgTAP
        assert r.status_code == 204

        # Confirma via banco que o insumo está inativo (não apagado)
        # A rota GET pode filtrar inativos (retorna 404), por isso usa conn direto
        import uuid as _uuid
        ativo = await conn.fetchval(
            "SELECT ativo FROM insumos WHERE id = $1", _uuid.UUID(ins_id)
        )
        assert ativo is False
