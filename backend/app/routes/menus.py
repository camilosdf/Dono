# backend/app/routes/menus.py — Sistema Dono
import uuid
from datetime import date, time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.pagination import Page, PageParams

router = APIRouter()


class ItemMenuOut(BaseModel):
    id: str
    refeicao_id: str
    genero_refeicao: str
    ordem_cronologica: int
    custo_snapshot: float | None


class MenuOut(BaseModel):
    id: str
    nome_evento: str
    data_criacao: date
    estilo_servico_id: str
    data_inicio: date
    horario_inicio: time
    data_fim: date
    horario_fim: time
    local_servico: str | None
    status: str
    itens: list[ItemMenuOut] = []


class CriarMenuRequest(BaseModel):
    nome_evento: str
    estilo_servico_id: str
    data_inicio: date
    horario_inicio: time
    data_fim: date
    horario_fim: time
    local_servico: str | None = None


class AdicionarItemMenuRequest(BaseModel):
    refeicao_id: str
    ordem_cronologica: int


def _item_out(r) -> ItemMenuOut:
    return ItemMenuOut(
        id=str(r["id"]), refeicao_id=str(r["refeicao_id"]), genero_refeicao=r["genero_refeicao"],
        ordem_cronologica=r["ordem_cronologica"],
        custo_snapshot=float(r["custo_snapshot"]) if r["custo_snapshot"] is not None else None,
    )


async def _menu_out(conn, r, incluir_itens: bool = True) -> MenuOut:
    itens = []
    if incluir_itens:
        rows = await conn.fetch(
            """SELECT im.*, r.genero_refeicao
                 FROM itens_menu im JOIN refeicoes r ON r.id = im.refeicao_id
                WHERE im.menu_id = $1 ORDER BY im.ordem_cronologica""",
            r["id"],
        )
        itens = [_item_out(x) for x in rows]
    return MenuOut(
        id=str(r["id"]), nome_evento=r["nome_evento"], data_criacao=r["data_criacao"],
        estilo_servico_id=str(r["estilo_servico_id"]), data_inicio=r["data_inicio"],
        horario_inicio=r["horario_inicio"], data_fim=r["data_fim"], horario_fim=r["horario_fim"],
        local_servico=r["local_servico"], status=r["status"], itens=itens,
    )


@router.get("/menus", response_model=Page)
async def listar_menus(
    data_inicio: date | None = None,
    status: str | None = None,
    pag: PageParams = Depends(),
    _: dict = Depends(require_perfil("CHEF", "GESTAO", "ADMIN", "COMPRAS")),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        where = "($1::date IS NULL OR data_inicio = $1) AND ($2::varchar IS NULL OR status = $2)"
        total = await conn.fetchval(f"SELECT count(*) FROM menus WHERE {where}", data_inicio, status)
        rows = await conn.fetch(
            f"SELECT * FROM menus WHERE {where} ORDER BY data_inicio DESC LIMIT $3 OFFSET $4",
            data_inicio, status, pag.page_size, pag.offset,
        )
        items = [(await _menu_out(conn, r, incluir_itens=False)).model_dump() for r in rows]
        return Page(items=items, total=total, page=pag.page, page_size=pag.page_size)


@router.post("/menus", response_model=MenuOut, status_code=201)
async def criar_menu(body: CriarMenuRequest, _: dict = Depends(require_perfil("CHEF", "GESTAO", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        estilo_existe = await conn.fetchval("SELECT 1 FROM estilos_servico WHERE id = $1", uuid.UUID(body.estilo_servico_id))
        if not estilo_existe:
            raise HTTPException(status_code=400, detail=error_detail("VALIDACAO_INVALIDA", "estilo_servico_id inválido"))
        row = await conn.fetchrow(
            """INSERT INTO menus (nome_evento, estilo_servico_id, data_inicio, horario_inicio,
                                   data_fim, horario_fim, local_servico)
               VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
            body.nome_evento, uuid.UUID(body.estilo_servico_id), body.data_inicio, body.horario_inicio,
            body.data_fim, body.horario_fim, body.local_servico,
        )
        return await _menu_out(conn, row)


@router.get("/menus/{menu_id}", response_model=MenuOut)
async def obter_menu(menu_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM menus WHERE id = $1", uuid.UUID(menu_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Menu não encontrado"))
        return await _menu_out(conn, row)


@router.post("/menus/{menu_id}/itens", response_model=ItemMenuOut, status_code=201)
async def adicionar_item_menu(menu_id: str, body: AdicionarItemMenuRequest,
                               _: dict = Depends(require_perfil("CHEF", "GESTAO", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        menu = await conn.fetchrow("SELECT status FROM menus WHERE id = $1", uuid.UUID(menu_id))
        if not menu:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Menu não encontrado"))
        if menu["status"] != "PLANEJADO":
            raise HTTPException(status_code=409, detail=error_detail(
                "MENU_JA_CONFIRMADO", "Só é possível adicionar itens a um menu em status PLANEJADO"))

        refeicao = await conn.fetchrow("SELECT * FROM refeicoes WHERE id = $1", uuid.UUID(body.refeicao_id))
        if not refeicao:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Refeição não encontrada"))

        try:
            row = await conn.fetchrow(
                """INSERT INTO itens_menu (menu_id, refeicao_id, ordem_cronologica)
                   VALUES ($1, $2, $3) RETURNING *""",
                uuid.UUID(menu_id), uuid.UUID(body.refeicao_id), body.ordem_cronologica,
            )
        except Exception:
            raise HTTPException(status_code=409, detail=error_detail(
                "ORDEM_CRONOLOGICA_DUPLICADA", "Já existe uma refeição nessa ordem cronológica neste menu"))
        return _item_out({**dict(row), "genero_refeicao": refeicao["genero_refeicao"]})


@router.patch("/menus/{menu_id}/confirmar", response_model=MenuOut)
async def confirmar_menu(menu_id: str, _: dict = Depends(require_perfil("GESTAO", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM menus WHERE id = $1", uuid.UUID(menu_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Menu não encontrado"))
        if row["status"] != "PLANEJADO":
            raise HTTPException(status_code=409, detail=error_detail(
                "TRANSICAO_STATUS_INVALIDA", "Só é possível confirmar a partir de PLANEJADO",
                {"status_atual": row["status"], "status_solicitado": "CONFIRMADO"}))
        # Dispara fn_snapshot_custo_menu (trigger em schema.sql), que soma
        # itens_refeicao.custo_snapshot × qtd_pessoas por refeição e
        # congela em itens_menu.custo_snapshot — histórico imutável daqui
        # em diante (ver fn_recalcular_abc_menu, que lê esse valor direto,
        # sem multiplicar de novo por qtd_pessoas).
        row = await conn.fetchrow(
            "UPDATE menus SET status = 'CONFIRMADO' WHERE id = $1 RETURNING *", uuid.UUID(menu_id)
        )
        return await _menu_out(conn, row)


@router.patch("/menus/{menu_id}/realizar", response_model=MenuOut)
async def realizar_menu(menu_id: str, _: dict = Depends(require_perfil("GESTAO", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE menus SET status = 'REALIZADO' WHERE id = $1 AND status = 'CONFIRMADO' RETURNING *",
            uuid.UUID(menu_id),
        )
        if not row:
            raise HTTPException(status_code=409, detail=error_detail(
                "TRANSICAO_STATUS_INVALIDA", "Só é possível marcar como realizado a partir de CONFIRMADO"))
        return await _menu_out(conn, row)


@router.patch("/menus/{menu_id}/cancelar", response_model=MenuOut)
async def cancelar_menu(menu_id: str, _: dict = Depends(require_perfil("GESTAO", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE menus SET status = 'CANCELADO' WHERE id = $1 AND status <> 'CANCELADO' RETURNING *",
            uuid.UUID(menu_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail=error_detail(
                "RECURSO_NAO_ENCONTRADO", "Menu não encontrado ou já cancelado"))
        return await _menu_out(conn, row)


@router.get("/menus/{menu_id}/abc")
async def abc_menu(menu_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT item_id AS refeicao_id, custo, percentual_acumulado, classe
                 FROM classificacoes_abc WHERE escopo_tipo = 'MENU' AND escopo_id_pai = $1
                ORDER BY custo DESC""",
            uuid.UUID(menu_id),
        )
        if not rows:
            raise HTTPException(status_code=404, detail=error_detail(
                "ABC_NAO_CALCULADO", "Classificação ABC ainda não foi calculada para este menu"))
        return [dict(r) for r in rows]


@router.get("/menus/{menu_id}/margem-contribuicao")
async def margem_contribuicao_menu(menu_id: str, _: dict = Depends(require_perfil("GESTAO", "ADMIN"))):
    # Premissa assumida (não há campo de "preço de venda do menu" no
    # schema): receita de cada refeição = soma, para os pratos servidos
    # nela, de pratos.preco_venda_praticado × refeicoes.qtd_pessoas.
    # Pratos sem preco_venda_praticado cadastrado são ignorados no total
    # de receita (fica registrado em "pratos_sem_preco", não silenciado).
    pool = get_pool()
    async with pool.acquire() as conn:
        menu = await conn.fetchrow("SELECT * FROM menus WHERE id = $1", uuid.UUID(menu_id))
        if not menu:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Menu não encontrado"))

        refeicoes = await conn.fetch(
            """SELECT im.refeicao_id, r.genero_refeicao, r.qtd_pessoas, im.custo_snapshot
                 FROM itens_menu im JOIN refeicoes r ON r.id = im.refeicao_id
                WHERE im.menu_id = $1 ORDER BY im.ordem_cronologica""",
            uuid.UUID(menu_id),
        )
        resultado = []
        for ref in refeicoes:
            pratos_receita = await conn.fetch(
                """SELECT p.nome, p.preco_venda_praticado
                     FROM itens_refeicao ir JOIN pratos p ON p.id = ir.prato_id
                    WHERE ir.refeicao_id = $1""",
                ref["refeicao_id"],
            )
            sem_preco = [p["nome"] for p in pratos_receita if p["preco_venda_praticado"] is None]
            receita = sum(
                float(p["preco_venda_praticado"]) * ref["qtd_pessoas"]
                for p in pratos_receita if p["preco_venda_praticado"] is not None
            )
            custo = float(ref["custo_snapshot"]) if ref["custo_snapshot"] is not None else None
            margem = (receita - custo) if custo is not None else None
            resultado.append({
                "refeicao_id": str(ref["refeicao_id"]), "genero_refeicao": ref["genero_refeicao"],
                "qtd_pessoas": ref["qtd_pessoas"], "custo_total": custo, "receita_total": round(receita, 2),
                "margem_contribuicao": round(margem, 2) if margem is not None else None,
                "pratos_sem_preco_cadastrado": sem_preco,
            })
        return {"menu_id": menu_id, "refeicoes": resultado}
