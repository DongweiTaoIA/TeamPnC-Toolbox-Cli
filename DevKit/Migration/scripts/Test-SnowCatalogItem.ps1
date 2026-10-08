<#
.SYNOPSIS
    Probes a ServiceNow catalog item and optionally submits a single order.

.DESCRIPTION
    Default behaviour is read-only: it fetches the catalog item definition and prints
    every variable's backend name, type, mandatory flag, reference table and choices.
    Those names are what the order_now payload requires -- the form labels will not work.

    Passing -Order submits exactly one request. It prompts for confirmation first.

.EXAMPLE
    .\Test-SnowCatalogItem.ps1

    Lists the variables for the "Generic request to an IT team" item.

.EXAMPLE
    .\Test-SnowCatalogItem.ps1 -Order -Variables @{
        service_type     = 'information_request'
        description      = 'TEST via API - please ignore'
        assignment_group = '35427da71b2bc8508d340d43cd4bcbd2'
        required_date    = '2026-10-06'
    }

    Submits one request after confirmation.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [string] $BaseUrl = 'https://iaaesf.service-now.com',

    [string] $ItemSysId = '53f7a515db98a0543694dd0968961972',

    [Parameter(Mandatory)]
    [pscredential] $Credential,

    [switch] $Order,

    [hashtable] $Variables = @{}
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Snow {
    param(
        [Parameter(Mandatory)] [string] $Method,
        [Parameter(Mandatory)] [string] $Path,
        [string] $Body
    )

    $params = @{
        Method         = $Method
        Uri            = "$BaseUrl$Path"
        Authentication = 'Basic'
        Credential     = $Credential
        ContentType    = 'application/json'
        Headers        = @{ Accept = 'application/json' }
    }
    if ($Body) { $params.Body = $Body }

    try {
        Invoke-RestMethod @params
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        switch ($status) {
            401 { throw "401 Unauthorized. Basic auth is likely disabled on this instance; you need an OAuth client." }
            403 { throw "403 Forbidden. The account authenticated but lacks rights on this catalog item." }
            default { throw "ServiceNow returned $status. $($_.Exception.Message)" }
        }
    }
}

function Show-ItemVariables {
    $item = (Invoke-Snow -Method Get -Path "/api/sn_sc/servicecatalog/items/$ItemSysId").result

    Write-Host "`nCatalog item: $($item.name)" -ForegroundColor Cyan
    Write-Host "sys_id      : $($item.sys_id)`n"

    $item.variables |
        Where-Object { $_.name } |
        Sort-Object order |
        ForEach-Object {
            [pscustomobject]@{
                Name      = $_.name
                Label     = $_.label
                Type      = $_.type
                Mandatory = [bool]$_.mandatory
                Reference = $_.reference
                Choices   = if ($_.choices) { ($_.choices | ForEach-Object { "$($_.value)=$($_.label)" }) -join '; ' } else { '' }
            }
        } |
        Format-List
}

function Submit-Order {
    if ($Variables.Count -eq 0) {
        throw "-Order requires -Variables. Run without -Order first to discover the variable names."
    }

    $payload = @{ sysparm_quantity = '1'; variables = $Variables } | ConvertTo-Json -Depth 5

    Write-Host "`nPayload to be submitted:" -ForegroundColor Yellow
    Write-Host $payload

    $target = "$BaseUrl catalog item $ItemSysId"
    if (-not $PSCmdlet.ShouldProcess($target, 'Create ONE ServiceNow request')) {
        Write-Host 'Aborted; nothing was submitted.' -ForegroundColor Yellow
        return
    }

    $result = (Invoke-Snow -Method Post -Path "/api/sn_sc/servicecatalog/items/$ItemSysId/order_now" -Body $payload).result

    Write-Host "`nCreated $($result.request_number)" -ForegroundColor Green
    Write-Host "Verify the variables actually landed -- a 200 does not mean they were accepted:"
    Write-Host "$BaseUrl/sp?id=ticket&table=sc_request&sys_id=$($result.sys_id)"
}

if ($Order) { Submit-Order } else { Show-ItemVariables }
