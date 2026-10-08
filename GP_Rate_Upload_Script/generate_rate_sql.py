"""
Generate SQL script for GP Rate Upload from Excel rate matrix.

Reads the 'Dlr Cost' worksheet from a GAP rates Excel file, parses Purchase/Lease
rate matrices, and generates a SQL script that:
1. Loads new rates into a temp table
2. Compares against current active rates
3. If changes detected: expires current rates and inserts new ones
4. If no changes: prints message and takes no action
"""

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook


def parse_price_range(text):
    """Parse a price range string like '$0 - $10,000' into (price_from, price_to)."""
    if not text:
        return None, None
    # Remove $ and commas, extract numbers
    numbers = re.findall(r'[\d,]+', str(text).replace(',', ''))
    if len(numbers) >= 2:
        return int(numbers[0]), int(numbers[1])
    return None, None


def parse_term(text):
    """Parse a term header like '24 Months' into integer 24."""
    if not text:
        return None
    match = re.search(r'(\d+)', str(text))
    return int(match.group(1)) if match else None


def parse_rate_value(cell_value):
    """Parse a rate cell value like '$336' or 336 into float 336.00."""
    if cell_value is None:
        return None
    if isinstance(cell_value, (int, float)):
        return float(cell_value)
    # Remove $ and commas
    cleaned = str(cell_value).replace('$', '').replace(',', '').strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def find_section_start(ws, section_name, start_row=1):
    """Find the row where a section (e.g., 'Purchase Rate' or 'Lease Rate') starts."""
    for row in ws.iter_rows(min_row=start_row, max_col=20):
        for cell in row:
            if cell.value and section_name.lower() in str(cell.value).lower():
                return cell.row
    return None


def find_header_row(ws, start_row):
    """Find the header row with term columns starting from a given row.
    Looks for a row containing 'Month' in multiple cells."""
    for row_idx in range(start_row, start_row + 10):
        month_count = 0
        for col_idx in range(1, 20):
            val = ws.cell(row=row_idx, column=col_idx).value
            if val and 'month' in str(val).lower():
                month_count += 1
        if month_count >= 3:
            return row_idx
    return None


def parse_terms_from_header(ws, header_row):
    """Parse term values and their column indices from the header row."""
    terms = []
    for col_idx in range(1, 30):
        val = ws.cell(row=header_row, column=col_idx).value
        if val and 'month' in str(val).lower():
            term = parse_term(val)
            if term:
                terms.append((col_idx, term))
    return terms


def find_price_column(ws, header_row):
    """Find the column that contains price range data (Amount Financed)."""
    for col_idx in range(1, 10):
        val = ws.cell(row=header_row, column=col_idx).value
        if val and ('amount' in str(val).lower() or 'finance' in str(val).lower()):
            return col_idx
    # Default: check the row below header for price-range-like values
    for col_idx in range(1, 10):
        val = ws.cell(row=header_row + 1, column=col_idx).value
        if val and '$' in str(val) and '-' in str(val):
            return col_idx
    return 1


def parse_rate_section(ws, section_start_row, financing_type):
    """Parse a rate section (Purchase or Lease) and return list of rate records."""
    header_row = find_header_row(ws, section_start_row)
    if not header_row:
        print(f"  WARNING: Could not find header row for section starting at row {section_start_row}")
        return []

    terms = parse_terms_from_header(ws, header_row)
    if not terms:
        print(f"  WARNING: No term columns found in header row {header_row}")
        return []

    price_col = find_price_column(ws, header_row)
    print(f"  Header row: {header_row}, Price column: {price_col}")
    print(f"  Terms found: {[t[1] for t in terms]}")

    rates = []
    row_idx = header_row + 1

    while True:
        price_cell = ws.cell(row=row_idx, column=price_col).value
        if not price_cell:
            break
        # Check if this looks like a price range
        if '$' not in str(price_cell) and not re.search(r'\d', str(price_cell)):
            break

        price_from, price_to = parse_price_range(price_cell)
        if price_from is None:
            row_idx += 1
            continue

        for col_idx, term in terms:
            rate_value = parse_rate_value(ws.cell(row=row_idx, column=col_idx).value)
            if rate_value is not None:
                rates.append({
                    'financing_type': financing_type,
                    'price_from': price_from,
                    'price_to': price_to,
                    'term': term,
                    'rate': rate_value
                })

        row_idx += 1

    print(f"  Parsed {len(rates)} rate records")
    return rates


def generate_sql(rates, program_id, coverage_type, provinces, prod_effect_date):
    """Generate the SQL script with temp table, change detection, and conditional insert."""
    total_rows = len(rates) * len(provinces)
    lines = []
    lines.append("-- Auto-generated by generate_rate_sql.py")
    lines.append(f"-- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"-- Program: {program_id}, Coverage: {coverage_type}, Provinces: {', '.join(provinces)}")
    lines.append(f"-- PROD Effect Date: {prod_effect_date}")
    lines.append(f"-- Total rate rows: {total_rows}")
    lines.append("")
    lines.append("BEGIN TRY")
    lines.append("BEGIN TRANSACTION;")
    lines.append("")
    lines.append("DECLARE @Today DATETIME = GETDATE();")
    lines.append(f"DECLARE @NewEffectDate DATETIME = IIF(DB_NAME() = 'DPPROD', '{prod_effect_date}', CAST(GETDATE() AS DATE));")
    lines.append("DECLARE @NewExpiryDate DATETIME = '9999-12-31';")
    lines.append("DECLARE @ModLoginId VARCHAR(50) = 'DOT';")
    lines.append("")

    # Step 1: Load rate matrix into temp table (only varying columns)
    lines.append("-- Step 1: Load rate matrix into temp table")
    lines.append("CREATE TABLE #RateMatrix (")
    lines.append("    FinancingType CHAR(1),")
    lines.append("    PriceFrom DECIMAL(18,2),")
    lines.append("    PriceTo DECIMAL(18,2),")
    lines.append("    Term SMALLINT,")
    lines.append("    Rate DECIMAL(18,2)")
    lines.append(");")
    lines.append("")
    lines.append("INSERT INTO #RateMatrix (FinancingType, PriceFrom, PriceTo, Term, Rate)")
    lines.append("VALUES")

    value_lines = []
    for r in rates:
        value_lines.append(
            f"    ('{r['financing_type']}', "
            f"{r['price_from']:.2f}, {r['price_to']:.2f}, {r['term']}, "
            f"{r['rate']:.2f})"
        )
    lines.append(",\n".join(value_lines) + ";")
    lines.append("")

    # Expand into full #NewRates by cross-joining with provinces and adding fixed values
    lines.append("-- Expand rate matrix with fixed values and provinces")
    lines.append("SELECT")
    lines.append(f"    '{program_id}' AS GpProgramId,")
    lines.append(f"    '{coverage_type}' AS CoverageType,")
    lines.append("    rm.FinancingType,")
    lines.append("    rm.PriceFrom,")
    lines.append("    rm.PriceTo,")
    lines.append("    rm.Term,")
    lines.append("    rm.Rate,")
    lines.append("    p.Province")
    lines.append("INTO #NewRates")
    lines.append("FROM #RateMatrix rm")
    lines.append("CROSS JOIN (VALUES " + ", ".join(f"('{prov}')" for prov in provinces) + ") AS p(Province);")
    lines.append("")

    # Step 2: Change detection
    lines.append("-- Step 2: Check for changes against current active rates")
    lines.append("DECLARE @HasChanges BIT = 0;")
    lines.append("")
    lines.append("-- Check if any new rate differs from existing active rates")
    lines.append("IF EXISTS (")
    lines.append("    -- New rates that don't match existing active rates (different Rate or missing)")
    lines.append("    SELECT 1")
    lines.append("    FROM #NewRates n")
    lines.append("    LEFT JOIN (")
    lines.append("        SELECT rd.*")
    lines.append("        FROM dbo.GP_cfRateDetl rd")
    lines.append("        INNER JOIN dbo.GP_cfRate r ON r.RateKey = rd.RateKey AND r.ExpiryDate = '9999-12-31'")
    lines.append("    ) rd")
    lines.append(f"        ON rd.GpProgramId = n.GpProgramId")
    lines.append("        AND rd.CoverageType = n.CoverageType")
    lines.append("        AND rd.FinancingType = n.FinancingType")
    lines.append("        AND rd.PriceFrom = n.PriceFrom")
    lines.append("        AND rd.PriceTo = n.PriceTo")
    lines.append("        AND rd.Term = n.Term")
    lines.append("        AND rd.Province = n.Province")
    lines.append("    WHERE rd.RateDetlKey IS NULL OR rd.Rate != n.Rate")
    lines.append(")")
    lines.append("OR EXISTS (")
    lines.append("    -- Existing active rates not in new data (removed rates)")
    lines.append("    SELECT 1")
    lines.append("    FROM dbo.GP_cfRateDetl rd")
    lines.append("    INNER JOIN dbo.GP_cfRate r ON r.RateKey = rd.RateKey AND r.ExpiryDate = '9999-12-31'")
    lines.append("    LEFT JOIN #NewRates n")
    lines.append(f"        ON n.GpProgramId = rd.GpProgramId")
    lines.append("        AND n.CoverageType = rd.CoverageType")
    lines.append("        AND n.FinancingType = rd.FinancingType")
    lines.append("        AND n.PriceFrom = rd.PriceFrom")
    lines.append("        AND n.PriceTo = rd.PriceTo")
    lines.append("        AND n.Term = rd.Term")
    lines.append("        AND n.Province = rd.Province")
    lines.append(f"    WHERE rd.GpProgramId = '{program_id}' AND n.GpProgramId IS NULL")
    lines.append(")")
    lines.append("BEGIN")
    lines.append("    SET @HasChanges = 1;")
    lines.append("END")
    lines.append("")

    # Step 3: Conditional expire and insert
    lines.append("IF @HasChanges = 1")
    lines.append("BEGIN")
    lines.append("    PRINT 'Changes detected. Expiring current rates and inserting new ones.';")
    lines.append("")
    lines.append("    -- Step 3: Expire all existing active rates for this program")
    lines.append("    UPDATE r")
    lines.append("    SET r.ExpiryDate = DATEADD(SECOND, -1, @NewEffectDate),")
    lines.append("        r.ModDtTime = @Today,")
    lines.append("        r.ModLoginId = @ModLoginId")
    lines.append("    FROM dbo.GP_cfRate r")
    lines.append("    INNER JOIN dbo.GP_cfRateDetl rd ON rd.RateKey = r.RateKey")
    lines.append(f"    WHERE rd.GpProgramId = '{program_id}'")
    lines.append("      AND r.ExpiryDate = '9999-12-31';")
    lines.append("")
    lines.append("    -- Step 4: Insert new rate header")
    lines.append("    DECLARE @NewRateKey INT = (SELECT ISNULL(MAX(RateKey), 0) + 1 FROM dbo.GP_cfRate);")
    lines.append("")
    lines.append("    INSERT INTO dbo.GP_cfRate (RateKey, EffectDate, ExpiryDate, ModDtTime, ModLoginId)")
    lines.append("    VALUES (@NewRateKey, @NewEffectDate, @NewExpiryDate, @Today, @ModLoginId);")
    lines.append("")
    lines.append("    -- Step 5: Insert all new rate details from temp table")
    lines.append("")
    lines.append("    INSERT INTO dbo.GP_cfRateDetl (RateKey, GpProgramId, CoverageType, FinancingType, PriceFrom, PriceTo, Term, OEMs, ProvStates, Rate, Province, ContractType, ModDtTime, ModLoginId)")
    lines.append("    SELECT")
    lines.append("        @NewRateKey,")
    lines.append("        n.GpProgramId,")
    lines.append("        n.CoverageType,")
    lines.append("        n.FinancingType,")
    lines.append("        n.PriceFrom,")
    lines.append("        n.PriceTo,")
    lines.append("        n.Term,")
    lines.append("        NULL,  -- OEMs")
    lines.append("        n.Province,  -- ProvStates")
    lines.append("        n.Rate,")
    lines.append("        n.Province,")
    lines.append("        'AU',  -- ContractType")
    lines.append("        @Today,")
    lines.append("        @ModLoginId")
    lines.append("    FROM #NewRates n;")
    lines.append("")
    lines.append(f"    PRINT 'Inserted ' + CAST(@@ROWCOUNT AS VARCHAR) + ' rate detail rows.';")
    lines.append("END")
    lines.append("ELSE")
    lines.append("BEGIN")
    lines.append("    PRINT 'No changes detected. No action taken.';")
    lines.append("END")
    lines.append("")

    # Cleanup
    lines.append("DROP TABLE #NewRates;")
    lines.append("DROP TABLE #RateMatrix;")
    lines.append("")
    lines.append("COMMIT TRANSACTION;")
    lines.append("PRINT 'Transaction committed successfully.';")
    lines.append("")
    lines.append("END TRY")
    lines.append("BEGIN CATCH")
    lines.append("    IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION;")
    lines.append("    THROW;")
    lines.append("END CATCH;")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate SQL script for GP Rate Upload from Excel rate matrix."
    )
    parser.add_argument("excel_file", help="Path to the Excel file containing rate data")
    parser.add_argument("--sheet-name", default="Dlr cost", help="Worksheet name (default: 'Dlr cost')")
    parser.add_argument("--program-id", default="GP007", help="GP Program ID (default: GP007)")
    parser.add_argument("--coverage-type", default="CO", help="Coverage Type (default: CO)")
    parser.add_argument("--province", nargs='+', default=["MB", "SK"], help="Province code(s) (default: MB SK)")
    parser.add_argument("--prod-effect-date", default="2026-07-01", help="Effect date for PROD (default: 2026-07-01)")
    parser.add_argument("--output", "-o", help="Output SQL file path (default: output_<timestamp>.sql)")

    args = parser.parse_args()

    excel_path = Path(args.excel_file)
    if not excel_path.exists():
        print(f"ERROR: File not found: {excel_path}")
        sys.exit(1)

    print(f"Loading workbook: {excel_path.name}")
    wb = load_workbook(str(excel_path), read_only=True, data_only=True)

    if args.sheet_name not in wb.sheetnames:
        print(f"ERROR: Worksheet '{args.sheet_name}' not found.")
        print(f"Available sheets: {wb.sheetnames}")
        sys.exit(1)

    ws = wb[args.sheet_name]
    print(f"Reading worksheet: '{args.sheet_name}'")

    # Find and parse Purchase Rates section
    print("\nLooking for Purchase Rate section...")
    purchase_start = find_section_start(ws, "Purchase")
    if purchase_start:
        print(f"  Found at row {purchase_start}")
        purchase_rates = parse_rate_section(ws, purchase_start, 'P')
    else:
        # If no explicit "Purchase" label, try parsing from the top
        print("  No explicit 'Purchase' label found, trying from row 1...")
        purchase_rates = parse_rate_section(ws, 1, 'P')

    # Find and parse Lease Rates section
    print("\nLooking for Lease Rate section...")
    lease_start_search = purchase_start + len(purchase_rates) // 13 + 5 if purchase_start else 30
    lease_start = find_section_start(ws, "Lease", start_row=lease_start_search)
    if lease_start:
        print(f"  Found at row {lease_start}")
        lease_rates = parse_rate_section(ws, lease_start, 'L')
    else:
        print("  WARNING: Lease Rate section not found")
        lease_rates = []

    wb.close()

    all_rates = purchase_rates + lease_rates
    if not all_rates:
        print("\nERROR: No rates parsed from the worksheet.")
        sys.exit(1)

    print(f"\nTotal rates parsed: {len(all_rates)} "
          f"(Purchase: {len(purchase_rates)}, Lease: {len(lease_rates)})")
    print(f"Provinces: {args.province}")
    print(f"PROD Effect Date: {args.prod_effect_date}")
    total_rows = len(all_rates) * len(args.province)
    print(f"Total rows after province expansion: {total_rows}")

    # Generate SQL
    sql = generate_sql(all_rates, args.program_id, args.coverage_type, args.province, args.prod_effect_date)

    # Write output
    if args.output:
        output_path = Path(args.output)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = excel_path.parent / f"output_{timestamp}.sql"

    output_path.write_text(sql, encoding='utf-8')
    print(f"\nSQL script written to: {output_path}")
    print(f"Total INSERT rows in temp table: {total_rows}")


if __name__ == "__main__":
    main()
