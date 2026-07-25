# backend/app/ficha_tecnica_pdf.py — Sistema Dono
#
# PDF hierárquico para GET /pratos/{id}/ficha-tecnica?formato=pdf, nos 3
# templates definidos em arquitetura-sistema-restaurante.md §2.2.1:
# gerencial, insumo, operacional. Layout modelado DIRETAMENTE a partir
# dos 3 PDFs de exemplo fornecidos pelo usuário (Ficha_Técnica_Gerencial,
# _Insumo, _Operacional) — mesma ordem de seções, mesmos rótulos de
# bullet, mesma tabela de ingredientes no gerencial — não um layout
# inventado.
#
# Diferente de exportacao.py (tabelas planas de relatório, uma ou mais
# seções tabulares), aqui o conteúdo é hierárquico: cabeçalho de bullets,
# seções numeradas, e no gerencial uma tabela seguida de totais FORA da
# tabela (Custo Total dos Ingredientes / Margem de Desperdício / Custo
# Total da Receita), exatamente como no PDF de exemplo. Por isso um
# gerador dedicado, em vez de forçar essa estrutura no gerador tabular
# genérico — era exatamente essa mistura hierárquica que tinha sido
# apontada como o motivo de ficar de fora da rodada anterior.
import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.exportacao import COR_CABECALHO_HEX

_ESTILOS = getSampleStyleSheet()
_ESTILO_BULLET = ParagraphStyle(
    "Bullet", parent=_ESTILOS["Normal"], leftIndent=14, spaceAfter=4,
)


def _fmt_moeda(v: Any) -> str:
    if v is None:
        return "não informado"
    texto = f"{float(v):,.2f}"
    # "1,234.56" (en-US, saída padrão do f-string) -> "1.234,56" (pt-BR),
    # igual aos valores em R$ dos 3 PDFs de exemplo enviados.
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def _fmt_num(v: Any, casas: int = 3) -> str:
    if v is None:
        return "-"
    texto = f"{float(v):.{casas}f}"
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    if texto in ("", "-"):
        return "0"
    # pt-BR: vírgula decimal — mesma convenção de _fmt_moeda, e dos 3
    # PDFs de exemplo ("2,600 kg", "69,6%"), não o ponto do f-string.
    return texto.replace(".", ",")


def _bullets(historia: list, itens: list[str]) -> None:
    for texto in itens:
        historia.append(Paragraph(f"• {texto}", _ESTILO_BULLET))


def _texto_armazenamento(armazenamento: dict | None) -> str:
    """Reproduz a frase-padrão dos PDFs de exemplo ("Se preparado com
    antecedência, resfriar rapidamente e armazenar em recipiente
    hermético sob refrigeração (1°C a 4°C) por no máximo 24 horas."),
    preenchida com os dados reais do prato — ambos os exemplos (Insumo e
    Operacional) usam a MESMA frase, o que confirma que essa informação
    é do PRATO (pratos.armazenamento_faixa_temp / _tempo_max_h), não do
    insumo individualmente — o schema não tem esse dado por insumo."""
    if not armazenamento:
        return "Não informado."
    faixa = armazenamento.get("faixa_temp")
    tempo = armazenamento.get("tempo_max_h")
    if not faixa and not tempo:
        return "Não informado."
    frase = "Se preparado com antecedência, resfriar rapidamente e armazenar em recipiente hermético"
    if faixa:
        frase += f" sob refrigeração ({faixa})"
    if tempo:
        frase += f" por no máximo {tempo} horas"
    return frase + "."


def _estilo_tabela() -> TableStyle:
    # Mesma paleta de exportacao.py (relatórios tabulares) — identidade
    # visual única entre os dois módulos de exportação em PDF.
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{COR_CABECALHO_HEX}")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ])


def _extrair_passos(modo_preparo: Any) -> list[str]:
    """modo_preparo é JSONB — schema.sql só documenta como 'passos
    estruturados', sem contrato de shape fixo. Aceita string única,
    lista de strings, ou lista de dicts com alguma chave textual comum."""
    if not modo_preparo:
        return []
    if isinstance(modo_preparo, str):
        return [modo_preparo]
    passos: list[str] = []
    for item in modo_preparo:
        if isinstance(item, str):
            passos.append(item)
        elif isinstance(item, dict):
            texto = item.get("texto") or item.get("descricao") or item.get("passo") or item.get("instrucao")
            if texto:
                passos.append(str(texto))
    return passos


def gerar_pdf_ficha_tecnica(tipo: str, dados: dict[str, Any]) -> bytes:
    """tipo: 'gerencial' | 'insumo' | 'operacional'. `dados` é o MESMO
    dict que routes/pratos.py já monta para a resposta JSON de cada
    tipo — nenhuma consulta nova ao banco acontece aqui, só layout."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, topMargin=40, bottomMargin=40, leftMargin=36, rightMargin=36,
        title=f"Ficha Técnica {tipo.capitalize()}",
    )
    historia: list = []

    if tipo == "gerencial":
        _montar_gerencial(historia, dados)
    elif tipo == "insumo":
        _montar_insumo(historia, dados)
    else:
        _montar_operacional(historia, dados)

    doc.build(historia)
    return buffer.getvalue()


def _montar_gerencial(historia: list, d: dict) -> None:
    historia.append(Paragraph("Ficha Técnica Gerencial", _ESTILOS["Title"]))
    historia.append(Spacer(1, 8))
    _bullets(historia, [
        f"Nome do Prato: {d['nome_prato']}",
        f"Rendimento da Receita Base: {_fmt_num(d['rendimento_base_porcoes'], 0)} porções",
        (f"Tempo de Preparo: {d['tempo_preparo_min']} minutos"
         if d.get("tempo_preparo_min") is not None else "Tempo de Preparo: não informado"),
        f"Custo por Porção: {_fmt_moeda(d['custo_total_porcao'])}",
    ])
    historia.append(Spacer(1, 10))

    historia.append(Paragraph("1. Tabela de Ingredientes e Custos", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    cabecalho = ["Ingrediente", "Peso Bruto (PB)", "Unidade", "Custo Unitário (R$)", "Custo Total (R$)"]
    linhas = [cabecalho] + [
        [ing["nome"], _fmt_num(ing["peso_bruto"]), ing["unidade"],
         _fmt_moeda(ing["custo_unitario"]), _fmt_moeda(ing["custo_total"])]
        for ing in d["ingredientes"]
    ]
    tabela = Table(linhas, repeatRows=1, hAlign="LEFT")
    tabela.setStyle(_estilo_tabela())
    historia.append(tabela)
    historia.append(Spacer(1, 10))

    custo_margem = d["custo_total_receita"] - d["custo_total_ingredientes"]
    _bullets(historia, [
        f"Custo Total dos Ingredientes: {_fmt_moeda(d['custo_total_ingredientes'])}",
        f"Margem de Desperdício / Custo Invisível ({_fmt_num(d['margem_desperdicio_pct'], 1)}%): {_fmt_moeda(custo_margem)}",
        f"CUSTO TOTAL DA RECEITA: {_fmt_moeda(d['custo_total_receita'])}",
    ])

    historia.append(Spacer(1, 16))
    historia.append(Paragraph("Resumo Financeiro (Por Porção)", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    margem = d.get("margem_lucro_bruta_pct")
    _bullets(historia, [
        f"Custo de Insumos (CMV): {_fmt_moeda(d['cmv_por_porcao'])}",
        f"Embalagem: {_fmt_moeda(d['custo_embalagem'])}",
        f"Custo Total da Porção: {_fmt_moeda(d['custo_total_porcao'])}",
        f"Preço de Venda Praticado: {_fmt_moeda(d['preco_venda_praticado'])}",
        (f"Margem de Lucro Bruta: {_fmt_num(margem, 1)}%" if margem is not None
         else "Margem de Lucro Bruta: não calculável (sem preço de venda cadastrado)"),
    ])


def _montar_insumo(historia: list, d: dict) -> None:
    historia.append(Paragraph(f"Ficha Técnica Insumo — {d['nome_insumo']}", _ESTILOS["Title"]))
    historia.append(Spacer(1, 8))
    _bullets(historia, [
        f"Nome do Insumo: {d['nome_insumo']}",
        f"Categoria: {d['categoria']}",
        f"Peso Bruto (PB): {_fmt_num(d['peso_bruto'])} {d['unidade']}",
        f"Unidade: {d['unidade']}",
        f"Custo Unitário (R$): {_fmt_moeda(d['custo_unitario'])}",
        f"Custo Total (R$): {_fmt_moeda(d['custo_total'])}",
        (f"Data de Atualização: {d['atualizado_em']}" if d.get("atualizado_em") else "Data de Atualização: não informado"),
    ])
    historia.append(Spacer(1, 10))
    historia.append(Paragraph("1. Informações de Controle e Armazenamento", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    equipamentos = d.get("equipamentos_utilizados") or []
    _bullets(historia, [
        "Equipamentos Utilizados: " + (", ".join(equipamentos) if equipamentos else "não informado"),
        "Armazenamento Pré-Evento: " + _texto_armazenamento(d.get("armazenamento")),
    ])


def _montar_operacional(historia: list, d: dict) -> None:
    historia.append(Paragraph("Ficha Técnica Operacional", _ESTILOS["Title"]))
    historia.append(Spacer(1, 8))
    equipamentos = d.get("equipamentos_utilizados") or []
    _bullets(historia, [
        f"Nome do Prato: {d['nome_prato']}",
        (f"Tempo de Preparo: {d['tempo_preparo_min']} minutos"
         if d.get("tempo_preparo_min") is not None else "Tempo de Preparo: não informado"),
        f"Rendimento: {_fmt_num(d['rendimento'], 0)} porção(ões)",
        "Equipamentos Utilizados: " + (", ".join(equipamentos) if equipamentos else "não informado"),
    ])
    historia.append(Spacer(1, 10))

    historia.append(Paragraph("1. Ingredientes e Quantidades", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    linhas_ingredientes = [
        f"{ing['nome']}: {_fmt_num(ing['quantidade'])} {ing['unidade']}" for ing in d["ingredientes"]
    ]
    _bullets(historia, linhas_ingredientes or ["Nenhum ingrediente cadastrado."])

    historia.append(Spacer(1, 10))
    historia.append(Paragraph("2. Modo de Preparo", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    passos = _extrair_passos(d.get("modo_preparo"))
    if passos:
        for i, passo in enumerate(passos, start=1):
            historia.append(Paragraph(f"{i}. {passo}", _ESTILO_BULLET))
    else:
        historia.append(Paragraph("Modo de preparo não cadastrado.", _ESTILOS["Italic"]))

    historia.append(Spacer(1, 10))
    historia.append(Paragraph("3. Instruções de Apresentação", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    instrucoes = d.get("instrucoes_apresentacao")
    if instrucoes:
        linhas_instrucoes = [linha.strip("•- ").strip() for linha in str(instrucoes).splitlines()]
        for linha in linhas_instrucoes:
            if linha:
                historia.append(Paragraph(f"• {linha}", _ESTILO_BULLET))
    else:
        historia.append(Paragraph("Não informado.", _ESTILOS["Italic"]))

    historia.append(Spacer(1, 10))
    historia.append(Paragraph("4. Informações de Controle e Armazenamento", _ESTILOS["Heading2"]))
    historia.append(Spacer(1, 4))
    _bullets(historia, [
        (f"Temperatura de Serviço: {d['temperatura_servico']}" if d.get("temperatura_servico")
         else "Temperatura de Serviço: não informado"),
        "Armazenamento Pré-Evento: " + _texto_armazenamento(d.get("armazenamento")),
    ])
