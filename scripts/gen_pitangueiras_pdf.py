"""Gera PDF da minuta Pitangueiras/DAIA-C para encaminhamento ao Deputado Arantes."""
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT, TA_CENTER
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

OUT = "/Users/rap/prospect-system/docs/Pitangueiras_Solicitacao_Secretario.pdf"

doc = SimpleDocTemplate(
    OUT, pagesize=A4,
    leftMargin=2.5 * cm, rightMargin=2.5 * cm,
    topMargin=2.2 * cm, bottomMargin=2.0 * cm,
    title="Solicitacao Fazenda Pitangueiras",
    author="Orestes Alves de Almeida Prado",
)

base = getSampleStyleSheet()

title = ParagraphStyle(
    "title", parent=base["Heading1"],
    fontName="Helvetica-Bold", fontSize=12, leading=15,
    alignment=TA_CENTER, spaceAfter=18,
)
section = ParagraphStyle(
    "section", parent=base["Heading2"],
    fontName="Helvetica-Bold", fontSize=11, leading=14,
    alignment=TA_LEFT, spaceBefore=10, spaceAfter=6,
)
body = ParagraphStyle(
    "body", parent=base["BodyText"],
    fontName="Helvetica", fontSize=10.5, leading=14,
    alignment=TA_JUSTIFY, spaceAfter=6,
)
bullet = ParagraphStyle(
    "bullet", parent=body,
    leftIndent=18, firstLineIndent=-12,
    spaceAfter=3, alignment=TA_LEFT,
)

story = []

story.append(Paragraph(
    "Solicitação de tramitação prioritária — DAIA-C (9,69 ha) e troca de áreas (10 ha)<br/>"
    "Procedimento MP/MG IC 04.16.0287.0070279/2024-84",
    title,
))

story.append(Paragraph("1. Situação", section))
story.append(Paragraph(
    "Conduzimos com o IEF de Poços de Caldas e a 3ª Promotoria Pública de Guaxupé "
    "processo corretivo originário do TAC firmado no âmbito do IC em referência, "
    "envolvendo as Fazendas Pitangueiras e Jaboticabeiras (contíguas). A Promotoria "
    "já manifestou que acompanhará a decisão do IEF.",
    body,
))

story.append(Paragraph("2. Solicitamos que o IEF/MG", section))
story.append(Paragraph(
    "<b>(a)</b> Conceda o <b>DAIA-C das glebas 6, 7 e 8 (9,69 ha)</b>, já protocolado "
    "sob processo 2100.01.0045291/2024-25 e aguardando decisão final desde nov/2025. "
    "Trata-se de área em estágio inicial de regeneração natural, atualmente improdutiva, "
    "correspondente a apenas 16% da área total da Fazenda Pitangueiras. Pedimos análise "
    "integrada, considerando o histórico de conformidade demonstrado.",
    body,
))
story.append(Paragraph(
    "<b>(b)</b> Acolha a <b>proposta de troca de 10 ha</b> — substituição de área "
    "comprovadamente sujeita a geadas recorrentes (inapta à cafeicultura sustentável) "
    "na Fazenda Jaboticabeiras (contígua) por área agronomicamente apta. A medida combina "
    "<b>ganho ambiental</b> (sem aumento líquido de área antropizada), <b>social</b> "
    "(geração de emprego direto na atividade cafeeira) e <b>econômico</b> (investimento "
    "produtivo e renda na região).",
    body,
))

story.append(Paragraph("3. Demonstração de boa-fé e adequações em curso", section))
boa_fe = [
    "TAC firmado e em cumprimento (IC 04.16.0287.0070279/2024-84);",
    "Adesão ao PECMA com pagamento integral da multa e reconhecimento dos termos legais;",
    "DAE de reposição florestal quitado;",
    "Retificação do CAR e detalhamento das Reservas Legais de ambas as fazendas;",
    "Reavaliação do barramento, com ajuste das áreas próximas a nascente;",
    "Revisão do estágio sucessional e relocação das parcelas testemunho — todas em "
    "execução pelo responsável técnico, com cronograma compartilhado com o "
    "IEF/Poços de Caldas.",
]
for item in boa_fe:
    story.append(Paragraph(f"•&nbsp;&nbsp;{item}", bullet))

story.append(Paragraph("4. Pedido objetivo", section))
story.append(Paragraph(
    "Designar interlocutor no IEF Central para destravar a análise integrada do DAIA-C "
    "e a tramitação prioritária do pedido de troca de áreas, evitando a fragmentação "
    "burocrática, sem prejuízo da observância técnica — abrindo igualmente espaço para "
    "a instrução do pedido de DAIA-C das glebas 1 a 5 sob solução de equivalência "
    "ambiental, hoje aguardando mesa técnica solicitada desde ago/2025.",
    body,
))

story.append(Paragraph("5. Por que importa", section))
story.append(Paragraph(
    "A indefinição prolongada compromete a viabilidade econômica de propriedade "
    "tradicional da cafeicultura sul-mineira, geradora de emprego e renda em Guaxupé. "
    "<b>A definição até o início da safra 2026/27 é determinante.</b>",
    body,
))

story.append(Paragraph(
    "Agradecemos a atenção e nos colocamos à disposição para esclarecimentos jurídicos "
    "por meio de nossa equipe envolvida.",
    body,
))

story.append(Spacer(1, 0.4 * cm))
story.append(Paragraph("Atenciosamente,", body))
story.append(Spacer(1, 1.0 * cm))
story.append(Paragraph("<b>Orestes Alves de Almeida Prado</b>", body))
story.append(Paragraph(
    "Arrendatário da Fazenda Pitangueiras e Proprietário da Fazenda Jaboticabeiras",
    body,
))

doc.build(story)
print(f"OK: {OUT}")
