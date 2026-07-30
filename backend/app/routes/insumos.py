import asyncpg
# backend/app/routes/insumos.py — Sistema Dono
#
# Rotas para gestão de insumos, lotes e cotações:
#   - CRUD de insumos (criar, listar, obter, atualizar, soft delete)
#   - Registro de lotes (atualiza custo médio e dispara evento PrecoAtualizado)
#   - Cotações manuais e via IA (assíncronas)
#
# ATUALIZAÇÃO (Rastreabilidade Total):
#   - O registro de lotes (POST /insumos/{id}/lotes) agora dispara o trigger
#     fn_atualizar_custo_medio_insumo que, além de atualizar o custo médio,
#     insere um evento em eventos_dominio com as colunas de auditoria
#     (usuario_id, ip_origem, user_agent) preenchidas automaticamente
#     pelo contexto definido no middleware AuditContextMiddleware.
#   - Nenhuma alteração adicional é necessária neste arquivo, pois a
#     lógica de auditoria está centralizada no banco e no middleware.
#   - O mesmo se aplica às aprovações de cotações (que registram aprovado_por),
#     embora essas já tivessem o campo aprovado_por explicitamente.

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import get_current_user, require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams
from app.rate_limit import acquire_ia_slot, check_ia_rate_limit, release_ia_slot

router = APIRouter()


# ---------------------------------------------------------------------
# Insumos
# ---------------------------------------------------------------------
class InsumoOut(BaseModel):
    id: str
    nome: str
    categoria_id: str
    unidade: str
    apresentacao: str | None
    marcas_aceitaveis: list[str] | None
    localizacao_estoque: str | None
    consumivel: bool
    custo_medio_ponderado: float
    ativo: bool


class CriarInsumoRequest(BaseModel):
    nome: str
    categoria_id: str
    unidade: str
    apresentacao: str | None = None
    marcas_aceitaveis: list[str] | None = None
    localizacao_estoque: str | None = None
    consumivel: bool = True


class AtualizarInsumoRequest(BaseModel):
    nome: str | None = None
    categoria_id: str | None = None
    unidade: str | None = None
    apresentacao: str | None = None
    marcas_aceitaveis: list[str] | None = None
    localizacao_estoque: str | None = None
    ativo: bool | None = None


def _insumo_out(r) -> InsumoOut:
    return InsumoOut(
        id=str(r["id"]), nome=r["nome"], categoria_id=str(r["categoria_id"]), unidade=r["unidade"],
        apresentacao=r["apresentacao"], marcas_aceitaveis=r["marcas_aceitaveis"],
        localizacao_estoque=r["localizacao_estoque"], consumivel=r["consumivel"],
        custo_medio_ponderado=float(r["custo_medio_ponderado"]), ativo=r["ativo"],
    )


@router.get("/insumos", response_model=Page)
async def listar_insumos(categoria_id: str | None = None, genero: str | None = None,
                          ativo: bool | None = None, pag: PageParams = Depends()):
    """Lista insumos com filtros opcionais por categoria, gênero e status ativo."""
    pool = get_pool()
    async with pool.acquire() as conn:
        where = ["($1::uuid IS NULL OR i.categoria_id = $1)",
                 "($2::varchar IS NULL OR g.nome = $2)",
                 "($3::boolean IS NULL OR i.ativo = $3)"]
        args = [uuid.UUID(categoria_id) if categoria_id else None, genero, ativo]
        query_base = f"""FROM insumos i
                          JOIN categorias c ON c.id = i.categoria_id
                          JOIN generos g ON g.id = c.genero_id
                         WHERE {' AND '.join(where)}"""
        total = await conn.fetchval(f"SELECT count(*) {query_base}", *args)
        rows = await conn.fetch(
            f"SELECT i.* {query_base} ORDER BY i.nome LIMIT ${len(args)+1} OFFSET ${len(args)+2}",
            *args, pag.page_size, pag.offset,
        )
        return Page(items=[_insumo_out(r).model_dump() for r in rows], total=total,
                    page=pag.page, page_size=pag.page_size)


@router.post("/insumos", response_model=InsumoOut, status_code=201)
async def criar_insumo(body: CriarInsumoRequest, _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Cria um novo insumo. O custo_medio_ponderado é inicializado como 0
    até que o primeiro lote seja registrado."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO insumos (nome, categoria_id, unidade, apresentacao, marcas_aceitaveis,
                                     localizacao_estoque, consumivel)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
            body.nome, uuid.UUID(body.categoria_id), body.unidade, body.apresentacao,
            body.marcas_aceitaveis, body.localizacao_estoque, body.consumivel,
        )
        return _insumo_out(row)


@router.get("/insumos/{insumo_id}", response_model=InsumoOut)
async def obter_insumo(insumo_id: str):
    """Obtém detalhes de um insumo específico."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM insumos WHERE id = $1", uuid.UUID(insumo_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Insumo não encontrado"))
        return _insumo_out(row)


@router.patch("/insumos/{insumo_id}", response_model=InsumoOut)
async def atualizar_insumo(insumo_id: str, body: AtualizarInsumoRequest,
                            _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Atualiza dados cadastrais do insumo. Não altera custo_medio_ponderado
    diretamente (isso só ocorre via registro de lote)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE insumos SET
                 nome = COALESCE($2, nome),
                 categoria_id = COALESCE($3, categoria_id),
                 unidade = COALESCE($4, unidade),
                 apresentacao = COALESCE($5, apresentacao),
                 marcas_aceitaveis = COALESCE($6, marcas_aceitaveis),
                 localizacao_estoque = COALESCE($7, localizacao_estoque),
                 ativo = COALESCE($8, ativo),
                 atualizado_em = now()
               WHERE id = $1 RETURNING *""",
            uuid.UUID(insumo_id), body.nome,
            uuid.UUID(body.categoria_id) if body.categoria_id else None,
            body.unidade, body.apresentacao, body.marcas_aceitaveis, body.localizacao_estoque, body.ativo,
        )
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Insumo não encontrado"))
        return _insumo_out(row)


@router.delete("/insumos/{insumo_id}", status_code=204)
async def remover_insumo(insumo_id: str, _: dict = Depends(require_perfil("ADMIN"))):
    """Soft delete do insumo (ativo = false). Bloqueado se houver itens_receita
    referenciando (ON DELETE RESTRICT) — nesse caso, a atualização falha e
    retorna 409."""
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("UPDATE insumos SET ativo = FALSE WHERE id = $1", uuid.UUID(insumo_id))
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=409, detail=error_detail("INSUMO_EM_USO", "Insumo em uso por alguma receita"))


# ---------------------------------------------------------------------
# Lotes
# ---------------------------------------------------------------------
class LoteOut(BaseModel):
    id: str
    insumo_id: str
    fornecedor_id: str | None
    valor_aquisicao: float
    data_aquisicao: date
    data_validade: date | None
    quantidade: float
    quantidade_disponivel: float


class CriarLoteRequest(BaseModel):
    fornecedor_id: str | None = None
    valor_aquisicao: Decimal
    data_aquisicao: date
    data_validade: date | None = None
    quantidade: Decimal


def _lote_out(r) -> LoteOut:
    return LoteOut(
        id=str(r["id"]), insumo_id=str(r["insumo_id"]),
        fornecedor_id=str(r["fornecedor_id"]) if r["fornecedor_id"] else None,
        valor_aquisicao=float(r["valor_aquisicao"]), data_aquisicao=r["data_aquisicao"],
        data_validade=r["data_validade"], quantidade=float(r["quantidade"]),
        quantidade_disponivel=float(r["quantidade_disponivel"]),
    )


@router.get("/insumos/{insumo_id}/lotes", response_model=list[LoteOut])
async def listar_lotes(insumo_id: str):
    """Lista todos os lotes de um insumo, ordenados por data de validade
    (FEFO: os que vencem primeiro vêm primeiro)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM lotes_insumo WHERE insumo_id = $1 ORDER BY data_validade NULLS LAST",
            uuid.UUID(insumo_id),
        )
        return [_lote_out(r) for r in rows]


@router.post("/insumos/{insumo_id}/lotes", response_model=LoteOut, status_code=201)
async def registrar_lote(insumo_id: str, body: CriarLoteRequest, _: dict = Depends(require_perfil("ADMIN"))):
    """Registra um novo lote de insumo.
    
    Único ponto de entrada para mudança de custo real: o trigger
    fn_atualizar_custo_medio_insumo (schema.sql) atualiza
    insumos.custo_medio_ponderado e grava um evento PrecoAtualizado no
    outbox, com as colunas de auditoria (usuario_id, ip_origem, user_agent)
    preenchidas automaticamente via contexto definido pelo middleware
    AuditContextMiddleware.
    
    O worker (fn_processar_eventos_pendentes) processa esse evento e
    recalcula a classificação ABC em cascata (Insumo/Gênero → Prato → Refeição → Menu).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO lotes_insumo (insumo_id, fornecedor_id, valor_aquisicao, data_aquisicao,
                                          data_validade, quantidade, quantidade_disponivel)
               VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING *""",
            uuid.UUID(insumo_id), uuid.UUID(body.fornecedor_id) if body.fornecedor_id else None,
            body.valor_aquisicao, body.data_aquisicao, body.data_validade, body.quantidade,
        )
        return _lote_out(row)


# ---------------------------------------------------------------------
# Cotações
# ---------------------------------------------------------------------
class CotacaoOut(BaseModel):
    id: str
    insumo_id: str
    fornecedor_id: str | None
    preco_unitario: float
    data_cotacao: date
    validade_cotacao: date | None
    origem: str
    status: str
    aprovado_por: str | None


class CriarCotacaoRequest(BaseModel):
    insumo_id: str
    fornecedor_id: str | None = None
    preco_unitario: Decimal
    validade_cotacao: date | None = None


def _cotacao_out(r) -> CotacaoOut:
    return CotacaoOut(
        id=str(r["id"]), insumo_id=str(r["insumo_id"]),
        fornecedor_id=str(r["fornecedor_id"]) if r["fornecedor_id"] else None,
        preco_unitario=float(r["preco_unitario"]), data_cotacao=r["data_cotacao"],
        validade_cotacao=r["validade_cotacao"], origem=r["origem"], status=r["status"],
        aprovado_por=str(r["aprovado_por"]) if r["aprovado_por"] else None,
    )


@router.get("/insumos/{insumo_id}/cotacoes", response_model=list[CotacaoOut])
async def listar_cotacoes_insumo(insumo_id: str, status: str | None = None):
    """Lista o histórico de cotações de um insumo, com filtro opcional por status."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM cotacoes WHERE insumo_id = $1 AND ($2::varchar IS NULL OR status = $2)
                ORDER BY data_cotacao DESC""",
            uuid.UUID(insumo_id), status,
        )
        return [_cotacao_out(r) for r in rows]


@router.post("/cotacoes", response_model=CotacaoOut, status_code=201)
async def criar_cotacao_manual(body: CriarCotacaoRequest, _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Registra uma cotação manual (origem = 'MANUAL'). Fica com status
    'PENDENTE_REVISAO' até ser aprovada/rejeitada."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO cotacoes (insumo_id, fornecedor_id, preco_unitario, validade_cotacao, origem)
               VALUES ($1, $2, $3, $4, 'MANUAL') RETURNING *""",
            uuid.UUID(body.insumo_id), uuid.UUID(body.fornecedor_id) if body.fornecedor_id else None,
            body.preco_unitario, body.validade_cotacao,
        )
        return _cotacao_out(row)


@router.patch("/cotacoes/{cotacao_id}/aprovar", response_model=CotacaoOut)
async def aprovar_cotacao(cotacao_id: str, current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Aprova uma cotação pendente. Registra aprovado_por = usuário atual.
    Se optar por aplicar, deve-se criar um novo lote com o preço aprovado
    (isso não é feito automaticamente aqui)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE cotacoes SET status = 'APROVADA', aprovado_por = $2
               WHERE id = $1 AND status = 'PENDENTE_REVISAO' RETURNING *""",
            uuid.UUID(cotacao_id), uuid.UUID(current_user["user_id"]),
        )
        if not row:
            raise HTTPException(status_code=409, detail=error_detail("COTACAO_JA_PROCESSADA",
                                 "Cotação não está mais pendente de revisão"))
        return _cotacao_out(row)


@router.patch("/cotacoes/{cotacao_id}/rejeitar", response_model=CotacaoOut)
async def rejeitar_cotacao(cotacao_id: str, _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Rejeita uma cotação pendente. Não afeta custos."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE cotacoes SET status = 'REJEITADA'
               WHERE id = $1 AND status = 'PENDENTE_REVISAO' RETURNING *""",
            uuid.UUID(cotacao_id),
        )
        if not row:
            raise HTTPException(status_code=409, detail=error_detail("COTACAO_JA_PROCESSADA",
                                 "Cotação não está mais pendente de revisão"))
        return _cotacao_out(row)


# ---------------------------------------------------------------------
# Cotação online via IA (§4.6a / §8 da API)
#
# IMPORTANTE — limitação real deste código: não há adaptador de scraping
# nem chave de LLM configurados neste ambiente. O contrato assíncrono
# (202 + job_id + polling) está implementado de verdade; a chamada
# externa em si (_buscar_precos_externos) é um ponto de extensão — hoje
# só marca o job como erro, deixando explícito que falta plugar um
# provedor real, em vez de fingir um resultado.
# ---------------------------------------------------------------------
class CotacaoOnlineRequest(BaseModel):
    insumo_ids: list[str]
    fornecedores_alvo: list[str] | None = None


class JobOut(BaseModel):
    job_id: str
    status: str
    resultado: dict | list | None = None
    erro_motivo: str | None = None


async def _buscar_precos_externos(insumo_ids: list[str], fornecedores_alvo: list[str] | None) -> dict:
    """Ponto de extensão: aqui entraria a chamada real a um adapter de
    scraping/API de fornecedores. Sem um provedor configurado, falha de
    forma explícita em vez de simular uma resposta."""
    raise NotImplementedError("Nenhum adapter de cotação online configurado neste ambiente")


@router.post("/cotacoes/ia-online", status_code=202)
async def solicitar_cotacao_ia(body: CotacaoOnlineRequest, current_user: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    """Dispara uma cotação online assistida por IA. Retorna 202 Accepted
    com job_id para consulta de status. O job é processado assincronamente."""
    await check_ia_rate_limit(current_user["user_id"])
    await acquire_ia_slot()
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                """INSERT INTO ia_jobs (tipo, solicitado_por, entrada)
                   VALUES ('COTACAO_ONLINE', $1, $2) RETURNING id""",
                uuid.UUID(current_user["user_id"]),
                {"insumo_ids": body.insumo_ids, "fornecedores_alvo": body.fornecedores_alvo},
            )
            try:
                resultado = await _buscar_precos_externos(body.insumo_ids, body.fornecedores_alvo)
                await conn.execute(
                    "UPDATE ia_jobs SET status = 'concluido', resultado = $2, concluido_em = now() WHERE id = $1",
                    job["id"], resultado,
                )
            except NotImplementedError as e:
                await conn.execute(
                    "UPDATE ia_jobs SET status = 'erro', erro_motivo = $2, concluido_em = now() WHERE id = $1",
                    job["id"], str(e),
                )
            return {"job_id": str(job["id"])}
    finally:
        await release_ia_slot()


@router.get("/cotacoes/ia-online/jobs/{job_id}", response_model=JobOut)
async def status_job_cotacao(job_id: str):
    """Consulta o status de um job de cotação online."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ia_jobs WHERE id = $1 AND tipo = 'COTACAO_ONLINE'", uuid.UUID(job_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Job não encontrado"))
        return JobOut(job_id=str(row["id"]), status=row["status"], resultado=row["resultado"], erro_motivo=row["erro_motivo"])