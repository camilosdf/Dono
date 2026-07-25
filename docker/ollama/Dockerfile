# docker/ollama/Dockerfile — Sistema Dono
#
# Dockerfile opcional para construir uma imagem customizada do Ollama.
# Por padrão, o docker-compose.yml usa a imagem oficial ollama/ollama:latest.
# Esta imagem é útil se você quiser:
#   1. Pré-baixar modelos (evitar download na primeira execução)
#   2. Adicionar configurações customizadas (ex.: variáveis de ambiente fixas)
#   3. Garantir uma versão específica do Ollama
#
# Uso:
#   1. Descomente o build no docker-compose.yml para este serviço:
#      ollama:
#        build:
#          context: ./docker/ollama
#          dockerfile: Dockerfile
#        # image: ollama/ollama:latest  # comente esta linha
#   2. Ajuste as variáveis OLLAMA_MODEL_PRELOAD no .env para o(s) modelo(s) desejado(s)
#   3. docker compose build ollama
#   4. docker compose up -d ollama
#
# Nota: Pré-baixar modelos aumenta o tamanho da imagem significativamente.
#       Use apenas se você tiver largura de banda limitada ou quiser
#       garantir que os modelos estejam disponíveis offline.

# -----------------------------------------------------------------------------
# Estágio 1: Baixar modelos (usando a imagem oficial como base)
# -----------------------------------------------------------------------------
FROM ollama/ollama:latest AS downloader

# Define variáveis de ambiente para o Ollama
ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_KEEP_ALIVE=5m

# Argumento para o modelo a ser pré-carregado (definido no .env ou docker-compose)
ARG OLLAMA_MODEL_PRELOAD=${OLLAMA_MODEL:-llama3.1:8b}

# Script para baixar o(s) modelo(s) durante o build
# Isso é feito em uma etapa separada para que o modelo fique no cache do Docker
# e possa ser reutilizado sem precisar baixar novamente a cada build.
RUN --mount=type=cache,target=/root/.ollama \
    echo "Baixando modelo: ${OLLAMA_MODEL_PRELOAD}" && \
    ollama pull ${OLLAMA_MODEL_PRELOAD}

# (Opcional) Baixe múltiplos modelos separados por espaço
# Se quiser mais de um modelo, ajuste a variável ou descomente:
# RUN --mount=type=cache,target=/root/.ollama \
#     ollama pull llama3.2:3b && \
#     ollama pull nomic-embed-text

# -----------------------------------------------------------------------------
# Estágio 2: Imagem final (copia os modelos baixados)
# -----------------------------------------------------------------------------
FROM ollama/ollama:latest

# Copia os modelos da etapa anterior para a imagem final
# O diretório padrão do Ollama é /root/.ollama
COPY --from=downloader /root/.ollama /root/.ollama

# Define as mesmas variáveis de ambiente
ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_KEEP_ALIVE=5m

# Opcional: script de entrada customizado (se precisar de mais configuração)
# COPY entrypoint.sh /entrypoint.sh
# RUN chmod +x /entrypoint.sh
# ENTRYPOINT ["/entrypoint.sh"]

# Comando padrão do Ollama (inicia o servidor)
CMD ["ollama", "serve"]