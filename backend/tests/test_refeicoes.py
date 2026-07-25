# backend/tests/test_refeicoes.py — Sistema Dono
#
# Cobre: fluxo completo de estados (PLANEJADA→CONFIRMADA→EXECUTADA→SERVIDA),
# estorno ao cancelar EXECUTADA, bloqueio de cancelamento após SERVIDA,
# validação de composição (422 COMPOSICAO_INVALIDA), estoque insuficiente
# (422 ESTOQUE_INSUFICIENTE), e imutabilidade do custo_snapshot.
import pytest

from tests.conftest import auth_headers, err


# ─── Helpers ────────────────────────────────────────────────────────────────

async def _setup_insumo_com_lote(client, token_admin, conn, qty=100):
    cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
    ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                              json={"nome": "Insumo Refeicao Teste", "categoria_id": str(cat_id), "unidade": "KG"})
    ins_id = ins_r.json()["id"]
    await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                      json={"valor_aquisicao": 10.0, "data_aquisicao": "2026-07-21", "quantidade": qty})
    return ins_id


async def _setup_prato(client, token_admin, ins_id, rendimento=5):
    r = await client.post("/pratos", headers=auth_headers(token_admin),
                          json={"nome": "Prato Refeicao Teste", "genero_prato": "Prato Principal",
                                "rendimento_base_porcoes": rendimento,
                                "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                   "peso_bruto": 2, "fator_correcao": 1}]})
    return r.json()["id"]


async def _criar_refeicao(client, token_chef, genero="Almoço Executivo", pessoas=5, data="2026-07-25"):
    r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                          json={"genero_refeicao": genero, "data": data,
                                "horario_inicio": "12:00", "horario_fim": "15:00", "qtd_pessoas": pessoas})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ─── Testes ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFluxoRefeicao:

    async def test_fluxo_completo_planejada_a_servida(self, client, token_admin, token_chef, conn):
        ins_id = await _setup_insumo_com_lote(client, token_admin, conn, qty=100)
        prato_id = await _setup_prato(client, token_admin, ins_id, rendimento=5)
        ref_id = await _criar_refeicao(client, token_chef, pessoas=5)

        # adicionar item
        r = await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                              json={"prato_id": prato_id})
        assert r.status_code == 201

        # confirmar → snapshot gravado, ABC materializado
        r = await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["status"] == "CONFIRMADA"
        assert r.json()["itens"][0]["custo_snapshot"] is not None

        # ABC disponível imediatamente (sem evento de preço)
        abc_r = await client.get(f"/refeicoes/{ref_id}/abc", headers=auth_headers(token_chef))
        assert abc_r.status_code == 200
        assert len(abc_r.json()) > 0

        # executar → baixa de estoque
        r = await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["status"] == "EXECUTADA"

        # lote deve ter reduzido: necessidade = 2kg × (5/5) = 2kg → 100−2 = 98
        lotes = await client.get(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin))
        assert lotes.json()[0]["quantidade_disponivel"] == 98.0

        # servir
        r = await client.patch(f"/refeicoes/{ref_id}/servir", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["status"] == "SERVIDA"

    async def test_cancelar_planejada_sem_estorno(self, client, token_chef):
        ref_id = await _criar_refeicao(client, token_chef, data="2026-07-26")
        r = await client.patch(f"/refeicoes/{ref_id}/cancelar", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELADA"

    async def test_cancelar_executada_estorna_estoque(self, client, token_admin, token_chef, conn):
        ins_id = await _setup_insumo_com_lote(client, token_admin, conn, qty=20)
        prato_id = await _setup_prato(client, token_admin, ins_id, rendimento=4)
        ref_id = await _criar_refeicao(client, token_chef, pessoas=4, data="2026-07-27")

        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))
        await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))

        # estoque baixou (2kg × 4/4 = 2kg → 20−2 = 18)
        lotes_apos_exec = await client.get(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin))
        assert lotes_apos_exec.json()[0]["quantidade_disponivel"] == 18.0

        # cancelar com estorno
        r = await client.patch(f"/refeicoes/{ref_id}/cancelar", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELADA"

        # estoque deve ter voltado
        lotes_apos_cancel = await client.get(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin))
        assert lotes_apos_cancel.json()[0]["quantidade_disponivel"] == 20.0

    async def test_cancelar_servida_bloqueado(self, client, token_admin, token_chef, conn):
        ins_id = await _setup_insumo_com_lote(client, token_admin, conn, qty=50)
        prato_id = await _setup_prato(client, token_admin, ins_id)
        ref_id = await _criar_refeicao(client, token_chef, pessoas=5, data="2026-07-28")

        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))
        await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))
        await client.patch(f"/refeicoes/{ref_id}/servir", headers=auth_headers(token_chef))

        r = await client.patch(f"/refeicoes/{ref_id}/cancelar", headers=auth_headers(token_chef))
        assert r.status_code == 409
        assert err(r) == "TRANSICAO_STATUS_INVALIDA"
        assert r.json().get("detail", r.json()).get("error", {}).get("details", {}).get("status_atual") == "SERVIDA"

    async def test_estoque_insuficiente_retorna_422(self, client, token_admin, token_chef, conn):
        ins_id = await _setup_insumo_com_lote(client, token_admin, conn, qty=1)
        prato_id = await _setup_prato(client, token_admin, ins_id, rendimento=1)
        # necessidade = 2kg × (100/1) = 200kg; estoque = 1kg
        ref_id = await _criar_refeicao(client, token_chef, pessoas=100, data="2026-07-29")

        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))

        r = await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))
        assert r.status_code == 422
        assert err(r) == "ESTOQUE_INSUFICIENTE"
        assert "insumos_faltantes" in (r.json().get("detail", r.json()).get("error", {}).get("details", {}))

        # estoque não pode ter baixado (passada 1 da função devolveu erro)
        lotes = await client.get(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin))
        assert lotes.json()[0]["quantidade_disponivel"] == 1.0

    async def test_composicao_invalida_retorna_422(self, client, token_admin, token_chef, conn):
        """Prato de gênero 'Digestivo/Café' não pode ser adicionado a 'Almoço Executivo'
        (só é aceito em 'Fine Dining' conforme seeds de regras_composicao)."""
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Bebidas'")
        assert cat_id is not None, "Categoria 'Bebidas' não encontrada nos seeds"
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo Cafe", "categoria_id": str(cat_id), "unidade": "L"})
        assert ins_r.status_code == 201, f"Criação de insumo falhou: {ins_r.text}"
        ins_id = ins_r.json()["id"]
        prato_r = await client.post("/pratos", headers=auth_headers(token_admin),
                                    json={"nome": "Café Expresso", "genero_prato": "Digestivo/Café",
                                          "rendimento_base_porcoes": 1,
                                          "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                             "peso_bruto": 0.05, "fator_correcao": 1}]})
        assert prato_r.status_code == 201, f"Criação de prato falhou: {prato_r.text}"
        prato_id = prato_r.json()["id"]
        # "Almoço Executivo" só aceita: Entrada, Prato Principal, Guarnição,
        # Bebida Quente, Bebida Fria, Sobremesa — não "Digestivo/Café"
        ref_id = await _criar_refeicao(client, token_chef, genero="Almoço Executivo")

        r = await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                              json={"prato_id": prato_id})
        assert r.status_code == 422, f"Esperava 422, veio {r.status_code}: {r.text}"
        assert err(r) == "COMPOSICAO_INVALIDA"

    async def test_imutabilidade_snapshot_apos_confirmacao(self, client, token_admin, token_chef, conn):
        """custo_snapshot não pode mudar depois de confirmada, mesmo que o
        preço do insumo suba — a câmera de custo está congelada."""
        ins_id = await _setup_insumo_com_lote(client, token_admin, conn, qty=50)
        prato_id = await _setup_prato(client, token_admin, ins_id, rendimento=5)
        ref_id = await _criar_refeicao(client, token_chef, pessoas=5, data="2026-07-30")

        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))

        antes = (await client.get(f"/refeicoes/{ref_id}", headers=auth_headers(token_chef))).json()
        snapshot_antes = antes["itens"][0]["custo_snapshot"]

        # Novo lote com preço muito diferente
        await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 999.0, "data_aquisicao": "2026-07-21", "quantidade": 50})

        depois = (await client.get(f"/refeicoes/{ref_id}", headers=auth_headers(token_chef))).json()
        assert depois["itens"][0]["custo_snapshot"] == snapshot_antes
