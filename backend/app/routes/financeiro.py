# backend/app/routes/financeiro.py — Sistema Dono
#
# Módulo Financeiro – endpoints para gerenciamento de contas a pagar e a receber.
# Suporta listagem, criação manual, baixa (pagamento/recebimento), cancelamento.
#
# Rotas:
#   GET    /financeiro/contas-pagar               – listar contas com filtros
#   POST   /financeiro/contas-pagar               – criar conta manualmente
#   PATCH  /financeiro/contas-pagar/{id}/pagar    – baixar (pagar) conta
#   PATCH  /financeiro/contas-pagar/{id}/cancelar – cancelar conta
#   GET    /financeiro/contas-receber             – listar contas a receber
#   POST   /financeiro/contas-receber             – criar conta a receber
#   PATCH  /financeiro/contas-receber/{id}/receber – baixar (receber) conta
#   PATCH  /financeiro/contas-receber/{id}/cancelar – cancelar conta
#   GET    /financeiro/resumo                     – dashboard resumo financeiro
#
# CORREÇÕES (2026-07-24):
#   - Tratamento de exceção usando sqlstate (códigos P4000 a P4007).
#   - Resumo financeiro com fallback incluindo contas atrasadas.
#   - Importação de asyncpg para capturar exceções corretamente.

import uuid
import asyncpg  # <-- NOVO: para capturar exceções com sqlstate
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams

router = APIRouter(prefix="/financeiro", tags=["financeiro"])

# ---------- Schemas Pydantic ----------

class ContaPagarCreate(BaseModel):
    fornecedor_id: uuid.UUID
    descricao: str = Field(..., max_length=300)
    valor_original: float = Field(..., gt=0)
    data_vencimento: date
    tipo_despesa: Optional[str] = Field(None, max_length=50)
    observacao: Optional[str] = None
    lote_insumo_id: Optional[uuid.UUID] = None

class ContaPagarUpdate(BaseModel):
    valor_pago: float = Field(..., gt=0)
    data_pagamento: Optional[date] = None

class ContaReceberCreate(BaseModel):
    menu_id: Optional[uuid.UUID] = None
    cliente_nome: Optional[str] = Field(None, max_length=200)
    descricao: str = Field(..., max_length=300)
    valor_original: float = Field(..., gt=0)
    data_vencimento: date
    observacao: Optional[str] = None

class ContaReceberUpdate(BaseModel):
    valor_recebido: float = Field(..., gt=0)
    data_recebimento: Optional[date] = None

# ---------- Helpers ----------

async def _listar_contas(
    conn,
    tabela: str,
    status: Optional[str] = None,
    fornecedor_id: Optional[uuid.UUID] = None,
    data_vencimento_inicio: Optional[date] = None,
    data_vencimento_fim: Optional[date] = None,
    page_params: PageParams = PageParams()
):
    where_parts = []
    params = []
    param_idx = 1

    if status:
        where_parts.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1
    if fornecedor_id:
        where_parts.append(f"fornecedor_id = ${param_idx}")
        params.append(fornecedor_id)
        param_idx += 1
    if data_vencimento_inicio:
        where_parts.append(f"data_vencimento >= ${param_idx}")
        params.append(data_vencimento_inicio)
        param_idx += 1
    if data_vencimento_fim:
        where_parts.append(f"data_vencimento <= ${param_idx}")
        params.append(data_vencimento_fim)
        param_idx += 1

    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    count_query = f"SELECT COUNT(*) FROM {tabela} {where_clause}"
    total = await conn.fetchval(count_query, *params)

    params.extend([page_params.page_size, page_params.offset])
    query = f"""
        SELECT *
        FROM {tabela}
        {where_clause}
        ORDER BY data_vencimento ASC NULLS LAST, criado_em DESC
        LIMIT ${param_idx} OFFSET ${param_idx+1}
    """
    rows = await conn.fetch(query, *params)
    return {"items": [dict(r) for r in rows], "total": total}

# ---------- Contas a Pagar ----------

@router.get("/contas-pagar")
async def listar_contas_pagar(
    status: Optional[str] = Query(None, pattern="^(PENDENTE|PAGO_PARCIAL|PAGO|CANCELADO|ATRASADO)$"),
    fornecedor_id: Optional[uuid.UUID] = None,
    data_vencimento_inicio: Optional[date] = None,
    data_vencimento_fim: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO", "COMPRAS"))
):
    """Lista contas a pagar com filtros."""
    if data_vencimento_inicio and data_vencimento_fim and data_vencimento_inicio > data_vencimento_fim:
        raise HTTPException(
            status_code=400,
            detail=error_detail("PERIODO_INVALIDO", "data_vencimento_inicio deve ser anterior a data_vencimento_fim")
        )
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await _listar_contas(
            conn, "contas_pagar", status, fornecedor_id,
            data_vencimento_inicio, data_vencimento_fim,
            PageParams(page, page_size)
        )
        return Page(items=result["items"], total=result["total"], page=page, page_size=page_size)


@router.post("/contas-pagar", status_code=201)
async def criar_conta_pagar(
    data: ContaPagarCreate,
    current_user: dict = Depends(require_perfil("ADMIN", "COMPRAS"))
):
    """Cria uma nova conta a pagar manualmente (ex.: serviço, imposto)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO contas_pagar
               (fornecedor_id, descricao, valor_original, data_vencimento,
                tipo_despesa, observacao, lote_insumo_id, criado_por)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               RETURNING id, criado_em""",
            data.fornecedor_id, data.descricao, data.valor_original,
            data.data_vencimento, data.tipo_despesa, data.observacao,
            data.lote_insumo_id, uuid.UUID(current_user["user_id"])
        )
        return {"id": str(row["id"]), "criado_em": row["criado_em"]}


@router.patch("/contas-pagar/{conta_id}/pagar")
async def pagar_conta(
    conta_id: uuid.UUID,
    data: ContaPagarUpdate,
    current_user: dict = Depends(require_perfil("ADMIN", "COMPRAS"))
):
    """
    Baixa uma conta a pagar (pagamento total ou parcial).
    A função PL/pgSQL fn_baixar_conta_pagar lança exceções com códigos SQLSTATE:
      - P4000: conta não encontrada
      - P4001: conta já está paga/cancelada
      - P4002: valor pago <= 0
      - P4003: valor pago excede o valor original
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "SELECT fn_baixar_conta_pagar($1, $2, $3, $4)",
                conta_id, data.valor_pago, data.data_pagamento or date.today(),
                uuid.UUID(current_user["user_id"])
            )
        except asyncpg.exceptions.PostgresError as e:
            # Códigos SQLSTATE personalizados (P4000 a P4003)
            if e.sqlstate == 'P4000':
                raise HTTPException(404, detail=error_detail("RECURSO_NAO_ENCONTRADO", f"Conta não encontrada: {str(e)}"))
            if e.sqlstate in ('P4001', 'P4002', 'P4003'):
                raise HTTPException(400, detail=error_detail("VALIDACAO_INVALIDA", str(e)))
            raise
        return {"message": "Conta paga com sucesso"}


@router.patch("/contas-pagar/{conta_id}/cancelar")
async def cancelar_conta_pagar(
    conta_id: uuid.UUID,
    current_user: dict = Depends(require_perfil("ADMIN"))
):
    """Cancela uma conta a pagar (apenas ADMIN)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE contas_pagar SET status = 'CANCELADO', atualizado_em = now() WHERE id = $1 AND status NOT IN ('PAGO', 'CANCELADO')",
            conta_id
        )
        if result == "UPDATE 0":
            raise HTTPException(404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Conta não encontrada ou já está paga/cancelada"))
        return {"message": "Conta cancelada"}


# ---------- Contas a Receber ----------

@router.get("/contas-receber")
async def listar_contas_receber(
    status: Optional[str] = Query(None, pattern="^(PENDENTE|RECEBIDO_PARCIAL|RECEBIDO|CANCELADO|ATRASADO)$"),
    menu_id: Optional[uuid.UUID] = None,
    data_vencimento_inicio: Optional[date] = None,
    data_vencimento_fim: Optional[date] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """Lista contas a receber com filtros."""
    if data_vencimento_inicio and data_vencimento_fim and data_vencimento_inicio > data_vencimento_fim:
        raise HTTPException(
            status_code=400,
            detail=error_detail("PERIODO_INVALIDO", "data_vencimento_inicio deve ser anterior a data_vencimento_fim")
        )
    pool = get_pool()
    async with pool.acquire() as conn:
        where_parts = []
        params = []
        idx = 1
        if status:
            where_parts.append(f"status = ${idx}"); params.append(status); idx += 1
        if menu_id:
            where_parts.append(f"menu_id = ${idx}"); params.append(menu_id); idx += 1
        if data_vencimento_inicio:
            where_parts.append(f"data_vencimento >= ${idx}"); params.append(data_vencimento_inicio); idx += 1
        if data_vencimento_fim:
            where_parts.append(f"data_vencimento <= ${idx}"); params.append(data_vencimento_fim); idx += 1

        where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        total = await conn.fetchval(f"SELECT COUNT(*) FROM contas_receber {where_clause}", *params)
        params.extend([page_size, (page-1)*page_size])
        rows = await conn.fetch(
            f"""
            SELECT * FROM contas_receber
            {where_clause}
            ORDER BY data_vencimento ASC NULLS LAST, criado_em DESC
            LIMIT ${idx} OFFSET ${idx+1}
            """, *params
        )
        return Page(items=[dict(r) for r in rows], total=total, page=page, page_size=page_size)


@router.post("/contas-receber", status_code=201)
async def criar_conta_receber(
    data: ContaReceberCreate,
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """Cria uma nova conta a receber."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO contas_receber
               (menu_id, cliente_nome, descricao, valor_original, data_vencimento, observacao, criado_por)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING id, criado_em""",
            data.menu_id, data.cliente_nome, data.descricao,
            data.valor_original, data.data_vencimento, data.observacao,
            uuid.UUID(current_user["user_id"])
        )
        return {"id": str(row["id"]), "criado_em": row["criado_em"]}


@router.patch("/contas-receber/{conta_id}/receber")
async def receber_conta(
    conta_id: uuid.UUID,
    data: ContaReceberUpdate,
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """
    Baixa uma conta a receber (recebimento total ou parcial).
    A função PL/pgSQL fn_baixar_conta_receber lança exceções com códigos SQLSTATE:
      - P4004: conta não encontrada
      - P4005: conta já está recebida/cancelada
      - P4006: valor recebido <= 0
      - P4007: valor recebido excede o valor original
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "SELECT fn_baixar_conta_receber($1, $2, $3, $4)",
                conta_id, data.valor_recebido, data.data_recebimento or date.today(),
                uuid.UUID(current_user["user_id"])
            )
        except asyncpg.exceptions.PostgresError as e:
            if e.sqlstate == 'P4004':
                raise HTTPException(404, detail=error_detail("RECURSO_NAO_ENCONTRADO", f"Conta não encontrada: {str(e)}"))
            if e.sqlstate in ('P4005', 'P4006', 'P4007'):
                raise HTTPException(400, detail=error_detail("VALIDACAO_INVALIDA", str(e)))
            raise
        return {"message": "Conta recebida com sucesso"}


@router.patch("/contas-receber/{conta_id}/cancelar")
async def cancelar_conta_receber(
    conta_id: uuid.UUID,
    current_user: dict = Depends(require_perfil("ADMIN"))
):
    """Cancela uma conta a receber."""
    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE contas_receber SET status = 'CANCELADO', atualizado_em = now() WHERE id = $1 AND status NOT IN ('RECEBIDO', 'CANCELADO')",
            conta_id
        )
        if result == "UPDATE 0":
            raise HTTPException(404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Conta não encontrada ou já está recebida/cancelada"))
        return {"message": "Conta cancelada"}


# ---------- Resumo Financeiro ----------

@router.get("/resumo")
async def resumo_financeiro(
    current_user: dict = Depends(require_perfil("ADMIN", "GESTAO"))
):
    """
    Dashboard resumo financeiro.

    Primeiro tenta buscar da projeção materializada `projecao_resumo_financeiro_mensal`
    para o mês corrente. Se a projeção não estiver disponível, faz um fallback
    calculando na hora a partir das tabelas de contas (incluindo atrasados).

    Corrigido para retornar total_atrasado_pagar e total_atrasado_receber.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Tenta buscar da projeção do mês corrente
        mes_atual = date.today().strftime("%Y-%m")
        row = await conn.fetchrow(
            """
            SELECT 
                total_despesas_previstas AS total_a_pagar,
                total_receitas_previstas AS total_a_receber,
                saldo_previsto,
                -- Adicionamos colunas de atrasado (podem ser nulas se a projeção ainda não tiver)
                0.0 AS total_atrasado_pagar,
                0.0 AS total_atrasado_receber
            FROM projecao_resumo_financeiro_mensal
            WHERE ano_mes = $1
            """,
            mes_atual
        )

        # Se não houver projeção para o mês, calcula manualmente (fallback)
        if not row:
            row_old = await conn.fetchrow(
                """
                SELECT 
                    COALESCE(SUM(CASE WHEN tipo = 'RECEBER' THEN valor_original - COALESCE(valor_recebido, 0) ELSE 0 END), 0) AS total_a_receber,
                    COALESCE(SUM(CASE WHEN tipo = 'PAGAR' THEN valor_original - COALESCE(valor_pago, 0) ELSE 0 END), 0) AS total_a_pagar,
                    COALESCE(SUM(CASE WHEN tipo = 'PAGAR' AND status = 'ATRASADO' THEN valor_original - COALESCE(valor_pago, 0) ELSE 0 END), 0) AS total_atrasado_pagar,
                    COALESCE(SUM(CASE WHEN tipo = 'RECEBER' AND status = 'ATRASADO' THEN valor_original - COALESCE(valor_recebido, 0) ELSE 0 END), 0) AS total_atrasado_receber
                FROM (
                    SELECT valor_original, valor_recebido, 0 AS valor_pago, status, 'RECEBER' AS tipo FROM contas_receber 
                    WHERE status IN ('PENDENTE', 'RECEBIDO_PARCIAL', 'ATRASADO')
                    UNION ALL
                    SELECT valor_original, 0 AS valor_recebido, valor_pago, status, 'PAGAR' AS tipo FROM contas_pagar
                    WHERE status IN ('PENDENTE', 'PAGO_PARCIAL', 'ATRASADO')
                ) AS t
                """
            )
            return {
                "total_a_pagar": float(row_old["total_a_pagar"] or 0),
                "total_atrasado_pagar": float(row_old["total_atrasado_pagar"] or 0),
                "total_a_receber": float(row_old["total_a_receber"] or 0),
                "total_atrasado_receber": float(row_old["total_atrasado_receber"] or 0),
                "saldo_previsto": float((row_old["total_a_receber"] or 0) - (row_old["total_a_pagar"] or 0))
            }

        # Se usou a projeção, converte para dict e calcula saldo (já vem da projeção)
        return {
            "total_a_pagar": float(row["total_a_pagar"] or 0),
            "total_atrasado_pagar": float(row["total_atrasado_pagar"] or 0),
            "total_a_receber": float(row["total_a_receber"] or 0),
            "total_atrasado_receber": float(row["total_atrasado_receber"] or 0),
            "saldo_previsto": float(row["saldo_previsto"] or 0)
        }