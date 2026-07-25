# backend/app/routes/relatorios.py — Sistema Dono
#
# Relatórios gerenciais. Agora com suporte a projeções (Read Models) para
# consultas de alto desempenho. O endpoint /ruptura-estoque utiliza a
# tabela materializada projecao_estoque_atual (populada via event_store).
#
# ATUALIZAÇÃO (2026-07-24):
#   - /curva-abc: ordem de parâmetros corrigida (id antes de escopo).
#   - /ruptura-estoque: query de lotes vencendo corrigida (uso de $1 * INTERVAL).
#   - /consumo: implementado completamente (consumo + perdas).
#   - Todos os Query usam pattern em vez de regex (depreciação).
#   - Adicionadas importações de Response e asyncpg (para consistência).

import asyncpg
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.exportacao import gerar_pdf_relatorio, gerar_xlsx_relatorio, slugificar

router = APIRouter(prefix="/relatorios", tags=["relatorios"])

# ---------- Helpers de exportação ----------

def _secao_relatorio(titulo: str, colunas, linhas):
    return {"titulo": titulo, "colunas": colunas, "linhas": linhas}


def _slug_titulo(titulo: str) -> str:
    return slugificar(titulo)


# ---------- 1. Curva ABC ----------
@router.get("/curva-abc")
async def curva_abc(
    id: uuid.UUID,  # <-- MOVIDO PARA ANTES do escopo (sem padrão primeiro)
    escopo: str = Query(..., pattern="^(INSUMO_GENERO|PRATO|REFEICAO|MENU)$"),
    formato: Optional[str] = Query("json", pattern="^(json|pdf|xlsx)$"),
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO", "COMPRAS"))
):
    """
    Classificação ABC para o escopo informado.
    Lê diretamente da tabela materializada `classificacoes_abc`.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT item_id, custo, percentual_acumulado, classe
               FROM classificacoes_abc
               WHERE escopo_tipo = $1 AND escopo_id_pai = $2
               ORDER BY percentual_acumulado""",
            escopo, id
        )
        if not rows:
            raise HTTPException(404, detail=error_detail("ABC_NAO_CALCULADO", "Nenhuma classificação ABC encontrada para este escopo"))

        data = [dict(r) for r in rows]

        if formato == "json":
            return data

        titulo = f"Curva ABC - {escopo}"
        secoes = [_secao_relatorio(
            titulo,
            [("Item ID", "item_id"), ("Custo", "custo"), ("% Acumulado", "percentual_acumulado"), ("Classe", "classe")],
            data
        )]

        if formato == "pdf":
            return Response(
                gerar_pdf_relatorio(titulo, secoes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.pdf"}
            )
        else:  # xlsx
            return Response(
                gerar_xlsx_relatorio(titulo, secoes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.xlsx"}
            )


# ---------- 2. MRP (Previsão de Compras) ----------
@router.get("/mrp")
async def mrp(
    data_limite: date = Query(..., description="Data limite para análise (ex.: data do evento mais distante)"),
    formato: Optional[str] = Query("json", pattern="^(json|pdf|xlsx)$"),
    current_user: dict = Depends(require_perfil("ADMIN", "COMPRAS"))
):
    """
    MRP – Material Requirements Planning.
    Executa a função fn_mrp_previsao_compras() e retorna a lista de compras sugeridas.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM fn_mrp_previsao_compras($1)",
            data_limite
        )
        data = [dict(r) for r in rows]

        if formato == "json":
            return data

        titulo = f"MRP - Necessidades de Compra até {data_limite.isoformat()}"
        secoes = [_secao_relatorio(
            titulo,
            [("Insumo", "insumo_nome"), ("Unidade", "unidade"),
             ("Necessidade Bruta", "necessidade_bruta"), ("Estoque", "estoque_disponivel"),
             ("Necessidade Líquida", "necessidade_liquida"), ("Classe ABC", "classe_abc"),
             ("Fornecedor Sugerido", "fornecedor_sugerido")],
            data
        )]

        if formato == "pdf":
            return Response(
                gerar_pdf_relatorio(titulo, secoes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.pdf"}
            )
        else:
            return Response(
                gerar_xlsx_relatorio(titulo, secoes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.xlsx"}
            )


# ---------- 3. Ruptura de Estoque (USANDO PROJEÇÃO) ----------
@router.get("/ruptura-estoque")
async def ruptura_estoque(
    dias: int = Query(7, ge=1, le=90, description="Dias para frente para verificar lotes vencendo"),
    formato: Optional[str] = Query("json", pattern="^(json|pdf|xlsx)$"),
    current_user: dict = Depends(require_perfil("ADMIN", "COMPRAS"))
):
    """
    Relatório de ruptura de estoque, utilizando a projeção materializada
    `projecao_estoque_atual` para consultas rápidas.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Insumos com saldo zero (ruptura total)
        insumos_zerados = await conn.fetch(
            """
            SELECT 
                i.id AS insumo_id,
                i.nome AS insumo_nome,
                i.unidade,
                c.nome AS categoria_nome
            FROM projecao_estoque_atual p
            JOIN insumos i ON i.id = p.insumo_id
            JOIN categorias c ON c.id = i.categoria_id
            WHERE p.saldo_atual = 0
            GROUP BY i.id, i.nome, i.unidade, c.nome
            ORDER BY i.nome
            """
        )

        # 2. Lotes vencendo nos próximos N dias (com saldo disponível)
        # CORREÇÃO: usar $1 * INTERVAL '1 day' para evitar erro de tipo
        lotes_vencendo = await conn.fetch(
            """
            SELECT 
                l.id AS lote_id,
                l.insumo_id,
                i.nome AS insumo_nome,
                l.data_validade,
                l.quantidade_disponivel,
                p.saldo_atual AS saldo_atual_lote
            FROM lotes_insumo l
            JOIN insumos i ON i.id = l.insumo_id
            JOIN projecao_estoque_atual p ON p.lote_insumo_id = l.id
            WHERE l.data_validade IS NOT NULL
              AND l.data_validade BETWEEN CURRENT_DATE AND CURRENT_DATE + ($1 * INTERVAL '1 day')
              AND l.quantidade_disponivel > 0
            ORDER BY l.data_validade
            """,
            dias
        )

        dados = {
            "insumos_zerados": [dict(r) for r in insumos_zerados],
            "lotes_vencendo": [dict(r) for r in lotes_vencendo]
        }

        if formato == "json":
            return dados

        titulo = f"Ruptura de Estoque ({dias} dias)"
        secoes = [
            _secao_relatorio(
                "Insumos com Estoque Zerado",
                [("Insumo", "insumo_nome"), ("Unidade", "unidade"), ("Categoria", "categoria_nome")],
                dados["insumos_zerados"]
            ),
            _secao_relatorio(
                f"Lotes Vencendo em {dias} dias",
                [("Insumo", "insumo_nome"), ("Validade", "data_validade"),
                 ("Disponível", "quantidade_disponivel"), ("Saldo do Lote", "saldo_atual_lote")],
                dados["lotes_vencendo"]
            )
        ]

        if formato == "pdf":
            return Response(
                gerar_pdf_relatorio(titulo, secoes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.pdf"}
            )
        else:
            return Response(
                gerar_xlsx_relatorio(titulo, secoes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.xlsx"}
            )


# ---------- 4. Consumo Real e Perdas ----------
@router.get("/consumo")
async def consumo(
    categoria_id: Optional[uuid.UUID] = None,
    periodo_inicio: Optional[date] = None,
    periodo_fim: Optional[date] = None,
    formato: Optional[str] = Query("json", pattern="^(json|pdf|xlsx)$"),
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """
    Relatório de consumo real (BAIXA_EXECUCAO) e perdas (AJUSTE_MANUAL)
    no período especificado. Se nenhum período for informado, considera
    todo o histórico.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Monta a cláusula WHERE
        where_parts = []
        params = []
        idx = 1

        if categoria_id:
            where_parts.append("m.insumo_id IN (SELECT id FROM insumos WHERE categoria_id = $1)")
            params.append(categoria_id)
            idx += 1
        if periodo_inicio:
            where_parts.append("DATE(m.criado_em) >= $" + str(idx))
            params.append(periodo_inicio)
            idx += 1
        if periodo_fim:
            where_parts.append("DATE(m.criado_em) <= $" + str(idx))
            params.append(periodo_fim)
            idx += 1

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"

        # 1. Consumo (BAIXA_EXECUCAO)
        consumo_rows = await conn.fetch(
            f"""
            SELECT 
                i.id AS insumo_id,
                i.nome AS insumo_nome,
                i.unidade,
                SUM(m.quantidade) AS quantidade_consumida
            FROM movimentacoes_estoque m
            JOIN insumos i ON i.id = m.insumo_id
            WHERE m.tipo = 'BAIXA_EXECUCAO' AND {where_clause}
            GROUP BY i.id, i.nome, i.unidade
            ORDER BY i.nome
            """,
            *params
        )

        # 2. Perdas (AJUSTE_MANUAL com tipo_perda_id)
        perdas_rows = await conn.fetch(
            f"""
            SELECT 
                i.id AS insumo_id,
                i.nome AS insumo_nome,
                i.unidade,
                tp.nome AS tipo_perda_nome,
                SUM(m.quantidade) AS quantidade_perdida
            FROM movimentacoes_estoque m
            JOIN insumos i ON i.id = m.insumo_id
            LEFT JOIN tipos_perda tp ON tp.id = m.tipo_perda_id
            WHERE m.tipo = 'AJUSTE_MANUAL' AND {where_clause}
            GROUP BY i.id, i.nome, i.unidade, tp.nome
            ORDER BY i.nome, tp.nome
            """,
            *params
        )

        resultado = {
            "consumo": [dict(r) for r in consumo_rows],
            "perdas": [dict(r) for r in perdas_rows]
        }

        if formato == "json":
            return resultado

        # Para exportação, transformamos em seções tabulares
        titulo = "Relatório de Consumo e Perdas"
        secoes = [
            _secao_relatorio(
                "Consumo (BAIXA_EXECUCAO)",
                [("Insumo", "insumo_nome"), ("Unidade", "unidade"), ("Quantidade", "quantidade_consumida")],
                resultado["consumo"]
            ),
            _secao_relatorio(
                "Perdas (AJUSTE_MANUAL)",
                [("Insumo", "insumo_nome"), ("Unidade", "unidade"), ("Tipo de Perda", "tipo_perda_nome"), ("Quantidade", "quantidade_perdida")],
                resultado["perdas"]
            )
        ]

        if formato == "pdf":
            return Response(
                gerar_pdf_relatorio(titulo, secoes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.pdf"}
            )
        else:
            return Response(
                gerar_xlsx_relatorio(titulo, secoes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.xlsx"}
            )


# ---------- 5. Margem de Menu ----------
@router.get("/margem-menu/{menu_id}")
async def margem_menu(
    menu_id: uuid.UUID,
    formato: Optional[str] = Query("json", pattern="^(json|pdf|xlsx)$"),
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """
    Relatório de margem de contribuição de um menu: custo total (snapshots)
    vs. preço de venda, segregado por refeição.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Verifica se o menu existe
        menu = await conn.fetchrow(
            "SELECT id, nome_evento FROM menus WHERE id = $1",
            menu_id
        )
        if not menu:
            raise HTTPException(404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Menu não encontrado"))

        # Busca os itens do menu com custo snapshot e preços de venda dos pratos
        rows = await conn.fetch(
            """
            SELECT 
                im.id AS item_menu_id,
                im.refeicao_id,
                r.genero_refeicao,
                im.custo_snapshot AS custo_total_refeicao,
                COALESCE((
                    SELECT SUM(p.preco_venda_praticado * ir.custo_snapshot / NULLIF(r2.qtd_pessoas, 0))
                    FROM itens_refeicao ir
                    JOIN pratos p ON p.id = ir.prato_id
                    WHERE ir.refeicao_id = im.refeicao_id
                ), 0) AS receita_total_estimada
            FROM itens_menu im
            JOIN refeicoes r ON r.id = im.refeicao_id
            LEFT JOIN itens_refeicao ir ON ir.refeicao_id = im.refeicao_id
            LEFT JOIN pratos p ON p.id = ir.prato_id
            WHERE im.menu_id = $1
            GROUP BY im.id, im.refeicao_id, r.genero_refeicao, im.custo_snapshot
            ORDER BY im.ordem_cronologica
            """,
            menu_id
        )

        if not rows:
            raise HTTPException(404, detail=error_detail("MENU_SEM_ITENS", "Este menu não possui itens ou nenhum custo snapshot foi congelado"))

        dados = {
            "menu_id": str(menu_id),
            "nome_evento": menu["nome_evento"],
            "itens": [dict(r) for r in rows]
        }

        if formato == "json":
            return dados

        # Exportação
        titulo = f"Margem de Contribuição - {menu['nome_evento']}"
        secoes = [
            _secao_relatorio(
                "Itens do Menu",
                [("Refeição", "genero_refeicao"), ("Custo Total (R$)", "custo_total_refeicao"),
                 ("Receita Estimada (R$)", "receita_total_estimada"),
                 ("Margem (R$)", lambda row: row["receita_total_estimada"] - row["custo_total_refeicao"])],
                dados["itens"]
            )
        ]

        if formato == "pdf":
            return Response(
                gerar_pdf_relatorio(titulo, secoes),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.pdf"}
            )
        else:
            return Response(
                gerar_xlsx_relatorio(titulo, secoes),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={_slug_titulo(titulo)}.xlsx"}
            )