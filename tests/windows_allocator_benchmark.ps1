$ErrorActionPreference='Stop'
Remove-Item Env:MIMALLOC_DHAT,Env:MIMALLOC_DHAT_DUMP_AT_EXIT,Env:MIMALLOC_DHAT_MAX_BYTES,Env:MIMALLOC_PROF,Env:MIMALLOC_PROF_ACTIVE,Env:MIMALLOC_PROF_DUMP_AT_EXIT,Env:MIMALLOC_PROF_SAMPLE_INTERVAL,Env:MIMALLOC_PROF_SAMPLE_RATE,Env:MI_PPROF -ErrorAction SilentlyContinue
$mi=(Resolve-Path build/llvm-ld-runner.exe).Path
$system=(Resolve-Path build/llvm-ld-runner-system.exe).Path
& build/allocator-probe.exe mimalloc; if($LASTEXITCODE){throw 'mimalloc timing configuration has active profiler or missing CRT redirect'}
& build/allocator-probe-system.exe system; if($LASTEXITCODE){throw 'system baseline allocator attribution failed'}
New-Item -ItemType Directory -Force bench-objects | Out-Null
$allObjects=@()
foreach($i in 0..2047){
  "int entry$i(void) { return $i; }" | Set-Content "bench-objects/$i.c"
  cl /nologo /c /O2 /Brepro "bench-objects/$i.c" "/Fo:bench-objects/$i.obj" | Out-Null
  if($LASTEXITCODE){throw "fixture compile $i failed"}; $allObjects += "bench-objects/$i.obj"
}
function Median([double[]]$values){
  $s=@($values|Sort-Object); $middle=[int]($s.Count/2)
  if($s.Count%2){return $s[$middle]}; return ($s[$middle-1]+$s[$middle])/2.0
}
function Invoke-Sample([string]$exe,[string[]]$arguments){
  $wall=0.0; $cpu=0.0; $rss=0.0
  foreach($repeat in 0..4){
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $process=Start-Process -FilePath $exe -ArgumentList $arguments -PassThru -NoNewWindow
    while(-not $process.HasExited){
      $process.Refresh()
      $rss=[Math]::Max($rss,[double]$process.WorkingSet64)
      Start-Sleep -Milliseconds 1
    }
    $process.WaitForExit(); $process.Refresh()
    $rss=[Math]::Max($rss,[double]$process.PeakWorkingSet64)
    $watch.Stop(); if($process.ExitCode){throw "benchmark link failed: $($process.ExitCode)"}
    $wall += $watch.Elapsed.TotalMilliseconds; $cpu += $process.TotalProcessorTime.TotalMilliseconds
  }
  return @{wall_ms=$wall/5.0;cpu_ms=$cpu/5.0;peak_rss_bytes=$rss}
}
function Bootstrap-MeanCI([double[]]$values,[int]$seed){
  $random=[Random]::new($seed); $means=[double[]]::new(10000)
  for($b=0;$b -lt $means.Count;$b++){
    $sum=0.0; for($i=0;$i -lt $values.Count;$i++){$sum += $values[$random.Next($values.Count)]}
    $means[$b]=$sum/$values.Count
  }
  [Array]::Sort($means); return @{lower=$means[249];upper=$means[9749]}
}
$randomOrder=[Random]::new(9501); $matrix=@(); $wallRatios=@(); $cpuRatios=@(); $rssRatios=@()
foreach($objectCount in 64,512,2048){
  $objects=@($allObjects[0..($objectCount-1)])
  (@('/entry:entry0','/subsystem:console','/nodefaultlib','/brepro')+$objects) | Set-Content "bench-$objectCount.rsp"
  $linkArgs=@('winlink','lld-link',"@bench-$objectCount.rsp")
  & $mi @linkArgs /out:mi-proof.exe; if($LASTEXITCODE){throw 'mimalloc proof link failed'}
  & $system @linkArgs /out:system-proof.exe; if($LASTEXITCODE){throw 'system proof link failed'}
  if((Get-FileHash mi-proof.exe).Hash -ne (Get-FileHash system-proof.exe).Hash){throw 'allocator-only variants changed artifact bytes'}
  $samples=@{mimalloc=@();system=@()}; $pairedWall=@(); $pairedCpu=@(); $pairedRss=@()
  foreach($iteration in 0..39){
    $order=if($randomOrder.Next(2)){@('system','mimalloc')}else{@('mimalloc','system')}
    $pair=@{}
    foreach($name in $order){
      $exe=if($name -eq 'mimalloc'){$mi}else{$system}
      $pair[$name]=Invoke-Sample $exe ($linkArgs+"/out:$name-$objectCount-$iteration.exe")
      $samples[$name]+=$pair[$name]
    }
    $pairedWall += $pair.mimalloc.wall_ms/$pair.system.wall_ms
    $pairedCpu += $pair.mimalloc.cpu_ms/$pair.system.cpu_ms
    $pairedRss += $pair.mimalloc.peak_rss_bytes/$pair.system.peak_rss_bytes
  }
  $miWall=Median @($samples.mimalloc|ForEach-Object{$_.wall_ms}); $sysWall=Median @($samples.system|ForEach-Object{$_.wall_ms})
  $miCpu=Median @($samples.mimalloc|ForEach-Object{$_.cpu_ms}); $sysCpu=Median @($samples.system|ForEach-Object{$_.cpu_ms})
  $miRss=Median @($samples.mimalloc|ForEach-Object{$_.peak_rss_bytes}); $sysRss=Median @($samples.system|ForEach-Object{$_.peak_rss_bytes})
  $wallCI=Bootstrap-MeanCI @($pairedWall|ForEach-Object{100.0*(1.0-$_)}) (9501+$objectCount)
  $cpuCI=Bootstrap-MeanCI $pairedCpu (19501+$objectCount); $rssCI=Bootstrap-MeanCI $pairedRss (29501+$objectCount)
  $matrix += @{objects=$objectCount;iterations=40;wall_ms=@{mimalloc=$miWall;system=$sysWall};cpu_ms=@{mimalloc=$miCpu;system=$sysCpu};peak_rss_bytes=@{mimalloc=$miRss;system=$sysRss};speedup_percent=100.0*($sysWall-$miWall)/$sysWall;wall_speedup_ci95=$wallCI;cpu_ratio_ci95=$cpuCI;rss_ratio_ci95=$rssCI;raw_pairs=@{wall_ratio=$pairedWall;cpu_ratio=$pairedCpu;rss_ratio=$pairedRss}}
  $wallRatios += ,$pairedWall; $cpuRatios += ,$pairedCpu; $rssRatios += ,$pairedRss
}
$aggregateWall=@(); $aggregateCpu=@(); $aggregateRss=@()
foreach($i in 0..39){
  $aggregateWall += [Math]::Exp((($wallRatios|ForEach-Object{[Math]::Log($_[$i])})|Measure-Object -Average).Average)
  $aggregateCpu += [Math]::Exp((($cpuRatios|ForEach-Object{[Math]::Log($_[$i])})|Measure-Object -Average).Average)
  $aggregateRss += [Math]::Exp((($rssRatios|ForEach-Object{[Math]::Log($_[$i])})|Measure-Object -Average).Average)
}
$aggregate=@{wall_speedup_ci95=Bootstrap-MeanCI @($aggregateWall|ForEach-Object{100.0*(1.0-$_)}) 39501;cpu_ratio_ci95=Bootstrap-MeanCI $aggregateCpu 49501;rss_ratio_ci95=Bootstrap-MeanCI $aggregateRss 59501}
$failed=@($matrix|Where-Object{$_.wall_speedup_ci95.lower -le -3.0 -or $_.cpu_ratio_ci95.upper -gt 1.03 -or $_.rss_ratio_ci95.upper -gt 1.03})
$accepted=($aggregate.wall_speedup_ci95.lower -gt 3.0 -and $aggregate.cpu_ratio_ci95.upper -le 1.03 -and $aggregate.rss_ratio_ci95.upper -le 1.03 -and !$failed)
$decision=if($accepted){'accepted'}else{'rejected'}
$result=@{schema=4;profilers=@{pprof=$false;dhat=$false};randomization_seed=9501;bootstrap_resamples=10000;decision=$decision;acceptance_gate=@{aggregate_wall_lower_ci95_min_percent=3.0;max_cpu_rss_ratio=1.03;per_workload_max_regression_percent=3.0};aggregate=$aggregate;matrix=$matrix}
$result|ConvertTo-Json -Depth 7|Set-Content allocator-benchmark.json
Get-Content allocator-benchmark.json
if(!$accepted){Write-Host 'Allocator hypothesis rejected by the predeclared gate; measurement completed successfully.'}
