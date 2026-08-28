# Toolchain file for cross-compiling llvm_ld on Linux for x86_64-pc-windows-msvc using
# clang-cl + lld-link + llvm-lib + llvm-rc, with the MSVC CRT and Windows SDK supplied by xwin
# (https://github.com/Jake-Shadle/xwin). This is the Firefox/Chromium production cross pattern;
# LLVM's own reference is llvm/cmake/platforms/WinMsvc.cmake, which is NOT part of this repo's
# pinned LLVM payload (see PROVENANCE.md) -- this file is our own, written against that pattern.
#
# Usage:
#   cmake -S . -B build-cross -G Ninja \
#     -DCMAKE_TOOLCHAIN_FILE=cmake/WinMsvcCross.cmake \
#     -DLLVM_NATIVE_TOOL_DIR=<stage1-native-build>/bin \
#     -DLLVM_DISABLE_ASSEMBLY_FILES=ON
#
# Required environment variable: LLVM_LD_XWIN_ROOT
#   Absolute path to an xwin splat directory (xwin's "splat" output layout), produced by:
#
#     xwin --accept-license \
#       --manifest <PINNED_VS_CHANNEL_MANIFEST_URL_OR_PATH> \
#       --manifest-version 16 \
#       --sdk-version <PINNED_SDK_VERSION> \
#       --crt-version <PINNED_CRT_VERSION> \
#       splat --include-debug-libs --output "$LLVM_LD_XWIN_ROOT"
#
#   --include-debug-libs is required, not optional: a default splat drops every crt/ucrt .lib whose
#   stem ends in `d` (libcmtd, msvcrtd, ucrtd, vcruntimed, msvcprtd, ...). Without it, any Debug
#   configuration -- including CMake's own compiler-check/ABI-detection try-compiles, which this
#   file pins to Release below precisely because they would otherwise default to Debug -- fails with
#   `lld-link: error: could not open 'msvcrtd.lib'`. See PROVENANCE.md for the exact pinned manifest
#   SHA-256 and versions this repository records.
#
#   The resulting splat contains proprietary Microsoft binaries (MSVC CRT + Windows SDK) that are
#   NOT redistributable. It must NEVER be committed to this repository or published as a build
#   artifact. Provisioning it on a Linux build machine (CI or local) to compile Windows binaries is
#   permitted by the Visual Studio Build Tools EULA's OSS/CI carve-out; only the splat's binary
#   contents must not leave that machine except embedded in the resulting Windows binaries.
#
# Expected splat subdirectories (xwin's default splat layout):
#   $LLVM_LD_XWIN_ROOT/crt/include
#   $LLVM_LD_XWIN_ROOT/crt/lib/x86_64
#   $LLVM_LD_XWIN_ROOT/sdk/include/ucrt
#   $LLVM_LD_XWIN_ROOT/sdk/include/um
#   $LLVM_LD_XWIN_ROOT/sdk/include/shared
#   $LLVM_LD_XWIN_ROOT/sdk/lib/ucrt/x86_64
#   $LLVM_LD_XWIN_ROOT/sdk/lib/um/x86_64

set(CMAKE_SYSTEM_NAME "Windows")
set(CMAKE_SYSTEM_VERSION 10.0)
set(CMAKE_SYSTEM_PROCESSOR "AMD64")

set(LLVM_LD_WINDOWS_TRIPLE "x86_64-pc-windows-msvc" CACHE STRING "Cross-compilation target triple")

if(NOT DEFINED ENV{LLVM_LD_XWIN_ROOT} OR "$ENV{LLVM_LD_XWIN_ROOT}" STREQUAL "")
  message(FATAL_ERROR
    "LLVM_LD_XWIN_ROOT is not set. It must point at an xwin splat directory containing the MSVC "
    "CRT and Windows SDK (see the header comment in cmake/WinMsvcCross.cmake for the exact `xwin "
    "... splat` command and the required subdirectory layout). This variable is read from the "
    "process environment, not a CMake cache variable, because the toolchain file must see it "
    "before the first try-compile.")
endif()

set(LLVM_LD_XWIN_ROOT "$ENV{LLVM_LD_XWIN_ROOT}" CACHE PATH "xwin splat root (MSVC CRT + Windows SDK)")

set(_llvm_ld_xwin_required_dirs
  "${LLVM_LD_XWIN_ROOT}/crt/include"
  "${LLVM_LD_XWIN_ROOT}/crt/lib/x86_64"
  "${LLVM_LD_XWIN_ROOT}/sdk/include/ucrt"
  "${LLVM_LD_XWIN_ROOT}/sdk/include/um"
  "${LLVM_LD_XWIN_ROOT}/sdk/include/shared"
  "${LLVM_LD_XWIN_ROOT}/sdk/lib/ucrt/x86_64"
  "${LLVM_LD_XWIN_ROOT}/sdk/lib/um/x86_64")
foreach(_llvm_ld_dir ${_llvm_ld_xwin_required_dirs})
  if(NOT IS_DIRECTORY "${_llvm_ld_dir}")
    message(FATAL_ERROR
      "xwin splat at LLVM_LD_XWIN_ROOT='${LLVM_LD_XWIN_ROOT}' is missing expected directory "
      "'${_llvm_ld_dir}'. Re-provision the splat with the `xwin ... splat --include-debug-libs` "
      "command documented at the top of cmake/WinMsvcCross.cmake.")
  endif()
endforeach()

set(CMAKE_C_COMPILER clang-cl)
set(CMAKE_CXX_COMPILER clang-cl)
set(CMAKE_C_COMPILER_TARGET "${LLVM_LD_WINDOWS_TRIPLE}")
set(CMAKE_CXX_COMPILER_TARGET "${LLVM_LD_WINDOWS_TRIPLE}")
set(CMAKE_LINKER lld-link)
set(CMAKE_AR llvm-lib)
set(CMAKE_RC_COMPILER llvm-rc)
set(CMAKE_RC_COMPILER_TARGET "${LLVM_LD_WINDOWS_TRIPLE}")

# -imsvc suppresses the "treat as system header" warnings clang-cl would otherwise emit for these
# CRT/SDK trees, matching how WinMsvc.cmake-style toolchain files feed xwin/Windows Kits headers.
set(_llvm_ld_imsvc_flags
  "-imsvc \"${LLVM_LD_XWIN_ROOT}/crt/include\""
  "-imsvc \"${LLVM_LD_XWIN_ROOT}/sdk/include/ucrt\""
  "-imsvc \"${LLVM_LD_XWIN_ROOT}/sdk/include/um\""
  "-imsvc \"${LLVM_LD_XWIN_ROOT}/sdk/include/shared\"")
string(JOIN " " _llvm_ld_imsvc_flags_joined ${_llvm_ld_imsvc_flags})
set(CMAKE_C_FLAGS_INIT "${_llvm_ld_imsvc_flags_joined}")
set(CMAKE_CXX_FLAGS_INIT "${_llvm_ld_imsvc_flags_joined}")

# /manifest:no: CMake 3.31+ prefers llvm-mt for clang-cl targets, and this repo builds with
# LLVM_ENABLE_LIBXML2=OFF, so an implicit mt invocation would fail. -libpath forwards the xwin CRT
# and SDK import libraries to lld-link.
set(_llvm_ld_link_flags
  "/manifest:no"
  "-libpath:\"${LLVM_LD_XWIN_ROOT}/crt/lib/x86_64\""
  "-libpath:\"${LLVM_LD_XWIN_ROOT}/sdk/lib/ucrt/x86_64\""
  "-libpath:\"${LLVM_LD_XWIN_ROOT}/sdk/lib/um/x86_64\"")
string(JOIN " " _llvm_ld_link_flags_joined ${_llvm_ld_link_flags})
set(CMAKE_EXE_LINKER_FLAGS_INIT "${_llvm_ld_link_flags_joined}")
set(CMAKE_SHARED_LINKER_FLAGS_INIT "${_llvm_ld_link_flags_joined}")
set(CMAKE_MODULE_LINKER_FLAGS_INIT "${_llvm_ld_link_flags_joined}")

# Static CRT selection. This *must* live in the toolchain file, not only in the root
# CMakeLists.txt, because the root CMakeLists.txt does not execute inside CMake's own
# compiler-check and ABI-detection test projects (CMakeTestCCompiler.cmake /
# CMakeDetermineCompilerABI.cmake), which run during project()/enable_language() -- i.e. before
# any of the root project's own `set()` calls have had any effect on them. The toolchain file, by
# contrast, is re-read inside every try_compile sub-configure, and CMake documents
# CMAKE_MSVC_RUNTIME_LIBRARY as being "propagated by calls to the try_compile() command into the
# test project". Without this, those test projects fall back to the CMP0091=NEW default
# `MultiThreaded$<$<CONFIG:Debug>:Debug>DLL` and lld-link fails with
# `could not open 'msvcrtd.lib'`.
#
# The value is character-for-character the same expression the root CMakeLists.txt sets under
# `if(MSVC)`, so the root project's enforcement cannot be weakened: whichever of the two runs last
# stores the identical string. This toolchain file only *extends* that enforcement backwards in
# time to cover the try-compiles; the root `if(MSVC)` block is still the sole authority for the
# native Windows build, where no toolchain file is loaded at all. The static CRT is a hard
# requirement, not a preference: mimalloc's Windows malloc override compiles out when `_DLL` is
# defined, which is exactly what /MD(d) defines.
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

# CMake's ABI-detection try-compile defaults to the Debug configuration when
# CMAKE_TRY_COMPILE_CONFIGURATION is unset (CMakeDetermineCompilerABI.cmake: `else() set(_tc_config
# "DEBUG")`). A default `xwin splat` ships no debug CRT at all -- it drops every crt/ucrt .lib whose
# stem ends in `d` unless `--include-debug-libs` is passed -- so pin the try-compiles to Release.
# This only affects try_compile(); the real build's configuration still comes from
# CMAKE_BUILD_TYPE on the configure command line.
set(CMAKE_TRY_COMPILE_CONFIGURATION "Release")

# Forward the splat root and triple into try-compile's ABI-detection sub-configure, otherwise it
# cannot see the CRT/SDK libs and every feature check fails.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
  LLVM_LD_XWIN_ROOT
  LLVM_LD_WINDOWS_TRIPLE)

# Deliberately NOT set: CMAKE_RC_FLAGS_INIT. llvm-rc gets no xwin include paths, so any target with
# a .rc input would fail to find <windows.h>. This is currently unreachable -- no target this repo
# builds has a .rc source -- and it is left alone on purpose: the -imsvc spelling used above is a
# clang driver flag that llvm-rc does not accept, llvm-rc's own include syntax could not be
# validated from a Windows host with no llvm-rc, and LLVM's reference cross toolchain
# (llvm/cmake/platforms/WinMsvc.cmake, llvmorg-23.1.0) sets no CMAKE_RC_FLAGS either, so there is no
# vetted pattern to copy. Add `/I` paths here, verified against a real llvm-rc, if a .rc is ever
# introduced.

set(CMAKE_FIND_ROOT_PATH "${LLVM_LD_XWIN_ROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
