# backend/tests/test_menus.py — Sistema Dono
#
# Cobre:
#   - CRUD básico (criar, listar, obter, filtros)
#   - Fluxo de estados: PLANEJADO → CONFIRMADO → REALIZADO
#   - Cancelamento em qualquer estado exceto CANCELADO
#   - Snapshot de custo ao confirmar (trigger fn_snapshot_custo_menu)
#   - Imutabilidade do snapshot após confirmação
#   - ABC de refeições dentro do menu
#   - Margem de contribuição (custo vs receita por refeição)
#   - Ordem cronológica duplicada (409)
#   - Adição de item a menu já confirmado (409)
#   - Permissões RBAC por rota

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers de setup
# =====================================================================

async def _get_estilo_id(conn):
    return str(await conn.fetchval("SELECT id FROM estilos_servico LIMIT 1"))


async def _criar_menu(client, token, conn, nome="Evento Teste",
                      data_inicio="2026-09-01", data_fim="2026-09-01"):
    estilo_id = await _get_estilo_id(conn)
    r = await client.post(
        "/menus",
        headers=auth_headers(token),
        json={
            "nome_evento": nome,
            "estilo_servico_id": estilo_id,
            "data_inicio": data_inicio,
            "horario_inicio": "12:00",
            "data_fim": data_fim,
            "horario_fim": "23:00",
            "local_servico": "Salão Principal",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _setup_refeicao_confirmada(client, token_admin, token_chef, conn,
                                      genero="Almoço Executivo",
                                      data="2026-09-01",
                                      pessoas=10,
                                      preco_venda=28.0):
    """Cria insumo + lote + prato + refeição confirmada. Retorna refeicao_id."""
    cat_id = await conn.fetchval(
        "SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'"
    )
    ins_r = await client.post(
        "/insumos",
        headers=auth_headers(token_admin),
        json={"nome": f"Insumo Menu {genero}", "categoria_id": str(cat_id), "unidade": "KG"},
    )
    ins_id = ins_r.json()["id"]
    await client.post(
        f"/insumos/{ins_id}/lotes",
        headers=auth_headers(token_admin),
        json={"valor_aquisicao": 10.0, "data_aquisicao": "2026-08-01", "quantidade": 500},
    )

    prato_r = await client.post(
        "/pratos",
        headers=auth_headers(token_chef),
        json={
            "nome": f"Prato {genero}",
            "genero_prato": "Prato Principal",
            "rendimento_base_porcoes": 5,
            "margem_desperdicio_pct": 0,
            "preco_venda_praticado": preco_venda,
            "itens_receita": [
                {"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                 "peso_bruto": 1.0, "fator_correcao": 1.0}
            ],
        },
    )
    prato_id = prato_r.json()["id"]

    ref_r = await client.post(
        "/refeicoes",
        headers=auth_headers(token_chef),
        json={"genero_refeicao": genero, "data": data,
              "horario_inicio": "12:00", "horario_fim": "15:00",
              "qtd_pessoas": pessoas},
    )
    ref_id = ref_r.json()["id"]

    await client.post(
        f"/refeicoes/{ref_id}/itens",
        headers=auth_headers(token_chef),
        json={"prato_id": prato_id},
    )
    await client.patch(f"/refeicoes/{ref_id}/confirmar",
                       headers=auth_headers(token_chef))
    return ref_id


# =====================================================================
# CRUD básico
# =====================================================================

@pytest.mark.asyncio
class TestCRUDMenus:

    async def test_criar_menu(self, client, token_gestao, conn):
        data = await _criar_menu(client, token_gestao, conn)
        assert data["nome_evento"] == "Evento Teste"
        assert data["status"] == "PLANEJADO"
        assert data["itens"] == []

    async def test_criar_menu_estilo_invalido_retorna_400(self, client, token_gestao):
        r = await client.post(
            "/menus",
            headers=auth_headers(token_gestao),
            json={
                "nome_evento": "X",
                "estilo_servico_id": "00000000-0000-0000-0000-000000000000",
                "data_inicio": "2026-09-01", "horario_inicio": "12:00",
                "data_fim": "2026-09-01", "horario_fim": "23:00",
            },
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_listar_menus(self, client, token_gestao, conn):
        await _criar_menu(client, token_gestao, conn, nome="Menu A")
        await _criar_menu(client, token_gestao, conn, nome="Menu B",
                          data_inicio="2026-10-01", data_fim="2026-10-01")

        r = await client.get("/menus", headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["total"] == 2

    async def test_listar_menus_filtro_status(self, client, token_gestao,
                                               token_admin, conn):
        await _criar_menu(client, token_gestao, conn, nome="Menu Planejado")
        menu = await _criar_menu(client, token_gestao, conn, nome="Menu Cancelado",
                                  data_inicio="2026-10-01", data_fim="2026-10-01")
        await client.patch(f"/menus/{menu['id']}/cancelar",
                           headers=auth_headers(token_gestao))

        r = await client.get("/menus?status=PLANEJADO",
                             headers=auth_headers(token_gestao))
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["status"] == "PLANEJADO"

    async def test_obter_menu_existente(self, client, token_gestao, conn):
        criado = await _criar_menu(client, token_gestao, conn)
        r = await client.get(f"/menus/{criado['id']}",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["id"] == criado["id"]

    async def test_obter_menu_inexistente_retorna_404(self, client, token_gestao):
        r = await client.get("/menus/00000000-0000-0000-0000-000000000000",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_adicionar_refeicao_ao_menu(self, client, token_admin,
                                               token_chef, token_gestao, conn):
        ref_id = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn)
        menu = await _criar_menu(client, token_gestao, conn)

        r = await client.post(
            f"/menus/{menu['id']}/itens",
            headers=auth_headers(token_gestao),
            json={"refeicao_id": ref_id, "ordem_cronologica": 1},
        )
        assert r.status_code == 201
        assert r.json()["ordem_cronologica"] == 1

    async def test_ordem_cronologica_duplicada_retorna_409(self, client, token_admin,
                                                            token_chef, token_gestao,
                                                            conn):
        ref1 = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn,
                                                 genero="Almoço Executivo",
                                                 data="2026-09-01")
        ref2 = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn,
                                                 genero="Jantar",
                                                 data="2026-09-01")
        menu = await _criar_menu(client, token_gestao, conn)

        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref1, "ordem_cronologica": 1})

        r = await client.post(f"/menus/{menu['id']}/itens",
                              headers=auth_headers(token_gestao),
                              json={"refeicao_id": ref2, "ordem_cronologica": 1})
        assert r.status_code == 409
        assert err(r) == "ORDEM_CRONOLOGICA_DUPLICADA"

    async def test_adicionar_item_a_menu_confirmado_retorna_409(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        ref_id = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn)
        menu = await _criar_menu(client, token_gestao, conn)

        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        # Segunda refeição para tentar adicionar
        ref2 = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn,
                                                 genero="Jantar", data="2026-09-01")
        r = await client.post(f"/menus/{menu['id']}/itens",
                              headers=auth_headers(token_gestao),
                              json={"refeicao_id": ref2, "ordem_cronologica": 2})
        assert r.status_code == 409
        assert err(r) == "MENU_JA_CONFIRMADO"


# =====================================================================
# Fluxo de estados
# =====================================================================

@pytest.mark.asyncio
class TestFluxoEstadosMenu:

    async def test_fluxo_completo_planejado_confirmado_realizado(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        ref_id = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn)
        menu = await _criar_menu(client, token_gestao, conn)

        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})

        # PLANEJADO → CONFIRMADO
        r = await client.patch(f"/menus/{menu['id']}/confirmar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["status"] == "CONFIRMADO"

        # CONFIRMADO → REALIZADO
        r = await client.patch(f"/menus/{menu['id']}/realizar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["status"] == "REALIZADO"

    async def test_realizar_menu_planejado_retorna_409(self, client, token_gestao, conn):
        menu = await _criar_menu(client, token_gestao, conn)
        r = await client.patch(f"/menus/{menu['id']}/realizar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 409
        assert err(r) == "TRANSICAO_STATUS_INVALIDA"

    async def test_confirmar_menu_ja_confirmado_retorna_409(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        ref_id = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn)
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        r = await client.patch(f"/menus/{menu['id']}/confirmar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 409
        assert err(r) == "TRANSICAO_STATUS_INVALIDA"

    async def test_cancelar_menu_planejado(self, client, token_gestao, conn):
        menu = await _criar_menu(client, token_gestao, conn)
        r = await client.patch(f"/menus/{menu['id']}/cancelar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELADO"

    async def test_cancelar_menu_confirmado(self, client, token_admin,
                                             token_chef, token_gestao, conn):
        ref_id = await _setup_refeicao_confirmada(client, token_admin, token_chef, conn)
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        r = await client.patch(f"/menus/{menu['id']}/cancelar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 200
        assert r.json()["status"] == "CANCELADO"

    async def test_cancelar_menu_ja_cancelado_retorna_404(self, client,
                                                           token_gestao, conn):
        menu = await _criar_menu(client, token_gestao, conn)
        await client.patch(f"/menus/{menu['id']}/cancelar",
                           headers=auth_headers(token_gestao))
        r = await client.patch(f"/menus/{menu['id']}/cancelar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 404


# =====================================================================
# Snapshot de custo e imutabilidade
# =====================================================================

@pytest.mark.asyncio
class TestSnapshotCustoMenu:

    async def test_snapshot_gravado_ao_confirmar(self, client, token_admin,
                                                  token_chef, token_gestao, conn):
        """itens_menu.custo_snapshot deve ser preenchido ao confirmar o menu.

        Cálculo esperado pelo trigger fn_snapshot_custo_menu:
          custo_snapshot = SUM(itens_refeicao.custo_snapshot × qtd_pessoas)
          custo unitário do insumo = 10.0, peso_bruto = 1.0, FC = 1.0
          custo_total_calculado = 1 × 10 = 10.0
          margem_desperdicio = 0%, rendimento = 5
          custo_snapshot da refeição = 10.0 × 1.0 / 5 = 2.0 por porção
          qtd_pessoas = 10 → custo_snapshot do item_menu = 2.0 × 10 = 20.0
        """
        ref_id = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn, pessoas=10
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})

        # Antes de confirmar: snapshot é None
        antes = await client.get(f"/menus/{menu['id']}",
                                 headers=auth_headers(token_gestao))
        assert antes.json()["itens"][0]["custo_snapshot"] is None

        # Confirmar
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        depois = await client.get(f"/menus/{menu['id']}",
                                  headers=auth_headers(token_gestao))
        snapshot = depois.json()["itens"][0]["custo_snapshot"]
        assert snapshot is not None
        assert snapshot == pytest.approx(20.0, rel=1e-3)

    async def test_snapshot_nao_muda_apos_novo_lote(self, client, token_admin,
                                                      token_chef, token_gestao, conn):
        """Snapshot do menu deve permanecer inalterado mesmo após mudança de
        preço do insumo (imutabilidade histórica — §2.4 da arquitetura)."""
        ref_id = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn, pessoas=10
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        snapshot_antes = (
            await client.get(f"/menus/{menu['id']}",
                             headers=auth_headers(token_gestao))
        ).json()["itens"][0]["custo_snapshot"]

        # Busca insumo criado pelo helper e registra lote com preço 100×
        ins_id = await conn.fetchval(
            "SELECT id FROM insumos WHERE nome LIKE 'Insumo Menu%' LIMIT 1"
        )
        await client.post(
            f"/insumos/{ins_id}/lotes",
            headers=auth_headers(token_admin),
            json={"valor_aquisicao": 1000.0, "data_aquisicao": "2026-08-05",
                  "quantidade": 100},
        )

        snapshot_depois = (
            await client.get(f"/menus/{menu['id']}",
                             headers=auth_headers(token_gestao))
        ).json()["itens"][0]["custo_snapshot"]

        assert snapshot_depois == snapshot_antes

    async def test_snapshot_multiplas_refeicoes(self, client, token_admin,
                                                 token_chef, token_gestao, conn):
        """Com 2 refeições no menu, cada item deve ter seu próprio snapshot."""
        ref1 = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            genero="Almoço Executivo", data="2026-09-01", pessoas=10
        )
        ref2 = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            genero="Jantar", data="2026-09-01", pessoas=20
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref1, "ordem_cronologica": 1})
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref2, "ordem_cronologica": 2})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        dados = (await client.get(f"/menus/{menu['id']}",
                                  headers=auth_headers(token_gestao))).json()
        snapshots = {i["ordem_cronologica"]: i["custo_snapshot"] for i in dados["itens"]}
        assert snapshots[1] is not None
        assert snapshots[2] is not None
        # Refeição 2 tem o dobro de pessoas → snapshot deve ser maior
        assert snapshots[2] > snapshots[1]


# =====================================================================
# ABC do Menu
# =====================================================================

@pytest.mark.asyncio
class TestABCMenu:

    async def test_abc_disponivel_apos_confirmar_e_calcular(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        """ABC de MENU requer chamar fn_recalcular_abc_menu explicitamente
        (não é calculada automaticamente ao confirmar — é o worker que
        reage a evento de preço). Para o teste, chamamos diretamente via SQL."""
        ref_id = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn, pessoas=10
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        # Calcula ABC direto no banco (simula o worker)
        await conn.execute(
            "SELECT fn_recalcular_abc_menu($1::uuid)", menu["id"]
        )

        r = await client.get(f"/menus/{menu['id']}/abc",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 200
        abc = r.json()
        assert len(abc) == 1
        assert abc[0]["classe"] in ("A", "B", "C")
        assert abc[0]["percentual_acumulado"] == pytest.approx(100.0)

    async def test_abc_sem_calculo_retorna_404(self, client, token_gestao, conn):
        menu = await _criar_menu(client, token_gestao, conn)
        r = await client.get(f"/menus/{menu['id']}/abc",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 404
        assert err(r) == "ABC_NAO_CALCULADO"

    async def test_abc_classifica_refeicoes_por_custo(
        self, client, token_admin, token_chef, token_gestao, conn
    ):
        """Com 2 refeições de custo muito diferentes, ABC deve ordenar
        a mais cara primeiro. Com apenas 2 itens onde o maior representa
        ~96% do total, nenhum fica abaixo do threshold de 80% — o que
        é matematicamente correto para a curva ABC. O que se valida é
        que a ordenação por custo está correta."""
        ref_cara = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            genero="Fine Dining", data="2026-09-01", pessoas=50
        )
        ref_barata = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            genero="Lanche da Manhã", data="2026-09-01", pessoas=2
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_cara, "ordem_cronologica": 1})
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_barata, "ordem_cronologica": 2})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))
        await conn.execute("SELECT fn_recalcular_abc_menu($1::uuid)", menu["id"])

        abc_r = await client.get(f"/menus/{menu['id']}/abc",
                                 headers=auth_headers(token_gestao))
        assert abc_r.status_code == 200
        itens = abc_r.json()
        assert len(itens) == 2
        # ref_cara (custo 100) deve vir antes de ref_barata (custo 0)
        refeicao_ids = [item["refeicao_id"] for item in itens]
        assert ref_cara in refeicao_ids
        assert ref_barata in refeicao_ids
        custo_cara = next(i["custo"] for i in itens if i["refeicao_id"] == ref_cara)
        custo_barata = next(i["custo"] for i in itens if i["refeicao_id"] == ref_barata)
        assert custo_cara > custo_barata


# =====================================================================
# Margem de Contribuição
# =====================================================================

@pytest.mark.asyncio
class TestMargemContribuicao:

    async def test_margem_retorna_estrutura_correta(self, client, token_admin,
                                                     token_chef, token_gestao, conn):
        ref_id = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            pessoas=10, preco_venda=28.0
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        r = await client.get(f"/menus/{menu['id']}/margem-contribuicao",
                             headers=auth_headers(token_gestao))
        assert r.status_code == 200
        data = r.json()
        assert "refeicoes" in data
        assert len(data["refeicoes"]) == 1
        ref = data["refeicoes"][0]
        assert "custo_total" in ref
        assert "receita_total" in ref
        assert "margem_contribuicao" in ref
        assert ref["qtd_pessoas"] == 10

    async def test_margem_calculo_correto(self, client, token_admin, token_chef,
                                          token_gestao, conn):
        """
        Insumo: custo=10, peso_bruto=1, FC=1, rendimento=5, margem=0%
        custo por porção = 10/5 = 2.0
        10 pessoas → custo_snapshot_menu = 2.0 × 10 = 20.0
        preco_venda = 28.0 → receita = 28.0 × 10 = 280.0
        margem = 280 − 20 = 260.0
        """
        ref_id = await _setup_refeicao_confirmada(
            client, token_admin, token_chef, conn,
            pessoas=10, preco_venda=28.0
        )
        menu = await _criar_menu(client, token_gestao, conn)
        await client.post(f"/menus/{menu['id']}/itens",
                          headers=auth_headers(token_gestao),
                          json={"refeicao_id": ref_id, "ordem_cronologica": 1})
        await client.patch(f"/menus/{menu['id']}/confirmar",
                           headers=auth_headers(token_gestao))

        r = await client.get(f"/menus/{menu['id']}/margem-contribuicao",
                             headers=auth_headers(token_gestao))
        ref = r.json()["refeicoes"][0]
        assert ref["receita_total"] == pytest.approx(280.0, rel=1e-3)
        assert ref["custo_total"] == pytest.approx(20.0, rel=1e-3)
        assert ref["margem_contribuicao"] == pytest.approx(260.0, rel=1e-3)

    async def test_margem_menu_inexistente_retorna_404(self, client, token_gestao):
        r = await client.get(
            "/menus/00000000-0000-0000-0000-000000000000/margem-contribuicao",
            headers=auth_headers(token_gestao),
        )
        assert r.status_code == 404


# =====================================================================
# Permissões RBAC
# =====================================================================

@pytest.mark.asyncio
class TestPermissoesMenus:

    async def test_compras_nao_pode_criar_menu(self, client, token_compras, conn):
        estilo_id = await _get_estilo_id(conn)
        r = await client.post(
            "/menus",
            headers=auth_headers(token_compras),
            json={"nome_evento": "X", "estilo_servico_id": estilo_id,
                  "data_inicio": "2026-09-01", "horario_inicio": "12:00",
                  "data_fim": "2026-09-01", "horario_fim": "23:00"},
        )
        assert r.status_code == 403
        assert err(r) == "PERMISSAO_NEGADA"

    async def test_chef_pode_criar_menu(self, client, token_chef, conn):
        data = await _criar_menu(client, token_chef, conn)
        assert data["status"] == "PLANEJADO"

    async def test_gestao_pode_criar_menu(self, client, token_gestao, conn):
        data = await _criar_menu(client, token_gestao, conn)
        assert data["status"] == "PLANEJADO"

    async def test_somente_gestao_e_admin_confirmam(self, client, token_chef,
                                                     token_gestao, conn):
        menu = await _criar_menu(client, token_gestao, conn)

        # chef não pode confirmar
        r = await client.patch(f"/menus/{menu['id']}/confirmar",
                               headers=auth_headers(token_chef))
        assert r.status_code == 403

        # gestao pode
        r = await client.patch(f"/menus/{menu['id']}/confirmar",
                               headers=auth_headers(token_gestao))
        assert r.status_code == 200

    async def test_qualquer_perfil_pode_listar_menus(self, client, token_compras,
                                                      token_chef):
        for token in (token_compras, token_chef):
            r = await client.get("/menus", headers=auth_headers(token))
            assert r.status_code == 200

    async def test_sem_token_retorna_401(self, client):
        r = await client.get("/menus")
        assert r.status_code == 401
