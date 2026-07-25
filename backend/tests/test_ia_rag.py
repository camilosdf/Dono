# backend/tests/test_ia_rag.py — Sistema Dono
#
# Testes de integração para o módulo de IA/RAG/OCR (Fase 7).
# Cobre os endpoints:
#   - POST /ia/consultar (RAG - busca semântica + LLM)
#   - POST /ia/processar-nota (OCR - processamento de notas fiscais)
#   - GET /ia/processar-nota/jobs/{job_id} (status do job de OCR)
#
# CORREÇÕES FINAIS (2026-07-24):
#   - Embedding fixo ([0.1] * 384) usado para documentos e pergunta,
#     garantindo similaridade e recuperação nos testes RAG.
#   - Adicionado mock_redis.ttl retornando 60 para evitar TypeError.
#   - Patches de gerar_embedding e consultar_llm devidamente aplicados.

import io
import json
import uuid
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image

from tests.conftest import auth_headers, err

# Vetor fixo para todos os embeddings nos testes RAG
VETOR_FIXO = [0.1] * 384


@pytest.mark.asyncio
class TestRAG:
    """Testes para o endpoint de consulta RAG (/ia/consultar)."""

    async def _criar_documento_teste(
        self,
        conn,
        titulo: str,
        conteudo: str,
        tipo: str = "FICHA_TECNICA",
        entidade_id: uuid.UUID = None
    ):
        """Cria um documento com embedding fixo (VETOR_FIXO)."""
        from app.rag import embedding_to_string

        embedding_str = embedding_to_string(VETOR_FIXO)

        doc_id = await conn.fetchval(
            """INSERT INTO documentos (titulo, conteudo, tipo, entidade_id, embedding)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            titulo, conteudo, tipo, entidade_id, embedding_str
        )
        return doc_id

    async def test_consultar_rag_sem_documentos(self, client, token_admin):
        """Deve retornar mensagem informativa quando não há documentos."""
        r = await client.post(
            "/ia/consultar",
            headers=auth_headers(token_admin),
            json={"pergunta": "Como preparo um molho bolonhesa?", "top_k": 3}
        )
        assert r.status_code == 200
        data = r.json()
        assert "resposta" in data
        assert "fontes" in data
        assert "Não encontrei informações relevantes" in data["resposta"]
        assert data["fontes"] == []

    async def test_consultar_rag_com_documentos(self, client, token_admin, conn):
        """Deve buscar documentos similares e gerar resposta com fontes."""
        # 1. Cria documentos com embedding fixo
        await self._criar_documento_teste(
            conn,
            "Receita Molho Bolonhesa",
            "Molho bolonhesa: refogue cebola, alho e aipo. Adicione carne moída e cozinhe. "
            "Adicione tomates pelados e cozinhe por 30 minutos.",
            "FICHA_TECNICA"
        )
        await self._criar_documento_teste(
            conn,
            "POP - Preparo de Carnes",
            "Carnes devem ser armazenadas a 4°C. O ponto ideal para carnes moídas é 71°C.",
            "POP"
        )

        # 2. Força o embedding da pergunta para o mesmo vetor fixo
        with patch("app.rag.gerar_embedding") as mock_gerar_embedding, \
             patch("app.routes.ia.consultar_llm") as mock_llm:
            mock_gerar_embedding.return_value = VETOR_FIXO
            mock_llm.return_value = (
                "Para preparar o molho bolonhesa, refogue cebola, alho e aipo, "
                "adicione carne moída e cozinhe, depois adicione tomates pelados "
                "e cozinhe por 30 minutos."
            )

            r = await client.post(
                "/ia/consultar",
                headers=auth_headers(token_admin),
                json={"pergunta": "Como preparo um molho bolonhesa?", "top_k": 2}
            )
            assert r.status_code == 200
            data = r.json()
            assert "resposta" in data
            assert "fontes" in data
            # Deve encontrar pelo menos o documento da receita
            assert len(data["fontes"]) > 0, "Nenhum documento encontrado (embedding não foi gerado corretamente)"

    async def test_consultar_rag_com_filtro_tipo(self, client, token_admin, conn):
        """Deve filtrar documentos por tipo (ex.: apenas POPs)."""
        await self._criar_documento_teste(
            conn, "Ficha Técnica Frango", "Frango assado...", "FICHA_TECNICA"
        )
        await self._criar_documento_teste(
            conn, "POP Higienização", "Lave as mãos...", "POP"
        )

        with patch("app.rag.gerar_embedding") as mock_gerar_embedding, \
             patch("app.routes.ia.consultar_llm") as mock_llm:
            mock_gerar_embedding.return_value = VETOR_FIXO
            mock_llm.return_value = "Resposta mockada"

            r = await client.post(
                "/ia/consultar",
                headers=auth_headers(token_admin),
                json={"pergunta": "Higienização", "top_k": 3, "tipo_documento": "POP"}
            )
            assert r.status_code == 200
            data = r.json()
            titulos = [f["titulo"] for f in data["fontes"]]
            assert any("POP" in t for t in titulos), "Nenhum documento POP encontrado"

    async def test_consultar_rag_com_entidade_id(self, client, token_admin, conn):
        """Deve filtrar documentos por entidade_id (ex.: prato_id)."""
        prato_id = uuid.uuid4()
        await self._criar_documento_teste(
            conn, "Ficha Prato A", "Conteúdo do prato A...", "FICHA_TECNICA"
        )
        await self._criar_documento_teste(
            conn,
            "Ficha Prato Específico",
            "Conteúdo específico...",
            "FICHA_TECNICA",
            entidade_id=prato_id
        )

        with patch("app.rag.gerar_embedding") as mock_gerar_embedding, \
             patch("app.routes.ia.consultar_llm") as mock_llm:
            mock_gerar_embedding.return_value = VETOR_FIXO
            mock_llm.return_value = "Resposta mockada"

            r = await client.post(
                "/ia/consultar",
                headers=auth_headers(token_admin),
                json={"pergunta": "Prato específico", "top_k": 3, "entidade_id": str(prato_id)}
            )
            assert r.status_code == 200
            data = r.json()
            titulos = [f["titulo"] for f in data["fontes"]]
            assert any("Ficha Prato Específico" in t for t in titulos), "Documento específico não encontrado"

    async def test_rag_rate_limit(self, client, token_admin):
        """Deve aplicar rate limit (10 req/hora para IA)."""
        # Dicionário local para simular o Redis
        redis_store = {}

        async def mock_incr(key):
            redis_store[key] = redis_store.get(key, 0) + 1
            return redis_store[key]

        async def mock_expire(key, seconds):
            return True

        async def mock_ttl(key):
            return 60

        with patch("app.rate_limit.get_redis") as mock_get_redis, \
            patch("app.routes.ia.consultar_llm") as mock_llm:
            mock_redis = AsyncMock()
            mock_redis.incr = AsyncMock(side_effect=mock_incr)
            mock_redis.expire = AsyncMock(side_effect=mock_expire)
            mock_redis.ttl = AsyncMock(side_effect=mock_ttl)
            mock_get_redis.return_value = mock_redis
            mock_llm.return_value = "Resposta mockada"

            for i in range(11):
                r = await client.post(
                    "/ia/consultar",
                    headers=auth_headers(token_admin),
                    json={"pergunta": f"Pergunta {i}", "top_k": 1}
                )
                if i < 10:
                    assert r.status_code == 200
                else:
                    assert r.status_code == 429, f"Esperado 429, recebido {r.status_code}"

    async def test_rag_sem_autenticacao(self, client):
        """Requisição sem token deve retornar 401."""
        r = await client.post(
            "/ia/consultar",
            json={"pergunta": "Teste", "top_k": 1}
        )
        assert r.status_code == 401

    async def test_rag_permissao_chef(self, client, token_chef):
        """Chef deve ter acesso ao RAG (permissão CHEF, GESTAO, ADMIN)."""
        with patch("app.routes.ia.consultar_llm") as mock_llm:
            mock_llm.return_value = "Resposta mockada"
            r = await client.post(
                "/ia/consultar",
                headers=auth_headers(token_chef),
                json={"pergunta": "Teste", "top_k": 1}
            )
            # Pode retornar 200 (se não encontrar documentos) ou 404
            assert r.status_code in (200, 404)

    async def test_rag_permissao_compras(self, client, token_compras):
        """Compras NÃO deve ter acesso ao RAG (apenas CHEF, GESTAO, ADMIN)."""
        r = await client.post(
            "/ia/consultar",
            headers=auth_headers(token_compras),
            json={"pergunta": "Teste", "top_k": 1}
        )
        assert r.status_code == 403


@pytest.mark.asyncio
class TestOCR:
    """Testes para o endpoint de OCR (/ia/processar-nota)."""

    async def _criar_imagem_nota_teste(self) -> bytes:
        img = Image.new('RGB', (800, 600), color='white')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    async def _criar_pdf_nota_teste(self) -> bytes:
        return b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n4 0 obj\n<< /Length 100 >>\nstream\nBT /F1 12 Tf 100 700 Td (Nota Fiscal Teste) Tj ET\nendstream\nendobj\nxref\n0 5\ntrailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n200\n%%EOF'

    async def test_processar_nota_sem_arquivo(self, client, token_admin):
        r = await client.post("/ia/processar-nota", headers=auth_headers(token_admin))
        assert r.status_code == 422

    async def test_processar_nota_arquivo_grande(self, client, token_admin):
        arquivo_grande = b"0" * (11 * 1024 * 1024)
        files = {"arquivo": ("nota_grande.pdf", io.BytesIO(arquivo_grande), "application/pdf")}
        r = await client.post(
            "/ia/processar-nota",
            headers=auth_headers(token_admin),
            files=files
        )
        assert r.status_code == 413
        assert err(r) == "ARQUIVO_MUITO_GRANDE"

    async def test_processar_nota_formato_invalido(self, client, token_admin):
        arquivo = b"conteudo qualquer"
        files = {"arquivo": ("nota.txt", io.BytesIO(arquivo), "text/plain")}
        r = await client.post(
            "/ia/processar-nota",
            headers=auth_headers(token_admin),
            files=files
        )
        assert r.status_code == 400
        assert err(r) == "FORMATO_INVALIDO"

    async def test_processar_nota_com_imagem(self, client, token_admin):
        imagem_bytes = await self._criar_imagem_nota_teste()
        files = {"arquivo": ("nota.png", io.BytesIO(imagem_bytes), "image/png")}

        with patch("app.routes.ia.enfileirar_job_ocr") as mock_enfileirar:
            mock_job_id = uuid.uuid4()
            mock_enfileirar.return_value = mock_job_id

            r = await client.post(
                "/ia/processar-nota",
                headers=auth_headers(token_admin),
                files=files
            )
            assert r.status_code == 202
            data = r.json()
            assert "job_id" in data
            assert data["status"] == "pendente"
            assert mock_enfileirar.called

    async def test_processar_nota_com_pdf(self, client, token_admin):
        pdf_bytes = await self._criar_pdf_nota_teste()
        files = {"arquivo": ("nota.pdf", io.BytesIO(pdf_bytes), "application/pdf")}

        with patch("app.routes.ia.enfileirar_job_ocr") as mock_enfileirar:
            mock_job_id = uuid.uuid4()
            mock_enfileirar.return_value = mock_job_id

            r = await client.post(
                "/ia/processar-nota",
                headers=auth_headers(token_admin),
                files=files
            )
            assert r.status_code == 202
            data = r.json()
            assert "job_id" in data
            assert mock_enfileirar.called

    async def test_ocr_rate_limit(self, client, token_admin):
        """Deve aplicar rate limit de IA (10 req/hora) para OCR também."""
        redis_store = {}

        async def mock_incr(key):
            redis_store[key] = redis_store.get(key, 0) + 1
            return redis_store[key]

        async def mock_expire(key, seconds):
            return True

        async def mock_ttl(key):
            return 60

        with patch("app.routes.ia.enfileirar_job_ocr") as mock_enfileirar, \
            patch("app.routes.ia.acquire_ia_slot", new_callable=AsyncMock) as mock_acquire, \
            patch("app.routes.ia.release_ia_slot", new_callable=AsyncMock) as mock_release, \
            patch("app.rate_limit.get_redis") as mock_get_redis:
            mock_enfileirar.return_value = uuid.uuid4()
            mock_redis = AsyncMock()
            mock_redis.incr = AsyncMock(side_effect=mock_incr)
            mock_redis.expire = AsyncMock(side_effect=mock_expire)
            mock_redis.ttl = AsyncMock(side_effect=mock_ttl)
            mock_get_redis.return_value = mock_redis

            arquivo = b"teste"
            files = {"arquivo": ("nota.pdf", io.BytesIO(arquivo), "application/pdf")}

            for i in range(11):
                r = await client.post(
                    "/ia/processar-nota",
                    headers=auth_headers(token_admin),
                    files=files
                )
                if i < 10:
                    assert r.status_code == 202, f"Esperado 202, recebido {r.status_code}"
                else:
                    assert r.status_code == 429, f"Esperado 429, recebido {r.status_code}"

    async def test_ocr_permissao_compras(self, client, token_compras):
        arquivo = b"teste"
        files = {"arquivo": ("nota.pdf", io.BytesIO(arquivo), "application/pdf")}

        with patch("app.routes.ia.enfileirar_job_ocr") as mock_enfileirar:
            mock_enfileirar.return_value = uuid.uuid4()
            r = await client.post(
                "/ia/processar-nota",
                headers=auth_headers(token_compras),
                files=files
            )
            assert r.status_code == 202

    async def test_ocr_permissao_chef_negada(self, client, token_chef):
        arquivo = b"teste"
        files = {"arquivo": ("nota.pdf", io.BytesIO(arquivo), "application/pdf")}

        r = await client.post(
            "/ia/processar-nota",
            headers=auth_headers(token_chef),
            files=files
        )
        assert r.status_code == 403


@pytest.mark.asyncio
class TestOCRJobs:
    """Testes para consulta de status de jobs de OCR."""

    async def _criar_job_teste(self, conn, usuario_id: uuid.UUID, status: str = "pendente") -> uuid.UUID:
        job_id = await conn.fetchval(
            """INSERT INTO ia_jobs (tipo, solicitado_por, entrada, status)
               VALUES ('OCR_NOTA', $1, jsonb_build_object('test', true), $2)
               RETURNING id""",
            usuario_id,
            status
        )
        return job_id

    async def test_status_job_encontrado(self, client, token_admin, usuario_admin, conn):
        usuario_id = uuid.UUID(usuario_admin["id"])
        job_id = await self._criar_job_teste(conn, usuario_id, "concluido")

        await conn.execute(
            """UPDATE ia_jobs
               SET resultado = jsonb_build_object('status', 'ok', 'itens', 5),
                   concluido_em = now()
               WHERE id = $1""",
            job_id
        )

        r = await client.get(
            f"/ia/processar-nota/jobs/{job_id}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["job_id"] == str(job_id)
        assert data["status"] == "concluido"
        assert "resultado" in data

        # CORREÇÃO: o campo 'resultado' é JSONB e pode vir como string
        resultado = json.loads(data["resultado"])
        assert resultado["status"] == "ok"

    async def test_status_job_nao_encontrado(self, client, token_admin):
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = await client.get(
            f"/ia/processar-nota/jobs/{fake_id}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_status_job_pertencente_outro_usuario(self, client, token_admin, token_compras, usuario_admin, conn):
        usuario_admin_id = uuid.UUID(usuario_admin["id"])
        job_id = await self._criar_job_teste(conn, usuario_admin_id, "pendente")

        r = await client.get(
            f"/ia/processar-nota/jobs/{job_id}",
            headers=auth_headers(token_compras)
        )
        assert r.status_code == 403
        assert err(r) == "PERMISSAO_NEGADA"

    async def test_status_job_erro(self, client, token_admin, usuario_admin, conn):
        usuario_id = uuid.UUID(usuario_admin["id"])
        job_id = await self._criar_job_teste(conn, usuario_id, "erro")

        await conn.execute(
            """UPDATE ia_jobs
               SET erro_motivo = 'Falha no OCR: texto não encontrado',
                   concluido_em = now()
               WHERE id = $1""",
            job_id
        )

        r = await client.get(
            f"/ia/processar-nota/jobs/{job_id}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "erro"
        assert data["erro_motivo"] == "Falha no OCR: texto não encontrado"

    async def test_status_job_pendente(self, client, token_admin, usuario_admin, conn):
        usuario_id = uuid.UUID(usuario_admin["id"])
        job_id = await self._criar_job_teste(conn, usuario_id, "pendente")

        r = await client.get(
            f"/ia/processar-nota/jobs/{job_id}",
            headers=auth_headers(token_admin)
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "pendente"
        assert "resultado" not in data or data["resultado"] is None
        assert "erro_motivo" not in data or data["erro_motivo"] is None


@pytest.mark.asyncio
class TestOCRIntegracaoWorker:
    """Testes de integração com o ai_worker (requer worker rodando)."""

    @pytest.mark.skip(reason="Requer ai_worker rodando e Redis configurado")
    async def test_fluxo_completo_ocr(self, client, token_admin):
        arquivo = b"teste"
        files = {"arquivo": ("nota.pdf", io.BytesIO(arquivo), "application/pdf")}

        r = await client.post(
            "/ia/processar-nota",
            headers=auth_headers(token_admin),
            files=files
        )
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        for _ in range(10):
            r2 = await client.get(
                f"/ia/processar-nota/jobs/{job_id}",
                headers=auth_headers(token_admin)
            )
            if r2.json()["status"] in ("concluido", "erro"):
                break
            await asyncio.sleep(1)

        assert r2.status_code == 200
        data = r2.json()
        assert data["status"] in ("concluido", "erro")