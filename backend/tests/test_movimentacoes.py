# backend/tests/test_movimentacoes.py — Sistema Dono
#
# Cobre:
#   - GET /movimentacoes/tipos-perda (listagem de catálogo)
#   - POST /movimentacoes/perda (lote específico e FEFO)
#   - POST /movimentacoes/perda — erros (estoque insuficiente, tipo inválido, UUID inválido)
#   - GET /movimentacoes (listagem com filtros: insumo_id, tipo, periodo)
#   - Permissões RBAC
#
# Complementa test_novas_funcionalidades.py que já cobre:
#   - auditoria (usuario_id, ip_origem, user_agent)
#   - perda com lote específico e FEFO
#   - relatório de consumo com perdas
# Este arquivo foca nos contratos HTTP e nas lacunas de cobertura.

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

async def _criar_insumo_com_lote(client, token_admin, conn,
                                  nome="Insumo Mov Teste",
                                  categoria="Carnes, Aves e Peixes",
                                  valor=10.0, qty=20.0,
                                  data_validade=None):
    cat_id = await conn.fetchval(
        "SELECT id FROM categorias WHERE nome = $1", categoria
    )
    r = await client.post(
        "/insumos",
        headers=auth_headers(token_admin),
        json={"nome": nome, "categoria_id": str(cat_id), "unidade": "KG"},
    )
    ins_id = r.json()["id"]
    lote_payload = {
        "valor_aquisicao": valor,
        "data_aquisicao": "2026-08-01",
        "quantidade": qty,
    }
    if data_validade:
        lote_payload["data_validade"] = data_validade
    lote_r = await client.post(
        f"/insumos/{ins_id}/lotes",
        headers=auth_headers(token_admin),
        json=lote_payload,
    )
    return ins_id, lote_r.json()["id"]


# =====================================================================
# Tipos de Perda
# =====================================================================

@pytest.mark.asyncio
class TestTiposPerda:

    async def test_listar_tipos_perda(self, client, token_compras):
        r = await client.get("/movimentacoes/tipos-perda",
                             headers=auth_headers(token_compras))
        assert r.status_code == 200
        tipos = r.json()
        assert len(tipos) > 0
        # Seeds devem incluir pelo menos VALIDADE, QUEBRA, PRODUCAO
        nomes = {t["nome"] for t in tipos}
        assert "VALIDADE" in nomes
        assert "QUEBRA" in nomes

    async def test_tipos_perda_possuem_campos_obrigatorios(self, client, token_admin):
        r = await client.get("/movimentacoes/tipos-perda",
                             headers=auth_headers(token_admin))
        for t in r.json():
            assert "id" in t
            assert "nome" in t

    async def test_somente_autenticados_acessam_tipos_perda(self, client):
        r = await client.get("/movimentacoes/tipos-perda")
        assert r.status_code == 401


# =====================================================================
# Registrar Perda
# =====================================================================

@pytest.mark.asyncio
class TestRegistrarPerda:

    async def test_registrar_perda_lote_especifico(self, client, token_admin,
                                                    token_compras, conn):
        ins_id, lote_id = await _criar_insumo_com_lote(client, token_admin, conn, qty=10.0)

        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={
                "insumo_id": ins_id,
                "quantidade": 3.0,
                "tipo_perda": "VALIDADE",
                "observacao": "Produto vencido",
                "lote_id": lote_id,
            },
        )
        assert r.status_code == 201
        assert "message" in r.json()

        # Estoque deve ter reduzido
        lotes = await client.get(f"/insumos/{ins_id}/lotes",
                                 headers=auth_headers(token_admin))
        assert lotes.json()[0]["quantidade_disponivel"] == pytest.approx(7.0)

    async def test_registrar_perda_fefo(self, client, token_admin,
                                        token_compras, conn):
        """Sem lote_id: deve consumir primeiro o lote com validade mais próxima."""
        ins_id, _ = await _criar_insumo_com_lote(
            client, token_admin, conn, nome="Insumo FEFO A",
            qty=5.0, data_validade="2026-08-10"
        )
        # Segundo lote — vence depois
        await client.post(
            f"/insumos/{ins_id}/lotes",
            headers=auth_headers(token_admin),
            json={"valor_aquisicao": 12.0, "data_aquisicao": "2026-08-02",
                  "quantidade": 5.0, "data_validade": "2026-09-10"},
        )

        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 4.0, "tipo_perda": "QUEBRA"},
        )
        assert r.status_code == 201

        lotes = await client.get(f"/insumos/{ins_id}/lotes",
                                 headers=auth_headers(token_admin))
        qtds = {l["data_validade"]: l["quantidade_disponivel"] for l in lotes.json()}
        # Lote que vence primeiro deve ter sido consumido (5 - 4 = 1 ou 0 + sobra)
        assert qtds["2026-08-10"] == pytest.approx(1.0)
        assert qtds["2026-09-10"] == pytest.approx(5.0)

    async def test_registrar_perda_estoque_insuficiente_retorna_422(
        self, client, token_admin, token_compras, conn
    ):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn, qty=2.0)

        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 50.0, "tipo_perda": "VALIDADE"},
        )
        assert r.status_code == 422
        assert err(r) == "ESTOQUE_INSUFICIENTE"

    async def test_registrar_perda_tipo_invalido_retorna_400(
        self, client, token_admin, token_compras, conn
    ):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn)

        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 1.0,
                  "tipo_perda": "TIPO_QUE_NAO_EXISTE"},
        )
        assert r.status_code == 400
        assert err(r) == "TIPO_PERDA_INVALIDO"

    async def test_registrar_perda_uuid_malformado_retorna_400(
        self, client, token_compras
    ):
        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": "nao-e-uuid", "quantidade": 1.0,
                  "tipo_perda": "VALIDADE"},
        )
        assert r.status_code == 400

    async def test_perda_sem_observacao_e_valida(self, client, token_admin,
                                                  token_compras, conn):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn)
        r = await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 1.0, "tipo_perda": "PRODUCAO"},
        )
        assert r.status_code == 201

    async def test_somente_compras_e_admin_registram_perda(
        self, client, token_chef, token_gestao, token_admin, conn
    ):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn)
        payload = {"insumo_id": ins_id, "quantidade": 1.0, "tipo_perda": "VALIDADE"}

        for token in (token_chef, token_gestao):
            r = await client.post("/movimentacoes/perda",
                                  headers=auth_headers(token), json=payload)
            assert r.status_code == 403

    async def test_sem_token_retorna_401(self, client):
        r = await client.post("/movimentacoes/perda",
                              json={"insumo_id": "x", "quantidade": 1, "tipo_perda": "X"})
        assert r.status_code == 401


# =====================================================================
# Listagem de Movimentações
# =====================================================================

@pytest.mark.asyncio
class TestListarMovimentacoes:

    async def test_listar_movimentacoes_vazio(self, client, token_admin):
        r = await client.get("/movimentacoes", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["items"] == []

    async def test_listar_movimentacoes_apos_perda(self, client, token_admin,
                                                    token_compras, conn):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn)
        await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 1.0, "tipo_perda": "QUEBRA"},
        )

        r = await client.get("/movimentacoes", headers=auth_headers(token_admin))
        assert r.json()["total"] == 1
        mov = r.json()["items"][0]
        assert mov["tipo"] == "AJUSTE_MANUAL"
        assert mov["quantidade"] == pytest.approx(1.0)
        assert mov["insumo_id"] == ins_id

    async def test_filtro_por_insumo_id(self, client, token_admin,
                                         token_compras, conn):
        ins1, _ = await _criar_insumo_com_lote(client, token_admin, conn,
                                                nome="Ins Mov 1")
        ins2, _ = await _criar_insumo_com_lote(client, token_admin, conn,
                                                nome="Ins Mov 2")

        for ins_id in (ins1, ins2):
            await client.post(
                "/movimentacoes/perda",
                headers=auth_headers(token_compras),
                json={"insumo_id": ins_id, "quantidade": 1.0,
                      "tipo_perda": "VALIDADE"},
            )

        r = await client.get(f"/movimentacoes?insumo_id={ins1}",
                             headers=auth_headers(token_admin))
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["insumo_id"] == ins1

    async def test_filtro_por_tipo(self, client, token_admin,
                                    token_compras, conn):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn, qty=10.0)
        await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 1.0, "tipo_perda": "VALIDADE"},
        )

        r = await client.get("/movimentacoes?tipo=AJUSTE_MANUAL",
                             headers=auth_headers(token_admin))
        assert r.json()["total"] >= 1
        assert all(m["tipo"] == "AJUSTE_MANUAL" for m in r.json()["items"])

    async def test_paginacao(self, client, token_admin, token_compras, conn):
        ins_id, _ = await _criar_insumo_com_lote(client, token_admin, conn, qty=100.0)
        for _ in range(5):
            await client.post(
                "/movimentacoes/perda",
                headers=auth_headers(token_compras),
                json={"insumo_id": ins_id, "quantidade": 1.0,
                      "tipo_perda": "PRODUCAO"},
            )

        r = await client.get("/movimentacoes?page=1&page_size=3",
                             headers=auth_headers(token_admin))
        assert r.json()["total"] == 5
        assert len(r.json()["items"]) == 3

        r2 = await client.get("/movimentacoes?page=2&page_size=3",
                              headers=auth_headers(token_admin))
        assert len(r2.json()["items"]) == 2

    async def test_estrutura_do_item_movimentacao(self, client, token_admin,
                                                   token_compras, conn):
        ins_id, lote_id = await _criar_insumo_com_lote(client, token_admin, conn)
        await client.post(
            "/movimentacoes/perda",
            headers=auth_headers(token_compras),
            json={"insumo_id": ins_id, "quantidade": 1.0,
                  "tipo_perda": "QUEBRA", "observacao": "Teste estrutura",
                  "lote_id": lote_id},
        )

        r = await client.get("/movimentacoes", headers=auth_headers(token_admin))
        mov = r.json()["items"][0]
        campos_obrigatorios = [
            "id", "lote_insumo_id", "insumo_id", "quantidade",
            "tipo", "criado_em",
        ]
        for campo in campos_obrigatorios:
            assert campo in mov, f"Campo '{campo}' ausente"
        assert mov["tipo_perda_nome"] == "QUEBRA"
        assert mov["observacao"] == "Teste estrutura"
