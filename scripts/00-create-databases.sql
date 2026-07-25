-- 00-create-databases.sql
-- Executado ANTES do schema.sql e seeds.sql pelo docker-entrypoint-initdb.d/
-- (ordenação alfabética: 00 < 01 < 02 < 03).
-- Cria databases auxiliares que precisam existir antes de qualquer sessão
-- de desenvolvimento ou teste. Roda no contexto padrão "postgres" (superuser),
-- fora de transação explícita — CREATE DATABASE não é permitido dentro de
-- bloco BEGIN/COMMIT.
--
-- dono_test: database usada pelos testes de integração do backend
-- (backend/tests/conftest.py). Recriada do zero a cada sessão pytest
-- via DROP/CREATE SCHEMA public (não DROP DATABASE — o pool já está conectado).
-- Precisa existir ANTES de qualquer `docker compose exec backend pytest`.
-- Sem este script, toda vez que o volume Postgres é recriado (docker compose
-- down -v) a dono_test some e os testes falham com
-- "InvalidCatalogNameError: database dono_test does not exist".
SELECT 'CREATE DATABASE dono_test OWNER dono'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'dono_test')\gexec
