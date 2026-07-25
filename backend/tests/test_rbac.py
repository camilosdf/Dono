# backend/tests/test_rbac.py — Sistema Dono
#
# Cobre: RBAC (perfil não-autorizado recebe 403, perfil autorizado
# passa). Um teste por rota sensível — não cobre todos os perfis de
# todas as rotas (isso seria combinatorial), mas garante que o mecanismo
# funciona e que cada perfil tem ao menos um teste de acesso negado.
import pytest

from tests.conftest import auth_headers, err


@pytest.mark.asyncio
class TestRBAC:

    # --- ADMIN como único perfil permitido ---------------------------------

    async def test_chef_nao_pode_criar_usuario(self, client, token_chef):
        r = await client.post("/usuarios", headers=auth_headers(token_chef),
                              json={"nome": "X", "email": "x@x.com", "senha": "123", "perfil": "CHEF"})
        assert r.status_code == 403
        envelope = r.json().get("detail", r.json()).get("error", {})
        assert envelope.get("code") == "PERMISSAO_NEGADA"
        assert "ADMIN" in str(envelope.get("details", {}).get("perfil_requerido", ""))
        assert envelope.get("details", {}).get("perfil_atual") == "CHEF"

    async def test_compras_nao_pode_criar_usuario(self, client, token_compras):
        r = await client.post("/usuarios", headers=auth_headers(token_compras),
                              json={"nome": "X", "email": "x@x.com", "senha": "123", "perfil": "CHEF"})
        assert r.status_code == 403

    async def test_gestao_nao_pode_criar_usuario(self, client, token_gestao):
        r = await client.post("/usuarios", headers=auth_headers(token_gestao),
                              json={"nome": "X", "email": "x@x.com", "senha": "123", "perfil": "CHEF"})
        assert r.status_code == 403

    async def test_admin_pode_criar_usuario(self, client, token_admin):
        r = await client.post("/usuarios", headers=auth_headers(token_admin),
                              json={"nome": "Novo", "email": "novo@teste.com", "senha": "123", "perfil": "CHEF"})
        assert r.status_code == 201

    # --- COMPRAS/ADMIN (ex.: criar insumo) ---------------------------------

    async def test_chef_nao_pode_criar_insumo(self, client, token_chef, conn):
        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        r = await client.post("/insumos", headers=auth_headers(token_chef),
                              json={"nome": "X", "categoria_id": str(cat_id), "unidade": "KG"})
        assert r.status_code == 403

    async def test_gestao_nao_pode_criar_insumo(self, client, token_gestao, conn):
        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        r = await client.post("/insumos", headers=auth_headers(token_gestao),
                              json={"nome": "X", "categoria_id": str(cat_id), "unidade": "KG"})
        assert r.status_code == 403

    async def test_compras_pode_criar_insumo(self, client, token_compras, conn):
        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        r = await client.post("/insumos", headers=auth_headers(token_compras),
                              json={"nome": "Insumo RBAC", "categoria_id": str(cat_id), "unidade": "KG"})
        assert r.status_code == 201

    # --- ADMIN apenas para registrar lote ----------------------------------

    async def test_compras_nao_pode_registrar_lote(self, client, token_compras, conn):
        """PATCH /insumos/{id}/lotes só ADMIN — mesmo COMPRAS não pode."""
        cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
        ins = await conn.fetchrow(
            "INSERT INTO insumos (nome, categoria_id, unidade) VALUES ('X',$1,'KG') RETURNING id", cat_id)
        r = await client.post(f"/insumos/{ins['id']}/lotes", headers=auth_headers(token_compras),
                              json={"valor_aquisicao": 10, "data_aquisicao": "2026-07-21", "quantidade": 1})
        assert r.status_code == 403

    # --- CHEF/ADMIN para criar prato ----------------------------------------

    async def test_compras_nao_pode_criar_prato(self, client, token_compras):
        r = await client.post("/pratos", headers=auth_headers(token_compras),
                              json={"nome": "X", "genero_prato": "Prato Principal",
                                    "rendimento_base_porcoes": 1})
        assert r.status_code == 403

    async def test_chef_pode_criar_prato(self, client, token_chef):
        r = await client.post("/pratos", headers=auth_headers(token_chef),
                              json={"nome": "Prato RBAC", "genero_prato": "Prato Principal",
                                    "rendimento_base_porcoes": 1})
        assert r.status_code == 201

    # --- Leitura é permitida para qualquer autenticado ----------------------

    async def test_qualquer_perfil_pode_listar_categorias(self, client, token_chef,
                                                           token_compras, token_gestao):
        for token in [token_chef, token_compras, token_gestao]:
            r = await client.get("/categorias", headers=auth_headers(token))
            assert r.status_code == 200

    async def test_categorias_e_rota_publica(self, client):
        """GET /categorias não exige autenticação — é um catálogo público.
        Só POST /categorias exige ADMIN."""
        r = await client.get("/categorias")
        assert r.status_code == 200

    # --- GESTAO/ADMIN para relatórios financeiros ---------------------------

    async def test_chef_nao_pode_ver_relatorio_consumo(self, client, token_chef):
        r = await client.get("/relatorios/consumo", headers=auth_headers(token_chef))
        assert r.status_code == 403

    async def test_gestao_pode_ver_relatorio_consumo(self, client, token_gestao):
        r = await client.get("/relatorios/consumo", headers=auth_headers(token_gestao))
        assert r.status_code == 200
