param(
  [string]$BuildDir = 'build'
)
$ErrorActionPreference = 'Stop'

# In-process Windows manifest merge test.
#
# Background (see PROVENANCE.md / vendored libxml2 section): lld/COFF/DriverUtils.cpp's
# createManifestXml() only reaches the merger at all when /manifestinput: is passed - a link that
# never merges (e.g. the gold test, which forces /manifest:no) is completely unaffected by any of
# this and cannot regress here. When /manifestinput: IS passed, createManifestXml() picks between
# createManifestXmlWithInternalMt() (libxml2, in-process) and createManifestXmlWithExternalMt()
# (shells out to `mt.exe`) based on whether LLVM was configured with LLVM_ENABLE_LIBXML2. Before
# vendoring libxml2 this repository built with LLVM_ENABLE_LIBXML2=OFF, so any /manifestinput: link
# fell into the external-mt path and died at DriverUtils.cpp:62 with
# "unable to find mt.exe in PATH" whenever the Windows SDK's mt.exe was not already on PATH.
#
# This test is RED->GREEN by construction, not by assertion: step 2 below scrubs $env:PATH to
# empty for the link invocation and calls the runner by absolute path, so mt.exe cannot possibly
# be found on PATH no matter what is installed on the machine running this script. Under the old
# LLVM_ENABLE_LIBXML2=OFF build this step fails with exit code != 0 and the "unable to find mt.exe
# in PATH" fatal diagnostic (RED). After vendoring libxml2 statically and flipping
# LLVM_ENABLE_LIBXML2=ON, the merge happens entirely in-process via WindowsManifestMerger and the
# link succeeds with an empty PATH (GREEN). No other tool on PATH (cl, clang-cl) is needed for this
# specific assertion since object compilation happens once, up front, before PATH is scrubbed.

function Assert-Same([string]$left, [string]$right, [string]$label) {
  if ((Get-FileHash $left).Hash -ne (Get-FileHash $right).Hash) { throw "$label differs: $left vs $right" }
}

function Resolve-BuildBinary([string]$buildDir, [string]$name) {
  $item = Get-Item (Join-Path $buildDir "Release/$name") -ErrorAction SilentlyContinue
  if (-not $item) { $item = Get-Item (Join-Path $buildDir $name) -ErrorAction SilentlyContinue }
  if (-not $item) { throw "cannot locate $name under $buildDir (checked Release/ and top level)" }
  return $item.FullName
}

$runner = Resolve-BuildBinary $BuildDir 'llvm-ld-runner.exe'
$repoRoot = Split-Path -Parent $PSScriptRoot
$manifestInput = Join-Path $repoRoot 'tests/manifest_input.manifest'
if (-not (Test-Path $manifestInput)) { throw "manifest fixture not found: $manifestInput" }
$peTriage = Join-Path $repoRoot 'tools/pe_triage.py'

# 1. Compile a trivial main() object with cl, same style as windows_correctness.ps1's hello.c.
'int mainCRTStartup(void) { return 0; }' | Set-Content hello.c
cl /nologo /c /Brepro hello.c /Fo:hello.obj
if ($LASTEXITCODE -ne 0) { throw 'MSVC compile failed' }

# 2. Link TWICE through llvm-ld-runner.exe with /manifest:embed /manifestinput:<fixture>, with
# $env:PATH scrubbed to empty and the runner invoked by absolute path. See the RED->GREEN
# discussion above: this is the load-bearing assertion of this test.
$savedPath = $env:PATH
try {
  $env:PATH = ''
  for ($iteration = 1; $iteration -le 2; $iteration++) {
    Remove-Item "merge-$iteration.exe" -ErrorAction SilentlyContinue
    & $runner winlink lld-link /entry:mainCRTStartup /subsystem:console /nodefaultlib /brepro `
      "/manifest:embed" "/manifestinput:$manifestInput" "/out:merge-$iteration.exe" hello.obj
    if ($LASTEXITCODE -ne 0) {
      throw "manifest-merge link failed with `$env:PATH scrubbed to empty (exit $LASTEXITCODE). " +
            "If this failure mentions 'unable to find mt.exe in PATH', libxml2 is not wired in as " +
            "the in-process manifest merger (LLVM_ENABLE_LIBXML2 is OFF, or the vendored library " +
            "did not link) - that is exactly the RED state this test exists to catch."
    }
  }
} finally {
  $env:PATH = $savedPath
}

# 3. Extract the embedded RT_MANIFEST resource and assert it contains distinctive strings from the
# input manifest fixture, proving a real merge happened rather than the default manifest being
# emitted unchanged. See tests/manifest_input.manifest for why these two strings are distinctive.
$extracted = Join-Path (Get-Location) 'merge-1.manifest.xml'
python $peTriage --extract-manifest merge-1.exe --out $extracted
if ($LASTEXITCODE -ne 0) { throw "pe_triage.py --extract-manifest failed: $LASTEXITCODE" }
$manifestText = Get-Content -Raw $extracted
foreach ($needle in @('llvm-ld manifest merge fixture', '1f676c76-80e1-4239-95bb-83d0f6d0da78')) {
  if ($manifestText -notlike "*$needle*") {
    throw "merged manifest is missing distinctive input string '$needle' - merge did not happen " +
          "(default manifest was emitted instead). Extracted manifest: $extracted"
  }
}

# 4. Byte-compare the two EXEs to assert merge determinism.
Assert-Same 'merge-1.exe' 'merge-2.exe' 'manifest-merge EXE self-determinism'

Write-Host 'windows_manifest_merge.ps1: in-process libxml2 manifest merge passed (RED->GREEN via scrubbed PATH)'
