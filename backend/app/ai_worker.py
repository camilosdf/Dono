# backend/app/ai_worker.py — Sistema Dono
#
# Worker dedicado para processar jobs de IA (OCR, RAG, embeddings).
# Consome jobs da tabela ia_jobs e os processa assincronamente.
#
# ATUALIZAÇÃO (Fase 7): Módulo novo para suportar IA/RAG/OCR.
#
# Responsabilidades:
#   1. Processar jobs de OCR (extração de dados de notas fiscais).
#   2. Gerar embeddings para documentos recém-criados (RAG).
#   3. (Futuro) Processar jobs de RAG assíncronos.
#
# O worker roda em loop contínuo, com polling na tabela ia_jobs.
# Usa FOR UPDATE SKIP LOCKED para evitar concorrência entre réplicas.
#
# CORREÇÕES (2026-07-24):
#   - Corrigido processar_documentos_sem_embedding: agora converte o embedding
#     para string usando embedding_to_string() antes de passar ao PostgreSQL.
#   - Adicionada importação da função embedding_to_string do módulo rag.
#   - Melhorado o log de erros para facilitar diagnóstico.

import os
import asyncio
import uuid
import logging
from datetime import datetime

import asyncpg

from app.database import get_pool
from app.rag import processar_job_ocr, gerar_embedding, embedding_to_string
from app.redis_client import get_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("dono-ai-worker")

# Intervalo entre verificações de novos jobs (segundos)
INTERVALO_POLLING = int(os.getenv("AI_WORKER_POLLING_INTERVAL", 5))


async def processar_proximo_job(db_pool: asyncpg.Pool) -> bool:
    """Busca o próximo job pendente (OCR, RAG, etc.) e processa.

    Usa FOR UPDATE SKIP LOCKED para garantir que cada job seja processado
    por apenas uma réplica do worker.

    Returns:
        True se um job foi processado, False se não havia jobs pendentes.
    """
    async with db_pool.acquire() as conn:
        # Busca o primeiro job pendente com lock
        job = await conn.fetchrow(
            """SELECT id, tipo, solicitado_por, entrada
               FROM ia_jobs
               WHERE status = 'pendente'
                 AND tipo IN ('OCR_NOTA', 'RAG_CONSULTA')
               ORDER BY criado_em
               LIMIT 1
               FOR UPDATE SKIP LOCKED"""
        )

        if not job:
            return False

        job_id = job["id"]
        tipo = job["tipo"]
        usuario_id = job["solicitado_por"]

        try:
            # Injeta contexto de auditoria com o usuário que solicitou o job
            await conn.execute(
                "SELECT fn_set_audit_context($1::uuid, $2, $3)",
                usuario_id, "worker://dono-ai-worker", "dono-ai-worker"
            )
            # Marca como processando
            await conn.execute(
                "UPDATE ia_jobs SET status = 'processando' WHERE id = $1",
                job_id
            )
            logger.info("Processando job %s (tipo: %s)", job_id, tipo)

            # Processa conforme o tipo
            if tipo == "OCR_NOTA":
                # Recupera o arquivo do Redis (armazenado pela rota)
                redis = get_redis()
                arquivo_bytes = await redis.get(f"ocr_job:{job_id}:arquivo")
                if not arquivo_bytes:
                    raise ValueError("Arquivo não encontrado no Redis (expirado ou não existe)")

                # Processa o OCR (função em app.rag que chama app.ocr)
                resultado = await processar_job_ocr(job_id, arquivo_bytes, usuario_id)

            elif tipo == "RAG_CONSULTA":
                # (Futuro) Processamento de consulta RAG assíncrono
                # Por enquanto, apenas marca como concluído com placeholder
                resultado = {
                    "status": "concluido",
                    "mensagem": "Processamento RAG assíncrono ainda não implementado"
                }

            else:
                raise ValueError(f"Tipo de job não suportado: {tipo}")

            # Atualiza job como concluído
            await conn.execute(
                """UPDATE ia_jobs
                   SET status = 'concluido',
                       resultado = $2,
                       concluido_em = now()
                   WHERE id = $1""",
                job_id,
                resultado
            )
            logger.info("Job %s concluído com sucesso", job_id)

        except Exception as e:
            logger.exception("Erro ao processar job %s", job_id)
            await conn.execute(
                """UPDATE ia_jobs
                   SET status = 'erro',
                       erro_motivo = $2,
                       concluido_em = now()
                   WHERE id = $1""",
                job_id,
                str(e)
            )

        return True


async def processar_documentos_sem_embedding(db_pool: asyncpg.Pool) -> None:
    """Processa documentos que não têm embedding, gerando-os.

    Esta função é executada nos momentos em que não há jobs pendentes,
    para garantir que todos os documentos estejam indexados para RAG.

    Processa até 10 documentos por vez para não sobrecarregar o sistema.
    
    CORREÇÃO: Agora converte o embedding para string antes de passar ao SQL,
    utilizando a função embedding_to_string() do módulo rag.
    """
    async with db_pool.acquire() as conn:
        # Busca documentos sem embedding (limitado a 10)
        docs = await conn.fetch(
            """SELECT id, conteudo FROM documentos
               WHERE embedding IS NULL
               LIMIT 10"""
        )

        if not docs:
            return

        logger.info("Gerando embeddings para %d documentos...", len(docs))

        for doc in docs:
            try:
                # Gera embedding do conteúdo
                embedding = await gerar_embedding(doc["conteudo"])
                # Converte para string no formato PostgreSQL
                embedding_str = embedding_to_string(embedding)

                # Atualiza o documento
                await conn.execute(
                    """UPDATE documentos
                       SET embedding = $1::vector,
                           atualizado_em = now()
                       WHERE id = $2""",
                    embedding_str,
                    doc["id"]
                )
                logger.info("Embedding gerado para documento %s", doc["id"])

            except Exception as e:
                logger.error("Erro ao gerar embedding para documento %s: %s", doc["id"], str(e))

        # Pequena pausa para não sobrecarregar o banco com muitas atualizações
        await asyncio.sleep(0.1)


async def main():
    """Loop principal do worker de IA."""
    database_url = os.environ["DATABASE_URL"]
    db_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

    logger.info(
        "Worker de IA iniciado (polling a cada %s segundos)",
        INTERVALO_POLLING
    )

    try:
        while True:
            # 1. Processa um job pendente (OCR, RAG, etc.)
            processou = await processar_proximo_job(db_pool)

            # 2. Se não havia job, tenta gerar embeddings para documentos
            if not processou:
                await processar_documentos_sem_embedding(db_pool)

            # 3. Aguarda o próximo ciclo
            await asyncio.sleep(INTERVALO_POLLING)

    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker de IA interrompido.")
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())