# backend/Dockerfile — Sistema Dono
# Multi-estágio: "runtime" é o alvo padrão (produção, imagem enxuta,
# usuário não-root); "dev" adiciona reload e roda como root só localmente.
# asyncpg tem wheel pré-compilada para linux — não precisa de gcc/libpq-dev
# (diferente de psycopg2, que exigiria dependências adicionais).

# ---------------------------------------------------------------------
# Estágio base: dependências comuns
# ---------------------------------------------------------------------
FROM python:3.11-slim AS base
WORKDIR /app

# scripts/seed_admin.py (e qualquer outro script solto em scripts/) roda
# como "python scripts/algo.py" — o Python só adiciona a PASTA DO SCRIPT
# (/app/scripts) ao sys.path automaticamente, não /app. Sem isso, "from
# app.auth import ..." dentro do script não encontra o pacote app/, que
# fica um nível acima. app/main.py e app/worker.py não sofrem disso
# porque rodam via "uvicorn app.main:app" / "python -m app.worker",
# formas que já usam /app (o diretório de trabalho) como base.
ENV PYTHONPATH=/app

# Copia e instala dependências antes do código (cache de camadas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------
# Estágio dev: hot-reload e bind-mount (desenvolvimento local)
# ---------------------------------------------------------------------
FROM base AS dev
# Não instala dependências extras, apenas expõe a porta e usa reload
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------
# Estágio runtime: produção (imagem enxuta, usuário não-root)
# ---------------------------------------------------------------------
FROM base AS runtime

# Instala o cliente PostgreSQL (psql) para executar migrações
# Isso é necessário para o script /run_migrations.sh.
# Remove o cache do apt para manter a imagem pequena.
RUN apt-get update && \
    apt-get install -y --no-install-recommends postgresql-client && \
    rm -rf /var/lib/apt/lists/*

# Copia o script de migrações (deve existir em scripts/run_migrations.sh)
# e torna-o executável.
COPY ./scripts/run_migrations.sh /run_migrations.sh
RUN chmod +x /run_migrations.sh

# Cria usuário não-root para executar a aplicação (segurança)
RUN useradd --create-home --shell /bin/bash dono_app

# Copia o código da aplicação (excluindo arquivos desnecessários via .dockerignore)
COPY . .

# Ajusta permissões para o usuário dono_app (opcional)
RUN chown -R dono_app:dono_app /app

# Muda para o usuário não-root
USER dono_app

# Expõe a porta da API
EXPOSE 8000

# Comando padrão: executa migrações (se o script existir) e inicia a aplicação.
# O script de migrações é executado com sudo? Não, o usuário dono_app pode
# executar psql se as credenciais estiverem no .env (mas ele precisa ter
# permissão de leitura no .env). No entanto, o script é executado antes
# de mudar para dono_app? Não, aqui o USER já é dono_app.
# Para garantir que o script de migrações possa se conectar ao banco,
# ele deve ser executado com as variáveis de ambiente carregadas.
# O formato abaixo usa sh -c para executar o script e depois a aplicação.
CMD ["sh", "-c", "if [ -f /run_migrations.sh ]; then /run_migrations.sh; fi; uvicorn app.main:app --host 0.0.0.0 --port 8000"]