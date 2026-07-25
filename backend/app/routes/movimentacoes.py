# backend/app/routes/movimentacoes.py — Sistema Dono
#
# Rotas para gestão de movimentações de estoque, com foco em perdas e ajustes:
#   - Listar tipos de perda disponíveis
#   - Registrar perdas/ajustes (com aplicação FEFO ou lote específico)
#   - Consultar histórico de movimentações com filtros
#
# ATUALIZAÇÃO (Tabela de Perdas e Ajustes):
#   - Módulo novo criado para suportar registro granular de perdas
#   - As perdas são registradas como movimentações do tipo 'AJUSTE_MANUAL'
#     com referência a um tipo de perda (tabela tipos_perda) e observação.
#   - A auditoria (usuario_id, ip_origem, user_agent) é passada explicitamente
#     como parâmetros para a função PL/pgSQL fn_registrar_perda, garantindo
#     que seja preenchida independentemente da conexão utilizada pelo pool.
#   - Permissões: COMPRAS e ADMIN podem registrar perdas; todos autenticados
#     podem consultar (filtrado por perfil via middleware).
#
# IMPORTANTE: Este módulo depende das funções PL/pgSQL:
#   - fn_registrar_perda (business-queries.sql)
#   - fn_listar_tipos_perda (business-queries.sql)

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams

router = APIRouter(prefix="/movimentacoes", tags=["movimentacoes"])


# ---------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------

class TipoPerdaOut(BaseModel):
    """Saída para listagem de tipos de perda."""
    id: str
    nome: str
    descricao: Optional[str] = None


class RegistrarPerdaRequest(BaseModel):
    """Payload para registrar uma perda/ajuste."""
    insumo_id: str = Field(..., description="ID do insumo afetado")
    quantidade: Decimal = Field(..., gt=0, description="Quantidade a ser baixada (positiva)")
    tipo_perda: str = Field(..., description="Nome do tipo de perda (deve existir em tipos_perda)")
    observacao: Optional[str] = Field(None, max_length=500, description="Detalhamento adicional")
    lote_id: Optional[str] = Field(None, description="ID do lote específico (opcional; se não informado, usa FEFO)")


class MovimentacaoOut(BaseModel):
    """Saída para listagem de movimentações."""
    id: str
    lote_insumo_id: str
    insumo_id: str
    insumo_nome: Optional[str] = None
    refeicao_id: Optional[str] = None
    quantidade: float
    tipo: str
    criado_em: datetime
    usuario_id: Optional[str] = None
    ip_origem: Optional[str] = None
    tipo_perda_id: Optional[str] = None
    tipo_perda_nome: Optional[str] = None
    observacao: Optional[str] = None


# ---------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------

@router.get("/tipos-perda", response_model=list[TipoPerdaOut])
async def listar_tipos_perda(
    _: dict = Depends(require_perfil("CHEF", "COMPRAS", "GESTAO", "ADMIN"))
):
    """Lista todos os tipos de perda ativos cadastrados no sistema.
    Acesso permitido a todos os perfis autenticados, pois é informação de catálogo."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM fn_listar_tipos_perda()")
        return [
            TipoPerdaOut(
                id=str(r["id"]),
                nome=r["nome"],
                descricao=r["descricao"]
            ) for r in rows
        ]


@router.post("/perda", status_code=201)
async def registrar_perda(
    body: RegistrarPerdaRequest,
    request: Request,
    current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN"))
):
    """Registra uma perda ou ajuste manual de estoque.
    
    Comportamento:
    - Se `lote_id` for informado: debita apenas daquele lote específico.
    - Se `lote_id` for omitido: aplica a baixa seguindo a ordem FEFO
      (lotes que vencem primeiro são consumidos primeiro).
    
    A auditoria (usuario_id, ip_origem, user_agent) é passada explicitamente
    para a função PL/pgSQL fn_registrar_perda, garantindo o preenchimento
    independentemente da conexão utilizada pelo pool.
    
    Possíveis erros:
    - 400: quantidade inválida (≤ 0) ou tipo_perda inexistente/inativo
    - 404: insumo ou lote não encontrado
    - 422: estoque insuficiente para atender à perda solicitada
    - 403: perfil sem permissão (apenas COMPRAS e ADMIN)
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            # Converte UUIDs para o tipo correto
            insumo_uuid = uuid.UUID(body.insumo_id)
            lote_uuid = uuid.UUID(body.lote_id) if body.lote_id else None

            # Extrai IP e user-agent da requisição
            ip_origem = request.client.host if request.client else None
            user_agent = request.headers.get("user-agent")

            # Chama a função PL/pgSQL que registra a perda, passando todos os parâmetros de auditoria
            await conn.execute(
                """
                SELECT fn_registrar_perda(
                    $1, $2, $3, $4, $5, $6, $7, $8
                )
                """,
                insumo_uuid,
                body.quantidade,
                body.tipo_perda,
                body.observacao,
                lote_uuid,
                uuid.UUID(current_user["user_id"]),
                ip_origem,
                user_agent,
            )
            return {"message": "Perda registrada com sucesso"}

        except asyncpg.PostgresError as e:
            # Mapeia os códigos de erro personalizados para respostas HTTP
            if e.sqlstate == "P2000":
                raise HTTPException(
                    status_code=400,
                    detail=error_detail("VALIDACAO_INVALIDA", str(e))
                )
            if e.sqlstate == "P2001":
                raise HTTPException(
                    status_code=400,
                    detail=error_detail("TIPO_PERDA_INVALIDO", str(e))
                )
            if e.sqlstate == "P2002":
                raise HTTPException(
                    status_code=404,
                    detail=error_detail("RECURSO_NAO_ENCONTRADO", str(e))
                )
            if e.sqlstate == "P2003":
                raise HTTPException(
                    status_code=422,
                    detail=error_detail("ESTOQUE_INSUFICIENTE", str(e))
                )
            # Erro inesperado
            raise HTTPException(
                status_code=500,
                detail=error_detail("ERRO_INTERNO", f"Erro ao registrar perda: {str(e)}")
            )
        except (ValueError, TypeError) as e:
            # UUID malformado
            raise HTTPException(
                status_code=400,
                detail=error_detail("VALIDACAO_INVALIDA", f"ID inválido: {str(e)}")
            )


@router.get("", response_model=Page)
async def listar_movimentacoes(
    insumo_id: Optional[str] = None,
    tipo: Optional[str] = None,
    tipo_perda: Optional[str] = None,
    periodo_inicio: Optional[date] = None,
    periodo_fim: Optional[date] = None,
    pag: PageParams = Depends(),
    _: dict = Depends(require_perfil("CHEF", "COMPRAS", "GESTAO", "ADMIN"))
):
    """Lista o histórico de movimentações de estoque com filtros opcionais.
    
    Filtros disponíveis:
    - insumo_id: UUID do insumo
    - tipo: BAIXA_EXECUCAO | ESTORNO_CANCELAMENTO | AJUSTE_MANUAL
    - tipo_perda: nome do tipo de perda (ex.: VALIDADE, QUEBRA) — só para AJUSTE_MANUAL
    - periodo_inicio / periodo_fim: intervalo de datas de criação
    
    Resultados paginados.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # Constroi a query com filtros dinâmicos
        where_clauses = []
        params = []
        param_index = 1

        if insumo_id:
            where_clauses.append(f"m.insumo_id = ${param_index}::uuid")
            params.append(uuid.UUID(insumo_id))
            param_index += 1

        if tipo:
            where_clauses.append(f"m.tipo = ${param_index}::varchar")
            params.append(tipo)
            param_index += 1

        if tipo_perda:
            # Junta com tipos_perda para filtrar pelo nome
            where_clauses.append(f"tp.nome = ${param_index}::varchar")
            params.append(tipo_perda)
            param_index += 1

        if periodo_inicio:
            where_clauses.append(f"m.criado_em >= ${param_index}::date")
            params.append(periodo_inicio)
            param_index += 1

        if periodo_fim:
            where_clauses.append(f"m.criado_em <= (${param_index}::date + interval '1 day')")
            params.append(periodo_fim)
            param_index += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # Query para total de registros
        count_query = f"""
            SELECT COUNT(*) FROM movimentacoes_estoque m
            LEFT JOIN tipos_perda tp ON tp.id = m.tipo_perda_id
            WHERE {where_sql}
        """
        total = await conn.fetchval(count_query, *params)

        # Query principal com paginação
        data_query = f"""
            SELECT 
                m.id,
                m.lote_insumo_id,
                m.insumo_id,
                i.nome AS insumo_nome,
                m.refeicao_id,
                m.quantidade,
                m.tipo,
                m.criado_em,
                m.usuario_id,
                m.ip_origem,
                m.tipo_perda_id,
                tp.nome AS tipo_perda_nome,
                m.observacao
            FROM movimentacoes_estoque m
            LEFT JOIN insumos i ON i.id = m.insumo_id
            LEFT JOIN tipos_perda tp ON tp.id = m.tipo_perda_id
            WHERE {where_sql}
            ORDER BY m.criado_em DESC
            LIMIT ${param_index} OFFSET ${param_index + 1}
        """
        params.append(pag.page_size)
        params.append(pag.offset)

        rows = await conn.fetch(data_query, *params)

        items = [
            MovimentacaoOut(
                id=str(r["id"]),
                lote_insumo_id=str(r["lote_insumo_id"]),
                insumo_id=str(r["insumo_id"]),
                insumo_nome=r.get("insumo_nome"),
                refeicao_id=str(r["refeicao_id"]) if r.get("refeicao_id") else None,
                quantidade=float(r["quantidade"]),
                tipo=r["tipo"],
                criado_em=r["criado_em"],
                usuario_id=str(r["usuario_id"]) if r.get("usuario_id") else None,
                ip_origem=r.get("ip_origem"),
                tipo_perda_id=str(r["tipo_perda_id"]) if r.get("tipo_perda_id") else None,
                tipo_perda_nome=r.get("tipo_perda_nome"),
                observacao=r.get("observacao")
            )
            for r in rows
        ]

        return Page(items=[item.model_dump() for item in items], total=total,
                    page=pag.page, page_size=pag.page_size)