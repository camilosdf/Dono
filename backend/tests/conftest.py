# backend/tests/conftest.py — Sistema Dono
#
# Estratégia: integração real contra PostgreSQL (sem mocks de asyncpg),
# httpx.AsyncClient com a app FastAPI montada diretamente (sem Uvicorn).
# Redis é o único componente mockado.
#
# Isolamento entre testes: TRUNCATE das tabelas de dados antes de cada teste
# (deixando schema e seeds intactos). É mais robusto que SAVEPOINT numa única
# conexão compartilhada porque as rotas abrem conexões próprias do pool.
#
# PRÉ-REQUISITO: Postgres em TEST_DATABASE_URL. Schema aplicado 1x por
# sessão (via CREATE OR REPLACE / CREATE TABLE IF NOT EXISTS ou recriação
# total). A database dono_test PRECISA existir antes de rodar os testes:
#   docker compose exec db psql -U dono -c "CREATE DATABASE dono_test OWNER dono;"
#
# RODAR:
#   docker compose exec backend pytest tests/ -v
#
# ATUALIZAÇÃO (2026-07-24):
#   - Mock do Redis agora usa AsyncMock em todos os métodos, resolvendo o erro
#     "object MagicMock can't be used in 'await' expression".
#   - A fixture event_loop está definida com scope session para evitar conflitos.
#   - Adicionadas as dependências de importação corretas.
import os
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.auth import create_access_token, hash_password
from app.main import app

_TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "").replace("/dono", "/dono_test"),
)


def _ler_sql(nome: str) -> str:
    """Lê um arquivo SQL dos diretórios possíveis (docker volume ou fallback local)."""
    candidatos = [
        # Volume montado em dev (docker-compose.override.yml: ./scripts:/scripts)
        f"/scripts/{nome}",
        # Fallback para rodar pytest fora do container (ex.: venv local)
        f"../scripts/{nome}",
        f"../../scripts/{nome}",
    ]
    for caminho in candidatos:
        try:
            with open(caminho, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            continue
    raise FileNotFoundError(
        f"Script {nome} não encontrado — rode pytest de dentro de backend/ "
        f"(tentei: {candidatos})"
    )


# ---------- event_loop (scope=session) ------------------------------------
@pytest.fixture(scope="session")
def event_loop():
    """Cria um loop de eventos para toda a sessão de testes.
    Necessário para evitar conflitos entre fixtures de sessão e o loop padrão do pytest-asyncio.
    """
    loop = asyncio.get_event_loop_policy().new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


# ---------- pool e schema (scope=session) ------------------------------------
@pytest_asyncio.fixture(scope="session")
async def db_pool():
    """Pool para a database de teste. O schema é recriado do zero a cada sessão
    de pytest via DROP/CREATE SCHEMA PUBLIC — elimina o DuplicateTableError."""
    pool = await asyncpg.create_pool(_TEST_DB_URL, min_size=2, max_size=10)

    schema = _ler_sql("schema.sql")
    queries = _ler_sql("business-queries.sql")
    seeds = _ler_sql("seeds.sql")

    async with pool.acquire() as c:
        # Recria o schema do zero — idempotente em qualquer re-execução
        await c.execute("DROP SCHEMA public CASCADE")
        await c.execute("CREATE SCHEMA public")
        await c.execute("GRANT ALL ON SCHEMA public TO dono")
        await c.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await c.execute(schema)
        await c.execute(queries)
        await c.execute(seeds)

    yield pool
    await pool.close()


# ---------- limpeza entre testes (scope=function) ----------------------------
_TABELAS_DADOS = [
    "classificacoes_abc",
    "eventos_dominio",
    "ia_jobs",
    "movimentacoes_estoque",
    "itens_menu",
    "itens_refeicao",
    "itens_receita",
    "cotacoes",
    "lotes_insumo",
    "fornecedores_categorias",
    "refresh_tokens",
    "menus",
    "refeicoes",
    "pratos",
    "insumos",
    "fornecedores",
    "usuarios",
]


@pytest_asyncio.fixture(autouse=True)
async def limpar_banco(db_pool):
    """Trunca as tabelas de dados antes de cada teste, deixando seeds intactos."""
    async with db_pool.acquire() as c:
        tabelas = ", ".join(_TABELAS_DADOS)
        await c.execute(f"TRUNCATE {tabelas} RESTART IDENTITY CASCADE")
    yield


# ---------- conn (acesso direto ao banco para fixtures de setup) -----------
@pytest_asyncio.fixture
async def conn(db_pool):
    """Conexão direta ao banco para setup de dados nas fixtures de teste."""
    async with db_pool.acquire() as connection:
        yield connection


# ---------- redis mock (autouse) --------------------------------------------
@pytest.fixture(autouse=True)
def mock_redis():
    """Mock do Redis para todos os testes.
    CORREÇÃO: Agora todos os métodos que podem ser 'await' são AsyncMock.
    Isso evita o erro 'object MagicMock can't be used in await expression'."""
    r = MagicMock(spec=["setex", "get", "incr", "expire", "ttl", "decr", "delete"])
    
    # Métodos que são chamados com await
    r.setex = AsyncMock(return_value=True)
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.expire = AsyncMock(return_value=True)
    r.ttl = AsyncMock(return_value=60)
    r.decr = AsyncMock(return_value=0)
    r.delete = AsyncMock(return_value=1)
    
    # Se algum método não estiver mockado, o MagicMock padrão será sincrono,
    # mas como temos os principais cobertos, isso é suficiente.
    with patch("app.redis_client._client", r), \
         patch("app.rate_limit.get_redis", return_value=r):
        yield r


# ---------- usuários e tokens ------------------------------------------------
async def _criar_usuario(conn, nome, email, perfil):
    row = await conn.fetchrow(
        """INSERT INTO usuarios (nome, email, senha_hash, perfil)
           VALUES ($1,$2,$3,$4) RETURNING id, perfil""",
        nome, email, hash_password("senha123"), perfil,
    )
    return {"id": str(row["id"]), "perfil": row["perfil"]}


def _token(u):
    return create_access_token(u["id"], u["perfil"])


@pytest_asyncio.fixture
async def usuario_admin(conn):
    return await _criar_usuario(conn, "Admin Teste", "admin@teste.com", "ADMIN")

@pytest_asyncio.fixture
async def usuario_chef(conn):
    return await _criar_usuario(conn, "Chef Teste", "chef@teste.com", "CHEF")

@pytest_asyncio.fixture
async def usuario_compras(conn):
    return await _criar_usuario(conn, "Compras Teste", "compras@teste.com", "COMPRAS")

@pytest_asyncio.fixture
async def usuario_gestao(conn):
    return await _criar_usuario(conn, "Gestao Teste", "gestao@teste.com", "GESTAO")

@pytest.fixture
def token_admin(usuario_admin):
    return _token(usuario_admin)

@pytest.fixture
def token_chef(usuario_chef):
    return _token(usuario_chef)

@pytest.fixture
def token_compras(usuario_compras):
    return _token(usuario_compras)

@pytest.fixture
def token_gestao(usuario_gestao):
    return _token(usuario_gestao)


# ---------- cliente HTTP ---------------------------------------------------
@pytest_asyncio.fixture
async def client(db_pool):
    """AsyncClient com o pool de teste injetado no lugar do pool global."""
    from app import database
    _orig = database._pool
    database._pool = db_pool
    async with AsyncClient(app=app, base_url="http://test") as c:
        yield c
    database._pool = _orig


# ---------- utilitários para os testes ------------------------------------
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def err(response, key: str = "code") -> str:
    """Acessa o campo de erro abstraindo o envelope do FastAPI."""
    data = response.json()
    erro = data.get("detail") or data
    if isinstance(erro, dict) and "error" in erro:
        return erro["error"][key]
    return str(erro)