# backend/app/routes/usuarios.py — Sistema Dono
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from app.auth import hash_password, revoke_all_tokens_for_user
from app.database import get_pool
from app.dependencies import get_current_user, require_perfil
from app.errors import error_detail

# Sem prefixo — registrado em main.py como app.include_router(router_me),
# porque GET /me fica na raiz, não em /usuarios/me (ver api-endpoints.md §1).
router_me = APIRouter()

# Com prefixo /usuarios — registrado como
# app.include_router(router, prefix="/usuarios").
router = APIRouter()


class UsuarioOut(BaseModel):
    id: str
    nome: str
    email: str
    perfil: str
    ativo: bool


class CriarUsuarioRequest(BaseModel):
    nome: str
    email: EmailStr
    senha: str
    perfil: str  # validado contra o CHECK do banco na hora do INSERT


class AtualizarUsuarioRequest(BaseModel):
    perfil: str | None = None
    ativo: bool | None = None


@router_me.get("/me", response_model=UsuarioOut)
async def me(current_user: dict = Depends(get_current_user)):
    # Busca fresca no banco (não só o que veio no JWT) — se o perfil do
    # usuário mudou depois que o access_token foi emitido, /me reflete o
    # estado atual, não o que ficou congelado no token por até 15 min.
    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, nome, email, perfil, ativo FROM usuarios WHERE id = $1",
            uuid.UUID(current_user["user_id"]),
        )
        if not user:
            raise HTTPException(
                status_code=401,
                detail=error_detail("NAO_AUTENTICADO", "Usuário do token não existe mais"),
            )
        return UsuarioOut(id=str(user["id"]), nome=user["nome"], email=user["email"],
                           perfil=user["perfil"], ativo=user["ativo"])


@router.get("", response_model=list[UsuarioOut])
async def listar_usuarios(current_user: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, nome, email, perfil, ativo FROM usuarios ORDER BY nome")
        return [UsuarioOut(id=str(r["id"]), nome=r["nome"], email=r["email"],
                            perfil=r["perfil"], ativo=r["ativo"]) for r in rows]


@router.post("", response_model=UsuarioOut, status_code=201)
async def criar_usuario(body: CriarUsuarioRequest, current_user: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        existente = await conn.fetchval("SELECT 1 FROM usuarios WHERE email = $1", body.email)
        if existente:
            raise HTTPException(
                status_code=409,
                detail=error_detail("EMAIL_JA_CADASTRADO", "Já existe um usuário com este email"),
            )
        try:
            row = await conn.fetchrow(
                """INSERT INTO usuarios (nome, email, senha_hash, perfil)
                   VALUES ($1, $2, $3, $4)
                   RETURNING id, nome, email, perfil, ativo""",
                body.nome, body.email, hash_password(body.senha), body.perfil,
            )
        except Exception:
            # cobre a CHECK constraint de perfil (perfil fora de
            # CHEF/COMPRAS/ADMIN/GESTAO) sem duplicar a lista aqui —
            # o banco já é a fonte da verdade para os valores válidos
            raise HTTPException(
                status_code=400,
                detail=error_detail("VALIDACAO_INVALIDA", "Perfil inválido ou dados malformados"),
            )
        return UsuarioOut(id=str(row["id"]), nome=row["nome"], email=row["email"],
                           perfil=row["perfil"], ativo=row["ativo"])


@router.patch("/{usuario_id}", response_model=UsuarioOut)
async def atualizar_usuario(usuario_id: str, body: AtualizarUsuarioRequest,
                             current_user: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE usuarios SET
                 perfil = COALESCE($2, perfil),
                 ativo = COALESCE($3, ativo)
               WHERE id = $1
               RETURNING id, nome, email, perfil, ativo""",
            uuid.UUID(usuario_id), body.perfil, body.ativo,
        )
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Usuário não encontrado"))
        if body.ativo is False:
            # Não invalida o access_token já emitido (JWT autocontido, vale
            # até expirar — no máximo 15 min), mas impede qualquer /refresh
            # futuro dessa sessão. Fecha a maior parte da janela sem
            # precisar de uma blacklist de access tokens.
            await revoke_all_tokens_for_user(conn, str(row["id"]))
        return UsuarioOut(id=str(row["id"]), nome=row["nome"], email=row["email"],
                           perfil=row["perfil"], ativo=row["ativo"])
