# backend/app/auth.py — Sistema Dono
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)


class RefreshTokenReusedError(Exception):
    """Levantada por rotate_refresh_token() quando um refresh token já
    revogado é reapresentado — indica possível vazamento de sessão (a
    família inteira já foi revogada no momento em que isso é levantado).
    Tipo próprio em vez de RuntimeError genérico: capturar RuntimeError
    correria o risco de engolir silenciosamente um bug não relacionado
    que por acaso levantasse a mesma exceção dentro do bloco try/except
    da rota."""


# ---------------------------------------------------------------------
# Senhas (lentas de propósito — argon2id via passlib)
# ---------------------------------------------------------------------
def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


# ---------------------------------------------------------------------
# Access token (JWT curto — nunca tocam o banco)
# ---------------------------------------------------------------------
def create_access_token(usuario_id: str, perfil: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": usuario_id,
        "perfil": perfil,
        "iat": now,
        "exp": now + ACCESS_TOKEN_TTL,
        "jti": str(uuid.uuid4()),
        "iss": "Dono",
    }
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm=ALGORITHM)


# ---------------------------------------------------------------------
# Refresh token (opaco, alta entropia — só o HASH vai para o banco;
# sha256 é suficiente aqui porque o token já nasce aleatório e longo,
# diferente de senha: não precisa de hash lento como argon2)
# ---------------------------------------------------------------------
def generate_refresh_token() -> tuple[str, str]:
    """Retorna (token_texto_puro, hash_para_armazenar). O texto puro só
    existe nesta resposta HTTP — nunca é persistido."""
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


async def store_refresh_token(conn, usuario_id: str, token_hash: str, familia_id: str) -> None:
    await conn.execute(
        """INSERT INTO refresh_tokens (usuario_id, token_hash, familia_id, expira_em)
           VALUES ($1, $2, $3, $4)""",
        uuid.UUID(usuario_id), token_hash, uuid.UUID(familia_id),
        datetime.now(timezone.utc) + REFRESH_TOKEN_TTL,
    )


async def get_refresh_token_data(conn, token_hash: str):
    return await conn.fetchrow(
        "SELECT id, usuario_id, familia_id, revogado, expira_em "
        "FROM refresh_tokens WHERE token_hash = $1",
        token_hash,
    )


async def rotate_refresh_token(conn, old_hash: str, new_hash: str):
    """Rotação com detecção de reuso (§11 da API):
    - token não encontrado -> None
    - token já revogado sendo reapresentado -> reuso: revoga a família
      inteira e levanta RefreshTokenReusedError
    - token expirado (mas nunca usado) -> None
    - caso normal -> revoga o token antigo, cria o novo na mesma família,
      devolve {usuario_id, familia_id}
    `FOR UPDATE` evita corrida: duas chamadas concorrentes de /auth/refresh
    com o mesmo token não conseguem rotacionar em paralelo.

    NOTA SOBRE TRANSAÇÕES: o caso de reuso usa DOIS blocos de transação
    separados de propósito. Um único `async with conn.transaction()` que
    levantasse exceção sofreria rollback automático pelo asyncpg — inclusive
    desfazendo o UPDATE de revogação da família, que precisa ser commitado
    antes de propagar o erro (bug detectado pelos testes de integração:
    a família parecia revogada para quem reusou o token, mas os demais
    tokens da família continuavam válidos porque o UPDATE nunca commitou).
    """
    # Leitura inicial sem lock — só determina o estado do token.
    # Sem FOR UPDATE aqui porque: (a) no caso de reuso, o UPDATE de
    # revogação precisa commitar em transação própria sem depender
    # desta leitura; (b) no caso normal, o FOR UPDATE está na
    # transação de rotação abaixo, onde o resultado é de fato usado.
    row = await conn.fetchrow(
        "SELECT id, usuario_id, familia_id, revogado, expira_em "
        "FROM refresh_tokens WHERE token_hash = $1",
        old_hash,
    )

    if row is None:
        return None

    if row["revogado"]:
        # Transação própria que COMMITA antes de propagar o erro —
        # garantia de que a família fica revogada mesmo que o chamador
        # trate a exceção e continue. Se fosse dentro de um único bloco
        # transaction() que levanta exceção, o asyncpg faria rollback
        # automático e a revogação seria desfeita (bug detectado pelos
        # testes de integração: token da família continuava válido após
        # detecção de reuso).
        async with conn.transaction():
            await conn.execute(
                "UPDATE refresh_tokens SET revogado = TRUE "
                "WHERE familia_id = $1 AND revogado = FALSE",
                row["familia_id"],
            )
        raise RefreshTokenReusedError()

    if row["expira_em"] < datetime.now(timezone.utc):
        return None

    # Rotação normal: FOR UPDATE dentro da própria transação que vai
    # usar o resultado — previne corrida entre duas requisições concorrentes
    # com o mesmo token válido.
    async with conn.transaction():
        locked = await conn.fetchrow(
            "SELECT id, usuario_id, familia_id, revogado, expira_em "
            "FROM refresh_tokens WHERE token_hash = $1 FOR UPDATE",
            old_hash,
        )
        # Re-verificar após lock — outra requisição pode ter rotacionado
        # o token entre a leitura acima e o FOR UPDATE aqui.
        if locked is None or locked["revogado"]:
            return None

        new_id = uuid.uuid4()
        await conn.execute(
            """INSERT INTO refresh_tokens (id, usuario_id, token_hash, familia_id, expira_em)
               VALUES ($1, $2, $3, $4, $5)""",
            new_id, locked["usuario_id"], new_hash, locked["familia_id"],
            datetime.now(timezone.utc) + REFRESH_TOKEN_TTL,
        )
        await conn.execute(
            "UPDATE refresh_tokens SET revogado = TRUE, substituido_por = $1 WHERE id = $2",
            new_id, locked["id"],
        )
    return {"usuario_id": str(locked["usuario_id"]), "familia_id": str(locked["familia_id"])}


async def revoke_refresh_token(conn, token_hash: str) -> bool:
    result = await conn.execute(
        "UPDATE refresh_tokens SET revogado = TRUE WHERE token_hash = $1 AND revogado = FALSE",
        token_hash,
    )
    return result.endswith(" 1")  # asyncpg retorna "UPDATE 1" quando altera 1 linha


async def revoke_all_tokens_for_user(conn, usuario_id: str) -> None:
    """Usado em /auth/logout-all. Revoga TODAS as famílias ativas do
    usuário — não só a mais recente. Um usuário pode ter sessões
    simultâneas em vários dispositivos, cada uma com sua própria
    familia_id; pegar só a última deixaria os outros dispositivos
    logados, contradizendo o próprio nome do endpoint."""
    await conn.execute(
        "UPDATE refresh_tokens SET revogado = TRUE WHERE usuario_id = $1 AND revogado = FALSE",
        uuid.UUID(usuario_id),
    )
