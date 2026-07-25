# backend/app/main.py — Sistema Dono
#
# Ponto de entrada da aplicação FastAPI. Configura:
#   - Lifespan (conexão com banco e Redis)
#   - Middlewares (Rate Limit e Auditoria)
#   - Handlers de exceção globais
#   - Registro de todas as rotas (autenticação, usuários, catálogos, insumos,
#     pratos, refeições, menus, relatórios, IA, movimentações, previsões, financeiro)
#   - Logs estruturados (JSON) para produção
#   - Métricas Prometheus
#
# ATUALIZAÇÃO (2026-07-25):
#   - Adicionado suporte a logs estruturados via variável LOG_JSON.
#   - Adicionado endpoint /metrics para coleta de métricas pelo Prometheus.
#   - Configuração de logging com python-json-logger.
#   - Registro do router de métricas (app.metrics).

import os
import logging
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

# Importa os módulos internos
from app import redis_client
from app.database import connect, disconnect, get_pool
from app.errors import error_detail
from app.middleware import AuditContextMiddleware, RateLimitMiddleware
from app.routes import (
    auth,
    catalogos,
    ia,
    insumos,
    menus,
    movimentacoes,
    pratos,
    previsoes,
    refeicoes,
    relatorios,
    usuarios,
    financeiro,        # Módulo financeiro (contas a pagar/receber)
)
#from app.metrics import router as metrics_router  # Métricas Prometheus

# =====================================================================
# Configuração de logs estruturados (JSON)
# =====================================================================
LOG_JSON = os.getenv("LOG_JSON", "false").lower() == "true"

def setup_logging() -> None:
    """Configura o logging da aplicação.
    - Se LOG_JSON=true, usa formato JSON (para coleta em produção).
    - Caso contrário, usa formato texto legível (desenvolvimento).
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Remove handlers existentes para evitar duplicação
    if logger.hasHandlers():
        logger.handlers.clear()

    handler = logging.StreamHandler()
    if LOG_JSON:
        # Usa python-json-logger para formatar logs em JSON
        try:
            from pythonjsonlogger import jsonlogger
            formatter = jsonlogger.JsonFormatter(
                '%(asctime)s %(levelname)s %(name)s %(message)s',
                timestamp=True,
                rename_fields={
                    'asctime': 'timestamp',
                    'levelname': 'level',
                    'name': 'logger'
                }
            )
        except ImportError:
            # Fallback caso python-json-logger não esteja instalado
            formatter = logging.Formatter(
                '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
            )
            logger.warning("python-json-logger não instalado; usando formato texto")
    else:
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        )

    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Configura nível de logs para bibliotecas externas (evitar poluição)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)

setup_logging()
logger = logging.getLogger(__name__)

# =====================================================================
# Lifespan (ciclo de vida da aplicação)
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia o ciclo de vida da aplicação:
    - Inicializa conexões com banco e Redis no startup.
    - Fecha conexões no shutdown.
    """
    logger.info("Iniciando aplicação...")
    await connect()
    await redis_client.connect()
    logger.info("Conexões estabelecidas (banco e Redis).")
    yield
    logger.info("Encerrando aplicação...")
    await redis_client.disconnect()
    await disconnect()
    logger.info("Conexões encerradas.")

# =====================================================================
# Aplicação FastAPI
# =====================================================================
app = FastAPI(
    title="API Dono",
    description="Sistema de gestão gastronômica com controle de estoque, produção, IA e previsões.",
    version="2.0.0",
    lifespan=lifespan,
)
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=[
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
    ],
).instrument(app).expose(app)
# =====================================================================
# Middlewares (ordem de execução: do primeiro adicionado ao último)
# =====================================================================
# 1. RateLimitMiddleware: aplica limite de 120 req/min por usuário.
#    Deve ser o mais externo para contar todas as requisições, inclusive
#    as que podem ser bloqueadas antes de definir contexto de auditoria.
app.add_middleware(RateLimitMiddleware)

# 2. AuditContextMiddleware: define o contexto de auditoria (usuário, IP,
#    user-agent) via fn_set_audit_context no banco. Deve ser executado
#    antes de qualquer rota que acesse o banco, para que os triggers e
#    funções PL/pgSQL preencham as colunas de auditoria.
#    A ordem após RateLimitMiddleware garante que o rate limit não dependa
#    do contexto e que o contexto esteja disponível para as rotas.
app.add_middleware(AuditContextMiddleware)

# =====================================================================
# Handlers de exceção globais
# =====================================================================
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    """Converte qualquer ValueError não tratado (ex.: UUID malformado,
    conversão de tipo inválida) em 400 VALIDACAO_INVALIDA, em vez de
    vazar um 500 Internal Server Error."""
    logger.warning("ValueError capturado: %s", str(exc))
    return JSONResponse(
        status_code=400,
        content=error_detail("VALIDACAO_INVALIDA", str(exc)),
    )

# =====================================================================
# Rotas
# =====================================================================
# Autenticação e usuários
app.include_router(auth.router, prefix="/auth", tags=["autenticacao"])
app.include_router(usuarios.router_me, tags=["usuarios"])
app.include_router(usuarios.router, prefix="/usuarios", tags=["usuarios"])

# Catálogos e insumos
app.include_router(catalogos.router, tags=["catalogos"])
app.include_router(insumos.router, tags=["insumos"])

# Pratos, refeições, menus
app.include_router(pratos.router, tags=["pratos"])
app.include_router(refeicoes.router, tags=["refeicoes"])
app.include_router(menus.router, tags=["menus"])

# Relatórios e previsões
app.include_router(relatorios.router, tags=["relatorios"])
app.include_router(previsoes.router, tags=["previsoes"])

# IA (inclui RAG, OCR, prospecção)
app.include_router(ia.router, tags=["ia"])

# Movimentações de estoque (perdas e ajustes)
app.include_router(movimentacoes.router, tags=["movimentacoes"])

# Módulo financeiro (contas a pagar/receber)
app.include_router(financeiro.router, tags=["financeiro"])

# Métricas Prometheus
#app.include_router(metrics_router, tags=["metrics"])

# =====================================================================
# Health check
# =====================================================================
@app.get("/health")
async def health():
    """Verifica se a aplicação está funcionando e consegue se conectar ao banco."""
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok", "timestamp": datetime.now().isoformat()}
    except Exception as e:
        logger.error("Health check falhou: %s", str(e))
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error": str(e)}
        )

# =====================================================================
# Fim do arquivo
# =====================================================================