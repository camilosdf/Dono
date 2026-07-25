# backend/app/middleware.py — Sistema Dono
#
# Middlewares globais da aplicação:
#   1. RateLimitMiddleware — aplica o limite geral de 120 req/min por usuário.
#   2. AuditContextMiddleware — define o contexto de auditoria (usuário, IP,
#      user-agent) para cada requisição autenticada, permitindo rastreabilidade
#      em todas as operações de escrita (movimentações de estoque, eventos, etc.).
#
# A ordem de execução é importante: o RateLimitMiddleware deve vir primeiro,
# pois não depende do contexto de auditoria, e o AuditContextMiddleware deve
# ser executado antes de qualquer rota que acesse o banco.
#
# ATUALIZAÇÃO (Rastreabilidade Total):
#   - Adicionado AuditContextMiddleware para definir variáveis de sessão
#     no PostgreSQL via fn_set_audit_context (ver dependencies.py).
#   - O contexto é definido mesmo para requisições não autenticadas
#     (ex.: /auth/login), com valores NULL, garantindo que todas as
#     conexões tenham as variáveis definidas.
#
# ATUALIZAÇÃO (Correção de UUID vazio):
#   - Ao extrair o usuário do token, agora garantimos que o valor seja
#     um UUID válido ou None, nunca uma string vazia. Isso evita o erro
#     "invalid input syntax for type uuid: ''" nas funções PL/pgSQL que
#     convertem current_setting('app.usuario_id') para UUID.
#   - A função fn_set_audit_context recebe o UUID diretamente, e as
#     funções do banco usam NULLIF para tratar valores vazios.

import os
import uuid
from typing import Optional

from fastapi import HTTPException
from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth import ALGORITHM
from app.database import get_pool
from app.errors import error_detail
from app.rate_limit import check_general_rate_limit


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Aplica o limite geral (§12: 120 req/min por usuário) a qualquer
    requisição com um Bearer token decodificável. Não substitui a
    autenticação de verdade (get_current_user) — só conta requisições;
    token ausente ou inválido passa direto e deixa o 401 de cada rota
    tratar normalmente. Faz a checagem via exceção capturada aqui, não
    deixada propagar pelo BaseHTTPMiddleware — levantar HTTPException
    dentro de dispatch() nem sempre chega ao exception handler global do
    FastAPI, dependendo da versão do Starlette; converter direto pra
    JSONResponse evita depender disso."""

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ").strip()
            try:
                payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[ALGORITHM], issuer="Dono")
            except JWTError:
                payload = None

            usuario_id = payload.get("sub") if payload else None
            if usuario_id:
                try:
                    await check_general_rate_limit(usuario_id)
                except HTTPException as exc:
                    return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)

        return await call_next(request)


class AuditContextMiddleware(BaseHTTPMiddleware):
    """Define o contexto de auditoria (usuário, IP, user-agent) para cada
    requisição, chamando fn_set_audit_context no banco. O contexto é usado
    pelos triggers e funções PL/pgSQL para preencher colunas de auditoria
    (usuario_id, ip_origem, user_agent) em movimentacoes_estoque e eventos_dominio.

    Características:
      - Tenta extrair o usuário do token JWT (se presente e válido).
      - Se o token for inválido ou ausente, define o contexto com NULL
        (não bloqueia a requisição).
      - Define o contexto para TODAS as requisições, inclusive as não
        autenticadas (ex.: /auth/login), garantindo que todas as conexões
        tenham as variáveis de sessão definidas.
      - Executa APÓS o RateLimitMiddleware, pois o rate limit não depende
        do contexto.
      - Executa ANTES do roteamento, para que todas as rotas encontrem
        o contexto já definido ao acessar o banco.

    CORREÇÃO: Agora garantimos que o usuario_id seja um UUID válido ou None,
    nunca uma string vazia. Isso evita o erro "invalid input syntax for type uuid: ''"
    nas funções PL/pgSQL que convertem current_setting('app.usuario_id') para UUID.
    """

    async def dispatch(self, request: Request, call_next):
        # 1. Extrai o usuário do token (se possível)
        usuario_id = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ").strip()
            try:
                payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[ALGORITHM], issuer="Dono")
                sub = payload.get("sub")
                if sub:
                    # Tenta converter para UUID; se falhar, mantém None
                    try:
                        usuario_id = uuid.UUID(sub)
                    except (ValueError, TypeError, AttributeError):
                        # sub não é um UUID válido; ignora
                        usuario_id = None
            except JWTError:
                # Token inválido ou expirado: não bloqueia, apenas não define usuário
                pass

        # 2. Extrai IP e User-Agent
        ip_origem = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        # 3. Define o contexto no banco (usando a pool de conexões)
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "SELECT fn_set_audit_context($1, $2, $3)",
                usuario_id,  # pode ser UUID ou None (nunca string vazia)
                ip_origem,
                user_agent,
            )

        # 4. Segue para o próximo middleware/rota
        return await call_next(request)