# backend/app/routes/refeicoes.py — Sistema Dono
#
# Rotas para o ciclo de vida das refeições:
#   - CRUD básico (criar, listar, obter)
#   - Adicionar/remover itens (pratos)
#   - Transições de status: confirmar, executar, servir, cancelar
#   - Classificação ABC da refeição
#
# Fluxo de status: PLANEJADA → CONFIRMADA → EXECUTADA → SERVIDA
#                  ↕            ↕            ↕
#                 CANCELADA ← (via estorno) ←
#
# ATUALIZAÇÃO (Auditoria com parâmetros explícitos):
#   - As operações que registram movimentações de estoque (executar e cancelar/estornar)
#     agora passam usuario_id, ip_origem e user_agent como parâmetros explícitos
#     para as funções PL/pgSQL (fn_executar_refeicao e fn_estornar_execucao_refeicao).
#   - Isso garante que os campos de auditoria sejam preenchidos independentemente
#     da conexão utilizada pelo pool, resolvendo o problema de variáveis de sessão
#     não propagadas entre conexões.
#   - IP e user-agent são extraídos do objeto Request e passados diretamente.

import json
import uuid
from datetime import date, time

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams

router = APIRouter()


class ItemRefeicaoOut(BaseModel):
    id: str
    prato_id: str
    prato_nome: str
    categoria_composicao: str
    custo_snapshot: float | None


class RefeicaoOut(BaseModel):
    id: str
    genero_refeicao: str
    data: date
    horario_inicio: time
    horario_fim: time
    qtd_pessoas: int
    status: str
    itens: list[ItemRefeicaoOut] = []


class CriarRefeicaoRequest(BaseModel):
    genero_refeicao: str
    data: date
    horario_inicio: time
    horario_fim: time
    qtd_pessoas: int


class AdicionarItemRequest(BaseModel):
    prato_id: str


def _item_out(r) -> ItemRefeicaoOut:
    return ItemRefeicaoOut(
        id=str(r["id"]),
        prato_id=str(r["prato_id"]),
        prato_nome=r["prato_nome"],
        categoria_composicao=r["categoria_composicao"],
        custo_snapshot=float(r["custo_snapshot"]) if r["custo_snapshot"] is not None else None,
    )


async def _refeicao_out(conn, r, incluir_itens: bool = True) -> RefeicaoOut:
    itens = []
    if incluir_itens:
        rows = await conn.fetch(
            """SELECT ir.*, p.nome AS prato_nome
                 FROM itens_refeicao ir
                 JOIN pratos p ON p.id = ir.prato_id
                WHERE ir.refeicao_id = $1""",
            r["id"],
        )
        itens = [_item_out(x) for x in rows]
    return RefeicaoOut(
        id=str(r["id"]),
        genero_refeicao=r["genero_refeicao"],
        data=r["data"],
        horario_inicio=r["horario_inicio"],
        horario_fim=r["horario_fim"],
        qtd_pessoas=r["qtd_pessoas"],
        status=r["status"],
        itens=itens,
    )


@router.get("/refeicoes", response_model=Page)
async def listar_refeicoes(
    data: date | None = None,
    genero_refeicao: str | None = None,
    status: str | None = None,
    pag: PageParams = Depends()
):
    """Lista refeições com filtros opcionais por data, gênero e status."""
    pool = get_pool()
    async with pool.acquire() as conn:
        where = (
            "($1::date IS NULL OR data = $1) "
            "AND ($2::varchar IS NULL OR genero_refeicao = $2) "
            "AND ($3::varchar IS NULL OR status = $3)"
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM refeicoes WHERE {where}",
            data, genero_refeicao, status
        )
        rows = await conn.fetch(
            f"SELECT * FROM refeicoes WHERE {where} "
            "ORDER BY data, horario_inicio LIMIT $4 OFFSET $5",
            data, genero_refeicao, status, pag.page_size, pag.offset,
        )
        items = [
            (await _refeicao_out(conn, r, incluir_itens=False)).model_dump()
            for r in rows
        ]
        return Page(items=items, total=total, page=pag.page, page_size=pag.page_size)


@router.post("/refeicoes", response_model=RefeicaoOut, status_code=201)
async def criar_refeicao(
    body: CriarRefeicaoRequest,
    _: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Cria uma nova refeição com status inicial PLANEJADA."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO refeicoes (genero_refeicao, data, horario_inicio, horario_fim, qtd_pessoas)
               VALUES ($1, $2, $3, $4, $5) RETURNING *""",
            body.genero_refeicao,
            body.data,
            body.horario_inicio,
            body.horario_fim,
            body.qtd_pessoas,
        )
        return await _refeicao_out(conn, row)


@router.get("/refeicoes/{refeicao_id}", response_model=RefeicaoOut)
async def obter_refeicao(refeicao_id: str):
    """Obtém os detalhes de uma refeição específica, incluindo seus itens."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
            )
        return await _refeicao_out(conn, row)


@router.post("/refeicoes/{refeicao_id}/itens", response_model=ItemRefeicaoOut, status_code=201)
async def adicionar_item_refeicao(
    refeicao_id: str,
    body: AdicionarItemRequest,
    _: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Adiciona um prato à refeição. Valida a composição contra regras_composicao.
    Só permitido se a refeição estiver PLANEJADA."""
    pool = get_pool()
    async with pool.acquire() as conn:
        refeicao = await conn.fetchrow(
            "SELECT * FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        if not refeicao:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
            )
        if refeicao["status"] != "PLANEJADA":
            raise HTTPException(
                status_code=409,
                detail=error_detail(
                    "REFEICAO_JA_CONFIRMADA",
                    "Só é possível adicionar itens a uma refeição em status PLANEJADA"
                )
            )

        prato = await conn.fetchrow(
            "SELECT * FROM pratos WHERE id = $1",
            uuid.UUID(body.prato_id)
        )
        if not prato:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Prato não encontrado")
            )

        # Validação de composição
        permitido = await conn.fetchval(
            """SELECT 1 FROM regras_composicao
                WHERE genero_refeicao = $1 AND genero_prato_obrigatorio = $2""",
            refeicao["genero_refeicao"],
            prato["genero_prato"],
        )
        if not permitido:
            categorias_aceitas = [
                r["genero_prato_obrigatorio"]
                for r in await conn.fetch(
                    "SELECT genero_prato_obrigatorio FROM regras_composicao "
                    "WHERE genero_refeicao = $1",
                    refeicao["genero_refeicao"],
                )
            ]
            raise HTTPException(
                status_code=422,
                detail=error_detail(
                    "COMPOSICAO_INVALIDA",
                    f"O prato '{prato['nome']}' (gênero: {prato['genero_prato']}) não atende nenhuma "
                    f"categoria obrigatória de {refeicao['genero_refeicao']}.",
                    {
                        "genero_refeicao": refeicao["genero_refeicao"],
                        "categorias_aceitas": categorias_aceitas,
                        "genero_prato_enviado": prato["genero_prato"],
                    },
                )
            )

        try:
            row = await conn.fetchrow(
                """INSERT INTO itens_refeicao (refeicao_id, prato_id, categoria_composicao)
                   VALUES ($1, $2, $3) RETURNING *""",
                uuid.UUID(refeicao_id),
                uuid.UUID(body.prato_id),
                prato["genero_prato"],
            )
        except Exception:
            raise HTTPException(
                status_code=409,
                detail=error_detail("ITEM_JA_EXISTE", "Este prato já está nesta refeição")
            )
        return _item_out({**dict(row), "prato_nome": prato["nome"]})


@router.delete("/refeicoes/{refeicao_id}/itens/{item_id}", status_code=204)
async def remover_item_refeicao(
    refeicao_id: str,
    item_id: str,
    _: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Remove um prato da refeição. Só permitido se a refeição estiver PLANEJADA."""
    pool = get_pool()
    async with pool.acquire() as conn:
        refeicao = await conn.fetchrow(
            "SELECT status FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        if not refeicao:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
            )
        if refeicao["status"] != "PLANEJADA":
            raise HTTPException(
                status_code=409,
                detail=error_detail(
                    "REFEICAO_JA_CONFIRMADA",
                    "Só é possível remover itens de uma refeição em status PLANEJADA"
                )
            )
        await conn.execute(
            "DELETE FROM itens_refeicao WHERE id = $1 AND refeicao_id = $2",
            uuid.UUID(item_id),
            uuid.UUID(refeicao_id),
        )


@router.patch("/refeicoes/{refeicao_id}/confirmar", response_model=RefeicaoOut)
async def confirmar_refeicao(
    refeicao_id: str,
    _: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Transição PLANEJADA → CONFIRMADA.
    Congela o custo_snapshot dos itens via trigger fn_snapshot_custo_refeicao.
    Também materializa a classificação ABC da refeição."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
            )
        if row["status"] != "PLANEJADA":
            raise HTTPException(
                status_code=409,
                detail=error_detail(
                    "TRANSICAO_STATUS_INVALIDA",
                    "Só é possível confirmar a partir de PLANEJADA",
                    {"status_atual": row["status"], "status_solicitado": "CONFIRMADA"},
                )
            )
        row = await conn.fetchrow(
            "UPDATE refeicoes SET status = 'CONFIRMADA' WHERE id = $1 RETURNING *",
            uuid.UUID(refeicao_id)
        )
        return await _refeicao_out(conn, row)


@router.patch("/refeicoes/{refeicao_id}/executar", response_model=RefeicaoOut)
async def executar_refeicao(
    refeicao_id: str,
    request: Request,
    current_user: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Transição CONFIRMADA → EXECUTADA.
    Dá baixa real (FEFO) dos insumos consumíveis, registra movimentacao_estoque
    e atualiza quantidade_disponivel dos lotes.
    
    A auditoria é passada explicitamente como parâmetros para a função PL/pgSQL,
    garantindo o preenchimento independentemente da conexão do pool.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    "SELECT fn_executar_refeicao($1, $2, $3, $4)",
                    uuid.UUID(refeicao_id),
                    uuid.UUID(current_user["user_id"]),
                    request.client.host if request.client else None,
                    request.headers.get("user-agent"),
                )
        except asyncpg.PostgresError as e:
            if e.sqlstate == "P1000":
                raise HTTPException(
                    status_code=404,
                    detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
                )
            if e.sqlstate == "P1001":
                raise HTTPException(
                    status_code=409,
                    detail=error_detail(
                        "TRANSICAO_STATUS_INVALIDA",
                        "Só é possível executar a partir de CONFIRMADA"
                    )
                )
            if e.sqlstate == "P1002":
                detalhes = None
                if getattr(e, "detail", None):
                    try:
                        detalhes = json.loads(e.detail)
                    except (ValueError, TypeError):
                        detalhes = None
                raise HTTPException(
                    status_code=422,
                    detail=error_detail(
                        "ESTOQUE_INSUFICIENTE",
                        "Estoque insuficiente para executar a refeição",
                        {"insumos_faltantes": detalhes} if detalhes else None,
                    )
                )
            raise
        row = await conn.fetchrow(
            "SELECT * FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        return await _refeicao_out(conn, row)


@router.patch("/refeicoes/{refeicao_id}/servir", response_model=RefeicaoOut)
async def servir_refeicao(
    refeicao_id: str,
    _: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Transição EXECUTADA → SERVIDA.
    Marca a refeição como servida. Não mexe em estoque (já foi debitado
    na execução)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE refeicoes SET status = 'SERVIDA' "
            "WHERE id = $1 AND status = 'EXECUTADA' RETURNING *",
            uuid.UUID(refeicao_id),
        )
        if not row:
            raise HTTPException(
                status_code=409,
                detail=error_detail(
                    "TRANSICAO_STATUS_INVALIDA",
                    "Só é possível marcar como servida a partir de EXECUTADA"
                )
            )
        return await _refeicao_out(conn, row)


@router.patch("/refeicoes/{refeicao_id}/cancelar", response_model=RefeicaoOut)
async def cancelar_refeicao(
    refeicao_id: str,
    request: Request,
    current_user: dict = Depends(require_perfil("CHEF", "ADMIN"))
):
    """Cancelamento com três comportamentos distintos:
      - PLANEJADA/CONFIRMADA: cancelamento simples (sem estorno de estoque).
      - EXECUTADA: estorno de estoque (devolve o que foi debitado) via
        fn_estornar_execucao_refeicao, registra ESTORNO_CANCELAMENTO.
        Os parâmetros de auditoria são passados explicitamente.
      - SERVIDA: bloqueado (comida já entregue, irreversível).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        atual = await conn.fetchrow(
            "SELECT status FROM refeicoes WHERE id = $1",
            uuid.UUID(refeicao_id)
        )
        if not atual:
            raise HTTPException(
                status_code=404,
                detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
            )

        if atual["status"] in ("PLANEJADA", "CONFIRMADA"):
            row = await conn.fetchrow(
                "UPDATE refeicoes SET status = 'CANCELADA' WHERE id = $1 RETURNING *",
                uuid.UUID(refeicao_id)
            )
            return await _refeicao_out(conn, row)

        if atual["status"] == "EXECUTADA":
            try:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT fn_estornar_execucao_refeicao($1, $2, $3, $4)",
                        uuid.UUID(refeicao_id),
                        uuid.UUID(current_user["user_id"]),
                        request.client.host if request.client else None,
                        request.headers.get("user-agent"),
                    )
            except asyncpg.PostgresError as e:
                if e.sqlstate == "P1000":
                    raise HTTPException(
                        status_code=404,
                        detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada")
                    )
                if e.sqlstate == "P1001":
                    raise HTTPException(
                        status_code=409,
                        detail=error_detail(
                            "TRANSICAO_STATUS_INVALIDA",
                            "Refeição não está mais em EXECUTADA"
                        )
                    )
                raise
            row = await conn.fetchrow(
                "SELECT * FROM refeicoes WHERE id = $1",
                uuid.UUID(refeicao_id)
            )
            return await _refeicao_out(conn, row)

        # SERVIDA ou CANCELADA: sem volta
        raise HTTPException(
            status_code=409,
            detail=error_detail(
                "TRANSICAO_STATUS_INVALIDA",
                "Só é possível cancelar uma refeição em PLANEJADA, CONFIRMADA ou EXECUTADA — "
                "depois de SERVIDA a comida já foi entregue, não há estoque a estornar",
                {"status_atual": atual["status"]},
            )
        )


@router.get("/refeicoes/{refeicao_id}/abc")
async def abc_refeicao(refeicao_id: str):
    """Retorna a classificação ABC dos pratos dentro da refeição,
    lendo da tabela materializada classificacoes_abc."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT item_id AS prato_id, custo, percentual_acumulado, classe
                 FROM classificacoes_abc
                WHERE escopo_tipo = 'REFEICAO' AND escopo_id_pai = $1
                ORDER BY custo DESC""",
            uuid.UUID(refeicao_id),
        )
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=error_detail(
                    "ABC_NAO_CALCULADO",
                    "Classificação ABC ainda não foi calculada para esta refeição"
                )
            )
        return [dict(r) for r in rows]