#!/usr/bin/env python3
# backend/scripts/popular_rag.py — Sistema Dono
#
# Script de população inicial da base RAG.
# Lê dados reais do banco e documentos do domínio e insere na tabela
# `documentos` com embeddings gerados via sentence-transformers.
#
# Tipos de documento inseridos:
#   FICHA_TECNICA     — ficha gerencial de cada prato ativo
#   REGRAS_COMPOSICAO — regras de composição por gênero de refeição
#   ESTILO_SERVICO    — descrição e dinâmica de cada estilo de serviço
#   GLOSSARIO         — glossário de domínio (Ubiquitous Language)
#   ADR               — Architecture Decision Records
#
# Uso (dentro do container backend):
#   python scripts/popular_rag.py [--limpar] [--tipos FICHA_TECNICA,ADR]
#
# Flags:
#   --limpar   Remove documentos existentes do tipo antes de reinserir
#   --tipos    Lista separada por vírgula dos tipos a processar
#              (padrão: todos)
#
# PRÉ-REQUISITO:
#   - Extensão pgvector instalada no banco
#   - Modelo sentence-transformers disponível (all-MiniLM-L6-v2)
#   - DATABASE_URL no ambiente
#
# Rodar:
#   docker compose exec backend python scripts/popular_rag.py
#   docker compose exec backend python scripts/popular_rag.py --limpar
#   docker compose exec backend python scripts/popular_rag.py --tipos FICHA_TECNICA

import asyncio
import argparse
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

import asyncpg

# Adiciona /app ao path para importar app.*
sys.path.insert(0, "/app")

from app.rag import gerar_embedding, embedding_to_string

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("popular_rag")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Diretório dos documentos estáticos (docs/ na raiz do projeto)
# Dentro do container, o projeto está em /app; docs/ está em /docs
DOCS_DIR = Path("/docs")
ADR_DIR = DOCS_DIR / "adr"


# =====================================================================
# Helpers
# =====================================================================

async def inserir_documento(
    conn,
    titulo: str,
    conteudo: str,
    tipo: str,
    entidade_id: Optional[uuid.UUID] = None,
    metadados: Optional[dict] = None,
) -> uuid.UUID:
    """Insere ou atualiza um documento e seu embedding."""
    embedding = await gerar_embedding(conteudo)
    embedding_str = embedding_to_string(embedding)

    # Upsert por (titulo, tipo) — evita duplicatas em reruns
    row = await conn.fetchrow(
        """INSERT INTO documentos (titulo, conteudo, tipo, entidade_id, metadados, embedding)
           VALUES ($1, $2, $3, $4, $5, CAST($6 AS vector))
           ON CONFLICT (titulo, tipo)
           DO UPDATE SET
               conteudo     = EXCLUDED.conteudo,
               embedding    = EXCLUDED.embedding,
               metadados    = EXCLUDED.metadados,
               atualizado_em = now()
           RETURNING id""",
        titulo,
        conteudo,
        tipo,
        entidade_id,
        metadados or {},
        embedding_str,
    )
    return row["id"]


async def limpar_tipo(conn, tipo: str) -> int:
    """Remove todos os documentos de um tipo."""
    result = await conn.execute(
        "DELETE FROM documentos WHERE tipo = $1", tipo
    )
    count = int(result.split()[-1])
    logger.info("Removidos %d documentos do tipo %s", count, tipo)
    return count


# =====================================================================
# Populadores por tipo
# =====================================================================

async def popular_fichas_tecnicas(conn, limpar: bool = False) -> int:
    """Gera fichas técnicas gerenciais de todos os pratos ativos."""
    if limpar:
        await limpar_tipo(conn, "FICHA_TECNICA")

    pratos = await conn.fetch(
        """SELECT p.id, p.nome, p.genero_prato, p.rendimento_base_porcoes,
                  p.tempo_preparo_min, p.margem_desperdicio_pct, p.custo_embalagem,
                  p.preco_venda_praticado, p.instrucoes_apresentacao,
                  p.temperatura_servico, p.modo_preparo
             FROM pratos p
            WHERE p.status = 'ATIVO'
            ORDER BY p.nome"""
    )

    count = 0
    for prato in pratos:
        itens = await conn.fetch(
            """SELECT i.nome AS insumo_nome, i.unidade,
                      ir.peso_bruto, ir.fator_correcao, ir.peso_liquido,
                      ir.custo_unitario_registrado, ir.custo_total_calculado, ir.tipo
                 FROM itens_receita ir
                 JOIN insumos i ON i.id = ir.insumo_id
                WHERE ir.prato_id = $1
                ORDER BY ir.custo_total_calculado DESC""",
            prato["id"],
        )

        custo_ingredientes = sum(float(i["custo_total_calculado"] or 0) for i in itens)
        margem = float(prato["margem_desperdicio_pct"] or 0)
        custo_total = custo_ingredientes * (1 + margem / 100)
        rendimento = float(prato["rendimento_base_porcoes"] or 1)
        cmv = custo_total / rendimento if rendimento else 0
        embalagem = float(prato["custo_embalagem"] or 0)
        preco = float(prato["preco_venda_praticado"]) if prato["preco_venda_praticado"] else None
        margem_lucro = ((preco - cmv - embalagem) / preco * 100) if preco else None

        linhas_ingredientes = []
        for item in itens:
            linhas_ingredientes.append(
                f"  - {item['insumo_nome']}: PB {float(item['peso_bruto']):.3f} {item['unidade']}, "
                f"FC {float(item['fator_correcao']):.2f}, "
                f"PL {float(item['peso_liquido']):.3f} {item['unidade']}, "
                f"custo unitário R$ {float(item['custo_unitario_registrado']):.2f}, "
                f"custo total R$ {float(item['custo_total_calculado']):.2f} "
                f"[{item['tipo']}]"
            )

        conteudo_parts = [
            f"FICHA TÉCNICA GERENCIAL — {prato['nome']}",
            f"Gênero: {prato['genero_prato']}",
            f"Rendimento base: {rendimento:.0f} porções",
        ]
        if prato["tempo_preparo_min"]:
            conteudo_parts.append(f"Tempo de preparo: {prato['tempo_preparo_min']} minutos")

        conteudo_parts.append("\nINGREDIENTES:")
        conteudo_parts.extend(linhas_ingredientes if linhas_ingredientes else ["  (sem ingredientes cadastrados)"])

        conteudo_parts += [
            f"\nCUSTO TOTAL DE INGREDIENTES: R$ {custo_ingredientes:.2f}",
            f"Margem de desperdício ({margem:.1f}%): R$ {custo_ingredientes * margem / 100:.2f}",
            f"CUSTO TOTAL DA RECEITA: R$ {custo_total:.2f}",
            f"CMV por porção: R$ {cmv:.2f}",
            f"Custo de embalagem: R$ {embalagem:.2f}",
            f"CUSTO TOTAL DA PORÇÃO: R$ {cmv + embalagem:.2f}",
        ]
        if preco:
            conteudo_parts.append(f"Preço de venda praticado: R$ {preco:.2f}")
        if margem_lucro is not None:
            conteudo_parts.append(f"Margem de lucro bruta: {margem_lucro:.1f}%")
        if prato["instrucoes_apresentacao"]:
            conteudo_parts.append(f"\nAPRESENTAÇÃO: {prato['instrucoes_apresentacao']}")
        if prato["temperatura_servico"]:
            conteudo_parts.append(f"Temperatura de serviço: {prato['temperatura_servico']}")

        conteudo = "\n".join(conteudo_parts)
        titulo = f"Ficha Técnica — {prato['nome']}"

        doc_id = await inserir_documento(
            conn,
            titulo=titulo,
            conteudo=conteudo,
            tipo="FICHA_TECNICA",
            entidade_id=prato["id"],
            metadados={
                "prato_id": str(prato["id"]),
                "genero_prato": prato["genero_prato"],
                "rendimento": rendimento,
                "cmv_porcao": round(cmv, 4),
            },
        )
        logger.info("Ficha técnica inserida: %s (id=%s)", titulo, doc_id)
        count += 1

    return count


async def popular_regras_composicao(conn, limpar: bool = False) -> int:
    """Insere as regras de composição por gênero de refeição."""
    if limpar:
        await limpar_tipo(conn, "REGRAS_COMPOSICAO")

    regras = await conn.fetch(
        """SELECT genero_refeicao,
                  string_agg(genero_prato_obrigatorio, ', ' ORDER BY genero_prato_obrigatorio)
                      AS categorias
             FROM regras_composicao
            GROUP BY genero_refeicao
            ORDER BY genero_refeicao"""
    )

    count = 0
    for regra in regras:
        conteudo = (
            f"REGRAS DE COMPOSIÇÃO — {regra['genero_refeicao']}\n\n"
            f"O gênero de refeição '{regra['genero_refeicao']}' aceita os seguintes "
            f"gêneros de prato:\n{regra['categorias']}\n\n"
            f"Ao adicionar um prato a uma refeição do tipo '{regra['genero_refeicao']}', "
            f"o gênero do prato deve ser um dos listados acima. "
            f"Pratos com gênero diferente serão rejeitados com erro COMPOSICAO_INVALIDA."
        )
        titulo = f"Regras de Composição — {regra['genero_refeicao']}"
        doc_id = await inserir_documento(
            conn,
            titulo=titulo,
            conteudo=conteudo,
            tipo="REGRAS_COMPOSICAO",
            metadados={"genero_refeicao": regra["genero_refeicao"]},
        )
        logger.info("Regra de composição inserida: %s (id=%s)", titulo, doc_id)
        count += 1

    return count


async def popular_estilos_servico(conn, limpar: bool = False) -> int:
    """Insere os estilos de serviço como documentos RAG."""
    if limpar:
        await limpar_tipo(conn, "ESTILO_SERVICO")

    estilos = await conn.fetch(
        "SELECT id, nome, descricao, dinamica FROM estilos_servico ORDER BY nome"
    )

    count = 0
    for estilo in estilos:
        conteudo_parts = [f"ESTILO DE SERVIÇO — {estilo['nome']}"]
        if estilo["descricao"]:
            conteudo_parts.append(f"\nDescrição: {estilo['descricao']}")
        if estilo["dinamica"]:
            conteudo_parts.append(f"\nDinâmica operacional: {estilo['dinamica']}")

        conteudo = "\n".join(conteudo_parts)
        titulo = f"Estilo de Serviço — {estilo['nome']}"
        doc_id = await inserir_documento(
            conn,
            titulo=titulo,
            conteudo=conteudo,
            tipo="ESTILO_SERVICO",
            entidade_id=estilo["id"],
            metadados={"estilo_id": str(estilo["id"]), "nome": estilo["nome"]},
        )
        logger.info("Estilo de serviço inserido: %s (id=%s)", titulo, doc_id)
        count += 1

    return count


async def popular_glossario(conn, limpar: bool = False) -> int:
    """Insere o glossário de domínio como documento RAG."""
    if limpar:
        await limpar_tipo(conn, "GLOSSARIO")

    # Procura o glossário nos locais possíveis
    candidatos = [
        DOCS_DIR / "glossario-dominio.md",
        Path("/app/docs/glossario-dominio.md"),
        Path("docs/glossario-dominio.md"),
    ]
    glossario_path = None
    for c in candidatos:
        if c.exists():
            glossario_path = c
            break

    if not glossario_path:
        logger.warning(
            "Glossário não encontrado em: %s — pulando",
            [str(c) for c in candidatos],
        )
        return 0

    conteudo = glossario_path.read_text(encoding="utf-8")

    # Divide por seção (cada termo como documento separado para busca mais precisa)
    secoes = conteudo.split("\n### ")
    count = 0

    # Primeira seção é o cabeçalho — insere como documento único de contexto geral
    if secoes:
        cabecalho = secoes[0].strip()
        if cabecalho:
            doc_id = await inserir_documento(
                conn,
                titulo="Glossário de Domínio — Introdução",
                conteudo=cabecalho,
                tipo="GLOSSARIO",
                metadados={"secao": "introducao"},
            )
            logger.info("Glossário: introdução inserida (id=%s)", doc_id)
            count += 1

    # Demais seções são os termos
    for secao in secoes[1:]:
        linhas = secao.strip().splitlines()
        if not linhas:
            continue
        termo = linhas[0].strip()
        corpo = "\n".join(linhas[1:]).strip()
        if not corpo:
            continue

        conteudo_secao = f"GLOSSÁRIO — {termo}\n\n{corpo}"
        doc_id = await inserir_documento(
            conn,
            titulo=f"Glossário — {termo}",
            conteudo=conteudo_secao,
            tipo="GLOSSARIO",
            metadados={"termo": termo},
        )
        logger.info("Glossário: termo '%s' inserido (id=%s)", termo, doc_id)
        count += 1

    return count


async def popular_adrs(conn, limpar: bool = False) -> int:
    """Insere cada ADR como documento RAG."""
    if limpar:
        await limpar_tipo(conn, "ADR")

    # Procura a pasta de ADRs
    candidatos = [
        ADR_DIR,
        Path("/app/docs/adr"),
        Path("docs/adr"),
    ]
    adr_dir = None
    for c in candidatos:
        if c.exists() and c.is_dir():
            adr_dir = c
            break

    if not adr_dir:
        logger.warning(
            "Pasta de ADRs não encontrada em: %s — pulando",
            [str(c) for c in candidatos],
        )
        return 0

    adr_files = sorted(adr_dir.glob("*.md"))
    # Exclui o README
    adr_files = [f for f in adr_files if f.name != "README.md"]

    count = 0
    for adr_file in adr_files:
        conteudo = adr_file.read_text(encoding="utf-8")
        # Extrai o título da primeira linha (# ADR NNN — Título)
        primeira_linha = conteudo.splitlines()[0].strip()
        titulo = primeira_linha.lstrip("# ").strip()

        doc_id = await inserir_documento(
            conn,
            titulo=titulo,
            conteudo=conteudo,
            tipo="ADR",
            metadados={"arquivo": adr_file.name},
        )
        logger.info("ADR inserido: %s (id=%s)", titulo, doc_id)
        count += 1

    return count


# =====================================================================
# Main
# =====================================================================

POPULADORES = {
    "FICHA_TECNICA":     popular_fichas_tecnicas,
    "REGRAS_COMPOSICAO": popular_regras_composicao,
    "ESTILO_SERVICO":    popular_estilos_servico,
    "GLOSSARIO":         popular_glossario,
    "ADR":               popular_adrs,
}


async def main(tipos: list[str], limpar: bool) -> None:
    logger.info("Iniciando população do RAG (tipos=%s, limpar=%s)", tipos, limpar)

    # Verifica se UNIQUE constraint (titulo, tipo) existe — necessária para upsert
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        constraint = await conn.fetchval(
            """SELECT constraint_name FROM information_schema.table_constraints
               WHERE table_name = 'documentos'
                 AND constraint_type = 'UNIQUE'
                 AND constraint_name LIKE '%titulo%tipo%'
               LIMIT 1"""
        )
        if not constraint:
            logger.info("Criando índice UNIQUE (titulo, tipo) em documentos...")
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_documentos_titulo_tipo "
                "ON documentos (titulo, tipo)"
            )
            logger.info("Índice criado.")
    finally:
        await conn.close()

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)
    total = 0
    try:
        async with pool.acquire() as conn:
            for tipo in tipos:
                if tipo not in POPULADORES:
                    logger.warning("Tipo desconhecido: %s — ignorado", tipo)
                    continue
                logger.info("--- Processando tipo: %s ---", tipo)
                n = await POPULADORES[tipo](conn, limpar=limpar)
                logger.info("Tipo %s: %d documentos inseridos/atualizados", tipo, n)
                total += n
    finally:
        await pool.close()

    logger.info("=== População concluída: %d documentos no total ===", total)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Popula a base RAG com documentos do domínio Dono")
    parser.add_argument(
        "--limpar",
        action="store_true",
        help="Remove documentos existentes do tipo antes de reinserir",
    )
    parser.add_argument(
        "--tipos",
        default=",".join(POPULADORES.keys()),
        help=f"Tipos a processar, separados por vírgula. Padrão: todos. "
             f"Disponíveis: {', '.join(POPULADORES.keys())}",
    )
    args = parser.parse_args()
    tipos = [t.strip().upper() for t in args.tipos.split(",") if t.strip()]

    if not DATABASE_URL:
        logger.error("DATABASE_URL não definida no ambiente")
        sys.exit(1)

    asyncio.run(main(tipos=tipos, limpar=args.limpar))
