# backend/app/routes/previsoes.py — Sistema Dono
#
# Rotas para consulta de previsões de consumo (Fase 4).
# Fornece endpoints para:
#   - Consultar previsões por insumo, período e método.
#   - Comparar previsão vs. consumo real (métrica de acurácia).
#   - Listar insumos com previsões disponíveis.
#
# ATUALIZAÇÃO (Frente A - Fase 4):
#   - Módulo novo para suporte ao motor de previsão de consumo.
#   - Todas as rotas são de leitura (GET) e utilizam funções PL/pgSQL
#     para calcular ou buscar dados.
#   - Permissões: GESTAO, ADMIN e COMPRAS podem acessar previsões.
#
# Dependências:
#   - Tabela previsoes_consumo (schema.sql)
#   - Função fn_calcular_previsao_consumo (business-queries.sql)

from datetime import date, datetime, timedelta
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams

router = APIRouter(prefix="/previsoes", tags=["previsoes"])


# =====================================================================
# Modelos Pydantic
# =====================================================================

class PrevisaoConsumoOut(BaseModel):
    """Saída para uma previsão de consumo diária."""
    id: str
    insumo_id: str
    insumo_nome: Optional[str] = None
    data_referencia: date
    quantidade_prevista: float
    quantidade_real: Optional[float] = None
    metodo: str
    gerado_em: datetime
    versao: int


class PrevisaoResumoOut(BaseModel):
    """Resumo de previsão para um insumo em um período."""
    insumo_id: str
    insumo_nome: str
    total_previsto: float
    total_real: Optional[float] = None
    dias_previstos: int
    dias_realizados: int
    acuracia: Optional[float] = None  # porcentagem de acerto (se houver dados reais)


# =====================================================================
# Rotas
# =====================================================================

@router.get("/insumos", response_model=list[dict])
async def listar_insumos_com_previsao(
    data_referencia: Optional[date] = None,
    _: dict = Depends(require_perfil("COMPRAS", "GESTAO", "ADMIN"))
):
    """Lista os insumos que possuem previsões para uma data específica.
    Se data_referencia for omitida, usa a data atual."""
    data_alvo = data_referencia or date.today()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT
                  i.id AS insumo_id,
                  i.nome AS insumo_nome,
                  i.unidade
               FROM previsoes_consumo pc
               JOIN insumos i ON i.id = pc.insumo_id
               WHERE pc.data_referencia = $1
               ORDER BY i.nome""",
            data_alvo
        )
        return [dict(r) for r in rows]


@router.get("/consumo", response_model=Page)
async def consultar_previsoes(
    insumo_id: Optional[str] = None,
    data_inicio: Optional[date] = None,
    data_fim: Optional[date] = None,
    versao: Optional[int] = None,
    incluir_real: bool = Query(True, description="Incluir consumo real (se disponível)"),
    pag: PageParams = Depends(),
    _: dict = Depends(require_perfil("COMPRAS", "GESTAO", "ADMIN"))
):
    """Consulta previsões de consumo com filtros.

    Se insumo_id não for informado, retorna previsões para todos os insumos.
    Se data_inicio e data_fim não forem informados, usa os próximos 30 dias.
    Versão mais recente é usada por padrão (se não especificada).

    O campo quantidade_real é preenchido pela função fn_preencher_consumo_real
    (executada pelo forecast_worker) para datas já passadas.
    """
    # Valida período
    if data_fim and data_inicio and data_fim < data_inicio:
        raise HTTPException(
            status_code=400,
            detail=error_detail("PERIODO_INVALIDO", "data_fim é anterior a data_inicio")
        )

    # Se não houver período, usa os próximos 30 dias a partir de hoje
    if not data_inicio and not data_fim:
        data_inicio = date.today()
        data_fim = date.today() + timedelta(days=30)

    # Converte UUID (se fornecido)
    insumo_uuid = uuid.UUID(insumo_id) if insumo_id else None

    pool = get_pool()
    async with pool.acquire() as conn:
        # Se versão não for especificada, usa a mais recente
        if versao is None and insumo_uuid:
            versao = await conn.fetchval(
                "SELECT MAX(versao) FROM previsoes_consumo WHERE insumo_id = $1",
                insumo_uuid
            )

        # Query principal com filtros
        where_parts = [
            "pc.data_referencia BETWEEN $1 AND $2",
            "($3::uuid IS NULL OR pc.insumo_id = $3)",
            "($4::int IS NULL OR pc.versao = $4)"
        ]
        params = [data_inicio, data_fim, insumo_uuid, versao]

        # Se insumo_id for NULL, pega a versão mais recente para cada insumo
        if insumo_uuid is None and versao is None:
            # Subquery para pegar a versão mais recente por insumo
            where_parts.append(
                "pc.versao = (SELECT MAX(versao) FROM previsoes_consumo pc2 WHERE pc2.insumo_id = pc.insumo_id)"
            )

        where_sql = " AND ".join(where_parts)

        # Query de total
        count_query = f"""
            SELECT COUNT(*) FROM previsoes_consumo pc
            WHERE {where_sql}
        """
        total = await conn.fetchval(count_query, *params)

        # Query de dados
        data_query = f"""
            SELECT
                pc.id,
                pc.insumo_id,
                i.nome AS insumo_nome,
                pc.data_referencia,
                pc.quantidade_prevista,
                pc.quantidade_real,
                pc.metodo,
                pc.gerado_em,
                pc.versao
            FROM previsoes_consumo pc
            JOIN insumos i ON i.id = pc.insumo_id
            WHERE {where_sql}
            ORDER BY pc.data_referencia, i.nome
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
        """
        params.append(pag.page_size)
        params.append(pag.offset)

        rows = await conn.fetch(data_query, *params)

        items = [
            PrevisaoConsumoOut(
                id=str(r["id"]),
                insumo_id=str(r["insumo_id"]),
                insumo_nome=r.get("insumo_nome"),
                data_referencia=r["data_referencia"],
                quantidade_prevista=float(r["quantidade_prevista"]),
                quantidade_real=float(r["quantidade_real"]) if r.get("quantidade_real") is not None else None,
                metodo=r["metodo"],
                gerado_em=r["gerado_em"],
                versao=r["versao"]
            ).model_dump()
            for r in rows
        ]

        return Page(items=items, total=total, page=pag.page, page_size=pag.page_size)


@router.get("/resumo/{insumo_id}", response_model=PrevisaoResumoOut)
async def resumo_previsao_insumo(
    insumo_id: str,
    dias: int = Query(30, ge=1, le=365, description="Número de dias para o resumo"),
    _: dict = Depends(require_perfil("COMPRAS", "GESTAO", "ADMIN"))
):
    """Resumo de previsões para um insumo específico nos próximos N dias.

    Retorna:
      - total_previsto: soma das previsões no período
      - total_real: soma do consumo real no período (se disponível)
      - dias_previstos: número de dias previstos
      - dias_realizados: número de dias com consumo real registrado
      - acuracia: porcentagem de acerto (1 - erro_absoluto_medio) se houver dados reais
    """
    insumo_uuid = uuid.UUID(insumo_id)
    data_inicio = date.today()
    data_fim = data_inicio + timedelta(days=dias - 1)

    pool = get_pool()
    async with pool.acquire() as conn:
        # Obtém a versão mais recente para este insumo
        versao = await conn.fetchval(
            "SELECT MAX(versao) FROM previsoes_consumo WHERE insumo_id = $1",
            insumo_uuid
        )

        # Busca dados de previsão e real
        row = await conn.fetchrow(
            """SELECT
                  i.nome AS insumo_nome,
                  i.unidade,
                  COUNT(*) AS dias_previstos,
                  SUM(pc.quantidade_prevista) AS total_previsto,
                  COUNT(pc.quantidade_real) AS dias_realizados,
                  SUM(pc.quantidade_real) AS total_real
               FROM previsoes_consumo pc
               JOIN insumos i ON i.id = pc.insumo_id
               WHERE pc.insumo_id = $1
                 AND pc.data_referencia BETWEEN $2 AND $3
                 AND pc.versao = $4
               GROUP BY i.nome, i.unidade""",
            insumo_uuid, data_inicio, data_fim, versao
        )

        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("PREVISAO_NAO_ENCONTRADA", "Nenhuma previsão encontrada para este insumo no período")
            )

        total_previsto = float(row["total_previsto"])
        total_real = float(row["total_real"]) if row["total_real"] is not None else None
        dias_previstos = row["dias_previstos"]
        dias_realizados = row["dias_realizados"]

        # Calcula acurácia (se houver dados reais)
        acuracia = None
        if total_real is not None and total_real > 0:
            erro_absoluto = abs(total_real - total_previsto) / total_real
            acuracia = max(0, 1 - erro_absoluto)  # garante que não fique negativo

        return PrevisaoResumoOut(
            insumo_id=insumo_id,
            insumo_nome=row["insumo_nome"],
            total_previsto=round(total_previsto, 3),
            total_real=round(total_real, 3) if total_real is not None else None,
            dias_previstos=dias_previstos,
            dias_realizados=dias_realizados,
            acuracia=round(acuracia * 100, 2) if acuracia is not None else None
        )


@router.get("/comparacao")
async def comparar_previsao_real(
    insumo_id: str,
    data_referencia: date,
    _: dict = Depends(require_perfil("COMPRAS", "GESTAO", "ADMIN"))
):
    """Compara a previsão com o consumo real para um insumo e dia específico.

    Retorna: { previsto, real, diferenca, percentual_diferenca }
    """
    insumo_uuid = uuid.UUID(insumo_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        # Busca a previsão mais recente para este dia
        row = await conn.fetchrow(
            """SELECT quantidade_prevista, quantidade_real
               FROM previsoes_consumo
               WHERE insumo_id = $1
                 AND data_referencia = $2
               ORDER BY versao DESC
               LIMIT 1""",
            insumo_uuid, data_referencia
        )

        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("PREVISAO_NAO_ENCONTRADA", "Nenhuma previsão encontrada para este dia")
            )

        previsto = float(row["quantidade_prevista"])
        real = float(row["quantidade_real"]) if row["quantidade_real"] is not None else None

        if real is None:
            # Busca consumo real diretamente
            real = await conn.fetchval(
                """SELECT COALESCE(SUM(quantidade), 0)
                   FROM movimentacoes_estoque
                   WHERE insumo_id = $1
                     AND tipo = 'BAIXA_EXECUCAO'
                     AND criado_em::date = $2""",
                insumo_uuid, data_referencia
            )
            if real is None:
                real = 0.0
            real = float(real)

        diferenca = previsto - real
        percentual = (diferenca / real * 100) if real > 0 else None

        return {
            "insumo_id": insumo_id,
            "data_referencia": data_referencia.isoformat(),
            "previsto": round(previsto, 3),
            "real": round(real, 3),
            "diferenca": round(diferenca, 3),
            "percentual_diferenca": round(percentual, 2) if percentual is not None else None,
            "acuracia": round(100 - (abs(diferenca) / real * 100), 2) if real > 0 else None
        }