# backend/tests/test_usuarios.py — Sistema Dono
#
# Suite dedicada ao módulo de usuários.
# Cobre:
#   A) GET /me — dados atualizados do usuário autenticado
#   B) GET /usuarios — listagem (somente ADMIN)
#   C) POST /usuarios — criação com validações de negócio
#   D) PATCH /usuarios/{id} — atualização de perfil e status
#   E) Desativação — revogação de tokens + impedimento de refresh
#
# O que JÁ está coberto em test_rbac.py e NÃO é repetido aqui:
#   - Permissão de criação (chef/compras/gestao → 403, admin → 201)
#
# Fixtures: client, conn, token_admin, token_chef, token_gestao,
#           token_compras, usuario_admin

import uuid
import pytest

from tests.conftest import auth_headers, err


# =====================================================================
# Helpers
# =====================================================================

async def _criar_usuario(client, token_admin, nome="Teste", perfil="CHEF",
                          email=None, senha="senha123"):
    """Cria um usuário via API e retorna o response JSON."""
    email = email or f"teste_{uuid.uuid4().hex[:8]}@dono.com"
    r = await client.post(
        "/usuarios",
        headers=auth_headers(token_admin),
        json={"nome": nome, "email": email, "senha": senha, "perfil": perfil},
    )
    return r


# =====================================================================
# A) GET /me
# =====================================================================

@pytest.mark.asyncio
class TestMe:
    """Testa GET /me — retorna dados atuais do usuário autenticado."""

    async def test_me_retorna_dados_corretos(self, client, token_admin, usuario_admin):
        """GET /me deve retornar id, nome, email, perfil e ativo do usuário
        cujo token foi informado."""
        r = await client.get("/me", headers=auth_headers(token_admin))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == str(usuario_admin["id"])
        assert data["perfil"] == "ADMIN"
        assert data["ativo"] is True

    async def test_me_tem_campos_obrigatorios(self, client, token_admin):
        """Resposta de /me deve conter todos os campos do modelo UsuarioOut."""
        r = await client.get("/me", headers=auth_headers(token_admin))
        data = r.json()
        for campo in ("id", "nome", "email", "perfil", "ativo"):
            assert campo in data, f"Campo '{campo}' ausente na resposta de /me"

    async def test_me_sem_token_retorna_401(self, client):
        """GET /me sem autenticação deve retornar 401."""
        r = await client.get("/me")
        assert r.status_code == 401

    async def test_me_reflete_perfil_atual_nao_do_token(
        self, client, token_admin, conn
    ):
        """GET /me busca o perfil atual no banco, não o que está no JWT.
        Valida que a implementação usa fetchrow e não apenas o payload do token.
        """
        # Cria usuário Chef e obtém token
        r_criar = await _criar_usuario(client, token_admin, nome="Chef Temp",
                                        perfil="CHEF",
                                        email=f"chef_{uuid.uuid4().hex[:6]}@dono.com")
        assert r_criar.status_code == 201
        usuario_id = r_criar.json()["id"]

        # Login para obter token com perfil CHEF
        email = r_criar.json()["email"]
        r_login = await client.post(
            "/auth/login",
            json={"email": email, "senha": "senha123"},
        )
        token_chef_temp = r_login.json()["access_token"]

        # /me com token de CHEF deve retornar CHEF
        r_me = await client.get("/me", headers=auth_headers(token_chef_temp))
        assert r_me.json()["perfil"] == "CHEF"

        # Atualiza perfil para GESTAO diretamente no banco
        await conn.execute(
            "UPDATE usuarios SET perfil = 'GESTAO' WHERE id = $1",
            uuid.UUID(usuario_id),
        )

        # /me com o MESMO token ainda válido deve agora retornar GESTAO
        # (prova que a rota busca do banco, não do JWT)
        r_me2 = await client.get("/me", headers=auth_headers(token_chef_temp))
        assert r_me2.json()["perfil"] == "GESTAO"


# =====================================================================
# B) GET /usuarios
# =====================================================================

@pytest.mark.asyncio
class TestListarUsuarios:
    """Testa GET /usuarios — listagem restrita a ADMIN."""

    async def test_listar_retorna_lista(self, client, token_admin):
        """GET /usuarios deve retornar uma lista (pelo menos o próprio admin)."""
        r = await client.get("/usuarios", headers=auth_headers(token_admin))
        assert r.status_code == 200
        assert isinstance(r.json(), list)
        assert len(r.json()) >= 1

    async def test_listar_campos_obrigatorios(self, client, token_admin):
        """Cada item da lista deve conter os campos do modelo UsuarioOut."""
        r = await client.get("/usuarios", headers=auth_headers(token_admin))
        for usuario in r.json():
            for campo in ("id", "nome", "email", "perfil", "ativo"):
                assert campo in usuario

    async def test_listar_sem_token_retorna_401(self, client):
        """GET /usuarios sem autenticação deve retornar 401."""
        r = await client.get("/usuarios")
        assert r.status_code == 401

    async def test_listar_perfil_nao_admin_retorna_403(
        self, client, token_chef, token_gestao, token_compras
    ):
        """Apenas ADMIN pode listar usuários."""
        for token in (token_chef, token_gestao, token_compras):
            r = await client.get("/usuarios", headers=auth_headers(token))
            assert r.status_code == 403

    async def test_usuario_criado_aparece_na_listagem(
        self, client, token_admin
    ):
        """Usuário criado via POST deve aparecer em GET /usuarios."""
        email = f"listagem_{uuid.uuid4().hex[:8]}@dono.com"
        await _criar_usuario(client, token_admin, email=email)

        r = await client.get("/usuarios", headers=auth_headers(token_admin))
        emails = [u["email"] for u in r.json()]
        assert email in emails


# =====================================================================
# C) POST /usuarios
# =====================================================================

@pytest.mark.asyncio
class TestCriarUsuario:
    """Testa POST /usuarios — criação com validações de negócio."""

    async def test_criar_usuario_chef(self, client, token_admin):
        """Criação de usuário CHEF deve retornar 201 com dados corretos."""
        email = f"chef_{uuid.uuid4().hex[:8]}@dono.com"
        r = await _criar_usuario(client, token_admin, nome="Chef Novo",
                                  perfil="CHEF", email=email)
        assert r.status_code == 201
        data = r.json()
        assert data["nome"] == "Chef Novo"
        assert data["email"] == email
        assert data["perfil"] == "CHEF"
        assert data["ativo"] is True
        assert "id" in data

    async def test_criar_todos_os_perfis_validos(self, client, token_admin):
        """Todos os perfis válidos (CHEF, COMPRAS, GESTAO, ADMIN) devem
        ser aceitos na criação."""
        for perfil in ("CHEF", "COMPRAS", "GESTAO", "ADMIN"):
            email = f"{perfil.lower()}_{uuid.uuid4().hex[:6]}@dono.com"
            r = await _criar_usuario(client, token_admin, perfil=perfil, email=email)
            assert r.status_code == 201, f"Falha ao criar perfil {perfil}: {r.json()}"
            assert r.json()["perfil"] == perfil

    async def test_email_duplicado_retorna_409(self, client, token_admin):
        """Tentativa de criar usuário com email já cadastrado deve retornar
        409 com código EMAIL_JA_CADASTRADO."""
        email = f"dup_{uuid.uuid4().hex[:8]}@dono.com"
        await _criar_usuario(client, token_admin, email=email)
        r = await _criar_usuario(client, token_admin, email=email)
        assert r.status_code == 409
        assert err(r) == "EMAIL_JA_CADASTRADO"

    async def test_perfil_invalido_retorna_400(self, client, token_admin):
        """Perfil fora dos valores válidos deve retornar 400."""
        r = await _criar_usuario(client, token_admin, perfil="SUPERADMIN")
        assert r.status_code == 400
        assert err(r) == "VALIDACAO_INVALIDA"

    async def test_email_invalido_retorna_422(self, client, token_admin):
        """Email malformado deve ser rejeitado pelo Pydantic com 422."""
        r = await client.post(
            "/usuarios",
            headers=auth_headers(token_admin),
            json={"nome": "Teste", "email": "nao-e-email", "senha": "123",
                  "perfil": "CHEF"},
        )
        assert r.status_code == 422

    async def test_usuario_criado_pode_fazer_login(self, client, token_admin):
        """Usuário criado via API deve conseguir autenticar com as credenciais
        informadas no momento da criação."""
        email = f"login_{uuid.uuid4().hex[:8]}@dono.com"
        senha = "minha_senha_forte"
        await _criar_usuario(client, token_admin, email=email, senha=senha)

        r = await client.post(
            "/auth/login",
            json={"email": email, "senha": senha},
        )
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_sem_token_retorna_401(self, client):
        """POST /usuarios sem autenticação deve retornar 401."""
        r = await client.post(
            "/usuarios",
            json={"nome": "X", "email": "x@x.com", "senha": "123",
                  "perfil": "CHEF"},
        )
        assert r.status_code == 401


# =====================================================================
# D) PATCH /usuarios/{id}
# =====================================================================

@pytest.mark.asyncio
class TestAtualizarUsuario:
    """Testa PATCH /usuarios/{id} — atualização de perfil e status."""

    async def test_atualizar_perfil(self, client, token_admin):
        """PATCH com novo perfil deve atualizar e retornar o perfil atualizado."""
        r_criar = await _criar_usuario(client, token_admin, perfil="CHEF")
        usuario_id = r_criar.json()["id"]

        r = await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"perfil": "GESTAO"},
        )
        assert r.status_code == 200
        assert r.json()["perfil"] == "GESTAO"

    async def test_desativar_usuario(self, client, token_admin):
        """PATCH com ativo=False deve desativar o usuário."""
        r_criar = await _criar_usuario(client, token_admin)
        usuario_id = r_criar.json()["id"]

        r = await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"ativo": False},
        )
        assert r.status_code == 200
        assert r.json()["ativo"] is False

    async def test_reativar_usuario(self, client, token_admin):
        """PATCH com ativo=True deve reativar usuário previamente desativado."""
        r_criar = await _criar_usuario(client, token_admin)
        usuario_id = r_criar.json()["id"]

        await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"ativo": False},
        )
        r = await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"ativo": True},
        )
        assert r.status_code == 200
        assert r.json()["ativo"] is True

    async def test_atualizar_campos_independentemente(self, client, token_admin):
        """PATCH com apenas perfil não deve alterar o campo ativo, e vice-versa
        (COALESCE no SQL preserva o valor atual)."""
        r_criar = await _criar_usuario(client, token_admin, perfil="CHEF")
        usuario_id = r_criar.json()["id"]

        # Atualiza apenas perfil — ativo deve permanecer True
        r = await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"perfil": "COMPRAS"},
        )
        assert r.json()["perfil"] == "COMPRAS"
        assert r.json()["ativo"] is True

    async def test_usuario_inexistente_retorna_404(self, client, token_admin):
        """PATCH em UUID inexistente deve retornar 404."""
        r = await client.patch(
            f"/usuarios/{uuid.uuid4()}",
            headers=auth_headers(token_admin),
            json={"perfil": "CHEF"},
        )
        assert r.status_code == 404
        assert err(r) == "RECURSO_NAO_ENCONTRADO"

    async def test_sem_token_retorna_401(self, client):
        """PATCH sem autenticação deve retornar 401."""
        r = await client.patch(
            f"/usuarios/{uuid.uuid4()}",
            json={"perfil": "CHEF"},
        )
        assert r.status_code == 401

    async def test_nao_admin_retorna_403(
        self, client, token_chef, token_gestao, token_compras, token_admin
    ):
        """Apenas ADMIN pode atualizar usuários."""
        r_criar = await _criar_usuario(client, token_admin)
        usuario_id = r_criar.json()["id"]

        for token in (token_chef, token_gestao, token_compras):
            r = await client.patch(
                f"/usuarios/{usuario_id}",
                headers=auth_headers(token),
                json={"perfil": "CHEF"},
            )
            assert r.status_code == 403


# =====================================================================
# E) Desativação — comportamento de autenticação pós-desativação
# =====================================================================

@pytest.mark.asyncio
class TestDesativacao:
    """Testa o comportamento de autenticação após desativação de usuário.

    A implementação revoga todos os refresh_tokens ao desativar (via
    revoke_all_tokens_for_user), mas o access_token já emitido continua
    válido por até 15 minutos (JWT autocontido — não há blacklist de
    access tokens). Esses testes validam o contrato documentado.
    """

    async def test_usuario_inativo_nao_pode_fazer_login(
        self, client, token_admin
    ):
        """Usuário desativado não deve conseguir fazer login (retorna 401)."""
        email = f"inativo_{uuid.uuid4().hex[:8]}@dono.com"
        r_criar = await _criar_usuario(client, token_admin, email=email,
                                        senha="senha123")
        usuario_id = r_criar.json()["id"]

        # Desativa o usuário
        await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"ativo": False},
        )

        # Tentativa de login deve falhar
        r_login = await client.post(
            "/auth/login",
            json={"email": email, "senha": "senha123"},
        )
        assert r_login.status_code == 401

    async def test_desativar_revoga_refresh_tokens(
        self, client, token_admin, conn
    ):
        """Desativar usuário deve invalidar seus refresh_tokens no banco,
        impedindo rotação futura."""
        email = f"revoke_{uuid.uuid4().hex[:8]}@dono.com"
        r_criar = await _criar_usuario(client, token_admin, email=email,
                                        senha="senha123")
        usuario_id = r_criar.json()["id"]

        # Login para gerar refresh_token
        r_login = await client.post(
            "/auth/login",
            json={"email": email, "senha": "senha123"},
        )
        refresh_token = r_login.json()["refresh_token"]

        # Desativa o usuário
        await client.patch(
            f"/usuarios/{usuario_id}",
            headers=auth_headers(token_admin),
            json={"ativo": False},
        )

        # Verifica que nenhum refresh_token ativo existe no banco
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM refresh_tokens
               WHERE usuario_id = $1 AND revogado = FALSE""",
            uuid.UUID(usuario_id),
        )
        assert count == 0, f"Esperava 0 tokens ativos após desativação, encontrou {count}"

        # Tentativa de refresh com o token antigo deve falhar
        r_refresh = await client.post(
            "/auth/refresh",
            json={"refresh_token": refresh_token},
        )
        assert r_refresh.status_code in (401, 403)
