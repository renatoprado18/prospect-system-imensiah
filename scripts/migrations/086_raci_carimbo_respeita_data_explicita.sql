-- 086 — o carimbo de conclusão datava pela ESCRITA, não pelo fato (UPDATE)
--
-- Achado em 07/10/2026 ao sincronizar uma decisão que o Renato tomou em 05/10:
-- o item #52 ("Política escrita de influenciadoras") foi concluído no dia 05,
-- com registro no grupo do cliente às 15h02 e o documento entregue às 15h01.
-- Ao gravar isso no INTEL dois dias depois, passando `concluido_em` explícito,
-- o trigger IGNOROU o valor e carimbou `now()` — 07/10 — com fonte 'gatilho'.
--
-- O defeito é de ASSIMETRIA, e o próprio comentário do INSERT já dizia o certo:
--
--     "Respeitar o valor explícito é o que permite migrar histórico sem o
--      trigger sobrescrever com hoje."
--
-- Exatamente essa necessidade existe no UPDATE — conclusão quase nunca é
-- registrada no instante em que acontece. Ela chega por relato no grupo, por
-- ata lida depois, por sessão que varre a semana. Ao datar pela escrita, o
-- trigger transformava "quando o cliente entregou" em "quando eu digitei", que
-- é a mesma classe de erro do check G da /cos (ordenar conversa pela data de
-- INGESTÃO em vez da de ENVIO) — e aqui contamina `concluido_no_prazo`, que é
-- derivado desta data: um item entregue no prazo e registrado uma semana depois
-- aparece como fora do prazo, contra o cliente.
--
-- A regra passa a ser a mesma nos dois caminhos: QUEM INFORMA A DATA, MANDA.
-- Sem data informada, carimba `now()` e marca 'gatilho' — que continua sendo o
-- caso comum e segue identificável, que é a razão de `concluido_em_fonte`
-- existir (distinguir o carimbado no ato do reconstruído por relato).
--
-- Reversível: basta reaplicar a versão anterior da função (ela está no corpo do
-- commit que acompanha esta migration).

CREATE OR REPLACE FUNCTION public.raci_carimba_conclusao()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Item que já nasce concluído (importação, ata retroativa): carimba,
        -- mas só se quem inseriu não trouxe a data.
        IF NEW.status::text = 'concluido' AND NEW.concluido_em IS NULL THEN
            NEW.concluido_em := now();
            NEW.concluido_em_fonte := 'gatilho';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status::text = 'concluido' AND OLD.status::text <> 'concluido' THEN
        -- 086: respeitar a data explícita TAMBÉM aqui. Conclusão quase nunca é
        -- registrada no instante em que acontece; datar pela escrita apaga
        -- quando o fato ocorreu e distorce `concluido_no_prazo`.
        IF NEW.concluido_em IS NULL OR NEW.concluido_em = OLD.concluido_em THEN
            NEW.concluido_em := now();
            NEW.concluido_em_fonte := 'gatilho';
        END IF;
    ELSIF NEW.status::text <> 'concluido' AND OLD.status::text = 'concluido' THEN
        NEW.concluido_em := NULL;
        NEW.concluido_em_fonte := NULL;
    END IF;
    -- UPDATE que não mexe no status não toca na data: sync que reescreve a
    -- linha inteira toda hora não pode reescrever quando o item fechou.
    RETURN NEW;
END;
$function$;
