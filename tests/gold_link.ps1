param(
  [string]$BuildDir = 'build',
  [switch]$SelfTest
)
$ErrorActionPreference = 'Stop'

# Tier 1 self-host gold test: extract the real object/library graph CMake/Ninja links into
# llvm-ld-direct.exe by statically parsing build.ninja (read-only - no relink, no forced rebuild),
# replay THAT graph (with our own controlled, deterministic flag set - not a verbatim replay of
# link.exe's full flag set, which lld-link does not fully accept) through both llvm-ld-direct.exe
# (stock lld::lldMain) and llvm-ld-runner.exe (the C ABI wrapper), and byte-compare every output.
# This is the strongest correctness signal in the repo because the inputs are a real self-hosted
# object/lib graph instead of a synthetic hello.c.

function Assert-Same([string]$left, [string]$right, [string]$label) {
  if ((Get-FileHash $left).Hash -ne (Get-FileHash $right).Hash) { throw "$label differs: $left vs $right" }
}

function Resolve-BuildBinary([string]$buildDir, [string]$name) {
  $item = Get-Item (Join-Path $buildDir "Release/$name") -ErrorAction SilentlyContinue
  if (-not $item) { $item = Get-Item (Join-Path $buildDir $name) -ErrorAction SilentlyContinue }
  if (-not $item) { throw "cannot locate $name under $buildDir (checked Release/ and top level)" }
  return $item.FullName
}

# Tokenizes a whitespace-separated ninja value (build-edge input list, or a KEY = VALUE variable
# body), resolving ninja's '$'-escapes: '$$' -> literal '$', '$ ' -> literal space (kept inside a
# token, not a separator), '$:' -> literal ':'. Any other '$x' sequence is an escape this script
# does not understand, so it fails loudly instead of silently mis-tokenizing.
#
# CMake also emits double-quoted spans in LINK_LIBRARIES for paths containing spaces (on a VS
# Enterprise runner the DIA SDK's diaguids.lib is one), so a quoted span is a single token and the
# quotes themselves are stripped: lld-link receives each token as a plain argv entry, and retaining
# the quotes would make it look for a file whose name literally starts with '"'. Splitting is
# single-pass and stateful rather than split-then-unescape, because whitespace is only a separator
# outside a quoted span.
function ConvertTo-NinjaTokens([string]$text) {
  $tokens = @()
  $sb = New-Object System.Text.StringBuilder
  $inQuote = $false
  $i = 0
  while ($i -lt $text.Length) {
    $c = $text[$i]
    if ($c -eq '$') {
      if ($i + 1 -ge $text.Length) { throw "unterminated '`$' escape at end of ninja text: $text" }
      $next = $text[$i + 1]
      if ($next -eq '$') { [void]$sb.Append('$'); $i += 2 }
      elseif ($next -eq ' ') { [void]$sb.Append(' '); $i += 2 }
      elseif ($next -eq ':') { [void]$sb.Append(':'); $i += 2 }
      else { throw "unhandled ninja '`$' escape sequence '`$$next' - extend ConvertTo-NinjaTokens in tests/gold_link.ps1 before proceeding. Context: $text" }
    } elseif ($c -eq '"') {
      $inQuote = -not $inQuote; $i += 1
    } elseif (-not $inQuote -and [char]::IsWhiteSpace($c)) {
      if ($sb.Length -gt 0) { $tokens += $sb.ToString(); [void]$sb.Clear() }
      $i += 1
    } else {
      [void]$sb.Append($c); $i += 1
    }
  }
  if ($inQuote) { throw "unterminated double quote in ninja text: $text" }
  if ($sb.Length -gt 0) { $tokens += $sb.ToString() }
  return $tokens
}

# Runnable with: ./tests/gold_link.ps1 -SelfTest (no build tree required).
function Invoke-TokenizerSelfTest {
  $dia = '"C:\Program Files\Microsoft Visual Studio\18\Enterprise\DIA SDK\lib\amd64\diaguids.lib"'
  $diaExpected = 'C:\Program Files\Microsoft Visual Studio\18\Enterprise\DIA SDK\lib\amd64\diaguids.lib'
  $cases = @(
    @{ Name = 'DIA SDK path alone'; Text = $dia; Expect = @($diaExpected) }
    @{ Name = 'DIA SDK path between unquoted tokens'; Text = "lib\LLVMSupport.lib $dia kernel32.lib"; Expect = @('lib\LLVMSupport.lib', $diaExpected, 'kernel32.lib') }
    @{ Name = 'two quoted tokens'; Text = '"a b.lib" "c d.lib"'; Expect = @('a b.lib', 'c d.lib') }
    @{ Name = 'quoted token containing a ninja $ escape'; Text = '"a$ b\c d.lib"'; Expect = @('a b\c d.lib') }
    @{ Name = 'quoted span adjacent to unquoted text'; Text = 'pre"a b"post'; Expect = @('prea bpost') }
    @{ Name = 'unquoted $$ and $: escapes'; Text = 'C$:\x$$y.lib z.lib'; Expect = @('C:\x$y.lib', 'z.lib') }
    @{ Name = 'unquoted $ escape (legacy behaviour)'; Text = 'a$ b.lib c.lib'; Expect = @('a b.lib', 'c.lib') }
  )
  foreach ($case in $cases) {
    $got = @(ConvertTo-NinjaTokens $case.Text)
    $expect = @($case.Expect)
    if ($got.Count -ne $expect.Count) { throw "$($case.Name): expected $($expect.Count) token(s), got $($got.Count): $($got -join ' | ')" }
    for ($k = 0; $k -lt $expect.Count; $k++) {
      if ($got[$k] -cne $expect[$k]) { throw "$($case.Name): token $k expected '$($expect[$k])', got '$($got[$k])'" }
    }
    Write-Host "ok  $($case.Name) -> [$($got -join '] [')]"
  }
  foreach ($bad in @('"C:\a b.lib', 'a.lib "b c')) {
    $threw = $false
    try { ConvertTo-NinjaTokens $bad } catch { $threw = $true }
    if (-not $threw) { throw "unterminated quote did not throw: $bad" }
    Write-Host "ok  unterminated quote throws -> $bad"
  }
  Write-Host 'gold_link.ps1: ConvertTo-NinjaTokens self-test passed'
}

if ($SelfTest) { Invoke-TokenizerSelfTest; exit 0 }

# Reads the static 'build <target>.exe: <rule> <inputs...> [| implicit] [|| order-only]' edge and
# its following indented KEY = VALUE variable block directly out of build.ninja. This avoids both
# problems seen in earlier revisions: the linker response file is ephemeral (ninja deletes it
# after linking, and forcing it to persist requires an expensive relink), and the wrapped
# 'cmake -E vs_link_exe ... -- link.exe ...' command line duplicates information already present,
# statically, in build.ninja. This function performs no relink and touches nothing in the build
# tree - it only reads build.ninja.
function Get-NinjaExeEdge([string]$buildDir, [string]$target) {
  $ninjaPath = Join-Path $buildDir 'build.ninja'
  if (-not (Test-Path $ninjaPath)) { throw "build.ninja not found at $ninjaPath - run cmake -S . -B $buildDir -G Ninja first" }
  $raw = Get-Content -Raw -Path $ninjaPath
  # Ninja line continuation: a line ending in an unescaped '$' continues on the next line.
  $joined = $raw -replace '\$\r?\n[ \t]*', ' '
  $lines = $joined -split "`r?`n"

  $editIndex = -1
  $objectsText = $null
  $pattern = "^build\s+" + [regex]::Escape("$target.exe") + ":\s+(\S+)\s*(.*)$"
  for ($i = 0; $i -lt $lines.Count; $i++) {
    $m = [regex]::Match($lines[$i], $pattern)
    if ($m.Success) {
      $editIndex = $i
      $restText = $m.Groups[2].Value
      $cutoff = $restText.Length
      foreach ($sep in @(' | ', ' || ')) {
        $idx = $restText.IndexOf($sep)
        if ($idx -ge 0 -and $idx -lt $cutoff) { $cutoff = $idx }
      }
      $objectsText = $restText.Substring(0, $cutoff)
      break
    }
  }
  if ($editIndex -lt 0) {
    throw "build.ninja: no 'build $target.exe: <rule> ...' edge found in $ninjaPath. " +
          "The CMake/Ninja generator output format may have changed - update tests/gold_link.ps1's Get-NinjaExeEdge."
  }

  $vars = @{}
  for ($j = $editIndex + 1; $j -lt $lines.Count; $j++) {
    $line = $lines[$j]
    if ($line -notmatch '^[ \t]') { break }
    $vm = [regex]::Match($line, '^\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
    if ($vm.Success) { $vars[$vm.Groups[1].Value] = $vm.Groups[2].Value }
  }

  return [pscustomobject]@{
    Objects       = ConvertTo-NinjaTokens $objectsText
    LinkLibraries = if ($vars.ContainsKey('LINK_LIBRARIES')) { ConvertTo-NinjaTokens $vars['LINK_LIBRARIES'] } else { @() }
    LinkFlags     = if ($vars.ContainsKey('LINK_FLAGS')) { ConvertTo-NinjaTokens $vars['LINK_FLAGS'] } else { @() }
    NinjaPath     = $ninjaPath
  }
}

$stockLld = Resolve-BuildBinary $BuildDir 'llvm-ld-direct.exe'
$runner = Resolve-BuildBinary $BuildDir 'llvm-ld-runner.exe'

$edge = Get-NinjaExeEdge $BuildDir 'llvm-ld-direct'
if (-not $edge.Objects -or $edge.Objects.Count -eq 0) { throw "build.ninja: 'llvm-ld-direct.exe' edge had zero object inputs (parsed from $($edge.NinjaPath))" }

# Flags we own and force ourselves (determinism + our own PDB handling). Any flag-shaped token
# from LINK_LIBRARIES that collides with this set is a real conflict worth stopping for -
# /debug is the one legitimate exception (handled below): we detect it to decide whether to run
# the PDB rows, then drop it, because we always supply our own controlled /debug/pdb set.
$collisionRegex = '^(?i)[-/](out|pdb|pdbaltpath|pdbsourcepath|incremental|manifest|brepro|threads|ilk)(:.*)?$'
$debugRegex = '^(?i)[-/]debug(:.*)?$'

# PDB byte-identity is a first-class part of the gold contract, so we ask for /debug ourselves
# rather than only when the captured edge happens to carry it. Release builds link llvm-ld-direct
# without /debug, so keying off the edge alone would leave every PDB row below permanently
# dormant. The objects need not carry full debug info for this to be meaningful: lld still emits
# a PDB, and the assertion is byte identity between the two linkers, not PDB richness.
$hasDebug = $true
foreach ($tok in (@($edge.LinkLibraries) + @($edge.LinkFlags))) {
  if ($tok -match $debugRegex) { $hasDebug = $true }
}

# Partition LINK_LIBRARIES into (a) path/lib tokens and non-colliding flags, both carried through
# verbatim - e.g. '-delayload:shell32.dll' is real link semantics, not something we can drop - and
# (b) /debug, silently dropped (we own PDB handling ourselves).
$libraryArgs = @()
foreach ($tok in $edge.LinkLibraries) {
  if ($tok -match $debugRegex) { continue }
  if ($tok -match $collisionRegex) {
    throw "build.ninja: LINK_LIBRARIES for the 'llvm-ld-direct.exe' edge contains '$tok', which collides " +
          "with a flag this script forces itself. Refusing to silently override real link semantics - " +
          "inspect the edge in $($edge.NinjaPath) and update tests/gold_link.ps1 if this is expected."
  }
  $libraryArgs += $tok
}

# From LINK_FLAGS we only carry through /machine: and /subsystem: (real target properties);
# everything else (/INCREMENTAL:NO, etc.) is deliberately dropped because we force our own
# determinism/PDB flag set instead.
$machineFlag = $null
$subsystemFlag = $null
foreach ($tok in $edge.LinkFlags) {
  if ($tok -match '^(?i)/machine:') { $machineFlag = $tok }
  elseif ($tok -match '^(?i)/subsystem:') { $subsystemFlag = $tok }
}
if (-not $subsystemFlag) { $subsystemFlag = '/subsystem:console' }

$baseArgs = @($edge.Objects) + @($libraryArgs)

# Determinism flag set (verified against pinned lld source; see task background). We do not
# force /entry: - direct_runner.cpp has a normal main() linked against the static CRT, so
# lld-link infers mainCRTStartup itself under /subsystem:console.
Remove-Item Env:\SOURCE_DATE_EPOCH -ErrorAction SilentlyContinue
Remove-Item Env:\LLD_REPRODUCE -ErrorAction SilentlyContinue
$forcedFlags = @('/brepro', '/manifest:no', '/lldignoreenv', $subsystemFlag)
if ($machineFlag) { $forcedFlags += $machineFlag }

# Dedicated scratch output directory inside the build tree, cleaned up unconditionally at the end
# of the script (success or failure) so no gold-test artifacts linger in the build tree. This
# script is otherwise read-only with respect to the build tree - it never touches build.ninja's
# own tracked outputs. Finished gold-*.exe/.pdb outputs are copied out to the repo root, matching
# windows_correctness.ps1's flat naming convention (and what the CI failure-artifact upload step
# expects).
# Absolute: every link runs with cwd pushed to $BuildDir, so a relative path here would resolve
# one level too deep.
$goldOutDir = (New-Item -ItemType Directory -Path (Join-Path $BuildDir 'gold-link-tmp') -Force).FullName
$goldSrcPath = Join-Path $goldOutDir 'gold-src'

# Every link writes to the SAME constant intermediate name and is renamed to its per-row stem
# afterwards. This is load-bearing under /debug, not cosmetic: measured with the pinned runner,
# two otherwise-identical /debug /brepro /pdbaltpath:%_PDB% links that differ only in their
# /out: and /pdb: names produce a DIFFERENT exe (the CodeView debug directory embeds the PDB
# basename that %_PDB% expands to) and a DIFFERENT pdb (the '* Linker *' module's S_ENVBLOCK
# records the /out: path). With per-row names, every comparison below - including the
# same-thread-count a-vs-b determinism rows - would fail spuriously the moment anyone configures
# a build whose LINK_FLAGS carry /debug. With a constant intermediate name the same pair is
# byte-identical. Non-/debug output does not embed either name, which is why the Release path
# passes either way.
$goldLinkStem = 'gold'

function Invoke-GoldLink([string]$exePath, [string[]]$extraArgs, [string]$outStem) {
  $outAbs = Join-Path $goldOutDir "$goldLinkStem.exe"
  $pdbAbs = Join-Path $goldOutDir "$goldLinkStem.pdb"
  Remove-Item $outAbs, $pdbAbs -ErrorAction SilentlyContinue
  $pdbArgs = @()
  if ($hasDebug) { $pdbArgs = @('/debug', "/pdb:$pdbAbs", '/pdbaltpath:%_PDB%', "/pdbsourcepath:$goldSrcPath") }
  Push-Location $BuildDir
  try {
    & $exePath winlink lld-link @baseArgs @forcedFlags @extraArgs @pdbArgs "/out:$outAbs"
    if ($LASTEXITCODE -ne 0) { throw "gold link failed ($LASTEXITCODE): $exePath $outStem" }
  } finally { Pop-Location }
  Move-Item $outAbs "$outStem.exe" -Force
  if ($hasDebug) { Move-Item $pdbAbs "$outStem.pdb" -Force }
}

try {
  foreach ($side in @('stock', 'library')) {
    $exe = if ($side -eq 'stock') { $stockLld } else { $runner }
    foreach ($iteration in 1, 2) {
      Invoke-GoldLink $exe @() "gold-$side-$iteration"
    }
    Assert-Same "gold-$side-1.exe" "gold-$side-2.exe" "gold $side EXE self-determinism"
    if ($hasDebug) { Assert-Same "gold-$side-1.pdb" "gold-$side-2.pdb" "gold $side PDB self-determinism" }
  }
  Assert-Same 'gold-stock-1.exe' 'gold-library-1.exe' 'gold EXE stock/library'
  if ($hasDebug) { Assert-Same 'gold-stock-1.pdb' 'gold-library-1.pdb' 'gold PDB stock/library' }

  # Thread count is part of the comparison contract, NOT an invariant to assert across.
  #
  # Measured on the self-host link of llvm-ld-direct: /threads:1 output differs from the
  # multi-threaded output (same length, different bytes), while every fixed-thread-count
  # comparison is byte-identical. Byte identity therefore holds only when both sides use the
  # same thread count, so the gold comparisons above must never straddle thread counts, and any
  # future cross-run comparison (against a reference binary, or across CI runs) must pin
  # /threads: identically on both sides.
  #
  # These rows assert the invariant that actually holds - determinism at a pinned thread count -
  # at both ends of the range, so a regression that makes a single thread count nondeterministic
  # is caught regardless of what the runner's default parallelism happens to be.
  foreach ($threadCount in 1, 8) {
    Invoke-GoldLink $runner @("/threads:$threadCount") "gold-threads-$threadCount-a"
    Invoke-GoldLink $runner @("/threads:$threadCount") "gold-threads-$threadCount-b"
    Assert-Same "gold-threads-$threadCount-a.exe" "gold-threads-$threadCount-b.exe" "gold /threads:$threadCount determinism"
    if ($hasDebug) { Assert-Same "gold-threads-$threadCount-a.pdb" "gold-threads-$threadCount-b.pdb" "gold /threads:$threadCount PDB determinism" }
  }

  Write-Host 'gold_link.ps1: all byte-identity comparisons passed'
} finally {
  Remove-Item $goldOutDir -Recurse -Force -ErrorAction SilentlyContinue
}
