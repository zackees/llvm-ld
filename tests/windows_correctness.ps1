$ErrorActionPreference = 'Stop'
$stockItem = Get-Item 'build/Release/llvm-ld-direct.exe' -ErrorAction SilentlyContinue
if (-not $stockItem) { $stockItem = Get-Item 'build/llvm-ld-direct.exe' }
$stockLld = $stockItem.FullName
$runnerItem = Get-Item 'build/Release/llvm-ld-runner.exe' -ErrorAction SilentlyContinue
if (-not $runnerItem) { $runnerItem = Get-Item 'build/llvm-ld-runner.exe' }
$runner = $runnerItem.FullName

function Assert-Same([string]$left, [string]$right, [string]$label) {
  if ((Get-FileHash $left).Hash -ne (Get-FileHash $right).Hash) { throw "$label differs: $left vs $right" }
}
function Invoke-Checked([scriptblock]$command) {
  & $command
  if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $command" }
}
function Test-WinLink([string]$label, [string]$object, [bool]$debug) {
  $common = @('/entry:mainCRTStartup','/subsystem:console','/nodefaultlib','/brepro')
  if ($debug) { $common += @('/debug:full','/pdb:result.pdb','/pdbaltpath:%_PDB%') }
  foreach ($side in @('stock','library')) {
    foreach ($iteration in 1,2) {
      Remove-Item result.exe,result.pdb -ErrorAction SilentlyContinue
      if ($side -eq 'stock') { & $stockLld winlink lld-link @common /out:result.exe $object }
      else { & $runner winlink lld-link @common /out:result.exe $object }
      if ($LASTEXITCODE -ne 0) { throw "$label $side link failed: $LASTEXITCODE" }
      Copy-Item result.exe "$label-$side-$iteration.exe"
      if ($debug) { Copy-Item result.pdb "$label-$side-$iteration.pdb" }
    }
    Assert-Same "$label-$side-1.exe" "$label-$side-2.exe" "$label $side EXE self-determinism"
    if ($debug) { Assert-Same "$label-$side-1.pdb" "$label-$side-2.pdb" "$label $side PDB self-determinism" }
  }
  Assert-Same "$label-stock-1.exe" "$label-library-1.exe" "$label EXE stock/library"
  if ($debug) { Assert-Same "$label-stock-1.pdb" "$label-library-1.pdb" "$label PDB stock/library" }
  & ".\$label-library-1.exe"
  if ($LASTEXITCODE -ne 0) { throw "$label native execution failed: $LASTEXITCODE" }
}

'int mainCRTStartup(void) { return 0; }' | Set-Content hello.c
cl /nologo /c /Brepro hello.c /Fo:hello.obj
if ($LASTEXITCODE -ne 0) { throw 'MSVC compile failed' }
Test-WinLink 'msvc-nodebug' 'hello.obj' $false
Test-WinLink 'msvc-debug' 'hello.obj' $true

clang-cl /nologo /c /Brepro /clang:-flto=thin hello.c /Fo:hello-thin.obj
if ($LASTEXITCODE -ne 0) { throw 'ThinLTO compile failed' }
Test-WinLink 'msvc-thinlto' 'hello-thin.obj' $false
clang-cl /nologo /c /Brepro /clang:-flto hello.c /Fo:hello-full.obj
if ($LASTEXITCODE -ne 0) { throw 'full LTO compile failed' }
Test-WinLink 'msvc-fulllto' 'hello-full.obj' $false

# MinGW persona: use a freestanding COFF object so the runner and pinned stock LLD receive exactly
# the same GNU-dialect inputs without depending on a floating MSYS2 runtime installation.
clang --target=x86_64-w64-windows-gnu -c hello.c -o hello-mingw.obj
if ($LASTEXITCODE -ne 0) { throw 'MinGW COFF compile failed' }
$gnuArgs = @('-m','i386pep','-e','mainCRTStartup','--no-insert-timestamp','-o')
foreach ($side in @('stock','library')) {
  foreach ($iteration in 1,2) {
    $out="mingw-$side-$iteration.exe"
    if ($side -eq 'stock') { & $stockLld mingw ld @gnuArgs $out hello-mingw.obj }
    else { & $runner mingw ld @gnuArgs $out hello-mingw.obj }
    if ($LASTEXITCODE -ne 0) { throw "MinGW $side link failed: $LASTEXITCODE" }
  }
  Assert-Same "mingw-$side-1.exe" "mingw-$side-2.exe" "MinGW $side self-determinism"
}
Assert-Same mingw-stock-1.exe mingw-library-1.exe 'MinGW stock/library'
& ./mingw-library-1.exe
if ($LASTEXITCODE -ne 0) { throw "MinGW native execution failed: $LASTEXITCODE" }
