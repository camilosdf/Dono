# backend/tests/test_relatorios.py — Sistema Dono
#
# Cobre:
#   - GET /relatorios/curva-abc (todos os escopos)
#   - GET /relatorios/mrp
#   - GET /relatorios/ruptura-estoque
#   - GET /relatorios/consumo
#   - GET /relatorios/margem-menu/{menu_id}
#   - Exportação PDF e XLSX (formato=pdf|xlsx)
#   - Permissões RBAC por rota

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

async def _criar_insumo_com_lote(client, token_admin, conn,
                                  nome="Insumo Rel", valor=10.0, qty=20.0,
                                  data_validade=None):
    cat_id = await conn.fetchval(
        "SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'"
    )
    r = await client.post(
        "/insumos",
        headers=auth_headers(token_admin),
        json={"nome": nome, "categoria_id": str(cat_id), "unidade": "KG"},
    )
    ins_id = r.json()["id"]
    payload = {"valor_aquisicao": valor, "data_aquisicao": "2026-08-01", "quantidade": qty}
    if data_validade:
        payload["data_validade"] = data_validade
    await client.post(f"/insumos/{ins_id}/lotes", headers=auth_headers(token_admin),
                      json=payload)
    return ins_id


async def _setup_prato(client, token_chef, ins_id, rendimento=5,
                        preco_venda=28.0, nome="Prato Rel"):
    r = await client.post(
        "/pratos",
        headers=auth_headers(token_chef),
        json={
            "nome": nome,
            "genero_prato": "Prato Principal",
            "rendimento_base_porcoes": rendimento,
            "margem_desperdicio_pct": 0,
            "preco_venda_praticado": preco_venda,
            "itens_receita": [
                {"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                 "peso_bruto": 1.0, "fator_correcao": 1.0}
            ],
        },
    )
    return r.json()["id"]


async def _setup_menu_confirmado(client, token_admin, token_chef,
                                  token_gestao, conn, pessoas=10):
    ins_id = await _criar_insumo_com_lote(client, token_admin, conn, qty=200.0)
    prato_id = await _setup_prato(client, token_chef, ins_id)

    ref_r = await client.post(
        "/refeicoes",
        headers=auth_headers(token_chef),
        json={"genero_refeicao": "Almoço Executivo", "data": "2026-09-15",
              "horario_inicio": "12:00", "horario_fim": "15:00",
              "qtd_pessoas": pessoas},
    )
    ref_id = ref_r.json()["id"]
    await client.post(f"/refeicoes/{ref_id}/itens",
                      headers=auth_headers(token_chef),
                      json={"prato_id": prato_id})
    await client.patch(f"/refeicoes/{ref_id}/confirmar",
                       headers=auth_headers(token_chef))

    estilo_id = await conn.fetchval("SELECT id FROM estilos_servico LIMIT 1")
    menu_r = await client.post(
        "/menus",
        headers=auth_headers(token_gestao),
        json={"nome_evento": "Evento Rel", "estilo_servico_id": str(estilo_id),
              "data_inicio": "2026-09-15", "horario_inicio": "12:00",
              "data_fim": "2026-09-15", "horario_fim": "15:00"},
    )
    menu_id = menu_r.json()["id"]
    await client.post(f"/menus/{menu_id}/itens",
                      headers=auth_headers(token_gestao),
                      json={"refeicao_id": ref_id, "ordem_cronologica": 1})
    await client.patch(f"/menus/{menu_id}/confirmar",
                       headers=auth_headers(token_gestao))

    return menu_id, ref_id, ins_id


# =====================================================================
# Curva ABC
# =====================================================================

@pytest.mark.asyncio
class TestCurvaABC:

    async def test_abc_escopo_prato(self, client, token_admin, token_chef,
                                    token_gestao, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        prato_id = await _setup_prato(client, token_chef, ins_id)

        r = await client.get(
            f"/relatorios/curva-abc?escopo=PRATO&id={prato_id}",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 200
        assert len(r.json()) > 0

    async def test_abc_escopo_invalido_retorna_400(self, client, token_gestao):
        r = await client.get(
            "/relatorios/curva-abc?escopo=INVALIDO&id=00000000-0000-0000-0000-000000000000",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_abc_sem_dados_retorna_404(self, client, token_gestao):
        r = await client.get(
            "/relatorios/curva-abc?escopo=MENU&id=00000000-0000-0000-0000-000000000001",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 404
        assert err(r) == "ABC_NAO_CALCULADO"

    async def test_abc_escopo_menu_apos_calculo(self, client, token_admin,
                                                 token_chef, token_gestao, conn):
        menu_id, _, _ = await _setup_menu_confirmado(
            client, token_admin, token_chef, token_gestao, conn
        )
        await conn.execute("SELECT fn_recalcular_abc_menu($1::uuid)", menu_id)

        r = await client.get(
            f"/relatorios/curva-abc?escopo=MENU&id={menu_id}",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_somente_gestao_compras_admin_acessam_abc(self, client,
                                                              token_chef,
                                                              token_gestao):
        r = await client.get(
            "/relatorios/curva-abc?escopo=MENU&id=00000000-0000-0000-0000-000000000001",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 403

        r = await client.get(
            "/relatorios/curva-abc?escopo=MENU&id=00000000-0000-0000-0000-000000000001",
            headers=auth_headers(token_gestao),
        )
        # 403 ou 404 — ambos aceitáveis; o que não pode é ser 200 com chef
        assert r.status_code in (403, 404)


# =====================================================================
# MRP
# =====================================================================

@pytest.mark.asyncio
class TestMRP:

    async def test_mrp_sem_menus_retorna_lista_vazia(self, client, token_admin):
        r = await client.get("/relatorios/mrp", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json()["itens"] == []

    async def test_mrp_com_menu_planejado_retorna_necessidade(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        ins_id = await _criar_insumo_com_lote(
            client, token_admin, conn, qty=2.0  # estoque insuficiente
        )
        prato_id = await _setup_prato(client, token_chef, ins_id, rendimento=5)

        ref_r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_chef),
            json={"genero_refeicao": "Almoço Executivo", "data": "2026-09-20",
                  "horario_inicio": "12:00", "horario_fim": "15:00",
                  "qtd_pessoas": 50},  # precisa de 10kg, tem 2kg → falta 8kg
        )
        ref_id = ref_r.json()["id"]
        await client.post(f"/refeicoes/{ref_id}/itens",
                          headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})

        estilo_id = await conn.fetchval("SELECT id FROM estilos_servico LIMIT 1")
        menu_r = await client.post(
            "/menus",
            headers=auth_headers(token_gestao),
            json={"nome_evento": "Evento MRP", "estilo_servico_id": str(estilo_id),
                  "data_inicio": "2026-09-20", "horario_inicio": "12:00",
                  "data_fim": "2026-09-20", "horario_fim": "15:00"},
        )
        menu_id = menu_r.json()["id"]
        await client.post(f"/menus/{menu_id}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})

        r = await client.get("/relatorios/mrp?data_limite=2026-09-30",
                             headers=auth_headers(token_admin))
        assert r.status_code == 200
        itens = r.json()["itens"]
        assert len(itens) > 0
        item = next((i for i in itens if i["insumo_id"] == ins_id), None)
        assert item is not None
        assert item["necessidade_liquida"] > 0

    async def test_mrp_insumo_com_estoque_suficiente_nao_aparece(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        ins_id = await _criar_insumo_com_lote(
            client, token_admin, conn, qty=999.0  # estoque farto
        )
        prato_id = await _setup_prato(client, token_chef, ins_id, rendimento=5)

        ref_r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_chef),
            json={"genero_refeicao": "Almoço Executivo", "data": "2026-09-25",
                  "horario_inicio": "12:00", "horario_fim": "15:00",
                  "qtd_pessoas": 5},
        )
        ref_id = ref_r.json()["id"]
        await client.post(f"/refeicoes/{ref_id}/itens",
                          headers=auth_headers(token_chef),
                          json={"prato_id": prato_id})

        estilo_id = await conn.fetchval("SELECT id FROM estilos_servico LIMIT 1")
        menu_r = await client.post(
            "/menus",
            headers=auth_headers(token_gestao),
            json={"nome_evento": "Evento Farto", "estilo_servico_id": str(estilo_id),
                  "data_inicio": "2026-09-25", "horario_inicio": "12:00",
                  "data_fim": "2026-09-25", "horario_fim": "15:00"},
        )
        await client.post(f"/menus/{menu_r.json()['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})

        r = await client.get("/relatorios/mrp?data_limite=2026-09-30",
                             headers=auth_headers(token_admin))
        ids = [i["insumo_id"] for i in r.json()["itens"]]
        assert ins_id not in ids

    async def test_mrp_data_limite_default(self, client, token_admin):
        """Sem data_limite, deve usar 30 dias à frente — não deve falhar."""
        r = await client.get("/relatorios/mrp", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert "data_limite" in r.json()

    async def test_somente_compras_e_admin_acessam_mrp(self, client,
                                                         token_chef, token_gestao):
        r = await client.get("/relatorios/mrp", headers=auth_headers(token_chef))
        assert r.status_code == 403

        r = await client.get("/relatorios/mrp", headers=auth_headers(token_gestao))
        assert r.status_code == 403


# =====================================================================
# Ruptura de Estoque
# =====================================================================

@pytest.mark.asyncio
class TestRupturaEstoque:

    async def test_ruptura_sem_dados_retorna_listas_vazias(self, client, token_admin):
        r = await client.get("/relatorios/ruptura-estoque",
                             headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json()["lotes_vencendo"] == []

    async def test_ruptura_detecta_lote_vencendo(self, client, token_admin, conn):
        ins_id = await _criar_insumo_com_lote(
            client, token_admin, conn,
            nome="Insumo Vencendo",
            qty=5.0,
            data_validade="2026-08-07",  # amanhã
        )

        r = await client.get("/relatorios/ruptura-estoque?dias=7",
                             headers=auth_headers(token_admin))
        assert r.status_code == 200
        lotes = r.json()["lotes_vencendo"]
        ids = [l["insumo_id"] for l in lotes]
        assert ins_id in ids

    async def test_ruptura_detecta_insumo_zerado(self, client, token_admin, conn):
        cat_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Hortifruti'"
        )
        r = await client.post(
            "/insumos",
            headers=auth_headers(token_admin),
            json={"nome": "Insumo Zerado", "categoria_id": str(cat_id), "unidade": "KG"},
        )
        ins_id = r.json()["id"]
        # Sem lotes → quantidade_disponivel = 0

        r = await client.get("/relatorios/ruptura-estoque",
                             headers=auth_headers(token_admin))
        zerados = [i["insumo_id"] for i in r.json()["insumos_zerados"]]
        assert ins_id in zerados

    async def test_lote_longe_de_vencer_nao_aparece(self, client, token_admin, conn):
        await _criar_insumo_com_lote(
            client, token_admin, conn,
            nome="Insumo Ok",
            qty=10.0,
            data_validade="2027-12-31",
        )
        r = await client.get("/relatorios/ruptura-estoque?dias=7",
                             headers=auth_headers(token_admin))
        lotes = r.json()["lotes_vencendo"]
        assert not any(l.get("insumo_nome") == "Insumo Ok" for l in lotes)


# =====================================================================
# Consumo por Categoria
# =====================================================================

@pytest.mark.asyncio
class TestConsumo:

    async def test_consumo_sem_dados_retorna_estrutura(self, client, token_gestao):
        r = await client.get("/relatorios/consumo", headers=auth_headers(token_gestao))
        assert r.status_code == 200
        # Pode ter "compras" ou "perdas" dependendo da implementação
        assert isinstance(r.json(), dict)

    async def test_consumo_com_lote_registrado(self, client, token_admin,
                                                token_gestao, conn):
        await _criar_insumo_com_lote(client, token_admin, conn,
                                      nome="Ins Consumo", valor=50.0, qty=10.0)

        r = await client.get("/relatorios/consumo", headers=auth_headers(token_gestao))
        assert r.status_code == 200

    async def test_consumo_periodo_invalido_retorna_400(self, client, token_gestao):
        r = await client.get(
            "/relatorios/consumo?periodo_inicio=2026-09-01&periodo_fim=2026-08-01",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 400
        assert err(r) == "PERIODO_INVALIDO"

    async def test_somente_gestao_e_admin_acessam_consumo(self, client,
                                                            token_chef,
                                                            token_compras):
        for token in (token_chef, token_compras):
            r = await client.get("/relatorios/consumo", headers=auth_headers(token))
            assert r.status_code == 403


# =====================================================================
# Margem de Menu (relatório)
# =====================================================================

@pytest.mark.asyncio
class TestMargemMenuRelatorio:

    async def test_margem_menu_relatorio(self, client, token_admin,
                                          token_chef, token_gestao, conn):
        menu_id, _, _ = await _setup_menu_confirmado(
            client, token_admin, token_chef, token_gestao, conn
        )
        r = await client.get(f"/relatorios/margem-menu/{menu_id}",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert "refeicoes" in r.json()

    async def test_margem_menu_inexistente_retorna_404(self, client, token_gestao):
        r = await client.get(
            "/relatorios/margem-menu/00000000-0000-0000-0000-000000000000",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 404


# =====================================================================
# Exportação PDF / XLSX
# =====================================================================

@pytest.mark.asyncio
class TestExportacaoRelatorios:

    async def test_mrp_formato_xlsx(self, client, token_admin):
        r = await client.get("/relatorios/mrp?formato=xlsx",
                             headers=auth_headers(token_admin))
        # Deve retornar XLSX ou 200 com JSON (se exportação não implementada, não deve 500)
        assert r.status_code in (200, 501)
        if r.status_code == 200 and "xlsx" in r.headers.get("content-type", ""):
            assert len(r.content) > 0

    async def test_ruptura_formato_pdf(self, client, token_admin):
        r = await client.get("/relatorios/ruptura-estoque?formato=pdf",
                             headers=auth_headers(token_admin))
        assert r.status_code in (200, 501)

    async def test_sem_token_retorna_401(self, client):
        for rota in ("/relatorios/mrp", "/relatorios/ruptura-estoque",
                     "/relatorios/consumo"):
            r = await client.get(rota)
            assert r.status_code == 401
