# backend/app/ocr.py — Sistema Dono
#
# Módulo OCR (Optical Character Recognition) para processamento de notas fiscais.
# Utiliza Tesseract (pytesseract) e PaddleOCR para extração de texto,
# e expressões regulares para parsear campos estruturados (CNPJ, itens, valores).
#
# ATUALIZAÇÃO (Fase 7): Módulo novo.
# Responsabilidades:
#   - Extrair texto de PDFs e imagens.
#   - Parsear dados estruturados de notas fiscais (fornecedor, CNPJ, itens).
#   - Salvar dados processados no banco (criação de fornecedores, insumos, lotes, contas a pagar).
#
# Dependências externas (instaladas via requirements.txt):
#   - pytesseract (OCR básico)
#   - paddleocr (OCR avançado para documentos estruturados)
#   - pdf2image (conversão PDF → imagem)
#   - Pillow (manipulação de imagens)

import io
import re
import uuid
import logging
from typing import Dict, Any, List, Optional
from datetime import date, datetime

import pytesseract
from PIL import Image
from pdf2image import convert_from_bytes
import asyncpg

from app.database import get_pool

logger = logging.getLogger(__name__)

# Inicialização do PaddleOCR (lazy loading para não impactar startup)
_ocr = None


def get_ocr():
    """Retorna a instância do PaddleOCR (lazy loading).
    O PaddleOCR é mais preciso para notas fiscais, mas mais pesado.
    """
    global _ocr
    if _ocr is None:
        try:
            logger.info("Inicializando PaddleOCR (pode levar alguns segundos)...")
            _ocr = paddleocr.PaddleOCR(use_angle_cls=True, lang='pt', show_log=False)
            logger.info("PaddleOCR inicializado")
        except Exception as e:
            logger.warning(
                "PaddleOCR nao disponivel (%s) -- OCR degradado para Tesseract apenas.", e
            )
            return None
    return _ocr


# =====================================================================
# Extração de Texto (OCR)
# =====================================================================

async def extrair_texto_imagem(imagem_bytes: bytes) -> str:
    """Extrai texto de uma imagem usando Tesseract (primeiro) e PaddleOCR (fallback).
    
    Args:
        imagem_bytes: Conteúdo da imagem em bytes (PNG, JPEG, etc.)
    
    Returns:
        Texto extraído da imagem.
    
    Raises:
        ValueError: Se nenhum texto for extraído ou ambos os OCRs falharem.
    """
    try:
        # 1. Tenta com Tesseract (mais leve e rápido)
        img = Image.open(io.BytesIO(imagem_bytes))
        texto = pytesseract.image_to_string(img, lang='por')
        if texto.strip():
            logger.info("Tesseract extraiu texto com sucesso (%d caracteres)", len(texto))
            return texto
    except Exception as e:
        logger.warning("Tesseract falhou: %s, tentando PaddleOCR", e)

    # 2. Fallback com PaddleOCR (mais preciso para notas fiscais)
    ocr = get_ocr()
    if ocr is None:
        raise ValueError(
            "Tesseract nao extraiu texto e PaddleOCR nao esta disponivel. "
            "Verifique os logs de inicializacao do OCR."
        )
    try:
        # PaddleOCR espera o caminho do arquivo ou imagem em array
        resultado = ocr.ocr(imagem_bytes, cls=True)
        if resultado and len(resultado) > 0:
            texto = "\n".join([line[1][0] for line in resultado[0]])
            if texto.strip():
                logger.info("PaddleOCR extraiu texto com sucesso (%d caracteres)", len(texto))
                return texto
    except Exception as e:
        logger.error("PaddleOCR falhou: %s", e)
        raise ValueError(f"Falha na extração de texto com ambos os OCRs: {e}")

    raise ValueError("Nenhum texto extraído da imagem")


async def extrair_texto_pdf(pdf_bytes: bytes) -> str:
    """Converte PDF para imagens e extrai texto de cada página.
    
    Args:
        pdf_bytes: Conteúdo do PDF em bytes.
    
    Returns:
        Texto concatenado de todas as páginas.
    
    Raises:
        ValueError: Se o PDF não puder ser processado ou nenhum texto extraído.
    """
    try:
        # Converte PDF para imagens (DPI 200 para equilíbrio entre qualidade e performance)
        images = convert_from_bytes(pdf_bytes, dpi=200)
        if not images:
            raise ValueError("Nenhuma imagem extraída do PDF")

        textos = []
        for i, img in enumerate(images):
            # Converte PIL Image para bytes (PNG)
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            texto_pagina = await extrair_texto_imagem(buf.getvalue())
            textos.append(f"--- Página {i+1} ---\n{texto_pagina}")

        texto_completo = "\n".join(textos)
        if texto_completo.strip():
            return texto_completo
        raise ValueError("Nenhum texto extraído das páginas do PDF")

    except Exception as e:
        logger.error("Falha ao processar PDF: %s", e)
        raise ValueError(f"Falha no processamento do PDF: {e}")


# =====================================================================
# Parseamento de Dados da Nota Fiscal
# =====================================================================

def extrair_cnpj(texto: str) -> Optional[str]:
    """Extrai CNPJ do texto usando regex.
    Suporta formatos com e sem pontuação.
    
    Exemplos:
        - 12.345.678/0001-99 -> 12345678000199
        - 12345678000199 -> 12345678000199
    """
    # Tentativa com máscara completa
    match = re.search(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', texto)
    if match:
        return match.group(0).replace('.', '').replace('/', '').replace('-', '')
    # Tentativa com 14 dígitos consecutivos
    match = re.search(r'\d{14}', texto)
    return match.group(0) if match else None


def extrair_fornecedor(texto: str) -> Optional[str]:
    """Tenta extrair o nome do fornecedor (antes do CNPJ, geralmente)."""
    # Procura por marcadores comuns em notas fiscais
    marcadores = ['Fornecedor:', 'Emitente:', 'VENDEDOR', 'FORNECEDOR', 'Nome/Razão Social']
    for marcador in marcadores:
        idx = texto.find(marcador)
        if idx != -1:
            # Pega até 200 caracteres após o marcador
            trecho = texto[idx: idx + 200]
            # Se encontrar CNPJ, corta antes dele
            if 'CNPJ' in trecho:
                trecho = trecho[:trecho.find('CNPJ')]
            # Remove o marcador e limpa
            nome = re.sub(r'^[\w:]*\s*', '', trecho).strip()
            # Remove caracteres estranhos no início
            nome = re.sub(r'^[^A-Za-zÀ-Úà-ú]', '', nome)
            if nome and len(nome) > 2:
                return nome
    return None


def extrair_insumos(texto: str) -> List[Dict[str, Any]]:
    """Extrai lista de itens/insumos da nota fiscal.
    
    Procura por padrões comuns: descrição seguida de quantidade e valor.
    Exemplo: "Filé Mignon 2,500 kg R$ 45,90"
    
    Returns:
        Lista de dicionários com 'nome', 'quantidade', 'valor_unitario', 'valor_total'.
    """
    itens = []
    linhas = texto.splitlines()

    for linha in linhas:
        # Critérios para identificar uma linha de item:
        # 1. Contém quantidade (ex.: 1,500 ou 2,000)
        # 2. Contém valor unitário ou total (ex.: R$ 15,90)
        has_quantidade = re.search(r'\b\d+,\d{3}\b', linha)
        has_valor = re.search(r'R\$\s*\d+,\d{2}', linha)

        if has_quantidade and has_valor:
            # Tenta extrair nome (remove números e valores)
            nome = re.sub(r'\b\d+,\d{3}\b', '', linha)      # remove quantidade
            nome = re.sub(r'R\$\s*\d+,\d{2}', '', nome)      # remove valor
            nome = re.sub(r'[^A-Za-zÀ-Úà-ú\s]', '', nome)   # remove caracteres especiais
            nome = ' '.join(nome.split()).strip()            # normaliza espaços

            if nome and len(nome) > 2:
                # Extrai quantidade (ex.: '2,500' -> 2.5)
                qtd_match = re.search(r'(\d+,\d{3})', linha)
                qtd = float(qtd_match.group(1).replace(',', '.')) if qtd_match else None

                # Extrai valor unitário ou total
                valor_match = re.search(r'R\$\s*(\d+,\d{2})', linha)
                valor = float(valor_match.group(1).replace(',', '.')) if valor_match else None

                if qtd and valor:
                    itens.append({
                        "nome": nome,
                        "quantidade": qtd,
                        "valor_unitario": valor,
                        "valor_total": round(qtd * valor, 2)
                    })

    return itens


# =====================================================================
# Orquestração: Extrair e Salvar
# =====================================================================

async def extrair_dados_nota(arquivo_bytes: bytes) -> Dict[str, Any]:
    """Extrai dados estruturados de uma nota fiscal (PDF ou imagem).
    
    Args:
        arquivo_bytes: Conteúdo do arquivo (PDF ou imagem).
    
    Returns:
        Dict com os campos extraídos: fornecedor, cnpj, data_emissao, itens.
    
    Raises:
        ValueError: Se não for possível extrair texto ou dados suficientes.
    """
    # 1. Determina o tipo pelo magic number (PDF ou imagem)
    is_pdf = arquivo_bytes[:4] == b'%PDF'
    if is_pdf:
        texto = await extrair_texto_pdf(arquivo_bytes)
    else:
        texto = await extrair_texto_imagem(arquivo_bytes)

    if not texto.strip():
        raise ValueError("Nenhum texto extraído do arquivo")

    logger.info("Texto extraído (primeiros 500 chars): %s...", texto[:500])

    # 2. Parseia os campos
    fornecedor = extrair_fornecedor(texto) or "Fornecedor não identificado"
    cnpj = extrair_cnpj(texto)
    itens = extrair_insumos(texto)

    if not itens:
        raise ValueError("Nenhum item identificado na nota fiscal (verifique se o arquivo é uma nota válida)")

    # 3. Tenta extrair data de emissão (opcional)
    data_match = re.search(r'(\d{2}/\d{2}/\d{4})', texto)
    data_emissao = data_match.group(1) if data_match else date.today().strftime('%d/%m/%Y')

    return {
        "fornecedor": fornecedor,
        "cnpj": cnpj,
        "data_emissao": data_emissao,
        "itens": itens,
        "texto_completo": texto  # útil para debug, mas não salvo no banco
    }


async def salvar_nota_processada(dados: Dict[str, Any], usuario_id: uuid.UUID) -> Dict[str, Any]:
    """Salva os dados extraídos da nota no banco de dados.
    
    Fluxo:
        1. Busca ou cria fornecedor (por CNPJ ou nome).
        2. Para cada item: busca ou cria insumo.
        3. Cria lote de insumo com valor e quantidade.
        4. Cria conta a pagar para o fornecedor.
    
    Args:
        dados: Dict com fornecedor, cnpj, itens (retornado por extrair_dados_nota).
        usuario_id: ID do usuário que processou (para auditoria).
    
    Returns:
        Dict com os IDs dos itens criados (insumo, lote, conta).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Busca ou cria fornecedor
        fornecedor_id = None

        # Tenta buscar por CNPJ (se disponível)
        if dados.get("cnpj"):
            fornecedor_id = await conn.fetchval(
                "SELECT id FROM fornecedores WHERE cnpj = $1",
                dados["cnpj"]
            )

        # Se não encontrou por CNPJ, busca por nome (exato ou similar)
        if not fornecedor_id:
            fornecedor_id = await conn.fetchval(
                "SELECT id FROM fornecedores WHERE nome ILIKE $1 LIMIT 1",
                f"%{dados['fornecedor']}%"
            )

        # Se ainda não encontrou, cria novo fornecedor
        if not fornecedor_id:
            logger.info("Criando novo fornecedor: %s", dados["fornecedor"])
            fornecedor_id = await conn.fetchval(
                """INSERT INTO fornecedores (nome, contato, ativo)
                   VALUES ($1, $2, TRUE) RETURNING id""",
                dados["fornecedor"],
                f"CNPJ: {dados.get('cnpj', 'N/A')}"
            )

        # 2. Para cada item, cria insumo (se não existir) e lote
        itens_criados = []
        # Busca uma categoria padrão para insumos (Secos e Despensa)
        categoria_padrao_id = await conn.fetchval(
            "SELECT id FROM categorias WHERE nome = 'Secos e Despensa' LIMIT 1"
        )
        if not categoria_padrao_id:
            # Fallback: usa a primeira categoria disponível
            categoria_padrao_id = await conn.fetchval(
                "SELECT id FROM categorias LIMIT 1"
            )

        for item in dados["itens"]:
            # Busca insumo pelo nome (case-insensitive)
            insumo_id = await conn.fetchval(
                "SELECT id FROM insumos WHERE nome ILIKE $1 LIMIT 1",
                f"%{item['nome']}%"
            )

            if not insumo_id:
                logger.info("Criando novo insumo: %s", item["nome"])
                # Define unidade padrão (KG, mas pode ser refinado)
                unidade = "KG" if "kg" in item["nome"].lower() or "quilo" in item["nome"].lower() else "PC"
                insumo_id = await conn.fetchval(
                    """INSERT INTO insumos (nome, categoria_id, unidade, ativo, consumivel)
                       VALUES ($1, $2, $3, TRUE, TRUE) RETURNING id""",
                    item["nome"],
                    categoria_padrao_id,
                    unidade
                )

            # Cria lote de insumo
            lote_id = await conn.fetchval(
                """INSERT INTO lotes_insumo (insumo_id, fornecedor_id, valor_aquisicao,
                                             data_aquisicao, quantidade, quantidade_disponivel)
                   VALUES ($1, $2, $3, CURRENT_DATE, $4, $4) RETURNING id""",
                insumo_id,
                fornecedor_id,
                item["valor_unitario"],
                item["quantidade"]
            )

            # Cria conta a pagar (simplificado: vencimento em 30 dias)
            conta_id = await conn.fetchval(
                """INSERT INTO contas_pagar (fornecedor_id, descricao, valor_original,
                                             data_vencimento, status, criado_em)
                   VALUES ($1, $2, $3, CURRENT_DATE + INTERVAL '30 days', 'PENDENTE', now())
                   RETURNING id""",
                fornecedor_id,
                f"Compra de {item['nome']} - Nota Fiscal",
                item["valor_total"]
            )

            itens_criados.append({
                "insumo_id": str(insumo_id),
                "lote_id": str(lote_id),
                "conta_id": str(conta_id),
                "nome": item["nome"],
                "quantidade": item["quantidade"],
                "valor_unitario": item["valor_unitario"]
            })

        return {
            "fornecedor": dados["fornecedor"],
            "fornecedor_id": str(fornecedor_id),
            "cnpj": dados.get("cnpj"),
            "itens": itens_criados,
            "total_itens": len(itens_criados)
        }