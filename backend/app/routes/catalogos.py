# backend/app/routes/catalogos.py — Sistema Dono
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.database import get_pool
from app.dependencies import require_perfil
from app.errors import error_detail

router = APIRouter()


# ---------------------------------------------------------------------
# Gêneros
# ---------------------------------------------------------------------
class GeneroOut(BaseModel):
    id: str
    nome: str


@router.get("/generos", response_model=list[GeneroOut])
async def listar_generos():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, nome FROM generos ORDER BY nome")
        return [GeneroOut(id=str(r["id"]), nome=r["nome"]) for r in rows]


# ---------------------------------------------------------------------
# Categorias
# ---------------------------------------------------------------------
class CategoriaOut(BaseModel):
    id: str
    nome: str
    genero: str


class CriarCategoriaRequest(BaseModel):
    nome: str
    genero: str  # 'ALIMENTICIO' | 'OPERACIONAL_UTENSILIO'


@router.get("/categorias", response_model=list[CategoriaOut])
async def listar_categorias(genero: str | None = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.nome, g.nome AS genero
                 FROM categorias c JOIN generos g ON g.id = c.genero_id
                WHERE ($1::varchar IS NULL OR g.nome = $1)
                ORDER BY g.nome, c.nome""",
            genero,
        )
        return [CategoriaOut(id=str(r["id"]), nome=r["nome"], genero=r["genero"]) for r in rows]


@router.post("/categorias", response_model=CategoriaOut, status_code=201)
async def criar_categoria(body: CriarCategoriaRequest, _: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        genero_id = await conn.fetchval("SELECT id FROM generos WHERE nome = $1", body.genero)
        if not genero_id:
            raise HTTPException(status_code=400, detail=error_detail("VALIDACAO_INVALIDA", "Gênero inválido"))
        try:
            row = await conn.fetchrow(
                "INSERT INTO categorias (nome, genero_id) VALUES ($1, $2) RETURNING id, nome",
                body.nome, genero_id,
            )
        except Exception:
            raise HTTPException(status_code=409, detail=error_detail("CATEGORIA_JA_EXISTE", "Categoria já cadastrada para este gênero"))
        return CategoriaOut(id=str(row["id"]), nome=row["nome"], genero=body.genero)


# ---------------------------------------------------------------------
# Fornecedores
# ---------------------------------------------------------------------
class FornecedorOut(BaseModel):
    id: str
    nome: str
    contato: str | None
    prazo_entrega_medio_dias: int | None
    condicoes_pagamento: str | None
    avaliacao: float | None
    ativo: bool


class CriarFornecedorRequest(BaseModel):
    nome: str
    contato: str | None = None
    prazo_entrega_medio_dias: int | None = None
    condicoes_pagamento: str | None = None
    avaliacao: float | None = None


class AtualizarFornecedorRequest(BaseModel):
    nome: str | None = None
    contato: str | None = None
    prazo_entrega_medio_dias: int | None = None
    condicoes_pagamento: str | None = None
    avaliacao: float | None = None
    ativo: bool | None = None


def _fornecedor_out(r) -> FornecedorOut:
    return FornecedorOut(
        id=str(r["id"]), nome=r["nome"], contato=r["contato"],
        prazo_entrega_medio_dias=r["prazo_entrega_medio_dias"],
        condicoes_pagamento=r["condicoes_pagamento"],
        avaliacao=float(r["avaliacao"]) if r["avaliacao"] is not None else None,
        ativo=r["ativo"],
    )


@router.get("/fornecedores", response_model=list[FornecedorOut])
async def listar_fornecedores(ativo: bool | None = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM fornecedores WHERE ($1::boolean IS NULL OR ativo = $1) ORDER BY nome""",
            ativo,
        )
        return [_fornecedor_out(r) for r in rows]


@router.post("/fornecedores", response_model=FornecedorOut, status_code=201)
async def criar_fornecedor(body: CriarFornecedorRequest, _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO fornecedores (nome, contato, prazo_entrega_medio_dias, condicoes_pagamento, avaliacao)
               VALUES ($1, $2, $3, $4, $5) RETURNING *""",
            body.nome, body.contato, body.prazo_entrega_medio_dias, body.condicoes_pagamento, body.avaliacao,
        )
        return _fornecedor_out(row)


@router.get("/fornecedores/{fornecedor_id}", response_model=FornecedorOut)
async def obter_fornecedor(fornecedor_id: str):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM fornecedores WHERE id = $1", uuid.UUID(fornecedor_id))
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Fornecedor não encontrado"))
        return _fornecedor_out(row)


@router.patch("/fornecedores/{fornecedor_id}", response_model=FornecedorOut)
async def atualizar_fornecedor(fornecedor_id: str, body: AtualizarFornecedorRequest,
                                _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE fornecedores SET
                 nome = COALESCE($2, nome),
                 contato = COALESCE($3, contato),
                 prazo_entrega_medio_dias = COALESCE($4, prazo_entrega_medio_dias),
                 condicoes_pagamento = COALESCE($5, condicoes_pagamento),
                 avaliacao = COALESCE($6, avaliacao),
                 ativo = COALESCE($7, ativo)
               WHERE id = $1 RETURNING *""",
            uuid.UUID(fornecedor_id), body.nome, body.contato, body.prazo_entrega_medio_dias,
            body.condicoes_pagamento, body.avaliacao, body.ativo,
        )
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("RECURSO_NAO_ENCONTRADO", "Fornecedor não encontrado"))
        return _fornecedor_out(row)


@router.post("/fornecedores/{fornecedor_id}/categorias", status_code=204)
async def vincular_categoria_fornecedor(fornecedor_id: str, categoria_id: str,
                                         _: dict = Depends(require_perfil("COMPRAS", "ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO fornecedores_categorias (fornecedor_id, categoria_id) VALUES ($1, $2)
               ON CONFLICT DO NOTHING""",
            uuid.UUID(fornecedor_id), uuid.UUID(categoria_id),
        )


# ---------------------------------------------------------------------
# Estilos de Serviço
# ---------------------------------------------------------------------
class EstiloServicoOut(BaseModel):
    id: str
    nome: str
    descricao: str | None
    dinamica: str | None


class CriarEstiloServicoRequest(BaseModel):
    nome: str
    descricao: str | None = None
    dinamica: str | None = None


@router.get("/estilos-servico", response_model=list[EstiloServicoOut])
async def listar_estilos_servico():
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM estilos_servico ORDER BY nome")
        return [EstiloServicoOut(id=str(r["id"]), nome=r["nome"], descricao=r["descricao"], dinamica=r["dinamica"]) for r in rows]


@router.post("/estilos-servico", response_model=EstiloServicoOut, status_code=201)
async def criar_estilo_servico(body: CriarEstiloServicoRequest, _: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO estilos_servico (nome, descricao, dinamica) VALUES ($1, $2, $3) RETURNING *",
            body.nome, body.descricao, body.dinamica,
        )
        return EstiloServicoOut(id=str(row["id"]), nome=row["nome"], descricao=row["descricao"], dinamica=row["dinamica"])


# ---------------------------------------------------------------------
# Regras de Composição
# ---------------------------------------------------------------------
class RegraComposicaoOut(BaseModel):
    id: str
    genero_refeicao: str
    genero_prato_obrigatorio: str


class CriarRegraRequest(BaseModel):
    genero_refeicao: str
    genero_prato_obrigatorio: str


@router.get("/regras-composicao", response_model=list[RegraComposicaoOut])
async def listar_regras_composicao(genero_refeicao: str | None = None):
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM regras_composicao
                WHERE ($1::varchar IS NULL OR genero_refeicao = $1)
                ORDER BY genero_refeicao, genero_prato_obrigatorio""",
            genero_refeicao,
        )
        return [RegraComposicaoOut(id=str(r["id"]), genero_refeicao=r["genero_refeicao"],
                                    genero_prato_obrigatorio=r["genero_prato_obrigatorio"]) for r in rows]


@router.post("/regras-composicao", response_model=RegraComposicaoOut, status_code=201)
async def criar_regra_composicao(body: CriarRegraRequest, _: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO regras_composicao (genero_refeicao, genero_prato_obrigatorio) VALUES ($1, $2) RETURNING *",
            body.genero_refeicao, body.genero_prato_obrigatorio,
        )
        return RegraComposicaoOut(id=str(row["id"]), genero_refeicao=row["genero_refeicao"],
                                   genero_prato_obrigatorio=row["genero_prato_obrigatorio"])


@router.delete("/regras-composicao/{regra_id}", status_code=204)
async def remover_regra_composicao(regra_id: str, _: dict = Depends(require_perfil("ADMIN"))):
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM regras_composicao WHERE id = $1", uuid.UUID(regra_id))
