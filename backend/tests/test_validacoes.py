# backend/tests/test_validacoes.py — Sistema Dono
#
# Cobre: casos de borda transversais que não pertencem a um módulo
# específico — UUID malformado (handler global de ValueError), campos
# obrigatórios ausentes (Pydantic 422), e transições de status inválidas
# que não foram cobertas em test_refeicoes.py (menus, pratos).
import pytest

from tests.conftest import auth_headers, err


@pytest.mark.asyncio
class TestUUIDMalformado:
    """Handler global de ValueError em main.py converte qualquer
    uuid.UUID('lixo') em 400 VALIDACAO_INVALIDA, sem 500."""

    async def test_insumo_uuid_invalido(self, client, token_admin):
        r = await client.get("/insumos/nao-e-uuid", headers=auth_headers(token_admin))
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_prato_uuid_invalido(self, client, token_admin):
        r = await client.get("/pratos/nao-e-uuid", headers=auth_headers(token_admin))
        assert r.status_code == 400

    async def test_refeicao_uuid_invalido(self, client, token_chef):
        r = await client.get("/refeicoes/nao-e-uuid", headers=auth_headers(token_chef))
        assert r.status_code == 400

    async def test_menu_uuid_invalido(self, client, token_admin):
        r = await client.get("/menus/nao-e-uuid", headers=auth_headers(token_admin))
        assert r.status_code == 400


@pytest.mark.asyncio
class TestCamposObrigatorios:
    """Pydantic rejeita antes de tocar no banco — status 422."""

    async def test_criar_insumo_sem_nome(self, client, token_compras, conn):
        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        r = await client.post("/insumos", headers=auth_headers(token_compras),
                              json={"categoria_id": str(cat_id), "unidade": "KG"})
        assert r.status_code == 422

    async def test_criar_prato_sem_rendimento(self, client, token_chef):
        r = await client.post("/pratos", headers=auth_headers(token_chef),
                              json={"nome": "X", "genero_prato": "Prato Principal"})
        assert r.status_code == 422

    async def test_criar_refeicao_sem_data(self, client, token_chef):
        r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                              json={"genero_refeicao": "Jantar", "horario_inicio": "18:00",
                                    "horario_fim": "21:00", "qtd_pessoas": 5})
        assert r.status_code == 422

    async def test_login_sem_senha(self, client):
        r = await client.post("/auth/login", json={"email": "x@x.com"})
        assert r.status_code == 422


@pytest.mark.asyncio
class TestTransicoesStatusInvalidas:

    async def test_confirmar_refeicao_ja_confirmada(self, client, token_chef):
        r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                              json={"genero_refeicao": "Jantar", "data": "2026-08-01",
                                    "horario_inicio": "18:00", "horario_fim": "21:00", "qtd_pessoas": 1})
        ref_id = r.json()["id"]
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))

        r2 = await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))
        assert r2.status_code == 409
        assert err(r2) == "TRANSICAO_STATUS_INVALIDA"

    async def test_executar_refeicao_planejada_retorna_409(self, client, token_chef):
        r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                              json={"genero_refeicao": "Jantar", "data": "2026-08-02",
                                    "horario_inicio": "18:00", "horario_fim": "21:00", "qtd_pessoas": 1})
        ref_id = r.json()["id"]
        # Tentativa de executar sem confirmar antes
        r2 = await client.patch(f"/refeicoes/{ref_id}/executar", headers=auth_headers(token_chef))
        assert r2.status_code == 409
        assert err(r2) == "TRANSICAO_STATUS_INVALIDA"

    async def test_servir_refeicao_confirmada_retorna_409(self, client, token_chef):
        r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                              json={"genero_refeicao": "Jantar", "data": "2026-08-03",
                                    "horario_inicio": "18:00", "horario_fim": "21:00", "qtd_pessoas": 1})
        ref_id = r.json()["id"]
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))
        # Tentativa de servir pulando EXECUTADA
        r2 = await client.patch(f"/refeicoes/{ref_id}/servir", headers=auth_headers(token_chef))
        assert r2.status_code == 409

    async def test_aprovar_prato_nao_pendente_retorna_409(self, client, token_chef):
        r = await client.post("/pratos", headers=auth_headers(token_chef),
                              json={"nome": "Prato Ativo", "genero_prato": "Prato Principal",
                                    "rendimento_base_porcoes": 1})
        prato_id = r.json()["id"]
        assert r.json()["status"] == "ATIVO"

        r2 = await client.patch(f"/pratos/{prato_id}/aprovar", headers=auth_headers(token_chef))
        assert r2.status_code == 409
        assert err(r2) == "PRATO_NAO_PENDENTE_APROVACAO"

    async def test_adicionar_item_refeicao_confirmada_retorna_409(self, client, token_chef, token_admin, conn):
        # Cria dois pratos distintos — o segundo é para tentar adicionar
        # depois da confirmação (usar o mesmo prato já adicionado poderia
        # disparar UNIQUE constraint em vez de REFEICAO_JA_CONFIRMADA)
        cat_id = await conn.fetchval("SELECT id FROM categorias WHERE nome='Carnes, Aves e Peixes'")
        ins_r = await client.post("/insumos", headers=auth_headers(token_admin),
                                  json={"nome": "Insumo Trans", "categoria_id": str(cat_id), "unidade": "KG"})
        ins_id = ins_r.json()["id"]

        prato1_r = await client.post("/pratos", headers=auth_headers(token_chef),
                                     json={"nome": "Prato Trans 1", "genero_prato": "Prato Principal",
                                           "rendimento_base_porcoes": 1,
                                           "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                              "peso_bruto": 1, "fator_correcao": 1}]})
        prato1_id = prato1_r.json()["id"]

        prato2_r = await client.post("/pratos", headers=auth_headers(token_chef),
                                     json={"nome": "Prato Trans 2", "genero_prato": "Guarnição",
                                           "rendimento_base_porcoes": 1,
                                           "itens_receita": [{"insumo_id": ins_id, "tipo": "ALIMENTICIO",
                                                              "peso_bruto": 0.5, "fator_correcao": 1}]})
        prato2_id = prato2_r.json()["id"]

        ref_r = await client.post("/refeicoes", headers=auth_headers(token_chef),
                                  json={"genero_refeicao": "Jantar", "data": "2026-08-04",
                                        "horario_inicio": "18:00", "horario_fim": "21:00", "qtd_pessoas": 1})
        ref_id = ref_r.json()["id"]
        await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                          json={"prato_id": prato1_id})
        await client.patch(f"/refeicoes/{ref_id}/confirmar", headers=auth_headers(token_chef))

        # Tenta adicionar SEGUNDO prato (diferente) depois de confirmada
        # — deve falhar por status, não por UNIQUE constraint
        r = await client.post(f"/refeicoes/{ref_id}/itens", headers=auth_headers(token_chef),
                              json={"prato_id": prato2_id})
        assert r.status_code == 409
        assert err(r) == "REFEICAO_JA_CONFIRMADA"
