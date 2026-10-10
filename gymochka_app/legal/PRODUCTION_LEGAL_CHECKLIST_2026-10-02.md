# Нямбо — production legal checklist (02.10.2026)

## Already reflected in code/documents
- Operator: Плотников Владимир Константинович.
- Address: г. Самара, проспект Карла Маркса, 231.
- Email in archive: plotnikov.v.k.92@gmail.com; operator must confirm before publication.
- Separate personal-data consent, offer and marketing consent.
- Marketing consent is optional and revocable.
- Medical diagnoses/analysis results are not requested or stored.
- Startup migrations are additive. Legacy client fields are retained; no automatic data deletion is enabled.
- Production admin access must use numeric Telegram IDs.

## Must be completed outside the code before launch
1. Publish the four legal documents over HTTPS and configure their URLs in `.env`.
2. Verify the actual Russian database location and backup location used by the chosen Beget service.
3. Identify actual processors/recipients (Telegram, hosting, payment provider and any other service actually used) and document the data categories and purposes for each.
4. Determine whether any cross-border transfer occurs and document the applicable legal basis/notice.
5. Submit/verify the Roskomnadzor notification requirement before processing, unless a documented statutory exception applies.
6. Finalize the refund procedure against the actual payment provider and service-delivery model.
7. Keep tax/payment records for the applicable statutory period; do not delete records needed for tax control merely because the user profile is deleted.

The checklist is not a legal opinion and does not claim 100% compliance.
