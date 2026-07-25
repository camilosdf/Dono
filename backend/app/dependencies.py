# backend/app/dependencies.py — Sistema Dono
#
# Dependências comuns para autenticação, autorização (RBAC) e auditoria.
# Inclui funções para:
#   - Extrair e validar o token JWT (get_current_user)
#   - Verificar permissões por perfil (require_perfil)
#   - Definir o contexto de auditoria (set_audit_context) para rastrear
#     quem fez o quê, de qual IP e dispositivo, em cada requisição que
#     modifica dados.
#
# ATUALIZAÇÃO (Rastreabilidade Total):
#   - Adicionada a função set_audit_context, que extrai o usuário, IP e
#     user-agent da requisição e chama fn_set_audit_context no banco,
#     definindo as variáveis de sessão que serão lidas pelos triggers
#     e funções PL/pgSQL para preencher colunas de auditoria.
#
# ATUALIZAÇÃO (Tabela de Perdas e Ajustes):
#   - Nenhuma alteração necessária: as permissões para registrar perdas
#     são aplicadas diretamente nas rotas (require_perfil("COMPRAS", "ADMIN")).
#   - O middleware de auditoria já fornece o contexto necessário para
#     que a função fn_registrar_perda preencha as colunas de auditoria.
#
# A dependência pode ser usada tanto por middlewares quanto por rotas
# individuais. Para garantir que toda operação de escrita tenha o contexto
# definido, recomendamos usá-la via middleware (ver app/middleware.py).

import os
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from jose import JWTError, jwt

from app.auth import ALGORITHM
from app.database import get_pool
from app.errors import error_detail


def _decode_token(token: str) -> dict:
    """Decodifica o token JWT e valida emissor e algoritmo.
    Levanta HTTPException 401 em caso de erro."""
    try:
        return jwt.decode(token, os.environ["JWT_SECRET"], algorithms=[ALGORITHM], issuer="Dono")
    except JWTError:
        raise HTTPException(
            status_code=401,
            detail=error_detail("NAO_AUTENTICADO", "Token ausente, malformado ou expirado"),
        )


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    """Dependência que extrai o usuário atual do token JWT.
    Retorna um dicionário com 'user_id' (str) e 'perfil' (str).
    Pode ser usada em rotas que precisam do usuário logado."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail=error_detail("NAO_AUTENTICADO", "Header Authorization: Bearer <token> ausente"),
        )
    payload = _decode_token(authorization.removeprefix("Bearer ").strip())
    return {"user_id": payload["sub"], "perfil": payload["perfil"]}


def require_perfil(*perfis_permitidos: str):
    """Fábrica de dependência para verificar permissões RBAC.
    Uso: Depends(require_perfil("ADMIN", "COMPRAS"))
    Levanta HTTPException 403 se o perfil do usuário não estiver na lista."""
    async def checker(current_user: dict = Depends(get_current_user)) -> dict:
        if current_user["perfil"] not in perfis_permitidos:
            raise HTTPException(
                status_code=403,
                detail=error_detail(
                    "PERMISSAO_NEGADA",
                    "Perfil não tem permissão para esta rota",
                    {"perfil_requerido": list(perfis_permitidos), "perfil_atual": current_user["perfil"]},
                ),
            )
        return current_user
    return checker


async def set_audit_context(request: Request, current_user: dict = Depends(get_current_user)) -> None:
    """Dependência que define o contexto de auditoria para a requisição atual.

    Extrai o usuario_id do token, o IP do cliente e o User-Agent,
    e executa a função PL/pgSQL fn_set_audit_context() no banco,
    que define as variáveis de sessão 'app.usuario_id', 'app.ip_origem'
    e 'app.user_agent' na conexão atual.

    Essa função deve ser usada **antes** de qualquer operação que possa
    gerar eventos de domínio ou movimentações de estoque, para que os
    triggers e funções (ex.: fn_atualizar_custo_medio_insumo,
    fn_executar_refeicao, fn_registrar_perda) possam preencher as colunas
    de auditoria (usuario_id, ip_origem, user_agent).

    O ideal é registrar essa dependência em todas as rotas que modificam
    dados, ou, melhor ainda, criar um middleware que a invoque para
    todas as requisições autenticadas (ver app/middleware.py).

    Esta função NÃO levanta exceções de autenticação; assume que o
    current_user já foi validado. Se o usuário não existir (caso raro),
    define o contexto com NULL, permitindo que a operação prossiga
    (mas sem rastreabilidade).
    """
    # Extrai dados da requisição
    usuario_id = current_user.get("user_id") if current_user else None
    ip_origem = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT fn_set_audit_context($1, $2, $3)",
            usuario_id, ip_origem, user_agent,
        )