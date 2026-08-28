cmake_minimum_required(VERSION 3.24)
project(llvm_ld VERSION 0.1.0 LANGUAGES C CXX)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(MSVC)
  # LLD and the wrapper must share one statically linked CRT so MI_MALLOC_OVERRIDE can redirect
  # every allocation inside this DLL. No allocated object crosses the C ABI.
  set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

option(LLVM_LD_ENABLE_PPROF "Compile mimalloc sampled pprof hooks" OFF)
option(LLVM_LD_ENABLE_DHAT "Compile mimalloc exact DHAT support" OFF)
option(LLVM_LD_USE_MIMALLOC "Route native allocation through mimalloc" ON)
option(LLVM_LD_BUILD_SYSTEM_BASELINE "Build an allocator-only system baseline for benchmarks" OFF)

include(cmake/LockedInputs.cmake)
include(cmake/Import.cmake)
llvm_ld_require_inputs()

set(LLVM_ENABLE_PROJECTS "lld" CACHE STRING "" FORCE)
set(LLVM_TARGETS_TO_BUILD "X86" CACHE STRING "" FORCE)
set(LLVM_INCLUDE_TESTS OFF CACHE BOOL "" FORCE)
set(LLVM_INCLUDE_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLVM_INCLUDE_BENCHMARKS OFF CACHE BOOL "" FORCE)
set(LLVM_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLD_BUILD_TOOLS ON CACHE BOOL "" FORCE)
set(LLVM_ENABLE_ZLIB OFF CACHE BOOL "" FORCE)
set(LLVM_ENABLE_ZSTD OFF CACHE BOOL "" FORCE)
# Vendored static libxml2 so LLVM's WindowsManifest library can merge Windows manifests
# in-process instead of shelling out to mt.exe. See cmake/VendoredLibXml2.cmake for the target
# (LibXml2::LibXml2) and cmake/find-shims/FindLibXml2.cmake for why a shim module is needed at all.
# Prepend (not append) so our shim shadows CMake's builtin FindLibXml2.cmake.
list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake/find-shims")
include(cmake/VendoredLibXml2.cmake)
# FORCE_ON (not plain ON): llvm/cmake/config-ix.cmake treats FORCE_ON as "find_package(LibXml2
# REQUIRED) or fail the configure", whereas plain ON silently falls back to LLVM_ENABLE_LIBXML2=OFF
# (and therefore mt.exe) if find_package/the symbol check fails. We need a hard failure here: a
# silently broken shim must not quietly regress lld back to shelling out to mt.exe.
set(LLVM_ENABLE_LIBXML2 FORCE_ON CACHE STRING "" FORCE)
set(LLVM_USE_STATIC_LIBXML2 OFF CACHE BOOL "" FORCE)
# llvm/cmake/config-ix.cmake's check_symbol_exists(xmlReadMemory ... HAVE_LIBXML2) try-links
# against LibXml2::LibXml2 at configure time, but llvm_ld_xml2 hasn't been built yet (it's an
# in-tree target, not a preexisting archive on disk) -- the try-link would fail even though the
# real build will succeed once llvm_ld_xml2 is built. CheckSymbolExists.cmake guards its whole body
# with `if(NOT DEFINED "${VARIABLE}" ...)`, so pre-seeding the cache variable short-circuits the
# probe entirely. This is sound because the library really is built in-tree from our own vendored
# sources and the real build (not this probe) is what proves usability.
set(HAVE_LIBXML2 1 CACHE INTERNAL "libxml2 is vendored and built in-tree; see cmake/VendoredLibXml2.cmake")
set(LLVM_ENABLE_TERMINFO OFF CACHE BOOL "" FORCE)
add_subdirectory("${LLVM_LD_LLVM_ROOT}/llvm" llvm EXCLUDE_FROM_ALL)
# Imported targets carry no build ordering (see cmake/VendoredLibXml2.cmake), so LLVMWindowsManifest
# must be told explicitly to wait on our vendored libxml2 archive. Must come after the
# add_subdirectory() above: LLVMWindowsManifest does not exist until llvm/lib/WindowsManifest's own
# CMakeLists.txt has run.
add_dependencies(LLVMWindowsManifest llvm_ld_xml2)

add_library(llvm_ld SHARED src/llvm_ld.cpp)
if(LLVM_LD_USE_MIMALLOC)
  target_sources(llvm_ld PRIVATE "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c")
endif()
target_compile_features(llvm_ld PRIVATE cxx_std_17)
if(NOT MSVC)
  target_compile_options(llvm_ld PRIVATE -fno-rtti)
endif()
target_include_directories(llvm_ld PUBLIC "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>")
target_include_directories(llvm_ld PRIVATE
  "${LLVM_LD_LLVM_ROOT}/lld/include"
  "${LLVM_LD_LLVM_ROOT}/llvm/include"
  "${CMAKE_CURRENT_BINARY_DIR}/llvm/include"
  "${LLVM_LD_MIMALLOC_ROOT}")
target_link_libraries(llvm_ld PRIVATE lldCOFF lldMinGW lldCommon LLVMSupport)
target_compile_definitions(llvm_ld PRIVATE LLVM_LD_BUILDING_LIBRARY=1)
if(UNIX AND NOT APPLE)
  target_link_options(llvm_ld PRIVATE "LINKER:--version-script=${CMAKE_CURRENT_SOURCE_DIR}/exports.map")
endif()
if(LLVM_LD_USE_MIMALLOC)
  target_compile_definitions(llvm_ld PRIVATE MI_STATIC_LIB=1 LLVM_LD_USE_MIMALLOC=1)
endif()
if(WIN32 AND LLVM_LD_USE_MIMALLOC)
  target_compile_definitions(llvm_ld PRIVATE MI_MALLOC_OVERRIDE=1)
  target_link_libraries(llvm_ld PRIVATE psapi shell32 user32 advapi32 bcrypt)
  if(LLVM_LD_ENABLE_PPROF)
    target_compile_definitions(llvm_ld PRIVATE MI_PPROF=1)
  endif()
endif()

install(TARGETS llvm_ld)
install(FILES include/llvm_ld.h DESTINATION include)

enable_testing()
add_executable(abi_smoke tests/abi_smoke.c)
target_link_libraries(abi_smoke PRIVATE llvm_ld)
add_test(NAME abi_smoke COMMAND abi_smoke)
add_executable(abi_contract tests/abi_contract.cpp)
target_link_libraries(abi_contract PRIVATE llvm_ld)
add_test(NAME abi_contract COMMAND abi_contract)
add_executable(abi_state_test tests/abi_state_test.cpp src/llvm_ld.cpp)
target_include_directories(abi_state_test PRIVATE include "${LLVM_LD_LLVM_ROOT}/lld/include" "${LLVM_LD_LLVM_ROOT}/llvm/include" "${CMAKE_CURRENT_BINARY_DIR}/llvm/include")
target_compile_definitions(abi_state_test PRIVATE LLVM_LD_BUILDING_LIBRARY=1)
if(NOT MSVC)
  target_compile_options(abi_state_test PRIVATE -fno-rtti)
endif()
target_link_libraries(abi_state_test PRIVATE LLVMSupport)
add_test(NAME abi_state_test COMMAND abi_state_test)
add_executable(llvm-ld-runner tools/runner.c)
target_link_libraries(llvm-ld-runner PRIVATE llvm_ld)
add_executable(llvm-ld-direct tools/direct_runner.cpp "${LLVM_LD_MIMALLOC_ROOT}/mimalloc-pprof-amalgamated.c")
target_compile_features(llvm-ld-direct PRIVATE cxx_std_17)
target_include_directories(llvm-ld-direct PRIVATE "${LLVM_LD_LLVM_ROOT}/lld/include" "${LLVM_LD_LLVM_ROOT}/llvm/include" "${CMAKE_CURRENT_BINARY_DIR}/llvm/include" "${LLVM_LD_MIMALLOC_ROOT}")
target_compile_definitions(llvm-ld-direct PRIVATE MI_STATIC_LIB=1)
target_link_libraries(llvm-ld-direct PRIVATE lldCOFF lldMinGW lldCommon LLVMSupport)
if(WIN32)
  target_compile_definitions(llvm-ld-direct PRIVATE MI_MALLOC_OVERRIDE=1)
  target_link_libraries(llvm-ld-direct PRIVATE psapi shell32 user32 advapi32 bcrypt)
endif()
add_executable(allocator-probe tests/allocator_probe.c)
target_link_libraries(allocator-probe PRIVATE llvm_ld)
if(LLVM_LD_BUILD_SYSTEM_BASELINE)
  add_library(llvm_ld_system SHARED src/llvm_ld.cpp)
  target_compile_features(llvm_ld_system PRIVATE cxx_std_17)
  if(NOT MSVC)
    target_compile_options(llvm_ld_system PRIVATE -fno-rtti)
  endif()
  target_include_directories(llvm_ld_system PUBLIC "$<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>")
  target_include_directories(llvm_ld_system PRIVATE "${LLVM_LD_LLVM_ROOT}/lld/include" "${LLVM_LD_LLVM_ROOT}/llvm/include" "${CMAKE_CURRENT_BINARY_DIR}/llvm/include")
  target_link_libraries(llvm_ld_system PRIVATE lldCOFF lldMinGW lldCommon LLVMSupport)
  target_compile_definitions(llvm_ld_system PRIVATE LLVM_LD_BUILDING_LIBRARY=1)
  if(UNIX AND NOT APPLE)
    target_link_options(llvm_ld_system PRIVATE "LINKER:--version-script=${CMAKE_CURRENT_SOURCE_DIR}/exports.map")
  endif()
  add_executable(llvm-ld-runner-system tools/runner.c)
  target_link_libraries(llvm-ld-runner-system PRIVATE llvm_ld_system)
  add_executable(allocator-probe-system tests/allocator_probe.c)
  target_link_libraries(allocator-probe-system PRIVATE llvm_ld_system)
endif()
add_test(NAME locked_inputs COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tools/verify.py" --root "${CMAKE_CURRENT_SOURCE_DIR}")
add_test(NAME payload_reproducer COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_materialize_payload.py")
