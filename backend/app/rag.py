# backend/app/rag.py — Sistema Dono
#
# Módulo RAG (Retrieval-Augmented Generation) para IA local.
# Responsabilidades:
#   - Gerar embeddings de documentos usando sentence-transformers.
#   - Buscar documentos similares por similaridade de cosseno (via pgvector).
#   - Consultar o LLM local (Ollama) com o contexto recuperado.
#   - Gerenciar jobs de OCR (enfileiramento e processamento).
#
# ATUALIZAÇÃO (Fase 7): Módulo novo para suportar IA/RAG/OCR.
# As funções de OCR (extrair_dados_nota, salvar_nota_processada) estão no módulo app.ocr.py.
# Este módulo foca exclusivamente em RAG (embeddings, busca e consulta LLM).
#
# CORREÇÕES (2026-07-24):
#   - Adicionada função embedding_to_string para converter lista de floats em string
#     no formato esperado pelo PostgreSQL (ex.: '[0.1,0.2,...]').
#   - Corrigida função buscar_documentos_similares: agora usa CAST($1 AS vector)
#     para garantir a conversão correta de text para vector no PostgreSQL.
#   - Corrigida função enfileirar_job_ocr para usar apenas um parâmetro na query.
#   - Adicionado tratamento de erro mais robusto e logs.

import os
import uuid
import asyncio
import logging
from typing import List, Dict, Any, Optional

import asyncpg
import httpx
#from sentence_transformers import SentenceTransformer

from app.database import get_pool
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

# Configurações (definidas no .env)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
EMBEDDING_MODEL_NAME = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

# Cache do modelo de embedding (carregado uma única vez)
_embedding_model = None


def get_embedding_model():
    """Lazy loading do modelo de embedding (sentence-transformers)."""
    global _embedding_model

    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer

        logger.info(
            "Carregando modelo de embedding: %s",
            EMBEDDING_MODEL_NAME,
        )

        _embedding_model = SentenceTransformer(
            EMBEDDING_MODEL_NAME
        )

    return _embedding_model


async def gerar_embedding(texto: str) -> List[float]:
    """Gera o embedding de um texto usando o modelo configurado.
    Executa em thread separada para não bloquear o event loop.
    
    Args:
        texto: Texto a ser transformado em embedding.
    
    Returns:
        Lista de floats representando o vetor de embedding.
    """
    loop = asyncio.get_event_loop()
    model = get_embedding_model()
    # O método encode pode ser pesado; executamos em thread pool
    embedding = await loop.run_in_executor(None, model.encode, texto)
    return embedding.tolist()


def embedding_to_string(embedding: List[float]) -> str:
    """Converte uma lista de floats em uma string formatada para PostgreSQL vector.
    O formato esperado é: '[0.1, 0.2, 0.3, ...]'
    
    Args:
        embedding: Lista de floats (ex.: [-0.054, 0.072, ...])
    
    Returns:
        String no formato '[0.1,0.2,...]' pronta para uso em SQL.
    """
    return '[' + ','.join(str(x) for x in embedding) + ']'


async def buscar_documentos_similares(
    pergunta: str,
    top_k: int = 5,
    tipo: Optional[str] = None,
    entidade_id: Optional[uuid.UUID] = None
) -> List[Dict[str, Any]]:
    """Busca os documentos mais similares à pergunta usando pgvector.
    
    Args:
        pergunta: Texto da pergunta (usado para gerar embedding).
        top_k: Número máximo de documentos a retornar.
        tipo: Filtrar por tipo de documento (FICHA_TECNICA, POP, LEGISLACAO, etc.)
        entidade_id: Filtrar por entidade relacionada (ex.: prato_id, insumo_id).
    
    Returns:
        Lista de dicts com id, titulo, conteudo, similaridade.
    """
    # 1. Gera o embedding da pergunta
    embedding = await gerar_embedding(pergunta)
    # 2. Converte para string no formato PostgreSQL
    embedding_str = embedding_to_string(embedding)

    # 3. Chama a função PL/pgSQL para busca por similaridade.
    #    Usamos CAST($1 AS vector) para garantir a conversão correta.
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM fn_buscar_documentos_similares(CAST($1 AS vector), $2, $3, $4)",
            embedding_str,
            top_k,
            tipo,
            entidade_id
        )
        return [
            {
                "id": str(r["id"]),
                "titulo": r["titulo"],
                "conteudo": r["conteudo"],
                "similaridade": float(r["similaridade"])
            }
            for r in rows
        ]


async def consultar_llm(
    pergunta: str,
    contexto: str,
    modelo: Optional[str] = None
) -> str:
    """Envia uma pergunta + contexto para o LLM local (Ollama) e retorna a resposta.
    
    Args:
        pergunta: Pergunta do usuário.
        contexto: Texto dos documentos recuperados (contexto para a resposta).
        modelo: Nome do modelo (padrão: OLLAMA_MODEL do .env).
    
    Returns:
        Resposta gerada pelo LLM.
    
    Raises:
        httpx.HTTPError: Se a chamada ao Ollama falhar.
    """
    modelo = modelo or OLLAMA_MODEL
    
    # Monta o prompt com instruções claras para o modelo
    prompt = f"""Você é um assistente especializado em gastronomia e gestão de restaurantes.
Responda à pergunta com base APENAS no contexto fornecido.
Se a resposta não estiver no contexto, diga que não encontrou a informação.

Contexto:
{contexto}

Pergunta: {pergunta}

Resposta:"""

    import os
    _timeout = float(os.getenv("OLLAMA_TIMEOUT", "120.0"))
    async with httpx.AsyncClient(timeout=_timeout) as client:
        response = await client.post(
            f"{OLLAMA_HOST}/api/generate",
            json={
                "model": modelo,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,   # Baixa temperatura para respostas mais precisas
                    "top_p": 0.9
                }
            }
        )
        response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()


# =====================================================================
# Funções para enfileiramento de jobs de IA (OCR e RAG)
# =====================================================================

async def enfileirar_job_ocr(arquivo_bytes: bytes, usuario_id: uuid.UUID) -> uuid.UUID:
    """Cria um job de OCR na tabela ia_jobs e armazena o arquivo no Redis.
    
    Args:
        arquivo_bytes: Conteúdo do arquivo (PDF ou imagem).
        usuario_id: ID do usuário que solicitou o processamento.
    
    Returns:
        job_id: UUID do job criado.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """INSERT INTO ia_jobs (tipo, solicitado_por, entrada)
               VALUES ('OCR_NOTA', $1, jsonb_build_object('timestamp', now()))
               RETURNING id""",
            usuario_id
        )
        job_id = job["id"]

    # Armazena o arquivo no Redis com TTL de 1 hora (tempo suficiente para processamento)
    redis = get_redis()
    await redis.setex(
        f"ocr_job:{job_id}:arquivo",
        3600,
        arquivo_bytes
    )

    return job_id


async def enfileirar_job_rag(pergunta: str, usuario_id: uuid.UUID, top_k: int = 5) -> uuid.UUID:
    """Cria um job de RAG para processamento assíncrono (opcional).
    Atualmente a consulta RAG é síncrona, mas esta função serve como preparação
    para futura implementação assíncrona.
    
    Args:
        pergunta: Pergunta do usuário.
        usuario_id: ID do usuário que solicitou.
        top_k: Número de documentos a recuperar.
    
    Returns:
        job_id: UUID do job criado.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            """INSERT INTO ia_jobs (tipo, solicitado_por, entrada)
               VALUES ('RAG_CONSULTA', $1, jsonb_build_object(
                   'pergunta', $2,
                   'top_k', $3,
                   'timestamp', now()
               ))
               RETURNING id""",
            usuario_id,
            pergunta,
            top_k
        )
        return job["id"]


# =====================================================================
# Função para processar jobs (chamada pelo ai_worker)
# =====================================================================

async def processar_job_ocr(job_id: uuid.UUID, arquivo_bytes: bytes, usuario_id: uuid.UUID) -> Dict[str, Any]:
    """Processa um job de OCR: extrai dados da nota fiscal e cria lote/conta a pagar.
    
    Esta função é chamada pelo ai_worker, que recebe o arquivo e o usuário.
    A lógica de extração e salvamento está em app.ocr.py para separação de responsabilidades.
    
    Args:
        job_id: ID do job (para atualizar status).
        arquivo_bytes: Conteúdo do arquivo (PDF ou imagem).
        usuario_id: ID do usuário que solicitou.
    
    Returns:
        Resultado do processamento (dados extraídos, itens criados, etc.)
    """
    from app.ocr import extrair_dados_nota, salvar_nota_processada

    try:
        # 1. Extrai dados da nota fiscal via OCR
        dados = await extrair_dados_nota(arquivo_bytes)

        # 2. Salva os dados processados (cria lote de insumo e conta a pagar)
        resultado = await salvar_nota_processada(dados, usuario_id)

        return {
            "status": "concluido",
            "dados": dados,
            "resultado": resultado
        }
    except ValueError as e:
        # Erro esperado: dados não extraídos (ex.: sem itens identificados)
        logger.warning("Falha na extração de dados da nota (job %s): %s", job_id, str(e))
        return {
            "status": "erro",
            "erro_motivo": str(e)
        }
    except Exception as e:
        logger.exception("Erro ao processar OCR job %s", job_id)
        return {
            "status": "erro",
            "erro_motivo": str(e)
        }


# =====================================================================
# Função para atualizar embedding de documento (chamada pelo ai_worker)
# =====================================================================

async def atualizar_embedding_documento(doc_id: uuid.UUID, embedding: List[float]) -> None:
    """Atualiza o embedding de um documento existente.
    
    Args:
        doc_id: ID do documento.
        embedding: Vetor de embedding (lista de floats).
    """
    pool = get_pool()
    embedding_str = embedding_to_string(embedding)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE documentos SET embedding = $1, atualizado_em = now() WHERE id = $2",
            embedding_str,
            doc_id
        )
        # Verifica se o documento foi atualizado
        if result == "UPDATE 0":
            raise ValueError(f"Documento {doc_id} não encontrado")