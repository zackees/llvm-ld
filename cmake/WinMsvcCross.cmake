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
#       splat --output "$LLVM_LD_XWIN_ROOT"
#
#   Add --include-debug-libs if a Debug configuration is ever built (the default splat omits
#   libcmtd/ucrtd). See PROVENANCE.md for the exact pinned manifest SHA-256 and versions this
#   repository records.
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
      "'${_llvm_ld_dir}'. Re-provision the splat with the `xwin ... splat` command documented at "
      "the top of cmake/WinMsvcCross.cmake (add --include-debug-libs if this is a Debug "
      "configuration).")
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

# Root CMakeLists.txt already sets CMAKE_MSVC_RUNTIME_LIBRARY to the static CRT under
# `if(MSVC)`; CMake sets MSVC=1 for clang-cl (via CMAKE_CXX_SIMULATE_ID=MSVC), so that logic
# fires for free here. Do not duplicate or override it in this toolchain file.

# Forward the splat root and triple into try-compile's ABI-detection sub-configure, otherwise it
# cannot see the CRT/SDK libs and every feature check fails.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES
  LLVM_LD_XWIN_ROOT
  LLVM_LD_WINDOWS_TRIPLE)

set(CMAKE_FIND_ROOT_PATH "${LLVM_LD_XWIN_ROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
