-- Created by GitHub Copilot in SSMS - review carefully before executing
-- Template: Expire current GP007 rates and upload new rates
-- Step 1: Expire current GP007 rates by setting ExpiryDate to today

BEGIN TRANSACTION;

DECLARE @Today DATETIME = GETDATE();
DECLARE @NewEffectDate DATETIME = GETDATE();
DECLARE @NewExpiryDate DATETIME = '2099-12-31';
DECLARE @ModLoginId VARCHAR(50) = SUSER_SNAME();

-- Step 1: Expire existing GP007 rates
UPDATE r
SET r.ExpiryDate = @Today,
    r.ModDtTime = @Today,
    r.ModLoginId = @ModLoginId
FROM dbo.GP_cfRate r
INNER JOIN dbo.GP_cfRateDetl rd ON rd.RateKey = r.RateKey
WHERE rd.GpProgramId = 'GP007'
  AND r.ExpiryDate > @Today;

-- Step 2: Insert new rate header
INSERT INTO dbo.GP_cfRate (RateKey, EffectDate, ExpiryDate, ModDtTime, ModLoginId)
VALUES (
    (SELECT ISNULL(MAX(RateKey), 0) + 1 FROM dbo.GP_cfRate),  -- New RateKey (adjust if identity)
    @NewEffectDate,
    @NewExpiryDate,
    @Today,
    @ModLoginId
);

DECLARE @NewRateKey INT = (SELECT MAX(RateKey) FROM dbo.GP_cfRate);

-- Step 3: Insert new rate details for GP007
-- Copy this INSERT block for each rate line you need to add
INSERT INTO dbo.GP_cfRateDetl (RateDetlKey, RateKey, GpProgramId, CoverageType, FinancingType, PriceFrom, PriceTo, Term, OEMs, ProvStates, Rate, Province, ContractType, ModDtTime, ModLoginId)
VALUES (
    (SELECT ISNULL(MAX(RateDetlKey), 0) + 1 FROM dbo.GP_cfRateDetl),  -- New RateDetlKey
    @NewRateKey,
    'GP007',
    '<CoverageType>',   -- e.g. 'FULL'
    '<FinancingType>',  -- char
    0.00,               -- PriceFrom
    0.00,               -- PriceTo
    0,                  -- Term (smallint)
    NULL,               -- OEMs (nullable)
    NULL,               -- ProvStates (nullable)
    0.00,               -- Rate
    '<Province>',       -- char
    NULL,               -- ContractType (nullable)
    @Today,
    @ModLoginId
);

-- Repeat Step 3 INSERT for additional rate detail rows as needed

-- Review and then commit
-- COMMIT TRANSACTION;
-- Or rollback if something is wrong:
-- ROLLBACK TRANSACTION;