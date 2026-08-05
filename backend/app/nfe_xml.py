# backend/app/nfe_xml.py — Sistema Dono
#
# Parser de XML de NF-e (Nota Fiscal Eletrônica) brasileira.
# Extrai dados estruturados diretamente do XML — sem OCR, sem regex em
# texto livre — usando ElementTree da stdlib Python.
#
# Por que XML em vez de OCR para NF-e:
#   - O emissor é obrigado por lei a fornecer o XML (SEFAZ, Art. 7° Ajuste SINIEF 07/05).
#   - O XML tem campos com nome e posição fixos (xNome, CNPJ, vProd, etc.).
#   - OCR em PDF de DANFE é uma aproximação — o XML é a fonte de verdade.
#
# Campos extraídos:
#   - Emitente: CNPJ, razão social, nome fantasia, endereço
#   - Destinatário: CNPJ, razão social
#   - Produtos: código, descrição, NCM, CFOP, unidade, quantidade, valor unitário, valor total
#   - Totais: valor dos produtos, frete, seguro, desconto, IPI, ICMS, valor total NF
#   - Identificação: chave de acesso, número NF, série, data emissão, natureza da operação
#
# Namespace padrão NF-e (versão 4.0):
#   http://www.portalfiscal.inf.br/nfe
#
# Uso:
#   from app.nfe_xml import parsear_xml_nfe
#   dados = parsear_xml_nfe(xml_bytes)

import xml.etree.ElementTree as ET
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import asyncpg

from app.database import get_pool

logger = logging.getLogger(__name__)

# Namespace padrão da NF-e versão 4.0
_NS = "http://www.portalfiscal.inf.br/nfe"
_NS_MAP = {"nfe": _NS}


# =====================================================================
# Helpers de extração
# =====================================================================

def _tag(nome: str) -> str:
    """Retorna a tag com namespace no formato {uri}tag."""
    return f"{{{_NS}}}{nome}"


def _texto(elem, *caminhos: str) -> Optional[str]:
    """Busca o texto do primeiro caminho que existir, retorna None se nenhum."""
    for caminho in caminhos:
        partes = caminho.split("/")
        atual = elem
        for parte in partes:
            if atual is None:
                break
            atual = atual.find(_tag(parte))
        if atual is not None and atual.text:
            return atual.text.strip()
    return None


def _decimal(elem, *caminhos: str) -> Optional[Decimal]:
    """Extrai valor decimal do primeiro caminho que existir."""
    texto = _texto(elem, *caminhos)
    if texto:
        try:
            return Decimal(texto)
        except InvalidOperation:
            return None
    return None


def _data(elem, *caminhos: str) -> Optional[date]:
    """Extrai data (AAAA-MM-DD) do primeiro caminho que existir."""
    texto = _texto(elem, *caminhos)
    if texto:
        try:
            # Formato ISO 8601 com ou sem timezone: 2024-03-15T10:30:00-03:00
            return datetime.fromisoformat(texto[:10]).date()
        except (ValueError, TypeError):
            return None
    return None


# =====================================================================
# Parser principal
# =====================================================================

def parsear_xml_nfe(xml_bytes: bytes) -> Dict[str, Any]:
    """Parseia um XML de NF-e e retorna dicionário estruturado.

    Args:
        xml_bytes: Conteúdo do arquivo XML da NF-e em bytes.

    Returns:
        Dicionário com emitente, destinatario, identificacao, produtos, totais.

    Raises:
        ValueError: Se o XML não for uma NF-e válida ou campos obrigatórios
                    estiverem ausentes.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise ValueError(f"XML inválido: {e}") from e

    # Remove namespace do root para localizar nfeProc ou nfe
    # Aceita tanto nfeProc (NF-e processada pela SEFAZ) quanto NFe (NF-e pura)
    nfe = None
    if root.tag == _tag("nfeProc"):
        nfe = root.find(_tag("NFe"))
    elif root.tag == _tag("NFe"):
        nfe = root
    else:
        # Tenta encontrar NFe em qualquer profundidade
        nfe = root.find(f".//{_tag('NFe')}")

    if nfe is None:
        raise ValueError(
            "Arquivo não reconhecido como NF-e. "
            "Esperado elemento raiz 'nfeProc' ou 'NFe' com namespace "
            f"'{_NS}'."
        )

    infNFe = nfe.find(_tag("infNFe"))
    if infNFe is None:
        raise ValueError("Elemento 'infNFe' não encontrado no XML.")

    # Chave de acesso (atributo Id sem o prefixo 'NFe')
    id_attr = infNFe.get("Id", "")
    chave_acesso = id_attr.replace("NFe", "") if id_attr else None

    # ----------------------------------------------------------------
    # Identificação (ide)
    # ----------------------------------------------------------------
    ide = infNFe.find(_tag("ide"))
    identificacao = {
        "chave_acesso": chave_acesso,
        "numero": _texto(ide, "nNF") if ide is not None else None,
        "serie": _texto(ide, "serie") if ide is not None else None,
        "natureza_operacao": _texto(ide, "natOp") if ide is not None else None,
        "data_emissao": _data(ide, "dhEmi", "dEmi") if ide is not None else None,
        "data_saida_entrada": _data(ide, "dhSaiEnt", "dSaiEnt") if ide is not None else None,
        "tipo_operacao": _texto(ide, "tpNF") if ide is not None else None,
        # 0 = entrada, 1 = saída
        "uf_emitente": _texto(ide, "cUF") if ide is not None else None,
    }

    # ----------------------------------------------------------------
    # Emitente (emit)
    # ----------------------------------------------------------------
    emit = infNFe.find(_tag("emit"))
    if emit is None:
        raise ValueError("Elemento 'emit' (emitente) não encontrado.")

    end_emit = emit.find(_tag("enderEmit"))
    emitente = {
        "cnpj": _texto(emit, "CNPJ"),
        "cpf": _texto(emit, "CPF"),  # pessoa física
        "razao_social": _texto(emit, "xNome"),
        "nome_fantasia": _texto(emit, "xFant"),
        "logradouro": _texto(end_emit, "xLgr") if end_emit is not None else None,
        "numero": _texto(end_emit, "nro") if end_emit is not None else None,
        "complemento": _texto(end_emit, "xCpl") if end_emit is not None else None,
        "bairro": _texto(end_emit, "xBairro") if end_emit is not None else None,
        "municipio": _texto(end_emit, "xMun") if end_emit is not None else None,
        "uf": _texto(end_emit, "UF") if end_emit is not None else None,
        "cep": _texto(end_emit, "CEP") if end_emit is not None else None,
        "telefone": _texto(end_emit, "fone") if end_emit is not None else None,
    }

    if not emitente["cnpj"] and not emitente["cpf"]:
        raise ValueError("Emitente sem CNPJ nem CPF no XML.")

    # ----------------------------------------------------------------
    # Destinatário (dest)
    # ----------------------------------------------------------------
    dest = infNFe.find(_tag("dest"))
    destinatario = {}
    if dest is not None:
        destinatario = {
            "cnpj": _texto(dest, "CNPJ"),
            "cpf": _texto(dest, "CPF"),
            "razao_social": _texto(dest, "xNome"),
        }

    # ----------------------------------------------------------------
    # Produtos (det → prod)
    # ----------------------------------------------------------------
    produtos = []
    for det in infNFe.findall(_tag("det")):
        prod = det.find(_tag("prod"))
        if prod is None:
            continue

        qtd = _decimal(prod, "qCom")
        vunit = _decimal(prod, "vUnCom")
        vtotal = _decimal(prod, "vProd")

        # Fallback: calcula valor total se não vier explícito
        if vtotal is None and qtd is not None and vunit is not None:
            vtotal = (qtd * vunit).quantize(Decimal("0.01"))

        item = {
            "numero_item": det.get("nItem"),
            "codigo_produto": _texto(prod, "cProd"),
            "codigo_ean": _texto(prod, "cEAN"),
            "descricao": _texto(prod, "xProd"),
            "ncm": _texto(prod, "NCM"),
            "cfop": _texto(prod, "CFOP"),
            "unidade_comercial": _texto(prod, "uCom"),
            "quantidade_comercial": float(qtd) if qtd is not None else None,
            "valor_unitario_comercial": float(vunit) if vunit is not None else None,
            "valor_total_produto": float(vtotal) if vtotal is not None else None,
            "codigo_beneficio_fiscal": _texto(prod, "cBenef"),
        }
        produtos.append(item)

    if not produtos:
        logger.warning("NF-e sem produtos identificados na chave %s", chave_acesso)

    # ----------------------------------------------------------------
    # Totais (total → ICMSTot)
    # ----------------------------------------------------------------
    total = infNFe.find(_tag("total"))
    icms_tot = total.find(_tag("ICMSTot")) if total is not None else None

    totais = {
        "valor_produtos": float(_decimal(icms_tot, "vProd") or 0),
        "valor_frete": float(_decimal(icms_tot, "vFrete") or 0),
        "valor_seguro": float(_decimal(icms_tot, "vSeg") or 0),
        "valor_desconto": float(_decimal(icms_tot, "vDesc") or 0),
        "valor_ipi": float(_decimal(icms_tot, "vIPI") or 0),
        "valor_icms": float(_decimal(icms_tot, "vICMS") or 0),
        "valor_total_nf": float(_decimal(icms_tot, "vNF") or 0),
    }

    return {
        "identificacao": identificacao,
        "emitente": emitente,
        "destinatario": destinatario,
        "produtos": produtos,
        "totais": totais,
        "fonte": "XML_NFE",
    }


# =====================================================================
# Salvar NF-e processada no banco
# =====================================================================

async def salvar_nfe_xml(dados: Dict[str, Any], usuario_id: uuid.UUID) -> Dict[str, Any]:
    """Salva os dados extraídos do XML de NF-e no banco de dados.

    Fluxo:
      1. Busca ou cria fornecedor pelo CNPJ do emitente.
      2. Para cada produto da NF-e, tenta associar a um insumo existente
         por código EAN ou descrição similar — nunca cria insumos
         automaticamente (requer revisão humana).
      3. Cria conta a pagar para o fornecedor com o valor total da NF-e.
      4. Retorna relatório de o que foi criado e o que ficou pendente.

    Args:
        dados: Dicionário retornado por parsear_xml_nfe().
        usuario_id: ID do usuário que disparou o processamento.

    Returns:
        Dicionário com fornecedor_id, conta_pagar_id, itens_associados,
        itens_pendentes (precisam de associação manual).
    """
    pool = get_pool()
    emit = dados["emitente"]
    ident = dados["identificacao"]
    totais = dados["totais"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ----------------------------------------------------------
            # 1. Busca ou cria fornecedor pelo CNPJ
            # ----------------------------------------------------------
            cnpj = emit.get("cnpj") or emit.get("cpf")
            fornecedor_id = None

            # Tenta buscar por CNPJ (campo metadados->>'cnpj' ou contato LIKE)
            if cnpj:
                fornecedor_id = await conn.fetchval(
                    "SELECT id FROM fornecedores WHERE contato ILIKE $1 AND ativo = TRUE LIMIT 1",
                    f"%{cnpj}%",
                )

            # Tenta buscar por razão social
            if not fornecedor_id and emit.get("razao_social"):
                fornecedor_id = await conn.fetchval(
                    "SELECT id FROM fornecedores WHERE nome ILIKE $1 AND ativo = TRUE LIMIT 1",
                    f"%{emit['razao_social'][:50]}%",
                )

            # Cria fornecedor se não encontrou
            if not fornecedor_id:
                nome_fornecedor = (
                    emit.get("nome_fantasia") or
                    emit.get("razao_social") or
                    f"Fornecedor CNPJ {cnpj}"
                )
                municipio = emit.get("municipio", "")
                uf = emit.get("uf", "")
                contato_info = f"CNPJ: {cnpj}"
                if municipio:
                    contato_info += f" | {municipio}/{uf}"
                if emit.get("telefone"):
                    contato_info += f" | Tel: {emit['telefone']}"

                fornecedor_id = await conn.fetchval(
                    """INSERT INTO fornecedores (nome, contato, ativo)
                       VALUES ($1, $2, TRUE) RETURNING id""",
                    nome_fornecedor[:200],
                    contato_info[:200],
                )
                logger.info(
                    "Fornecedor criado: %s (CNPJ: %s)",
                    nome_fornecedor, cnpj,
                )

            # ----------------------------------------------------------
            # 2. Tenta associar produtos a insumos existentes
            # ----------------------------------------------------------
            itens_associados = []
            itens_pendentes = []

            for item in dados["produtos"]:
                descricao = item.get("descricao", "")
                ean = item.get("codigo_ean")
                insumo_id = None

                # Tenta por EAN (código de barras) — mais confiável
                if ean and ean not in ("SEM GTIN", "0", "00000000000000"):
                    insumo_id = await conn.fetchval(
                        """SELECT id FROM insumos
                           WHERE marcas_aceitaveis @> ARRAY[$1]::text[]
                              OR apresentacao ILIKE $1
                           LIMIT 1""",
                        ean,
                    )

                # Tenta por descrição similar (trigram não disponível — usa ILIKE)
                if not insumo_id and descricao:
                    palavras = [p for p in descricao.split() if len(p) > 3][:3]
                    for palavra in palavras:
                        insumo_id = await conn.fetchval(
                            "SELECT id FROM insumos WHERE nome ILIKE $1 AND ativo = TRUE LIMIT 1",
                            f"%{palavra}%",
                        )
                        if insumo_id:
                            break

                if insumo_id:
                    itens_associados.append({
                        "insumo_id": str(insumo_id),
                        "descricao_nfe": descricao,
                        "quantidade": item.get("quantidade_comercial"),
                        "valor_unitario": item.get("valor_unitario_comercial"),
                        "valor_total": item.get("valor_total_produto"),
                        "unidade": item.get("unidade_comercial"),
                    })
                else:
                    itens_pendentes.append({
                        "descricao_nfe": descricao,
                        "codigo_produto": item.get("codigo_produto"),
                        "ean": ean,
                        "quantidade": item.get("quantidade_comercial"),
                        "valor_unitario": item.get("valor_unitario_comercial"),
                        "valor_total": item.get("valor_total_produto"),
                        "unidade": item.get("unidade_comercial"),
                        "ncm": item.get("ncm"),
                        "pendencia": "Insumo não encontrado — associação manual necessária",
                    })

            # ----------------------------------------------------------
            # 3. Cria conta a pagar
            # ----------------------------------------------------------
            descricao_conta = (
                f"NF-e {ident.get('numero', 'S/N')} "
                f"Série {ident.get('serie', '1')} — "
                f"{emit.get('razao_social') or emit.get('nome_fantasia', 'Fornecedor')}"
            )
            data_vencimento = ident.get("data_saida_entrada") or ident.get("data_emissao") or date.today()
            valor_total = totais.get("valor_total_nf") or totais.get("valor_produtos") or 0.0

            conta_pagar_id = await conn.fetchval(
                """INSERT INTO contas_pagar
                       (fornecedor_id, descricao, valor_original, data_vencimento, status)
                   VALUES ($1, $2, $3, $4, 'PENDENTE')
                   ON CONFLICT DO NOTHING
                   RETURNING id""",
                fornecedor_id,
                descricao_conta[:500],
                Decimal(str(valor_total)),
                data_vencimento,
            )

            if conta_pagar_id is None:
                # Conta já existia (ON CONFLICT) — busca o id
                conta_pagar_id = await conn.fetchval(
                    "SELECT id FROM contas_pagar WHERE descricao = $1 AND fornecedor_id = $2 LIMIT 1",
                    descricao_conta[:500],
                    fornecedor_id,
                )

    return {
        "fornecedor_id": str(fornecedor_id),
        "fornecedor_nome": emit.get("razao_social") or emit.get("nome_fantasia"),
        "fornecedor_cnpj": cnpj,
        "conta_pagar_id": str(conta_pagar_id) if conta_pagar_id else None,
        "valor_total_nf": valor_total,
        "data_emissao": str(ident.get("data_emissao") or ""),
        "numero_nfe": ident.get("numero"),
        "chave_acesso": ident.get("chave_acesso"),
        "itens_associados": itens_associados,
        "itens_pendentes": itens_pendentes,
        "resumo": {
            "total_itens": len(dados["produtos"]),
            "itens_associados": len(itens_associados),
            "itens_pendentes": len(itens_pendentes),
        },
        "fonte": "XML_NFE",
    }
