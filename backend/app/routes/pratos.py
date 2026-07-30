import asyncpg
# backend/app/routes/pratos.py — Sistema Dono
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail
from app.exportacao import slugificar
from app.ficha_tecnica_pdf import gerar_pdf_ficha_tecnica
from app.pagination import Page, PageParams

router = APIRouter()


class ItemReceitaIn(BaseModel):
    insumo_id: str
    tipo: str  # 'ALIMENTICIO' | 'OPERACIONAL' | 'UTENSILIO'
    peso_bruto: Decimal
    fator_correcao: Decimal = Decimal("1.0")


class ItemReceitaOut(BaseModel):
    id: str
    insumo_id: str
    tipo: str
    peso_bruto: float
    fator_correcao: float
    peso_liquido: float
    custo_unitario_registrado: float
    custo_total_calculado: float


class PratoOut(BaseModel):
    id: str
    nome: str
    genero_prato: str
    tempo_preparo_min: int | None
    rendimento_base_porcoes: float
    tamanho_porcao_g: float | None
    instrucoes_apresentacao: str | None
    equipamentos_utilizados: list[str] | None
    temperatura_servico: str | None
    margem_desperdicio_pct: float
    custo_embalagem: float
    preco_venda_praticado: float | None
    origem: str
    status: str
    itens_receita: list[ItemReceitaOut] = []


class CriarPratoRequest(BaseModel):
    nome: str
    genero_prato: str
    rendimento_base_porcoes: Decimal
    tempo_preparo_min: int | None = None
    tamanho_porcao_g: Decimal | None = None
    instrucoes_apresentacao: str | None = None
    equipamentos_utilizados: list[str] | None = None
    temperatura_servico: str | None = None
    armazenamento_faixa_temp: str | None = None
    armazenamento_tempo_max_h: int | None = None
    margem_desperdicio_pct: Decimal = Decimal("0")
    custo_embalagem: Decimal = Decimal("0")
    preco_venda_praticado: Decimal | None = None
    itens_receita: list[ItemReceitaIn] = []


class AtualizarPratoRequest(BaseModel):
    nome: str | None = None
    tempo_preparo_min: int | None = None
    rendimento_base_porcoes: Decimal | None = None
    tamanho_porcao_g: Decimal | None = None
    instrucoes_apresentacao: str | None = None
    equipamentos_utilizados: list[str] | None = None
    temperatura_servico: str | None = None
    margem_desperdicio_pct: Decimal | None = None
    custo_embalagem: Decimal | None = None
    preco_venda_praticado: Decimal | None = None
    status: str | None = None


def _item_out(r) -> ItemReceitaOut:
    return ItemReceitaOut(
        id=str(r["id"]), insumo_id=str(r["insumo_id"]), tipo=r["tipo"],
        peso_bruto=float(r["peso_bruto"]), fator_correcao=float(r["fator_correcao"]),
        peso_liquido=float(r["peso_liquido"]), custo_unitario_registrado=float(r["custo_unitario_registrado"]),
        custo_total_calculado=float(r["custo_total_calculado"]),
    )


async def _prato_out(conn, r, incluir_itens: bool = True) -> PratoOut:
    itens = []
    if incluir_itens:
        rows = await conn.fetch("SELECT * FROM itens_receita WHERE prato_id = $1", r["id"])
        itens = [_item_out(x) for x in rows]
    return PratoOut(
        id=str(r["id"]), nome=r["nome"], genero_prato=r["genero_prato"],
        tempo_preparo_min=r["tempo_preparo_min"], rendimento_base_porcoes=float(r["rendimento_base_porcoes"]),
        tamanho_porcao_g=float(r["tamanho_porcao_g"]) if r["tamanho_porcao_g"] is not None else None,
        instrucoes_apresentacao=r["instrucoes_apresentacao"], equipamentos_utilizados=r["equipamentos_utilizados"],
        temperatura_servico=r["temperatura_servico"], margem_desperdicio_pct=float(r["margem_desperdicio_pct"]),
        custo_embalagem=float(r["custo_embalagem"]),
        preco_venda_praticado=float(r["preco_venda_praticado"]) if r["preco_venda_praticado"] is not None else None,
        origem=r["origem"], status=r["status"], itens_receita=itens,
    )


@router.get("/pratos", response_model=Page)
async def listar_pratos(genero_prato: str | None = None, status: str | None = None, pag: PageParams = Depends()):
    pool = get_pool()
    async with pool.acquire() as conn:
        where = "($1::varchar IS NULL OR genero_prato = $1) AND ($2::varchar IS NULL OR status = $2)"
        total = await conn.fetchval(f"SELECT count(*) FROM pratos WHERE {where}", genero_prato, status)
        rows = await conn.fetch(
            f"SELECT * FROM pratos WHERE {where} ORDER BY nome LIMIT $3 OFFSET $4",
            genero_prato, status, pag.page_size, pag.offset,
        )
        items = [(await _prato_out(conn, r, incluir_itens=False)).model_dump() for r in rows]
        return Page(items=items, total=total, page=pag.page, page_size=pag.page_size)


@router.post("/pratos", response_model=PratoOut, status_code=201)
async def criar_prato(body: CriarPratoRequest, _: dict = Depends(require_perfil("CHEF", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        prato = await conn.fetchrow(
            """INSERT INTO pratos (nome, genero_prato, rendimento_base_porcoes, tempo_preparo_min,
                                    tamanho_porcao_g, instrucoes_apresentacao, equipamentos_utilizados,
                                    temperatura_servico, armazenamento_faixa_temp, armazenamento_tempo_max_h,
                                    margem_desperdicio_pct, custo_embalagem, preco_venda_praticado)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *""",
            body.nome, body.genero_prato, body.rendimento_base_porcoes, body.tempo_preparo_min,
            body.tamanho_porcao_g, body.instrucoes_apresentacao, body.equipamentos_utilizados,
            body.temperatura_servico, body.armazenamento_faixa_temp, body.armazenamento_tempo_max_h,
            body.margem_desperdicio_pct, body.custo_embalagem, body.preco_venda_praticado,
        )
        for item in body.itens_receita:
            custo_unitario = await conn.fetchval(
                "SELECT custo_medio_ponderado FROM insumos WHERE id = $1", uuid.UUID(item.insumo_id)
            )
            if custo_unitario is None:
                raise HTTPException(status_code=400, detail=error_detail(
                    "VALIDACAO_INVALIDA", f"Insumo {item.insumo_id} não encontrado"))
            await conn.execute(
                """INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao,
                                               custo_unitario_registrado)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                prato["id"], uuid.UUID(item.insumo_id), item.tipo, item.peso_bruto, item.fator_correcao,
                custo_unitario,
            )
        if body.itens_receita:
            # ABC de PRATO depende só dos itens deste prato — calcula na
            # hora, não espera o worker (que só reage a evento de preço
            # de insumo, não à criação/edição de uma receita).
            await conn.fetchval("SELECT fn_recalcular_abc_prato($1)", prato["id"])
        return await _prato_out(conn, prato)


@router.get("/pratos/{prato_id}", response_model=PratoOut)
async def obter_prato(prato_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pratos WHERE id = $1", uuid.UUID(prato_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Prato não encontrado"))
        return await _prato_out(conn, row)


@router.patch("/pratos/{prato_id}", response_model=PratoOut)
async def atualizar_prato(prato_id: str, body: AtualizarPratoRequest, _: dict = Depends(require_perfil("CHEF", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE pratos SET
                 nome = COALESCE($2, nome), tempo_preparo_min = COALESCE($3, tempo_preparo_min),
                 rendimento_base_porcoes = COALESCE($4, rendimento_base_porcoes),
                 tamanho_porcao_g = COALESCE($5, tamanho_porcao_g),
                 instrucoes_apresentacao = COALESCE($6, instrucoes_apresentacao),
                 equipamentos_utilizados = COALESCE($7, equipamentos_utilizados),
                 temperatura_servico = COALESCE($8, temperatura_servico),
                 margem_desperdicio_pct = COALESCE($9, margem_desperdicio_pct),
                 custo_embalagem = COALESCE($10, custo_embalagem),
                 preco_venda_praticado = COALESCE($11, preco_venda_praticado),
                 status = COALESCE($12, status),
                 atualizado_em = now()
               WHERE id = $1 RETURNING *""",
            uuid.UUID(prato_id), body.nome, body.tempo_preparo_min, body.rendimento_base_porcoes,
            body.tamanho_porcao_g, body.instrucoes_apresentacao, body.equipamentos_utilizados,
            body.temperatura_servico, body.margem_desperdicio_pct, body.custo_embalagem,
            body.preco_venda_praticado, body.status,
        )
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Prato não encontrado"))
        return await _prato_out(conn, row)


@router.put("/pratos/{prato_id}/itens-receita", response_model=PratoOut)
async def substituir_itens_receita(prato_id: str, itens: list[ItemReceitaIn],
                                    _: dict = Depends(require_perfil("CHEF", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        prato = await conn.fetchrow("SELECT * FROM pratos WHERE id = $1", uuid.UUID(prato_id))
        if not prato:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Prato não encontrado"))
        await conn.execute("DELETE FROM itens_receita WHERE prato_id = $1", uuid.UUID(prato_id))
        for item in itens:
            custo_unitario = await conn.fetchval(
                "SELECT custo_medio_ponderado FROM insumos WHERE id = $1", uuid.UUID(item.insumo_id)
            )
            if custo_unitario is None:
                raise HTTPException(status_code=400, detail=error_detail(
                    "VALIDACAO_INVALIDA", f"Insumo {item.insumo_id} não encontrado"))
            await conn.execute(
                """INSERT INTO itens_receita (prato_id, insumo_id, tipo, peso_bruto, fator_correcao,
                                               custo_unitario_registrado)
                   VALUES ($1,$2,$3,$4,$5,$6)""",
                uuid.UUID(prato_id), uuid.UUID(item.insumo_id), item.tipo, item.peso_bruto,
                item.fator_correcao, custo_unitario,
            )
        # Mesmo motivo do POST /pratos: ABC de PRATO não pode esperar o
        # worker aqui, porque substituir a receita não é um evento de
        # preço — é a receita mudando, e o worker só escuta preço mudar.
        await conn.execute("DELETE FROM classificacoes_abc WHERE escopo_tipo = 'PRATO' AND escopo_id_pai = $1",
                            uuid.UUID(prato_id))
        if itens:
            await conn.fetchval("SELECT fn_recalcular_abc_prato($1)", uuid.UUID(prato_id))
        return await _prato_out(conn, prato)


@router.patch("/pratos/{prato_id}/aprovar", response_model=PratoOut)
async def aprovar_prato(prato_id: str, _: dict = Depends(require_perfil("CHEF", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE pratos SET status = 'ATIVO' WHERE id = $1 AND status = 'PENDENTE_APROVACAO' RETURNING *",
            uuid.UUID(prato_id),
        )
        if not row:
            raise HTTPException(status_code=409, detail=error_detail(
                "PRATO_NAO_PENDENTE_APROVACAO", "Prato não está pendente de aprovação"))
        return await _prato_out(conn, row)


@router.delete("/pratos/{prato_id}", status_code=204)
async def remover_prato(prato_id: str, _: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("UPDATE pratos SET status = 'INATIVO' WHERE id = $1", uuid.UUID(prato_id))
        except asyncpg.ForeignKeyViolationError:
            raise HTTPException(status_code=409, detail=error_detail("PRATO_EM_USO", "Prato em uso em alguma refeição"))


@router.get("/pratos/{prato_id}/abc")
async def abc_prato(prato_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT item_id AS insumo_id, custo, percentual_acumulado, classe
                 FROM classificacoes_abc WHERE escopo_tipo = 'PRATO' AND escopo_id_pai = $1
                ORDER BY custo DESC""",
            uuid.UUID(prato_id),
        )
        if not rows:
            raise HTTPException(status_code=404, detail=error_detail(
                "ABC_NAO_CALCULADO", "Classificação ABC ainda não foi calculada para este prato"))
        return [dict(r) for r in rows]


@router.get("/pratos/{prato_id}/ficha-tecnica")
async def ficha_tecnica(prato_id: str, tipo: str = Query(..., pattern="^(gerencial|insumo|operacional)$"),
                         insumo_id: str | None = None,
                         formato: str = Query("json", pattern="^(json|pdf)$")):
    pool = get_pool()
    async with pool.acquire() as conn:
        prato = await conn.fetchrow("SELECT * FROM pratos WHERE id = $1", uuid.UUID(prato_id))
        if not prato:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Prato não encontrado"))

        if tipo == "operacional":
            # Cozinha/produção — sem dados financeiros de propósito (§2.2.1)
            itens = await conn.fetch(
                """SELECT i.nome, ir.peso_bruto, ir.fator_correcao, ir.unidade AS insumo_unidade
                     FROM itens_receita ir JOIN insumos i ON i.id = ir.insumo_id
                    WHERE ir.prato_id = $1""".replace("ir.unidade", "i.unidade"),
                uuid.UUID(prato_id),
            )
            dados = {
                "nome_prato": prato["nome"], "tempo_preparo_min": prato["tempo_preparo_min"],
                "rendimento": float(prato["rendimento_base_porcoes"]),
                "equipamentos_utilizados": prato["equipamentos_utilizados"],
                "ingredientes": [{"nome": i["nome"], "quantidade": float(i["peso_bruto"]),
                                   "unidade": i["insumo_unidade"]} for i in itens],
                "modo_preparo": prato["modo_preparo"],
                "instrucoes_apresentacao": prato["instrucoes_apresentacao"],
                "temperatura_servico": prato["temperatura_servico"],
                "armazenamento": {"faixa_temp": prato["armazenamento_faixa_temp"],
                                   "tempo_max_h": prato["armazenamento_tempo_max_h"]},
            }
            if formato == "pdf":
                return _pdf_ficha_tecnica("operacional", dados, prato["nome"])
            return dados

        if tipo == "insumo":
            if not insumo_id:
                raise HTTPException(status_code=400, detail=error_detail(
                    "VALIDACAO_INVALIDA", "tipo=insumo requer ?insumo_id="))
            item = await conn.fetchrow(
                """SELECT ir.*, i.nome AS insumo_nome, i.unidade, i.atualizado_em, c.nome AS categoria_nome
                     FROM itens_receita ir
                     JOIN insumos i ON i.id = ir.insumo_id
                     JOIN categorias c ON c.id = i.categoria_id
                    WHERE ir.prato_id = $1 AND ir.insumo_id = $2""",
                uuid.UUID(prato_id), uuid.UUID(insumo_id),
            )
            if not item:
                raise HTTPException(status_code=404, detail=error_detail(
                    "RECURSO_NAO_ENCONTRADO", "Insumo não utilizado neste prato"))
            dados = {
                "nome_insumo": item["insumo_nome"], "categoria": item["categoria_nome"],
                "peso_bruto": float(item["peso_bruto"]), "unidade": item["unidade"],
                "custo_unitario": float(item["custo_unitario_registrado"]),
                "custo_total": float(item["custo_total_calculado"]),
                # Campos adicionados nesta rodada (não quebram consumidores
                # existentes do JSON — só somam chaves) para cobrir a seção
                # "Informações de Controle e Armazenamento" do template real
                # de Ficha Técnica Insumo: equipamento/armazenamento não têm
                # coluna própria por insumo no schema, então vêm do PRATO
                # (pratos.equipamentos_utilizados / armazenamento_*) — os
                # dois PDFs de exemplo (Insumo e Operacional) trazem a MESMA
                # frase de armazenamento, confirmando que é um dado do prato,
                # não do insumo isolado.
                "atualizado_em": item["atualizado_em"].isoformat() if item["atualizado_em"] else None,
                "equipamentos_utilizados": prato["equipamentos_utilizados"],
                "armazenamento": {"faixa_temp": prato["armazenamento_faixa_temp"],
                                   "tempo_max_h": prato["armazenamento_tempo_max_h"]},
            }
            if formato == "pdf":
                return _pdf_ficha_tecnica("insumo", dados, f"{prato['nome']}_{item['insumo_nome']}")
            return dados

        # tipo == "gerencial"
        itens = await conn.fetch(
            """SELECT ir.*, i.nome AS insumo_nome, i.unidade
                 FROM itens_receita ir JOIN insumos i ON i.id = ir.insumo_id
                WHERE ir.prato_id = $1""",
            uuid.UUID(prato_id),
        )
        custo_ingredientes = sum(float(i["custo_total_calculado"]) for i in itens)
        margem_pct = float(prato["margem_desperdicio_pct"])
        custo_total_receita = custo_ingredientes * (1 + margem_pct / 100)
        rendimento = float(prato["rendimento_base_porcoes"]) or 1
        cmv_porcao = custo_total_receita / rendimento
        custo_embalagem = float(prato["custo_embalagem"])
        custo_total_porcao = cmv_porcao + custo_embalagem
        preco_venda = float(prato["preco_venda_praticado"]) if prato["preco_venda_praticado"] is not None else None
        margem_lucro = (
            (preco_venda - custo_total_porcao) / preco_venda if preco_venda else None
        )
        dados = {
            "nome_prato": prato["nome"], "rendimento_base_porcoes": rendimento,
            "tempo_preparo_min": prato["tempo_preparo_min"],
            "ingredientes": [
                {"nome": i["insumo_nome"], "peso_bruto": float(i["peso_bruto"]),
                 "fator_correcao": float(i["fator_correcao"]), "peso_liquido": float(i["peso_liquido"]),
                 "unidade": i["unidade"], "custo_unitario": float(i["custo_unitario_registrado"]),
                 "custo_total": float(i["custo_total_calculado"])}
                for i in itens
            ],
            "custo_total_ingredientes": round(custo_ingredientes, 4),
            "margem_desperdicio_pct": margem_pct,
            "custo_total_receita": round(custo_total_receita, 4),
            "cmv_por_porcao": round(cmv_porcao, 4),
            "custo_embalagem": custo_embalagem,
            "custo_total_porcao": round(custo_total_porcao, 4),
            "preco_venda_praticado": preco_venda,
            "margem_lucro_bruta_pct": round(margem_lucro * 100, 2) if margem_lucro is not None else None,
        }
        if formato == "pdf":
            return _pdf_ficha_tecnica("gerencial", dados, prato["nome"])
        return dados


def _pdf_ficha_tecnica(tipo: str, dados: dict, nome_para_arquivo: str) -> Response:
    """Gera o PDF (app/ficha_tecnica_pdf.py) e devolve como download —
    compartilhado pelos 3 ramos de GET /pratos/{id}/ficha-tecnica acima."""
    conteudo = gerar_pdf_ficha_tecnica(tipo, dados)
    nome_arquivo = slugificar(f"ficha_tecnica_{tipo}_{nome_para_arquivo}")
    return Response(
        content=conteudo,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}.pdf"'},
    )
