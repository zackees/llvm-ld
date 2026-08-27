$ErrorActionPreference = 'Stop'
$direct = (Get-Item build/llvm-ld-direct.exe).FullName
$abi = (Get-Item build/llvm-ld-runner.exe).FullName
$common = @('winlink','lld-link','/entry:mainCRTStartup','/subsystem:console','/nodefaultlib','/brepro')
$samples = @()
foreach ($iteration in 1..20) {
  foreach ($kind in @('direct','c_abi')) {
    $program = if ($kind -eq 'direct') { $direct } else { $abi }
    $out = "bridge-$kind-$iteration.exe"
    $watch = [Diagnostics.Stopwatch]::StartNew()
    & $program @common "/out:$out" hello.obj
    $watch.Stop()
    if ($LASTEXITCODE) { throw "$kind bridge profile failed: $LASTEXITCODE" }
    $samples += [ordered]@{iteration=$iteration; kind=$kind; elapsed_ms=$watch.Elapsed.TotalMilliseconds}
  }
}
& $direct @common '/out:time-trace.exe' '--time-trace=lld-time-trace.json' hello.obj
if ($LASTEXITCODE -or !(Test-Path lld-time-trace.json)) { throw 'non-timed LLD time trace failed' }
[ordered]@{purpose='non-gating direct lld::lldMain vs C ABI subprocess overhead'; timed_profilers='off'; samples=$samples} |
  ConvertTo-Json -Depth 4 | Set-Content bridge-profile.json
