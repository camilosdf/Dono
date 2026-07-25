#!/bin/bash
# scripts/run_migrations.sh
# Aplica migrações SQL sequenciais para produção.

set -e

DB_URL="postgresql://dono:${DB_PASSWORD}@db:5432/dono"

echo "Aplicando migrações..."

# Lista arquivos de migração na ordem (timestamp)
for file in /migrations/*.sql; do
    if [ -f "$file" ]; then
        echo "Executando $file..."
        psql "$DB_URL" -f "$file"
    fi
done

echo "Migrações concluídas."