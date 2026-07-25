# backend/app/routes/auth.py — Sistema Dono
import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr

from app.auth import (
    RefreshTokenReusedError,
    create_access_token,
    generate_refresh_token,
    revoke_all_tokens_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
    store_refresh_token,
    verify_password,
)
from app.database import get_pool
from app.dependencies import get_current_user
from app.errors import error_detail
from app.rate_limit import check_login_rate_limit

router = APIRouter()


class LoginRequest(BaseModel):
    email: EmailStr
    senha: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900
    perfil: str


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    await check_login_rate_limit(request, body.email)
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, perfil, senha_hash FROM usuarios WHERE email = $1 AND ativo = TRUE",
            body.email,
        )
        # Mensagem idêntica para "usuário não existe" e "senha errada" —
        # de propósito, para não dar pista de qual das duas aconteceu.
        if not user or not verify_password(body.senha, user["senha_hash"]):
            raise HTTPException(
                status_code=401,
                detail=error_detail("CREDENCIAIS_INVALIDAS", "Email ou senha inválidos"),
            )

        access_token = create_access_token(str(user["id"]), user["perfil"])
        refresh_token, refresh_hash = generate_refresh_token()
        familia_id = str(uuid.uuid4())
        await store_refresh_token(conn, str(user["id"]), refresh_hash, familia_id)

        return TokenResponse(access_token=access_token, refresh_token=refresh_token, perfil=user["perfil"])


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    old_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    new_refresh_token, new_hash = generate_refresh_token()

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            result = await rotate_refresh_token(conn, old_hash, new_hash)
        except RefreshTokenReusedError:
            raise HTTPException(
                status_code=401,
                detail=error_detail(
                    "REFRESH_TOKEN_REUTILIZADO",
                    "Refresh token reutilizado — sessão comprometida, faça login novamente",
                ),
            )

        if result is None:
            raise HTTPException(
                status_code=401,
                detail=error_detail("REFRESH_TOKEN_INVALIDO_OU_EXPIRADO", "Refresh token inválido ou expirado"),
            )

        user = await conn.fetchrow(
            "SELECT perfil FROM usuarios WHERE id = $1 AND ativo = TRUE",
            uuid.UUID(result["usuario_id"]),
        )
        if not user:
            raise HTTPException(
                status_code=401,
                detail=error_detail("NAO_AUTENTICADO", "Usuário inativo ou inexistente"),
            )

        access_token = create_access_token(result["usuario_id"], user["perfil"])
        return TokenResponse(access_token=access_token, refresh_token=new_refresh_token, perfil=user["perfil"])


@router.post("/logout")
async def logout(body: RefreshRequest):
    token_hash = hashlib.sha256(body.refresh_token.encode()).hexdigest()
    pool = get_pool()
    async with pool.acquire() as conn:
        revoked = await revoke_refresh_token(conn, token_hash)
    if not revoked:
        raise HTTPException(
            status_code=404,
            detail=error_detail("RECURSO_NAO_ENCONTRADO", "Token não encontrado ou já revogado"),
        )
    return {"message": "Logout realizado com sucesso"}


@router.post("/logout-all")
async def logout_all(current_user: dict = Depends(get_current_user)):
    pool = get_pool()
    async with pool.acquire() as conn:
        await revoke_all_tokens_for_user(conn, current_user["user_id"])
    return {"message": "Todos os dispositivos foram desconectados"}
