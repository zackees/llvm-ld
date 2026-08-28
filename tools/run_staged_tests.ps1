<#
Runs the cross-compiled (Linux -> x86_64-pc-windows-msvc) llvm_ld test binaries on a Windows
runner from a staged artifact directory produced by the build-linux-cross CI job. Windows has no
rpath equivalent for DLL lookup, so llvm_ld.dll must sit next to the test executables in -Stage;
this script fails loudly if it does not.
#>
param(
  [string]$Stage = "./stage"
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$stageItem = Get-Item $Stage -ErrorAction SilentlyContinue
if (-not $stageItem) { throw "Stage directory not found: $Stage" }
$stageDir = $stageItem.FullName

function Get-StagedExe([string]$name) {
  $item = Get-Item (Join-Path $stageDir $name) -ErrorAction SilentlyContinue
  if (-not $item) { throw "Missing staged binary: $name (expected in $stageDir)" }
  return $item.FullName
}

function Invoke-Checked([string]$exe, [string[]]$exeArgs, [string]$label) {
  & $exe @exeArgs
  if ($LASTEXITCODE -ne 0) { throw "$label failed with exit code $LASTEXITCODE" }
  Write-Host "PASS: $label"
}

# Windows has no rpath: the loader only looks in the app directory (plus system paths), so
# llvm_ld.dll must be staged alongside every exe that links it.
$dllItem = Get-Item (Join-Path $stageDir 'llvm_ld.dll') -ErrorAction SilentlyContinue
if (-not $dllItem) {
  throw "llvm_ld.dll not found next to the staged test executables in $stageDir. Windows has no " +
    "rpath; the DLL must be copied into the same directory as the exes in the uploaded artifact."
}

$results = [System.Collections.Generic.List[string]]::new()
$failed = $false

$checks = @(
  @{ Name = 'abi_smoke'; Exe = 'abi_smoke.exe'; Args = @() },
  @{ Name = 'abi_contract'; Exe = 'abi_contract.exe'; Args = @() },
  @{ Name = 'abi_state_test'; Exe = 'abi_state_test.exe'; Args = @() }
)

foreach ($check in $checks) {
  try {
    $exe = Get-StagedExe $check.Exe
    Invoke-Checked $exe $check.Args $check.Name
    $results.Add("PASS  $($check.Name)")
  } catch {
    $failed = $true
    $results.Add("FAIL  $($check.Name): $($_.Exception.Message)")
  }
}

# allocator-probe mimalloc is a hard failure, not a soft one: it is the only proof that the
# mimalloc static-CRT malloc override (MI_MALLOC_OVERRIDE, requires /MT since mimalloc's override
# compiles out when _DLL/dynamic CRT is defined) actually survived cross-compilation with
# clang-cl + lld-link + xwin instead of a native cl/link toolchain.
try {
  $probe = Get-StagedExe 'allocator-probe.exe'
  Invoke-Checked $probe @('mimalloc') 'allocator-probe mimalloc (static-CRT override survival)'
  $results.Add('PASS  allocator-probe mimalloc')
} catch {
  $failed = $true
  $results.Add("FAIL  allocator-probe mimalloc: $($_.Exception.Message)")
  $results.Add('      This proves the mimalloc static-CRT malloc override did NOT survive cross-compilation.')
}

Write-Host ''
Write-Host '=== Staged cross-compiled test summary ==='
foreach ($line in $results) { Write-Host $line }
Write-Host '==========================================='

if ($failed) {
  throw 'One or more staged cross-compiled tests failed; see summary above.'
}

Write-Host 'All staged cross-compiled tests passed.'
