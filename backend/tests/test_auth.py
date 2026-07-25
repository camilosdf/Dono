# backend/tests/test_auth.py — Sistema Dono
#
# Cobre: login (sucesso, credenciais erradas, usuário inativo),
# refresh (rotação, reuso detectado, expirado), logout, logout-all,
# e o fluxo de detecção de comprometimento de sessão.
import pytest
import pytest_asyncio

from tests.conftest import auth_headers, err


@pytest.mark.asyncio
class TestLogin:
    async def test_login_sucesso(self, client, usuario_admin):
        r = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["perfil"] == "ADMIN"
        assert data["token_type"] == "bearer"

    async def test_login_senha_errada(self, client, usuario_admin):
        r = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "errada"})
        assert r.status_code == 401
        assert err(r) == "CREDENCIAIS_INVALIDAS"

    async def test_login_email_inexistente(self, client):
        r = await client.post("/auth/login", json={"email": "nao@existe.com", "senha": "qualquer"})
        assert r.status_code == 401
        # Mesma mensagem que "senha errada" — de propósito, para não
        # revelar se o e-mail existe ou não (user enumeration)
        assert err(r) == "CREDENCIAIS_INVALIDAS"

    async def test_login_usuario_inativo(self, client, conn, usuario_admin):
        await conn.execute("UPDATE usuarios SET ativo = FALSE WHERE id = $1", usuario_admin["id"])
        r = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        assert r.status_code == 401
        assert err(r) == "CREDENCIAIS_INVALIDAS"

    async def test_login_email_invalido(self, client):
        r = await client.post("/auth/login", json={"email": "isso-nao-e-email", "senha": "qualquer"})
        # Pydantic rejeita antes de chegar no banco
        assert r.status_code == 422


@pytest.mark.asyncio
class TestRefresh:
    async def test_refresh_rotaciona_token(self, client, usuario_admin):
        login = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        rt_original = login.json()["refresh_token"]

        r = await client.post("/auth/refresh", json={"refresh_token": rt_original})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["refresh_token"] != rt_original  # novo token gerado

    async def test_refresh_token_invalido(self, client):
        r = await client.post("/auth/refresh", json={"refresh_token": "token-inventado"})
        assert r.status_code == 401
        assert err(r) == "REFRESH_TOKEN_INVALIDO_OU_EXPIRADO"

    async def test_refresh_token_reutilizado_revoga_familia(self, client, usuario_admin):
        """Rotação com detecção de reuso: usar um refresh_token já rotacionado
        deve revogar a família inteira — protege contra token roubado."""
        login = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        rt1 = login.json()["refresh_token"]

        # Primeiro refresh: rt1 vira rt2
        r1 = await client.post("/auth/refresh", json={"refresh_token": rt1})
        assert r1.status_code == 200
        rt2 = r1.json()["refresh_token"]

        # Reusa rt1 (já revogado): deve revogar toda a família
        r2 = await client.post("/auth/refresh", json={"refresh_token": rt1})
        assert r2.status_code == 401, f"esperava 401, veio {r2.status_code}: {r2.text}"
        assert err(r2) == "REFRESH_TOKEN_REUTILIZADO", f"code errado: {r2.text}"

        # rt2 também deve estar revogado agora (família comprometida).
        # Nota: a revogação da família acontece DENTRO da transação que detecta
        # o reuso — quando a rota de refresh processa rt1 (já revogado), ela
        # revoga todos os tokens não-revogados da família (incluindo rt2) e
        # lança RefreshTokenReusedError. Portanto rt2 agora está inválido.
        r3 = await client.post("/auth/refresh", json={"refresh_token": rt2})
        assert r3.status_code == 401, f"rt2 deveria estar revogado, veio {r3.status_code}: {r3.text}"


@pytest.mark.asyncio
class TestLogout:
    async def test_logout_revoga_token(self, client, usuario_admin):
        login = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        rt = login.json()["refresh_token"]

        r = await client.post("/auth/logout", json={"refresh_token": rt})
        assert r.status_code == 200

        # Tentar usar o token revogado deve falhar
        r2 = await client.post("/auth/refresh", json={"refresh_token": rt})
        assert r2.status_code == 401

    async def test_logout_token_ja_revogado(self, client, usuario_admin):
        login = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        rt = login.json()["refresh_token"]
        await client.post("/auth/logout", json={"refresh_token": rt})

        r = await client.post("/auth/logout", json={"refresh_token": rt})
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_logout_all_revoga_todos_os_dispositivos(self, client, usuario_admin):
        """Dois logins simultâneos (dois dispositivos) — logout-all deve
        revogar ambos."""
        l1 = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        l2 = await client.post("/auth/login", json={"email": "admin@teste.com", "senha": "senha123"})
        rt1 = l1.json()["refresh_token"]
        rt2 = l2.json()["refresh_token"]
        at1 = l1.json()["access_token"]

        r = await client.post("/auth/logout-all", headers=auth_headers(at1))
        assert r.status_code == 200

        # Ambos os refresh tokens devem estar inválidos
        r1 = await client.post("/auth/refresh", json={"refresh_token": rt1})
        r2 = await client.post("/auth/refresh", json={"refresh_token": rt2})
        assert r1.status_code == 401
        assert r2.status_code == 401


@pytest.mark.asyncio
class TestAutenticacaoGeral:
    async def test_sem_token_retorna_401(self, client):
        r = await client.get("/me")
        assert r.status_code == 401
        assert err(r) == "NAO_AUTENTICADO"

    async def test_token_malformado_retorna_401(self, client):
        r = await client.get("/me", headers={"Authorization": "Bearer token-lixo"})
        assert r.status_code == 401

    async def test_me_retorna_dados_do_usuario_logado(self, client, token_admin, usuario_admin):
        r = await client.get("/me", headers=auth_headers(token_admin))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == usuario_admin["id"]
        assert data["perfil"] == "ADMIN"
        assert data["ativo"] is True
