# User-facing launcher for the Fusion daily-usage diagnostic.
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonScript = Join-Path $PSScriptRoot "probe_fusion_daily_usage.py"
$venvPath = Join-Path $repoRoot ".venv-fusion-probe"
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        & $pythonLauncher.Source -3 -m venv $venvPath
    }
    else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            Write-Error "Python 3 is required. Install Python from https://www.python.org/downloads/ and try again."
            exit 1
        }
        & $pythonCommand.Source -m venv $venvPath
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Could not create the probe's Python environment."
        exit $LASTEXITCODE
    }
}

& $venvPython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('bs4') and importlib.util.find_spec('requests') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Setting up the probe's dependencies (only needed the first time)..."
    & $venvPython -m pip install "beautifulsoup4>=4.12.3" requests
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Could not install probe dependencies. Check your internet connection and try again."
        exit $LASTEXITCODE
    }
}

$username = Read-Host "Dominion username"
$securePassword = Read-Host "Dominion password" -AsSecureString
$passwordPointer = [IntPtr]::Zero
$plainPassword = $null

$oldUsername = [Environment]::GetEnvironmentVariable("DOMINIONSC_TEST_USERNAME", "Process")
$oldPassword = [Environment]::GetEnvironmentVariable("DOMINIONSC_TEST_PASSWORD", "Process")
$exitCode = 1

try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)

    $env:DOMINIONSC_TEST_USERNAME = $username
    $env:DOMINIONSC_TEST_PASSWORD = $plainPassword

    Push-Location $repoRoot
    try {
        & $venvPython $pythonScript @args
        $exitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $oldUsername) {
        Remove-Item Env:\DOMINIONSC_TEST_USERNAME -ErrorAction SilentlyContinue
    }
    else {
        $env:DOMINIONSC_TEST_USERNAME = $oldUsername
    }

    if ($null -eq $oldPassword) {
        Remove-Item Env:\DOMINIONSC_TEST_PASSWORD -ErrorAction SilentlyContinue
    }
    else {
        $env:DOMINIONSC_TEST_PASSWORD = $oldPassword
    }

    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    $plainPassword = $null
    $securePassword.Dispose()
}

exit $exitCode
