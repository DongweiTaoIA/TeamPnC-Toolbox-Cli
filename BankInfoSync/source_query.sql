-- Source query for BankInfoSync.
--
-- REQUIREMENTS:
--   * Must return one row per bank-info record to be evaluated against the target.
--   * MUST include a column named `ProducerCode` — used to verify the parent record
--     exists in pvt.dmProducer.
--   * MUST include a column named `ProductClientKey` — the key used to look up an
--     existing row in pvt.dmProducerBankInfo.
--   * All OTHER columns returned will be inserted into pvt.dmProducerBankInfo
--     (column names must match the target table exactly) AND used for the
--     "data difference" comparison when a target row already exists.
--
-- Example (replace with your real query):
--
SELECT db.DealerId, b.AccountNumber, B.AccountName, B.TransitNumber, b.BankNumber, b.IsForEFT, b.PaymentNotificationEmail
FROM (
    SELECT db.DealerId, db.BankInfoId,
           ROW_NUMBER() OVER (PARTITION BY db.DealerId ORDER BY b.StartDtTime DESC) AS rn
    FROM dps.dmDealerBankInfo db
    INNER JOIN dbo.dmDealer d ON db.DealerId = d.DealerId
    INNER JOIN dps.dmBankInfo b ON db.BankInfoId = b.Id
    WHERE d.ProvState = 'QC' AND d.IsDemoYN = 'N'
      AND GETDATE() BETWEEN b.StartDtTime AND ISNULL(b.EndDtTime, '9999-12-31')
) db
INNER JOIN dps.dmBankInfo b ON db.BankInfoId = b.Id
WHERE db.rn = 1
ORDER BY db.DealerId
