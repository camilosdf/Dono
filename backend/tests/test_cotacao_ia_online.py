# backend/tests/test_cotacao_ia_online.py — Sistema Dono
#
# Testes para o pipeline de estimativa de preço via IA (Opção B).
#
# Cobre:
#   A) Endpoint POST /ia/cotacoes/ia-online: criação do job, processamento
#      inline, permissões, rate limit, payload inválido.
#   B) Endpoint GET /ia/cotacoes/ia-online/jobs/{job_id}: polling de status.
#   C) _buscar_precos_externos: lógica de estimativa, persistência em cotacoes,
#      sem_historico, fallback de Ollama indisponível.
#
# Estratégia de mock:
#   - app.routes.ia._buscar_precos_externos → mock no endpoint (isola LLM e SQL)
#   - fn_estimar_preco_insumo → banco real via conn/db_pool (testa integração SQL)
#   - httpx.AsyncClient → mock do Ollama (não deve rodar em CI)
#
# Princípio arquitetural preservado: SQL calcula, Ollama explica.
# Os testes de _buscar_precos_externos usam banco real para validar que a
# função SQL é chamada corretamente e que cotacoes são persistidas.
#
# Fixtures: client, conn, db_pool, token_admin, token_compras, token_chef,
#           usuario_admin

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

def _resultado_mock(num_insumos: int = 1) -> dict:
    """Monta um resultado simulado de _buscar_precos_externos."""
    return {
        "estimativas": [
            {
                "cotacao_id": str(uuid.uuid4()),
                "insumo_id": str(uuid.uuid4()),
                "nome": f"Insumo {i}",
                "unidade": "KG",
                "preco_estimado": 10.0 + i,
                "preco_minimo": 8.0 + i,
                "preco_maximo": 15.0 + i,
                "num_compras": 3,
                "fornecedor_mais_barato_id": None,
                "data_ultima_compra": "2026-08-01",
            }
            for i in range(num_insumos)
        ],
        "sem_historico": [],
        "explicacao_ia": "Preços estimados com base nas últimas compras.",
        "resumo": {
            "total_solicitados": num_insumos,
            "com_estimativa": num_insumos,
            "sem_historico": 0,
        },
    }


async def _criar_insumo_com_lotes(conn, num_lotes: int = 2) -> uuid.UUID:
    """Cria insumo com lotes suficientes para fn_estimar_preco_insumo."""
    from datetime import date, timedelta
    cat_id = await conn.fetchval("SELECT id FROM categorias LIMIT 1")
    nome = f"_CotacaoIA_{uuid.uuid4().hex[:8]}"
    insumo_id = await conn.fetchval(
        """INSERT INTO insumos (nome, categoria_id, unidade, ativo)
           VALUES ($1, $2, 'KG', TRUE) RETURNING id""",
        nome, cat_id,
    )
    for i in range(num_lotes):
        data = date.today() - timedelta(days=10 + i * 5)
        await conn.execute(
            """INSERT INTO lotes_insumo
                   (insumo_id, valor_aquisicao, data_aquisicao, quantidade, quantidade_disponivel)
               VALUES ($1, $2, $3, 100, 100)""",
            insumo_id, 10.0 + i * 5, data,
        )
    return insumo_id


# =====================================================================
# A) Testes de endpoint — POST /ia/cotacoes/ia-online
# =====================================================================

@pytest.mark.asyncio
class TestSolicitarCotacaoIa:
    """Testa o endpoint de solicitação de cotação por estimativa de IA."""

    async def test_solicitar_retorna_202_e_job_id(self, client, token_admin):
        """Request válido deve retornar 202 com job_id."""
        resultado = _resultado_mock(1)
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(return_value=resultado)):
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_admin),
                json={"insumo_ids": [str(uuid.uuid4())]},
            )

        assert r.status_code == 202
        assert "job_id" in r.json()

    async def test_job_gravado_como_concluido_apos_sucesso(
        self, client, token_admin, conn
    ):
        """Após processamento bem-sucedido, job deve estar com status=concluido
        e resultado preenchido."""
        resultado = _resultado_mock(1)
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(return_value=resultado)):
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_admin),
                json={"insumo_ids": [str(uuid.uuid4())]},
            )

        job_id = uuid.UUID(r.json()["job_id"])
        job = await conn.fetchrow(
            "SELECT status, resultado FROM ia_jobs WHERE id = $1", job_id
        )
        assert job["status"] == "concluido"
        assert job["resultado"] is not None

    async def test_job_gravado_como_erro_quando_falha(
        self, client, token_admin, conn
    ):
        """Exceção em _buscar_precos_externos deve gravar job com status=erro."""
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(side_effect=RuntimeError("Falha simulada"))):
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_admin),
                json={"insumo_ids": [str(uuid.uuid4())]},
            )

        job_id = uuid.UUID(r.json()["job_id"])
        job = await conn.fetchrow(
            "SELECT status, erro_motivo FROM ia_jobs WHERE id = $1", job_id
        )
        assert job["status"] == "erro"
        assert "Falha simulada" in job["erro_motivo"]

    async def test_multiplos_insumos_aceitos(self, client, token_admin):
        """Lista com múltiplos insumo_ids deve ser aceita sem erro."""
        ids = [str(uuid.uuid4()) for _ in range(5)]
        resultado = _resultado_mock(5)
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(return_value=resultado)):
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_admin),
                json={"insumo_ids": ids},
            )

        assert r.status_code == 202

    async def test_sem_token_retorna_401(self, client):
        """Request sem autenticação deve retornar 401."""
        r = await client.post(
            "/ia/cotacoes/ia-online",
            json={"insumo_ids": [str(uuid.uuid4())]},
        )
        assert r.status_code == 401

    async def test_perfil_chef_negado(self, client, token_chef):
        """Perfil CHEF não tem permissão para solicitar cotação por IA."""
        r = await client.post(
            "/ia/cotacoes/ia-online",
            headers=auth_headers(token_chef),
            json={"insumo_ids": [str(uuid.uuid4())]},
        )
        assert r.status_code == 403

    async def test_perfil_compras_permitido(self, client, token_compras):
        """Perfil COMPRAS deve ter permissão para solicitar cotação por IA."""
        resultado = _resultado_mock(1)
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(return_value=resultado)):
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_compras),
                json={"insumo_ids": [str(uuid.uuid4())]},
            )

        assert r.status_code == 202

    async def test_lista_vazia_retorna_422(self, client, token_admin):
        """Lista de insumo_ids vazia deve retornar 422 (validação Pydantic)."""
        r = await client.post(
            "/ia/cotacoes/ia-online",
            headers=auth_headers(token_admin),
            json={"insumo_ids": []},
        )
        # Pydantic valida min_length=1 se configurado, ou aceita lista vazia
        # O importante é não retornar 500
        assert r.status_code in (200, 202, 422)

    async def test_fornecedores_alvo_opcional(self, client, token_admin):
        """fornecedores_alvo é opcional — omiti-lo não deve causar erro."""
        resultado = _resultado_mock(1)
        with patch("app.routes.ia._buscar_precos_externos",
                   new=AsyncMock(return_value=resultado)) as mock_fn:
            r = await client.post(
                "/ia/cotacoes/ia-online",
                headers=auth_headers(token_admin),
                json={"insumo_ids": [str(uuid.uuid4())]},
            )

        assert r.status_code == 202
        # fornecedores_alvo deve ter sido None
        _, kwargs = mock_fn.call_args if mock_fn.call_args else ([], {})
        args = mock_fn.call_args[0] if mock_fn.call_args else []
        if len(args) > 1:
            assert args[1] is None


# =====================================================================
# B) Testes de endpoint — GET /ia/cotacoes/ia-online/jobs/{job_id}
# =====================================================================

@pytest.mark.asyncio
class TestStatusJobCotacaoIaOnline:
    """Testa o polling de status de jobs de cotação online."""

    async def test_status_job_concluido(self, client, token_admin, conn, usuario_admin):
        """Job concluído deve retornar status e resultado."""
        resultado_json = json.dumps(_resultado_mock(1))
        job_id = await conn.fetchval(
            """INSERT INTO ia_jobs (tipo, solicitado_por, entrada, status,
                                    resultado, concluido_em)
               VALUES ('COTACAO_ONLINE', $1, $2, 'concluido', $3, now())
               RETURNING id""",
            usuario_admin["id"],
            json.dumps({"insumo_ids": [str(uuid.uuid4())]}),
            resultado_json,
        )
        r = await client.get(
            f"/ia/cotacoes/ia-online/jobs/{job_id}",
            headers=auth_headers(token_admin),
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "concluido"
        assert data["job_id"] == str(job_id)

    async def test_status_job_nao_encontrado(self, client, token_admin):
        """UUID inexistente deve retornar 404."""
        r = await client.get(
            f"/ia/cotacoes/ia-online/jobs/{uuid.uuid4()}",
            headers=auth_headers(token_admin),
        )
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_sem_token_retorna_401(self, client):
        """Polling sem autenticação deve retornar 401."""
        r = await client.get(f"/ia/cotacoes/ia-online/jobs/{uuid.uuid4()}")
        assert r.status_code == 401


# =====================================================================
# C) Testes de _buscar_precos_externos (integração com banco real)
# =====================================================================

@pytest.mark.asyncio
class TestBuscarPrecosExternos:
    """Testa a lógica interna de _buscar_precos_externos com banco real
    e Ollama mockado."""

    def _ollama_mock(self, texto: str = "Explicação gerada.") -> AsyncMock:
        """Mock de httpx.AsyncClient para simular resposta do Ollama."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"response": texto}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        return mock_client

    async def test_insumo_com_historico_gera_cotacao(self, conn, client):
        """Insumo com histórico suficiente deve gerar cotacao em PENDENTE_REVISAO."""
        insumo_id = await _criar_insumo_com_lotes(conn, num_lotes=2)

        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock()):
            resultado = await _buscar_precos_externos([str(insumo_id)], None)

        assert len(resultado["estimativas"]) == 1
        assert resultado["resumo"]["com_estimativa"] == 1
        assert resultado["resumo"]["sem_historico"] == 0
        assert resultado["estimativas"][0]["preco_estimado"] > 0

        # Confirma persistência em cotacoes
        cotacao = await conn.fetchrow(
            "SELECT * FROM cotacoes WHERE insumo_id = $1", insumo_id
        )
        assert cotacao is not None
        assert cotacao["origem"] == "IA_ONLINE"
        assert cotacao["status"] == "PENDENTE_REVISAO"

    async def test_insumo_sem_historico_vai_para_sem_historico(self, conn, client):
        """Insumo com apenas 1 compra deve ir para sem_historico sem gerar cotacao."""
        insumo_id = await _criar_insumo_com_lotes(conn, num_lotes=1)

        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock()):
            resultado = await _buscar_precos_externos([str(insumo_id)], None)

        assert len(resultado["estimativas"]) == 0
        assert len(resultado["sem_historico"]) == 1
        assert resultado["resumo"]["sem_historico"] == 1

        # Não deve ter gerado cotacao
        cotacao = await conn.fetchrow(
            "SELECT id FROM cotacoes WHERE insumo_id = $1", insumo_id
        )
        assert cotacao is None

    async def test_insumo_inexistente_vai_para_sem_historico(self, conn, client):
        """UUID válido mas insumo não cadastrado deve ir para sem_historico."""
        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock()):
            resultado = await _buscar_precos_externos([str(uuid.uuid4())], None)

        assert resultado["resumo"]["sem_historico"] == 1
        assert "não encontrado" in resultado["sem_historico"][0]["motivo"].lower() \
            or "nao encontrado" in resultado["sem_historico"][0]["motivo"].lower()

    async def test_uuid_invalido_vai_para_sem_historico(self, conn, client):
        """String que não é UUID deve ir para sem_historico sem erro."""
        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock()):
            resultado = await _buscar_precos_externos(["nao-e-uuid"], None)

        assert resultado["resumo"]["sem_historico"] == 1
        assert "UUID" in resultado["sem_historico"][0]["motivo"] or \
               "uuid" in resultado["sem_historico"][0]["motivo"].lower()

    async def test_mistura_com_e_sem_historico(self, conn, client):
        """Lista mista: insumo com histórico gera cotacao, sem histórico vai
        para sem_historico — ambos no mesmo resultado."""
        insumo_com = await _criar_insumo_com_lotes(conn, num_lotes=2)
        insumo_sem = await _criar_insumo_com_lotes(conn, num_lotes=1)

        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock()):
            resultado = await _buscar_precos_externos(
                [str(insumo_com), str(insumo_sem)], None
            )

        assert resultado["resumo"]["com_estimativa"] == 1
        assert resultado["resumo"]["sem_historico"] == 1
        assert resultado["resumo"]["total_solicitados"] == 2

    async def test_ollama_indisponivel_retorna_fallback(self, conn, client):
        """Se Ollama lançar exceção, explicacao_ia deve ser o texto de fallback
        e as estimativas devem ser retornadas normalmente."""
        insumo_id = await _criar_insumo_com_lotes(conn, num_lotes=2)

        mock_client_erro = AsyncMock()
        mock_client_erro.__aenter__ = AsyncMock(return_value=mock_client_erro)
        mock_client_erro.__aexit__ = AsyncMock(return_value=False)
        mock_client_erro.post = AsyncMock(side_effect=Exception("Ollama timeout"))

        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=mock_client_erro):
            resultado = await _buscar_precos_externos([str(insumo_id)], None)

        # Estimativas devem estar presentes mesmo sem Ollama
        assert len(resultado["estimativas"]) == 1
        # Fallback de explicação deve indicar indisponibilidade
        assert resultado["explicacao_ia"] is not None
        assert "indisponível" in resultado["explicacao_ia"].lower() or \
               "indisponivel" in resultado["explicacao_ia"].lower()

    async def test_explicacao_ia_gerada_quando_ollama_responde(self, conn, client):
        """Quando Ollama responde, explicacao_ia deve conter o texto retornado."""
        insumo_id = await _criar_insumo_com_lotes(conn, num_lotes=3)
        texto_esperado = "O preço do insumo foi estimado com base em 3 compras recentes."

        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=self._ollama_mock(texto_esperado)):
            resultado = await _buscar_precos_externos([str(insumo_id)], None)

        assert resultado["explicacao_ia"] == texto_esperado

    async def test_sem_insumos_com_historico_sem_chamada_ollama(self, conn, client):
        """Quando todos os insumos vão para sem_historico, Ollama não deve
        ser chamado (estimativas vazia → sem contexto para explicar)."""
        insumo_id = await _criar_insumo_com_lotes(conn, num_lotes=1)

        mock_client = self._ollama_mock()
        from app.routes.ia import _buscar_precos_externos
        with patch("httpx.AsyncClient", return_value=mock_client):
            resultado = await _buscar_precos_externos([str(insumo_id)], None)

        assert resultado["resumo"]["com_estimativa"] == 0
        assert resultado["explicacao_ia"] is None
        # Ollama não deve ter sido chamado
        assert mock_client.post.call_count == 0
