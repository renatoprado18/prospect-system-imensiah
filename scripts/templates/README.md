# Templates de Ata

Cada empresa pode ter um template customizado.
O template padrão está em `scripts/ata_to_docx.py`.

Para criar um template customizado:
1. Crie `scripts/templates/{nome}_ata.py`
2. Exporte `generate_ata_docx(ata_md, empresa_nome, data_reuniao, output_path)`
3. Configure `ata_template = "{nome}"` na empresa no ConselhoOS
