# backend/tests/test_pratos.py — Sistema Dono
#
# Cobre:
#   - CRUD básico (criar, listar, obter, atualizar, soft delete)
#   - Criação com itens_receita aninhados e snapshot de custo_unitario
#   - ABC calculada na criação e após substituição de itens
#   - Fichas técnicas (gerencial, insumo, operacional)
#   - Substituição de itens (PUT /pratos/{id}/itens-receita)
#   - Aprovação de prato PENDENTE_APROVACAO
#   - Soft delete bloqueado quando prato em uso
#   - Permissões RBAC por rota
#
# Padrão: integração real contra PostgreSQL (conftest.py),
# Redis mockado, helpers de setup alinhados com test_refeicoes.py.

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers de setup
# =====================================================================

async def _criar_insumo_com_lote(client, token_admin, conn,
                                  nome="Filé Mignon Teste",
                                  categoria="Carnes, Aves e Peixes",
                                  unidade="KG",
                                  valor_lote=60.0,
                                  qty=10):
    cat_id = await conn.fetchval(
        "SELECT id FROM categorias WHERE nome = $1", categoria
    )
    r = await client.post(
        "/insumos",
        headers=auth_headers(token_admin),
        json={"nome": nome, "categoria_id": str(cat_id), "unidade": unidade},
    )
    assert r.status_code == 201, r.text
    ins_id = r.json()["id"]
    await client.post(
        f"/insumos/{ins_id}/lotes",
        headers=auth_headers(token_admin),
        json={"valor_aquisicao": valor_lote, "data_aquisicao": "2026-08-01", "quantidade": qty},
    )
    return ins_id


async def _criar_prato_simples(client, token_chef, ins_id,
                                nome="Prato Teste",
                                genero="Prato Principal",
                                rendimento=10,
                                margem=10.0,
                                preco_venda=28.0,
                                peso_bruto=2.6,
                                fator_correcao=1.3):
    r = await client.post(
        "/pratos",
        headers=auth_headers(token_chef),
        json={
            "nome": nome,
            "genero_prato": genero,
            "rendimento_base_porcoes": rendimento,
            "margem_desperdicio_pct": margem,
            "preco_venda_praticado": preco_venda,
            "itens_receita": [
                {
                    "insumo_id": ins_id,
                    "tipo": "ALIMENTICIO",
                    "peso_bruto": peso_bruto,
                    "fator_correcao": fator_correcao,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# =====================================================================
# CRUD básico
# =====================================================================

@pytest.mark.asyncio
class TestCRUDPratos:

    async def test_criar_prato_sem_itens(self, client, token_chef):
        r = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={
                "nome": "Prato Vazio",
                "genero_prato": "Sobremesa",
                "rendimento_base_porcoes": 4,
            },
        )
        assert r.status_code == 201
        data = r.json()
        assert data["nome"] == "Prato Vazio"
        assert data["status"] == "ATIVO"
        assert data["itens_receita"] == []

    async def test_criar_prato_com_itens(self, client, token_admin, token_chef, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn, valor_lote=60.0)
        data = await _criar_prato_simples(client, token_chef, ins_id)

        assert data["nome"] == "Prato Teste"
        assert len(data["itens_receita"]) == 1
        item = data["itens_receita"][0]
        # custo_unitario_registrado = snapshot do custo_medio_ponderado no momento da criação
        assert item["custo_unitario_registrado"] == 60.0
        # custo_total_calculado = peso_bruto × custo_unitario (2.6 × 60 = 156)
        assert item["custo_total_calculado"] == pytest.approx(156.0)
        # peso_liquido = peso_bruto / fator_correcao (2.6 / 1.3 = 2.0)
        assert item["peso_liquido"] == pytest.approx(2.0)

    async def test_listar_pratos(self, client, token_chef, token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        await _criar_prato_simples(client, token_chef, ins_id, nome="Prato A")
        await _criar_prato_simples(client, token_chef, ins_id, nome="Prato B",
                                    genero="Guarnição", rendimento=5)

        r = await client.get("/pratos", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["total"] == 2

    async def test_listar_pratos_filtro_genero(self, client, token_chef, token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        await _criar_prato_simples(client, token_chef, ins_id, nome="Principal",
                                    genero="Prato Principal")
        await _criar_prato_simples(client, token_chef, ins_id, nome="Guarnição",
                                    genero="Guarnição", rendimento=5)

        r = await client.get("/pratos?genero_prato=Prato%20Principal",
                             headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["genero_prato"] == "Prato Principal"

    async def test_obter_prato_existente(self, client, token_chef, token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.get(f"/pratos/{criado['id']}", headers=auth_headers(token_chef))
        assert r.status_code == 200
        assert r.json()["id"] == criado["id"]
        assert len(r.json()["itens_receita"]) == 1

    async def test_obter_prato_inexistente_retorna_404(self, client, token_chef):
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = await client.get(f"/pratos/{fake_id}", headers=auth_headers(token_chef))
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_uuid_malformado_retorna_400(self, client, token_chef):
        r = await client.get("/pratos/nao-e-um-uuid", headers=auth_headers(token_chef))
        assert r.status_code == 400

    async def test_atualizar_prato(self, client, token_chef, token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.patch(
            f"/pratos/{criado['id']}",
            headers=auth_headers(token_chef),
            json={"nome": "Prato Atualizado", "preco_venda_praticado": 35.0},
        )
        assert r.status_code == 200
        assert r.json()["nome"] == "Prato Atualizado"
        assert r.json()["preco_venda_praticado"] == 35.0
        # itens_receita não mudam com PATCH
        assert len(r.json()["itens_receita"]) == 1

    async def test_soft_delete_prato_sem_uso(self, client, token_admin, token_chef, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.delete(f"/pratos/{criado['id']}",
                                headers=auth_headers(token_admin))
        assert r.status_code == 204

        # Prato aparece como INATIVO
        row = await conn.fetchrow("SELECT status FROM pratos WHERE id = $1::uuid",
                                  criado["id"])
        assert row["status"] == "INATIVO"

    async def test_soft_delete_prato_em_uso_retorna_409(self, client, token_admin,
                                                          token_chef, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn, qty=50)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        # Cria refeição e adiciona o prato
        ref_r = await client.post(
            "/refeicoes",
            headers=auth_headers(token_chef),
            json={"genero_refeicao": "Almoço Executivo", "data": "2026-08-10",
                  "horario_inicio": "12:00", "horario_fim": "15:00", "qtd_pessoas": 5},
        )
        ref_id = ref_r.json()["id"]
        await client.post(f"/refeicoes/{ref_id}/itens",
                          headers=auth_headers(token_chef),
                          json={"prato_id": criado["id"]})

        r = await client.delete(f"/pratos/{criado['id']}",
                                headers=auth_headers(token_admin))
        assert r.status_code == 409
        assert err(r) == "PRATO_EM_USO"


# =====================================================================
# Custo e ABC
# =====================================================================

@pytest.mark.asyncio
class TestCustoABC:

    async def test_custo_snapshot_usa_custo_medio_no_momento_da_criacao(
        self, client, token_admin, token_chef, conn
    ):
        """custo_unitario_registrado deve ser o custo_medio_ponderado do insumo
        no momento do POST /pratos, não o custo atual se mudar depois."""
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn, valor_lote=60.0, qty=10)
        criado = await _criar_prato_simples(client, token_chef, ins_id)
        snapshot_original = criado["itens_receita"][0]["custo_unitario_registrado"]
        assert snapshot_original == 60.0

        # Novo lote muda o custo médio
        await client.post(
            f"/insumos/{ins_id}/lotes",
            headers=auth_headers(token_admin),
            json={"valor_aquisicao": 120.0, "data_aquisicao": "2026-08-02", "quantidade": 10},
        )

        # Prato já criado mantém o snapshot original
        r = await client.get(f"/pratos/{criado['id']}", headers=auth_headers(token_chef))
        assert r.json()["itens_receita"][0]["custo_unitario_registrado"] == snapshot_original

    async def test_abc_calculada_na_criacao(self, client, token_admin, token_chef, conn):
        """ABC de PRATO deve estar disponível imediatamente após POST /pratos,
        sem precisar esperar o worker (worker só reage a evento de preço)."""
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.get(f"/pratos/{criado['id']}/abc",
                             headers=auth_headers(token_chef))
        assert r.status_code == 200
        abc = r.json()
        assert len(abc) == 1
        assert abc[0]["classe"] in ("A", "B", "C")
        assert abc[0]["percentual_acumulado"] == pytest.approx(100.0)

    async def test_abc_classifica_multiplos_insumos(self, client, token_admin,
                                                     token_chef, conn):
        """Com 3 insumos em proporção 80/15/5, ABC deve classificar A, B, C."""
        cat_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Carnes, Aves e Peixes'"
        )
        # Cria 3 insumos com custos médios distintos
        ids = []
        for nome, valor in [("Insumo Alto", 800.0), ("Insumo Medio", 150.0), ("Insumo Baixo", 50.0)]:
            r = await client.post(
                "/insumos",
                headers=auth_headers(token_admin),
                json={"nome": nome, "categoria_id": str(cat_id), "unidade": "KG"},
            )
            ins_id = r.json()["id"]
            await client.post(
                f"/insumos/{ins_id}/lotes",
                headers=auth_headers(token_admin),
                json={"valor_aquisicao": valor, "data_aquisicao": "2026-08-01", "quantidade": 1},
            )
            ids.append(ins_id)

        prato_r = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={
                "nome": "Prato ABC",
                "genero_prato": "Prato Principal",
                "rendimento_base_porcoes": 1,
                "itens_receita": [
                    {"insumo_id": ids[0], "tipo": "ALIMENTICIO", "peso_bruto": 1, "fator_correcao": 1},
                    {"insumo_id": ids[1], "tipo": "ALIMENTICIO", "peso_bruto": 1, "fator_correcao": 1},
                    {"insumo_id": ids[2], "tipo": "ALIMENTICIO", "peso_bruto": 1, "fator_correcao": 1},
                ],
            },
        )
        assert prato_r.status_code == 201
        prato_id = prato_r.json()["id"]

        abc_r = await client.get(f"/pratos/{prato_id}/abc", headers=auth_headers(token_chef))
        assert abc_r.status_code == 200
        abc = {item["insumo_id"]: item["classe"] for item in abc_r.json()}
        assert abc[ids[0]] == "A"
        assert abc[ids[1]] == "B"
        assert abc[ids[2]] == "C"

    async def test_abc_nao_calculado_retorna_404(self, client, token_chef):
        """Prato criado sem itens não tem ABC — endpoint deve retornar 404."""
        r = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={"nome": "Prato Sem Itens", "genero_prato": "Entrada",
                  "rendimento_base_porcoes": 4},
        )
        prato_id = r.json()["id"]

        abc_r = await client.get(f"/pratos/{prato_id}/abc", headers=auth_headers(token_chef))
        assert abc_r.status_code == 404
        assert err(abc_r) == "ABC_NAO_CALCULADO"

    async def test_abc_recalculada_apos_substituicao_de_itens(
        self, client, token_admin, token_chef, conn
    ):
        """PUT /pratos/{id}/itens-receita deve limpar e recalcular a ABC."""
        cat_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Secos e Despensa'"
        )
        # Insumo caro (vai ser A)
        r1 = await client.post("/insumos", headers=auth_headers(token_admin),
                               json={"nome": "Ins Caro", "categoria_id": str(cat_id), "unidade": "KG"})
        id1 = r1.json()["id"]
        await client.post(f"/insumos/{id1}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 500.0, "data_aquisicao": "2026-08-01", "quantidade": 1})

        # Insumo barato (vai ser C depois)
        r2 = await client.post("/insumos", headers=auth_headers(token_admin),
                               json={"nome": "Ins Barato", "categoria_id": str(cat_id), "unidade": "KG"})
        id2 = r2.json()["id"]
        await client.post(f"/insumos/{id2}/lotes", headers=auth_headers(token_admin),
                          json={"valor_aquisicao": 10.0, "data_aquisicao": "2026-08-01", "quantidade": 1})

        # Cria prato só com o insumo barato
        prato_r = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={"nome": "Prato Substituicao", "genero_prato": "Guarnição",
                  "rendimento_base_porcoes": 1,
                  "itens_receita": [{"insumo_id": id2, "tipo": "ALIMENTICIO",
                                     "peso_bruto": 1, "fator_correcao": 1}]},
        )
        prato_id = prato_r.json()["id"]

        # Substitui itens — agora ambos
        r = await client.put(
            f"/pratos/{prato_id}/itens-receita",
            headers=auth_headers(token_chef),
            json=[
                {"insumo_id": id1, "tipo": "ALIMENTICIO", "peso_bruto": 1, "fator_correcao": 1},
                {"insumo_id": id2, "tipo": "ALIMENTICIO", "peso_bruto": 1, "fator_correcao": 1},
            ],
        )
        assert r.status_code == 200
        assert len(r.json()["itens_receita"]) == 2

        # ABC recalculada: id1 deve ser A, id2 deve ser C
        abc_r = await client.get(f"/pratos/{prato_id}/abc", headers=auth_headers(token_chef))
        abc = {item["insumo_id"]: item["classe"] for item in abc_r.json()}
        assert abc[id1] == "A"
        assert abc[id2] == "C"


# =====================================================================
# Fichas Técnicas
# =====================================================================

@pytest.mark.asyncio
class TestFichasTecnicas:

    async def test_ficha_gerencial_campos_obrigatorios(self, client, token_chef,
                                                        token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn, valor_lote=60.0)
        criado = await _criar_prato_simples(client, token_chef, ins_id,
                                            rendimento=10, margem=10.0, preco_venda=28.0)
        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=gerencial",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 200
        data = r.json()
        # Campos obrigatórios
        assert "custo_total_ingredientes" in data
        assert "custo_total_receita" in data
        assert "cmv_por_porcao" in data
        assert "margem_lucro_bruta_pct" in data
        assert "ingredientes" in data
        assert len(data["ingredientes"]) == 1

    async def test_ficha_gerencial_calculo_correto(self, client, token_chef,
                                                    token_admin, conn):
        """Filé Mignon: PB=2.6, FC=1.3, custo=60 → custo_total=156
        Margem 10%, rendimento 10 → cmv_porção = 156×1.1/10 = 17.16"""
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn,
                                               valor_lote=60.0, qty=20)
        criado = await _criar_prato_simples(client, token_chef, ins_id,
                                            rendimento=10, margem=10.0,
                                            peso_bruto=2.6, fator_correcao=1.3)
        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=gerencial",
            headers=auth_headers(token_chef),
        )
        data = r.json()
        assert data["custo_total_ingredientes"] == pytest.approx(156.0, rel=1e-3)
        assert data["custo_total_receita"] == pytest.approx(171.6, rel=1e-3)
        assert data["cmv_por_porcao"] == pytest.approx(17.16, rel=1e-3)

    async def test_ficha_operacional_sem_dados_financeiros(self, client, token_chef,
                                                            token_admin, conn):
        """Ficha operacional não deve expor custo_unitario nem custo_total."""
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=operacional",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 200
        data = r.json()
        # Campos presentes
        assert "ingredientes" in data
        assert "modo_preparo" in data
        # Campos financeiros ausentes
        assert "custo_unitario" not in data
        assert "custo_total_ingredientes" not in data
        assert "margem_lucro_bruta_pct" not in data

    async def test_ficha_insumo_retorna_dados_do_insumo(self, client, token_chef,
                                                          token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn, valor_lote=60.0)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=insumo&insumo_id={ins_id}",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["custo_unitario"] == 60.0
        assert data["peso_bruto"] == pytest.approx(2.6)

    async def test_ficha_insumo_sem_insumo_id_retorna_400(self, client, token_chef,
                                                            token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=insumo",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_ficha_insumo_nao_pertencente_retorna_404(self, client, token_chef,
                                                              token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        outro_ins = await _criar_insumo_com_lote(client, token_admin, conn,
                                                   nome="Insumo Outro",
                                                   categoria="Hortifruti")
        r = await client.get(
            f"/pratos/{criado['id']}/ficha-tecnica?tipo=insumo&insumo_id={outro_ins}",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 404


# =====================================================================
# Aprovação de pratos IA
# =====================================================================

@pytest.mark.asyncio
class TestAprovacaoPrato:

    async def test_aprovar_prato_pendente(self, client, token_chef, conn):
        """Prato criado diretamente como PENDENTE_APROVACAO deve poder ser aprovado."""
        await conn.execute(
            """INSERT INTO pratos (nome, genero_prato, rendimento_base_porcoes,
                                   origem, status)
               VALUES ('Prato IA', 'Entrada', 4, 'IA_RASCUNHO', 'PENDENTE_APROVACAO')"""
        )
        prato_id = await conn.fetchval(
            "SELECT id FROM pratos WHERE nome = 'Prato IA'"
        )

        r = await client.patch(
            f"/pratos/{prato_id}/aprovar",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ATIVO"

    async def test_aprovar_prato_ja_ativo_retorna_409(self, client, token_chef,
                                                       token_admin, conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)
        # Prato criado via POST já nasce ATIVO

        r = await client.patch(
            f"/pratos/{criado['id']}/aprovar",
            headers=auth_headers(token_chef),
        )
        assert r.status_code == 409
        assert err(r) == "PRATO_NAO_PENDENTE_APROVACAO"


# =====================================================================
# Permissões RBAC
# =====================================================================

@pytest.mark.asyncio
class TestPermissoesPratos:

    async def test_gestao_nao_pode_criar_prato(self, client, token_gestao):
        r = await client.post(
            "/pratos",
            headers=auth_headers(token_gestao),
            json={"nome": "X", "genero_prato": "Entrada", "rendimento_base_porcoes": 1},
        )
        assert r.status_code == 403
        assert err(r) == "PERMISSAO_NEGADA"

    async def test_compras_nao_pode_criar_prato(self, client, token_compras):
        r = await client.post(
            "/pratos",
            headers=auth_headers(token_compras),
            json={"nome": "X", "genero_prato": "Entrada", "rendimento_base_porcoes": 1},
        )
        assert r.status_code == 403

    async def test_chef_pode_criar_prato(self, client, token_chef):
        r = await client.post(
            "/pratos",
            headers=auth_headers(token_chef),
            json={"nome": "Prato Chef", "genero_prato": "Guarnição",
                  "rendimento_base_porcoes": 4},
        )
        assert r.status_code == 201

    async def test_qualquer_perfil_pode_listar_pratos(self, client, token_gestao,
                                                       token_compras):
        for token in (token_gestao, token_compras):
            r = await client.get("/pratos", headers=auth_headers(token))
            assert r.status_code == 200

    async def test_somente_admin_pode_deletar_prato(self, client, token_chef,
                                                     token_admin, token_gestao,
                                                     conn):
        ins_id = await _criar_insumo_com_lote(client, token_admin, conn)
        criado = await _criar_prato_simples(client, token_chef, ins_id)

        # chef não pode deletar
        r = await client.delete(f"/pratos/{criado['id']}",
                                headers=auth_headers(token_chef))
        assert r.status_code == 403

        # gestao não pode deletar
        r = await client.delete(f"/pratos/{criado['id']}",
                                headers=auth_headers(token_gestao))
        assert r.status_code == 403

        # admin pode
        r = await client.delete(f"/pratos/{criado['id']}",
                                headers=auth_headers(token_admin))
        assert r.status_code == 204

    async def test_sem_token_retorna_401(self, client):
        r = await client.get("/pratos")
        assert r.status_code == 401
