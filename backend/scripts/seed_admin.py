# backend/scripts/seed_admin.py — Sistema Dono
#
# Roda DE DENTRO do container backend, onde "app" é importável:
#   docker compose exec backend python scripts/seed_admin.py
#
# (Um texto anterior sugeriu colocar este arquivo em scripts/ na RAIZ do
# projeto — mas só ./backend é copiado para dentro da imagem do backend
# via Dockerfile; a pasta scripts/ na raiz só é montada no container do
# banco, para os .sql de inicialização. Um script Python que importa
# `app.database` precisa estar dentro do build context do backend.)
import asyncio

from app.auth import hash_password
from app.database import connect, disconnect, get_pool


async def main() -> None:
    await connect()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO usuarios (nome, email, senha_hash, perfil)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (email) DO NOTHING
               RETURNING id""",
            "Admin Dono", "admin@dono.com", hash_password("admin123"), "ADMIN",
        )
    await disconnect()

    if row:
        print("Usuário admin@dono.com / admin123 criado.")
    else:
        print("Usuário admin@dono.com já existia — nada foi alterado.")


if __name__ == "__main__":
    asyncio.run(main())
