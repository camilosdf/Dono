# db/Dockerfile — Sistema Dono
#
# PostgreSQL 15 Alpine com:
#   - pgtap (para testes)
#   - pgvector (para RAG/embeddings - Fase 7)
#
# A compilação é feita no momento do build para evitar dependências externas.

FROM postgres:15-alpine

RUN apk add --no-cache --virtual .build-deps \
        git make gcc musl-dev perl patch clang llvm \
    # Compila e instala pgtap
    && git clone --depth 1 https://github.com/theory/pgtap.git /tmp/pgtap \
    && cd /tmp/pgtap \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/pgtap \
    # Compila e instala pgvector
    && git clone --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/pgvector \
    # Remove pacotes de compilação (imagem final mais leve)
    && apk del .build-deps