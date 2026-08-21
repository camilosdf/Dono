# backend/tests/test_insumos_lotes_cotacoes.py — Sistema Dono
#
# Expansão de cobertura do módulo de insumos, focando nas lacunas
# não cobertas por test_insumos.py:
#
#   A) PATCH /insumos/{id} — atualização cadastral
#   B) GET /insumos/{id}/lotes — listagem e ordenação FEFO
#   C) POST /insumos/{id}/lotes — lote com validade, fornecedor, permissões
#   D) POST /cotacoes — criação manual (origem=MANUAL, PENDENTE_REVISAO)
#   E) GET /insumos/{id}/cotacoes — listagem com filtro de status
#   F) PATCH /cotacoes/{id}/aprovar — aprovação com aprovado_por
#   G) PATCH /cotacoes/{id}/rejeitar — rejeição
#   H) Idempotência — aprovar/rejeitar cotação já processada → 409
#
# O que JÁ está em test_insumos.py e NÃO é repetido aqui:
#   - Criar insumo (201, campos)
#   - Listar com filtro de gênero
#   - Obter existente/inexistente/UUID malformado
#   - Lote que atualiza custo_medio
#   - Soft delete (sem receita / em uso)
#
# Fixtures: client, conn, token_admin, token_compras, token_chef, token_gestao

import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

async def _criar_insumo(client, token_admin, conn, nome=None, unidade="KG"):
    """Cria um insumo via API e retorna o id."""
    cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
    nome = nome or f"Insumo_{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/insumos",
        headers=auth_headers(token_admin),
        json={"nome": nome, "categoria_id": str(cat_id), "unidade": unidade},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _criar_lote(client, token_admin, insumo_id,
                       valor=10.0, quantidade=100.0,
                       data_aquisicao=None, data_validade=None,
                       fornecedor_id=None):
    """Cria um lote via API e retorna o response JSON."""
    data_aquisicao = data_aquisicao or date.today().isoformat()
    payload = {
        "valor_aquisicao": valor,
        "data_aquisicao": data_aquisicao,
        "quantidade": quantidade,
    }
    if data_validade:
        payload["data_validade"] = data_validade
    if fornecedor_id:
        payload["fornecedor_id"] = fornecedor_id
    r = await client.post(
        f"/insumos/{insumo_id}/lotes",
        headers=auth_headers(token_admin),
        json=payload,
    )
    return r


async def _criar_cotacao_manual(client, token_compras, insumo_id,
                                  preco=15.0, fornecedor_id=None):
    """Cria uma cotação manual via API e retorna o response JSON."""
    payload = {"insumo_id": insumo_id, "preco_unitario": preco}
    if fornecedor_id:
        payload["fornecedor_id"] = fornecedor_id
    r = await client.post(
        "/cotacoes",
        headers=auth_headers(token_compras),
        json=payload,
    )
    return r


# =====================================================================
# A) PATCH /insumos/{id}
# =====================================================================

@pytest.mark.asyncio
class TestAtualizarInsumo:
    """Testa PATCH /insumos/{id} — atualização cadastral."""

    async def test_atualizar_nome(self, client, token_admin, conn):
        """PATCH com novo nome deve atualizar e retornar o nome atualizado."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.patch(
            f"/insumos/{insumo_id}",
            headers=auth_headers(token_admin),
            json={"nome": "Nome Atualizado"},
        )
        assert r.status_code == 200
        assert r.json()["nome"] == "Nome Atualizado"

    async def test_atualizar_campos_independentemente(self, client, token_admin, conn):
        """PATCH com apenas um campo não deve alterar os demais (COALESCE)."""
        insumo_id = await _criar_insumo(client, token_admin, conn, unidade="KG")
        r = await client.patch(
            f"/insumos/{insumo_id}",
            headers=auth_headers(token_admin),
            json={"nome": "Novo Nome"},
        )
        # unidade deve continuar KG
        assert r.json()["unidade"] == "KG"

    async def test_atualizar_localizacao_estoque(self, client, token_admin, conn):
        """PATCH pode atualizar localizacao_estoque."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.patch(
            f"/insumos/{insumo_id}",
            headers=auth_headers(token_admin),
            json={"localizacao_estoque": "Prateleira A-3"},
        )
        assert r.status_code == 200
        assert r.json()["localizacao_estoque"] == "Prateleira A-3"

    async def test_atualizar_insumo_inexistente_retorna_404(
        self, client, token_admin
    ):
        """PATCH em UUID inexistente deve retornar 404."""
        r = await client.patch(
            f"/insumos/{uuid.uuid4()}",
            headers=auth_headers(token_admin),
            json={"nome": "X"},
        )
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_chef_nao_pode_atualizar_insumo(self, client, token_chef, conn, token_admin):
        """Perfil CHEF não pode atualizar insumo (requer COMPRAS ou ADMIN)."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.patch(
            f"/insumos/{insumo_id}",
            headers=auth_headers(token_chef),
            json={"nome": "Hack"},
        )
        assert r.status_code == 403


# =====================================================================
# B) GET /insumos/{id}/lotes
# =====================================================================

@pytest.mark.asyncio
class TestListarLotes:
    """Testa GET /insumos/{id}/lotes — listagem e ordenação FEFO."""

    async def test_listar_lotes_vazio(self, client, token_admin, conn):
        """Insumo sem lotes deve retornar lista vazia."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.get(f"/insumos/{insumo_id}/lotes",
                              headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json() == []

    async def test_listar_lotes_apos_criacao(self, client, token_admin, conn):
        """Lote criado deve aparecer na listagem."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_lote(client, token_admin, insumo_id)

        r = await client.get(f"/insumos/{insumo_id}/lotes",
                              headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert len(r.json()) == 1

    async def test_listar_lotes_ordenacao_fefo(self, client, token_admin, conn):
        """Lotes com validade devem ser ordenados do que vence primeiro
        (FEFO — First Expired, First Out)."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        hoje = date.today()

        # Lote que vence depois
        await _criar_lote(client, token_admin, insumo_id,
                           data_validade=(hoje + timedelta(days=30)).isoformat())
        # Lote que vence antes
        await _criar_lote(client, token_admin, insumo_id,
                           data_validade=(hoje + timedelta(days=5)).isoformat())

        r = await client.get(f"/insumos/{insumo_id}/lotes",
                              headers=auth_headers(token_admin))
        lotes = r.json()
        assert len(lotes) == 2
        # Primeiro deve ser o que vence em 5 dias
        assert lotes[0]["data_validade"] < lotes[1]["data_validade"]

    async def test_listar_lotes_sem_validade_vem_por_ultimo(
        self, client, token_admin, conn
    ):
        """Lotes sem data_validade (NULLS LAST) devem vir após os com validade."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        hoje = date.today()

        # Lote sem validade
        await _criar_lote(client, token_admin, insumo_id)
        # Lote com validade
        await _criar_lote(client, token_admin, insumo_id,
                           data_validade=(hoje + timedelta(days=10)).isoformat())

        r = await client.get(f"/insumos/{insumo_id}/lotes",
                              headers=auth_headers(token_admin))
        lotes = r.json()
        assert len(lotes) == 2
        assert lotes[0]["data_validade"] is not None
        assert lotes[1]["data_validade"] is None

    async def test_lote_campos_obrigatorios(self, client, token_admin, conn):
        """Cada lote deve conter os campos do modelo LoteOut."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_lote(client, token_admin, insumo_id)

        r = await client.get(f"/insumos/{insumo_id}/lotes",
                              headers=auth_headers(token_admin))
        lote = r.json()[0]
        for campo in ("id", "insumo_id", "valor_aquisicao", "data_aquisicao",
                       "quantidade", "quantidade_disponivel"):
            assert campo in lote


# =====================================================================
# C) POST /insumos/{id}/lotes
# =====================================================================

@pytest.mark.asyncio
class TestRegistrarLote:
    """Testa POST /insumos/{id}/lotes — criação de lote com variações."""

    async def test_lote_com_validade(self, client, token_admin, conn):
        """Lote com data_validade deve ser registrado e retornar o campo."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        validade = (date.today() + timedelta(days=90)).isoformat()
        r = await _criar_lote(client, token_admin, insumo_id, data_validade=validade)

        assert r.status_code == 201
        assert r.json()["data_validade"] == validade

    async def test_lote_com_fornecedor(self, client, token_admin, conn):
        """Lote com fornecedor_id deve persistir a referência."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        forn_id = await conn.fetchval(
            "INSERT INTO fornecedores (nome, ativo) VALUES ('Forn Lote', TRUE) RETURNING id"
        )
        r = await _criar_lote(client, token_admin, insumo_id,
                               fornecedor_id=str(forn_id))

        assert r.status_code == 201
        assert r.json()["fornecedor_id"] == str(forn_id)

    async def test_lote_sem_validade_e_sem_fornecedor(self, client, token_admin, conn):
        """Campos opcionais (data_validade, fornecedor_id) podem ser omitidos."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await _criar_lote(client, token_admin, insumo_id)

        assert r.status_code == 201
        assert r.json()["data_validade"] is None
        assert r.json()["fornecedor_id"] is None

    async def test_quantidade_disponivel_igual_a_quantidade(
        self, client, token_admin, conn
    ):
        """No momento do registro, quantidade_disponivel deve igualar quantidade."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await _criar_lote(client, token_admin, insumo_id, quantidade=250.0)

        assert r.status_code == 201
        assert r.json()["quantidade"] == 250.0
        assert r.json()["quantidade_disponivel"] == 250.0

    async def test_compras_nao_pode_registrar_lote(
        self, client, token_compras, token_admin, conn
    ):
        """Perfil COMPRAS não pode registrar lote (requer ADMIN)."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.post(
            f"/insumos/{insumo_id}/lotes",
            headers=auth_headers(token_compras),
            json={"valor_aquisicao": 10.0,
                  "data_aquisicao": date.today().isoformat(),
                  "quantidade": 100.0},
        )
        assert r.status_code == 403

    async def test_sem_token_retorna_401(self, client, token_admin, conn):
        """POST em lotes sem autenticação deve retornar 401."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.post(
            f"/insumos/{insumo_id}/lotes",
            json={"valor_aquisicao": 10.0,
                  "data_aquisicao": date.today().isoformat(),
                  "quantidade": 100.0},
        )
        assert r.status_code == 401


# =====================================================================
# D) POST /cotacoes
# =====================================================================

@pytest.mark.asyncio
class TestCriarCotacaoManual:
    """Testa POST /cotacoes — criação de cotação manual."""

    async def test_criar_cotacao_manual_retorna_201(
        self, client, token_compras, token_admin, conn
    ):
        """Cotação manual deve retornar 201 com origem=MANUAL e
        status=PENDENTE_REVISAO."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await _criar_cotacao_manual(client, token_compras, insumo_id, preco=12.50)

        assert r.status_code == 201
        data = r.json()
        assert data["origem"] == "MANUAL"
        assert data["status"] == "PENDENTE_REVISAO"
        assert data["preco_unitario"] == 12.50
        assert data["insumo_id"] == insumo_id

    async def test_cotacao_com_fornecedor(
        self, client, token_compras, token_admin, conn
    ):
        """Cotação com fornecedor_id deve persistir a referência."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        forn_id = await conn.fetchval(
            "INSERT INTO fornecedores (nome, ativo) VALUES ('Forn Cotacao', TRUE) RETURNING id"
        )
        r = await _criar_cotacao_manual(client, token_compras, insumo_id,
                                         fornecedor_id=str(forn_id))

        assert r.status_code == 201
        assert r.json()["fornecedor_id"] == str(forn_id)

    async def test_chef_nao_pode_criar_cotacao(
        self, client, token_chef, token_admin, conn
    ):
        """Perfil CHEF não pode criar cotação manual (requer COMPRAS ou ADMIN)."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.post(
            "/cotacoes",
            headers=auth_headers(token_chef),
            json={"insumo_id": insumo_id, "preco_unitario": 10.0},
        )
        assert r.status_code == 403

    async def test_sem_token_retorna_401(self, client):
        """POST /cotacoes sem autenticação deve retornar 401."""
        r = await client.post(
            "/cotacoes",
            json={"insumo_id": str(uuid.uuid4()), "preco_unitario": 10.0},
        )
        assert r.status_code == 401


# =====================================================================
# E) GET /insumos/{id}/cotacoes
# =====================================================================

@pytest.mark.asyncio
class TestListarCotacoes:
    """Testa GET /insumos/{id}/cotacoes — listagem com filtro de status."""

    async def test_listar_cotacoes_vazio(self, client, token_admin, conn):
        """Insumo sem cotações deve retornar lista vazia."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        r = await client.get(f"/insumos/{insumo_id}/cotacoes",
                              headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert r.json() == []

    async def test_cotacao_criada_aparece_na_listagem(
        self, client, token_compras, token_admin, conn
    ):
        """Cotação criada deve aparecer em GET /insumos/{id}/cotacoes."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_cotacao_manual(client, token_compras, insumo_id)

        r = await client.get(f"/insumos/{insumo_id}/cotacoes",
                              headers=auth_headers(token_admin))
        assert len(r.json()) == 1

    async def test_filtro_por_status_pendente(
        self, client, token_compras, token_admin, conn
    ):
        """Filtro status=PENDENTE_REVISAO deve retornar apenas cotações pendentes."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_cotacao_manual(client, token_compras, insumo_id)

        r = await client.get(
            f"/insumos/{insumo_id}/cotacoes?status=PENDENTE_REVISAO",
            headers=auth_headers(token_admin),
        )
        assert len(r.json()) == 1
        assert all(c["status"] == "PENDENTE_REVISAO" for c in r.json())

    async def test_filtro_status_aprovada_retorna_vazio_antes_de_aprovar(
        self, client, token_compras, token_admin, conn
    ):
        """Filtro status=APROVADA deve retornar vazio antes de qualquer aprovação."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_cotacao_manual(client, token_compras, insumo_id)

        r = await client.get(
            f"/insumos/{insumo_id}/cotacoes?status=APROVADA",
            headers=auth_headers(token_admin),
        )
        assert r.json() == []

    async def test_cotacao_campos_obrigatorios(
        self, client, token_compras, token_admin, conn
    ):
        """Cada cotação deve conter os campos do modelo CotacaoOut."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        await _criar_cotacao_manual(client, token_compras, insumo_id)

        r = await client.get(f"/insumos/{insumo_id}/cotacoes",
                              headers=auth_headers(token_admin))
        cotacao = r.json()[0]
        for campo in ("id", "insumo_id", "preco_unitario", "data_cotacao",
                       "origem", "status"):
            assert campo in cotacao


# =====================================================================
# F) PATCH /cotacoes/{id}/aprovar
# =====================================================================

@pytest.mark.asyncio
class TestAprovarCotacao:
    """Testa PATCH /cotacoes/{id}/aprovar."""

    async def test_aprovar_cotacao_pendente(
        self, client, token_compras, token_admin, conn
    ):
        """Aprovar cotação pendente deve mudar status para APROVADA e
        registrar aprovado_por."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        r = await client.patch(
            f"/cotacoes/{cotacao_id}/aprovar",
            headers=auth_headers(token_admin),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "APROVADA"
        assert data["aprovado_por"] is not None

    async def test_aprovar_registra_aprovado_por_correto(
        self, client, token_compras, token_admin, conn, usuario_admin
    ):
        """aprovado_por deve ser o id do usuário que aprovou."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        r = await client.patch(
            f"/cotacoes/{cotacao_id}/aprovar",
            headers=auth_headers(token_admin),
        )
        assert r.json()["aprovado_por"] == str(usuario_admin["id"])

    async def test_aprovar_cotacao_ja_aprovada_retorna_409(
        self, client, token_compras, token_admin, conn
    ):
        """Tentativa de aprovar cotação já aprovada deve retornar 409
        com código COTACAO_JA_PROCESSADA."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        await client.patch(f"/cotacoes/{cotacao_id}/aprovar",
                           headers=auth_headers(token_admin))
        r = await client.patch(f"/cotacoes/{cotacao_id}/aprovar",
                                headers=auth_headers(token_admin))
        assert r.status_code == 409
        assert err(r) == "COTACAO_JA_PROCESSADA"

    async def test_sem_token_retorna_401(self, client):
        """PATCH /aprovar sem autenticação deve retornar 401."""
        r = await client.patch(f"/cotacoes/{uuid.uuid4()}/aprovar")
        assert r.status_code == 401


# =====================================================================
# G) PATCH /cotacoes/{id}/rejeitar
# =====================================================================

@pytest.mark.asyncio
class TestRejeitarCotacao:
    """Testa PATCH /cotacoes/{id}/rejeitar."""

    async def test_rejeitar_cotacao_pendente(
        self, client, token_compras, token_admin, conn
    ):
        """Rejeitar cotação pendente deve mudar status para REJEITADA."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        r = await client.patch(
            f"/cotacoes/{cotacao_id}/rejeitar",
            headers=auth_headers(token_admin),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "REJEITADA"

    async def test_rejeitar_cotacao_ja_rejeitada_retorna_409(
        self, client, token_compras, token_admin, conn
    ):
        """Rejeitar cotação já rejeitada deve retornar 409."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        await client.patch(f"/cotacoes/{cotacao_id}/rejeitar",
                           headers=auth_headers(token_admin))
        r = await client.patch(f"/cotacoes/{cotacao_id}/rejeitar",
                                headers=auth_headers(token_admin))
        assert r.status_code == 409
        assert err(r) == "COTACAO_JA_PROCESSADA"

    async def test_rejeitar_cotacao_aprovada_retorna_409(
        self, client, token_compras, token_admin, conn
    ):
        """Rejeitar cotação já aprovada deve retornar 409 — transição
        APROVADA → REJEITADA não é permitida."""
        insumo_id = await _criar_insumo(client, token_admin, conn)
        cotacao_id = (await _criar_cotacao_manual(
            client, token_compras, insumo_id
        )).json()["id"]

        await client.patch(f"/cotacoes/{cotacao_id}/aprovar",
                           headers=auth_headers(token_admin))
        r = await client.patch(f"/cotacoes/{cotacao_id}/rejeitar",
                                headers=auth_headers(token_admin))
        assert r.status_code == 409

    async def test_sem_token_retorna_401(self, client):
        """PATCH /rejeitar sem autenticação deve retornar 401."""
        r = await client.patch(f"/cotacoes/{uuid.uuid4()}/rejeitar")
        assert r.status_code == 401


# =====================================================================
# H) Fluxo completo de cotação
# =====================================================================

@pytest.mark.asyncio
class TestFluxoCotacao:
    """Testa o fluxo completo: criar → aprovar → verificar na listagem."""

    async def test_fluxo_criar_aprovar_verificar(
        self, client, token_compras, token_admin, conn
    ):
        """Fluxo: criar cotação → aprovar → verificar status na listagem."""
        insumo_id = await _criar_insumo(client, token_admin, conn)

        # Criar
        r_criar = await _criar_cotacao_manual(client, token_compras, insumo_id, preco=8.75)
        assert r_criar.status_code == 201
        cotacao_id = r_criar.json()["id"]
        assert r_criar.json()["status"] == "PENDENTE_REVISAO"

        # Aprovar
        r_aprovar = await client.patch(
            f"/cotacoes/{cotacao_id}/aprovar",
            headers=auth_headers(token_admin),
        )
        assert r_aprovar.status_code == 200
        assert r_aprovar.json()["status"] == "APROVADA"

        # Verificar na listagem com filtro
        r_lista = await client.get(
            f"/insumos/{insumo_id}/cotacoes?status=APROVADA",
            headers=auth_headers(token_admin),
        )
        ids = [c["id"] for c in r_lista.json()]
        assert cotacao_id in ids

    async def test_fluxo_criar_rejeitar_verificar(
        self, client, token_compras, token_admin, conn
    ):
        """Fluxo: criar cotação → rejeitar → verificar status na listagem."""
        insumo_id = await _criar_insumo(client, token_admin, conn)

        r_criar = await _criar_cotacao_manual(client, token_compras, insumo_id)
        cotacao_id = r_criar.json()["id"]

        await client.patch(f"/cotacoes/{cotacao_id}/rejeitar",
                           headers=auth_headers(token_admin))

        r_lista = await client.get(
            f"/insumos/{insumo_id}/cotacoes?status=REJEITADA",
            headers=auth_headers(token_admin),
        )
        ids = [c["id"] for c in r_lista.json()]
        assert cotacao_id in ids

    async def test_multiplas_cotacoes_mesmo_insumo(
        self, client, token_compras, token_admin, conn
    ):
        """Um insumo pode ter múltiplas cotações simultâneas de diferentes
        fornecedores — nenhuma restrição de unicidade deve bloquear."""
        insumo_id = await _criar_insumo(client, token_admin, conn)

        for preco in (10.0, 12.0, 15.0):
            r = await _criar_cotacao_manual(client, token_compras, insumo_id,
                                             preco=preco)
            assert r.status_code == 201

        r_lista = await client.get(
            f"/insumos/{insumo_id}/cotacoes",
            headers=auth_headers(token_admin),
        )
        assert len(r_lista.json()) == 3
